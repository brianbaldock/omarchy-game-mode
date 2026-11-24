"""What Gaming Home shows and asks of the machine's power profile, apart from where the facts come from.

A power profile is one of the three that power-profiles-daemon (PPD) offers: power-saver, balanced and performance.
On hardware that backs them (a firmware platform profile, the CPU's boost and energy preference) a profile changes
real limits and not only a label; on a machine where nothing backs a profile, PPD's placeholder driver changes
nothing, and this module does not offer such a profile. The names are PPD's own, which are also Omarchy's and the
desktop's, so there is nothing to translate between.

This module is the contract between the window (``ui.py``) and whatever supplies the profile: the PPD client
(``powerprofiles.py``) in the app and a scripted stand-in in the tests. It holds plain data, a few pure helpers
and the ``Power`` protocol. It imports nothing from GTK, GLib or D-Bus.

Who decides
-----------
PPD's active profile is what is in force now, and a source shows it live: Gaming Home keeps no memory of its own.
Others change it too. Omarchy's shell switches on every plug and unplug, to the profile Omarchy remembers for that
power source; asusd does the same with its own AC and battery profiles; a game launcher that uses Feral's GameMode
sets one per game. Gaming Home does not fight them: it never switches again after another program has changed the
profile, it says so (``CHANGED_ELSEWHERE``) and leaves it there. Taking a hold for a game (a later, opt-in feature)
belongs to the resident core, which runs while the window is hidden, on a connection of its own. A source of this
module never takes one.

Lifecycle
---------
``start()`` is called when Gaming Home is shown and ``stop()`` when it is hidden, closed or the app quits. Between
the two a source may watch the PPD service. Outside them it holds nothing at all: no timer, no bus watch or
subscription and no call in flight (but see the helper below). Both are idempotent, ``start()`` never starts the
service (starting PPD would stop the other power managers it conflicts with), and ``on_change`` is not called once
``stop()`` has returned. Nothing is polled: PPD says when the profile changes, whoever changed it.

``stop()`` forgets a request that is still on its way. The service may well carry it out, but the source reports
nothing about it, clears ``pending`` and starts no timer; the next ``start()`` reads the service again. The one
process a source starts is the short helper that makes Omarchy remember a choice. It may finish after ``stop()``,
because ending it would lose the choice at the next plug or unplug: it is bounded by its own time limit, killed
with everything it started if that runs out, reaped, and never reports back. What was last read stays across
``stop()`` and ``start()``, so that the header does not blink while the first reading is on its way; ``pending``,
``failure`` and ``note`` do not. The window gets its source from a ``PowerFactory``.

Changes
-------
``on_change()`` is called on the main loop after ``snapshot`` has changed, and a burst of changes may be reported
once. Reading ``snapshot`` is cheap and never blocks. ``set_profile`` returns at once: progress shows as
``snapshot.pending``, the outcome as ``snapshot.active`` changing or as ``snapshot.failure``. No method raises.

Operations
----------
* ``set_profile(profile)``:
  * ends at once with ``UNAVAILABLE`` when the state is not ``READY``, and with ``UNSUPPORTED`` for a profile that is
    not in ``available``; it is ignored while ``snapshot.pending`` is set;
  * when the profile is already the active one nothing is asked of the service (it would do nothing, and would leave
    another program's hold in place), but Omarchy is still told to remember it;
  * otherwise it asks the service and ``pending`` holds the profile until the answer. The answer is the outcome: the
    service applies a profile before it replies, so there is no waiting for it to take hold. ``DENIED`` is the
    system refusing this session, ``UNAVAILABLE`` the service going away, ``TIMEOUT`` no answer in time and ``FAILED``
    anything else. After a ``TIMEOUT`` the failure goes again if the service later reports that profile;
  * afterwards the source tells Omarchy to remember the choice for the current power source, with Omarchy's own
    command when it is installed (a profile set straight in PPD is forgotten at the next plug or unplug). That is
    bookkeeping: when it fails the profile is set all the same and the note ``NOT_REMEMBERED`` says so.
* ``retry`` reads the service again after ``ERROR``. ``dismiss`` clears the failure and the note, and so does a new
  ``set_profile``.

Holds
-----
Another program may hold a profile for as long as it runs. A hold is listed in ``snapshot.holds`` but is in force
only while ``active`` is that profile: a change by another program does not release it, and when holds disagree
power-saver wins. Choosing a different profile releases **every** hold, so the window says whose request a choice
ends. A program's name is whatever it called itself, cleaned and short.

Evidence
--------
``snapshot.evidence`` is what the kernel was told once the profile is set: the firmware's platform profile and
whether the CPU may boost. A change is never judged by it: the kernel keeps what it was asked for and does not
check the firmware's answer, other programs write some of these files too (asusd sets the CPU's energy preference
with the profile), and PPD itself says when the profile changed. Its use is to show the window and the doctor when
the hardware's view and Gaming Home's differ. The amdgpu power caps are deliberately not part of it: they read the
same in every profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from .textsafe import clean_text

# The profiles, from the least power to the most. These are power-profiles-daemon's own names.
SAVER, BALANCED, PERFORMANCE = "power-saver", "balanced", "performance"
PROFILES = (SAVER, BALANCED, PERFORMANCE)

HOLD_NAME_MAX = 32    # the longest name of a program that holds a profile

# What is known about the service.
UNKNOWN = "unknown"    # nothing has been read yet: the window is not shown, or the service has not answered
ABSENT = "absent"      # no power-profiles service is running
READY = "ready"        # the service answered
ERROR = "error"        # the service is there and could not be read; ``retry`` asks again

# Why the service holds performance back. It may name several.
HEAT = "heat"          # the machine is too hot
LAP = "lap"            # the machine is on a lap (laptops with a proximity sensor)
LIMITED = "limited"    # any other reason

# Why a change failed. A source maps whatever the system says onto exactly one of these.
DENIED = "denied"            # the system does not let this session change the profile
UNSUPPORTED = "unsupported"  # the machine offers no such profile
TIMEOUT = "timeout"          # nothing answered in time
UNAVAILABLE = "unavailable"  # the service cannot be reached, or went away meanwhile
FAILED = "failed"            # anything else
FAILURES = (DENIED, UNSUPPORTED, TIMEOUT, UNAVAILABLE, FAILED)

# What is worth saying about a change that did not fail.
CHANGED_ELSEWHERE = "changed-elsewhere"  # another program changed the profile after Gaming Home did, or in its stead
NOT_REMEMBERED = "not-remembered"        # the profile is set, but Omarchy could not be told to keep it for this source
NOTES = (CHANGED_ELSEWHERE, NOT_REMEMBERED)


@dataclass(frozen=True)
class Evidence:
    firmware: str = ""    # the platform profile the kernel was told ("performance", "balanced", "quiet"), or ""
    boost: str = ""       # "on" or "off": whether the CPU may boost, or "" when the machine does not say


@dataclass(frozen=True)
class Hold:
    profile: str          # the profile the program asked for
    name: str = ""        # the program's name, cleaned and short; empty when it gave none
    ours: bool = False    # Gaming Home's own hold (a later feature); a source that only reads never has one


@dataclass(frozen=True)
class Failure:
    code: str             # one of FAILURES
    profile: str = ""     # the profile that was asked for


@dataclass(frozen=True)
class Snapshot:
    state: str = UNKNOWN
    active: str = ""                       # one of PROFILES, or "" when unknown
    available: tuple[str, ...] = ()        # the profiles this machine can really apply, in PROFILES order
    degraded: tuple[str, ...] = ()         # why performance is held back (HEAT, LAP, LIMITED); empty is not a promise
    holds: tuple[Hold, ...] = ()
    pending: str = ""                      # the profile being asked for, or ""
    failure: Failure | None = None
    note: str = ""                         # one of NOTES, or ""
    evidence: Evidence = Evidence()

    @property
    def usable(self) -> bool:
        """There is a choice to make: the service answered and can really apply at least two profiles."""
        return self.state == READY and len(self.available) >= 2

    @property
    def visible(self) -> bool:
        """The header has a power profile button: a choice to make, or a service that could not be read."""
        return self.usable or self.state == ERROR


def is_real(cpu_driver: str, platform_driver: str) -> bool:
    """Whether a profile is backed by something that changes the hardware. PPD reports the drivers behind each
    profile; its placeholder driver, which stands in on a machine with nothing to switch, does nothing, and a
    profile with no driver at all is not applied either."""
    return any(isinstance(name, str) and name not in ("", "placeholder") for name in (cpu_driver, platform_driver))


def degraded_reasons(text: str) -> tuple[str, ...]:
    """Why performance is held back, from the service's ``PerformanceDegraded`` text, which can name several
    reasons separated by commas. Empty when it names none. Nothing on some machines ever sets it, so empty is not
    a promise that nothing is throttled."""
    if not isinstance(text, str):
        return ()
    names = {"high-operating-temperature": HEAT, "lap-detected": LAP}
    found: list[str] = []
    for part in text.split(","):
        part = part.strip()
        if part:
            reason = names.get(part, LIMITED)
            if reason not in found:
                found.append(reason)
    return tuple(found)


def next_profile(active: str, available: Iterable[str]) -> str:
    """The profile after ``active`` when cycling power-saver, balanced, performance and round again through the
    profiles the machine offers. "" when ``active`` is not one of them: cycling from an unknown place must not land
    on power-saver in the middle of a game. "" too when nothing is offered."""
    have = set(available)
    offered = [profile for profile in PROFILES if profile in have]
    if active not in offered:
        return ""
    return offered[(offered.index(active) + 1) % len(offered)]


_KERNEL_WORD = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")


def evidence_text(text: str) -> str:
    """A word the kernel reported, if it looks like one (lower case letters, digits, ``-`` and ``_``, at most 24
    characters), else "". Whatever a file contains, nothing else gets into ``Evidence``."""
    if not isinstance(text, str):
        return ""
    word = text.strip()
    return word if _KERNEL_WORD.fullmatch(word) else ""


def boost_text(text: str) -> str:
    """"on" or "off" for what the kernel's ``boost`` file says (``1`` or ``0``), else ""."""
    return {"1": "on", "0": "off"}.get(text.strip(), "") if isinstance(text, str) else ""


def hold_name(text: str) -> str:
    """What a program called itself, made safe to show and cut to ``HOLD_NAME_MAX`` characters."""
    return clean_text(text, HOLD_NAME_MAX)


OnChange = Callable[[], None]


class Power(Protocol):
    """What the window needs from a power profile source. The documentation of this module says how it behaves."""

    @property
    def snapshot(self) -> Snapshot: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def retry(self) -> None: ...

    def set_profile(self, profile: str) -> None: ...

    def dismiss(self) -> None: ...


# How the window gets its source: it hands over what to call when the snapshot changed.
PowerFactory = Callable[[OnChange], Power]
