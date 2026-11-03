"""Hyprland IPC: event socket (socket2) watched by GLib, requests on demand.

Window state is only re-read when Hyprland reports a relevant event; there
is no polling. Requests use the command socket directly (the same protocol
``hyprctl -j`` speaks) so no processes are spawned per event.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from gi.repository import Gio, GLib

from .core import ADDRESS_RE

try:
    from gi.repository import GLibUnix

    _fd_add = GLibUnix.fd_add_full
except (ImportError, AttributeError):
    _fd_add = GLib.unix_fd_add_full

WINDOW_EVENTS = frozenset({
    "activewindowv2", "openwindow", "closewindow", "windowtitlev2",
    "movewindowv2", "fullscreen", "changefloatingmode", "workspacev2",
})
RECONNECT_DELAYS = (1, 2, 4, 8, 15, 30, 60)


def socket_dir(env=os.environ) -> Path | None:
    signature = env.get("HYPRLAND_INSTANCE_SIGNATURE")
    runtime = env.get("XDG_RUNTIME_DIR")
    if not signature or not runtime or "/" in signature:
        return None
    return Path(runtime) / "hypr" / signature


def parse_event(line: str) -> tuple[str, str] | None:
    name, sep, data = line.partition(">>")
    if not sep or not name:
        return None
    return name, data


def focus_dispatch(address: str) -> tuple[str, str]:
    if not ADDRESS_RE.match(address):
        raise ValueError(f"invalid window address {address!r}")
    return f'dispatch hl.dsp.focus({{ window = "address:{address}" }})', f"dispatch focuswindow address:{address}"


class HyprlandIPC:
    def __init__(self, on_windows_changed, on_connection_changed, env=os.environ):
        self.on_windows_changed = on_windows_changed
        self.on_connection_changed = on_connection_changed
        self.dir = socket_dir(env)
        self.connected = False
        self.error: str | None = None if self.dir else "not running under Hyprland"
        self._sock = None
        self._source = None
        self._buffer = b""
        self._refresh_source = None
        self._retry_source = None
        self._retry_index = 0
        self._monitor = None

    # ---- event socket ----------------------------------------------------
    def start(self) -> bool:
        if self.dir is None:
            return False
        if self._monitor is None and self.dir.parent.is_dir():
            try:
                self._monitor = Gio.File.new_for_path(str(self.dir)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
                self._monitor.connect("changed", self._on_dir_changed)
            except GLib.Error:
                self._monitor = None
        return self._connect()

    def _connect(self) -> bool:
        if self.dir is None:
            return False
        path = self.dir / ".socket2.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            sock.connect(str(path))
        except OSError as error:
            sock.close()
            self.error = f"event socket: {error.strerror or error}"
            return False
        sock.setblocking(False)
        self._sock = sock
        self._buffer = b""
        condition = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        self._source = _fd_add(GLib.PRIORITY_DEFAULT, sock.fileno(), condition, self._on_readable, None)
        self.connected = True
        self.error = None
        self._retry_index = 0
        self._cancel_retry()
        self.on_connection_changed(True)
        self.schedule_refresh()
        return True

    def stop(self):
        self._cancel_retry()
        if self._monitor:
            self._monitor.cancel()
            self._monitor = None
        self._disconnect(notify=False)

    def _disconnect(self, notify=True, source_done=False):
        if self._source is not None and not source_done:
            GLib.source_remove(self._source)
        self._source = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        was_connected = self.connected
        self.connected = False
        if notify and was_connected:
            self.on_connection_changed(False)

    def _on_readable(self, _fd, condition, _data):
        while True:
            try:
                chunk = self._sock.recv(65536)
            except BlockingIOError:
                break
            except OSError as error:
                return self._lost(str(error))
            if not chunk:
                return self._lost("Hyprland closed the event socket")
            self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        if any((parse_event(l.decode("utf-8", "replace")) or ("", ""))[0] in WINDOW_EVENTS for l in lines):
            self.schedule_refresh()
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            return self._lost("event socket hung up")
        return True

    def _lost(self, reason):
        self.error = reason
        self._disconnect(source_done=True)
        self._schedule_retry()
        return False

    def _schedule_retry(self):
        if self._retry_source is not None or self._retry_index >= len(RECONNECT_DELAYS):
            return
        delay = RECONNECT_DELAYS[self._retry_index]
        self._retry_index += 1
        self._retry_source = GLib.timeout_add_seconds(delay, self._on_retry)

    def _on_retry(self):
        self._retry_source = None
        if not self.connected and not self._connect():
            self._schedule_retry()
        return False

    def _cancel_retry(self):
        if self._retry_source is not None:
            GLib.source_remove(self._retry_source)
            self._retry_source = None

    def retry_now(self):
        self._retry_index = 0
        self._cancel_retry()
        if not self.connected:
            self.start() or self._schedule_retry()

    def _on_dir_changed(self, _monitor, file, _other, event):
        if self.connected or event != Gio.FileMonitorEvent.CREATED:
            return
        if file.get_basename() == ".socket2.sock":
            self._connect()

    def schedule_refresh(self):
        if self._refresh_source is None:
            self._refresh_source = GLib.idle_add(self._do_refresh)

    def _do_refresh(self):
        self._refresh_source = None
        self.on_windows_changed()
        return False

    # ---- requests -------------------------------------------------------
    def request(self, command: str, timeout=1.0) -> str:
        if self.dir is None:
            raise OSError("not running under Hyprland")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.dir / ".socket.sock"))
            sock.sendall(command.encode())
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def snapshot(self) -> tuple[list, dict | None]:
        clients = json.loads(self.request("j/clients") or "[]")
        active = json.loads(self.request("j/activewindow") or "{}")
        return (clients if isinstance(clients, list) else []), (active if isinstance(active, dict) else None)

    def focus(self, address: str) -> bool:
        lua, legacy = focus_dispatch(address)
        try:
            if self.request(lua).strip() == "ok":
                return True
            return self.request(legacy).strip() == "ok"
        except OSError:
            return False
