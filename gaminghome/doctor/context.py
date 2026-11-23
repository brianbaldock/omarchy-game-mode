"""Injectable access to the outside world: files, subprocesses, sockets and clocks.

Every check receives a `Context` and touches the system only through it, so tests
replace each primitive with a fake and never need a network, a device or root.
The real primitives here are the only place that spawns processes or opens files:
commands are argv lists (never a shell) with a deadline and a bounded capture, and
file reads are bounded and read-only.
"""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .redact import Redactor
from .runner import parallel_map

READ_LIMIT = 65536
OUTPUT_LIMIT = 262144
LIST_LIMIT = 4096


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

    def read(self, absolute: str, limit: int = READ_LIMIT) -> str | None:
        try:
            with open(self.path(absolute), "rb") as handle:
                return handle.read(limit).decode("utf-8", "replace")
        except OSError:
            return None

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

    def resolved(self, absolute: str) -> str:
        """Symlink-free form of a path, expressed below the root again."""
        real = os.path.realpath(self.path(absolute))
        root = os.path.realpath(self.root)
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return "/" + os.path.relpath(real, root)
        return real


def _command_env(env: Mapping[str, str] | None) -> dict[str, str]:
    merged = dict(os.environ if env is None else env)
    merged.update(LC_ALL="C", LANG="C")
    return merged


def _drain(proc: subprocess.Popen[bytes], deadline: float, limit: int) -> tuple[bytes, bytes, bool]:
    """Collect at most `limit` bytes per stream until both close or the deadline passes."""
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
    if not timed_out:
        try:
            proc.wait(max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
    return bytes(buffers[proc.stdout]), bytes(buffers[proc.stderr]), timed_out


def _reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def run_command(argv: Sequence[str], timeout: float = 5.0, *, env: Mapping[str, str] | None = None,
                limit: int = OUTPUT_LIMIT) -> CommandResult:
    """Run `argv` without a shell, with a deadline and bounded output. Never raises."""
    argv = tuple(argv)
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=_command_env(env), start_new_session=True)
    except FileNotFoundError:
        return CommandResult(argv, error="not-found")
    except OSError as exc:
        return CommandResult(argv, error=f"os-error: {exc.strerror or type(exc).__name__}")
    try:
        out, err, timed_out = _drain(proc, deadline, limit)
    finally:
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
                 tls_context: Callable[[], Any] = ssl.create_default_context,
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
        """Run a tool found on PATH; a missing tool is a result, not an exception."""
        path = self.which(argv[0])
        if not path:
            return CommandResult(tuple(argv), error="not-found")
        return self.run((path, *argv[1:]), timeout)

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
