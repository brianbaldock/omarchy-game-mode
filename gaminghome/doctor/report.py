"""Rendering of a doctor report as terminal text, JSON and a pasteable Markdown issue report.

Renderers only ever see a report that has already been through the redactor; the
caller scrubs the rendered output once more before it is written. Markers are plain
`[PASS] [WARN] [FAIL] [SKIP] [INFO]`: no colour, no emoji, so a paste stays readable.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any, Iterable, Mapping

from .checks import CATEGORY_TITLES
from .model import CheckResult, Report, Status

TEXT_WIDTH = 100
INDENT = " " * 9
REDACTION_NOTE = ("Addresses are masked (<public-ip-N>, 192.168.x.x). Network names, MAC addresses, host and user "
                  "names and serial numbers are never included.")


def environment(checks: Iterable[CheckResult]) -> dict[str, Any]:
    """Report header assembled from allowlisted evidence, so it cannot hold more than the checks do."""
    found = {check.id: check.evidence for check in checks}

    def pick(check_id: str, *keys: str) -> dict[str, Any]:
        evidence = found.get(check_id, {})
        return {key: evidence[key] for key in keys if evidence.get(key) not in (None, "", [])}

    env: dict[str, Any] = pick("system.platform", "os", "os_version", "kernel", "architecture", "omarchy",
                               "hyprland", "session_type", "desktop", "game_mode")
    device = pick("system.device", "vendor", "product", "family", "board", "bios", "handheld")
    gpu = pick("system.gpu", "gpus", "mesa")
    network = {**pick("network.ipv4.default_route", "interface", "interface_type"),
               **pick("network.ipv6.consistency", "ipv6_state")}
    wireless = {**pick("wifi.link", "band", "frequency_mhz", "channel", "width_mhz", "dfs", "signal_dbm", "generation"),
                **pick("wifi.powersave", "power_save")}
    clients = [{"name": row["name"], "version": row["version"]}
               for row in found.get("client.browsers", {}).get("browsers", [])]
    if found.get("client.greenlight_version", {}).get("installed"):
        clients.append({"name": "Greenlight", "version": found["client.greenlight_version"].get("version", "")})
    controllers = {**pick("input.stack", "inputplumber", "hhd", "antimicrox", "steam"),
                   "gamepads": [f"{row['name']} ({row['kind']})"
                                for row in found.get("input.gamepads", {}).get("gamepads", [])]}
    for key, value in (("device", device), ("gpu", gpu), ("network", network), ("wireless", wireless),
                       ("clients", clients), ("controllers", controllers if "input.stack" in found else {})):
        if value:
            env[key] = value
    return env


def _join(parts: Iterable[str], separator: str = ", ") -> str:
    return separator.join(part for part in parts if part)


def environment_lines(env: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(label, value) rows shared by the text header and the Markdown table."""
    device, gpu = env.get("device", {}), env.get("gpu", {})
    network, wireless = env.get("network", {}), env.get("wireless", {})
    controllers = env.get("controllers", {})
    session = _join([f"Hyprland {env['hyprland']}" if env.get("hyprland") else env.get("desktop", ""),
                     f"({env['session_type']})" if env.get("session_type") else ""], " ")
    radio = _join([wireless.get("band", ""), f"channel {wireless['channel']}" if "channel" in wireless else "",
                   f"{wireless['width_mhz']} MHz" if "width_mhz" in wireless else "",
                   f"{wireless['signal_dbm']} dBm" if "signal_dbm" in wireless else "",
                   "DFS" if wireless.get("dfs") else "",
                   "power save on" if wireless.get("power_save") else ""])
    daemons = _join([name for name, key in (("InputPlumber", "inputplumber"), ("Handheld Daemon", "hhd"),
                                            ("AntiMicroX", "antimicrox")) if controllers.get(key)]) or "none"
    rows = [
        ("Device", _join([_join([device.get("vendor", ""), device.get("product") or device.get("family", "")], " "),
                          f"board {device['board']}" if device.get("board") else "",
                          f"BIOS {device['bios']}" if device.get("bios") else ""])),
        ("System", _join([_join([env.get("os", ""), env.get("os_version", "")], " "),
                          _join(["Linux", env.get("kernel", ""), env.get("architecture", "")], " ")
                          if env.get("kernel") else ""])),
        ("Session", session),
        ("Game Mode", env.get("game_mode", "")),
        ("GPU", _join([_join([f"{row['vendor']} {row['id']}" + (f" ({row['driver']})" if row.get("driver") else "")
                              for row in gpu.get("gpus", [])], "; "),
                       f"Mesa {gpu['mesa']}" if gpu.get("mesa") else ""])),
        ("Clients", _join(_join([row["name"], row["version"]], " ") for row in env.get("clients", []))),
        ("Network", _join([_join([network.get("interface_type", ""), radio], ": " if radio else ""),
                           f"IPv6 {network['ipv6_state']}" if network.get("ipv6_state") else ""], "; ")),
        ("Controllers", _join([f"input daemons: {daemons}", _join(controllers.get("gamepads", []), "; ")], "; ")
         if controllers else ""),
    ]
    return [(label, value) for label, value in rows if value]


def _grouped(report: Report) -> list[tuple[str, list[CheckResult]]]:
    groups: dict[str, list[CheckResult]] = {}
    for check in report.checks:
        groups.setdefault(check.category, []).append(check)
    order = [category for category in CATEGORY_TITLES if category in groups]
    order += [category for category in groups if category not in CATEGORY_TITLES]
    return [(CATEGORY_TITLES.get(category, category.title() or "Other"), groups[category]) for category in order]


def _wrap(text: str, first: str, rest: str = INDENT) -> list[str]:
    return textwrap.wrap(text, TEXT_WIDTH, initial_indent=first, subsequent_indent=rest,
                         break_long_words=False, break_on_hyphens=False) or [first.rstrip()]


def _counts(report: Report) -> str:
    counts = report.counts()
    return (f"{counts['pass']} pass, {counts['warn']} warn, {counts['fail']} fail, {counts['skip']} skip, "
            f"{counts['info']} info in {report.duration_ms / 1000:.1f} s")


def render_text(report: Report) -> str:
    lines = [f"Omarchy Game Mode doctor ({report.scope})"]
    for label, value in environment_lines(report.environment):
        lines += _wrap(value, f"{label + ':':<13}", " " * 13)
    for title, checks in _grouped(report):
        lines += ["", title]
        for check in checks:
            lines += _wrap(f"{check.title}: {check.detail}", f"  {check.status.marker} ")
            if check.remediation and check.status in (Status.WARN, Status.FAIL, Status.SKIP):
                lines += _wrap(check.remediation, f"{INDENT}fix: ", INDENT + "     ")
            if check.proves or check.does_not_prove:
                lines += _wrap(f"{check.proves} Not: {check.does_not_prove}", f"{INDENT}proves: ", INDENT + "        ")
    lines += [""] + _wrap(report.headline(), "Result: ", "        ")
    lines.append(_counts(report) + ".")
    if report.privacy.get("redacted", True):
        lines += _wrap(REDACTION_NOTE + " For a pasteable report: omarchy-game-mode doctor --markdown", "", "")
    else:
        lines += _wrap("Addresses are shown in full (--no-redact): do not post this output.", "", "")
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def markdown_escape(text: str, table: bool = True) -> str:
    """Make dynamic text safe for GitHub Markdown.

    Angle brackets would be read as HTML (and `<public-ip-1>` would vanish), except
    inside backtick code spans, where they are literal already. Pipes end a table
    cell, so they are escaped there and nowhere else (a command must stay copyable).
    """
    text = str(text).replace("\r", " ").replace("\n", " ")
    parts = (text.replace("|", "\\|") if table else text).split("`")
    for index in range(0, len(parts), 2):
        parts[index] = parts[index].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "`".join(parts)


def _table(header: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(markdown_escape(cell) for cell in row) + " |" for row in rows]
    return lines


def render_markdown(report: Report) -> str:
    """Issue-ready report: environment, results, what each check proves, and the open next steps."""
    lines = ["### Omarchy Game Mode doctor report", "", f"**Result:** {markdown_escape(report.headline(), False)}", ""]
    lines += _table(("Environment", ""), environment_lines(report.environment))
    lines += ["", f"**Results** (scope `{report.scope}`, {_counts(report)})", ""]
    lines += _table(("Status", "Check", "Result"),
                    ((check.status.name, check.title, check.detail) for check in report.checks))
    todo = [check for check in report.checks if (check.remediation or check.root_command)
            and check.status in (Status.WARN, Status.FAIL, Status.SKIP)]
    if todo:
        lines += ["", "**Next steps**", ""]
        for check in todo:
            command = f"run `{check.root_command}`. " if check.root_command else ""
            lines.append(f"- **{markdown_escape(check.title, False)}** ({check.status.name}): "
                         f"{markdown_escape(command + check.remediation, False)}")
    lines += ["", "<details><summary>What each check proves and does not prove</summary>", ""]
    lines += _table(("Check", "Proves", "Does not prove"),
                    ((f"{check.title} (`{check.id}`)", check.proves, check.does_not_prove) for check in report.checks))
    lines += ["", "</details>", "",
              f"_{markdown_escape(REDACTION_NOTE, False)} Generated by `omarchy-game-mode doctor --markdown`._"]
    return "\n".join(lines) + "\n"
