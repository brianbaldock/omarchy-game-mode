"""Injectable access to the outside world: files, subprocesses, sockets and clocks.

Every check receives a `Context` and touches the system only through it, so tests
replace each primitive with a fake and never need a network, a device or root.
The real primitives here are the only place that spawns processes or opens files:
commands are argv lists (never a shell) with a deadline and a bounded capture, and
file reads are bounded and read-only.

Every command runs in a process group of its own and stays tracked until it has been
collected, so `stop_commands` can end all of them, and whatever they started, when the
doctor stops: normally, on Ctrl-C, or on SIGTERM or SIGHUP (see `ending_signals_raise`).
"""

from __future__ import annotations

import contextlib
import os
import select
import selectors
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .redact import Redactor
from .runner import cancelled, check_cancelled, parallel_map, time_left

READ_LIMIT = 65536
OUTPUT_LIMIT = 262144
LIST_LIMIT = 4096
# Python's `ssl.create_default_context` (and Chromium) append TLS session secrets to the file this
# variable names: a read-only diagnostic must never create or extend it.
KEY_LOG_VARIABLE = "SSLKEYLOGFILE"
# Variables that can send a tool somewhere else: an X11 or Wayland display or a D-Bus bus on another
# machine, an HTTP proxy, or resolver search domains and host aliases. No tool the doctor runs gets them.
ENDPOINT_VARIABLES = frozenset({
    "DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_SYSTEM_BUS_ADDRESS", "LOCALDOMAIN", "RES_OPTIONS", "HOSTALIASES",
    *(name for proxy in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy")
      for name in (proxy, proxy.upper())),
})
ENDING_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Interrupted(KeyboardInterrupt):
    """A signal ended the command; raised in place of the signal's default action, so clean-up runs."""

    def __init__(self, number: int):
        super().__init__(number)
        self.number = number


class _Ending:
    """Main-thread state for the handler below: how deep inside `ending_signals_held`, and what waits."""

    holding = 0
    waiting: int | None = None


def _interrupt(number: int, _frame: Any) -> None:
    if _Ending.holding:
        if _Ending.waiting is None:
            _Ending.waiting = number
        return
    raise Interrupted(number)


@contextlib.contextmanager
def ending_signals_raise() -> Iterator[None]:
    """While inside, SIGINT, SIGTERM and SIGHUP raise `Interrupted` instead of killing the process.

    A signal the process was started with ignored stays ignored. Outside the main thread,
    where Python cannot install handlers, nothing changes.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    try:
        for number in ENDING_SIGNALS:
            if signal.getsignal(number) is not signal.SIG_IGN:
                previous[number] = signal.signal(number, _interrupt)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextlib.contextmanager
def ending_signals_held() -> Iterator[None]:
    """Hold `Interrupted` back for a moment; a signal that arrived meanwhile raises it on the way out.

    The signal mask stays as it is on purpose: a command started inside would inherit it.
    Only the main thread ever gets `Interrupted`, so elsewhere there is nothing to hold.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _Ending.holding += 1
    try:
        yield
    finally:
        _Ending.holding -= 1
        if not _Ending.holding and _Ending.waiting is not None:
            number, _Ending.waiting = _Ending.waiting, None
            raise Interrupted(number)


@contextlib.contextmanager
def ending_signals_ignored() -> Iterator[None]:
    """While inside, the ending signals do nothing: for clean-up that has to finish once begun."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    try:
        for number in ENDING_SIGNALS:
            previous[number] = signal.signal(number, signal.SIG_IGN)
        yield
    finally:
        for number, handler in previous.items():
            if handler is not None:
                signal.signal(number, handler)


@dataclass(frozen=True)
class Options:
    scope: str = "all"
    timeout: float = 6.0
    console_ip: str = ""
    interface: str = ""
    redact: bool = True


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error


class FileSystem:
    """Bounded, read-only file access below `root` (tests point it at a scratch tree)."""

    def __init__(self, root: str | os.PathLike[str] = "/"):
        self.root = Path(root)

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    def read_bytes(self, absolute: str, limit: int = READ_LIMIT) -> bytes | None:
        try:
            with open(self.path(absolute), "rb") as handle:
                return handle.read(limit)
        except OSError:
            return None

    def read(self, absolute: str, limit: int = READ_LIMIT) -> str | None:
        data = self.read_bytes(absolute, limit)
        return None if data is None else data.decode("utf-8", "replace")

    def line(self, absolute: str) -> str:
        """First line, stripped; empty when the file is missing or unreadable."""
        text = self.read(absolute, 4096)
        return text.splitlines()[0].strip() if text and text.strip() else ""

    def listdir(self, absolute: str) -> list[str]:
        try:
            return sorted(os.listdir(self.path(absolute)))[:LIST_LIMIT]
        except OSError:
            return []

    def exists(self, absolute: str) -> bool:
        return os.path.lexists(self.path(absolute))

    def mode(self, absolute: str) -> int | None:
        try:
            return os.stat(self.path(absolute)).st_mode
        except OSError:
            return None

    def readable(self, absolute: str) -> bool:
        return os.access(self.path(absolute), os.R_OK)

    def is_char_device(self, absolute: str) -> bool:
        mode = self.mode(absolute)
        return mode is not None and stat.S_ISCHR(mode)

    def resolved(self, absolute: str) -> str:
        """Symlink-free form of a path, expressed below the root again."""
        real = os.path.realpath(self.path(absolute))
        root = os.path.realpath(self.root)
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return "/" + os.path.relpath(real, root)
        return real


def _command_env(env: Mapping[str, str] | None) -> dict[str, str]:
    merged = {name: value for name, value in (os.environ if env is None else env).items()
              if name != KEY_LOG_VARIABLE and name not in ENDPOINT_VARIABLES}
    merged.update(LC_ALL="C", LANG="C")
    return merged


def tls_client_context() -> ssl.SSLContext:
    """A verifying TLS client context with the settings of `ssl.create_default_context`.

    It is built by hand because that function opens the file named by SSLKEYLOGFILE for
    appending, which would make the doctor write session secrets to disk.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN | ssl.VERIFY_X509_STRICT
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return context


def _drain(proc: subprocess.Popen[bytes], deadline: float, limit: int) -> tuple[bytes, bytes, bool]:
    """Collect at most `limit` bytes per stream until both close; True as well when the deadline passed."""
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    timed_out = False
    with selectors.DefaultSelector() as selector:
        for stream in buffers:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _events in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[key.fileobj]
                buffer += chunk[:max(0, limit - len(buffer))]
    return bytes(buffers[proc.stdout]), bytes(buffers[proc.stderr]), timed_out


def _exited(proc: subprocess.Popen[bytes], deadline: float) -> bool:
    """Wait until the command itself exits, without collecting it.

    A process that has not been collected keeps its pid, so `_reap` can still name its
    process group without any risk of the pid having been reused.
    """
    if proc.returncode is not None:  # `stop_commands` got there first
        return True
    try:
        handle = os.pidfd_open(proc.pid)
    except (AttributeError, OSError):
        try:
            proc.wait(max(0.05, deadline - time.monotonic()))
            return True
        except subprocess.TimeoutExpired:
            return False
    try:
        return bool(select.select([handle], [], [], max(0.05, deadline - time.monotonic()))[0])
    finally:
        os.close(handle)


_RUNNING: set[subprocess.Popen[bytes]] = set()
_RUNNING_LOCK = threading.Lock()


def _start(argv: tuple[str, ...], env: Mapping[str, str] | None) -> subprocess.Popen[bytes] | None:
    """Start `argv` in a new session (so a process group of its own) and track it.

    None once the check it runs for has to stop: `stop_commands` may already have run,
    and nothing may start after it.
    """
    with _RUNNING_LOCK:
        if cancelled():
            return None
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_command_env(env), start_new_session=True)
        _RUNNING.add(proc)
        return proc


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the command's process group; called once, by whoever takes it off the tracked set.

    `_exited` leaves the command uncollected, so its pid still names its group and nothing else.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        proc.kill()


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Kill the command's whole process group, then collect the command.

    The group goes whether or not the command is still running: a child it started stays in
    its group and can outlive it, still running or still holding a pipe open.
    """
    with _RUNNING_LOCK:
        if proc in _RUNNING:  # else `stop_commands` has killed it already
            _RUNNING.discard(proc)
            _kill_group(proc)
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def stop_commands(timeout: float = 2.0) -> None:
    """Kill every command still running, with everything it started, and collect them.

    For the way out: however the doctor ends, nothing it started keeps running. A check
    still running by then cannot start anything new, because the runner has stopped it.
    """
    with _RUNNING_LOCK:
        stopping = list(_RUNNING)
        _RUNNING.clear()
        for proc in stopping:
            _kill_group(proc)
    deadline = time.monotonic() + timeout
    for proc in stopping:
        try:
            proc.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass


def run_command(argv: Sequence[str], timeout: float = 5.0, *, env: Mapping[str, str] | None = None,
                limit: int = OUTPUT_LIMIT) -> CommandResult:
    """Run `argv` without a shell, with a deadline and bounded output. Never raises."""
    argv = tuple(argv)
    deadline = time.monotonic() + timeout
    proc = None
    try:
        with ending_signals_held():  # no signal may land between starting the command and tracking it
            proc = _start(argv, env)
        if proc is None:
            return CommandResult(argv, error="cancelled")
        out, err, timed_out = _drain(proc, deadline, limit)
        timed_out = timed_out or not _exited(proc, deadline)
    except FileNotFoundError:
        return CommandResult(argv, error="not-found")
    except OSError as exc:
        reason = os.strerror(exc.errno).lower() if exc.errno else type(exc).__name__
        return CommandResult(argv, error=f"os-error: {reason}")
    finally:
        if proc is not None:
            _reap(proc)
    return CommandResult(argv, None if timed_out else proc.returncode, out.decode("utf-8", "replace"),
                         err.decode("utf-8", "replace"), "timeout" if timed_out else "")


def _udp_socket(family: int = socket.AF_INET) -> socket.socket:
    return socket.socket(family, socket.SOCK_DGRAM)


class _Once:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = False
        self.value: Any = None


class Context:
    def __init__(self, options: Options | None = None, *, fs: FileSystem | None = None,
                 env: Mapping[str, str] | None = None,
                 run: Callable[[Sequence[str], float], CommandResult] = run_command,
                 which: Callable[[str], str | None] = shutil.which,
                 getaddrinfo: Callable[..., list] = socket.getaddrinfo,
                 udp_socket: Callable[..., Any] = _udp_socket,
                 tcp_connect: Callable[..., Any] = socket.create_connection,
                 tls_context: Callable[[], Any] = tls_client_context,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_time: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 parallel: Callable[..., list] = parallel_map,
                 redactor: Redactor | None = None):
        self.options = options or Options()
        self.fs = fs or FileSystem()
        self.env = os.environ if env is None else env
        self.run = run
        self.which = which
        self.getaddrinfo = getaddrinfo
        self.udp_socket = udp_socket
        self.tcp_connect = tcp_connect
        self.tls_context = tls_context
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.sleep = sleep
        self.parallel = parallel
        self.redactor = redactor or Redactor()
        self._memo: dict[str, _Once] = {}
        self._memo_lock = threading.Lock()

    def command(self, argv: Sequence[str], timeout: float = 5.0) -> CommandResult:
        """Run a tool found on PATH; a missing tool is a result, not an exception.

        Inside a check the time limit is cut to what is left of the check's own, and nothing
        new starts once the runner has given up on the check.
        """
        check_cancelled()
        path = self.which(argv[0])
        if not path:
            return CommandResult(tuple(argv), error="not-found")
        result = self.run((path, *argv[1:]), time_left(timeout))
        check_cancelled()  # a command cut short by the check's deadline has no result to report
        return result

    def once(self, key: str, producer: Callable[[], Any]) -> Any:
        """Share one probe between checks that run in parallel."""
        with self._memo_lock:
            entry = self._memo.setdefault(key, _Once())
        with entry.lock:
            if not entry.done:
                entry.value = producer()
                entry.done = True
        return entry.value

    def secret(self, value: str | None) -> None:
        """Register a value (SSID, search domain) that must never reach the output."""
        if value:
            self.redactor.add_secret(value)

    def trust(self, value: str) -> str:
        """Vouch for a model name or version so its long tokens are not masked as serial numbers.

        Only for values that describe a product, never a unit or a person: see `Redactor.trust`.
        """
        self.redactor.trust(value)
        return value
