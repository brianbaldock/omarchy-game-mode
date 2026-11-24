"""A fake power-profiles-daemon on a private bus, for the tests of the power profile client.

It owns ``org.freedesktop.UPower.PowerProfiles`` (and can give the name up and take it again), serves the
properties of ``/org/freedesktop/UPower/PowerProfiles`` and answers the ``org.freedesktop.DBus.Properties`` calls
itself, so *every* call that reaches it is logged: who sent it, where it was addressed, and whether the caller let
the bus start a service for it. Where the client depends on it, it behaves like PPD 0.30:

* a ``Set`` of ``ActiveProfile`` applies the profile and sends ``PropertiesChanged`` before it replies;
* a ``Set`` to the profile that is already active replies, sends nothing and leaves the holds alone;
* a ``Set`` to another profile releases every hold and tells each holder with ``ProfileReleased``;
* another program changing the profile (``external``) releases no hold;
* ``PropertiesChanged`` goes out on the legacy object as well.

It can also refuse every change (``deny``), answer a method with an error (``failures``), keep a call back
(``hold``), apply a ``Set`` at once and keep only its reply back (``late``), and answer ``GetAll`` with what PPD never
sends (``extra`` properties, a ``garbage`` reply). The interface description was written by hand from the names and
types of PPD's documented D-Bus interface; no code of PPD is copied.

Tests that use it need ``dbus-daemon`` and PyGObject; ``PrivateBus.start`` raises ``unittest.SkipTest`` when either
is missing.
"""

from __future__ import annotations

from typing import NamedTuple

from gi.repository import Gio, GLib

BUS_NAME = "org.freedesktop.UPower.PowerProfiles"
OBJECT_PATH = "/org/freedesktop/UPower/PowerProfiles"
INTERFACE = "org.freedesktop.UPower.PowerProfiles"
LEGACY_PATH = "/net/hadess/PowerProfiles"
LEGACY_INTERFACE = "net.hadess.PowerProfiles"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
INTROSPECTABLE_IFACE = "org.freedesktop.DBus.Introspectable"
READS = frozenset({(PROPERTIES_IFACE, "Get"), (PROPERTIES_IFACE, "GetAll"), (INTROSPECTABLE_IFACE, "Introspect")})
ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"

# (profile, CPU driver, platform driver) as an ROG Xbox Ally lists them, and as a machine with nothing to switch does.
RC73YA = (("power-saver", "amd_pstate", "platform_profile"), ("balanced", "amd_pstate", "platform_profile"),
          ("performance", "amd_pstate", "platform_profile"))
PLACEHOLDER = (("power-saver", "", "placeholder"), ("balanced", "", "placeholder"))

TYPES = {"ActiveProfile": "s", "PerformanceInhibited": "s", "PerformanceDegraded": "s", "Profiles": "aa{sv}",
         "Actions": "as", "ActionsInfo": "aa{sv}", "ActiveProfileHolds": "aa{sv}", "Version": "s",
         "BatteryAware": "b"}
WRITABLE = ("ActiveProfile", "BatteryAware")
PROPERTIES = "".join(f'<property name="{name}" type="{kind}" access="{"readwrite" if name in WRITABLE else "read"}"/>'
                     for name, kind in TYPES.items())
XML = f"""<node>
  <interface name="{PROPERTIES_IFACE}">
    <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{{sv}}" direction="out"/></method>
    <method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method>
    <signal name="PropertiesChanged"><arg type="s"/><arg type="a{{sv}}"/><arg type="as"/></signal>
  </interface>
  <interface name="{INTROSPECTABLE_IFACE}">
    <method name="Introspect"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="{INTERFACE}">
    <method name="HoldProfile">
      <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/>
      <arg type="u" direction="out"/>
    </method>
    <method name="ReleaseProfile"><arg type="u" direction="in"/></method>
    <method name="SetActionEnabled"><arg type="s" direction="in"/><arg type="b" direction="in"/></method>
    <signal name="ProfileReleased"><arg type="u"/></signal>
    {PROPERTIES}
  </interface>
</node>"""


class Call(NamedTuple):
    sender: str
    path: str
    interface: str
    member: str
    arguments: tuple
    destination: str       # the name the caller addressed: a unique name, or a well-known name the bus resolved
    auto_start: bool       # True when the caller let the bus start a service for this call


def profile_entries(profiles) -> GLib.Variant:
    """``Profiles`` the way PPD lists them: a driver that is not there is left out, ``Driver`` is the old summary."""
    entries = []
    for name, cpu, platform in profiles:
        entry = {"Profile": GLib.Variant("s", name)}
        if cpu:
            entry["CpuDriver"] = GLib.Variant("s", cpu)
        if platform:
            entry["PlatformDriver"] = GLib.Variant("s", platform)
        entry["Driver"] = GLib.Variant("s", "multiple" if cpu and platform else cpu or platform)
        entries.append(entry)
    return GLib.Variant("aa{sv}", entries)


class FakePPD:
    """One fake daemon: its own connection, the PowerProfiles object, its state and a log of every call."""

    def __init__(self, bus, active="performance", profiles=RC73YA, degraded=""):
        self.connection = bus.connect()
        self.unique_name = self.connection.get_unique_name()
        self.calls: list[Call] = []
        self.hold = None            # predicate(call) -> True keeps the call back: not applied, not answered
        self.late = None            # predicate(call) -> True applies a Set at once and keeps only its reply back
        self.held: list = []        # callables that go on with what was kept back
        self.failures: dict = {}    # member -> (error name, message) it answers with
        self.deny = False           # polkit refuses every change
        self.extra: dict = {}       # name -> GLib.Variant that GetAll serves next to the real ones, or in their place
        self.garbage = None         # a GLib.Variant that GetAll answers with instead of its (a{sv})
        self.active = active
        self.profiles = tuple(profiles)
        self.degraded = degraded
        self.battery_aware = True
        self.holds: list[dict] = []
        self.released: list[int] = []
        self._cookie = 0
        self._before_holds = None
        for interface in Gio.DBusNodeInfo.new_for_xml(XML).interfaces:
            self.connection.register_object(OBJECT_PATH, interface, self._on_call, None, None)

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
    def names(self) -> list[str]:
        return [name for name, _cpu, _platform in self.profiles]

    def properties(self) -> dict:
        values = {"ActiveProfile": GLib.Variant("s", self.active), "PerformanceInhibited": GLib.Variant("s", ""),
                  "PerformanceDegraded": GLib.Variant("s", self.degraded), "Profiles": profile_entries(self.profiles),
                  "Actions": GLib.Variant("as", []), "ActionsInfo": GLib.Variant("aa{sv}", []),
                  "ActiveProfileHolds": GLib.Variant("aa{sv}", [
                      {key: GLib.Variant("s", hold[key]) for key in ("ApplicationId", "Profile", "Reason")}
                      for hold in self.holds]),
                  "Version": GLib.Variant("s", "0.30"), "BatteryAware": GLib.Variant("b", self.battery_aware)}
        values.update(self.extra)
        return values

    def emit_changed(self, *names):
        """PropertiesChanged with the present values of ``names``, on the object and on the legacy one, as PPD does."""
        values = self.properties()
        changed = {name: values[name] for name in names}
        for path, interface in ((OBJECT_PATH, INTERFACE), (LEGACY_PATH, LEGACY_INTERFACE)):
            self.connection.emit_signal(None, path, PROPERTIES_IFACE, "PropertiesChanged",
                                        GLib.Variant("(sa{sv}as)", (interface, changed, [])))

    def external(self, profile):
        """Another program (a hotkey, asusd through the platform profile) changes the profile. No hold is released."""
        if profile != self.active:
            self.active = profile
            self.emit_changed("ActiveProfile")

    def set_degraded(self, text):
        self.degraded = text
        self.emit_changed("PerformanceDegraded")

    def add_hold(self, profile, application_id, reason="", holder=None, signal=True) -> int:
        """A hold as another program leaves it; ``holder`` is its unique name, told with ProfileReleased at the end."""
        self._cookie += 1
        self.holds.append({"cookie": self._cookie, "ApplicationId": application_id, "Profile": profile,
                           "Reason": reason, "holder": holder})
        if signal:
            self.emit_changed("ActiveProfileHolds")
        return self._cookie

    def _release_holds(self) -> bool:
        holds, self.holds = self.holds, []
        for hold in holds:
            self.released.append(hold["cookie"])
            if hold["holder"]:
                self.connection.emit_signal(hold["holder"], OBJECT_PATH, INTERFACE, "ProfileReleased",
                                            GLib.Variant("(u)", (hold["cookie"],)))
        return bool(holds)

    # ---- calls -------------------------------------------------------------
    def _on_call(self, _connection, sender, path, interface, member, parameters, invocation):
        message = invocation.get_message()
        call = Call(sender, path, interface, member, parameters.unpack(), message.get_destination(),
                    not message.get_flags() & Gio.DBusMessageFlags.NO_AUTO_START)
        self.calls.append(call)
        if self.hold is not None and self.hold(call):
            self.held.append(lambda: self._answer(call, parameters, invocation))
            return
        self._answer(call, parameters, invocation)

    def _answer(self, call, parameters, invocation):
        key = (call.interface, call.member)
        if call.member in self.failures:
            invocation.return_dbus_error(*self.failures[call.member])
        elif key == (PROPERTIES_IFACE, "GetAll"):
            self._get_all(call.arguments[0], invocation)
        elif key == (PROPERTIES_IFACE, "Get"):
            value = self.properties().get(call.arguments[1]) if call.arguments[0] == INTERFACE else None
            if value is None:
                invocation.return_dbus_error(INVALID_ARGS, "no such property")
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif key == (PROPERTIES_IFACE, "Set"):
            self._set(call, parameters.get_child_value(2).get_variant(), invocation)
        elif key == (INTROSPECTABLE_IFACE, "Introspect"):
            invocation.return_value(GLib.Variant("(s)", (XML,)))
        elif key == (INTERFACE, "HoldProfile"):
            self._hold_profile(call, invocation)
        elif key == (INTERFACE, "ReleaseProfile"):
            self._release_profile(call, invocation)
        else:
            invocation.return_value(None)                      # SetActionEnabled: logged, and nothing to do

    def _get_all(self, interface, invocation):
        if interface != INTERFACE:
            invocation.return_dbus_error(INVALID_ARGS, "no such interface")
        elif self.garbage is not None:
            # Gio will not send a reply of the wrong type, so this one is put together by hand.
            reply = Gio.DBusMessage.new_method_reply(invocation.get_message())
            reply.set_body(self.garbage)
            self.connection.send_message(reply, Gio.DBusSendMessageFlags.NONE)
        else:
            invocation.return_value(GLib.Variant("(a{sv})", (self.properties(),)))

    def _set(self, call, value, invocation):
        interface, name, _value = call.arguments
        if interface != INTERFACE or name not in WRITABLE:
            invocation.return_dbus_error(INVALID_ARGS, "no such writable property")
        elif self.deny:
            invocation.return_dbus_error(ACCESS_DENIED, "the fake daemon refuses this caller")
        elif name == "BatteryAware":
            if value.get_type_string() != "b":
                invocation.return_dbus_error(INVALID_ARGS, "not a boolean")
                return
            self.battery_aware = value.get_boolean()
            self.emit_changed("BatteryAware")
            invocation.return_value(None)
        else:
            profile = value.get_string() if value.get_type_string() == "s" else None
            if profile not in self.names():
                invocation.return_dbus_error(INVALID_ARGS, "no such profile")
                return
            if profile != self.active:
                released = self._release_holds()
                self.active = profile
                self.emit_changed("ActiveProfile", *(("ActiveProfileHolds",) if released else ()))
            if self.late is not None and self.late(call):
                self.held.append(lambda: invocation.return_value(None))
            else:
                invocation.return_value(None)

    def _hold_profile(self, call, invocation):
        profile, reason, application_id = call.arguments
        if profile not in ("power-saver", "performance") or profile not in self.names():
            invocation.return_dbus_error(INVALID_ARGS, "this profile cannot be held")
            return
        if not self.holds:
            self._before_holds = self.active
        cookie = self.add_hold(profile, application_id, reason, holder=call.sender, signal=False)
        wanted = "power-saver" if any(hold["Profile"] == "power-saver" for hold in self.holds) else "performance"
        changed = ["ActiveProfileHolds"]
        if wanted != self.active:
            self.active = wanted
            changed.insert(0, "ActiveProfile")
        self.emit_changed(*changed)
        invocation.return_value(GLib.Variant("(u)", (cookie,)))

    def _release_profile(self, call, invocation):
        hold = next((hold for hold in self.holds if hold["cookie"] == call.arguments[0]), None)
        if hold is None:
            invocation.return_dbus_error(INVALID_ARGS, "no such hold")
            return
        self.holds.remove(hold)
        changed = ["ActiveProfileHolds"]
        if not self.holds and self._before_holds not in (None, self.active):
            self.active = self._before_holds
            changed.insert(0, "ActiveProfile")
        self.emit_changed(*changed)
        invocation.return_value(None)

    def answer_held(self):
        held, self.held = self.held, []
        for answer in held:
            answer()

    # ---- the log -----------------------------------------------------------
    def sent_by(self, sender) -> list[Call]:
        return [call for call in self.calls if call.sender == sender]

    def writes(self) -> list[Call]:
        """Every logged call that is not a read: a Set, a hold, an action."""
        return [call for call in self.calls if (call.interface, call.member) not in READS]

    def count(self, member) -> int:
        return sum(1 for call in self.calls if call.member == member)
