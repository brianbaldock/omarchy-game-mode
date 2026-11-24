"""Text from another program, made safe to show in the window.

Names and descriptions come from programs and devices that Gaming Home does not control: another program's
application id, the name a Bluetooth device announces. They may hold control characters, characters that reverse
or hide text, line breaks and any amount of white space, and they may be very long. ``clean_text`` is the one place
that deals with that, so that every module that shows such text does it the same way.
"""

from __future__ import annotations

import unicodedata

# Invisible or direction-changing characters (format characters that are not part of writing a language).
_HIDDEN = frozenset("\u061c\u200b\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff"
                    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Cn"})


def clean_text(text: str, limit: int) -> str:
    """``text`` without control characters and without the ones that reverse or hide text, every kind of white
    space and line break turned into one space, runs of spaces collapsed and the ends trimmed, cut to at most
    ``limit`` characters. Zero-width joiners stay, because some scripts need them. Anything that is not a string
    gives the empty string."""
    if not isinstance(text, str):
        return ""
    kept = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif ch not in _HIDDEN and unicodedata.category(ch) not in _UNSAFE_CATEGORIES:
            kept.append(ch)
    return " ".join("".join(kept).split())[:max(limit, 0)].rstrip()
