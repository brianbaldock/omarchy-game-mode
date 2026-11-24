"""A fake bluetoothd on a private bus, and the clock that the Bluetooth tests fire by hand.

``FakeBlueZ`` owns ``org.bluez`` and serves what Gaming Home uses of BlueZ: the ObjectManager at ``/``,
``AgentManager1`` at ``/org/bluez``, adapters (``Adapter1``), devices (``Device1``), a device's own ``Battery1`` and
battery objects below a device. The interface descriptions were written by hand from the names and types in BlueZ's
documented D-Bus API; no code or XML of BlueZ is copied. Where it matters, the fake follows what BlueZ 5.87 does
(``src/device.c``, ``src/adapter.c``, ``src/agent.c``), not only what its documentation says.

It answers ``org.freedesktop.DBus.Properties`` itself, so *every* method call that reaches it is recorded (sender,
path, interface, member, arguments, and the destination and flags it was sent with). A test can fail a call
(``failures``), and it can make the fake late in two ways:

* ``defer``: the call is handled at once and only its answer waits for ``answer_deferred``, which is what BlueZ does
  with its slow methods. ``Pair`` makes the bond and announces ``Bonded``, and then only the answer (and ``Paired``)
  wait: the service discovery BlueZ runs after a bond. ``Connect``'s answer waits, and a ``Disconnect`` meanwhile
  answers it ``br-connection-canceled``. ``StartDiscovery`` counts the client as searching with its start pending, and
  ``StopDiscovery`` keeps it searching until answered, so a stop or a start from that client meanwhile is refused
  ``InProgress``. A write of ``Powered`` waits, and another one meanwhile gets ``Busy``.
* ``hold``: the call is not handled at all until ``answer_held``, and that client's later calls queue behind it: a
  stuck or very slow bluetoothd, which BlueZ itself is not.

Like BlueZ:

* agents queue: the first one registered is the default, and a program's own agent answers the pairings that program
  starts (``RequestDefaultAgent`` would move one to the front);
* each client has its own discovery session per adapter;
* a client whose connection goes loses its discovery sessions, its agent and its pairing (``NameOwnerChanged``);
* ``Connect`` on a device whose profiles are all connected, and ``Disconnect`` on a disconnected one, succeed;
* writing ``Powered`` is answered before the change is announced, and refused ``Blocked`` while rfkill blocks the
  adapter.

``Pair()`` brings the link up (``Connected``) unless ``link_on_pair`` is off, then runs the device's script
(``scripts[path]``): calls to the pairing agent, each sent when the one before was answered. A step is
``(member, *arguments)`` without the device, ``WAIT`` (go on when the test calls ``resume``), ``BOND`` (the bond is
made here: ``Bonded``, and the pairing request is over) or ``(FAIL, error name)``. At the end come ``Bonded``, then
``Paired``, then the answer: BlueZ sends pending property signals before any reply. An agent that refuses makes BlueZ
refuse the pairing to the device, which comes back from ``Pair()`` as ``AuthenticationFailed``.

``CancelPairing`` while the pairing request exists ends ``Pair()`` with ``AuthenticationCanceled``. Without one (after
the bond, or with no pairing at all) BlueZ 5.87 removes the device's keys from the kernel and answers
``DoesNotExist``; the fake records removed keys in ``unpaired`` and drops the link, which a device without keys keeps.
Cancels that found a pairing request are in ``cancelled``. BlueZ is not told of its own unpairing,
so ``Paired`` and ``Bonded`` stay as they were. A device removed during a pairing gets ``Pair()`` answered first:
``AuthenticationCanceled`` while the link is up, ``ConnectionAttemptFailed`` otherwise, and no answer at all once
the bond is made. Tests need ``dbus-daemon`` and PyGObject; ``PrivateBus.start`` skips them when either is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gi.repository import Gio, GLib

from fakes.clockwork import Timers

BUS_NAME = "org.bluez"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
BATTERY = "org.bluez.Battery1"
AGENT_MANAGER = "org.bluez.AgentManager1"
AGENT = "org.bluez.Agent1"
ERROR = "org.bluez.Error."
REJECTED, CANCELED = ERROR + "Rejected", ERROR + "Canceled"
WAIT, FAIL, BOND = "wait", "fail", "bond"
CAPABILITIES = ("DisplayOnly", "DisplayYesNo", "KeyboardOnly", "NoInputNoOutput", "KeyboardDisplay", "")

# D-Bus types of the properties the fake knows.
TYPES = {"Address": "s", "AddressType": "s", "Name": "s", "Alias": "s", "Icon": "s", "Class": "u", "Appearance": "q",
         "Paired": "b", "Bonded": "b", "Trusted": "b", "Blocked": "b", "Connected": "b", "Connectable": "b",
         "LegacyPairing": "b", "ServicesResolved": "b", "WakeAllowed": "b", "RSSI": "n", "TxPower": "n",
         "UUIDs": "as", "Adapter": "o", "ManufacturerData": "a{qv}", "Powered": "b", "PowerState": "s",
         "Discovering": "b", "Discoverable": "b", "Pairable": "b", "Percentage": "y", "Source": "s", "Device": "o"}

_PROPERTIES_XML = """<interface name="org.freedesktop.DBus.Properties">
  <method name="Get">
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>
  <method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method>
  <signal name="PropertiesChanged"><arg type="s"/><arg type="a{sv}"/><arg type="as"/></signal>
</interface>"""
_XML = f"""<node>
{_PROPERTIES_XML}
<interface name="org.freedesktop.DBus.ObjectManager">
  <method name="GetManagedObjects"><arg type="a{{oa{{sa{{sv}}}}}}" direction="out"/></method>
  <signal name="InterfacesAdded"><arg type="o"/><arg type="a{{sa{{sv}}}}"/></signal>
  <signal name="InterfacesRemoved"><arg type="o"/><arg type="as"/></signal>
</interface>
<interface name="org.bluez.AgentManager1">
  <method name="RegisterAgent"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="UnregisterAgent"><arg type="o" direction="in"/></method>
  <method name="RequestDefaultAgent"><arg type="o" direction="in"/></method>
</interface>
<interface name="org.bluez.Adapter1">
  <method name="StartDiscovery"/>
  <method name="StopDiscovery"/>
  <method name="RemoveDevice"><arg type="o" direction="in"/></method>
  <method name="SetDiscoveryFilter"><arg type="a{{sv}}" direction="in"/></method>
  <method name="GetDiscoveryFilters"><arg type="as" direction="out"/></method>
  <method name="ConnectDevice"><arg type="a{{sv}}" direction="in"/><arg type="o" direction="out"/></method>
</interface>
<interface name="org.bluez.Device1">
  <method name="Pair"/>
  <method name="CancelPairing"/>
  <method name="Connect"/>
  <method name="Disconnect"/>
  <method name="ConnectProfile"><arg type="s" direction="in"/></method>
  <method name="DisconnectProfile"><arg type="s" direction="in"/></method>
</interface>
<interface name="org.bluez.Battery1"/>
</node>"""
INTERFACES = {info.name: info for info in Gio.DBusNodeInfo.new_for_xml(_XML).interfaces}

AGENT_XML = """<node><interface name="org.bluez.Agent1">
  <method name="Release"/>
  <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
  <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
  <method name="DisplayPasskey">
    <arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/>
  </method>
  <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
  <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
  <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
  <method name="Cancel"/>
</interface></node>"""
AGENT_INFO = Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0]
# What each agent method takes, device first where there is one.
AGENT_SIGNATURES = {"Release": "()", "Cancel": "()", "RequestPinCode": "(o)", "DisplayPinCode": "(os)",
                    "RequestPasskey": "(o)", "DisplayPasskey": "(ouq)", "RequestConfirmation": "(ou)",
                    "RequestAuthorization": "(o)", "AuthorizeService": "(os)"}
AGENT_REPLIES = {"RequestPinCode": "(s)", "RequestPasskey": "(u)"}


def variant(name: str, value) -> GLib.Variant:
    return value if isinstance(value, GLib.Variant) else GLib.Variant(TYPES[name], value)


def blob(size: int) -> GLib.Variant:
    """``size`` zero bytes as an ``ay``, built in one piece (``GLib.Variant("ay", ...)`` makes one child per byte)."""
    return GLib.Variant.new_from_bytes(GLib.VariantType("ay"), GLib.Bytes.new(bytes(size)), True)


def address_path(address: str, adapter: int = 0) -> str:
    return f"/org/bluez/hci{adapter}/dev_{address.replace(':', '_')}"


@dataclass
class AgentCall:
    """One call to a pairing agent and what it answered: ``value`` (unpacked) or the D-Bus ``error`` name."""

    member: str
    arguments: tuple
    done: bool = False
    value: tuple | None = None
    error: str = ""


def call_agent(connection, destination: str, path: str, member: str, arguments: tuple = (), then=None) -> AgentCall:
    """Call ``member`` of the agent at ``destination`` and ``path`` over ``connection``, as BlueZ (or anyone) would."""
    record = AgentCall(member, tuple(arguments))

    def done(bus, result, _data):
        try:
            record.value = bus.call_finish(result).unpack()
        except GLib.Error as error:
            record.error = Gio.DBusError.get_remote_error(error) or f"local:{error.code}"
        record.done = True
        if then is not None:
            then(record)

    connection.call(destination, path, AGENT, member, GLib.Variant(AGENT_SIGNATURES[member], tuple(arguments)),
                    GLib.VariantType(AGENT_REPLIES.get(member, "()")), Gio.DBusCallFlags.NONE, 5000, None, done, None)
    return record


@dataclass
class Pairing:
    device: str
    sender: str
    invocation: object
    agent: tuple                     # (unique name, object path) of the agent BlueZ asks
    steps: list
    deferred: bool = False           # the answer waits for ``answer_deferred`` once the bond is made
    waiting: AgentCall | None = None
    paused: bool = False
    bonded: bool = False             # the bond is made: BlueZ's pairing request is over


class FakeBlueZ:
    """One fake bluetoothd: its own connection, its objects, the agents and sessions of its clients, and a call log."""

    def __init__(self, bus, adapters=(0,), powered=True):
        self.connection = bus.connect()
        self.unique_name = self.connection.get_unique_name()
        self.calls: list[tuple] = []        # (sender, path, interface, member, arguments)
        self.sent: list[tuple] = []         # (destination, flags) of each recorded call
        self.hold = None                    # predicate(call) -> True: the call is not handled until answer_held
        self.held: list = []
        self.defer = None                   # predicate(call) -> True: handled now, answered by answer_deferred
        self.deferred: list = []            # (member, function that answers)
        self.failures: dict = {}            # member or (path, member) -> (error name, message)
        self.objects: dict[str, dict[str, dict]] = {}
        self.registrations: dict[str, list[int]] = {}
        self.agents: dict[str, tuple] = {}  # sender -> (path, capability)
        self.queue: list[str] = []          # senders whose agents may be the default, the default first
        self.sessions: dict[str, set] = {}  # adapter path -> senders searching
        self.starting: set = set()          # (adapter path, sender) whose start is unanswered
        self.stopping: set = set()          # (adapter path, sender) whose stop is unanswered
        self.powering: set = set()          # adapter paths with a write of Powered unanswered
        self.connecting: dict = {}          # device path -> invocation of a Connect that waits
        self.profiles: set = set()          # devices whose profiles are connected, not just their link
        self.cancelled: list[str] = []      # devices whose pairing request CancelPairing ended
        self.unpaired: list[str] = []       # devices whose keys BlueZ removed on CancelPairing
        self.link_on_pair = True            # Pair() brings the link up first, as BlueZ does
        self._call = None                   # the call being handled, for ``_later``
        self.gone: set = set()              # unique names that left the bus
        self.scripts: dict[str, list] = {}  # device path -> what Pair() asks the agent
        self.pairings: dict[str, Pairing] = {}
        self.asked: list[tuple] = []        # (agent, AgentCall) for every agent call the fake made
        self.managed_reply: GLib.Variant | None = None   # replaces the GetManagedObjects answer when set
        self.powered_follows = True         # False: a write of Powered is answered and nothing happens
        self._register("/", (OBJECT_MANAGER,))
        self.objects["/org/bluez"] = {AGENT_MANAGER: {}}
        self._register("/org/bluez", (PROPERTIES, AGENT_MANAGER))
        self.connection.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                         "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE,
                                         self._on_owner_changed)
        for index in adapters:
            self.add_adapter(index, powered=powered, signal=False)

    # ---- bus name ----------------------------------------------------------
    def _bus_call(self, method, signature, *arguments):
        return self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                         method, GLib.Variant(signature, arguments), None, Gio.DBusCallFlags.NONE,
                                         3000, None)

    def own(self) -> bool:
        return self._bus_call("RequestName", "(su)", BUS_NAME, 4).unpack()[0] == 1

    def release(self):
        self._bus_call("ReleaseName", "(s)", BUS_NAME)

    def close(self):
        if not self.connection.is_closed():
            self.connection.close_sync(None)

    # ---- objects -----------------------------------------------------------
    def _register(self, path, interfaces):
        self.registrations.setdefault(path, []).extend(
            self.connection.register_object(path, INTERFACES[name], self._on_call, None, None) for name in interfaces)

    def _export(self, path, interfaces: dict, signal=True):
        known = path in self.objects
        self.objects.setdefault(path, {}).update(interfaces)
        if not known:
            self._register(path, (PROPERTIES, *interfaces))
        if signal:
            self.emit_added(path, interfaces)

    def _unexport(self, path, signal=True):
        interfaces = list(self.objects.pop(path))
        for registration in self.registrations.pop(path, []):
            self.connection.unregister_object(registration)
        if signal:
            self._emit("/", OBJECT_MANAGER, "InterfacesRemoved", GLib.Variant("(oas)", (path, interfaces)))

    def add_adapter(self, index=0, powered=True, power_state=None, signal=True, **extra) -> str:
        path = f"/org/bluez/hci{index}"
        properties = {"Address": "AA:BB:CC:DD:EE:F0", "AddressType": "public", "Name": "fake", "Alias": "fake",
                      "Class": 0, "Powered": powered, "PowerState": power_state or ("on" if powered else "off"),
                      "Discovering": False, "Discoverable": False, "Pairable": True, **extra}
        self._export(path, {ADAPTER: {name: variant(name, value) for name, value in properties.items()}}, signal)
        self.sessions.setdefault(path, set())
        return path

    def remove_adapter(self, index=0):
        path = f"/org/bluez/hci{index}"
        for other in [other for other in self.objects if other.startswith(path + "/")]:
            if other in self.objects:
                self.remove_device(other)
        self.sessions.pop(path, None)
        self._unexport(path)

    def add_device(self, address="AA:BB:CC:DD:EE:01", adapter=0, name="Pad", icon="input-gaming", paired=False,
                   trusted=False, connected=False, battery=None, signal=True, script=None, **extra) -> str:
        """A device as BlueZ announces it. ``extra`` adds or replaces properties (a GLib.Variant keeps its type);
        ``name=None`` leaves Name out and makes Alias the address with dashes, as BlueZ does."""
        path = address_path(address, adapter)
        properties = {"Address": address, "AddressType": "public", "Alias": name or address.replace(":", "-"),
                      "Paired": paired, "Bonded": paired, "Trusted": trusted, "Blocked": False,
                      "Connected": connected, "LegacyPairing": False, "ServicesResolved": connected,
                      "Adapter": f"/org/bluez/hci{adapter}", "UUIDs": [], "RSSI": -60}
        if name is not None:
            properties["Name"] = name
        if icon is not None:
            properties["Icon"] = icon
        properties.update(extra)
        interfaces = {DEVICE: {key: variant(key, value) for key, value in properties.items() if value is not None}}
        if battery is not None:
            interfaces[BATTERY] = {"Percentage": variant("Percentage", battery), "Source": variant("Source", "HFP")}
        if script is not None:
            self.scripts[path] = list(script)
        if connected:
            self.profiles.add(path)
        self._export(path, interfaces, signal)
        return path

    def remove_device(self, path):
        for child in [other for other in self.objects if other.startswith(path + "/")]:
            self._unexport(child)
        pairing = self.pairings.get(path)
        if pairing is not None and pairing.bonded:
            del self.pairings[path]             # BlueZ drops the waiting Pair() without an answer
        elif pairing is not None:   # BlueZ answers the pairing first and announces the removal afterwards
            if self.value(path, DEVICE, "Connected"):
                self._fail_pair(pairing, ERROR + "AuthenticationCanceled", "Authentication Canceled")
            else:
                self._fail_pair(pairing, ERROR + "ConnectionAttemptFailed", "Page Timeout")
        self._unexport(path)

    def add_battery(self, device, percentage, signal=True):
        """Battery1 appears on the device's own object."""
        properties = {"Percentage": variant("Percentage", percentage), "Source": variant("Source", "HFP")}
        self.objects[device][BATTERY] = properties
        if signal:
            self.emit_added(device, {BATTERY: properties})

    def remove_battery(self, device):
        del self.objects[device][BATTERY]
        self._emit("/", OBJECT_MANAGER, "InterfacesRemoved", GLib.Variant("(oas)", (device, [BATTERY])))

    def add_component_battery(self, device, number=1, percentage=5, signal=True) -> str:
        """A battery object below the device, such as one earbud's (``.../battery_N``), carrying ``Device``."""
        path = f"{device}/battery_{number}"
        self._export(path, {BATTERY: {"Percentage": variant("Percentage", percentage),
                                      "Device": variant("Device", device), "Source": variant("Source", "GATT")}},
                     signal)
        return path

    def set(self, path, interface, signal=True, **values):
        """Change properties and, like BlueZ, announce what changed."""
        changed = {name: variant(name, value) for name, value in values.items()}
        self.objects[path][interface].update(changed)
        if signal:
            self.emit_changed(path, interface, changed)

    def emit_changed(self, path, interface, changed: dict, invalidated=()):
        self._emit(path, PROPERTIES, "PropertiesChanged",
                   GLib.Variant("(sa{sv}as)", (interface, changed, list(invalidated))))

    def emit_added(self, path, interfaces: dict):
        self._emit("/", OBJECT_MANAGER, "InterfacesAdded", GLib.Variant("(oa{sa{sv}})", (path, interfaces)))

    def _emit(self, path, interface, member, parameters):
        if not self.connection.is_closed():         # a call queued before the fake went away may still arrive
            self.connection.emit_signal(None, path, interface, member, parameters)

    def managed(self) -> GLib.Variant:
        objects = {path: {name: dict(properties) for name, properties in interfaces.items()}
                   for path, interfaces in self.objects.items()}
        return GLib.Variant("(a{oa{sa{sv}}})", (objects,))

    def value(self, path, interface, name):
        found = self.objects.get(path, {}).get(interface, {}).get(name)
        return None if found is None else found.unpack()

    # ---- agents and sessions -----------------------------------------------
    def default_agent(self) -> tuple | None:
        """(unique name, path) of the agent BlueZ asks about anything that no program's own agent handles."""
        return (self.queue[0], self.agents[self.queue[0]][0]) if self.queue else None

    def agent_of(self, sender) -> tuple | None:
        entry = self.agents.get(sender)
        return (sender, entry[0]) if entry else None

    def _on_owner_changed(self, _connection, _sender, _path, _interface, _signal, parameters, *_data):
        name, _old, new = parameters.unpack()
        if not name.startswith(":") or new:
            return
        self.gone.add(name)
        self.agents.pop(name, None)
        if name in self.queue:
            self.queue.remove(name)
        self.starting = {(path, sender) for path, sender in self.starting if sender != name}
        self.stopping = {(path, sender) for path, sender in self.stopping if sender != name}
        for path, senders in self.sessions.items():
            if name in senders:
                senders.discard(name)
                self._discovering(path)
        for pairing in [pairing for pairing in self.pairings.values() if pairing.sender == name]:
            self._fail_pair(pairing, ERROR + "AuthenticationCanceled", "requester left")

    def _discovering(self, adapter):
        searching = bool(self.sessions.get(adapter))
        if adapter in self.objects and self.value(adapter, ADAPTER, "Discovering") != searching:
            self.set(adapter, ADAPTER, Discovering=searching)

    # ---- calls -------------------------------------------------------------
    def _on_call(self, _connection, sender, path, interface, member, parameters, invocation):
        if self.connection.is_closed():
            return
        call = (sender, path, interface, member, parameters.unpack())
        message = invocation.get_message()
        self.calls.append(call)
        self.sent.append((message.get_destination(), message.get_flags()))
        # A stuck bluetoothd still handles one client's calls in order: behind a held call, its later ones wait.
        if (self.hold is not None and self.hold(call)) or any(who == sender for who, _answer in self.held):
            self.held.append((sender, lambda: self._answer(call, invocation)))
            return
        self._answer(call, invocation)

    def answer_held(self):
        held, self.held = self.held, []
        for _sender, answer in held:
            answer()

    def _later(self, call, answer) -> bool:
        """Keep ``answer`` for ``answer_deferred`` when the test defers this call; False: answer now."""
        if self.defer is None or not self.defer(call):
            return False
        self.deferred.append((call[3], answer))
        return True

    def answer_deferred(self, member=None):
        """Send the answers kept back by ``defer`` (of ``member`` only, when given), in the order they came."""
        kept, self.deferred = self.deferred, []
        for name, answer in kept:
            if member is None or name == member:
                answer()
            else:
                self.deferred.append((name, answer))

    def _answer(self, call, invocation):
        sender, path, interface, member, arguments = call
        failure = self.failures.get((path, member)) or self.failures.get(member)
        if failure is not None:
            invocation.return_dbus_error(*failure)
            return
        if path not in self.objects and path != "/":       # gone while the call was held back
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownObject", "no such object")
            return
        handler = getattr(self, f"_{member}", None)
        if interface == PROPERTIES:
            handler = {"Get": self._get, "GetAll": self._get_all, "Set": self._set}[member]
        if handler is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "the fake refuses this call")
            return
        self._call = call
        handler(sender, path, arguments, invocation)

    @staticmethod
    def _error(invocation, short, message=""):
        invocation.return_dbus_error(ERROR + short, message or short)

    def _GetManagedObjects(self, _sender, _path, _arguments, invocation):
        reply = self.managed_reply if self.managed_reply is not None else self.managed()
        if reply.get_type_string() == "(a{oa{sa{sv}}})":
            invocation.return_value(reply)
            return
        # A malformed answer, sent as it is: GDBus would refuse to send it as the method's reply.
        message = Gio.DBusMessage.new_method_reply(invocation.get_message())
        message.set_body(reply)
        self.connection.send_message(message, Gio.DBusSendMessageFlags.NONE)

    def _get(self, _sender, path, arguments, invocation):
        found = self.objects.get(path, {}).get(arguments[0], {}).get(arguments[1])
        if found is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "No such property")
        else:
            invocation.return_value(GLib.Variant("(v)", (found,)))

    def _get_all(self, _sender, path, arguments, invocation):
        properties = self.objects.get(path, {}).get(arguments[0])
        if properties is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "No such interface")
        else:
            invocation.return_value(GLib.Variant("(a{sv})", (dict(properties),)))

    def _set(self, _sender, path, arguments, invocation):
        interface, name, value = arguments
        properties = self.objects.get(path, {}).get(interface)
        if properties is None or name not in properties:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "No such property")
            return
        if (interface, name) == (ADAPTER, "Powered"):
            if path in self.powering:
                self._error(invocation, "Busy")
                return
            if value and self.value(path, ADAPTER, "PowerState") == "off-blocked":
                self._error(invocation, "Blocked", "Blocked through rfkill")
                return
            if bool(value) == self._powered(path):
                invocation.return_value(None)    # nothing to change: answered, and nothing announced
                return

            def done():
                self.powering.discard(path)
                invocation.return_value(None)    # BlueZ answers first and announces the change afterwards
                if self.powered_follows and path in self.objects:
                    self.set(path, ADAPTER, Powered=bool(value), PowerState="on" if value else "off")
                    if not value:
                        self.sessions[path] = set()
                        self._discovering(path)

            self.powering.add(path)
            if not self._later(self._call, done):
                done()
            return
        self.set(path, interface, **{name: GLib.Variant(properties[name].get_type_string(), value)})
        invocation.return_value(None)

    def _RegisterAgent(self, sender, _path, arguments, invocation):
        agent_path, capability = arguments
        if sender in self.agents:
            self._error(invocation, "AlreadyExists")
        elif capability not in CAPABILITIES:
            self._error(invocation, "InvalidArguments")
        else:
            self.agents[sender] = (agent_path, capability)
            self.queue.append(sender)            # the first one registered is the default
            invocation.return_value(None)

    def _UnregisterAgent(self, sender, _path, arguments, invocation):
        if self.agents.get(sender, (None,))[0] != arguments[0]:
            self._error(invocation, "DoesNotExist")
            return
        del self.agents[sender]
        self.queue.remove(sender)
        invocation.return_value(None)

    def _RequestDefaultAgent(self, sender, _path, arguments, invocation):
        if self.agents.get(sender, (None,))[0] != arguments[0]:
            self._error(invocation, "DoesNotExist")
            return
        self.queue.remove(sender)
        self.queue.insert(0, sender)
        invocation.return_value(None)

    def _powered(self, adapter) -> bool:
        return bool(self.value(adapter, ADAPTER, "Powered"))

    def _StartDiscovery(self, sender, path, _arguments, invocation):
        if not self._powered(path):
            self._error(invocation, "NotReady")
        elif sender in self.sessions[path]:      # searching, starting or still stopping: BlueZ says busy
            self._error(invocation, "InProgress", "Operation already in progress")
        else:
            self.sessions[path].add(sender)
            self.starting.add((path, sender))

            def done():
                self.starting.discard((path, sender))
                if sender in self.sessions.get(path, ()):
                    self._discovering(path)
                invocation.return_value(None)

            if not self._later(self._call, done):
                done()

    def _StopDiscovery(self, sender, path, _arguments, invocation):
        if not self._powered(path):
            self._error(invocation, "NotReady")
        elif sender not in self.sessions[path]:
            self._error(invocation, "Failed", "No discovery started")
        elif (path, sender) in self.starting or (path, sender) in self.stopping:
            self._error(invocation, "InProgress", "Operation already in progress")
        else:
            self.stopping.add((path, sender))

            def done():
                self.stopping.discard((path, sender))
                self.sessions.get(path, set()).discard(sender)
                self._discovering(path)
                invocation.return_value(None)

            if not self._later(self._call, done):
                done()

    def _RemoveDevice(self, _sender, path, arguments, invocation):
        device = arguments[0]
        if not device.startswith(path + "/dev_") or device not in self.objects:
            self._error(invocation, "DoesNotExist", "Does Not Exist")
        elif not self._powered(path):
            self._error(invocation, "NotReady")
        else:
            self.remove_device(device)
            invocation.return_value(None)

    def _Connect(self, _sender, path, _arguments, invocation):
        if path in self.profiles:
            invocation.return_value(None)        # all connected already: BlueZ 5.87 answers success
            return

        def done():
            if self.connecting.get(path) is not invocation:
                return                           # a Disconnect answered it meanwhile
            del self.connecting[path]
            if path in self.objects:
                self.profiles.add(path)
                if not self.value(path, DEVICE, "Connected"):
                    self.set(path, DEVICE, Connected=True, ServicesResolved=True)
                invocation.return_value(None)
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownObject", "no such object")

        self.connecting[path] = invocation
        if not self._later(self._call, done):
            done()

    def _Disconnect(self, _sender, path, _arguments, invocation):
        waiting = self.connecting.pop(path, None)
        if waiting is not None:                  # a Connect that waits is answered first
            waiting.return_dbus_error(ERROR + "Failed", "br-connection-canceled")
        self.profiles.discard(path)
        if self.value(path, DEVICE, "Connected"):
            self.set(path, DEVICE, Connected=False, ServicesResolved=False)
        invocation.return_value(None)            # not connected: BlueZ 5.87 answers success too

    # ---- pairing -----------------------------------------------------------
    def _Pair(self, sender, path, _arguments, invocation):
        if path not in self.objects:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownObject", "no such device")
        elif self.value(path, DEVICE, "Paired"):
            self._error(invocation, "AlreadyExists", "Already Paired")
        elif path in self.pairings:
            self._error(invocation, "InProgress", "In Progress")
        else:
            agent = self.agent_of(sender) or self.default_agent()      # the caller's own agent first
            if agent is None:
                self._error(invocation, "AuthenticationFailed")
                return
            deferred = self.defer is not None and self.defer(self._call)
            pairing = Pairing(path, sender, invocation, agent, list(self.scripts.get(path, [])), deferred)
            self.pairings[path] = pairing
            if self.link_on_pair and not self.value(path, DEVICE, "Connected"):
                self.set(path, DEVICE, Connected=True)
            self._next(pairing)

    def _CancelPairing(self, _sender, path, _arguments, invocation):
        pairing = self.pairings.get(path)
        if pairing is not None and not pairing.bonded:
            if pairing.waiting is not None and not pairing.waiting.done:
                self.ask("Cancel", pairing=pairing)
            self.cancelled.append(path)
            self._fail_pair(pairing, ERROR + "AuthenticationCanceled", "Authentication Canceled")
            invocation.return_value(None)
            return
        # BlueZ 5.87 cancel_pairing() without a pairing request: btd_adapter_remove_bonding(), then DoesNotExist.
        # The kernel's unpair_device() removes the keys and then drops the link; a device without keys gets
        # NOT_PAIRED before that, and keeps its link.
        if path in self.objects and self.value(path, DEVICE, "Bonded"):
            self.unpaired.append(path)          # the keys are gone; BlueZ still says Paired and Bonded
            self.profiles.discard(path)
            if self.value(path, DEVICE, "Connected"):
                self.set(path, DEVICE, Connected=False, ServicesResolved=False)
        self._error(invocation, "DoesNotExist", "Does Not Exist")

    def ask(self, member, *arguments, pairing: Pairing | None = None, then=None) -> AgentCall:
        """Ask the agent of a pairing (the only one running, by default) as BlueZ would. Before ``Cancel`` and
        ``Release`` BlueZ gives up the request that waits, so its answer no longer counts."""
        pairing = pairing or next(iter(self.pairings.values()))
        if member in ("Release", "Cancel"):
            pairing.waiting = None
        full = arguments if member in ("Release", "Cancel") else (pairing.device, *arguments)
        record = call_agent(self.connection, *pairing.agent, member, full, then)
        self.asked.append((pairing.agent, record))
        return record

    def _next(self, pairing: Pairing):
        if self.pairings.get(pairing.device) is not pairing or pairing.paused:
            return
        if not pairing.steps:
            self._bond(pairing)
            if pairing.deferred:
                self.deferred.append(("Pair", lambda: self._complete(pairing)))
            else:
                self._complete(pairing)
            return
        step = pairing.steps.pop(0)
        if step == WAIT:
            pairing.paused = True
        elif step == BOND:
            self._bond(pairing)
            self._next(pairing)
        elif step[0] == FAIL:
            self._fail_pair(pairing, step[1], "scripted")
        elif step[0] in ("Release", "Cancel", "DisplayPasskey"):     # BlueZ wants no answer to these
            self.ask(*step, pairing=pairing)
            self._next(pairing)
        else:
            pairing.waiting = self.ask(*step, pairing=pairing, then=lambda record: self._answered(pairing, record))

    def _answered(self, pairing: Pairing, record: AgentCall):
        if self.pairings.get(pairing.device) is not pairing or pairing.waiting is not record:
            return
        if record.error:                         # BlueZ refuses the pairing to the device, which then fails
            self._fail_pair(pairing, ERROR + "AuthenticationFailed", "Authentication Failed")
        else:
            self._next(pairing)

    def resume(self, device=None):
        pairing = self.pairings[device] if device else next(iter(self.pairings.values()))
        pairing.paused = False
        self._next(pairing)

    def finish_pair(self, device, error: str | None = None):
        """End the running pairing of ``device``: paired, or failed with ``error``."""
        pairing = self.pairings.get(device)
        if pairing is None:
            return
        if error is not None:
            self._fail_pair(pairing, error, "scripted")
            return
        self._bond(pairing)
        self._complete(pairing)

    def _bond(self, pairing: Pairing):
        """The keys are made and stored: BlueZ announces Bonded at once, and its pairing request is over."""
        if not pairing.bonded:
            pairing.bonded = True
            self.set(pairing.device, DEVICE, Bonded=True)

    def _complete(self, pairing: Pairing):
        """Service discovery is over: Paired is announced, then Pair() is answered."""
        if self.pairings.get(pairing.device) is not pairing:
            return
        del self.pairings[pairing.device]
        self.set(pairing.device, DEVICE, Paired=True)
        pairing.invocation.return_value(None)

    def _fail_pair(self, pairing: Pairing, error: str, message: str):
        if self.pairings.get(pairing.device) is pairing:
            del self.pairings[pairing.device]
            pairing.invocation.return_dbus_error(error, message)

    # ---- the log -----------------------------------------------------------
    def members(self, sender=None) -> list[tuple]:
        return [(interface, member) for who, _path, interface, member, _arguments in self.calls
                if sender is None or who == sender]

    def count(self, member, sender=None) -> int:
        return sum(1 for who, _path, _interface, name, _arguments in self.calls
                   if name == member and (sender is None or who == sender))

    def last(self, member):
        return next(call for call in reversed(self.calls) if call[3] == member)


class StandInAgent:
    """Another program's agent, registered before Gaming Home's one: what ``bt-agent`` is on Omarchy
    (``NoInputNoOutput``, accepting). It records every call that reaches it."""

    def __init__(self, bus, path="/org/bluez/standin", capability="NoInputNoOutput"):
        self.connection = bus.connect()
        self.unique_name = self.connection.get_unique_name()
        self.path, self.capability = path, capability
        self.calls: list[tuple] = []
        self.registered = None
        self.connection.register_object(path, AGENT_INFO, self._on_call, None, None)

    def register(self):
        def done(bus, result, _data):
            try:
                bus.call_finish(result)
                self.registered = True
            except GLib.Error:
                self.registered = False
        self.connection.call(BUS_NAME, "/org/bluez", AGENT_MANAGER, "RegisterAgent",
                             GLib.Variant("(os)", (self.path, self.capability)), None, Gio.DBusCallFlags.NONE, 3000,
                             None, done, None)

    def _on_call(self, _connection, sender, _path, _interface, member, parameters, invocation):
        self.calls.append((sender, member, parameters.unpack()))
        reply = {"RequestPinCode": GLib.Variant("(s)", ("0000",)), "RequestPasskey": GLib.Variant("(u)", (0,))}
        invocation.return_value(reply.get(member))


class Clock(Timers):
    """``Timers`` that also know each timer's delay, so that a test fires the one it means."""

    def __init__(self):
        super().__init__()
        self.delay_of: dict[int, int] = {}

    def schedule(self, milliseconds, function):
        token = super().schedule(milliseconds, function)
        self.delay_of[token] = milliseconds
        return token

    def delays_pending(self) -> list[int]:
        return sorted(self.delay_of[token] for token in self.pending)

    def armed(self, milliseconds) -> bool:
        return milliseconds in self.delays_pending()

    def fire_after(self, milliseconds):
        """Fire the one pending timer with this delay."""
        token, = [token for token in self.pending if self.delay_of[token] == milliseconds]
        function = self.pending.pop(token)
        function()

    def settle(self, longest=200):
        """Fire every pending timer of at most ``longest`` ms, including the ones that firing arms."""
        for _ in range(100):
            due = [token for token in self.pending if self.delay_of[token] <= longest]
            if not due:
                return
            self.pending.pop(due[0])()
        raise AssertionError("timers keep arming each other")


@dataclass
class Changes:
    """Counts ``on_change`` calls and keeps the snapshot each one saw."""

    source: object = None
    seen: list = field(default_factory=list)

    def __call__(self):
        self.seen.append(self.source.snapshot if self.source is not None else None)

    def __len__(self):
        return len(self.seen)
