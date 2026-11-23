"""Button translations for controllers whose evdev codes are wrong.

Some controllers report their buttons in a sensible order but under the
wrong Linux codes: no kernel driver describes them, and the generic HID
driver numbers buttons by position. Game Mode would then take View for LB,
the Xbox button for View, and so on.

A quirk only changes how *Game Mode* reads a device for its own menus. It
does not touch the device, the kernel, or what other programs see; the
system-wide fix is a udev hwdb entry (``contrib/udev``). Every quirk is
gated on an exact fingerprint of the device and stops applying as soon as
the codes are right, for example once that hwdb entry or a kernel driver is
in place.

Pure module: no GTK and no IO, apart from one environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .core import (ABS_HAT0X, ABS_HAT0Y, ABS_X, ABS_Y, BTN_EAST, BTN_MODE, BTN_SELECT, BTN_SOUTH, BTN_START, BTN_TL,
                   BTN_TR)

# linux/input-event-codes.h, the gamepad buttons core.py does not need itself
BTN_C, BTN_NORTH, BTN_WEST, BTN_Z = 306, 307, 308, 309
BTN_TL2, BTN_TR2 = 312, 313
BTN_THUMBL, BTN_THUMBR = 317, 318

# Set to "1" to read every controller exactly as the kernel reports it.
DISABLE_ENV = "GAMEMODE_NO_BUTTON_QUIRKS"

# ASUS ROG Xbox Ally (RC73YA / RC73XA) built-in pad without a kernel driver.
#
# Its HID descriptor declares a game pad with 14 buttons, and the kernel's
# generic hid-input gives button N the code BTN_GAMEPAD + N - 1. The buttons
# are in XInput order (A, B, X, Y, LB, RB, View, Menu, L3, R3, Xbox, then
# three whose meaning is unknown), so everything from the third one on wears
# the code of another button.
#
# Raw code -> code of the button that was pressed. The three unknown buttons
# map to None: they are dropped, because an unknown button must never be
# taken for a standard one.
#
# The table is derived from two independent projects plus one observation,
# and not yet confirmed by a physical press test: Handheld Daemon's Ally X
# button map and the fallback capability map InputPlumber once shipped for
# the Ally X, both for this USB id, agree on X, Y, LB, RB, View, Menu, L3 and
# R3. The Xbox button is in HHD's map only, and was seen once as BTN_SELECT
# on an RC73YA.
XINPUT_ORDER_HID: Mapping[int, int | None] = MappingProxyType({
    BTN_SOUTH: BTN_SOUTH,     # A
    BTN_EAST: BTN_EAST,       # B
    BTN_C: BTN_NORTH,         # X
    BTN_NORTH: BTN_WEST,      # Y
    BTN_WEST: BTN_TL,         # LB
    BTN_Z: BTN_TR,            # RB
    BTN_TL: BTN_SELECT,       # View
    BTN_TR: BTN_START,        # Menu
    BTN_TL2: BTN_THUMBL,      # L3
    BTN_TR2: BTN_THUMBR,      # R3
    BTN_SELECT: BTN_MODE,     # Xbox
    BTN_START: None,
    BTN_MODE: None,
    BTN_THUMBL: None,
})

ROG_XBOX_ALLY_VENDOR, ROG_XBOX_ALLY_PRODUCT = 0x0B05, 0x1B4C
ROG_XBOX_ALLY_NAME = "ASUSTeK Computer Inc. N-KEY Device"
GAMEPAD_AXES = frozenset({ABS_X, ABS_Y, ABS_HAT0X, ABS_HAT0Y})


@dataclass(frozen=True, eq=False)
class Quirk:
    """How to read one kind of device: a name, a translation of key codes and a description."""
    name: str
    table: Mapping[int, int | None]
    summary: str

    def translate(self, code: int) -> int | None:
        """The code Game Mode acts on for a raw key code; None when the button is ignored."""
        return self.table.get(code, code)

    def translate_all(self, codes: Iterable[int]) -> frozenset:
        """Translate a set of raw codes: the buttons a device has, or the ones held right now."""
        return frozenset(code for code in map(self.translate, codes) if code is not None)

    def describe(self) -> str:
        return f"{self.name}: {self.summary}"


XINPUT_ORDER_QUIRK = Quirk(
    "xinput-order-hid", XINPUT_ORDER_HID,
    "buttons arrive in XInput order under generic HID codes; Game Mode translates them for its own menus only. "
    "The table is derived from two independent projects plus one observation, "
    "not yet confirmed by a physical press test")


def might_need_quirk(identity, env: Mapping[str, str] = os.environ) -> bool:
    """Whether a device with this identity can need a quirk at all, depending on its key codes.

    False means never, whatever the device reports later; such a device is
    read as it is without asking for its key codes again.
    """
    if env.get(DISABLE_ENV) == "1" or identity is None:
        return False
    return ((identity.vendor, identity.product) == (ROG_XBOX_ALLY_VENDOR, ROG_XBOX_ALLY_PRODUCT)
            and identity.name == ROG_XBOX_ALLY_NAME)


def quirk_for(info, env: Mapping[str, str] = os.environ) -> Quirk | None:
    """The quirk a probed device needs, or None.

    ``info`` is an ``input.Probe`` or an open device: an identity plus the key
    and axis codes the kernel reports for it. Everything has to match. The pad
    must also lack BTN_THUMBR: with correct codes it has one, so a corrected
    device (hwdb entry, kernel driver) no longer matches and is read as it is.
    """
    if not might_need_quirk(info.identity, env):
        return None
    if not set(XINPUT_ORDER_HID) <= info.keys or BTN_THUMBR in info.keys:
        return None
    if not GAMEPAD_AXES <= info.axes:
        return None
    return XINPUT_ORDER_QUIRK
