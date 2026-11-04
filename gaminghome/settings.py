"""Persistent user settings, library pins, login autostart and theme colors."""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from . import APP_ID, LEGACY_APP_IDS

SETTINGS_VERSION = 1
AUTOSTART_MARKER = "X-Omarchy-Game-Mode-Owned=true"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def config_home(env=os.environ) -> Path:
    return Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME", "~")).expanduser() / ".config")


def state_home(env=os.environ) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path(env.get("HOME", "~")).expanduser() / ".local/state")


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class Settings:
    """``settings.json``: library pins, UI language and mapper opt-in."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_home() / "omarchy-game-mode" / "settings.json"
        self.language = "system"
        self.library: list[str] | None = None
        self.mapper_managed = False
        self.load_error: str | None = None
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            self.load_error = str(error)
            return
        if not isinstance(data, dict):
            self.load_error = "settings must be an object"
            return
        language = data.get("language")
        if isinstance(language, str) and (language == "system" or re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$", language)):
            self.language = language
        self.mapper_managed = data.get("manageAntimicrox") is True
        library = data.get("library")
        if isinstance(library, list):
            self.library = [i for i in dict.fromkeys(library) if isinstance(i, str) and re.match(r"^[a-z0-9-]{1,48}$", i)]

    def save(self):
        data = {"version": SETTINGS_VERSION, "language": self.language, "manageAntimicrox": self.mapper_managed}
        if self.library is not None:
            data["library"] = self.library
        atomic_write(self.path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def pinned(self, catalog) -> list[str]:
        known = set(catalog.ids)
        if self.library is None:
            return [app.id for app in catalog.apps if app.default_pinned]
        return [i for i in self.library if i in known]

    def set_pinned(self, catalog, app_id: str, pinned: bool):
        current = self.pinned(catalog)
        if pinned and app_id not in current:
            current.append(app_id)
        elif not pinned and app_id in current:
            current.remove(app_id)
        # Keep pins of ids unknown to this catalog version untouched.
        unknown = [i for i in (self.library or []) if i not in set(catalog.ids)]
        self.library = current + unknown
        self.save()


# ---- autostart -------------------------------------------------------------

def autostart_path(env=os.environ) -> Path:
    return config_home(env) / "autostart" / f"{APP_ID}.desktop"


def legacy_autostart_paths(env=os.environ) -> list[Path]:
    return [config_home(env) / "autostart" / f"{app_id}.desktop" for app_id in LEGACY_APP_IDS]


def _owned_autostart(path: Path) -> bool:
    try:
        return AUTOSTART_MARKER in path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False


def _remove_owned_legacy_autostart(env) -> list[Path]:
    removed = [path for path in legacy_autostart_paths(env) if _owned_autostart(path)]
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def launcher_executable(env=os.environ) -> str | None:
    """Absolute path of the installed launcher (never a dev checkout)."""
    exe = env.get("OMARCHY_GAME_MODE_EXEC")
    if exe and os.path.isabs(exe) and os.access(exe, os.X_OK):
        return exe
    found = shutil.which("omarchy-game-mode")
    return os.path.abspath(found) if found else None


def desktop_exec_quote(path: str) -> str:
    if re.match(r"^[A-Za-z0-9/._+-]+$", path):
        return path
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def autostart_entry(executable: str) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Omarchy Game Mode",
        "Comment=Show Omarchy Game Mode at login",
        f"Exec={desktop_exec_quote(executable)} --autostart",
        f"Icon={APP_ID}",
        "Terminal=false",
        "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true",
        AUTOSTART_MARKER,
        "",
    ])


def autostart_enabled(env=os.environ) -> bool:
    return any(_owned_autostart(path) for path in (autostart_path(env), *legacy_autostart_paths(env)))


def set_autostart(enabled: bool, env=os.environ) -> tuple[bool, str]:
    path = autostart_path(env)
    if enabled:
        executable = launcher_executable(env)
        if not executable:
            return False, "omarchy-game-mode is not installed on PATH; install it before enabling login start"
        atomic_write(path, autostart_entry(executable))
        _remove_owned_legacy_autostart(env)
        return True, str(path)
    removed = _remove_owned_legacy_autostart(env)
    if not path.exists():
        return True, ", ".join(map(str, removed)) or "already off"
    try:
        owned = AUTOSTART_MARKER in path.read_text(encoding="utf-8")
    except OSError as error:
        return False, str(error)
    if not owned:
        return False, f"{path} was not created by Omarchy Game Mode; leaving it alone"
    path.unlink()
    return True, str(path)


# ---- theme -------------------------------------------------------------------

FALLBACK_COLORS = {
    "background": "#0d1017",
    "surface": "#151a24",
    "foreground": "#e8ecf4",
    "muted": "#8b93a7",
    "accent": "#6ea8ff",
    "success": "#7ad97a",
    "warning": "#f0c060",
    "danger": "#ff7a85",
}


def theme_dir(env=os.environ) -> Path:
    return state_home(env) / "omarchy" / "current"


def theme_colors(env=os.environ) -> dict:
    """Omarchy theme colors when present, otherwise a portable dark palette.

    The home surface stays dark for a console look; a light theme
    contributes only its accent and status colors.
    """
    colors = dict(FALLBACK_COLORS)
    try:
        with open(theme_dir(env) / "theme" / "colors.toml", "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return colors

    def pick(*names):
        for name in names:
            value = data.get(name)
            if isinstance(value, str) and COLOR_RE.match(value):
                return value
        return None

    for key, names in (("accent", ("accent", "blue")), ("success", ("green",)), ("warning", ("yellow",)), ("danger", ("red",))):
        value = pick(*names)
        if value:
            colors[key] = value
    if data.get("mode", "dark") != "light":
        for key, names in (
            ("background", ("darker_background", "dark_background", "background")),
            ("surface", ("lighter_background", "background")),
            ("foreground", ("bright_foreground", "light_foreground", "foreground")),
            ("muted", ("foreground",)),
        ):
            value = pick(*names)
            if value:
                colors[key] = value
    return colors
