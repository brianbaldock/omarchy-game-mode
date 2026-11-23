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
MAX_PATTERN_CHARS = 200
MAX_PATTERN_REPEAT = 64
MAX_PATTERN_WAYS = 2 ** 20
# A counted repeat as re reads one ({n}, {m,n}, {m,} or {,n}); any other "{" is a literal.
COUNTED_REPEAT_RE = re.compile(r"\{([0-9]*)(?:(,)([0-9]*))?\}")

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
    patterns: tuple[re.Pattern, ...] = ()
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
        if any(pattern.fullmatch(c) for c in classes for pattern in self.patterns):
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


def _patterns(value, warnings: list[str]) -> tuple[re.Pattern, ...]:
    """Regular expressions for window classes; each has to match a whole class, in any case.

    A pattern that does not compile, or whose matching could take very long
    (see _check_backtracking), is left out with one warning: the entry still loads.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        warnings.append("patterns ignored: expected a list of strings")
        return ()
    patterns = []
    for index, text in enumerate(value):
        try:
            if not isinstance(text, str) or not text:
                raise CatalogError("expected a non-empty string")
            if len(text) > MAX_PATTERN_CHARS:
                raise CatalogError(f"longer than {MAX_PATTERN_CHARS} characters")
            pattern = re.compile(text, re.IGNORECASE)
            _check_backtracking(text)
            patterns.append(pattern)
        except Exception as error:  # noqa: BLE001 - no pattern may keep the catalog from loading
            reason = error if isinstance(error, CatalogError) else f"{type(error).__name__}: {error}"
            warnings.append(f"patterns[{index}] ignored: {reason}")
    return tuple(patterns)


@dataclass
class _Part:
    """What the backtracking check knows about one part of a pattern."""
    ways: int = 1            # at most this many ways to match: alternatives times repeat counts
    unbounded: bool = False  # holds a repeat without an upper bound
    optional: bool = False   # can match nothing


class _Group:
    """A group of a pattern under check, or the whole pattern."""

    def __init__(self):
        self.closed_ways, self.closed_optional = 0, False  # the alternatives already ended by "|"
        self.ways, self.optional = 1, True                 # the current alternative so far
        self.unbounded = False
        self.open_end = False  # the current one ends with a repeat without an upper bound and optional parts

    def add(self, part: _Part):
        if part.unbounded:
            if self.open_end:
                raise CatalogError("two repeats without an upper bound follow each other")
            self.open_end = True
        elif not part.optional:
            self.open_end = False
        self.ways = min(self.ways * part.ways, MAX_PATTERN_WAYS + 1)
        self.optional = self.optional and part.optional
        self.unbounded = self.unbounded or part.unbounded

    def alternative(self):
        self.closed_ways += self.ways
        self.closed_optional = self.closed_optional or self.optional
        self.ways, self.optional, self.open_end = 1, True, False

    def close(self) -> _Part:
        self.alternative()
        return _Part(ways=self.closed_ways, unbounded=self.unbounded, optional=self.closed_optional)


def _repeat(text: str, i: int) -> tuple[int, int | None, int]:
    """The repeat at text[i] as (minimum, maximum or None, index after it); (1, 1, i) if there is none."""
    symbol = text[i:i + 1]
    if symbol in ("*", "+", "?"):
        low, high = {"*": (0, None), "+": (1, None), "?": (0, 1)}[symbol]
        i += 1
    elif (count := COUNTED_REPEAT_RE.match(text, i)) and count.group() != "{}":
        low = int(count[1] or 0)
        high = (int(count[3]) if count[3] else None) if count[2] else low
        i = count.end()
    else:
        return 1, 1, i
    if text[i:i + 1] in ("?", "+"):  # lazy or possessive: the same repeat for this check
        i += 1
    return low, high, i


def _repeated(part: _Part, low: int, high: int | None) -> _Part:
    if (low, high) == (1, 1):
        return part
    if max(low, high or 0) > MAX_PATTERN_REPEAT:
        raise CatalogError(f"a repeat count is above {MAX_PATTERN_REPEAT}")
    if part.ways > 1 and (high is None or high > 1):
        raise CatalogError("a group with alternatives or a variable repeat is repeated")
    top = max(low, MAX_PATTERN_REPEAT) if high is None else high
    return _Part(ways=sum(part.ways ** count for count in range(low, top + 1)),
                 unbounded=part.unbounded or high is None, optional=part.optional or low == 0)


def _class_end(text: str, i: int) -> int:
    """Index after the character class that starts at text[i]; a "]" first in it is literal."""
    i += 2 if text.startswith("[^", i) else 1
    if text[i:i + 1] == "]":
        i += 1
    while text[i] != "]":
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _check_backtracking(text: str):
    """Refuse a pattern whose matching could take very long on a long window class.

    Backtracking gets slow when a class can be split among repeats in many
    ways. So no repeat count may be above MAX_PATTERN_REPEAT, a group that
    repeats may not hold alternatives or a repeat of variable count, two
    repeats without an upper bound may not follow each other with only
    optional parts between them, and there may be no more than
    MAX_PATTERN_WAYS ways to match, a repeat without an upper bound counted
    as one of up to MAX_PATTERN_REPEAT: no (a+)+, (a|aa)+, .*.* or long runs
    of a?. Groups are ( ) or (?: ) and escapes stand for one character or a
    boundary. Runs after re.compile, which has already refused bad syntax.
    """
    groups = [_Group()]
    i = 0
    while i < len(text):
        char = text[i]
        if char == "(":
            if text.startswith("(?", i) and not text.startswith("(?:", i):
                raise CatalogError("only ( ) and (?: ) groups are supported")
            groups.append(_Group())
            i += 3 if text.startswith("(?:", i) else 1
            continue
        if char == "|":
            groups[-1].alternative()
            i += 1
            continue
        if char == ")":
            part, i = groups.pop().close(), i + 1
        elif char == "[":
            part, i = _Part(), _class_end(text, i)
        elif char == "\\":
            escaped = text[i + 1]
            if escaped.isdigit() or escaped in "xuUN":
                raise CatalogError(f"unsupported escape \\{escaped}")
            part, i = _Part(optional=escaped in "bBAZz"), i + 2
        else:
            part, i = _Part(optional=char in "^$"), i + 1
        low, high, i = _repeat(text, i)
        groups[-1].add(_repeated(part, low, high))
    if groups[0].close().ways > MAX_PATTERN_WAYS:
        raise CatalogError(f"more than {MAX_PATTERN_WAYS} ways to match a class")


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


def parse_entry(raw: dict, warnings: list[str] | None = None) -> AppEntry:
    """Check one app entry. A class pattern left out does not reject the entry: why is added to warnings."""
    if not isinstance(raw, dict):
        raise CatalogError("app entry must be an object")
    app_id = raw.get("id")
    if not isinstance(app_id, str) or not ID_RE.match(app_id):
        raise CatalogError(f"invalid app id {app_id!r}")
    ignored: list[str] = []
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
        entry = AppEntry(
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
            patterns=_patterns(match.get("patterns"), ignored),
            title_contains=_strings(match.get("titleContains"), lower=True),
            default_pinned=bool(raw.get("defaultPinned", False)),
            information_url=url,
        )
    except CatalogError as error:
        raise CatalogError(f"{app_id}: {error}") from None
    if warnings is not None:
        warnings.extend(f"{app_id}: {reason}" for reason in ignored)
    return entry


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
        """Exact classes beat prefixes and patterns, which beat titles; ties go to catalog order."""
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
            entry = parse_entry(raw, errors)
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
