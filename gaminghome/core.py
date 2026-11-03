"""Pure state machines for Gaming Home (no GTK, no IO).

Everything here is driven by explicit events so it can be unit tested:
window snapshots from Hyprland, Home visibility, launch results, and raw
controller events. The model returns *effects* that the application layer
executes (show/hide Home, focus a window, set the mapper mode, ...).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from . import APP_ID
from .catalog import Catalog, LINEAGE_LAUNCHERS, launch_target_matches, window_kind_for, RELAUNCH_WHEN_RUNNING

# evdev constants (linux/input-event-codes.h)
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT, SYN_DROPPED = 0, 3
BTN_SOUTH, BTN_EAST = 304, 305
BTN_TL, BTN_TR = 310, 311
BTN_SELECT, BTN_START, BTN_MODE = 314, 315, 316
ABS_X, ABS_Y, ABS_HAT0X, ABS_HAT0Y = 0, 1, 16, 17

GAME, DESKTOP, AUTO = "game", "desktop", "auto"
MODES = (AUTO, GAME, DESKTOP)

KIND_HOME, KIND_GAME, KIND_LAUNCHER, KIND_DESKTOP = "home", "game", "launcher", "desktop"

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")

# Controller actions
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
ACTIVATE, BACK, MENU, HOME = "activate", "back", "menu", "home"
PREV_TAB, NEXT_TAB = "prev_tab", "next_tab"


@dataclass(frozen=True)
class Window:
    address: str
    wm_class: str = ""
    title: str = ""
    initial_class: str = ""
    content_type: str = ""
    pid: int = 0
    focus_history: int = 1_000_000
    fullscreen: bool = False

    @classmethod
    def from_hypr(cls, data) -> "Window | None":
        if not isinstance(data, dict) or data.get("mapped") is False or data.get("hidden") is True:
            return None
        address = str(data.get("address") or "")
        if not ADDRESS_RE.match(address) or int(address, 16) == 0:
            return None
        history = data.get("focusHistoryID")
        return cls(
            address=address,
            wm_class=str(data.get("class") or ""),
            title=str(data.get("title") or ""),
            initial_class=str(data.get("initialClass") or ""),
            content_type=str(data.get("contentType") or ""),
            pid=int(data.get("pid") or 0),
            focus_history=history if isinstance(history, int) and history >= 0 else 1_000_000,
            fullscreen=_is_fullscreen(data.get("fullscreen")),
        )


def _is_fullscreen(value) -> bool:
    # Older Hyprland reports a bool; newer an int mode (1 = maximized, 2 = fullscreen).
    if isinstance(value, bool):
        return value
    return isinstance(value, int) and value >= 2


def _proc_stat_fields(pid: int, root: str) -> list[bytes]:
    """Fields after the command name in /proc/<pid>/stat (only the stat line)."""
    try:
        with open(f"{root}/{int(pid)}/stat", "rb") as handle:
            data = handle.read(1024)
    except (OSError, ValueError):
        return []
    # The command name may contain spaces/parentheses; fields follow the last ')'.
    return data[data.rfind(b")") + 2:].split()


def proc_parent(pid: int, root: str = "/proc") -> int | None:
    """Parent pid (stat field 4)."""
    try:
        return int(_proc_stat_fields(pid, root)[1])
    except (IndexError, ValueError):
        return None


def proc_start(pid: int, root: str = "/proc") -> int | None:
    """Process start time (stat field 22); None when the process is gone."""
    try:
        return int(_proc_stat_fields(pid, root)[19])
    except (IndexError, ValueError):
        return None


# Fullscreen windows of these classes are ordinary desktop use (video,
# browsing, terminals, documents), never assumed to be games.
DESKTOP_CLASSES = frozenset({
    "alacritty", "kitty", "foot", "footclient", "com.mitchellh.ghostty", "ghostty", "org.wezfurlong.wezterm",
    "xterm", "org.gnome.console", "org.gnome.ptyxis", "konsole",
    "chromium", "google-chrome", "brave-browser", "firefox", "librewolf", "zen", "vivaldi-stable",
    "microsoft-edge", "org.qutebrowser.qutebrowser",
    "org.gnome.nautilus", "thunar", "org.kde.dolphin", "pcmanfm",
    "mpv", "vlc", "org.gnome.totem", "celluloid", "io.github.celluloid_player.celluloid", "imv",
    "org.pwmt.zathura", "org.gnome.evince", "org.gnome.papers", "typora", "obsidian", "code", "code-oss",
    "cursor", "dev.zed.zed", "libreoffice", "signal", "discord", "spotify", "org.gnome.calculator",
})
DESKTOP_CLASS_PREFIXES = ("chrome-", "brave-", "msedge-", "libreoffice-", "org.libreoffice.")
LINEAGE_MAX_DEPTH = 24
LINEAGE_MAX_TRACKED = 64


class Classifier:
    """Maps windows to (service, kind) using the catalog plus generic rules."""

    def __init__(self, catalog: Catalog, parent_of: Callable[[int], int | None] = proc_parent,
                 start_of: Callable[[int], int | None] = proc_start):
        self.catalog = catalog
        self.parent_of = parent_of
        self.start_of = start_of
        # pid -> (service, start time). Both maps live as long as the process
        # does (start time guards against pid reuse), not as long as a window:
        # a launcher may close its window, or exit, while its game keeps running.
        self.launcher_pids: dict[int, tuple[str, int | None]] = {}
        self.child_pids: dict[int, tuple[str, int | None]] = {}
        self._ancestors: dict[int, tuple[int, ...]] = {}

    def _alive(self, pid: int, start: int | None) -> bool:
        current = self.start_of(pid)
        return current is not None and (start is None or current == start)

    def _prune(self, pids: dict[int, tuple[str, int | None]]) -> dict[int, tuple[str, int | None]]:
        live = {pid: entry for pid, entry in pids.items() if self._alive(pid, entry[1])}
        while len(live) > LINEAGE_MAX_TRACKED:
            live.pop(next(iter(live)))
        return live

    def set_windows(self, windows):
        """Remember launcher window pids for the lineage fallback."""
        self._ancestors = {}
        launchers = self._prune(self.launcher_pids)
        self.child_pids = self._prune(self.child_pids)
        for window in windows:
            app = self.catalog.match_window(window.wm_class, window.initial_class, window.title)
            if app and app.id in LINEAGE_LAUNCHERS and window.pid > 1:
                launchers[window.pid] = (app.id, self.start_of(window.pid))
        self.launcher_pids = launchers

    def _ancestry(self, pid: int) -> tuple[int, ...]:
        if pid not in self._ancestors:
            chain, current = [], pid
            for _ in range(LINEAGE_MAX_DEPTH):
                current = self.parent_of(current)
                if not current or current <= 1 or current in chain:
                    break
                chain.append(current)
            self._ancestors[pid] = tuple(chain)
        return self._ancestors[pid]

    @staticmethod
    def is_desktop_class(window: Window) -> bool:
        for name in (window.wm_class.lower(), window.initial_class.lower()):
            if name in DESKTOP_CLASSES or name.startswith(DESKTOP_CLASS_PREFIXES):
                return True
        return False

    def service(self, window: Window) -> str | None:
        if window.wm_class.lower() == APP_ID.lower():
            return "home"
        app = self.catalog.match_window(window.wm_class, window.initial_class, window.title)
        if app:
            return app.id
        if "gamescope" in (window.wm_class.lower(), window.initial_class.lower()):
            return "gamescope"
        if window.content_type == "game":
            return "game"
        # Unknown native/Wine games rarely set contentType: treat fullscreen
        # windows (except known desktop apps) and children of a supported
        # launcher (Heroic, Lutris, ...) as games. Manual mode covers the rest.
        if self.is_desktop_class(window):
            return None
        if window.fullscreen:
            return "game"
        if window.pid > 1 and window.pid not in self.launcher_pids:
            if window.pid in self.child_pids:
                return "game"
            for pid in self._ancestry(window.pid):
                if pid in self.launcher_pids:
                    self.child_pids[window.pid] = (self.launcher_pids[pid][0], self.start_of(window.pid))
                    return "game"
        return None

    def kind(self, window: Window) -> str:
        service = self.service(window)
        if service == "home":
            return KIND_HOME
        if service in ("gamescope", "game"):
            return KIND_GAME
        if service is None:
            return KIND_DESKTOP
        return window_kind_for(self.catalog.get(service), window.title)

    def is_launch_target(self, service: str, window: Window) -> bool:
        app = self.catalog.get(service)
        if not app or self.service(window) != service:
            return False
        return launch_target_matches(app, window.wm_class, window.initial_class, window.title)


@dataclass
class PendingLaunch:
    service: str
    token: int
    started: float
    known: frozenset
    handoff: bool = True


class HomeModel:
    """Focus, session, launch and controller-mode policy."""

    def __init__(self, classifier: Classifier, clock: Callable[[], float] = time.monotonic):
        self.classifier = classifier
        self.clock = clock
        self.windows: dict[str, Window] = {}
        self.active: Window | None = None
        self.home_visible = False
        self.home_active = False
        self.ipc_connected = False
        self.override: tuple[str, str] | None = None
        self.target_address: str | None = None
        self.desktop_address: str | None = None
        self.game_address: str | None = None
        self.session_service: str | None = None
        self.pending: PendingLaunch | None = None
        self.requested_mode: str | None = None
        self.message: tuple[str, str, dict] | None = None
        self._token = 0

    # ---- helpers -------------------------------------------------------
    def kind(self, window: Window | None) -> str | None:
        return self.classifier.kind(window) if window else None

    def service(self, window: Window | None) -> str | None:
        return self.classifier.service(window) if window else None

    def _notify(self, level: str, key: str, **params):
        self.message = (level, key, params)
        return [("notify", level, key, params)]

    def _session_window(self, service: str | None) -> Window | None:
        if not service:
            return None
        candidates = [
            w for w in self.windows.values()
            if self.service(w) == service and self.kind(w) in (KIND_GAME, KIND_LAUNCHER)
        ]
        return min(candidates, key=lambda w: w.focus_history, default=None)

    def _valid(self, address: str | None) -> bool:
        return bool(address) and address in self.windows

    @property
    def home_focused(self) -> bool:
        if not self.home_visible:
            return False
        return self.home_active or self.kind(self.active) == KIND_HOME

    # ---- mode policy ---------------------------------------------------
    def desired_mode(self) -> str | None:
        if not self.ipc_connected:
            return DESKTOP
        if self.home_focused:
            return GAME
        active = self.active
        if active is None or self.kind(active) == KIND_HOME:
            return None
        if self.override and self.override[0] == active.address:
            return self.override[1]
        return GAME if self.kind(active) == KIND_GAME else DESKTOP

    def _recompute_mode(self):
        desired = self.desired_mode()
        if desired is None or desired == self.requested_mode:
            return []
        self.requested_mode = desired
        return [("mapper", desired)]

    def target_window(self) -> Window | None:
        if self.active is not None and self.kind(self.active) != KIND_HOME:
            return self.active
        return self.windows.get(self.target_address) if self.target_address else None

    def target_mode_choice(self) -> str:
        target = self.target_window()
        if target and self.override and self.override[0] == target.address:
            return self.override[1]
        return AUTO

    def target_effective_mode(self) -> str | None:
        target = self.target_window()
        if target is None:
            return None
        choice = self.target_mode_choice()
        if choice != AUTO:
            return choice
        return GAME if self.kind(target) == KIND_GAME else DESKTOP

    def set_manual_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError(mode)
        if mode == AUTO:
            self.override = None
            return self._recompute_mode()
        target = self.target_window()
        if target is None:
            return self._notify("info", "status.noTarget")
        self.override = (target.address, mode)
        return self._recompute_mode()

    # ---- window events -------------------------------------------------
    def update_windows(self, windows: list[Window], active: Window | None):
        previous_active = self.active.address if self.active else None
        self.windows = {w.address: w for w in windows}
        if active is not None:
            self.windows.setdefault(active.address, active)
        self.active = active
        self.classifier.set_windows(self.windows.values())

        if active is not None and self.kind(active) != KIND_HOME:
            # A real focus change drops a manual override for another window;
            # layers (menus, launchers) report no active window and keep it.
            if self.override and self.override[0] != active.address:
                self.override = None
            self.target_address = active.address
            kind = self.kind(active)
            if kind == KIND_DESKTOP:
                self.desktop_address = active.address
            else:
                self.game_address = active.address
                self.session_service = self.service(active)

        if self.override and not self._valid(self.override[0]):
            self.override = None
        if not self._valid(self.target_address):
            self.target_address = None
        if not self._valid(self.desktop_address):
            self.desktop_address = None

        effects = self._match_pending()
        effects += self._check_session(previous_active)
        effects += self._recompute_mode()
        return effects

    def _match_pending(self):
        pending = self.pending
        if pending is None:
            return []
        matches = [w for w in self.windows.values() if self.classifier.is_launch_target(pending.service, w)]
        if not matches:
            return []
        fresh = [w for w in matches if w.address not in pending.known]
        window = min(fresh or matches, key=lambda w: w.focus_history)
        self.pending = None
        self.game_address = window.address
        self.session_service = pending.service
        self.message = None
        effects = [("cancel_launch_timeout",), ("launched", pending.service)]
        if pending.handoff:
            effects.append(("hide",))
            if not self.active or self.active.address != window.address:
                effects.append(("focus", window.address))
        return effects

    def _check_session(self, previous_active: str | None):
        if self.game_address is None or self._valid(self.game_address):
            return []
        ended, service = self.game_address, self.session_service
        replacement = self._session_window(service)
        if replacement is not None:
            self.game_address = replacement.address
            return []
        self.game_address = None
        self.session_service = None
        if self.home_visible or self.pending:
            return []
        # Return Home only when the user was in the session that just closed
        # (or nothing has focus); never pull focus from someone browsing.
        if previous_active == ended or self.active is None:
            return [("show",)] + self._notify("info", "status.sessionEnded", service=service)
        return []

    def set_home_state(self, visible: bool, active: bool):
        self.home_visible = visible
        self.home_active = visible and active
        return self._recompute_mode()

    # ---- user actions --------------------------------------------------
    def begin_launch(self, service: str):
        existing = self._session_window(service)
        if existing is not None and service not in RELAUNCH_WHEN_RUNNING:
            self.pending = None
            self.game_address = existing.address
            self.session_service = service
            self.message = None
            return [("cancel_launch_timeout",), ("hide",), ("focus", existing.address)]
        self._token += 1
        self.pending = PendingLaunch(service, self._token, self.clock(), frozenset(self.windows))
        self.message = ("progress", "status.starting", {"service": service})
        effects = [("spawn", service, self._token), ("arm_launch_timeout", self._token)]
        return effects + self._match_pending()

    def launch_failed(self, token: int, key: str, **params):
        if self.pending is None or self.pending.token != token:
            return []
        service = self.pending.service
        self.pending = None
        return [("cancel_launch_timeout",), ("show",)] + self._notify("error", key, service=service, **params)

    def launch_exited(self, token: int, success: bool, status: int = 0):
        # Wrappers (uwsm-app, steam) may exit before the real window exists;
        # only a failing exit ends the wait early.
        if success:
            return []
        return self.launch_failed(token, "status.launchFailed", reason=f"exit {status}")

    def launch_timed_out(self, token: int):
        return self.launch_failed(token, "status.launchTimeout")

    def resume(self):
        address = self.game_address if self._valid(self.game_address) else None
        if address is None:
            window = self._session_window(self.session_service)
            address = window.address if window else None
        if address is None:
            return self._notify("info", "hero.noSession")
        self.game_address = address
        return [("hide",), ("focus", address)]

    def can_resume(self) -> bool:
        return self._valid(self.game_address) or self._session_window(self.session_service) is not None

    def desktop(self):
        if self.pending:
            self.pending.handoff = False
        effects = [("hide",)]
        if self._valid(self.desktop_address) and (not self.active or self.active.address != self.desktop_address):
            effects.append(("focus", self.desktop_address))
        self.requested_mode = DESKTOP
        effects.append(("mapper", DESKTOP))
        return effects

    # ---- IPC -------------------------------------------------------------
    def ipc_lost(self):
        self.ipc_connected = False
        self.active = None
        self.windows = {}
        effects = [("reset_input",)]
        if self.pending:
            self.pending = None
            effects.append(("cancel_launch_timeout",))
        self.requested_mode = DESKTOP
        effects += [("mapper", DESKTOP), ("show",)]
        return effects + self._notify("error", "status.ipcLost")

    def ipc_restored(self):
        self.ipc_connected = True
        if self.message and self.message[1] == "status.ipcLost":
            self.message = None
        return []

    def clear_message(self):
        self.message = None

    # ---- status ----------------------------------------------------------
    def describe(self, window: Window | None):
        if window is None:
            return None
        return {
            "address": window.address,
            "class": window.wm_class,
            "title": window.title,
            "kind": self.kind(window),
            "service": self.service(window),
        }

    def home_address(self, pid: int = 0) -> str | None:
        """Address of Home's own window (preferring this process's pid)."""
        homes = [w for w in self.windows.values() if self.service(w) == "home"]
        own = [w for w in homes if pid and w.pid == pid]
        window = (own or homes or [None])[0]
        return window.address if window else None

    def session_window(self) -> Window | None:
        if self._valid(self.game_address):
            return self.windows[self.game_address]
        return self._session_window(self.session_service)


class ControllerInterpreter:
    """Turns raw evdev events into navigation actions.

    * Navigation (D-pad / left stick) emits on the edge and repeats only
      while a direction is actively held; timers exist only during a hold.
    * A/B/LB/RB fire on press, Menu fires on release when not part of a chord.
    * View+Menu held together for ``chord_ms`` emits HOME once per hold.
    * Guide (BTN_MODE) is ignored entirely.
    * SYN_DROPPED discards state until the next SYN_REPORT, after which the
      caller resyncs held buttons without generating actions.
    """

    STICK_PRESS = 0.55
    STICK_RELEASE = 0.35

    def __init__(self, schedule, cancel, emit, chord_ms=800, repeat_delay_ms=380, repeat_interval_ms=120, on_resync=None):
        self.schedule = schedule
        self.cancel = cancel
        self.emit = emit
        self.on_resync = on_resync
        self.chord_ms = chord_ms
        self.repeat_delay_ms = repeat_delay_ms
        self.repeat_interval_ms = repeat_interval_ms
        self.nav_enabled = False
        self.devices: dict[str, _DeviceState] = {}

    def add_device(self, device_id: str):
        self.devices.setdefault(device_id, _DeviceState())

    def remove_device(self, device_id: str):
        state = self.devices.pop(device_id, None)
        if state:
            self._clear(state)

    def reset(self):
        for state in self.devices.values():
            self._clear(state)

    def set_range(self, device_id: str, code: int, minimum: int, maximum: int, flat: int = 0):
        """Use the kernel's EVIOCGABS range: sticks may be 0..65535, not signed."""
        if maximum > minimum:
            self.devices.setdefault(device_id, _DeviceState()).ranges[code] = (minimum, maximum, max(0, flat))

    def set_nav_enabled(self, enabled: bool):
        if enabled == self.nav_enabled:
            return
        self.nav_enabled = enabled
        for device_id, state in list(self.devices.items()):
            self._cancel_repeat(state)
            state.menu_candidate = False
            if not enabled or state.dropping:
                continue
            # Adopt the physical state now (held buttons stay consumed, so a
            # stale A never activates); a direction still held resumes
            # repeating after the normal delay instead of stalling.
            if self.on_resync:
                self.on_resync(device_id)
                state = self.devices.get(device_id)
            if state is not None and state.direction is not None:
                direction = state.direction
                state.repeat_token = self.schedule(self.repeat_delay_ms, lambda d=device_id, dr=direction: self._repeat(d, dr))

    def held(self) -> bool:
        return any(s.pressed or s.direction for s in self.devices.values())

    def sync_state(self, device_id: str, pressed: set[int], axes: dict[int, int]):
        """Adopt the kernel's current state without emitting actions."""
        state = self.devices.setdefault(device_id, _DeviceState())
        self._clear(state)
        state.pressed = set(pressed) & {BTN_SOUTH, BTN_EAST, BTN_TL, BTN_TR, BTN_SELECT, BTN_START, BTN_MODE}
        state.chord_consumed = {BTN_SELECT, BTN_START} <= state.pressed
        for code, value in axes.items():
            self._set_axis(state, code, value)
        state.direction = self._direction(state)

    def feed(self, device_id: str, etype: int, code: int, value: int):
        state = self.devices.setdefault(device_id, _DeviceState())
        if etype == EV_SYN:
            if code == SYN_DROPPED:
                self._clear(state)
                state.dropping = True
            elif code == SYN_REPORT and state.dropping:
                state.dropping = False
                if self.on_resync:
                    self.on_resync(device_id)
            return
        if state.dropping:
            return
        if etype == EV_KEY:
            self._key(device_id, state, code, value)
        elif etype == EV_ABS and code in (ABS_X, ABS_Y, ABS_HAT0X, ABS_HAT0Y):
            self._set_axis(state, code, value)
            self._update_direction(device_id, state)

    # ---- buttons --------------------------------------------------------
    def _key(self, device_id, state, code, value):
        if value == 2:
            return
        pressed = bool(value)
        if pressed:
            if code in state.pressed:
                return
            state.pressed.add(code)
        else:
            if code not in state.pressed:
                return
            state.pressed.discard(code)
        if code == BTN_MODE:
            return
        if code in (BTN_SELECT, BTN_START):
            self._chord(device_id, state, pressed)
        if code == BTN_START:
            if pressed:
                state.menu_candidate = BTN_SELECT not in state.pressed
            elif state.menu_candidate:
                state.menu_candidate = False
                self._nav(MENU)
            return
        if not pressed:
            return
        action = {BTN_SOUTH: ACTIVATE, BTN_EAST: BACK, BTN_TL: PREV_TAB, BTN_TR: NEXT_TAB}.get(code)
        if action:
            self._nav(action)

    def _chord(self, device_id, state, pressed):
        both = {BTN_SELECT, BTN_START} <= state.pressed
        if both:
            state.menu_candidate = False
            if pressed and not state.chord_consumed and state.chord_token is None:
                state.chord_token = self.schedule(self.chord_ms, lambda: self._chord_fire(device_id))
            return
        self._cancel_chord(state)
        if not ({BTN_SELECT, BTN_START} & state.pressed):
            state.chord_consumed = False

    def _chord_fire(self, device_id):
        state = self.devices.get(device_id)
        if state is None:
            return
        state.chord_token = None
        if {BTN_SELECT, BTN_START} <= state.pressed and not state.chord_consumed:
            state.chord_consumed = True
            self.emit(HOME)

    def _cancel_chord(self, state):
        if state.chord_token is not None:
            self.cancel(state.chord_token)
            state.chord_token = None

    # ---- directions ----------------------------------------------------
    def _set_axis(self, state, code, value):
        if code == ABS_HAT0X:
            state.hat[0] = (value > 0) - (value < 0)
        elif code == ABS_HAT0Y:
            state.hat[1] = (value > 0) - (value < 0)
        else:
            index = 0 if code == ABS_X else 1
            low, high, flat = state.ranges.get(code, (-32768, 32767, 0))
            center = (low + high) / 2
            half = (high - low) / 2 or 1
            offset = value - center
            norm = 0.0 if abs(offset) <= flat else offset / half
            if abs(norm) >= self.STICK_PRESS:
                state.stick[index] = 1 if norm > 0 else -1
            elif abs(norm) <= self.STICK_RELEASE:
                state.stick[index] = 0

    @staticmethod
    def _direction(state):
        for x, y in (state.hat, state.stick):
            if y:
                return UP if y < 0 else DOWN
            if x:
                return LEFT if x < 0 else RIGHT
        return None

    def _update_direction(self, device_id, state):
        direction = self._direction(state)
        if direction == state.direction:
            return
        state.direction = direction
        self._cancel_repeat(state)
        if direction is None or not self.nav_enabled:
            return
        self.emit(direction)
        state.repeat_token = self.schedule(self.repeat_delay_ms, lambda: self._repeat(device_id, direction))

    def _repeat(self, device_id, direction):
        state = self.devices.get(device_id)
        if state is None:
            return
        state.repeat_token = None
        if state.direction != direction or not self.nav_enabled:
            return
        self.emit(direction)
        state.repeat_token = self.schedule(self.repeat_interval_ms, lambda: self._repeat(device_id, direction))

    def _cancel_repeat(self, state):
        if state.repeat_token is not None:
            self.cancel(state.repeat_token)
            state.repeat_token = None

    def _nav(self, action):
        if self.nav_enabled:
            self.emit(action)

    def _clear(self, state):
        self._cancel_chord(state)
        self._cancel_repeat(state)
        state.pressed.clear()
        state.hat = [0, 0]
        state.stick = [0, 0]
        state.direction = None
        state.menu_candidate = False
        state.chord_consumed = False
        state.dropping = False


class _DeviceState:
    def __init__(self):
        self.pressed: set[int] = set()
        self.dropping = False
        self.hat = [0, 0]
        self.stick = [0, 0]
        self.ranges: dict[int, tuple[int, int]] = {}
        self.direction: str | None = None
        self.repeat_token = None
        self.chord_token = None
        self.chord_consumed = False
        self.menu_candidate = False
