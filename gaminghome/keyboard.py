"""Best-effort visibility control for wvkbd without grabbing or injecting input.

The installed man page and upstream wvkbd README/``main.c`` at
4ae78249f39e1c9ce67a939d338aab2ad1e3d919 document SIGUSR1 = hide,
SIGUSR2 = show and SIGRTMIN = toggle. On Hyprland, Game Mode observes the
exact ``wvkbd`` layer-shell namespace. wvkbd provides no acknowledgement API.

Process discovery reads bounded ``/proc/<pid>/comm``, ``stat``, ``status``,
``environ``, task statuses and the executable magic link. A pidfd pins the
candidate before validation. The executable must resolve to a trusted system
binary, all four Uids and the Wayland/Hyprland session variables must match,
and all threads must expose the requested signal in SigBlk or SigCgt. The age,
session, executable, state and thread-mask checks are a best-effort safety net:
they are not an acknowledgement from wvkbd and still have a small race before
the pidfd send.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BACKENDS = ("wvkbd-mobintl", "wvkbd")
TRUSTED_BIN_DIRS = (Path("/usr/bin"), Path("/usr/local/bin"), Path("/bin"))
MAX_PROC_ENTRIES = 8192
MAX_PROC_READ = 8192
MAX_ENVIRON = 256 * 1024
MAX_TASKS = 64
MAX_MONITOR_OUTPUT = 64 * 1024
MAX_LAYERS_OUTPUT = 256 * 1024
MAX_ARGS = 16
MAX_ARG_CHARS = 4096
MAX_ARGS_CHARS = 16384
READY_AGE_S = 2.0
CONFIRM_DELAY_S = 0.2
FALLBACK_MONITOR_HEIGHT = 720
LOCK_RETRY_S = 0.05
LOCK_TIMEOUT_S = 2.0

NO_BACKEND = "wvkbd is not installed (AUR: wvkbd)"
STILL_STARTING = "wvkbd is still starting; try again"
_NO_VALUE = object()
_DEFAULT_PIDFD = object()
_DECIMAL_RE = re.compile(r"^[0-9]+$")
_FONT_RE = re.compile(r"^(?P<family>[A-Za-z0-9 -]{1,63}) (?P<size>[0-9]+)$")


class KeyboardLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeyboardArgs:
    value: list[str]
    present: bool
    error: str | None
    ignored_legacy_override: bool
    settings_error: str | None = None


@dataclass(frozen=True)
class TrustedExecutable:
    name: str
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    backend: str
    start_time: int
    age: float


@dataclass
class PinnedProcess:
    snapshot: ProcessSnapshot
    pidfd: int


@dataclass
class Discovery:
    processes: list[PinnedProcess]
    error: str | None = None

    def close(self, close_fd: Callable[[int], None]):
        for process in self.processes:
            try:
                close_fd(process.pidfd)
            except OSError:
                pass
        self.processes.clear()


@dataclass(frozen=True)
class Visibility:
    value: str
    error: str | None = None


def keyboard_sizing(logical_height: int) -> dict[str, int]:
    """Return bounded landscape height and font-size hints."""
    try:
        height = int(logical_height)
    except (TypeError, ValueError, OverflowError):
        height = FALLBACK_MONITOR_HEIGHT
    height = max(1, height)
    landscape_height = max(220, min(420, round(height * 0.28)))
    font_size = max(16, min(24, round(landscape_height / 14.5)))
    return {"landscape_height": landscape_height, "font_size": font_size}


def _run_bounded(argv: list[str], timeout: float, output_limit: int) -> bytes:
    """Run one argv-only query with bounded output and wall time."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    if process.stdout is None:
        process.wait()
        raise OSError("query did not expose stdout")
    data = bytearray()
    deadline = time.monotonic() + timeout
    eof = False
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.terminate()
                break
            events = selector.select(remaining)
            if not events:
                continue
            remaining_bytes = output_limit + 1 - len(data)
            chunk = os.read(process.stdout.fileno(), min(65536, remaining_bytes))
            if chunk:
                data.extend(chunk)
                if len(data) > output_limit:
                    process.terminate()
                    break
            else:
                eof = True
    try:
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait(timeout=0.5)
    if len(data) > output_limit:
        raise ValueError(f"query output exceeds {output_limit} bytes")
    if returncode != 0 or not eof:
        raise OSError(f"query failed with exit status {returncode}")
    return bytes(data)


def focused_monitor_logical_height(
    run: Callable[[list[str], float, int], bytes],
    hyprctl: str | None,
) -> int:
    """Return the focused monitor's logical height, or 720."""
    if not hyprctl:
        return FALLBACK_MONITOR_HEIGHT
    try:
        raw = run([hyprctl, "monitors", "-j"], 1.0, MAX_MONITOR_OUTPUT)
        payload = json.loads(raw)
        if not isinstance(payload, list):
            return FALLBACK_MONITOR_HEIGHT
        focused = next(
            (item for item in payload if isinstance(item, dict) and item.get("focused") is True),
            None,
        )
        if not isinstance(focused, dict):
            return FALLBACK_MONITOR_HEIGHT
        height = float(focused["height"])
        scale = float(focused["scale"])
        if not math.isfinite(height) or not 200 <= height <= 8000:
            return FALLBACK_MONITOR_HEIGHT
        if not math.isfinite(scale) or not 0.5 <= scale <= 8:
            return FALLBACK_MONITOR_HEIGHT
        logical = round(height / scale)
        return logical if logical > 0 else FALLBACK_MONITOR_HEIGHT
    except (
        KeyError,
        OSError,
        OverflowError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return FALLBACK_MONITOR_HEIGHT


def layers(
    run: Callable[[list[str], float, int], bytes],
    hyprctl: str,
):
    """Run a bounded Hyprland layers query."""
    raw = run([hyprctl, "layers", "-j"], 1.0, MAX_LAYERS_OUTPUT)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Hyprland layers response is not an object")
    return payload


def _layer_visibility(payload, expected_pid: int | None = None) -> Visibility:
    if not isinstance(payload, dict) or not payload:
        return Visibility("unknown")
    recognized = False
    found = False
    mismatched = []
    for monitor in payload.values():
        if not isinstance(monitor, dict):
            return Visibility("unknown")
        wrapped = monitor.get("levels", _NO_VALUE)
        level_data = wrapped if wrapped is not _NO_VALUE else monitor
        if not isinstance(level_data, dict):
            return Visibility("unknown")
        for value in level_data.values():
            if not isinstance(value, list):
                return Visibility("unknown")
            recognized = True
            for layer in value:
                if not isinstance(layer, dict) or not isinstance(layer.get("namespace"), str):
                    return Visibility("unknown")
                if layer["namespace"] not in ("wvkbd", "wvkbd-mobintl"):
                    continue
                found = True
                pid = layer.get("pid", _NO_VALUE)
                if pid is _NO_VALUE:
                    continue
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                    return Visibility("unknown")
                if expected_pid is not None and pid != expected_pid:
                    mismatched.append(pid)
    if mismatched:
        pids = ", ".join(map(str, sorted(set(mismatched))))
        return Visibility("unknown", f"wvkbd layer belongs to pid {pids}, not candidate {expected_pid}")
    if found:
        return Visibility("visible")
    return Visibility("hidden" if recognized else "unknown")


def visibility_from_layers(payload, expected_pid: int | None = None) -> str:
    """Pure visibility projection for validated Hyprland layer JSON."""
    return _layer_visibility(payload, expected_pid).value


def hyprland_visibility(
    run: Callable[[list[str], float, int], bytes],
    hyprctl: str | None,
    expected_pid: int | None = None,
) -> Visibility:
    if not hyprctl:
        return Visibility("unknown")
    try:
        return _layer_visibility(layers(run, hyprctl), expected_pid)
    except (OSError, RecursionError, subprocess.SubprocessError, TypeError, ValueError,
            json.JSONDecodeError):
        return Visibility("unknown")


def validate_keyboard_args(value) -> tuple[list[str] | None, str | None]:
    """Validate the safe subset of wvkbd 0.21 arguments."""
    if not isinstance(value, list):
        return None, "keyboardArgs must be a list of strings"
    if len(value) > MAX_ARGS:
        return None, f"keyboardArgs has more than {MAX_ARGS} items"
    for index, item in enumerate(value):
        if not isinstance(item, str):
            return None, f"keyboardArgs item {index} is not a string"
        if not item:
            return None, f"keyboardArgs item {index} is empty"
        if len(item) > MAX_ARG_CHARS:
            return None, f"keyboardArgs item {index} exceeds {MAX_ARG_CHARS} characters"
        if "\x00" in item or "\n" in item or "\r" in item:
            return None, f"keyboardArgs item {index} contains NUL or a newline"
    if sum(len(item) for item in value) > MAX_ARGS_CHARS:
        return None, f"keyboardArgs exceeds {MAX_ARGS_CHARS} characters"

    result = []
    seen = set()
    options = {"-H", "-L", "--fn"}
    flags = {"--no-popup", "--no-highlight", "--no-feedback", "--non-exclusive"}
    index = 0
    while index < len(value):
        option = value[index]
        if option not in options | flags:
            return None, "keyboardArgs contains an argument that is not allowed"
        if option in seen:
            return None, f"keyboardArgs option {option} is repeated"
        seen.add(option)
        result.append(option)
        if option in flags:
            index += 1
            continue
        if index + 1 >= len(value):
            return None, f"keyboardArgs option {option} is missing its value"
        item = value[index + 1]
        if item.startswith("-"):
            return None, f"keyboardArgs option {option} is missing its value"
        if option in ("-H", "-L"):
            if not _DECIMAL_RE.fullmatch(item) or not 160 <= int(item) <= 600:
                return None, f"keyboardArgs option {option} must be an integer from 160 to 600"
        else:
            match = _FONT_RE.fullmatch(item)
            if (
                not match
                or not any(character.isalnum() and character.isascii() for character in match.group("family"))
                or not 12 <= int(match.group("size")) <= 32
            ):
                return None, "keyboardArgs --fn must be '<family> <size>' with size 12..32"
        result.append(item)
        index += 2
    return result, None


@contextmanager
def keyboard_action_lock(
    runtime_dir: str | None = None,
    timeout: float = LOCK_TIMEOUT_S,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
):
    """Serialize direct CLI actions with a private, non-blocking lock."""
    if runtime_dir is None:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir or not os.path.isabs(runtime_dir):
        raise KeyboardLockError("XDG_RUNTIME_DIR is unavailable; cannot safely run keyboard action")
    directory = Path(runtime_dir) / "omarchy-game-mode"
    path = directory / "keyboard.lock"
    descriptor = -1
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise KeyboardLockError(f"{path} is not a regular lock file")
        os.fchmod(descriptor, 0o600)
        deadline = clock() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise KeyboardLockError(
                        "another keyboard action is already in progress; try again"
                    ) from error
                sleep(min(LOCK_RETRY_S, remaining))
        yield descriptor
    except KeyboardLockError:
        raise
    except OSError as error:
        raise KeyboardLockError(f"keyboard action lock is unavailable: {error}") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


class KeyboardController:
    """Synchronous, pidfd-pinned wvkbd process control for the CLI."""

    def __init__(
        self,
        *,
        proc_root: str | Path = "/proc",
        pidfd_open=_DEFAULT_PIDFD,
        pidfd_send_signal=_DEFAULT_PIDFD,
        close_fd=os.close,
        stat_result=os.stat,
        realpath=os.path.realpath,
        which=shutil.which,
        run: Callable[[list[str], float, int], bytes] = _run_bounded,
        spawn: Callable[[list[str]], object] | None = None,
        boot_clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monitor_height: Callable[[], int] | None = None,
        visibility_provider=None,
        settings_accessor=None,
        environment=None,
        uid: int | None = None,
        scan_limit: int = MAX_PROC_ENTRIES,
        clock_ticks: int | None = None,
    ):
        self.proc_root = Path(proc_root)
        self._pidfd_open = getattr(os, "pidfd_open", None) if pidfd_open is _DEFAULT_PIDFD else pidfd_open
        self._pidfd_send_signal = (
            getattr(signal, "pidfd_send_signal", None)
            if pidfd_send_signal is _DEFAULT_PIDFD
            else pidfd_send_signal
        )
        self._close_fd = close_fd
        self._stat = stat_result
        self._realpath = realpath
        self._which = which
        self._run = run
        self._spawn = spawn or self._spawn_detached
        self._boot_clock = boot_clock or self._boottime
        self._sleep = sleep
        self._monitor_height = monitor_height
        self._visibility_provider = visibility_provider
        self._settings_accessor = settings_accessor
        self._environment = dict(os.environ if environment is None else environment)
        self._uid = os.getuid() if uid is None else uid
        self._scan_limit = scan_limit
        self._clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
        self._last_command: str | None = None
        self.command_error: str | None = None
        self.args_error: str | None = None
        self.ignored_legacy_override = False

    @staticmethod
    def _boottime() -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME)

    @staticmethod
    def _result(ok: bool, action: str, detail: str, visibility: str | None = None) -> dict:
        result = {"ok": ok, "action": action, "detail": detail}
        if visibility is not None:
            result["visibility"] = visibility
        return result

    def status(self) -> dict:
        args = self._settings()
        self.args_error = args.error
        self.ignored_legacy_override = args.ignored_legacy_override
        backends = self._trusted_backends()
        discovery = self._discover(backends)
        try:
            running = len(discovery.processes)
            candidate = discovery.processes[0].snapshot.pid if running == 1 else None
            visibility = self._visibility(candidate)
            command_error = discovery.error
            if not backends:
                command_error = NO_BACKEND
                if args.settings_error:
                    command_error += f"; {args.settings_error}"
            elif not command_error:
                command_error = args.settings_error or visibility.error
            self.command_error = command_error
            return {
                "installed": bool(backends),
                "running": bool(running),
                "backend": (
                    discovery.processes[0].snapshot.backend
                    if len(discovery.processes) == 1
                    else self._preferred_backend(backends)
                ),
                "last_command": self._last_command,
                "visibility": visibility.value,
                "command_error": command_error,
                "args_error": args.error,
                "ignored_legacy_override": args.ignored_legacy_override,
            }
        finally:
            discovery.close(self._close_fd)

    def show(self) -> dict:
        return self._command("show")

    def hide(self) -> dict:
        return self._command("hide")

    def toggle(self) -> dict:
        return self._command("toggle")

    def _settings(self) -> KeyboardArgs:
        accessor = self._settings_accessor
        if accessor is None:
            from .settings import Settings

            accessor = Settings
        try:
            settings = accessor() if callable(accessor) else accessor
        except (OSError, TypeError, ValueError, UnicodeError, RecursionError) as error:
            return KeyboardArgs([], False, None, False, f"could not read settings: {error}")
        if isinstance(settings, dict):
            present = "keyboardArgs" in settings
            raw = settings.get("keyboardArgs")
            legacy = "keyboardCommand" in settings
            error = None
        else:
            present = bool(getattr(settings, "keyboard_args_present", False))
            raw = getattr(settings, "keyboard_args", None)
            legacy = bool(getattr(settings, "legacy_keyboard_command_present", False))
            error = getattr(settings, "load_error", None)
        settings_error = f"settings load error: {error}" if error else None
        if not present:
            return KeyboardArgs([], False, None, legacy, settings_error)
        value, reason = validate_keyboard_args(raw)
        return KeyboardArgs(value or [], True, reason, legacy, settings_error)

    def _trusted_executable(self, name: str) -> TrustedExecutable | None:
        try:
            raw = self._which(name)
        except OSError:
            return None
        if not isinstance(raw, str) or not os.path.isabs(raw):
            return None
        try:
            supplied_dir = Path(raw).parent
            resolved = Path(self._realpath(raw))
            if supplied_dir not in TRUSTED_BIN_DIRS or resolved.parent not in TRUSTED_BIN_DIRS:
                return None
            identity = self._stat(str(resolved))
            if not stat.S_ISREG(identity.st_mode) or identity.st_mode & 0o111 == 0:
                return None
        except (OSError, RuntimeError, ValueError):
            return None
        return TrustedExecutable(name, str(resolved), identity.st_dev, identity.st_ino)

    def _trusted_backends(self) -> dict[str, TrustedExecutable]:
        return {
            name: executable
            for name in BACKENDS
            if (executable := self._trusted_executable(name)) is not None
        }

    @staticmethod
    def _preferred_backend(backends: dict[str, TrustedExecutable]) -> str | None:
        return next((name for name in BACKENDS if name in backends), None)

    def _read_bounded(self, path: Path, limit: int = MAX_PROC_READ) -> bytes:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{path} exceeds {limit} bytes")
        return data

    @staticmethod
    def _parse_stat(data: bytes) -> int:
        text = data.decode("utf-8", "replace")
        closing = text.rfind(")")
        if closing < 0:
            raise ValueError("missing stat command terminator")
        fields = text[closing + 1 :].split()
        if len(fields) <= 19:
            raise ValueError("short process stat")
        return int(fields[19])

    def _parse_status(self, data: bytes) -> tuple[list[int], str]:
        uids = None
        state = None
        for line in data.decode("ascii", "replace").splitlines():
            fields = line.split()
            if line.startswith("Uid:"):
                if len(fields) != 5 or not all(value.isdecimal() for value in fields[1:]):
                    raise ValueError("malformed Uid status")
                uids = [int(value) for value in fields[1:]]
            elif line.startswith("State:") and len(fields) > 1:
                state = fields[1]
        if uids is None or state is None:
            raise ValueError("missing Uid or State")
        return uids, state

    def _session_environment(self, pid: int) -> str | None:
        own_wayland = self._environment.get("WAYLAND_DISPLAY")
        own_signature = self._environment.get("HYPRLAND_INSTANCE_SIGNATURE")
        own_runtime_dir = self._environment.get("XDG_RUNTIME_DIR")
        if not own_wayland or not own_signature or not own_runtime_dir:
            return "current Wayland/Hyprland/XDG runtime session variables are unavailable"
        try:
            raw = self._read_bounded(self.proc_root / str(pid) / "environ", MAX_ENVIRON)
        except (OSError, ValueError):
            return f"pid {pid} session environment is unreadable"
        values = {}
        for field in raw.split(b"\x00"):
            if b"=" not in field:
                continue
            key, value = field.split(b"=", 1)
            values[key] = value
        candidate_wayland = values.get(b"WAYLAND_DISPLAY")
        candidate_signature = values.get(b"HYPRLAND_INSTANCE_SIGNATURE")
        candidate_runtime_dir = values.get(b"XDG_RUNTIME_DIR")
        if b"WAYLAND_SOCKET" in values:
            return f"pid {pid} has WAYLAND_SOCKET set; refusing cross-session control"
        if (
            candidate_wayland != os.fsencode(own_wayland)
            or candidate_signature != os.fsencode(own_signature)
            or candidate_runtime_dir != os.fsencode(own_runtime_dir)
        ):
            return f"pid {pid} belongs to a different Wayland/Hyprland/XDG runtime session"
        return None

    def _snapshot(
        self,
        pid: int,
        backends: dict[str, TrustedExecutable],
    ) -> tuple[ProcessSnapshot | None, str | None]:
        process_dir = self.proc_root / str(pid)
        try:
            comm = self._read_bounded(process_dir / "comm").decode("utf-8", "replace").strip()
            trusted = backends.get(comm)
            if trusted is None:
                return None, f"pid {pid} is no longer a selected wvkbd process"
            identity = self._stat(str(process_dir / "exe"))
            if (identity.st_dev, identity.st_ino) != (trusted.device, trusted.inode):
                return None, f"pid {pid} is not the trusted wvkbd executable"
            uids, state = self._parse_status(self._read_bounded(process_dir / "status"))
            if state[0].upper() in ("Z", "X"):
                return None, None
            if len(uids) != 4 or any(value != self._uid for value in uids):
                return None, f"pid {pid} is not owned by the current uid"
            start_time = self._parse_stat(self._read_bounded(process_dir / "stat"))
            session_error = self._session_environment(pid)
            if session_error:
                return None, session_error
            age = self._boot_clock() - (start_time / self._clock_ticks)
        except (OSError, TypeError, ValueError, UnicodeError) as error:
            return None, f"pid {pid} could not be validated: {error}"
        return ProcessSnapshot(pid, comm, start_time, age), None

    def _discover(self, backends: dict[str, TrustedExecutable]) -> Discovery:
        if not backends:
            return Discovery([], NO_BACKEND)
        if self._pidfd_open is None or self._pidfd_send_signal is None:
            return Discovery([], "pidfd support is unavailable; refusing to control wvkbd")
        pinned = []
        errors = []
        count = 0
        try:
            with os.scandir(self.proc_root) as entries:
                for entry in entries:
                    if count >= self._scan_limit:
                        discovery = Discovery([], f"process scan exceeded {self._scan_limit} entries; refusing to act")
                        discovery.close(self._close_fd)
                        for process in pinned:
                            try:
                                self._close_fd(process.pidfd)
                            except OSError:
                                pass
                        return discovery
                    count += 1
                    if not entry.name.isdecimal():
                        continue
                    pid = int(entry.name)
                    try:
                        comm = self._read_bounded(self.proc_root / entry.name / "comm").decode(
                            "utf-8", "replace"
                        ).strip()
                    except (OSError, ValueError):
                        continue
                    if comm not in BACKENDS:
                        continue
                    try:
                        pidfd = self._pidfd_open(pid)
                    except ProcessLookupError:
                        errors.append(f"pid {pid} vanished during validation")
                        continue
                    except OSError as error:
                        errors.append(f"could not pin pid {pid}: {error}")
                        continue
                    try:
                        snapshot, error = self._snapshot(pid, backends)
                    except Exception:
                        self._close_fd(pidfd)
                        raise
                    if snapshot is None:
                        self._close_fd(pidfd)
                        if error:
                            errors.append(error)
                        continue
                    pinned.append(PinnedProcess(snapshot, pidfd))
        except BaseException as error:
            for process in pinned:
                try:
                    self._close_fd(process.pidfd)
                except OSError:
                    pass
            if isinstance(error, OSError):
                return Discovery([], f"could not scan wvkbd processes: {error}")
            raise
        if len(pinned) > 1:
            return Discovery(pinned, self._multiple_message(len(pinned)))
        if errors:
            for process in pinned:
                self._close_fd(process.pidfd)
            return Discovery([], errors[0])
        return Discovery(pinned)

    @staticmethod
    def _multiple_message(count: int) -> str:
        return f"{count} wvkbd instances are running; refusing ambiguous visibility control"

    def _thread_signal_ready(self, pid: int, sig) -> bool:
        tasks = self.proc_root / str(pid) / "task"
        target = 1 << (sig - 1)
        count = 0
        seen = False
        try:
            with os.scandir(tasks) as entries:
                for entry in entries:
                    if count >= MAX_TASKS:
                        return False
                    count += 1
                    if not entry.name.isdecimal():
                        continue
                    seen = True
                    try:
                        status = self._read_bounded(Path(entry.path) / "status").decode("ascii", "replace")
                    except (OSError, ValueError):
                        return False
                    masks = []
                    for line in status.splitlines():
                        if line.startswith("SigBlk:") or line.startswith("SigCgt:"):
                            fields = line.split()
                            if len(fields) != 2:
                                return False
                            try:
                                masks.append(int(fields[1], 16))
                            except ValueError:
                                return False
                    if len(masks) != 2 or not ((masks[0] | masks[1]) & target):
                        return False
        except OSError:
            return False
        return seen

    def _visibility(self, expected_pid: int | None) -> Visibility:
        if self._visibility_provider is not None:
            try:
                result = self._visibility_provider(expected_pid)
                if isinstance(result, Visibility):
                    return result
                if result in ("visible", "hidden", "unknown"):
                    return Visibility(result)
                return Visibility("unknown")
            except Exception:
                return Visibility("unknown")
        hyprctl = self._trusted_executable("hyprctl")
        return hyprland_visibility(self._run, hyprctl.path if hyprctl else None, expected_pid)

    def _monitor_height_value(self) -> int:
        if self._monitor_height is not None:
            try:
                return int(self._monitor_height())
            except (OSError, TypeError, ValueError):
                return FALLBACK_MONITOR_HEIGHT
        hyprctl = self._trusted_executable("hyprctl")
        return focused_monitor_logical_height(self._run, hyprctl.path if hyprctl else None)

    @staticmethod
    def _action_signal(action: str, visibility: str):
        if action == "show":
            return None if visibility == "visible" else signal.SIGUSR2
        if action == "hide":
            return None if visibility == "hidden" else signal.SIGUSR1
        if visibility == "visible":
            return signal.SIGUSR1
        if visibility == "hidden":
            return signal.SIGUSR2
        return signal.SIGRTMIN

    @staticmethod
    def _target_visibility(action: str, before: str):
        if action == "show":
            return "visible"
        if action == "hide":
            return "hidden"
        if before == "visible":
            return "hidden"
        if before == "hidden":
            return "visible"
        return None

    def _spawn_args(self, args: KeyboardArgs) -> list[str]:
        if args.present and args.error is None:
            return list(args.value)
        sizing = keyboard_sizing(self._monitor_height_value())
        return ["-H", str(sizing["landscape_height"]), "-L", str(sizing["landscape_height"])]

    def _launch_command(
        self,
        backends: dict[str, TrustedExecutable],
        args: KeyboardArgs,
    ) -> tuple[list[str] | None, str | None]:
        backend = self._preferred_backend(backends)
        if backend is None:
            return None, NO_BACKEND
        return [backends[backend].path, *self._spawn_args(args)], None

    def _spawn_detached(self, argv: list[str]):
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=dict(os.environ),
        )

    def _start_visible(self, backends: dict[str, TrustedExecutable], args: KeyboardArgs) -> dict:
        command, error = self._launch_command(backends, args)
        if error:
            return self._result(False, self._last_command or "show", error)
        try:
            self._spawn(command)
        except (OSError, TypeError, ValueError) as error:
            return self._result(False, self._last_command or "show", f"could not start wvkbd: {error}")
        return self._result(True, self._last_command or "show", "started visible", "visible")

    def _signal_existing(
        self,
        action: str,
        process: PinnedProcess,
        backends: dict[str, TrustedExecutable],
        visibility: Visibility,
    ) -> dict:
        if visibility.error:
            return self._result(False, action, f"{visibility.error}; refusing to signal", "unknown")
        sig = self._action_signal(action, visibility.value)
        if sig is None:
            return self._result(True, action, f"already {visibility.value}", visibility.value)
        snapshot, error = self._snapshot(process.snapshot.pid, backends)
        if snapshot is None or error:
            return self._result(False, action, error or STILL_STARTING, visibility.value)
        if snapshot.start_time != process.snapshot.start_time:
            return self._result(False, action, f"pid {snapshot.pid} changed during validation", visibility.value)
        if snapshot.age < READY_AGE_S or not self._thread_signal_ready(snapshot.pid, sig):
            return self._result(False, action, STILL_STARTING, visibility.value)
        snapshot, error = self._snapshot(process.snapshot.pid, backends)
        if (
            snapshot is None
            or error
            or snapshot.start_time != process.snapshot.start_time
        ):
            return self._result(False, action, error or f"pid {process.snapshot.pid} changed before signal",
                                visibility.value)
        try:
            self._pidfd_send_signal(process.pidfd, sig, None, 0)
        except (OSError, PermissionError, ProcessLookupError) as error:
            return self._result(False, action, f"could not signal wvkbd safely: {error}", visibility.value)
        result = self._result(True, action, f"{action} signal sent", visibility.value)
        self._sleep(CONFIRM_DELAY_S)
        observed = self._visibility(snapshot.pid)
        expected = self._target_visibility(action, visibility.value)
        if expected is None:
            if observed.value in ("visible", "hidden"):
                result["detail"] = f"toggled; observed {observed.value}"
                result["visibility"] = observed.value
            else:
                result["detail"] += "; could not confirm"
            return result
        if observed.value == expected:
            verb = "shown" if expected == "visible" else "hidden"
            result["detail"] = f"{verb}; confirmed {expected}"
            result["visibility"] = expected
        else:
            result["detail"] += "; could not confirm"
            result["visibility"] = observed.value
        return result

    def _command(self, action: str) -> dict:
        self._last_command = action
        args = self._settings()
        self.args_error = args.error
        self.ignored_legacy_override = args.ignored_legacy_override
        backends = self._trusted_backends()
        if not backends:
            self.command_error = NO_BACKEND
            return self._result(False, action, NO_BACKEND)
        discovery = self._discover(backends)
        try:
            if discovery.error:
                self.command_error = discovery.error
                return self._result(False, action, discovery.error)
            if not discovery.processes:
                if action == "hide":
                    self.command_error = None
                    return self._result(True, action, "already hidden/not running", "hidden")
                result = self._start_visible(backends, args)
                self.command_error = None if result["ok"] else result["detail"]
                return result
            visibility = self._visibility(discovery.processes[0].snapshot.pid)
            result = self._signal_existing(
                action,
                discovery.processes[0],
                backends,
                visibility,
            )
            self.command_error = None if result["ok"] else result["detail"]
            return result
        finally:
            discovery.close(self._close_fd)
