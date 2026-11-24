"""Time under a test's control: timers it fires by hand, a wall clock it moves, and a locale it pins."""

from __future__ import annotations

import contextlib
import locale


class Timers:
    """Timers the test fires by hand. Cancelling one that is not pending is an error, as it is for GLib."""

    def __init__(self):
        self.pending: dict[int, object] = {}
        self.delays: list[int] = []
        self.cancelled: list[int] = []
        self._count = 0

    def schedule(self, milliseconds, function):
        self._count += 1
        self.pending[self._count] = function
        self.delays.append(milliseconds)
        return self._count

    def cancel(self, token):
        del self.pending[token]     # a KeyError: a timer that already fired, or one that never was
        self.cancelled.append(token)

    def fire(self):
        (token, function), = self.pending.items()
        del self.pending[token]
        function()


class Wall:
    """A wall clock that stands still until a test moves it, and counts how often it was read."""

    def __init__(self, moment):
        self.moment, self.reads = moment, 0

    def __call__(self):
        self.reads += 1
        return self.moment


@contextlib.contextmanager
def posix_time():
    """Month and day names in English, whatever locale the process was started with."""
    previous = locale.setlocale(locale.LC_TIME)
    locale.setlocale(locale.LC_TIME, "C")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_TIME, previous)
