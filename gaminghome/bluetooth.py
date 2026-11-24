"""What Gaming Home shows and asks of Bluetooth, apart from where the facts come from.

This module is the contract between the window (``ui.py``) and whatever supplies Bluetooth: the BlueZ client
(``bluez.py``) in the app and a scripted stand-in in the tests. It holds plain data, a few pure helpers and the
``Bluetooth`` protocol. It imports nothing from GTK, GLib or D-Bus.

Lifecycle
---------
``start()`` is called when Gaming Home is shown and ``stop()`` when it is hidden, closed or the app quits. Between
the two a source may watch BlueZ. Outside them it holds nothing at all: no timer, no bus watch or subscription,
no discovery session, no registered pairing agent and no call in flight. Both are idempotent, ``start()`` never
starts the Bluetooth service, and ``on_change`` is not called once ``stop()`` has returned.

Changes
-------
``on_change()`` is called on the main loop after ``snapshot`` has changed, and a burst of changes may be reported
once. Reading ``snapshot`` is cheap and never blocks. Every operation returns at once: progress shows as
``snapshot.activity``, the outcome as the devices changing or as ``snapshot.failure``. No method raises.

Operations
----------
Only one activity runs at a time. While ``snapshot.activity`` is set, ``set_power``, ``connect``, ``disconnect``,
``pair`` and ``forget`` are ignored (the window disables its buttons), whereas ``scan(False)``, ``cancel`` and
``answer`` always work. A new operation clears an earlier ``failure``; ``dismiss`` clears it by hand.

* ``scan(True)`` starts discovery. It only ever starts because the user asked, and it ends by itself after
  ``SCAN_SECONDS``, on ``scan(False)``, when a pairing starts and on ``stop()``.
* ``pair(path)`` pairs with a nearby device, trusts it and connects to it. A pairing agent exists only for that
  attempt and only answers for that device. What the remote device asks of the user arrives as
  ``snapshot.prompt``; the window answers it with ``answer`` or abandons the attempt with ``cancel``.
* ``connect`` and ``disconnect`` act on a remembered (paired) device; ``forget`` removes it, disconnecting first.
* ``set_power`` switches the adapter. A machine with Omarchy keeps that choice across reboots through its own
  command, so a source should use it when it is there.

Names
-----
``Device.name`` is safe to show as it is: control and format characters are gone, whitespace is collapsed and it
is at most ``NAME_MAX`` characters. It is empty for a device that announces no name. Addresses are never part of
this model and are never shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

SCAN_SECONDS = 45     # a search for new devices ends by itself after this long
NAME_MAX = 48

# What the adapter is doing.
UNKNOWN = "unknown"          # nothing has been read yet: the window is not shown, or BlueZ has not answered
ABSENT = "absent"            # nothing owns org.bluez: the Bluetooth service is not installed or not running
NO_ADAPTER = "no-adapter"    # the service runs and there is no adapter (none present, or it was removed)
OFF = "off"                  # an adapter exists and is not powered
ON = "on"                    # an adapter exists and is powered

# What a device is, for choosing its icon.
KINDS = ("gamepad", "headphones", "headset", "speaker", "keyboard", "mouse", "phone", "computer", "other")

# What the source is busy with.
CONNECTING, DISCONNECTING, PAIRING, FORGETTING, POWERING = (
    "connecting", "disconnecting", "pairing", "forgetting", "powering")

# What a pairing asks of the user.
CONFIRM = "confirm"      # both sides show a code: does it match? answer(id, True) or answer(id, False)
CODE = "code"            # type this code on the device, then press Enter there; nothing to answer
PIN = "pin"              # enter the device's PIN (up to ``max_length`` digits): answer(id, "0000")
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
    path: str                    # BlueZ's object path; the handle every operation takes
    name: str = ""
    kind: str = "other"
    paired: bool = False         # remembered by the adapter
    trusted: bool = False
    connected: bool = False
    battery: int | None = None   # percent, when the device reports one
    order: int = 0               # when this source first saw the device: a stable position while a search runs


@dataclass(frozen=True)
class Activity:
    kind: str                    # one of CONNECTING ... POWERING
    path: str = ""               # the device it concerns, empty for the adapter


@dataclass(frozen=True)
class Prompt:
    id: int                      # pass it back to ``answer``; a new prompt has a new id
    kind: str                    # CONFIRM, CODE, PIN or PASSKEY
    path: str
    name: str                    # the device's name, for the question
    code: str = ""               # CONFIRM and CODE: the digits to compare or to type
    typed: int = 0               # CODE: how many digits the device has received so far
    max_length: int = 0          # PIN and PASSKEY: how many digits may be entered


@dataclass(frozen=True)
class Failure:
    code: str                    # one of FAILURES
    action: str = ""             # the activity kind that failed, or "scanning"
    path: str = ""
    name: str = ""               # the device's name when it failed, for the message


@dataclass(frozen=True)
class Snapshot:
    state: str = UNKNOWN
    scanning: bool = False
    devices: tuple[Device, ...] = ()
    activity: Activity | None = None
    prompt: Prompt | None = None
    failure: Failure | None = None

    @property
    def usable(self) -> bool:
        """There is an adapter, so there is something to show and to switch."""
        return self.state in (OFF, ON)


def remembered(devices: Iterable[Device]) -> list[Device]:
    """The adapter's paired devices in listing order: connected ones first, then by name."""
    return sorted((d for d in devices if d.paired), key=lambda d: (not d.connected, d.name.casefold(), d.path))


def nearby(devices: Iterable[Device]) -> list[Device]:
    """Unpaired devices that announce a name, in the order they were first seen, so the list holds still while the
    user moves through it. Anonymous ones (beacons, trackers) are left out."""
    return sorted((d for d in devices if not d.paired and d.name), key=lambda d: (d.order, d.path))


def connected(devices: Iterable[Device]) -> list[Device]:
    return [d for d in remembered(devices) if d.connected]


OnChange = Callable[[], None]


class Bluetooth(Protocol):
    """What the window needs from a Bluetooth source. The documentation of this module says how it behaves."""

    @property
    def snapshot(self) -> Snapshot: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def set_power(self, on: bool) -> None: ...

    def scan(self, on: bool) -> None: ...

    def connect(self, path: str) -> None: ...

    def disconnect(self, path: str) -> None: ...

    def pair(self, path: str) -> None: ...

    def forget(self, path: str) -> None: ...

    def cancel(self) -> None: ...

    def answer(self, prompt_id: int, reply: bool | str) -> None: ...

    def dismiss(self) -> None: ...
