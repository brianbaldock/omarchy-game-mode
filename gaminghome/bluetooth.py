"""What Gaming Home shows and asks of Bluetooth, apart from where the facts come from.

This module is the contract between the window (``ui.py``) and whatever supplies Bluetooth: the BlueZ client
(``bluez.py``) in the app and a scripted stand-in in the tests. It holds plain data, a few pure helpers and the
``Bluetooth`` protocol. It imports nothing from GTK, GLib or D-Bus.

Lifecycle
---------
``start()`` is called when Gaming Home is shown and ``stop()`` when it is hidden, closed or the app quits. Between
the two a source may watch BlueZ. Outside them it holds nothing at all: no timer, no connection to the bus, no
subscription, no discovery session, no pairing agent and no call in flight. Both are idempotent, ``start()``
never starts the Bluetooth service, and ``on_change`` is not called once ``stop()`` has returned. The window
gets its source from a ``BluetoothFactory``.

A source talks to BlueZ over a connection of its own, and ``stop()`` closes it. BlueZ drops a program's discovery
session and pairing agent when its connection goes, so whatever a late reply left behind goes with it.

Changes
-------
``on_change()`` is called on the main loop after ``snapshot`` has changed, and a burst of changes may be reported
once. Reading ``snapshot`` is cheap and never blocks. Every operation returns at once: progress shows as
``snapshot.activity``, the outcome as the devices changing or as ``snapshot.failure``. No method raises.

Operations
----------
Only one activity runs at a time. While ``snapshot.activity`` is set, ``set_power``, ``connect``, ``disconnect``,
``pair`` and ``forget`` are ignored (the window disables its buttons), whereas ``scan(False)``, ``cancel`` and
``answer`` always work. A new operation clears an earlier ``failure``; ``dismiss`` clears it by hand. A device is
named by its ``id``, and an id that no longer names a device ends the operation with ``GONE``.

* ``scan(True)`` starts a search for devices. It only ever starts because the user asked, and it ends by itself
  after ``SCAN_SECONDS``, on ``scan(False)``, when a pairing starts and on ``stop()``. ``snapshot.scanning`` is
  this source's own search: another program may keep the adapter searching after it ended.
* ``pair(device)`` pairs with a nearby device, trusts it and connects to it, in that order (the ``STAGE_*``
  values). The pairing agent is registered for that attempt only and answers only for that device. BlueZ makes the
  first agent registered the default one for everything, so a pairing that another device starts meanwhile is
  refused. What the remote device asks of the user arrives as ``snapshot.prompt``; the window answers it with
  ``answer`` or abandons the attempt with ``cancel``. A device that pairs and then does not connect stays paired
  and the failure says so (``Failure.paired``): it is among the ``remembered`` and ``connect`` tries again.
* Trust lets a device reconnect by itself. ``pair`` is the one operation that grants it; ``connect`` and
  ``disconnect`` act on a remembered (paired) device and never change its trust.
* ``connected`` is the state of the link, not a promise that every profile of the device works.
* ``forget`` removes a remembered device; BlueZ disconnects it, and refuses while the adapter is off
  (``NOT_READY``).
* ``set_power`` switches Bluetooth on or off for the whole machine: Omarchy blocks every radio and keeps the choice
  across reboots through its own command, which a source uses when it is there. The source looks at the adapter
  afterwards and says ``BLOCKED`` only when it knows that a block is the reason.
* ``retry`` reads BlueZ again after ``ERROR``.

Devices and names
-----------------
BlueZ's object paths hold the device address, and nothing of the kind is part of this model. A device has an
opaque ``id`` that no other device ever gets from the same source, so that a button pressed after the list changed
cannot reach a different device. ``Device.name`` comes from ``display_name``: safe to show as it is, never an
address, and empty for a device that announces no name. A snapshot lists at most ``MAX_DEVICES`` devices,
remembered ones first, and says so in ``Snapshot.truncated``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

SCAN_SECONDS = 45     # a search for new devices ends by itself after this long
NAME_MAX = 48
MAX_DEVICES = 64      # the most devices a snapshot lists
PIN_MAX = 16          # a legacy PIN has 1 to 16 characters; Gaming Home enters digits only
PASSKEY_MAX = 6       # a passkey is a number from 0 to 999999

# What the adapter is doing.
UNKNOWN = "unknown"          # nothing has been read yet: the window is not shown, or BlueZ has not answered
ABSENT = "absent"            # nothing owns org.bluez: the Bluetooth service is not installed or not running
NO_ADAPTER = "no-adapter"    # the service runs and there is no adapter (none present, or it was removed)
OFF = "off"                  # an adapter exists and is not powered
ON = "on"                    # an adapter exists and is powered
ERROR = "error"              # the service is there and could not be read; ``retry`` asks again

# What a device is, for choosing its icon.
KINDS = ("gamepad", "headphones", "headset", "speaker", "keyboard", "mouse", "phone", "computer", "other")

# What the source is busy with.
CONNECTING, DISCONNECTING, PAIRING, FORGETTING, POWERING = (
    "connecting", "disconnecting", "pairing", "forgetting", "powering")

# The steps of a pairing, in order: ``Activity.stage`` is the one under way, ``Failure.stage`` the one that failed.
STAGE_PAIR, STAGE_TRUST, STAGE_CONNECT = "pair", "trust", "connect"
STAGES = (STAGE_PAIR, STAGE_TRUST, STAGE_CONNECT)

# ``Failure.action`` of a search that could not start (a search is not an activity: it blocks nothing).
SEARCHING = "searching"

# What a pairing asks of the user.
CONFIRM = "confirm"      # both sides show a number: does it match? answer(id, True) or answer(id, False)
CODE = "code"            # type this code on the device, then press Enter there; nothing to answer
PIN = "pin"              # enter the device's PIN, digits only (up to ``max_length``): answer(id, "0000")
PASSKEY = "passkey"      # enter the passkey the device shows (up to ``max_length`` digits): answer(id, "123456")

# Why something failed. A source maps whatever BlueZ says onto exactly one of these.
BLOCKED = "blocked"              # Bluetooth is switched off by a block (airplane mode, rfkill) and could not be unblocked
NOT_READY = "not-ready"          # the adapter is off or still starting
TIMEOUT = "timeout"              # nothing answered in time
UNREACHABLE = "unreachable"      # the device did not answer: out of range, switched off or asleep
AUTH_FAILED = "auth-failed"      # the device or the user rejected the pairing, or the code was wrong
CANCELED = "canceled"            # the user gave up
IN_PROGRESS = "in-progress"      # BlueZ is already doing something with that device
UNSUPPORTED = "unsupported"      # nothing this machine can use is on that device
GONE = "gone"                    # the device or the adapter went away meanwhile
UNAVAILABLE = "unavailable"      # the Bluetooth service cannot be reached
FAILED = "failed"                # anything else
FAILURES = (BLOCKED, NOT_READY, TIMEOUT, UNREACHABLE, AUTH_FAILED, CANCELED, IN_PROGRESS, UNSUPPORTED, GONE,
            UNAVAILABLE, FAILED)


@dataclass(frozen=True)
class Device:
    id: int                      # opaque handle that every operation takes; never given to another device
    name: str = ""
    kind: str = "other"
    paired: bool = False         # BlueZ remembers it
    trusted: bool = False        # it may reconnect by itself
    connected: bool = False      # the link is up
    battery: int | None = None   # percent, 0 to 100, when the device reports one
    order: int = 0               # when this source first saw the device: a stable position while a search runs


@dataclass(frozen=True)
class Activity:
    kind: str                    # one of CONNECTING ... POWERING
    device: int = 0              # the device it concerns, 0 for the adapter
    name: str = ""               # that device's name, for the message
    stage: str = ""              # PAIRING: the STAGE_* under way


@dataclass(frozen=True)
class Prompt:
    id: int                      # pass it back to ``answer``; a new prompt has a new id
    kind: str                    # CONFIRM, CODE, PIN or PASSKEY
    device: int
    name: str                    # the device's name, for the question
    code: str = ""               # CONFIRM and CODE: the six digits to compare or to type, leading zeros kept
    typed: int = 0               # CODE: how many digits the device has received so far
    max_length: int = 0          # PIN and PASSKEY: how many digits may be entered


@dataclass(frozen=True)
class Failure:
    code: str                    # one of FAILURES
    action: str = ""             # the activity kind that failed, or SEARCHING
    stage: str = ""              # PAIRING: the STAGE_* that failed
    device: int = 0
    name: str = ""               # the device's name when it failed, for the message
    paired: bool = False         # the device is paired although the action failed: it is among the remembered


@dataclass(frozen=True)
class Snapshot:
    state: str = UNKNOWN
    scanning: bool = False
    devices: tuple[Device, ...] = ()
    activity: Activity | None = None
    prompt: Prompt | None = None
    failure: Failure | None = None
    truncated: bool = False      # more devices exist than ``devices`` lists

    @property
    def usable(self) -> bool:
        """There is an adapter, so there is something to show and to switch."""
        return self.state in (OFF, ON)

    @property
    def visible(self) -> bool:
        """The header has a Bluetooth button: an adapter to use, or a service that could not be read."""
        return self.state in (OFF, ON, ERROR)


def remembered(devices: Iterable[Device]) -> list[Device]:
    """The adapter's paired devices by name. A device keeps its place when it connects or disconnects, so the
    focus stays where the user left it."""
    return sorted((d for d in devices if d.paired), key=lambda d: (d.name.casefold(), d.id))


def nearby(devices: Iterable[Device]) -> list[Device]:
    """Unpaired devices that announce a name, in the order they were first seen, so the list holds still while the
    user moves through it. Anonymous ones (beacons, trackers) are left out."""
    return sorted((d for d in devices if not d.paired and d.name), key=lambda d: (d.order, d.id))


def connected(devices: Iterable[Device]) -> list[Device]:
    return [d for d in remembered(devices) if d.connected]


def labels(devices: Iterable[Device], unnamed: str) -> dict[int, str]:
    """What to call each of these devices: its name, ``unnamed`` when it has none, and ``(2)``, ``(3)`` after the
    first of several devices with the same label, so that no two differ only by an address nobody sees."""
    used: set[str] = set()
    result: dict[int, str] = {}
    for device in devices:
        base = device.name or unnamed
        label, count = base, 1
        while label.casefold() in used:
            count += 1
            label = f"{base} ({count})"
        used.add(label.casefold())
        result[device.id] = label
    return result


# Six pairs of hex digits joined by one of the separators BlueZ and its tools use for an address.
_ADDRESS = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:_-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
# Invisible or direction-changing characters (format characters that are not part of writing a language).
_HIDDEN = frozenset("\u061c\u200b\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff"
                    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cs", "Co", "Cn"})
_EMPTY_BRACKETS = re.compile(r"[(\[{<]\s*[)\]}>]")


def _clean(text: str) -> str:
    kept = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif ch not in _HIDDEN and unicodedata.category(ch) not in _UNSAFE_CATEGORIES:
            kept.append(ch)
    text = _EMPTY_BRACKETS.sub(" ", _ADDRESS.sub(" ", "".join(kept)))
    return " ".join(text.split())[:NAME_MAX].rstrip()


def display_name(*candidates: str) -> str:
    """The first of these names that is left with something to show, made safe to show.

    Pass BlueZ's ``Alias`` first and its ``Name`` second. BlueZ makes the alias of a device without a name out of
    its address, and a device may announce an address as its name, so anything that looks like an address is cut
    out. Control characters and the ones that reverse or hide text are removed, every kind of white space and
    line break becomes one space, and the result has at most ``NAME_MAX`` characters. Zero-width joiners stay,
    because some scripts need them. Empty when no candidate has a letter, a digit or a symbol left.
    """
    for text in candidates:
        if isinstance(text, str):
            text = _clean(text)
            if any(unicodedata.category(ch)[0] in "LNS" for ch in text):
                return text
    return ""


OnChange = Callable[[], None]


class Bluetooth(Protocol):
    """What the window needs from a Bluetooth source. The documentation of this module says how it behaves."""

    @property
    def snapshot(self) -> Snapshot: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def retry(self) -> None: ...

    def set_power(self, on: bool) -> None: ...

    def scan(self, on: bool) -> None: ...

    def connect(self, device: int) -> None: ...

    def disconnect(self, device: int) -> None: ...

    def pair(self, device: int) -> None: ...

    def forget(self, device: int) -> None: ...

    def cancel(self) -> None: ...

    def answer(self, prompt: int, reply: bool | str) -> None: ...

    def dismiss(self) -> None: ...


# How the window gets its source: it hands over what to call when the snapshot changed.
BluetoothFactory = Callable[[OnChange], Bluetooth]
