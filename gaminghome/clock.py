"""The clock in the Home header: how it counts hours, what it says and when it looks again.

Nothing here touches GTK, GLib or the system. The wall clock, the locale's formats and the timers are handed
in, so every rule can be driven by a test without a display, a locale or any waiting.
"""

from __future__ import annotations

import locale
import re
from dataclasses import dataclass, replace

# Added to the time left in the minute, so a timer lands just after the next minute begins and never just before it.
SLACK_MS = 30
DEFAULT_TWELVE_HOUR_FORMAT = "%I:%M:%S %p"

# One strftime conversion: flags, width, an E or O modifier, then its letter. "%%" is a literal percent sign.
_CONVERSION = re.compile(r"%[-_0^#]*[0-9]*[EO]?([A-Za-z%])")
_TWELVE_HOUR = frozenset("rIlpP")   # %r is the locale's own 12-hour time; the rest count to 12 or mark am/pm
_HOUR_12, _MARKER = frozenset("Il"), frozenset("pP")


def conversions(pattern: str) -> list[str]:
    """The letters of a strftime pattern's conversions, in order, without literal percent signs."""
    return [letter for letter in _CONVERSION.findall(pattern or "") if letter != "%"]


def uses_twelve_hour(time_format: str) -> bool:
    """Whether a locale's time format (``nl_langinfo(T_FMT)``) counts hours on a 12-hour clock."""
    return not _TWELVE_HOUR.isdisjoint(conversions(time_format))


def _first(order: list[str], letters: frozenset) -> int | None:
    return next((index for index, letter in enumerate(order) if letter in letters), None)


@dataclass(frozen=True)
class ClockStyle:
    """The conventions of a locale that the header follows. The defaults are those of the POSIX locale."""

    twelve_hour: bool = False
    am: str = "AM"
    pm: str = "PM"
    marker_first: bool = False   # am/pm comes before the time, as in Korean or Chinese

    @classmethod
    def from_formats(cls, time_format="", twelve_hour_format="", am="", pm="") -> "ClockStyle":
        """Read the style off a locale's formats (``T_FMT``, ``T_FMT_AMPM``) and am/pm markers.

        The marker's place is the one of the locale's 12-hour time even when its usual time is a 24-hour one, since
        the hours can be asked to count to 12 (see ``counting``).
        """
        twelve = uses_twelve_hour(time_format)
        layout = time_format
        if not twelve or "r" in conversions(time_format):
            layout = twelve_hour_format or DEFAULT_TWELVE_HOUR_FORMAT
        order = conversions(layout)
        marker, hour = _first(order, _MARKER), _first(order, _HOUR_12)
        return cls(twelve_hour=twelve, am=(am or "").strip() or "AM", pm=(pm or "").strip() or "PM",
                   marker_first=marker is not None and hour is not None and marker < hour)

    def counting(self, twelve_hour: bool | None) -> "ClockStyle":
        """This style with the hours counted to 12 (True) or to 24 (False); unchanged when None."""
        return self if twelve_hour is None else replace(self, twelve_hour=twelve_hour)


def system_style() -> ClockStyle:
    """The style of the locale this process runs in, as the C library reports it."""
    langinfo = getattr(locale, "nl_langinfo", None)
    if langinfo is None:
        return ClockStyle()
    try:
        return ClockStyle.from_formats(langinfo(locale.T_FMT), langinfo(locale.T_FMT_AMPM), langinfo(locale.AM_STR),
                                       langinfo(locale.PM_STR))
    except (AttributeError, ValueError, OSError):
        return ClockStyle()


def format_time(moment, style: ClockStyle) -> str:
    """Hour and minute, no seconds: ``21:41``, or ``9:41 PM`` on a 12-hour clock (noon is 12 PM, midnight 12 AM)."""
    if not style.twelve_hour:
        return f"{moment.hour:02d}:{moment.minute:02d}"
    marker = style.pm if moment.hour >= 12 else style.am
    clock = f"{moment.hour % 12 or 12}:{moment.minute:02d}"
    return f"{marker} {clock}" if style.marker_first else f"{clock} {marker}"


def format_date(moment) -> str:
    """The weekday and the date as the locale this process runs in writes them: ``Monday, 11/23/2026`` in en_US.

    A locale whose own date already names the weekday (Arabic does) does not get it twice.
    """
    weekday, date = moment.strftime("%A"), moment.strftime("%x")
    return date if weekday in date else f"{weekday}, {date}"


def ms_until_next_minute(moment) -> int:
    """Milliseconds from ``moment`` until the wall clock shows the next minute, plus a little slack.

    Only the seconds are used, never the hour or the zone, so a change of offset cannot skew it.
    """
    return 60_000 - (moment.second * 1000 + moment.microsecond // 1000) + SLACK_MS


class MinuteTicker:
    """Calls ``callback(moment)`` now, and again each time the wall clock reaches a new minute, until stopped.

    At most one timer exists, and it is always armed from the clock as it reads at that tick. A timer that ran
    late (the machine was suspended) or a clock that moved (time zone, NTP) therefore corrects itself at the next
    tick. ``schedule(milliseconds, function)`` returns a token that ``cancel`` takes back.
    """

    def __init__(self, callback, now, schedule, cancel):
        self._callback, self._now, self._schedule, self._cancel = callback, now, schedule, cancel
        self._timer = None
        self.running = False

    @property
    def armed(self) -> bool:
        return self._timer is not None

    def start(self):
        self.running = True
        self.sync()

    def sync(self):
        """Read the clock now and arm the next tick from it, in place of whatever timer there was."""
        if not self.running:
            return
        self._disarm()
        self._tick()

    def stop(self):
        self.running = False
        self._disarm()

    def _disarm(self):
        timer, self._timer = self._timer, None
        if timer is not None:
            self._cancel(timer)

    def _on_timer(self):
        self._timer = None
        if self.running:
            self._tick()

    def _tick(self):
        # One reading serves both the callback and the delay: two could straddle the minute and wait a whole one.
        moment = self._now()
        try:
            self._callback(moment)
        finally:
            if self.running and self._timer is None:
                self._timer = self._schedule(ms_until_next_minute(moment), self._on_timer)
