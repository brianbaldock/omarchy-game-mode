"""Read-only view of UPower's DisplayDevice on the system bus.

UPower (https://upower.freedesktop.org) adds up the batteries that power the machine into one object, the
DisplayDevice. Game Mode only *observes* it, so the Home header can show the charge:

* the bus name is watched without ever activating the service: UPower is never started for this;
* the only method call is ``GetAll`` of ``org.freedesktop.DBus.Properties`` on that one object, addressed to
  the unique name of the current owner, never to whoever owns the well-known name later;
* ``PropertiesChanged`` signals are hints to read again, nothing more, and hints that arrive while a read is
  in flight ask for one more read, not one each;
* nothing is polled, and nothing exists before ``start()`` or after ``stop()``: the window starts the client when
  it is mapped and stops it when it is unmapped, so a hidden Gaming Home holds no watch, no subscription, no
  timer and no call in flight.

This module has no code that changes UPower: no ``Refresh``, no charge threshold, no property write. Keep it
that way; writing to UPower would be a different feature, to discuss first.
"""

from __future__ import annotations

import functools

from gi.repository import Gio, GLib

from .battery import Battery, from_upower

BUS_NAME = "org.freedesktop.UPower"
DISPLAY_DEVICE = "/org/freedesktop/UPower/devices/DisplayDevice"
DEVICE_IFACE = "org.freedesktop.UPower.Device"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
# Every method this module may call. The one place that sends a call refuses anything else.
READ_METHODS = frozenset({(PROPERTIES_IFACE, "GetAll")})

TIMEOUT_MS = 3000
MAX_REPLY_BYTES = 64 * 1024   # the DisplayDevice answers with about 40 small properties
MAX_TEXT = 96

# What is known about UPower.
STATE_UNKNOWN = "unknown"     # the bus has not said yet whether anyone owns the name
STATE_ABSENT = "absent"       # nobody owns it
STATE_PENDING = "pending"     # UPower is there and has not been read yet
STATE_READY = "ready"         # the last read worked; ``battery`` is what it said, None for no battery
STATE_FAILED = "failed"       # UPower is there and the last read failed


def _text(value) -> str:
    """A bounded, printable string for an error."""
    return "".join(ch for ch in str(value) if ch.isprintable())[:MAX_TEXT].strip()


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


class UPowerClient:
    """Keeps the last reading of UPower's DisplayDevice while it is started.

    ``on_change`` is called whenever ``state`` or ``battery`` changed. ``connection`` replaces the system bus in
    tests.
    """

    def __init__(self, on_change, connection: Gio.DBusConnection | None = None):
        self.on_change = on_change
        self.owner: str | None = None
        self.battery: Battery | None = None
        self.error: str | None = None
        self.handler_error: str | None = None
        self.name_known = False    # the bus has said whether UPower owns its name
        self.answered = False      # the current owner has been read, once
        self.reads = 0             # calls sent
        self._connection = connection
        self._watch = None
        self._bus: Gio.DBusConnection | None = None
        self._subscription: int | None = None
        self._cancellable: Gio.Cancellable | None = None
        self._epoch = 0
        self._reading = False
        self._again = False

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
    def state(self) -> str:
        if self.owner is None:
            return STATE_ABSENT if self.name_known else STATE_UNKNOWN
        if self.error is not None:
            return STATE_FAILED
        return STATE_READY if self.answered else STATE_PENDING

    @property
    def idle(self) -> bool:
        """No call in flight: the client only waits for hints."""
        return not self._reading

    def refresh(self):
        """Read again now, or once more when the read in flight is done. Nothing happens while UPower is absent."""
        if self.owner is None:
            return
        if self._reading:
            self._again = True
        else:
            self._read()

    # ---- name ownership ----------------------------------------------------
    @_guarded
    def _appeared(self, connection, _name, owner):
        if self._watch is None:
            return
        self._forget()
        self._bus, self.owner, self.name_known = connection, owner, True
        self._cancellable = Gio.Cancellable()
        self._subscription = connection.signal_subscribe(
            owner, PROPERTIES_IFACE, "PropertiesChanged", DISPLAY_DEVICE, DEVICE_IFACE,
            Gio.DBusSignalFlags.NONE, self._on_properties_changed, self._epoch)
        self._notify()
        self._read()

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
        if self._bus is not None and self._subscription is not None:
            self._bus.signal_unsubscribe(self._subscription)
        self._subscription = None
        self._bus, self.owner, self.battery, self.error, self.answered = None, None, None, None, False
        self._reading = self._again = False

    @_guarded
    def _on_properties_changed(self, _bus, sender, _path, _interface, _signal, _parameters, epoch):
        if epoch == self._epoch and sender == self.owner:
            self.refresh()

    # ---- reading -----------------------------------------------------------
    def _read(self):
        self._reading, self._again = True, False
        if not self._call(DISPLAY_DEVICE, PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", (DEVICE_IFACE,)),
                          "(a{sv})", self._on_reply):
            self._reading = False

    def _call(self, path, interface, method, parameters, reply, callback) -> bool:
        """Send one read. A call that cannot be sent fails the client; it is never waited for."""
        if (interface, method) not in READ_METHODS:
            raise ValueError(f"{interface}.{method} is not a read")
        # Gio drops a call with a malformed name or path without ever answering it.
        if not Gio.dbus_is_unique_name(self.owner or ""):
            self._fail(f"not sent: {self.owner!r} is not a unique bus name")
            return False
        try:
            self._bus.call(self.owner, path, interface, method, parameters, GLib.VariantType(reply),
                           Gio.DBusCallFlags.NO_AUTO_START, TIMEOUT_MS, self._cancellable, callback, self._epoch)
        except (TypeError, ValueError, GLib.Error) as error:
            self._fail(error)
            return False
        self.reads += 1
        return True

    @_guarded
    def _on_reply(self, bus, result, epoch):
        try:
            reply = bus.call_finish(result)
        except GLib.Error as error:
            reply = error
        if epoch != self._epoch:
            return  # the answer to a call of a previous owner, or of a client that was stopped
        self._reading = False
        if isinstance(reply, GLib.Error):
            self._fail(reply)
        elif reply.get_size() > MAX_REPLY_BYTES:
            self._fail(f"a reply of {reply.get_size()} bytes is too large")
        else:
            value = reply.unpack()
            if isinstance(value, tuple) and value and isinstance(value[0], dict):
                self._succeed(from_upower(value[0]))
            else:
                self._fail("unexpected reply")
        if self._again and self.owner is not None:
            self._read()

    def _succeed(self, battery: Battery | None):
        before = (self.state, self.battery)
        self.battery, self.error, self.answered = battery, None, True
        if before != (self.state, self.battery):
            self._notify()

    def _fail(self, error):
        before = self.state
        self.error = _text(getattr(error, "message", None) or error) or "read failed"
        if before != self.state:
            self._notify()

    def _notify(self):
        try:
            self.on_change()
            self.handler_error = None
        except Exception as error:  # noqa: BLE001
            self.handler_error = _text(f"change handler failed: {error}")

    def _callback_failed(self, error):
        """A callback raised: end the read in flight so later hints can start a new one."""
        self._reading = False
        self._fail(f"{type(error).__name__}: {error}")
