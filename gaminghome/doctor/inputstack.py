"""Controller audit: which gamepads apps can see, what they cannot report, whether games
can make them vibrate, and which input daemons sit in between.

Read-only and passive: capabilities and HID report descriptors come from sysfs, daemons
are found through systemd, the system D-Bus name list and /proc. No device node is
opened, nothing is grabbed and nothing is installed; the advice for a handheld is
printed text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Sequence

from .context import Context
from .devices import Handheld, dmi, known_handheld
from .evdev import FF_RUMBLE, InputDevice, force_feedback, gamepads, hid_parent, hidraw_nodes, physical_device
from .hid import MAX_DESCRIPTOR_BYTES, Haptics, haptics_report
from .model import Outcome, info, ok, skip, warn

INPUTPLUMBER_BUS_NAME = "org.shadowblip.InputPlumber"
WATCHED_PROCESSES = frozenset({"inputplumber", "hhd", "antimicrox", "steam"})
INSTALL_INPUTPLUMBER = ("`omarchy pkg add inputplumber` then `sudo systemctl enable --now inputplumber.service "
                        "inputplumber-suspend.service`")
UDEV_ENTRY_ADVICE = ("Game Mode ships a udev entry that has the kernel report the standard codes; its root steps are "
                     "in the README under \"ASUS ROG Xbox Ally controller layout\". On a kernel without ASUS's Ally "
                     "HID driver, InputPlumber is not a mapping upgrade by itself.")
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
        handheld = known_handheld(dmi(ctx))
        remedy = (_udev_advice(handheld) if handheld and handheld.udev_entry else
                  "On a handheld, an input daemon such as InputPlumber provides a controller with the standard codes.")
        return warn(f"Apps see a controller without the standard control codes: {summary}",
                    "Games and Steam act on the standard codes, so a control that is not listed, or arrives "
                    "under another code, does the wrong thing or nothing. `omarchy-game-mode doctor buttons` "
                    f"shows what each press really sends. {remedy}", **evidence)
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


def _udev_advice(handheld: Handheld) -> str:
    return UDEV_ENTRY_ADVICE + ("" if handheld.verified else " This project has not tested the entry on this model.")


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
        if raw and not any(device.missing_controls() or device.hid_ordered for device in raw):
            return info(f"{handheld.name}: no handheld input daemon is running, and the built-in controller already "
                        "lists the standard controls. Back paddles and vendor buttons still arrive as keyboard keys "
                        "or not at all.", **evidence)
        missing = next((device.missing_controls() for device in raw if device.missing_controls()), [])
        gap = f" The built-in controller does not list {', '.join(missing)}." if missing else ""
        remedy = (_udev_advice(handheld) if handheld.udev_entry else
                  f"Install InputPlumber, which ships a profile for this device: {INSTALL_INPUTPLUMBER}.{tested}")
        return warn(f"{handheld.name}: no handheld input daemon is running, so apps get the raw built-in "
                    f"controller.{gap} Back paddles and vendor buttons arrive as keyboard keys or not at all.",
                    remedy, **evidence)
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


def _haptics(ctx: Context, pad: InputDevice) -> tuple[str, Haptics] | None:
    """A haptics output report declared by a raw HID interface of `pad`'s own physical device, and that interface.

    Report descriptors are read from sysfs, which every user may read: no device node is opened. The pad's
    own interface is looked at first; a virtual pad has no descriptor of its own.
    """
    sysfs = f"/sys/class/input/{pad.node}/device"
    device = "" if pad.virtual else physical_device(ctx, sysfs)
    if not device:
        return None
    own = hid_parent(ctx, sysfs)
    nodes = [node.node for node in hidraw_nodes(ctx)
             if physical_device(ctx, f"/sys/class/hidraw/{node.node}/device") == device]
    for node in sorted(nodes, key=lambda name: hid_parent(ctx, f"/sys/class/hidraw/{name}/device") != own):
        descriptor = ctx.fs.read_bytes(f"/sys/class/hidraw/{node}/device/report_descriptor", MAX_DESCRIPTOR_BYTES)
        found = haptics_report(descriptor or b"")
        if found:
            return node, found
    return None


def check_vibration(ctx: Context) -> Outcome:
    """Whether games can make a controller vibrate through evdev, and whether its HID descriptor says it can."""
    found = gamepads(ctx)
    if not found:
        return skip("No gamepad was found.", "Connect or wake a controller and run the doctor again.")
    rows, offered, unopened, haptic = [], [], [], None
    for pad in found:
        rumble = FF_RUMBLE in force_feedback(ctx, pad)
        declared = _haptics(ctx, pad)
        rows.append({"name": pad.name, "node": pad.node, "kind": pad.kind(), "access": pad.access, "rumble": rumble,
                     "haptics_report": f"0x{declared[1].report_id:02X}" if declared else "",
                     "xbox_layout": bool(declared and declared[1].xbox_layout),
                     "interface": declared[0] if declared else ""})
        if rumble:
            (offered if pad.access == "readable" else unopened).append(pad.name)
        elif declared and haptic is None:
            haptic = (pad, declared[1])
    evidence = dict(controllers=rows)
    if offered:
        return ok(f"Vibration is offered to games (FF_RUMBLE on {', '.join(dict.fromkeys(offered))}).", **evidence)
    if haptic:
        pad, report = haptic
        layout = "laid out like" if report.xbox_layout else "not laid out like"
        return replace(warn(
            f"The controller can vibrate (haptics output report 0x{report.report_id:02X} on {pad.name}) but the "
            "kernel offers no force feedback for it, so games cannot make it vibrate through evdev.",
            f"A kernel driver or a handheld daemon has to forward rumble to that report ({layout} the Xbox One "
            "controller's rumble report).", **evidence),
            proves="The controller's HID descriptor declares a haptics report; its event node offers no rumble.",
            does_not_prove="Whether Steam's own HID driver reaches it.")
    if unopened:
        return info(f"Only controllers this user cannot open offer force feedback (FF_RUMBLE on "
                    f"{', '.join(dict.fromkeys(unopened))}), so games cannot use it.", **evidence)
    return replace(info("No controller offers force feedback (FF_RUMBLE) to games, and no physical controller's HID "
                        "descriptor declares a haptics output report.", **evidence),
                   proves="No event node offers rumble, and no HID descriptor declares a haptics output report.",
                   does_not_prove="That the controller has no motors: a report the doctor does not know may drive "
                                  "them.")
