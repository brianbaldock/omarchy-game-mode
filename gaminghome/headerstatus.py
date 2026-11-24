"""The clock and the battery of the Home header: what they show and when they look again.

The window starts this when it is mapped and stops it when it is unmapped, and between the two it owns every
source of wake-ups these two chips have: one timer that fires when the minute changes, and the battery's UPower
watch and subscription. A hidden Gaming Home holds none of them. The window repaints its labels through the two
callbacks and never from a full ``refresh()``.
"""

from __future__ import annotations

import datetime

from gi.repository import GLib

from . import clock
from .battery import SYSFS_ROOT, Battery, read_sysfs
from .upower import STATE_ABSENT, STATE_FAILED, STATE_READY, UPowerClient

SOURCE_UPOWER = "upower"
SOURCE_SYSFS = "sysfs"


def local_now() -> datetime.datetime:
    """The wall clock in the local time zone as it is at this moment.

    GLib looks the zone up again on every call, so a change of time zone is seen while the process runs.
    """
    moment = GLib.DateTime.new_now_local()
    return datetime.datetime(moment.get_year(), moment.get_month(), moment.get_day_of_month(), moment.get_hour(),
                             moment.get_minute(), moment.get_second(), moment.get_microsecond())


def _timeout_add(milliseconds, callback):
    def fire():
        callback()
        return GLib.SOURCE_REMOVE
    return GLib.timeout_add(milliseconds, fire)


class BatteryMonitor:
    """Where the battery comes from: UPower while it is on the bus, sysfs otherwise.

    UPower says when something changed. sysfs cannot, so it is read when the monitor starts and then each time
    ``poll`` is called (the header does that on its minute tick). ``battery`` is what to show; None shows nothing,
    which is right for a machine without a battery and for one whose battery cannot be read.
    """

    def __init__(self, on_change, make_client=UPowerClient, sysfs_root: str = SYSFS_ROOT):
        self.on_change = on_change
        self.battery: Battery | None = None
        self.source: str | None = None
        self.running = False
        self._client = make_client(self._on_client)
        self._root = sysfs_root

    def start(self):
        self.running = True
        self._client.start()

    def stop(self):
        """What was last read stays, so the next start shows it until the answer to its own look arrives."""
        self.running = False
        self._client.stop()

    def poll(self):
        """Look again where nothing says when to: sysfs, and a UPower that failed to answer."""
        if not self.running:
            return
        if self.source == SOURCE_SYSFS:
            self._show(read_sysfs(self._root), SOURCE_SYSFS)
        if self._client.state == STATE_FAILED:
            self._client.refresh()

    def _on_client(self):
        if not self.running:
            return
        state = self._client.state
        if state == STATE_READY:
            self._show(self._client.battery, SOURCE_UPOWER)
        elif state in (STATE_ABSENT, STATE_FAILED):
            self._show(read_sysfs(self._root), SOURCE_SYSFS)

    def _show(self, battery: Battery | None, source: str):
        changed = battery != self.battery
        self.battery, self.source = battery, source
        if changed:
            self.on_change()


class HeaderStatus:
    """Tells the window what to show for the time and for the battery, and when to look again.

    ``on_clock(text, date)`` and ``on_battery(battery)`` repaint the two chips; ``battery`` is None to hide it.
    Everything that can wake the process exists only between ``start()`` and ``stop()``. The remaining
    arguments replace the system in tests: the wall clock, the locale's conventions, the timers, the battery.
    """

    def __init__(self, on_clock, on_battery, make_battery=BatteryMonitor, now=local_now, style=None,
                 schedule=_timeout_add, cancel=GLib.source_remove):
        self._on_clock, self._on_battery = on_clock, on_battery
        self._now = now
        self._style = style or clock.system_style()
        self.monitor = make_battery(self._battery_changed)
        self._ticker = clock.MinuteTicker(self._tick, now, schedule, cancel)

    @property
    def running(self) -> bool:
        return self._ticker.running

    @property
    def armed(self) -> bool:
        """Whether the minute timer exists."""
        return self._ticker.armed

    def start(self):
        """The window is shown: look at the battery and the clock now, and again whenever they change."""
        self.monitor.start()
        self._ticker.start()

    def stop(self):
        """The window is hidden or gone: nothing of this may keep running."""
        self._ticker.stop()
        self.monitor.stop()

    def refresh(self):
        """The window became active again, perhaps after a long suspend: read the clock and sysfs now."""
        self._ticker.sync()

    def repaint(self):
        """The window built its widgets again: show the present state in them. This starts and stops nothing."""
        self._paint(self._now())
        self._on_battery(self.monitor.battery)

    def _tick(self, moment):
        self._paint(moment)
        self.monitor.poll()

    def _paint(self, moment):
        self._on_clock(clock.format_time(moment, self._style), clock.format_date(moment, self._style))

    def _battery_changed(self):
        self._on_battery(self.monitor.battery)
