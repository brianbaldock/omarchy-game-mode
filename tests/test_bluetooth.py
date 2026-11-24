"""The Bluetooth model: how devices are grouped and ordered for the window."""

import dataclasses
import unittest

from gaminghome import bluetooth as bt


def device(path, name="", **fields):
    return bt.Device(f"/org/bluez/hci0/dev_{path}", name, **fields)


class OrderingTest(unittest.TestCase):
    def test_remembered_lists_connected_devices_first_then_by_name_without_regard_to_case(self):
        devices = [device("a", "zeta", paired=True), device("b", "Alpha", paired=True),
                   device("c", "mu", paired=True, connected=True), device("d", "beta", paired=True),
                   device("e", "Nearby", paired=False)]
        self.assertEqual([d.name for d in bt.remembered(devices)], ["mu", "Alpha", "beta", "zeta"])

    def test_nearby_keeps_first_seen_order_and_leaves_out_remembered_and_anonymous_devices(self):
        devices = [device("a", "second", order=2), device("b", "first", order=1), device("c", "", order=0),
                   device("d", "paired", paired=True, order=3), device("e", "third", order=3)]
        self.assertEqual([d.name for d in bt.nearby(devices)], ["first", "second", "third"])

    def test_the_paths_break_ties_so_the_order_never_depends_on_how_devices_arrived(self):
        one, two = device("a", "same", paired=True), device("b", "same", paired=True)
        self.assertEqual(bt.remembered([two, one]), [one, two])

    def test_connected_is_the_remembered_devices_that_are_connected(self):
        devices = [device("a", "x", paired=True, connected=True), device("b", "y", paired=True),
                   device("c", "z", connected=True)]
        self.assertEqual([d.name for d in bt.connected(devices)], ["x"])


class SnapshotTest(unittest.TestCase):
    def test_only_a_machine_with_an_adapter_has_something_to_show(self):
        states = {state: bt.Snapshot(state).usable for state in
                  (bt.UNKNOWN, bt.ABSENT, bt.NO_ADAPTER, bt.OFF, bt.ON)}
        self.assertEqual(states, {bt.UNKNOWN: False, bt.ABSENT: False, bt.NO_ADAPTER: False, bt.OFF: True,
                                  bt.ON: True})

    def test_a_snapshot_cannot_be_changed_in_place(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            bt.Snapshot().scanning = True

    def test_every_failure_code_is_distinct(self):
        self.assertEqual(len(set(bt.FAILURES)), len(bt.FAILURES))


if __name__ == "__main__":
    unittest.main()
