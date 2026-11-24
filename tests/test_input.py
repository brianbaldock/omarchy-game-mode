"""ControllerMonitor against a mocked sysfs tree and FIFO event nodes.

No controller, no D-Bus: the virtual-gamepad allowlist is injected, and what
the kernel would say about an opened node is answered by the fake tree, its
held keys and axes by a fake evdev.
"""

import array
import errno
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import Gio

from fakes.fake_input import (ELITE, PHYSICAL, RAW_ALLY_KEYS, STEAM_PAD, UHID, USB, XBOX_360, FakeEvdev, FakeInputTree,
                              bitmap)
from fakes.loop import pump
from gaminghome import core, quirks
from gaminghome.input import EVENT, EVIOCGABS, EVIOCGKEY, KEY_BYTES, ControllerMonitor, VirtualGamepads, probe

SCRATCH = Path(__file__).resolve().parent / "scratch" / "input"
USB_PAD = "devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/0003:045E:028E.0009/input"
USB_AGAIN = "devices/pci0000:00/0000:00:08.1/usb3/3-6/3-6:1.5/0003:0B05:1B4C.000A/input"
# The ROG Xbox Ally pad without a kernel driver: A B X Y LB RB View Menu L3 R3 Xbox and three unknown buttons
# arrive as codes 304 to 317. READ is what Game Mode makes of them, CORRECTED what a hwdb entry turns them into.
RAW = list(range(core.BTN_SOUTH, core.BTN_SOUTH + 14))
READ = [core.BTN_SOUTH, core.BTN_EAST, quirks.BTN_NORTH, quirks.BTN_WEST, core.BTN_TL, core.BTN_TR, core.BTN_SELECT,
        core.BTN_START, quirks.BTN_THUMBL, quirks.BTN_THUMBR, core.BTN_MODE]
CORRECTED_KEYS = bitmap([*READ, 0x2C0, 0x2C1, 0x2C2])


class RecordingInterpreter(core.ControllerInterpreter):
    """The real interpreter, noting which keys and which adopted state the monitor hands it."""

    def __init__(self, *arguments):
        super().__init__(*arguments)
        self.keys, self.synced = [], []

    def feed(self, device_id, etype, code, value):
        if etype == core.EV_KEY:
            self.keys.append((code, value))
        super().feed(device_id, etype, code, value)

    def sync_state(self, device_id, pressed, axes):
        self.synced.append((set(pressed), dict(axes)))
        super().sync_state(device_id, pressed, axes)


class MonitorCase(unittest.TestCase):
    def setUp(self):
        # The caller's environment must not decide how the fake pads are read.
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(quirks.DISABLE_ENV, None)
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.tree = FakeInputTree(SCRATCH)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.evdev = FakeEvdev(self.tree)
        self.addCleanup(self.evdev.close)
        reads = mock.patch("gaminghome.input.fcntl", self.evdev)   # held keys and axes, answered as evdev does
        reads.start()
        self.addCleanup(reads.stop)
        self.allow = VirtualGamepads()
        self.actions, self.changes, self.hints = [], [], []
        self.timers, self.timer = {}, 0

    def monitor(self, **reads) -> ControllerMonitor:
        interpreter = RecordingInterpreter(self.schedule, lambda token: self.timers.pop(token, None),
                                           self.actions.append)
        interpreter.set_nav_enabled(True)
        monitor = ControllerMonitor(interpreter, lambda: self.changes.append(1), dev_dir=str(self.tree.dev),
                                    sysfs_root=str(self.tree.sys), extra_gamepads=lambda: self.allow,
                                    on_candidate=self.candidate, identify=self.tree.identify, **reads)
        self.addCleanup(monitor.stop)
        monitor.start()
        return monitor

    def schedule(self, _ms, callback):
        self.timer += 1
        self.timers[self.timer] = callback
        return self.timer

    def fire_timers(self):
        timers, self.timers = self.timers, {}
        for callback in timers.values():
            callback()

    def press(self, node, *keys):
        for code, value in keys:
            self.evdev.send(node, (core.EV_KEY, code, value))

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

    def writer(self, node) -> int:
        writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        return writer

    def send(self, node, events):
        """Write events into a node (a path or a writer) as the kernel would deliver them.

        Each event is (code, value) for a key, or (type, code, value).
        """
        records = (EVENT.pack(0, 0, *(event if len(event) == 3 else (core.EV_KEY, *event))) for event in events)
        os.write(node if isinstance(node, int) else self.writer(node), b"".join(records))


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
        self.assertEqual(monitor.status()["devices"],
                         [{"path": node, "name": ELITE.name, "quirk": None, "virtual": True}])
        writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, writer)
        press = EVENT.pack(0, 0, core.EV_KEY, core.BTN_SOUTH, 1) + EVENT.pack(0, 0, core.EV_SYN, core.SYN_REPORT, 0)
        os.write(writer, press)
        self.assertTrue(pump(lambda: self.actions))
        self.assertEqual(self.actions, [core.ACTIVATE])

    def test_guide_chords_are_ignored_on_the_announced_virtual_pad_only(self):
        elite, usb = self.tree.add(21, ELITE), self.tree.add(7, PHYSICAL, where=USB)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.assertEqual(sorted(monitor.devices), sorted([elite, usb]))
        presses = ((core.BTN_MODE, 1), (core.BTN_SOUTH, 1), (core.BTN_SOUTH, 0), (core.BTN_MODE, 0), (core.BTN_EAST, 1))
        chord = b"".join(EVENT.pack(0, 0, core.EV_KEY, code, value) + EVENT.pack(0, 0, core.EV_SYN, core.SYN_REPORT, 0)
                         for code, value in presses)
        for node, expected in ((elite, [core.BACK]), (usb, [core.ACTIVATE, core.BACK])):
            del self.actions[:]
            writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
            self.addCleanup(os.close, writer)
            os.write(writer, chord)
            self.assertTrue(pump(lambda: core.BACK in self.actions))
            self.assertEqual(self.actions, expected, node)

    def test_focus_does_not_read_the_virtual_pad_and_a_failed_read_empties_an_ordinary_one(self):
        elite, usb = self.tree.add(21, ELITE), self.tree.add(7, PHYSICAL, where=USB)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()

        def focus_again(code):
            self.assertTrue(pump(lambda: all(code in monitor.interpreter.devices[node].pressed for node in (elite, usb))))
            self.evdev.fail[(usb, EVIOCGKEY(KEY_BYTES))] = errno.ENODEV   # as on a node that is going away
            reads = self.evdev.key_reads[elite]
            monitor.interpreter.set_nav_enabled(False)
            monitor.interpreter.set_nav_enabled(True)
            self.assertEqual(self.evdev.key_reads[elite], reads)          # the virtual pad is followed, not read

        for node in (elite, usb):
            self.press(node, (core.BTN_MODE, 1))
        focus_again(core.BTN_MODE)
        self.press(elite, (core.BTN_SOUTH, 1), (core.BTN_SOUTH, 0), (core.BTN_MODE, 0), (core.BTN_EAST, 1))
        self.assertTrue(pump(lambda: core.BACK in self.actions))
        self.assertEqual(self.actions, [core.BACK])   # Guide was still held: A belonged to its chord
        self.press(usb, (core.BTN_MODE, 0))
        for node in (elite, usb):
            self.press(node, (core.BTN_SELECT, 1))
        focus_again(core.BTN_SELECT)
        for node, expected in ((elite, [core.BACK]), (usb, [core.MENU, core.BACK])):
            del self.actions[:]
            self.press(node, (core.BTN_START, 1), (core.BTN_START, 0), (core.BTN_EAST, 0), (core.BTN_EAST, 1))
            self.assertTrue(pump(lambda: core.BACK in self.actions))
            # View is still held on the virtual pad, so Menu began the View + Menu hold; the other
            # pad took the failed read for nothing held, as before.
            self.assertEqual(self.actions, expected, node)

    def test_events_queued_when_home_is_focused_are_read_in_order_with_their_guide(self):
        elite = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.press(elite, (core.BTN_SELECT, 1))                      # a plain View hold
        self.assertTrue(pump(lambda: core.BTN_SELECT in monitor.interpreter.devices[elite].pressed))
        # Still queued when Home is focused: View again for a Guide chord, Guide + A and a Guide + D-pad tap.
        self.press(elite, (core.BTN_SELECT, 0), (core.BTN_MODE, 1), (core.BTN_SELECT, 1), (core.BTN_MODE, 0),
                   (core.BTN_MODE, 1), (core.BTN_SOUTH, 1), (core.BTN_SOUTH, 0))
        self.evdev.send(elite, (core.EV_ABS, core.ABS_HAT0X, 1))
        self.evdev.send(elite, (core.EV_ABS, core.ABS_HAT0X, 0))
        self.press(elite, (core.BTN_MODE, 0))
        monitor.interpreter.set_nav_enabled(False)
        monitor.interpreter.set_nav_enabled(True)
        self.press(elite, (core.BTN_START, 1))
        self.assertTrue(pump(lambda: core.BTN_START in monitor.interpreter.devices[elite].pressed))
        self.fire_timers()
        self.assertEqual(self.actions, [])

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


class RecoveryTest(MonitorCase):
    """Reads of the state on connect and after dropped events, answered as evdev does."""

    DROPPED = [(core.EV_SYN, core.SYN_DROPPED, 0), (core.EV_SYN, core.SYN_REPORT, 0)]

    def virtual_pad(self):
        node = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        return node, self.monitor()

    def chord(self, node, monitor):
        """A pressed while Guide is down and read on its own, then Guide let go and B pressed: only B may act."""
        self.press(node, (core.BTN_SOUTH, 1))
        self.assertTrue(pump(lambda: self.evdev.unread(monitor.devices[node].fd) == 0))
        self.press(node, (core.BTN_SOUTH, 0), (core.BTN_MODE, 0), (core.BTN_EAST, 1))
        self.assertTrue(pump(lambda: core.BACK in self.actions))

    def test_batch_read_with_a_report_of_dropped_events_is_superseded_by_the_read_that_follows(self):
        elite, monitor = self.virtual_pad()
        # In one read: events were dropped, then View went down and Guide up. The read of the state
        # that follows comes after all of them and finds View held.
        self.evdev.keys[elite] = {core.BTN_SELECT}
        self.evdev.queue(elite, self.DROPPED + [(core.EV_KEY, core.BTN_SELECT, 1), (core.EV_SYN, core.SYN_REPORT, 0),
                                                (core.EV_KEY, core.BTN_MODE, 0), (core.EV_SYN, core.SYN_REPORT, 0)])
        self.assertTrue(pump(lambda: self.evdev.unread(monitor.devices[elite].fd) == 0))
        self.press(elite, (core.BTN_START, 1))
        self.assertTrue(pump(lambda: core.BTN_START in monitor.interpreter.devices[elite].pressed))
        self.fire_timers()
        self.assertEqual(self.actions, [])
        # A read taken before View went down: View is pressed during the Guide it found held.
        self.press(elite, (core.BTN_START, 0), (core.BTN_SELECT, 0))
        self.assertTrue(pump(lambda: self.actions == [core.MENU]))   # without View: a plain Menu press
        del self.actions[:]
        self.evdev.keys[elite] = {core.BTN_MODE}
        self.evdev.queue(elite, self.DROPPED)
        self.assertTrue(pump(lambda: core.BTN_MODE in monitor.interpreter.devices[elite].pressed))
        self.press(elite, (core.BTN_SELECT, 1), (core.BTN_MODE, 0), (core.BTN_START, 1))
        self.assertTrue(pump(lambda: core.BTN_START in monitor.interpreter.devices[elite].pressed))
        self.fire_timers()
        self.assertEqual(self.actions, [])

    def test_only_the_virtual_pad_drops_events_of_a_batch_that_a_read_superseded(self):
        usb, (elite, monitor) = self.tree.add(7, PHYSICAL, where=USB), self.virtual_pad()
        for node in (usb, elite):           # A was let go by the time of the read
            self.evdev.queue(node, self.DROPPED + [(core.EV_KEY, core.BTN_SOUTH, 1), (core.EV_SYN, core.SYN_REPORT, 0)])
        self.assertTrue(pump(lambda: all(self.evdev.unread(monitor.devices[node].fd) == 0 for node in (usb, elite))))
        self.assertEqual(self.actions, [core.ACTIVATE])   # the ordinary pad feeds the rest of its batch, as before

    def test_virtual_pad_that_cannot_be_read_on_connect_is_read_again_at_its_next_report(self):
        elite = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        self.evdev.keys[elite] = {core.BTN_MODE}           # Guide is already down when the pad appears
        self.evdev.fail[(elite, EVIOCGKEY(KEY_BYTES))] = errno.EIO
        monitor = self.monitor()
        self.chord(elite, monitor)
        self.assertEqual(self.actions, [core.BACK])
        self.assertEqual(self.evdev.key_reads[elite], 2)  # the failed read, and the one at the next report

    def test_virtual_pad_that_cannot_be_read_after_dropped_events_is_read_again_at_its_next_report(self):
        elite, monitor = self.virtual_pad()
        self.press(elite, (core.BTN_MODE, 1))
        self.assertTrue(pump(lambda: core.BTN_MODE in monitor.interpreter.devices[elite].pressed))
        self.evdev.fail[(elite, EVIOCGKEY(KEY_BYTES))] = errno.EIO
        self.evdev.queue(elite, self.DROPPED)
        self.assertTrue(pump(lambda: self.evdev.key_reads[elite] == 2))   # on connect, and failed now
        self.chord(elite, monitor)
        self.assertEqual(self.actions, [core.BACK])
        self.assertEqual(self.evdev.key_reads[elite], 3)

    def test_keys_read_after_dropped_events_count_when_an_axis_cannot_be_read(self):
        elite, monitor = self.virtual_pad()
        self.evdev.keys[elite] = {core.BTN_MODE}           # Guide went down among the dropped events
        self.evdev.fail[(elite, EVIOCGABS(core.ABS_X))] = errno.EINTR
        self.evdev.queue(elite, self.DROPPED)
        self.assertTrue(pump(lambda: core.BTN_MODE in monitor.interpreter.devices[elite].pressed))
        self.chord(elite, monitor)
        self.assertEqual(self.actions, [core.BACK])

    def test_ordinary_pad_keeps_its_keys_when_only_an_axis_cannot_be_read(self):
        pad = self.tree.add(7, XBOX_360, where=USB_PAD)
        monitor = self.monitor()
        state = monitor.interpreter.devices[pad]
        self.press(pad, (core.BTN_SELECT, 1))                       # View, held across the focus change
        self.assertTrue(pump(lambda: core.BTN_SELECT in state.pressed))
        for failing, expected in ((EVIOCGABS(core.ABS_HAT0X), [core.HOME]), (EVIOCGKEY(KEY_BYTES), [core.MENU])):
            del self.actions[:]
            self.evdev.fail[(pad, failing)] = errno.EINVAL
            monitor.interpreter.set_nav_enabled(False)
            monitor.interpreter.set_nav_enabled(True)
            self.press(pad, (core.BTN_START, 1))
            self.assertTrue(pump(lambda: core.BTN_START in state.pressed))
            self.fire_timers()
            self.press(pad, (core.BTN_START, 0))
            self.assertTrue(pump(lambda: core.BTN_START not in state.pressed))
            # An axis that cannot be read is left out: View still holds, so Menu makes the Home hold.
            # Keys that cannot be read count as nothing held, as before: Menu alone opens Settings.
            self.assertEqual(self.actions, expected, failing)
        self.assertIn(({core.BTN_SELECT}, {core.ABS_X: 0, core.ABS_Y: 0, core.ABS_HAT0Y: 0}),
                      monitor.interpreter.synced)


class EvdevReadTest(MonitorCase):
    def test_reading_held_keys_drops_queued_key_events_but_not_axis_events(self):
        # What evdev does, and what the controller tests rest on.
        node = self.tree.add(7, PHYSICAL, where=USB)
        fd = self.monitor().devices[node].fd
        self.evdev.send(node, (core.EV_KEY, core.BTN_SOUTH, 1), (core.EV_ABS, core.ABS_HAT0X, 1))
        self.evdev.send(node, (core.EV_KEY, core.BTN_SOUTH, 0))
        self.evdev.ioctl(fd, EVIOCGABS(core.ABS_HAT0X), array.array("i", [0] * 6))
        self.assertEqual(self.evdev.unread(fd), 5)
        keys = bytearray(KEY_BYTES)
        self.evdev.ioctl(fd, EVIOCGKEY(KEY_BYTES), keys)
        self.assertEqual(self.evdev.unread(fd), 2)    # the axis event and its SYN_REPORT
        self.assertEqual(keys, bytearray(KEY_BYTES))  # A was let go again


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
        self.assertEqual(monitor.status(), {
            "devices": [{"path": elite, "name": ELITE.name, "quirk": None, "virtual": True}],
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


class ButtonQuirkTest(MonitorCase):
    """The raw pad read through its quirk; what is held and the codes reported are set on the tree itself."""

    def monitor(self) -> ControllerMonitor:
        return super().monitor(snapshot=self.tree.snapshot, capabilities=self.tree.capabilities)

    def raw_pad(self) -> str:
        return self.tree.add(6, PHYSICAL, where=USB, keys=RAW_ALLY_KEYS)

    @staticmethod
    def quirk(monitor):
        device, = monitor.status()["devices"]
        return device["quirk"]

    def test_every_button_of_the_raw_pad_is_read_through_the_quirk(self):
        node = self.raw_pad()
        monitor = self.monitor()
        self.assertEqual(monitor.status()["devices"],
                         [{"path": node, "name": PHYSICAL.name, "quirk": "xinput-order-hid"}])
        keys = monitor.interpreter.keys
        self.send(node, [*((code, 1) for code in RAW), (core.BTN_SOUTH, 0)])
        self.assertTrue(pump(lambda: keys[-1:] == [(core.BTN_SOUTH, 0)]))
        self.assertEqual(keys[:-1], [(code, 1) for code in READ])    # eleven buttons; the unknown three are dropped

    def test_keys_held_when_the_pad_is_adopted_are_read_through_the_quirk_too(self):
        node = self.raw_pad()
        self.tree.hold(6, RAW, {core.ABS_HAT0X: 1})
        monitor = self.monitor()
        pressed, axes = monitor.interpreter.synced[-1]
        self.assertEqual((sorted(pressed), axes), (sorted(READ), {core.ABS_HAT0X: 1}))
        self.tree.hold(6, [core.BTN_TL, core.BTN_TR])                # View and Menu are down when Home is shown
        monitor.resync(node)
        self.assertEqual(monitor.interpreter.devices[node].pressed, {core.BTN_SELECT, core.BTN_START})

    def test_probe_judges_the_pad_by_the_buttons_it_really_has(self):
        info = probe(self.raw_pad(), str(self.tree.sys))
        self.assertEqual((info.gamepad, info.quirk.name, info.keys), (True, "xinput-order-hid", frozenset(RAW)))
        self.assertEqual(info.buttons, frozenset(READ))
        clone = self.tree.add(22, PHYSICAL, keys=RAW_ALLY_KEYS)      # uinput: what another program re-emits
        self.assertIsNone(probe(clone, str(self.tree.sys)).quirk)

    def test_same_pad_with_correct_codes_is_read_as_it_is(self):
        node = self.tree.add(6, PHYSICAL, where=USB)                 # it has a right stick click: a driver is at work
        monitor = self.monitor()
        self.assertIsNone(self.quirk(monitor))
        self.send(node, [(core.BTN_TL, 1), (core.BTN_SELECT, 1)])
        self.assertTrue(pump(lambda: len(monitor.interpreter.keys) == 2))
        self.assertEqual(monitor.interpreter.keys, [(core.BTN_TL, 1), (core.BTN_SELECT, 1)])

    def test_environment_variable_reads_the_raw_pad_as_it_is(self):
        node = self.raw_pad()
        with mock.patch.dict(os.environ, {quirks.DISABLE_ENV: "1"}):
            monitor = self.monitor()
            self.assertIsNone(self.quirk(monitor))
            self.tree.hold(6, [core.BTN_TL])
            monitor.resync(node)
            self.assertEqual(monitor.interpreter.synced[-1][0], {core.BTN_TL})
            self.send(node, [(core.BTN_SELECT, 1)])
            self.assertTrue(pump(lambda: monitor.interpreter.keys == [(core.BTN_SELECT, 1)]))
            self.assertEqual(self.tree.asked, [])                    # its layout is not even watched

    def test_inputplumbers_virtual_pad_is_not_read_through_the_quirk(self):
        raw = self.raw_pad()                                         # the source InputPlumber re-emits...
        elite = self.tree.add(21, ELITE)                             # ...through this pad, with its own codes
        self.allow = VirtualGamepads({ELITE: 1}, frozenset({raw}))
        monitor = self.monitor()
        self.assertEqual(monitor.status()["devices"],
                         [{"path": elite, "name": ELITE.name, "quirk": None, "virtual": True}])
        self.send(elite, [(core.BTN_TL, 1), (core.BTN_SELECT, 1)])
        self.assertTrue(pump(lambda: len(monitor.interpreter.keys) == 2))
        self.assertEqual(monitor.interpreter.keys, [(core.BTN_TL, 1), (core.BTN_SELECT, 1)])

    def test_codes_corrected_on_the_open_pad_are_noticed_before_udev_says_anything(self):
        node = self.raw_pad()
        monitor = self.monitor()
        synced = len(monitor.interpreter.synced)
        self.tree.set_keys(6, CORRECTED_KEYS)                        # a hwdb entry was applied; udev is not heard yet
        self.send(node, [(core.BTN_TR, 1), (core.BTN_TR, 0)])        # RB under its corrected code: must not be Menu
        self.assertTrue(pump(lambda: len(monitor.interpreter.synced) > synced))
        self.assertEqual((self.actions, monitor.interpreter.keys, self.quirk(monitor)), ([], [], None))
        self.send(node, [(core.BTN_START, 1), (core.BTN_START, 0)])  # Menu: no longer dropped as an unknown button
        self.assertTrue(pump(lambda: self.actions))
        self.assertEqual(self.actions, [core.MENU])

    def test_layout_change_between_two_reads_drops_what_is_queued_and_reads_the_state_again(self):
        node = self.raw_pad()
        monitor = self.monitor()
        self.send(node, [(quirks.BTN_Z, 1)])                         # RB of the raw pad
        self.assertTrue(pump(lambda: self.actions == [core.NEXT_TAB]))
        synced = len(monitor.interpreter.synced)
        self.tree.set_keys(6, CORRECTED_KEYS)
        self.tree.hold(6, [core.BTN_SELECT])                         # View is down, under its corrected code
        reports = [(core.EV_SYN, core.SYN_REPORT, 0)] * 64           # more than one read takes
        self.send(node, [*reports, (core.BTN_TL, 1)])                # still queued from before: View, not LB
        self.assertTrue(pump(lambda: len(monitor.interpreter.synced) > synced))
        self.assertEqual(monitor.interpreter.keys, [(core.BTN_TR, 1)])
        self.assertEqual(monitor.interpreter.synced[-1][0], {core.BTN_SELECT})
        self.assertEqual(self.actions, [core.NEXT_TAB])

    def test_dropped_events_around_a_layout_change_switch_nothing_halfway_through_a_batch(self):
        node = self.raw_pad()
        monitor = self.monitor()
        synced = len(monitor.interpreter.synced)
        self.tree.set_keys(6, CORRECTED_KEYS)
        # The queue overflowed around the change; a View pressed before it follows, under its old code.
        self.send(node, [(core.EV_SYN, core.SYN_DROPPED, 0), (core.EV_SYN, core.SYN_REPORT, 0), (core.BTN_TL, 1)])
        self.assertTrue(pump(lambda: len(monitor.interpreter.synced) > synced))
        self.assertEqual((self.actions, monitor.interpreter.keys), ([], []))

    def test_raw_pad_whose_layout_stays_costs_one_question_per_read_and_nothing_else(self):
        node = self.raw_pad()
        monitor = self.monitor()
        asked, synced = len(self.tree.asked), len(monitor.interpreter.synced)
        self.send(node, [(core.BTN_TL, 1), (core.BTN_TL, 0)])        # View
        self.assertTrue(pump(lambda: len(monitor.interpreter.keys) == 2))
        self.assertEqual(monitor.interpreter.keys, [(core.BTN_SELECT, 1), (core.BTN_SELECT, 0)])
        self.assertEqual((len(self.tree.asked) - asked, len(monitor.interpreter.synced) - synced), (1, 0))

    def test_pads_no_quirk_could_apply_to_are_read_without_any_question(self):
        pad = self.tree.add(7, XBOX_360, where=USB_PAD)
        elite = self.tree.add(21, ELITE)
        self.allow = VirtualGamepads({ELITE: 1})
        monitor = self.monitor()
        self.send(pad, [(core.BTN_START, 1), (core.BTN_START, 0)])
        self.send(elite, [(core.BTN_TR, 1)])
        self.assertTrue(pump(lambda: len(self.actions) == 2))
        self.assertEqual((sorted(self.actions), self.tree.asked), (sorted([core.MENU, core.NEXT_TAB]), []))

    def test_node_name_taken_by_another_pad_changes_nothing_for_the_open_one(self):
        unrelated = self.tree.add(7, XBOX_360, where=USB_PAD)
        corrected = self.tree.add(8, PHYSICAL, where=USB, keys=CORRECTED_KEYS)
        monitor = self.monitor()
        for number, node in ((7, unrelated), (8, corrected)):
            # Both stay open while their node names already lead to a raw ROG Xbox Ally pad.
            self.tree.reassign(number, PHYSICAL, where=USB_AGAIN, keys=RAW_ALLY_KEYS)
            self.hotplug(monitor, node, Gio.FileMonitorEvent.ATTRIBUTE_CHANGED)
            monitor.resync(node)
            self.send(node, [(core.BTN_START, 1), (core.BTN_START, 0)])  # Menu, queued on the open pad
        self.assertTrue(pump(lambda: len(self.actions) == 2))
        self.assertEqual(self.actions, [core.MENU, core.MENU])
        self.assertEqual([device["quirk"] for device in monitor.status()["devices"]], [None, None])

    def test_pad_whose_descriptor_no_longer_answers_is_closed(self):
        node = self.raw_pad()
        monitor = self.monitor()
        changes = len(self.changes)
        self.tree.unplug(6)
        self.send(node, [(core.BTN_SOUTH, 1)])
        self.assertTrue(pump(lambda: not monitor.devices))
        self.assertEqual((monitor.interpreter.keys, len(self.changes) - changes), ([], 1))


class RawPadEvdevTest(MonitorCase):
    """The raw pad read the way evdev answers, next to what the virtual pad's recovery does."""

    def raw_pad(self):
        node = self.tree.add(6, PHYSICAL, where=USB, keys=RAW_ALLY_KEYS)
        return node, self.monitor()

    def test_layout_change_drops_the_batch_read_with_it_and_feeds_nothing_of_it(self):
        raw, monitor = self.raw_pad()
        self.tree.set_keys(6, CORRECTED_KEYS)                         # a hwdb entry was applied meanwhile
        self.press(raw, (core.BTN_TR, 1), (core.BTN_TR, 0), (core.BTN_TR, 1))   # RB under its corrected code
        self.assertTrue(pump(lambda: monitor.devices[raw].quirk is None))
        self.assertEqual((self.actions, monitor.interpreter.keys), ([], []))  # not Menu, and not RB either
        self.assertEqual(monitor.interpreter.devices[raw].pressed, {core.BTN_TR})   # read again: RB is held

    def test_virtual_pad_is_neither_translated_nor_layout_watched(self):
        clone = self.tree.add(21, PHYSICAL, keys=RAW_ALLY_KEYS)      # re-emitted by an announced stack
        self.allow = VirtualGamepads({PHYSICAL: 1})
        monitor = self.monitor()
        self.press(clone, (core.BTN_TL, 1))
        self.assertTrue(pump(lambda: self.actions))
        self.assertEqual((self.actions, monitor.devices[clone].keys, self.tree.asked), ([core.PREV_TAB], None, []))
        self.assertIsNone(monitor.status()["devices"][0]["quirk"])

    def test_raw_pad_is_read_again_on_focus_and_never_distrusted(self):
        raw, monitor = self.raw_pad()
        device, state = monitor.devices[raw], monitor.interpreter.devices[raw]
        self.evdev.keys[raw] = {core.BTN_TL}                         # View held, under its raw code
        monitor.interpreter.set_nav_enabled(False)
        monitor.interpreter.set_nav_enabled(True)
        self.assertEqual((self.evdev.key_reads[raw], state.pressed), (2, {core.BTN_SELECT}))
        # Dropped events, and the read after them fails: nothing held, as before, and the batch goes on.
        self.evdev.fail[(raw, EVIOCGKEY(KEY_BYTES))] = errno.EIO
        self.evdev.queue(raw, [(core.EV_SYN, core.SYN_DROPPED, 0), (core.EV_SYN, core.SYN_REPORT, 0),
                               (core.EV_KEY, quirks.BTN_Z, 1), (core.EV_SYN, core.SYN_REPORT, 0)])
        self.assertTrue(pump(lambda: self.actions))
        self.assertEqual((self.actions, device.reads, state.dropping), ([core.NEXT_TAB], 0, False))


    def test_raw_pad_keeps_its_translated_keys_when_only_an_axis_cannot_be_read(self):
        raw, monitor = self.raw_pad()
        state = monitor.interpreter.devices[raw]
        self.press(raw, (core.BTN_TL, 1))                           # View, under its raw code
        self.assertTrue(pump(lambda: state.pressed == {core.BTN_SELECT}))
        self.evdev.fail[(raw, EVIOCGABS(core.ABS_X))] = errno.EINVAL
        monitor.interpreter.set_nav_enabled(False)
        monitor.interpreter.set_nav_enabled(True)
        self.assertEqual(monitor.interpreter.synced[-1],
                         ({core.BTN_SELECT}, {core.ABS_Y: 0, core.ABS_HAT0X: 0, core.ABS_HAT0Y: 0}))
        self.press(raw, (core.BTN_TR, 1))                           # Menu, under its raw code: the Home hold
        self.assertTrue(pump(lambda: core.BTN_START in state.pressed))
        self.fire_timers()
        self.press(raw, (core.BTN_TR, 0))
        self.assertTrue(pump(lambda: core.BTN_START not in state.pressed))
        self.assertEqual(self.actions, [core.HOME])


if __name__ == "__main__":
    unittest.main()
