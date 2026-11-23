"""Read-only, non-exclusive evdev controller input driven by GLib IO watches.

No EVIOCGRAB, no uinput, no synthetic events: other apps (Steam, games,
AntiMicroX) keep receiving the same controller events.

Virtual devices are skipped, with one exception: an input stack that hides
the physical controller and re-emits it through a virtual gamepad (see
``gaminghome.inputplumber``) can announce that gamepad through an injected
allowlist. Recognition is a heuristic, not proof of origin: a virtual node is
used only when its kernel identity is announced and no more look-alikes
exist than were announced. An ambiguous group is rejected as a whole. After a
virtual gamepad went away, another one is used only on an allowlist that was
read afterwards: the earlier one may still describe the device that is gone.

While such a virtual gamepad is in use, the physical nodes that stack reads
are its duplicates: they are not opened, not kept open and not reported.
Without one, a hidden node is a controller that is present but unreadable.

A physical controller whose key codes are known to be wrong is read through a
quirk (see ``gaminghome.quirks``): every key code that reaches the
interpreter, live or adopted as held, is translated first, and so are the
buttons a device is judged by. Which quirk applies is decided on the open
descriptor, never on what its path leads to now: the identity it had when it
was opened, and the key codes it reports, asked again before each batch of
events is used, because a udev hwdb entry can correct the codes of a device
that is already open. When they changed, that batch and whatever else is
queued may come from either layout: they are dropped and the held state is
read again. Only a device a quirk could apply to is asked.
"""

from __future__ import annotations

import array
import errno
import fcntl
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from gi.repository import Gio, GLib

from . import core, quirks

try:
    from gi.repository import GLibUnix

    _fd_add = GLibUnix.fd_add_full
except (ImportError, AttributeError):  # older GLib
    _fd_add = GLib.unix_fd_add_full

EVENT = struct.Struct("llHHi")
INPUT_ID = struct.Struct("HHHH")
LONG_BITS = struct.calcsize("l") * 8
BUS_VIRTUAL = 0x06
KEY_BYTES = 0x2FF // 8 + 1
ABS_BYTES = 0x3F // 8 + 1
NAME_BYTES = 256


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("E") << 8) | nr


def EVIOCGKEY(length):
    return _ioc(2, 0x18, length)


def EVIOCGBIT(event_type, length):
    return _ioc(2, 0x20 + event_type, length)


def EVIOCGABS(code):
    return _ioc(2, 0x40 + code, 24)


def EVIOCGID():
    return _ioc(2, 0x02, INPUT_ID.size)


def EVIOCGNAME(length):
    return _ioc(2, 0x06, length)


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


@dataclass(frozen=True)
class GamepadIdentity:
    """How the kernel describes an input device."""
    name: str
    bustype: int
    vendor: int
    product: int


@dataclass(frozen=True)
class VirtualGamepads:
    """Virtual gamepads an input stack currently announces.

    ``expected`` counts the announced devices per kernel identity.
    ``owned_nodes`` are the physical event nodes that stack reads itself, and
    normally hides from users. ``serial`` numbers the read of the stack this
    was learned from; a later read has a higher one.
    """
    expected: Mapping[GamepadIdentity, int] = field(default_factory=dict)
    owned_nodes: frozenset = frozenset()
    serial: int = field(default=0, compare=False)


@dataclass
class Probe:
    """What sysfs says about an event node. ``keys`` are the key codes exactly as the kernel reports them."""
    node: str
    name: str
    gamepad: bool
    virtual: bool
    identity: GamepadIdentity | None = None
    uinput: bool = False
    keys: frozenset = frozenset()
    axes: frozenset = frozenset()
    quirk: quirks.Quirk | None = None

    @property
    def buttons(self) -> frozenset:
        """The key codes as Game Mode reads them: through the quirk, when the device has one."""
        return self.quirk.translate_all(self.keys) if self.quirk else self.keys


def probe(node: str, sysfs_root: str = "/sys/class/input") -> Probe | None:
    base = Path(sysfs_root) / os.path.basename(node) / "device"

    def read(rel):
        try:
            return (base / rel).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def read_hex(rel):
        try:
            return int(read(rel) or "0", 16)
        except ValueError:
            return 0

    name = read("name")
    if not name and not base.exists():
        return None
    bustype = read_hex("id/bustype")
    keys = frozenset(parse_bitmap(read("capabilities/key")))
    axes = frozenset(parse_bitmap(read("capabilities/abs")))
    device_path = os.path.realpath(base)
    identity = GamepadIdentity(name, bustype, read_hex("id/vendor"), read_hex("id/product"))
    info = Probe(node, name or os.path.basename(node), False, is_virtual(device_path, bustype, name), identity,
                 "/devices/virtual/input/" in device_path, keys, axes)
    # A virtual device carries what another program emits: only a physical one is read through a quirk.
    info.quirk = None if info.virtual else quirks.quirk_for(info)
    info.gamepad = is_gamepad(info.buttons, axes)
    return info


def evdev_identity(fd: int) -> GamepadIdentity | None:
    """Identity of an open event node as the kernel reports it; None when it cannot be read."""
    try:
        ids = bytearray(INPUT_ID.size)
        fcntl.ioctl(fd, EVIOCGID(), ids, True)
        name = bytearray(NAME_BYTES)
        fcntl.ioctl(fd, EVIOCGNAME(NAME_BYTES), name, True)
    except OSError:
        return None
    bustype, vendor, product, _version = INPUT_ID.unpack(ids)
    return GamepadIdentity(bytes(name).split(b"\0", 1)[0].decode("utf-8", "replace"), bustype, vendor, product)


def evdev_snapshot(fd: int) -> tuple[set[int], dict[int, int]]:
    """Key codes held and stick and D-pad positions of an open event node; OSError when the keys cannot be read.

    An axis that cannot be read is left out.
    """
    buf = bytearray(KEY_BYTES)
    fcntl.ioctl(fd, EVIOCGKEY(KEY_BYTES), buf, True)
    pressed = {i for i in range(KEY_BYTES * 8) if buf[i // 8] & (1 << (i % 8))}
    axes = {}
    for code in core.DIRECTION_AXES:
        info = array.array("i", [0] * 6)
        try:
            fcntl.ioctl(fd, EVIOCGABS(code), info, True)
        except OSError:
            continue
        axes[code] = info[0]
    return pressed, axes


def evdev_capabilities(fd: int, event_type: int) -> frozenset[int]:
    """Key or axis codes an open event node reports right now; OSError when it cannot be asked (device gone)."""
    size = KEY_BYTES if event_type == core.EV_KEY else ABS_BYTES
    buf = bytearray(size)
    fcntl.ioctl(fd, EVIOCGBIT(event_type, size), buf, True)
    return frozenset(i for i in range(size * 8) if buf[i // 8] & (1 << (i % 8)))


class _Device:
    """An open event node. ``keys`` is None unless its layout is watched for a quirk."""

    def __init__(self, node, name, fd, virtual=False):
        self.node, self.name, self.fd, self.virtual = node, name, fd, virtual
        self.source = None
        self.reads = 0  # reads of a virtual gamepad's state; events read before one are stale
        self.identity: GamepadIdentity | None = None     # as the descriptor reported it when it was opened
        self.axes: frozenset = frozenset()
        self.keys: frozenset | None = None
        self.quirk: quirks.Quirk | None = None


class ControllerMonitor:
    """Discovers gamepads and feeds their events to the interpreter.

    ``extra_gamepads`` returns the current :class:`VirtualGamepads` allowlist;
    ``on_candidate`` is told when a virtual gamepad came or went, so the
    provider of that allowlist can look again, and returns the serial of the
    first read that can answer. Both are optional. ``identify`` reads the
    identity of an opened node, ``capabilities`` its key or axis codes and
    ``snapshot`` what is held on it; they exist so tests need no evdev.
    """

    def __init__(self, interpreter: core.ControllerInterpreter, on_change, dev_dir="/dev/input",
                 sysfs_root="/sys/class/input", extra_gamepads: Callable[[], VirtualGamepads] | None = None,
                 on_candidate: Callable[[], int | None] | None = None,
                 identify: Callable[[int], GamepadIdentity | None] = evdev_identity,
                 snapshot: Callable[..., tuple[set[int], dict[int, int]]] = evdev_snapshot,
                 capabilities: Callable[[int, int], frozenset[int]] = evdev_capabilities):
        self.interpreter = interpreter
        self.interpreter.on_resync = self.resync
        self.on_change = on_change
        self.dev_dir = dev_dir
        self.sysfs_root = sysfs_root
        self.extra_gamepads = extra_gamepads
        self.on_candidate = on_candidate
        self.identify = identify
        self.snapshot = snapshot
        self.capabilities = capabilities
        self.devices: dict[str, _Device] = {}
        self.errors: dict[str, str] = {}
        self.hidden: dict[str, str] = {}
        self.ambiguous: tuple[str, ...] = ()
        self._lookalikes: set[str] = set()
        self._fresh_from = 0       # serial the allowlist must have before another virtual gamepad is used
        self._list_error: str | None = None
        self._monitor = None

    def start(self):
        try:
            self._monitor = Gio.File.new_for_path(self.dev_dir).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_dir_changed)
        except GLib.Error as error:
            self.errors[self.dev_dir] = f"hotplug unavailable: {error.message}"
        self._scan()
        self.on_change()

    def rescan(self):
        """Judge every node again after the virtual-gamepad allowlist changed."""
        if self._scan():
            self.on_change()

    def stop(self):
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None
        for node in list(self.devices):
            self._remove(node, notify=False)

    def status(self) -> dict:
        return {
            "devices": [{"path": d.node, "name": d.name, "quirk": d.quirk.name if d.quirk else None,
                         **({"virtual": True} if d.virtual else {})} for d in self.devices.values()],
            "errors": [{"path": p, "error": e} for p, e in self.errors.items()],
            "ambiguous": list(self.ambiguous),
            "unreadable": [{"path": p, "name": n} for p, n in self.hidden.items()],
        }

    def _on_dir_changed(self, _monitor, file, _other, event):
        node = file.get_path()
        if not node or not os.path.basename(node).startswith("event"):
            return
        if event == Gio.FileMonitorEvent.DELETED:
            changed = node in self.devices or node in self.errors or node in self.hidden
            lookalike = node in self._lookalikes or (node in self.devices and self.devices[node].virtual)
            self._remove(node, notify=False)
            self.errors.pop(node, None)
            self.hidden.pop(node, None)
            if lookalike:
                # One look-alike fewer can settle an ambiguous group, once the allowlist was read again.
                # The name may already belong to a new device, so the listing alone would not show the loss.
                self._lookalikes.discard(node)
                self._await_fresh()
                changed = self._scan() or changed
            if changed:
                self.on_change()
        elif event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.ATTRIBUTE_CHANGED):
            # udev applies ACLs after the node appears: retry on attribute change.
            if node in self.devices:
                # It also changes them when a stack hides a node that is already open.
                if node in self._allowlist().owned_nodes and self._scan():
                    self.on_change()
                return
            info = probe(node, self.sysfs_root)
            if info is None or not info.gamepad:
                return
            if info.virtual:
                # A virtual gamepad is never judged alone, only against every look-alike.
                changed = self._scan()
                if info.uinput:
                    self._candidate()
                if changed:
                    self.on_change()
            else:
                allow = self._allowlist()
                if not self._duplicate(node, allow) and self._open(node, info, allow):
                    self.on_change()

    def _watch_layout(self, device: _Device):
        """Decide the quirk of a physical device on its open descriptor; OSError when that cannot be asked."""
        identity = self.identify(device.fd)
        if not quirks.might_need_quirk(identity):
            return
        device.identity = identity
        device.axes = self.capabilities(device.fd, core.EV_ABS)
        self._judge_layout(device)

    def _judge_layout(self, device: _Device) -> bool:
        """Ask a watched descriptor for its key codes; True when they changed, and the quirk was decided again."""
        keys = self.capabilities(device.fd, core.EV_KEY)
        if keys == device.keys:
            return False
        device.keys = keys
        device.quirk = quirks.quirk_for(device)
        return True

    @staticmethod
    def _drain(fd):
        """Throw away what is queued on a descriptor; OSError when the device is gone."""
        while True:
            try:
                if not os.read(fd, EVENT.size * 64):
                    raise OSError(errno.ENODEV, "end of input")
            except BlockingIOError:
                return

    def _duplicate(self, node, allow: VirtualGamepads) -> bool:
        """A source of the stack whose virtual gamepad is in use: the same presses, twice."""
        return node in allow.owned_nodes and any(device.virtual for device in self.devices.values())

    def _allowlist(self) -> VirtualGamepads:
        return (self.extra_gamepads() if self.extra_gamepads else None) or VirtualGamepads()

    def _candidate(self) -> int | None:
        return self.on_candidate() if self.on_candidate else None

    def _await_fresh(self):
        """A virtual gamepad went away: have the allowlist read again, and trust no older one for a new device."""
        needed = self._candidate()
        if needed is not None:
            self._fresh_from = max(self._fresh_from, needed)

    @staticmethod
    def _judge_virtual(probes: dict, allow: VirtualGamepads) -> tuple[set[str], tuple[str, ...], set[str]]:
        """Virtual nodes to use, names of groups rejected as ambiguous, and every look-alike seen."""
        groups: dict[GamepadIdentity, list[str]] = {}
        lookalikes = set()
        for node, info in probes.items():
            if info is not None and info.gamepad and info.uinput:
                lookalikes.add(node)
                if info.identity in allow.expected:
                    groups.setdefault(info.identity, []).append(node)
        accepted, ambiguous = set(), set()
        for identity, nodes in groups.items():
            # More look-alikes than announced devices: nothing says which one
            # is the announced gamepad, so none of them is used.
            if len(nodes) <= allow.expected[identity]:
                accepted.update(nodes)
            else:
                ambiguous.add(identity.name)
        return accepted, tuple(sorted(ambiguous)), lookalikes

    def _list_nodes(self) -> list[str]:
        try:
            names = sorted(n for n in os.listdir(self.dev_dir) if n.startswith("event"))
        except OSError as error:
            self._list_error = self.errors[self.dev_dir] = str(error)
            return []
        if self._list_error is not None and self.errors.get(self.dev_dir) == self._list_error:
            del self.errors[self.dev_dir]
        self._list_error = None
        return [os.path.join(self.dev_dir, name) for name in names]

    def _scan(self, skip=None) -> bool:
        """Open every eligible node, except ``skip``, and retire those that stopped qualifying."""
        nodes = self._list_nodes()
        allow = self._allowlist()
        probes = {node: probe(node, self.sysfs_root) for node in nodes}
        accepted, ambiguous, lookalikes = self._judge_virtual(probes, allow)
        if self._lookalikes - lookalikes:
            self._await_fresh()
        self._lookalikes = lookalikes
        if allow.serial < self._fresh_from:
            # Nothing tells the stack's next gamepad from another program's look-alike,
            # and this allowlist may still list the one that went away.
            accepted &= set(self.devices)
        changed = ambiguous != self.ambiguous
        self.ambiguous = ambiguous
        for node, device in list(self.devices.items()):
            if device.virtual and node not in accepted:
                self._remove(node, notify=False)
                changed = True
        # Virtual gamepads first: whether a physical node is a duplicate depends on them.
        gamepads = {node: info for node, info in probes.items() if info is not None and info.gamepad}
        for node in sorted(accepted - set(self.devices) - {skip}):
            changed = self._open(node, gamepads[node], allow) or changed
        for node, info in gamepads.items():
            if info.virtual:
                continue
            if self._duplicate(node, allow):
                if node in self.devices:
                    self._remove(node, notify=False)
                    changed = True
                changed = self.errors.pop(node, None) is not None or changed
            elif node not in self.devices:
                changed = self._open(node, info, allow) or changed
            elif node in allow.owned_nodes and not os.access(node, os.R_OK):
                # Opened before the stack hid it: the descriptor is grabbed and stays silent.
                self._remove(node, notify=False)
                self.hidden[node] = info.name
                changed = True
        stale = [n for n in self.hidden if n not in gamepads or n in self.devices or self._duplicate(n, allow)
                 or n not in allow.owned_nodes]
        for node in stale:
            del self.hidden[node]
        return changed or bool(stale)

    def _fail(self, node, message) -> bool:
        changed = self.errors.get(node) != message
        self.errors[node] = message
        return changed

    def _open(self, node, info: Probe, allow: VirtualGamepads) -> bool:
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError:
            if node in allow.owned_nodes:
                # Hidden on purpose by the stack that re-emits it: not an error.
                changed = self.hidden.get(node) != info.name or node in self.errors
                self.hidden[node] = info.name
                self.errors.pop(node, None)
                return changed
            self.hidden.pop(node, None)
            return self._fail(node, f"permission denied reading {info.name}")
        except OSError as error:
            self.hidden.pop(node, None)
            return self._fail(node, f"{info.name}: {error.strerror}")
        if info.virtual and self.identify(fd) != info.identity:
            # The node is not the device that was judged in sysfs (replaced in between).
            os.close(fd)
            return self.errors.pop(node, None) is not None
        device = _Device(node, info.name, fd, info.virtual)
        if not info.virtual:
            try:
                self._watch_layout(device)
            except OSError as error:
                os.close(fd)
                self.hidden.pop(node, None)
                return self._fail(node, f"{info.name}: {error.strerror}")
        self.errors.pop(node, None)
        self.hidden.pop(node, None)
        # A virtual gamepad is only ever one that an input stack announced.
        self.interpreter.add_device(node, guide_chords=info.virtual)
        for code in (core.ABS_X, core.ABS_Y):
            try:
                buf = array.array("i", [0] * 6)
                fcntl.ioctl(fd, EVIOCGABS(code), buf, True)
                self.interpreter.set_range(node, code, buf[1], buf[2], buf[4])
            except OSError:
                pass
        condition = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        device.source = _fd_add(GLib.PRIORITY_DEFAULT, fd, condition, self._on_readable, node)
        self.devices[node] = device
        self.resync(node)
        return True

    def resync(self, node):
        """Adopt held buttons/axes on connect and when the interpreter asks for it (no actions).

        Only keys that cannot be read make a read fail: an axis that cannot be
        read is left out, and the keys and axes read are adopted, the keys of a
        pad with a quirk through it. A virtual gamepad whose keys cannot be
        read is not taken for one with nothing held: it is read again at its
        next report, and an axis of it that cannot be read keeps its side. Any
        other pad whose keys cannot be read counts as one with nothing held.
        """
        device = self.devices.get(node)
        if device is None:
            return
        if device.virtual:
            device.reads += 1
        try:
            pressed, axes = self.snapshot(device.fd)
        except OSError:
            if device.virtual:
                self.interpreter.distrust(node)
                return
            pressed, axes = set(), {}
        if device.quirk is not None:
            pressed = device.quirk.translate_all(pressed)
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
            if device.keys is not None:
                try:
                    relaid = self._judge_layout(device)
                    if relaid:
                        # Read, or still queued, under either layout: none of it can be trusted.
                        self._drain(fd)
                except OSError:
                    self._remove(node, source_done=True)
                    return False
                if relaid:
                    self.resync(node)
                    continue
            for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _sec, _usec, etype, code, value = EVENT.unpack_from(data, offset)
                if etype == core.EV_KEY and device.quirk is not None:
                    code = device.quirk.translate(code)
                    if code is None:
                        continue
                reads = device.reads
                self.interpreter.feed(node, etype, code, value)
                if node not in self.devices:
                    return False
                if device.reads != reads:
                    break  # the state was just read: the rest of this batch is older than that read
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
            if device.virtual:
                # Its duplicates are closed; with the virtual gamepad gone they count again.
                # The node that just failed is left alone until something announces it anew.
                self._await_fresh()
                self._scan(skip=node)
            self.on_change()
