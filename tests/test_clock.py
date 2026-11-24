"""The clock: 12 or 24 hours from the locale, hour and minute only, and a timer that follows the wall clock.

No display, no locale installed on the machine, no waiting: formats arrive as strings, the wall clock is a
variable and the timers are a dictionary the test fires by hand.
"""

import ast
import locale
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fakes.clockwork import Timers, Wall, posix_time
from gaminghome import clock
from gaminghome.clock import (SLACK_MS, ClockStyle, MinuteTicker, conversions, format_date, format_time,
                              ms_until_next_minute, system_style, uses_twelve_hour)

# What glibc says for these locales (locale -k LC_TIME): t_fmt, t_fmt_ampm, d_fmt, am, pm.
EN_US = dict(time_format="%r", twelve_hour_format="%I:%M:%S %p", date_format="%m/%d/%Y", am="AM", pm="PM")
EN_GB = dict(time_format="%T", twelve_hour_format="%I:%M:%S %p", date_format="%d/%m/%y", am="am", pm="pm")
DE_DE = dict(time_format="%T", twelve_hour_format="", date_format="%d.%m.%Y", am="", pm="")
JA_JP = dict(time_format="%H時%M分%S秒", twelve_hour_format="%p%I時%M分%S秒", date_format="%Y年%m月%d日", am="午前", pm="午後")
KO_KR = dict(time_format="%p %I시 %M분 %S초", twelve_hour_format="%p %I시 %M분 %S초", date_format="%Y년 %m월 %d일", am="오전",
             pm="오후")
AR_SA = dict(time_format="%I:%M:%S %p", twelve_hour_format="%I:%M:%S %p", date_format="%d/%m/%Y", am="ص", pm="م")
POSIX = dict(time_format="%H:%M:%S", twelve_hour_format="", date_format="%m/%d/%y", am="AM", pm="PM")


def at(hour, minute=0, second=0, microsecond=0, **date):
    return datetime(**{"year": 2026, "month": 11, "day": 23, **date}, hour=hour, minute=minute, second=second,
                    microsecond=microsecond)


class TwelveHourTest(unittest.TestCase):
    def test_what_the_c_library_says_for_each_locale(self):
        for name, formats, twelve in (("en_US", EN_US, True), ("en_GB", EN_GB, False), ("de_DE", DE_DE, False),
                                      ("ja_JP", JA_JP, False), ("ko_KR", KO_KR, True), ("ar_SA", AR_SA, True),
                                      ("C", POSIX, False)):
            self.assertEqual(uses_twelve_hour(formats["time_format"]), twelve, name)
            self.assertEqual(ClockStyle.from_formats(**formats).twelve_hour, twelve, name)

    def test_any_way_of_counting_to_twelve_is_twelve_hours(self):
        for pattern in ("%r", "%I:%M:%S %p", "%l:%M:%S %p", "%-I:%M %P", "%EI:%M", "%_I:%M", "%p %I", "x %OI y"):
            self.assertTrue(uses_twelve_hour(pattern), pattern)

    def test_any_way_of_counting_to_twenty_four_is_twenty_four_hours(self):
        for pattern in ("%T", "%R", "%H:%M:%S", "%k:%M", "%-H.%M", "%OH:%M", "%H時%M分", "", "%X"):
            self.assertFalse(uses_twelve_hour(pattern), pattern)

    def test_a_literal_percent_sign_is_not_a_conversion(self):
        self.assertEqual(conversions("100%% %I%%p %p"), ["I", "p"])
        self.assertFalse(uses_twelve_hour("%H%%I"))
        self.assertFalse(uses_twelve_hour("%H%"))
        self.assertTrue(uses_twelve_hour("%H %%%r"))

    def test_nothing_in_the_input_can_make_it_raise(self):
        for junk in (None, "", "%", "%%", "%-", "%E", "plain text", "\u0000%I"):
            uses_twelve_hour(junk)
            ClockStyle.from_formats(time_format=junk, date_format=junk, am=None, pm=None)


class StyleTest(unittest.TestCase):
    def test_the_marker_comes_where_the_locale_puts_it(self):
        self.assertFalse(ClockStyle.from_formats(**EN_US).marker_first)
        self.assertFalse(ClockStyle.from_formats(**AR_SA).marker_first)
        self.assertTrue(ClockStyle.from_formats(**KO_KR).marker_first)
        # %r stands for the 12-hour format the locale names separately; without a name it is the POSIX one
        self.assertTrue(ClockStyle.from_formats("%r", "%p %I:%M:%S").marker_first)
        self.assertFalse(ClockStyle.from_formats("%r", "").marker_first)
        self.assertFalse(ClockStyle.from_formats(**DE_DE).marker_first)         # 24 hours: no marker at all

    def test_the_day_comes_before_the_month_where_the_locale_writes_it_so(self):
        for formats, day_first in ((EN_US, False), (EN_GB, True), (DE_DE, True), (JA_JP, False), (KO_KR, False),
                                   (AR_SA, True), (POSIX, False)):
            self.assertEqual(ClockStyle.from_formats(**formats).day_first, day_first, formats["date_format"])
        self.assertFalse(ClockStyle.from_formats(date_format="").day_first)
        self.assertFalse(ClockStyle.from_formats(date_format="%Y").day_first)
        self.assertTrue(ClockStyle.from_formats(date_format="%e %b %Y").day_first)

    def test_markers_that_the_locale_leaves_empty_fall_back_to_am_and_pm(self):
        style = ClockStyle.from_formats(time_format="%r", am="", pm="  ")
        self.assertEqual((style.am, style.pm), ("AM", "PM"))
        style = ClockStyle.from_formats(**JA_JP)
        self.assertEqual((style.am, style.pm), ("午前", "午後"))

    def test_the_style_of_this_process_is_read_from_the_c_library(self):
        style = system_style()
        self.assertIsInstance(style, ClockStyle)
        self.assertEqual(style.twelve_hour, uses_twelve_hour(locale.nl_langinfo(locale.T_FMT)))

    def test_a_c_library_without_the_answers_leaves_the_posix_defaults(self):
        from unittest import mock
        with mock.patch.object(clock.locale, "nl_langinfo", side_effect=ValueError("no such item")):
            self.assertEqual(system_style(), ClockStyle())
        with mock.patch.object(clock, "locale", mock.Mock(spec=[])):
            self.assertEqual(system_style(), ClockStyle())


class FormatTest(unittest.TestCase):
    def test_a_24_hour_clock_is_two_digits_each_without_seconds(self):
        style = ClockStyle.from_formats(**DE_DE)
        for moment, text in ((at(0, 0), "00:00"), (at(0, 5), "00:05"), (at(9, 41, 59), "09:41"), (at(12, 0), "12:00"),
                             (at(13, 7), "13:07"), (at(21, 41, 7), "21:41"), (at(23, 59, 59, 999999), "23:59")):
            self.assertEqual(format_time(moment, style), text)

    def test_a_12_hour_clock_has_no_leading_zero_and_a_marker(self):
        style = ClockStyle.from_formats(**EN_US)
        for moment, text in ((at(0, 0), "12:00 AM"), (at(0, 5), "12:05 AM"), (at(1, 0), "1:00 AM"),
                             (at(9, 41), "9:41 AM"), (at(11, 59), "11:59 AM"), (at(12, 0), "12:00 PM"),
                             (at(12, 59), "12:59 PM"), (at(13, 7), "1:07 PM"), (at(21, 41, 7), "9:41 PM"),
                             (at(23, 59, 59), "11:59 PM")):
            self.assertEqual(format_time(moment, style), text)

    def test_the_marker_and_its_place_follow_the_locale(self):
        self.assertEqual(format_time(at(21, 41), ClockStyle.from_formats(**KO_KR)), "오후 9:41")
        self.assertEqual(format_time(at(0, 5), ClockStyle.from_formats(**KO_KR)), "오전 12:05")
        self.assertEqual(format_time(at(21, 41), ClockStyle.from_formats(**AR_SA)), "9:41 م")
        self.assertEqual(format_time(at(21, 41), ClockStyle(twelve_hour=True, am="a.m.", pm="p.m.")), "9:41 p.m.")

    def test_the_date_is_weekday_month_and_day_in_the_locales_order(self):
        with posix_time():
            self.assertEqual(format_date(at(21, 41), ClockStyle()), "Monday, November 23")
            self.assertEqual(format_date(at(21, 41), ClockStyle(day_first=True)), "Monday, 23 November")
            self.assertEqual(format_date(at(0, 0, day=8, month=3), ClockStyle()), "Sunday, March 8")   # no zero padding

    def test_the_minute_is_never_rounded_up_by_the_seconds(self):
        style = ClockStyle(twelve_hour=True)
        self.assertEqual(format_time(at(9, 41, 59, 999999), style), "9:41 AM")


class NextMinuteTest(unittest.TestCase):
    def test_the_delay_reaches_just_past_the_next_minute(self):
        for moment, milliseconds in ((at(21, 41, 0), 60_000), (at(21, 41, 0, 1000), 59_999), (at(21, 41, 30, 500_000), 29_500),
                                     (at(21, 41, 59, 0), 1_000), (at(21, 41, 59, 900_000), 100),
                                     (at(21, 41, 59, 999_999), 1), (at(21, 41, 59, 999_000), 1)):
            self.assertEqual(ms_until_next_minute(moment), milliseconds + SLACK_MS, moment)

    def test_the_slack_keeps_the_timer_on_the_far_side_of_the_boundary(self):
        self.assertGreater(SLACK_MS, 0)
        self.assertLess(SLACK_MS, 1000)

    def test_a_change_of_offset_does_not_move_the_boundary(self):
        # New York, 2026-03-08: clocks jump from 01:59:59.5 EST to 03:00:00.0 EDT. Only the seconds matter.
        est, edt = timezone(timedelta(hours=-5)), timezone(timedelta(hours=-4))
        before = datetime(2026, 3, 8, 1, 59, 59, 500_000, tzinfo=est)
        after = datetime(2026, 3, 8, 3, 0, 0, 0, tzinfo=edt)
        self.assertEqual(after - before, timedelta(milliseconds=500))      # half a second later, an hour on the dial
        self.assertEqual(ms_until_next_minute(before), 500 + SLACK_MS)
        self.assertEqual(ms_until_next_minute(after), 60_000 + SLACK_MS)
        self.assertEqual(format_time(after, ClockStyle(twelve_hour=True)), "3:00 AM")
        # And back: the clock set from 02:30 back to 01:30 loses nothing either.
        self.assertEqual(ms_until_next_minute(datetime(2026, 11, 1, 1, 30, 15, tzinfo=edt)), 45_000 + SLACK_MS)
        self.assertEqual(ms_until_next_minute(datetime(2026, 11, 1, 1, 30, 15, tzinfo=est)), 45_000 + SLACK_MS)


class MinuteTickerTest(unittest.TestCase):
    def setUp(self):
        self.wall, self.timers, self.shown = Wall(at(21, 41, 7, 250_000)), Timers(), []
        self.ticker = MinuteTicker(self.shown.append, self.wall, self.timers.schedule, self.timers.cancel)

    def test_nothing_exists_until_it_is_started(self):
        self.assertEqual((self.ticker.running, self.ticker.armed, self.timers.pending, self.shown, self.wall.reads),
                         (False, False, {}, [], 0))
        self.ticker.sync()                                  # a window that was never shown becomes active
        self.assertEqual((self.timers.pending, self.shown), ({}, []))

    def test_starting_shows_the_time_now_and_arms_one_timer_for_the_next_minute(self):
        self.ticker.start()
        self.assertEqual(self.shown, [at(21, 41, 7, 250_000)])
        self.assertEqual(self.timers.delays, [52_750 + SLACK_MS])
        self.assertEqual(len(self.timers.pending), 1)
        self.assertTrue(self.ticker.armed)

    def test_each_tick_shows_the_new_minute_and_leaves_exactly_one_timer(self):
        self.ticker.start()
        for minute in range(42, 48):
            self.wall.moment = at(21, minute, 0, 12_000)
            self.timers.fire()
            self.assertEqual(self.shown[-1].minute, minute)
            self.assertEqual(len(self.timers.pending), 1)
        self.assertEqual(self.timers.delays[1:], [60_000 - 12 + SLACK_MS] * 6)
        self.assertEqual(self.timers.cancelled, [])                  # a timer that fired is not cancelled

    def test_stopping_cancels_the_timer_and_a_late_callback_does_nothing(self):
        self.ticker.start()
        _, callback = next(iter(self.timers.pending.items()))
        self.ticker.stop()
        self.assertEqual((self.timers.pending, self.ticker.armed, self.ticker.running), ({}, False, False))
        shown = len(self.shown)
        callback()                                                    # a source GLib had already dispatched
        self.assertEqual((len(self.shown), self.timers.pending), (shown, {}))
        self.ticker.stop()                                            # idempotent
        self.assertEqual(self.timers.cancelled, [1])

    def test_starting_again_never_leaves_two_timers(self):
        for _ in range(5):
            self.ticker.start()
            self.assertEqual(len(self.timers.pending), 1)
        self.assertEqual(len(self.timers.cancelled), 4)
        self.ticker.stop()
        for _ in range(5):
            self.ticker.start()
            self.ticker.stop()
            self.assertEqual(self.timers.pending, {})

    def test_a_timer_that_ran_late_after_suspend_shows_the_time_it_is_now(self):
        self.ticker.start()
        self.wall.moment = at(5, 3, 12, day=24)                       # eight hours later, the timer fires on resume
        self.timers.fire()
        self.assertEqual(self.shown[-1], at(5, 3, 12, day=24))
        self.assertEqual(self.timers.delays[-1], 48_000 + SLACK_MS)

    def test_the_wall_clock_moving_backwards_or_a_new_zone_corrects_itself_at_the_next_tick(self):
        self.ticker.start()
        self.wall.moment = at(18, 41, 55)                             # the zone changed by three hours
        self.timers.fire()
        self.assertEqual((self.shown[-1].hour, self.timers.delays[-1]), (18, 5_000 + SLACK_MS))
        self.wall.moment = at(18, 20, 0)                              # and the clock was set back
        self.timers.fire()
        self.assertEqual((self.shown[-1].minute, self.timers.delays[-1]), (20, 60_000 + SLACK_MS))

    def test_syncing_reads_the_clock_again_and_replaces_the_timer(self):
        self.ticker.start()
        self.wall.moment = at(21, 55, 30)
        self.ticker.sync()                                            # the window became active again
        self.assertEqual(self.shown[-1].minute, 55)
        self.assertEqual((len(self.timers.pending), self.timers.delays[-1]), (1, 30_000 + SLACK_MS))
        self.assertEqual(self.timers.cancelled, [1])

    def test_one_reading_of_the_clock_serves_the_tick_and_the_next_delay(self):
        # Two readings could straddle the minute: the label shows 21:41 and the timer waits a whole minute.
        self.ticker.start()
        reads = self.wall.reads
        self.wall.moment = at(21, 41, 59, 999_000)                    # a timer that fired a moment early
        self.timers.fire()
        self.assertEqual(self.wall.reads - reads, 1)
        self.assertEqual(self.shown[-1].minute, 41)
        self.assertEqual(self.timers.delays[-1], 1 + SLACK_MS)        # so the next tick shows 21:42
        self.wall.moment = at(21, 42, 0, 31_000)
        self.timers.fire()
        self.assertEqual(self.shown[-1].minute, 42)

    def test_a_failing_callback_does_not_stop_the_clock(self):
        def fail(_moment):
            raise RuntimeError("the label is gone")

        ticker = MinuteTicker(fail, self.wall, self.timers.schedule, self.timers.cancel)
        with self.assertRaises(RuntimeError):
            ticker.start()
        self.assertEqual(len(self.timers.pending), 1)                 # still ticking
        with self.assertRaises(RuntimeError):
            self.timers.fire()
        self.assertEqual(len(self.timers.pending), 1)

    def test_a_callback_that_stops_the_ticker_leaves_no_timer(self):
        ticker = MinuteTicker(lambda _moment: ticker.stop(), self.wall, self.timers.schedule, self.timers.cancel)
        ticker.start()
        self.assertEqual((self.timers.pending, ticker.running), ({}, False))

    def test_a_callback_that_syncs_the_ticker_does_not_double_the_timer(self):
        calls = []

        def resync(_moment):
            calls.append(1)
            if len(calls) == 1:
                ticker.sync()

        ticker = MinuteTicker(resync, self.wall, self.timers.schedule, self.timers.cancel)
        ticker.start()
        self.assertEqual((len(calls), len(self.timers.pending), len(self.timers.delays)), (2, 1, 1))


class NoGtkTest(unittest.TestCase):
    def test_the_clock_needs_neither_gtk_nor_glib(self):
        modules = set()
        for node in ast.walk(ast.parse(Path(clock.__file__).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertFalse(modules & {"gi", "Gtk", "GLib", "Gio"}, modules)


if __name__ == "__main__":
    unittest.main()
