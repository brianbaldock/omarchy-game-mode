"""The Bluetooth button and sheet without a display: what they show, what their controls do, and that every way out
of the sheet ends its search and its pairing. The window's own methods run on stand-ins, as in test_headerstatus,
because the real window needs a display; the source is always the scripted FakeBluetooth."""

import ast
import importlib
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes.fake_bluetooth import FakeBluetooth
from gaminghome import bluetooth as bt
from gaminghome import bluetoothsheet as sheet
from gaminghome.bluetoothsheet import (BluetoothSheet, DigitEntry, button_view, choose_focus, control_states,
                                       failure_message, opening_focus)
from gaminghome.i18n import Translator

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "data" / "locales"
ENGLISH = Translator(LOCALES, "en", {"LANG": "C"}, Path("/nonexistent/language"))


def world(state=bt.ON):
    """A started source with the three remembered devices most tests use: ids 1 (connected), 2 and 3."""
    source = FakeBluetooth(state=state)
    source.add_device("Wireless Controller", "gamepad", paired=True, connected=state == bt.ON, battery=80)
    source.add_device("Studio Headphones", "headphones", paired=True)
    source.add_device("Desk Keyboard", "keyboard", paired=True)
    source.start()
    return source


def opened(source):
    """The sheet opened on this source, told of each change as the window tells it."""
    visit = BluetoothSheet(source)
    source.on_change = visit.sync
    visit.enter()
    return visit


def texts(blocks):
    return [getattr(block, "text", "") for block in blocks]


def controls(blocks):
    return dict(control_states(blocks))


def prompt_for(kind, max_length=0, number=7):
    return bt.Prompt(number, kind, 4, "Pad", max_length=max_length)


class DigitEntryTest(unittest.TestCase):
    def entry(self, kind=bt.PASSKEY, max_length=6):
        entry = DigitEntry()
        entry.begin(prompt_for(kind, max_length))
        return entry

    def test_the_digits_are_a_string_and_keep_their_leading_zeros(self):
        entry = self.entry()
        for digit in "007":
            entry.press(digit)
        self.assertEqual(entry.digits, "007")

    def test_no_more_digits_than_the_prompt_allows(self):
        entry = self.entry(bt.PIN, max_length=4)
        self.assertEqual([entry.press(d) for d in "12345"], [True, True, True, True, False])
        self.assertEqual(entry.digits, "1234")

    def test_the_kind_caps_what_a_prompt_allows_and_fills_in_what_it_leaves_out(self):
        self.assertEqual(DigitEntry.limit_for(prompt_for(bt.PASSKEY, 9)), bt.PASSKEY_MAX)
        self.assertEqual(DigitEntry.limit_for(prompt_for(bt.PIN, 40)), bt.PIN_MAX)
        self.assertEqual(DigitEntry.limit_for(prompt_for(bt.PIN, 0)), bt.PIN_MAX)
        self.assertEqual(DigitEntry.limit_for(prompt_for(bt.PASSKEY, 0)), bt.PASSKEY_MAX)

    def test_erasing_takes_the_last_digit_and_nothing_from_an_empty_entry(self):
        entry = self.entry()
        entry.press("4")
        entry.press("2")
        self.assertTrue(entry.erase())
        self.assertEqual(entry.digits, "4")
        self.assertTrue(entry.erase())
        self.assertFalse(entry.erase())
        self.assertEqual(entry.digits, "")

    def test_done_needs_one_digit(self):
        entry = self.entry()
        self.assertFalse(entry.ready)
        entry.press("0")
        self.assertTrue(entry.ready)

    def test_only_one_ascii_digit_at_a_time_is_taken(self):
        entry = self.entry(bt.PIN, 16)
        for key in ("", "12", "a", "\u0663", "\uff13", " "):
            self.assertFalse(entry.press(key), repr(key))
        self.assertEqual(entry.digits, "")

    def test_a_passkey_shows_a_blank_for_each_digit_to_come_and_a_pin_does_not(self):
        passkey, pin = self.entry(), self.entry(bt.PIN, 16)
        for entry in (passkey, pin):
            entry.press("0")
            entry.press("4")
        self.assertEqual((passkey.shown, pin.shown), ("04____", "04"))

    def test_the_digits_stay_out_of_its_repr(self):
        entry = self.entry()
        for digit in "918273":
            entry.press(digit)
        self.assertNotIn("918273", repr(entry))
        pad = sheet.Pad("prompt:7", display=entry.shown)
        self.assertNotIn("918273", repr(pad))


class FailureMessageTest(unittest.TestCase):
    def test_the_words_come_from_the_action_and_the_reason(self):
        message = failure_message(bt.Failure(bt.UNREACHABLE, bt.CONNECTING, device=2, name="Buds"), "Buds")
        self.assertEqual((message.action, message.reason, message.hint, message.name),
                         ("bluetooth.failed.connecting", "bluetooth.reason.unreachable", "", "Buds"))
        self.assertEqual(message.text(ENGLISH), "Couldn't connect to Buds. The device didn't answer. Is it on and "
                                                "close by?")

    def test_a_device_paired_that_did_not_connect_is_said_to_be_paired_and_connect_is_pointed_at(self):
        failure = bt.Failure(bt.UNREACHABLE, bt.PAIRING, bt.STAGE_CONNECT, 5, "Speaker", paired=True)
        message = failure_message(failure, "Speaker")
        self.assertEqual((message.action, message.hint), ("bluetooth.failed.paired", "bluetooth.failed.pairedHint"))
        self.assertIn("was paired but did not connect", message.text(ENGLISH))
        self.assertIn("Connect", ENGLISH(message.hint))

    def test_a_failure_the_window_does_not_know_still_says_something_plain(self):
        message = failure_message(bt.Failure("something-new", "something-else"), "Pad")
        self.assertEqual((message.action, message.reason), ("bluetooth.failed.other", "bluetooth.reason.failed"))
        self.assertEqual(failure_message(bt.Failure(bt.BLOCKED, bt.SEARCHING), "").action,
                         "bluetooth.failed.searching")

    def test_every_action_and_reason_has_words_in_every_language(self):
        keys = {f"bluetooth.reason.{code}" for code in bt.FAILURES} | {
            f"bluetooth.failed.{action}" for action in sheet.ACTIONS} | {
            "bluetooth.failed.other", "bluetooth.failed.paired", "bluetooth.failed.pairedHint", "bluetooth.failure"}
        for path in sorted(LOCALES.glob("*.json")):
            strings = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(keys - set(strings), set(), path.name)
            translator = Translator(LOCALES, path.stem)
            for code in bt.FAILURES:
                for action in sheet.ACTIONS:
                    text = failure_message(bt.Failure(code, action, name="Pad"), "Pad").text(translator)
                    self.assertFalse(re.search(r"[{}]", text), f"{path.stem}: {text}")

    def test_no_word_of_the_plumbing_reaches_the_user(self):
        jargon = re.compile(r"bluez|rfkill|d-?bus|hci\d|adapter|address", re.I)
        for path in sorted(LOCALES.glob("*.json")):
            strings = json.loads(path.read_text(encoding="utf-8"))
            for key, text in strings.items():
                if key.startswith("bluetooth."):
                    self.assertIsNone(jargon.search(text), f"{path.name}: {key}: {text}")


class FocusTest(unittest.TestCase):
    CONTROLS = [("bt:dismiss", True), ("bt:power", True), ("bt:connect:1", True), ("bt:forget:1", True),
                ("bt:search", True), ("bt:pair:9", True), ("bt:close", True)]

    def test_the_focus_stays_on_the_control_of_the_same_name(self):
        self.assertEqual(choose_focus("bt:connect:1", self.CONTROLS), "bt:connect:1")

    def test_a_control_disabled_while_something_runs_keeps_its_claim_on_the_focus(self):
        busy = [(name, name in ("bt:search", "bt:close", "bt:dismiss")) for name, _ in self.CONTROLS]
        self.assertIsNone(choose_focus("bt:connect:1", busy))
        self.assertEqual(choose_focus("bt:connect:1", self.CONTROLS), "bt:connect:1")

    def test_when_the_control_is_gone_the_focus_goes_to_search_then_close(self):
        self.assertEqual(choose_focus("bt:pair:4", self.CONTROLS), "bt:search")
        without_search = [item for item in self.CONTROLS if item[0] != "bt:search"]
        self.assertEqual(choose_focus("bt:forget-yes:3", without_search), "bt:close")

    def test_the_focus_never_falls_on_forget_or_pair(self):
        only = [("bt:forget:1", True), ("bt:pair:9", True), ("bt:forget-yes:2", True), ("bt:pad:1", True)]
        self.assertEqual(choose_focus("bt:connect:7", only), "bt:pad:1")
        self.assertIsNone(choose_focus("bt:connect:7", only[:3]))

    def test_a_part_that_appears_says_where_the_focus_starts_unless_that_is_disabled(self):
        panel = [("bt:yes", True), ("bt:no", True), ("bt:close", True)]
        self.assertEqual(choose_focus("bt:pair:9", panel, first="bt:no"), "bt:no")
        self.assertEqual(choose_focus("bt:close", [("bt:pad:done", False), ("bt:close", True)], "bt:pad:done"),
                         "bt:close")

    def test_the_sheet_opens_on_a_control_that_changes_no_connection(self):
        self.assertEqual(opening_focus(self.CONTROLS), "bt:search")
        self.assertEqual(opening_focus([("bt:power", True), ("bt:close", True)]), "bt:power")
        self.assertEqual(opening_focus([("bt:retry", True), ("bt:close", True)]), "bt:retry")
        self.assertEqual(opening_focus([("bt:power", False), ("bt:close", True)]), "bt:close")


class LifecycleTest(unittest.TestCase):
    def test_starting_and_stopping_reach_the_source_and_nothing_is_reported_after_stop(self):
        source = FakeBluetooth()
        visit = BluetoothSheet(source)
        visit.start()
        visit.start()
        self.assertEqual((source.running, source.changes), (True, 1))
        visit.stop()
        visit.stop()
        self.assertFalse(source.running)
        changes = source.changes
        source.add_device("Pad", "gamepad")
        self.assertEqual(source.changes, changes)

    def test_leaving_ends_the_search_even_when_none_is_known_to_run(self):
        source = world()
        visit = opened(source)
        self.assertTrue(visit.leave())
        self.assertEqual(source.calls[-1:], [("scan", False)])
        self.assertNotIn(("cancel",), source.calls)

    def test_leaving_abandons_a_prompt_and_a_pairing_that_asks_nothing(self):
        for prompt in (True, False):
            with self.subTest(prompt=prompt):
                source = world()
                visit = opened(source)
                visit.press(sheet.SEARCH)
                visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
                if prompt:
                    source.show_prompt(bt.PASSKEY, max_length=6)
                visit.leave()
                self.assertEqual(source.calls[-2:], [("scan", False), ("cancel",)])
                self.assertIsNone(source.snapshot.activity)

    def test_leaving_lets_a_connection_go_on(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.CONNECT}2")
        visit.leave()
        self.assertNotIn(("cancel",), source.calls)
        self.assertEqual(source.snapshot.activity.kind, bt.CONNECTING)

    def test_leaving_twice_or_without_entering_calls_nothing(self):
        source = world()
        visit = BluetoothSheet(source)
        self.assertFalse(visit.leave())
        visit.enter()
        visit.leave()
        calls = list(source.calls)
        self.assertFalse(visit.leave())
        self.assertEqual(source.calls, calls)

    def test_stopping_leaves_first(self):
        source = world()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        visit.stop()
        self.assertEqual(source.calls[-2:], [("scan", False), ("stop",)])
        self.assertFalse(visit.visiting)

    def test_what_a_visit_remembers_ends_with_it(self):
        source = world()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        visit.press(f"{sheet.FORGET}3")
        visit.leave()
        visit.enter()
        self.assertEqual((visit.searched, visit.forgetting), (False, 0))


class ControlsTest(unittest.TestCase):
    def test_connect_and_disconnect_follow_the_device_as_it_is_when_pressed(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.CONNECT}1")
        source.finish()
        visit.press(f"{sheet.CONNECT}2")
        self.assertEqual([call for call in source.calls if call[0] in ("connect", "disconnect")],
                         [("disconnect", 1), ("connect", 2)])

    def test_nothing_that_the_contract_ignores_while_busy_is_sent_while_busy(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.CONNECT}2")
        before = list(source.calls)
        for name in (sheet.POWER, f"{sheet.CONNECT}3", f"{sheet.FORGET}3", f"{sheet.PAIR}2"):
            visit.press(name)
        self.assertEqual(source.calls, before)
        self.assertEqual(visit.forgetting, 0)

    def test_forget_asks_first_and_forgets_the_device_of_the_question(self):
        source = world()
        visit = opened(source)
        self.assertIsNone(visit.press(f"{sheet.FORGET}3"))
        self.assertEqual(visit.forgetting, 3)
        self.assertNotIn(("forget", 3), source.calls)
        visit.press(f"{sheet.FORGET_YES}3")
        self.assertEqual((source.calls[-1], visit.forgetting), (("forget", 3), 0))

    def test_cancel_and_b_take_the_question_back_and_return_to_forget(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.FORGET}3")
        self.assertEqual(visit.press(f"{sheet.FORGET_NO}3"), f"{sheet.FORGET}3")
        visit.press(f"{sheet.FORGET}2")
        self.assertEqual(visit.back(), (True, f"{sheet.FORGET}2"))
        self.assertEqual(visit.forgetting, 0)
        self.assertFalse(any(call[0] == "forget" for call in source.calls))

    def test_a_question_about_a_device_that_went_away_goes_with_it(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.FORGET}3")
        source.remove_device(3)
        visit.sync()
        self.assertEqual(visit.forgetting, 0)

    def test_b_gives_up_a_pairing_and_otherwise_lets_the_sheet_close(self):
        source = world()
        visit = opened(source)
        self.assertEqual(visit.back(), (False, None))
        visit.press(sheet.SEARCH)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        self.assertEqual(visit.back(), (True, None))
        self.assertEqual(source.calls[-1], ("cancel",))

    def test_the_power_switch_asks_for_the_other_state(self):
        for state, wanted in ((bt.ON, False), (bt.OFF, True)):
            source = world(state)
            opened(source).press(sheet.POWER)
            self.assertEqual(source.calls[-1], ("set_power", wanted))

    def test_search_starts_and_stops_and_only_while_bluetooth_is_on(self):
        source = world()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        visit.press(sheet.SEARCH)
        self.assertEqual([call for call in source.calls if call[0] == "scan"], [("scan", True), ("scan", False)])
        off = world(bt.OFF)
        opened(off).press(sheet.SEARCH)
        self.assertNotIn(("scan", True), off.calls)

    def test_a_confirmation_is_answered_for_its_own_prompt(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        number = source.show_prompt(bt.CONFIRM, code="042917")
        visit.press(sheet.NO)
        self.assertEqual(source.calls[-1], ("answer", number, False))

    def test_the_pad_answers_with_the_digits_as_a_string_and_forgets_them(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        number = source.show_prompt(bt.PIN, max_length=16)
        visit.sync()
        for key in ("0", "0", "4", "erase", "7"):
            visit.press(f"{sheet.PAD}{key}")
        visit.press(sheet.PAD_DONE)
        self.assertEqual(source.calls[-1], ("answer", number, "007"))
        self.assertEqual((visit.entry.digits, visit.entry.prompt), ("", None))

    def test_done_does_nothing_before_the_first_digit(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        source.show_prompt(bt.PASSKEY, max_length=6)
        visit.sync()
        visit.press(sheet.PAD_DONE)
        self.assertFalse(any(call[0] == "answer" for call in source.calls))

    def test_the_digits_of_a_prompt_that_ended_or_changed_are_dropped(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        source.show_prompt(bt.PIN, max_length=16)
        visit.sync()
        visit.press(f"{sheet.PAD}5")
        source.show_prompt(bt.PIN, max_length=16)                  # the device asks again: a new prompt
        visit.sync()
        self.assertEqual(visit.entry.digits, "")
        visit.press(f"{sheet.PAD}5")
        source.fail(bt.AUTH_FAILED)
        visit.sync()
        self.assertEqual((visit.entry.digits, visit.entry.prompt), ("", None))

    def test_the_keyboard_types_on_the_pad_and_moves_the_focus_so_that_enter_answers(self):
        source = world()
        visit = opened(source)
        self.assertIsNone(visit.key("4"))                           # no pad, no typing
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        source.show_prompt(bt.PASSKEY, max_length=6)
        visit.sync()
        self.assertEqual(visit.key("4"), sheet.PAD_DONE)
        self.assertIsNone(visit.key("x"))
        self.assertEqual(visit.key("erase"), sheet.PAD_ERASE)       # Done is disabled again
        self.assertEqual(visit.entry.digits, "")

    def test_an_answer_to_a_prompt_that_is_no_longer_asked_is_not_sent(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        source.show_prompt(bt.CONFIRM, code="111111")
        source.answer(source.snapshot.prompt.id, True)              # answered elsewhere meanwhile
        visit.press(sheet.YES)
        self.assertEqual(sum(call[0] == "answer" for call in source.calls), 1)


class BlocksTest(unittest.TestCase):
    def blocks(self, source, visit=None):
        return (visit or opened(source)).blocks(ENGLISH)

    def test_off_shows_the_switch_and_one_line(self):
        blocks = self.blocks(world(bt.OFF))
        self.assertEqual([block.key for block in blocks], ["title", "power", "off", "close"])
        self.assertIn("Turn Bluetooth on to connect devices.", texts(blocks))

    def test_a_service_that_could_not_be_read_offers_to_try_again_and_no_switch(self):
        blocks = self.blocks(world(bt.ERROR))
        self.assertEqual([block.key for block in blocks], ["title", "retry", "close"])
        self.assertEqual(blocks[1].text, "Couldn't read Bluetooth.")
        self.assertEqual(set(controls(blocks)), {sheet.RETRY, sheet.CLOSE})

    def test_bluetooth_that_went_away_while_the_sheet_is_open_says_so(self):
        source = world()
        visit = opened(source)
        source.set_state(bt.NO_ADAPTER)
        blocks = self.blocks(source, visit)
        self.assertEqual(texts(blocks)[1:2], ["Bluetooth isn't available."])
        self.assertEqual(set(controls(blocks)), {sheet.CLOSE})

    def test_my_devices_say_how_they_are_and_keep_their_place_when_one_connects(self):
        source = world()
        visit = opened(source)
        rows = [block for block in self.blocks(source, visit) if isinstance(block, sheet.Row)]
        self.assertEqual([(row.title, row.status) for row in rows],
                         [("Desk Keyboard", "Not connected"), ("Studio Headphones", "Not connected"),
                          ("Wireless Controller", "Connected · 80%")])
        visit.press(f"{sheet.CONNECT}2")
        rows = [block for block in self.blocks(source, visit) if isinstance(block, sheet.Row)]
        self.assertEqual(rows[1].status, "Connecting…")
        source.finish()
        after = [block for block in self.blocks(source, visit) if isinstance(block, sheet.Row)]
        self.assertEqual([row.key for row in after], [row.key for row in rows])
        self.assertEqual(after[1].controls[0].text, "Disconnect")

    def test_while_something_runs_only_what_the_contract_ignores_is_disabled(self):
        source = world()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        source.add_device("Speaker", "speaker")
        visit.press(f"{sheet.CONNECT}2")
        states = controls(self.blocks(source, visit))
        disabled = {name for name, enabled in states.items() if not enabled}
        self.assertEqual(disabled, {sheet.POWER, "bt:connect:1", "bt:connect:2", "bt:connect:3", "bt:forget:1",
                                    "bt:forget:2", "bt:forget:3", "bt:pair:4"})

    def test_the_nearby_list_appears_only_after_search_was_pressed_in_this_visit(self):
        source = world()
        visit = opened(source)
        source.add_device("Speaker", "speaker")
        self.assertFalse(any(block.key.startswith("nearby:") for block in self.blocks(source, visit)))
        visit.press(sheet.SEARCH)
        self.assertIn("nearby:4", [block.key for block in self.blocks(source, visit)])
        source.set_scanning(False)                                  # the search ends by itself: the list stays
        self.assertIn("nearby:4", [block.key for block in self.blocks(source, visit)])
        visit.leave()
        visit.enter()
        self.assertNotIn("nearby:4", [block.key for block in self.blocks(source, visit)])

    def test_the_lines_of_a_search(self):
        source = world()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        blocks = self.blocks(source, visit)
        self.assertIn("Looking for devices…", texts(blocks))
        self.assertEqual(next(block for block in blocks if block.key == "add").status, "Searching…")
        self.assertEqual(controls(blocks)[sheet.SEARCH], True)
        self.assertEqual(next(block for block in blocks if block.key == "search").controls[0].text, "Stop searching")
        source.set_scanning(False)
        blocks = self.blocks(source, visit)
        self.assertIn("Nothing found. Check that the device is in pairing mode.", texts(blocks))
        self.assertEqual(next(block for block in blocks if block.key == "add").status, "")

    def test_a_search_asked_for_reads_as_one_before_the_source_says_it_runs(self):
        source = world()
        visit = opened(source)
        with mock.patch.object(source, "scan", lambda on: source.calls.append(("scan", on))):
            visit.press(sheet.SEARCH)                               # the source has not answered yet
        self.assertIn("Looking for devices…", texts(self.blocks(source, visit)))
        source.fail(bt.BLOCKED, bt.SEARCHING)
        visit.sync()
        self.assertIn("Nothing found. Check that the device is in pairing mode.", texts(self.blocks(source, visit)))

    def test_more_devices_than_listed_says_so(self):
        source = world()
        visit = opened(source)
        for number in range(bt.MAX_DEVICES):
            source.add_device(f"Sensor {number}", "other")
        self.assertIn("More devices were found than are shown.", texts(self.blocks(source, visit)))

    def test_devices_with_the_same_name_or_none_are_told_apart_without_an_address(self):
        source = FakeBluetooth()
        source.add_device("Buds", "headphones", paired=True)
        source.add_device("Buds", "headphones", paired=True)
        source.add_device("", "other", paired=True)
        source.start()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        source.add_device("Buds", "headphones")
        titles = [block.title for block in self.blocks(source, visit) if isinstance(block, sheet.Row)]
        self.assertEqual(titles, ["Unnamed device", "Buds", "Buds (2)", "Buds (3)"])

    def test_a_pairing_takes_the_place_of_the_lists_and_keeps_the_switch_disabled(self):
        source = world()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        blocks = self.blocks(source, visit)
        self.assertEqual([block.key for block in blocks], ["title", "power", "pairing", "close"])
        self.assertEqual(blocks[2].text, "Pairing with Speaker…")
        self.assertFalse(controls(blocks)[sheet.POWER])
        source.stage(bt.STAGE_TRUST)
        self.assertEqual(self.blocks(source, visit)[2].text, "Connecting to Speaker…")

    def test_a_number_to_compare_starts_on_no_and_promises_nothing_about_who_the_device_is(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        source.show_prompt(bt.CONFIRM, code="042917")
        panel = self.blocks(source, visit)[2]
        self.assertEqual((panel.first, panel.code, [c.name for c in panel.controls]),
                         (sheet.NO, "042917", [sheet.YES, sheet.NO]))
        self.assertEqual(panel.text, "Does this number match the one shown on Speaker?")
        for language in ("en", "de", "es", "fr", "pt-BR", "ja", "ar"):
            words = Translator(LOCALES, language)("bluetooth.confirm", name="X").casefold()
            self.assertFalse(re.search(r"prove|verif|secure|genuine|identit", words), language)

    def test_a_code_to_type_shows_how_far_the_device_got(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        source.show_prompt(bt.CODE, code="314159")
        source.type_digits(2)
        panel = self.blocks(source, visit)[2]
        self.assertEqual((panel.text, panel.typed, panel.first), ("On Speaker, type 314159 and press Enter.", 2,
                                                                  sheet.CANCEL))

    def test_the_pad_is_rows_of_keys_and_then_cancel(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.PAIR}{source.add_device('Speaker', 'speaker')}")
        source.show_prompt(bt.PIN, max_length=16)
        visit.sync()
        pad = self.blocks(source, visit)[2]
        self.assertEqual(pad.rows, (("bt:pad:1", "bt:pad:2", "bt:pad:3"), ("bt:pad:4", "bt:pad:5", "bt:pad:6"),
                                    ("bt:pad:7", "bt:pad:8", "bt:pad:9"),
                                    ("bt:pad:erase", "bt:pad:0", "bt:pad:done"), ("bt:cancel",)))
        self.assertEqual({name for row in pad.rows for name in row}, {c.name for c in pad.controls})
        self.assertEqual((pad.text, pad.first, controls([pad])[sheet.PAD_DONE]),
                         ("Enter the PIN for Speaker.", "bt:pad:1", False))

    def test_switching_on_says_so_even_once_the_state_has_moved(self):
        source = world(bt.OFF)
        visit = opened(source)
        visit.press(sheet.POWER)
        self.assertEqual(next(b for b in self.blocks(source, visit) if b.key == "power").status, "Turning on…")
        source.set_state(bt.ON)                                     # powered, and the call has not returned yet
        visit.sync()
        self.assertEqual(next(b for b in self.blocks(source, visit) if b.key == "power").status, "Turning on…")
        source.finish()
        visit.sync()
        self.assertEqual(next(b for b in self.blocks(source, visit) if b.key == "power").status, "On")

    def test_a_failure_is_a_line_with_dismiss_above_the_rest(self):
        source = world()
        visit = opened(source)
        visit.press(f"{sheet.CONNECT}2")
        source.fail(bt.UNREACHABLE)
        blocks = self.blocks(source, visit)
        self.assertEqual((blocks[1].key, blocks[1].text), (
            "failure", "Couldn't connect to Studio Headphones. The device didn't answer. Is it on and close by?"))
        visit.press(sheet.DISMISS)
        self.assertNotIn("failure", [block.key for block in self.blocks(source, visit)])

    def test_every_part_and_every_control_has_a_name_of_its_own(self):
        source = world()
        visit = opened(source)
        visit.press(sheet.SEARCH)
        for name in ("Buds", "Buds", "Pad"):
            source.add_device(name, "headphones")
        source.fail(bt.TIMEOUT, bt.SEARCHING)
        blocks = self.blocks(source, visit)
        keys, names = [block.key for block in blocks], [name for name, _ in control_states(blocks)]
        self.assertEqual((len(keys), len(names)), (len(set(keys)), len(set(names))))


class ButtonTest(unittest.TestCase):
    def test_the_button_exists_for_an_adapter_and_for_a_service_that_could_not_be_read(self):
        for state, visible in ((bt.UNKNOWN, False), (bt.ABSENT, False), (bt.NO_ADAPTER, False), (bt.OFF, True),
                               (bt.ON, True), (bt.ERROR, True)):
            self.assertEqual(button_view(bt.Snapshot(state), ENGLISH).visible, visible, state)

    def test_the_button_tells_off_on_connected_and_error_apart(self):
        views = {state: button_view(world(state).snapshot, ENGLISH) for state in (bt.OFF, bt.ERROR)}
        views["connected"] = button_view(world().snapshot, ENGLISH)
        views["on"] = button_view(bt.Snapshot(bt.ON), ENGLISH)
        self.assertEqual({key: (view.icon, view.tone) for key, view in views.items()},
                         {bt.OFF: ("bluetooth-off", "off"), bt.ERROR: ("bluetooth-error", "warn"),
                          "connected": ("bluetooth-connected", "connected"), "on": ("bluetooth", "")})
        self.assertEqual(views["connected"].tooltip, "Connected:\nWireless Controller")
        self.assertEqual(views["on"].tooltip, "Bluetooth is on")

    def test_every_icon_the_window_asks_for_is_shipped(self):
        icons = {"bluetooth", "bluetooth-off", "bluetooth-connected", "bluetooth-error", "backspace", "check",
                 "warning"} | {sheet.device_icon(kind) for kind in bt.KINDS}
        for icon in icons:
            self.assertTrue((ROOT / "data" / "icons" / f"gh-{icon}-symbolic.svg").is_file(), icon)


class FakeBluetoothTest(unittest.TestCase):
    """The stand-in keeps the contract's rules, so that tests built on it mean something."""

    def test_operations_are_ignored_while_an_activity_runs_and_still_recorded(self):
        source = world()
        source.connect(2)
        activity = source.snapshot.activity
        for call, args in (("set_power", (False,)), ("connect", (3,)), ("disconnect", (1,)), ("pair", (3,)),
                           ("forget", (3,))):
            getattr(source, call)(*args)
            self.assertEqual(source.calls[-1], (call, *args))
        self.assertEqual(source.snapshot.activity, activity)

    def test_ending_a_search_cancel_and_answer_work_during_an_activity(self):
        source = world()
        source.scan(True)
        source.add_device("Speaker", "speaker")
        source.pair(4)
        self.assertFalse(source.snapshot.scanning)                    # a pairing ends the search
        source.scan(True)
        source.scan(False)
        self.assertFalse(source.snapshot.scanning)
        number = source.show_prompt(bt.CONFIRM, code="123456")
        source.answer(number, True)
        self.assertIsNone(source.snapshot.prompt)
        source.cancel()
        self.assertEqual((source.snapshot.activity, source.snapshot.failure.code), (None, bt.CANCELED))

    def test_an_answer_to_an_old_prompt_is_ignored(self):
        source = world()
        source.pair(2)
        old = source.show_prompt(bt.PIN, max_length=16)
        new = source.show_prompt(bt.PIN, max_length=16)
        source.answer(old, "0000")
        self.assertEqual(source.snapshot.prompt.id, new)

    def test_a_new_operation_clears_the_failure_and_dismiss_does_by_hand(self):
        source = world()
        source.connect(2)
        source.fail(bt.TIMEOUT)
        source.connect(3)
        self.assertIsNone(source.snapshot.failure)
        source.fail(bt.TIMEOUT)
        source.dismiss()
        self.assertIsNone(source.snapshot.failure)

    def test_an_id_that_names_no_device_ends_with_gone(self):
        source = world()
        source.connect(99)
        self.assertEqual((source.snapshot.failure.code, source.snapshot.activity), (bt.GONE, None))

    def test_forgetting_while_off_is_refused(self):
        source = world(bt.OFF)
        source.forget(2)
        self.assertEqual(source.snapshot.failure.code, bt.NOT_READY)
        self.assertEqual(len(bt.remembered(source.snapshot.devices)), 3)

    def test_start_and_stop_are_idempotent_and_nothing_is_reported_after_stop(self):
        changes = []
        source = FakeBluetooth(lambda: changes.append(1))
        source.add_device("Pad", "gamepad")                           # not started: nobody is told
        source.start()
        source.start()
        self.assertEqual(len(changes), 1)
        source.scan(True)
        source.stop()
        source.stop()
        self.assertFalse(source.snapshot.scanning)
        source.add_device("Speaker", "speaker")
        source.set_state(bt.OFF)
        self.assertEqual(len(changes), 2)

    def test_stopping_drops_the_search_the_prompt_and_the_call_in_flight(self):
        source = world()
        source.pair(2)
        source.show_prompt(bt.CONFIRM, code="000000")
        source.stop()
        self.assertEqual((source.snapshot.scanning, source.snapshot.prompt, source.snapshot.activity),
                         (False, None, None))

    def test_at_most_the_contracts_number_of_devices_is_listed_remembered_first(self):
        source = FakeBluetooth()
        for number in range(bt.MAX_DEVICES):
            source.add_device(f"Sensor {number}", "other")
        source.add_device("Pad", "gamepad", paired=True)
        self.assertTrue(source.snapshot.truncated)
        self.assertEqual(len(source.snapshot.devices), bt.MAX_DEVICES)
        self.assertEqual(source.snapshot.devices[0].name, "Pad")

    def test_the_stand_in_owns_no_timer_and_touches_nothing_of_the_machine(self):
        tree = ast.parse((ROOT / "tests" / "fakes" / "fake_bluetooth.py").read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "dataclasses", "gaminghome"})


class WindowTest(unittest.TestCase):
    """The window's Bluetooth glue, run on a stand-in for the window around a real BluetoothSheet and the fake."""

    @classmethod
    def setUpClass(cls):
        importlib.import_module("gaminghome.application")             # pins Gtk 4 before the window module needs it
        cls.window = importlib.import_module("gaminghome.ui").HomeWindow
        cls.source_text = (ROOT / "gaminghome" / "ui.py").read_text(encoding="utf-8")
        tree = next(node for node in ast.parse(cls.source_text).body if isinstance(node, ast.ClassDef)
                    and node.name == "HomeWindow")
        cls.methods = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    def calls(self, name):
        return [node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                for node in ast.walk(self.methods[name]) if isinstance(node, ast.Call)]

    def stand_in(self, source, sheet_name="bluetooth"):
        window = mock.MagicMock()
        window._bluetooth = BluetoothSheet(source)
        window._bluetooth.enter()
        window.sheet = sheet_name
        for name in ("_leave_bluetooth", "close_sheet", "back", "handle_action", "open_settings", "open_app",
                     "_on_unmap", "_on_map", "stop_status"):
            setattr(window, name, getattr(self.window, name).__get__(window))
        return window

    def pairing_world(self):
        source = world()
        source.add_device("Speaker", "speaker")
        source.pair(4)
        source.show_prompt(bt.PIN, max_length=16)
        return source

    def assert_left(self, source, cancelled=True):
        self.assertIn(("scan", False), source.calls)
        self.assertEqual(("cancel",) in source.calls, cancelled)
        self.assertIsNone(source.snapshot.prompt)

    def test_b_and_close_and_escape_leave_through_close_sheet(self):
        from gaminghome.core import BACK
        source = self.pairing_world()
        window = self.stand_in(source)
        window.handle_action(BACK)                                    # first B gives up the pairing
        self.assertEqual((window.sheet, source.calls[-1]), ("bluetooth", ("cancel",)))
        window.handle_action(BACK)                                    # then closes
        self.assertIsNone(window.sheet)
        self.assertFalse(window._bluetooth.visiting)
        self.assert_left(source)

    def test_closing_the_sheet_leaves_it(self):
        source = self.pairing_world()
        window = self.stand_in(source)
        window.close_sheet()
        self.assert_left(source)

    def test_menu_closes_the_bluetooth_sheet_instead_of_opening_settings(self):
        from gaminghome.core import MENU
        source = self.pairing_world()
        window = self.stand_in(source)
        window.handle_action(MENU)
        self.assertIsNone(window.sheet)
        self.assert_left(source)
        window._build_settings_sheet.assert_not_called()

    def test_another_sheet_taking_its_place_leaves_it(self):
        for opening in (lambda window: window.open_settings(), lambda window: window.open_app("steam")):
            source = self.pairing_world()
            window = self.stand_in(source)
            opening(window)
            self.assertNotEqual(window.sheet, "bluetooth")
            self.assert_left(source)

    def test_hiding_the_window_leaves_and_closes_the_sheet_and_stops_bluetooth(self):
        source = self.pairing_world()
        window = self.stand_in(source)
        window._on_unmap()
        self.assertIsNone(window.sheet)
        window.scrim.set_visible.assert_called_with(False)
        self.assert_left(source)
        self.assertEqual((source.running, source.calls[-1]), (False, ("stop",)))

    def test_quitting_leaves_the_sheet_and_stops_bluetooth(self):
        source = self.pairing_world()
        window = self.stand_in(source)
        window.stop_status()
        self.assert_left(source)
        self.assertFalse(source.running)

    def test_showing_the_window_starts_bluetooth(self):
        source = FakeBluetooth()
        window = self.stand_in(source, None)
        window._on_map()
        self.assertTrue(source.running)

    def test_building_the_window_again_leaves_the_sheet_first(self):
        # build() makes widgets and needs a display, so its first lines are read off its source.
        body = self.methods["build"].body
        statements = [ast.get_source_segment(self.source_text, node) for node in body[0:2]]
        self.assertEqual(statements[0], "self._leave_bluetooth()")
        self.assertTrue(statements[1].startswith('if self.sheet == "bluetooth":'))

    def test_every_way_out_goes_through_the_one_cleanup(self):
        for name in ("close_sheet", "open_settings", "open_app", "build", "_on_unmap", "stop_status"):
            self.assertIn("_leave_bluetooth", self.calls(name), name)
        self.assertEqual([name for name, node in self.methods.items() if "leave" in self.calls(name)],
                         ["_leave_bluetooth"])

    def test_the_source_is_made_once_by_the_constructor(self):
        made = [name for name in self.methods if "bluetooth" in self.calls(name)]
        self.assertEqual(made, ["__init__"])
        segment = ast.get_source_segment(self.source_text, self.methods["__init__"])
        self.assertIn("runtime.bluetooth(self._on_bluetooth)", segment)

    def test_a_change_repaints_the_button_and_the_sheet_and_never_the_whole_window(self):
        for name in ("_on_bluetooth", "_paint_bluetooth_button", "_render_bluetooth"):
            self.assertFalse(set(self.calls(name)) & {"refresh", "on_ui_changed", "build", "show_page", "idle_add",
                                                      "timeout_add", "timeout_add_seconds"}, name)

    def test_the_bluetooth_code_of_the_window_owns_no_timer_and_no_animation(self):
        names = [name for name in self.methods if "bluetooth" in name or name.startswith("_bt_")]
        self.assertGreater(len(names), 10)
        for name in names:
            segment = ast.get_source_segment(self.source_text, self.methods[name])
            for word in ("timeout_add", "idle_add", "Spinner", "add_tick_callback"):
                self.assertNotIn(word, segment, name)

    def test_the_bluetooth_button_is_between_the_status_chips_and_settings_in_the_header_row(self):
        rows = ast.get_source_segment(self.source_text, self.methods["nav_rows"])
        self.assertIn("header = [self.tab_home, self.tab_discover, self.bluetooth_button, self.settings_button]", rows)
        header = ast.get_source_segment(self.source_text, self.methods["_build_header"])
        self.assertLess(header.index("header.append(self.clock_label)"),
                        header.index("header.append(self.bluetooth_button)"))
        self.assertLess(header.index("header.append(self.bluetooth_button)"),
                        header.index("header.append(self.settings_button)"))

    def test_a_pad_key_counts_every_press_of_a(self):
        from gaminghome.core import ACTIVATE
        key = mock.Mock()
        key.get_name.return_value = "bt:pad:0"
        window = SimpleNamespace(get_focus=lambda: key)
        self.window.handle_action(window, ACTIVATE)
        self.window.handle_action(window, ACTIVATE)
        self.assertEqual(key.emit.call_args_list, [mock.call("clicked")] * 2)
        key.activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
