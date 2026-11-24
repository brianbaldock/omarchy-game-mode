"""evdev code names and read-only device enumeration from sysfs, shared by the input checks.

Nothing here opens a device node. Capabilities, names and bus ids come from
/sys/class/input. The `uniq` and `phys` attributes and the HID `uevent` file (serial
numbers, Bluetooth and USB addresses) are never read: a raw HID interface is
identified from its sysfs path alone. A device name is free text chosen by firmware
or by whoever paired the device, so only a known model name is kept when it is
read; any other name becomes a neutral label.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, replace

from .context import Context

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST, BTN_Z = 0x130, 0x131, 0x132, 0x133, 0x134, 0x135
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13A, 0x13B, 0x13C, 0x13D, 0x13E
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
BTN_TRIGGER_HAPPY1 = 0x2C0
KEY_PROG1, KEY_F13 = 148, 183
FF_RUMBLE = 0x50
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y, ABS_HAT2X, ABS_HAT2Y = 0x09, 0x0A, 0x10, 0x11, 0x14, 0x15
BUS_NAMES = {0x03: "USB", 0x05: "Bluetooth", 0x06: "virtual", 0x18: "I2C", 0x19: "built-in"}
BUS_VIRTUAL = 0x06
LONG_BITS = struct.calcsize("l") * 8
NAME_LIMIT = 80
# Names that drivers and input daemons give the controllers and key devices the doctor reports on.
# Only these are ever printed: any other name was chosen by firmware or by whoever paired the
# device, and can carry a serial number, an address or the owner's name.
KNOWN_NAMES = frozenset({
    "ASUSTeK Computer Inc. N-KEY Device", "Asus WMI hotkeys", "InputPlumber Keyboard", "InputPlumber Mouse",
    "Microsoft X-Box 360 pad", "Microsoft X-Box One pad", "Microsoft X-Box One S pad", "Microsoft X-Box One Elite pad",
    "Microsoft X-Box One Elite 2 pad", "Microsoft Xbox Series S|X Controller", "Xbox Wireless Controller",
    "Generic X-Box pad", "Handheld Daemon Controller",
})
UNKNOWN_CONTROLLER, UNKNOWN_DEVICE = "<controller>", "<device>"
# Key devices of an input daemon or of the platform's own buttons, recognised by their raw name.
DAEMON_KEYS = re.compile(r"^(InputPlumber|Handheld Daemon)\b|hotkeys$", re.I)
# A HID device directory: bus, vendor, product and instance, as in `0003:0B05:1B4C.0006`.
HID_DEVICE = re.compile(r"[0-9A-Fa-f]{4}:([0-9A-Fa-f]{4,8}):([0-9A-Fa-f]{4,8})\.[0-9A-Fa-f]{4}")
# A USB interface directory, as in `3-6:1.5`: its parent is the USB device itself.
USB_INTERFACE = re.compile(r"\d+-[\d.]+:\d+\.\d+")

# Values from linux/input-event-codes.h. Gamepad buttons, the keyboard keys handheld
# buttons are commonly wired to, and every function/programmable key in between.
KEY_NAMES = {
    1: "KEY_ESC", 15: "KEY_TAB", 28: "KEY_ENTER", 29: "KEY_LEFTCTRL", 31: "KEY_S", 32: "KEY_D", 34: "KEY_G",
    42: "KEY_LEFTSHIFT", 56: "KEY_LEFTALT", 57: "KEY_SPACE", 99: "KEY_SYSRQ", 103: "KEY_UP", 105: "KEY_LEFT",
    106: "KEY_RIGHT", 108: "KEY_DOWN", 111: "KEY_DELETE", 113: "KEY_MUTE", 114: "KEY_VOLUMEDOWN",
    115: "KEY_VOLUMEUP", 116: "KEY_POWER", 125: "KEY_LEFTMETA", 126: "KEY_RIGHTMETA", 127: "KEY_COMPOSE",
    139: "KEY_MENU", 142: "KEY_SLEEP", 148: "KEY_PROG1", 149: "KEY_PROG2", 152: "KEY_SCREENLOCK", 167: "KEY_RECORD",
    171: "KEY_CONFIG", 172: "KEY_HOMEPAGE", 202: "KEY_PROG3", 203: "KEY_PROG4", 204: "KEY_ALL_APPLICATIONS",
    210: "KEY_PRINT", 212: "KEY_CAMERA", 224: "KEY_BRIGHTNESSDOWN", 225: "KEY_BRIGHTNESSUP",
    228: "KEY_KBDILLUMTOGGLE", 248: "KEY_MICMUTE", 0x160: "KEY_OK", 0x176: "KEY_KEYBOARD", 0x1D0: "KEY_FN",
    0x27A: "KEY_SELECTIVE_SCREENSHOT",
    0x130: "BTN_SOUTH", 0x131: "BTN_EAST", 0x132: "BTN_C", 0x133: "BTN_NORTH", 0x134: "BTN_WEST", 0x135: "BTN_Z",
    0x136: "BTN_TL", 0x137: "BTN_TR", 0x138: "BTN_TL2", 0x139: "BTN_TR2", 0x13A: "BTN_SELECT", 0x13B: "BTN_START",
    0x13C: "BTN_MODE", 0x13D: "BTN_THUMBL", 0x13E: "BTN_THUMBR",
    0x220: "BTN_DPAD_UP", 0x221: "BTN_DPAD_DOWN", 0x222: "BTN_DPAD_LEFT", 0x223: "BTN_DPAD_RIGHT",
    0x224: "BTN_GRIPL", 0x225: "BTN_GRIPR", 0x226: "BTN_GRIPL2", 0x227: "BTN_GRIPR2",
    **{59 + index: f"KEY_F{index + 1}" for index in range(10)}, 87: "KEY_F11", 88: "KEY_F12",
    **{183 + index: f"KEY_F{index + 13}" for index in range(12)},
    **{0x290 + index: f"KEY_MACRO{index + 1}" for index in range(30)},
    **{0x2C0 + index: f"BTN_TRIGGER_HAPPY{index + 1}" for index in range(40)},
}
ABS_NAMES = {
    0x00: "ABS_X", 0x01: "ABS_Y", 0x02: "ABS_Z", 0x03: "ABS_RX", 0x04: "ABS_RY", 0x05: "ABS_RZ",
    0x06: "ABS_THROTTLE", 0x07: "ABS_RUDDER", 0x08: "ABS_WHEEL", 0x09: "ABS_GAS", 0x0A: "ABS_BRAKE",
    0x10: "ABS_HAT0X", 0x11: "ABS_HAT0Y", 0x12: "ABS_HAT1X", 0x13: "ABS_HAT1Y", 0x14: "ABS_HAT2X",
    0x15: "ABS_HAT2Y", 0x16: "ABS_HAT3X", 0x17: "ABS_HAT3Y", 0x28: "ABS_MISC",
}
# What an Xbox-style pad reports on Linux. BTN_NORTH is the kernel's BTN_X and BTN_WEST its BTN_Y.
STANDARD_BUTTONS = (
    (BTN_SOUTH, "A"), (BTN_EAST, "B"), (BTN_NORTH, "X"), (BTN_WEST, "Y"), (BTN_TL, "LB"), (BTN_TR, "RB"),
    (BTN_SELECT, "View"), (BTN_START, "Menu"), (BTN_THUMBL, "L3"), (BTN_THUMBR, "R3"),
)
TRIGGER_SOURCES = (({BTN_TL2, BTN_TR2}, EV_KEY), ({ABS_Z, ABS_RZ}, EV_ABS), ({ABS_BRAKE, ABS_GAS}, EV_ABS),
                   ({ABS_HAT2X, ABS_HAT2Y}, EV_ABS))


def key_name(code: int) -> str:
    return KEY_NAMES.get(code) or (f"BTN_0x{code:x}" if 0x100 <= code < 0x160 or code >= 0x220 else f"KEY_{code}")


def abs_name(code: int) -> str:
    return ABS_NAMES.get(code) or f"ABS_0x{code:02x}"


def code_label(kind: int, code: int) -> str:
    """`BTN_SELECT (314)`: the kernel name with the decimal code tools like evtest show."""
    return f"{key_name(code) if kind == EV_KEY else abs_name(code)} ({code})"


def parse_bitmap(text: str) -> set[int]:
    """Parse a sysfs capability bitmap ("3fff000000000000 0 0 0 0").

    Private copy of gaminghome/input.py::parse_bitmap: that module imports GLib, which
    the doctor must never load.
    """
    bits = set()
    for index, word in enumerate(reversed(text.split())):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        base = index * LONG_BITS
        while value:
            low = value & -value
            bits.add(base + low.bit_length() - 1)
            value ^= low
    return bits


@dataclass(frozen=True)
class InputDevice:
    node: str
    name: str
    bus: str
    vendor_product: str
    virtual: bool
    keys: frozenset[int]
    axes: frozenset[int]
    access: str
    bus_type: int = 0
    version: int = 0
    daemon_keys: bool = False

    @property
    def path(self) -> str:
        return f"/dev/input/{self.node}"

    @property
    def vendor(self) -> int:
        return _hex(self.vendor_product.partition(":")[0])

    @property
    def product(self) -> int:
        return _hex(self.vendor_product.partition(":")[2])

    @property
    def is_gamepad(self) -> bool:
        """Same heuristic as the launcher: BTN_SOUTH plus sticks or a hat."""
        return BTN_SOUTH in self.keys and ({ABS_X, ABS_Y} <= self.axes or {ABS_HAT0X, ABS_HAT0Y} <= self.axes)

    @property
    def is_keyboard(self) -> bool:
        return not self.is_gamepad and bool(self.keys & set(range(1, 0x100)))

    @property
    def has_guide(self) -> bool:
        return BTN_MODE in self.keys

    @property
    def hid_ordered(self) -> bool:
        """Heuristic: buttons numbered in HID order by a generic driver, not by layout.

        Such a pad lists an unbroken run from BTN_SOUTH that includes BTN_C and BTN_Z,
        codes an Xbox-style driver never uses. Its buttons then arrive, but often under
        another button's code; only pressing them shows which.
        """
        return {BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST, BTN_Z} <= self.keys

    def missing_controls(self) -> list[str]:
        """Standard gamepad controls this device does not list, as `R3 (BTN_THUMBR)`."""
        missing = [f"{label} ({key_name(code)})" for code, label in STANDARD_BUTTONS if code not in self.keys]
        if not any(codes <= (self.keys if kind == EV_KEY else self.axes) for codes, kind in TRIGGER_SOURCES):
            missing.append("LT/RT (triggers)")
        if not ({ABS_HAT0X, ABS_HAT0Y} <= self.axes or {BTN_DPAD_UP, BTN_DPAD_DOWN} <= self.keys):
            missing.append("D-pad")
        return missing

    def kind(self) -> str:
        return "virtual" if self.virtual else "physical"


def _access(ctx: Context, node: str) -> str:
    mode = ctx.fs.mode(f"/dev/input/{node}")
    if mode is None:
        return "missing"
    if mode & 0o777 == 0:
        return "hidden"
    return "readable" if ctx.fs.readable(f"/dev/input/{node}") else "no-permission"


def _hex(text: str) -> int:
    try:
        return int(text, 16)
    except ValueError:
        return 0


def device_label(ctx: Context, text: str, gamepad: bool) -> str:
    """The name to print for a device: a known model name, or `<controller>` / `<device>`.

    Names can carry a serial number, a Bluetooth address or the owner's name, so nothing
    downstream (verdicts, prompts, the SDL mapping line) ever sees the raw text.
    """
    name = " ".join(text.split())
    if name in KNOWN_NAMES:
        return ctx.redactor.text(name)[:NAME_LIMIT]
    return UNKNOWN_CONTROLLER if gamepad else UNKNOWN_DEVICE


def input_devices(ctx: Context) -> list[InputDevice]:
    """Every evdev device, in event-number order."""
    devices = []
    nodes = [name for name in ctx.fs.listdir("/sys/class/input") if re.fullmatch(r"event\d+", name)]
    for node in sorted(nodes, key=lambda name: int(name[5:])):
        base = f"/sys/class/input/{node}/device"
        bus = _hex(ctx.fs.line(f"{base}/id/bustype"))
        raw_name = ctx.fs.line(f"{base}/name")
        vendor, product = (_hex(ctx.fs.line(f"{base}/id/{field}")) for field in ("vendor", "product"))
        device = InputDevice(
            node=node, name="", bus=BUS_NAMES.get(bus, f"bus 0x{bus:02x}"),
            vendor_product=f"{vendor:04x}:{product:04x}",
            virtual=bus == BUS_VIRTUAL or "/devices/virtual/" in ctx.fs.resolved(base),
            keys=frozenset(parse_bitmap(ctx.fs.read(f"{base}/capabilities/key") or "")),
            axes=frozenset(parse_bitmap(ctx.fs.read(f"{base}/capabilities/abs") or "")),
            access=_access(ctx, node), bus_type=bus, version=_hex(ctx.fs.line(f"{base}/id/version")),
            daemon_keys=bool(DAEMON_KEYS.search(raw_name)))
        devices.append(replace(device, name=device_label(ctx, raw_name, device.is_gamepad)))
    return devices


def hid_parent(ctx: Context, sysfs_device: str) -> str:
    """Path of the HID device (`0003:0B05:1B4C.0006`) above a sysfs node, or empty."""
    parts = ctx.fs.resolved(sysfs_device).split("/")
    for index, part in enumerate(parts):
        if HID_DEVICE.fullmatch(part):
            return "/".join(parts[:index + 1])
    return ""


def physical_device(ctx: Context, sysfs_device: str) -> str:
    """Path of the physical device behind a sysfs node, or empty for virtual and platform devices.

    That is the USB device for a USB HID interface (all interfaces of one controller share it),
    and the HID device itself on other buses (Bluetooth, I2C), where it stands for one device.
    """
    hid = hid_parent(ctx, sysfs_device)
    parent = hid.rpartition("/")[0]
    above, _slash, name = parent.rpartition("/")
    return above if hid and USB_INTERFACE.fullmatch(name) else hid


def gamepads(ctx: Context) -> list[InputDevice]:
    return ctx.once("gamepads", lambda: [device for device in input_devices(ctx) if device.is_gamepad])


def force_feedback(ctx: Context, device: InputDevice) -> set[int]:
    """The force-feedback effects (FF_RUMBLE and the like) that the kernel offers on an evdev node."""
    return parse_bitmap(ctx.fs.read(f"/sys/class/input/{device.node}/device/capabilities/ff", 4096) or "")


@dataclass(frozen=True)
class HidrawNode:
    node: str
    vendor: int
    product: int
    interface: int | None
    mode: int | None
    access: str

    @property
    def path(self) -> str:
        return f"/dev/{self.node}"


def hidraw_nodes(ctx: Context) -> list[HidrawNode]:
    """Raw HID interfaces with their USB interface number and node permissions. Nothing is opened.

    Vendor and product come from the name of the HID device directory, so the `uevent`
    file next to it, which also holds the device's serial number, is not read.
    """
    found = []
    names = [name for name in ctx.fs.listdir("/sys/class/hidraw") if re.fullmatch(r"hidraw\d+", name)]
    for name in sorted(names, key=lambda node: int(node[6:])):
        device = hid_parent(ctx, f"/sys/class/hidraw/{name}/device")
        directory, _slash, identity = device.rpartition("/")
        match = HID_DEVICE.fullmatch(identity)
        if match is None:
            continue
        number = ctx.fs.line(f"{directory}/bInterfaceNumber")
        mode = ctx.fs.mode(f"/dev/{name}")
        if mode is None:
            access = "missing"
        elif mode & 0o777 == 0:
            access = "hidden"
        else:
            access = "readable" if ctx.fs.readable(f"/dev/{name}") else "no-permission"
        found.append(HidrawNode(name, _hex(match.group(1)), _hex(match.group(2)), _hex(number) if number else None,
                                None if mode is None else mode & 0o777, access))
    return found
