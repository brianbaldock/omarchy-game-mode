"""The Bluetooth source of Gaming Home: a client of BlueZ, the Bluetooth service, on the system bus.

``BlueZ(on_change)`` behaves as the contract in ``bluetooth.py`` describes, so the class itself is a
``bluetooth.BluetoothFactory``. It holds a D-Bus connection of its own from ``start()`` to ``stop()``: BlueZ ties a
discovery session and a pairing agent to the connection that asked for them, so closing it at ``stop()`` releases
both, even when a reply is still on its way. Nothing of it exists outside ``start()`` and ``stop()``.

What it sends to BlueZ, and when
--------------------------------
Every call goes through one method, ``_call``, which refuses anything not in ``CALLS`` or ``WRITES``. Each call is
addressed to the unique name that owns ``org.bluez`` at that moment (never to the well-known name), carries
``NO_AUTO_START`` and has its own timeout: ``CALL_TIMEOUT_MS`` unless ``TIMEOUTS`` says otherwise.

* ``start()``: watches the name ``org.bluez`` without starting the service. While somebody owns it, the client
  subscribes to that owner's ``InterfacesAdded``, ``InterfacesRemoved`` and ``PropertiesChanged`` and calls
  ``ObjectManager.GetManagedObjects`` once. It reads again only for a new owner (BlueZ restarted), when another
  adapter becomes the lowest-numbered one, and on ``retry()`` after a read failed. Nothing else is sent before the
  user asks for something.
* ``scan(True)``: ``Adapter1.StartDiscovery``, and ``Adapter1.StopDiscovery`` when the search ends (``scan(False)``,
  ``SCAN_SECONDS`` later, or ``pair``). BlueZ refuses a stop while the start is unanswered, so the stop waits for
  that answer; it is not sent when the adapter went off or away or the connection is being closed, which end the
  search anyway. No discovery filter is ever set: any filter removes BlueZ's default RSSI threshold, so that every
  change of signal strength becomes a message, and makes BlueZ list devices that cannot be connected to.
* ``pair(device)``: ``AgentManager1.RegisterAgent`` for the agent of ``bluezagent.py`` (capability
  ``KeyboardDisplay``), ``Device1.Pair`` (``PAIR_TIMEOUT_MS``: one agent request alone may take a minute), then
  ``Properties.Set`` of the device's ``Trusted`` to true and ``Device1.Connect``. ``AgentManager1.UnregisterAgent`` as
  soon as the pairing step is over, however it ended. ``Device1.CancelPairing`` when the user cancels or the pairing
  times out, but only before the bond is made. BlueZ announces ``Bonded`` when it stores the keys and then looks up
  the device's services before it answers ``Pair()``; its pairing request is over by then, and BlueZ 5.87 answers a
  ``CancelPairing`` without one by removing the new keys. So once the device is bonded the client sends nothing,
  waits for ``Pair()`` (at most ``CALL_TIMEOUT_MS`` after a cancel) and reports ``CANCELED`` or ``TIMEOUT`` with
  ``paired=True``: the bond stays and nothing more is done to the device. One race remains: a cancel in the few
  milliseconds between BlueZ storing the keys and this client hearing ``Bonded`` still sends ``CancelPairing``; BlueZ
  then removes the keys but keeps showing the device as paired, and it has to be forgotten and paired again.
  A device or adapter that goes away during a pairing: BlueZ answers ``Pair()`` before it announces the removal,
  ``AuthenticationCanceled`` while the link is up (``AUTH_FAILED``) and ``ConnectionAttemptFailed`` while the device
  is still being reached (``UNREACHABLE``). Once the bond is made it drops that answer instead, and the removal ends
  the pairing with ``GONE``, as it ends any operation whose call still waits.
* ``connect(device)``: ``Device1.Connect`` (``CONNECT_TIMEOUT_MS``), and ``Device1.Disconnect`` to abandon it when it
  times out or the user cancels. It never changes trust.
* ``disconnect(device)``: ``Device1.Disconnect``.
* ``forget(device)``: ``Adapter1.RemoveDevice``, which disconnects the device as needed.
* ``set_power(on)``: Omarchy's ``omarchy-bluetooth-power on|off`` when it is installed, then ``Properties.Set`` of
  the adapter's ``Powered`` only when the adapter did not follow (or when the command is not installed).

Never sent: ``SetDiscoveryFilter``, ``RequestDefaultAgent``, ``ConnectDevice``, and no other property write
(``Discoverable``, ``Pairable``, ``Alias``, ``Blocked``, ``WakeAllowed``, ``PreferredBearer``, ...). What an operation
did is read back from BlueZ's signals, never assumed from a reply.

The pairing agent
-----------------
The agent exists from the press of Pair to the end of the pairing step, on this client's connection, and answers
only BlueZ and only about that device (``bluezagent.py`` says how). BlueZ asks the agent a program registered about
the pairings that program starts, and its default agent about everything else: pairings that a device starts and
services that ask to connect. Omarchy runs ``bt-agent`` (from bluez-tools, ``NoInputNoOutput``, accepting) as that
default agent, so the agent registered here queues behind it, answers the pairing Gaming Home started and leaves
everything else to it. That is what lets Gaming Home pair a device that needs a code, such as a keyboard's passkey or
a number to compare. Where no other agent is registered, BlueZ makes the first one registered the default: then, for
the seconds of a pairing, this agent is the default one and refuses a pairing that another device starts meanwhile.

Power
-----
``omarchy-bluetooth-power`` switches every Bluetooth radio of the machine through rfkill and keeps the choice across
reboots. It runs as an argument list without a shell, with a minimal environment and its output silenced and never
read. After ``POWER_COMMAND_TIMEOUT_MS`` (it may legitimately take about ten seconds) only the command itself, a
shell script, is killed: what it started (``bluetoothctl`` under ``timeout``, ``sleep``) ends by its own time limits
within a few seconds. Killing a process group would not reach further, since GNU ``timeout`` moves into a group of
its own. Whatever the exit status, the selected adapter is checked afterwards, and its ``Powered`` is set once
directly when it did not follow; when BlueZ answers that write ``Busy``, a change is under way already and is waited
for. ``BLOCKED`` is reported only when the adapter says ``off-blocked`` or the write was refused because of rfkill. A
command that still runs at ``stop()`` is left to finish on its own.

Timers and the rest
-------------------
Nothing polls. A timer exists only while it has something to do: the notification coalescer while a change waits to
be reported (``COALESCE_MS``, or at once for what the user did), the search limit while a search runs, one timeout
while an operation runs, and the power command's. ``idle`` says that there is none and no call in flight. Replies,
signals and timers that come after ``stop()``, or from an owner that is gone, are dropped by an epoch counter.
Replies are bounded (``MAX_REPLY_BYTES``), at most ``MAX_TRACKED`` devices are tracked, and every property is
type-checked as it is read. Device ids count up and are never reused. Object paths, addresses, error texts and codes
never leave this module, and nothing is logged.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
from dataclasses import dataclass

from gi.repository import Gio, GLib

from . import APP_ID, bluetooth
from .bluetooth import (ABSENT, AUTH_FAILED, BLOCKED, CANCELED, CONNECTING, DISCONNECTING, ERROR, FAILED,
                        FORGETTING, GONE, IN_PROGRESS, MAX_DEVICES, NO_ADAPTER, NOT_READY, OFF, ON, PAIRING,
                        POWERING, SCAN_SECONDS, SEARCHING, STAGE_CONNECT, STAGE_PAIR, STAGE_TRUST, TIMEOUT, UNAVAILABLE,
                        UNKNOWN, UNREACHABLE, UNSUPPORTED, Activity, Failure, Prompt, Snapshot)
from .bluezagent import CAPABILITY, PairingAgent

BUS_NAME = "org.bluez"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
BATTERY = "org.bluez.Battery1"
AGENT_MANAGER = "org.bluez.AgentManager1"
AGENT_MANAGER_PATH = "/org/bluez"
AGENT_ROOT = "/" + APP_ID.replace(".", "/")       # the agents' object paths are under this one
POWER_COMMAND = "omarchy-bluetooth-power"

CALL_TIMEOUT_MS = 10_000
PAIR_TIMEOUT_MS = 120_000          # BlueZ gives an agent request 60 s, and pairing also connects and looks up services
CONNECT_TIMEOUT_MS = 40_000
POWER_COMMAND_TIMEOUT_MS = 15_000  # the command waits for the adapter and may take about 10 s
COALESCE_MS = 200
MAX_REPLY_BYTES = 4 * 1024 * 1024
MAX_TRACKED = 256                  # devices of the adapter that are tracked; more only set ``truncated``
MAX_ADAPTERS = 16
MAX_TEXT = 1024                    # how much of a name is kept before ``display_name`` cuts it

# Every call this client makes. ``_call`` refuses anything else.
CALLS = frozenset({
    (OBJECT_MANAGER, "GetManagedObjects"),
    (ADAPTER, "StartDiscovery"), (ADAPTER, "StopDiscovery"), (ADAPTER, "RemoveDevice"),
    (DEVICE, "Pair"), (DEVICE, "CancelPairing"), (DEVICE, "Connect"), (DEVICE, "Disconnect"),
    (AGENT_MANAGER, "RegisterAgent"), (AGENT_MANAGER, "UnregisterAgent"),
})
# Every property this client may write (``Properties.Set``), always a boolean.
WRITES = frozenset({(ADAPTER, "Powered"), (DEVICE, "Trusted")})
TIMEOUTS = {(DEVICE, "Pair"): PAIR_TIMEOUT_MS, (DEVICE, "Connect"): CONNECT_TIMEOUT_MS}
# What the power command gets of the environment, and nothing else.
POWER_ENVIRONMENT = ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "DBUS_SYSTEM_BUS_ADDRESS")

# What an error from BlueZ (or from the bus) means for the user, by the last part of its name. ``GONE`` applies to a
# call about a device or an adapter; for anything else those names mean ``FAILED``.
ERRORS = {
    "AuthenticationFailed": AUTH_FAILED,
    "AuthenticationRejected": AUTH_FAILED,
    "AuthenticationCanceled": AUTH_FAILED,       # CANCELED when the user cancelled: see ``failure_code``
    "AuthenticationTimeout": TIMEOUT,
    "ConnectionAttemptFailed": UNREACHABLE,
    "InProgress": IN_PROGRESS,
    "Busy": IN_PROGRESS,                         # a write of a setting while BlueZ still makes an earlier one
    "Blocked": BLOCKED,                          # BlueZ 5.87's answer to powering an adapter rfkill blocks
    "NotReady": NOT_READY,
    "NotSupported": UNSUPPORTED,
    "ProfileUnavailable": UNSUPPORTED,           # org.bluez.Error.BREDR.ProfileUnavailable
    "DoesNotExist": GONE,
    "UnknownObject": GONE,
    "UnknownMethod": GONE,                       # what a call to an object path that is gone may get
    "InvalidArguments": GONE,
    "InvalidArgs": GONE,
    "AccessDenied": UNAVAILABLE,
    "NotAuthorized": UNAVAILABLE,
    "PermissionDenied": UNAVAILABLE,
    "ServiceUnknown": UNAVAILABLE,
    "NameHasNoOwner": UNAVAILABLE,
    "Disconnected": UNAVAILABLE,
    "NoReply": TIMEOUT,
    "Timeout": TIMEOUT,
    "TimedOut": TIMEOUT,
}
# The short reason of a ``Failed`` error is not documented API: these words are looked for loosely, first match wins.
# In BlueZ 5.87 (src/error.h) a device that does not answer gives ``br-connection-page-timeout``,
# ``br-connection-timeout`` or ``le-connection-timeout``, and an LE connection the kernel gave up on
# ``le-connection-abort-by-local``; ``*-key-missing`` is a device that forgot the pairing. BR/EDR's
# ``br-connection-aborted-by-local`` (spelled differently) is a local termination for other reasons: FAILED.
REASONS = (("page-timeout", UNREACHABLE), ("page timeout", UNREACHABLE), ("connection-timeout", UNREACHABLE),
           ("abort-by-local", UNREACHABLE), ("host is down", UNREACHABLE), ("key-missing", AUTH_FAILED),
           ("rfkill", BLOCKED), ("blocked", BLOCKED))

# Icon names BlueZ gives devices, and what the window draws for them.
ICONS = {"audio-headset": "headset", "audio-headphones": "headphones", "audio-card": "speaker",
         "input-gaming": "gamepad", "input-keyboard": "keyboard", "input-mouse": "mouse", "phone": "phone",
         "computer": "computer"}

IDLE = Snapshot()
READ_DONE, READ_ERROR = "done", "error"
COMMAND, WRITE, VERIFY = "command", "write", "verify"     # the steps of POWERING

_ADAPTER_PATH = re.compile(r"/org/bluez/hci([0-9]{1,4})")
_DEVICE_PATH = re.compile(r"/org/bluez/hci([0-9]{1,4})/dev_[0-9A-Za-z_]{1,64}")
_TYPES = {signature: GLib.VariantType.new(signature) for signature in ("s", "b", "u", "q", "y", "a{sv}", "a{sa{sv}}")}


# ---- pure helpers ------------------------------------------------------------------------------------------------
def allowed(interface: str, member: str, parameters: GLib.Variant | None = None) -> bool:
    """Whether this client may send this call: one of ``CALLS``, or a boolean written to one of ``WRITES``."""
    if (interface, member) == (PROPERTIES, "Set"):
        if not isinstance(parameters, GLib.Variant) or parameters.get_type_string() != "(ssv)":
            return False
        target = (parameters.get_child_value(0).get_string(), parameters.get_child_value(1).get_string())
        return target in WRITES and parameters.get_child_value(2).get_variant().get_type_string() == "b"
    return (interface, member) in CALLS


def failure_code(name: str, reason: str = "", *, cancelled: bool = False, about_object: bool = True) -> str:
    """The one ``bluetooth`` failure code for an error, from its D-Bus name and short reason.

    ``cancelled``: the user cancelled the attempt this error ends, which makes any error ``CANCELED``.
    ``about_object``: the call was about a device or an adapter, so that "no such object" means it is gone.
    """
    if cancelled:
        return CANCELED
    short = (name or "").rsplit(".", 1)[-1]
    if short == "Failed":
        reason = (reason or "").casefold()
        return next((code for word, code in REASONS if word in reason), FAILED)
    code = ERRORS.get(short, FAILED)
    return FAILED if code == GONE and not about_object else code


def error_parts(error) -> tuple[str, str]:
    """The D-Bus name of an error (empty for one that did not come over the bus) and its reason, for matching only."""
    try:
        name = Gio.DBusError.get_remote_error(error) or ""
    except (TypeError, ValueError):
        name = ""
    message = str(getattr(error, "message", "") or "")
    prefix = f"GDBus.Error:{name}: "
    if name and message.startswith(prefix):
        message = message[len(prefix):]
    return name, message[:MAX_TEXT]


def short_name(error) -> str:
    return error_parts(error)[0].rsplit(".", 1)[-1]


def timed_out(error) -> bool:
    """Nothing answered the call in time."""
    return isinstance(error, GLib.Error) and error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT)


def code_of(error, *, cancelled: bool = False, about_object: bool = True) -> str:
    if cancelled:
        return CANCELED
    if timed_out(error):
        return TIMEOUT
    if isinstance(error, GLib.Error) and error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CLOSED):
        return UNAVAILABLE
    name, reason = error_parts(error)
    return failure_code(name, reason, about_object=about_object)


def kind_of(icon: str = "", device_class: int | None = None, appearance: int | None = None) -> str:
    """What a device is, for its icon: one of ``bluetooth.KINDS``.

    BlueZ picks ``Icon`` from the device's class or appearance itself, so that decides whenever it is there. Only a
    device without one is looked at more closely: the major class of its Class of Device (computer, phone, the
    headsets, headphones and loudspeakers among audio devices, and the keyboards, mice, joysticks and gamepads among
    peripherals), then the category of its appearance (phone, computer, and the keyboards, mice,
    joysticks and gamepads among human interface devices). Anything else is ``other``.
    """
    if icon:
        return ICONS.get(icon, "other")
    if isinstance(device_class, int) and not isinstance(device_class, bool):
        kind = _kind_of_class(device_class)
        if kind:
            return kind
    if isinstance(appearance, int) and not isinstance(appearance, bool):
        kind = _kind_of_appearance(appearance)
        if kind:
            return kind
    return "other"


def _kind_of_class(device_class: int) -> str | None:
    major, minor = (device_class >> 8) & 0x1F, (device_class >> 2) & 0x3F
    if major == 1:
        return "computer"
    if major == 2:
        return "phone"
    if major == 4:                                  # audio and video
        return {1: "headset", 2: "headset", 5: "speaker", 6: "headphones"}.get(minor)
    if major == 5:                                  # peripheral: keyboard and pointing bits, then the subtype
        pointing, subtype = minor >> 4, minor & 0x0F
        if pointing == 0 and subtype in (1, 2):
            return "gamepad"
        if pointing == 2 and subtype == 5:
            return None                             # a digitizer tablet
        return {1: "keyboard", 2: "mouse"}.get(pointing)
    return None


def _kind_of_appearance(appearance: int) -> str | None:
    category, subcategory = appearance >> 6, appearance & 0x3F
    if category == 1:
        return "phone"
    if category == 2:
        return "computer"
    if category == 0x0F:                            # human interface device
        return {1: "keyboard", 2: "mouse", 3: "gamepad", 4: "gamepad"}.get(subcategory)
    return None


def state_of(name_known: bool, owner: str | None, read: str, powered: bool | None) -> str:
    """The adapter state the window shows. ``read`` is ``READ_DONE``, ``READ_ERROR`` or empty while nothing came
    back yet; ``powered`` is the selected adapter's ``Powered``, None when there is no adapter."""
    if not name_known:
        return UNKNOWN
    if owner is None:
        return ABSENT
    if read == READ_ERROR:
        return ERROR
    if read != READ_DONE:
        return UNKNOWN
    if powered is None:
        return NO_ADAPTER
    return ON if powered else OFF


def adapter_index(path: str) -> int | None:
    match = _ADAPTER_PATH.fullmatch(path or "")
    return int(match.group(1)) if match else None


def device_adapter(path: str) -> int | None:
    """The number of the adapter a device object belongs to, None for any other object (a battery of a device too)."""
    match = _DEVICE_PATH.fullmatch(path or "")
    return int(match.group(1)) if match else None


def section(interfaces: GLib.Variant | None, interface: str) -> GLib.Variant | None:
    """The properties of one interface out of an ``a{sa{sv}}``, or None."""
    if not isinstance(interfaces, GLib.Variant) or not interfaces.is_of_type(_TYPES["a{sa{sv}}"]):
        return None
    return interfaces.lookup_value(interface, _TYPES["a{sv}"])


def prop(properties: GLib.Variant | None, name: str, signature: str):
    """One property out of an ``a{sv}``, or None when it is missing or of another type. Never raises.

    Strings are cut to ``MAX_TEXT`` characters."""
    if not isinstance(properties, GLib.Variant) or not properties.is_of_type(_TYPES["a{sv}"]):
        return None
    value = properties.lookup_value(name, _TYPES[signature])
    if value is None:
        return None
    if signature == "s":
        return value.get_string()[:MAX_TEXT]
    return value.unpack()


def present(properties: GLib.Variant | None, name: str) -> bool:
    return (isinstance(properties, GLib.Variant) and properties.is_of_type(_TYPES["a{sv}"])
            and properties.lookup_value(name, None) is not None)


def power_environment(environ=None) -> list[str]:
    """The environment of the power command: ``POWER_ENVIRONMENT`` taken from ours, as far as it is set."""
    environ = os.environ if environ is None else environ
    return [f"{key}={environ[key]}" for key in POWER_ENVIRONMENT if environ.get(key)]


def run_power_command(argv: list[str], environment: list[str]) -> Gio.Subprocess:
    """Start the power command: no shell, stdin from /dev/null, stdout and stderr thrown away."""
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
    launcher.set_environ(environment)
    return launcher.spawnv(argv)


def find_power_command() -> str | None:
    return shutil.which(POWER_COMMAND)


def _timeout_add(milliseconds, callback):
    def fire():
        callback()
        return GLib.SOURCE_REMOVE
    return GLib.timeout_add(milliseconds, fire)


def _guarded(method):
    """Public methods and callbacks from the GLib loop: no exception ever leaves them."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as error:  # noqa: BLE001
            self._broke(error)
            return None
    return wrapper


# ---- what is known about BlueZ ---------------------------------------------------------------------------------
@dataclass(eq=False)
class _Adapter:
    path: str
    powered: bool = False
    power_state: str = ""          # BlueZ's PowerState, empty when it has none

    def apply(self, properties, full: bool, invalidated=frozenset()) -> bool:
        before = (self.powered, self.power_state)
        for name, signature, attribute, default in (("Powered", "b", "powered", False),
                                                    ("PowerState", "s", "power_state", "")):
            value = prop(properties, name, signature)
            if value is None and not full and name not in invalidated and not present(properties, name):
                continue
            setattr(self, attribute, default if value is None else value)
        return before != (self.powered, self.power_state)


# Device1 properties the model uses: (name, D-Bus type, attribute, value when missing or of another type).
_DEVICE_FIELDS = (("Alias", "s", "alias", ""), ("Name", "s", "name", ""), ("Icon", "s", "icon", ""),
                  ("Class", "u", "device_class", None), ("Appearance", "q", "appearance", None),
                  ("Paired", "b", "paired", False), ("Bonded", "b", "bonded", False),
                  ("Trusted", "b", "trusted", False), ("Connected", "b", "connected", False),
                  ("Connectable", "b", "connectable", None))


@dataclass(eq=False)
class _Device:
    id: int
    path: str
    alias: str = ""
    name: str = ""
    icon: str = ""
    device_class: int | None = None
    appearance: int | None = None
    paired: bool = False
    bonded: bool = False                    # its keys are stored: BlueZ says so before Paired, see ``cancel``
    trusted: bool = False
    connected: bool = False
    connectable: bool | None = None         # not a Device1 property in BlueZ 5.87; kept for a BlueZ that has one
    battery: int | None = None
    view: bluetooth.Device | None = None    # what the snapshot lists

    def apply(self, properties, full: bool, invalidated=frozenset()) -> bool:
        """Take in properties; True when what the snapshot lists changed. A search brings a stream of changes to
        RSSI and advertising data that the model does not use: those return before anything is computed."""
        changed = self.view is None
        for name, signature, attribute, default in _DEVICE_FIELDS:
            value = prop(properties, name, signature)
            if value is None and not full and name not in invalidated and not present(properties, name):
                continue
            value = default if value is None else value
            if getattr(self, attribute) != value:
                setattr(self, attribute, value)
                changed = True
        return self.refresh() if changed else False

    def apply_battery(self, properties, full: bool, invalidated=frozenset()) -> bool:
        """The device's own Battery1. A battery object below the device (``battery_*``) never gets here."""
        value = prop(properties, "Percentage", "y")
        if value is None and not full and "Percentage" not in invalidated and not present(properties, "Percentage"):
            return False
        value = None if value is None else max(0, min(100, value))
        if value == self.battery and self.view is not None:
            return False
        self.battery = value
        return self.refresh()

    def refresh(self) -> bool:
        view = bluetooth.Device(self.id, bluetooth.display_name(self.alias, self.name),
                                kind_of(self.icon, self.device_class, self.appearance), self.paired, self.trusted,
                                self.connected, self.battery, order=self.id)
        changed, self.view = view != self.view, view
        return changed


@dataclass(eq=False)
class _Operation:
    kind: str                        # CONNECTING ... POWERING
    device: int = 0
    path: str = ""                   # the device's object path
    name: str = ""
    stage: str = ""                  # PAIRING: the STAGE_* under way
    cancelled: bool = False          # the user cancelled it
    cancel_sent: bool = False        # CancelPairing went out
    disconnect_sent: bool = False    # Disconnect went out to abandon a Connect
    agent: PairingAgent | None = None
    registering: bool = False        # RegisterAgent went out, so UnregisterAgent follows
    pair_sent: bool = False
    paired: bool = False             # the pairing step succeeded
    want: bool = False               # POWERING: on or off
    step: str = ""                   # POWERING: COMMAND, WRITE or VERIFY
    process: object = None           # POWERING: the power command while it runs
    write_code: str = ""             # POWERING: what the direct write's error meant
    timed_out: bool = False          # POWERING: one of its timers fired


class BlueZ:
    """The Bluetooth source of the window, talking to bluetoothd. The module documentation says what it sends.

    The keyword arguments replace parts of the system in tests: ``address`` of the bus (the system bus by default),
    ``schedule(milliseconds, callback) -> token`` and ``cancel(token)`` for timers, and how the power command is
    found (``find_power_command() -> path or None``) and run (``run_power_command(argv, environment)``).
    """

    def __init__(self, on_change, *, address: str | None = None, schedule=_timeout_add, cancel=GLib.source_remove,
                 find_power_command=find_power_command, run_power_command=run_power_command):
        self._on_change = on_change
        self._address = address
        self._schedule, self._cancel_timer = schedule, cancel
        self._find_power_command, self._run_power_command = find_power_command, run_power_command
        self.error = ""               # the class name of the last exception a handler or callback raised
        self._ids = 0                 # the last device id given out: never reset, so never reused
        self._prompt_ids = 0
        self._agent_count = 0
        self._run = 0                 # one per start()
        self._epoch = 0               # one per start(), stop() and owner of org.bluez
        self._read_token = 0
        self._running = False
        self._timers: dict[str, tuple] = {}
        self._notify_delay: int | None = None
        self._reset()
        self._snapshot = self._delivered = IDLE
        self._dirty = False

    def _reset(self):
        self._cancellable: Gio.Cancellable | None = None
        self._connection: Gio.DBusConnection | None = None
        self._opening = False
        self._watch = None
        self._name_known = False      # the bus has said whether anybody owns org.bluez
        self._forget()

    def _forget(self):
        """Drop everything an owner of org.bluez said."""
        self._subscriptions: list[int] = []
        self._owner: str | None = None
        self._read = ""
        self._reading = False
        self._in_flight = 0
        self._adapters: dict[int, _Adapter] = {}
        self._adapter: int | None = None      # the adapter whose devices are tracked: the lowest-numbered
        self._devices: dict[str, _Device] = {}
        self._paths: dict[int, str] = {}
        self._overflow = 0
        self._scanning = self._scan_starting = self._scan_active = self._scan_stop = False
        self._scan_stopping = self._scan_restart = False
        self._scan_path = ""
        self._op: _Operation | None = None
        self._prompt: Prompt | None = None
        self._failure: Failure | None = None
        self._dirty = True

    # ---- what the window reads -------------------------------------------------------------------------------------
    @property
    def snapshot(self) -> Snapshot:
        if self._dirty:
            self._dirty = False
            try:
                fresh = self._build()
            except Exception as error:  # noqa: BLE001 - the last good snapshot stays
                self.error = type(error).__name__
                return self._snapshot
            if fresh != self._snapshot:
                self._snapshot = fresh
        return self._snapshot

    @property
    def idle(self) -> bool:
        """No timer, no call in flight and no power command running: the source only listens to BlueZ."""
        return (not self._timers and self._in_flight == 0 and not self._opening
                and (self._op is None or self._op.process is None))

    # ---- lifecycle -------------------------------------------------------------------------------------------------
    @_guarded
    def start(self):
        if self._running:
            return
        self._running = True
        self._run += 1
        self._epoch += 1
        self._cancellable = Gio.Cancellable()
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        try:
            address = self._address or Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
            Gio.DBusConnection.new_for_address(address, flags, None, self._cancellable, self._on_opened, self._run)
        except (GLib.Error, TypeError, ValueError):
            self._name_known = True             # no system bus: nobody can own org.bluez
            self._changed(urgent=True)
            return
        self._opening = True

    @_guarded
    def stop(self):
        if not self._running:
            return
        self._running = False
        self._run += 1
        self._epoch += 1
        connection, watch, subscriptions, op = self._connection, self._watch, self._subscriptions, self._op
        self._op = None
        try:
            if op is not None and op.agent is not None:
                op.agent.close()                # answers a waiting request Canceled, takes the agent off the bus
            if self._cancellable is not None:
                self._cancellable.cancel()      # every call in flight, the opening and the power command's wait
            self._disarm_all()
            for subscription in subscriptions:
                connection.signal_unsubscribe(subscription)
            if watch is not None:
                Gio.bus_unwatch_name(watch)
            if connection is not None and not connection.is_closed():
                # BlueZ drops this client's discovery session and agent with the connection.
                connection.close(None, None, None)
        finally:
            self._reset()
            self._snapshot = self._delivered = IDLE
            self._dirty = False

    @_guarded
    def _on_opened(self, _source, result, run):
        try:
            connection = Gio.DBusConnection.new_for_address_finish(result)
        except GLib.Error:
            connection = None
        if run != self._run or not self._running:
            if connection is not None and not connection.is_closed():
                connection.close(None, None, None)       # opened for a start() that was stopped meanwhile
            return
        self._opening = False
        if connection is None:
            self._name_known = True
            self._changed(urgent=True)
            return
        connection.set_exit_on_close(False)
        self._connection = connection
        self._watch = Gio.bus_watch_name_on_connection(
            connection, BUS_NAME, Gio.BusNameWatcherFlags.NONE,       # watch only: never start BlueZ
            lambda _bus, _name, owner: self._appeared(run, owner), lambda _bus, _name: self._vanished(run))

    @_guarded
    def _appeared(self, run, owner):
        if run != self._run or not self._running:
            return
        self._drop_owner()
        self._name_known, self._owner = True, owner
        if not Gio.dbus_is_unique_name(owner or ""):
            self._read = READ_ERROR             # a name that cannot be addressed cannot be read
            self._changed(urgent=True)
            return
        epoch = self._epoch
        for interface, member, path, arg0 in ((OBJECT_MANAGER, "InterfacesAdded", "/", None),
                                              (OBJECT_MANAGER, "InterfacesRemoved", "/", None),
                                              (PROPERTIES, "PropertiesChanged", None, ADAPTER),
                                              (PROPERTIES, "PropertiesChanged", None, DEVICE),
                                              (PROPERTIES, "PropertiesChanged", None, BATTERY)):
            self._subscriptions.append(self._connection.signal_subscribe(
                owner, interface, member, path, arg0, Gio.DBusSignalFlags.NONE, self._on_signal, epoch))
        self._read_objects()
        self._changed(urgent=True)

    @_guarded
    def _vanished(self, run):
        if run != self._run or not self._running:
            return
        self._drop_owner()
        self._name_known = True
        self._changed(urgent=True)

    def _drop_owner(self):
        """The owner of org.bluez is gone or replaced: end what ran, unsubscribe, and forget what it said."""
        self._epoch += 1
        if self._op is not None:
            self._end(UNAVAILABLE, unregister=False)    # nobody to unregister with
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = Gio.Cancellable()
        for subscription in self._subscriptions:
            self._connection.signal_unsubscribe(subscription)
        failure = self._failure
        self._disarm("scan")
        self._forget()
        self._failure = failure

    # ---- reading -----------------------------------------------------------------------------------------------
    def _read_objects(self):
        self._read_token += 1
        self._reading = True
        if not self._call("/", OBJECT_MANAGER, "GetManagedObjects", None, "(a{oa{sa{sv}}})", self._on_objects,
                          self._read_token):
            self._read_failed()

    def _on_objects(self, reply, error, token):
        if token != self._read_token:
            return                              # a newer read is on its way
        if error is not None or reply.get_size() > MAX_REPLY_BYTES:
            self._read_failed()
            return
        self._load(reply.get_child_value(0))
        self._reading, self._read = False, READ_DONE
        self._changed(urgent=True)

    def _read_failed(self):
        self._reading, self._read = False, READ_ERROR
        if self._op is not None:
            self._end(FAILED)
        self._end_scan(send=False)
        self._adapters, self._adapter, self._devices, self._paths, self._overflow = {}, None, {}, {}, 0
        self._changed(urgent=True)

    def _load(self, objects: GLib.Variant):
        """Take in a whole ``GetManagedObjects`` answer. Known devices keep their ids."""
        adapters: dict[int, _Adapter] = {}
        devices = []
        for position in range(objects.n_children()):
            entry = objects.get_child_value(position)
            path, interfaces = entry.get_child_value(0).get_string(), entry.get_child_value(1)
            index = adapter_index(path)
            if index is not None:
                properties = section(interfaces, ADAPTER)
                if properties is not None:
                    adapter = _Adapter(path)
                    adapter.apply(properties, full=True)
                    self._keep_adapter(adapters, index, adapter)
            elif device_adapter(path) is not None:
                devices.append((device_adapter(path), path, interfaces))
        self._adapters = adapters
        self._adapter = min(adapters) if adapters else None
        entries = []
        for index, path, interfaces in devices:
            properties = section(interfaces, DEVICE)
            if index == self._adapter and properties is not None:
                entries.append((not prop(properties, "Paired", "b"), path, properties, section(interfaces, BATTERY)))
        entries.sort(key=lambda entry: entry[0])        # remembered devices first, so that they are always tracked
        known, self._devices, self._paths, self._overflow = self._devices, {}, {}, 0
        for _unpaired, path, properties, battery in entries:
            if len(self._devices) >= MAX_TRACKED:
                self._overflow += 1
                continue
            record = known.pop(path, None)
            if record is None:
                record = self._track(path)
            else:
                self._devices[path], self._paths[record.id] = record, path
            record.apply(properties, full=True)
            record.apply_battery(battery, full=True)
        op = self._op
        if op is not None and op.path and op.path not in self._devices and op.kind != FORGETTING:
            self._end(GONE)

    @staticmethod
    def _keep_adapter(adapters: dict, index: int, adapter: _Adapter):
        adapters[index] = adapter
        if len(adapters) > MAX_ADAPTERS:
            del adapters[max(adapters)]

    def _make_room(self, properties: GLib.Variant) -> bool:
        """Crowded: stop tracking the oldest unpaired device, so that one seen now can be tracked. Nameless devices
        go first, since the window never lists them; a remembered or bonded device and the device of the operation
        under way are kept, and a nameless newcomer does not push out a device with a name. False: no room."""
        target = self._op.path if self._op is not None else None
        candidates = [record for record in self._devices.values()
                      if not record.paired and not record.bonded and record.path != target]
        anonymous = [record for record in candidates if not record.view.name]
        named = bluetooth.display_name(prop(properties, "Alias", "s") or "", prop(properties, "Name", "s") or "")
        pool = anonymous or (candidates if named else [])
        if not pool:
            return False
        victim = min(pool, key=lambda record: record.id)
        del self._devices[victim.path]
        self._paths.pop(victim.id, None)
        self._overflow += 1                     # still there, no longer tracked
        return True

    def _track(self, path: str) -> _Device:
        self._ids += 1
        record = _Device(self._ids, path)
        self._devices[path], self._paths[record.id] = record, path
        return record

    def _record(self, device) -> _Device | None:
        if type(device) is not int:
            return None
        path = self._paths.get(device)
        return self._devices.get(path) if path is not None else None

    def _selected(self) -> _Adapter | None:
        return self._adapters.get(self._adapter) if self._adapter is not None else None

    def _state(self) -> str:
        if not self._running:
            return UNKNOWN
        adapter = self._selected()
        return state_of(self._name_known, self._owner, self._read, None if adapter is None else adapter.powered)

    # ---- signals -----------------------------------------------------------------------------------------------
    @_guarded
    def _on_signal(self, _connection, sender, path, _interface, member, parameters, epoch):
        if epoch != self._epoch or not self._running or self._owner is None or sender != self._owner:
            return
        if self._read != READ_DONE or parameters is None or parameters.get_size() > MAX_REPLY_BYTES:
            return                              # nothing read to apply it to, or too large to be BlueZ's
        signature = parameters.get_type_string()
        if member == "InterfacesAdded" and signature == "(oa{sa{sv}})":
            self._interfaces_added(parameters.get_child_value(0).get_string(), parameters.get_child_value(1))
        elif member == "InterfacesRemoved" and signature == "(oas)":
            self._interfaces_removed(parameters.get_child_value(0).get_string(),
                                     _strings(parameters.get_child_value(1)))
        elif member == "PropertiesChanged" and signature == "(sa{sv}as)":
            self._properties_changed(path, parameters.get_child_value(0).get_string(), parameters.get_child_value(1),
                                     _strings(parameters.get_child_value(2)))

    def _interfaces_added(self, path: str, interfaces: GLib.Variant):
        index = adapter_index(path)
        if index is not None:
            properties = section(interfaces, ADAPTER)
            if properties is not None:
                adapter = self._adapters.get(index) or _Adapter(path)
                adapter.apply(properties, full=True)
                self._keep_adapter(self._adapters, index, adapter)
                self._adapters_changed()
            return
        if device_adapter(path) != self._adapter or self._adapter is None:
            return
        device, battery = section(interfaces, DEVICE), section(interfaces, BATTERY)
        record = self._devices.get(path)
        if record is None:
            if device is None:
                return                          # another interface of a device that is not tracked
            if len(self._devices) >= MAX_TRACKED and not self._make_room(device):
                self._overflow += 1
                self._changed()
                return
            record = self._track(path)
        changed = record.apply(device, full=True) if device is not None else False
        if battery is not None:
            changed = record.apply_battery(battery, full=True) or changed
        if changed:
            self._changed()

    def _interfaces_removed(self, path: str, interfaces: frozenset):
        index = adapter_index(path)
        if index is not None:
            if ADAPTER in interfaces and index in self._adapters:
                del self._adapters[index]
                self._adapters_changed()
            return
        if device_adapter(path) != self._adapter or self._adapter is None:
            return
        record = self._devices.get(path)
        if record is None:
            if DEVICE in interfaces and self._overflow:
                self._overflow -= 1
                self._changed()
        elif DEVICE in interfaces:
            self._untrack(record)
        elif BATTERY in interfaces and record.battery is not None:
            record.battery = None
            record.refresh()
            self._changed()

    def _properties_changed(self, path: str, interface: str, changed: GLib.Variant, invalidated: frozenset):
        if interface == ADAPTER:
            index = adapter_index(path)
            adapter = self._adapters.get(index) if index is not None else None
            if adapter is not None and adapter.apply(changed, False, invalidated) and index == self._adapter:
                self._adapter_changed()
            return
        record = self._devices.get(path)
        if record is None:
            return
        if interface == DEVICE and record.apply(changed, False, invalidated):
            self._changed()
        elif interface == BATTERY and record.apply_battery(changed, False, invalidated):
            self._changed()
        op = self._op
        if (interface == DEVICE and op is not None and op.kind == PAIRING and op.cancelled
                and op.stage == STAGE_PAIR and op.path == path and record.paired):
            self._end(CANCELED, paired=True)    # cancelled after the bond: paired, and nothing more is done

    def _untrack(self, record: _Device):
        """A device went away. A device that comes back is a new one, with a new id."""
        del self._devices[record.path]
        self._paths.pop(record.id, None)
        op = self._op
        if op is not None and op.path == record.path and op.kind != FORGETTING:
            self._end(GONE)
        self._changed()

    def _adapters_changed(self):
        lowest = min(self._adapters) if self._adapters else None
        if lowest != self._adapter:
            self._switch_adapter(lowest)
        self._changed(urgent=True)

    def _switch_adapter(self, index: int | None):
        """Another adapter is the lowest-numbered one now, or none is left: its devices have to be read."""
        self._adapter = index
        op = self._op
        if op is not None:
            self._end(None if op.kind == POWERING and not op.want else GONE)
        self._end_scan(send=True)
        self._devices, self._paths, self._overflow = {}, {}, 0
        if index is not None:
            self._read_objects()

    def _adapter_changed(self):
        adapter = self._selected()
        if not adapter.powered:
            self._end_scan(send=False)          # BlueZ ends every search of an adapter that goes off
        op = self._op
        if op is not None and op.kind == POWERING and op.step in (WRITE, VERIFY) and adapter.powered == op.want:
            self._end()
        self._changed(urgent=True)

    # ---- calls -------------------------------------------------------------------------------------------------
    def _call(self, path: str, interface: str, member: str, parameters: GLib.Variant | None, reply_type: str | None,
              callback, *data) -> bool:
        """Send one call to the current owner of org.bluez. The one place that sends anything to BlueZ.

        Anything not on the allow-list raises ``ValueError``: that is a mistake in this module, never a state.
        False when the call cannot be sent; ``callback(reply, error, *data)`` runs when it is answered, unless the
        owner changed or the client was stopped meanwhile.
        """
        if not allowed(interface, member, parameters):
            raise ValueError(f"{interface}.{member} is not a call this client makes")
        connection, owner = self._connection, self._owner
        # Gio drops a call with a malformed destination without ever answering it.
        if connection is None or connection.is_closed() or not Gio.dbus_is_unique_name(owner or ""):
            return False
        try:
            connection.call(owner, path, interface, member, parameters,
                            GLib.VariantType.new(reply_type) if reply_type else None, Gio.DBusCallFlags.NO_AUTO_START,
                            TIMEOUTS.get((interface, member), CALL_TIMEOUT_MS), self._cancellable, self._on_reply,
                            (self._epoch, callback, data))
        except (GLib.Error, TypeError, ValueError):
            return False
        self._in_flight += 1
        return True

    @_guarded
    def _on_reply(self, connection, result, token):
        epoch, callback, data = token
        try:
            reply, error = connection.call_finish(result), None
        except GLib.Error as failure:
            reply, error = None, failure
        if epoch != self._epoch or not self._running:
            return                              # for an owner that is gone, or a client that was stopped
        self._in_flight = max(0, self._in_flight - 1)
        callback(reply, error, *data)

    @staticmethod
    def _ignore(_reply, _error, *_data):
        """For calls whose outcome shows in the signals, or does not matter: StopDiscovery, CancelPairing, ..."""

    # ---- searching ---------------------------------------------------------------------------------------------
    @_guarded
    def scan(self, on):
        if not self._running:
            return
        if on:
            self._start_scan()
        else:
            self._end_scan(send=True)

    def _start_scan(self):
        adapter = self._selected()
        if self._scanning or self._op is not None or self._state() != ON:
            return
        if self._scan_starting and self._scan_path != adapter.path:
            return                              # the start for the adapter before is still unanswered
        self._failure = None
        self._scanning, self._scan_stop = True, False
        if self._scan_stopping:
            # BlueZ still counts this client's last search until it answers the stop, and would call a start
            # meanwhile busy: the start is sent with that answer.
            self._scan_restart = True
        elif not self._scan_starting and not self._send_start(adapter.path):
            return
        self._arm("scan", SCAN_SECONDS * 1000, self._on_scan_limit)
        self._changed(urgent=True)

    def _send_start(self, path: str) -> bool:
        if self._call(path, ADAPTER, "StartDiscovery", None, None, self._on_scan_started, path):
            self._scan_starting, self._scan_path = True, path
            return True
        self._scanning = False
        self._disarm("scan")
        self._failure = Failure(FAILED, SEARCHING)
        self._changed(urgent=True)
        return False

    def _on_scan_started(self, _reply, error, path):
        if path != self._scan_path or not self._scan_starting:
            return
        self._scan_starting = False
        stop, self._scan_stop = self._scan_stop, False
        # InProgress: BlueZ already counts a search of this client. A start is never sent while this client's
        # start or stop is unanswered, so that search is the one this client asked for.
        if error is None or short_name(error) == "InProgress":
            if self._scanning and not stop:
                self._scan_active = True
            else:
                self._send_stop_discovery(path)                     # asked to stop before BlueZ had answered
        elif self._scanning and not stop:
            self._scanning = False
            self._disarm("scan")
            self._failure = Failure(code_of(error), SEARCHING)
            self._changed(urgent=True)

    def _end_scan(self, send: bool):
        """End this client's search. ``send``: say so to BlueZ, which is pointless when the adapter went off or away
        or the connection closes, since BlueZ ends the search itself then."""
        self._scan_restart = False
        if self._scanning:
            self._scanning = False
            self._disarm("scan")
            self._changed(urgent=True)
        if self._scan_starting:
            self._scan_stop = True              # BlueZ refuses a stop before it answered the start
        elif self._scan_active:
            self._scan_active = False
            if send:
                self._send_stop_discovery(self._scan_path)

    def _send_stop_discovery(self, path: str):
        adapter = next((adapter for adapter in self._adapters.values() if adapter.path == path), None)
        if adapter is not None and adapter.powered:
            if self._call(path, ADAPTER, "StopDiscovery", None, None, self._on_scan_stopped, path):
                self._scan_stopping = True

    def _on_scan_stopped(self, _reply, _error, _path):
        self._scan_stopping = False
        if self._scan_restart:
            self._scan_restart = False
            adapter = self._selected()
            if self._scanning and adapter is not None and self._state() == ON:
                self._send_start(adapter.path)

    def _on_scan_limit(self):
        self._end_scan(send=True)

    # ---- operations --------------------------------------------------------------------------------------------
    def _refusal(self, kind: str) -> str | None:
        """Why an operation cannot start in this state, or None."""
        state = self._state()
        if state == ON or (state == OFF and kind == POWERING):
            return None
        return NOT_READY if state in (OFF, NO_ADAPTER) else UNAVAILABLE

    def _refuse(self, code: str, kind: str, device=0, record: _Device | None = None):
        self._failure = Failure(code, kind, "", device if type(device) is int else 0,
                                record.view.name if record is not None else "",
                                bool(record is not None and record.paired))
        self._changed(urgent=True)

    def _begin(self, kind: str, record: _Device | None = None, **fields) -> _Operation:
        op = _Operation(kind, **fields)
        if record is not None:
            op.device, op.path, op.name = record.id, record.path, record.view.name
        self._op, self._failure = op, None
        self._changed(urgent=True)
        return op

    def _end(self, code: str | None = None, *, paired: bool | None = None, unregister: bool = True):
        """The operation is over: ``code`` says why it failed, None that it did what it was asked."""
        op, self._op = self._op, None
        if op is None:
            return
        self._disarm("op")
        self._close_agent(op, unregister)
        op.process = None                       # a power command still running finishes without us
        self._prompt = None
        if code is not None:
            record = self._devices.get(op.path) if op.path else None
            if paired is None:
                paired = record is not None and (record.paired or record.bonded or (op.kind == PAIRING and op.paired))
            self._failure = Failure(code, op.kind, op.stage if op.kind == PAIRING else "", op.device, op.name,
                                    bool(paired))
        self._changed(urgent=True)

    def _close_agent(self, op: _Operation, unregister: bool):
        agent, op.agent = op.agent, None
        if agent is None:
            return
        agent.close()
        if unregister and op.registering and not agent.released:
            # Sent after a RegisterAgent that may still be unanswered: BlueZ handles them in order.
            self._call(AGENT_MANAGER_PATH, AGENT_MANAGER, "UnregisterAgent", GLib.Variant("(o)", (agent.path,)), None,
                       self._ignore)

    @_guarded
    def connect(self, device):
        self._device_operation(CONNECTING, device)

    @_guarded
    def disconnect(self, device):
        self._device_operation(DISCONNECTING, device)

    @_guarded
    def forget(self, device):
        self._device_operation(FORGETTING, device)

    def _device_operation(self, kind: str, device):
        """Connect, disconnect or forget a remembered device."""
        if not self._running or self._op is not None:
            return
        record = self._record(device)
        refusal = self._refusal(kind)
        if refusal is not None or record is None:
            self._refuse(refusal or GONE, kind, device, record)
            return
        if not record.paired:
            return                              # only remembered devices are connected, disconnected or forgotten
        op = self._begin(kind, record)
        if kind == CONNECTING:
            sent = self._call(record.path, DEVICE, "Connect", None, None, self._on_connected, op)
        elif kind == DISCONNECTING:
            sent = self._call(record.path, DEVICE, "Disconnect", None, None, self._on_disconnected, op)
        else:
            # Directly: BlueZ disconnects the device as needed and refuses while the adapter is off.
            sent = self._call(self._selected().path, ADAPTER, "RemoveDevice", GLib.Variant("(o)", (record.path,)),
                              None, self._on_removed, op)
        if not sent:
            self._end(FAILED)
        else:
            self._arm("op", CONNECT_TIMEOUT_MS if kind == CONNECTING else CALL_TIMEOUT_MS, self._on_operation_timeout)

    def _on_connected(self, _reply, error, op):
        if self._op is not op:
            return
        if error is not None and short_name(error) != "AlreadyConnected":
            if timed_out(error):
                self._on_operation_timeout()
            else:
                self._end(code_of(error, cancelled=op.cancelled))
        elif op.cancelled:
            self._end(CANCELED)                 # it connected all the same; the Disconnect sent on cancel undoes it
        else:
            self._end()

    def _on_disconnected(self, _reply, error, op):
        if self._op is not op:
            return
        if error is None or short_name(error) == "NotConnected":
            self._end()
        elif timed_out(error):
            self._on_operation_timeout()
        else:
            self._end(code_of(error))

    def _on_removed(self, _reply, error, op):
        if self._op is not op:
            return
        if error is None:
            self._end()
        elif timed_out(error):
            self._on_operation_timeout()
        else:
            self._end(code_of(error))

    def _abandon_connect(self, op: _Operation):
        if not op.disconnect_sent:
            op.disconnect_sent = True
            self._call(op.path, DEVICE, "Disconnect", None, None, self._ignore)

    def _cancel_pairing(self, op: _Operation):
        if not op.cancel_sent:
            op.cancel_sent = True
            self._call(op.path, DEVICE, "CancelPairing", None, None, self._ignore)

    def _on_operation_timeout(self):
        op = self._op
        if op is None:
            return
        if op.kind == POWERING:
            self._power_timeout(op)
            return
        if op.kind == PAIRING and op.stage == STAGE_PAIR and op.pair_sent:
            if self._bonded(op):
                self._end(CANCELED if op.cancelled else TIMEOUT, paired=True)
                return
            self._cancel_pairing(op)
        elif op.kind == CONNECTING or (op.kind == PAIRING and op.stage == STAGE_CONNECT):
            self._abandon_connect(op)
        self._end(CANCELED if op.cancelled else TIMEOUT)

    def _bonded(self, op: _Operation) -> bool:
        """The device of a pairing has its bond already. BlueZ's pairing request is over then, and BlueZ 5.87
        answers a CancelPairing without one by removing the device's keys: never send it after this."""
        record = self._devices.get(op.path)
        return record is not None and (record.bonded or record.paired)

    # ---- pairing -----------------------------------------------------------------------------------------------
    @_guarded
    def pair(self, device):
        if not self._running or self._op is not None:
            return
        record = self._record(device)
        refusal = self._refusal(PAIRING)
        if refusal is not None or record is None:
            self._refuse(refusal or GONE, PAIRING, device, record)
            return
        if record.paired:
            return                              # remembered already: connect is what it needs
        self._end_scan(send=True)
        op = self._begin(PAIRING, record, stage=STAGE_PAIR)
        self._agent_count += 1
        agent = PairingAgent(self._connection, f"{AGENT_ROOT}/agent{self._agent_count}", record.path,
                             lambda: self._owner, functools.partial(self._show, op), functools.partial(self._hide, op))
        if not agent.export():
            self._end(FAILED)
            return
        op.agent = agent
        if not self._call(AGENT_MANAGER_PATH, AGENT_MANAGER, "RegisterAgent",
                          GLib.Variant("(os)", (agent.path, CAPABILITY)), None, self._on_registered, op):
            self._end(FAILED)
            return
        op.registering = True
        self._arm("op", CALL_TIMEOUT_MS, self._on_operation_timeout)

    def _on_registered(self, _reply, error, op):
        if self._op is not op:
            return
        if error is not None:
            if timed_out(error):
                self._on_operation_timeout()
            else:
                self._end(code_of(error, about_object=False))
            return
        # Only now: Pair() without this agent registered would be answered by the default agent.
        if not self._call(op.path, DEVICE, "Pair", None, None, self._on_paired, op):
            self._end(FAILED)
            return
        op.pair_sent = True
        self._arm("op", PAIR_TIMEOUT_MS, self._on_operation_timeout)

    def _on_paired(self, _reply, error, op):
        if self._op is not op:
            return
        if error is not None and short_name(error) != "AlreadyExists":     # AlreadyExists: paired already
            if timed_out(error):
                self._on_operation_timeout()
            else:
                self._end(code_of(error, cancelled=op.cancelled))
            return
        op.paired = True
        self._close_agent(op, unregister=True)  # the pairing step is over
        self._prompt = None
        if op.cancelled:
            # The bond was made before the cancel took effect: it is kept, and nothing more is done to the device.
            self._end(CANCELED, paired=True)
            return
        op.stage = STAGE_TRUST
        trusted = GLib.Variant("(ssv)", (DEVICE, "Trusted", GLib.Variant("b", True)))
        if not self._call(op.path, PROPERTIES, "Set", trusted, None, self._on_trusted, op):
            self._end(FAILED)
            return
        self._arm("op", CALL_TIMEOUT_MS, self._on_operation_timeout)
        self._changed(urgent=True)

    def _on_trusted(self, _reply, error, op):
        if self._op is not op:
            return
        if op.cancelled:
            self._end(CANCELED)
        elif error is not None:
            if timed_out(error):
                self._on_operation_timeout()
            else:
                self._end(code_of(error))
        else:
            op.stage = STAGE_CONNECT
            if not self._call(op.path, DEVICE, "Connect", None, None, self._on_connected, op):
                self._end(FAILED)
                return
            self._arm("op", CONNECT_TIMEOUT_MS, self._on_operation_timeout)
            self._changed(urgent=True)

    def _show(self, op: _Operation, kind: str, code: str = "", typed: int = 0, max_length: int = 0,
              update: bool = False):
        """The agent has something for the user. ``update``: the same prompt (same id) with new values."""
        if self._op is not op:
            return
        current = self._prompt
        if update and current is not None and current.kind == kind:
            number = current.id
        else:
            self._prompt_ids += 1
            number = self._prompt_ids
        self._prompt = Prompt(number, kind, op.device, op.name, code, typed, max_length)
        self._changed(urgent=True)

    def _hide(self, op: _Operation):
        if self._op is op and self._prompt is not None:
            self._prompt = None
            self._changed(urgent=True)

    @_guarded
    def cancel(self):
        op = self._op
        if op is None or op.cancelled or op.kind not in (PAIRING, CONNECTING):
            return
        op.cancelled = True
        if op.kind == PAIRING and op.stage == STAGE_PAIR and not op.pair_sent:
            self._end(CANCELED)                 # nothing reached the device yet
            return
        if op.kind == CONNECTING or op.stage == STAGE_CONNECT:
            self._abandon_connect(op)
        elif op.stage == STAGE_PAIR and not self._bonded(op):
            # Before the agent answers: BlueZ then cancels its own pairing request, instead of failing it on the
            # agent's answer and taking a CancelPairing that comes after as one without a request.
            self._cancel_pairing(op)
        if op.kind == PAIRING:
            # The agent ends here: what waits is answered Canceled, and anything BlueZ asks from now on, such as a
            # pairing the device starts itself while this agent is the default one, finds nobody to answer it.
            self._close_agent(op, unregister=True)
            self._prompt = None
        # BlueZ answers the call in flight soon after this; it is waited for at most this long.
        self._arm("op", CALL_TIMEOUT_MS, self._on_operation_timeout)
        self._changed(urgent=True)

    @_guarded
    def answer(self, prompt, reply):
        current, op = self._prompt, self._op
        if current is None or op is None or op.agent is None or type(prompt) is not int or prompt != current.id:
            return                              # a prompt that is gone or was replaced
        if op.agent.answer(reply):
            self._prompt = None
            self._changed(urgent=True)

    # ---- power -------------------------------------------------------------------------------------------------
    @_guarded
    def set_power(self, on):
        if not self._running or self._op is not None:
            return
        want = bool(on)
        refusal = self._refusal(POWERING)
        if refusal is not None:
            self._refuse(refusal, POWERING)
            return
        if self._selected().powered == want:
            return                              # it is so already
        op = self._begin(POWERING, want=want)
        command = self._find_power_command()
        process = None
        if command:
            try:
                process = self._run_power_command([command, "on" if want else "off"], power_environment())
            except (GLib.Error, OSError, TypeError, ValueError):
                process = None
        if process is None:
            self._write_powered(op)
            return
        op.step, op.process = COMMAND, process
        process.wait_async(self._cancellable, self._on_power_command_done, (self._epoch, op))
        self._arm("op", POWER_COMMAND_TIMEOUT_MS, self._on_operation_timeout)

    @_guarded
    def _on_power_command_done(self, process, result, token):
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        epoch, op = token
        if epoch != self._epoch or self._op is not op or op.process is not process:
            return
        op.process = None
        self._disarm("op")
        self._verify_power(op)

    def _verify_power(self, op: _Operation):
        """Whatever the power command said: look at the adapter, and write ``Powered`` once if it did not follow."""
        adapter = self._selected()
        if adapter is None or adapter.powered == op.want or op.step == WRITE or op.write_code:
            self._power_outcome(op)
        else:
            self._write_powered(op)

    def _write_powered(self, op: _Operation):
        op.step = WRITE
        adapter = self._selected()
        if adapter is None or not self._call(adapter.path, PROPERTIES, "Set",
                                             GLib.Variant("(ssv)", (ADAPTER, "Powered", GLib.Variant("b", op.want))),
                                             None, self._on_powered_written, op):
            op.write_code = op.write_code or FAILED
            self._power_outcome(op)
            return
        self._arm("op", CALL_TIMEOUT_MS, self._on_operation_timeout)

    def _on_powered_written(self, _reply, error, op):
        if self._op is not op:
            return
        if error is not None and short_name(error) == "Busy":
            op.step = VERIFY                    # a change of Powered is under way already: wait for it
            return
        if error is not None:
            op.write_code = code_of(error)
            op.timed_out = op.timed_out or op.write_code == TIMEOUT
            self._power_outcome(op)
            return
        adapter = self._selected()
        if adapter is None or adapter.powered == op.want:
            self._power_outcome(op)
        else:
            op.step = VERIFY                    # BlueZ answers before it announces the change: wait for that

    def _power_timeout(self, op: _Operation):
        op.timed_out = True
        process, op.process = op.process, None
        if process is not None:
            try:
                process.force_exit()
            except (GLib.Error, TypeError):
                pass
            self._verify_power(op)
        else:
            self._power_outcome(op)

    def _power_outcome(self, op: _Operation):
        adapter = self._selected()
        if adapter is None:
            self._end(GONE if op.want else None)        # an adapter that went away is off, as asked or not
        elif adapter.powered == op.want:
            self._end()
        elif adapter.power_state == "off-blocked" or op.write_code == BLOCKED:
            self._end(BLOCKED)
        else:
            self._end(TIMEOUT if op.timed_out else FAILED)

    # ---- the rest ----------------------------------------------------------------------------------------------
    @_guarded
    def retry(self):
        if self._running and self._read == READ_ERROR and not self._reading and self._owner is not None:
            self._read_objects()

    @_guarded
    def dismiss(self):
        if self._failure is not None:
            self._failure = None
            self._changed(urgent=True)

    def _broke(self, error: Exception):
        """A handler or callback raised: end what ran, so that the source is never stuck in it."""
        self.error = type(error).__name__
        try:
            if self._running and self._op is not None:
                self._end(FAILED)
        except Exception as again:  # noqa: BLE001
            self.error = type(again).__name__
            self._op, self._prompt = None, None
        try:
            self._changed(urgent=True)
        except Exception:  # noqa: BLE001
            pass

    # ---- timers ------------------------------------------------------------------------------------------------
    def _arm(self, name: str, milliseconds: int, callback):
        """One timer per name: arming it again replaces it."""
        self._disarm(name)
        mark = object()

        def fire():
            entry = self._timers.get(name)
            if entry is None or entry[1] is not mark:
                return
            del self._timers[name]
            self._fire(callback)

        self._timers[name] = (self._schedule(milliseconds, fire), mark)

    def _disarm(self, name: str):
        entry = self._timers.pop(name, None)
        if entry is not None:
            if name == "notify":
                self._notify_delay = None
            self._cancel_timer(entry[0])

    def _disarm_all(self):
        for name in list(self._timers):
            try:
                self._disarm(name)
            except Exception as error:  # noqa: BLE001 - every other timer still goes
                self.error = type(error).__name__
        self._timers.clear()
        self._notify_delay = None

    @_guarded
    def _fire(self, callback):
        if self._running:
            callback()

    # ---- telling the window ------------------------------------------------------------------------------------
    def _changed(self, urgent: bool = False):
        """The snapshot may differ now. The window hears of it on the main loop: at once for what the user did,
        after ``COALESCE_MS`` for what BlueZ reported, so that a burst of signals is one notification."""
        self._dirty = True
        if not self._running:
            return
        delay = 0 if urgent else COALESCE_MS
        if "notify" in self._timers:
            if self._notify_delay is not None and self._notify_delay <= delay:
                return
            self._disarm("notify")
        self._arm("notify", delay, self._deliver)
        self._notify_delay = delay

    def _deliver(self):
        self._notify_delay = None
        snapshot = self.snapshot
        if snapshot is self._delivered or not self._running:
            return
        self._delivered = snapshot
        try:
            self._on_change()
        except Exception as error:  # noqa: BLE001 - the window's trouble must not become this source's
            self.error = type(error).__name__

    def _build(self) -> Snapshot:
        if not self._running:
            return IDLE
        state = self._state()
        devices, truncated = self._listing() if state in (ON, OFF) else ((), False)
        op = self._op
        activity = None if op is None else Activity(op.kind, op.device, op.name,
                                                    op.stage if op.kind == PAIRING else "")
        return Snapshot(state, self._scanning, devices, activity, self._prompt, self._failure, truncated)

    def _listing(self) -> tuple[tuple[bluetooth.Device, ...], bool]:
        """Remembered devices first, then named ones nearby, then anonymous ones, each in the order first seen."""
        remembered, named, anonymous = [], [], []
        for record in self._devices.values():
            if record.paired:
                remembered.append(record)
            elif record.connectable is False:
                continue                        # a newer BlueZ says it cannot be connected to (5.87 never does)
            elif record.view.name:
                named.append(record)
            else:
                anonymous.append(record)
        candidates = [record for group in (remembered, named, anonymous)
                      for record in sorted(group, key=lambda record: record.id)]
        listed = tuple(record.view for record in candidates[:MAX_DEVICES])
        return listed, len(candidates) > MAX_DEVICES or self._overflow > 0


def _strings(variant: GLib.Variant, limit: int = 64) -> frozenset:
    """The first ``limit`` strings of an ``as``."""
    return frozenset(variant.get_child_value(index).get_string() for index in range(min(variant.n_children(), limit)))
