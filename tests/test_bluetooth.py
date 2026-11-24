"""The Bluetooth model: how devices are grouped, ordered and named for the window."""

import ast
import dataclasses
import unittest
from pathlib import Path

from gaminghome import bluetooth as bt


def device(number, name="", **fields):
    return bt.Device(number, name, **fields)


class OrderingTest(unittest.TestCase):
    def test_remembered_lists_devices_by_name_without_regard_to_case_and_whether_they_are_connected(self):
        devices = [device(1, "zeta", paired=True), device(2, "Alpha", paired=True),
                   device(3, "mu", paired=True, connected=True), device(4, "beta", paired=True),
                   device(5, "Nearby", paired=False)]
        self.assertEqual([d.name for d in bt.remembered(devices)], ["Alpha", "beta", "mu", "zeta"])

    def test_a_remembered_device_keeps_its_place_when_it_connects(self):
        before = [device(1, "Alpha", paired=True), device(2, "Beta", paired=True)]
        after = [device(1, "Alpha", paired=True), device(2, "Beta", paired=True, connected=True)]
        self.assertEqual([d.id for d in bt.remembered(before)], [d.id for d in bt.remembered(after)])

    def test_nearby_keeps_first_seen_order_and_leaves_out_remembered_and_anonymous_devices(self):
        devices = [device(1, "second", order=2), device(2, "first", order=1), device(3, "", order=0),
                   device(4, "paired", paired=True, order=3), device(5, "third", order=3)]
        self.assertEqual([d.name for d in bt.nearby(devices)], ["first", "second", "third"])

    def test_the_ids_break_ties_so_the_order_never_depends_on_how_devices_arrived(self):
        one, two = device(1, "same", paired=True), device(2, "same", paired=True)
        self.assertEqual(bt.remembered([two, one]), [one, two])

    def test_connected_is_the_remembered_devices_that_are_connected(self):
        devices = [device(1, "x", paired=True, connected=True), device(2, "y", paired=True),
                   device(3, "z", connected=True)]
        self.assertEqual([d.name for d in bt.connected(devices)], ["x"])


class LabelsTest(unittest.TestCase):
    def test_a_device_without_a_name_gets_the_placeholder(self):
        self.assertEqual(bt.labels([device(1, "Pad"), device(2)], "Unnamed device"),
                         {1: "Pad", 2: "Unnamed device"})

    def test_devices_with_the_same_name_are_told_apart_by_a_number_and_not_by_an_address(self):
        devices = [device(1, "Buds"), device(2, "buds"), device(3, "Pad"), device(4, "Buds")]
        self.assertEqual(bt.labels(devices, "?"), {1: "Buds", 2: "buds (2)", 3: "Pad", 4: "Buds (3)"})

    def test_a_name_that_already_looks_like_a_number_does_not_make_two_labels_alike(self):
        devices = [device(1, "Buds"), device(2, "Buds (2)"), device(3, "Buds")]
        shown = bt.labels(devices, "?")
        self.assertEqual(len(set(label.casefold() for label in shown.values())), 3)

    def test_the_first_device_keeps_the_plain_name(self):
        self.assertEqual(bt.labels([device(7, "Pad")], "?"), {7: "Pad"})


class DisplayNameTest(unittest.TestCase):
    def test_an_ordinary_name_is_kept(self):
        self.assertEqual(bt.display_name("WH-1000XM4"), "WH-1000XM4")
        self.assertEqual(bt.display_name("Xbox Wireless Controller"), "Xbox Wireless Controller")

    def test_the_alias_bluez_makes_out_of_an_address_gives_way_to_the_name(self):
        self.assertEqual(bt.display_name("AA-BB-CC-DD-EE-FF", "Desk Speaker"), "Desk Speaker")
        self.assertEqual(bt.display_name("aa-bb-cc-dd-ee-ff", "Desk Speaker"), "Desk Speaker")

    def test_a_device_that_has_only_an_address_for_a_name_has_none(self):
        for text in ("AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:FF", "aa_bb_cc_dd_ee_ff", "  AA-BB-CC-DD-EE-FF  "):
            with self.subTest(text=text):
                self.assertEqual(bt.display_name(text, text), "")

    def test_an_address_inside_a_name_is_cut_out_with_the_brackets_it_leaves_empty(self):
        self.assertEqual(bt.display_name("Buds (AA:BB:CC:DD:EE:FF)"), "Buds")
        self.assertEqual(bt.display_name("Speaker AA-BB-CC-DD-EE-FF mini"), "Speaker mini")
        self.assertEqual(bt.display_name("Pad [aa_bb_cc_dd_ee_ff] 2"), "Pad 2")

    def test_words_that_only_look_a_little_like_an_address_stay(self):
        for text in ("Room 12-34-56", "AA-BB-CC-DD-EE", "Mi Band 7", "ABCDEF123456"):
            with self.subTest(text=text):
                self.assertEqual(bt.display_name(text), text)

    def test_control_characters_and_text_direction_tricks_are_removed(self):
        self.assertEqual(bt.display_name("Pad\x00\x07\x1b[31m"), "Pad[31m")
        self.assertEqual(bt.display_name("Pa\u202ed\u2066x\u2069\u200f\ufeff\u200b"), "Padx")
        self.assertEqual(bt.display_name("\ud800Pad\ue000"), "Pad")

    def test_line_breaks_and_runs_of_white_space_become_one_space(self):
        self.assertEqual(bt.display_name("  My \n\t Pad\u2028  Two\u00a0 "), "My Pad Two")

    def test_a_name_is_cut_to_the_length_the_window_has_room_for(self):
        name = bt.display_name("x" * 500)
        self.assertEqual(len(name), bt.NAME_MAX)
        self.assertEqual(bt.display_name("word " * 40), ("word " * 40)[:bt.NAME_MAX].rstrip())

    def test_writing_that_needs_zero_width_joiners_and_symbols_survives(self):
        self.assertEqual(bt.display_name("می\u200cخواهم"), "می\u200cخواهم")
        self.assertEqual(bt.display_name("🎮"), "🎮")
        self.assertEqual(bt.display_name("ヘッドホン"), "ヘッドホン")

    def test_punctuation_alone_is_not_a_name(self):
        self.assertEqual(bt.display_name("()", "...", "  "), "")

    def test_the_first_candidate_with_something_to_show_wins_and_other_types_are_ignored(self):
        self.assertEqual(bt.display_name("", None, "\x00", "Second", "Third"), "Second")
        self.assertEqual(bt.display_name(), "")


class SnapshotTest(unittest.TestCase):
    def test_only_a_machine_with_an_adapter_has_something_to_switch(self):
        states = {state: bt.Snapshot(state).usable for state in
                  (bt.UNKNOWN, bt.ABSENT, bt.NO_ADAPTER, bt.OFF, bt.ON, bt.ERROR)}
        self.assertEqual(states, {bt.UNKNOWN: False, bt.ABSENT: False, bt.NO_ADAPTER: False, bt.OFF: True,
                                  bt.ON: True, bt.ERROR: False})

    def test_the_header_has_a_button_for_an_adapter_and_for_a_service_that_could_not_be_read(self):
        states = {state: bt.Snapshot(state).visible for state in
                  (bt.UNKNOWN, bt.ABSENT, bt.NO_ADAPTER, bt.OFF, bt.ON, bt.ERROR)}
        self.assertEqual(states, {bt.UNKNOWN: False, bt.ABSENT: False, bt.NO_ADAPTER: False, bt.OFF: True,
                                  bt.ON: True, bt.ERROR: True})

    def test_a_snapshot_cannot_be_changed_in_place(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            bt.Snapshot().scanning = True

    def test_every_failure_code_and_every_stage_is_distinct(self):
        self.assertEqual(len(set(bt.FAILURES)), len(bt.FAILURES))
        self.assertEqual(len(set(bt.STAGES)), len(bt.STAGES))

    def test_a_failure_says_how_far_a_pairing_got(self):
        failure = bt.Failure(bt.UNREACHABLE, bt.PAIRING, bt.STAGE_CONNECT, 4, "Pad", paired=True)
        self.assertEqual((failure.stage, failure.paired), (bt.STAGE_CONNECT, True))
        self.assertFalse(bt.Failure(bt.FAILED).paired)


class ContractTest(unittest.TestCase):
    def test_the_contract_imports_nothing_but_the_standard_library(self):
        tree = ast.parse(Path(bt.__file__).read_text(encoding="utf-8"))
        modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for alias in node.names}
        modules |= {node.module.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module}
        self.assertEqual(modules, {"__future__", "re", "unicodedata", "dataclasses", "typing"})


if __name__ == "__main__":
    unittest.main()
