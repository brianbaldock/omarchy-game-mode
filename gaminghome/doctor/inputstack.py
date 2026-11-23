"""Controller audit: which gamepads apps can see, what they cannot report, and which
input daemons sit in between.

Read-only and passive: capabilities come from sysfs, daemons are found through
systemd, the system D-Bus name list and /proc. No device node is opened, nothing
is grabbed and nothing is installed; the advice for a handheld is printed text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .context import Context
from .devices import Handheld, dmi, known_handheld
from .evdev import InputDevice, gamepads, hidraw_nodes
from .model import Outcome, info, ok, skip, warn

INPUTPLUMBER_BUS_NAME = "org.shadowblip.InputPlumber"
WATCHED_PROCESSES = frozenset({"inputplumber", "hhd", "antimicrox", "steam"})
INSTALL_INPUTPLUMBER = ("`omarchy pkg add inputplumber` then `sudo systemctl enable --now inputplumber.service "
                        "inputplumber-suspend.service`")
ENABLE_SUSPEND_HOOK = "sudo systemctl enable inputplumber-suspend.service"
ASUS_VENDOR = 0x0B05
UDEV_RUNTIME_RULES = "/run/udev/rules.d"
STOP_DAEMON = {"InputPlumber": "sudo systemctl stop inputplumber.service",
               "Handheld Daemon": "sudo systemctl stop hhd.service"}


@dataclass(frozen=True)
class Stack:
    inputplumber: bool = False
    inputplumber_bus: bool = False
    suspend_hook: str = "unknown"
    hhd: bool = False
    antimicrox: bool = False
    steam: bool = False
    inputplumber_version: str = ""

    @property
    def daemons(self) -> list[str]:
        return [name for name, active in (("InputPlumber", self.inputplumber), ("Handheld Daemon", self.hhd)) if active]


def running_processes(ctx: Context) -> set[str]:
    found = set()
    for entry in ctx.fs.listdir("/proc"):
        if entry.isdigit():
            name = ctx.fs.line(f"/proc/{entry}/comm")
            if name in WATCHED_PROCESSES:
                found.add(name)
    return found


def _probe_stack(ctx: Context) -> Stack:
    processes = running_processes(ctx)
    states = ctx.command(["systemctl", "is-active", "inputplumber.service", "hhd.service"], 4).stdout.split()
    service = dict(zip(("inputplumber", "hhd"), (state == "active" for state in states)))
    inputplumber = service.get("inputplumber", False) or "inputplumber" in processes
    hook, version = "unknown", ""
    if inputplumber:
        enabled = ctx.command(["systemctl", "is-enabled", "inputplumber-suspend.service"], 4).stdout.split()
        hook = enabled[0] if enabled and enabled[0] in ("enabled", "disabled", "static", "masked") else "unknown"
        package = ctx.command(["pacman", "-Q", "inputplumber"], 5).stdout
        listed = re.search(r"(?m)^inputplumber (?:\d+:)?(\d[\w.]*)", package)
        version = listed.group(1) if listed else ""
    return Stack(
        inputplumber=inputplumber,
        inputplumber_bus=inputplumber and ctx.command(["busctl", "--system", "status", INPUTPLUMBER_BUS_NAME], 4).ok,
        suspend_hook=hook, hhd=service.get("hhd", False) or "hhd" in processes,
        antimicrox="antimicrox" in processes, steam="steam" in processes, inputplumber_version=version)


def input_stack(ctx: Context) -> Stack:
    return ctx.once("input-stack", lambda: _probe_stack(ctx))


def _describe(device: InputDevice) -> str:
    missing = device.missing_controls()
    state = f"does not list {', '.join(missing)}" if missing else "lists every standard control"
    if device.hid_ordered:
        state += "; its buttons are numbered by a generic driver, so a press can arrive under another button's code"
    hidden = {"hidden": "; hidden from apps by an input daemon", "no-permission": "; not readable by this user",
              "missing": "; has no device node"}.get(device.access, "")
    guide = "" if device.has_guide else "; no Guide button (BTN_MODE)"
    return f"{device.name} ({device.kind()}, {device.bus} {device.vendor_product}) {state}{guide}{hidden}"


def _rows(devices: Sequence[InputDevice]) -> list[dict[str, object]]:
    return [{"name": device.name, "node": device.node, "kind": device.kind(), "bus": device.bus,
             "id": device.vendor_product, "access": device.access, "missing": device.missing_controls(),
             "guide": device.has_guide, "hid_order": device.hid_ordered} for device in devices]


def check_gamepads(ctx: Context) -> Outcome:
    found = gamepads(ctx)
    evidence = dict(gamepads=_rows(found))
    if not found:
        return skip("No gamepad was found.", "Connect or wake a controller and run the doctor again.", **evidence)
    summary = "; ".join(_describe(device) for device in found) + "."
    usable = [device for device in found if device.access == "readable"]
    if not usable:
        return warn(f"No gamepad is readable by this user: {summary}",
                    "Game Mode reads controllers directly. Your user needs read access to /dev/input/event* "
                    "(usually membership of the `input` group).", **evidence)
    incomplete = [device for device in usable if device.missing_controls() or device.hid_ordered]
    if incomplete:
        return warn(f"Apps see a controller without the standard control codes: {summary}",
                    "Games and Steam act on the standard codes, so a control that is not listed, or arrives "
                    "under another code, does the wrong thing or nothing. `omarchy-game-mode doctor buttons` "
                    "shows what each press really sends. On a handheld, an input daemon such as InputPlumber "
                    "provides a controller with the standard codes.", **evidence)
    return ok(f"Every controller apps can see lists the standard controls: {summary}", **evidence)


def check_input_stack(ctx: Context) -> Outcome:
    stack = input_stack(ctx)
    hook = {"enabled": "suspend hook enabled", "static": "suspend hook enabled", "disabled": "suspend hook disabled",
            "masked": "suspend hook masked"}.get(stack.suspend_hook, "suspend hook state unknown")
    version = f" {stack.inputplumber_version}" if stack.inputplumber_version else ""
    bus = "answers on the system bus" if stack.inputplumber_bus else "not on the system bus"
    plumber = f"running{version} ({bus}, {hook})" if stack.inputplumber else "not running"
    parts = [f"InputPlumber: {plumber}", f"Handheld Daemon: {'running' if stack.hhd else 'not running'}",
             f"AntiMicroX: {'running' if stack.antimicrox else 'not running'}",
             f"Steam: {'running' if stack.steam else 'not running'}"]
    evidence = dict(inputplumber=stack.inputplumber, inputplumber_bus=stack.inputplumber_bus,
                    inputplumber_version=stack.inputplumber_version, suspend_hook=stack.suspend_hook, hhd=stack.hhd,
                    antimicrox=stack.antimicrox, steam=stack.steam)
    if stack.inputplumber and stack.hhd:
        return warn("; ".join(parts) + ". Two handheld daemons are competing for the same controller.",
                    "Keep one of InputPlumber and Handheld Daemon and stop the other.", **evidence)
    return info("; ".join(parts) + ".", **evidence)


def handheld_verdict(handheld: Handheld | None, found: Sequence[InputDevice], stack: Stack) -> Outcome:
    """What the controller setup means on a known handheld. Pure: all inputs are already probed."""
    evidence = dict(handheld=handheld.name if handheld else "", verified=bool(handheld and handheld.verified),
                    daemon=", ".join(stack.daemons))
    if handheld is None:
        return info("This device is not a handheld the doctor has specific advice for.", **evidence)
    tested = "" if handheld.verified else " InputPlumber lists this model; this project has not tested it."
    raw = [device for device in found if not device.virtual]
    virtual = [device for device in found if device.virtual and not device.missing_controls()]
    if not stack.daemons:
        missing = next((device.missing_controls() for device in raw if device.missing_controls()), [])
        gap = f" The built-in controller does not list {', '.join(missing)}." if missing else ""
        return warn(f"{handheld.name}: no handheld input daemon is running, so apps get the raw built-in "
                    f"controller.{gap} Back paddles and vendor buttons arrive as keyboard keys or not at all.",
                    f"Install InputPlumber, which ships a profile for this device: {INSTALL_INPUTPLUMBER}.{tested}",
                    **evidence)
    daemon = " and ".join(stack.daemons)
    if not virtual:
        return warn(f"{handheld.name}: {daemon} is running, but no complete virtual controller was found.",
                    "Check the daemon's status and log; it may not have taken over the built-in controller.",
                    **evidence)
    exposed = [device for device in raw if device.access == "readable"]
    if stack.inputplumber and exposed:
        return warn(f"{handheld.name}: InputPlumber provides a virtual controller ({virtual[0].name}), but the "
                    "raw built-in controller is still readable, so apps can see both and act twice.",
                    "Check that InputPlumber manages this device (its device profile hides the raw nodes).",
                    **evidence)
    if stack.inputplumber and stack.suspend_hook in ("disabled", "masked"):
        return warn(f"{handheld.name}: InputPlumber provides a complete virtual controller ({virtual[0].name}), "
                    "but its suspend hook is not enabled, so the controller can stop working after sleep.",
                    f"Enable it: `{ENABLE_SUSPEND_HOOK}`", **evidence)
    return ok(f"{handheld.name}: {daemon} provides a complete virtual controller ({virtual[0].name}) and the "
              f"raw built-in controller is hidden from apps.{tested}", **evidence)


def check_handheld(ctx: Context) -> Outcome:
    return handheld_verdict(known_handheld(dmi(ctx)), gamepads(ctx), input_stack(ctx))


def check_hidraw_access(ctx: Context) -> Outcome:
    """Raw HID interfaces of the ASUS built-in controller that this user cannot read, and who may hide them."""
    nodes = [node for node in hidraw_nodes(ctx) if node.vendor == ASUS_VENDOR]
    if not nodes:
        return skip("This device has no ASUS N-KEY controller interfaces.")
    stack = input_stack(ctx)
    hide_rules = [name for name in ctx.fs.listdir(UDEV_RUNTIME_RULES) if "-hide-" in name and name.endswith(".rules")]
    rows = [{"node": node.node, "interface": node.interface, "mode": "" if node.mode is None else f"{node.mode:03o}",
             "access": node.access} for node in nodes]
    evidence = dict(interfaces=rows, hide_rules=len(hide_rules), inputplumber=stack.inputplumber, hhd=stack.hhd)
    unreadable = [node for node in nodes if node.access != "readable"]
    if not unreadable:
        return ok(f"All {len(nodes)} raw HID interfaces of the built-in controller are readable by this user.",
                  **evidence)
    hidden = [node for node in unreadable if node.access == "hidden"]
    counts = ", ".join(filter(None, (
        f"{len(hidden)} hidden with mode 000" if hidden else "",
        f"{len(unreadable) - len(hidden)} readable by root only" if len(unreadable) > len(hidden) else "")))
    summary = (f"{len(unreadable)} of {len(nodes)} raw HID interfaces of the built-in controller are not readable "
               f"by this user ({counts})")
    if not (hidden or hide_rules or stack.daemons):
        return info(f"{summary}. That is the system default; nothing appears to be hiding them.", **evidence)
    clues = [f"{daemon} is running" for daemon in stack.daemons]
    if hide_rules:
        owner = "InputPlumber" if any("inputplumber" in name for name in hide_rules) else "an input daemon"
        rules = "1 temporary udev rule" if len(hide_rules) == 1 else f"{len(hide_rules)} temporary udev rules"
        clues.append(f"{rules} from {owner} hide device nodes")
    command = next((STOP_DAEMON[daemon] for daemon in stack.daemons), "")
    manual = (f" To test without it, stop it yourself (`{command}`) and start it again afterwards." if command else "")
    return warn(f"{summary}. Another input daemon may be hiding the controller interfaces"
                f"{': ' + '; '.join(clues) if clues else ''}.",
                "This is expected while a handheld daemon manages the controller, but tools that read the raw "
                f"interfaces cannot work at the same time.{manual} The doctor never does this for you.", **evidence)
