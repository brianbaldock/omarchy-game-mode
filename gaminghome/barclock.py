"""How Omarchy's bar counts the hours of its clock, so that the header's clock can say the same.

The bar's clock is an entry of the ``shell.json`` that Omarchy's shell reads, somewhere inside it:
``{"id": "omarchy.clock", "format": "dddd HH:mm", ...}``. Its ``format`` is a Qt date format. There are two such
files, the user's own and the packaged default behind it. This module only reads them, when it is asked to and
never before: nothing here writes, watches or polls, and a file that is missing, unreadable or not what it should
be is simply a file that does not say, so that the caller goes on to the next source.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .settings import config_home

CLOCK_ID = "omarchy.clock"
OMARCHY_PATH = "/usr/share/omarchy"   # where the package puts Omarchy, unless the environment says otherwise
MAX_BYTES = 1024 * 1024


def twelve_hour_format(time_format) -> bool | None:
    """Whether a Qt time format counts hours to 12 (True) or to 24 (False); None when it shows no hour at all.

    Text in single quotes is literal, and so is whatever follows an opening quote that is never closed. Outside
    of it an ``a``, ``A``, ``ap`` or ``AP`` asks for AM/PM and the hours count to 12; with none of them, ``h``,
    ``hh``, ``H`` and ``HH`` count to 24.
    """
    if not isinstance(time_format, str):
        return None
    bare = "".join(time_format.split("'")[::2])      # the pieces outside the quotes
    if "h" not in bare and "H" not in bare:
        return None
    return "a" in bare or "A" in bare


def clock_format(document) -> str | None:
    """The ``format`` of the first ``omarchy.clock`` entry of a parsed shell.json, wherever it sits in it.

    Only a dictionary whose ``id`` is the clock's and whose ``format`` is a string counts. ``formatAlt``, the
    format the bar shows on a click, and ``verticalFormat`` are not looked at.
    """
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("id") == CLOCK_ID and isinstance(node.get("format"), str):
                return node["format"]
            pending.extend(reversed(list(node.values())))     # the first entry of the file is looked at first
        elif isinstance(node, list):
            pending.extend(reversed(node))
    return None


def config_paths(env=os.environ) -> list[Path]:
    """The files that can name the bar's clock format: the user's own first, then the packaged default."""
    omarchy = Path(env.get("OMARCHY_PATH") or OMARCHY_PATH)
    return [config_home(env) / "omarchy" / "shell.json", omarchy / "config" / "omarchy" / "shell.json"]


def _open_without_blocking(path, flags):
    return os.open(path, flags | os.O_NONBLOCK)


def _read_json(path):
    """What a file holds as JSON; None when it cannot be read, is too large, is not a regular file or is not JSON."""
    try:
        with open(path, "rb", opener=_open_without_blocking) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):   # a pipe or a device must not make us wait
                return None
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


def bar_twelve_hour(paths=None) -> bool | None:
    """How the bar counts its clock's hours: True for 12, False for 24, None when no file says.

    The first file with a clock format that shows an hour decides, so the user's own file wins over the packaged
    default. ``paths`` replaces the files that are looked at.
    """
    for path in config_paths() if paths is None else paths:
        counted = twelve_hour_format(clock_format(_read_json(path)))
        if counted is not None:
            return counted
    return None
