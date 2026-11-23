"""ControllerMonitor against a mocked sysfs tree and FIFO event nodes.

No controller, no D-Bus: the virtual-gamepad allowlist is injected, and what
the kernel would say about an opened node is answered by the fake tree.
"""

import os
import shutil
import unittest
from pathlib import Path

from gi.repository import Gio

from fakes.fake_input import ELITE, PHYSICAL, STEAM_PAD, UHID, USB, XBOX_360, FakeInputTree
from fakes.loop import pump
from gaminghome import core
from gaminghome.input import EVENT, ControllerMonitor, VirtualGamepads, probe

SCRATCH = Path(__file__).resolve().parent / "scratch" / "input"


class MonitorCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.tree = FakeInputTree(SCRATCH)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.allow = VirtualGamepads()
        self.actions, self.changes, self.hints = [], [], []

    def monitor(self) -> ControllerMonitor:
        interpreter = core.ControllerInterpreter(lambda _ms, _callback: 0, lambda _token: None, self.actions.append)
        interpreter.set_nav_enabled(True)
        monitor = ControllerMonitor(interpreter, lambda: self.changes.append(1), dev_dir=str(self.tree.dev),
                                    sysfs_root=str(self.tree.sys), extra_gamepads=lambda: self.allow,
                                    on_candidate=self.candidate, identify=self.tree.identify)
        self.addCleanup(monitor.stop)
        monitor.start()
        return monitor

    def candidate(self) -> int:
        """The provider of the allowlist is told to look again and names the read that will answer."""
        self.hints.append(1)
        return self.allow.serial + 1

    def announce(self, monitor, expected, owned=frozenset()):
        """A complete read of the stack that began after the last hint."""
        self.allow = VirtualGamepads(expected, owned, self.allow.serial + 1)
        monitor.rescan()

    @staticmethod
    def hotplug(monitor, node, event=Gio.FileMonitorEvent.CREATED):
        monitor._on_dir_changed(None, Gio.File.new_for_path(node), None, event)

    @staticmethod
    def names(monitor):
        return [device["name"] for device in monitor.status()["devices"]]


class VirtualGamepadTest(MonitorCase):
    def test_probe_reads_identity_and_tells_uinput_from_other_virtual_devices(self):
        uinput = probe(self.tree.add(21, ELITE), str(self.tree.sys))
        uhid = probe(self.tree.add(22, ELITE, where=UHID), str(self.tree.sys))
        usb = probe(self.tree.add(6, PHYSICAL, where=USB), str(self.tree.sys))
        self.assertEqual((uinput.identity, uinput.gamepad, uinput.virtual, uinput.uinput), (ELITE, True, True, True))
        self.assertEqual((uhid.virtual, uhid.uinput), (True, False))
        self.assertEqual((usb.identity, usb.virtual, usb.uinput), (PHYSICAL, False, False))

    def test_virtual_gamepads_stay_skipped_without_an_announcement(self):
        # Also the spoof case: a pad that looks like InputPlumber's while InputPlumber is not there.
        node = self.tree.add(21, ELITE)
        monitor = self.monitor()
        self.assertEqual(monitor.status(), {"devices": [], "errors": [], "ambiguous": [], "unreadable": []})
        self.hotplug(monitor, node)
        self.assertEqual(monitor.devices, {})
        self.assertEqual(len(self.hints), 1)  # the allowlist provider is asked to look again

    def test_announced_virtual_pad_navigates_like_a_physical_one(self):
        node = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.assertEqual(monitor.status()["devices"], [{"path": node, "name": ELITE.name, "virtual": True}])
        writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        press = EVENT.pack(0, 0, core.EV_KEY, core.BTN_SOUTH, 1) + EVENT.pack(0, 0, core.EV_SYN, core.SYN_REPORT, 0)
        os.write(writer, press)
        self.assertTrue(pump(lambda: self.actions))
        self.assertEqual(self.actions, [core.ACTIVATE])

    def test_only_the_announced_identity_is_used(self):
        self.tree.add(20, STEAM_PAD)                 # Steam Input's own virtual pad
        self.tree.add(22, ELITE, where=UHID)         # right identity, but not a uinput device
        elite = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.assertEqual(list(monitor.devices), [elite])

    def test_more_lookalikes_than_announced_are_all_rejected(self):
        first, second = self.tree.add(21, ELITE), self.tree.add(23, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.assertEqual(monitor.devices, {})
        self.assertEqual(monitor.status()["ambiguous"], [ELITE.name])
        self.tree.remove(23)
        self.hotplug(monitor, second, Gio.FileMonitorEvent.DELETED)
        self.assertEqual((monitor.devices, monitor.status()["ambiguous"]), ({}, []))
        self.announce(monitor, {ELITE: 1})           # which of the two went away is only known after a new read
        self.assertEqual(list(monitor.devices), [first])
        third = self.tree.add(24, ELITE)
        self.announce(monitor, {ELITE: 2})           # two composites, two announced pads
        self.assertEqual(sorted(monitor.devices), [first, third])

    def test_virtual_pad_is_retired_when_it_is_no_longer_announced(self):
        node = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.assertIn(node, monitor.interpreter.devices)
        self.allow, changes = VirtualGamepads(), len(self.changes)
        monitor.rescan()
        self.assertEqual(monitor.devices, {})
        self.assertNotIn(node, monitor.interpreter.devices)
        self.assertEqual(len(self.changes), changes + 1)
        monitor.rescan()
        self.assertEqual(len(self.changes), changes + 1)  # nothing new, nothing reported

    def test_replacement_for_a_vanished_virtual_pad_waits_for_a_new_read_of_the_stack(self):
        # The stack says nothing when its pad goes away: what it announced earlier still lists the pad.
        pad = self.tree.add(21, XBOX_360)
        self.allow = VirtualGamepads({XBOX_360: 1}, serial=4)
        monitor = self.monitor()
        self.assertEqual(list(monitor.devices), [pad])
        self.tree.remove(21)
        self.hotplug(monitor, pad, Gio.FileMonitorEvent.DELETED)
        other = self.tree.add(22, XBOX_360)          # another program's pad with the same identity
        self.hotplug(monitor, other)
        monitor.rescan()
        self.assertEqual((monitor.devices, monitor.status()["devices"]), ({}, []))
        self.assertGreaterEqual(len(self.hints), 1)
        self.announce(monitor, {})                   # read again: the stack has no such pad any more
        self.assertEqual(monitor.devices, {})
        self.announce(monitor, {XBOX_360: 1})        # read again: it does announce one
        self.assertEqual(list(monitor.devices), [other])

    def test_virtual_pad_that_hangs_up_is_not_replaced_on_the_old_announcement(self):
        pad = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        os.close(os.open(pad, os.O_WRONLY | os.O_NONBLOCK))      # the device behind the node is destroyed
        self.assertTrue(pump(lambda: pad not in monitor.devices))
        self.tree.remove(21)
        other = self.tree.add(22, ELITE)
        self.hotplug(monitor, other)
        self.assertEqual(monitor.devices, {})
        self.announce(monitor, {ELITE: 1})
        self.assertEqual(list(monitor.devices), [other])

    def test_look_alike_under_the_same_node_name_is_not_taken_for_the_pad_that_went_away(self):
        pad = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.tree.remove(21)
        self.assertEqual(self.tree.add(21, ELITE), pad)          # the kernel hands out the lowest free number
        self.hotplug(monitor, pad, Gio.FileMonitorEvent.DELETED)  # both events were already waiting
        self.hotplug(monitor, pad)
        self.assertEqual(monitor.devices, {})
        self.announce(monitor, {ELITE: 1})
        self.assertEqual(list(monitor.devices), [pad])

    def test_node_replaced_between_sysfs_and_open_is_rejected(self):
        self.tree.add(21, ELITE, kernel=STEAM_PAD)   # sysfs still describes the announced pad
        self.allow = VirtualGamepads({ELITE: 1})
        self.assertEqual(self.monitor().status()["devices"], [])


@unittest.skipIf(os.geteuid() == 0, "root can read every node")
class HiddenSourceTest(MonitorCase):
    def test_unreadable_pad_is_an_error_when_no_input_stack_owns_it(self):
        self.tree.add(6, PHYSICAL, where=USB, readable=False)
        status = self.monitor().status()
        self.assertEqual([entry["error"] for entry in status["errors"]], [f"permission denied reading {PHYSICAL.name}"])
        self.assertEqual(status["unreadable"], [])

    def test_hidden_source_is_a_silent_duplicate_of_the_virtual_pad_in_use(self):
        hidden, elite = self.tree.add(6, PHYSICAL, where=USB, readable=False), self.tree.add(21, ELITE)
        monitor = self.monitor()
        self.assertEqual(len(monitor.status()["errors"]), 1)  # before the stack announced anything
        self.allow = VirtualGamepads({ELITE: 1}, frozenset({hidden}))
        monitor.rescan()
        self.assertEqual(monitor.status(), {"devices": [{"path": elite, "name": ELITE.name, "virtual": True}],
                                            "errors": [], "ambiguous": [], "unreadable": []})
        self.allow = VirtualGamepads()                         # the stack went away, the node stayed hidden
        monitor.rescan()
        self.assertEqual(len(monitor.status()["errors"]), 1)

    def test_hidden_source_without_a_usable_virtual_pad_is_present_but_unreadable(self):
        hidden = self.tree.add(6, PHYSICAL, where=USB, readable=False)
        self.allow = VirtualGamepads({}, frozenset({hidden}))  # owned, but no recognized virtual gamepad
        monitor = self.monitor()
        self.assertEqual(monitor.status(), {"devices": [], "errors": [], "ambiguous": [],
                                            "unreadable": [{"path": hidden, "name": PHYSICAL.name}]})
        changes = len(self.changes)
        monitor.rescan()
        self.assertEqual(len(self.changes), changes)
        self.tree.remove(6)
        self.hotplug(monitor, hidden, Gio.FileMonitorEvent.DELETED)
        self.assertEqual(monitor.status()["unreadable"], [])

    def test_pad_hidden_after_it_was_opened_is_released_and_reported(self):
        # The stack hid the node while Game Mode held it: the old descriptor is grabbed and silent.
        node = self.tree.add(6, PHYSICAL, where=USB)
        monitor = self.monitor()
        self.assertEqual(list(monitor.devices), [node])
        os.chmod(node, 0)
        self.allow = VirtualGamepads({}, frozenset({node}))
        self.hotplug(monitor, node, Gio.FileMonitorEvent.ATTRIBUTE_CHANGED)
        self.assertEqual(monitor.status(), {"devices": [], "errors": [], "ambiguous": [],
                                            "unreadable": [{"path": node, "name": PHYSICAL.name}]})
        self.assertNotIn(node, monitor.interpreter.devices)

    def test_readable_pad_is_used_when_listed_as_owned_without_a_virtual_pad(self):
        node = self.tree.add(6, PHYSICAL, where=USB)
        self.allow = VirtualGamepads({}, frozenset({node}))    # a stale or pass-through source list
        self.assertEqual(self.names(self.monitor()), [PHYSICAL.name])


class DuplicateSourceTest(MonitorCase):
    def test_source_opened_earlier_is_closed_while_its_virtual_pad_is_used(self):
        # Game Mode was running first: it still holds the node the stack now reads and re-emits.
        source = self.tree.add(6, PHYSICAL, where=USB)
        other = self.tree.add(7, PHYSICAL, where=USB.replace("3-6", "3-7"))
        monitor = self.monitor()
        self.assertEqual(sorted(monitor.devices), [source, other])
        elite = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1}, frozenset({source}))
        monitor.rescan()
        self.assertEqual(sorted(monitor.devices), [elite, other])       # an unrelated pad is untouched
        self.assertNotIn(source, monitor.interpreter.devices)
        self.assertEqual(monitor.status()["unreadable"], [])
        self.hotplug(monitor, source, Gio.FileMonitorEvent.ATTRIBUTE_CHANGED)
        self.assertNotIn(source, monitor.devices)
        writer = os.open(elite, os.O_WRONLY | os.O_NONBLOCK)
        self.tree.remove(21)                                              # the virtual pad is destroyed (suspend)
        os.close(writer)
        self.assertTrue(pump(lambda: elite not in monitor.devices))
        self.assertEqual(sorted(monitor.devices), [source, other])       # the source counts again at once
        self.assertGreaterEqual(len(self.hints), 1)


if __name__ == "__main__":
    unittest.main()
