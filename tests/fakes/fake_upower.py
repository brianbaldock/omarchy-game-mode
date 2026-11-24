"""Stand-ins for UPower: a fake connection for logic tests and a fake service on a private bus.

The service exports the DisplayDevice with the properties Game Mode reads, answers ``Get`` and ``GetAll`` itself
and records *every* call that reaches it, so a test can prove that nothing but reads was ever sent. The interface
description below was written by hand from the names and types of a few properties and two methods of UPower's
public D-Bus interface, and nothing else; no code or XML of UPower is copied.

Tests that use the service need ``dbus-daemon`` and PyGObject; ``PrivateBus.start`` raises ``unittest.SkipTest``
when either is missing. The bus listens on an abstract socket, so nothing touches the user's buses.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from gi.repository import Gio, GLib

from fakes.fake_inputplumber import FIXTURES, PrivateBus
from fakes.rc73ya import rc73ya
from gaminghome.upower import UPowerClient

BUS_NAME = "org.freedesktop.UPower"
DISPLAY_DEVICE = "/org/freedesktop/UPower/devices/DisplayDevice"
DEVICE_IFACE = "org.freedesktop.UPower.Device"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
READS = frozenset({(PROPERTIES_IFACE, "Get"), (PROPERTIES_IFACE, "GetAll")})

# Property names and D-Bus types of the DisplayDevice that the fake serves.
TYPES = {"IsPresent": "b", "Percentage": "d", "State": "u", "Type": "u", "TimeToEmpty": "x", "TimeToFull": "x",
         "IconName": "s", "Energy": "d", "EnergyRate": "d", "UpdateTime": "t", "WarningLevel": "u"}
PROPERTIES = "".join(f'<property name="{name}" type="{kind}" access="read"/>' for name, kind in TYPES.items())
XML = f"""<node>
  <interface name="{PROPERTIES_IFACE}">
    <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{{sv}}" direction="out"/></method>
    <method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{{sv}}"/><arg type="as"/></signal>
  </interface>
  <interface name="{DEVICE_IFACE}">
    <method name="Refresh"/>
    <method name="EnableChargeThreshold"><arg type="b" direction="in"/></method>
    {PROPERTIES}
  </interface>
</node>"""


def variants(values: dict) -> dict:
    return {name: GLib.Variant(TYPES[name], value) for name, value in values.items()}


def reply(values: dict):
    """What ``GetAll`` answers, in the shape ``Gio.DBusConnection.call_finish`` returns."""
    return GLib.Variant("(a{sv})", (variants(values),))


class StubBus:
    """Stands in for a Gio.DBusConnection: records requests, and the test delivers the replies and signals."""

    def __init__(self):
        self.calls, self.subscriptions, self.unsubscribed = [], {}, []

    def call(self, bus_name, path, interface, method, parameters, _reply_type, flags, timeout, cancellable, callback,
             data):
        self.calls.append(SimpleNamespace(bus_name=bus_name, path=path, interface=interface, method=method,
                                          parameters=parameters, flags=flags, timeout=timeout,
                                          cancellable=cancellable, callback=callback, data=data, answered=False))

    def call_finish(self, result):
        if isinstance(result, Exception):
            raise result
        return result

    def signal_subscribe(self, sender, interface, member, path, arg0, flags, callback, *data):
        identifier = len(self.subscriptions) + len(self.unsubscribed) + 1
        self.subscriptions[identifier] = SimpleNamespace(sender=sender, interface=interface, member=member,
                                                         path=path, arg0=arg0, flags=flags, callback=callback,
                                                         data=data)
        return identifier

    def signal_unsubscribe(self, identifier):
        self.unsubscribed.append(self.subscriptions.pop(identifier))

    def pending(self):
        return [call for call in self.calls if not call.answered]

    def answer(self, call, result):
        call.answered = True
        call.callback(self, result, call.data)

    def answer_all(self, values=None):
        """Answer everything pending with the DisplayDevice of ``values`` (an RC73YA on battery by default)."""
        for call in self.pending():
            self.answer(call, reply(rc73ya() if values is None else values))

    def emit_changed(self, sender, path=DISPLAY_DEVICE, interface=DEVICE_IFACE):
        """Deliver a PropertiesChanged signal the way the bus would: only to subscriptions whose filters match."""
        parameters = GLib.Variant("(sa{sv}as)", (interface, {}, []))
        delivered = 0
        for subscription in list(self.subscriptions.values()):
            if (subscription.sender in (None, sender) and subscription.path in (None, path)
                    and subscription.arg0 in (None, interface)):
                subscription.callback(self, sender, path, PROPERTIES_IFACE, "PropertiesChanged", parameters,
                                      *subscription.data)
                delivered += 1
        return delivered


class StubClient(UPowerClient):
    """The client without a bus: the name watch is a record, and the test calls what the watch would call."""

    def __init__(self, on_change):
        super().__init__(on_change)
        self.watches, self.unwatched = [], []

    def _watch_name(self):
        self.watches.append(f"watch-{len(self.watches) + 1}")
        return self.watches[-1]

    def _unwatch_name(self, watch):
        self.unwatched.append(watch)

    def appear(self, bus, owner=":1.42"):
        self._appeared(bus, BUS_NAME, owner)

    def vanish(self, bus):
        self._vanished(bus, BUS_NAME)


def activatable_bus_config(directory: Path, markers: dict[str, Path]) -> Path:
    """A bus configuration on which each of the given names *can* be started, by a command that leaves its marker.

    The tests then show that watching UPower's name never starts it, which a bus without services cannot show.
    """
    services = directory / "services"
    services.mkdir(parents=True)
    for name, marker in markers.items():
        (services / f"{name}.service").write_text(
            f'[D-BUS Service]\nName={name}\nExec={shutil.which("touch")} "{marker}"\n', encoding="utf-8")
    config = directory / "bus.conf"
    config.write_text((FIXTURES / "bus.conf").read_text(encoding="utf-8").replace(
        "</busconfig>", f"  <servicedir>{services}</servicedir>\n</busconfig>"), encoding="utf-8")
    return config


class FakeUPower:
    """One fake UPower instance: its own connection, the DisplayDevice object and a log of every call."""

    def __init__(self, bus: PrivateBus, **changes):
        self.connection = bus.connect()
        self.unique_name = self.connection.get_unique_name()
        self.calls: list[tuple] = []       # (sender, path, interface, member, arguments)
        self.hold = None                   # predicate(call) -> True keeps the reply back
        self.held: list = []               # callables that deliver a kept-back reply
        self.failures: dict = {}           # member -> (error name, message)
        self.values = rc73ya(**changes)
        for interface in Gio.DBusNodeInfo.new_for_xml(XML).interfaces:
            self.connection.register_object(DISPLAY_DEVICE, interface, self._on_call, None, None)

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

    # ---- state -------------------------------------------------------------
    def set(self, signal=True, **changes):
        """Change properties and, like UPower, announce what changed."""
        self.values.update(changes)
        if signal:
            self.emit_changed(**changes)

    def emit_changed(self, **changed):
        self.connection.emit_signal(None, DISPLAY_DEVICE, PROPERTIES_IFACE, "PropertiesChanged",
                                    GLib.Variant("(sa{sv}as)", (DEVICE_IFACE, variants(changed), [])))

    # ---- calls -------------------------------------------------------------
    def _on_call(self, _connection, sender, path, interface, member, parameters, invocation):
        call = (sender, path, interface, member, parameters.unpack())
        self.calls.append(call)
        if self.hold is not None and self.hold(call):
            self.held.append(lambda: self._answer(call, invocation))
            return
        self._answer(call, invocation)

    def _answer(self, call, invocation):
        _sender, _path, interface, member, arguments = call
        if member in self.failures:
            invocation.return_dbus_error(*self.failures[member])
        elif (interface, member) == (PROPERTIES_IFACE, "GetAll") and arguments[0] == DEVICE_IFACE:
            invocation.return_value(GLib.Variant("(a{sv})", (variants(self.values),)))
        elif (interface, member) == (PROPERTIES_IFACE, "Get") and arguments[0] == DEVICE_IFACE \
                and arguments[1] in self.values:
            invocation.return_value(GLib.Variant("(v)", (variants(self.values)[arguments[1]],)))
        elif (interface, member) in READS:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownInterface", arguments[0])
        else:
            # Anything else would change UPower: Refresh, a charge threshold, a property. Refuse it and log it.
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "the fake UPower refuses writes")

    def answer_held(self):
        held, self.held = self.held, []
        for answer in held:
            answer()

    def members(self) -> list[tuple]:
        return [(interface, member) for _sender, _path, interface, member, _arguments in self.calls]

    def writes(self) -> list[tuple]:
        """Every recorded call that is not a Properties read."""
        return [call for call in self.calls if (call[2], call[3]) not in READS]

    def count(self, member) -> int:
        return sum(1 for call in self.calls if call[3] == member)
