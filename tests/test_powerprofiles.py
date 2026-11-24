"""The power profile client, the fake power-profiles-daemon it is tested against, and the ``power`` command.

The pure parts (parsing, the error table, the allow-list, the evidence files, the helper's environment) need no
D-Bus. Everything else runs against a fake power-profiles-daemon on a private ``dbus-daemon`` (skipped when there is
none), a scratch sysfs tree and a stand-in for Omarchy's ``omarchy-powerprofiles-set``. Nothing here talks to the
real daemon or changes the machine's profile.
"""

import ast
import contextlib
import os
import shutil
import signal
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import Gio, GLib

from fakes import fake_ppd
from fakes.clockwork import Timers
from fakes.fake_inputplumber import PrivateBus
from fakes.fake_ppd import BUS_NAME, INTERFACE, OBJECT_PATH, PROPERTIES_IFACE, TYPES, FakePPD
from fakes.fake_upower import activatable_bus_config
from fakes.loop import pump
from gaminghome import power, powerprofiles
from gaminghome.power import (ABSENT, BALANCED, CHANGED_ELSEWHERE, DENIED, ERROR, FAILED, NOT_REMEMBERED, PERFORMANCE,
                              PROFILES, READY, SAVER, TIMEOUT, UNAVAILABLE, UNKNOWN, UNSUPPORTED, Evidence, Failure,
                              Hold)
from gaminghome.powerprofiles import PowerProfiles, Unusable, read_properties

SCRATCH = Path(__file__).resolve().parent / "scratch" / "powerprofiles"


def scratch(test, name) -> Path:
    """An empty directory of the git-ignored scratch area, removed again after the test."""
    directory = SCRATCH / name
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    test.addCleanup(shutil.rmtree, SCRATCH, True)
    return directory


def stand_in(directory: Path, mode="ok", gate=False) -> Path:
    """A stand-in for Omarchy's omarchy-powerprofiles-set in ``directory``. It writes down in ``record/`` how it was
    started (argv, its environment as it was handed over, stdin, where its standard streams go, its process group and
    which python3 its PATH finds), then succeeds (``ok``), fails (``fail``) or hangs with a child of its own
    (``hang``). With ``gate`` it first waits until the test writes to ``gate``."""
    record = directory / "record"
    record.mkdir(parents=True)
    lines = ["#!/bin/sh", f'cd "{record}" || exit 3', 'printf "%s\\n" "$0" "$@" > argv',
             "cat /proc/$$/environ > environ", "cat > stdin", "cat /proc/$$/stat > stat",
             "readlink /proc/$$/fd/0 /proc/$$/fd/1 /proc/$$/fd/2 > fds", "command -v python3 > python3"]
    if gate:
        os.mkfifo(directory / "gate")
        lines += [": > waiting", f'read line < "{directory}/gate"']
    lines += {"ok": ["exit 0"], "fail": ["exit 1"], "hang": ["sleep 30 &", "echo $! > child", "sleep 30"]}[mode]
    script = directory / "omarchy-powerprofiles-set"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def gone(pid: int) -> bool:
    """The process has ended: it is not there any more, or only as a zombie its new parent has not reaped yet."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except OSError:
        return True


def set_call(profile, value_type="s", interface=INTERFACE, name="ActiveProfile"):
    return GLib.Variant("(ssv)", (interface, name, GLib.Variant(value_type, profile)))


class AllowListTest(unittest.TestCase):
    def test_a_read_of_the_daemons_properties_and_a_change_of_its_profile_may_be_sent(self):
        get_all = GLib.Variant("(s)", (INTERFACE,))
        self.assertTrue(powerprofiles.allowed(OBJECT_PATH, PROPERTIES_IFACE, "GetAll", get_all))
        for profile in PROFILES:
            with self.subTest(profile=profile):
                self.assertTrue(powerprofiles.allowed(OBJECT_PATH, PROPERTIES_IFACE, "Set", set_call(profile)))

    def test_anything_else_is_refused(self):
        refused = (
            (OBJECT_PATH, PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", ("org.freedesktop.DBus.Peer",))),
            (OBJECT_PATH, PROPERTIES_IFACE, "Get", GLib.Variant("(ss)", (INTERFACE, "ActiveProfile"))),
            (OBJECT_PATH, PROPERTIES_IFACE, "Set", set_call("turbo")),
            (OBJECT_PATH, PROPERTIES_IFACE, "Set", set_call(1, "i")),
            (OBJECT_PATH, PROPERTIES_IFACE, "Set", set_call(False, "b", name="BatteryAware")),
            (OBJECT_PATH, PROPERTIES_IFACE, "Set", set_call(BALANCED, interface="net.hadess.PowerProfiles")),
            (OBJECT_PATH, PROPERTIES_IFACE, "Set", GLib.Variant("(ss)", (INTERFACE, "ActiveProfile"))),
            (OBJECT_PATH, PROPERTIES_IFACE, "GetAll", None),
            (OBJECT_PATH, INTERFACE, "HoldProfile", GLib.Variant("(sss)", (PERFORMANCE, "", "org.example.Game"))),
            (OBJECT_PATH, INTERFACE, "ReleaseProfile", GLib.Variant("(u)", (1,))),
            (OBJECT_PATH, INTERFACE, "SetActionEnabled", GLib.Variant("(sb)", ("trickle_charge", True))),
            (OBJECT_PATH, "org.freedesktop.DBus.Introspectable", "Introspect", GLib.Variant("()", ())),
            ("/net/hadess/PowerProfiles", PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", (INTERFACE,))),
            ("/net/hadess/PowerProfiles", PROPERTIES_IFACE, "Set", set_call(BALANCED)),
        )
        for path, interface, member, parameters in refused:
            with self.subTest(path=path, member=member, parameters=parameters):
                self.assertFalse(powerprofiles.allowed(path, interface, member, parameters))


class FailureCodeTest(unittest.TestCase):
    def test_each_error_of_the_system_becomes_exactly_one_failure(self):
        table = {"org.freedesktop.DBus.Error.AccessDenied": power.DENIED,
                 "org.freedesktop.DBus.Error.ServiceUnknown": power.UNAVAILABLE,
                 "org.freedesktop.DBus.Error.NameHasNoOwner": power.UNAVAILABLE,
                 "org.freedesktop.DBus.Error.Disconnected": power.UNAVAILABLE,
                 "org.freedesktop.DBus.Error.NoReply": power.UNAVAILABLE,
                 "org.freedesktop.DBus.Error.InvalidArgs": power.FAILED,
                 "org.freedesktop.DBus.Error.Failed": power.FAILED,
                 "org.example.Anything": power.FAILED}
        for name, code in table.items():
            with self.subTest(name=name):
                error = Gio.DBusError.new_for_dbus_error(name, "Not Authorized: text of the daemon")
                self.assertEqual(powerprofiles.failure_code(error), code)

    def test_running_out_of_time_is_a_timeout_and_a_closed_connection_is_unavailable(self):
        def local(code, text):
            return GLib.Error.new_literal(Gio.io_error_quark(), text, code)
        self.assertEqual(powerprofiles.failure_code(local(Gio.IOErrorEnum.TIMED_OUT, "Timeout was reached")),
                         power.TIMEOUT)
        self.assertEqual(powerprofiles.failure_code(local(Gio.IOErrorEnum.CLOSED, "The connection is closed")),
                         power.UNAVAILABLE)
        for other in (local(Gio.IOErrorEnum.INVALID_ARGUMENT, "returned type (s)"), ValueError("x"), None):
            with self.subTest(error=other):
                self.assertEqual(powerprofiles.failure_code(other), power.FAILED)


RC73YA = [{"Profile": profile, "CpuDriver": "amd_pstate", "PlatformDriver": "platform_profile", "Driver": "multiple"}
          for profile in PROFILES]


class ReadPropertiesTest(unittest.TestCase):
    def test_the_rc73ya_offers_all_three_profiles(self):
        fields = read_properties({"ActiveProfile": PERFORMANCE, "Profiles": RC73YA, "PerformanceDegraded": "",
                                  "ActiveProfileHolds": [], "Version": "0.30", "BatteryAware": True})
        self.assertEqual(fields, {"active": PERFORMANCE, "available": PROFILES, "degraded": (), "holds": ()})

    def test_a_profile_only_the_placeholder_backs_is_not_offered(self):
        placeholder = [{"Profile": SAVER, "PlatformDriver": "placeholder", "Driver": "placeholder"},
                       {"Profile": BALANCED, "PlatformDriver": "placeholder", "Driver": "placeholder"}]
        self.assertEqual(read_properties({"ActiveProfile": BALANCED, "Profiles": placeholder})["available"], ())

    def test_the_drivers_are_judged_per_profile(self):
        mixed = [{"Profile": SAVER, "CpuDriver": "intel_pstate", "PlatformDriver": "placeholder"},
                 {"Profile": BALANCED, "PlatformDriver": "placeholder"},
                 {"Profile": PERFORMANCE, "CpuDriver": "intel_pstate"}]
        self.assertEqual(read_properties({"Profiles": mixed})["available"], (SAVER, PERFORMANCE))

    def test_profiles_this_version_does_not_know_are_ignored_and_the_order_is_the_contracts(self):
        listed = [{"Profile": name, "CpuDriver": "amd_pstate"} for name in (PERFORMANCE, "turbo", SAVER)]
        self.assertEqual(read_properties({"Profiles": listed})["available"], (SAVER, PERFORMANCE))

    def test_more_profiles_than_the_daemon_ever_lists_make_the_reading_unusable(self):
        entry = {"Profile": BALANCED, "CpuDriver": "amd_pstate"}
        self.assertEqual(read_properties({"Profiles": [entry] * powerprofiles.MAX_PROFILES})["available"], (BALANCED,))
        with self.assertRaises(Unusable):
            read_properties({"Profiles": [entry] * (powerprofiles.MAX_PROFILES + 1)})
        for reading in (None, [], "ActiveProfile", 3):
            with self.subTest(reading=reading), self.assertRaises(Unusable):
                read_properties(reading)

    def test_an_active_profile_this_version_does_not_know_is_unknown(self):
        for active in ("turbo", "", "Performance", " balanced"):
            with self.subTest(active=active):
                self.assertEqual(read_properties({"ActiveProfile": active})["active"], "")

    def test_a_wrongly_typed_property_falls_back_and_never_raises(self):
        fields = read_properties({"ActiveProfile": 3, "Profiles": PERFORMANCE, "PerformanceDegraded": ["lap-detected"],
                                  "ActiveProfileHolds": {"ApplicationId": "org.example.Game"}})
        self.assertEqual(fields, {"active": "", "available": (), "degraded": (), "holds": ()})
        fields = read_properties({"Profiles": [1, "balanced", None, {"Profile": 5, "CpuDriver": "amd_pstate"},
                                               {"Profile": BALANCED, "CpuDriver": 7}],
                                  "ActiveProfileHolds": [1, {"Profile": None},
                                                         {"Profile": PERFORMANCE, "ApplicationId": 9}]})
        self.assertEqual((fields["available"], fields["holds"]), ((), (Hold(PERFORMANCE, ""),)))

    def test_several_reasons_for_holding_performance_back_are_read(self):
        fields = read_properties({"PerformanceDegraded": "lap-detected,high-operating-temperature"})
        self.assertEqual(fields["degraded"], (power.LAP, power.HEAT))

    def test_a_hold_keeps_a_clean_short_name_and_never_its_reason(self):
        holds = [{"ApplicationId": "\u202eorg.example.Game\u200f\n" + "x" * 10_000, "Profile": PERFORMANCE,
                  "Reason": "a reason nobody should see"},
                 {"ApplicationId": "Steam", "Profile": SAVER, "Reason": "battery"},
                 {"ApplicationId": "org.example.Other", "Profile": "turbo"}]
        fields = read_properties({"ActiveProfileHolds": holds})
        self.assertEqual(fields["holds"], (Hold(PERFORMANCE, "org.example.Game " + "x" * 15), Hold(SAVER, "Steam")))
        self.assertNotIn("nobody", repr(fields))

    def test_at_most_sixteen_holds_are_kept(self):
        holds = [{"ApplicationId": f"org.example.Game{index}", "Profile": PERFORMANCE} for index in range(40)]
        kept = read_properties({"ActiveProfileHolds": holds})["holds"]
        self.assertEqual([hold.name for hold in kept], [f"org.example.Game{index}" for index in range(16)])

    def test_a_change_gives_only_what_it_names(self):
        self.assertEqual(read_properties({"ActiveProfile": BALANCED}, complete=False), {"active": BALANCED})
        self.assertEqual(read_properties({"Version": "0.31", "BatteryAware": False}, complete=False), {})


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.root = scratch(self, "sys")

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def evidence(self, firmware, boost):
        self.write(powerprofiles.PLATFORM_PROFILE, firmware)
        self.write(powerprofiles.CPU_BOOST, boost)
        return powerprofiles.read_evidence(str(self.root))

    def test_what_the_kernel_was_told_in_each_profile(self):
        self.assertEqual(self.evidence("performance\n", "1\n"), Evidence("performance", "on"))
        self.assertEqual(self.evidence("quiet\n", "0\n"), Evidence("quiet", "off"))
        self.assertEqual(self.evidence("custom\n", "1\n"), Evidence("custom", "on"))

    def test_missing_files_say_nothing(self):
        self.assertEqual(powerprofiles.read_evidence(str(self.root)), Evidence())
        self.assertEqual(powerprofiles.read_evidence(str(self.root / "absent")), Evidence())

    def test_files_that_cannot_be_read_say_nothing(self):
        (self.root / powerprofiles.PLATFORM_PROFILE).mkdir(parents=True)
        (self.root / powerprofiles.CPU_BOOST).mkdir(parents=True)
        self.assertEqual(powerprofiles.read_evidence(str(self.root)), Evidence())

    @unittest.skipIf(os.geteuid() == 0, "root reads a file without permissions")
    def test_a_file_without_permission_to_read_it_says_nothing(self):
        self.write(powerprofiles.CPU_BOOST, "1\n")
        self.write(powerprofiles.PLATFORM_PROFILE, "balanced\n").chmod(0)
        self.assertEqual(powerprofiles.read_evidence(str(self.root)), Evidence("", "on"))

    def test_a_file_longer_than_a_word_says_nothing_whatever_it_starts_with(self):
        limit = powerprofiles.MAX_FILE_BYTES
        self.assertEqual(self.evidence("performance" + " " * (limit - 11), "1" + "\n" * (limit - 1)),
                         Evidence("performance", "on"))
        self.assertEqual(self.evidence("performance" + " " * (limit - 10), "1" + "\n" * limit), Evidence())

    def test_text_that_is_not_a_kernel_word_says_nothing(self):
        for firmware in ("p\u00e9rformance\n", b"\xff\xfeperformance\n", "Performance\n", "balanced performance\n",
                         "\u202eperformance\n", "performance\x00\n"):
            with self.subTest(firmware=firmware):
                self.assertEqual(self.evidence(firmware, "yes\n"), Evidence())


class HelperEnvironmentTest(unittest.TestCase):
    ENVIRON = {"PATH": "/home/user/.local/share/mise/shims:/usr/bin", "HOME": "/home/user", "LANG": "en_US.UTF-8",
               "XDG_RUNTIME_DIR": "/run/user/1000", "XDG_STATE_HOME": "/home/user/.local/state",
               "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "LD_PRELOAD": "/home/user/hook.so",
               "PYTHONPATH": "/home/user/python", "VIRTUAL_ENV": "/home/user/venv", "LC_ALL": "C"}

    def test_the_path_is_the_commands_own_directory_and_the_systems_never_the_inherited_one(self):
        script = "/home/user/.local/share/omarchy/bin/omarchy-powerprofiles-set"
        self.assertEqual(powerprofiles.helper_environment(script, self.ENVIRON), {
            "PATH": "/home/user/.local/share/omarchy/bin:/usr/local/bin:/usr/bin:/bin", "HOME": "/home/user",
            "LANG": "en_US.UTF-8", "XDG_RUNTIME_DIR": "/run/user/1000", "XDG_STATE_HOME": "/home/user/.local/state",
            "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket"})

    def test_variables_that_are_not_set_are_left_out_and_the_path_has_each_directory_once(self):
        environment = powerprofiles.helper_environment("/usr/bin/omarchy-powerprofiles-set",
                                                       {"PATH": "/opt/shims", "HOME": "/home/user"})
        self.assertEqual(environment, {"PATH": "/usr/bin:/usr/local/bin:/bin", "HOME": "/home/user"})

    def test_a_directory_that_cannot_be_put_in_a_path_is_left_out(self):
        self.assertEqual(powerprofiles.helper_environment("/opt/a:b/omarchy-powerprofiles-set", {}),
                         {"PATH": "/usr/local/bin:/usr/bin:/bin"})


class BusCase(unittest.TestCase):
    """Against the fake daemon on a private bus. Every test ends by checking each call its sources sent."""

    @classmethod
    def setUpClass(cls):
        cls.bus = PrivateBus.start()
        cls.addClassCleanup(cls.bus.stop)

    def setUp(self):
        self.root = scratch(self, "sys")
        self.kernel("performance", "1")
        self.timers = Timers()
        self.fakes, self.sources, self.seen = [], [], []
        self.addCleanup(self.finish)

    def kernel(self, firmware, boost):
        for relative, text in ((powerprofiles.PLATFORM_PROFILE, firmware), (powerprofiles.CPU_BOOST, boost)):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{text}\n", encoding="utf-8")

    def finish(self):
        for source in self.sources:
            source.stop()
        try:
            senders = {source._connection.get_unique_name() for source in self.sources}
            for fake in self.fakes:
                for call in fake.calls:
                    if call.sender in senders:
                        self.assert_allowed(call, fake)
        finally:
            self.bus.close_connections()

    def assert_allowed(self, call, fake):
        """A call of the client: on the allow-list, to the owner's unique name, never starting a service."""
        self.assertEqual((call.path, call.interface), (OBJECT_PATH, PROPERTIES_IFACE), call)
        if call.member == "GetAll":
            self.assertEqual(call.arguments, (INTERFACE,), call)
        else:
            self.assertEqual(call.member, "Set", call)
            self.assertEqual(call.arguments[:2], (INTERFACE, "ActiveProfile"), call)
            self.assertIn(call.arguments[2], PROFILES, call)
        self.assertEqual(call.destination, fake.unique_name, call)
        self.assertFalse(call.auto_start, call)

    def fake(self, own=True, **state) -> FakePPD:
        fake = FakePPD(self.bus, **state)
        self.fakes.append(fake)
        if own:
            self.assertTrue(fake.own())
        return fake

    def source(self, on_change=None, **options) -> PowerProfiles:
        options = {"connection": self.bus.connect(), "schedule": self.timers.schedule, "cancel": self.timers.cancel,
                   "find_helper": lambda: None, "sysfs_root": str(self.root), **options}
        source = PowerProfiles(on_change or (lambda: self.seen.append(source.snapshot)), **options)
        self.sources.append(source)
        return source

    def ready(self, source=None) -> PowerProfiles:
        """Start a source and wait for its own reading of the service (a restarted one shows the last one at once)."""
        source = source or self.source()
        sender = source._connection.get_unique_name()

        def readings():
            return sum(call.member == "GetAll" for fake in self.fakes for call in fake.sent_by(sender))
        before = readings()
        source.start()
        self.assertTrue(pump(lambda: readings() > before and source.snapshot.state == READY and source.idle),
                        "the service was not read")
        return source

    def sent(self, fake, source) -> list:
        return fake.sent_by(source._connection.get_unique_name())

    @staticmethod
    def deliver(source, changed=None, sender=None, path=OBJECT_PATH, epoch=None, parameters=None):
        """Hand the source a PropertiesChanged the way the bus would, without the bus."""
        if parameters is None:
            parameters = GLib.Variant("(sa{sv}as)", (INTERFACE, changed or {}, []))
        source._on_properties_changed(None, sender or source.owner, path, PROPERTIES_IFACE, "PropertiesChanged",
                                      parameters, source._epoch if epoch is None else epoch)


class ReadingTest(BusCase):
    def test_a_cold_start_reads_the_service_once_and_shows_what_it_said(self):
        fake = self.fake()
        source = self.ready()
        snapshot = source.snapshot
        self.assertEqual((snapshot.state, snapshot.active, snapshot.available, snapshot.degraded, snapshot.holds),
                         (READY, PERFORMANCE, PROFILES, (), ()))
        self.assertEqual((snapshot.pending, snapshot.failure, snapshot.note, snapshot.usable), ("", None, "", True))
        self.assertEqual(snapshot.evidence, Evidence("performance", "on"))
        self.assertEqual([call.member for call in self.sent(fake, source)], ["GetAll"])
        self.assertEqual(source.owner, fake.unique_name)

    def test_until_the_service_answers_nothing_is_known(self):
        fake = self.fake()
        fake.hold = lambda call: True
        source = self.source()
        source.start()
        self.assertTrue(pump(lambda: fake.held))
        self.assertEqual((source.snapshot.state, source.snapshot.visible, source.idle), (UNKNOWN, False, False))
        fake.answer_held()
        self.assertTrue(pump(lambda: source.snapshot.state == READY))

    def test_without_a_service_the_state_is_absent_and_nothing_is_sent(self):
        bystander = self.fake(own=False)
        source = self.source()
        source.start()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        source.retry()
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((bystander.calls, source.idle, source.snapshot.visible), ([], True, False))

    def test_garbage_from_the_service_is_an_error_until_a_retry_reads_it_once(self):
        fake = self.fake()
        fake.garbage = GLib.Variant("(s)", ("not properties",))
        source = self.source()
        source.start()
        self.assertTrue(pump(lambda: source.snapshot.state == ERROR))
        self.assertEqual((source.snapshot.active, source.snapshot.available, source.snapshot.visible), ("", (), True))
        fake.garbage = None
        source.retry()
        source.retry()                                                 # one read, however often it is asked
        self.assertTrue(pump(lambda: source.snapshot.state == READY))
        source.retry()                                                 # nothing to retry once it is read
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(fake.count("GetAll"), 2)

    def test_a_reply_over_the_cap_or_with_too_many_profiles_is_an_error(self):
        for spoil in ({"extra": {"Padding": GLib.Variant("s", "x" * powerprofiles.MAX_REPLY_BYTES)}},
                      {"profiles": fake_ppd.RC73YA * 3}):
            with self.subTest(spoil=list(spoil)):
                fake = self.fake(profiles=spoil.get("profiles", fake_ppd.RC73YA))
                fake.extra.update(spoil.get("extra", {}))
                source = self.source()
                source.start()
                self.assertTrue(pump(lambda: source.snapshot.state == ERROR))
                source.stop()
                fake.release()

    def test_a_reading_that_runs_out_of_time_is_an_error(self):
        fake = self.fake()
        fake.hold = lambda call: True
        source = self.source()
        with mock.patch.object(powerprofiles, "CALL_TIMEOUT_MS", 100):
            source.start()
            self.assertTrue(pump(lambda: source.snapshot.state == ERROR, 3))
        self.assertTrue(source.idle)
        fake.hold = None
        source.retry()
        self.assertTrue(pump(lambda: source.snapshot.state == READY))

    def test_a_refused_reading_is_an_error_and_the_services_words_are_not_kept(self):
        fake = self.fake()
        fake.failures["GetAll"] = ("org.freedesktop.DBus.Error.AccessDenied", "words of the daemon")
        source = self.source()
        source.start()
        self.assertTrue(pump(lambda: source.snapshot.state == ERROR))
        self.assertNotIn("words", repr(source.snapshot))

    def test_a_wrongly_typed_property_is_read_as_its_empty_value(self):
        fake = self.fake()
        fake.extra["ActiveProfile"] = GLib.Variant("i", 3)
        snapshot = self.ready().snapshot
        self.assertEqual((snapshot.state, snapshot.active, snapshot.available), (READY, "", PROFILES))

    def test_profiles_only_the_placeholder_backs_are_not_offered(self):
        self.fake(active=BALANCED, profiles=fake_ppd.PLACEHOLDER)
        snapshot = self.ready().snapshot
        self.assertEqual((snapshot.active, snapshot.available, snapshot.usable, snapshot.visible),
                         (BALANCED, (), False, False))

    def test_drivers_are_judged_per_profile_and_one_real_profile_is_no_choice(self):
        self.fake(active=BALANCED, profiles=((SAVER, "", "placeholder"), (BALANCED, "amd_pstate", "placeholder"),
                                             (PERFORMANCE, "", "")))
        snapshot = self.ready().snapshot
        self.assertEqual((snapshot.state, snapshot.available, snapshot.usable, snapshot.visible),
                         (READY, (BALANCED,), False, False))

    def test_an_active_profile_this_version_does_not_know_is_unknown(self):
        self.fake(active="low-power", profiles=fake_ppd.RC73YA + (("low-power", "amd_pstate", "platform_profile"),))
        snapshot = self.ready().snapshot
        self.assertEqual((snapshot.active, snapshot.available, snapshot.usable), ("", PROFILES, True))


class SignalTest(BusCase):
    def test_what_the_service_announces_is_followed_without_asking_again(self):
        fake = self.fake()
        source = self.ready()
        self.kernel("quiet", "0")
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER))
        self.assertEqual(source.snapshot.evidence, Evidence("quiet", "off"))
        fake.set_degraded("lap-detected,high-operating-temperature")
        self.assertTrue(pump(lambda: source.snapshot.degraded == (power.LAP, power.HEAT)))
        fake.add_hold(PERFORMANCE, "\u202eorg.example.Game\u200f" + "x" * 10_000, reason="a reason nobody sees")
        self.assertTrue(pump(lambda: source.snapshot.holds))
        self.assertEqual(source.snapshot.holds, (Hold(PERFORMANCE, "org.example.Game" + "x" * 16),))
        self.assertNotIn("nobody", repr(source.snapshot))
        self.assertEqual(source.snapshot.note, "")                      # nobody asked for anything here
        self.assertEqual([call.member for call in self.sent(fake, source)], ["GetAll"])
        self.assertTrue(source.idle)

    def test_a_hold_stays_listed_when_another_program_changes_the_profile(self):
        fake = self.fake(active=PERFORMANCE)
        fake.add_hold(PERFORMANCE, "org.example.Game", signal=False)
        source = self.ready()
        fake.external(BALANCED)
        self.assertTrue(pump(lambda: source.snapshot.active == BALANCED))
        self.assertEqual(source.snapshot.holds, (Hold(PERFORMANCE, "org.example.Game"),))   # listed, not in force

    def test_signals_of_anyone_but_the_owner_and_of_other_objects_are_ignored(self):
        fake = self.fake(active=PERFORMANCE)
        source = self.ready()
        saver = {"ActiveProfile": GLib.Variant("s", SAVER)}
        self.bus.connect().emit_signal(None, OBJECT_PATH, PROPERTIES_IFACE, "PropertiesChanged",
                                       GLib.Variant("(sa{sv}as)", (INTERFACE, saver, [])))
        for path, interface in ((OBJECT_PATH, "org.example.Other"), (fake_ppd.LEGACY_PATH, fake_ppd.LEGACY_INTERFACE)):
            fake.connection.emit_signal(None, path, PROPERTIES_IFACE, "PropertiesChanged",
                                        GLib.Variant("(sa{sv}as)", (interface, saver, [])))
        fake.set_degraded("lap-detected")                              # after those, from the owner itself
        self.assertTrue(pump(lambda: source.snapshot.degraded == (power.LAP,)))
        self.assertFalse(pump(lambda: source.snapshot.active != PERFORMANCE, 0.05))
        for sender, path, epoch in ((":1.999", OBJECT_PATH, None), (None, fake_ppd.LEGACY_PATH, None),
                                    (None, OBJECT_PATH, source._epoch - 1)):
            self.deliver(source, saver, sender=sender, path=path, epoch=epoch)
        self.assertEqual(source.snapshot.active, PERFORMANCE)

    def test_a_change_that_changes_nothing_keeps_the_same_snapshot_and_tells_nobody(self):
        self.fake()
        source = self.ready()
        snapshot, told = source.snapshot, len(self.seen)
        self.deliver(source, {"ActiveProfile": GLib.Variant("s", PERFORMANCE), "Version": GLib.Variant("s", "0.31")})
        self.assertIs(source.snapshot, snapshot)
        self.assertEqual(len(self.seen), told)

    def test_a_signal_the_source_cannot_use_is_an_error_until_a_retry(self):
        self.fake()
        too_many = GLib.Variant("aa{sv}", [{"Profile": GLib.Variant("s", BALANCED)}] * 9)
        for parameters in (GLib.Variant("(s)", (INTERFACE,)),
                           GLib.Variant("(sa{sv}as)", (INTERFACE, {"Profiles": too_many}, [])),
                           GLib.Variant("(sa{sv}as)", (INTERFACE, {"x": GLib.Variant("s", "x" * 70_000)}, []))):
            with self.subTest(parameters=parameters.get_type_string()):
                source = self.ready()
                self.deliver(source, parameters=parameters)
                self.assertEqual((source.snapshot.state, source.snapshot.active), (ERROR, ""))
                source.retry()
                self.assertTrue(pump(lambda: source.snapshot.state == READY))
                source.stop()


class OwnerTest(BusCase):
    def test_the_owner_leaving_is_absent_and_a_new_owner_is_read_from_scratch(self):
        first = self.fake(active=PERFORMANCE)
        first.add_hold(PERFORMANCE, "org.example.Game", signal=False)
        source = self.ready()
        first.release()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        snapshot = source.snapshot
        self.assertEqual((snapshot.active, snapshot.available, snapshot.holds, source.owner), ("", (), (), None))
        asked = len(first.calls)
        second = self.fake(active=SAVER)
        self.assertTrue(pump(lambda: source.snapshot.state == READY))
        self.assertEqual((source.snapshot.active, source.snapshot.holds, source.owner), (SAVER, (), second.unique_name))
        first.external(BALANCED)                                       # the old owner no longer counts
        second.set_degraded("lap-detected")
        self.assertTrue(pump(lambda: source.snapshot.degraded == (power.LAP,)))
        self.assertEqual((source.snapshot.active, len(first.calls)), (SAVER, asked))

    def test_a_late_reply_for_a_previous_owner_is_ignored(self):
        first = self.fake(active=PERFORMANCE)
        first.hold = lambda call: True
        source = self.source()
        source.start()
        self.assertTrue(pump(lambda: first.held))
        first.release()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        self.fake(active=SAVER)
        self.assertTrue(pump(lambda: source.snapshot.state == READY))
        first.answer_held()
        self.assertFalse(pump(lambda: source.snapshot.active != SAVER, 0.1))


class LifecycleTest(BusCase):
    def test_a_started_untouched_source_holds_no_timer_no_call_and_no_process(self):
        fake = self.fake()
        source = self.ready()
        self.assertEqual((source.idle, self.timers.pending, self.timers.delays), (True, {}, []))
        calls = len(fake.calls)
        self.assertFalse(pump(timeout=0.08))
        self.assertEqual(len(fake.calls), calls)

    def test_start_and_stop_are_idempotent_and_each_start_reads_once(self):
        fake = self.fake()
        source = self.source()
        source.start()
        source.start()
        self.assertTrue(pump(lambda: source.snapshot.state == READY and source.idle))
        source.stop()
        source.stop()
        source.start()
        self.assertTrue(pump(lambda: fake.count("GetAll") == 2 and source.idle))
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(fake.count("GetAll"), 2)

    def test_while_stopped_nothing_is_heard_asked_or_told(self):
        fake = self.fake()
        source = self.ready()
        source.stop()
        told, calls = len(self.seen), len(fake.calls)
        fake.external(SAVER)
        source.retry()
        source.dismiss()
        self.assertFalse(pump(timeout=0.08))
        self.assertEqual((len(self.seen), len(fake.calls), source.snapshot.active, source.owner),
                         (told, calls, PERFORMANCE, None))

    def test_what_was_last_read_stays_across_stop_and_start_so_the_header_does_not_blink(self):
        fake = self.fake()
        source = self.ready()
        before = source.snapshot
        source.stop()
        self.assertIs(source.snapshot, before)
        fake.hold = lambda call: True
        source.start()
        self.assertTrue(pump(lambda: fake.held))
        self.assertEqual(source.snapshot, before)
        fake.external(SAVER)                                           # while the reading is on its way
        fake.answer_held()
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER and source.idle))

    def test_stopping_mid_read_leaves_nothing_behind_and_tells_nobody(self):
        fake = self.fake()
        fake.hold = lambda call: True
        source = self.source()
        source.start()
        self.assertTrue(pump(lambda: fake.held))
        source.stop()
        told = len(self.seen)
        fake.answer_held()
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((len(self.seen), source.snapshot.state, source.idle, source.owner),
                         (told, UNKNOWN, True, None))

    def test_stop_clears_the_request_its_failure_and_its_note_and_keeps_what_was_read(self):
        fake = self.fake(active=BALANCED)
        source = self.ready()
        fake.deny = True
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: source.snapshot.failure is not None))
        source.stop()
        self.assertEqual((source.snapshot.failure, source.snapshot.state, source.snapshot.active),
                         (None, READY, BALANCED))
        self.ready(source)
        fake.deny = False
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: not source.snapshot.pending))
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.note == CHANGED_ELSEWHERE))
        source.stop()
        self.assertEqual((source.snapshot.note, source.snapshot.active), ("", SAVER))

    def test_stop_forgets_a_request_on_its_way_and_the_next_start_reads_the_service_again(self):
        fake = self.fake(active=SAVER)
        fake.hold = lambda call: call.member == "Set"
        source = self.ready()
        source.set_profile(BALANCED)
        self.assertTrue(pump(lambda: fake.held))
        told = len(self.seen)
        source.stop()
        self.assertEqual((source.snapshot.pending, source.snapshot.failure, source.idle), ("", None, True))
        fake.answer_held()                                             # the service carries it out all the same
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((len(self.seen), source.snapshot.active, self.timers.pending), (told, SAVER, {}))
        self.ready(source)
        self.assertEqual((source.snapshot.active, source.snapshot.failure), (BALANCED, None))

    def test_an_exception_in_on_change_does_not_break_the_source(self):
        fake = self.fake()
        told = []

        def on_change():
            told.append(source.snapshot.active)
            raise RuntimeError("the window is gone")
        source = self.ready(self.source(on_change))
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER))
        self.assertEqual((told[-1], source.handler_error), (SAVER, "RuntimeError"))

    def test_a_call_outside_the_allow_list_is_refused_and_never_sent(self):
        fake = self.fake()
        source = self.ready()
        sent = len(fake.calls)
        for path, interface, member, parameters in (
                (OBJECT_PATH, INTERFACE, "HoldProfile", GLib.Variant("(sss)", (PERFORMANCE, "", "org.example.Game"))),
                (OBJECT_PATH, PROPERTIES_IFACE, "Set", set_call(False, "b", name="BatteryAware")),
                (OBJECT_PATH, PROPERTIES_IFACE, "Set", set_call("turbo")),
                (OBJECT_PATH, PROPERTIES_IFACE, "Get", GLib.Variant("(ss)", (INTERFACE, "ActiveProfile")))):
            with self.subTest(member=member), self.assertRaises(ValueError):
                source._call(path, interface, member, parameters, "()", 1000, lambda *_: None)
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(len(fake.calls), sent)


class SetTest(BusCase):
    def sets(self, fake, source) -> list:
        return [call.arguments[2] for call in self.sent(fake, source) if call.member == "Set"]

    def settle(self, source):
        self.assertTrue(pump(lambda: not source.snapshot.pending and source.idle), "the request did not end")

    def test_every_profile_can_be_chosen_from_every_other_one(self):
        fake = self.fake(active=SAVER)
        source = self.ready()
        for profile in (BALANCED, PERFORMANCE, SAVER, PERFORMANCE, BALANCED, SAVER):
            with self.subTest(profile=profile):
                before = source.snapshot.active
                source.set_profile(profile)
                self.assertEqual((source.snapshot.pending, source.snapshot.active, self.seen[-1].pending),
                                 (profile, before, profile))
                self.settle(source)
                snapshot = source.snapshot
                self.assertEqual((snapshot.active, snapshot.failure, snapshot.note), (profile, None, ""))
        self.assertEqual(self.sets(fake, source), [BALANCED, PERFORMANCE, SAVER, PERFORMANCE, BALANCED, SAVER])
        self.assertEqual(fake.count("GetAll"), 1)

    def test_the_reply_is_the_outcome_and_nothing_waits_after_it(self):
        fake = self.fake(active=BALANCED)
        fake.late = lambda call: call.member == "Set"
        source = self.ready()
        self.kernel("performance", "1")
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: source.snapshot.active == PERFORMANCE))     # applied and announced ...
        self.assertEqual(source.snapshot.pending, PERFORMANCE)                    # ... but only the answer ends it
        fake.answer_held()
        self.settle(source)
        self.assertEqual((source.snapshot.failure, source.snapshot.note), (None, ""))
        self.assertEqual((self.timers.pending, self.timers.delays, fake.count("GetAll")), ({}, [], 1))

    def test_choosing_the_active_profile_asks_the_service_nothing(self):
        fake = self.fake(active=PERFORMANCE)
        fake.add_hold(PERFORMANCE, "org.example.Game", signal=False)
        source = self.ready()
        source.set_profile(PERFORMANCE)
        snapshot = source.snapshot
        self.assertEqual((snapshot.pending, snapshot.failure, snapshot.note, source.idle), ("", None, "", True))
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((self.sets(fake, source), len(fake.holds)), ([], 1))
        self.assertEqual(source.snapshot.holds, (Hold(PERFORMANCE, "org.example.Game"),))

    def test_choosing_another_profile_ends_every_hold(self):
        fake = self.fake(active=PERFORMANCE)
        fake.add_hold(PERFORMANCE, "org.example.Game", signal=False)
        fake.add_hold(SAVER, "org.example.Reader", signal=False)
        source = self.ready()
        self.assertEqual(len(source.snapshot.holds), 2)
        source.set_profile(BALANCED)
        self.settle(source)
        self.assertEqual((source.snapshot.active, source.snapshot.holds, fake.released), (BALANCED, (), [1, 2]))

    def test_without_a_service_that_was_read_a_request_is_unavailable_and_nothing_is_sent(self):
        source = self.source()
        source.set_profile(PERFORMANCE)                                # not started: ignored
        self.assertIsNone(source.snapshot.failure)
        source.start()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        source.set_profile(PERFORMANCE)
        self.assertEqual(source.snapshot.failure, Failure(UNAVAILABLE, PERFORMANCE))
        fake = self.fake()
        fake.garbage = GLib.Variant("(s)", ("garbage",))
        self.assertTrue(pump(lambda: source.snapshot.state == ERROR))
        source.set_profile(SAVER)
        self.assertEqual(source.snapshot.failure, Failure(UNAVAILABLE, SAVER))
        self.assertEqual(self.sets(fake, source), [])

    def test_a_request_before_this_visits_own_reading_is_unavailable(self):
        fake = self.fake(active=BALANCED)
        source = self.ready()
        source.stop()
        fake.hold = lambda call: True
        source.start()                                                 # the last reading is shown at once
        source.set_profile(PERFORMANCE)
        self.assertEqual((source.snapshot.state, source.snapshot.failure), (READY, Failure(UNAVAILABLE, PERFORMANCE)))
        self.assertTrue(pump(lambda: fake.held))
        source.set_profile(SAVER)                                      # the reading is still on its way
        self.assertEqual(source.snapshot.failure, Failure(UNAVAILABLE, SAVER))
        fake.hold = None
        fake.answer_held()
        self.assertTrue(pump(lambda: source.idle))
        source.set_profile(PERFORMANCE)
        self.settle(source)
        self.assertEqual((source.snapshot.active, source.snapshot.failure, self.sets(fake, source)),
                         (PERFORMANCE, None, [PERFORMANCE]))

    def test_a_profile_the_machine_does_not_offer_is_unsupported_and_nothing_is_sent(self):
        fake = self.fake(active=BALANCED, profiles=fake_ppd.PLACEHOLDER)
        source = self.ready()
        for profile, asked in ((BALANCED, BALANCED), (PERFORMANCE, PERFORMANCE), ("turbo", ""), (None, ""), ([], "")):
            with self.subTest(profile=profile):
                source.set_profile(profile)
                self.assertEqual(source.snapshot.failure, Failure(UNSUPPORTED, asked))
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(self.sets(fake, source), [])

    def test_a_request_is_ignored_while_another_is_on_its_way(self):
        fake = self.fake(active=BALANCED)
        fake.hold = lambda call: call.member == "Set"
        source = self.ready()
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: fake.held))
        source.set_profile(SAVER)
        source.set_profile(PERFORMANCE)
        self.assertEqual(source.snapshot.pending, PERFORMANCE)
        fake.answer_held()
        self.settle(source)
        self.assertEqual((source.snapshot.active, self.sets(fake, source)), (PERFORMANCE, [PERFORMANCE]))

    def test_each_refusal_becomes_its_failure_without_the_services_words(self):
        fake = self.fake(active=BALANCED)
        source = self.ready()
        for setup, code in ((lambda: setattr(fake, "deny", True), DENIED),
                            (lambda: fake.failures.update(Set=("org.freedesktop.DBus.Error.InvalidArgs", "words")),
                             FAILED),
                            (lambda: fake.failures.update(Set=("org.freedesktop.DBus.Error.Failed", "words")), FAILED),
                            (lambda: fake.failures.update(Set=("org.freedesktop.DBus.Error.ServiceUnknown", "words")),
                             UNAVAILABLE)):
            with self.subTest(code=code):
                fake.deny = False
                fake.failures.clear()
                setup()
                source.set_profile(PERFORMANCE)
                self.settle(source)
                snapshot = source.snapshot
                self.assertEqual((snapshot.failure, snapshot.active, snapshot.note), (Failure(code, PERFORMANCE),
                                                                                      BALANCED, ""))
                self.assertNotIn("words", repr(snapshot))
                source.dismiss()
                self.assertIsNone(source.snapshot.failure)

    def test_a_new_request_clears_the_failure_of_the_last_one(self):
        fake = self.fake(active=BALANCED)
        fake.deny = True
        source = self.ready()
        source.set_profile(PERFORMANCE)
        self.settle(source)
        fake.deny = False
        source.set_profile(SAVER)
        self.assertEqual((source.snapshot.failure, source.snapshot.pending), (None, SAVER))
        self.settle(source)
        self.assertEqual((source.snapshot.active, source.snapshot.failure), (SAVER, None))

    def test_a_request_that_runs_out_of_time_is_a_timeout_that_goes_when_the_profile_arrives(self):
        fake = self.fake(active=BALANCED)
        fake.hold = lambda call: call.member == "Set"
        source = self.ready()
        with mock.patch.object(powerprofiles, "SET_TIMEOUT_MS", 100):
            source.set_profile(PERFORMANCE)
            self.assertTrue(pump(lambda: source.snapshot.failure is not None, 3))
        self.assertEqual((source.snapshot.failure, source.snapshot.pending, source.idle),
                         (Failure(TIMEOUT, PERFORMANCE), "", True))
        fake.answer_held()                                             # the service was only slow
        self.assertTrue(pump(lambda: source.snapshot.failure is None))
        self.assertEqual((source.snapshot.active, source.snapshot.note), (PERFORMANCE, ""))

    def test_a_timeout_after_the_profile_was_announced_is_no_failure(self):
        fake = self.fake(active=BALANCED)
        fake.late = lambda call: call.member == "Set"
        source = self.ready()
        with mock.patch.object(powerprofiles, "SET_TIMEOUT_MS", 100):
            source.set_profile(PERFORMANCE)
            self.assertTrue(pump(lambda: not source.snapshot.pending, 3))
        self.assertEqual((source.snapshot.active, source.snapshot.failure), (PERFORMANCE, None))

    def test_the_owner_leaving_during_a_request_ends_it_as_unavailable(self):
        fake = self.fake(active=BALANCED)
        fake.hold = lambda call: call.member == "Set"
        source = self.ready()
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: fake.held))
        fake.release()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        snapshot = source.snapshot
        self.assertEqual((snapshot.failure, snapshot.pending, source.idle),
                         (Failure(UNAVAILABLE, PERFORMANCE), "", True))

    def test_the_reply_of_an_owner_that_restarted_mid_call_is_ignored(self):
        first = self.fake(active=BALANCED)
        first.hold = lambda call: call.member == "Set"
        source = self.ready()
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: first.held))
        first.release()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        second = self.fake(active=SAVER)
        self.assertTrue(pump(lambda: source.snapshot.state == READY))
        first.answer_held()                                            # applies there, answers here: too late
        self.assertFalse(pump(lambda: source.snapshot.active != SAVER or source.snapshot.note, 0.1))
        self.assertEqual((source.snapshot.failure, self.sets(second, source)), (Failure(UNAVAILABLE, PERFORMANCE), []))

    def test_no_public_method_raises_whatever_it_is_given(self):
        self.fake()
        source = self.ready()
        for argument in (None, 3, [], {}, object(), "x" * 10_000):
            source.set_profile(argument)
        source.retry()
        source.dismiss()
        self.assertEqual(source.internal_error, None)


class ChangedElsewhereTest(BusCase):
    def chosen(self, profile, active=BALANCED):
        fake = self.fake(active=active)
        source = self.ready()
        source.set_profile(profile)
        self.assertTrue(pump(lambda: not source.snapshot.pending))
        return fake, source

    def test_another_programs_change_after_a_successful_request_is_noted_and_kept(self):
        fake, source = self.chosen(PERFORMANCE)
        self.assertEqual(source.snapshot.note, "")
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.note == CHANGED_ELSEWHERE))
        self.assertEqual(source.snapshot.active, SAVER)
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual([call.arguments[2] for call in fake.writes()], [PERFORMANCE])     # nothing switched back

    def test_choosing_the_active_profile_counts_as_a_request(self):
        fake, source = self.chosen(BALANCED)
        fake.external(PERFORMANCE)
        self.assertTrue(pump(lambda: source.snapshot.note == CHANGED_ELSEWHERE))

    def test_a_change_when_nothing_was_asked_or_after_dismiss_or_a_failure_is_not_noted(self):
        fake = self.fake(active=BALANCED)
        source = self.ready()
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER))
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: not source.snapshot.pending))
        source.dismiss()
        fake.external(BALANCED)
        self.assertTrue(pump(lambda: source.snapshot.active == BALANCED))
        fake.deny = True
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: source.snapshot.failure is not None))
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER))
        self.assertEqual(source.snapshot.note, "")
        self.assertNotIn(CHANGED_ELSEWHERE, [snapshot.note for snapshot in self.seen])

    def test_a_change_while_the_request_is_on_its_way_and_ours_lands_last_is_not_noted(self):
        fake = self.fake(active=BALANCED)
        fake.hold = lambda call: call.member == "Set"
        source = self.ready()
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: fake.held))
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER))
        self.assertEqual((source.snapshot.pending, source.snapshot.note), (PERFORMANCE, ""))
        fake.answer_held()
        self.assertTrue(pump(lambda: not source.snapshot.pending))
        self.assertEqual((source.snapshot.active, source.snapshot.note), (PERFORMANCE, ""))

    def test_a_change_between_the_services_answer_and_its_arrival_is_noted(self):
        fake = self.fake(active=BALANCED)
        fake.late = lambda call: call.member == "Set"
        source = self.ready()
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: fake.held))
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER))
        fake.answer_held()
        self.assertTrue(pump(lambda: not source.snapshot.pending))
        self.assertEqual((source.snapshot.active, source.snapshot.note, source.snapshot.failure),
                         (SAVER, CHANGED_ELSEWHERE, None))

    def test_stop_forgets_the_request_and_the_next_visit_notes_nothing(self):
        fake, source = self.chosen(PERFORMANCE)
        source.stop()
        source.start()
        self.assertTrue(pump(lambda: source.idle and fake.count("GetAll") == 2))
        fake.external(SAVER)
        self.assertTrue(pump(lambda: source.snapshot.active == SAVER))
        self.assertEqual(source.snapshot.note, "")


class RememberTest(BusCase):
    """Omarchy's command, played by a stand-in that the source finds through the injected lookup."""

    def setUp(self):
        super().setUp()
        self.home = scratch(self, "helper")
        self.addCleanup(self.end_stand_ins)

    def end_stand_ins(self):
        """A failed test must not leave a stand-in behind: end every group one recorded, and let GLib reap it."""
        for stat in self.home.glob("*/record/stat"):
            with contextlib.suppress(OSError, ValueError, IndexError):
                os.killpg(int(stat.read_text().rsplit(")", 1)[1].split()[2]), signal.SIGKILL)
        pump(lambda: not any(source.helping for source in self.sources), 3)

    def with_helper(self, script) -> PowerProfiles:
        return self.ready(self.source(find_helper=lambda: str(script)))

    def record(self, script) -> dict:
        record = script.parent / "record"
        stat = (record / "stat").read_text()
        return {"argv": (record / "argv").read_text().splitlines(),
                "environ": dict(entry.split("=", 1) for entry in (record / "environ").read_text().split("\0") if entry),
                "stdin": (record / "stdin").read_text(), "fds": (record / "fds").read_text().splitlines(),
                "pid": int(stat.split()[0]), "group": int(stat.rsplit(")", 1)[1].split()[2]),
                "python3": (record / "python3").read_text().strip()}

    def child_of(self, script) -> int:
        """The process the hanging stand-in started, once it has."""
        child = script.parent / "record" / "child"
        self.assertTrue(pump(lambda: child.exists() and child.read_text().strip()), "the stand-in did not start")
        return int(child.read_text())

    def open_gate(self, script):
        """Let a gated stand-in go on. The pipe stays open until the test ends, so the line waits for its reader."""
        descriptor = os.open(script.parent / "gate", os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, descriptor)
        os.write(descriptor, b"go\n")

    def test_after_a_change_omarchys_command_keeps_it_and_gets_only_what_it_needs(self):
        script = stand_in(self.home / "ok")
        shims = self.home / "shims"
        shims.mkdir()
        (shims / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (shims / "python3").chmod(0o755)
        self.fake(active=BALANCED)
        source = self.with_helper(script)
        environment = {"PATH": f"{shims}:{os.environ.get('PATH', '/usr/bin')}", "HOME": "/home/user",
                       "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": "/run/user/1000",
                       "XDG_STATE_HOME": "/home/user/.local/state",
                       "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket",
                       "GAMING_HOME_TEST_LEAK": "1"}
        with mock.patch.dict(os.environ, environment):
            source.set_profile(PERFORMANCE)
            self.assertTrue(pump(lambda: not source.snapshot.pending))
        self.assertTrue(pump(lambda: source.idle))
        self.assertEqual((source.snapshot.active, source.snapshot.note, source.snapshot.failure),
                         (PERFORMANCE, "", None))
        seen = self.record(script)
        self.assertEqual(seen["argv"], [str(script), "autodetect", PERFORMANCE])
        self.assertEqual(seen["environ"], {"PATH": f"{script.parent}:/usr/local/bin:/usr/bin:/bin",
                                           **{name: environment[name] for name in powerprofiles.HELPER_ENVIRONMENT}})
        self.assertFalse(seen["python3"].startswith(str(shims)), seen["python3"])
        self.assertEqual((seen["stdin"], seen["fds"]), ("", ["/dev/null"] * 3))
        self.assertEqual(seen["group"], seen["pid"])                   # a process group of its own
        self.assertNotEqual(seen["group"], os.getpgrp())
        self.assertEqual((self.timers.delays, self.timers.pending, len(self.timers.cancelled)),
                         ([powerprofiles.HELPER_LIMIT_MS], {}, 1))

    def test_choosing_the_active_profile_still_tells_omarchy_and_asks_the_service_nothing(self):
        script = stand_in(self.home / "ok")
        fake = self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(BALANCED)
        self.assertTrue(source.helping)
        self.assertTrue(pump(lambda: source.idle))
        self.assertEqual(self.record(script)["argv"], [str(script), "autodetect", BALANCED])
        self.assertEqual(([call.member for call in self.sent(fake, source)], source.snapshot.note), (["GetAll"], ""))

    def test_a_command_that_fails_is_a_note_and_the_profile_stays_set(self):
        script = stand_in(self.home / "fail", "fail")
        fake = self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: source.snapshot.note == NOT_REMEMBERED and source.idle))
        self.assertEqual((source.snapshot.active, source.snapshot.failure, fake.active),
                         (PERFORMANCE, None, PERFORMANCE))

    def test_a_command_that_hangs_is_killed_with_everything_it_started_when_its_limit_runs_out(self):
        script = stand_in(self.home / "hang", "hang")
        self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(PERFORMANCE)
        child = self.child_of(script)
        leader = self.record(script)["pid"]
        self.assertEqual((source.snapshot.pending, source.helping, self.timers.delays),
                         ("", True, [powerprofiles.HELPER_LIMIT_MS]))
        self.timers.fire()
        self.assertTrue(pump(lambda: not source.helping and gone(child), 3), "the group outlived its limit")
        self.assertTrue(gone(leader))
        self.assertEqual((source.snapshot.note, source.idle, self.timers.pending), (NOT_REMEMBERED, True, {}))

    def test_without_omarchys_command_the_step_is_skipped_without_a_note(self):
        fake = self.fake(active=BALANCED)
        for lookup in (lambda: None, lambda: "omarchy-powerprofiles-set", lambda: 1 / 0):
            with self.subTest():
                source = self.ready(self.source(find_helper=lookup))
                source.set_profile(SAVER if fake.active != SAVER else BALANCED)
                self.assertTrue(pump(lambda: source.idle and not source.snapshot.pending))
                self.assertEqual((source.snapshot.note, source.helping, self.timers.delays), ("", False, []))
                source.stop()

    def test_a_command_that_cannot_be_started_is_a_note(self):
        script = self.home / "broken" / "omarchy-powerprofiles-set"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")         # not executable
        self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: source.snapshot.note == NOT_REMEMBERED and source.idle))
        self.assertEqual((source.snapshot.failure, self.timers.pending), (None, {}))

    def test_the_command_runs_on_after_stop_and_reports_nothing(self):
        script = stand_in(self.home / "gated", "fail", gate=True)
        self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(PERFORMANCE)
        self.assertTrue(pump(lambda: (script.parent / "record" / "waiting").exists()))
        self.assertEqual(source.snapshot.pending, "")                  # the request ended with the reply
        source.stop()
        told = len(self.seen)
        limit, = self.timers.pending                                   # the one thing left: the command's own limit
        self.assertEqual((self.timers.delays, source.helping, source.owner),
                         ([powerprofiles.HELPER_LIMIT_MS], True, None))
        self.open_gate(script)
        self.assertTrue(pump(lambda: not source.helping))
        self.assertEqual((len(self.seen), source.snapshot.note, self.timers.pending, self.timers.cancelled),
                         (told, "", {}, [limit]))

    def test_after_stop_a_command_that_hangs_still_ends_at_its_limit_and_reports_nothing(self):
        script = stand_in(self.home / "hang", "hang")
        self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(PERFORMANCE)
        child = self.child_of(script)
        source.stop()
        told = len(self.seen)
        self.timers.fire()
        self.assertTrue(pump(lambda: not source.helping and gone(child), 3))
        self.assertEqual((len(self.seen), source.snapshot.note, self.timers.pending), (told, "", {}))

    def test_a_new_choice_ends_the_command_still_running_for_the_last_one(self):
        first, second = stand_in(self.home / "first", "hang"), stand_in(self.home / "second")
        scripts = iter((first, second))
        self.fake(active=BALANCED)
        source = self.ready(self.source(find_helper=lambda: str(next(scripts))))
        source.set_profile(PERFORMANCE)
        child = self.child_of(first)
        source.set_profile(SAVER)
        self.assertTrue(pump(lambda: gone(child) and source.idle, 3))
        self.assertEqual(self.record(second)["argv"][1:], ["autodetect", SAVER])
        self.assertEqual((source.snapshot.active, source.snapshot.note, self.timers.pending), (SAVER, "", {}))

    def test_another_programs_change_ends_the_command_so_it_cannot_switch_back(self):
        script = stand_in(self.home / "hang", "hang")
        fake = self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(PERFORMANCE)
        child = self.child_of(script)
        fake.external(SAVER)
        self.assertTrue(pump(lambda: gone(child) and not source.helping, 3))
        self.assertEqual((source.snapshot.note, source.snapshot.active, self.timers.pending),
                         (CHANGED_ELSEWHERE, SAVER, {}))


    def test_a_command_left_running_by_the_last_visit_ends_when_another_program_changes_the_profile(self):
        script = stand_in(self.home / "hang", "hang")
        fake = self.fake(active=BALANCED)
        source = self.with_helper(script)
        source.set_profile(PERFORMANCE)
        child = self.child_of(script)
        source.stop()
        source.start()                                                 # not idle: the command still runs
        self.assertTrue(pump(lambda: fake.count("GetAll") == 2))       # answered before the change below is sent
        fake.external(SAVER)
        self.assertTrue(pump(lambda: gone(child) and not source.helping, 3))
        self.assertEqual((source.snapshot.note, source.snapshot.active, self.timers.pending), ("", SAVER, {}))


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.root = str(scratch(self, "sys"))

    def test_the_name_is_only_watched_on_the_system_bus_so_the_daemon_is_never_started(self):
        source = PowerProfiles(lambda: None, sysfs_root=self.root)
        with mock.patch.object(powerprofiles.Gio, "bus_watch_name", return_value=7) as watch, \
                mock.patch.object(powerprofiles.Gio, "bus_unwatch_name") as unwatch:
            source.start()
            source.start()
            self.assertEqual(watch.call_count, 1)
            self.assertEqual(watch.call_args.args[:3],
                             (Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameWatcherFlags.NONE))
            source.stop()
            source.stop()
            unwatch.assert_called_once_with(7)

    def test_nothing_is_watched_or_read_before_start(self):
        with mock.patch.object(powerprofiles.Gio, "bus_watch_name") as watch:
            source = PowerProfiles(lambda: None, sysfs_root=self.root)
            source.stop()
        watch.assert_not_called()
        self.assertEqual(source.snapshot, power.Snapshot())

    def test_the_module_names_no_call_that_holds_a_profile_or_writes_anything_else(self):
        text = Path(powerprofiles.__file__).read_text(encoding="utf-8")
        strings = {node.value for node in ast.walk(ast.parse(text))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertFalse(strings & {"HoldProfile", "ReleaseProfile", "SetActionEnabled", "BatteryAware", "Get",
                                    "Introspect"})
        self.assertNotIn("net.hadess", text)
        self.assertNotIn("call_sync", text)
        self.assertNotIn("AUTO_START", text.replace("NO_AUTO_START", ""))


class NeverStartedTest(unittest.TestCase):
    """This private bus *can* start the daemon by its name, and a control name proves that it would."""

    CONTROL = "org.example.Control"

    def setUp(self):
        directory = scratch(self, "activation")
        self.started, self.control = directory / "daemon-started", directory / "control-started"
        config = activatable_bus_config(directory / "bus", {BUS_NAME: self.started, self.CONTROL: self.control})
        self.bus = PrivateBus.start(config)
        self.addCleanup(self.bus.stop)
        self.start_service(self.CONTROL)
        if not pump(self.control.exists, 3):
            self.skipTest("this bus cannot start services here")

    def start_service(self, name):
        """Ask the bus to start ``name``. The command only leaves its marker, so no reply is awaited."""
        self.bus.connect().call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "StartServiceByName", GLib.Variant("(su)", (name, 0)), None, Gio.DBusCallFlags.NONE,
                                5000, None, lambda _bus, _result, _data: None, None)

    def test_the_bus_starts_the_daemon_when_it_is_asked_to(self):
        self.start_service(BUS_NAME)
        self.assertTrue(pump(self.started.exists, 3), "this bus cannot start the daemon: the next test proves nothing")

    def test_watching_reading_and_asking_never_start_it(self):
        source = PowerProfiles(lambda: None, connection=self.bus.connect(), find_helper=lambda: None,
                               sysfs_root=str(self.started.parent))
        self.addCleanup(source.stop)
        source.start()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        source.retry()
        source.set_profile(PERFORMANCE)
        self.assertEqual(source.snapshot.failure, Failure(UNAVAILABLE, PERFORMANCE))
        source.stop()
        source.start()
        self.assertTrue(pump(lambda: source.snapshot.state == ABSENT))
        self.assertFalse(pump(self.started.exists, 0.3))


class FakeServiceTest(unittest.TestCase):
    """The fake does what the client relies on the daemon doing. Without that the other tests would prove nothing."""

    @classmethod
    def setUpClass(cls):
        cls.bus = PrivateBus.start()
        cls.addClassCleanup(cls.bus.stop)

    def setUp(self):
        self.addCleanup(self.bus.close_connections)
        self.fake = FakePPD(self.bus, active="balanced")
        self.assertTrue(self.fake.own())
        self.caller, self.events = self.bus.connect(), []
        self.caller.signal_subscribe(self.fake.unique_name, PROPERTIES_IFACE, "PropertiesChanged", OBJECT_PATH,
                                     INTERFACE, Gio.DBusSignalFlags.NONE,
                                     lambda *signal: self.events.append(("changed", signal[5].unpack()[1])))

    def call(self, interface, member, parameters, caller=None, destination=BUS_NAME, flags=Gio.DBusCallFlags.NONE):
        replies = []

        def done(bus, result, _data):
            try:
                reply = bus.call_finish(result).unpack()
            except GLib.Error as error:
                reply = Gio.DBusError.get_remote_error(error) or error.message
            self.events.append(("reply", reply))
            replies.append(reply)
        (caller or self.caller).call(destination, OBJECT_PATH, interface, member, parameters, None, flags, 2000, None,
                                     done, None)
        self.assertTrue(pump(lambda: replies), f"{member} was not answered")
        return replies[0]

    def set_profile(self, profile):
        return self.call(PROPERTIES_IFACE, "Set",
                         GLib.Variant("(ssv)", (INTERFACE, "ActiveProfile", GLib.Variant("s", profile))))

    def test_it_serves_the_properties_the_daemon_has(self):
        values, = self.call(PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", (INTERFACE,)))
        self.assertEqual(set(values), set(TYPES))
        self.assertEqual(values["ActiveProfile"], "balanced")
        self.assertEqual([entry["Profile"] for entry in values["Profiles"]], ["power-saver", "balanced", "performance"])

    def test_a_change_is_announced_before_the_reply(self):
        self.assertEqual(self.set_profile("performance"), ())
        self.assertEqual(self.events, [("changed", {"ActiveProfile": "performance"}), ("reply", ())])

    def test_choosing_the_active_profile_answers_announces_nothing_and_keeps_the_holds(self):
        cookie = self.fake.add_hold("performance", "org.example.Game", signal=False)
        self.assertEqual(self.set_profile("balanced"), ())
        self.assertFalse(pump(lambda: len(self.events) > 1, 0.05))
        self.assertEqual(self.events, [("reply", ())])
        self.assertEqual([hold["cookie"] for hold in self.fake.holds], [cookie])

    def test_choosing_another_profile_releases_every_hold_and_tells_the_holder(self):
        holder, released = self.bus.connect(), []
        holder.signal_subscribe(self.fake.unique_name, INTERFACE, "ProfileReleased", OBJECT_PATH, None,
                                Gio.DBusSignalFlags.NONE, lambda *signal: released.append(signal[5].unpack()[0]))
        hold = GLib.Variant("(sss)", ("performance", "testing", "org.example.Game"))
        cookie, = self.call(INTERFACE, "HoldProfile", hold, caller=holder)
        self.fake.add_hold("power-saver", "org.example.Other", signal=False)
        self.assertEqual(self.fake.active, "performance")
        self.assertEqual(self.set_profile("balanced"), ())
        self.assertTrue(pump(lambda: released))
        self.assertEqual((released, self.fake.holds, self.fake.released), ([cookie], [], [cookie, cookie + 1]))
        self.assertEqual(self.events[-2], ("changed", {"ActiveProfile": "balanced", "ActiveProfileHolds": []}))

    def test_another_program_changing_the_profile_releases_no_hold(self):
        self.fake.add_hold("performance", "org.example.Game", signal=False)
        self.fake.external("power-saver")
        self.assertTrue(pump(lambda: self.events))
        self.assertEqual(self.events, [("changed", {"ActiveProfile": "power-saver"})])
        self.assertEqual(len(self.fake.holds), 1)

    def test_a_refusal_and_an_unknown_profile_are_errors_that_change_nothing(self):
        self.assertEqual(self.set_profile("turbo"), "org.freedesktop.DBus.Error.InvalidArgs")
        self.fake.deny = True
        self.assertEqual(self.set_profile("performance"), "org.freedesktop.DBus.Error.AccessDenied")
        self.assertEqual((self.fake.active, [kind for kind, _ in self.events]), ("balanced", ["reply", "reply"]))

    def test_every_call_is_logged_with_its_address_and_whether_it_could_start_a_service(self):
        self.call(INTERFACE, "SetActionEnabled", GLib.Variant("(sb)", ("trickle_charge", False)))
        self.call(PROPERTIES_IFACE, "Set",
                  GLib.Variant("(ssv)", (INTERFACE, "BatteryAware", GLib.Variant("b", False))))
        self.call(PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", (INTERFACE,)), destination=self.fake.unique_name,
                  flags=Gio.DBusCallFlags.NO_AUTO_START)
        writes = self.fake.writes()
        self.assertEqual([call.member for call in writes], ["SetActionEnabled", "Set"])
        self.assertEqual({(call.sender, call.destination, call.auto_start) for call in writes},
                         {(self.caller.get_unique_name(), BUS_NAME, True)})
        read = self.fake.calls[-1]
        self.assertEqual((read.member, read.destination, read.auto_start), ("GetAll", self.fake.unique_name, False))
        self.assertFalse(self.fake.battery_aware)


if __name__ == "__main__":
    unittest.main()
