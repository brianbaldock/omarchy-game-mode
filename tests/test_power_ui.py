"""The power profile button and sheet without a display: what they show, what their controls do, and that every way
out of the sheet leaves it, so that a click GTK delivers late chooses nothing. The window's own methods run on
stand-ins, as in test_bluetooth_ui, because the real window needs a display; the source is always the scripted
FakePower, which is checked here against the rules of the contract too."""

import ast
import importlib
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes.fake_power import FakePower
from gaminghome import power
from gaminghome import powersheet as pw
from gaminghome.battery import CHARGING, DISCHARGING, FULL, UNKNOWN as BATTERY_UNKNOWN, Battery
from gaminghome.i18n import Translator
from gaminghome.power import (ABSENT, BALANCED, CHANGED_ELSEWHERE, DENIED, ERROR, FAILED, HEAT, LAP, LIMITED,
                              NOT_REMEMBERED, PERFORMANCE, PROFILES, READY, SAVER, TIMEOUT, UNAVAILABLE, UNKNOWN,
                              UNSUPPORTED, Evidence, Failure, Hold, Snapshot)
from gaminghome.powersheet import (CLOSE, DISMISS, RETRY, Notice, Option, PowerSheet, button_view, choose_focus,
                                   control_names, current_option, option_name, opening_focus)

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "data" / "locales"
LANGUAGES = ("en", "de", "es", "fr", "pt-BR", "ja", "ar")
ENGLISH = Translator(LOCALES, "en", {"LANG": "C"}, Path("/nonexistent/language"))
ON_BATTERY = Battery(78, DISCHARGING, 28173)


def started(**world) -> FakePower:
    """A started source; unless the test says otherwise performance is active and all three profiles are offered,
    and a request is answered when the test says so."""
    source = FakePower(**{"answers": "later", **world})
    source.start()
    return source


def opened(source) -> PowerSheet:
    visit = PowerSheet(source)
    visit.enter()
    return visit


def keys(blocks):
    return [block.key for block in blocks]


def block(blocks, key):
    return next(item for item in blocks if item.key == key)


def options(blocks):
    return [item for item in blocks if isinstance(item, Option)]


class FakePowerTest(unittest.TestCase):
    """The stand-in keeps the contract's rules, and the client's where a window could be fooled."""

    def test_it_knows_nothing_until_it_is_started_and_then_reads_the_service(self):
        source = FakePower()
        self.assertEqual(source.snapshot, Snapshot())
        source.start()
        snapshot = source.snapshot
        self.assertEqual((snapshot.state, snapshot.active, snapshot.available), (READY, PERFORMANCE, PROFILES))

    def test_stop_clears_the_request_its_failure_and_its_note_and_keeps_what_was_read(self):
        source = started(active=BALANCED)
        source.set_profile(PERFORMANCE)
        source.fail(DENIED)
        source.set_profile(SAVER)
        source.stop()
        snapshot = source.snapshot
        self.assertEqual((snapshot.pending, snapshot.failure, snapshot.note), ("", None, ""))
        self.assertEqual((snapshot.state, snapshot.active, snapshot.available), (READY, BALANCED, PROFILES))

    def test_a_request_is_ignored_while_another_is_on_its_way_and_while_stopped(self):
        source = started(active=BALANCED)
        source.set_profile(PERFORMANCE)
        source.set_profile(SAVER)
        self.assertEqual((source.sent, source.snapshot.pending), ([PERFORMANCE], PERFORMANCE))
        source.finish()
        source.stop()
        source.set_profile(SAVER)
        self.assertEqual(source.sent, [PERFORMANCE])
        self.assertEqual(source.calls.count(("set_profile", SAVER)), 2)    # every call is recorded, ignored or not

    def test_the_active_profile_asks_the_service_nothing_and_only_tells_omarchy(self):
        source = started()
        source.set_profile(PERFORMANCE)
        self.assertEqual((source.sent, source.remembered, source.snapshot.pending), ([], [PERFORMANCE], ""))

    def test_a_request_is_answered_at_once_or_when_the_test_says_so(self):
        now = started(active=BALANCED, answers="now")
        now.set_profile(SAVER)
        self.assertEqual((now.snapshot.active, now.snapshot.pending), (SAVER, ""))
        later = started(active=BALANCED)
        later.set_profile(SAVER)
        self.assertEqual((later.snapshot.active, later.snapshot.pending), (BALANCED, SAVER))
        later.finish()
        self.assertEqual((later.snapshot.active, later.snapshot.pending, later.remembered), (SAVER, "", [SAVER]))
        later.set_profile(PERFORMANCE)
        later.fail(TIMEOUT)
        self.assertEqual((later.snapshot.active, later.snapshot.failure), (SAVER, Failure(TIMEOUT, PERFORMANCE)))

    def test_a_request_the_service_cannot_take_ends_at_once(self):
        for world, profile, failure in (({"state": ERROR}, SAVER, Failure(UNAVAILABLE, SAVER)),
                                        ({"available": (BALANCED, PERFORMANCE)}, SAVER, Failure(UNSUPPORTED, SAVER)),
                                        ({}, "turbo", Failure(UNSUPPORTED, ""))):
            with self.subTest(world=world, profile=profile):
                source = started(**world)
                source.set_profile(profile)
                self.assertEqual((source.snapshot.failure, source.snapshot.pending, source.sent), (failure, "", []))

    def test_before_this_visits_reading_the_last_one_is_shown_and_a_request_is_unavailable(self):
        source = started(active=BALANCED)
        source.stop()
        source.read_at_start = False
        source.change(active=SAVER)                                   # while hidden: nobody reads it
        source.start()
        self.assertEqual((source.snapshot.state, source.snapshot.active), (READY, BALANCED))
        source.set_profile(PERFORMANCE)
        self.assertEqual((source.snapshot.failure, source.sent), (Failure(UNAVAILABLE, PERFORMANCE), []))
        source.read()
        self.assertEqual(source.snapshot.active, SAVER)

    def test_nothing_is_reported_after_stop(self):
        told = []
        source = FakePower(lambda: told.append(1), answers="later")
        source.start()
        source.set_profile(BALANCED)
        source.stop()
        before = len(told)
        source.finish()                                               # the service carries it out all the same
        source.change(active=SAVER)
        source.dismiss()
        source.retry()
        self.assertEqual((len(told), source.snapshot.active), (before, PERFORMANCE))
        source.start()
        self.assertEqual(source.snapshot.active, SAVER)

    def test_a_change_made_inside_a_call_is_reported_later_unless_the_test_asks_for_it_at_once(self):
        told = []
        source = FakePower(lambda: told.append(source.snapshot.pending), answers="later")
        source.start()
        source.flush()
        told.clear()
        source.set_profile(BALANCED)
        self.assertEqual(told, [])                                    # not from inside the call
        source.flush()
        self.assertEqual(told, [BALANCED])
        source.finish()                                               # what the service does is reported at once
        self.assertEqual(told, [BALANCED, ""])
        eager = FakePower(lambda: told.append(eager.snapshot.pending), answers="later", reentrant=True)
        eager.start()
        told.clear()
        eager.set_profile(SAVER)
        self.assertEqual(told, [SAVER])

    def test_a_deferred_report_comes_once_for_a_burst_and_never_after_stop(self):
        later, told = [], []
        source = FakePower(lambda: told.append(source.snapshot), answers="later", defer=later.append)
        source.start()
        source.change(degraded=(HEAT,))
        source.change(holds=(Hold(PERFORMANCE, "Game"),))
        self.assertEqual((told, len(later)), ([], 1))
        later.pop()()
        self.assertEqual((len(told), told[-1].holds), (1, (Hold(PERFORMANCE, "Game"),)))
        source.change(active=SAVER)
        source.stop()
        source.start()
        source.change(active=BALANCED)
        for report in list(later):
            report()
        self.assertEqual(told[-1].active, BALANCED)
        self.assertEqual(len(told), 2)

    def test_another_programs_change_is_noted_only_after_a_request_of_this_visit_succeeded(self):
        source = started(active=BALANCED)
        source.change(active=SAVER)
        self.assertEqual(source.snapshot.note, "")
        source.set_profile(PERFORMANCE)
        source.finish()
        source.change(active=BALANCED)
        self.assertEqual(source.snapshot.note, CHANGED_ELSEWHERE)
        source.dismiss()
        source.change(active=SAVER)
        self.assertEqual(source.snapshot.note, "")

    def test_a_request_whose_answer_never_came_counts_as_done_when_its_profile_shows_up_in_time(self):
        timed_out = started(active=BALANCED)
        timed_out.set_profile(PERFORMANCE)
        timed_out.fail(TIMEOUT)
        timed_out.finish()                                            # the service carried it out after all
        snapshot = timed_out.snapshot
        self.assertEqual((snapshot.active, snapshot.failure, timed_out.remembered), (PERFORMANCE, None,
                                                                                     [PERFORMANCE]))
        timed_out.change(active=SAVER)                                # done: another program's change is noted
        self.assertEqual(timed_out.snapshot.note, CHANGED_ELSEWHERE)
        hidden = started(active=BALANCED)
        hidden.set_profile(SAVER)
        hidden.stop()                                                 # on its way when the window was hidden
        hidden.finish()
        hidden.start()
        self.assertEqual((hidden.snapshot.active, hidden.remembered), (SAVER, [SAVER]))

    def test_later_the_profile_is_no_answer_and_neither_after_dismiss_or_a_visit_that_did_not_find_it(self):
        source = started(active=BALANCED)
        source.set_profile(PERFORMANCE)
        source.fail(TIMEOUT)
        source.expire()
        source.finish()
        self.assertEqual((source.snapshot.active, source.snapshot.failure, source.remembered),
                         (PERFORMANCE, Failure(TIMEOUT, PERFORMANCE), []))
        for forget in (FakePower.dismiss, lambda visit: (visit.stop(), visit.start())):
            with self.subTest(forget=forget):
                source = started(active=BALANCED)
                source.set_profile(PERFORMANCE)
                source.fail(TIMEOUT)
                forget(source)
                source.finish()
                self.assertEqual((source.snapshot.active, source.remembered), (PERFORMANCE, []))

    def test_omarchy_not_keeping_the_choice_is_a_note_and_a_new_request_clears_it(self):
        source = started(active=BALANCED)
        source.set_profile(PERFORMANCE)
        source.finish()
        source.not_remembered()
        self.assertEqual(source.snapshot.note, NOT_REMEMBERED)
        source.set_profile(SAVER)
        self.assertEqual(source.snapshot.note, "")

    def test_the_service_going_away_ends_the_request_and_a_service_that_could_not_be_read_waits_for_retry(self):
        source = started(active=BALANCED)
        source.set_profile(PERFORMANCE)
        source.change(state=ABSENT)
        self.assertEqual((source.snapshot.state, source.snapshot.failure), (ABSENT, Failure(UNAVAILABLE, PERFORMANCE)))
        broken = started(state=ERROR)
        broken.change(state=READY)
        self.assertEqual(broken.snapshot.state, ERROR)
        broken.retry()
        self.assertEqual(broken.snapshot.state, READY)

    def test_start_and_stop_are_idempotent(self):
        told = []
        source = FakePower(lambda: told.append(1), defer=lambda report: report())
        source.start()
        source.start()
        source.stop()
        source.stop()
        self.assertEqual((len(told), source.running), (1, False))

    def test_it_has_the_methods_of_the_contract_and_touches_nothing_of_the_machine(self):
        protocol = {name for name in vars(power.Power) if not name.startswith("_")}
        self.assertLessEqual(protocol, set(dir(FakePower)))
        tree = ast.parse((ROOT / "tests" / "fakes" / "fake_power.py").read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "functools", "contextlib", "dataclasses", "gaminghome.power"})


class BlocksTest(unittest.TestCase):
    def blocks(self, source, battery=ON_BATTERY, visit=None):
        return (visit or opened(source)).blocks(ENGLISH, battery)

    def test_a_choice_shows_the_profiles_offered_in_order_with_the_active_one_checked(self):
        blocks = self.blocks(started(active=BALANCED, available=(PERFORMANCE, SAVER, BALANCED)))
        self.assertEqual(keys(blocks), ["title", "intro", "option:power-saver", "option:balanced",
                                        "option:performance", "sources", "close"])
        self.assertEqual([(o.title, o.current, o.dimmed, o.status) for o in options(blocks)],
                         [("Power saver", False, False, ""), ("Balanced", True, False, ""),
                          ("Performance", False, False, "")])
        self.assertEqual(blocks[0].text, "Power profile")
        self.assertEqual(options(blocks)[0].body,
                         "Lowest power and heat. Longest battery life; demanding games run slower.")

    def test_only_the_profiles_the_machine_offers_are_shown(self):
        blocks = self.blocks(started(active=SAVER, available=(SAVER, PERFORMANCE)))
        self.assertEqual([o.profile for o in options(blocks)], [SAVER, PERFORMANCE])

    def test_the_profile_asked_for_says_so_and_the_others_are_dimmed_until_the_answer(self):
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        shown = {o.profile: (o.current, o.pending, o.status, o.dimmed) for o in options(self.blocks(source,
                                                                                                   visit=visit))}
        self.assertEqual(shown, {SAVER: (False, False, "", True), BALANCED: (True, False, "", True),
                                 PERFORMANCE: (False, True, "Switching…", False)})
        source.finish()
        shown = {o.profile: (o.current, o.pending, o.dimmed) for o in options(self.blocks(source, visit=visit))}
        self.assertEqual(shown, {SAVER: (False, False, False), BALANCED: (False, False, False),
                                 PERFORMANCE: (True, False, False)})

    def test_a_service_that_could_not_be_read_offers_to_try_again_and_no_profile(self):
        blocks = self.blocks(started(state=ERROR))
        self.assertEqual(keys(blocks), ["title", "intro", "retry", "close"])
        self.assertEqual((blocks[2].text, blocks[2].controls[0].text), ("Couldn't read the power profile.", "Try again"))

    def test_a_service_that_went_away_or_offers_no_choice_says_so(self):
        for world in ({"state": ABSENT}, {"available": (BALANCED,), "active": BALANCED}):
            with self.subTest(world=world):
                blocks = self.blocks(started(**world))
                self.assertEqual(keys(blocks), ["title", "intro", "unavailable", "close"])
                self.assertEqual(control_names(blocks), [CLOSE])

    def test_a_failure_is_a_sentence_from_its_code_with_dismiss_and_never_the_systems_words(self):
        words = {DENIED: "Couldn't switch to Performance. This session isn't allowed to change the power profile.",
                 UNSUPPORTED: "Couldn't switch to Performance. This device doesn't offer that profile.",
                 TIMEOUT: "Couldn't switch to Performance. The power service didn't answer in time.",
                 UNAVAILABLE: "Couldn't switch to Performance. The power service couldn't be reached just then.",
                 FAILED: "Couldn't switch to Performance. Something went wrong.",
                 "org.freedesktop.Strange": "Couldn't switch to Performance. Something went wrong."}
        for code, text in words.items():
            with self.subTest(code=code):
                self.assertEqual(pw.failure_text(Failure(code, PERFORMANCE), ENGLISH), text)
        self.assertEqual(pw.failure_text(Failure(UNSUPPORTED, ""), ENGLISH), "This device doesn't offer that profile.")
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        source.fail(DENIED)
        failure = block(self.blocks(source, visit=visit), "failure")
        self.assertEqual((failure.tone, control_names([failure])), ("warn", [DISMISS]))

    def test_a_choice_made_before_this_visits_first_reading_does_not_call_the_service_broken(self):
        # Until the service is read again the last visit's reading is shown, and a choice made meanwhile ends at once
        # as unavailable; a moment later the service answers as usual, and the sheet must not contradict that.
        source = started(active=BALANCED)
        source.stop()
        source.read_at_start = False
        source.start()
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        source.read()
        blocks = self.blocks(source, visit=visit)
        self.assertEqual(block(blocks, "failure").text,
                         "Couldn't switch to Performance. The power service couldn't be reached just then.")
        self.assertEqual([o.profile for o in options(blocks) if o.current or o.pending], [BALANCED])

    def test_the_notes_say_what_happened_after_a_choice_and_carry_dismiss_without_a_failure(self):
        source = started(active=BALANCED, answers="now")
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        source.change(active=SAVER)
        note = block(self.blocks(source, visit=visit), "note")
        self.assertEqual((note.text, control_names([note])),
                         ("Another program changed the power profile after you did. Gaming Home leaves it as it is.",
                          [DISMISS]))
        visit.press(option_name(PERFORMANCE))
        source.not_remembered()
        self.assertEqual(block(self.blocks(source, visit=visit), "note").text,
                         "The profile is set, but Omarchy could not be told to keep it for next time.")

    def test_a_failure_and_a_note_at_once_share_one_dismiss_on_the_failure(self):
        snapshot = Snapshot(READY, PERFORMANCE, PROFILES, failure=Failure(TIMEOUT, SAVER), note=CHANGED_ELSEWHERE)
        blocks = PowerSheet(SimpleNamespace(snapshot=snapshot)).blocks(ENGLISH, ON_BATTERY)
        self.assertEqual([(item.key, control_names([item])) for item in blocks if isinstance(item, Notice)],
                         [("failure", [DISMISS]), ("note", [])])

    def test_a_failure_leaves_the_sheet_once_the_profile_it_asked_for_is_active_after_all(self):
        # The client takes an answer that comes within about ten seconds and drops the time-out itself. Later, or
        # when another program made the switch, the failure stays, and next to the check mark it would say the
        # opposite of what the sheet shows.
        for case, code, then in (("answered in time", TIMEOUT, lambda source: source.finish()),
                                 ("answered too late", TIMEOUT, lambda source: (source.expire(), source.finish())),
                                 ("switched by another program", DENIED,
                                  lambda source: source.change(active=PERFORMANCE))):
            with self.subTest(case):
                source = started(active=BALANCED)
                visit = opened(source)
                visit.press(option_name(PERFORMANCE))
                source.fail(code)
                self.assertIn("failure", keys(self.blocks(source, visit=visit)))
                then(source)
                blocks = self.blocks(source, visit=visit)
                self.assertEqual(([o.profile for o in options(blocks) if o.current], control_names(blocks)[-2:]),
                                 ([PERFORMANCE], [option_name(PERFORMANCE), CLOSE]))
                self.assertNotIn("failure", keys(blocks))
        snapshot = Snapshot(READY, PERFORMANCE, PROFILES, failure=Failure(TIMEOUT, PERFORMANCE), note=CHANGED_ELSEWHERE)
        blocks = PowerSheet(SimpleNamespace(snapshot=snapshot)).blocks(ENGLISH, ON_BATTERY)
        self.assertEqual([(item.key, control_names([item])) for item in blocks if isinstance(item, Notice)],
                         [("note", [DISMISS])])

    def test_performance_held_back_is_one_sentence_for_every_reason_and_nothing_for_none(self):
        cases = {(HEAT,): "Performance is limited right now: the device is too hot.",
                 (LAP,): "Performance is limited right now: it is on a lap.",
                 (LIMITED,): "Performance is limited right now: the system is limiting it.",
                 (LAP, HEAT): "Performance is limited right now: the device is too hot and it is on a lap.",
                 (): None}
        for reasons, text in cases.items():
            with self.subTest(reasons=reasons):
                blocks = self.blocks(started(degraded=reasons))
                self.assertEqual(next((b.text for b in blocks if b.key == "degraded"), None), text)
        self.assertNotIn("degraded", keys(self.blocks(started(active=SAVER, available=(SAVER, BALANCED),
                                                              degraded=(HEAT,)))))

    def test_programs_asking_for_a_profile_are_named_at_most_three_and_none_is_said_to_be_in_force(self):
        holds = (Hold(PERFORMANCE, "Racing Game"), Hold(PERFORMANCE, "Racing Game"), Hold(SAVER),
                 Hold(PERFORMANCE, "Ours", ours=True), Hold(BALANCED, "Launcher"), Hold(PERFORMANCE, "Fourth"))
        text = block(self.blocks(started(active=BALANCED, holds=holds)), "holds").text
        self.assertEqual(text.split("\n"), [
            "Racing Game is asking for Performance. Choosing another profile ends its request.",
            "Another program is asking for Power saver. Choosing another profile ends its request.",
            "Launcher is asking for Balanced. Choosing another profile ends its request."])
        self.assertIsNone(re.search(r"\b(is|are) (using|in force|holding|active)\b", text))

    def test_what_the_kernel_was_told_is_shown_only_when_it_disagrees_with_the_active_profile(self):
        agree = {SAVER: ("low-power", "quiet", "power-saver"), BALANCED: ("balanced", "balanced-performance"),
                 PERFORMANCE: ("performance", "balanced-performance")}
        for active, words in agree.items():
            boost = "off" if active == SAVER else "on"
            for word in words + ("",):
                with self.subTest(active=active, firmware=word):
                    self.assertNotIn("evidence", keys(self.blocks(started(active=active,
                                                                          evidence=Evidence(word, boost)))))
        cases = {(PERFORMANCE, "quiet", "on"): "The firmware is set to quiet, not Performance. Another program may "
                                               "have changed it.",
                 (SAVER, "custom", "off"): "The firmware is set to custom, not Power saver. Another program may have "
                                           "changed it.",
                 (BALANCED, "balanced", "off"): "The processor isn't allowed to boost, which doesn't fit Balanced. "
                                                "Another program may have changed it.",
                 (SAVER, "", "on"): "The processor is allowed to boost, which doesn't fit Power saver. Another "
                                    "program may have changed it."}
        for (active, firmware, boost), text in cases.items():
            with self.subTest(active=active, firmware=firmware, boost=boost):
                blocks = self.blocks(started(active=active, evidence=Evidence(firmware, boost)))
                self.assertEqual(block(blocks, "evidence").text, text)

    def test_the_notes_come_in_order_below_the_profiles(self):
        source = started(active=BALANCED, degraded=(HEAT,), holds=(Hold(PERFORMANCE, "Game"),),
                         evidence=Evidence("quiet", "on"))
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        source.fail(TIMEOUT)
        self.assertEqual(keys(self.blocks(source, visit=visit)),
                         ["title", "intro", "option:power-saver", "option:balanced", "option:performance", "failure",
                          "degraded", "holds", "evidence", "sources", "close"])

    def test_the_line_about_plugged_in_and_battery_is_general_and_needs_a_battery(self):
        source = started()
        for battery in (ON_BATTERY, Battery(64, CHARGING), Battery(100, FULL), Battery(50, BATTERY_UNKNOWN)):
            with self.subTest(battery=battery):
                self.assertEqual(block(self.blocks(source, battery), "sources").text,
                                 "Omarchy keeps a separate power profile for when this device is plugged in and for "
                                 "when it is on battery.")
        self.assertNotIn("sources", keys(self.blocks(source, None)))
        self.assertNotIn("sources", keys(self.blocks(started(state=ERROR))))

    def test_every_part_and_every_control_has_a_name_of_its_own_and_no_name_carries_a_programs_words(self):
        source = started(holds=(Hold(PERFORMANCE, "pw:performance"), Hold(SAVER, "Close")), degraded=(HEAT, LAP),
                         evidence=Evidence("quiet", "off"))
        visit = opened(source)
        visit.press(option_name(SAVER))
        source.fail(DENIED)
        for state in (READY, ERROR, ABSENT):
            with self.subTest(state=state):
                if state != READY:
                    source.change(state=state)
                blocks = self.blocks(source, visit=visit)
                names = control_names(blocks)
                self.assertEqual((len(keys(blocks)), len(names)), (len(set(keys(blocks))), len(set(names))))
                self.assertLessEqual(set(names), set(pw.NAMES))


class ButtonTest(unittest.TestCase):
    def test_the_button_exists_for_a_choice_and_for_a_service_that_could_not_be_read(self):
        cases = ((Snapshot(UNKNOWN), False), (Snapshot(ABSENT), False), (Snapshot(ERROR), True),
                 (Snapshot(READY, BALANCED, (BALANCED,)), False), (Snapshot(READY, BALANCED, PROFILES), True))
        for snapshot, visible in cases:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(button_view(snapshot, ENGLISH).visible, visible)

    def test_one_glyph_per_profile_a_warning_for_an_unreadable_service_and_the_profile_in_its_name(self):
        views = {active: button_view(Snapshot(READY, active, PROFILES), ENGLISH) for active in PROFILES}
        self.assertEqual({active: (view.icon, view.tone, view.tooltip) for active, view in views.items()},
                         {SAVER: ("power-saver", "", "Power profile: Power saver"),
                          BALANCED: ("power-balanced", "", "Power profile: Balanced"),
                          PERFORMANCE: ("power-performance", "", "Power profile: Performance")})
        error = button_view(Snapshot(ERROR), ENGLISH)
        self.assertEqual((error.icon, error.tone, error.tooltip), ("power-error", "warn",
                                                                   "Couldn't read the power profile"))
        unknown = button_view(Snapshot(READY, "", PROFILES), ENGLISH)
        self.assertEqual((unknown.icon, unknown.tooltip), ("power", "Power profile"))

    def test_a_request_on_its_way_does_not_change_the_button(self):
        source = started(active=BALANCED)
        before = button_view(source.snapshot, ENGLISH)
        opened(source).press(option_name(PERFORMANCE))
        self.assertEqual(button_view(source.snapshot, ENGLISH), before)

    def test_the_button_does_not_blink_while_the_next_reading_is_on_its_way(self):
        source = started(active=BALANCED)
        shown = button_view(source.snapshot, ENGLISH)
        source.stop()
        source.read_at_start = False
        self.assertEqual(button_view(source.snapshot, ENGLISH), shown)
        source.start()
        self.assertEqual(button_view(source.snapshot, ENGLISH), shown)

    def test_every_icon_the_window_asks_for_is_shipped_and_drawn_as_a_symbolic_icon(self):
        icons = {pw.ICONS[profile] for profile in PROFILES} | {"power", "power-error", "check", "warning"}
        for icon in icons:
            path = ROOT / "data" / "icons" / f"gh-{icon}-symbolic.svg"
            self.assertTrue(path.is_file(), icon)
            self.assertIn('fill="#2e3436"', path.read_text(encoding="utf-8"), icon)


class FocusTest(unittest.TestCase):
    NAMES = [option_name(SAVER), option_name(BALANCED), option_name(PERFORMANCE), DISMISS, CLOSE]

    def test_the_focus_stays_on_the_control_of_the_same_name(self):
        self.assertEqual(choose_focus(option_name(SAVER), self.NAMES, option_name(BALANCED)), option_name(SAVER))

    def test_when_its_control_is_gone_the_focus_goes_to_one_that_changes_nothing(self):
        self.assertEqual(choose_focus(option_name("x"), self.NAMES, option_name(BALANCED)), option_name(BALANCED))
        self.assertEqual(choose_focus(DISMISS, [RETRY, CLOSE], None), RETRY)
        self.assertEqual(choose_focus(option_name(SAVER), [CLOSE], None), CLOSE)
        self.assertIsNone(choose_focus(CLOSE, [], None))

    def test_the_sheet_opens_on_the_active_profile_so_that_a_starts_nothing(self):
        source = started(active=BALANCED)
        blocks = opened(source).blocks(ENGLISH)
        names, current = control_names(blocks), current_option(source.snapshot, blocks)
        self.assertEqual(opening_focus(names, current), option_name(BALANCED))
        broken = started(state=ERROR)
        blocks = opened(broken).blocks(ENGLISH)
        self.assertEqual(opening_focus(control_names(blocks), current_option(broken.snapshot, blocks)), RETRY)

    def test_an_active_profile_that_is_not_offered_leaves_the_focus_on_close_and_not_on_a_choice(self):
        source = started(active=BALANCED, available=(SAVER, PERFORMANCE))
        blocks = opened(source).blocks(ENGLISH)
        self.assertIsNone(current_option(source.snapshot, blocks))
        self.assertEqual(opening_focus(control_names(blocks), None), CLOSE)

    def test_a_profile_that_goes_away_while_it_has_the_focus_hands_it_to_the_active_profile(self):
        source = started(active=PERFORMANCE)
        visit = opened(source)
        source.change(available=(BALANCED, PERFORMANCE))
        blocks = visit.blocks(ENGLISH)
        self.assertEqual(choose_focus(option_name(SAVER), control_names(blocks), current_option(source.snapshot,
                                                                                                  blocks)),
                         option_name(PERFORMANCE))


class PressTest(unittest.TestCase):
    def test_a_profile_applies_at_once_with_one_request(self):
        source = started(active=BALANCED)
        visit = opened(source)
        self.assertIsNone(visit.press(option_name(SAVER)))
        self.assertEqual((source.calls[-1], source.sent), (("set_profile", SAVER), [SAVER]))

    def test_nothing_is_asked_for_the_active_profile_for_one_not_offered_or_for_a_name_that_is_no_profile(self):
        source = started(active=BALANCED, available=(BALANCED, PERFORMANCE))
        visit = opened(source)
        for name in (option_name(BALANCED), option_name(SAVER), "pw:turbo", "pw:", "bt:search", "power", CLOSE):
            visit.press(name)
        self.assertNotIn("set_profile", [call[0] for call in source.calls])

    def test_nothing_is_asked_while_a_request_is_on_its_way(self):
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        visit.press(option_name(SAVER))
        visit.press(option_name(PERFORMANCE))
        self.assertEqual([call for call in source.calls if call[0] == "set_profile"],
                         [("set_profile", PERFORMANCE)])

    def test_nothing_is_asked_of_a_service_that_offers_no_choice(self):
        for world in ({"state": ERROR}, {"state": ABSENT}, {"available": (BALANCED,), "active": BALANCED}):
            with self.subTest(world=world):
                source = started(**world)
                opened(source).press(option_name(SAVER))
                self.assertNotIn("set_profile", [call[0] for call in source.calls])

    def test_a_press_after_the_sheet_left_does_nothing(self):
        # GTK clicks a button 250 ms after A or Enter: A then B, A then Menu or Enter then Escape all leave first.
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        source.fail(TIMEOUT)                                          # something for Dismiss to clear
        visit.leave()
        calls = list(source.calls)
        for name in (option_name(SAVER), option_name(PERFORMANCE), DISMISS, RETRY):
            self.assertIsNone(visit.press(name))
        self.assertEqual((source.calls, source.snapshot.active, source.snapshot.pending), (calls, BALANCED, ""))

    def test_dismiss_clears_the_failure_and_the_note_and_try_again_reads_the_service(self):
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        source.fail(DENIED)
        visit.press(DISMISS)
        self.assertEqual((source.calls[-1], source.snapshot.failure), (("dismiss",), None))
        visit.press(DISMISS)                                          # nothing left to dismiss: nothing is called
        self.assertEqual(source.calls.count(("dismiss",)), 1)
        broken = started(state=ERROR)
        broken_visit = opened(broken)
        broken.change(state=READY)
        broken_visit.press(RETRY)
        self.assertEqual((broken.calls[-1], broken.snapshot.state), (("retry",), READY))
        broken_visit.press(RETRY)                                     # read: nothing to try again
        self.assertEqual(broken.calls.count(("retry",)), 1)


class LifecycleTest(unittest.TestCase):
    def test_starting_and_stopping_reach_the_source_and_stopping_leaves_first(self):
        source = FakePower()
        visit = PowerSheet(source)
        visit.start()
        visit.enter()
        visit.stop()
        self.assertEqual((source.running, visit.visiting, source.calls[-1]), (False, False, ("stop",)))

    def test_entering_dismisses_a_failure_or_a_note_left_from_before(self):
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        source.fail(DENIED)
        visit.leave()
        visit.enter()
        self.assertEqual((source.snapshot.failure, source.calls[-1]), (None, ("dismiss",)))
        visit.press(option_name(PERFORMANCE))
        source.finish()
        source.change(active=SAVER)                                   # noted, while the sheet is open
        visit.leave()
        visit.enter()
        self.assertEqual(source.snapshot.note, "")

    def test_a_failure_that_arrives_after_the_sheet_left_does_not_greet_the_next_visit(self):
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(PERFORMANCE))
        visit.leave()
        source.fail(TIMEOUT)                                          # the answer comes after B
        self.assertIsNotNone(source.snapshot.failure)
        visit.enter()
        self.assertNotIn("failure", keys(visit.blocks(ENGLISH)))

    def test_entering_with_nothing_to_dismiss_calls_nothing_and_entering_twice_does_nothing_more(self):
        source = started()
        visit = opened(source)
        visit.enter()
        self.assertNotIn(("dismiss",), source.calls)

    def test_a_request_on_its_way_goes_on_when_the_sheet_leaves_and_shows_at_the_next_visit(self):
        source = started(active=BALANCED)
        visit = opened(source)
        visit.press(option_name(SAVER))
        self.assertTrue(visit.leave())
        self.assertFalse(visit.leave())
        visit.enter()
        self.assertEqual(next(o for o in options(visit.blocks(ENGLISH)) if o.pending).profile, SAVER)
        source.finish()
        self.assertEqual(source.snapshot.active, SAVER)


class TranslationTest(unittest.TestCase):
    def strings(self, language):
        return json.loads((LOCALES / f"{language}.json").read_text(encoding="utf-8"))

    def test_every_word_the_sheet_and_the_button_use_is_in_every_language_with_nothing_left_to_fill(self):
        snapshots = []
        for code in power.FAILURES:
            snapshots.append(Snapshot(READY, PERFORMANCE, PROFILES, failure=Failure(code, SAVER),
                                      note=CHANGED_ELSEWHERE))
        snapshots += [Snapshot(READY, SAVER, PROFILES, degraded=(HEAT, LAP, LIMITED), note=NOT_REMEMBERED,
                               holds=(Hold(PERFORMANCE, "Game"), Hold(BALANCED)), evidence=Evidence("custom", "on"),
                               pending=BALANCED),
                      Snapshot(READY, BALANCED, PROFILES, evidence=Evidence("", "off")), Snapshot(ERROR),
                      Snapshot(ABSENT)]
        for language in LANGUAGES:
            translator = Translator(LOCALES, language)
            strings = self.strings(language)
            for snapshot in snapshots:
                source = SimpleNamespace(snapshot=snapshot)
                texts = [getattr(item, name, "") for item in PowerSheet(source).blocks(translator, ON_BATTERY)
                         for name in ("text", "title", "body", "status")]
                texts += [control.text for item in PowerSheet(source).blocks(translator) for control in item.controls]
                texts.append(button_view(snapshot, translator).tooltip)
                for text in texts:
                    self.assertFalse(re.search(r"[{}]|power\.", text), f"{language}: {text}")
            used = {key for key in strings if key.startswith("power.")}
            self.assertEqual(used, {key for key in self.strings("en") if key.startswith("power.")}, language)

    def test_no_word_of_the_plumbing_and_no_number_reaches_the_user(self):
        jargon = re.compile(r"d-?bus|polkit|sysfs|ppd|daemon|platform_profile|epp|\bwatts?\b|\d", re.I)
        for language in LANGUAGES:
            for key, text in self.strings(language).items():
                if key.startswith("power."):
                    self.assertIsNone(jargon.search(text), f"{language}: {key}: {text}")

    def test_the_sheet_never_says_that_a_choice_was_remembered(self):
        # Gaming Home cannot always tell whether Omarchy kept a choice. Neither the words nor the sheet after a switch,
        # whatever came of it, may say that it did; that Omarchy keeps a profile per power source, or could not be
        # told to keep this one, is no such claim.
        claim = re.compile(r"remember|\bsaved?\b|\bkept\b|will keep|keeps (it|your|this|the choice)\b", re.I)
        texts = [text for key, text in self.strings("en").items() if key.startswith("power.")]
        for then in (lambda source: None, FakePower.not_remembered, lambda source: source.change(active=SAVER)):
            source = started(active=BALANCED)
            visit = opened(source)
            visit.press(option_name(PERFORMANCE))
            source.finish()
            then(source)
            for battery in (ON_BATTERY, Battery(64, CHARGING), Battery(100, FULL)):
                texts += [getattr(item, name, "") for item in visit.blocks(ENGLISH, battery)
                          for name in ("text", "title", "body", "status")]
        for text in texts:
            self.assertIsNone(claim.search(text), text)

    def test_names_from_other_programs_and_the_kernel_stay_left_to_right_inside_arabic(self):
        strings = self.strings("ar")
        for key in ("power.hold", "power.evidence.firmware"):
            text = strings[key]
            for name in re.findall(r"\{(\w+)\}", text):
                if name in ("name", "firmware"):
                    self.assertIn(f"\u2066{{{name}}}\u2069", text, key)


class WindowTest(unittest.TestCase):
    """The window's power glue, run on a stand-in for the window around a real PowerSheet and the fake."""

    @classmethod
    def setUpClass(cls):
        importlib.import_module("gaminghome.application")             # pins Gtk 4 before the window module needs it
        cls.ui = importlib.import_module("gaminghome.ui")
        cls.window = cls.ui.HomeWindow
        cls.source_text = (ROOT / "gaminghome" / "ui.py").read_text(encoding="utf-8")
        tree = next(node for node in ast.parse(cls.source_text).body if isinstance(node, ast.ClassDef)
                    and node.name == "HomeWindow")
        cls.methods = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    def calls(self, name):
        return [node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                for node in ast.walk(self.methods[name]) if isinstance(node, ast.Call)]

    def stand_in(self, source, sheet_name="power"):
        window = mock.MagicMock()
        window.t = ENGLISH
        window._power = PowerSheet(source)
        window._power.enter()
        window.sheet = sheet_name
        window._pw_content = None
        window._bt_content = None
        for name in ("_leave_power", "_leave_bluetooth", "close_sheet", "back", "handle_action", "open_settings",
                     "open_app", "_on_unmap", "_on_map", "stop_status", "_on_pw_clicked", "_on_power",
                     "_update_battery"):
            setattr(window, name, getattr(self.window, name).__get__(window))
        return window

    def assert_left(self, window, source):
        self.assertFalse(window._power.visiting)
        self.assertEqual((window._pw_buttons, window._pw_content), ({}, None))
        late = len(source.calls)
        window._power.press(option_name(SAVER))                       # a click GTK was about to deliver
        self.assertEqual(source.calls[late:], [])

    def button(self, name):
        widget = mock.Mock()
        widget.get_name.return_value = name
        return widget

    def test_b_escape_close_and_menu_each_leave_the_sheet(self):
        from gaminghome.core import BACK, MENU
        gdk = self.ui.Gdk
        close = self.button(CLOSE)
        ways = {"B": lambda window: window.handle_action(BACK),
                "Menu": lambda window: window.handle_action(MENU),
                "Escape": lambda window: self.window._on_key(window, None, gdk.KEY_Escape, 0, gdk.ModifierType(0)),
                "Close": lambda window: window._on_pw_clicked(close)}
        for way, leave in ways.items():
            with self.subTest(way):
                source = started(active=BALANCED)
                window = self.stand_in(source)
                window._pw_buttons = {CLOSE: close}
                leave(window)
                self.assertIsNone(window.sheet)
                self.assert_left(window, source)
                window._build_settings_sheet.assert_not_called()      # Menu closes the sheet, it opens nothing

    def test_a_header_button_opens_its_sheet_only_on_a_shown_window_with_no_sheet_open(self):
        # GTK clicks a button 250 ms after A or Enter: by then B may have hidden the window, which is unmapped and not
        # unrealized, so the click still comes, or Menu may have opened Settings.
        header = ast.get_source_segment(self.source_text, self.methods["_build_header"])
        openings = {"bluetooth_button": "open_bluetooth", "power_button": "open_power",
                    "settings_button": "open_settings"}
        for button, opening in openings.items():
            self.assertIn(f'self.{button}.connect("clicked", lambda *_: self._from_header(self.{opening}))', header)
        for mapped, sheet, opens in ((False, None, False), (True, "settings", False), (True, "bluetooth", False),
                                     (True, "power", False), (True, ("app", "steam"), False), (True, None, True)):
            for opening in openings.values():
                with self.subTest(mapped=mapped, sheet=sheet, opening=opening):
                    window = mock.MagicMock()
                    window.sheet = sheet
                    window.get_mapped.return_value = mapped
                    self.window._from_header(window, getattr(window, opening))
                    self.assertEqual(getattr(window, opening).called, opens)

    def test_another_sheet_taking_its_place_leaves_it(self):
        for opening in (lambda window: window.open_settings(), lambda window: window.open_app("steam")):
            source = started(active=BALANCED)
            window = self.stand_in(source)
            opening(window)
            self.assertNotEqual(window.sheet, "power")
            self.assert_left(window, source)

    def test_hiding_or_destroying_the_window_leaves_and_closes_the_sheet_and_stops_the_source(self):
        source = started(active=BALANCED)
        window = self.stand_in(source)
        window._on_unmap()
        self.assertIsNone(window.sheet)
        window.scrim.set_visible.assert_called_with(False)
        self.assert_left(window, source)
        self.assertEqual((source.running, source.calls[-1]), (False, ("stop",)))

    def test_quitting_leaves_the_sheet_and_stops_the_source(self):
        source = started(active=BALANCED)
        window = self.stand_in(source)
        window.stop_status()
        self.assert_left(window, source)
        self.assertFalse(source.running)

    def test_showing_the_window_starts_the_source(self):
        source = FakePower()
        window = self.stand_in(source, None)
        window._on_map()
        self.assertTrue(source.running)

    def test_every_way_out_goes_through_the_one_cleanup(self):
        for name in ("close_sheet", "open_settings", "open_app", "open_bluetooth", "build", "_on_unmap",
                     "stop_status"):
            self.assertIn("_leave_power", self.calls(name), name)

        def leaves(node):
            return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "leave"
                    and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "_power")
        self.assertEqual([name for name, method in self.methods.items() if any(map(leaves, ast.walk(method)))],
                         ["_leave_power"])

    def test_building_the_window_again_leaves_the_sheet_and_forgets_it(self):
        # build() makes widgets and needs a display, so its first lines are read off its source.
        body = self.methods["build"].body
        statements = [ast.get_source_segment(self.source_text, node) for node in body[2:4]]
        self.assertEqual(statements[0], "self._leave_power()")
        self.assertTrue(statements[1].startswith('if self.sheet == "power":'))

    def test_leaving_takes_the_sheet_widgets_out_of_the_window(self):
        # GTK calls a pending click off only when the button is unrealized, which removing it does.
        source = started()
        window = self.stand_in(source)
        holder = mock.Mock()
        window._pw_content = mock.Mock()
        window._pw_content.get_ancestor.return_value = holder
        window._pw_buttons = {CLOSE: self.button(CLOSE)}
        window._leave_power()
        holder.get_parent.return_value.remove.assert_called_once_with(holder)
        self.assertEqual((window._pw_content, window._pw_buttons, window._power.visiting), (None, {}, False))

    def test_a_click_that_arrives_after_the_sheet_changed_or_went_away_is_dropped(self):
        source = started(active=BALANCED)
        window = self.stand_in(source)
        shown, stale = self.button(option_name(SAVER)), self.button(option_name(SAVER))
        window._pw_buttons = {option_name(SAVER): shown}
        window._on_pw_clicked(stale)                                  # a button the sheet no longer shows
        window.sheet = "settings"
        window._on_pw_clicked(shown)                                  # another sheet took its place
        window.sheet = None
        window._on_pw_clicked(shown)                                  # the sheet has gone
        self.assertEqual(source.sent, [])
        window.sheet = "power"
        window._on_pw_clicked(shown)
        self.assertEqual(source.sent, [SAVER])
        window._render_power.assert_called_with(None)
        window._paint_power_button.assert_called()

    def test_a_source_that_reports_from_inside_the_call_is_followed_without_harm(self):
        window = mock.MagicMock()
        source = FakePower(lambda: self.window._on_power(window), active=BALANCED, answers="later", reentrant=True)
        source.start()
        window._power = PowerSheet(source)
        window._power.enter()
        window.sheet = "power"
        option = self.button(option_name(PERFORMANCE))
        window._pw_buttons = {option_name(PERFORMANCE): option}
        self.window._on_pw_clicked(window, option)
        self.assertEqual((source.sent, source.snapshot.pending), ([PERFORMANCE], PERFORMANCE))
        self.assertEqual(window._render_power.call_count, 2)          # once from inside the call, once after it

    def test_a_change_repaints_the_button_and_the_open_sheet_and_never_the_whole_window(self):
        for name in ("_on_power", "_paint_power_button", "_render_power"):
            self.assertFalse(set(self.calls(name)) & {"refresh", "on_ui_changed", "build", "show_page", "idle_add",
                                                      "timeout_add", "timeout_add_seconds"}, name)
        window = mock.MagicMock()
        window.sheet = None
        self.window._on_power(window)
        window._paint_power_button.assert_called_once_with()
        window._render_power.assert_not_called()
        window.sheet = "power"
        self.window._on_power(window)
        window._render_power.assert_called_once_with()

    def test_the_power_code_of_the_window_owns_no_timer_and_no_animation(self):
        names = [name for name in self.methods if "power" in name or name.startswith("_pw_")]
        self.assertGreater(len(names), 10)
        for name in names:
            segment = ast.get_source_segment(self.source_text, self.methods[name])
            for word in ("timeout_add", "idle_add", "Spinner", "add_tick_callback"):
                self.assertNotIn(word, segment, name)

    def test_the_source_is_made_once_by_the_constructor(self):
        made = [name for name in self.methods if "power" in self.calls(name)]
        self.assertEqual(made, ["__init__"])
        segment = ast.get_source_segment(self.source_text, self.methods["__init__"])
        self.assertIn("pw.PowerSheet(runtime.power(self._on_power))", segment)

    def test_a_choice_that_would_do_nothing_now_is_dimmed_and_stays_sensitive_so_it_keeps_the_focus(self):
        # GTK moves the focus away from a widget that turns insensitive.
        disabled = self.ui.Gtk.AccessibleState.DISABLED
        button = mock.Mock()
        button.has_css_class.return_value = False
        self.window._pw_dim(button, True)
        button.set_sensitive.assert_not_called()
        button.add_css_class.assert_called_once_with("dimmed")
        button.update_state.assert_called_once_with([disabled], [True])
        button.has_css_class.return_value = True
        self.window._pw_dim(button, False)
        button.remove_css_class.assert_called_once_with("dimmed")
        self.assertNotIn("set_sensitive", "".join(ast.get_source_segment(self.source_text, self.methods[name])
                                                  for name in self.methods if name.startswith("_pw_")))

    def test_a_dimmed_choice_fades_its_content_and_never_its_focus_ring(self):
        # The ring is the option's own border and shadow, so fading the option would fade the ring as well, and the
        # D-pad still moves onto a dimmed option. The Bluetooth sheet has a rule of its own.
        css = re.sub(r"/\*.*?\*/", "", self.ui.CSS.template, flags=re.S)
        fading = [selector.strip() for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)
                  if "opacity" in body for selector in selectors.split(",")
                  if "dimmed" in selector and ".gh-bt-sheet" not in selector]
        self.assertEqual(fading, [".gh-pw-option.dimmed > box"])

    def test_the_power_button_sits_between_bluetooth_and_settings_in_the_header_row(self):
        rows = ast.get_source_segment(self.source_text, self.methods["nav_rows"])
        self.assertIn("self.bluetooth_button, self.power_button, self.settings_button]", rows)
        header = ast.get_source_segment(self.source_text, self.methods["_build_header"])
        self.assertLess(header.index("header.append(self.bluetooth_button)"),
                        header.index("header.append(self.power_button)"))
        self.assertLess(header.index("header.append(self.power_button)"),
                        header.index("header.append(self.settings_button)"))

    def test_closing_the_sheet_gives_the_focus_back_to_the_power_button(self):
        source = started()
        window = self.stand_in(source)
        window._focus_by_name.return_value = True
        window.close_sheet()
        window._focus_by_name.assert_called_once_with(pw.BUTTON)

    def test_the_status_names_every_power_control_by_its_neutral_name(self):
        window = mock.MagicMock()
        for name in pw.NAMES:
            window._selected.get_name.return_value = name
            self.assertEqual(self.window.selected_name(window), name)

    def test_the_line_about_plugged_in_and_battery_follows_a_battery_that_comes_or_goes(self):
        source = started()
        window = self.stand_in(source)
        window._battery = None
        window._update_battery(ON_BATTERY)
        window._update_battery(Battery(77, DISCHARGING, 27000))       # a new estimate changes nothing it says
        window._update_battery(None)
        self.assertEqual(window._render_power.call_count, 2)
        window.sheet = None
        window._update_battery(ON_BATTERY)
        self.assertEqual(window._render_power.call_count, 2)


if __name__ == "__main__":
    unittest.main()
