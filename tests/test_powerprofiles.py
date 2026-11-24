"""The power profile client, the fake power-profiles-daemon it is tested against, and the ``power`` command.

The pure parts (parsing, the error table, the allow-list, the evidence files, the helper's environment) need no
D-Bus. Everything else runs against a fake power-profiles-daemon on a private ``dbus-daemon`` (skipped when there is
none), a scratch sysfs tree and a stand-in for Omarchy's ``omarchy-powerprofiles-set``. Nothing here talks to the
real daemon or changes the machine's profile.
"""

import unittest

from gi.repository import Gio, GLib

from fakes.fake_inputplumber import PrivateBus
from fakes.fake_ppd import BUS_NAME, INTERFACE, OBJECT_PATH, PROPERTIES_IFACE, TYPES, FakePPD
from fakes.loop import pump


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
