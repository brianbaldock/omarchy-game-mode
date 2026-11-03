"""App catalog: loading, validation, availability and window matching.

The catalog is data (``data/catalog.json``). Machine values (ids, argv
vectors, availability probes, window matchers) are never translated; user
visible strings are looked up through locale keys.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

CATEGORIES = ("native", "streaming", "retro", "stores")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
# Store installs only run fixed Omarchy commands, in a visible terminal.
INSTALL_PREFIXES = (("omarchy", "install"), ("omarchy", "pkg", "add"), ("omarchy", "pkg", "aur-add"), ("omarchy", "pkg", "aur", "add"))
INSTALL_TOKEN_RE = re.compile(r"^-{0,2}[A-Za-z0-9][A-Za-z0-9._=-]*$")
DESKTOP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

# Launcher UIs that have no controller navigation of their own: they keep
# desktop (pointer) controls while focused. Everything else that matches a
# catalog entry is treated as a game surface that wants raw controller input.
DESKTOP_INPUT_APPS = frozenset({"lutris", "battlenet", "minecraft"})

# Windows whose process descends from one of these launchers count as games.
LINEAGE_LAUNCHERS = frozenset({"steam", "heroic", "lutris", "battlenet", "minecraft"})

# Greenlight's console picker is a launcher; only its stream is a game.
LAUNCHER_UNLESS_TITLE = {"remote": ("streaming",)}

# Launch handoff needs the window the user actually wants, not any helper
# dialog (Steam opens updater/login windows with the same class).
LAUNCH_TITLE_HINTS = {"steam": ("big picture",)}
LAUNCH_PREFIX_HINTS = {"steam": ("steam_app_",)}

# Window titles only identify an app outside general browsers (a tab about
# Moonlight is not Moonlight), except for apps that live in a browser.
BROWSER_TITLE_APPS = frozenset({"cloud"})
BROWSER_HINTS = ("chromium", "chrome", "firefox", "brave", "vivaldi", "opera", "edge", "librewolf", "zen", "helium", "epiphany", "qutebrowser")

MATCH_CLASS, MATCH_PREFIX, MATCH_TITLE = 3, 2, 1

# Apps whose launcher forwards to an already running instance; launching
# again is the supported way to bring them to the requested view.
RELAUNCH_WHEN_RUNNING = frozenset({"steam"})


@dataclass(frozen=True)
class AppEntry:
    id: str
    name: str
    description_key: str
    icon: str
    category: str
    architectures: tuple[str, ...]
    launch: tuple[str, ...]
    install: tuple[str, ...] | None
    availability: tuple[tuple[str, str], ...]
    classes: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    title_contains: tuple[str, ...] = ()
    default_pinned: bool = False
    information_url: str | None = None

    def supported_on(self, arch: str) -> bool:
        return not self.architectures or arch in self.architectures

    def match_level(self, wm_class: str, initial_class: str, title: str) -> int:
        classes = {wm_class.lower(), initial_class.lower()} - {""}
        if classes & set(self.classes):
            return MATCH_CLASS
        if any(c.startswith(p) for c in classes for p in self.prefixes):
            return MATCH_PREFIX
        if self.id not in BROWSER_TITLE_APPS and any(h in c for c in classes for h in BROWSER_HINTS):
            return 0
        lowered = title.lower()
        return MATCH_TITLE if any(fragment in lowered for fragment in self.title_contains) else 0

    def matches(self, wm_class: str, initial_class: str, title: str) -> bool:
        return self.match_level(wm_class, initial_class, title) > 0


class CatalogError(ValueError):
    pass


def machine_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x64"):
        return "x86_64"
    if machine == "arm64":
        return "aarch64"
    return machine


def _strings(value, lower=False) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise CatalogError("expected a list of non-empty strings")
    return tuple(v.lower() if lower else v for v in value)


def _argv(value, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CatalogError(f"{what} must be a non-empty argv list")
    for token in value:
        if not isinstance(token, str) or not token or "\0" in token or "\n" in token:
            raise CatalogError(f"{what} contains an invalid argument")
    return tuple(value)


def _install(value) -> tuple[str, ...] | None:
    if value is None:
        return None
    argv = _argv(value, "install")
    prefix = next((p for p in INSTALL_PREFIXES if argv[:len(p)] == p), None)
    if prefix is None or len(argv) <= len(prefix):
        raise CatalogError("install must be an 'omarchy install' or 'omarchy pkg add|aur-add' command")
    if not all(INSTALL_TOKEN_RE.match(token) for token in argv[len(prefix):]):
        raise CatalogError("install contains an unsafe token")
    return argv


def _availability(value) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not value:
        raise CatalogError("availability must be an object")
    probes = []
    for kind, target in value.items():
        if kind not in ("command", "desktop", "flatpak"):
            raise CatalogError(f"unknown availability probe {kind!r}")
        targets = target if isinstance(target, list) else [target]
        for item in targets:
            if not isinstance(item, str) or not item:
                raise CatalogError("availability target must be a string")
            pattern = COMMAND_RE if kind == "command" else DESKTOP_ID_RE
            if "/" in item or not pattern.match(item):
                raise CatalogError(f"unsafe availability target {item!r}")
            probes.append((kind, item))
    return tuple(probes)


def parse_entry(raw: dict) -> AppEntry:
    if not isinstance(raw, dict):
        raise CatalogError("app entry must be an object")
    app_id = raw.get("id")
    if not isinstance(app_id, str) or not ID_RE.match(app_id):
        raise CatalogError(f"invalid app id {app_id!r}")
    try:
        category = raw.get("category", "native")
        if category not in CATEGORIES:
            raise CatalogError(f"unknown category {category!r}")
        match = raw.get("match") or {}
        if not isinstance(match, dict):
            raise CatalogError("match must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise CatalogError("name is required")
        url = raw.get("informationUrl")
        if url is not None and not (isinstance(url, str) and url.startswith("https://")):
            raise CatalogError("informationUrl must be https")
        return AppEntry(
            id=app_id,
            name=name,
            description_key=str(raw.get("descriptionKey") or f"app.{app_id}.description"),
            icon=str(raw.get("icon") or app_id),
            category=category,
            architectures=_strings(raw.get("architectures")),
            launch=_argv(raw.get("launch"), "launch"),
            install=_install(raw.get("install")),
            availability=_availability(raw.get("availability")),
            classes=_strings(match.get("classes"), lower=True),
            prefixes=_strings(match.get("prefixes"), lower=True),
            title_contains=_strings(match.get("titleContains"), lower=True),
            default_pinned=bool(raw.get("defaultPinned", False)),
            information_url=url,
        )
    except CatalogError as error:
        raise CatalogError(f"{app_id}: {error}") from None


BUILTIN_CATALOG = {
    "schemaVersion": 1,
    "apps": [
        {
            "id": "steam",
            "name": "Steam",
            "descriptionKey": "app.steam.description",
            "icon": "steam",
            "category": "native",
            "architectures": ["x86_64"],
            "launch": ["steam", "-gamepadui"],
            "install": ["omarchy", "install", "gaming", "steam"],
            "availability": {"command": "steam"},
            "match": {"classes": ["steam"], "prefixes": ["steam_app_"]},
            "defaultPinned": True,
        },
        {
            "id": "remote",
            "name": "Xbox Remote Play",
            "descriptionKey": "app.remote.description",
            "icon": "remote",
            "category": "streaming",
            "architectures": ["x86_64", "aarch64"],
            "launch": ["gtk-launch", "greenlight-desktop"],
            "install": None,
            "availability": {"desktop": "greenlight-desktop"},
            "match": {"classes": ["greenlight-desktop", "greenlight"]},
            "defaultPinned": True,
        },
        {
            "id": "cloud",
            "name": "Xbox Cloud Gaming",
            "descriptionKey": "app.cloud.description",
            "icon": "cloud",
            "category": "streaming",
            "architectures": ["x86_64", "aarch64"],
            "launch": ["omarchy", "launch", "webapp", "https://www.xbox.com/en-US/play"],
            "install": None,
            "availability": {"command": "omarchy"},
            "match": {"prefixes": ["chrome-www.xbox.com", "chromium-www.xbox.com"], "titleContains": ["xbox cloud gaming"]},
            "defaultPinned": True,
        },
    ],
}


@dataclass
class Catalog:
    apps: list[AppEntry]
    errors: list[str] = field(default_factory=list)
    source: str = "builtin"

    def get(self, app_id: str) -> AppEntry | None:
        return next((app for app in self.apps if app.id == app_id), None)

    @property
    def ids(self) -> list[str]:
        return [app.id for app in self.apps]

    def match_window(self, wm_class: str, initial_class: str, title: str) -> AppEntry | None:
        """Exact classes beat prefixes beat titles; ties go to catalog order."""
        best, best_level = None, 0
        for app in self.apps:
            level = app.match_level(wm_class, initial_class, title)
            if level > best_level:
                best, best_level = app, level
        return best


def parse_catalog(data, source="inline") -> Catalog:
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise CatalogError("unsupported catalog schemaVersion")
    raw_apps = data.get("apps")
    if not isinstance(raw_apps, list):
        raise CatalogError("apps must be a list")
    apps, errors, seen = [], [], set()
    for raw in raw_apps:
        try:
            entry = parse_entry(raw)
        except CatalogError as error:
            errors.append(str(error))
            continue
        if entry.id in seen:
            errors.append(f"{entry.id}: duplicate id")
            continue
        seen.add(entry.id)
        apps.append(entry)
    return Catalog(apps=apps, errors=errors, source=source)


def load_catalog(path: Path) -> Catalog:
    """Load the data catalog, falling back to the built-in minimal catalog."""
    try:
        with open(path, encoding="utf-8") as handle:
            catalog = parse_catalog(json.load(handle), source=str(path))
        if catalog.apps:
            return catalog
        reason = "catalog has no valid apps"
    except (OSError, ValueError) as error:
        reason = str(error)
    fallback = parse_catalog(BUILTIN_CATALOG, source="builtin")
    fallback.errors.insert(0, f"{path}: {reason}")
    return fallback


def application_dirs(env=os.environ) -> list[Path]:
    home = Path(env.get("HOME", "~")).expanduser()
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local/share")
    data_dirs = [Path(d) for d in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d]
    candidates = [data_home, *data_dirs, data_home / "flatpak/exports/share", Path("/var/lib/flatpak/exports/share")]
    seen, result = set(), []
    for base in candidates:
        directory = base / "applications"
        if directory not in seen:
            seen.add(directory)
            result.append(directory)
    return result


class Availability:
    """Answers whether an app is installed without spawning processes."""

    def __init__(self, which: Callable[[str], str | None] = shutil.which, app_dirs: Iterable[Path] | None = None):
        self.which = which
        self.app_dirs = list(app_dirs) if app_dirs is not None else application_dirs()

    def desktop_file(self, desktop_id: str) -> Path | None:
        name = desktop_id if desktop_id.endswith(".desktop") else f"{desktop_id}.desktop"
        for directory in self.app_dirs:
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None

    def installed(self, app: AppEntry) -> bool:
        for kind, target in app.availability:
            if kind == "command" and self.which(target):
                return True
            if kind in ("desktop", "flatpak") and self.desktop_file(target):
                return True
        return False


def window_kind_for(app: AppEntry | None, title: str) -> str:
    """'game' for raw controller surfaces, 'launcher' for pointer-driven UIs."""
    if app is None:
        return "desktop"
    if app.id in DESKTOP_INPUT_APPS:
        return "launcher"
    needles = LAUNCHER_UNLESS_TITLE.get(app.id)
    if needles and not any(n in title.lower() for n in needles):
        return "launcher"
    return "game"


def launch_target_matches(app: AppEntry, wm_class: str, initial_class: str, title: str) -> bool:
    if not app.matches(wm_class, initial_class, title):
        return False
    titles = LAUNCH_TITLE_HINTS.get(app.id)
    if not titles:
        return True
    classes = (wm_class.lower(), initial_class.lower())
    if any(c.startswith(p) for c in classes for p in LAUNCH_PREFIX_HINTS.get(app.id, ())):
        return True
    return any(t in title.lower() for t in titles)
