"""Button quirks: the gate, the table, and what the interpreter makes of translated presses.

Pure: no devices and no GTK. ``tests/test_input.py`` covers how the controller
monitor applies a quirk to a device.
"""

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from gaminghome import core, quirks
from gaminghome.quirks import BTN_C, BTN_NORTH, BTN_THUMBL, BTN_THUMBR, BTN_TL2, BTN_TR2, BTN_WEST, BTN_Z, quirk_for

HWDB = Path(__file__).resolve().parent.parent / "contrib" / "udev" / "61-rog-xbox-ally-gamepad.hwdb"
BTN_GAMEPAD = core.BTN_SOUTH
RAW_KEYS = frozenset(range(BTN_GAMEPAD, BTN_GAMEPAD + 14))     # what the kernel reports for the pad without a driver
AXES = frozenset({core.ABS_X, core.ABS_Y, 2, 3, 4, 5, core.ABS_HAT0X, core.ABS_HAT0Y})
EVDEV_CODES = {"btn_north": BTN_NORTH, "btn_west": BTN_WEST, "btn_tl": core.BTN_TL, "btn_tr": core.BTN_TR,
               "btn_select": core.BTN_SELECT, "btn_start": core.BTN_START, "btn_mode": core.BTN_MODE,
               "btn_thumbl": BTN_THUMBL, "btn_thumbr": BTN_THUMBR,
               "btn_trigger_happy1": 0x2C0, "btn_trigger_happy2": 0x2C1, "btn_trigger_happy3": 0x2C2}


def pad(name="ASUSTeK Computer Inc. N-KEY Device", vendor=0x0B05, product=0x1B4C, keys=RAW_KEYS, axes=AXES):
    """What probing the raw ROG Xbox Ally pad yields, or something close to it."""
    return SimpleNamespace(identity=SimpleNamespace(name=name, bustype=0x03, vendor=vendor, product=product),
                           keys=frozenset(keys), axes=frozenset(axes))


def hwdb_buttons() -> dict[int, str]:
    """HID button number -> evdev code name, as the shipped hwdb entry assigns them."""
    entries = re.findall(r"^ KEYBOARD_KEY_9([0-9a-f]{4})=(\w+)$", HWDB.read_text(encoding="utf-8"), re.M)
    return {int(usage, 16): name for usage, name in entries}


class GateTest(unittest.TestCase):
    def test_raw_rog_xbox_ally_pad_gets_the_quirk(self):
        quirk = quirk_for(pad(), env={})
        self.assertEqual(quirk.name, "xinput-order-hid")
        self.assertTrue(quirk.describe().startswith("xinput-order-hid: "))
        self.assertIs(quirk_for(pad(), env={quirks.DISABLE_ENV: "0"}), quirk)

    def test_anything_but_that_exact_pad_is_read_as_it_is(self):
        others = {
            "another product": pad(product=0x1ABE),
            "another vendor": pad(vendor=0x045E),
            "another name": pad(name="ASUSTeK Computer Inc. N-KEY Device Keyboard"),
            "a button of the fingerprint is missing": pad(keys=RAW_KEYS - {BTN_C}),
            "the pad has a right stick click": pad(keys=RAW_KEYS | {BTN_THUMBR}),
            "no D-pad": pad(axes=AXES - {core.ABS_HAT0X, core.ABS_HAT0Y}),
            "no left stick": pad(axes=AXES - {core.ABS_X}),
            "no identity": SimpleNamespace(identity=None, keys=RAW_KEYS, axes=AXES),
        }
        for reason, info in others.items():
            self.assertIsNone(quirk_for(info, env={}), reason)

    def test_environment_variable_turns_every_quirk_off(self):
        self.assertIsNone(quirk_for(pad(), env={quirks.DISABLE_ENV: "1"}))

    def test_quirk_switches_itself_off_once_the_hwdb_entry_corrected_the_codes(self):
        # The kernel then reports each button under the code the entry gives it.
        buttons = hwdb_buttons()
        corrected = {EVDEV_CODES[buttons[number]] if number in buttons else BTN_GAMEPAD + number - 1
                     for number in range(1, 15)}
        self.assertIn(BTN_THUMBR, corrected)
        self.assertFalse(corrected & {BTN_C, BTN_Z, BTN_TL2, BTN_TR2})
        self.assertIsNone(quirk_for(pad(keys=corrected), env={}))


class TableTest(unittest.TestCase):
    def setUp(self):
        self.quirk = quirk_for(pad(), env={})

    def test_table_and_hwdb_entry_agree_on_every_button(self):
        buttons = hwdb_buttons()
        self.assertEqual(sorted(buttons), list(range(3, 15)))
        for number in range(1, 15):
            translated = self.quirk.translate(BTN_GAMEPAD + number - 1)
            if number <= 2:
                self.assertEqual(translated, BTN_GAMEPAD + number - 1)              # A and B are right already
            elif number <= 11:
                self.assertEqual(translated, EVDEV_CODES[buttons[number]], number)
            else:
                # Unknown buttons: the entry parks them on spare codes, Game Mode drops them.
                self.assertIsNone(translated, number)
                self.assertGreaterEqual(EVDEV_CODES[buttons[number]], 0x2C0)

    def test_no_two_buttons_end_up_on_one_code_and_other_codes_pass_through(self):
        translated = [self.quirk.translate(code) for code in sorted(RAW_KEYS)]
        kept = [code for code in translated if code is not None]
        self.assertEqual((len(kept), len(set(kept))), (11, 11))
        self.assertEqual(self.quirk.translate_all(RAW_KEYS), frozenset(kept))
        self.assertEqual([self.quirk.translate(code) for code in (30, 0x2C0)], [30, 0x2C0])


class NavigationTest(unittest.TestCase):
    """The interpreter behind the quirk: what the buttons of the raw pad do in Game Mode."""

    def setUp(self):
        self.quirk = quirk_for(pad(), env={})
        self.timers, self.actions = [], []
        self.ci = core.ControllerInterpreter(lambda _ms, callback: self.timers.append(callback) or len(self.timers),
                                             lambda _token: self.timers.clear(), self.actions.append)
        self.ci.set_nav_enabled(True)

    def press(self, raw, value=1, translate=True):
        code = self.quirk.translate(raw) if translate else raw
        if code is not None:
            self.ci.feed("pad", core.EV_KEY, code, value)

    def hold(self):
        timers, self.timers[:] = list(self.timers), []
        for callback in timers:
            callback()

    def test_view_and_menu_form_the_home_chord_and_the_xbox_button_is_guide(self):
        self.press(core.BTN_SELECT)                # Xbox: Guide, which Game Mode never acts on
        self.press(core.BTN_TR)                    # Menu: were Xbox still read as View, this would be the chord
        self.press(core.BTN_START)                 # an unknown button
        self.hold()
        self.assertEqual(self.actions, [])
        self.press(core.BTN_SELECT, 0)
        self.press(core.BTN_TL)                    # View, with Menu still held
        self.hold()
        self.assertEqual(self.actions, [core.HOME])

    def test_without_the_quirk_the_same_presses_are_misread(self):
        self.press(core.BTN_SELECT, translate=False)
        self.press(core.BTN_START, translate=False)
        self.hold()
        self.assertEqual(self.actions, [core.HOME])                  # Xbox + an unknown button
        del self.actions[:]
        self.press(core.BTN_TL, translate=False)
        self.press(core.BTN_TR, translate=False)
        self.hold()
        self.assertEqual(self.actions, [core.PREV_TAB, core.NEXT_TAB])   # View and Menu taken for LB and RB

    def test_face_buttons_bumpers_and_menu_do_what_is_printed_on_them(self):
        for raw in (core.BTN_SOUTH, core.BTN_EAST, BTN_WEST, BTN_Z, core.BTN_TR):   # A, B, LB, RB, Menu
            self.press(raw)
            self.press(raw, 0)
        self.assertEqual(self.actions, [core.ACTIVATE, core.BACK, core.PREV_TAB, core.NEXT_TAB, core.MENU])
        del self.actions[:]
        for raw in (BTN_C, BTN_NORTH, core.BTN_TL, BTN_TL2, BTN_TR2, core.BTN_MODE, BTN_THUMBL):
            self.press(raw)                         # X, Y, View, L3, R3 and two unknown ones: nothing in Home
            self.press(raw, 0)
        self.assertEqual(self.actions, [])


if __name__ == "__main__":
    unittest.main()
