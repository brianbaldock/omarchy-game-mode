"""What the Home header says about the battery: its reading, the two places it comes from and the words for it.

The reading is built from UPower's DisplayDevice (see ``upower.py``) or, when UPower is not on the bus or has
nothing to say about the machine's battery, from sysfs. Nothing here touches GTK, GLib or D-Bus, and nothing here
raises on missing or garbled data: a reading that cannot be trusted is no reading, and then the header shows no
battery at all.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import NamedTuple

SYSFS_ROOT = "/sys/class/power_supply"

CHARGING = "charging"
DISCHARGING = "discharging"
FULL = "full"
UNKNOWN = "unknown"

LOW_PERCENT = 20        # at or below this, while the battery is being used, the chip warns
CRITICAL_PERCENT = 10   # and at or below this it calls it critical
MAX_ESTIMATE_S = 100 * 3600   # UPower divides by a rate that can be near zero: longer than this is no estimate
MAX_SUPPLIES = 64             # entries of /sys/class/power_supply that are looked at
DIGITS = re.compile(r"[0-9]{1,18}")

# org.freedesktop.UPower.Device: State (u) and Type (u). A pending charge is a battery that is plugged in and
# waits (or holds at a charge limit), so it is shown as charging; a pending discharge is not being charged.
UPOWER_STATES = {1: CHARGING, 2: DISCHARGING, 3: DISCHARGING, 4: FULL, 5: CHARGING, 6: DISCHARGING}
UPOWER_BATTERY = 2

# power_supply status files, in the words of the kernel. "Not charging" is what UPower calls a pending charge.
_PENDING = "pending"
SYSFS_STATUSES = {"charging": CHARGING, "discharging": DISCHARGING, "empty": DISCHARGING, "full": FULL,
                  "not charging": _PENDING}


@dataclass(frozen=True)
class Battery:
    """The battery as the header shows it."""

    percent: int                  # 0 to 100
    state: str = UNKNOWN
    seconds: int | None = None    # until full while charging, until empty while discharging; None when unknown

    @property
    def tone(self) -> str:
        """``bad``, ``warn``, ``ok`` or nothing: the style names the header's status chips already use."""
        if self.state == DISCHARGING:
            if self.percent <= CRITICAL_PERCENT:
                return "bad"
            return "warn" if self.percent <= LOW_PERCENT else ""
        return "ok" if self.state in (CHARGING, FULL) else ""


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _percent(value) -> int | None:
    """A whole percentage from a number, rounding half up; None when it is not one or is out of range."""
    if not _number(value) or not math.isfinite(value):
        return None
    percent = math.floor(value + 0.5)
    return percent if 0 <= percent <= 100 else None


def _estimate(value) -> int | None:
    """Seconds from a time-to-empty or time-to-full property; 0 means UPower does not know."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < MAX_ESTIMATE_S:
        return None
    return value


class CannotSay:
    """What ``from_upower`` answers when UPower's display device is not about this machine's battery."""

    def __repr__(self):
        return "CANNOT_SAY"


CANNOT_SAY = CannotSay()


def from_upower(properties) -> Battery | CannotSay | None:
    """The battery behind UPower's DisplayDevice, None when UPower says there is none, CANNOT_SAY when it cannot tell.

    No battery is what UPower says on a desktop or in a virtual machine: nothing is present. A UPS is another
    matter. A present UPS becomes the whole display device (``Type`` 3) and hides the battery of the laptop
    behind it, so UPower has nothing to say about that battery and the caller has to look elsewhere.
    Peripherals never reach the DisplayDevice: it only adds up the batteries that power the system.
    """
    if not isinstance(properties, dict) or properties.get("IsPresent") is not True:
        return None
    kind = properties.get("Type")
    if isinstance(kind, int) and not isinstance(kind, bool) and kind != UPOWER_BATTERY:
        return CANNOT_SAY
    percent = _percent(properties.get("Percentage"))
    if percent is None:
        return None
    raw = properties.get("State")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return Battery(percent)
    seconds = None
    if raw == 1:
        seconds = _estimate(properties.get("TimeToFull"))
    elif raw in (2, 3, 6):
        seconds = _estimate(properties.get("TimeToEmpty"))
    return Battery(percent, UPOWER_STATES.get(raw, UNKNOWN), seconds)


# ---- sysfs ------------------------------------------------------------------------------------------------

class _Supply(NamedTuple):
    percent: int
    status: str            # a state of this module, or "pending" for the kernel's "Not charging"
    unit: str = ""         # what ``size`` is counted in: "energy" or "charge"
    size: int = 0          # the full capacity in that unit, 0 when the kernel does not say


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(64).strip()
    except OSError:
        return None


def _supply(directory: str) -> _Supply | None:
    """A system battery of ``/sys/class/power_supply``; None for anything else that is listed there."""
    if (_read(f"{directory}/type") or "").casefold() != "battery":
        return None
    if (_read(f"{directory}/scope") or "").casefold() == "device":   # a mouse, a controller, a headset
        return None
    if _read(f"{directory}/present") == "0":                          # an empty bay
        return None
    capacity = _read(f"{directory}/capacity") or ""
    if not DIGITS.fullmatch(capacity) or int(capacity) > 100:
        return None
    status = SYSFS_STATUSES.get((_read(f"{directory}/status") or "").casefold(), UNKNOWN)
    for unit, name in (("energy", "energy_full"), ("charge", "charge_full")):
        size = _read(f"{directory}/{name}") or ""
        if DIGITS.fullmatch(size) and int(size) > 0:
            return _Supply(int(capacity), status, unit, int(size))
    return _Supply(int(capacity), status)


def _combine(supplies: list[_Supply]) -> Battery | None:
    if not supplies:
        return None
    # Several batteries count by their size, which needs every size and one unit (watt-hours or amp-hours).
    sized = len(supplies) > 1 and all(s.size for s in supplies) and len({s.unit for s in supplies}) == 1
    if sized:
        value = sum(s.percent * s.size for s in supplies) / sum(s.size for s in supplies)
    else:
        value = sum(s.percent for s in supplies) / len(supplies)
    statuses = {s.status for s in supplies}
    if CHARGING in statuses:
        state = CHARGING
    elif DISCHARGING in statuses:
        state = DISCHARGING
    elif _PENDING in statuses:
        state = CHARGING
    else:
        state = FULL if statuses == {FULL} else UNKNOWN
    return Battery(math.floor(value + 0.5), state)


def read_sysfs(root: str = SYSFS_ROOT) -> Battery | None:
    """The system's batteries from ``/sys/class/power_supply``, or None when it has none.

    Mains adapters, USB ports and anything whose ``scope`` is ``Device`` are not the machine's battery. The
    kernel has no time estimate to give, so ``seconds`` stays None.
    """
    try:
        with os.scandir(root) as entries:
            names = sorted(entry.name for entry in entries)[:MAX_SUPPLIES]
    except OSError:
        return None
    return _combine([supply for supply in (_supply(f"{root}/{name}") for name in names) if supply])


# ---- words ------------------------------------------------------------------------------------------------

def duration(seconds, t) -> str | None:
    """A time span for the tooltip in the language's units; None when it is not a plausible estimate."""
    if _estimate(seconds) is None:
        return None
    hours, minutes = divmod(max(1, seconds // 60), 60)
    if hours:
        return t("battery.hoursMinutes", hours=hours, minutes=f"{minutes:02d}")
    return t("battery.minutes", minutes=minutes)


def describe(battery: Battery | None, t) -> str | None:
    """The tooltip: the state in words and, when it is known, the time left. None when there is no state to name."""
    words = {CHARGING: "battery.charging", DISCHARGING: "battery.discharging", FULL: "battery.full"}
    if battery is None or battery.state not in words:
        return None
    parts = [t(words[battery.state])]
    span = duration(battery.seconds, t) if battery.state != FULL else None
    if span:
        parts.append(t("battery.untilFull" if battery.state == CHARGING else "battery.remaining", time=span))
    return " · ".join(parts)
