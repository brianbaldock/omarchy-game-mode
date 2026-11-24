"""A shell.json laid out like the one Omarchy packages, for the tests that read how its bar counts the hours."""

from __future__ import annotations


def shell(format="dddd HH:mm", **entry) -> dict:
    """Omarchy's default shell.json in shape: the bar's layout holds the clock between other entries.

    ``format`` and anything else given replaces what the clock entry holds, so a test can break it in any way.
    """
    clock = {"id": "omarchy.clock", "format": format, "formatAlt": "d MMMM 'W'ww yyyy",
             "verticalFormat": "HH\n\u2014\nmm"}
    clock.update(entry)
    return {"version": 1,
            "idle": {"screensaver": 150, "lock": 300},
            "bar": {"position": "top", "transparent": False, "centerAnchor": "omarchy.clock",
                    "layout": {"left": [{"id": "omarchy.menu"}, {"id": "omarchy.workspaces"}],
                               "center": [{"id": "omarchy.indicators"}, clock, {"id": "omarchy.weather"}],
                               "right": [{"id": "omarchy.tray"}, {"id": "omarchy.power"}]}},
            "plugins": []}
