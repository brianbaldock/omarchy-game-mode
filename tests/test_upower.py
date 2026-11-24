"""The read-only UPower client.

Two layers: the client against a stub connection (always runs, fully deterministic), and the client against a fake
UPower on a private bus (skipped when ``dbus-daemon`` is not installed). Every bus test ends by proving that the
fake recorded nothing but reads, and one shows that UPower is not started by watching for it.
"""

import ast
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gi.repository import Gio, GLib

from fakes.fake_inputplumber import PrivateBus
from fakes.fake_upower import (BUS_NAME, DEVICE_IFACE, DISPLAY_DEVICE, PROPERTIES_IFACE, READS, FakeUPower, StubBus,
                               StubClient, activatable_bus_config, reply)
from fakes.loop import pump
from fakes.rc73ya import rc73ya
from gaminghome import upower
from gaminghome.battery import CHARGING, DISCHARGING, Battery
from gaminghome.upower import UPowerClient

SCRATCH = Path(__file__).resolve().parent / "scratch" / "upower"
RC73YA = Battery(78, DISCHARGING, 28173)


class WatchTest(unittest.TestCase):
    def test_the_name_is_only_watched_so_upower_is_never_started(self):
        client = UPowerClient(lambda: None)
        with mock.patch.object(upower.Gio, "bus_watch_name", return_value=7) as watch, \
                mock.patch.object(upower.Gio, "bus_unwatch_name") as unwatch:
            client.start()
            client.start()
            self.assertEqual(watch.call_count, 1)
            bus_type, name, flags = watch.call_args.args[:3]
            self.assertEqual((bus_type, name), (Gio.BusType.SYSTEM, BUS_NAME))
            self.assertEqual(flags, Gio.BusNameWatcherFlags.NONE)
            self.assertFalse(flags & Gio.BusNameWatcherFlags.AUTO_START)
            client.stop()
            client.stop()
            unwatch.assert_called_once_with(7)

    def test_a_given_connection_is_watched_the_same_way(self):
        connection = object()
        client = UPowerClient(lambda: None, connection=connection)
        with mock.patch.object(upower.Gio, "bus_watch_name_on_connection", return_value=9) as watch, \
                mock.patch.object(upower.Gio, "bus_unwatch_name"):
            client.start()
            self.assertEqual(watch.call_args.args[:3], (connection, BUS_NAME, Gio.BusNameWatcherFlags.NONE))
            client.stop()

    def test_nothing_is_watched_before_start(self):
        with mock.patch.object(upower.Gio, "bus_watch_name") as watch:
            UPowerClient(lambda: None)
            self.assertEqual(watch.call_count, 0)


class ClientLogicTest(unittest.TestCase):
    def setUp(self):
        self.bus, self.seen = StubBus(), []
        self.client = StubClient(lambda: self.seen.append((self.client.state, self.client.battery)))
        self.client.start()

    def test_calls_and_subscriptions_are_pinned_to_the_current_unique_owner(self):
        self.assertEqual(self.client.state, "unknown")                 # the bus has not said anything yet
        self.client.appear(self.bus)
        subscription, = self.bus.subscriptions.values()
        self.assertEqual((subscription.sender, subscription.interface, subscription.member, subscription.path,
                          subscription.arg0, subscription.flags),
                         (":1.42", PROPERTIES_IFACE, "PropertiesChanged", DISPLAY_DEVICE, DEVICE_IFACE,
                          Gio.DBusSignalFlags.NONE))
        call, = self.bus.calls
        self.assertEqual((call.bus_name, call.path, call.interface, call.method, call.parameters.unpack()),
                         (":1.42", DISPLAY_DEVICE, PROPERTIES_IFACE, "GetAll", (DEVICE_IFACE,)))
        self.assertTrue(call.flags & Gio.DBusCallFlags.NO_AUTO_START)
        self.assertEqual(call.timeout, upower.TIMEOUT_MS)
        self.assertIsNotNone(call.cancellable)

    def test_the_answer_becomes_the_battery(self):
        self.client.appear(self.bus)
        self.assertEqual((self.client.state, self.client.battery), ("pending", None))
        self.bus.answer_all()
        self.assertEqual((self.client.state, self.client.battery), ("ready", RC73YA))
        self.assertEqual([state for state, _ in self.seen], ["pending", "ready"])
        self.assertIsNone(self.client.error)

    def test_no_battery_is_an_answer_too(self):
        self.client.appear(self.bus)
        self.bus.answer_all(rc73ya(IsPresent=False, Type=0, State=0, Percentage=0.0))
        self.assertEqual((self.client.state, self.client.battery), ("ready", None))

    def test_a_change_signal_is_one_read_and_a_burst_is_one_more(self):
        self.client.appear(self.bus)
        self.bus.answer_all()
        reads = self.client.reads
        self.assertEqual(self.bus.emit_changed(":1.42"), 1)
        self.assertEqual(len(self.bus.pending()), 1)
        for _ in range(50):
            self.bus.emit_changed(":1.42")
        self.assertEqual(len(self.bus.pending()), 1)                   # the same read: nothing else is sent
        self.bus.answer_all(rc73ya(Percentage=77.0))
        self.assertEqual(self.client.battery.percent, 77)
        self.assertEqual(len(self.bus.pending()), 1)                   # one more, for the burst
        self.bus.answer_all(rc73ya(Percentage=76.0))
        self.assertEqual((self.bus.pending(), self.client.battery.percent, self.client.reads - reads), ([], 76, 2))
        self.assertTrue(self.client.idle)

    def test_a_quiet_client_sends_nothing(self):
        self.client.appear(self.bus)
        self.bus.answer_all()
        sent = len(self.bus.calls)
        self.assertTrue(self.client.idle)
        self.assertEqual((len(self.bus.calls), self.bus.pending()), (sent, []))

    def test_signals_from_anyone_else_are_not_hints(self):
        self.client.appear(self.bus)
        self.bus.answer_all()
        self.assertEqual(self.bus.emit_changed(":1.99"), 0)             # the bus does not deliver it to this client
        subscription, = self.bus.subscriptions.values()
        epoch = subscription.data[0]
        signal = (self.bus, ":1.99", DISPLAY_DEVICE, PROPERTIES_IFACE, "PropertiesChanged", None)
        subscription.callback(*signal, epoch)                           # and if it got through, it is dropped
        subscription.callback(self.bus, ":1.42", *signal[2:], epoch - 1)   # as is a signal of an earlier owner
        self.assertEqual(self.bus.pending(), [])

    def test_a_failed_read_is_a_state_and_the_next_hint_tries_again(self):
        self.client.appear(self.bus)
        self.bus.answer_all()
        self.bus.emit_changed(":1.42")
        call, = self.bus.pending()
        self.bus.answer(call, GLib.Error("timed out", "g-io-error-quark", 24))
        self.assertEqual((self.client.state, self.client.idle), ("failed", True))
        self.assertIn("timed out", self.client.error)
        self.bus.emit_changed(":1.42")
        self.bus.answer_all(rc73ya(Percentage=60.0))
        self.assertEqual((self.client.state, self.client.error, self.client.battery.percent), ("ready", None, 60))

    def test_a_reply_that_is_too_large_or_the_wrong_shape_fails_the_read(self):
        self.client.appear(self.bus)
        oversized = SimpleNamespace(get_size=lambda: upower.MAX_REPLY_BYTES + 1,
                                    unpack=lambda: self.fail("an oversized reply was unpacked"))
        self.bus.answer(self.bus.pending()[0], oversized)
        self.assertEqual(self.client.state, "failed")
        self.assertIn("too large", self.client.error)
        self.client.refresh()
        self.bus.answer(self.bus.pending()[0], GLib.Variant("(s)", ("not a dictionary",)))
        self.assertEqual((self.client.state, self.client.error), ("failed", "unexpected reply"))
        self.client.refresh()
        self.bus.answer_all()
        self.assertEqual(self.client.state, "ready")

    def test_refresh_reads_once_whatever_is_in_flight(self):
        self.client.refresh()                                           # nobody is there: nothing to ask
        self.assertEqual(self.bus.calls, [])
        self.client.appear(self.bus)
        for _ in range(5):
            self.client.refresh()
        self.assertEqual(len(self.bus.pending()), 1)
        self.bus.answer_all()
        self.assertEqual(len(self.bus.pending()), 1)                    # exactly one more for the five requests
        self.bus.answer_all()
        self.assertEqual(self.bus.pending(), [])

    def test_the_name_vanishing_forgets_everything_and_invalidates_replies(self):
        self.client.appear(self.bus)
        call, = self.bus.calls
        self.client.vanish(self.bus)
        self.assertTrue(call.cancellable.is_cancelled())
        self.assertEqual((self.bus.subscriptions, len(self.bus.unsubscribed)), ({}, 1))
        self.assertEqual((self.client.state, self.client.battery, self.client.owner), ("absent", None, None))
        self.bus.answer(call, reply(rc73ya()))                          # the reply of the owner that is gone
        self.assertEqual((self.client.state, self.client.battery), ("absent", None))

    def test_without_a_system_bus_the_name_is_simply_absent(self):
        self.client.vanish(None)                                        # what the name watcher reports then
        self.client.refresh()
        self.assertEqual((self.client.state, self.client.reads, self.bus.calls), ("absent", 0, []))

    def test_a_new_owner_is_read_and_the_old_ones_replies_are_ignored(self):
        self.client.appear(self.bus)
        old, = self.bus.calls
        self.client.vanish(self.bus)
        self.client.appear(self.bus, ":1.77")
        self.bus.answer(old, reply(rc73ya(Percentage=11.0)))            # a late reply addressed to the old owner
        self.assertEqual((self.client.state, self.client.battery), ("pending", None))
        self.bus.answer_all(rc73ya(Percentage=90.0))
        self.assertEqual(self.client.battery.percent, 90)
        self.assertEqual({call.bus_name for call in self.bus.calls[1:]}, {":1.77"})
        subscription, = self.bus.subscriptions.values()
        self.assertEqual(subscription.sender, ":1.77")

    def test_a_name_that_is_not_a_unique_name_is_never_called(self):
        self.client.appear(self.bus, "com.example.NotUnique")
        self.assertEqual((self.client.state, self.bus.calls, self.client.idle), ("failed", [], True))

    def test_stop_makes_pending_replies_and_queued_callbacks_harmless(self):
        self.client.appear(self.bus)
        call, = self.bus.calls
        changes = len(self.seen)
        self.client.stop()
        self.client.stop()
        self.bus.answer(call, reply(rc73ya()))
        self.client.appear(self.bus, ":1.99")                           # a name-watch callback that was already queued
        self.client.vanish(self.bus)
        self.client.refresh()
        self.assertEqual((self.client.owner, self.client.state, len(self.seen), self.bus.subscriptions),
                         (None, "unknown", changes, {}))
        self.assertEqual(len(self.bus.calls), 1)

    def test_starting_and_stopping_over_and_over_holds_nothing_while_stopped(self):
        for _ in range(10):
            self.client.start()
            self.client.appear(self.bus)
            self.bus.answer_all()
            self.assertEqual(len(self.bus.subscriptions), 1)
            self.client.stop()
            self.assertEqual((self.bus.subscriptions, self.client.owner, self.client.idle), ({}, None, True))
        self.assertEqual(self.client.unwatched, self.client.watches)    # every watch was given back
        self.assertEqual(len(self.client.watches), 10)
        self.assertEqual(len(self.bus.unsubscribed), 10)

    def test_only_reads_can_be_sent(self):
        self.client.appear(self.bus)
        self.bus.answer_all()
        sent = len(self.bus.calls)
        for interface, method in ((DEVICE_IFACE, "Refresh"), (DEVICE_IFACE, "EnableChargeThreshold"),
                                  (PROPERTIES_IFACE, "Set"), (PROPERTIES_IFACE, "Get"), (DEVICE_IFACE, "GetAll")):
            with self.assertRaises(ValueError, msg=method):
                self.client._call(DISPLAY_DEVICE, interface, method, None, "()", lambda *_arguments: None)
        self.assertEqual((len(self.bus.calls), self.client.idle), (sent, True))

    def test_a_call_that_cannot_be_sent_fails_the_read_instead_of_stalling_it(self):
        with mock.patch.object(self.bus, "call", side_effect=TypeError("the connection refused the call")):
            self.client.appear(self.bus)
        self.assertEqual((self.client.state, self.client.idle), ("failed", True))
        self.assertIn("refused the call", self.client.error)
        self.client.refresh()
        self.bus.answer_all()
        self.assertEqual((self.client.state, self.client.battery), ("ready", RC73YA))

    def test_exceptions_in_callbacks_never_escape_and_do_not_wedge_the_client(self):
        self.client.appear(self.bus)
        unexpected = SimpleNamespace(get_size=lambda: 0, unpack=lambda: 1 / 0)
        self.bus.answer(self.bus.pending()[0], unexpected)
        self.assertIn("ZeroDivisionError", self.client.error)
        self.assertEqual((self.client.state, self.client.idle), ("failed", True))
        self.client.on_change = lambda: 1 / 0                           # a failing consumer
        self.client.refresh()
        self.bus.answer_all()
        self.assertEqual(self.client.state, "ready")
        self.assertIn("change handler failed", self.client.handler_error)


class ReadOnlyContractTest(unittest.TestCase):
    def test_the_client_names_nothing_that_changes_upower_or_starts_it(self):
        source = Path(upower.__file__).read_text(encoding="utf-8")
        strings = {node.value for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertFalse(strings & {"Refresh", "EnableChargeThreshold", "Set", "Get", "Introspect"}, strings)
        self.assertEqual(upower.READ_METHODS, {(PROPERTIES_IFACE, "GetAll")})
        self.assertNotIn("call_sync", source)
        self.assertNotIn("AUTO_START", source.replace("NO_AUTO_START", ""))   # it never asks for activation

    def test_the_fake_does_record_writes(self):
        # Without this, "no writes were recorded" would prove nothing.
        try:
            bus = PrivateBus.start()
        except unittest.SkipTest:
            self.skipTest("dbus-daemon is not installed")
        self.addCleanup(bus.stop)
        fake = FakeUPower(bus)
        self.assertTrue(fake.own())
        caller, done = bus.connect(), []
        for interface, member, arguments in (
                (DEVICE_IFACE, "Refresh", None), (DEVICE_IFACE, "EnableChargeThreshold", GLib.Variant("(b)", (True,))),
                (PROPERTIES_IFACE, "Set", GLib.Variant("(ssv)", (DEVICE_IFACE, "State", GLib.Variant("u", 1))))):
            caller.call(BUS_NAME, DISPLAY_DEVICE, interface, member, arguments, None, Gio.DBusCallFlags.NONE, 2000,
                        None, lambda _bus, _result, _data: done.append(1), None)
        self.assertTrue(pump(lambda: len(done) == 3))
        self.assertEqual([call[3] for call in fake.writes()], ["Refresh", "EnableChargeThreshold", "Set"])
        self.assertEqual(fake.values["State"], 2)


class ClientBusTest(unittest.TestCase):
    """Against the fake service on a private bus."""

    @classmethod
    def setUpClass(cls):
        cls.bus = PrivateBus.start()
        cls.addClassCleanup(cls.bus.stop)

    def setUp(self):
        self.fakes, self.clients, self.changes = [], [], []
        self.addCleanup(self.finish)

    def finish(self):
        for client in self.clients:
            client.stop()
        try:
            for fake in self.fakes:
                self.assertEqual(fake.writes(), [], "UPower must only ever be read")
                self.assertLessEqual(set(fake.members()), READS)
        finally:
            self.bus.close_connections()

    def fake(self, own=True, **changes) -> FakeUPower:
        fake = FakeUPower(self.bus, **changes)
        self.fakes.append(fake)
        if own:
            self.assertTrue(fake.own())
        return fake

    def client(self) -> UPowerClient:
        client = UPowerClient(lambda: self.changes.append((client.state, client.battery)),
                              connection=self.bus.connect())
        self.clients.append(client)
        return client

    def ready(self, client):
        client.start()
        self.assertTrue(pump(lambda: client.state == "ready"), "the client was not read")

    def test_reads_the_display_device_and_nothing_else(self):
        fake = self.fake()
        client = self.client()
        self.ready(client)
        self.assertEqual(client.battery, RC73YA)
        call, = fake.calls
        self.assertEqual(call, (client._connection.get_unique_name(), DISPLAY_DEVICE, PROPERTIES_IFACE, "GetAll",
                                (DEVICE_IFACE,)))
        self.assertEqual(client.owner, fake.unique_name)

    def test_a_change_signal_is_one_more_read(self):
        fake = self.fake()
        client = self.client()
        self.ready(client)
        fake.set(Percentage=77.0, TimeToEmpty=27000, State=1, TimeToFull=3900)
        self.assertTrue(pump(lambda: client.battery == Battery(77, CHARGING, 3900)))
        self.assertEqual(fake.count("GetAll"), 2)

    def test_a_quiet_client_makes_no_calls(self):
        fake = self.fake()
        client = self.client()
        self.ready(client)
        calls = len(fake.calls)
        self.assertFalse(pump(timeout=0.08))
        self.assertEqual(len(fake.calls), calls)
        self.assertTrue(client.idle)

    def test_a_flood_of_signals_is_a_bounded_number_of_reads(self):
        fake = self.fake()
        client = self.client()
        self.ready(client)
        seen, reads = [], fake.count("GetAll")
        client._connection.signal_subscribe(None, PROPERTIES_IFACE, "PropertiesChanged", None, None,
                                            Gio.DBusSignalFlags.NONE, lambda *_args: seen.append(1))
        for _ in range(300):
            fake.emit_changed(UpdateTime=1795502081)
        self.assertTrue(pump(lambda: len(seen) == 300))
        self.assertTrue(pump(lambda: client.idle))
        self.assertGreaterEqual(fake.count("GetAll") - reads, 1)
        self.assertLessEqual(fake.count("GetAll") - reads, 8)          # 300 signals, but never one read each

    def test_a_service_that_is_not_there_is_never_called(self):
        bystander = self.fake(own=False)                  # the object exists, but nobody owns the name
        client = self.client()
        client.start()
        self.assertTrue(pump(lambda: client.state == "absent"))
        client.refresh()
        self.assertEqual((bystander.calls, client.reads), ([], 0))

    def test_a_machine_without_a_battery_answers_no_battery(self):
        self.fake(IsPresent=False, Type=0, State=0, Percentage=0.0, TimeToEmpty=0)
        client = self.client()
        self.ready(client)
        self.assertIsNone(client.battery)

    def test_a_refused_read_is_a_failure_and_a_later_request_recovers(self):
        fake = self.fake()
        fake.failures["GetAll"] = ("org.freedesktop.DBus.Error.AccessDenied", "not allowed")
        client = self.client()
        client.start()
        self.assertTrue(pump(lambda: client.state == "failed"))
        self.assertIn("AccessDenied", client.error)
        del fake.failures["GetAll"]
        client.refresh()
        self.assertTrue(pump(lambda: client.state == "ready"))
        self.assertEqual(client.battery, RC73YA)

    def test_an_owner_change_drops_the_old_reading_and_reads_only_the_new_owner(self):
        first = self.fake()
        client = self.client()
        self.ready(client)
        first.release()
        self.assertTrue(pump(lambda: client.state == "absent"))
        self.assertIsNone(client.battery)
        asked = len(first.calls)
        second = self.fake(Percentage=40.0, State=1, TimeToFull=3900)
        self.assertTrue(pump(lambda: client.state == "ready"))
        self.assertEqual((client.battery, client.owner), (Battery(40, CHARGING, 3900), second.unique_name))
        self.assertEqual(len(first.calls), asked)

    def test_a_late_reply_of_the_previous_owner_is_ignored(self):
        first = self.fake()
        first.hold = lambda call: True
        client = self.client()
        client.start()
        self.assertTrue(pump(lambda: first.held))
        first.release()
        self.assertTrue(pump(lambda: client.state == "absent"))
        self.fake(Percentage=40.0)
        self.assertTrue(pump(lambda: client.state == "ready"))
        first.answer_held()
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(client.battery.percent, 40)

    def test_nothing_is_subscribed_or_asked_while_stopped(self):
        fake = self.fake()
        client = self.client()
        self.ready(client)
        client.stop()
        reads, changes = fake.count("GetAll"), len(self.changes)
        fake.set(Percentage=10.0)                        # UPower announces a change nobody listens to
        self.assertFalse(pump(timeout=0.08))
        self.assertEqual((fake.count("GetAll"), len(self.changes)), (reads, changes))
        self.assertEqual((client.owner, client.state, client.idle), (None, "unknown", True))

    def test_starting_and_stopping_again_reads_once_per_start_and_listens_only_while_started(self):
        fake = self.fake()
        client = self.client()
        for cycle in range(1, 5):
            self.ready(client)
            self.assertEqual(fake.count("GetAll"), 2 * cycle - 1)
            fake.set(Percentage=70.0 - cycle)            # heard while started: one more read, then quiet
            self.assertTrue(pump(lambda: client.battery.percent == 70 - cycle))
            client.stop()
            fake.set(Percentage=30.0)                    # not heard while stopped
            self.assertFalse(pump(timeout=0.04))
        self.assertEqual(fake.count("GetAll"), 8)        # two per cycle: the start, and the one change it heard

    def test_stopping_mid_read_leaves_nothing_behind(self):
        fake = self.fake()
        fake.hold = lambda call: True
        client = self.client()
        client.start()
        self.assertTrue(pump(lambda: fake.held))
        client.stop()
        changes = len(self.changes)
        fake.answer_held()
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((client.battery, client.state, len(self.changes)), (None, "unknown", changes))


class NeverStartedTest(unittest.TestCase):
    """The private bus here *can* start services: UPower's name is one, and so is a control that proves it."""

    CONTROL = "org.example.Control"

    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.started, self.control = SCRATCH / "upower-started", SCRATCH / "control-started"
        config = activatable_bus_config(SCRATCH, {BUS_NAME: self.started, self.CONTROL: self.control})
        self.bus = PrivateBus.start(config)
        self.addCleanup(self.bus.stop)

    def start_service(self, name):
        """Ask the bus to start ``name``. The command leaves its marker and never takes the name, so no reply is awaited."""
        self.bus.connect().call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "StartServiceByName", GLib.Variant("(su)", (name, 0)), None, Gio.DBusCallFlags.NONE,
                                5000, None, lambda _bus, _result, _data: None, None)

    def test_the_bus_starts_a_service_when_it_is_asked_to(self):
        self.start_service(self.CONTROL)
        if not pump(self.control.exists, 3):
            self.skipTest("this bus cannot start services here")
        self.start_service(BUS_NAME)
        self.assertTrue(pump(self.started.exists, 3), "the test bus cannot start UPower, so the next test proves nothing")

    def test_watching_reading_and_refreshing_never_start_it(self):
        self.start_service(self.CONTROL)
        if not pump(self.control.exists, 3):
            self.skipTest("this bus cannot start services here")
        client = UPowerClient(lambda: None, connection=self.bus.connect())
        self.addCleanup(client.stop)
        client.start()
        self.assertTrue(pump(lambda: client.state == "absent"))
        for _ in range(3):
            client.refresh()
        client.stop()
        client.start()
        self.assertTrue(pump(lambda: client.state == "absent"))
        self.assertFalse(pump(self.started.exists, 0.4))
        self.assertFalse(self.started.exists())


if __name__ == "__main__":
    unittest.main()
