"""Persistent user settings, library pins, login autostart and theme colors."""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import tomllib
from pathlib import Path

from . import APP_ID, LEGACY_APP_IDS

SETTINGS_VERSION = 1
SETTINGS_MAX_BYTES = 1024 * 1024
SETTINGS_LOCK_RETRY_S = 0.05
SETTINGS_LOCK_TIMEOUT_S = 1.5
AUTOSTART_MARKER = "X-Omarchy-Game-Mode-Owned=true"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LOGGER = logging.getLogger(__name__)
HOME = Path.home()


def config_home(env=os.environ) -> Path:
    return Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME", "~")).expanduser() / ".config")


def state_home(env=os.environ) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path(env.get("HOME", "~")).expanduser() / ".local/state")


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp.exists():
            temp.unlink()


class SettingsFileTooLarge(ValueError):
    pass


class SettingsFileNotRegular(ValueError):
    pass


class SettingsLockTimeout(BlockingIOError):
    pass


def _lock_settings(descriptor: int):
    deadline = time.monotonic() + SETTINGS_LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SettingsLockTimeout("settings save lock timed out after 1.5 seconds") from error
            time.sleep(min(SETTINGS_LOCK_RETRY_S, remaining))


def _read_settings_document(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SettingsFileNotRegular(f"{path} is not a regular file")
        if metadata.st_size > SETTINGS_MAX_BYTES:
            raise SettingsFileTooLarge(f"settings file exceeds {SETTINGS_MAX_BYTES} bytes")
        raw = b""
        while len(raw) <= SETTINGS_MAX_BYTES:
            chunk = os.read(descriptor, min(65536, SETTINGS_MAX_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    if len(raw) > SETTINGS_MAX_BYTES:
        raise SettingsFileTooLarge(f"settings file exceeds {SETTINGS_MAX_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeError, ValueError) as error:
        raise ValueError(str(error)) from error
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    return data


class Settings:
    """``settings.json``: library pins, UI language and mapper opt-in."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_home() / "omarchy-game-mode" / "settings.json"
        self.language = "system"
        self.library: list[str] | None = None
        self.library_present = False
        self.mapper_managed = False
        self._keyboard_args = None
        self._keyboard_args_changed = False
        self.keyboard_args_present = False
        self.legacy_keyboard_command_present = False
        self.load_error: str | None = None
        self.load_failed = False
        self.save_error: str | None = None
        self._reported_save_errors: set[str] = set()
        self._original = {}
        self._document_valid = False
        self.load()

    @property
    def keyboard_args(self):
        return self._keyboard_args

    @keyboard_args.setter
    def keyboard_args(self, value):
        self._keyboard_args = value
        self._keyboard_args_changed = True

    def load(self):
        self.load_error = None
        self.load_failed = False
        try:
            data = _read_settings_document(self.path)
        except FileNotFoundError:
            self._document_valid = False
            self._record_original()
            return
        except (OSError, RecursionError, UnicodeError, ValueError) as error:
            self.load_error = self.sanitize_error(error)
            self.load_failed = True
            self._document_valid = False
            self._record_original()
            return
        disk_version = data.get("version")
        if (
            isinstance(disk_version, int)
            and not isinstance(disk_version, bool)
            and disk_version > SETTINGS_VERSION
        ):
            self.load_error = (
                f"settings version {disk_version} is newer than supported version {SETTINGS_VERSION}"
            )
            self.load_failed = True
            self._document_valid = False
            self._record_original()
            return
        self._document_valid = True
        language = data.get("language")
        valid_language = language == "system" or (
            isinstance(language, str) and re.match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$", language)
        )
        if valid_language:
            self.language = language
        self.mapper_managed = data.get("manageAntimicrox") is True
        self.keyboard_args_present = "keyboardArgs" in data
        self._keyboard_args = data.get("keyboardArgs")
        self.legacy_keyboard_command_present = "keyboardCommand" in data
        library = data.get("library")
        self.library_present = "library" in data
        if isinstance(library, list):
            strings = (i for i in library if isinstance(i, str) and re.match(r"^[a-z0-9-]{1,48}$", i))
            self.library = list(dict.fromkeys(strings))
        self._record_original()

    def _record_original(self):
        self._original = {
            "language": self.language,
            "manageAntimicrox": self.mapper_managed,
            "library": copy.deepcopy(self.library),
            "library_present": self.library_present,
            "keyboard_args_present": self.keyboard_args_present,
        }

    def _refuse_save(self, reason: str, message: str) -> bool:
        self.save_error = self.sanitize_error(message)
        if reason not in self._reported_save_errors:
            LOGGER.warning("%s; refusing to save settings", self.save_error)
            self._reported_save_errors.add(reason)
        return False

    @staticmethod
    def sanitize_error(message) -> str:
        text = " ".join(str(message or "unknown error").splitlines())
        home = str(HOME)
        if home:
            text = text.replace(home, "~")
        return text[:300]

    def save(self):
        self.save_error = None
        if self.load_failed:
            return self._refuse_save(
                "load-failed",
                f"settings could not be loaded: {self.sanitize_error(self.load_error)}",
            )
        target = Path(os.path.realpath(self.path))
        lock_path = target.with_name(f".{target.name}.lock")
        descriptor = -1
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            _lock_settings(descriptor)
            document_valid = False
            try:
                data = _read_settings_document(target)
                document_valid = True
            except FileNotFoundError:
                data = {}
            except (OSError, RecursionError, UnicodeError, ValueError) as error:
                return self._refuse_save(
                    f"read:{type(error).__name__}",
                    f"settings could not be read safely: {error}",
                )
            disk_version = data.get("version")
            if (
                isinstance(disk_version, int)
                and not isinstance(disk_version, bool)
                and disk_version > SETTINGS_VERSION
            ):
                message = f"settings version {disk_version} is newer than supported version {SETTINGS_VERSION}"
                return self._refuse_save("future-version", message)
            data["version"] = SETTINGS_VERSION
            if not document_valid or self.language != self._original["language"]:
                data["language"] = self.language
            if not document_valid or self.mapper_managed != self._original["manageAntimicrox"]:
                data["manageAntimicrox"] = self.mapper_managed
            library_changed = (
                self.library != self._original["library"]
                or self.library_present != self._original["library_present"]
            )
            if not document_valid or library_changed:
                if self.library is not None:
                    data["library"] = self.library
                else:
                    data.pop("library", None)
            args_changed = (
                self._keyboard_args_changed
                or self.keyboard_args_present != self._original["keyboard_args_present"]
            )
            if args_changed:
                if self.keyboard_args_present or self.keyboard_args is not None:
                    data["keyboardArgs"] = self.keyboard_args
                else:
                    data.pop("keyboardArgs", None)
            text = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
            if len(text.encode("utf-8")) > SETTINGS_MAX_BYTES:
                return self._refuse_save(
                    "serialized-too-large",
                    f"serialized settings exceeds {SETTINGS_MAX_BYTES} bytes",
                )
            atomic_write(target, text)
        except SettingsLockTimeout as error:
            return self._refuse_save("lock-timeout", str(error))
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError) as error:
            return self._refuse_save(f"write:{type(error).__name__}", str(error))
        finally:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)
        self._document_valid = True
        self._keyboard_args_changed = False
        self._record_original()
        return True

    def pinned(self, catalog) -> list[str]:
        known = set(catalog.ids)
        if self.library is None:
            return [app.id for app in catalog.apps if app.default_pinned]
        return [i for i in self.library if i in known]

    def set_pinned(self, catalog, app_id: str, pinned: bool):
        previous = self.library
        current = self.pinned(catalog)
        if pinned and app_id not in current:
            current.append(app_id)
        elif not pinned and app_id in current:
            current.remove(app_id)
        # Keep pins of ids unknown to this catalog version untouched.
        unknown = [i for i in (self.library or []) if i not in set(catalog.ids)]
        self.library = current + unknown
        if not self.save():
            self.library = previous
            return False
        return True


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
