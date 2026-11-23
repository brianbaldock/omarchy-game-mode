"""Command line for `omarchy-game-mode doctor`.

Runs entirely in the invoking process: it never talks to a resident Game Mode
instance and never imports GTK. Exit codes: 0 no failed check (warnings and
inconclusive results included), 1 at least one failed check, 2 usage error.

Other doctor commands plug in through `COMMANDS` (name -> `main(argv, stdout, stderr)`),
reusing the model, runner, redactor and `render`; `buttons` is one, and a
failed-attempt `capture` command is the planned next one.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import sys
import uuid
from typing import IO, Any, Callable, NoReturn, Sequence

from . import console
from .checks import SCOPES, plan
from .context import KEY_LOG_VARIABLE, Context, Options
from .model import EXIT_USAGE, Report
from .net import INTERFACE_NAME
from .redact import Redactor
from .report import environment, render_json, render_markdown, render_text
from .runner import run_checks

OVERALL_DEADLINE_S = 45.0
# What an error message may still name: a long option or a single-letter short one.
_OPTION_NAME = re.compile(r"--[a-z][a-z-]{0,30}|-[A-Za-z]")
# argparse messages that end with the rejected value.
_VALUE_AT_END = re.compile(r"(invalid choice|ignored explicit argument|invalid [\w ]{1,30} value)\b.*", re.S)
EPILOG = """\
scopes:
  all         everything below (default)
  streaming   network path, UDP/STUN/TURN, Wi-Fi, firewall, clients, video decode
  input       controllers and input daemons
  system      environment facts only

other commands:
  omarchy-game-mode doctor buttons    guided, read-only physical button test
                                      (see doctor buttons --help)

The doctor is read-only: no root, no installs, no configuration changes, no sign-in.
It sends anonymous probes to Microsoft's relay and service names, two public STUN
servers, one NTP server, your own gateway and, with --console-ip, that one address.
Exit status: 0 no failed check, 1 at least one failed check, 2 usage error.
"""


class Parser(argparse.ArgumentParser):
    """argparse that writes to the streams the caller chose and never repeats a rejected value.

    argparse quotes what it rejects (`invalid choice: '...'`, `unrecognized arguments: ...`).
    A mistyped address or path would then end up in a terminal log or an issue report, so
    error messages keep option names and the parser's own words only.
    """

    def __init__(self, *args: Any, stdout: IO[str], stderr: IO[str], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._stdout, self._stderr = stdout, stderr

    def _print_message(self, message: str, file: IO[str] | None = None) -> None:
        if message:
            (self._stdout if file is sys.stdout else self._stderr).write(message)

    def error(self, message: str) -> NoReturn:
        self.print_usage(self._stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {system_redactor().text(without_values(message))}\n")


def without_values(message: str) -> str:
    """An argparse error message with everything the user typed taken out, option names apart."""
    head, found, rest = message.partition("unrecognized arguments: ")
    if found:
        names = [token.split("=", 1)[0] for token in rest.split()]
        named = list(dict.fromkeys(name for name in names if _OPTION_NAME.fullmatch(name)))
        return head + "unrecognized arguments" + (f": {' '.join(named)}" if named else "")
    head, found, matches = message.rpartition(" could match ")
    if found and "ambiguous option" in head:
        return f"ambiguous option: could match {matches}"
    return _VALUE_AT_END.sub(r"\1", message)


def _scope(text: str) -> str:
    if text not in SCOPES:
        raise argparse.ArgumentTypeError(f"not one of {', '.join(SCOPES)}")
    return text


def _console_ip(text: str) -> str:
    try:
        return console.private_ipv4(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a private IPv4 address") from None


def _interface(text: str) -> str:
    if not INTERFACE_NAME.match(text):
        raise argparse.ArgumentTypeError("not a network interface name")
    return text


def _timeout(text: str) -> float:
    try:
        seconds = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number") from None
    if not 1 <= seconds <= 60:
        raise argparse.ArgumentTypeError("must be between 1 and 60 seconds")
    return seconds


def build_parser(stdout: IO[str], stderr: IO[str]) -> argparse.ArgumentParser:
    parser = Parser(prog="omarchy-game-mode doctor", stdout=stdout, stderr=stderr, epilog=EPILOG,
                    formatter_class=argparse.RawDescriptionHelpFormatter,
                    description="Check what Xbox streaming and the controller need on this device, and say "
                                "what each result proves and does not prove.")
    parser.add_argument("scope", nargs="?", type=_scope, default="all", metavar="SCOPE",
                        help="what to check: streaming, input, system or all (default: all)")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable report")
    output.add_argument("--markdown", action="store_true", help="redacted report to paste into a GitHub issue")
    parser.add_argument("--console-ip", metavar="IPV4", type=_console_ip,
                        help="also probe this one private address (your console on the home network)")
    parser.add_argument("--interface", metavar="IFACE", type=_interface,
                        help="Wi-Fi interface to inspect (default: the one carrying the default route)")
    parser.add_argument("--timeout", metavar="SECONDS", type=_timeout, default=6.0,
                        help="time limit per check, 1-60 (default: 6)")
    parser.add_argument("--no-redact", action="store_true",
                        help="show IP addresses in full for local debugging; never for sharing")
    return parser


def _login_name() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def system_redactor(mask_addresses: bool = True) -> Redactor:
    """Redactor primed with this machine's home directory, user name and host name."""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    redactor = Redactor(home=os.path.expanduser("~"), user=_login_name(), host=host.split(".")[0],
                        mask_addresses=mask_addresses)
    redactor.add_secret(host)
    return redactor


def _timestamp(seconds: float) -> str:
    moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def diagnose(ctx: Context) -> Report:
    """Run the plan for `ctx.options` and return the raw (not yet redacted) report."""
    options = ctx.options
    started_wall, started = ctx.wall_time(), ctx.monotonic()
    results = run_checks(plan(options), ctx, timeout=options.timeout, deadline=OVERALL_DEADLINE_S)
    privacy = {"redacted": options.redact, "addresses_masked": options.redact, "root_used": False,
               "tokens_read": False, "accounts_read": False, "cookies_read": False, "browser_profiles_read": False,
               "lan_discovery": False, "console_address_probed": bool(options.console_ip)}
    return Report(scope=options.scope, checks=results, environment=environment(results), privacy=privacy,
                  run_id=uuid.uuid4().hex, started_at=_timestamp(started_wall), finished_at=_timestamp(ctx.wall_time()),
                  duration_ms=round((ctx.monotonic() - started) * 1000))


def render(report: Report, redactor: Redactor, style: str, *, text: Callable[[Report], str] | None = None,
           markdown: Callable[[Report], str] | None = None) -> str:
    """Redact the report, render it, then scrub the rendered output once more.

    `text` and `markdown` let another doctor command supply its own layout while
    keeping this exact redaction path.
    """
    redactor.trust(report.run_id)  # generated here: random, and not a serial number
    safe = report.transformed(redactor.text, redactor.data)
    if style == "json":
        # Scrubbing string by string keeps the document valid JSON.
        return json.dumps(redactor.data(json.loads(render_json(safe))), indent=2, ensure_ascii=False) + "\n"
    if style == "markdown":
        return redactor.text((markdown or render_markdown)(safe))
    return redactor.text((text or render_text)(safe))


def emit(stream: IO[str], text: str) -> None:
    """Write a report even to a terminal whose encoding lacks a character it uses."""
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "replace").decode(encoding))


def _diagnose_command(argv: Sequence[str], stdout: IO[str], stderr: IO[str],
                      context_factory: Callable[[Options], Context] | None = None) -> int:
    parser = build_parser(stdout, stderr)
    args = parser.parse_args(list(argv))
    if args.no_redact and args.markdown:
        parser.error("--markdown is meant for sharing and is always redacted; drop --no-redact")
    options = Options(scope=args.scope, timeout=args.timeout, console_ip=args.console_ip or "",
                      interface=args.interface or "", redact=not args.no_redact)
    ctx = context_factory(options) if context_factory else Context(options, redactor=system_redactor(options.redact))
    report = diagnose(ctx)
    emit(stdout, render(report, ctx.redactor, "json" if args.json else "markdown" if args.markdown else "text"))
    return report.exit_code()


def _buttons_command(argv: Sequence[str], stdout: IO[str], stderr: IO[str]) -> int:
    from . import buttonsio
    return buttonsio.main(argv, stdout, stderr)


COMMANDS: dict[str, Callable[[Sequence[str], IO[str], IO[str]], int]] = {"buttons": _buttons_command}


def main(argv: Sequence[str] | None = None, *, stdout: IO[str] | None = None, stderr: IO[str] | None = None,
         context_factory: Callable[[Options], Context] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    os.environ.pop(KEY_LOG_VARIABLE, None)  # nothing the doctor starts may write TLS secrets to disk
    try:
        if argv and argv[0] in COMMANDS:
            code = COMMANDS[argv[0]](argv[1:], stdout, stderr)
        else:
            code = _diagnose_command(argv, stdout, stderr, context_factory)
        stdout.flush()
        return code
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    except KeyboardInterrupt:
        stderr.write("\nInterrupted.\n")
        return 130
    except BrokenPipeError:
        # The reader went away (`doctor | head`). Point stdout at /dev/null so the
        # interpreter's own flush at exit does not report the same error again.
        if stdout is sys.stdout:
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except (OSError, ValueError):
                pass
        return 0
