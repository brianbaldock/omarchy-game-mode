"""The power profile client, the fake power-profiles-daemon it is tested against, and the ``power`` command.

The pure parts (parsing, the error table, the allow-list, the evidence files, the helper's environment) need no
D-Bus. Everything else runs against a fake power-profiles-daemon on a private ``dbus-daemon`` (skipped when there is
none), a scratch sysfs tree and a stand-in for Omarchy's ``omarchy-powerprofiles-set``. Nothing here talks to the
real daemon or changes the machine's profile.
"""

import os
import shutil
import unittest
from pathlib import Path

from gi.repository import Gio, GLib

from fakes.fake_inputplumber import PrivateBus
from fakes.fake_ppd import BUS_NAME, INTERFACE, OBJECT_PATH, PROPERTIES_IFACE, TYPES, FakePPD
from fakes.loop import pump
from gaminghome import power, powerprofiles
from gaminghome.power import BALANCED, PERFORMANCE, PROFILES, SAVER, Evidence, Hold
from gaminghome.powerprofiles import Unusable, read_properties

SCRATCH = Path(__file__).resolve().parent / "scratch" / "powerprofiles"


def scratch(test, name) -> Path:
    """An empty directory of the git-ignored scratch area, removed again after the test."""
    directory = SCRATCH / name
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    test.addCleanup(shutil.rmtree, SCRATCH, True)
    return directory


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
