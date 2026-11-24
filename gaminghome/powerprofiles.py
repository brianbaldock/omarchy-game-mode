"""The machine's power profile from power-profiles-daemon, for Gaming Home and its ``power`` command.

power-profiles-daemon (PPD) owns the profile: it offers power-saver, balanced and performance, applies one through
the drivers that back it and announces every change, whoever made it. ``power.py`` says what Gaming Home shows and
asks; this module is how that reaches the machine. ``PowerProfiles`` is the window's source, and ``main`` the
``power`` command, which starts a source of its own and needs no window.

Every call it may make, and when
--------------------------------
* ``start()`` watches the name ``org.freedesktop.UPower.PowerProfiles`` on the system bus with ``Gio.bus_watch_name``
  and no flags: watching never starts PPD, which would stop the power managers it conflicts with. ``stop()`` gives
  the watch back. PPD's legacy name is never used.
* When an owner appears while the source is started: one ``org.freedesktop.DBus.Properties.GetAll`` of the
  ``org.freedesktop.UPower.PowerProfiles`` interface of ``/org/freedesktop/UPower/PowerProfiles``, and a subscription
  to that owner's ``PropertiesChanged`` of the same object and interface, which says every change from then on.
  ``retry()`` sends the same ``GetAll`` again, and only after a reading failed.
* ``set_profile()`` sends one ``org.freedesktop.DBus.Properties.Set`` of ``ActiveProfile`` to one of the three
  profiles: only when the service was read in this visit, offers that profile and is not in it already, and no
  other request is on its way.

Nothing else: no ``HoldProfile``, ``ReleaseProfile`` or ``SetActionEnabled``, and no other property. One method,
``_call``, sends every call. It refuses anything but those two, addresses the unique name of the current owner (never
the well-known name, which a new owner could take meanwhile), never lets the bus start a service (``NO_AUTO_START``)
and gives each call Gio's own time limit (``CALL_TIMEOUT_MS``, ``SET_TIMEOUT_MS``). Replies and signals of an owner
that left, or for a stopped source, are dropped. Nothing the system says is logged, an error's text is never kept and
neither is the reason another program gave for its hold.

Files and the one process
-------------------------
* Two kernel files are read, never written, a few hundred bytes at most: ``firmware/acpi/platform_profile`` and
  ``devices/system/cpu/cpufreq/policy0/boost`` under ``/sys``. They are read at ``start()``, with each reading of
  the service, each ``PropertiesChanged`` and each reply to a ``Set``, and they never decide whether a change worked.
* Omarchy's ``omarchy-powerprofiles-set autodetect PROFILE``, found on ``PATH``, runs after a change succeeded or
  when the active profile is chosen again, so that Omarchy keeps the choice for the current power source (it
  switches to what it keeps at every plug and unplug). No shell, stdin closed, output dropped, its own directory and
  ``/usr/local/bin:/usr/bin:/bin`` as ``PATH`` (never the inherited one: a version manager's shims there would give
  ``powerprofilesctl`` another Python), only ``HOME``, ``LANG``, ``XDG_RUNTIME_DIR``, ``XDG_STATE_HOME`` and
  ``DBUS_SYSTEM_BUS_ADDRESS`` besides, and a process group of its own that is killed as a whole when
  ``HELPER_LIMIT_MS`` runs out. Its exit status is a note at most. It is left to finish after ``stop()`` and then
  reports nothing; a newer choice, or another program changing the profile, ends it first, because it would set the
  old profile again and Gaming Home never does that.

Nothing is polled. The only timer is that command's limit. A started source that is left alone holds the name watch
and the subscription and nothing else (``idle``). A hold is never taken here: that belongs to the resident core.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import os
import shutil
import signal
import subprocess
import sys

from gi.repository import Gio, GLib

from . import power
from .power import (ABSENT, CHANGED_ELSEWHERE, DENIED, ERROR, FAILED, NOT_REMEMBERED, PROFILES, READY, TIMEOUT,
                    UNAVAILABLE, UNKNOWN, UNSUPPORTED, Evidence, Failure, Hold, Snapshot)

BUS_NAME = "org.freedesktop.UPower.PowerProfiles"
OBJECT_PATH = "/org/freedesktop/UPower/PowerProfiles"
INTERFACE = "org.freedesktop.UPower.PowerProfiles"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
ACTIVE_PROFILE = "ActiveProfile"

CALL_TIMEOUT_MS = 5_000         # a reading of the service
SET_TIMEOUT_MS = 5_000          # a change: PPD answers in milliseconds
HELPER_LIMIT_MS = 10_000        # Omarchy's command takes well under a second

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


def _which_helper() -> str | None:
    return shutil.which(HELPER)


def spawn_helper(argv: list[str], environment: dict[str, str], on_exit):
    """Start ``argv`` with ``environment`` and no shell, in a process group of its own, with stdin closed and its
    output dropped. ``on_exit(code)`` is called on the main loop once it has ended and been reaped. Returns a function
    that kills the whole group, and does nothing once the process was reaped (its number may be another's by then)."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=environment, cwd="/", close_fds=True,
                               process_group=0)

    def exited(_pid, status):
        try:
            code = os.waitstatus_to_exitcode(status)
        except ValueError:
            code = -1
        process.returncode = code                     # GLib reaped it: Popen must not wait for it again
        on_exit(code)

    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, exited)

    def kill():
        if process.returncode is None:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
    return kill


class _Run:
    """One run of Omarchy's command."""

    def __init__(self, profile: str):
        self.profile = profile
        self.kill = lambda: None    # ends its whole process group
        self.limit = None           # the token of its time limit while that is pending
        self.timed_out = False


def _timeout_add(milliseconds, callback):
    def fire():
        callback()
        return GLib.SOURCE_REMOVE
    return GLib.timeout_add(milliseconds, fire)


def _guarded(method):
    """For what the main loop or the window calls: an exception never escapes, and never wedges the source."""
    @functools.wraps(method)
    def wrapper(self, *args):
        try:
            return method(self, *args)
        except Exception as error:  # noqa: BLE001
            self._went_wrong(error)
            return None
    return wrapper


def _service(state: str) -> dict:
    """The fields of a snapshot that come from the service, as they are before it says anything."""
    return {"state": state, "active": "", "available": (), "degraded": (), "holds": ()}


class PowerProfiles:
    """The power profile source of ``power.py``, backed by power-profiles-daemon on the system bus.

    ``on_change`` is called on the main loop after ``snapshot`` changed. The keyword arguments replace the system in
    tests: ``connection`` the system bus, ``schedule(milliseconds, function)`` and ``cancel(token)`` the timers,
    ``find_helper()`` and ``run_helper(argv, environment, on_exit)`` how Omarchy's command is found and run, and
    ``sysfs_root`` where the kernel's files are.
    """

    def __init__(self, on_change, *, connection: Gio.DBusConnection | None = None, schedule=_timeout_add,
                 cancel=GLib.source_remove, find_helper=None, run_helper=None, sysfs_root: str = SYSFS_ROOT):
        self._on_change = on_change
        self._connection = connection
        self._schedule, self._cancel = schedule, cancel
        self._find_helper, self._run_helper = find_helper, run_helper
        self._root = sysfs_root
        self._snapshot = self._announced = Snapshot()
        self.handler_error: str | None = None    # the kind of exception ``on_change`` last raised
        self.internal_error: str | None = None   # the kind of exception this source last caught in itself
        self._started = False
        self._visit = 0          # one more at each start() and stop(): name watch callbacks of another are dropped
        self._epoch = 0          # one more per owner and per visit: replies and signals of another are dropped
        self._fresh = False      # nothing has been heard of the name in this visit yet
        self._watch = None
        self._bus: Gio.DBusConnection | None = None
        self._owner: str | None = None
        self._subscription: int | None = None
        self._cancellable: Gio.Cancellable | None = None
        self._reading = False
        self._live = False       # the service was read in this visit, from the current owner
        self._armed = False      # a request of this visit succeeded: another program's change is worth a note
        self._helper: _Run | None = None    # the run of Omarchy's command whose outcome is still worth a note
        self._runs: set[_Run] = set()       # every run of it that has not been reaped yet

    # ---- what the window reads ---------------------------------------------
    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def owner(self) -> str | None:
        """The unique bus name every call goes to, or None."""
        return self._owner

    @property
    def idle(self) -> bool:
        """No call in flight, no timer and no process: the source only waits for the service to say something."""
        return not self._reading and not self._snapshot.pending and not self._runs

    @property
    def helping(self) -> bool:
        """Omarchy's command is still running: the one thing that may outlive ``stop()``, bounded by its limit."""
        return bool(self._runs)

    # ---- lifecycle ---------------------------------------------------------
    @_guarded
    def start(self):
        if self._started:
            return
        self._started, self._fresh, self._live = True, True, False
        self._visit += 1
        self._epoch += 1
        self._update(evidence=read_evidence(self._root))
        self._watch = self._watch_name(self._visit)
        self._announce()

    @_guarded
    def stop(self):
        """Idempotent. What was last read stays; a request on its way is forgotten, and nothing reports any more."""
        watch, self._watch = self._watch, None
        self._started = False
        self._visit += 1
        self._forget_owner()
        self._armed = False
        self._helper = None                           # it runs on to its end or its limit, and reports nothing
        self._update(pending="", failure=None, note="")
        if watch is not None:
            self._unwatch_name(watch)

    def _watch_name(self, visit):
        flags = Gio.BusNameWatcherFlags.NONE          # watch only: never start the service
        appeared = functools.partial(self._appeared, visit)
        vanished = functools.partial(self._vanished, visit)
        if self._connection is not None:
            return Gio.bus_watch_name_on_connection(self._connection, BUS_NAME, flags, appeared, vanished)
        return Gio.bus_watch_name(Gio.BusType.SYSTEM, BUS_NAME, flags, appeared, vanished)

    @staticmethod
    def _unwatch_name(watch):
        Gio.bus_unwatch_name(watch)

    def _forget_owner(self):
        """Drop the current owner: what is in flight to it is cancelled, and its replies and signals no longer count."""
        self._epoch += 1
        if self._cancellable is not None:
            self._cancellable.cancel()
        if self._bus is not None and self._subscription is not None:
            self._bus.signal_unsubscribe(self._subscription)
        self._bus = self._owner = self._subscription = self._cancellable = None
        self._reading = False

    # ---- the owner of the name ---------------------------------------------
    @_guarded
    def _appeared(self, visit, connection, _name, owner):
        if visit != self._visit or owner == self._owner:
            return
        changes = self._owner_gone() if self._owner is not None else {}
        if not self._fresh:
            changes.update(_service(UNKNOWN))     # a new owner is read from scratch
        self._fresh = False
        self._bus, self._owner = connection, owner
        self._cancellable = Gio.Cancellable()
        self._subscription = connection.signal_subscribe(
            owner, PROPERTIES_IFACE, "PropertiesChanged", OBJECT_PATH, INTERFACE, Gio.DBusSignalFlags.NONE,
            self._on_properties_changed, self._epoch)
        self._update(**changes)
        self._read()
        self._announce()

    @_guarded
    def _vanished(self, visit, _connection, _name):
        if visit != self._visit:
            return
        changes = self._owner_gone()
        changes.update(_service(ABSENT))
        self._fresh = False
        self._update(**changes)
        self._announce()

    def _owner_gone(self) -> dict:
        """The owner left or was replaced: a request on its way to it ends as UNAVAILABLE."""
        pending = self._snapshot.pending
        self._forget_owner()
        self._live = self._armed = False
        changes = {"pending": "", "note": ""}
        if pending:
            changes["failure"] = Failure(UNAVAILABLE, pending)
        return changes

    # ---- reading -----------------------------------------------------------
    def _read(self):
        self._reading = True
        if not self._call(OBJECT_PATH, PROPERTIES_IFACE, "GetAll", GLib.Variant("(s)", (INTERFACE,)), "(a{sv})",
                          CALL_TIMEOUT_MS, self._on_read):
            self._reading = False
            self._update(**_service(ERROR))

    def _call(self, path, interface, member, parameters, reply_type, timeout, callback, *data) -> bool:
        """Send one call to the current owner: the one place that sends. It refuses anything not ``allowed``."""
        if not allowed(path, interface, member, parameters):
            raise ValueError(f"{interface}.{member} is not a call this module sends")
        if self._bus is None or not Gio.dbus_is_unique_name(self._owner or ""):
            return False                              # Gio drops a call to a malformed name and never answers it
        try:
            self._bus.call(self._owner, path, interface, member, parameters, GLib.VariantType(reply_type),
                           Gio.DBusCallFlags.NO_AUTO_START, timeout, self._cancellable, callback,
                           (self._epoch, *data))
        except (TypeError, ValueError, GLib.Error):
            return False
        return True

    @staticmethod
    def _finish(bus, result):
        """The reply of a call, or the error it ended with."""
        try:
            return bus.call_finish(result)
        except Exception as error:  # noqa: BLE001 - a cancelled call, a time-out, an error of the service
            return error

    @_guarded
    def _on_read(self, bus, result, data):
        reply = self._finish(bus, result)
        if data[0] != self._epoch:
            return                                    # an answer for a previous owner, or for a stopped source
        self._reading = False
        fields = None
        if isinstance(reply, GLib.Variant) and reply.get_size() <= MAX_REPLY_BYTES:
            try:
                fields = read_properties(reply.unpack()[0])
            except (Unusable, IndexError):
                fields = None
        self._live = fields is not None
        changes = _service(ERROR) if fields is None else {**_service(READY), **fields}
        failure = self._snapshot.failure
        if fields and failure is not None and failure.code == TIMEOUT and fields["active"] == failure.profile:
            changes["failure"] = None
        self._update(evidence=read_evidence(self._root), **changes)
        self._announce()

    @_guarded
    def _on_properties_changed(self, _bus, sender, path, _interface, _member, parameters, epoch):
        if epoch != self._epoch or sender != self._owner or path != OBJECT_PATH:
            return
        if self._reading or self._snapshot.state != READY:
            return                                    # the reading on its way is newer, or there is nothing to update
        changes = {"evidence": read_evidence(self._root)}
        try:
            if parameters.get_type_string() != "(sa{sv}as)" or parameters.get_size() > MAX_REPLY_BYTES:
                raise Unusable("not a change of properties")
            interface, changed, _invalidated = parameters.unpack()
            if interface != INTERFACE:
                return
            fields = read_properties(changed, complete=False)
        except Unusable:
            fields, changes = {}, {**changes, **_service(ERROR)}
        changes.update(fields)
        if fields.get("active", self._snapshot.active) != self._snapshot.active:
            self._profile_changed(fields["active"], changes)
        self._update(**changes)
        self._announce()

    def _profile_changed(self, active: str, changes: dict):
        """What a change of the active profile announced by the service means for the request of this visit."""
        failure = self._snapshot.failure
        if failure is not None and failure.code == TIMEOUT and active == failure.profile:
            changes["failure"] = None
        if not self._snapshot.pending:
            self._end_helper()                        # a run still going would set the old one again
            if self._armed:
                changes["note"] = CHANGED_ELSEWHERE

    # ---- operations --------------------------------------------------------
    @_guarded
    def set_profile(self, profile):
        """Ask the service for ``profile``. Returns at once: ``power.py`` says what follows."""
        snapshot = self._snapshot
        if not self._started or snapshot.pending:
            return                                    # ignored while a request is on its way, and while stopped
        self._armed = False
        asked = profile if profile in PROFILES else ""
        if snapshot.state != READY or not self._live:   # what is shown may be the last visit's until it is read
            self._update(failure=Failure(UNAVAILABLE, asked), note="")
        elif profile not in snapshot.available:
            self._update(failure=Failure(UNSUPPORTED, asked), note="")
        elif profile == snapshot.active:
            # The service would do nothing (and keep another program's hold), so it is not asked.
            self._armed = True
            self._update(failure=None, note="")
            self._remember(profile)
        else:
            self._end_helper()                        # its choice no longer stands: it must not set it again
            if self._call(OBJECT_PATH, PROPERTIES_IFACE, "Set",
                          GLib.Variant("(ssv)", (INTERFACE, ACTIVE_PROFILE, GLib.Variant("s", profile))), "()",
                          SET_TIMEOUT_MS, self._on_set, profile):
                self._update(pending=profile, failure=None, note="")
            else:
                self._update(failure=Failure(FAILED, profile), note="")
        self._announce()

    @_guarded
    def _on_set(self, bus, result, data):
        reply = self._finish(bus, result)
        epoch, profile = data
        if epoch != self._epoch:
            return                                    # forgotten: the owner left, or the source was stopped
        snapshot = self._snapshot
        changes = {"pending": "", "evidence": read_evidence(self._root)}
        remember = False
        if isinstance(reply, GLib.Variant):
            # The reply is the outcome: the service applied the profile, and announced it, before it answered.
            if snapshot.state == READY:
                self._armed = True
                remember = snapshot.active == profile
                if not remember:
                    changes["note"] = CHANGED_ELSEWHERE
        else:
            code = failure_code(reply)
            if code != TIMEOUT or snapshot.active != profile:  # a profile announced in time is no time-out
                changes["failure"] = Failure(code, profile)
        self._update(**changes)
        if remember:
            self._remember(profile)
        self._announce()

    # ---- Omarchy's memory of the choice ------------------------------------
    def _remember(self, profile: str):
        """Have Omarchy keep ``profile`` for the current power source, with its own command when it is installed.
        Its outcome is a note at most: the profile is set either way."""
        self._end_helper()
        try:
            script = (self._find_helper or _which_helper)()
        except Exception:  # noqa: BLE001 - a lookup that fails finds nothing
            script = None
        if not isinstance(script, str) or not os.path.isabs(script):
            return                                    # not installed: nothing to tell and nothing to say
        run = _Run(profile)
        self._runs.add(run)
        self._helper = run
        run.limit = self._schedule(HELPER_LIMIT_MS, functools.partial(self._helper_limit, run))
        try:
            run.kill = (self._run_helper or spawn_helper)([script, "autodetect", profile],
                                                         helper_environment(script, os.environ),
                                                         functools.partial(self._helper_exited, run))
        except (OSError, ValueError, subprocess.SubprocessError):
            self._cancel(run.limit)
            run.limit = self._helper = None
            self._runs.discard(run)
            self._update(note=NOT_REMEMBERED)

    def _end_helper(self):
        """Every run still going ends with its whole group and reports nothing: a newer choice, or another
        program's, stands now."""
        self._helper = None
        for run in list(self._runs):
            run.kill()

    @_guarded
    def _helper_limit(self, run: _Run):
        run.limit = None
        run.timed_out = True
        run.kill()

    @_guarded
    def _helper_exited(self, run: _Run, code: int):
        if run.limit is not None:
            self._cancel(run.limit)
            run.limit = None
        self._runs.discard(run)
        if run is not self._helper:
            return                                    # ended for a newer choice, or the window went meanwhile
        self._helper = None
        if code != 0 or run.timed_out:
            self._update(note=NOT_REMEMBERED)
            self._announce()

    @_guarded
    def retry(self):
        """Read the service again after it could not be read. Nothing otherwise."""
        if self._started and self._snapshot.state == ERROR and self._owner is not None and not self._reading:
            self._read()
            self._announce()

    @_guarded
    def dismiss(self):
        self._armed = False
        self._update(failure=None, note="")
        self._announce()

    # ---- the snapshot ------------------------------------------------------
    def _update(self, **changes):
        """A new snapshot only when something changed: an unchanged one stays the same object."""
        snapshot = dataclasses.replace(self._snapshot, **changes)
        if snapshot != self._snapshot:
            self._snapshot = snapshot

    def _announce(self):
        """Tell the window the snapshot changed since it was last told. Never while stopped."""
        if not self._started or self._snapshot == self._announced:
            return
        self._announced = self._snapshot
        try:
            self._on_change()
        except Exception as error:  # noqa: BLE001 - the window's trouble must not become the source's
            self.handler_error = type(error).__name__

    def _went_wrong(self, error):
        """Something unexpected raised: end what was in flight, so the source keeps working. Its text is not kept."""
        self.internal_error = type(error).__name__
        changes = {}
        if self._reading:
            self._reading = False
            changes.update(_service(ERROR))
        if self._snapshot.pending:
            changes.update(pending="", failure=Failure(FAILED, self._snapshot.pending))
        try:
            self._update(**changes)
            self._announce()
        except Exception:  # noqa: BLE001
            pass


# ---- the ``power`` command ---------------------------------------------------------------------------------------

FIRST_READING_S = CALL_TIMEOUT_MS / 1000 + 1                        # the bus says who owns the name, then one read
OUTCOME_S = (SET_TIMEOUT_MS + HELPER_LIMIT_MS) / 1000 + 1           # the reply, then Omarchy's command
STATE_REASONS = {ABSENT: "unavailable: power-profiles-daemon is not running",
                 ERROR: "failed: power-profiles-daemon could not be read",
                 UNKNOWN: "timeout: power-profiles-daemon did not answer in time"}
FAILURE_REASONS = {DENIED: "denied: this session may not change the power profile",
                   UNSUPPORTED: "unsupported: this machine does not offer {profile}",
                   UNAVAILABLE: "unavailable: power-profiles-daemon cannot be reached",
                   TIMEOUT: "timeout: power-profiles-daemon did not answer in time",
                   FAILED: "failed: power-profiles-daemon did not change the profile"}
NOTE_TEXTS = {CHANGED_ELSEWHERE: "changed elsewhere: another program has changed the profile since",
              NOT_REMEMBERED: "not remembered: Omarchy could not keep {profile} for this power source"}


def _wait(predicate, seconds: float) -> bool:
    """Run the main loop until ``predicate`` holds, for ``seconds`` at most. Whether it held."""
    context = GLib.MainContext.default()
    expired = []
    timer = GLib.timeout_add(max(1, round(seconds * 1000)), lambda: expired.append(True) or GLib.SOURCE_REMOVE)
    try:
        while not predicate() and not expired:
            context.iteration(True)
    finally:
        if not expired:
            GLib.source_remove(timer)
    return predicate()


def _status(snapshot: Snapshot) -> str:
    lines = [snapshot.active or "unknown", "offered: " + (", ".join(snapshot.available) or "none")]
    if snapshot.degraded:
        lines.append("performance held back: " + ", ".join(snapshot.degraded))
    lines += [f"held: {hold.profile}" + (f" by {hold.name}" if hold.name else "") for hold in snapshot.holds]
    return "\n".join(lines)


def main(args: list[str], usage: str = "", stdout=None, stderr=None, make_source=None) -> int:
    """``power [status]``, ``power set PROFILE`` and ``power cycle``: print or change the profile, then exit.

    It needs neither the window nor a running Gaming Home: it starts a source of its own, waits a bounded time for
    the first reading, acts, waits a bounded time for the outcome and for Omarchy's command, stops and exits. 0 is
    success, 1 a change or a reading that failed (with one line on stderr saying why) and 2 bad usage."""
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    action = args[0] if args else "status"
    if not (action in ("status", "cycle") and len(args) <= 1 or action == "set" and len(args) == 2
            and args[1] in PROFILES):
        print(usage, file=stderr)
        return 2
    source = (make_source or PowerProfiles)(lambda: None)
    source.start()
    try:
        _wait(lambda: source.snapshot.state != UNKNOWN, FIRST_READING_S)
        snapshot = source.snapshot
        if snapshot.state != READY:
            print(STATE_REASONS[snapshot.state], file=stderr)
            return 1
        if action == "status":
            print(_status(snapshot), file=stdout)
            return 0
        profile = args[1] if action == "set" else power.next_profile(snapshot.active, snapshot.available)
        if not profile:
            print("unknown profile: the active one is not one this machine offers, so there is no next one",
                  file=stderr)
            return 1
        source.set_profile(profile)
        _wait(lambda: not source.snapshot.pending and source.idle, OUTCOME_S)
        snapshot = source.snapshot
        if snapshot.failure is not None or snapshot.pending:
            code = snapshot.failure.code if snapshot.failure is not None else TIMEOUT
            print(FAILURE_REASONS[code].format(profile=profile), file=stderr)
            return 1
        print(snapshot.active or "unknown", file=stdout)
        if snapshot.note:
            print(NOTE_TEXTS[snapshot.note].format(profile=profile), file=stderr)
        return 0
    finally:
        source.stop()
