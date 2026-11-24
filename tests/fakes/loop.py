"""Run the GLib main context from a test, bounded by a deadline."""

from __future__ import annotations

import time
import warnings

from gi.repository import GLib


def pump(predicate=lambda: False, timeout=3.0) -> bool:
    """Run the default main context until ``predicate`` holds; False on timeout."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    with warnings.catch_warnings():
        # PyGObject's asyncio glue warns about asyncio policies on new Pythons.
        warnings.simplefilter("ignore", DeprecationWarning)
        while not predicate():
            if time.monotonic() > deadline:
                return False
            if not context.iteration(False):
                time.sleep(0.001)
    return True
