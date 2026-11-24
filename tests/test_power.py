"""The power profile model: which profiles there are, which ones are real, how they are cycled and cleaned."""

import ast
import dataclasses
import unittest
from pathlib import Path

from gaminghome import power


class ProfilesTest(unittest.TestCase):
    def test_the_profiles_are_the_services_own_names_from_the_least_power_to_the_most(self):
        self.assertEqual(power.PROFILES, ("power-saver", "balanced", "performance"))
        self.assertEqual((power.SAVER, power.BALANCED, power.PERFORMANCE), power.PROFILES)


class IsRealTest(unittest.TestCase):
    def test_a_profile_is_real_when_a_driver_that_changes_the_hardware_backs_it(self):
        for cpu, platform in (("amd_pstate", "platform_profile"), ("amd_pstate", ""), ("", "platform_profile"),
                              ("amd_pstate", "placeholder"), ("placeholder", "platform_profile"),
                              ("intel_pstate", "")):
            with self.subTest(cpu=cpu, platform=platform):
                self.assertTrue(power.is_real(cpu, platform))

    def test_a_profile_only_the_placeholder_backs_changes_nothing_and_is_not_real(self):
        for cpu, platform in (("placeholder", ""), ("", "placeholder"), ("placeholder", "placeholder"), ("", ""),
                              (None, None), (3, b"x")):
            with self.subTest(cpu=cpu, platform=platform):
                self.assertFalse(power.is_real(cpu, platform))


class DegradedTest(unittest.TestCase):
    def test_the_services_reasons_become_ours_and_any_other_text_is_a_limit(self):
        self.assertEqual(power.degraded_reasons("high-operating-temperature"), (power.HEAT,))
        self.assertEqual(power.degraded_reasons("lap-detected"), (power.LAP,))
        self.assertEqual(power.degraded_reasons("something-new"), (power.LIMITED,))

    def test_several_reasons_are_separated_by_commas_and_each_is_counted_once(self):
        self.assertEqual(power.degraded_reasons("lap-detected,high-operating-temperature"), (power.LAP, power.HEAT))
        self.assertEqual(power.degraded_reasons(" lap-detected , lap-detected ,, x , y "), (power.LAP, power.LIMITED))

    def test_no_reason_means_none_is_reported_which_is_not_a_promise_that_nothing_is_throttled(self):
        for text in ("", " ", ",", None, 0, b"lap-detected"):
            with self.subTest(text=text):
                self.assertEqual(power.degraded_reasons(text), ())


class CycleTest(unittest.TestCase):
    def test_cycling_goes_up_one_profile_at_a_time_and_round_again(self):
        order = [power.next_profile(profile, power.PROFILES) for profile in power.PROFILES]
        self.assertEqual(order, [power.BALANCED, power.PERFORMANCE, power.SAVER])

    def test_cycling_skips_a_profile_the_machine_does_not_offer(self):
        offered = (power.SAVER, power.PERFORMANCE)
        self.assertEqual(power.next_profile(power.SAVER, offered), power.PERFORMANCE)
        self.assertEqual(power.next_profile(power.PERFORMANCE, offered), power.SAVER)

    def test_cycling_from_an_unknown_place_does_nothing_rather_than_drop_to_power_saver(self):
        for active in ("", "turbo", None, power.SAVER):
            with self.subTest(active=active):
                self.assertEqual(power.next_profile(active, (power.BALANCED, power.PERFORMANCE)), "")

    def test_cycling_gives_nothing_when_nothing_is_offered(self):
        self.assertEqual(power.next_profile(power.SAVER, ()), "")

    def test_the_order_in_which_profiles_are_offered_does_not_matter(self):
        self.assertEqual(power.next_profile(power.SAVER, [power.PERFORMANCE, power.SAVER, power.BALANCED]),
                         power.BALANCED)

    def test_the_profiles_offered_may_come_from_an_iterator_that_can_be_read_only_once(self):
        self.assertEqual(power.next_profile(power.BALANCED, iter(power.PROFILES)), power.PERFORMANCE)


class EvidenceTextTest(unittest.TestCase):
    def test_a_word_the_kernel_reports_is_kept(self):
        for word in ("performance", "balance_power", "low-power", "quiet", "powersave", "a"):
            with self.subTest(word=word):
                self.assertEqual(power.evidence_text(word + "\n"), word)

    def test_anything_else_is_dropped(self):
        for text in ("", "   ", "Performance", "two words", "x" * 25, "-x", "perf\x00", "\u202eperf", "a/b",
                     "balanced\nperformance", None, 7):
            with self.subTest(text=text):
                self.assertEqual(power.evidence_text(text), "")

    def test_the_longest_word_allowed_is_24_characters(self):
        self.assertEqual(power.evidence_text("a" * 24), "a" * 24)

    def test_the_kernels_boost_switch_is_on_or_off_and_anything_else_is_unknown(self):
        self.assertEqual((power.boost_text("1\n"), power.boost_text("0\n")), ("on", "off"))
        for text in ("", "2", "yes", "on", None, 1):
            with self.subTest(text=text):
                self.assertEqual(power.boost_text(text), "")


class HoldNameTest(unittest.TestCase):
    def test_a_programs_name_is_kept_when_it_is_plain(self):
        self.assertEqual(power.hold_name("org.example.Game"), "org.example.Game")

    def test_a_programs_name_is_made_safe_and_short(self):
        self.assertEqual(power.hold_name("Game\u202e\x00 \n Two"), "Game Two")
        self.assertEqual(power.hold_name("x" * 10_000), "x" * power.HOLD_NAME_MAX)
        self.assertEqual(power.hold_name(None), "")


class SnapshotTest(unittest.TestCase):
    def snapshot(self, state, available=power.PROFILES):
        return power.Snapshot(state, available=tuple(available))

    def test_there_is_a_choice_only_when_the_service_answered_and_can_apply_two_profiles_or_more(self):
        self.assertTrue(self.snapshot(power.READY).usable)
        self.assertTrue(self.snapshot(power.READY, (power.SAVER, power.BALANCED)).usable)
        self.assertFalse(self.snapshot(power.READY, (power.BALANCED,)).usable)
        self.assertFalse(self.snapshot(power.READY, ()).usable)
        for state in (power.UNKNOWN, power.ABSENT, power.ERROR):
            with self.subTest(state=state):
                self.assertFalse(self.snapshot(state).usable)

    def test_the_header_has_a_button_for_a_choice_and_for_a_service_that_could_not_be_read(self):
        states = {state: self.snapshot(state).visible for state in
                  (power.UNKNOWN, power.ABSENT, power.READY, power.ERROR)}
        self.assertEqual(states, {power.UNKNOWN: False, power.ABSENT: False, power.READY: True, power.ERROR: True})
        self.assertFalse(self.snapshot(power.READY, (power.BALANCED,)).visible)

    def test_a_snapshot_cannot_be_changed_in_place(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            power.Snapshot().active = power.SAVER

    def test_an_empty_snapshot_says_nothing_is_known(self):
        empty = power.Snapshot()
        self.assertEqual((empty.state, empty.active, empty.available, empty.degraded, empty.holds, empty.pending,
                          empty.failure, empty.note), (power.UNKNOWN, "", (), (), (), "", None, ""))
        self.assertEqual(empty.evidence, power.Evidence())

    def test_every_failure_code_and_every_note_is_distinct_and_the_two_sets_do_not_overlap(self):
        self.assertEqual(len(set(power.FAILURES)), len(power.FAILURES))
        self.assertEqual(len(set(power.NOTES)), len(power.NOTES))
        self.assertFalse(set(power.FAILURES) & set(power.NOTES))

    def test_a_hold_that_gaming_home_does_not_own_is_the_default(self):
        self.assertFalse(power.Hold(power.PERFORMANCE, "Game").ours)


class ContractTest(unittest.TestCase):
    def test_the_contract_imports_nothing_but_the_standard_library_and_the_text_helper(self):
        tree = ast.parse(Path(power.__file__).read_text(encoding="utf-8"))
        absolute = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        relative = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                (relative if node.level else absolute).add(node.module.split(".")[0])
        self.assertEqual(absolute, {"__future__", "re", "dataclasses", "typing"})
        self.assertEqual(relative, {"textsafe"})


if __name__ == "__main__":
    unittest.main()
