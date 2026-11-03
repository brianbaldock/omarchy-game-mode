"""Optional AntiMicroX profile-set switching over D-Bus.

Management is opt-in (off by default). When on, set index 1 (AntiMicroX's
"Set 2", which must stay empty) is used for games and Gaming Home, and set
index 0 ("Set 1") for desktop navigation. Each controller's active set is
read once before the first change and restored on stop or opt-out.

When AntiMicroX runs but is not managed, the app never touches it and
treats it as an external mapper (raw navigation stays off). The app is fully
usable without AntiMicroX.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from gi.repository import Gio, GLib

from .core import DESKTOP, GAME

BUS_NAME = "io.github.antimicrox"
ROOT = "/InputDevice"
INTERFACE = "io.github.antimicrox.InputDevice"
SET_FOR_MODE = {GAME: 1, DESKTOP: 0}
TIMEOUT_MS = 1500


class AntimicroxMapper:
    def __init__(self, on_change, managed: bool = False):
        self.on_change = on_change
        self.managed = managed
        self.available = False
        self.requested: str | None = None
        self.confirmed: str | None = None
        self.error: str | None = None
        self.initial: dict[str, int] = {}
        self._bus = None
        self._watch = None
        self._generation = 0

    def start(self):
        self._watch = Gio.bus_watch_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameWatcherFlags.NONE, self._appeared, self._vanished)

    def stop(self):
        self.restore_sync()
        if self._watch:
            Gio.bus_unwatch_name(self._watch)
            self._watch = None

    @property
    def external(self) -> bool:
        """AntiMicroX runs but Game Mode must not change its sets."""
        return self.available and not self.managed

    def raw_navigation_allowed(self) -> bool:
        if not self.available:
            return True
        return self.managed and self.confirmed == GAME

    def status(self) -> dict:
        return {"available": self.available, "managed": self.managed, "external": self.external,
                "requested": self.requested, "confirmed": self.confirmed, "error": self.error,
                "initial_sets": dict(self.initial)}

    def set_managed(self, managed: bool):
        if managed == self.managed:
            return
        if not managed:
            self.restore_sync()
        self.managed = managed
        self.confirmed = None
        self.error = None
        if managed and self.available and self.requested:
            self._apply()
        self.on_change()

    def _appeared(self, connection, _name, _owner):
        self._bus = connection
        self.available = True
        self.confirmed = None
        self.error = None
        self.initial = {}
        if self.managed and self.requested:
            self._apply()
        self.on_change()

    def _vanished(self, _connection, _name):
        self._generation += 1
        self.available = False
        self.confirmed = None
        self.initial = {}
        self.on_change()

    def request(self, mode: str):
        self.requested = mode
        if self.confirmed != mode:
            self.confirmed = None
        if self.available and self.managed:
            self._apply()
        self.on_change()

    def _call(self, path, method, args, reply, callback, data):
        self._bus.call(BUS_NAME, path, INTERFACE if method != "Introspect" else "org.freedesktop.DBus.Introspectable",
                       method, args, GLib.VariantType(reply) if reply else None, Gio.DBusCallFlags.NONE,
                       TIMEOUT_MS, None, callback, data)

    def _apply(self):
        self._generation += 1
        self._call(ROOT, "Introspect", None, "(s)", self._on_introspect, (self._generation, self.requested))

    def _on_introspect(self, bus, result, data):
        generation, mode = data
        try:
            xml = bus.call_finish(result).unpack()[0]
            nodes = [n.get("name") for n in ET.fromstring(xml).findall("node") if n.get("name")]
        except (GLib.Error, ET.ParseError) as error:
            return self._failed(generation, str(getattr(error, "message", error)))
        if generation != self._generation:
            return
        if not nodes:
            # No controller is mapped, so raw input already reaches apps.
            self.confirmed, self.error = mode, None
            return self.on_change()
        remaining = {"count": len(nodes), "ok": True}
        for node in nodes:
            path = f"{ROOT}/{node}"
            step = (generation, mode, path, remaining)
            if path in self.initial:
                self._set(step)
            else:
                self._call(path, "getActiveSetNumber", None, "(i)", self._on_snapshot, step)

    def _on_snapshot(self, bus, result, data):
        generation, _mode, path, _remaining = data
        try:
            number = bus.call_finish(result).unpack()[0]
        except GLib.Error as error:
            # Without a snapshot the set could not be restored; leave it alone.
            return self._failed(generation, error.message)
        self.initial.setdefault(path, number)
        if generation == self._generation:
            self._set(data)

    def _set(self, data):
        _generation, mode, path, _remaining = data
        self._call(path, "setActiveSetNumber", GLib.Variant("(i)", (SET_FOR_MODE[mode],)), None, self._on_set, data)

    def _on_set(self, bus, result, data):
        generation, _mode, path, _remaining = data
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            return self._failed(generation, error.message)
        self._call(path, "getActiveSetNumber", None, "(i)", self._on_get, data)

    def _on_get(self, bus, result, data):
        generation, mode, _path, remaining = data
        try:
            number = bus.call_finish(result).unpack()[0]
        except GLib.Error as error:
            return self._failed(generation, error.message)
        if generation != self._generation:
            return
        remaining["ok"] = remaining["ok"] and number == SET_FOR_MODE[mode]
        remaining["count"] -= 1
        if remaining["count"] == 0:
            self.confirmed = mode if remaining["ok"] else None
            self.error = None if remaining["ok"] else "AntiMicroX did not switch sets"
            self.on_change()

    def _failed(self, generation, message):
        if generation != self._generation:
            return
        self.confirmed = None
        self.error = message
        self.on_change()

    def restore_sync(self):
        """Put every controller back on the set it had before Game Mode changed it."""
        self._generation += 1
        initial, self.initial = self.initial, {}
        self.confirmed = None
        if not initial or not self.available or self._bus is None:
            return
        for path, number in initial.items():
            try:
                self._bus.call_sync(BUS_NAME, path, INTERFACE, "setActiveSetNumber", GLib.Variant("(i)", (number,)),
                                    None, Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
            except GLib.Error:
                pass
