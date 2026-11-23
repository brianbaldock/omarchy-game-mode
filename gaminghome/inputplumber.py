"""Read-only view of InputPlumber on the system bus.

InputPlumber (https://github.com/ShadowBlip/InputPlumber) is a root daemon
for handhelds. It hides the physical controller nodes and re-emits them
through virtual devices. Game Mode only *observes* it, so its own menus can
read the virtual gamepad and so status can describe the setup honestly:

* the bus name is watched without ever activating the service;
* the only method calls are ``Introspect`` and the ``Get``/``GetAll`` calls
  of ``org.freedesktop.DBus.Properties``, each addressed to the unique name
  of the current owner, never to whoever owns the well-known name later;
* ``PropertiesChanged`` signals are hints to look again, nothing more.

This module has no code that changes InputPlumber: it loads no profile, sets
no property, creates or stops no device and sends no event. Keep it that way;
a write needs its own review (authorization, held input, recovery).

Every reply is matched to the connection, the owner and the discovery pass
that asked for it; anything else is dropped. Hints are coalesced into one
discovery in flight, a discovery that changed the picture is confirmed a
bounded number of times, and nothing runs while InputPlumber is quiet.
"""

from __future__ import annotations

import functools
import re
import xml.parsers.expat
from dataclasses import dataclass

from gi.repository import Gio, GLib

from .input import GamepadIdentity, VirtualGamepads

BUS_NAME = "org.shadowblip.InputPlumber"
ROOT = "/org/shadowblip/InputPlumber"
MANAGER = f"{ROOT}/Manager"
MANAGER_IFACE = "org.shadowblip.InputManager"
COMPOSITE_IFACE = "org.shadowblip.Input.CompositeDevice"
TARGET_IFACE = "org.shadowblip.Input.Target"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
INTROSPECTABLE_IFACE = "org.freedesktop.DBus.Introspectable"
# Every method this module may call. The one place that sends a call refuses anything else.
READ_METHODS = frozenset({(INTROSPECTABLE_IFACE, "Introspect"), (PROPERTIES_IFACE, "Get"),
                          (PROPERTIES_IFACE, "GetAll")})
# Replies that mean "this object is not there (any more)", not that reading failed.
GONE_ERRORS = ("org.freedesktop.DBus.Error.UnknownObject", "org.freedesktop.DBus.Error.UnknownInterface",
               "org.freedesktop.DBus.Error.UnknownMethod")

TIMEOUT_MS = 3000
SETTLE_MS = 150            # coalesce a burst of hints into one discovery
SETTLE_BACKOFF = 16        # ceiling factor while hints keep arriving and nothing changes
CONFIRM_MS = 600           # look once more after a discovery that changed the picture
MAX_CONFIRMS = 4
RETRY_MS = (500, 1500, 4000)   # after a failed discovery, then wait for the next hint

# Checked on a reply before it is unpacked. The root answers Introspect with its
# whole object tree (437 KiB on an ASUS ROG Xbox Ally); every other reply is a
# handful of properties (7 KiB for that device's composite).
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_REPLY_BYTES = 256 * 1024
MAX_COMPOSITES = 16
MAX_TARGETS = 16
MAX_LIST = 1024
MAX_TEXT = 96

# Always used with fullmatch: "$" would let a trailing newline through, and "\d" any Unicode digit.
COMPOSITE_NODE_RE = re.compile(r"CompositeDevice([0-9]{1,4})")
COMPOSITE_PATH_RE = re.compile(rf"{re.escape(ROOT)}/CompositeDevice[0-9]{{1,4}}")
TARGET_PATH_RE = re.compile(rf"{re.escape(ROOT)}/devices/target/[A-Za-z0-9_]{{1,64}}")
EVENT_NODE_RE = re.compile(r"/dev/input/event[0-9]{1,5}")
KIND_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
RIGHT_STICK_BUTTON = "Gamepad:Button:RightStick"

# Kernel identity of the uinput gamepad InputPlumber creates for a target
# DeviceType. These are facts read from InputPlumber v0.82.0 (the identities
# in src/input/target/xpad.rs, and src/input/target/mod.rs where "gamepad"
# is built as the Xbox 360 pad); no code is copied. Only "xbox-elite" was
# also observed on a device. The D-Bus Target.Name is a display name that
# differs from the kernel name, so it is never used for matching.
#
# Targets backed by uhid or usbip (deck, deck-uhid, ds5, ds5-edge, hori-steam,
# 8bitdo-u2, unified-gamepad) get their node from a kernel HID driver. They
# are left out and reported as unrecognized instead of being guessed.
_XBOX_360 = GamepadIdentity("Microsoft X-Box 360 pad", 0x03, 0x045E, 0x028E)
GAMEPAD_TARGETS = {
    "xb360": _XBOX_360,
    "gamepad": _XBOX_360,
    "xbox-elite": GamepadIdentity("Microsoft X-Box One Elite 2 pad", 0x03, 0x045E, 0x0B00),
    "xbox-series": GamepadIdentity("Microsoft Xbox Series S|X Controller", 0x03, 0x045E, 0x0B12),
}
# Target types InputPlumber itself does not count as gamepads.
NON_GAMEPAD_TARGETS = frozenset({"dbus", "null", "touchscreen", "touchpad", "mouse", "keyboard", "debug"})

# What is known about InputPlumber. Only "empty" and "ready" come from a
# complete read of the current owner; "failed" may still hold an older picture.
STATE_UNKNOWN = "unknown"      # the bus has not said yet whether anyone owns the name
STATE_ABSENT = "absent"        # nobody owns the name
STATE_PENDING = "pending"      # InputPlumber is there and has not been read yet
STATE_FAILED = "failed"        # it is there, and the last attempt to read it failed
STATE_EMPTY = "empty"          # read: it manages no device
STATE_READY = "ready"          # read: it manages at least one device

NOTE_NO_DEVICE = "no-device"
NOTE_UNRECOGNIZED = "unrecognized-target"
NOTE_EVDEV_ONLY = "evdev-only"
NOTE_NO_R3 = "no-r3"


@dataclass(frozen=True)
class Composite:
    """One InputPlumber composite device, reduced to what Game Mode shows or needs.

    The persistent identifier is deliberately not kept: it can embed a serial.
    A path names an object only within one discovery; InputPlumber reuses paths.
    """
    path: str
    name: str
    profile: str
    targets: tuple[str, ...]
    dbus_targets: int
    sources: int
    hidraw_sources: bool
    event_nodes: frozenset
    right_stick_button: bool

    @property
    def gamepads(self) -> tuple[str, ...]:
        return tuple(kind for kind in self.targets if kind not in NON_GAMEPAD_TARGETS)

    def describe(self) -> dict:
        return {"name": self.name, "profile": self.profile, "targets": list(self.targets),
                "sources": self.sources, "hidraw_sources": self.hidraw_sources,
                "dbus_targets": self.dbus_targets, "right_stick_button": self.right_stick_button}


def composite_nodes(document: str) -> list[str]:
    """Names of the CompositeDevice children in an introspection document of ``ROOT``.

    InputPlumber answers with the whole object tree below the node, so only
    direct children are collected and no tree is built.
    """
    if not isinstance(document, str) or len(document) > MAX_XML_BYTES:
        raise ValueError("introspection data is missing or too large")
    found: dict[int, str] = {}
    depth = 0

    def start(tag, attributes):
        nonlocal depth
        depth += 1
        if depth == 2 and tag == "node":
            match = COMPOSITE_NODE_RE.fullmatch(attributes.get("name", ""))
            if match and len(found) < MAX_COMPOSITES:
                found[int(match.group(1))] = match.group(0)

    def end(_tag):
        nonlocal depth
        depth -= 1

    parser = xml.parsers.expat.ParserCreate()
    parser.StartElementHandler, parser.EndElementHandler = start, end
    try:
        parser.Parse(document, True)
    except xml.parsers.expat.ExpatError as error:
        raise ValueError(f"introspection data is not XML: {error}") from None
    return [found[index] for index in sorted(found)]


def _text(value) -> str:
    """A bounded, printable string for labels and JSON."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:MAX_TEXT].strip()


def _strings(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value[:MAX_LIST] if isinstance(item, str)]


def _target_paths(value) -> list[str]:
    return [path for path in _strings(value) if TARGET_PATH_RE.fullmatch(path)][:MAX_TARGETS]


def _guarded(method):
    """Callbacks run from the GLib loop: never let an exception escape into it."""
    @functools.wraps(method)
    def wrapper(self, *args):
        try:
            return method(self, *args)
        except Exception as error:  # noqa: BLE001
            self._callback_failed(error)
            return None
    return wrapper


def _timeout_add(milliseconds, callback):
    def fire():
        callback()
        return GLib.SOURCE_REMOVE
    return GLib.timeout_add(milliseconds, fire)


class _Discovery:
    """What one discovery pass has learned so far."""

    def __init__(self):
        self.version: str | None = None
        self.composites: dict[str, dict] = {}
        self.kinds: dict[str, str | None] = {}
        self.error: str | None = None

    def fail(self, error):
        if self.error is None:
            self.error = _text(getattr(error, "message", None) or str(error)) or "discovery failed"


class InputPlumberClient:
    """Keeps a read-only snapshot of InputPlumber's composite devices.

    ``on_change`` is called whenever the snapshot or ``state`` changed.
    ``connection`` replaces the system bus in tests; ``schedule`` and ``cancel``
    replace GLib timers.
    """

    def __init__(self, on_change, connection: Gio.DBusConnection | None = None, schedule=None, cancel=None,
                 settle_ms: int = SETTLE_MS, confirm_ms: int = CONFIRM_MS, retry_ms: tuple[int, ...] = RETRY_MS):
        self.on_change = on_change
        self.owner: str | None = None
        self.version: str | None = None
        self.composites: tuple[Composite, ...] = ()
        self.error: str | None = None
        self.name_known = False    # the bus has said whether InputPlumber owns its name
        self.serial = 0            # number of the last complete read of the current owner; 0 while there is none
        self.discoveries = 0
        self._handler_error: str | None = None
        self._connection = connection
        self._schedule = schedule or _timeout_add
        self._cancel = cancel or GLib.source_remove
        self._settle_ms, self._confirm_ms, self._retry_ms = settle_ms, confirm_ms, tuple(retry_ms)
        self._bus: Gio.DBusConnection | None = None
        self._watch = None
        self._subscriptions: list[int] = []
        self._cancellable: Gio.Cancellable | None = None
        self._epoch = 0
        self._pass = 0
        self._outstanding = 0
        self._discovery: _Discovery | None = None
        self._dirty = False
        self._hinted = False       # a hint from outside D-Bus arrived since the last discovery began
        self._expecting = False    # ...and the discovery in flight is the one that answers it
        self._timer = None
        self._confirms = 0
        self._failures = 0
        self._unchanged = 0

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        if self._watch is None:
            self._watch = self._watch_name()

    def stop(self):
        """Idempotent: forget everything and make every pending callback a no-op."""
        watch, self._watch = self._watch, None
        self._forget()
        self.name_known = False
        if watch is not None:
            self._unwatch_name(watch)

    def _watch_name(self):
        flags = Gio.BusNameWatcherFlags.NONE  # watch only: never start the service
        if self._connection is not None:
            return Gio.bus_watch_name_on_connection(self._connection, BUS_NAME, flags, self._appeared, self._vanished)
        return Gio.bus_watch_name(Gio.BusType.SYSTEM, BUS_NAME, flags, self._appeared, self._vanished)

    @staticmethod
    def _unwatch_name(watch):
        Gio.bus_unwatch_name(watch)

    @property
    def available(self) -> bool:
        return self.owner is not None

    @property
    def state(self) -> str:
        if not self.available:
            return STATE_ABSENT if self.name_known else STATE_UNKNOWN
        if self.error is not None:
            return STATE_FAILED
        if not self.serial:
            return STATE_PENDING
        return STATE_READY if self.composites else STATE_EMPTY

    @property
    def settled(self) -> bool:
        """Whether InputPlumber is there, and if so what it manages, has a complete and current answer."""
        return self.state in (STATE_ABSENT, STATE_EMPTY, STATE_READY)

    @property
    def idle(self) -> bool:
        """Nothing in flight and no timer armed: the client only waits for hints."""
        return self._discovery is None and self._timer is None

    def hint(self):
        """Something outside D-Bus (a virtual gamepad node came or went) suggests looking again."""
        if self.available:
            self._hinted = True
            self._mark_dirty()

    # ---- what Game Mode reads ---------------------------------------------
    def virtual_gamepads(self) -> VirtualGamepads:
        """Allowlist for ``ControllerMonitor``: empty unless InputPlumber owns its name."""
        expected: dict[GamepadIdentity, int] = {}
        owned: set[str] = set()
        for composite in self.composites:
            owned |= composite.event_nodes
            for kind in composite.targets:
                identity = GAMEPAD_TARGETS.get(kind)
                if identity is not None:
                    expected[identity] = expected.get(identity, 0) + 1
        return VirtualGamepads(expected, frozenset(owned))

    def unrecognized_targets(self) -> list[str]:
        """Gamepad target types announced by InputPlumber that Game Mode cannot identify."""
        kinds = {kind for composite in self.composites for kind in composite.gamepads}
        return sorted(kinds - set(GAMEPAD_TARGETS))

    def notes(self) -> list[str]:
        """Machine-readable observations; each is a fact read from InputPlumber, not a diagnosis."""
        if not self.available:
            return []
        if not self.composites:
            return [NOTE_NO_DEVICE] if self.state == STATE_EMPTY else []
        notes = []
        pads = [composite for composite in self.composites if composite.gamepads]
        if self.unrecognized_targets():
            notes.append(NOTE_UNRECOGNIZED)
        if any(not composite.hidraw_sources for composite in pads):
            notes.append(NOTE_EVDEV_ONLY)
        if any(not composite.right_stick_button for composite in pads):
            notes.append(NOTE_NO_R3)
        return notes

    def status(self) -> dict:
        return {"available": self.available, "state": self.state, "version": self.version, "settled": self.settled,
                "composites": [composite.describe() for composite in self.composites],
                "notes": self.notes(), "error": self.error or self._handler_error}

    # ---- name ownership ----------------------------------------------------
    @_guarded
    def _appeared(self, connection, _name, owner):
        if self._watch is None:
            return
        self._forget()
        self._bus, self.owner, self.name_known = connection, owner, True
        self._cancellable = Gio.Cancellable()
        for interface, path in ((COMPOSITE_IFACE, None), (MANAGER_IFACE, MANAGER)):
            self._subscriptions.append(connection.signal_subscribe(
                owner, PROPERTIES_IFACE, "PropertiesChanged", path, interface,
                Gio.DBusSignalFlags.NONE, self._on_properties_changed, self._epoch))
        self._begin()
        self._notify()

    @_guarded
    def _vanished(self, _connection, _name):
        if self._watch is None:
            return
        changed = self.state != STATE_ABSENT
        self._forget()
        self.name_known = True
        if changed:
            self._notify()

    def _forget(self):
        """Drop everything learned from the previous owner and invalidate its replies."""
        self._epoch += 1
        if self._cancellable is not None:
            self._cancellable.cancel()
            self._cancellable = None
        if self._timer is not None:
            self._cancel(self._timer)
            self._timer = None
        if self._bus is not None:
            for subscription in self._subscriptions:
                self._bus.signal_unsubscribe(subscription)
        self._subscriptions = []
        self._bus, self.owner, self.version, self.composites, self.error, self.serial = None, None, None, (), None, 0
        self._discovery, self._outstanding, self._dirty = None, 0, False
        self._hinted = self._expecting = False
        self._confirms = self._failures = self._unchanged = 0

    # ---- hints -------------------------------------------------------------
    @_guarded
    def _on_properties_changed(self, _bus, sender, path, _interface, _signal, _parameters, epoch):
        if epoch != self._epoch or sender != self.owner:
            return
        if path == MANAGER or COMPOSITE_PATH_RE.fullmatch(path or ""):
            self._mark_dirty()

    def _mark_dirty(self):
        if not self.available:
            return
        self._dirty = True
        if self._discovery is None and self._timer is None:
            self._arm(self._settle_delay())

    def _settle_delay(self) -> int:
        """Back off while hints keep coming and discoveries find nothing new."""
        return min(self._settle_ms << self._unchanged, self._settle_ms * SETTLE_BACKOFF)

    def _arm(self, milliseconds):
        self._timer = self._schedule(milliseconds, self._on_timer)

    @_guarded
    def _on_timer(self):
        self._timer = None
        if self.available and self._discovery is None:
            self._begin()

    # ---- discovery ---------------------------------------------------------
    def _begin(self):
        self._pass += 1
        self.discoveries += 1
        self._dirty = False
        self._expecting, self._hinted = self._hinted, False
        self._outstanding = 0
        self._discovery = _Discovery()
        self._call(ROOT, INTROSPECTABLE_IFACE, "Introspect", None, "(s)", self._on_root)
        version = GLib.Variant("(ss)", (MANAGER_IFACE, "Version"))
        self._call(MANAGER, PROPERTIES_IFACE, "Get", version, "(v)", self._on_version)
        self._advance()

    def _call(self, path, interface, method, parameters, reply, callback, *context):
        """Send one read. A call that cannot be sent fails the pass in flight; it is never waited for."""
        if (interface, method) not in READ_METHODS:
            raise ValueError(f"{interface}.{method} is not a read")
        # Gio drops a call with a malformed name or path without ever answering it.
        if not Gio.dbus_is_unique_name(self.owner or ""):
            self._discovery.fail(f"not sent: {self.owner!r} is not a unique bus name")
            return
        if not GLib.Variant.is_object_path(path):
            self._discovery.fail(f"not sent: {path!r} is not an object path")
            return
        try:
            self._bus.call(self.owner, path, interface, method, parameters, GLib.VariantType(reply),
                           Gio.DBusCallFlags.NO_AUTO_START, TIMEOUT_MS, self._cancellable, callback,
                           (self._epoch, self._pass, *context))
        except (TypeError, ValueError, GLib.Error) as error:
            self._discovery.fail(error)
            return
        self._outstanding += 1

    def _reply(self, bus, result, token, limit=MAX_REPLY_BYTES):
        """``(current, value)``: a reply that is not from the pass in flight is not current.

        ``value`` is the unpacked reply, or the error that stands for it. Only a
        current reply within ``limit`` bytes is unpacked.
        """
        try:
            reply = bus.call_finish(result)
        except GLib.Error as error:
            reply = error
        if token != (self._epoch, self._pass) or self._discovery is None:
            return False, None
        self._outstanding -= 1
        if isinstance(reply, GLib.Error):
            return True, reply
        size = reply.get_size()
        if size > limit:
            return True, GLib.Error(f"a reply of {size} bytes is too large")
        return True, reply.unpack()

    @staticmethod
    def _gone(error) -> bool:
        """The object went away between being listed and being read: not a failure."""
        return Gio.DBusError.is_remote_error(error) and Gio.DBusError.get_remote_error(error) in GONE_ERRORS

    @_guarded
    def _on_root(self, bus, result, context):
        current, value = self._reply(bus, result, context[:2], MAX_XML_BYTES)
        if not current:
            return
        if isinstance(value, GLib.Error):
            self._discovery.fail(value)
        else:
            try:
                names = composite_nodes(value[0])
            except ValueError as error:
                names = []
                self._discovery.fail(error)
            for name in names:
                path = f"{ROOT}/{name}"
                self._call(path, PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", (COMPOSITE_IFACE,)), "(a{sv})",
                           self._on_composite, path)
        self._advance()

    @_guarded
    def _on_version(self, bus, result, context):
        current, value = self._reply(bus, result, context[:2])
        if not current:
            return
        if isinstance(value, GLib.Error):
            self._discovery.fail(value)
        else:
            self._discovery.version = _text(value[0]) or None
        self._advance()

    @_guarded
    def _on_composite(self, bus, result, context):
        current, value = self._reply(bus, result, context[:2])
        if not current:
            return
        path = context[2]
        if isinstance(value, GLib.Error):
            if not self._gone(value):
                self._discovery.fail(value)
        elif isinstance(value[0], dict):
            properties = value[0]
            self._discovery.composites[path] = properties
            for target in _target_paths(properties.get("TargetDevices")):
                if target not in self._discovery.kinds:
                    self._discovery.kinds[target] = None
                    self._call(target, PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", (TARGET_IFACE,)), "(a{sv})",
                               self._on_target, target)
        self._advance()

    @_guarded
    def _on_target(self, bus, result, context):
        current, value = self._reply(bus, result, context[:2])
        if not current:
            return
        if isinstance(value, GLib.Error):
            if not self._gone(value):
                self._discovery.fail(value)
        elif isinstance(value[0], dict):
            kind = value[0].get("DeviceType")
            if isinstance(kind, str) and KIND_RE.fullmatch(kind):
                self._discovery.kinds[context[2]] = kind
        self._advance()

    def _advance(self):
        if self._outstanding == 0:
            self._finish()

    @staticmethod
    def _composite(path, properties, kinds) -> Composite:
        sources = _strings(properties.get("SourceDevicePaths"))
        targets = [kinds.get(target) for target in _target_paths(properties.get("TargetDevices"))]
        return Composite(
            path=path,
            name=_text(properties.get("Name")),
            profile=_text(properties.get("ProfileName")),
            targets=tuple(kind for kind in targets if kind),
            dbus_targets=len(_target_paths(properties.get("DbusDevices"))),
            sources=len(sources),
            hidraw_sources=any(source.startswith("/dev/hidraw") for source in sources),
            event_nodes=frozenset(source for source in sources if EVENT_NODE_RE.fullmatch(source)),
            right_stick_button=RIGHT_STICK_BUTTON in _strings(properties.get("Capabilities")),
        )

    def _finish(self):
        discovery, self._discovery = self._discovery, None
        before = (self.version, self.composites, self.error, self.state)
        if discovery.error is None:
            self._failures = 0
            self.serial = self._pass
            self.version = discovery.version
            self.composites = tuple(self._composite(path, properties, discovery.kinds)
                                    for path, properties in sorted(discovery.composites.items()))
        # A failed discovery keeps the last complete picture instead of a partial one.
        self.error = discovery.error
        changed = before != (self.version, self.composites, self.error, self.state)
        self._plan(changed, discovery.error is not None)
        if changed:
            self._notify()

    def _plan(self, changed, failed):
        """Decide whether to look again; every branch is bounded, and quiet means no timer."""
        self._unchanged = 0 if changed else self._unchanged + 1
        if failed and self._failures < len(self._retry_ms):
            self._failures += 1
            self._arm(self._retry_ms[self._failures - 1])
        elif self._dirty:
            self._arm(self._settle_delay())
        elif (changed or self._expecting) and not failed and self._confirms < MAX_CONFIRMS:
            # Targets attach and property mirrors update shortly after the
            # change that was just seen, without a signal of their own. The
            # same holds when a node appeared before its target was listed.
            self._confirms += 1
            self._arm(self._confirm_ms)
        else:
            self._confirms = self._failures = self._unchanged = 0

    def _notify(self):
        try:
            self.on_change()
            self._handler_error = None
        except Exception as error:  # noqa: BLE001
            self._handler_error = _text(f"change handler failed: {error}")

    def _callback_failed(self, error):
        """A callback raised: end the pass in flight so later hints can start a new one."""
        self.error = _text(f"{type(error).__name__}: {error}")
        self._pass += 1
        self._discovery, self._outstanding, self._expecting = None, 0, False
        self._notify()
