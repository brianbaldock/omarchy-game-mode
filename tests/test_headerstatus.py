"""What the header shows and when it looks again: where the battery comes from, the minute timer, and their lifecycle.

The point of most of these tests is what exists while Gaming Home is *not* shown: no timer, no UPower watch, no
subscription, no call in flight. Everything runs on stand-ins (a stub bus, a scratch sysfs, timers fired by hand),
plus one test that uses GLib's own timers to prove that a stopped timer is really gone from the main loop.
"""

import ast
import importlib
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gi.repository import GLib

from fakes.clockwork import Timers, Wall, posix_time
from fakes.fake_upower import StubBus, StubClient
from fakes.loop import pump
from fakes.rc73ya import rc73ya, rc73ya_tree
from gaminghome import headerstatus
from gaminghome.battery import CHARGING, DISCHARGING, Battery
from gaminghome.clock import SLACK_MS, ClockStyle
from gaminghome.headerstatus import BatteryMonitor, HeaderStatus

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(__file__).resolve().parent / "scratch" / "headerstatus"
EVENING = datetime(2026, 11, 23, 21, 41, 7, 250_000)
NOTHING_HELD = {"timers": 0, "watches": 0, "subscriptions": 0, "calls": 0}


class ScriptedClient:
    """A UPower client whose state the test sets by hand."""

    def __init__(self, on_change):
        self.on_change, self.state, self.battery = on_change, "unknown", None
        self.started = self.stopped = self.refreshes = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def refresh(self):
        self.refreshes += 1

    def say(self, state, battery=None):
        self.state, self.battery = state, battery
        self.on_change()


class BatteryMonitorTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.root = SCRATCH / "power_supply"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.changes = []
        self.monitor = BatteryMonitor(lambda: self.changes.append(self.monitor.battery), make_client=self.client_for,
                                      sysfs_root=str(self.root))
        self.monitor.start()

    def client_for(self, on_change):
        self.client = ScriptedClient(on_change)
        return self.client

    def set_capacity(self, percent):
        (self.root / "BAT1" / "capacity").write_text(f"{percent}\n", encoding="utf-8")

    def test_starting_starts_the_client_and_shows_nothing_until_a_source_answers(self):
        self.assertEqual((self.client.started, self.monitor.battery, self.monitor.source, self.changes),
                         (1, None, None, []))
        self.client.say("pending")
        self.client.say("unknown")
        self.assertEqual((self.monitor.battery, self.changes), (None, []))

    def test_without_upower_the_battery_comes_from_sysfs_and_is_read_again_on_each_poll(self):
        rc73ya_tree(self.root)
        self.client.say("absent")
        self.assertEqual((self.monitor.battery, self.monitor.source), (Battery(78, DISCHARGING), "sysfs"))
        self.set_capacity(77)
        self.assertEqual(len(self.changes), 1)                       # sysfs cannot say when it changes
        self.monitor.poll()
        self.assertEqual([battery.percent for battery in self.changes], [78, 77])
        for _ in range(3):
            self.monitor.poll()
        self.assertEqual(len(self.changes), 2)                       # nothing changed, so nobody is told

    def test_upower_is_the_source_while_it_answers_and_sysfs_is_left_alone(self):
        rc73ya_tree(self.root)
        with mock.patch.object(headerstatus, "read_sysfs", wraps=headerstatus.read_sysfs) as sysfs:
            self.client.say("ready", Battery(60, CHARGING, 3000))
            for _ in range(3):
                self.monitor.poll()
            self.assertEqual((self.monitor.battery, self.monitor.source, sysfs.call_count),
                             (Battery(60, CHARGING, 3000), "upower", 0))
        self.assertEqual(self.client.refreshes, 0)                   # and a healthy UPower is not asked again

    def test_upower_that_knows_of_no_battery_is_believed(self):
        rc73ya_tree(self.root)
        self.client.say("ready", None)
        self.assertEqual((self.monitor.battery, self.monitor.source), (None, "upower"))

    def test_a_failed_upower_falls_back_to_sysfs_and_is_asked_again_on_each_poll(self):
        rc73ya_tree(self.root)
        self.client.say("failed")
        self.assertEqual((self.monitor.battery.percent, self.monitor.source), (78, "sysfs"))
        self.monitor.poll()
        self.monitor.poll()
        self.assertEqual(self.client.refreshes, 2)
        self.client.say("ready", Battery(77, DISCHARGING, 1000))
        self.assertEqual((self.monitor.source, self.monitor.battery.percent), ("upower", 77))
        self.monitor.poll()
        self.assertEqual(self.client.refreshes, 2)

    def test_upower_vanishing_hands_over_to_sysfs_and_appearing_takes_back(self):
        rc73ya_tree(self.root)
        self.client.say("ready", Battery(60, DISCHARGING, 3000))
        self.client.say("absent")
        self.assertEqual((self.monitor.source, self.monitor.battery), ("sysfs", Battery(78, DISCHARGING)))
        self.client.say("pending")                                   # UPower is back and has not answered yet
        self.assertEqual(self.monitor.source, "sysfs")
        self.client.say("ready", Battery(79, CHARGING, 600))
        self.assertEqual((self.monitor.source, self.monitor.battery), ("upower", Battery(79, CHARGING, 600)))

    def test_a_machine_without_a_battery_shows_none_and_tells_nobody(self):
        self.client.say("absent")                                    # an empty sysfs, as on a desktop or in CI
        self.monitor.poll()
        self.assertEqual((self.monitor.battery, self.monitor.source, self.changes), (None, "sysfs", []))

    def test_a_battery_that_goes_away_is_reported_once(self):
        rc73ya_tree(self.root)
        self.client.say("absent")
        shutil.rmtree(self.root / "BAT1")
        self.monitor.poll()
        self.monitor.poll()
        self.assertEqual(self.changes, [Battery(78, DISCHARGING), None])

    def test_sysfs_is_not_read_before_start_or_after_stop(self):
        rc73ya_tree(self.root)
        self.client.say("absent")
        self.monitor.stop()
        self.assertEqual((self.client.stopped, self.monitor.running), (1, False))
        self.client.say("ready", Battery(10, DISCHARGING))           # a callback that was already queued
        self.set_capacity(50)
        self.monitor.poll()
        self.assertEqual(self.monitor.battery, Battery(78, DISCHARGING))   # what was read last stays, for the next start
        self.monitor.start()
        self.assertEqual(self.client.started, 2)
        self.monitor.poll()                                          # the first look of the new start
        self.assertEqual(self.monitor.battery.percent, 50)


class FakeMonitor:
    def __init__(self, on_change):
        self.on_change, self.battery = on_change, None
        self.running, self.starts, self.stops, self.polls = False, 0, 0, 0

    def start(self):
        self.running = True
        self.starts += 1

    def stop(self):
        self.running = False
        self.stops += 1

    def poll(self):
        self.polls += 1

    def change(self, battery):
        self.battery = battery
        self.on_change()


class HeaderStatusTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(posix_time())
        self.wall, self.timers, self.clocks, self.batteries = Wall(EVENING), Timers(), [], []
        self.status = HeaderStatus(lambda text, date: self.clocks.append((text, date)), self.batteries.append,
                                   make_battery=self.make_monitor, now=self.wall, style=ClockStyle(twelve_hour=True),
                                   schedule=self.timers.schedule, cancel=self.timers.cancel)

    def make_monitor(self, on_change):
        self.monitor = FakeMonitor(on_change)
        return self.monitor

    def test_nothing_runs_until_the_window_is_shown(self):
        self.assertEqual((self.status.running, self.status.armed, self.timers.pending, self.monitor.running,
                          self.clocks, self.batteries, self.wall.reads), (False, False, {}, False, [], [], 0))

    def test_showing_paints_the_time_now_and_arms_one_timer_for_the_next_minute(self):
        self.status.start()
        self.assertEqual(self.clocks, [("9:41 PM", "Monday, 11/23/26")])
        self.assertEqual(self.timers.delays, [52_750 + SLACK_MS])
        self.assertEqual((len(self.timers.pending), self.monitor.starts, self.monitor.running), (1, 1, True))

    def test_each_minute_repaints_the_clock_and_looks_at_the_battery_source(self):
        self.status.start()
        for minute in (42, 43, 44):
            self.wall.moment = datetime(2026, 11, 23, 21, minute, 0, 20_000)
            self.timers.fire()
            self.assertEqual(self.clocks[-1][0], f"9:{minute} PM")
            self.assertEqual(len(self.timers.pending), 1)
        self.assertEqual((self.monitor.polls, self.batteries), (4, []))   # the tick never paints the battery itself

    def test_a_battery_change_repaints_the_battery_and_not_the_clock(self):
        self.status.start()
        painted = len(self.clocks)
        self.monitor.change(Battery(64, CHARGING, 3900))
        self.assertEqual((self.batteries, len(self.clocks), len(self.timers.delays)),
                         ([Battery(64, CHARGING, 3900)], painted, 1))

    def test_hiding_stops_the_timer_and_the_battery(self):
        self.status.start()
        self.status.stop()
        self.status.stop()
        self.assertEqual((self.timers.pending, self.status.armed, self.status.running, self.monitor.running,
                          self.monitor.stops), ({}, False, False, False, 2))

    def test_showing_and_hiding_again_never_holds_more_than_one_timer(self):
        for _ in range(20):
            self.status.start()
            self.assertEqual(len(self.timers.pending), 1)
            self.status.stop()
            self.assertEqual(self.timers.pending, {})
        self.assertEqual(self.monitor.starts, 20)

    def test_becoming_active_again_reads_the_clock_and_the_battery_now(self):
        self.status.start()
        self.wall.moment = datetime(2026, 11, 24, 6, 5, 40)           # after a night of suspend
        polls = self.monitor.polls
        self.status.refresh()
        self.assertEqual(self.clocks[-1], ("6:05 AM", "Tuesday, 11/24/26"))
        self.assertEqual((len(self.timers.pending), self.monitor.polls - polls), (1, 1))
        self.assertEqual(self.timers.delays[-1], 20_000 + SLACK_MS)

    def test_a_window_that_is_not_shown_ignores_becoming_active(self):
        self.status.refresh()
        self.assertEqual((self.clocks, self.timers.pending, self.monitor.polls), ([], {}, 0))

    def test_a_timer_that_ran_late_after_suspend_still_shows_the_right_time(self):
        self.status.start()
        self.wall.moment = datetime(2026, 11, 24, 6, 5, 40)
        self.timers.fire()
        self.assertEqual(self.clocks[-1][0], "6:05 AM")
        self.assertEqual(self.timers.delays[-1], 20_000 + SLACK_MS)

    def test_building_the_widgets_again_repaints_and_starts_and_stops_nothing(self):
        self.status.start()
        self.monitor.change(Battery(78, DISCHARGING, 28173))
        timers, starts, stops = len(self.timers.delays), self.monitor.starts, self.monitor.stops
        for _ in range(25):                                           # a language change builds the header again
            self.status.repaint()
        self.assertEqual((len(self.timers.pending), len(self.timers.delays), self.monitor.starts, self.monitor.stops),
                         (1, timers, starts, stops))
        self.assertEqual(self.batteries[-1], Battery(78, DISCHARGING, 28173))
        self.assertEqual(self.clocks[-1], ("9:41 PM", "Monday, 11/23/26"))

    def test_repainting_a_window_that_is_not_shown_starts_nothing(self):
        self.status.repaint()
        self.assertEqual((self.timers.pending, self.monitor.running, self.status.running), ({}, False, False))
        self.assertEqual((len(self.clocks), self.batteries), (1, [None]))

    def test_the_clock_follows_a_24_hour_locale(self):
        self.status = HeaderStatus(lambda text, date: self.clocks.append((text, date)), self.batteries.append,
                                   make_battery=self.make_monitor, now=self.wall, style=ClockStyle(),
                                   schedule=self.timers.schedule, cancel=self.timers.cancel)
        self.status.start()
        self.assertEqual(self.clocks[-1][0], "21:41")


class WiredTest(unittest.TestCase):
    """The real monitor and the real UPower client on a stub bus, a scratch sysfs and timers fired by hand."""

    def setUp(self):
        self.enterContext(posix_time())
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.root = SCRATCH / "power_supply"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.bus, self.timers, self.wall = StubBus(), Timers(), Wall(EVENING)
        self.clocks, self.batteries, self.clients = [], [], []
        self.status = HeaderStatus(lambda text, date: self.clocks.append(text), self.batteries.append,
                                   make_battery=self.make_battery, now=self.wall, style=ClockStyle(twelve_hour=True),
                                   schedule=self.timers.schedule, cancel=self.timers.cancel)

    def make_battery(self, on_change):
        return BatteryMonitor(on_change, make_client=self.make_client, sysfs_root=str(self.root))

    def make_client(self, on_change):
        self.client = StubClient(on_change)
        return self.client

    def held(self):
        """Everything that can wake the process: timers, bus watches, subscriptions and calls in flight."""
        return {"timers": len(self.timers.pending),
                "watches": len(self.client.watches) - len(self.client.unwatched),
                "subscriptions": len(self.bus.subscriptions), "calls": len(self.bus.pending())}

    def test_a_window_that_is_hidden_holds_nothing_that_could_wake_the_process(self):
        self.assertEqual(self.held(), NOTHING_HELD)                  # built, never shown
        self.status.start()
        self.client.appear(self.bus)                                 # the bus names UPower's owner
        self.assertEqual(self.held(), {"timers": 1, "watches": 1, "subscriptions": 1, "calls": 1})
        self.bus.answer_all()
        self.assertEqual(self.held(), {"timers": 1, "watches": 1, "subscriptions": 1, "calls": 0})
        self.assertEqual(self.batteries[-1], Battery(78, DISCHARGING, 28173))
        self.status.stop()
        self.assertEqual(self.held(), NOTHING_HELD)
        before = (len(self.clocks), len(self.batteries), len(self.bus.calls))
        self.assertEqual(self.bus.emit_changed(":1.42"), 0)          # UPower announces a change: nobody is listening
        self.assertEqual((len(self.clocks), len(self.batteries), len(self.bus.calls)), before)

    def test_showing_and_hiding_over_and_over_never_accumulates_anything(self):
        most = dict(NOTHING_HELD)
        for _ in range(12):
            self.status.start()
            self.client.appear(self.bus)
            most = {key: max(value, self.held()[key]) for key, value in most.items()}
            self.bus.answer_all()
            self.status.repaint()                                    # the widgets are built again meanwhile
            self.status.refresh()                                    # and the window becomes active
            most = {key: max(value, self.held()[key]) for key, value in most.items()}
            self.status.stop()
            self.assertEqual(self.held(), NOTHING_HELD)
        self.assertEqual(most, {"timers": 1, "watches": 1, "subscriptions": 1, "calls": 1})

    def test_with_upower_the_battery_is_event_driven_and_the_minute_tick_asks_nobody(self):
        rc73ya_tree(self.root, capacity=11)                          # sysfs would say something else
        self.status.start()
        self.client.appear(self.bus)
        self.bus.answer_all()
        for minute in range(42, 52):
            self.wall.moment = datetime(2026, 11, 23, 21, minute, 0, 10_000)
            self.timers.fire()
        self.assertEqual(len(self.bus.calls), 1)                     # ten ticks, one read
        self.assertEqual(self.batteries, [Battery(78, DISCHARGING, 28173)])
        self.bus.emit_changed(":1.42")                               # UPower speaks: one more read, answered
        self.bus.answer_all(rc73ya(Percentage=77.0))
        self.assertEqual((len(self.bus.calls), self.batteries[-1].percent), (2, 77))

    def test_without_upower_sysfs_is_read_on_the_minute_tick_and_nothing_else_is_used(self):
        rc73ya_tree(self.root)
        self.status.start()
        self.client.vanish(None)                                     # UPower is not on the bus
        self.assertEqual(self.batteries, [Battery(78, DISCHARGING)])
        (self.root / "BAT1" / "capacity").write_text("77\n", encoding="utf-8")
        (self.root / "BAT1" / "status").write_text("Charging\n", encoding="utf-8")
        self.assertEqual(len(self.batteries), 1)                     # sysfs has no change signal
        self.wall.moment = datetime(2026, 11, 23, 21, 42, 0, 10_000)
        self.timers.fire()
        self.assertEqual((self.clocks[-1], self.batteries[-1]), ("9:42 PM", Battery(77, CHARGING)))
        self.assertEqual((self.bus.calls, self.bus.subscriptions), ([], {}))

    def test_a_machine_without_a_battery_shows_none_whichever_source_is_there(self):
        self.status.start()
        self.client.vanish(None)                                     # no UPower, an empty sysfs: a desktop or CI
        self.wall.moment = datetime(2026, 11, 23, 21, 42, 0, 10_000)
        self.timers.fire()
        self.assertEqual(self.batteries, [])                          # never told of anything: the chip stays hidden
        self.status.repaint()
        self.assertEqual(self.batteries, [None])


class GlibTimerTest(unittest.TestCase):
    def test_a_real_timer_fires_on_the_minute_and_is_really_gone_from_the_main_loop_when_stopped(self):
        wall, painted = Wall(datetime(2026, 11, 23, 21, 41, 59, 900_000)), []

        def on_clock(text, _date):
            painted.append(text)
            wall.moment = datetime(2026, 11, 23, 21, 42, 0, 50_000)    # the minute has come

        status = HeaderStatus(on_clock, lambda _battery: None, make_battery=FakeMonitor, now=wall,
                              style=ClockStyle(twelve_hour=True))
        context = GLib.MainContext.default()
        self.addCleanup(status.stop)
        status.start()
        first = status._ticker._timer
        self.assertIsNotNone(context.find_source_by_id(first))
        self.assertTrue(pump(lambda: len(painted) == 2, timeout=2))
        self.assertEqual(painted, ["9:41 PM", "9:42 PM"])
        self.assertIsNone(context.find_source_by_id(first))            # the one that fired is gone
        second = status._ticker._timer
        self.assertIsNotNone(context.find_source_by_id(second))        # and exactly one new one stands
        status.stop()
        self.assertIsNone(context.find_source_by_id(second))
        self.assertFalse(pump(lambda: len(painted) > 2, timeout=0.2))

    def test_a_real_timer_that_is_replaced_leaves_no_second_source(self):
        wall = Wall(datetime(2026, 11, 23, 21, 41, 7))
        status = HeaderStatus(lambda *_: None, lambda _battery: None, make_battery=FakeMonitor, now=wall,
                              style=ClockStyle())
        context = GLib.MainContext.default()
        self.addCleanup(status.stop)
        status.start()
        tokens = [status._ticker._timer]
        for _ in range(5):
            status.refresh()
            tokens.append(status._ticker._timer)
        alive = [token for token in tokens if context.find_source_by_id(token) is not None]
        self.assertEqual(alive, [tokens[-1]])
        status.stop()
        self.assertEqual([token for token in tokens if context.find_source_by_id(token) is not None], [])


class RuntimeTest(unittest.TestCase):
    def test_the_runtime_hands_out_a_status_that_is_not_running(self):
        from gaminghome.application import Runtime
        status = Runtime.header_status(None, lambda *_: None, lambda _battery: None)
        self.assertIsInstance(status, HeaderStatus)
        self.assertEqual((status.running, status.armed, status.monitor.running), (False, False, False))

    def test_stopping_the_runtime_stops_what_the_window_started(self):
        from gaminghome.application import Runtime
        runtime = Runtime.__new__(Runtime)
        runtime._stopped, runtime._monitors = False, []
        runtime._cancel_timeout = lambda: None
        runtime.window = mock.Mock()
        for name in ("interpreter", "mapper", "inputplumber", "input", "hypr"):
            setattr(runtime, name, mock.Mock())
        Runtime.stop(runtime)
        runtime.window.stop_status.assert_called_once_with()
        Runtime.stop(runtime)                                         # idempotent
        runtime.window.stop_status.assert_called_once_with()


class WindowHandlersTest(unittest.TestCase):
    """The window's own handlers, called on a stand-in for the window: the real one needs a display."""

    @classmethod
    def setUpClass(cls):
        importlib.import_module("gaminghome.application")             # pins Gtk 4 before the window module needs it
        cls.window = importlib.import_module("gaminghome.ui").HomeWindow

    def stand_in(self, active=True, mapped=True):
        return SimpleNamespace(_status=mock.Mock(), is_active=lambda: active, get_mapped=lambda: mapped)

    def test_mapping_starts_the_status_and_unmapping_or_destroying_stops_it(self):
        window = self.stand_in()
        self.window._on_map(window, object())
        window._status.start.assert_called_once_with()
        window._status.stop.assert_not_called()
        for handler in (self.window._on_unmap, self.window._on_unmap):    # unmap, then destroy: the same handler
            handler(window, object())
        self.assertEqual(window._status.stop.call_count, 2)
        self.assertEqual(window._status.start.call_count, 1)

    def test_the_runtime_stops_the_status_through_the_window(self):
        window = self.stand_in()
        self.window.stop_status(window)
        window._status.stop.assert_called_once_with()

    def test_becoming_active_refreshes_only_a_mapped_window(self):
        for active, mapped, refreshed in ((True, True, 1), (True, False, 0), (False, True, 0), (False, False, 0)):
            window = self.stand_in(active, mapped)
            self.window._on_active_changed(window, object(), object())
            self.assertEqual(window._status.refresh.call_count, refreshed, (active, mapped))
            window._status.start.assert_not_called()                      # becoming active never starts anything
            window._status.stop.assert_not_called()


class WindowWiringTest(unittest.TestCase):
    """The window cannot be built without a display, so what it must and must not do is read off its source."""

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "gaminghome" / "ui.py").read_text(encoding="utf-8")
        window = next(node for node in ast.parse(cls.source).body if isinstance(node, ast.ClassDef)
                      and node.name == "HomeWindow")
        cls.methods = {node.name: node for node in window.body if isinstance(node, ast.FunctionDef)}

    def calls(self, name):
        found = []
        for node in ast.walk(self.methods[name]):
            if isinstance(node, ast.Call):
                found.append(node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        return found

    def test_the_status_is_made_once_by_the_constructor_and_hooked_to_map_unmap_and_destroy(self):
        self.assertEqual(sum(self.calls(name).count("header_status") for name in self.methods), 1)
        self.assertIn("header_status", self.calls("__init__"))
        connected = {node.args[0].value: node.args[1].attr for node in ast.walk(self.methods["__init__"])
                     if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "connect"
                     and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)
                     and isinstance(node.args[1], ast.Attribute)}
        for signal, handler in {"map": "_on_map", "unmap": "_on_unmap", "destroy": "_on_unmap",
                                "notify::is-active": "_on_active_changed"}.items():
            self.assertEqual(connected.get(signal), handler, signal)

    def test_building_the_widgets_creates_no_timer_watch_or_status(self):
        for name in ("build", "_build_header"):
            self.assertFalse(set(self.calls(name)) & {"header_status", "start", "stop", "sync", "timeout_add",
                                                      "timeout_add_seconds", "idle_add", "signal_subscribe"}, name)
        self.assertIn("repaint", self.calls("build"))

    def test_the_two_updates_touch_only_their_labels(self):
        for name in ("_update_clock", "_update_battery"):
            self.assertFalse(set(self.calls(name)) & {"refresh", "on_ui_changed", "show_page", "grab_focus",
                                                      "set_focus", "build", "idle_add", "timeout_add"}, name)

    def test_the_battery_glyph_is_not_mirrored_in_a_right_to_left_layout(self):
        # halign=START is resolved with the direction of the widget aligned, so its fill needs the direction too,
        # not only the row it sits in: mirrored, the fill would grow from the terminal's side.
        header = ast.get_source_segment(self.source, self.methods["_build_header"])
        self.assertRegex(header, r"for part in \(glyph, body, self\.battery_level\):\s+"
                                 r"part\.set_direction\(Gtk\.TextDirection\.LTR\)")

    def test_the_chips_are_display_only_and_not_in_the_navigation_rows(self):
        header = ast.get_source_segment(self.source, self.methods["_build_header"])
        chips = header[header.index("# Display only"):header.index("header.append(self.clock_label)")]
        chips = "\n".join(line.partition("#")[0] for line in chips.splitlines())      # code, not comments
        for word in ("Button", "_nav_button", "_hover_selects", "gh-nav", "focusable", "set_sensitive"):
            self.assertNotIn(word, chips)
        rows = ast.get_source_segment(self.source, self.methods["nav_rows"])
        self.assertIn("header = [self.tab_home, self.tab_discover, self.settings_button]", rows)
        for word in ("battery", "clock"):
            self.assertNotIn(word, rows)


if __name__ == "__main__":
    unittest.main()
