"""The battery reading: UPower's DisplayDevice, sysfs, the thresholds and the words for the tooltip.

Everything here runs without GTK, a display, D-Bus or a real battery: sysfs is a scratch tree laid out like the
one on an ASUS ROG Xbox Ally (RC73YA), with its mains adapter, its battery and its two USB ports.
"""

import ast
import json
import math
import os
import shutil
import unittest
from pathlib import Path

from fakes.rc73ya import rc73ya, rc73ya_tree, supply
from gaminghome import battery as battery_module
from gaminghome.battery import (CHARGING, CRITICAL_PERCENT, DISCHARGING, FULL, LOW_PERCENT, MAX_ESTIMATE_S, UNKNOWN,
                                Battery, describe, duration, from_upower, read_sysfs)
from gaminghome.i18n import Translator

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(__file__).resolve().parent / "scratch" / "battery"
ENGLISH = Translator(ROOT / "data" / "locales", "en")


class UPowerTest(unittest.TestCase):
    def test_the_reading_of_a_rog_xbox_ally_on_battery(self):
        self.assertEqual(from_upower(rc73ya()), Battery(78, DISCHARGING, 28173))

    def test_every_state_code_maps_to_what_the_header_shows(self):
        states = {0: UNKNOWN, 1: CHARGING, 2: DISCHARGING, 3: DISCHARGING, 4: FULL, 5: CHARGING, 6: DISCHARGING,
                  7: UNKNOWN, 99: UNKNOWN, -1: UNKNOWN}
        for code, state in states.items():
            self.assertEqual(from_upower(rc73ya(State=code)).state, state, code)
        for junk in (None, "2", 2.0, True, [2]):
            self.assertEqual(from_upower(rc73ya(State=junk)), Battery(78), repr(junk))

    def test_percentage_is_rounded_half_up_and_must_be_a_percentage(self):
        for value, percent in ((77.4, 77), (77.5, 78), (0.0, 0), (0.49, 0), (0.5, 1), (100.0, 100), (100.4, 100),
                               (-0.4, 0), (20, 20), (99.99999999, 100)):
            self.assertEqual(from_upower(rc73ya(Percentage=value)).percent, percent, value)
        for value in (-0.6, 100.5, 250.0, math.nan, math.inf, -math.inf, "78", True, None, [78]):
            self.assertIsNone(from_upower(rc73ya(Percentage=value)), repr(value))
        properties = rc73ya()
        del properties["Percentage"]
        self.assertIsNone(from_upower(properties))

    def test_no_battery_means_nothing_to_show(self):
        # What UPower says on a desktop, in a virtual machine or in CI: IsPresent false, Type 0, no state.
        self.assertIsNone(from_upower(rc73ya(IsPresent=False, Type=0, State=0, Percentage=0.0)))
        for present in (False, 1, "true", None):
            self.assertIsNone(from_upower(rc73ya(IsPresent=present)), repr(present))
        properties = rc73ya()
        del properties["IsPresent"]
        self.assertIsNone(from_upower(properties))

    def test_only_a_battery_counts_not_a_ups_or_anything_else(self):
        for kind in (0, 1, 3, 5, 12, 99):
            self.assertIsNone(from_upower(rc73ya(Type=kind)), kind)
        properties = rc73ya()
        del properties["Type"]
        self.assertEqual(from_upower(properties).percent, 78)       # a reading without a type is not rejected
        self.assertEqual(from_upower(rc73ya(Type="2")).percent, 78)

    def test_the_time_comes_from_the_property_that_fits_the_state(self):
        both = {"TimeToEmpty": 28173, "TimeToFull": 3900}
        self.assertEqual(from_upower(rc73ya(State=1, **both)).seconds, 3900)
        for state in (2, 3, 6):
            self.assertEqual(from_upower(rc73ya(State=state, **both)).seconds, 28173, state)
        for state in (0, 4, 5, 7):                               # unknown, full, pending charge: no estimate
            self.assertIsNone(from_upower(rc73ya(State=state, **both)).seconds, state)
        self.assertIsNone(from_upower(rc73ya(State=2, TimeToEmpty=0)).seconds)
        self.assertIsNone(from_upower(rc73ya(State=1, TimeToFull=0, TimeToEmpty=28173)).seconds)

    def test_an_implausible_estimate_is_not_shown(self):
        # UPower divides energy by a rate that can be near zero.
        self.assertEqual(from_upower(rc73ya(TimeToEmpty=MAX_ESTIMATE_S - 1)).seconds, MAX_ESTIMATE_S - 1)
        for value in (MAX_ESTIMATE_S, 10 ** 12, -5, 0, 1.5, "60", True, None):
            self.assertIsNone(from_upower(rc73ya(TimeToEmpty=value)).seconds, repr(value))

    def test_garbage_never_raises(self):
        for junk in (None, [], "battery", 3, 2.5, (), {}, {"IsPresent": True}, {"IsPresent": True, "Percentage": {}},
                     {"IsPresent": [True], "Type": object(), "State": {}}):
            self.assertIsNone(from_upower(junk), repr(junk))


class ToneTest(unittest.TestCase):
    def test_warnings_apply_only_while_the_battery_is_being_used(self):
        self.assertEqual((LOW_PERCENT, CRITICAL_PERCENT), (20, 10))
        for percent, tone in ((100, ""), (50, ""), (21, ""), (20, "warn"), (11, "warn"), (10, "bad"), (1, "bad"),
                              (0, "bad")):
            self.assertEqual(Battery(percent, DISCHARGING).tone, tone, percent)

    def test_charging_and_full_are_ok_whatever_the_level(self):
        for percent in (0, 5, 10, 20, 21, 100):
            self.assertEqual(Battery(percent, CHARGING).tone, "ok", percent)
            self.assertEqual(Battery(percent, FULL).tone, "ok", percent)

    def test_an_unknown_state_is_never_a_warning(self):
        for percent in (0, 5, 15, 78):
            self.assertEqual(Battery(percent).tone, "", percent)

    def test_the_upower_and_sysfs_readings_agree_on_the_edges(self):
        for percent in (21, 20, 11, 10):
            self.assertEqual(from_upower(rc73ya(Percentage=float(percent))).tone,
                             Battery(percent, DISCHARGING).tone, percent)


class WordsTest(unittest.TestCase):
    def test_the_tooltips_the_header_promises(self):
        self.assertEqual(describe(Battery(64, CHARGING, 3900), ENGLISH), "Charging · 1 h 05 min until full")
        self.assertEqual(describe(Battery(78, DISCHARGING, 28173), ENGLISH), "On battery · 7 h 49 min left")
        self.assertEqual(describe(Battery(100, FULL), ENGLISH), "Fully charged")

    def test_the_time_is_left_out_when_it_is_not_known(self):
        self.assertEqual(describe(Battery(64, CHARGING), ENGLISH), "Charging")
        self.assertEqual(describe(Battery(64, CHARGING, 0), ENGLISH), "Charging")
        self.assertEqual(describe(Battery(78, DISCHARGING, MAX_ESTIMATE_S), ENGLISH), "On battery")
        self.assertEqual(describe(Battery(100, FULL, 3900), ENGLISH), "Fully charged")   # nothing is left to wait for

    def test_an_unknown_state_has_no_words(self):
        self.assertIsNone(describe(Battery(78), ENGLISH))
        self.assertIsNone(describe(None, ENGLISH))

    def test_durations_round_down_but_never_to_nothing(self):
        for seconds, text in ((1, "1 min"), (59, "1 min"), (60, "1 min"), (119, "1 min"), (3599, "59 min"),
                              (3600, "1 h 00 min"), (3900, "1 h 05 min"), (28173, "7 h 49 min"),
                              (MAX_ESTIMATE_S - 1, "99 h 59 min")):
            self.assertEqual(duration(seconds, ENGLISH), text, seconds)
        for seconds in (0, -1, MAX_ESTIMATE_S, None, True, 1.5, "60"):
            self.assertIsNone(duration(seconds, ENGLISH), repr(seconds))

    def test_every_language_says_it_without_leaving_a_placeholder(self):
        for path in sorted((ROOT / "data" / "locales").glob("*.json")):
            translator = Translator(path.parent, path.stem)
            for battery in (Battery(64, CHARGING, 3900), Battery(78, DISCHARGING, 28173), Battery(90, CHARGING, 1800),
                            Battery(100, FULL)):
                text = describe(battery, translator)
                self.assertTrue(text and "{" not in text and "}" not in text, f"{path.stem}: {text!r}")
            self.assertIn("05", describe(Battery(64, CHARGING, 3900), translator), path.stem)
            self.assertIn("49", describe(Battery(78, DISCHARGING, 28173), translator), path.stem)

    def test_the_battery_strings_exist_in_every_language_file(self):
        keys = {key for key in json.loads((ROOT / "data/locales/en.json").read_text(encoding="utf-8"))
                if key.startswith("battery.")}
        self.assertEqual(len(keys), 7)
        for path in (ROOT / "data" / "locales").glob("*.json"):
            self.assertLessEqual(keys, set(json.loads(path.read_text(encoding="utf-8"))), path.name)


class SysfsTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.root = SCRATCH / "power_supply"
        self.root.mkdir()

    def read(self):
        return read_sysfs(str(self.root))

    def test_the_rog_xbox_ally_has_one_battery_and_three_things_that_are_not(self):
        rc73ya_tree(self.root)
        self.assertEqual(self.read(), Battery(78, DISCHARGING))

    def test_the_kernels_status_words(self):
        for status, state in (("Charging", CHARGING), ("Discharging", DISCHARGING), ("Full", FULL),
                              ("Not charging", CHARGING), ("Empty", DISCHARGING), ("Unknown", UNKNOWN),
                              ("charging", CHARGING), (" FULL ", FULL), ("garbage", UNKNOWN), ("", UNKNOWN)):
            shutil.rmtree(self.root)
            self.root.mkdir()
            rc73ya_tree(self.root, status=status)
            self.assertEqual(self.read(), Battery(78, state), repr(status))

    def test_a_battery_without_a_status_file_is_still_a_battery(self):
        supply(self.root, "BAT0", type="Battery", capacity="55")
        self.assertEqual(self.read(), Battery(55, UNKNOWN))

    def test_peripherals_are_not_the_machines_battery(self):
        supply(self.root, "hid-0003:045E:0B00.0007-battery", type="Battery", scope="Device", status="Discharging",
               capacity="3")
        supply(self.root, "ps-controller-battery-aa", type="Battery", scope="device", capacity="40")
        self.assertIsNone(self.read())
        rc73ya_tree(self.root)
        self.assertEqual(self.read(), Battery(78, DISCHARGING))     # and they do not change the answer

    def test_scope_system_and_a_missing_scope_both_count(self):
        supply(self.root, "BAT0", type="Battery", scope="System", capacity="40", status="Charging")
        self.assertEqual(self.read(), Battery(40, CHARGING))
        shutil.rmtree(self.root / "BAT0")
        supply(self.root, "BAT0", type="Battery", scope="Unknown", capacity="41")
        self.assertEqual(self.read().percent, 41)

    def test_an_empty_battery_bay_is_not_a_battery(self):
        supply(self.root, "BAT0", type="Battery", present="0", capacity="0", status="Unknown")
        self.assertIsNone(self.read())

    def test_two_batteries_count_by_their_size(self):
        supply(self.root, "BAT0", type="Battery", capacity="50", status="Discharging", energy_full="60000000")
        supply(self.root, "BAT1", type="Battery", capacity="100", status="Full", energy_full="20000000")
        self.assertEqual(self.read(), Battery(63, DISCHARGING))     # (30 + 20) Wh of 80 Wh is 62.5 %

    def test_batteries_sized_in_amp_hours_count_by_their_size_too(self):
        supply(self.root, "BAT0", type="Battery", capacity="10", status="Charging", charge_full="3000000")
        supply(self.root, "BAT1", type="Battery", capacity="90", status="Discharging", charge_full="1000000")
        self.assertEqual(self.read(), Battery(30, CHARGING))        # charging wins over discharging

    def test_without_every_size_in_one_unit_they_are_averaged(self):
        supply(self.root, "BAT0", type="Battery", capacity="50", status="Full", energy_full="60000000")
        supply(self.root, "BAT1", type="Battery", capacity="100", status="Full")
        self.assertEqual(self.read(), Battery(75, FULL))
        supply(self.root, "BAT2", type="Battery", capacity="25", status="Full", charge_full="2000000")
        self.assertEqual(self.read().percent, 58)                    # (50 + 100 + 25) / 3 = 58.3

    def test_several_batteries_agree_on_one_state(self):
        for statuses, state in ((("Full", "Full"), FULL), (("Full", "Unknown"), UNKNOWN),
                                (("Not charging", "Full"), CHARGING), (("Not charging", "Discharging"), DISCHARGING),
                                (("Discharging", "Charging"), CHARGING), (("Empty", "Empty"), DISCHARGING)):
            shutil.rmtree(self.root)
            self.root.mkdir()
            for index, status in enumerate(statuses):
                supply(self.root, f"BAT{index}", type="Battery", capacity="50", status=status)
            self.assertEqual(self.read().state, state, statuses)

    def test_a_capacity_that_is_not_a_percentage_is_not_a_reading(self):
        for text in ("", "abc", "101", "-5", "7 8", "78.5", "٧٨", "0x4e", "9" * 40):
            shutil.rmtree(self.root, ignore_errors=True)
            self.root.mkdir()
            supply(self.root, "BAT1", type="Battery", capacity=text, status="Discharging")
            self.assertIsNone(self.read(), repr(text))
        for text in ("0", "100", "007"):
            shutil.rmtree(self.root)
            self.root.mkdir()
            supply(self.root, "BAT1", type="Battery", capacity=text)
            self.assertEqual(self.read().percent, int(text))

    def test_one_unreadable_battery_does_not_hide_the_other(self):
        supply(self.root, "BAT0", type="Battery", capacity="garbage")
        supply(self.root, "BAT1", type="Battery", capacity="64", status="Discharging")
        self.assertEqual(self.read(), Battery(64, DISCHARGING))

    def test_nothing_to_read_is_no_battery(self):
        self.assertIsNone(self.read())                                           # an empty directory
        supply(self.root, "ACAD", type="Mains", online="1")
        supply(self.root, "ucsi-source-psy-USBC000:001", type="USB", scope="Device", status="Not charging")
        supply(self.root, "ups0", type="UPS", capacity="90")
        supply(self.root, "empty")                                               # not a single attribute
        self.assertIsNone(self.read())
        self.assertIsNone(read_sysfs(str(SCRATCH / "no-such-directory")))
        (SCRATCH / "a-file").write_text("not a directory", encoding="utf-8")
        self.assertIsNone(read_sysfs(str(SCRATCH / "a-file")))

    def test_real_sysfs_entries_are_symlinks(self):
        devices = SCRATCH / "devices"
        supply(devices, "BAT1", type="Battery", capacity="64", status="Charging")
        os.symlink(devices / "BAT1", self.root / "BAT1")
        os.symlink(SCRATCH / "nowhere", self.root / "BAT0")                      # a link that leads nowhere
        self.assertEqual(self.read(), Battery(64, CHARGING))

    def test_unreadable_and_binary_files_are_no_reading_and_no_error(self):
        directory = supply(self.root, "BAT0", type="Battery", status="Charging")
        (directory / "capacity").write_bytes(b"\xff\xfe\x00\x00 78 \x00")
        self.assertIsNone(self.read())
        (directory / "capacity").write_text("60\n", encoding="utf-8")
        (directory / "status").write_bytes(b"\xff\xff\xff")
        self.assertEqual(self.read(), Battery(60, UNKNOWN))
        (directory / "status").unlink()
        (directory / "status").mkdir()                                           # a directory where a file belongs
        self.assertEqual(self.read(), Battery(60, UNKNOWN))
        if os.geteuid() != 0:
            (directory / "capacity").chmod(0)
            self.assertIsNone(self.read())

    def test_only_the_first_entries_are_looked_at(self):
        for index in range(battery_module.MAX_SUPPLIES):
            supply(self.root, f"AC{index:03d}", type="Mains")
        supply(self.root, "ZZ", type="Battery", capacity="50")
        self.assertIsNone(self.read())


class GtkFreeTest(unittest.TestCase):
    def test_this_module_needs_neither_gtk_nor_glib_nor_a_bus(self):
        source = Path(battery_module.__file__).read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertFalse(modules & {"gi", "Gtk", "GLib", "Gio", "dbus"}, modules)


if __name__ == "__main__":
    unittest.main()
