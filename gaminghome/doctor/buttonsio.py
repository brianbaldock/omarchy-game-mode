"""`omarchy-game-mode doctor buttons`: the interactive, read-only half of the button test.

Device nodes are opened O_RDONLY|O_NONBLOCK and only read. There is no EVIOCGRAB,
no write and no uinput, so every other program (Steam, games, input daemons) keeps
receiving the same events, the Guide button included. Raw HID reads (`--raw`) are
the same: read-only, and only from the judged controller's own physical device; when
that has no readable raw interface, the test says why and goes on without one.
Everything that was opened is closed, and the terminal mode restored, on exit, on
Ctrl-C and when the test is ended by SIGTERM or a hang-up: those signals are turned
into an exception for the duration of the test, so the clean-up always runs.

Everything this command prints, prompts included, goes through the redactor, and a
controller's name, free text that can hold a serial number or an address, is only
printed when it is a known model name (see `evdev.KNOWN_NAMES`).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import select
import signal
import struct
import sys
import termios
import time
import uuid
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Sequence

from . import buttons, evdev, hid
from .buttons import AxisInfo, Control, ControlResult, Event, Observation
from .checks import INPUT, SYSTEM
from .cli import Parser
from .context import Context, Interrupted, Options, ending_signals_held, ending_signals_raise
from .evdev import InputDevice
from .inputstack import input_stack
from .model import EXIT_USAGE, Report
from .report import environment, environment_lines, markdown_escape, markdown_table, wrap_text
from .runner import run_checks

EVENT = struct.Struct("llHHi")
ABSINFO = struct.Struct("6i")
KEY_BYTES = 0x2FF // 8 + 1
EVENT_NODE = re.compile(r"/dev/input/event\d+")
SKIP_KEYS, QUIT_KEYS = b"\n\r", b"qQ\x04"
UNREADABLE = {"hidden": "an input daemon hides it (mode 000)",
              "no-permission": "this user may not read it (key devices are normally readable only by root and "
                               "the `input` group)",
              "missing": "it has no device node"}
RAW_UNTIED = "cannot tell which interface belongs to this controller"
NOT_A_TERMINAL = ("omarchy-game-mode doctor buttons is interactive: run it in a terminal. With --json or --markdown "
                  "the prompts go to stderr, so the report itself can be redirected.\n")


class NotOpened(Exception):
    """A node or the terminal could not be opened; the message is a short reason, never a path."""


def run_every(*steps: Callable[[], Any]) -> None:
    """Run every clean-up step even when one fails.

    A terminal or node that is already gone (an error from termios or the OS) is expected and
    ignored; anything else, such as a second Ctrl-C, is raised once every step has run.
    """
    pending: BaseException | None = None
    for step in steps:
        try:
            step()
        except (OSError, termios.error):
            pass
        except BaseException as exc:  # noqa: BLE001 - re-raised below, after the remaining steps
            pending = pending or exc
    if pending is not None:
        raise pending


class Scrubbed:
    """A text stream whose every write is scrubbed first, so no prompt bypasses the redactor."""

    def __init__(self, stream: IO[str], scrub: Callable[[str], str]):
        self._stream, self._scrub = stream, scrub

    def write(self, text: str) -> int:
        return self._stream.write(self._scrub(text))

    def flush(self) -> None:
        self._stream.flush()


def _reason(exc: BaseException) -> str:
    """Why a device could not be opened, from the error number alone: the message would name paths."""
    number = exc.errno if isinstance(exc, OSError) else next(iter(exc.args), None)
    return os.strerror(number).lower() if isinstance(number, int) and number > 0 else type(exc).__name__


def _ioc_read(number: int, size: int) -> int:
    """_IOR('E', number, size): a read-only evdev ioctl request."""
    return (2 << 30) | (size << 16) | (ord("E") << 8) | number


def _eviocgkey(length: int) -> int:
    return _ioc_read(0x18, length)


def _eviocgabs(code: int) -> int:
    return _ioc_read(0x40 + code, ABSINFO.size)


# ---- choosing what to read ----------------------------------------------------------

def validate_device_path(ctx: Context, text: str) -> str:
    """Node name for a `--device` argument that really is an evdev character device."""
    if not text.startswith("/dev/input/"):
        raise ValueError("--device must be an event node under /dev/input, for example /dev/input/event21")
    real = ctx.fs.resolved(text)
    if not EVENT_NODE.fullmatch(real):
        raise ValueError("--device must resolve to /dev/input/eventN")
    if not ctx.fs.is_char_device(real):
        raise ValueError(f"{real} is not a character device")
    return real.rsplit("/", 1)[1]


@dataclass
class Selection:
    candidates: list[InputDevice] = field(default_factory=list)
    watched: list[InputDevice] = field(default_factory=list)
    primary: InputDevice | None = None
    notes: list[str] = field(default_factory=list)


def _related_keyboards(devices: Sequence[InputDevice], pads: Sequence[InputDevice]) -> list[InputDevice]:
    """Keyboard-type nodes of the same physical controller, of an input daemon, or of the platform hotkeys."""
    identities = {pad.vendor_product for pad in pads if not pad.virtual}
    return [device for device in devices if device.is_keyboard
            and (device.vendor_product in identities and not device.virtual or device.daemon_keys)]


def select_devices(ctx: Context, device: str = "") -> Selection:
    """Pick the gamepad to judge and the nodes to read, and say plainly what cannot be read."""
    devices = evdev.input_devices(ctx)
    pads = [item for item in devices if item.is_gamepad]
    if device:
        node = validate_device_path(ctx, device)
        chosen = next((item for item in devices if item.node == node), None)
        if chosen is None:
            raise ValueError(f"/dev/input/{node} is not a known input device")
        if chosen.access != "readable":
            raise ValueError(f"/dev/input/{node} cannot be read: {UNREADABLE.get(chosen.access, chosen.access)}")
        return Selection([chosen], [chosen], chosen if chosen.is_gamepad else None)
    keyboards = _related_keyboards(devices, pads)
    selection = Selection(candidates=[*pads, *keyboards])
    readable = [pad for pad in pads if pad.access == "readable"]
    virtual = [pad for pad in readable if pad.virtual]
    physical = [pad for pad in readable if not pad.virtual]
    # With a handheld daemon running, its virtual pad is what games see; otherwise the physical one is.
    preferred = virtual + physical if input_stack(ctx).daemons else physical + virtual
    selection.primary = preferred[0] if preferred else None
    if selection.primary:
        selection.watched = [selection.primary, *(item for item in keyboards if item.access == "readable")]
    for item in selection.candidates:
        if item.access != "readable":
            role = "controller" if item.is_gamepad else "key device"
            selection.notes.append(f"{item.path} \"{item.name}\" ({item.kind()} {role}) cannot be read: "
                                   f"{UNREADABLE.get(item.access, item.access)}.")
    return selection


@dataclass
class RawNode:
    path: str
    fields: list[hid.Field]


def _with_buttons(ctx: Context, node: evdev.HidrawNode) -> RawNode | None:
    """The node with its parsed report descriptor, when that declares buttons."""
    descriptor = ctx.fs.read_bytes(f"/sys/class/hidraw/{node.node}/device/report_descriptor", 4096)
    fields = hid.parse_report_descriptor(descriptor or b"")
    return RawNode(node.path, fields) if any(item.is_buttons for item in fields) else None


def _own_raw_node(ctx: Context, pad: InputDevice) -> RawNode:
    """The raw HID report of `pad`'s own physical device, or a ValueError that says why there is none.

    A HID interface counts only when sysfs shows it under the same USB device (or, on another
    bus, the same HID device) as the pad's node; the pad's own interface comes first.
    Anything else would attribute another controller's bits to this one.
    """
    own = evdev.hid_parent(ctx, f"/sys/class/input/{pad.node}/device")
    device = evdev.physical_device(ctx, f"/sys/class/input/{pad.node}/device")
    nodes = [node for node in evdev.hidraw_nodes(ctx)
             if evdev.physical_device(ctx, f"/sys/class/hidraw/{node.node}/device") == device]
    readable = sorted((node for node in nodes if node.access == "readable"),
                      key=lambda node: evdev.hid_parent(ctx, f"/sys/class/hidraw/{node.node}/device") != own)
    for node in readable:
        found = _with_buttons(ctx, node)
        if found:
            return found
    if not nodes:
        raise ValueError(f"the device behind {pad.path} has no raw HID interface")
    if not readable:
        reasons = "; ".join(sorted({UNREADABLE.get(node.access, node.access) for node in nodes}))
        raise ValueError(f"no raw HID interface of the device behind {pad.path} is readable: {reasons}")
    raise ValueError(f"no readable raw HID interface of the device behind {pad.path} declares buttons")


def _tied_to_hardware(ctx: Context, pad: InputDevice) -> bool:
    return not pad.virtual and bool(evdev.physical_device(ctx, f"/sys/class/input/{pad.node}/device"))


def find_raw_node(ctx: Context, selection: Selection) -> tuple[RawNode | None, str]:
    """The raw HID report of the judged controller itself, or None and the reason there is none.

    Only an interface of the judged controller's own physical device counts. A virtual
    controller, or one sysfs does not tie to a device, is tested without a raw report rather
    than with one that may belong to another controller.
    """
    pad = selection.primary
    if pad is None or not _tied_to_hardware(ctx, pad):
        return None, RAW_UNTIED
    try:
        return _own_raw_node(ctx, pad), ""
    except ValueError as exc:
        return None, str(exc)


def raw_node_of(ctx: Context, chosen: InputDevice) -> RawNode:
    """The raw HID report of the device chosen with `--device`, from that same physical device only.

    Without one the test does not start: a ValueError says why.
    """
    if chosen.virtual:
        raise ValueError(f"--raw: {chosen.path} is a virtual device, so it has no raw HID report of its own")
    if not _tied_to_hardware(ctx, chosen):
        raise ValueError(f"--raw: {chosen.path} is not a USB, Bluetooth or I2C HID device, so it has no raw HID "
                         "report")
    try:
        return _own_raw_node(ctx, chosen)
    except ValueError as exc:
        raise ValueError(f"--raw: {exc}") from None


# ---- live event source ----------------------------------------------------------------

class EvdevReader:
    """One evdev node, opened read-only and non-blocking. Never grabbed, never written to."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)

    def keys_down(self) -> set[int]:
        state = bytearray(KEY_BYTES)
        try:
            fcntl.ioctl(self.fd, _eviocgkey(len(state)), state)
        except OSError:
            return set()
        return {index * 8 + bit for index, byte in enumerate(state) for bit in range(8) if byte >> bit & 1}

    def axes(self, codes: Sequence[int]) -> dict[int, AxisInfo]:
        found = {}
        for code in codes:
            buffer = bytearray(ABSINFO.size)
            try:
                fcntl.ioctl(self.fd, _eviocgabs(code), buffer)
            except OSError:
                continue
            value, minimum, maximum, _fuzz, _flat, _resolution = ABSINFO.unpack(buffer)
            found[code] = AxisInfo(minimum, maximum, value)
        return found

    def read(self) -> list[tuple[int, int, int]] | None:
        """Pending (type, code, value) events; None once the device is gone."""
        try:
            data = os.read(self.fd, EVENT.size * 64)
        except BlockingIOError:
            return []
        except OSError:
            return None
        return [EVENT.unpack_from(data, offset)[2:] for offset in range(0, len(data) - EVENT.size + 1, EVENT.size)]

    def close(self) -> None:
        os.close(self.fd)


class LiveSource:
    """Multiplexes the watched evdev nodes, the optional hidraw node and the keyboard of the terminal."""

    def __init__(self, devices: Sequence[InputDevice], raw_path: str = "", stdin_fd: int = 0):
        self.clock = time.monotonic
        self._readers: list[EvdevReader] = []
        self._gone: set[int] = set()
        self._raw_fd = -1
        self._stdin = stdin_fd
        self._saved = None
        self._axes: dict[int, dict[int, AxisInfo]] = {}
        try:  # whatever fails, nothing stays open and the terminal gets its echo back
            for device in devices:
                self._readers.append(EvdevReader(device.path))
            if raw_path:
                self._raw_fd = os.open(raw_path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            self._axes = {index: reader.axes(sorted(device.axes))
                          for index, (reader, device) in enumerate(zip(self._readers, devices))}
            self._saved = termios.tcgetattr(stdin_fd)
            quiet = termios.tcgetattr(stdin_fd)
            quiet[3] &= ~(termios.ICANON | termios.ECHO)  # single keys, not echoed; Ctrl-C still interrupts
            quiet[6][termios.VMIN], quiet[6][termios.VTIME] = 1, 0
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, quiet)
        except BaseException:
            self.close()
            raise

    def keys_down(self) -> dict[int, set[int]]:
        return {index: reader.keys_down() for index, reader in enumerate(self._readers)}

    def axes(self) -> dict[int, dict[int, AxisInfo]]:
        return self._axes

    def begin_prompt(self, control: Control) -> None:
        """Drop keystrokes typed (or produced by a button mapper) before this prompt."""
        try:
            termios.tcflush(self._stdin, termios.TCIFLUSH)
        except termios.error:
            pass

    def poll(self, timeout: float) -> list[Event]:
        watched = [reader.fd for index, reader in enumerate(self._readers) if index not in self._gone]
        watched += [self._stdin] + ([self._raw_fd] if self._raw_fd >= 0 else [])
        ready, _write, _error = select.select(watched, [], [], max(0.0, timeout))
        events: list[Event] = []
        for index, reader in enumerate(self._readers):
            if reader.fd not in ready or index in self._gone:
                continue
            pending = reader.read()
            if pending is None:  # unplugged, or taken over by a daemon: stop polling it
                self._gone.add(index)
                continue
            events += [Event("input", index, kind, code, value) for kind, code, value in pending]
        if self._raw_fd in ready:
            try:
                events.append(Event("raw", data=os.read(self._raw_fd, 4096)))
            except BlockingIOError:
                pass
            except OSError:
                os.close(self._raw_fd)
                self._raw_fd = -1
        if self._stdin in ready:
            try:
                typed = os.read(self._stdin, 64) or b"q"
            except OSError:  # the terminal hung up
                typed = b"q"
            for byte in typed:
                if byte in QUIT_KEYS:
                    events.append(Event("quit"))
                elif byte in SKIP_KEYS:
                    events.append(Event("skip"))
        return events

    def close(self) -> None:
        """Restore the terminal and close every node; a step that fails never skips the others."""
        saved, self._saved = self._saved, None
        readers, self._readers = self._readers, []
        raw, self._raw_fd = self._raw_fd, -1
        steps: list[Callable[[], Any]] = []
        if saved is not None:
            steps.append(lambda: termios.tcsetattr(self._stdin, termios.TCSADRAIN, saved))
        steps += [reader.close for reader in readers]
        if raw >= 0:
            steps.append(lambda: os.close(raw))
        run_every(*steps)


class Console(buttons.Listener):
    """Prints each prompt and, immediately, what arrived for it."""

    def __init__(self, stream: IO[str], devices: Sequence[InputDevice], timeout: float):
        self.stream, self.devices, self.timeout = stream, devices, timeout

    def _say(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def prompt(self, index: int, total: int, control: Control) -> None:
        action = "Hold" if control.hold else "Press"
        self._say(f"[{index + 1}/{total}] {action}: {control.prompt}   [Enter = skip]")

    def observed(self, control: Control, observation: Observation) -> None:
        device = self.devices[observation.device]
        self._say(f"       -> {observation.label()} on {device.path} \"{device.name}\"")

    def raw(self, control: Control, bit: hid.UndeclaredBit) -> None:
        self._say(f"       -> raw HID report 0x{bit.report_id:02x}: bit {bit.bit} changed ({bit.where})")

    def finished(self, result: ControlResult) -> None:
        if result.status == buttons.TIMEOUT:
            self._say(f"       (nothing arrived within {self.timeout:g} s)")
        elif result.status == buttons.SKIPPED:
            self._say("       (skipped)")


# ---- report ---------------------------------------------------------------------------

def build_report(ctx: Context, selection: Selection, results: Sequence[ControlResult], raw: dict[str, Any],
                 started: float) -> Report:
    facts = run_checks([*SYSTEM, *(check for check in INPUT if check.id == "input.stack")], ctx, workers=4)
    found = buttons.verdicts(results, selection.watched, selection.primary)
    mapping: dict[str, Any] = {"device": "", "line": "", "missing": []}
    if selection.primary is not None:
        # The GUID is built from the bus, vendor, product and version numbers: it names a model, not a unit.
        ctx.trust(buttons.sdl_guid(selection.primary))
        line, missing = buttons.sdl_mapping(results, selection.primary, selection.watched.index(selection.primary))
        mapping = {"device": selection.primary.node, "line": line, "missing": missing}
    sections = {"devices": buttons.device_rows(selection.candidates, selection.watched),
                "controls": buttons.control_rows(results, selection.watched), "sdl_mapping": mapping, "raw": raw}
    privacy = {"redacted": True, "root_used": False, "devices_grabbed": False, "events_injected": False,
               "serials_read": False, "raw_hid_read": raw.get("status") == "watched"}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ctx.wall_time()))
    return Report(scope="buttons", mode="buttons", checks=buttons.verdict_checks(found),
                  environment=environment(facts), privacy=privacy, sections=sections, run_id=uuid.uuid4().hex,
                  started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), finished_at=stamp,
                  duration_ms=round(max(0.0, ctx.wall_time() - started) * 1000))


def _mapping_lines(mapping: dict[str, Any]) -> tuple[str, str]:
    """(mapping line, sentence about what it leaves out); both empty when no press was seen on the pad."""
    if not mapping["line"]:
        return "", ""
    missing = (f"Not seen and left out: {', '.join(mapping['missing'])}." if mapping["missing"]
               else "Every mapped control was seen.")
    return mapping["line"], missing


def _raw_line(raw: dict[str, Any]) -> str:
    if raw.get("status") == "unavailable":
        return f"Raw HID report unavailable: {raw.get('reason') or 'none found'}."
    if raw.get("status") != "watched":
        return ""
    changes = raw.get("undeclared_changes", [])
    if not changes:
        return (f"Raw HID: report 0x{raw['report_id']:02x} declares {raw['declared_buttons']} buttons; no bit "
                "outside the declared fields changed.")
    seen = "; ".join(f"bit {item['bit']} during {item['control']} ({item['where']})" for item in changes)
    return f"Raw HID: report 0x{raw['report_id']:02x} declares {raw['declared_buttons']} buttons; changed: {seen}."


def render_text(report: Report) -> str:
    sections = report.sections
    rows = buttons.summary_table(sections["controls"])
    widths = [max(len(row[column]) for row in [("Control", "Expected", "Observed", "Result"), *rows])
              for column in range(4)]
    lines = ["", "Omarchy Game Mode button test"]
    for label, value in environment_lines(report.environment):
        lines += wrap_text(value, f"{label + ':':<13}", " " * 13)
    lines.append("")
    for row in [("Control", "Expected", "Observed", "Result"), *rows]:
        lines.append("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    if report.checks:
        lines += ["", "Findings"]
    for check in report.checks:
        lines += wrap_text(f"{check.title}: {check.detail}", f"  {check.status.marker} ")
        if check.remediation:
            lines += wrap_text(check.remediation, "         note: ", " " * 15)
    line, missing = _mapping_lines(sections["sdl_mapping"])
    if line:
        lines += ["", f"SDL mapping built only from the presses seen on {sections['sdl_mapping']['device']}:",
                  f"  {line}"] + wrap_text(missing, "  ", "  ")
    if _raw_line(sections["raw"]):
        lines += [""] + wrap_text(_raw_line(sections["raw"]), "", "")
    lines += [""] + wrap_text("Read-only test: no device was grabbed and no input was sent. For a report to paste "
                          "into an issue: omarchy-game-mode doctor buttons --markdown", "", "")
    return "\n".join(lines) + "\n"


def render_markdown(report: Report) -> str:
    sections = report.sections
    outcome = {buttons.PASS: "pass", buttons.RECORDED: "pass (recorded)", buttons.DIFFERS: "fail (differs)",
               buttons.NO_EVENT: "fail (no event)", buttons.NOT_TESTED: "not tested"}
    lines = ["### Omarchy Game Mode button test", ""]
    lines += markdown_table(("Environment", ""), environment_lines(report.environment))
    lines += ["", "**Devices**", ""]
    lines += markdown_table(("Node", "Name", "Kind", "ID", "Access", "Read"),
                    ((row["node"], row["name"], f"{row['kind']} {row['role']}", f"{row['bus']} {row['id']}",
                      row["access"], "yes" if row["watched"] else "no") for row in sections["devices"]))
    lines += ["", "**Controls**", ""]
    lines += markdown_table(("Control", "Expected", "Observed", "Result"),
                    ((name, expected, seen, outcome.get(result, result))
                     for name, expected, seen, result in buttons.summary_table(sections["controls"])))
    if report.checks:
        lines += ["", "**Findings**", ""]
        lines += [f"- **{markdown_escape(check.title, False)}** ({check.status.name}): "
                  f"{markdown_escape(' '.join(filter(None, (check.detail, check.remediation))), False)}"
                  for check in report.checks]
    line, missing = _mapping_lines(sections["sdl_mapping"])
    if line:
        lines += ["", "**SDL mapping from the presses that were seen**", "", "```", line, "```", "",
                  markdown_escape(missing, False)]
    if _raw_line(sections["raw"]):
        lines += ["", markdown_escape(_raw_line(sections["raw"]), False)]
    lines += ["", "_Read-only test: no device was grabbed and no input was sent. Serial numbers and device "
                  "addresses are never read. Generated by `omarchy-game-mode doctor buttons --markdown`._"]
    return "\n".join(lines) + "\n"


# ---- command -------------------------------------------------------------------------

def _timeout(text: str) -> float:
    try:
        seconds = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number") from None
    if not 2 <= seconds <= 120:
        raise argparse.ArgumentTypeError("must be between 2 and 120 seconds")
    return seconds


def build_parser(stdout: IO[str], stderr: IO[str]) -> argparse.ArgumentParser:
    parser = Parser(
        prog="omarchy-game-mode doctor buttons", stdout=stdout, stderr=stderr,
        description="Guided physical button test. Press each control when asked; the test records the codes "
                    "that really arrive and compares them with an Xbox-style pad on Linux. Read-only: nothing "
                    "is grabbed, written or remapped. Devices are read non-exclusively, so every press, the "
                    "Xbox (Guide) button included, still reaches Steam, games and other apps while the test "
                    "runs.")
    parser.add_argument("--device", metavar="/dev/input/eventN", default="",
                        help="read only this node (default: the controller games see, plus its key interfaces)")
    parser.add_argument("--timeout", metavar="SECONDS", type=_timeout, default=12.0,
                        help="time to wait for each control, 2-120 (default: 12)")
    parser.add_argument("--raw", action="store_true",
                        help="also watch the raw HID report of the controller under test, from an interface of "
                             "that same physical device only, for buttons the kernel does not expose; with "
                             "--device, the test does not start without one")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable report (prompts go to stderr)")
    output.add_argument("--markdown", action="store_true", help="report to paste into an issue (prompts go to stderr)")
    return parser


def _is_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _raw_info(node: RawNode | None, results: Sequence[ControlResult], requested: bool,
              reason: str = "") -> dict[str, Any]:
    if node is None:
        return {"status": "unavailable", "reason": reason} if requested else {"status": "not requested"}
    summary = hid.RawWatcher(node.fields).summary()
    changes = [{"control": result.control.name, "bit": bit.bit, "report_id": bit.report_id, "where": bit.where}
               for result in results for bit in result.raw_bits]
    return {"status": "watched", **summary, "undeclared_changes": changes}


def _run_session(selection: Selection, raw_node: RawNode | None, args: argparse.Namespace, prompts: IO[str],
                 stdin: Any, source_factory: Callable[[Selection, RawNode | None], Any] | None) -> list[ControlResult]:
    """Open the nodes, ask for every control and close everything again, whatever happens in between."""
    source = None
    try:
        # A signal that arrives while the terminal changes mode is handled once `source` can be closed.
        with ending_signals_held():
            try:
                source = (source_factory(selection, raw_node) if source_factory
                          else LiveSource(selection.watched, raw_node.path if raw_node else "", stdin.fileno()))
            except (OSError, termios.error) as exc:
                raise NotOpened(_reason(exc)) from None
        tracker = buttons.Tracker(source.keys_down(), source.axes())
        session = buttons.Session(source, tracker, source.clock, timeout=args.timeout,
                                  watcher=hid.RawWatcher(raw_node.fields) if raw_node else None,
                                  listener=Console(prompts, selection.watched, args.timeout))
        return session.run()
    finally:
        if source is not None:
            source.close()


def main(argv: Sequence[str], stdout: IO[str], stderr: IO[str], *, ctx: Context | None = None,
         stdin: Any = None, is_terminal: Callable[[Any], bool] = _is_terminal,
         source_factory: Callable[[Selection, RawNode | None], Any] | None = None) -> int:
    from .cli import emit, render, system_redactor
    parser = build_parser(stdout, stderr)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    prompts = stderr if args.json or args.markdown else stdout
    stdin = sys.stdin if stdin is None else stdin
    if not (is_terminal(stdin) and is_terminal(prompts)):
        stderr.write(NOT_A_TERMINAL)
        return EXIT_USAGE
    ctx = ctx or Context(Options(scope="input"), redactor=system_redactor())
    prompts, stderr = Scrubbed(prompts, ctx.redactor.text), Scrubbed(stderr, ctx.redactor.text)
    try:
        selection = select_devices(ctx, args.device)
        raw_node: RawNode | None = None
        raw_reason = ""
        if args.raw and args.device:
            raw_node = raw_node_of(ctx, selection.watched[0])
        elif args.raw:
            raw_node, raw_reason = find_raw_node(ctx, selection)
    except ValueError as exc:
        stderr.write(f"omarchy-game-mode doctor buttons: error: {exc}\n")
        return EXIT_USAGE
    if raw_reason:
        selection.notes.append(f"raw report unavailable: {raw_reason}; the test goes on without it.")
    prompts.write("Omarchy Game Mode button test: read-only, nothing is grabbed or remapped.\n")
    for device in selection.candidates:
        mark = "read " if device in selection.watched else "skip "
        role = "controller" if device.is_gamepad else "keys"
        prompts.write(f"  {mark} {device.path}  {device.name} ({device.kind()} {role}, {device.bus} "
                      f"{device.vendor_product})\n")
    for note in selection.notes:
        prompts.write(f"  note: {note}\n")
    if not selection.watched:
        prompts.write("No readable controller was found, so there is nothing to test.\n")
        return 1
    judged = (f"Judging {selection.primary.path} \"{selection.primary.name}\". " if selection.primary
              else "That device is not a gamepad, so its codes are recorded but not judged. ")
    prompts.write(f"{judged}Press each control when asked; Enter skips one, q stops the test.\n\n")
    started = ctx.wall_time()
    try:
        with ending_signals_raise():
            results = _run_session(selection, raw_node, args, prompts, stdin, source_factory)
    except Interrupted as stop:
        with contextlib.suppress(OSError, ValueError):
            stderr.write(f"\nStopped by {signal.Signals(stop.number).name}; the terminal was restored.\n")
        return 128 + stop.number
    except NotOpened as exc:
        stderr.write(f"omarchy-game-mode doctor buttons: could not open the controller for reading: {exc}\n")
        return 1
    report = build_report(ctx, selection, results, _raw_info(raw_node, results, args.raw, raw_reason), started)
    style = "json" if args.json else "markdown" if args.markdown else "text"
    emit(stdout, render(report, ctx.redactor, style, text=render_text, markdown=render_markdown))
    return 0
