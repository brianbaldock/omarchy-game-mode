"""The BlueZ client: its pure parts without D-Bus, and its behaviour against a fake bluetoothd on a private bus.

Every bus test ends by checking what the client sent: only calls on the allow-list, each addressed to the unique name
of the fake and flagged not to start a service, and no timer left once it was stopped. The tests never use the
system bus: the address is always the private bus's, and asking for the system bus fails the test.
"""

import ast
import contextlib
import json
import shutil
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import Gio, GLib

from fakes.fake_bluez import (ADAPTER, BATTERY, BOND, CANCELED, DEVICE, FAIL, PROPERTIES, REJECTED, WAIT, Changes,
                              Clock, FakeBlueZ, StandInAgent, address_path, blob, call_agent)
from fakes.fake_inputplumber import PrivateBus
from fakes.fake_upower import activatable_bus_config
from fakes.loop import pump
from gaminghome import bluetooth as bt
from gaminghome import bluez

SCRATCH = Path(__file__).resolve().parent / "scratch" / "bluez"
PAD, BUDS, KEYS = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02", "AA:BB:CC:DD:EE:03"
HEADSET_UUID = "0000111e-0000-1000-8000-00805f9b34fb"


# ---- pure parts ----------------------------------------------------------------------------------------------------
class ErrorTableTest(unittest.TestCase):
    def test_each_error_bluez_documents_has_one_meaning(self):
        table = {"org.bluez.Error.AuthenticationFailed": bt.AUTH_FAILED,
                 "org.bluez.Error.AuthenticationRejected": bt.AUTH_FAILED,
                 "org.bluez.Error.AuthenticationCanceled": bt.AUTH_FAILED,
                 "org.bluez.Error.AuthenticationTimeout": bt.TIMEOUT,
                 "org.bluez.Error.ConnectionAttemptFailed": bt.UNREACHABLE,
                 "org.bluez.Error.InProgress": bt.IN_PROGRESS,
                 "org.bluez.Error.NotReady": bt.NOT_READY,
                 "org.bluez.Error.NotSupported": bt.UNSUPPORTED,
                 "org.bluez.Error.BREDR.ProfileUnavailable": bt.UNSUPPORTED,
                 "org.bluez.Error.DoesNotExist": bt.GONE,
                 "org.freedesktop.DBus.Error.UnknownObject": bt.GONE,
                 "org.bluez.Error.InvalidArguments": bt.GONE,
                 "org.freedesktop.DBus.Error.AccessDenied": bt.UNAVAILABLE,
                 "org.bluez.Error.NotAuthorized": bt.UNAVAILABLE,
                 "org.bluez.Error.PermissionDenied": bt.UNAVAILABLE,
                 "org.freedesktop.DBus.Error.ServiceUnknown": bt.UNAVAILABLE,
                 "org.freedesktop.DBus.Error.NoReply": bt.TIMEOUT,
                 "org.bluez.Error.Busy": bt.IN_PROGRESS,
                 "org.bluez.Error.Blocked": bt.BLOCKED,
                 "org.bluez.Error.Failed": bt.FAILED,
                 "org.bluez.Error.SomethingNew": bt.FAILED,
                 "": bt.FAILED}
        self.assertEqual({name: bluez.failure_code(name) for name in table}, table)

    def test_canceled_means_the_user_gave_up_only_when_the_user_did(self):
        name = "org.bluez.Error.AuthenticationCanceled"
        self.assertEqual(bluez.failure_code(name), bt.AUTH_FAILED)
        self.assertEqual(bluez.failure_code(name, cancelled=True), bt.CANCELED)
        self.assertEqual(bluez.failure_code("org.bluez.Error.AuthenticationFailed", cancelled=True), bt.CANCELED)

    def test_the_reason_of_failed_is_matched_loosely(self):
        reasons = {"br-connection-page-timeout": bt.UNREACHABLE, "br-connection-timeout": bt.UNREACHABLE,
                   "le-connection-timeout": bt.UNREACHABLE, "le-connection-abort-by-local": bt.UNREACHABLE,
                   "Host is down": bt.UNREACHABLE, "br-connection-key-missing": bt.AUTH_FAILED,
                   "le-connection-key-missing": bt.AUTH_FAILED, "Blocked through rfkill": bt.BLOCKED,
                   "adapter BLOCKED": bt.BLOCKED, "br-connection-aborted-by-local": bt.FAILED,
                   "le-connection-abort-by-remote": bt.FAILED, "br-connection-refused": bt.FAILED,
                   "br-connection-canceled": bt.FAILED, "": bt.FAILED}
        self.assertEqual({reason: bluez.failure_code("org.bluez.Error.Failed", reason) for reason in reasons}, reasons)
        # The user's cancel still decides, whatever the reason says.
        self.assertEqual(bluez.failure_code("org.bluez.Error.Failed", "le-connection-abort-by-local", cancelled=True),
                         bt.CANCELED)

    def test_no_such_object_means_gone_only_for_a_device_or_an_adapter(self):
        for name in ("org.bluez.Error.DoesNotExist", "org.bluez.Error.InvalidArguments",
                     "org.freedesktop.DBus.Error.UnknownObject"):
            self.assertEqual(bluez.failure_code(name, about_object=False), bt.FAILED)

    def test_errors_that_did_not_come_from_bluez(self):
        timeout = GLib.Error("Timeout was reached", "g-io-error-quark", int(Gio.IOErrorEnum.TIMED_OUT))
        closed = GLib.Error("The connection is closed", "g-io-error-quark", int(Gio.IOErrorEnum.CLOSED))
        self.assertEqual((bluez.code_of(timeout), bluez.code_of(closed)), (bt.TIMEOUT, bt.UNAVAILABLE))
        self.assertTrue(bluez.timed_out(timeout))

    def test_every_code_is_one_the_window_knows(self):
        codes = set(bluez.ERRORS.values()) | {code for _word, code in bluez.REASONS}
        self.assertLessEqual(codes, set(bt.FAILURES))


class KindTest(unittest.TestCase):
    def test_the_icon_decides(self):
        icons = {"audio-headset": "headset", "audio-headphones": "headphones", "audio-card": "speaker",
                 "input-gaming": "gamepad", "input-keyboard": "keyboard", "input-mouse": "mouse", "phone": "phone",
                 "computer": "computer", "input-tablet": "other", "camera-photo": "other", "printer": "other"}
        self.assertEqual({icon: bluez.kind_of(icon, 0x000104, 0x03C1) for icon in icons}, icons)

    def test_without_an_icon_the_class_and_then_the_appearance_say(self):
        classes = {0x000104: "computer", 0x00020C: "phone", 0x240404: "headset", 0x200408: "headset",
                   0x240418: "headphones", 0x240414: "speaker", 0x002540: "keyboard", 0x002580: "mouse",
                   0x002508: "gamepad", 0x002504: "gamepad", 0x0025C0: "other", 0x002594: "other",
                   0x000600: "other"}
        self.assertEqual({value: bluez.kind_of("", value) for value in classes}, classes)
        appearances = {0x0040: "phone", 0x0080: "computer", 0x03C1: "keyboard", 0x03C2: "mouse", 0x03C3: "gamepad",
                       0x03C4: "gamepad", 0x03C5: "other", 0x0941: "other"}
        self.assertEqual({value: bluez.kind_of("", None, value) for value in appearances}, appearances)
        self.assertEqual(bluez.kind_of("", 0x000600, 0x03C1), "keyboard")

    def test_values_of_the_wrong_kind_are_other(self):
        self.assertEqual(bluez.kind_of("", True, False), "other")
        self.assertEqual(bluez.kind_of(), "other")


class StateTest(unittest.TestCase):
    def test_what_the_window_is_told_about_the_adapter(self):
        cases = {(False, None, "", None): bt.UNKNOWN, (True, None, "", None): bt.ABSENT,
                 (True, ":1.5", "", None): bt.UNKNOWN, (True, ":1.5", bluez.READ_ERROR, True): bt.ERROR,
                 (True, ":1.5", bluez.READ_DONE, None): bt.NO_ADAPTER, (True, ":1.5", bluez.READ_DONE, True): bt.ON,
                 (True, ":1.5", bluez.READ_DONE, False): bt.OFF}
        self.assertEqual({case: bluez.state_of(*case) for case in cases}, cases)

    def test_object_paths_that_are_an_adapter_or_a_device(self):
        self.assertEqual([bluez.adapter_index(path) for path in ("/org/bluez/hci0", "/org/bluez/hci12", "/org/bluez",
                                                               "/org/bluez/hci0x", "/org/bluez/hci12345", "")],
                         [0, 12, None, None, None, None])
        device = address_path(PAD)
        self.assertEqual([bluez.device_adapter(path) for path in (device, address_path(PAD, 3), f"{device}/battery_1",
                                                                 "/org/bluez/hci0", "/org/bluez/hci0/player0")],
                         [0, 3, None, None, None])


class PropertyTest(unittest.TestCase):
    def test_a_property_of_the_wrong_type_is_as_good_as_missing(self):
        properties = GLib.Variant("a{sv}", {"Name": GLib.Variant("s", "Pad"), "Paired": GLib.Variant("s", "yes"),
                                            "Class": GLib.Variant("s", "0x240404"),
                                            "Alias": GLib.Variant("s", "x" * 5000)})
        self.assertEqual(bluez.prop(properties, "Name", "s"), "Pad")
        self.assertIsNone(bluez.prop(properties, "Paired", "b"))
        self.assertIsNone(bluez.prop(properties, "Class", "u"))
        self.assertIsNone(bluez.prop(properties, "Missing", "b"))
        self.assertEqual(len(bluez.prop(properties, "Alias", "s")), bluez.MAX_TEXT)
        self.assertTrue(bluez.present(properties, "Paired"))
        for wrong in (GLib.Variant("s", "not a dictionary"), GLib.Variant("a{ss}", {"Name": "Pad"}), None, {}):
            self.assertIsNone(bluez.prop(wrong, "Name", "s"))
            self.assertFalse(bluez.present(wrong, "Name"))
            self.assertIsNone(bluez.section(wrong, bluez.DEVICE))

    def test_the_power_command_gets_a_minimal_environment(self):
        environ = {"PATH": "/usr/bin", "HOME": "/home/user", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": "/run/user/1000",
                   "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket", "SECRET": "x",
                   "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "LD_PRELOAD": "/x.so"}
        self.assertEqual(bluez.power_environment(environ),
                         ["PATH=/usr/bin", "HOME=/home/user", "LANG=C.UTF-8", "XDG_RUNTIME_DIR=/run/user/1000",
                          "DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket"])
        self.assertEqual(bluez.power_environment({"PATH": "/usr/bin"}), ["PATH=/usr/bin"])


class AllowListTest(unittest.TestCase):
    def test_the_calls_it_makes(self):
        for interface, member in bluez.CALLS:
            self.assertTrue(bluez.allowed(interface, member))
        self.assertEqual(len(bluez.CALLS), 10)
        for interface, name in bluez.WRITES:
            self.assertTrue(bluez.allowed(bluez.PROPERTIES, "Set",
                                          GLib.Variant("(ssv)", (interface, name, GLib.Variant("b", True)))))

    def test_the_calls_it_never_makes(self):
        never = [(bluez.ADAPTER, "SetDiscoveryFilter"), (bluez.AGENT_MANAGER, "RequestDefaultAgent"),
                 (bluez.ADAPTER, "ConnectDevice"), (bluez.PROPERTIES, "Get"), (bluez.PROPERTIES, "GetAll"),
                 (bluez.DEVICE, "ConnectProfile"), ("org.freedesktop.DBus.Introspectable", "Introspect"),
                 (bluez.DEVICE, "RemoveDevice"), (bluez.ADAPTER, "Pair")]
        for interface, member in never:
            self.assertFalse(bluez.allowed(interface, member), member)
        writes = [(bluez.ADAPTER, "Discoverable", "b"), (bluez.ADAPTER, "Pairable", "b"), (bluez.DEVICE, "Alias", "s"),
                  (bluez.ADAPTER, "Alias", "s"), (bluez.DEVICE, "Blocked", "b"), (bluez.DEVICE, "WakeAllowed", "b"),
                  (bluez.DEVICE, "PreferredBearer", "s"), (bluez.ADAPTER, "Powered", "s"),
                  (bluez.DEVICE, "Trusted", "u"), (bluez.ADAPTER, "Trusted", "b"), (bluez.DEVICE, "Powered", "b")]
        for interface, name, signature in writes:
            value = GLib.Variant(signature, {"b": True, "s": "x", "u": 1}[signature])
            self.assertFalse(bluez.allowed(bluez.PROPERTIES, "Set", GLib.Variant("(ssv)", (interface, name, value))),
                             (interface, name, signature))
        self.assertFalse(bluez.allowed(bluez.PROPERTIES, "Set", None))
        self.assertFalse(bluez.allowed(bluez.PROPERTIES, "Set", GLib.Variant("(ss)", (bluez.DEVICE, "Trusted"))))

    def test_the_module_names_nothing_it_must_not_send_and_logs_nothing(self):
        source = Path(bluez.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        strings = {node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        forbidden = {"SetDiscoveryFilter", "RequestDefaultAgent", "ConnectDevice", "Discoverable", "Pairable",
                     "Blocked", "WakeAllowed", "PreferredBearer", "Introspect", "GetAll"}
        docstring = ast.get_docstring(tree)
        # Error names such as org.bluez.Error.Blocked are not property names.
        self.assertEqual({text for text in strings if text != docstring} - set(bluez.ERRORS) & forbidden, set())
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("logging", imported)
        self.assertNotIn("print(", source)
        self.assertNotIn("call_sync", source)
        self.assertNotIn("bus_get", source)                       # never the process-wide shared connection
        self.assertNotIn("AUTO_START", source.replace("NO_AUTO_START", ""))
        self.assertNotIn("shell=True", source)


# ---- against the fake bluetoothd -----------------------------------------------------------------------------------
class BusCase(unittest.TestCase):
    """One private bus per class; a fake bluetoothd and clients per test."""

    @classmethod
    def setUpClass(cls):
        cls.bus = PrivateBus.start()
        cls.addClassCleanup(cls.bus.stop)

    def setUp(self):
        self.clock = Clock()
        self.fakes, self.clients, self.names = [], [], set()
        guard = mock.patch.object(Gio, "dbus_address_get_for_bus_sync",
                                  side_effect=AssertionError("a test asked for the system bus"))
        guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.finish)

    def finish(self):
        try:
            for client in self.clients:
                client.stop()
                self.assertEqual(client.error, "", "a handler or callback raised")
            self.assertEqual(self.clock.pending, {}, "a timer outlived stop()")
            for fake in self.fakes:
                for (sender, _path, interface, member, arguments), (destination, flags) in zip(fake.calls, fake.sent):
                    if sender not in self.names:
                        continue
                    self.assertEqual(destination, fake.unique_name, member)      # the unique name, never org.bluez
                    self.assertTrue(flags & Gio.DBusMessageFlags.NO_AUTO_START, member)
                    if (interface, member) == (PROPERTIES, "Set"):
                        self.assertIn(tuple(arguments[:2]), bluez.WRITES)
                        self.assertIs(type(arguments[2]), bool)
                    else:
                        self.assertIn((interface, member), bluez.CALLS)
        finally:
            self.bus.close_connections()

    def fake(self, own=True, **options) -> FakeBlueZ:
        fake = FakeBlueZ(self.bus, **options)
        self.fakes.append(fake)
        if own:
            self.assertTrue(fake.own())
        return fake

    def client(self, **options) -> bluez.BlueZ:
        changes = Changes()
        settings = {"address": self.bus.address, "schedule": self.clock.schedule, "cancel": self.clock.cancel,
                    "find_power_command": lambda: None}
        settings.update(options)
        client = bluez.BlueZ(changes, **settings)
        changes.source = client
        client.changes = changes
        self.clients.append(client)
        return client

    def until(self, predicate, timeout=3.0):
        """Run the main loop, firing notifications as they fall due, until ``predicate`` holds."""
        def check():
            self.clock.settle()
            return predicate()
        self.assertTrue(pump(check, timeout), "the expected state never came")

    def started(self, client, state=bt.ON) -> bluez.BlueZ:
        client.start()
        self.until(lambda: client.snapshot.state == state)
        self.names.add(client._connection.get_unique_name())
        return client

    def ready(self, state=bt.ON, **options) -> bluez.BlueZ:
        return self.started(self.client(**options), state)

    @staticmethod
    def find(client, name) -> bt.Device:
        return next(device for device in client.snapshot.devices if device.name == name)

    @staticmethod
    def name_of(client) -> str:
        return client._connection.get_unique_name()

    def sent(self, fake, client, member=None) -> list[str]:
        """The members ``client`` sent to ``fake``, in order."""
        name = self.name_of(client) if client._connection is not None else None
        return [call[3] for call in fake.calls if call[0] in ({name} if name else self.names)
                and (member is None or call[3] == member)]

    @contextlib.contextmanager
    def timeouts(self):
        """Record the timeout of every call any connection of this process sends."""
        seen, original = [], Gio.DBusConnection.call

        def spy(connection, name, path, interface, member, parameters, reply_type, flags, timeout, *rest):
            seen.append((member, timeout))
            return original(connection, name, path, interface, member, parameters, reply_type, flags, timeout, *rest)

        with mock.patch.object(Gio.DBusConnection, "call", spy):
            yield seen


class ReadingTest(BusCase):
    def test_a_cold_start_into_each_state(self):
        cases = [("absent", {}, bt.ABSENT), ("no adapter", {"adapters": ()}, bt.NO_ADAPTER),
                 ("on", {}, bt.ON), ("off", {"powered": False}, bt.OFF)]
        for label, options, state in cases:
            with self.subTest(label):
                fake = self.fake(own=label != "absent", **options)
                client = self.ready(state)
                self.assertEqual(client.snapshot.state, state)
                client.stop()
                fake.close()

    def test_an_adapter_blocked_by_rfkill_is_off(self):
        fake = self.fake(adapters=())
        fake.add_adapter(0, powered=False, power_state="off-blocked", signal=False)
        self.ready(bt.OFF)

    def test_a_bluez_that_cannot_be_read_is_an_error_until_retry_reads_it(self):
        fake = self.fake()
        fake.add_device(BUDS, name="Buds", paired=True, signal=False)
        fake.failures["GetManagedObjects"] = ("org.freedesktop.DBus.Error.AccessDenied", "no")
        client = self.ready(bt.ERROR)
        self.assertEqual(client.snapshot.devices, ())
        client.retry()                                              # still refused: still an error
        self.until(lambda: fake.count("GetManagedObjects") == 2 and client.idle)
        self.assertEqual(client.snapshot.state, bt.ERROR)
        del fake.failures["GetManagedObjects"]
        client.retry()
        self.until(lambda: client.snapshot.state == bt.ON)
        self.assertEqual([device.name for device in client.snapshot.devices], ["Buds"])

    def test_an_oversize_or_malformed_answer_is_an_error(self):
        fake = self.fake()
        fake.add_device(PAD, name="Pad", signal=False,
                        ManufacturerData=GLib.Variant("a{qv}", {0xFFFF: blob(bluez.MAX_REPLY_BYTES)}))
        client = self.ready(bt.ERROR)
        fake.remove_device(address_path(PAD))
        fake.managed_reply = GLib.Variant("(s)", ("not what was asked",))
        client.retry()
        self.until(lambda: fake.count("GetManagedObjects") == 2 and client.idle)
        self.assertEqual(client.snapshot.state, bt.ERROR)
        fake.managed_reply = None
        client.retry()
        self.until(lambda: client.snapshot.state == bt.ON)

    def test_a_read_that_is_never_answered_is_an_error(self):
        fake = self.fake()
        fake.hold = lambda call: call[3] == "GetManagedObjects"
        with mock.patch.object(bluez, "CALL_TIMEOUT_MS", 60):
            client = self.ready(bt.ERROR)
        fake.answer_held()                                          # far too late: dropped
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(client.snapshot.state, bt.ERROR)

    def test_retry_does_nothing_unless_the_read_failed(self):
        fake = self.fake()
        client = self.ready()
        client.retry()
        self.assertEqual(fake.count("GetManagedObjects"), 1)

    def test_the_name_appearing_and_vanishing(self):
        client = self.ready(bt.ABSENT)
        fake = self.fake(own=False)
        fake.add_device(BUDS, name="Buds", paired=True, signal=False)
        self.assertTrue(fake.own())
        self.until(lambda: client.snapshot.state == bt.ON and client.snapshot.devices)
        fake.release()
        self.until(lambda: client.snapshot.state == bt.ABSENT)
        self.assertEqual(client.snapshot, bt.Snapshot(bt.ABSENT))

    def test_bluez_restarting_is_read_from_scratch_and_the_old_answers_are_dropped(self):
        first = self.fake()
        first.add_device(BUDS, name="Buds", paired=True, signal=False)
        client = self.ready()
        old = self.find(client, "Buds").id
        first.hold = lambda call: True
        first.emit_changed(address_path(BUDS), DEVICE, {"Connected": GLib.Variant("b", True)})
        self.until(lambda: self.find(client, "Buds").connected)
        client.scan(True)
        self.until(lambda: first.held)
        first.close()
        self.until(lambda: client.snapshot.state == bt.ABSENT)
        self.assertFalse(client.snapshot.scanning)
        second = self.fake()
        second.add_device(BUDS, name="Buds", paired=True, signal=False)
        second.add_device(PAD, name="Pad", signal=False)
        self.until(lambda: client.snapshot.state == bt.ON and len(client.snapshot.devices) == 2)
        self.assertNotIn(old, [device.id for device in client.snapshot.devices])
        self.assertFalse(self.find(client, "Buds").connected)
        self.assertEqual(self.sent(second, client), ["GetManagedObjects"])

    def test_the_lowest_numbered_adapter_is_used(self):
        fake = self.fake(adapters=(1, 0))
        fake.add_device(PAD, adapter=1, name="Second", paired=True, signal=False)
        fake.add_device(BUDS, adapter=0, name="First", paired=True, signal=False)
        client = self.ready()
        self.assertEqual([device.name for device in client.snapshot.devices], ["First"])
        fake.set("/org/bluez/hci1", ADAPTER, Powered=False)                 # not the adapter in use: no change
        fake.remove_adapter(0)
        self.until(lambda: [device.name for device in client.snapshot.devices] == ["Second"])
        self.assertEqual(client.snapshot.state, bt.OFF)
        self.assertEqual(fake.count("GetManagedObjects"), 2)                # the next adapter's devices are read
        fake.remove_adapter(1)
        self.until(lambda: client.snapshot.state == bt.NO_ADAPTER)
        fake.add_adapter(2)
        self.until(lambda: client.snapshot.state == bt.ON)

    def test_devices_and_their_names(self):
        fake = self.fake()
        fake.add_device(PAD, name="\u202ePad\x07 (AA:BB:CC:DD:EE:01)", signal=False)
        fake.add_device(BUDS, name=None, icon=None, signal=False)               # Alias is the address: no name
        fake.add_device(KEYS, name="Keys", icon=None, signal=False, Class=0x002540)
        client = self.ready()
        devices = {device.id: device for device in client.snapshot.devices}
        self.assertEqual(sorted((device.name, device.kind) for device in devices.values()),
                         [("", "other"), ("Keys", "keyboard"), ("Pad", "gamepad")])
        self.assertEqual([device.name for device in bt.nearby(devices.values())], ["Pad", "Keys"])
        for device in devices.values():
            self.assertNotIn("AA", repr(device))                             # no address, no path

    def test_a_device_that_goes_and_comes_back_is_a_new_device(self):
        fake = self.fake()
        client = self.ready()
        path = fake.add_device(PAD, name="Pad")
        self.until(lambda: client.snapshot.devices)
        first = self.find(client, "Pad")
        fake.remove_device(path)
        self.until(lambda: not client.snapshot.devices)
        fake.add_device(PAD, name="Pad")
        self.until(lambda: client.snapshot.devices)
        second = self.find(client, "Pad")
        self.assertGreater(second.id, first.id)
        self.assertGreater(second.order, first.order)

    def test_a_connectable_property_a_newer_bluez_may_add_hides_unpaired_devices_it_rules_out(self):
        # BlueZ 5.87 has no Device1.Connectable: this only shows that the client copes when one appears.
        fake = self.fake()
        fake.add_device(PAD, name="Beacon", signal=False, Connectable=False)
        fake.add_device(BUDS, name="Buds", paired=True, signal=False, Connectable=False)
        fake.add_device(KEYS, name="Keys", signal=False, Connectable=True)
        client = self.ready()
        self.assertEqual(sorted(device.name for device in client.snapshot.devices), ["Buds", "Keys"])

    def test_the_battery_comes_from_the_device_itself(self):
        fake = self.fake()
        path = fake.add_device(BUDS, name="Buds", paired=True, signal=False)
        client = self.ready()
        self.assertIsNone(self.find(client, "Buds").battery)
        fake.add_component_battery(path, 1, 5)                                  # one earbud: ignored
        fake.add_battery(path, 80)
        self.until(lambda: self.find(client, "Buds").battery == 80)
        fake.set(path, BATTERY, Percentage=120)
        self.until(lambda: self.find(client, "Buds").battery == 100)            # clamped
        fake.set(f"{path}/battery_1", BATTERY, Percentage=1)
        fake.emit_changed(path, BATTERY, {"Percentage": GLib.Variant("i", 3)})  # the wrong type: no battery
        self.until(lambda: self.find(client, "Buds").battery is None)
        fake.set(path, BATTERY, Percentage=64)
        self.until(lambda: self.find(client, "Buds").battery == 64)
        fake.emit_changed(path, BATTERY, {}, ["Percentage"])
        self.until(lambda: self.find(client, "Buds").battery is None)
        fake.set(path, BATTERY, Percentage=30)
        self.until(lambda: self.find(client, "Buds").battery == 30)
        fake.remove_battery(path)
        self.until(lambda: self.find(client, "Buds").battery is None)

    def test_properties_of_the_wrong_type_fall_back_and_never_raise(self):
        fake = self.fake()
        fake.add_device(PAD, signal=False, name=GLib.Variant("i", 5), Alias=GLib.Variant("u", 7),
                        Paired=GLib.Variant("s", "yes"), Connected=GLib.Variant("i", 1), Icon=GLib.Variant("b", True),
                        Class=GLib.Variant("s", "0x2540"), Appearance=GLib.Variant("u", 0x03C1))
        fake.add_adapter(1, powered=GLib.Variant("s", "yes"), signal=False)
        client = self.ready()
        device, = client.snapshot.devices
        self.assertEqual((device.name, device.kind, device.paired, device.connected), ("", "other", False, False))
        path = address_path(PAD)
        fake.emit_changed(path, DEVICE, {"Paired": GLib.Variant("ay", b"\x01"), "Name": GLib.Variant("s", "Pad")})
        self.until(lambda: client.snapshot.devices[0].name == "Pad")
        fake.connection.emit_signal(None, "/", "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
                                    GLib.Variant("(s)", ("not the right shape",)))
        fake.connection.emit_signal(None, path, PROPERTIES, "PropertiesChanged",
                                    GLib.Variant("(sa{ss})", (DEVICE, {"Name": "x"})))
        fake.emit_added(path, {DEVICE: {"Name": GLib.Variant("s", "Pad two")}, "org.example.Other": {}})
        self.until(lambda: client.snapshot.devices[0].name == "Pad two")

    def test_thousands_of_devices_are_cut_to_what_a_snapshot_lists(self):
        fake = self.fake()
        objects = {path: {name: dict(properties) for name, properties in interfaces.items()}
                   for path, interfaces in fake.objects.items()}
        for number in range(2000):
            address = f"AA:BB:CC:{number >> 16 & 0xFF:02X}:{number >> 8 & 0xFF:02X}:{number & 0xFF:02X}"
            objects[address_path(address)] = {DEVICE: {"Alias": GLib.Variant("s", f"Device {number}"),
                                                       "Name": GLib.Variant("s", f"Device {number}"),
                                                       "Paired": GLib.Variant("b", number in (1990, 1999))}}
        fake.managed_reply = GLib.Variant("(a{oa{sa{sv}}})", (objects,))
        client = self.ready()
        snapshot = client.snapshot
        self.assertTrue(snapshot.truncated)
        self.assertEqual(len(snapshot.devices), bt.MAX_DEVICES)
        self.assertEqual(len(client._devices), bluez.MAX_TRACKED)
        self.assertEqual([device.name for device in bt.remembered(snapshot.devices)], ["Device 1990", "Device 1999"])
        self.assertEqual(snapshot.devices[2].name, "Device 0")                  # then the rest in the order read

    def crowd(self, *devices) -> tuple[FakeBlueZ, bluez.BlueZ, list[str]]:
        """A fake with these (name, paired) devices, read by a client that tracks four at most."""
        patch = mock.patch.object(bluez, "MAX_TRACKED", 4)
        patch.start()
        self.addCleanup(patch.stop)
        fake = self.fake()
        paths = [fake.add_device(f"AA:BB:CC:DD:EE:{number:02X}", name=name, icon=None, paired=paired, signal=False)
                 for number, (name, paired) in enumerate(devices, start=16)]
        return fake, self.ready(), paths

    def tracked(self, client) -> set[str]:
        return {record.path for record in client._devices.values()}

    def test_in_a_crowd_a_new_device_takes_the_place_of_the_oldest_nameless_one(self):
        fake, client, paths = self.crowd(("Named", False), (None, False), (None, False), ("Kept", True))
        self.assertEqual((self.tracked(client), client.snapshot.truncated), (set(paths), False))
        newcomer = fake.add_device(PAD, name="Pad")
        self.until(lambda: newcomer in self.tracked(client))
        self.assertEqual(self.tracked(client), {paths[0], paths[2], paths[3], newcomer})   # not the oldest: named
        self.assertIn("Pad", [device.name for device in bt.nearby(client.snapshot.devices)])
        self.assertTrue(client.snapshot.truncated)                     # the one let go is still there
        fake.remove_device(paths[1])                                   # and once it is gone, nothing is left out
        self.until(lambda: not client.snapshot.truncated)

    def test_in_a_crowd_remembered_devices_and_the_device_being_paired_stay(self):
        fake, client, paths = self.crowd(("Remembered", True), ("Target", False), ("Old", False), ("Newer", False))
        fake.hold = lambda call: call[3] == "RegisterAgent"            # the pairing stays where it is
        client.pair(self.find(client, "Target").id)
        self.until(lambda: fake.held)
        newcomer = fake.add_device(PAD, name="Pad")
        self.until(lambda: newcomer in self.tracked(client))
        self.assertEqual(self.tracked(client), {paths[0], paths[1], paths[3], newcomer})
        self.assertEqual(client.snapshot.activity.kind, bt.PAIRING)

    def test_in_a_crowd_a_nameless_newcomer_does_not_push_out_a_named_device(self):
        fake, client, paths = self.crowd(("One", False), ("Two", False), ("Three", False), ("Four", False))
        nameless = fake.add_device(PAD, name=None, icon=None)
        self.until(lambda: client.snapshot.truncated)
        self.assertEqual(self.tracked(client), set(paths))
        fake.remove_device(nameless)                                   # never tracked, and gone: nothing left out
        self.until(lambda: not client.snapshot.truncated)

    def test_changes_nobody_shows_do_not_even_arm_a_timer(self):
        fake = self.fake()
        path = fake.add_device(PAD, name="Pad", signal=False)
        client = self.ready()
        snapshot = client.snapshot
        for changed in ({"RSSI": GLib.Variant("n", -40)}, {"TxPower": GLib.Variant("n", 4)},
                        {"ManufacturerData": GLib.Variant("a{qv}", {0x004C: blob(64)})},
                        {"ServiceData": GLib.Variant("a{sv}", {})}, {"AdvertisingFlags": GLib.Variant("ay", b"\x06")},
                        {"Name": GLib.Variant("s", "Pad"), "Paired": GLib.Variant("b", False)}):
            client._on_signal(None, fake.unique_name, path, PROPERTIES, "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)", (DEVICE, changed, [])), client._epoch)
            self.assertEqual(self.clock.pending, {}, changed)
        self.assertIs(client.snapshot, snapshot)
        self.assertTrue(client.idle)

    def test_changes_nobody_shows_are_not_reported(self):
        fake = self.fake()
        path = fake.add_device(PAD, name="Pad", signal=False)
        client = self.ready()
        snapshot, changes = client.snapshot, len(client.changes)
        fake.set(path, DEVICE, RSSI=-30, TxPower=4)
        fake.set(path, DEVICE, ManufacturerData=GLib.Variant("a{qv}", {0x004C: blob(2_000_000)}))
        fake.set(path, DEVICE, ServicesResolved=True, UUIDs=[HEADSET_UUID])
        fake.emit_changed(path, DEVICE, {"ServiceData": GLib.Variant("a{sv}", {}),
                                         "AdvertisingFlags": GLib.Variant("ay", b"\x06")})
        fake.set("/org/bluez/hci0", ADAPTER, Discovering=True)
        fake.set(path, DEVICE, Name="Pad")                                      # the same as before
        marker = fake.add_device(KEYS, name="Marker")
        self.until(lambda: len(client.snapshot.devices) == 2)
        self.assertEqual(len(client.changes), changes + 1)                      # the marker, and nothing before it
        self.assertIs(client.changes.seen[-1], client.snapshot)
        self.assertEqual(client.snapshot.devices[0], snapshot.devices[0])
        fake.remove_device(marker)
        self.until(lambda: len(client.snapshot.devices) == 1)

    def test_a_burst_of_changes_is_one_timer_and_one_notification(self):
        fake = self.fake()
        path = fake.add_device(BUDS, name="Buds", paired=True, battery=50, signal=False)
        client = self.ready()
        self.assertTrue(client.idle)
        changes = len(client.changes)
        for percent in range(51, 91):
            fake.set(path, BATTERY, Percentage=percent)
        self.assertTrue(pump(lambda: self.find(client, "Buds").battery == 90))
        self.assertEqual(self.clock.delays_pending(), [bluez.COALESCE_MS])      # one timer for the whole burst
        self.assertEqual(len(client.changes), changes)
        self.clock.fire_after(bluez.COALESCE_MS)
        self.assertEqual(len(client.changes), changes + 1)
        self.assertEqual(client.changes.seen[-1].devices[0].battery, 90)
        self.assertTrue(client.idle)

    def test_signals_from_anyone_but_the_owner_are_ignored(self):
        fake = self.fake()
        path = fake.add_device(PAD, name="Pad", signal=False)
        client = self.ready()
        snapshot = client.snapshot
        impostor = self.bus.connect()
        renamed = {"Alias": GLib.Variant("s", "Impostor"), "Name": GLib.Variant("s", "Impostor")}
        impostor.emit_signal(None, path, PROPERTIES, "PropertiesChanged",
                             GLib.Variant("(sa{sv}as)", (DEVICE, renamed, [])))
        impostor.emit_signal(None, "/", "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved",
                             GLib.Variant("(oas)", (path, [DEVICE])))
        parameters = GLib.Variant("(sa{sv}as)", (DEVICE, renamed, []))
        client._on_signal(None, impostor.get_unique_name(), path, PROPERTIES, "PropertiesChanged", parameters,
                          client._epoch)                                        # delivered after all: dropped
        client._on_signal(None, fake.unique_name, path, PROPERTIES, "PropertiesChanged", parameters,
                          client._epoch - 1)                                    # from an earlier owner: dropped
        self.assertFalse(pump(timeout=0.05))
        self.clock.settle()
        self.assertIs(client.snapshot, snapshot)
        self.assertEqual(client.snapshot.devices[0].name, "Pad")


class LifecycleTest(BusCase):
    def test_a_started_source_that_is_left_alone_holds_no_timer_and_sends_nothing_more(self):
        fake = self.fake()
        fake.add_device(PAD, name="Pad", signal=False)
        client = self.ready()
        self.assertTrue(client.idle)
        self.assertEqual(self.clock.pending, {})
        self.assertFalse(pump(timeout=0.1))
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects"])       # nothing before the user asks
        self.assertEqual(fake.sessions["/org/bluez/hci0"], set())
        self.assertEqual(fake.agents, {})

    def test_start_never_opens_the_shared_system_connection(self):
        self.fake()
        with mock.patch.object(Gio, "bus_get") as bus_get, mock.patch.object(Gio, "bus_get_sync") as bus_get_sync, \
                mock.patch.object(Gio, "bus_watch_name") as watch:
            client = self.ready()
        self.assertEqual((bus_get.call_count, bus_get_sync.call_count, watch.call_count), (0, 0, 0))
        self.assertFalse(client._connection.get_exit_on_close())

    def test_without_an_address_it_asks_for_the_system_bus_address_and_nothing_else(self):
        self.fake()
        client = bluez.BlueZ(lambda: None, schedule=self.clock.schedule, cancel=self.clock.cancel)
        self.clients.append(client)
        with mock.patch.object(Gio, "dbus_address_get_for_bus_sync", return_value=self.bus.address) as address:
            client.start()
        address.assert_called_once_with(Gio.BusType.SYSTEM, None)
        self.until(lambda: client.snapshot.state == bt.ON)
        self.names.add(self.name_of(client))

    def test_a_bus_that_cannot_be_reached_means_no_bluetooth(self):
        client = self.client(address="unix:abstract=omarchy-game-mode-test-nobody-listens-here")
        client.start()
        self.until(lambda: client.snapshot.state == bt.ABSENT)
        self.assertTrue(client.idle)

    def test_start_and_stop_are_idempotent(self):
        fake = self.fake()
        client = self.client()
        client.start()
        client.start()
        self.until(lambda: client.snapshot.state == bt.ON)
        self.names.add(self.name_of(client))
        self.assertEqual(fake.count("GetManagedObjects"), 1)
        client.stop()
        client.stop()
        self.assertIs(client.snapshot, bluez.IDLE)
        self.assertTrue(client.idle)

    def test_stopping_mid_search_closes_the_connection_which_ends_the_search(self):
        fake = self.fake()
        client = self.ready()
        name, connection = self.name_of(client), client._connection
        client.scan(True)
        self.until(lambda: fake.sessions["/org/bluez/hci0"] == {name})
        client.stop()
        self.until(lambda: name in fake.gone)
        self.assertTrue(connection.is_closed())
        self.assertEqual(fake.sessions["/org/bluez/hci0"], set())
        self.assertFalse(fake.value("/org/bluez/hci0", ADAPTER, "Discovering"))
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects", "StartDiscovery"])   # no StopDiscovery

    def test_stopping_mid_pairing_drops_the_agent_and_nothing_runs_afterwards(self):
        fake = self.fake()
        path = fake.add_device(PAD, name="Pad", signal=False, script=[("RequestConfirmation", 11)])
        client = self.ready()
        name = self.name_of(client)
        client.pair(self.find(client, "Pad").id)
        self.until(lambda: client.snapshot.prompt is not None)
        prompt, agent = client.snapshot.prompt, client._op.agent
        fake.hold = lambda call: True
        fake.set(path, DEVICE, Name="Renamed")
        changes = len(client.changes)
        client.stop()
        self.assertEqual((agent.active, agent.exported, agent.waiting), (False, False, ""))   # answered Canceled
        self.until(lambda: name in fake.gone)
        self.assertEqual((fake.agents, fake.queue, fake.pairings), ({}, [], {}))
        confirmation = next(record for _agent, record in fake.asked if record.member == "RequestConfirmation")
        self.until(lambda: confirmation.done)
        self.assertIn(confirmation.error, (CANCELED, "org.freedesktop.DBus.Error.NoReply"))
        client.answer(prompt.id, True)
        client.cancel()
        fake.set(path, DEVICE, Name="Again")
        fake.answer_held()
        self.assertFalse(pump(timeout=0.1))
        self.assertEqual((len(client.changes), self.clock.pending), (changes, {}))
        self.assertIs(client.snapshot, bluez.IDLE)
        self.assertEqual(self.sent(fake, client, "UnregisterAgent"), [])        # the closed connection took it
        self.assertEqual(self.sent(fake, client, "CancelPairing"), [])

    def test_ids_are_never_reused_across_stop_and_start(self):
        fake = self.fake()
        fake.add_device(PAD, name="Pad", signal=False)
        client = self.ready()
        seen = {self.find(client, "Pad").id}
        for _ in range(3):
            client.stop()
            self.started(client)
            ident = self.find(client, "Pad").id
            self.assertNotIn(ident, seen)
            seen.add(ident)

    def test_the_snapshot_is_the_same_object_until_something_changes(self):
        fake = self.fake()
        path = fake.add_device(PAD, name="Pad", signal=False)
        client = self.ready()
        first = client.snapshot
        self.assertIs(client.snapshot, first)
        fake.set(path, DEVICE, RSSI=-20)
        self.assertFalse(pump(timeout=0.05))
        self.assertIs(client.snapshot, first)
        fake.set(path, DEVICE, Connected=True)
        self.until(lambda: client.snapshot is not first)
        with self.assertRaises(AttributeError):
            client.snapshot.state = bt.OFF

    def test_a_failing_window_does_not_break_the_source(self):
        fake = self.fake()
        path = fake.add_device(PAD, name="Pad", paired=True, signal=False)
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError("the window broke")

        client = self.client()
        client._on_change = broken
        self.started(client)
        fake.set(path, DEVICE, Connected=True)
        self.until(lambda: self.find(client, "Pad").connected and len(calls) == 2)
        self.assertEqual(client.error, "RuntimeError")
        client.connect(self.find(client, "Pad").id)                    # it still works, and still reports
        self.until(lambda: client.snapshot.activity is None and len(calls) >= 4)
        self.assertIsNone(client.snapshot.failure)
        client.error = ""

    def test_no_public_method_raises_whatever_it_is_given(self):
        self.fake()
        client = self.ready()
        for method in (client.connect, client.disconnect, client.pair, client.forget):
            for value in (None, "1", 1.0, True, -1, 10 ** 30):
                method(value)
                client.dismiss()
        for value in (None, "", object()):
            client.answer(value, value)
            client.scan(value)
        self.assertEqual(client.error, "")


class SearchTest(BusCase):
    HCI0 = "/org/bluez/hci0"

    def test_a_search_starts_only_when_asked_and_ends_by_itself(self):
        fake = self.fake()
        client = self.ready()
        name = self.name_of(client)
        client.scan(True)
        self.assertTrue(client.snapshot.scanning)                              # from the moment it is sent
        self.until(lambda: fake.sessions[self.HCI0] == {name})
        self.assertIn(bt.SCAN_SECONDS * 1000, self.clock.delays_pending())
        path = fake.add_device(PAD, name="Pad")
        self.until(lambda: client.snapshot.devices)
        self.clock.fire_after(bt.SCAN_SECONDS * 1000)
        self.assertFalse(client.snapshot.scanning)
        self.until(lambda: fake.sessions[self.HCI0] == set() and client.idle)
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects", "StartDiscovery", "StopDiscovery"])
        self.assertEqual([member for member in fake.members() if member[1] == "SetDiscoveryFilter"], [])
        self.assertIn(path, fake.objects)

    def test_scan_false_ends_it_and_a_second_scan_true_starts_another(self):
        fake = self.fake()
        client = self.ready()
        client.scan(True)
        client.scan(True)
        self.until(lambda: fake.sessions[self.HCI0])
        client.scan(False)
        client.scan(False)
        self.until(lambda: not fake.sessions[self.HCI0])
        client.scan(True)
        self.until(lambda: fake.sessions[self.HCI0])
        self.assertEqual(self.sent(fake, client)[1:], ["StartDiscovery", "StopDiscovery", "StartDiscovery"])
        self.assertEqual(self.clock.delays_pending(), [bt.SCAN_SECONDS * 1000])

    def test_a_stop_asked_for_before_bluez_answered_the_start_follows_the_answer(self):
        fake = self.fake()
        client = self.ready()
        name = self.name_of(client)
        fake.defer = lambda call: call[3] == "StartDiscovery"
        client.scan(True)
        self.until(lambda: fake.deferred)
        self.assertEqual(fake.sessions[self.HCI0], {name})                     # BlueZ counts it, start pending
        client.scan(False)
        self.assertFalse(client.snapshot.scanning)
        self.assertFalse(self.clock.armed(bt.SCAN_SECONDS * 1000))
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(self.sent(fake, client, "StopDiscovery"), [])         # BlueZ would refuse it now
        fake.answer_deferred()
        self.until(lambda: self.sent(fake, client, "StopDiscovery") and client.idle)
        self.assertEqual(fake.sessions[self.HCI0], set())                      # the stop came, and was taken
        self.assertIsNone(client.snapshot.failure)

    def test_a_search_asked_for_while_the_last_stop_is_unanswered_starts_after_it(self):
        fake = self.fake()
        client = self.ready()
        name = self.name_of(client)
        client.scan(True)
        self.until(lambda: client._scan_active)
        fake.defer = lambda call: call[3] == "StopDiscovery"
        client.scan(False)
        self.until(lambda: fake.deferred)                   # BlueZ still counts this client until it answers
        client.scan(True)
        self.assertTrue(client.snapshot.scanning)
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(self.sent(fake, client)[1:], ["StartDiscovery", "StopDiscovery"])   # no start yet
        fake.defer = None
        fake.answer_deferred()
        self.until(lambda: client._scan_active)
        self.assertEqual(self.sent(fake, client)[1:], ["StartDiscovery", "StopDiscovery", "StartDiscovery"])
        self.assertEqual(fake.sessions[self.HCI0], {name})                     # a search that really runs
        self.assertTrue(client.snapshot.scanning)
        self.assertIsNone(client.snapshot.failure)

    def test_a_search_cancelled_while_the_last_stop_is_unanswered_sends_nothing_more(self):
        fake = self.fake()
        client = self.ready()
        client.scan(True)
        self.until(lambda: client._scan_active)
        fake.defer = lambda call: call[3] == "StopDiscovery"
        client.scan(False)
        self.until(lambda: fake.deferred)
        client.scan(True)
        client.scan(False)
        fake.answer_deferred()
        self.until(lambda: client.idle)
        self.assertEqual(self.sent(fake, client)[1:], ["StartDiscovery", "StopDiscovery"])
        self.assertEqual(fake.sessions[self.HCI0], set())
        self.assertFalse(client.snapshot.scanning)

    def test_a_stop_before_the_start_was_answered_and_then_stop_sends_nothing_to_the_closed_connection(self):
        fake = self.fake()
        client = self.ready()
        name = self.name_of(client)
        fake.defer = lambda call: call[3] == "StartDiscovery"
        client.scan(True)
        self.until(lambda: fake.deferred)
        client.stop()
        self.until(lambda: name in fake.gone)
        fake.answer_deferred()                                                 # the start, answered too late
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(fake.sessions[self.HCI0], set())                      # BlueZ drops it with the connection
        self.assertEqual(self.sent(fake, client)[-1], "StartDiscovery")

    def test_a_search_that_cannot_start_says_why(self):
        fake = self.fake()
        client = self.ready()
        fake.failures["StartDiscovery"] = ("org.bluez.Error.NotReady", "Resource Not Ready")
        client.scan(True)
        self.until(lambda: client.snapshot.failure is not None)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.NOT_READY, bt.SEARCHING))
        self.assertFalse(client.snapshot.scanning)
        self.assertEqual(self.clock.delays_pending(), [])
        del fake.failures["StartDiscovery"]
        client.scan(True)                                                      # a new search clears the failure
        self.assertIsNone(client.snapshot.failure)
        self.until(lambda: fake.sessions[self.HCI0])

    def test_already_searching_counts_as_searching(self):
        fake = self.fake()
        client = self.ready()
        fake.failures["StartDiscovery"] = ("org.bluez.Error.InProgress", "Operation already in progress")
        client.scan(True)
        self.until(lambda: client._scan_active)
        self.assertTrue(client.snapshot.scanning)
        self.assertIsNone(client.snapshot.failure)

    def test_no_search_while_the_adapter_is_off_or_something_runs(self):
        fake = self.fake(powered=False)
        client = self.ready(bt.OFF)
        client.scan(True)
        self.assertFalse(client.snapshot.scanning)
        fake.set(self.HCI0, ADAPTER, Powered=True)
        self.until(lambda: client.snapshot.state == bt.ON)
        fake.add_device(BUDS, name="Buds", paired=True)
        self.until(lambda: client.snapshot.devices)
        fake.defer = lambda call: call[3] == "Connect"
        client.connect(self.find(client, "Buds").id)
        client.scan(True)
        self.assertFalse(client.snapshot.scanning)
        self.assertEqual(self.sent(fake, client, "StartDiscovery"), [])

    def test_the_adapter_going_off_ends_the_search_without_a_stop(self):
        fake = self.fake()
        client = self.ready()
        client.scan(True)
        self.until(lambda: fake.sessions[self.HCI0])
        fake.set(self.HCI0, ADAPTER, Powered=False, PowerState="off")
        fake.sessions[self.HCI0] = set()
        self.until(lambda: client.snapshot.state == bt.OFF)
        self.assertFalse(client.snapshot.scanning)
        self.assertEqual(self.sent(fake, client, "StopDiscovery"), [])
        self.assertEqual(self.clock.delays_pending(), [])

    def test_the_adapter_going_away_ends_the_search(self):
        fake = self.fake()
        client = self.ready()
        client.scan(True)
        self.until(lambda: fake.sessions[self.HCI0])
        fake.remove_adapter(0)
        self.until(lambda: client.snapshot.state == bt.NO_ADAPTER)
        self.assertFalse(client.snapshot.scanning)
        self.assertEqual(self.sent(fake, client, "StopDiscovery"), [])


class PairingCase(BusCase):
    """A client that is ready, a fake with a device to pair, and what both said."""

    def pairing(self, script=None, **device) -> tuple[FakeBlueZ, bluez.BlueZ, int, str]:
        fake = self.fake()
        path = fake.add_device(PAD, name="Pad", signal=False, script=script or [], **device)
        client = self.ready()
        return fake, client, self.find(client, "Pad").id, path

    def agent(self, fake, client) -> tuple[str, str]:
        return self.name_of(client), fake.agents[self.name_of(client)][0]

    def assert_agent_gone(self, fake, client, unregistered=True):
        """Neither registered with BlueZ nor answering on the bus."""
        name = self.name_of(client)
        self.until(lambda: name not in fake.agents)
        path = next(arguments[0] for sender, _path, _interface, member, arguments in fake.calls
                    if member == "RegisterAgent" and sender == name)
        self.assertTrue(path.startswith(bluez.AGENT_ROOT + "/"))
        record = call_agent(self.bus.connect(), name, path, "RequestAuthorization", (address_path(PAD),))
        self.until(lambda: record.done)
        self.assertEqual(record.error, "org.freedesktop.DBus.Error.UnknownMethod")    # nothing there any more
        self.assertEqual(len(self.sent(fake, client, "UnregisterAgent")), 1 if unregistered else 0)


class PairingTest(PairingCase):
    def test_pairing_registers_an_agent_pairs_trusts_and_connects(self):
        fake, client, device, path = self.pairing()
        with self.timeouts() as timeouts:
            client.pair(device)
            self.assertEqual(client.snapshot.activity, bt.Activity(bt.PAIRING, device, "Pad", bt.STAGE_PAIR))
            self.until(lambda: client.snapshot.activity is None)
        stages = [snapshot.activity.stage for snapshot in client.changes.seen if snapshot.activity is not None]
        self.assertEqual(sorted(set(stages), key=stages.index), [bt.STAGE_PAIR, bt.STAGE_TRUST, bt.STAGE_CONNECT])
        pad = self.find(client, "Pad")
        self.assertEqual((pad.paired, pad.trusted, pad.connected, client.snapshot.failure), (True, True, True, None))
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects", "RegisterAgent", "Pair", "UnregisterAgent",
                                                   "Set", "Connect"])
        register = fake.last("RegisterAgent")
        self.assertEqual(register[4][1], "KeyboardDisplay")
        self.assertEqual(fake.last("Set")[1:], (path, PROPERTIES, "Set", (DEVICE, "Trusted", True)))
        self.assertEqual(dict(timeouts), {"RegisterAgent": bluez.CALL_TIMEOUT_MS, "Pair": bluez.PAIR_TIMEOUT_MS,
                                          "UnregisterAgent": bluez.CALL_TIMEOUT_MS, "Set": bluez.CALL_TIMEOUT_MS,
                                          "Connect": bluez.CONNECT_TIMEOUT_MS})
        self.assert_agent_gone(fake, client)
        self.assertTrue(client.idle)

    def test_a_search_ends_when_a_pairing_starts(self):
        fake, client, device, _path = self.pairing()
        client.scan(True)
        self.until(lambda: fake.sessions["/org/bluez/hci0"])
        client.pair(device)
        self.assertFalse(client.snapshot.scanning)
        self.until(lambda: client.snapshot.activity is None and not fake.sessions["/org/bluez/hci0"])
        self.assertEqual(sorted(self.sent(fake, client)[1:4]), ["RegisterAgent", "StartDiscovery", "StopDiscovery"])

    def test_the_agent_is_the_one_bluez_asks_and_it_never_becomes_the_default_by_request(self):
        fake, client, device, _path = self.pairing([WAIT, ("RequestAuthorization",)])
        client.pair(device)
        self.until(lambda: fake.pairings)
        ours = self.agent(fake, client)
        self.assertEqual(fake.default_agent(), ours)                           # the first registered: the default
        fake.resume()
        self.until(lambda: client.snapshot.activity is None)
        (agent, record), = fake.asked
        self.assertEqual((agent, record.error, record.value), (ours, "", ()))
        self.assertIsNone(client.snapshot.failure)
        self.assertEqual(self.sent(fake, client, "RequestDefaultAgent"), [])
        self.assertIsNone(fake.default_agent())

    def test_another_programs_agent_stays_the_default_and_is_never_asked_about_this_pairing(self):
        fake = self.fake()
        fake.add_device(PAD, name="Pad", signal=False, script=[("RequestConfirmation", 4321)])
        standin = StandInAgent(self.bus)
        standin.register()
        self.until(lambda: standin.registered)
        client = self.ready()
        client.pair(self.find(client, "Pad").id)
        self.until(lambda: client.snapshot.prompt is not None)
        self.assertEqual(fake.default_agent(), (standin.unique_name, standin.path))   # ours queues behind it
        self.assertEqual(fake.queue, [standin.unique_name, self.name_of(client)])
        self.assertEqual(fake.asked[0][0], self.agent(fake, client))                  # our Pair(): our agent
        client.answer(client.snapshot.prompt.id, True)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(standin.calls, [])
        self.assertEqual(fake.default_agent(), (standin.unique_name, standin.path))
        self.assertEqual(list(fake.agents), [standin.unique_name])

    def test_a_number_to_compare(self):
        fake, client, device, _path = self.pairing([("RequestConfirmation", 4321)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        prompt = client.snapshot.prompt
        self.assertEqual(prompt, bt.Prompt(prompt.id, bt.CONFIRM, device, "Pad", code="004321"))
        client.answer(prompt.id + 1, True)                                     # a prompt that does not exist
        client.answer(prompt.id, "yes")
        self.assertIs(client.snapshot.prompt, prompt)
        client.answer(prompt.id, True)
        self.assertIsNone(client.snapshot.prompt)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(fake.asked[0][1].value, ())
        self.assertTrue(self.find(client, "Pad").connected)

    def test_a_number_that_does_not_match(self):
        fake, client, device, _path = self.pairing([("RequestConfirmation", 4321)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        client.answer(client.snapshot.prompt.id, False)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(fake.asked[0][1].error, REJECTED)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.AUTH_FAILED, bt.PAIRING, bt.STAGE_PAIR, device, "Pad"))
        self.assert_agent_gone(fake, client)
        self.assertEqual(self.sent(fake, client)[-2:], ["Pair", "UnregisterAgent"])

    def test_a_passkey_typed_on_the_device_is_one_prompt_that_counts(self):
        steps = [("DisplayPasskey", 9, entered) for entered in range(7)]
        script = [step for pair in zip(steps, [WAIT] * 7) for step in pair]
        fake, client, device, _path = self.pairing(script)
        client.pair(device)
        prompts = []
        for typed in range(7):
            self.until(lambda typed=typed: client.snapshot.prompt is not None and client.snapshot.prompt.typed == typed)
            prompts.append(client.snapshot.prompt)
            fake.resume()
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual({prompt.id for prompt in prompts}, {prompts[0].id})
        self.assertEqual({(prompt.kind, prompt.code) for prompt in prompts}, {(bt.CODE, "000009")})
        self.assertIsNone(client.snapshot.prompt)
        self.assertIsNone(client.snapshot.failure)

    def test_a_passkey_to_enter(self):
        fake, client, device, _path = self.pairing([("RequestPasskey",)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        prompt = client.snapshot.prompt
        self.assertEqual((prompt.kind, prompt.max_length, prompt.code), (bt.PASSKEY, bt.PASSKEY_MAX, ""))
        for wrong in ("12a", "1234567", "١٢٣٤", "", 12, None):
            client.answer(prompt.id, wrong)
        self.assertIs(client.snapshot.prompt, prompt)
        client.answer(prompt.id, "000123")
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(fake.asked[0][1].value, (123,))

    def test_a_pin_to_enter(self):
        fake, client, device, _path = self.pairing([("RequestPinCode",)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        prompt = client.snapshot.prompt
        self.assertEqual((prompt.kind, prompt.max_length), (bt.PIN, bt.PIN_MAX))
        client.answer(prompt.id, "1" * 17)
        self.assertIs(client.snapshot.prompt, prompt)
        client.answer(prompt.id, "0000")
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(fake.asked[0][1].value, ("0000",))

    def test_a_pin_to_type_on_the_device_stays_until_the_pairing_ends(self):
        fake, client, device, _path = self.pairing([("DisplayPinCode", "\u202e1234\x1b"), WAIT])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        self.assertEqual(client.snapshot.prompt, bt.Prompt(client.snapshot.prompt.id, bt.CODE, device, "Pad",
                                                           code="1234"))
        client.answer(client.snapshot.prompt.id, "1234")                       # nothing to answer
        self.assertFalse(pump(timeout=0.05))
        self.assertIsNotNone(client.snapshot.prompt)
        fake.resume()
        self.until(lambda: client.snapshot.activity is None)
        self.assertIsNone(client.snapshot.prompt)

    def test_a_service_is_never_authorised(self):
        fake, client, device, _path = self.pairing([("AuthorizeService", HEADSET_UUID)])
        client.pair(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(fake.asked[0][1].error, REJECTED)
        self.assertEqual(client.snapshot.failure.code, bt.AUTH_FAILED)
        self.assertNotIn(bt.CONFIRM, [snapshot.prompt.kind for snapshot in client.changes.seen if snapshot.prompt])

    def test_bluez_taking_the_question_back(self):
        fake, client, device, path = self.pairing([("RequestConfirmation", 1)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        prompt = client.snapshot.prompt
        cancel = fake.ask("Cancel")
        self.until(lambda: cancel.done and client.snapshot.prompt is None)
        self.until(lambda: fake.asked[0][1].done)
        self.assertEqual(fake.asked[0][1].error, CANCELED)                     # the question, answered once
        client.answer(prompt.id, True)                                         # too late: nothing is sent
        self.assertEqual(client.snapshot.activity.stage, bt.STAGE_PAIR)        # Pair() is still to answer
        fake.finish_pair(path, "org.bluez.Error.AuthenticationCanceled")       # the device gave up
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.AUTH_FAILED)         # not the user's cancel
        self.assertEqual(len(fake.asked), 2)

    def test_release_ends_the_agent_without_unregistering_it(self):
        fake, client, device, path = self.pairing([("RequestPasskey",)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        release = fake.ask("Release")
        self.until(lambda: release.done and client.snapshot.prompt is None)
        late = fake.ask("RequestPinCode")
        self.until(lambda: late.done)
        self.assertEqual(late.error, REJECTED)                                 # a released agent answers nothing
        fake.finish_pair(path)
        self.until(lambda: client.snapshot.activity is None and client.idle)
        self.assertIsNone(client.snapshot.failure)
        self.assertEqual(self.sent(fake, client, "UnregisterAgent"), [])       # BlueZ dropped it already
        self.assertEqual(self.sent(fake, client)[-2:], ["Set", "Connect"])

    def test_nobody_but_bluez_may_use_the_agent_and_only_about_this_device(self):
        fake, client, device, path = self.pairing([WAIT])
        fake.add_device(BUDS, name="Buds")
        client.pair(device)
        self.until(lambda: fake.pairings)
        name, agent = self.agent(fake, client)
        hostile = self.bus.connect()
        calls = [call_agent(hostile, name, agent, "RequestConfirmation", (path, 1234)),
                 call_agent(hostile, name, agent, "RequestAuthorization", (path,)),
                 call_agent(hostile, name, agent, "Cancel"),
                 call_agent(fake.connection, name, agent, "RequestConfirmation", (address_path(BUDS), 1234)),
                 call_agent(fake.connection, name, agent, "RequestAuthorization", (address_path(BUDS),)),
                 call_agent(fake.connection, name, agent, "AuthorizeService", (path, HEADSET_UUID))]
        self.until(lambda: all(record.done for record in calls))
        self.assertEqual({record.error for record in calls}, {REJECTED})
        self.assertIsNone(client.snapshot.prompt)
        first = fake.ask("RequestPinCode")
        self.until(lambda: client.snapshot.prompt is not None)
        second = fake.ask("RequestPasskey")
        self.until(lambda: second.done)
        self.assertEqual((second.error, first.done, client.snapshot.prompt.kind), (REJECTED, False, bt.PIN))
        client.answer(client.snapshot.prompt.id, "1234")
        self.until(lambda: first.done)
        self.assertEqual(first.value, ("1234",))
        fake.resume()
        self.until(lambda: client.snapshot.activity is None)

    def test_a_device_that_is_paired_already_is_trusted_and_connected(self):
        fake, client, device, _path = self.pairing()
        fake.failures["Pair"] = ("org.bluez.Error.AlreadyExists", "Already Paired")
        client.pair(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertIsNone(client.snapshot.failure)
        self.assertEqual(self.sent(fake, client)[-3:], ["UnregisterAgent", "Set", "Connect"])

    def test_paired_but_not_trusted(self):
        fake, client, device, _path = self.pairing()
        fake.failures["Set"] = ("org.bluez.Error.Failed", "nope")
        client.pair(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.FAILED, bt.PAIRING, bt.STAGE_TRUST, device, "Pad", paired=True))
        self.assertEqual(self.sent(fake, client, "Connect"), [])
        self.assertIn(device, [remembered.id for remembered in bt.remembered(client.snapshot.devices)])
        self.assert_agent_gone(fake, client)

    def test_paired_and_trusted_but_out_of_reach(self):
        fake, client, device, _path = self.pairing()
        fake.failures["Connect"] = ("org.bluez.Error.Failed", "br-connection-page-timeout")
        client.pair(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.UNREACHABLE, bt.PAIRING, bt.STAGE_CONNECT, device, "Pad", paired=True))
        self.assertTrue(self.find(client, "Pad").trusted)
        self.assert_agent_gone(fake, client)

    def test_cancelling_before_the_bond_cancels_the_pairing_once(self):
        fake, client, device, path = self.pairing([("RequestConfirmation", 77)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        self.assertFalse(client._devices[path].bonded)
        client.cancel()
        client.cancel()
        self.assertIsNone(client.snapshot.prompt)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(self.sent(fake, client, "CancelPairing"), ["CancelPairing"])
        self.assertEqual(fake.asked[0][1].error, CANCELED)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.CANCELED, bt.PAIRING, bt.STAGE_PAIR, device, "Pad"))
        self.assertEqual(self.sent(fake, client, "Set") + self.sent(fake, client, "Connect"), [])
        self.assertEqual((fake.cancelled, fake.unpaired), ([path], []))   # BlueZ cancelled its own request
        self.assert_agent_gone(fake, client)

    def test_the_same_error_without_the_users_cancel_is_a_failed_authentication(self):
        fake, client, device, _path = self.pairing([(FAIL, "org.bluez.Error.AuthenticationCanceled")])
        client.pair(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.AUTH_FAILED)
        self.assert_agent_gone(fake, client)

    def test_cancelling_after_the_bond_keeps_it_and_waits_for_bluez(self):
        fake, client, device, path = self.pairing()
        fake.defer = lambda call: call[3] == "Pair"
        client.pair(device)
        self.until(lambda: fake.deferred and client._devices[path].bonded)    # BlueZ looks up services: Pair() waits
        self.assertFalse(self.find(client, "Pad").paired)                     # Paired comes just before the answer
        client.cancel()
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(self.sent(fake, client, "CancelPairing"), [])        # it would remove the new keys
        self.assertEqual(client.snapshot.activity, bt.Activity(bt.PAIRING, device, "Pad", bt.STAGE_PAIR))
        fake.answer_deferred()
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.CANCELED, bt.PAIRING, bt.STAGE_PAIR, device, "Pad", paired=True))
        self.assertEqual(self.sent(fake, client, "Set") + self.sent(fake, client, "Connect"), [])
        self.assertTrue(self.find(client, "Pad").paired)
        self.assertEqual(fake.unpaired, [])
        self.assert_agent_gone(fake, client)

    def test_cancelling_after_the_bond_ends_when_bluez_says_paired(self):
        fake, client, device, path = self.pairing([BOND, WAIT])
        client.pair(device)
        self.until(lambda: client._devices[path].bonded)
        client.cancel()
        fake.set(path, DEVICE, Paired=True)                                    # announced before the answer
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.CANCELED, bt.PAIRING, bt.STAGE_PAIR, device, "Pad", paired=True))
        fake.resume()                                                          # the answer, after the end: ignored
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((self.sent(fake, client, "CancelPairing"), fake.unpaired), ([], []))
        self.assert_agent_gone(fake, client)

    def test_after_a_cancel_the_agent_answers_nothing_more(self):
        fake, client, device, path = self.pairing([BOND, WAIT])
        client.pair(device)
        self.until(lambda: client._devices[path].bonded)
        name = self.name_of(client)
        client.cancel()
        self.until(lambda: name not in fake.agents)                            # unregistered at once
        late = fake.ask("RequestConfirmation", 123456)                         # the device starts again
        self.until(lambda: late.done)
        self.assertEqual(late.error, "org.freedesktop.DBus.Error.UnknownMethod")
        self.assertIsNone(client.snapshot.prompt)
        self.assertEqual(client.snapshot.activity.kind, bt.PAIRING)            # Pair() is still to answer
        fake.resume()
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.CANCELED)
        self.assert_agent_gone(fake, client)

    def test_cancelling_after_the_bond_waits_for_bluez_at_most_a_while(self):
        fake, client, device, path = self.pairing([BOND, WAIT])
        client.pair(device)
        self.until(lambda: client._devices[path].bonded)
        client.cancel()
        self.assertTrue(self.clock.armed(bluez.CALL_TIMEOUT_MS))
        self.clock.fire_after(bluez.CALL_TIMEOUT_MS)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.CANCELED, bt.PAIRING, bt.STAGE_PAIR, device, "Pad", paired=True))
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((self.sent(fake, client, "CancelPairing"), fake.unpaired), ([], []))
        self.assert_agent_gone(fake, client)

    def test_a_pairing_that_times_out_after_the_bond_keeps_it(self):
        fake, client, device, path = self.pairing([BOND, WAIT])
        client.pair(device)
        self.until(lambda: client._devices[path].bonded)
        self.clock.fire_after(bluez.PAIR_TIMEOUT_MS)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.TIMEOUT, bt.PAIRING, bt.STAGE_PAIR, device, "Pad", paired=True))
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual((self.sent(fake, client, "CancelPairing"), fake.unpaired), ([], []))
        fake.resume()                                                          # BlueZ finishes: still paired
        self.until(lambda: self.find(client, "Pad").paired)
        self.assertEqual(client.snapshot.failure.code, bt.TIMEOUT)
        self.assert_agent_gone(fake, client)

    def test_cancelling_while_the_agent_registers_sends_no_pair(self):
        fake, client, device, _path = self.pairing()
        fake.hold = lambda call: call[3] == "RegisterAgent"         # a slow bluetoothd: BlueZ answers at once
        client.pair(device)
        self.until(lambda: fake.held)
        client.cancel()
        self.assertEqual(client.snapshot.failure.code, bt.CANCELED)
        fake.hold = None
        fake.answer_held()
        self.assert_agent_gone(fake, client)
        self.assertEqual(self.sent(fake, client, "Pair"), [])

    def test_cancelling_while_connecting_after_pairing_abandons_the_connection(self):
        fake, client, device, _path = self.pairing()
        fake.defer = lambda call: call[3] == "Connect"
        client.pair(device)
        self.until(lambda: fake.deferred)
        self.assertEqual(client.snapshot.activity.stage, bt.STAGE_CONNECT)
        client.cancel()                                 # Disconnect: BlueZ answers the Connect br-connection-canceled
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(self.sent(fake, client, "Disconnect"), ["Disconnect"])
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.CANCELED, bt.PAIRING, bt.STAGE_CONNECT, device, "Pad", paired=True))

    def test_a_pairing_that_takes_too_long_is_cancelled(self):
        fake, client, device, _path = self.pairing([WAIT])
        client.pair(device)
        self.until(lambda: fake.pairings)
        self.assertEqual(self.clock.delays_pending(), [bluez.PAIR_TIMEOUT_MS])
        self.clock.fire_after(bluez.PAIR_TIMEOUT_MS)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.TIMEOUT, bt.PAIRING, bt.STAGE_PAIR, device, "Pad"))
        self.until(lambda: not fake.pairings)
        self.assertEqual(self.sent(fake, client, "CancelPairing"), ["CancelPairing"])
        self.assertEqual(fake.unpaired, [])             # before the bond: the pairing request was cancelled
        self.assert_agent_gone(fake, client)

    def test_a_trust_or_connect_step_that_takes_too_long(self):
        for member, stage, timeout, after in (("Set", bt.STAGE_TRUST, bluez.CALL_TIMEOUT_MS, []),
                                              ("Connect", bt.STAGE_CONNECT, bluez.CONNECT_TIMEOUT_MS, ["Disconnect"])):
            with self.subTest(member):
                fake, client, device, _path = self.pairing()
                if member == "Set":                     # a slow bluetoothd: BlueZ answers Trusted at once
                    fake.hold = lambda call: call[3] == "Set"
                else:
                    fake.defer = lambda call: call[3] == "Connect"
                client.pair(device)
                self.until(lambda: fake.held or fake.deferred)
                self.clock.fire_after(timeout)
                self.assertEqual(client.snapshot.failure,
                                 bt.Failure(bt.TIMEOUT, bt.PAIRING, stage, device, "Pad", paired=True))
                self.until(lambda: self.sent(fake, client)[-1] == (after or [member])[-1])
                client.stop()
                fake.close()

    def test_pairing_needs_an_unpaired_device_that_exists_and_an_adapter_that_is_on(self):
        fake, client, device, path = self.pairing()
        client.pair(device + 100)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.GONE, bt.PAIRING, "", device + 100))
        fake.add_device(BUDS, name="Buds", paired=True)
        self.until(lambda: len(client.snapshot.devices) == 2)
        failure = client.snapshot.failure
        client.pair(self.find(client, "Buds").id)                              # remembered already: nothing
        self.assertIs(client.snapshot.failure, failure)
        fake.set("/org/bluez/hci0", ADAPTER, Powered=False)
        self.until(lambda: client.snapshot.state == bt.OFF)
        client.pair(device)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.NOT_READY, bt.PAIRING, "", device, "Pad"))
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects"])
        self.assertIn(path, fake.objects)


class AgentExitTest(PairingCase):
    """However a pairing ends, its agent is neither registered nor exported afterwards."""

    def test_the_device_going_away(self):
        # BlueZ answers Pair() before it says that the device is gone, so its answer is what the failure says:
        # AuthenticationCanceled while the link is up, ConnectionAttemptFailed while it is still being paged.
        for link, code in ((True, bt.AUTH_FAILED), (False, bt.UNREACHABLE)):
            with self.subTest(link=link):
                fake, client, device, path = self.pairing([WAIT])
                fake.link_on_pair = link
                client.pair(device)
                self.until(lambda: fake.pairings)
                fake.remove_device(path)
                self.until(lambda: client.snapshot.activity is None and not client.snapshot.devices)
                self.assertEqual(client.snapshot.failure, bt.Failure(code, bt.PAIRING, bt.STAGE_PAIR, device, "Pad"))
                self.assert_agent_gone(fake, client)
                client.stop()
                fake.close()

    def test_the_device_going_away_while_a_call_waits_is_gone(self):
        fake, client, device, path = self.pairing()
        fake.defer = lambda call: call[3] == "Connect"
        client.pair(device)
        self.until(lambda: fake.deferred)
        fake.remove_device(path)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.GONE, bt.PAIRING, bt.STAGE_CONNECT, device, "Pad"))
        fake.answer_deferred()
        self.assert_agent_gone(fake, client)

    def test_the_adapter_going_away(self):
        fake, client, device, _path = self.pairing([("RequestConfirmation", 5)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        fake.remove_adapter(0)
        self.until(lambda: client.snapshot.state == bt.NO_ADAPTER)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.AUTH_FAILED, bt.PAIRING, bt.STAGE_PAIR, device, "Pad"))   # the link was up
        self.assertIsNone(client.snapshot.prompt)
        self.assert_agent_gone(fake, client)

    def test_the_adapter_going_away_after_the_bond_is_gone(self):
        fake, client, device, _path = self.pairing()
        fake.defer = lambda call: call[3] == "Pair"
        client.pair(device)
        self.until(lambda: fake.deferred)                     # bonded; BlueZ looks up the services, Pair() waits
        fake.remove_adapter(0)                                # BlueZ drops the waiting Pair() without an answer
        self.until(lambda: client.snapshot.state == bt.NO_ADAPTER)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.GONE, bt.PAIRING, bt.STAGE_PAIR, device, "Pad"))
        self.assert_agent_gone(fake, client)

    def test_bluez_going_away(self):
        fake, client, device, _path = self.pairing([("RequestConfirmation", 5)])
        client.pair(device)
        self.until(lambda: client.snapshot.prompt is not None)
        name, agent = self.agent(fake, client)
        fake.close()
        self.until(lambda: client.snapshot.state == bt.ABSENT)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.UNAVAILABLE, bt.PAIRING, bt.STAGE_PAIR, device, "Pad"))
        self.assertIsNone(client.snapshot.prompt)
        self.assertEqual(self.sent(fake, client, "UnregisterAgent"), [])         # nobody to unregister with
        record = call_agent(self.bus.connect(), name, agent, "RequestAuthorization", (address_path(PAD),))
        self.until(lambda: record.done)
        self.assertEqual(record.error, "org.freedesktop.DBus.Error.UnknownMethod")
        successor = self.fake()
        self.until(lambda: client.snapshot.state == bt.ON)
        self.assertEqual(successor.agents, {})

    def test_a_pairing_that_fails(self):
        fake, client, device, _path = self.pairing([(FAIL, "org.bluez.Error.ConnectionAttemptFailed")])
        client.pair(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.UNREACHABLE)
        self.assert_agent_gone(fake, client)

    def test_an_agent_that_bluez_refuses(self):
        fake, client, device, _path = self.pairing()
        fake.failures["RegisterAgent"] = ("org.bluez.Error.AlreadyExists", "Already Exists")
        client.pair(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.FAILED)
        self.assertEqual(self.sent(fake, client, "Pair"), [])
        self.assert_agent_gone(fake, client)


class DeviceTest(BusCase):
    def remembered(self, **device) -> tuple[FakeBlueZ, bluez.BlueZ, int, str]:
        fake = self.fake()
        path = fake.add_device(BUDS, name="Buds", icon="audio-headset", paired=True, signal=False, **device)
        client = self.ready()
        return fake, client, self.find(client, "Buds").id, path

    def test_connect_and_disconnect_never_change_trust(self):
        fake, client, device, _path = self.remembered()
        with self.timeouts() as timeouts:
            client.connect(device)
            self.assertEqual(client.snapshot.activity, bt.Activity(bt.CONNECTING, device, "Buds"))
            self.until(lambda: client.snapshot.activity is None and self.find(client, "Buds").connected)
            client.disconnect(device)
            self.assertEqual(client.snapshot.activity, bt.Activity(bt.DISCONNECTING, device, "Buds"))
            self.until(lambda: client.snapshot.activity is None and not self.find(client, "Buds").connected)
        self.assertEqual(timeouts, [("Connect", bluez.CONNECT_TIMEOUT_MS), ("Disconnect", bluez.CALL_TIMEOUT_MS)])
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects", "Connect", "Disconnect"])
        self.assertFalse(self.find(client, "Buds").trusted)
        self.assertIsNone(client.snapshot.failure)
        self.assertTrue(client.idle)

    def test_connected_already_and_not_connected_are_what_was_asked(self):
        fake, client, device, _path = self.remembered(connected=True)
        client.connect(device)                          # BlueZ 5.87 answers success
        self.until(lambda: client.snapshot.activity is None)
        fake.set(address_path(BUDS), DEVICE, Connected=False)
        client.disconnect(device)                       # BlueZ 5.87 answers success here too
        self.until(lambda: client.snapshot.activity is None)
        self.assertIsNone(client.snapshot.failure)
        # The errors BlueZ documents for these cases, which 5.87 does not send, mean the same.
        fake.failures["Connect"] = ("org.bluez.Error.AlreadyConnected", "Already Connected")
        fake.failures["Disconnect"] = ("org.bluez.Error.NotConnected", "Not Connected")
        client.connect(device)
        self.until(lambda: client.snapshot.activity is None)
        client.disconnect(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertIsNone(client.snapshot.failure)
        self.assertEqual(self.sent(fake, client)[1:], ["Connect", "Disconnect", "Connect", "Disconnect"])

    def test_a_connection_that_fails_says_why_without_its_text(self):
        fake, client, device, _path = self.remembered()
        for reason, code in (("br-connection-page-timeout", bt.UNREACHABLE), ("le-connection-abort-by-local",
                                                                              bt.UNREACHABLE),
                             ("br-connection-key-missing", bt.AUTH_FAILED)):
            fake.failures["Connect"] = ("org.bluez.Error.Failed", reason)
            client.connect(device)
            self.until(lambda: client.snapshot.activity is None)
            self.assertEqual(client.snapshot.failure, bt.Failure(code, bt.CONNECTING, "", device, "Buds", paired=True))
            self.assertNotIn(reason, repr(client.snapshot))
        fake.failures["Connect"] = ("org.bluez.Error.BREDR.ProfileUnavailable", "Exhausted the list")
        client.connect(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.UNSUPPORTED)
        client.dismiss()
        self.assertIsNone(client.snapshot.failure)

    def test_a_connection_that_takes_too_long_is_abandoned(self):
        fake, client, device, _path = self.remembered()
        fake.defer = lambda call: call[3] == "Connect"
        client.connect(device)
        self.until(lambda: fake.deferred)
        self.clock.fire_after(bluez.CONNECT_TIMEOUT_MS)
        self.assertEqual(client.snapshot.failure,
                         bt.Failure(bt.TIMEOUT, bt.CONNECTING, "", device, "Buds", paired=True))
        self.until(lambda: self.sent(fake, client, "Disconnect"))
        fake.answer_deferred()                                                 # answered by the Disconnect already
        self.until(lambda: client.idle)
        self.assertEqual(client.snapshot.failure.code, bt.TIMEOUT)

    def test_cancelling_a_connection_abandons_it(self):
        fake, client, device, _path = self.remembered()
        fake.defer = lambda call: call[3] == "Connect"
        client.connect(device)
        self.until(lambda: fake.deferred)
        client.cancel()
        client.cancel()
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.CANCELED)
        self.assertEqual(self.sent(fake, client, "Disconnect"), ["Disconnect"])

    def test_only_one_thing_at_a_time(self):
        fake, client, device, _path = self.remembered()
        fake.add_device(PAD, name="Pad")
        self.until(lambda: len(client.snapshot.devices) == 2)
        fake.defer = lambda call: call[3] == "Connect"
        client.connect(device)
        for action in (lambda: client.connect(device), lambda: client.disconnect(device),
                       lambda: client.forget(device), lambda: client.pair(self.find(client, "Pad").id),
                       lambda: client.set_power(False)):
            action()
        self.assertEqual(client.snapshot.activity.kind, bt.CONNECTING)
        self.until(lambda: fake.deferred)
        self.assertEqual(self.sent(fake, client)[1:], ["Connect"])

    def test_unknown_ids_and_unremembered_devices(self):
        fake, client, device, _path = self.remembered()
        fake.add_device(PAD, name="Pad")
        self.until(lambda: len(client.snapshot.devices) == 2)
        for action, kind in ((client.connect, bt.CONNECTING), (client.disconnect, bt.DISCONNECTING),
                             (client.forget, bt.FORGETTING)):
            action(device + 1000)
            self.assertEqual(client.snapshot.failure, bt.Failure(bt.GONE, kind, "", device + 1000))
            client.dismiss()
            action(self.find(client, "Pad").id)                                 # not remembered: nothing happens
            self.assertIsNone(client.snapshot.failure)
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects"])

    def test_forgetting_removes_the_device_directly(self):
        fake, client, device, path = self.remembered(connected=True)
        client.forget(device)
        self.until(lambda: client.snapshot.activity is None and not client.snapshot.devices)
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects", "RemoveDevice"])
        self.assertEqual(fake.last("RemoveDevice")[1:], ("/org/bluez/hci0", ADAPTER, "RemoveDevice", (path,)))
        self.assertIsNone(client.snapshot.failure)

    def test_forgetting_while_the_adapter_is_off_is_refused_without_asking(self):
        fake, client, device, _path = self.remembered()
        fake.set("/org/bluez/hci0", ADAPTER, Powered=False)
        self.until(lambda: client.snapshot.state == bt.OFF)
        client.forget(device)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.NOT_READY, bt.FORGETTING, "", device, "Buds", True))
        self.assertIsNone(client.snapshot.activity)
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects"])
        self.assertEqual([device.name for device in client.snapshot.devices], ["Buds"])   # listed while off

    def test_forgetting_a_device_bluez_no_longer_knows(self):
        fake, client, device, _path = self.remembered()
        fake.failures["RemoveDevice"] = ("org.bluez.Error.DoesNotExist", "Does Not Exist")
        client.forget(device)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure.code, bt.GONE)


class PowerTest(BusCase):
    HCI0 = "/org/bluez/hci0"

    def setUp(self):
        super().setUp()
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)

    def command(self, body: str) -> Path:
        """A stand-in for omarchy-bluetooth-power. It notes its arguments and exact environment first."""
        path = SCRATCH / "power"
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json, os, pathlib, sys, time
            here = pathlib.Path(__file__).parent
            environment = [item.split("=", 1)[0] for item in
                           pathlib.Path("/proc/self/environ").read_bytes().decode().split("\\0") if item]
            (here / "seen.json").write_text(json.dumps({"argv": sys.argv[1:], "environment": environment,
                                                       "pid": os.getpid()}))
        """) + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def waiting_command(self, status=0) -> Path:
        return self.command(f"""\
            deadline = time.monotonic() + 5
            while not (here / "go").exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            sys.exit({status})
        """)

    def seen(self) -> dict:
        return json.loads((SCRATCH / "seen.json").read_text(encoding="utf-8"))

    def test_switching_off_through_omarchys_command(self):
        fake = self.fake()
        command = self.waiting_command()
        client = self.ready(find_power_command=lambda: str(command))
        client.set_power(False)
        self.assertEqual(client.snapshot.activity, bt.Activity(bt.POWERING))
        self.assertFalse(client.idle)
        self.until(lambda: (SCRATCH / "seen.json").exists())
        self.assertEqual(self.seen()["argv"], ["off"])
        self.assertLessEqual(set(self.seen()["environment"]), set(bluez.POWER_ENVIRONMENT))
        fake.set(self.HCI0, ADAPTER, Powered=False, PowerState="off-blocked")    # what rfkill does
        self.until(lambda: client.snapshot.state == bt.OFF)
        self.assertIsNotNone(client.snapshot.activity)                           # until the command is done
        (SCRATCH / "go").touch()
        self.until(lambda: client.snapshot.activity is None)
        self.assertIsNone(client.snapshot.failure)
        self.assertEqual(self.sent(fake, client, "Set"), [])
        self.assertTrue(client.idle)

    def test_switching_on_through_omarchys_command(self):
        fake = self.fake(adapters=())
        fake.add_adapter(0, powered=False, power_state="off-blocked", signal=False)
        command = self.waiting_command()
        client = self.ready(bt.OFF, find_power_command=lambda: str(command))
        client.set_power(True)
        self.until(lambda: (SCRATCH / "seen.json").exists())
        self.assertEqual(self.seen()["argv"], ["on"])
        fake.set(self.HCI0, ADAPTER, Powered=True, PowerState="on")
        (SCRATCH / "go").touch()
        self.until(lambda: client.snapshot.activity is None and client.snapshot.state == bt.ON)
        self.assertIsNone(client.snapshot.failure)
        self.assertEqual(self.sent(fake, client, "Set"), [])

    def test_when_the_adapter_did_not_follow_the_command_it_is_switched_directly(self):
        fake = self.fake()
        command = self.command("sys.exit(0)\n")
        client = self.ready(find_power_command=lambda: str(command))
        client.set_power(False)
        self.until(lambda: client.snapshot.activity is None)
        self.assertIsNone(client.snapshot.failure)
        self.assertEqual(client.snapshot.state, bt.OFF)
        self.assertEqual(fake.last("Set")[4], (ADAPTER, "Powered", False))

    def test_a_command_that_hangs_is_killed_and_the_adapter_is_looked_at(self):
        fake = self.fake()
        fake.powered_follows = False
        sleep = shutil.which("sleep")
        command = self.command(f'os.execv("{sleep}", ["sleep", "30"])\n')
        client = self.ready(find_power_command=lambda: str(command))
        client.set_power(False)
        self.until(lambda: (SCRATCH / "seen.json").exists())
        pid = self.seen()["pid"]
        self.assertEqual(self.clock.delays_pending(), [bluez.POWER_COMMAND_TIMEOUT_MS])
        self.clock.fire_after(bluez.POWER_COMMAND_TIMEOUT_MS)
        self.until(lambda: not Path(f"/proc/{pid}").exists())                   # killed, and reaped
        self.until(lambda: self.sent(fake, client, "Set"))                      # then written directly
        self.until(lambda: client.idle is False and self.clock.armed(bluez.CALL_TIMEOUT_MS))
        self.clock.fire_after(bluez.CALL_TIMEOUT_MS)                            # and it never followed
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.TIMEOUT, bt.POWERING))
        self.assertIsNone(client.snapshot.activity)

    def test_without_omarchys_command_powered_is_written_and_the_change_waited_for(self):
        fake = self.fake(powered=False)
        client = self.ready(bt.OFF)
        with self.timeouts() as timeouts:
            client.set_power(True)
            self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.state, bt.ON)
        self.assertEqual(timeouts, [("Set", bluez.CALL_TIMEOUT_MS)])
        self.assertEqual(fake.last("Set")[4], (ADAPTER, "Powered", True))
        client.set_power(True)                                                  # on already: nothing to do
        self.assertEqual(len(self.sent(fake, client, "Set")), 1)

    def test_a_radio_blocked_by_rfkill_is_blocked(self):
        fake = self.fake(adapters=())
        fake.add_adapter(0, powered=False, power_state="off-blocked", signal=False)
        client = self.ready(bt.OFF)
        client.set_power(True)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.BLOCKED, bt.POWERING))

    def test_a_write_that_is_answered_but_never_happens(self):
        fake = self.fake(powered=False)
        fake.powered_follows = False
        client = self.ready(bt.OFF)
        client.set_power(True)
        self.until(lambda: self.sent(fake, client, "Set") and client._op.step == bluez.VERIFY)
        self.clock.fire_after(bluez.CALL_TIMEOUT_MS)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.TIMEOUT, bt.POWERING))
        client.set_power(True)
        self.until(lambda: client._op is not None and client._op.step == bluez.VERIFY)
        fake.set(self.HCI0, ADAPTER, PowerState="off-blocked")                  # rfkill blocked it meanwhile
        self.until(lambda: client._selected().power_state == "off-blocked")
        self.clock.fire_after(bluez.CALL_TIMEOUT_MS)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.BLOCKED, bt.POWERING))

    def test_a_write_answered_busy_waits_for_the_change_under_way(self):
        fake = self.fake(powered=False)
        client = self.ready(bt.OFF)
        other = self.bus.connect()                        # bluetoothctl, still switching the adapter on
        fake.defer = lambda call: call[3] == "Set" and call[0] == other.get_unique_name()
        other.call(fake.unique_name, self.HCI0, PROPERTIES, "Set",
                   GLib.Variant("(ssv)", (ADAPTER, "Powered", GLib.Variant("b", True))), None,
                   Gio.DBusCallFlags.NONE, 3000, None, lambda *_args: None, None)
        self.until(lambda: fake.deferred)
        client.set_power(True)
        self.until(lambda: self.sent(fake, client, "Set") and client._op.step == bluez.VERIFY)
        self.assertIsNone(client.snapshot.failure)        # Busy: not a failure, a change to wait for
        fake.answer_deferred()
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual((client.snapshot.state, client.snapshot.failure), (bt.ON, None))

    def test_a_write_answered_busy_that_never_follows_times_out(self):
        fake = self.fake(powered=False)
        fake.failures["Set"] = ("org.bluez.Error.Busy", "")
        client = self.ready(bt.OFF)
        client.set_power(True)
        self.until(lambda: client._op is not None and client._op.step == bluez.VERIFY)
        self.clock.fire_after(bluez.CALL_TIMEOUT_MS)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.TIMEOUT, bt.POWERING))

    def test_a_write_refused_blocked_is_blocked_whatever_the_power_state_says(self):
        fake = self.fake(powered=False)
        fake.failures["Set"] = ("org.bluez.Error.Blocked", "Blocked through rfkill")
        client = self.ready(bt.OFF)
        client.set_power(True)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.BLOCKED, bt.POWERING))

    def test_a_write_that_fails_for_another_reason(self):
        fake = self.fake(powered=False)
        fake.failures["Set"] = ("org.bluez.Error.Failed", "something else")
        client = self.ready(bt.OFF)
        client.set_power(True)
        self.until(lambda: client.snapshot.activity is None)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.FAILED, bt.POWERING))

    def test_nothing_to_switch_without_an_adapter(self):
        fake = self.fake(adapters=())
        client = self.ready(bt.NO_ADAPTER)
        client.set_power(True)
        self.assertEqual(client.snapshot.failure, bt.Failure(bt.NOT_READY, bt.POWERING))
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects"])

    def test_stopping_while_the_command_runs_lets_it_finish_without_waiting_for_it(self):
        self.fake()
        command = self.waiting_command()
        client = self.ready(find_power_command=lambda: str(command))
        client.set_power(False)
        self.until(lambda: (SCRATCH / "seen.json").exists())
        pid = self.seen()["pid"]
        client.stop()
        self.assertTrue(client.idle)
        self.assertEqual(self.clock.pending, {})
        self.assertTrue(Path(f"/proc/{pid}").exists())                          # not killed
        (SCRATCH / "go").touch()
        self.until(lambda: not Path(f"/proc/{pid}").exists())
        self.assertIs(client.snapshot, bluez.IDLE)


class AllowListOnTheBusTest(BusCase):
    def test_a_call_that_is_not_on_the_list_is_refused_before_it_is_sent(self):
        fake = self.fake()
        client = self.ready()
        for interface, member, parameters in (
                (bluez.ADAPTER, "SetDiscoveryFilter", GLib.Variant("(a{sv})", ({},))),
                (bluez.AGENT_MANAGER, "RequestDefaultAgent", GLib.Variant("(o)", ("/x",))),
                (bluez.PROPERTIES, "Set",
                 GLib.Variant("(ssv)", (bluez.ADAPTER, "Discoverable", GLib.Variant("b", True))))):
            with self.assertRaises(ValueError):
                client._call("/org/bluez/hci0", interface, member, parameters, None, client._ignore)
        self.assertFalse(pump(timeout=0.05))
        self.assertEqual(self.sent(fake, client), ["GetManagedObjects"])
        self.assertTrue(client.idle)


class NeverStartedTest(unittest.TestCase):
    """This private bus *can* start services, BlueZ's name among them, and a control proves that it does."""

    CONTROL = "org.example.Control"

    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.started, self.control = SCRATCH / "bluez-started", SCRATCH / "control-started"
        self.bus = PrivateBus.start(activatable_bus_config(SCRATCH, {bluez.BUS_NAME: self.started,
                                                                     self.CONTROL: self.control}))
        self.addCleanup(self.bus.stop)

    def test_watching_reading_and_every_request_never_start_bluez(self):
        self.bus.connect().call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "StartServiceByName", GLib.Variant("(su)", (self.CONTROL, 0)), None,
                                Gio.DBusCallFlags.NONE, 5000, None, lambda *_args: None, None)
        if not pump(self.control.exists, 3):
            self.skipTest("this bus cannot start services here")
        clock = Clock()
        client = bluez.BlueZ(lambda: None, address=self.bus.address, schedule=clock.schedule, cancel=clock.cancel,
                             find_power_command=lambda: None)
        self.addCleanup(client.stop)
        client.start()
        self.assertTrue(pump(lambda: client.snapshot.state == bt.ABSENT))
        for action in (lambda: client.scan(True), lambda: client.pair(1), lambda: client.connect(1),
                       lambda: client.set_power(True), client.retry, lambda: client.forget(1)):
            action()
        client.stop()
        client.start()
        self.assertTrue(pump(lambda: client.snapshot.state == bt.ABSENT))
        self.assertFalse(pump(self.started.exists, 0.4))
        self.assertFalse(self.started.exists())
        self.assertEqual(client.error, "")


class RuntimeTest(unittest.TestCase):
    def test_the_window_gets_a_bluez_source_that_holds_nothing_until_started(self):
        from gaminghome.application import Runtime
        source = Runtime.bluetooth(None, lambda: None)
        self.assertIsInstance(source, bluez.BlueZ)
        self.assertIs(source.snapshot, bluez.IDLE)
        self.assertTrue(source.idle)
        self.assertIsNone(source._connection)


if __name__ == "__main__":
    unittest.main()
