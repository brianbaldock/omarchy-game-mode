"""The machine's power profile from power-profiles-daemon, for Gaming Home and its ``power`` command.

power-profiles-daemon (PPD) owns the profile: it offers power-saver, balanced and performance, applies one through
the drivers that back it and announces every change, whoever made it. ``power.py`` says what Gaming Home shows and
asks; this module says how that reaches the machine.
"""

from __future__ import annotations

import os

from gi.repository import Gio, GLib

from . import power
from .power import DENIED, FAILED, PROFILES, TIMEOUT, UNAVAILABLE, Evidence, Hold

BUS_NAME = "org.freedesktop.UPower.PowerProfiles"
OBJECT_PATH = "/org/freedesktop/UPower/PowerProfiles"
INTERFACE = "org.freedesktop.UPower.PowerProfiles"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
ACTIVE_PROFILE = "ActiveProfile"

MAX_REPLY_BYTES = 64 * 1024     # PPD answers GetAll with about a kilobyte
MAX_PROFILES = 8                # PPD knows three
MAX_HOLDS = 16
MAX_FILE_BYTES = 256            # a word of the kernel's and its newline

SYSFS_ROOT = "/sys"
PLATFORM_PROFILE = "firmware/acpi/platform_profile"
CPU_BOOST = "devices/system/cpu/cpufreq/policy0/boost"

HELPER = "omarchy-powerprofiles-set"
HELPER_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
HELPER_ENVIRONMENT = ("HOME", "LANG", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "DBUS_SYSTEM_BUS_ADDRESS")

# What the error of a Set means. Anything else, InvalidArgs among them, is FAILED.
DENIED_ERRORS = frozenset({"org.freedesktop.DBus.Error.AccessDenied"})
UNAVAILABLE_ERRORS = frozenset({"org.freedesktop.DBus.Error.ServiceUnknown",
                                "org.freedesktop.DBus.Error.NameHasNoOwner",
                                "org.freedesktop.DBus.Error.Disconnected",
                                "org.freedesktop.DBus.Error.NoReply"})   # the owner left without answering


def allowed(path, interface, member, parameters) -> bool:
    """Whether this module may send a call: ``GetAll`` of PPD's interface, or ``Set`` of exactly its
    ``ActiveProfile`` to a string that is one of ``power.PROFILES``, on PPD's object. Nothing else."""
    if path != OBJECT_PATH or interface != PROPERTIES_IFACE or not isinstance(parameters, GLib.Variant):
        return False
    kind = parameters.get_type_string()
    if member == "GetAll":
        return kind == "(s)" and parameters.get_child_value(0).get_string() == INTERFACE
    if member == "Set" and kind == "(ssv)":
        value = parameters.get_child_value(2).get_variant()
        return (parameters.get_child_value(0).get_string() == INTERFACE
                and parameters.get_child_value(1).get_string() == ACTIVE_PROFILE
                and value.get_type_string() == "s" and value.get_string() in PROFILES)
    return False


def failure_code(error) -> str:
    """What the error of a ``Set`` means: one of ``power.FAILURES``. The error's text is never kept."""
    if not isinstance(error, GLib.Error):
        return FAILED
    name = Gio.DBusError.get_remote_error(error)
    if name is not None:
        return DENIED if name in DENIED_ERRORS else UNAVAILABLE if name in UNAVAILABLE_ERRORS else FAILED
    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
        return TIMEOUT
    if error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CLOSED):
        return UNAVAILABLE                          # the connection to the bus is gone
    return FAILED


class Unusable(ValueError):
    """What the service said cannot be used: more of it than PPD ever sends, or not in PPD's shape."""


def _typed(properties: dict, name: str, kind, fallback):
    """The one accessor for what the service says: the value when it has the expected type, else ``fallback``."""
    value = properties.get(name, fallback)
    return value if isinstance(value, kind) else fallback


def _available(entries: list) -> tuple[str, ...]:
    if len(entries) > MAX_PROFILES:
        raise Unusable("more profiles than the service lists")
    real = set()
    for entry in entries:
        if isinstance(entry, dict) and power.is_real(_typed(entry, "CpuDriver", str, ""),
                                                     _typed(entry, "PlatformDriver", str, "")):
            real.add(_typed(entry, "Profile", str, ""))
    return tuple(profile for profile in PROFILES if profile in real)


def _holds(entries: list) -> tuple[Hold, ...]:
    holds = []
    for entry in entries:
        if len(holds) == MAX_HOLDS:
            break
        if isinstance(entry, dict) and _typed(entry, "Profile", str, "") in PROFILES:
            holds.append(Hold(entry["Profile"], power.hold_name(_typed(entry, "ApplicationId", str, ""))))
    return tuple(holds)


def read_properties(properties, complete: bool = True) -> dict:
    """The fields of a ``power.Snapshot`` that PPD's properties give: ``active``, ``available``, ``degraded`` and
    ``holds``.

    ``complete`` is for ``GetAll``: a property that is missing or of the wrong type gives the empty value. Without it
    (a ``PropertiesChanged``) only the properties present count. A hold's reason is never kept. Raises ``Unusable``
    for more than ``MAX_PROFILES`` profiles, or when ``properties`` is not a dictionary."""
    if not isinstance(properties, dict):
        raise Unusable("not a dictionary of properties")
    fields = {}
    if complete or ACTIVE_PROFILE in properties:
        active = _typed(properties, ACTIVE_PROFILE, str, "")
        fields["active"] = active if active in PROFILES else ""
    if complete or "Profiles" in properties:
        fields["available"] = _available(_typed(properties, "Profiles", list, []))
    if complete or "PerformanceDegraded" in properties:
        fields["degraded"] = power.degraded_reasons(_typed(properties, "PerformanceDegraded", str, ""))
    if complete or "ActiveProfileHolds" in properties:
        fields["holds"] = _holds(_typed(properties, "ActiveProfileHolds", list, []))
    return fields


def _read_word(path: str) -> str:
    """A kernel file as text; "" when it is missing, cannot be read or holds more than ``MAX_FILE_BYTES``."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError:
        return ""
    try:
        data = os.read(descriptor, MAX_FILE_BYTES + 1)
    except OSError:
        return ""
    finally:
        os.close(descriptor)
    return "" if len(data) > MAX_FILE_BYTES else data.decode("utf-8", "replace")


def read_evidence(root: str = SYSFS_ROOT) -> Evidence:
    """What the kernel was told: the firmware's platform profile and whether the CPU may boost."""
    return Evidence(power.evidence_text(_read_word(os.path.join(root, PLATFORM_PROFILE))),
                    power.boost_text(_read_word(os.path.join(root, CPU_BOOST))))


def helper_environment(script: str, environ) -> dict[str, str]:
    """The environment Omarchy's command gets: a ``PATH`` of its own directory and the system's, never the inherited
    one (a version manager's shims there would hand ``powerprofilesctl`` another Python), and the few variables it
    needs when they are set."""
    directory = os.path.dirname(script)
    entries = [directory] if directory and ":" not in directory else []
    environment = {"PATH": ":".join(dict.fromkeys([*entries, *HELPER_PATH]))}
    environment.update({name: environ[name] for name in HELPER_ENVIRONMENT if name in environ})
    return environment
