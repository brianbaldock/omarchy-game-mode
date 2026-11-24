"""A mocked ``/dev/input`` and ``/sys/class/input`` for controller tests.

Event nodes are FIFOs, so a test can write evdev records into them, and what
the kernel would answer about an opened node (its identity, its key and axis
codes, and which keys are held) comes from the tree instead of an ioctl. Like
the kernel, the tree answers for the device a descriptor was opened on, even
after another device got the node's name. :class:`FakeEvdev` also answers the
reads of held keys and axes as evdev does.
"""

from __future__ import annotations

import array
import errno
import fcntl
import os
import shutil
import termios
from pathlib import Path

from gaminghome import core
from gaminghome.input import (ABS_BYTES, EVENT, EVIOCGABS, EVIOCGBIT, EVIOCGKEY, KEY_BYTES, LONG_BITS,
                              GamepadIdentity, parse_bitmap)

ELITE = GamepadIdentity("Microsoft X-Box One Elite 2 pad", 0x03, 0x045E, 0x0B00)
XBOX_360 = GamepadIdentity("Microsoft X-Box 360 pad", 0x03, 0x045E, 0x028E)   # an identity many programs create
STEAM_PAD = GamepadIdentity("Microsoft X-Box 360 pad 0", 0x03, 0x28DE, 0x11FF)
PHYSICAL = GamepadIdentity("ASUSTeK Computer Inc. N-KEY Device", 0x03, 0x0B05, 0x1B4C)
UINPUT = "devices/virtual/input"
UHID = "devices/virtual/misc/uhid/0003:045E:0B00.0007/input"
USB = "devices/pci0000:00/0000:00:08.1/usb3/3-6/3-6:1.5/0003:0B05:1B4C.0006/input"
GAMEPAD_KEYS = "7cdb000000000000 0 0 0 0"  # BTN_SOUTH and the other pad buttons
GAMEPAD_AXES = "3003f"                     # both sticks, triggers and the hat
RAW_ALLY_KEYS = "3fff000000000000 0 0 0 0"  # the ROG Xbox Ally pad without a kernel driver: codes 304 to 317


def bitmap(codes) -> str:
    """A set of codes the way sysfs prints a capability bitmap."""
    words: dict[int, int] = {}
    for code in codes:
        words[code // LONG_BITS] = words.get(code // LONG_BITS, 0) | 1 << (code % LONG_BITS)
    return " ".join(f"{words.get(index, 0):x}" for index in range(max(words, default=0), -1, -1))


class FakeInputTree:
    """``/dev/input`` and ``/sys/class/input`` look-alikes under a scratch directory."""

    def __init__(self, root: Path):
        self.root = root
        self.dev = root / "dev" / "input"
        self.sys = root / "sys" / "class" / "input"
        self.dev.mkdir(parents=True)
        self.sys.mkdir(parents=True)
        self.kernel: dict[int, GamepadIdentity] = {}
        self.nodes: dict[int, str] = {}
        self.held: dict[int, tuple[set, dict]] = {}
        self.devices: dict[int, Path] = {}     # node inode -> the device it was created for
        self.gone: set[int] = set()
        self.asked: list[int] = []             # one entry per capability question, as for an ioctl

    def _device(self, number, identity, where, keys) -> Path:
        device = self.root / "sys" / where / f"input{number}"
        (device / "id").mkdir(parents=True)
        (device / "capabilities").mkdir()
        (device / "name").write_text(f"{identity.name}\n")
        for field, value in (("bustype", identity.bustype), ("vendor", identity.vendor), ("product", identity.product)):
            (device / "id" / field).write_text(f"{value:04x}\n")
        (device / "capabilities" / "key").write_text(f"{keys}\n")
        (device / "capabilities" / "abs").write_text(f"{GAMEPAD_AXES}\n")
        return device

    def add(self, number, identity, where=UINPUT, readable=True, kernel=None, keys=GAMEPAD_KEYS) -> str:
        device = self._device(number, identity, where, keys)
        (self.sys / f"event{number}").mkdir()
        (self.sys / f"event{number}" / "device").symlink_to(device)
        node = self.dev / f"event{number}"
        os.mkfifo(node)
        self.kernel[os.stat(node).st_ino] = kernel or identity
        self.nodes[os.stat(node).st_ino] = str(node)
        self.devices[os.stat(node).st_ino] = device
        if not readable:
            os.chmod(node, 0)
        return str(node)

    def remove(self, number):
        (self.dev / f"event{number}").unlink()
        device = (self.sys / f"event{number}" / "device").resolve()
        shutil.rmtree(self.sys / f"event{number}")
        shutil.rmtree(device)

    def reassign(self, number, identity, where, keys=GAMEPAD_KEYS):
        """The node's name now leads to another device; a descriptor opened before still belongs to the old one."""
        link = self.sys / f"event{number}" / "device"
        link.unlink()
        link.symlink_to(self._device(number, identity, where, keys))

    def unplug(self, number):
        """The device behind a node is gone: asking an open descriptor of it fails, as with ENODEV."""
        self.gone.add(os.stat(self.dev / f"event{number}").st_ino)

    def identify(self, fd):
        return self.kernel.get(os.fstat(fd).st_ino)

    def capabilities(self, fd, event_type):
        inode = os.fstat(fd).st_ino
        self.asked.append(inode)
        if inode in self.gone:
            raise OSError(errno.ENODEV, os.strerror(errno.ENODEV))
        name = {core.EV_KEY: "key", core.EV_ABS: "abs"}[event_type]
        return frozenset(parse_bitmap((self.devices[inode] / "capabilities" / name).read_text()))

    def set_keys(self, number, keys):
        """The kernel reports other key codes for a device from now on, as after a keymap change."""
        (self.sys / f"event{number}" / "device" / "capabilities" / "key").write_text(f"{keys}\n")

    def hold(self, number, pressed=(), axes=None):
        """Keys and axis positions the kernel would report as held on a node right now."""
        self.held[os.stat(self.dev / f"event{number}").st_ino] = (set(pressed), dict(axes or {}))

    def snapshot(self, fd):
        pressed, axes = self.held.get(os.fstat(fd).st_ino, ((), {}))
        return set(pressed), dict(axes)


class FakeEvdev:
    """The kernel side of the fake event nodes: held keys, axis values and queued events.

    ``send`` reports events the way a device does: the held keys and axis
    values change at once, and the events queue up for the reader, closed by
    a SYN_REPORT. ``queue`` adds events as they are and changes no state.
    ``ioctl`` stands in for fcntl.ioctl and answers as evdev does: EVIOCGKEY
    gives the held keys and drops the key events still queued, because its
    answer already holds them; EVIOCGABS gives one axis and drops nothing.
    EVIOCGBIT, the codes a node reports, is answered by the tree.
    """

    RANGES = {core.ABS_X: (-32768, 32767), core.ABS_Y: (-32768, 32767), core.ABS_HAT0X: (-1, 1), core.ABS_HAT0Y: (-1, 1)}

    def __init__(self, tree: FakeInputTree):
        self.tree = tree
        self.keys: dict[str, set[int]] = {}
        self.axes: dict[str, dict[int, int]] = {}
        self.key_reads: dict[str, int] = {}
        self.fail: dict[tuple[str, int], int] = {}     # the next such read fails with this errno
        self.writers: dict[str, int] = {}

    def close(self):
        for writer in self.writers.values():
            os.close(writer)
        self.writers.clear()

    def send(self, node, *events):
        """Report events, each ``(type, code, value)``, then a SYN_REPORT."""
        keys, axes = self.keys.setdefault(node, set()), self.axes.setdefault(node, {})
        for etype, code, value in events:
            if etype == core.EV_KEY and value in (0, 1):
                (keys.add if value else keys.discard)(code)
            elif etype == core.EV_ABS:
                axes[code] = value
        self.queue(node, [*events, (core.EV_SYN, core.SYN_REPORT, 0)])

    def queue(self, node, events):
        if node not in self.writers:
            self.writers[node] = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
        os.write(self.writers[node], b"".join(EVENT.pack(0, 0, *event) for event in events))

    @staticmethod
    def unread(fd) -> int:
        """How many events wait to be read from a node."""
        count = array.array("i", [0])
        fcntl.ioctl(fd, termios.FIONREAD, count, True)
        return count[0] // EVENT.size

    def ioctl(self, fd, request, buf, _mutate=True):
        node = self.tree.nodes[os.fstat(fd).st_ino]
        if request == EVIOCGKEY(KEY_BYTES):
            self.key_reads[node] = self.key_reads.get(node, 0) + 1
        failure = self.fail.pop((node, request), None)
        if failure:
            raise OSError(failure, os.strerror(failure))
        if request == EVIOCGKEY(KEY_BYTES):
            self._drop_queued_keys(fd, node)
            for code in self.keys.get(node, ()):
                buf[code // 8] |= 1 << (code % 8)
            return 0
        for code, (low, high) in self.RANGES.items():
            if request == EVIOCGABS(code):
                buf[0], buf[1], buf[2] = self.axes.get(node, {}).get(code, 0), low, high
                return 0
        for event_type, size in ((core.EV_KEY, KEY_BYTES), (core.EV_ABS, ABS_BYTES)):
            if request == EVIOCGBIT(event_type, size):
                for code in self.tree.capabilities(fd, event_type):
                    buf[code // 8] |= 1 << (code % 8)
                return 0
        raise OSError(errno.ENOTTY, os.strerror(errno.ENOTTY))

    def _drop_queued_keys(self, fd, node):
        """What __evdev_flush_queue does: drop key events, and SYN_REPORTs left with nothing to close."""
        data = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            data += chunk
        kept, since_report = [], 1                       # a leading SYN_REPORT is kept
        for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
            event = EVENT.unpack_from(data, offset)[2:]
            report = event[:2] == (core.EV_SYN, core.SYN_REPORT)
            if event[0] == core.EV_KEY or (report and not since_report):
                continue
            kept.append(event)
            since_report = 0 if report else since_report + 1
        if kept:
            self.queue(node, kept)
