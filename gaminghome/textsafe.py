"""Text from another program, made safe to show in the window.

Names and descriptions come from programs and devices that Gaming Home does not control: another program's
application id, the name a Bluetooth device announces. They may hold control characters, characters that reverse
or hide text, combining marks stacked without end, line breaks and any amount of white space, and they may be very
long. ``clean_text`` is the one place that deals with that, so that every module that shows such text does it the
same way.
"""

from __future__ import annotations

import unicodedata

# Format characters reverse, hide or annotate text. The only ones kept are the two joiners some scripts need.
_JOINERS = frozenset("\u200c\u200d")
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Cn"})
_MARKS = frozenset({"Mn", "Me"})
MAX_MARKS = 3    # combining marks in a row on one character: enough for writing, too few to bury the line


def clean_text(text: str, limit: int) -> str:
    """``text`` without control characters, without format characters (those that reverse, hide or annotate text)
    except the zero-width joiner and non-joiner, which some scripts need, with at most ``MAX_MARKS`` combining marks
    in a row, every kind of white space and line break turned into one space, runs of spaces collapsed and the ends
    trimmed, cut to at most ``limit`` characters. Anything that is not a string gives the empty string."""
    if not isinstance(text, str):
        return ""
    kept = []
    marks = 0
    for ch in text:
        if ch.isspace():
            kept.append(" ")
            marks = 0
            continue
        category = unicodedata.category(ch)
        if category in _UNSAFE_CATEGORIES or (category == "Cf" and ch not in _JOINERS):
            continue
        if category in _MARKS:
            marks += 1
            if marks > MAX_MARKS:
                continue
        elif ch not in _JOINERS:                      # a joiner between marks does not begin a new character
            marks = 0
        kept.append(ch)
    return " ".join("".join(kept).split())[:max(limit, 0)].rstrip()
