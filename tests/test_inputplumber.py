"""The read-only InputPlumber client.

Three layers: pure parsing, the client against a stub connection (always
runs, fully deterministic), and the client against the fake service on a
private bus (skipped when ``dbus-daemon`` is not installed). Every bus test
ends by proving that the fake recorded nothing but reads.
"""

import ast
import json
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace

from gi.repository import Gio, GLib

from fakes import fake_input
from fakes.fake_inputplumber import (BUS_NAME, COMPOSITE_IFACE, FIXTURES, MANAGER, MANAGER_IFACE, PROPERTIES_IFACE,
                                     READS, ROOT, TARGET_IFACE, FakeInputPlumber, PrivateBus)
from fakes.loop import pump
from gaminghome import core, inputplumber
from gaminghome.input import ControllerMonitor, VirtualGamepads
from gaminghome.inputplumber import GAMEPAD_TARGETS, InputPlumberClient, composite_nodes

SCRATCH = Path(__file__).resolve().parent / "scratch" / "inputplumber"
ELITE = GAMEPAD_TARGETS["xbox-elite"]
RC73YA_NODES = frozenset({"/dev/input/event3", "/dev/input/event4", "/dev/input/event5", "/dev/input/event6"})
RC73YA_STATUS = {"name": "ASUS ROG Xbox Ally", "profile": "Default", "targets": ["mouse", "xbox-elite", "keyboard"],
                 "sources": 5, "hidraw_sources": False, "dbus_targets": 1, "right_stick_button": False}

# The shape InputPlumber really answers with: the whole tree, nested.
NESTED = """
<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.DBus.ObjectManager"/>
  <node name="Manager"><interface name="org.shadowblip.InputManager"/></node>
  <node name="devices">
    <node name="source"><node name="CompositeDevice7"/><node name="event6"/></node>
    <node name="target"><node name="gamepad0"/></node>
  </node>
  <node name="CompositeDevice10"><interface name="org.shadowblip.Input.CompositeDevice"/></node>
  <node name="CompositeDevice2"/>
  <node name="CompositeDevice0"><node name="CompositeDevice5"/></node>
  <node name="CompositeDeviceX"/>
  <node name="xCompositeDevice1"/>
</node>"""


class ParsingTest(unittest.TestCase):
    def test_only_direct_children_named_like_composites_are_listed_in_numeric_order(self):
        self.assertEqual(composite_nodes(NESTED), ["CompositeDevice0", "CompositeDevice2", "CompositeDevice10"])

    def test_garbage_is_refused_and_the_list_is_bounded(self):
        for document in ("<node", "", None, b"<node/>", "x" * (inputplumber.MAX_XML_CHARS + 1)):
            with self.assertRaises(ValueError):
                composite_nodes(document)
        many = "<node>" + "".join(f'<node name="CompositeDevice{i}"/>' for i in range(40)) + "</node>"
        self.assertEqual(len(composite_nodes(many)), inputplumber.MAX_COMPOSITES)


class ReadOnlyContractTest(unittest.TestCase):
    def test_client_source_names_nothing_that_changes_inputplumber(self):
        source = Path(inputplumber.__file__).read_text(encoding="utf-8")
        members = set()
        for path in FIXTURES.glob("*.xml"):
            for interface in Gio.DBusNodeInfo.new_for_xml(path.read_text(encoding="utf-8")).interfaces:
                if interface.name.startswith("org.shadowblip."):
                    members |= {method.name for method in interface.methods}
        self.assertGreaterEqual(members, {
            "LoadProfilePath", "LoadProfileFromYaml", "GetProfileYaml", "SetTargetDevices", "SendEvent",
            "SendButtonChord", "SetInterceptActivation", "Stop", "HookSleep", "HookWake", "CreateCompositeDevice",
            "CreateTargetDevice", "StopTargetDevice", "AttachTargetDevice", "SendKey", "MoveCursor"})
        for member in sorted(members):
            self.assertNotIn(member, source)
        strings = {node.value for node in ast.walk(ast.parse(source))
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertFalse(strings & {"Set", "InterceptMode", "FilteredEvents", "GamepadOrder", "ManageAllDevices"})
        self.assertNotIn("call_sync", source)


def composite_properties(name="ASUS ROG Xbox Ally", targets=("gamepad0",), sources=("/dev/input/event6",)):
    return {"Name": GLib.Variant("s", name), "ProfileName": GLib.Variant("s", "Default"),
            "PersistentId": GLib.Variant("s", "Vendor_Pad_SERIAL123456"),
            "TargetDevices": GLib.Variant("as", [f"{ROOT}/devices/target/{target}" for target in targets]),
            "DbusDevices": GLib.Variant("as", [f"{ROOT}/devices/target/dbus0"]),
            "SourceDevicePaths": GLib.Variant("as", list(sources)), "Capabilities": GLib.Variant("as", [])}


class StubBus:
    """Stands in for a Gio.DBusConnection: records requests, and the test delivers the replies."""

    def __init__(self):
        self.calls, self.subscriptions, self.unsubscribed = [], {}, []

    def call(self, bus_name, path, interface, method, _parameters, _reply_type, flags, timeout, cancellable, callback,
             data):
        self.calls.append(SimpleNamespace(bus_name=bus_name, path=path, interface=interface, method=method,
                                          flags=flags, timeout=timeout, cancellable=cancellable, callback=callback,
                                          data=data, answered=False))

    def call_finish(self, result):
        if isinstance(result, Exception):
            raise result
        return result

    def signal_subscribe(self, sender, interface, member, path, arg0, _flags, callback, *data):
        identifier = len(self.subscriptions) + len(self.unsubscribed) + 1
        self.subscriptions[identifier] = SimpleNamespace(sender=sender, interface=interface, member=member,
                                                         path=path, arg0=arg0, callback=callback, data=data)
        return identifier

    def signal_unsubscribe(self, identifier):
        self.unsubscribed.append(self.subscriptions.pop(identifier))

    def pending(self):
        return [call for call in self.calls if not call.answered]

    def answer(self, call, result):
        call.answered = True
        call.callback(self, result, call.data)

    def answer_all(self, composites=None, kinds=None, version="0.82.0", only=None):
        """Answer the given calls (default: everything pending, and whatever that triggers)."""
        composites = {f"{ROOT}/CompositeDevice0": composite_properties()} if composites is None else composites
        kinds = {f"{ROOT}/devices/target/gamepad0": "xbox-elite"} if kinds is None else kinds
        document = "<node>" + "".join(f'<node name="{path.rsplit("/", 1)[1]}"/>' for path in composites) + "</node>"
        queue = list(only) if only is not None else None
        while True:
            batch = queue if queue is not None else self.pending()
            if not batch:
                return
            for call in batch:
                if call.method == "Introspect":
                    result = GLib.Variant("(s)", (document,))
                elif call.method == "Get":
                    result = GLib.Variant("(v)", (GLib.Variant("s", version),))
                elif call.path in composites:
                    result = GLib.Variant("(a{sv})", (composites[call.path],))
                elif call.path in kinds:
                    result = GLib.Variant("(a{sv})", ({"DeviceType": GLib.Variant("s", kinds[call.path])},))
                else:
                    result = Gio.DBusError.new_for_dbus_error("org.freedesktop.DBus.Error.UnknownObject", call.path)
                self.answer(call, result)
            if queue is not None:
                return


class StubClient(InputPlumberClient):
    def _watch_name(self):
        return "watch"

    @staticmethod
    def _unwatch_name(_watch):
        pass


class ClientLogicTest(unittest.TestCase):
    def setUp(self):
        self.bus, self.timers, self.delays, self.changes = StubBus(), {}, [], []
        self.client = StubClient(lambda: self.changes.append(1), schedule=self.schedule, cancel=self.timers.pop,
                                 settle_ms=5, confirm_ms=7, retry_ms=(11, 13))
        self.client.start()

    def schedule(self, milliseconds, callback):
        token = len(self.delays) + 1
        self.delays.append(milliseconds)
        self.timers[token] = callback
        return token

    def fire(self):
        (token, callback), = self.timers.items()
        del self.timers[token]
        callback()

    def appear(self, owner=":1.42"):
        self.client._appeared(self.bus, BUS_NAME, owner)

    def settle(self):
        """Answer discoveries until the client only waits for hints."""
        for _ in range(8):
            self.bus.answer_all()
            if self.client.idle:
                return
            self.fire()
        self.fail("client did not settle")

    def test_calls_and_subscriptions_are_pinned_to_the_current_unique_owner(self):
        self.appear()
        self.assertEqual([(call.path, call.interface, call.method) for call in self.bus.calls],
                         [(ROOT, "org.freedesktop.DBus.Introspectable", "Introspect"),
                          (MANAGER, PROPERTIES_IFACE, "Get")])
        self.settle()
        calls = self.bus.calls
        self.assertEqual({call.bus_name for call in calls}, {":1.42"})
        self.assertTrue(all(call.flags & Gio.DBusCallFlags.NO_AUTO_START and call.cancellable for call in calls))
        self.assertEqual({call.method for call in calls}, {"Introspect", "Get", "GetAll"})
        self.assertEqual({(s.sender, s.member, s.arg0) for s in self.bus.subscriptions.values()},
                         {(":1.42", "PropertiesChanged", COMPOSITE_IFACE),
                          (":1.42", "PropertiesChanged", MANAGER_IFACE)})
        self.assertEqual(self.client.virtual_gamepads(),
                         VirtualGamepads({ELITE: 1}, frozenset({"/dev/input/event6"})))

    def test_replies_from_a_previous_owner_or_pass_change_nothing(self):
        self.appear()
        old = self.bus.pending()
        cancellable = old[0].cancellable
        self.client._vanished(self.bus, BUS_NAME)
        self.assertTrue(cancellable.is_cancelled())
        self.assertEqual(len(self.bus.unsubscribed), 2)
        self.appear(":1.77")
        self.bus.answer_all(only=old)                      # late replies addressed to the old owner
        self.assertEqual(self.client.composites, ())
        self.assertFalse(self.client.settled and self.client.idle)
        self.bus.answer_all(composites={f"{ROOT}/CompositeDevice3": composite_properties(name="Second pad")})
        self.assertEqual([composite.name for composite in self.client.composites], ["Second pad"])
        self.assertEqual({call.bus_name for call in self.bus.calls if call not in old}, {":1.77"})

    def test_hints_are_coalesced_and_back_off_while_nothing_changes(self):
        self.appear()
        self.settle()
        del self.delays[:]
        for _ in range(50):
            self.client.hint()                             # a burst arms one timer
        self.assertEqual(self.delays, [5])
        for _ in range(6):
            self.fire()
            for _ in range(50):
                self.client.hint()                         # hints during a pass never start a second one
            started = self.client.discoveries
            self.bus.answer_all()
            self.assertEqual(self.client.discoveries, started)
        self.assertEqual(self.delays, [5, 10, 20, 40, 80, 80, 80])
        self.fire()
        self.bus.answer_all()                              # the hints stopped and nothing changed:
        self.assertEqual(self.delays[-1], 7)               # one more look, because a hint asked for it
        self.fire()
        self.bus.answer_all()                              # then quiet: no timer, nothing in flight
        self.assertTrue(self.client.idle)
        self.assertEqual(self.timers, {})
        self.client.hint()
        self.assertEqual(self.delays[-1], 5)

    def test_a_change_is_confirmed_a_bounded_number_of_times(self):
        self.appear()
        for index in range(12):                            # a daemon whose answer differs on every read
            self.bus.answer_all(composites={f"{ROOT}/CompositeDevice0": composite_properties(name=f"pad {index}")})
            if self.client.idle:
                break
            self.fire()
        self.assertEqual(self.client.discoveries, 1 + inputplumber.MAX_CONFIRMS)
        self.assertEqual(self.delays, [7] * inputplumber.MAX_CONFIRMS)

    def test_failed_discovery_keeps_the_last_picture_and_retries_are_bounded(self):
        self.appear()
        self.settle()
        picture, started = self.client.composites, self.client.discoveries
        del self.delays[:]
        self.client.hint()
        for _ in range(8):
            if self.client.idle:
                break
            self.fire()
            for call in self.bus.pending():
                self.bus.answer(call, GLib.Error("timed out", "g-io-error-quark", 24))
        self.assertEqual(self.client.discoveries - started, 3)
        self.assertEqual(self.delays, [5, 11, 13])
        self.assertEqual(self.client.composites, picture)
        self.assertIn("timed out", self.client.error)
        self.client.hint()
        self.settle()
        self.assertIsNone(self.client.error)

    def test_object_that_vanished_mid_discovery_is_not_a_failure(self):
        self.appear()
        self.bus.answer_all(kinds={})                      # the target is gone when it is asked for
        self.assertIsNone(self.client.error)
        self.assertEqual([composite.targets for composite in self.client.composites], [()])
        self.assertEqual(self.client.virtual_gamepads().expected, {})

    def test_stop_makes_pending_replies_and_timers_harmless(self):
        self.appear()
        self.settle()
        self.client.hint()
        self.fire()
        pending, changes = self.bus.pending(), len(self.changes)
        self.client.stop()
        self.client.stop()
        self.bus.answer_all(only=pending, composites={f"{ROOT}/CompositeDevice9": composite_properties(name="late")})
        self.client._appeared(self.bus, BUS_NAME, ":1.99")  # a name-watch callback that was already queued
        self.client.hint()
        self.assertEqual((self.client.composites, self.client.owner, self.timers, len(self.changes)),
                         ((), None, {}, changes))
        self.assertEqual(self.bus.subscriptions, {})

    def test_exceptions_in_callbacks_never_escape_and_do_not_wedge_the_client(self):
        self.appear()
        self.bus.answer(self.bus.pending()[0], SimpleNamespace(unpack=lambda: 1 / 0))  # a reply nobody expected
        self.assertIn("ZeroDivisionError", self.client.error)
        self.assertTrue(self.client.settled and self.client.idle)
        self.client.on_change = lambda: 1 / 0                                          # a failing consumer
        self.client.hint()
        self.settle()
        self.assertEqual(len(self.client.composites), 1)
        self.assertIsNone(self.client.error)
        self.assertIn("change handler failed", self.client.status()["error"])

    def test_the_call_helper_refuses_everything_but_reads(self):
        self.appear()
        self.settle()
        sent = len(self.bus.calls)
        for interface, method in ((COMPOSITE_IFACE, "Stop"), (PROPERTIES_IFACE, "Set"), (MANAGER_IFACE, "HookSleep"),
                                  (COMPOSITE_IFACE, "GetAll"), (PROPERTIES_IFACE, "Introspect")):
            with self.assertRaises(ValueError, msg=method):
                self.client._call(MANAGER, interface, method, None, "()", lambda *_arguments: None)
        self.assertEqual(len(self.bus.calls), sent)
        self.assertTrue(self.client.idle)

    def test_unowned_name_settles_without_a_single_call(self):
        self.client._vanished(None, BUS_NAME)
        self.client.hint()
        self.assertEqual((self.client.settled, self.client.available, self.bus.calls, self.timers),
                         (True, False, [], {}))
        self.assertEqual(self.client.virtual_gamepads(), VirtualGamepads())
        self.assertEqual(self.client.status(), {"available": False, "version": None, "settled": True,
                                                "composites": [], "notes": [], "error": None})


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
                self.assertEqual(fake.writes(), [], "InputPlumber must only ever be read")
                self.assertLessEqual(set(fake.members()), READS)
        finally:
            self.bus.close_connections()

    def fake(self, own=True, composite=True, **options) -> FakeInputPlumber:
        fake = FakeInputPlumber(self.bus)
        self.fakes.append(fake)
        if composite:
            fake.add_composite(**options)
        if own:
            self.assertTrue(fake.own())
        return fake

    def client(self, **options) -> InputPlumberClient:
        client = InputPlumberClient(lambda: self.changes.append(1), connection=self.bus.connect(),
                                    **{"settle_ms": 5, "confirm_ms": 10, "retry_ms": (5, 5), **options})
        self.clients.append(client)
        client.start()
        return client

    def settle(self, client):
        self.assertTrue(pump(lambda: client.settled and client.idle), "the client did not settle")

    def count_signals(self, client):
        """How many PropertiesChanged signals reached the client's connection, from anyone."""
        seen = []
        client._connection.signal_subscribe(None, PROPERTIES_IFACE, "PropertiesChanged", None, None,
                                            Gio.DBusSignalFlags.NONE, lambda *_args: seen.append(1))
        return seen

    def test_reads_what_inputplumber_manages_and_nothing_personal(self):
        fake = self.fake(persistent_id="Vendor_Pad_SERIAL123456")
        client = self.client()
        self.settle(client)
        self.assertEqual(client.status(), {"available": True, "version": "0.82.0", "settled": True,
                                           "composites": [RC73YA_STATUS], "notes": ["evdev-only", "no-r3"],
                                           "error": None})
        self.assertEqual(client.virtual_gamepads(), VirtualGamepads({ELITE: 1}, RC73YA_NODES))
        self.assertNotIn("SERIAL123456", json.dumps(client.status()) + repr(client.composites))
        self.assertEqual(set(fake.members()), READS)
        self.assertEqual(client.owner, fake.unique_name)

    def test_idle_client_makes_no_calls(self):
        fake = self.fake()
        client = self.client()
        self.settle(client)
        calls = len(fake.calls)
        self.assertFalse(pump(timeout=0.08))
        self.assertEqual(len(fake.calls), calls)
        self.assertTrue(client.idle)

    def test_absent_service_is_never_called_or_started(self):
        bystander = self.fake(own=False)
        client = self.client()
        self.settle(client)
        self.assertFalse(client.available)
        self.assertEqual(client.virtual_gamepads(), VirtualGamepads())
        self.assertEqual(bystander.calls, [])

    def test_owner_change_drops_the_old_picture_and_reads_only_the_new_owner(self):
        first = self.fake()
        client = self.client()
        self.settle(client)
        first.release()
        self.assertTrue(pump(lambda: not client.available))
        self.assertEqual((client.composites, client.virtual_gamepads()), ((), VirtualGamepads()))
        asked = len(first.calls)
        second = self.fake(name="Other pad", targets=("xb360",), sources=("/dev/input/event9", "/dev/hidraw3"),
                           capabilities=("Gamepad:Button:RightStick",))
        self.assertTrue(pump(lambda: client.owner == second.unique_name))
        self.settle(client)
        self.assertEqual([composite.name for composite in client.composites], ["Other pad"])
        self.assertEqual(client.virtual_gamepads(),
                         VirtualGamepads({GAMEPAD_TARGETS["xb360"]: 1}, frozenset({"/dev/input/event9"})))
        self.assertEqual(client.notes(), [])
        self.assertEqual(len(first.calls), asked)

    def test_late_replies_from_the_previous_owner_are_ignored(self):
        first = self.fake()
        first.hold = lambda call: call[3] == "GetAll"
        client = self.client()
        self.assertTrue(pump(lambda: first.held))
        first.release()
        self.assertTrue(pump(lambda: not client.available))
        second = self.fake(name="Second pad")
        self.settle(client)
        first.answer_held()
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual([composite.name for composite in client.composites], ["Second pad"])
        self.assertEqual(client.owner, second.unique_name)
        self.assertIsNone(client.error)

    def test_stopping_mid_discovery_leaves_nothing_behind(self):
        fake = self.fake()
        fake.hold = lambda call: call[3] == "GetAll"
        client = self.client()
        self.assertTrue(pump(lambda: fake.held))
        client.stop()
        changes = len(self.changes)
        fake.answer_held()
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((client.composites, client.available, client.idle, len(self.changes)),
                         ((), False, True, changes))

    def test_renumbered_composite_and_reused_path_are_followed(self):
        fake = self.fake(composite=False)
        zero = fake.add_composite(index=0, name="First")
        client = self.client()
        self.settle(client)
        self.assertEqual([composite.path for composite in client.composites], [zero])
        fake.remove_composite(zero)
        one = fake.add_composite(index=1, name="Renumbered", targets=("xbox-series",))
        self.assertTrue(pump(lambda: [composite.path for composite in client.composites] == [one]))
        self.settle(client)
        self.assertEqual(client.virtual_gamepads().expected, {GAMEPAD_TARGETS["xbox-series"]: 1})
        fake.remove_composite(one)
        fake.add_composite(index=0, name="Same path, another device", targets=("keyboard",))
        self.assertTrue(pump(lambda: [c.name for c in client.composites] == ["Same path, another device"]))
        self.settle(client)
        self.assertEqual(client.virtual_gamepads().expected, {})

    def test_target_removed_and_added_again_needs_only_a_hint(self):
        fake = self.fake()
        path = f"{ROOT}/CompositeDevice0"
        client = self.client()
        self.settle(client)
        pad = next(target for target in fake.targets(path) if "gamepad" in target)
        fake.detach_target(path, pad)                      # suspend: the daemon says nothing about it
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(client.virtual_gamepads().expected, {ELITE: 1})
        client.hint()                                      # ...but the virtual pad's event node went away
        self.assertTrue(pump(lambda: not client.virtual_gamepads().expected))
        self.settle(client)
        fake.attach_target(path, "xbox-elite")             # resume
        client.hint()
        self.assertTrue(pump(lambda: client.virtual_gamepads().expected == {ELITE: 1}))
        self.settle(client)
        self.assertEqual(client.virtual_gamepads().owned_nodes, RC73YA_NODES)

    def test_target_listed_only_after_its_node_appeared_is_still_found(self):
        # Resume: the virtual pad's node shows up (a hint) before InputPlumber lists the target.
        fake = self.fake(targets=("mouse", "keyboard"))
        client = self.client(confirm_ms=40)
        self.settle(client)
        started = client.discoveries
        client.hint()
        self.assertTrue(pump(lambda: client.discoveries == started + 1 and client._discovery is None))
        fake.attach_target(f"{ROOT}/CompositeDevice0", "xbox-elite")   # no signal, and no further hint
        self.settle(client)
        self.assertEqual(client.virtual_gamepads().expected, {ELITE: 1})

    def test_target_attached_right_after_a_change_is_found_without_any_hint(self):
        fake = self.fake(composite=False)
        client = self.client(confirm_ms=40)
        self.settle(client)
        path = fake.add_composite(targets=("mouse", "keyboard"))
        self.assertTrue(pump(lambda: len(client.composites) == 1))
        fake.attach_target(path, "xbox-elite")             # no signal: the confirmation pass must find it
        self.settle(client)
        self.assertEqual(client.virtual_gamepads().expected, {ELITE: 1})

    def test_flood_of_change_signals_is_bounded(self):
        fake = self.fake()
        path = f"{ROOT}/CompositeDevice0"
        client = self.client()
        self.settle(client)
        seen, started, reads = self.count_signals(client), client.discoveries, fake.count("GetAll", path)
        for _ in range(300):
            fake.emit_changed(path, COMPOSITE_IFACE, {"ProfileName": GLib.Variant("s", "Default")})
        self.assertTrue(pump(lambda: len(seen) == 300))
        self.settle(client)
        self.assertLessEqual(client.discoveries - started, 6)
        self.assertGreaterEqual(client.discoveries - started, 1)
        self.assertEqual(fake.count("GetAll", path) - reads, client.discoveries - started)

    def test_signals_from_anyone_but_the_owner_are_not_hints(self):
        self.fake()
        impostor = self.fake(own=False)
        client = self.client()
        self.settle(client)
        seen, started = self.count_signals(client), client.discoveries
        impostor.emit_changed(f"{ROOT}/CompositeDevice0", COMPOSITE_IFACE,
                              {"ProfileName": GLib.Variant("s", "Evil")})
        impostor.emit_changed(MANAGER, MANAGER_IFACE, {"GamepadOrder": GLib.Variant("as", [])})
        self.assertTrue(pump(lambda: len(seen) == 2))
        self.assertFalse(pump(timeout=0.03))
        self.assertEqual((client.discoveries, client.idle), (started, True))
        self.assertEqual(impostor.calls, [])

    def test_unreadable_listing_keeps_the_last_picture_with_bounded_retries(self):
        fake = self.fake()
        client = self.client()
        self.settle(client)
        picture, asked = client.composites, fake.count("Introspect")
        fake.root_document = "<node"
        client.hint()
        self.assertTrue(pump(lambda: client.error))
        self.settle(client)
        self.assertEqual(fake.count("Introspect") - asked, 3)
        self.assertEqual(client.composites, picture)
        fake.root_document = None
        client.hint()
        self.assertTrue(pump(lambda: client.error is None))
        self.settle(client)

    def test_hostile_values_are_reduced_to_safe_ones(self):
        fake = self.fake(targets=("xbox-elite", "ds5"))
        path = f"{ROOT}/CompositeDevice0"
        pad, second = fake.targets(path)
        properties = fake.composite(path)
        properties["Name"] = GLib.Variant("s", "Evil\x1b[31m\nPad " + "x" * 300)
        properties["ProfileName"] = GLib.Variant("u", 7)
        properties["TargetDevices"] = GLib.Variant(
            "as", ["/evil", f"{ROOT}/devices/target/../../Manager", pad, second])
        properties["DbusDevices"] = GLib.Variant("s", "not a list")
        properties["SourceDevicePaths"] = GLib.Variant(
            "as", ["/dev/input/event6", "/dev/input/../sda", "/etc/passwd", "/dev/hidraw2"])
        fake.target(second)["DeviceType"] = GLib.Variant("s", "Xbox Elite; rm -rf")
        client = self.client()
        self.settle(client)
        composite, = client.composites
        self.assertEqual(composite.name, ("Evil[31mPad " + "x" * 300)[:inputplumber.MAX_TEXT])
        self.assertEqual((composite.profile, composite.targets, composite.dbus_targets), ("", ("xbox-elite",), 0))
        self.assertEqual((composite.event_nodes, composite.sources, composite.hidraw_sources),
                         (frozenset({"/dev/input/event6"}), 4, True))
        asked = {call[1] for call in fake.calls if call[3] == "GetAll" and call[4] == (TARGET_IFACE,)}
        self.assertEqual(asked, {pad, second})

    def test_unrecognized_gamepad_target_is_reported_not_guessed(self):
        self.fake(targets=("deck-uhid", "mouse"))
        client = self.client()
        self.settle(client)
        self.assertEqual(client.virtual_gamepads().expected, {})
        self.assertEqual(client.unrecognized_targets(), ["deck-uhid"])
        self.assertIn("unrecognized-target", client.notes())

    def test_two_composites_announce_two_gamepads(self):
        fake = self.fake()
        fake.add_composite(name="Second", sources=("/dev/input/event30",))
        client = self.client()
        self.settle(client)
        self.assertEqual(client.virtual_gamepads(),
                         VirtualGamepads({ELITE: 2}, RC73YA_NODES | {"/dev/input/event30"}))

    def test_virtual_pad_is_read_only_while_inputplumber_owns_its_name_and_announces_it(self):
        # The three conditions together, wired the way the runtime wires them.
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        tree = fake_input.FakeInputTree(SCRATCH)
        pad = tree.add(21, fake_input.ELITE)
        source = tree.add(6, fake_input.PHYSICAL, where=fake_input.USB)

        def allowlist():  # the client speaks of /dev/input, the mocked tree lives elsewhere
            allow = client.virtual_gamepads()
            owned = frozenset(str(tree.dev / os.path.basename(node)) for node in allow.owned_nodes)
            return VirtualGamepads(allow.expected, owned)

        interpreter = core.ControllerInterpreter(lambda _ms, _callback: 0, lambda _token: None, lambda _action: None)
        client = InputPlumberClient(lambda: monitor.rescan(), connection=self.bus.connect(), settle_ms=5, confirm_ms=10)
        monitor = ControllerMonitor(interpreter, lambda: None, dev_dir=str(tree.dev), sysfs_root=str(tree.sys),
                                    extra_gamepads=allowlist, on_candidate=client.hint, identify=tree.identify)
        self.clients.append(client)
        self.addCleanup(monitor.stop)

        impostor = self.fake(own=False, sources=("/dev/input/event6",))     # same objects, but not the name
        client.start()
        monitor.start()
        self.settle(client)
        self.assertEqual(list(monitor.devices), [source])                   # a look-alike alone is never used

        owner = self.fake(sources=("/dev/input/event6",))
        self.assertTrue(pump(lambda: list(monitor.devices) == [pad]))       # used, and its source is a duplicate
        self.settle(client)
        gamepad = next(target for target in owner.targets(f"{ROOT}/CompositeDevice0") if "gamepad" in target)
        owner.detach_target(f"{ROOT}/CompositeDevice0", gamepad)            # no longer announced
        client.hint()
        self.assertTrue(pump(lambda: list(monitor.devices) == [source]))
        owner.attach_target(f"{ROOT}/CompositeDevice0", "xbox-elite")
        client.hint()
        self.assertTrue(pump(lambda: list(monitor.devices) == [pad]))
        self.settle(client)
        owner.release()                                                     # the name is gone
        self.assertTrue(pump(lambda: list(monitor.devices) == [source]))
        self.assertEqual(impostor.calls, [])

    def test_the_fake_does_record_writes(self):
        # Without this, "no writes were recorded" would prove nothing.
        fake = self.fake()
        self.fakes.remove(fake)
        caller, done = self.bus.connect(), []
        path = f"{ROOT}/CompositeDevice0"
        for target, interface, member, arguments in (
                (path, COMPOSITE_IFACE, "LoadProfilePath", GLib.Variant("(s)", ("/nonexistent/profile.yaml",))),
                (path, COMPOSITE_IFACE, "GetProfileYaml", None),
                (path, PROPERTIES_IFACE, "Set",
                 GLib.Variant("(ssv)", (COMPOSITE_IFACE, "InterceptMode", GLib.Variant("u", 2)))),
                (MANAGER, MANAGER_IFACE, "HookSleep", None)):
            caller.call(BUS_NAME, target, interface, member, arguments, None, Gio.DBusCallFlags.NONE, 2000, None,
                        lambda _bus, _result, _data: done.append(1), None)
        self.assertTrue(pump(lambda: len(done) == 4))
        self.assertEqual([call[3] for call in fake.writes()],
                         ["LoadProfilePath", "GetProfileYaml", "Set", "HookSleep"])


if __name__ == "__main__":
    unittest.main()
