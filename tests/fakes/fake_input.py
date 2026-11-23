"""A mocked ``/dev/input`` and ``/sys/class/input`` for controller tests.

Event nodes are FIFOs, so a test can write evdev records into them, and what
the kernel would answer about an opened node comes from the tree instead of
an ioctl.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from gaminghome.input import GamepadIdentity

ELITE = GamepadIdentity("Microsoft X-Box One Elite 2 pad", 0x03, 0x045E, 0x0B00)
STEAM_PAD = GamepadIdentity("Microsoft X-Box 360 pad 0", 0x03, 0x28DE, 0x11FF)
PHYSICAL = GamepadIdentity("ASUSTeK Computer Inc. N-KEY Device", 0x03, 0x0B05, 0x1B4C)
UINPUT = "devices/virtual/input"
UHID = "devices/virtual/misc/uhid/0003:045E:0B00.0007/input"
USB = "devices/pci0000:00/0000:00:08.1/usb3/3-6/3-6:1.5/0003:0B05:1B4C.0006/input"
GAMEPAD_KEYS = "7cdb000000000000 0 0 0 0"  # BTN_SOUTH and the other pad buttons
GAMEPAD_AXES = "3003f"                     # both sticks, triggers and the hat


class FakeInputTree:
    """``/dev/input`` and ``/sys/class/input`` look-alikes under a scratch directory."""

    def __init__(self, root: Path):
        self.root = root
        self.dev = root / "dev" / "input"
        self.sys = root / "sys" / "class" / "input"
        self.dev.mkdir(parents=True)
        self.sys.mkdir(parents=True)
        self.kernel: dict[int, GamepadIdentity] = {}

    def add(self, number, identity, where=UINPUT, readable=True, kernel=None) -> str:
        device = self.root / "sys" / where / f"input{number}"
        (device / "id").mkdir(parents=True)
        (device / "capabilities").mkdir()
        (device / "name").write_text(f"{identity.name}\n")
        for field, value in (("bustype", identity.bustype), ("vendor", identity.vendor), ("product", identity.product)):
            (device / "id" / field).write_text(f"{value:04x}\n")
        (device / "capabilities" / "key").write_text(f"{GAMEPAD_KEYS}\n")
        (device / "capabilities" / "abs").write_text(f"{GAMEPAD_AXES}\n")
        (self.sys / f"event{number}").mkdir()
        (self.sys / f"event{number}" / "device").symlink_to(device)
        node = self.dev / f"event{number}"
        os.mkfifo(node)
        self.kernel[os.stat(node).st_ino] = kernel or identity
        if not readable:
            os.chmod(node, 0)
        return str(node)

    def remove(self, number):
        (self.dev / f"event{number}").unlink()
        shutil.rmtree(self.sys / f"event{number}")

    def identify(self, fd):
        return self.kernel.get(os.fstat(fd).st_ino)
