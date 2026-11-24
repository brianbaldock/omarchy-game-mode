"""The ``power`` command, against the fake power-profiles-daemon on a private bus, and how the app dispatches it."""

import io
import unittest
import warnings
from unittest import mock

from gi.repository import GLib

from fakes import fake_ppd
from fakes.fake_inputplumber import PrivateBus
from fakes.fake_ppd import FakePPD
from gaminghome import powerprofiles
from gaminghome.power import BALANCED, PERFORMANCE, SAVER, Snapshot
from gaminghome.powerprofiles import PowerProfiles
from test_powerprofiles import gone, scratch, stand_in

USAGE = "Usage: omarchy-game-mode power ..."


class PowerCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus = PrivateBus.start()
        cls.addClassCleanup(cls.bus.stop)

    def setUp(self):
        self.root = scratch(self, "command")
        self.helper = None
        self.made = []
        self.addCleanup(self.bus.close_connections)

    def fake(self, own=True, **state) -> FakePPD:
        fake = FakePPD(self.bus, **state)
        if own:
            self.assertTrue(fake.own())
        return fake

    def make_source(self, on_change):
        source = PowerProfiles(on_change, connection=self.bus.connect(), sysfs_root=str(self.root),
                               find_helper=lambda: self.helper and str(self.helper))
        self.made.append(source)
        return source

    def run_command(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with warnings.catch_warnings():
            # PyGObject's asyncio glue warns about asyncio policies on new Pythons, as in fakes.loop.pump.
            warnings.simplefilter("ignore", DeprecationWarning)
            code = powerprofiles.main(list(args), usage=USAGE, stdout=stdout, stderr=stderr,
                                      make_source=self.make_source)
        return code, stdout.getvalue(), stderr.getvalue()

    def sets(self, fake) -> list:
        return [call.arguments[2] for call in fake.writes()]

    def test_status_prints_the_profile_what_is_offered_and_what_holds_one(self):
        fake = self.fake(active=PERFORMANCE, degraded="lap-detected")
        fake.add_hold(PERFORMANCE, "\u202eorg.example.Game\n", reason="not printed", signal=False)
        fake.add_hold(SAVER, "", signal=False)
        expected = ("performance\noffered: power-saver, balanced, performance\nperformance held back: lap\n"
                    "held: performance by org.example.Game\nheld: power-saver\n")
        self.assertEqual(self.run_command("status"), (0, expected, ""))
        self.assertEqual(self.run_command(), (0, expected, ""))
        self.assertEqual(fake.writes(), [])
        self.assertFalse(any(source.helping or source.owner for source in self.made))     # stopped on the way out

    def test_status_of_a_machine_with_nothing_to_switch_offers_nothing(self):
        self.fake(active=BALANCED, profiles=fake_ppd.PLACEHOLDER)
        self.assertEqual(self.run_command(), (0, "balanced\noffered: none\n", ""))

    def test_without_a_service_or_with_one_that_cannot_be_read_it_says_why_and_fails(self):
        for action in (["status"], ["set", PERFORMANCE], ["cycle"]):
            with self.subTest(action=action):
                self.assertEqual(self.run_command(*action),
                                 (1, "", "unavailable: power-profiles-daemon is not running\n"))
        fake = self.fake()
        fake.garbage = GLib.Variant("(s)", ("garbage",))
        self.assertEqual(self.run_command(), (1, "", "failed: power-profiles-daemon could not be read\n"))

    def test_a_service_that_does_not_answer_in_time_is_a_timeout(self):
        fake = self.fake()
        fake.hold = lambda call: True
        with mock.patch.object(powerprofiles, "FIRST_READING_S", 0.1):
            self.assertEqual(self.run_command("set", SAVER),
                             (1, "", "timeout: power-profiles-daemon did not answer in time\n"))
        self.assertEqual(self.sets(fake), [])

    def test_set_changes_the_profile_and_waits_for_omarchy_to_keep_it(self):
        fake = self.fake(active=BALANCED)
        self.helper = stand_in(self.root / "helper")
        self.assertEqual(self.run_command("set", PERFORMANCE), (0, "performance\n", ""))
        self.assertEqual((fake.active, self.sets(fake)), (PERFORMANCE, [PERFORMANCE]))
        record = self.helper.parent / "record"
        self.assertEqual((record / "argv").read_text().split(), [str(self.helper), "autodetect", PERFORMANCE])
        self.assertTrue(gone(int((record / "stat").read_text().split()[0])))       # ended before the command did

    def test_setting_the_active_profile_sends_nothing_and_still_tells_omarchy(self):
        fake = self.fake(active=BALANCED)
        self.helper = stand_in(self.root / "helper")
        self.assertEqual(self.run_command("set", BALANCED), (0, "balanced\n", ""))
        self.assertEqual(self.sets(fake), [])
        self.assertTrue((self.helper.parent / "record" / "argv").exists())

    def test_a_profile_omarchy_could_not_keep_is_set_with_a_note(self):
        self.fake(active=BALANCED)
        self.helper = stand_in(self.root / "helper", "fail")
        note = "not remembered: Omarchy could not keep power-saver for this power source\n"
        self.assertEqual(self.run_command("set", SAVER), (0, "power-saver\n", note))

    def test_each_failure_of_a_change_is_one_line_and_exit_status_1(self):
        cases = ((lambda fake: setattr(fake, "deny", True), "denied: this session may not change the power profile"),
                 (lambda fake: fake.failures.update(Set=("org.freedesktop.DBus.Error.InvalidArgs", "words")),
                  "failed: power-profiles-daemon did not change the profile"),
                 (lambda fake: fake.failures.update(Set=("org.freedesktop.DBus.Error.ServiceUnknown", "words")),
                  "unavailable: power-profiles-daemon cannot be reached"))
        for spoil, reason in cases:
            with self.subTest(reason=reason):
                fake = self.fake(active=BALANCED)
                spoil(fake)
                self.assertEqual(self.run_command("set", PERFORMANCE), (1, "", reason + "\n"))
                self.assertEqual(fake.active, BALANCED)
                fake.release()

    def test_a_change_that_runs_out_of_time_is_a_timeout(self):
        fake = self.fake(active=BALANCED)
        fake.hold = lambda call: call.member == "Set"
        with mock.patch.object(powerprofiles, "SET_TIMEOUT_MS", 100):
            self.assertEqual(self.run_command("set", PERFORMANCE),
                             (1, "", "timeout: power-profiles-daemon did not answer in time\n"))

    def test_a_profile_the_machine_does_not_offer_is_unsupported(self):
        fake = self.fake(active=BALANCED, profiles=fake_ppd.PLACEHOLDER)
        self.assertEqual(self.run_command("set", PERFORMANCE),
                         (1, "", "unsupported: this machine does not offer performance\n"))
        self.assertEqual(self.sets(fake), [])

    def test_cycle_goes_up_one_offered_profile_at_a_time_and_round_again(self):
        fake = self.fake(active=SAVER)
        for expected in (BALANCED, PERFORMANCE, SAVER):
            self.assertEqual(self.run_command("cycle"), (0, expected + "\n", ""))
        self.assertEqual(self.sets(fake), [BALANCED, PERFORMANCE, SAVER])
        fake.release()
        fake = self.fake(active=PERFORMANCE, profiles=((SAVER, "amd_pstate", ""), (BALANCED, "", "placeholder"),
                                                       (PERFORMANCE, "amd_pstate", "")))
        self.assertEqual(self.run_command("cycle"), (0, "power-saver\n", ""))

    def test_cycle_from_a_profile_that_is_not_offered_does_nothing(self):
        fake = self.fake(active="low-power", profiles=fake_ppd.RC73YA + (("low-power", "amd_pstate", ""),))
        code, stdout, stderr = self.run_command("cycle")
        self.assertEqual((code, stdout), (1, ""))
        self.assertTrue(stderr.startswith("unknown profile: "), stderr)
        self.assertEqual(self.sets(fake), [])

    def test_bad_usage_is_exit_status_2_and_touches_nothing(self):
        for args in (["bogus"], ["set"], ["set", "turbo"], ["set", "Performance"], ["set", BALANCED, "now"],
                     ["status", "now"], ["cycle", "up"], ["--help"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_command(*args), (2, "", USAGE + "\n"))
        self.assertEqual(self.made, [])


class DispatchTest(unittest.TestCase):
    def test_the_app_answers_power_itself_without_the_window_or_a_running_instance(self):
        from gaminghome import application
        with mock.patch("gaminghome.powerprofiles.main", return_value=0) as command, \
                mock.patch("gaminghome.application.GameModeApplication") as app:
            self.assertEqual(application.main(["omarchy-game-mode", "power", "set", BALANCED]), 0)
        command.assert_called_once_with(["set", BALANCED], usage=application.USAGE)
        app.assert_not_called()

    def test_bad_usage_never_reaches_the_service(self):
        from gaminghome import application
        with mock.patch("gaminghome.powerprofiles.PowerProfiles") as source, \
                mock.patch("gaminghome.application.GameModeApplication") as app, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(application.main(["omarchy-game-mode", "power", "set", "turbo"]), 2)
        source.assert_not_called()
        app.assert_not_called()
        self.assertIn("power set PROFILE", stderr.getvalue())
        self.assertIn("power [status|cycle]", application.USAGE)

    def test_the_runtime_hands_the_window_a_source_that_is_not_started(self):
        from gaminghome.application import Runtime
        with mock.patch.object(powerprofiles.Gio, "bus_watch_name") as watch:
            source = Runtime.power(None, lambda: None)
        self.assertIsInstance(source, PowerProfiles)
        self.assertEqual((source.snapshot, source.owner, source.idle), (Snapshot(), None, True))
        watch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
