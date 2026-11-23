"""Guided physical button test: the script, event tracking, verdicts and report.

This is the pure half of `doctor buttons`: no device is opened here. A session is
driven by an event source and a clock, so the whole flow runs in tests from a
scripted list of events. Nothing about a controller is assumed: for every prompted
control the test records the evdev codes that actually arrived, then compares them
with what an Xbox-style pad reports on Linux and says where they differ.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .evdev import (ABS_BRAKE, ABS_GAS, ABS_HAT0X, ABS_HAT0Y, ABS_HAT2X, ABS_HAT2Y, ABS_RX, ABS_RY, ABS_RZ, ABS_X,
                    ABS_Y, ABS_Z, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT, BTN_DPAD_UP, BTN_EAST, BTN_MODE,
                    BTN_NORTH, BTN_SELECT, BTN_SOUTH, BTN_START, BTN_THUMBL, BTN_THUMBR, BTN_TL, BTN_TL2, BTN_TR,
                    BTN_TR2, BTN_WEST, EV_ABS, EV_KEY, InputDevice, abs_name, key_name)
from .hid import RawWatcher, UndeclaredBit
from .model import CheckResult, Status

KEY, HAT, AXIS = "key", "hat", "axis"
OBSERVED, SKIPPED, TIMEOUT, NOT_TESTED = "observed", "skipped", "timeout", "not tested"
PASS, DIFFERS, NO_EVENT, RECORDED = "pass", "differs", "no event", "recorded"
HAT_FIRST, HAT_LAST = 0x10, 0x17
BTN_JOYSTICK = 0x120
# An analog control counts as pressed past 35% of its range and released under 15%:
# stick drift and resting triggers stay well inside that.
PRESS_FRACTION, RELEASE_FRACTION = 0.35, 0.15
SETTLE_SECONDS = 0.25
HOLD_SECONDS = 2.5
# A mapper can turn a controller button into Enter; a key that follows an input this closely is not the user.
SKIP_GUARD_SECONDS = 0.4


@dataclass(frozen=True)
class Control:
    key: str
    name: str
    prompt: str
    sdl: str = ""
    expected: tuple[tuple[str, int, int], ...] = ()
    hold: bool = False
    base: str = ""


def _keys(*codes: int) -> tuple[tuple[str, int, int], ...]:
    return tuple((KEY, code, 0) for code in codes)


CONTROLS = (
    Control("guide", "Xbox", "Xbox (center) button", "guide", _keys(BTN_MODE)),
    Control("view", "View", "View button (two overlapping squares)", "back", _keys(BTN_SELECT)),
    Control("menu", "Menu", "Menu button (three lines)", "start", _keys(BTN_START)),
    Control("a", "A", "A", "a", _keys(BTN_SOUTH)),
    Control("b", "B", "B", "b", _keys(BTN_EAST)),
    # The kernel's BTN_X is BTN_NORTH and its BTN_Y is BTN_WEST; pads that follow geometry swap them.
    Control("x", "X", "X", "x", _keys(BTN_NORTH, BTN_WEST)),
    Control("y", "Y", "Y", "y", _keys(BTN_WEST, BTN_NORTH)),
    Control("lb", "LB", "LB (left bumper)", "leftshoulder", _keys(BTN_TL)),
    Control("rb", "RB", "RB (right bumper)", "rightshoulder", _keys(BTN_TR)),
    Control("lt", "LT", "LT (pull the left trigger all the way)", "lefttrigger",
            ((AXIS, ABS_Z, 1), (KEY, BTN_TL2, 0), (AXIS, ABS_BRAKE, 1), (AXIS, ABS_HAT2Y, 1))),
    Control("rt", "RT", "RT (pull the right trigger all the way)", "righttrigger",
            ((AXIS, ABS_RZ, 1), (KEY, BTN_TR2, 0), (AXIS, ABS_GAS, 1), (AXIS, ABS_HAT2X, 1))),
    Control("l3", "L3", "L3 (press the left stick in)", "leftstick", _keys(BTN_THUMBL)),
    Control("r3", "R3", "R3 (press the right stick in)", "rightstick", _keys(BTN_THUMBR)),
    Control("dpad_up", "D-pad up", "D-pad up", "dpup", ((HAT, ABS_HAT0Y, -1), (KEY, BTN_DPAD_UP, 0))),
    Control("dpad_down", "D-pad down", "D-pad down", "dpdown", ((HAT, ABS_HAT0Y, 1), (KEY, BTN_DPAD_DOWN, 0))),
    Control("dpad_left", "D-pad left", "D-pad left", "dpleft", ((HAT, ABS_HAT0X, -1), (KEY, BTN_DPAD_LEFT, 0))),
    Control("dpad_right", "D-pad right", "D-pad right", "dpright", ((HAT, ABS_HAT0X, 1), (KEY, BTN_DPAD_RIGHT, 0))),
    Control("ls_right", "Left stick right", "Left stick: push fully right", "leftx", ((AXIS, ABS_X, 1),)),
    Control("ls_down", "Left stick down", "Left stick: pull fully down", "lefty", ((AXIS, ABS_Y, 1),)),
    Control("rs_right", "Right stick right", "Right stick: push fully right", "rightx", ((AXIS, ABS_RX, 1),)),
    Control("rs_down", "Right stick down", "Right stick: pull fully down", "righty", ((AXIS, ABS_RY, 1),)),
    Control("paddle_left", "Left back paddle", "Left back paddle", "paddle2"),
    Control("paddle_right", "Right back paddle", "Right back paddle", "paddle1"),
    Control("armoury", "Command Center button", "Left button under the screen (Command Center / Armoury Crate), "
                                                 "short press"),
    Control("library", "Library button", "Right button under the screen (Library), short press"),
    Control("armoury_hold", "Command Center button, held", "Left button under the screen again, held for two "
                                                           "seconds (optional)", hold=True, base="armoury"),
    Control("library_hold", "Library button, held", "Right button under the screen again, held for two seconds "
                                                    "(optional)", hold=True, base="library"),
)
EXTRA_BUTTONS = frozenset({"paddle_left", "paddle_right", "armoury", "library", "armoury_hold", "library_hold"})
ASUS_VENDOR = 0x0B05


@dataclass(frozen=True)
class Observation:
    device: int
    kind: str
    code: int
    direction: int = 0

    @property
    def signature(self) -> tuple[str, int, int]:
        return self.kind, self.code, self.direction

    def label(self) -> str:
        if self.kind == KEY:
            return f"{key_name(self.code)} ({self.code})"
        sign = "+" if self.direction > 0 else "-"
        return f"{abs_name(self.code)} ({self.code}) {sign}"


@dataclass(frozen=True)
class AxisInfo:
    minimum: int
    maximum: int
    value: int = 0


@dataclass(frozen=True)
class Event:
    kind: str
    device: int = -1
    type: int = 0
    code: int = 0
    value: int = 0
    data: bytes = b""


class Source(Protocol):
    def poll(self, timeout: float) -> Sequence[Event]:
        """Events that arrived within `timeout` seconds; empty when none did."""

    def begin_prompt(self, control: Control) -> None:
        """Called before each prompt, to drop keystrokes typed earlier."""


class Tracker:
    """Turns raw evdev events into presses: key downs, hat edges and analog threshold crossings.

    State is kept across prompts, so a button that is already down (when the test
    starts, or still held from the previous prompt) is not counted until it has
    been released and pressed again.
    """

    def __init__(self, keys_down: Mapping[int, Iterable[int]] | None = None,
                 axes: Mapping[int, Mapping[int, AxisInfo]] | None = None):
        self._down = {device: set(codes) for device, codes in (keys_down or {}).items()}
        self._axes = {(device, code): info for device, table in (axes or {}).items() for code, info in table.items()}
        self._active: dict[tuple[int, int], int] = {}

    def feed(self, device: int, kind: int, code: int, value: int) -> Observation | None:
        if kind == EV_KEY:
            return self._key(device, code, value)
        if kind == EV_ABS:
            return self._absolute(device, code, value)
        return None

    def _key(self, device: int, code: int, value: int) -> Observation | None:
        down = self._down.setdefault(device, set())
        if value == 0:
            down.discard(code)
            return None
        if value != 1 or code in down:
            return None
        down.add(code)
        return Observation(device, KEY, code)

    def _absolute(self, device: int, code: int, value: int) -> Observation | None:
        hat = HAT_FIRST <= code <= HAT_LAST
        if hat:
            pressed, released, direction = value != 0, value == 0, (value > 0) - (value < 0)
        else:
            info = self._axes.get((device, code))
            span = info.maximum - info.minimum if info else 0
            if info is None or span <= 0:
                return None
            offset = value - info.value
            pressed = abs(offset) >= PRESS_FRACTION * span
            released = abs(offset) <= RELEASE_FRACTION * span
            direction = (offset > 0) - (offset < 0)
        if released:
            self._active.pop((device, code), None)
            return None
        if not pressed or self._active.get((device, code)) == direction:
            return None
        self._active[(device, code)] = direction
        return Observation(device, HAT if hat else AXIS, code, direction)


@dataclass
class ControlResult:
    control: Control
    status: str = NOT_TESTED
    observations: list[Observation] = field(default_factory=list)
    raw_bits: list[UndeclaredBit] = field(default_factory=list)


class Listener:
    """What a front end shows while the session runs. The default shows nothing."""

    def prompt(self, index: int, total: int, control: Control) -> None:
        pass

    def observed(self, control: Control, observation: Observation) -> None:
        pass

    def raw(self, control: Control, bit: UndeclaredBit) -> None:
        pass

    def finished(self, result: ControlResult) -> None:
        pass


class Session:
    def __init__(self, source: Source, tracker: Tracker, clock: Callable[[], float], *, timeout: float = 12.0,
                 controls: Sequence[Control] = CONTROLS, watcher: RawWatcher | None = None,
                 listener: Listener | None = None):
        self.source, self.tracker, self.clock = source, tracker, clock
        self.timeout, self.controls, self.watcher = timeout, controls, watcher
        self.listener = listener or Listener()
        self._last_input = float("-inf")

    def run(self) -> list[ControlResult]:
        """Ask for every control in turn. A quit leaves the remaining ones `not tested`."""
        results: list[ControlResult] = []
        stopped = False
        for index, control in enumerate(self.controls):
            if stopped:
                results.append(ControlResult(control))
                continue
            result, stopped = self._ask(index, control)
            self.listener.finished(result)
            results.append(result)
        return results

    def _ask(self, index: int, control: Control) -> tuple[ControlResult, bool]:
        result = ControlResult(control)
        self.source.begin_prompt(control)
        self.listener.prompt(index, len(self.controls), control)
        limit = self.clock() + self.timeout
        seen = False
        while True:
            now = self.clock()
            if now >= limit:
                break
            for event in self.source.poll(limit - now):
                if event.kind == "quit":
                    result.status = OBSERVED if seen else NOT_TESTED
                    return result, True
                if event.kind == "skip":
                    if not seen and self.clock() - self._last_input >= SKIP_GUARD_SECONDS:
                        result.status = SKIPPED
                        return result, False
                    continue
                if not self._record(event, result):
                    continue
                if not seen:
                    # Keep listening briefly: one press can arrive as several codes or on two devices.
                    seen, limit = True, self.clock() + (HOLD_SECONDS if control.hold else SETTLE_SECONDS)
        result.status = OBSERVED if seen else TIMEOUT
        return result, False

    def _record(self, event: Event, result: ControlResult) -> bool:
        """Store what an event adds to this control's result; True when it added something."""
        if event.kind == "raw":
            bits = self.watcher.feed(event.data) if self.watcher else []
            for bit in bits:
                result.raw_bits.append(bit)
                self.listener.raw(result.control, bit)
            return bool(bits)
        if event.kind != "input":
            return False
        self._last_input = self.clock()
        observation = self.tracker.feed(event.device, event.type, event.code, event.value)
        if observation is None or observation in result.observations:
            return False
        result.observations.append(observation)
        self.listener.observed(result.control, observation)
        return True


# ---- judging what was observed ---------------------------------------------------

def outcome(result: ControlResult) -> str:
    """pass, differs, no event, recorded (no standard code to compare with) or not tested."""
    if result.status in (SKIPPED, NOT_TESTED):
        return NOT_TESTED
    if not result.observations:
        return NO_EVENT
    if not result.control.expected:
        return RECORDED
    return PASS if any(seen.signature in result.control.expected for seen in result.observations) else DIFFERS


def expected_label(control: Control) -> str:
    if not control.expected:
        return "no standard code"
    return Observation(0, *control.expected[0]).label()


@dataclass(frozen=True)
class Verdict:
    key: str
    title: str
    status: Status
    detail: str
    advice: str = ""


def _by_key(results: Sequence[ControlResult]) -> dict[str, ControlResult]:
    return {result.control.key: result for result in results}


def _guide_verdict(results: Mapping[str, ControlResult]) -> Verdict | None:
    guide = results.get("guide")
    if guide is None or outcome(guide) == NOT_TESTED:
        return None
    title = "Xbox button"
    if outcome(guide) == NO_EVENT:
        return Verdict("guide", title, Status.WARN,
                       "The Xbox button sent nothing on the watched devices. A daemon or the firmware may keep it "
                       "for itself, or it arrives on a device this test could not read.")
    codes = [seen.code for seen in guide.observations if seen.kind == KEY]
    if BTN_MODE in codes:
        return Verdict("guide", title, Status.PASS, "The Xbox button reports BTN_MODE (316), the code Steam and "
                                                    "games use for Guide.")
    seen = guide.observations[0].label()
    if BTN_SELECT in codes:
        view = results.get("view")
        same = view is not None and any(obs.kind == KEY and obs.code == BTN_SELECT for obs in view.observations)
        clash = (" View reports the same code, so a remap of that code would change both buttons: they cannot be "
                 "told apart at this layer." if same else "")
        return Verdict("guide", title, Status.WARN,
                       f"The Xbox button reports {seen}, the code of View/Back, instead of BTN_MODE (316). Steam "
                       f"will treat it as View/Back and will not open its menu; a handheld daemon profile can "
                       f"remap it.{clash}",
                       "Suggested profile entry for a handheld daemon such as InputPlumber (a description only; "
                       "nothing is written): map the source gamepad button `Select` to the target gamepad button "
                       "`Guide`.")
    return Verdict("guide", title, Status.WARN,
                   f"The Xbox button reports {seen} instead of BTN_MODE (316), so Steam and games will not treat "
                   "it as Guide; a handheld daemon profile can remap it.")


def _r3_verdict(results: Mapping[str, ControlResult], primary: InputDevice | None) -> Verdict | None:
    r3 = results.get("r3")
    if r3 is None or outcome(r3) == NOT_TESTED:
        return None
    title = "R3 (right stick click)"
    if any(seen.kind == KEY and seen.code == BTN_THUMBR for seen in r3.observations):
        return Verdict("r3", title, Status.PASS, "R3 reports BTN_THUMBR (318).")
    if r3.observations:
        return Verdict("r3", title, Status.WARN,
                       f"R3 reports {r3.observations[0].label()} instead of BTN_THUMBR (318), so games will not "
                       "treat it as the right-stick click until the code is corrected (by a handheld daemon, a "
                       "udev hwdb entry or a kernel driver for this controller).")
    if r3.raw_bits:
        bit = r3.raw_bits[0]
        return Verdict("r3", title, Status.WARN,
                       f"R3 unavailable to apps: pressing it changed bit {bit.bit} of raw HID report "
                       f"0x{bit.report_id:02x}, {bit.where}, which the kernel does not expose as a button. The "
                       "button exists in the hardware; it needs the Ally HID kernel driver or a handheld daemon "
                       "that reads the raw report.")
    if primary is not None and BTN_THUMBR not in primary.keys:
        needs = "needs the Ally HID kernel driver" if primary.vendor == ASUS_VENDOR else "its driver does not expose it"
        return Verdict("r3", title, Status.WARN,
                       f"R3 unavailable ({needs}): nothing arrived, and the controller does not list BTN_THUMBR "
                       "among its buttons.",
                       "A handheld daemon such as InputPlumber reads R3 from the raw HID report and adds it to "
                       "its virtual controller.")
    return Verdict("r3", title, Status.WARN, "R3 was not seen, although the controller lists BTN_THUMBR (318).")


def _leak_verdict(results: Mapping[str, ControlResult], devices: Sequence[InputDevice]) -> Verdict | None:
    leaks, routed = [], []
    for key in sorted(EXTRA_BUTTONS):
        for seen in results[key].observations if key in results else ():
            device = devices[seen.device] if 0 <= seen.device < len(devices) else None
            if seen.kind != KEY or device is None or not device.is_keyboard:
                continue
            (routed if device.virtual else leaks).append(f"{results[key].control.name}: {seen.label()}")
    if leaks:
        return Verdict("extra_keys", "Paddles and vendor buttons", Status.WARN,
                       f"These buttons arrive as keyboard keys on a keyboard device that every app can read "
                       f"({'; '.join(dict.fromkeys(leaks))}): the keys leak into applications; use InputPlumber.")
    if routed:
        return Verdict("extra_keys", "Paddles and vendor buttons", Status.INFO,
                       f"These buttons arrive as keys from an input daemon's virtual keyboard "
                       f"({'; '.join(dict.fromkeys(routed))}); its profile decides what they do.")
    return None


def _duplicate_verdict(results: Sequence[ControlResult]) -> Verdict | None:
    owners: dict[tuple[int, str, int, int], list[ControlResult]] = {}
    for result in results:
        for seen in result.observations:
            owners.setdefault((seen.device, *seen.signature), []).append(result)
    clashes = []
    for (_device, *signature), sharing in owners.items():
        bases = {result.control.base or result.control.key for result in sharing}
        if len(bases) > 1:
            names = " and ".join(dict.fromkeys(result.control.name for result in sharing))
            clashes.append(f"{names} both report {Observation(0, *signature).label()}")
    if not clashes:
        return None
    return Verdict("duplicates", "Buttons that share a code", Status.WARN,
                   f"{'; '.join(clashes)}. Buttons that send the same code cannot be told apart by any app.")


def _mismatch_verdict(results: Sequence[ControlResult]) -> Verdict | None:
    differing = [f"{result.control.name} reports {result.observations[0].label()}, "
                 f"expected {expected_label(result.control)}"
                 for result in results if result.control.key not in ("guide", "r3") and outcome(result) == DIFFERS]
    if not differing:
        return None
    return Verdict("mismatch", "Controls with an unexpected code", Status.WARN,
                   f"{'; '.join(differing)}. Games that expect an Xbox-style layout will act on the wrong control.")


# The order in which an XInput-style controller declares its buttons to HID.
HID_BUTTON_ORDER = ("a", "b", "x", "y", "lb", "rb", "view", "menu", "l3", "r3", "guide")


def _hid_order_verdict(results: Mapping[str, ControlResult]) -> Verdict | None:
    """Buttons that arrive as consecutive codes from BTN_SOUTH: numbered by a generic driver."""
    pressed = {key: next((seen.code for seen in results[key].observations if seen.kind == KEY), None)
               for key in HID_BUTTON_ORDER if key in results}
    seen = {key: code for key, code in pressed.items() if code is not None}
    in_order = all(code == BTN_SOUTH + HID_BUTTON_ORDER.index(key) for key, code in seen.items())
    wrong = [key for key in seen if outcome(results[key]) == DIFFERS]
    if len(seen) < 6 or not in_order or not wrong:
        return None
    return Verdict("hid_order", "Button numbering", Status.WARN,
                   "The buttons arrive as consecutive codes in the order A, B, X, Y, LB, RB, View, Menu, L3, R3, "
                   "Xbox, starting at BTN_SOUTH (304): a generic driver numbered them without knowing the layout, "
                   "so everything from the third button on carries another button's code.",
                   "A handheld daemon such as InputPlumber, a udev hwdb entry or a kernel driver for this "
                   "controller assigns the standard codes. The SDL mapping below already matches what was "
                   "pressed.")


def _silent_verdict(results: Sequence[ControlResult]) -> Verdict | None:
    silent = [result.control.name for result in results
              if result.control.key not in ("guide", "r3") and not result.control.hold and outcome(result) == NO_EVENT]
    if not silent:
        return None
    return Verdict("silent", "Controls that sent nothing", Status.WARN,
                   f"No event arrived for: {', '.join(silent)}. They are either not exposed on the watched "
                   "devices or were not pressed before the time limit.")


def verdicts(results: Sequence[ControlResult], devices: Sequence[InputDevice],
             primary: InputDevice | None) -> list[Verdict]:
    """Compare what was observed with Xbox-style gamepad semantics on Linux."""
    by_key = _by_key(results)
    found = [_hid_order_verdict(by_key), _guide_verdict(by_key), _r3_verdict(by_key, primary),
             _leak_verdict(by_key, devices), _duplicate_verdict(results), _mismatch_verdict(results),
             _silent_verdict(results)]
    return [verdict for verdict in found if verdict is not None]


# ---- SDL mapping built only from what was pressed ----------------------------------

def sdl_guid(device: InputDevice) -> str:
    """SDL's joystick GUID for an evdev device: bus, vendor, product and version, with a zero name CRC."""
    return struct.pack("<8H", device.bus_type, 0, device.vendor, 0, device.product, 0, device.version, 0).hex()


def _sdl_element(device: InputDevice, seen: Observation) -> str:
    """SDL's name for an input: it numbers buttons, axes and hats in code order, joystick buttons first."""
    if seen.kind == KEY:
        ordered = sorted(code for code in device.keys if code >= BTN_JOYSTICK)
        ordered += sorted(code for code in device.keys if code < BTN_JOYSTICK)
        return f"b{ordered.index(seen.code)}" if seen.code in ordered else ""
    if seen.kind == HAT:
        pairs = [first for first in range(HAT_FIRST, HAT_LAST, 2) if {first, first + 1} & device.axes]
        base = seen.code - (seen.code - HAT_FIRST) % 2
        if base not in pairs:
            return ""
        vertical = (seen.code - HAT_FIRST) % 2 == 1
        mask = (1 if seen.direction < 0 else 4) if vertical else (8 if seen.direction < 0 else 2)
        return f"h{pairs.index(base)}.{mask}"
    ordered = [code for code in sorted(device.axes) if not HAT_FIRST <= code <= HAT_LAST]
    if seen.code not in ordered:
        return ""
    return f"a{ordered.index(seen.code)}" + ("~" if seen.direction < 0 else "")


def sdl_mapping(results: Sequence[ControlResult], device: InputDevice, index: int) -> tuple[str, list[str]]:
    """(mapping line, controls it leaves out) for the gamepad at `index` in the watched list."""
    entries, missing = [], []
    for result in results:
        control = result.control
        if not control.sdl:
            continue
        element = next((_sdl_element(device, seen) for seen in result.observations if seen.device == index), "")
        if element:
            entries.append(f"{control.sdl}:{element}")
        else:
            missing.append(f"{control.sdl} ({control.name})")
    if not entries:
        return "", missing
    name = device.name.replace(",", " ")
    return f"{sdl_guid(device)},{name},{','.join(entries)},platform:Linux,", missing


# ---- report -----------------------------------------------------------------------

def control_rows(results: Sequence[ControlResult], devices: Sequence[InputDevice]) -> list[dict[str, Any]]:
    rows = []
    for result in results:
        # The key is `event_code`: a bare `code` is on the redactor's list of credential-looking keys.
        observed = [{"node": devices[seen.device].node if 0 <= seen.device < len(devices) else "",
                     "event": seen.label(), "type": seen.kind, "event_code": seen.code, "direction": seen.direction}
                    for seen in result.observations]
        observed += [{"node": "hidraw", "event": f"raw HID bit {bit.bit} of report 0x{bit.report_id:02x}",
                      "type": "raw", "event_code": bit.bit, "direction": 0} for bit in result.raw_bits]
        rows.append({"control": result.control.key, "label": result.control.name, "status": result.status,
                     "result": outcome(result), "expected": expected_label(result.control), "observed": observed})
    return rows


def device_rows(devices: Sequence[InputDevice], watched: Sequence[InputDevice]) -> list[dict[str, Any]]:
    """Identity of every candidate device: no serial, `uniq` or `phys` is ever part of it."""
    nodes = {device.node for device in watched}
    return [{"node": device.node, "name": device.name, "kind": device.kind(),
             "role": "gamepad" if device.is_gamepad else "keyboard", "bus": device.bus, "id": device.vendor_product,
             "access": device.access, "watched": device.node in nodes} for device in devices]


def verdict_checks(found: Sequence[Verdict]) -> list[CheckResult]:
    return [CheckResult(f"buttons.{verdict.key}", verdict.title, verdict.status, verdict.detail, "buttons",
                        "What this controller sent while the button was pressed.",
                        "That a game or Steam acts on it the same way.", verdict.advice) for verdict in found]


def summary_table(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str, str]]:
    """(control, expected, observed, result) rows for the text and Markdown tables."""
    return [(row["label"], row["expected"],
             "; ".join(f"{item['event']} on {item['node']}" for item in row["observed"]) or "-", row["result"])
            for row in rows]
