"""Read-only, non-exclusive evdev controller input driven by GLib IO watches.

No EVIOCGRAB, no uinput, no synthetic events: other apps (Steam, games,
AntiMicroX) keep receiving the same controller events.
"""

from __future__ import annotations

import array
import errno
import fcntl
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Gio, GLib

from . import core

try:
    from gi.repository import GLibUnix

    _fd_add = GLibUnix.fd_add_full
except (ImportError, AttributeError):  # older GLib
    _fd_add = GLib.unix_fd_add_full

EVENT = struct.Struct("llHHi")
LONG_BITS = struct.calcsize("l") * 8
BUS_VIRTUAL = 0x06
KEY_BYTES = 0x2FF // 8 + 1


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("E") << 8) | nr


def EVIOCGKEY(length):
    return _ioc(2, 0x18, length)


def EVIOCGABS(code):
    return _ioc(2, 0x40 + code, 24)


def parse_bitmap(text: str) -> set[int]:
    """Parse a sysfs capability bitmap ("3fff000000000000 0 0 0 0")."""
    bits = set()
    for index, word in enumerate(reversed(text.split())):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        base = index * LONG_BITS
        while value:
            low = value & -value
            bits.add(base + low.bit_length() - 1)
            value ^= low
    return bits


def is_gamepad(keys: set[int], axes: set[int]) -> bool:
    sticks = {core.ABS_X, core.ABS_Y} <= axes
    hats = {core.ABS_HAT0X, core.ABS_HAT0Y} <= axes
    return core.BTN_SOUTH in keys and (sticks or hats)


def is_virtual(device_path: str, bustype: int, name: str) -> bool:
    lowered = name.lower()
    return "/devices/virtual/" in device_path or bustype == BUS_VIRTUAL or "antimicrox" in lowered or "uinput" in lowered


@dataclass
class Probe:
    node: str
    name: str
    gamepad: bool
    virtual: bool


def probe(node: str, sysfs_root: str = "/sys/class/input") -> Probe | None:
    base = Path(sysfs_root) / os.path.basename(node) / "device"

    def read(rel):
        try:
            return (base / rel).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    name = read("name")
    if not name and not base.exists():
        return None
    try:
        bustype = int(read("id/bustype") or "0", 16)
    except ValueError:
        bustype = 0
    keys = parse_bitmap(read("capabilities/key"))
    axes = parse_bitmap(read("capabilities/abs"))
    return Probe(node, name or os.path.basename(node), is_gamepad(keys, axes), is_virtual(os.path.realpath(base), bustype, name))


class _Device:
    def __init__(self, node, name, fd, source):
        self.node, self.name, self.fd, self.source = node, name, fd, source


class ControllerMonitor:
    """Discovers physical gamepads and feeds their events to the interpreter."""

    def __init__(self, interpreter: core.ControllerInterpreter, on_change, dev_dir="/dev/input", sysfs_root="/sys/class/input"):
        self.interpreter = interpreter
        self.interpreter.on_resync = self.resync
        self.on_change = on_change
        self.dev_dir = dev_dir
        self.sysfs_root = sysfs_root
        self.devices: dict[str, _Device] = {}
        self.errors: dict[str, str] = {}
        self._monitor = None

    def start(self):
        try:
            self._monitor = Gio.File.new_for_path(self.dev_dir).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_dir_changed)
        except GLib.Error as error:
            self.errors[self.dev_dir] = f"hotplug unavailable: {error.message}"
        try:
            nodes = sorted(n for n in os.listdir(self.dev_dir) if n.startswith("event"))
        except OSError as error:
            self.errors[self.dev_dir] = str(error)
            nodes = []
        for name in nodes:
            self._try_add(os.path.join(self.dev_dir, name))
        self.on_change()

    def stop(self):
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None
        for node in list(self.devices):
            self._remove(node, notify=False)

    def status(self) -> dict:
        return {
            "devices": [{"path": d.node, "name": d.name} for d in self.devices.values()],
            "errors": [{"path": p, "error": e} for p, e in self.errors.items()],
        }

    def _on_dir_changed(self, _monitor, file, _other, event):
        node = file.get_path()
        if not node or not os.path.basename(node).startswith("event"):
            return
        if event == Gio.FileMonitorEvent.DELETED:
            changed = node in self.devices or node in self.errors
            self._remove(node, notify=False)
            self.errors.pop(node, None)
            if changed:
                self.on_change()
        elif event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.ATTRIBUTE_CHANGED):
            # udev applies ACLs after the node appears: retry on attribute change.
            if node not in self.devices and self._try_add(node):
                self.on_change()

    def _try_add(self, node) -> bool:
        if node in self.devices:
            return False
        info = probe(node, self.sysfs_root)
        if info is None or not info.gamepad or info.virtual:
            return False
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError:
            self.errors[node] = f"permission denied reading {info.name}"
            return True
        except OSError as error:
            self.errors[node] = f"{info.name}: {error.strerror}"
            return True
        self.errors.pop(node, None)
        self.interpreter.add_device(node)
        for code in (core.ABS_X, core.ABS_Y):
            try:
                buf = array.array("i", [0] * 6)
                fcntl.ioctl(fd, EVIOCGABS(code), buf, True)
                self.interpreter.set_range(node, code, buf[1], buf[2], buf[4])
            except OSError:
                pass
        condition = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        source = _fd_add(GLib.PRIORITY_DEFAULT, fd, condition, self._on_readable, node)
        self.devices[node] = _Device(node, info.name, fd, source)
        self.resync(node)
        return True

    def resync(self, node):
        """Adopt held buttons/axes after connect or SYN_DROPPED (no actions)."""
        device = self.devices.get(node)
        if device is None:
            return
        pressed, axes = set(), {}
        try:
            buf = bytearray(KEY_BYTES)
            fcntl.ioctl(device.fd, EVIOCGKEY(KEY_BYTES), buf, True)
            pressed = {i for i in range(KEY_BYTES * 8) if buf[i // 8] & (1 << (i % 8))}
            for code in (core.ABS_X, core.ABS_Y, core.ABS_HAT0X, core.ABS_HAT0Y):
                info = array.array("i", [0] * 6)
                fcntl.ioctl(device.fd, EVIOCGABS(code), info, True)
                axes[code] = info[0]
        except OSError:
            pass
        self.interpreter.sync_state(node, pressed, axes)

    def _on_readable(self, fd, condition, node):
        device = self.devices.get(node)
        if device is None:
            return False
        while True:
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                break
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                self._remove(node, source_done=True)
                return False
            if not data:
                self._remove(node, source_done=True)
                return False
            for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _sec, _usec, etype, code, value = EVENT.unpack_from(data, offset)
                self.interpreter.feed(node, etype, code, value)
                if node not in self.devices:
                    return False
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self._remove(node, source_done=True)
            return False
        return True

    def _remove(self, node, notify=True, source_done=False):
        device = self.devices.pop(node, None)
        if device is None:
            return
        if not source_done:
            GLib.source_remove(device.source)
        try:
            os.close(device.fd)
        except OSError:
            pass
        self.interpreter.remove_device(node)
        if notify:
            self.on_change()
