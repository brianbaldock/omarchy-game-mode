"""Locale loading with a complete English fallback.

Locale files are flat ``{"key": "string"}`` JSON maps in ``data/locales``.
The language follows an explicit user choice, then
``~/.config/omarchy/language``, then the POSIX locale variables, and finally
English. Missing keys fall back to the English file, then to the built-in
English defaults below, then to the key itself.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

RTL_LANGUAGES = frozenset({"ar", "fa", "he", "ur"})
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")

# Endonyms are not translated: a language is always offered in its own name.
ENDONYMS = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "pt-BR": "Português (Brasil)",
    "ja": "日本語",
    "ar": "العربية",
}

DEFAULTS = {
    "app.name": "Omarchy Game Mode",
    "nav.home": "Home",
    "nav.discover": "Discover",
    "nav.settings": "Settings",
    "hero.title": "Your next session starts here.",
    "hero.subtitle": "Your games. Your platforms. Your desktop.",
    "hero.resume": "Resume session",
    "hero.noSession": "Ready when you are",
    "hero.noSessionBody": "Choose a platform below or find something new in Discover.",
    "section.library": "YOUR LIBRARY",
    "section.discover": "DISCOVER",
    "button.launch": "Launch",
    "button.install": "Install",
    "button.add": "Add to Home",
    "button.remove": "Remove from Home",
    "button.back": "Back",
    "button.desktop": "Desktop",
    "button.close": "Close",
    "button.retry": "Retry",
    "status.installed": "Installed",
    "status.notInstalled": "Not installed",
    "status.installing": "Installation opened in a terminal",
    "status.starting": "Starting {name}...",
    "status.unavailable": "Unavailable on this device",
    "status.controllerConnected": "Controller connected",
    "status.controllerUnavailable": "Keyboard and touch ready",
    "status.ready": "Ready to play",
    "status.error": "Could not launch {name}: {reason}",
    "settings.title": "Make yourself at home",
    "settings.language": "Language",
    "settings.systemLanguage": "Follow system",
    "settings.autostart": "Show Omarchy Game Mode at login",
    "settings.autostartHelp": "Start here next time. You can always return to Desktop.",
    "settings.controllerMode": "Controller mode",
    "settings.modeAuto": "Automatic",
    "settings.modeGame": "Game input",
    "settings.modeDesktop": "Desktop navigation",
    "settings.modeHelp": "Automatic follows the focused app. A manual choice stays until you switch apps.",
    "footer.select": "A Select",
    "footer.desktop": "B Desktop",
    "footer.home": "Hold View + Menu: Home",
    "footer.guide": "Xbox Guide stays with your game",
    "discover.title": "More ways to play",
    "discover.subtitle": "Add your platforms. Install only what you choose.",
    "discover.empty": "All your platforms are already on Home.",
    "discover.unsupported": "Not available for this architecture",
    "discover.installHelp": "Installation requires confirmation in the terminal.",
    "category.native": "Native games",
    "category.streaming": "Streaming",
    "category.retro": "Retro games",
    "category.stores": "Game stores",
    "app.steam.description": "Your Steam library and store, including Proton games, on this desktop.",
    "app.remote.description": "Stream an Xbox console on your network with Greenlight.",
    "app.cloud.description": "Opens Xbox Cloud Gaming in its own window. Sign in with your own account.",
    "app.geforce-now.description": "Stream supported PC games with NVIDIA GeForce NOW. Sign in with your own account.",
    "app.moonlight.description": "Stream a PC running Sunshine. Moonlight is already included with Omarchy.",
    "app.retroarch.description": "Classic systems through RetroArch and libretro. Keep ROMs in your Games folder.",
    "app.heroic.description": "Epic Games, GOG, and Amazon Games through Heroic Games Launcher.",
    "app.lutris.description": "Windows and Linux games through Lutris, including stores without their own installer.",
    "app.battlenet.description": "Battle.net games through Omarchy's launcher, without Steam.",
    "app.minecraft.description": "The official Minecraft Launcher on this desktop.",
    "app.moonlight.installHelp": "Moonlight ships with Omarchy, so there is no separate installer here. If the moonlight command is missing, reinstall the moonlight-qt package from Omarchy's base set.",
    "app.remote.installHelp": "Choosing Install is the confirmation. Omarchy then adds the verified greenlight-bin package and does not ask again in the terminal.",
    "hero.startingBody": "Waiting for its window to open. Press B to go to the desktop meanwhile.",
    "library.addMore": "Add more",
    "library.addMoreBody": "Find stores, streaming and emulators in Discover.",
    "library.empty": "Your library is empty. Add apps from Discover.",
    "button.controls": "Controls",
    "button.info": "Website",
    "status.running": "Running",
    "status.inLibrary": "On Home",
    "status.controllerPermission": "Controller not readable",
    "status.launchTimeout": "{name} did not open a window. Try again, or start it from the desktop.",
    "status.sessionEnded": "{name} closed.",
    "status.ipcLost": "Lost the connection to Hyprland. Window tracking is paused; keyboard and touch still work.",
    "status.noTarget": "Open a game or app first, then choose how its controller works.",
    "status.notInstalledHelp": "{name} is not installed, and there is no installer for it here.",
    "status.installedNow": "{name} is installed and on Home.",
    "status.installOpened": "Installing {name} in a terminal. Follow the prompts there.",
    "status.noTerminal": "No terminal is available to install {name}.",
    "status.mapperMissing": "AntiMicroX is not running, so the controller always goes straight to apps.",
    "status.modeGame": "Game input",
    "status.modeDesktop": "Desktop navigation",
    "battery.charging": "Charging",
    "battery.discharging": "On battery",
    "battery.full": "Fully charged",
    "battery.untilFull": "{time} until full",
    "battery.remaining": "{time} left",
    "battery.hoursMinutes": "{hours} h {minutes} min",
    "battery.minutes": "{minutes} min",
    "settings.on": "On",
    "settings.off": "Off",
    "settings.modeAutoHelp": "Games get the controller; desktop apps get pointer and keys.",
    "settings.modeGameHelp": "The controller goes straight to the app.",
    "settings.modeDesktopHelp": "The controller moves the pointer and presses keys.",
    "settings.modeTarget": "Applies to {name}",
    "settings.modeNoTarget": "Open a game or app to choose its controls.",
    "settings.mapper": "Switch AntiMicroX sets",
    "settings.mapperHelp": "Uses set 1 on the desktop and an empty set 2 in games and Gaming Home. Off leaves AntiMicroX alone.",
    "status.mapperExternal": "Controller paused by AntiMicroX",
    "status.mapperExternalHelp": "AntiMicroX is running and Game Mode is not switching its sets, so use keyboard or touch here, or turn on Switch AntiMicroX sets in Settings.",
    "inputplumber.device": "InputPlumber {version} manages {name} with the profile {profile}.",
    "inputplumber.reading": "Game Mode reads its virtual gamepad and changes nothing in InputPlumber.",
    "inputplumber.noDevice": "InputPlumber is running but manages no controller.",
    "inputplumber.readFailed": "InputPlumber is running, but Game Mode could not read its state.",
    "inputplumber.unrecognized": "Game Mode does not recognize its virtual controller type ({kinds}), so use keyboard or touch here.",
    "inputplumber.ambiguous": "Several virtual controllers look like the one InputPlumber announces, so Game Mode uses none of them. Use keyboard or touch here.",
    "inputplumber.hidden": "{name} is present, but InputPlumber hides it and Game Mode cannot read it.",
    "inputplumber.evdevOnly": "It reads this controller through evdev only.",
    "inputplumber.noR3": "It reports no right stick click (R3).",
    "inputplumber.padName": "{name} (InputPlumber)",
    "bluetooth.title": "Bluetooth",
    "bluetooth.tooltipOn": "Bluetooth is on",
    "bluetooth.tooltipOff": "Bluetooth is off",
    "bluetooth.tooltipConnected": "Connected:",
    "bluetooth.tooltipError": "Couldn't read Bluetooth",
    "bluetooth.unnamed": "Unnamed device",
    "bluetooth.powerHint": "Turns Bluetooth on or off for the whole computer. It stays that way after a restart.",
    "bluetooth.turningOn": "Turning on…",
    "bluetooth.turningOff": "Turning off…",
    "bluetooth.offBody": "Turn Bluetooth on to connect devices.",
    "bluetooth.readFailed": "Couldn't read Bluetooth.",
    "bluetooth.tryAgain": "Try again",
    "bluetooth.unavailable": "Bluetooth isn't available.",
    "bluetooth.myDevices": "My devices",
    "bluetooth.noDevices": "No devices yet. Add one below.",
    "bluetooth.connected": "Connected",
    "bluetooth.connectedBattery": "Connected · {percent}%",
    "bluetooth.notConnected": "Not connected",
    "bluetooth.connecting": "Connecting…",
    "bluetooth.disconnecting": "Disconnecting…",
    "bluetooth.forgetting": "Forgetting…",
    "bluetooth.connect": "Connect",
    "bluetooth.disconnect": "Disconnect",
    "bluetooth.forget": "Forget",
    "bluetooth.forgetQuestion": "Forget {name}?",
    "bluetooth.cancel": "Cancel",
    "bluetooth.canceling": "Canceling…",
    "bluetooth.addDevice": "Add a device",
    "bluetooth.addHint": "Put your device in pairing mode, then search. Devices you add can reconnect by themselves; forget one to stop that.",
    "bluetooth.search": "Search",
    "bluetooth.stopSearch": "Stop searching",
    "bluetooth.searching": "Searching…",
    "bluetooth.looking": "Looking for devices…",
    "bluetooth.nothingFound": "Nothing found. Check that the device is in pairing mode.",
    "bluetooth.truncated": "More devices were found than are shown.",
    "bluetooth.pair": "Pair",
    "bluetooth.confirm": "Does this number match the one shown on {name}?",
    "bluetooth.matches": "Yes, it matches",
    "bluetooth.no": "No",
    "bluetooth.typeCode": "On {name}, type {code} and press Enter.",
    "bluetooth.enterPin": "Enter the PIN for {name}.",
    "bluetooth.enterPasskey": "Enter the passkey shown on {name}.",
    "bluetooth.erase": "Delete",
    "bluetooth.done": "Done",
    "bluetooth.pairingWith": "Pairing with {name}…",
    "bluetooth.connectingTo": "Connecting to {name}…",
    "bluetooth.dismiss": "Dismiss",
    "bluetooth.failure": "{action} {reason}",
    "bluetooth.failed.connecting": "Couldn't connect to {name}.",
    "bluetooth.failed.disconnecting": "Couldn't disconnect {name}.",
    "bluetooth.failed.pairing": "Couldn't add {name}.",
    "bluetooth.failed.forgetting": "Couldn't forget {name}.",
    "bluetooth.failed.powering": "Couldn't switch Bluetooth on or off.",
    "bluetooth.failed.searching": "Couldn't search for devices.",
    "bluetooth.failed.other": "Bluetooth ran into a problem.",
    "bluetooth.failed.paired": "{name} was paired but did not connect.",
    "bluetooth.failed.pairedHint": "Choose Connect under My devices to try again.",
    "bluetooth.failed.canceledPaired": "{name} had already paired when you canceled.",
    "bluetooth.failed.canceledPairedHint": "It's in My devices: choose Connect to use it, or Forget to remove it. It won't reconnect by itself unless you forget it and add it again.",
    "bluetooth.failed.canceledTrustedHint": "It's in My devices: choose Connect to use it, or Forget to remove it.",
    "bluetooth.reason.blocked": "Bluetooth is blocked, for example by airplane mode.",
    "bluetooth.reason.not-ready": "Bluetooth is off or still starting.",
    "bluetooth.reason.timeout": "It took too long.",
    "bluetooth.reason.unreachable": "The device didn't answer. Is it on and close by?",
    "bluetooth.reason.auth-failed": "The device or you rejected the pairing, or the code was wrong.",
    "bluetooth.reason.canceled": "It was canceled.",
    "bluetooth.reason.in-progress": "The device is busy. Try again in a moment.",
    "bluetooth.reason.unsupported": "This device has nothing this computer can use.",
    "bluetooth.reason.gone": "The device or Bluetooth went away meanwhile.",
    "bluetooth.reason.unavailable": "Bluetooth isn't responding.",
    "bluetooth.reason.failed": "Something went wrong.",
}


def normalize(code: str | None) -> str | None:
    """'pt_BR.UTF-8' -> 'pt-BR', 'en_US' -> 'en-US', 'C' -> None."""
    if not code:
        return None
    code = code.strip().split(".")[0].split("@")[0]
    if not code or code in ("C", "POSIX") or not LANG_RE.match(code):
        return None
    parts = code.replace("_", "-").split("-")
    if len(parts) == 1:
        return parts[0].lower()
    return f"{parts[0].lower()}-{parts[1].upper() if len(parts[1]) == 2 else parts[1]}"


def best_match(code: str | None, available: list[str]) -> str | None:
    code = normalize(code)
    if not code:
        return None
    if code in available:
        return code
    base = code.split("-")[0]
    if base in available:
        return base
    return next((a for a in available if a.split("-")[0] == base), None)


def system_language_candidates(env=os.environ, omarchy_language_file: Path | None = None) -> list[str]:
    candidates = []
    if omarchy_language_file is None and env.get("OMARCHY_LANGUAGE_FILE"):
        omarchy_language_file = Path(env["OMARCHY_LANGUAGE_FILE"])
    if omarchy_language_file is None:
        config_home = Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME", "~")).expanduser() / ".config")
        omarchy_language_file = config_home / "omarchy" / "language"
    try:
        first = omarchy_language_file.read_text(encoding="utf-8").split()
        if first:
            candidates.append(first[0])
    except OSError:
        pass
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(var)
        if value:
            candidates.extend(value.split(":") if var == "LANGUAGE" else [value])
    return candidates


class Translator:
    def __init__(self, locales_dir: Path, choice: str = "system", env=os.environ, omarchy_language_file: Path | None = None):
        self.locales_dir = Path(locales_dir)
        self.available = self._available()
        self.english = {**DEFAULTS, **self._load("en")}
        self.language = self.resolve(choice, env, omarchy_language_file)
        self.strings = self._load(self.language) if self.language != "en" else {}

    def _available(self) -> list[str]:
        try:
            names = sorted(p.stem for p in self.locales_dir.glob("*.json") if LANG_RE.match(p.stem))
        except OSError:
            names = []
        return names or ["en"]

    def _load(self, language: str) -> dict:
        try:
            with open(self.locales_dir / f"{language}.json", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def resolve(self, choice, env, omarchy_language_file) -> str:
        if choice and choice != "system":
            match = best_match(choice, self.available)
            if match:
                return match
        for candidate in system_language_candidates(env, omarchy_language_file):
            match = best_match(candidate, self.available)
            if match:
                return match
        return "en"

    @property
    def rtl(self) -> bool:
        return self.language.split("-")[0] in RTL_LANGUAGES

    def has(self, key: str) -> bool:
        return key in self.strings or key in self.english

    def __call__(self, key: str, **params) -> str:
        text = self.strings.get(key) or self.english.get(key) or key
        if params:
            try:
                text = text.format_map(_SafeParams(params))
            except (ValueError, IndexError):
                pass
        return text

    def endonym(self, language: str) -> str:
        return ENDONYMS.get(language, language)


class _SafeParams(dict):
    def __missing__(self, key):
        return "{" + key + "}"
