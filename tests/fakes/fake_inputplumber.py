"""In-process fake of InputPlumber's D-Bus surface, on a private bus.

The interface descriptions in ``tests/fixtures/inputplumber`` were captured
from InputPlumber 0.82.0 and drive what the fake exposes. The fake answers
``Introspect`` and the ``org.freedesktop.DBus.Properties`` calls itself
instead of leaving them to GDBus, so *every* method call that reaches it is
recorded and can be held back, failed or answered late.

Tests that use it need ``dbus-daemon`` and PyGObject; ``PrivateBus.start``
raises ``unittest.SkipTest`` when either is missing. The bus listens on an
abstract socket with no service directories: nothing touches the user's
buses, nothing can be activated and no file is created.
"""

from __future__ import annotations

import os
import re
import secrets
import select
import shutil
import subprocess
import unittest
from pathlib import Path

try:
    from gi.repository import Gio, GLib
except (ImportError, ValueError):  # pragma: no cover - PyGObject is a runtime dependency
    Gio = GLib = None

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "inputplumber"
BUS_NAME = "org.shadowblip.InputPlumber"
ROOT = "/org/shadowblip/InputPlumber"
MANAGER = f"{ROOT}/Manager"
MANAGER_IFACE = "org.shadowblip.InputManager"
COMPOSITE_IFACE = "org.shadowblip.Input.CompositeDevice"
TARGET_IFACE = "org.shadowblip.Input.Target"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
INTROSPECTABLE_IFACE = "org.freedesktop.DBus.Introspectable"
READS = frozenset({(INTROSPECTABLE_IFACE, "Introspect"), (PROPERTIES_IFACE, "Get"), (PROPERTIES_IFACE, "GetAll")})
DEFAULT_PROFILE = "/usr/share/inputplumber/profiles/default.yaml"

# Target type -> (object class used in the path, fixture, extra interface, display name).
# The display names are the ones the daemon reports over D-Bus; they are not
# the kernel names of the virtual devices.
TARGET_TYPES = {
    "xbox-elite": ("gamepad", "gamepad0.xml", "org.shadowblip.Input.Gamepad", "Microsoft X-Box One Elite pad"),
    "xbox-series": ("gamepad", "gamepad0.xml", "org.shadowblip.Input.Gamepad", "Microsoft Xbox Series S|X Controller"),
    "xb360": ("gamepad", "gamepad0.xml", "org.shadowblip.Input.Gamepad", "Microsoft X-Box 360 pad"),
    "gamepad": ("gamepad", "gamepad0.xml", "org.shadowblip.Input.Gamepad", "InputPlumber Gamepad"),
    "deck-uhid": ("gamepad", "gamepad0.xml", "org.shadowblip.Input.Gamepad", "Valve Steam Deck Controller"),
    "ds5": ("gamepad", "gamepad0.xml", "org.shadowblip.Input.Gamepad",
            "Sony Interactive Entertainment DualSense Wireless Controller"),
    "mouse": ("mouse", "mouse0.xml", "org.shadowblip.Input.Mouse", "InputPlumber Mouse"),
    "keyboard": ("keyboard", "keyboard0.xml", "org.shadowblip.Input.Keyboard", "InputPlumber Keyboard"),
    "dbus": ("dbus", "dbus0.xml", "org.shadowblip.Input.DBusDevice", "DBus Device"),
}

# Source capabilities and source nodes as reported for an ASUS ROG Xbox Ally
# (RC73YA) running on evdev sources only: there is no right stick button.
RC73YA_CAPABILITIES = (
    "Gamepad:Button:DPadUp", "Gamepad:Button:DPadDown", "Gamepad:Button:DPadLeft", "Gamepad:Button:DPadRight",
    "Gamepad:Button:South", "Gamepad:Button:East", "Gamepad:Button:North", "Gamepad:Button:West",
    "Gamepad:Button:LeftBumper", "Gamepad:Button:RightBumper", "Gamepad:Button:Select", "Gamepad:Button:Start",
    "Gamepad:Button:Guide", "Gamepad:Button:LeftStick", "Gamepad:Button:LeftPaddle1", "Gamepad:Button:LeftPaddle2",
    "Gamepad:Button:RightPaddle1", "Gamepad:Button:Keyboard", "Gamepad:Button:QuickAccess",
    "Gamepad:Button:QuickAccess2", "Gamepad:Axis:LeftStick", "Gamepad:Axis:RightStick",
    "Gamepad:Trigger:LeftTrigger", "Gamepad:Trigger:RightTrigger", "Gyroscope:Center", "Accelerometer:Center",
)
RC73YA_SOURCES = ("/dev/iio:device0", "/dev/input/event5", "/dev/input/event4", "/dev/input/event3",
                  "/dev/input/event6")
ROOT_BODY = ('<interface name="org.freedesktop.DBus.Introspectable"><method name="Introspect">'
             '<arg type="s" direction="out"/></method></interface>'
             '<interface name="org.freedesktop.DBus.ObjectManager"/>')


class PrivateBus:
    """A throwaway ``dbus-daemon`` for one test class."""

    def __init__(self, process, address):
        self.process, self.address = process, address
        self.connections = []

    @classmethod
    def start(cls) -> "PrivateBus":
        if Gio is None:
            raise unittest.SkipTest("PyGObject is not available")
        daemon = shutil.which("dbus-daemon")
        if daemon is None:
            raise unittest.SkipTest("dbus-daemon is not installed")
        socket_name = f"omarchy-game-mode-test-{os.getpid()}-{secrets.token_hex(6)}"
        argv = [daemon, f"--config-file={FIXTURES / 'bus.conf'}", f"--address=unix:abstract={socket_name}",
                "--nofork", "--print-address=1"]
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as error:
            raise unittest.SkipTest(f"dbus-daemon did not start: {error}") from None
        ready, _, _ = select.select([process.stdout], [], [], 5)
        address = process.stdout.readline().strip() if ready else ""
        if not address:
            process.kill()
            process.wait(5)
            raise unittest.SkipTest("dbus-daemon did not provide an address")
        return cls(process, address)

    def connect(self):
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        connection = Gio.DBusConnection.new_for_address_sync(self.address, flags, None, None)
        self.connections.append(connection)
        return connection

    def close_connections(self):
        connections, self.connections = self.connections, []
        for connection in connections:
            if not connection.is_closed():
                try:
                    connection.close_sync(None)
                except GLib.Error:
                    pass

    def stop(self):
        self.close_connections()
        self.process.terminate()
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(5)
        self.process.stdout.close()


class FakeInputPlumber:
    """One fake daemon instance: its own connection, objects and call log."""

    def __init__(self, bus: PrivateBus, version="0.82.0"):
        self.connection = bus.connect()
        self.unique_name = self.connection.get_unique_name()
        self.calls: list[tuple] = []       # (sender, path, interface, member, arguments)
        self.hold = None                   # predicate(call) -> True keeps the reply back
        self.held: list = []               # callables that deliver a kept-back reply
        self.failures: dict = {}           # (path, member) -> (error name, message)
        self.root_document: str | None = None
        self.decoy = True
        self._fixtures = {name: (FIXTURES / name).read_text(encoding="utf-8")
                          for name in ("manager.xml", "composite0.xml", "gamepad0.xml", "mouse0.xml",
                                       "keyboard0.xml", "dbus0.xml")}
        self._interfaces = {name: {info.name: info for info in Gio.DBusNodeInfo.new_for_xml(text).interfaces}
                            for name, text in self._fixtures.items()}
        self._objects: dict[str, dict] = {}
        self._export(ROOT, "manager.xml", {}, only=(INTROSPECTABLE_IFACE,), body=ROOT_BODY)
        self._export(MANAGER, "manager.xml", {MANAGER_IFACE: {
            "Version": GLib.Variant("s", version),
            "GamepadOrder": GLib.Variant("as", []),
            "ManageAllDevices": GLib.Variant("b", False),
            "SupportedTargetDeviceIds": GLib.Variant("as", sorted(TARGET_TYPES)),
            "SupportedTargetDevices": GLib.Variant("as", [TARGET_TYPES[kind][3] for kind in sorted(TARGET_TYPES)]),
        }})

    # ---- bus name ----------------------------------------------------------
    def _bus_call(self, method, signature, *arguments):
        return self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                         method, GLib.Variant(signature, arguments), None, Gio.DBusCallFlags.NONE,
                                         3000, None)

    def own(self) -> bool:
        """Take the well-known name without queueing; True when this fake now owns it."""
        return self._bus_call("RequestName", "(su)", BUS_NAME, 4).unpack()[0] == 1

    def release(self):
        self._bus_call("ReleaseName", "(s)", BUS_NAME)

    def close(self):
        if not self.connection.is_closed():
            self.connection.close_sync(None)

    # ---- objects -----------------------------------------------------------
    def _export(self, path, fixture, properties, only=None, body=None):
        registrations = []
        for name, info in self._interfaces[fixture].items():
            if name == "org.freedesktop.DBus.Peer" or (only is not None and name not in only):
                continue
            registrations.append(self.connection.register_object(path, info, self._on_call, None, None))
        if body is None:
            body = re.search(r"<node>(.*)</node>", self._fixtures[fixture], re.S).group(1)
        self._objects[path] = {"body": body, "properties": properties, "registrations": registrations}

    def _unexport(self, path):
        for registration in self._objects.pop(path)["registrations"]:
            self.connection.unregister_object(registration)

    def _free_path(self, prefix):
        index = 0
        while f"{prefix}{index}" in self._objects:
            index += 1
        return f"{prefix}{index}"

    def _add_target(self, kind):
        object_class, fixture, interface, display = TARGET_TYPES[kind]
        path = self._free_path(f"{ROOT}/devices/target/{object_class}")
        self._export(path, fixture, {
            TARGET_IFACE: {"Name": GLib.Variant("s", display), "DeviceType": GLib.Variant("s", kind)},
            interface: {"Name": GLib.Variant("s", display)},
        })
        return path

    def add_composite(self, index=None, name="ASUS ROG Xbox Ally", targets=("mouse", "xbox-elite", "keyboard"),
                      dbus=True, sources=RC73YA_SOURCES, capabilities=RC73YA_CAPABILITIES, profile="Default",
                      profile_path=DEFAULT_PROFILE, persistent_id="ASUSTeK_Computer_Inc._N-KEY_Device",
                      order=True) -> str:
        """Create a composite device the way the daemon would; returns its object path."""
        path = self._free_path(f"{ROOT}/CompositeDevice") if index is None else f"{ROOT}/CompositeDevice{index}"
        target_paths = [self._add_target(kind) for kind in targets]
        dbus_paths = [self._add_target("dbus")] if dbus else []
        self._export(path, "composite0.xml", {COMPOSITE_IFACE: {
            "Name": GLib.Variant("s", name),
            "ProfileName": GLib.Variant("s", profile),
            "ProfilePath": GLib.Variant("s", profile_path),
            "PersistentId": GLib.Variant("s", persistent_id),
            "InterceptMode": GLib.Variant("u", 0),
            "TargetDevices": GLib.Variant("as", target_paths),
            "DbusDevices": GLib.Variant("as", dbus_paths),
            "SourceDevicePaths": GLib.Variant("as", list(sources)),
            "Capabilities": GLib.Variant("as", list(capabilities)),
            "TargetCapabilities": GLib.Variant("as", ["Gamepad:Button:South", "Gamepad:Button:RightStick"]),
            "OutputCapabilities": GLib.Variant("as", []),
            "FilterableEvents": GLib.Variant("a{sas}", {}),
            "FilteredEvents": GLib.Variant("a{sas}", {}),
        }})
        if order:
            self.set_gamepad_order([*self.gamepad_order(), path])
        return path

    def remove_composite(self, path, order=True):
        properties = self._objects[path]["properties"][COMPOSITE_IFACE]
        for target in [*properties["TargetDevices"].unpack(), *properties["DbusDevices"].unpack()]:
            if target in self._objects:
                self._unexport(target)
        self._unexport(path)
        if order:
            self.set_gamepad_order([entry for entry in self.gamepad_order() if entry != path])

    def composite(self, path) -> dict:
        return self._objects[path]["properties"][COMPOSITE_IFACE]

    def targets(self, composite) -> list[str]:
        return list(self.composite(composite)["TargetDevices"].unpack())

    def target(self, path) -> dict:
        return self._objects[path]["properties"][TARGET_IFACE]

    def attach_target(self, composite, kind) -> str:
        """Add a target. Like the daemon, this announces nothing over D-Bus."""
        path = self._add_target(kind)
        self.composite(composite)["TargetDevices"] = GLib.Variant("as", [*self.targets(composite), path])
        return path

    def detach_target(self, composite, target):
        self.composite(composite)["TargetDevices"] = GLib.Variant(
            "as", [entry for entry in self.targets(composite) if entry != target])
        self._unexport(target)

    def gamepad_order(self) -> list[str]:
        return list(self._objects[MANAGER]["properties"][MANAGER_IFACE]["GamepadOrder"].unpack())

    def set_gamepad_order(self, order):
        self.set_property(MANAGER, MANAGER_IFACE, "GamepadOrder", GLib.Variant("as", list(order)))

    def set_property(self, path, interface, name, value, signal=True):
        self._objects[path]["properties"][interface][name] = value
        if signal:
            self.emit_changed(path, interface, {name: value})

    def emit_changed(self, path, interface, changed):
        self.connection.emit_signal(None, path, PROPERTIES_IFACE, "PropertiesChanged",
                                    GLib.Variant("(sa{sv}as)", (interface, dict(changed), [])))

    # ---- introspection the way zbus answers: the whole tree, nested -------
    def _tree(self, path) -> str:
        prefix = f"{path}/"
        children = {other[len(prefix):].split("/", 1)[0] for other in self._objects if other.startswith(prefix)}
        if path == ROOT:
            children.add("devices")
        elif path == f"{ROOT}/devices":
            children |= {"source", "target"}
        body = self._objects[path]["body"] if path in self._objects else ""
        if path == f"{ROOT}/devices/source" and self.decoy:
            # A nested node with a composite-like name must never be taken for a composite.
            body += '<node name="event6"/><node name="CompositeDevice99"/>'
        return body + "".join(f'<node name="{child}">{self._tree(prefix + child)}</node>' for child in sorted(children))

    def introspect(self, path) -> str:
        if path == ROOT and self.root_document is not None:
            return self.root_document
        return f"<node>{self._tree(path)}</node>"

    # ---- calls -------------------------------------------------------------
    def _on_call(self, _connection, sender, path, interface, member, parameters, invocation):
        call = (sender, path, interface, member, parameters.unpack())
        self.calls.append(call)
        if self.hold is not None and self.hold(call):
            self.held.append(lambda: self._answer(call, invocation))
            return
        self._answer(call, invocation)

    def _answer(self, call, invocation):
        _sender, path, interface, member, arguments = call
        if (path, member) in self.failures:
            invocation.return_dbus_error(*self.failures[(path, member)])
            return
        entry = self._objects.get(path)
        if entry is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownObject", f"Unknown object '{path}'")
        elif (interface, member) == (INTROSPECTABLE_IFACE, "Introspect"):
            invocation.return_value(GLib.Variant("(s)", (self.introspect(path),)))
        elif (interface, member) == (PROPERTIES_IFACE, "GetAll"):
            properties = entry["properties"].get(arguments[0])
            if properties is None:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownInterface", arguments[0])
            else:
                invocation.return_value(GLib.Variant("(a{sv})", (dict(properties),)))
        elif (interface, member) == (PROPERTIES_IFACE, "Get"):
            value = entry["properties"].get(arguments[0], {}).get(arguments[1])
            if value is None:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", arguments[1])
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        else:
            # Anything else would change the daemon. The fake refuses it and the log shows it.
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied",
                                         "the fake InputPlumber refuses writes")

    def answer_held(self):
        held, self.held = self.held, []
        for answer in held:
            answer()

    def members(self) -> list[tuple]:
        return [(interface, member) for _sender, _path, interface, member, _arguments in self.calls]

    def writes(self) -> list[tuple]:
        """Every recorded call that is not Introspect or a Properties read."""
        return [call for call in self.calls if (call[2], call[3]) not in READS]

    def count(self, member, path=None) -> int:
        return sum(1 for call in self.calls if call[3] == member and (path is None or call[1] == path))
