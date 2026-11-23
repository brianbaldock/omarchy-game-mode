"""Safe, non-privileged Quick Settings providers and command-line backend.

The module deliberately has no GTK dependency. D-Bus is imported lazily, and
all subprocesses use fixed argument vectors with bounded timeouts.
"""

from __future__ import annotations

import atexit
import json
import math
import re
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any, Callable

from . import APP_ID

DBUS_TIMEOUT_MS = 2000
COMMAND_TIMEOUT_S = 2.5
OSD_TIMEOUT_S = 2.0

PPD_ENDPOINTS = (
    ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles",
     "org.freedesktop.UPower.PowerProfiles"),
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles"),
)
PROFILE_LABELS = {
    "power-saver": "Quiet",
    "balanced": "Balanced",
    "performance": "Performance",
}
PROPERTIES = "org.freedesktop.DBus.Properties"
UPOWER = "org.freedesktop.UPower"
UPOWER_DEVICE = "org.freedesktop.UPower.Device"
ASUSD = "xyz.ljones.Asusd"
ASUSD_PATH = "/xyz/ljones"
ASUSD_PLATFORM = "xyz.ljones.Platform"
LOGIN1 = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"
DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"
DEFAULT_SOURCE = "@DEFAULT_AUDIO_SOURCE@"
DBUS_WELL_KNOWN_NAME = re.compile(
    r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+"
)
DBUS_UNIQUE_NAME = re.compile(r":[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+")
QUICK_USAGE = (
    "Usage: omarchy-game-mode quick "
    "[status|profile <power-saver|balanced|performance>|brightness <N|+N|-N>|"
    "volume <N|+N|-N|mute|unmute>|mic mute|unmute]"
)

Runner = Callable[..., Any]


def _plain(value: Any) -> Any:
    unpack = getattr(value, "unpack", None)
    if callable(unpack):
        return _plain(unpack())
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _json_value(value: Any) -> Any:
    value = _plain(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return str(value)


def _detail(value: BaseException | Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:240] or value.__class__.__name__


def _ok(detail: str) -> dict[str, Any]:
    return {"ok": True, "detail": detail}


def _failed(detail: Any) -> dict[str, Any]:
    return {"ok": False, "detail": _detail(detail)}


def _mapping(value: Any) -> dict[str, Any]:
    value = _plain(value)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        try:
            return dict(value)
        except (TypeError, ValueError):
            return {}
    return {}


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str) and len(value) <= 16:
            match = re.fullmatch(r"[+-]?\d{1,4}(?:\.\d{1,2})?", value)
            if not match:
                raise ValueError(f"invalid {name}")
            number = float(value)
        else:
            raise ValueError(f"invalid {name}")
    except (OverflowError, TypeError):
        raise ValueError(f"invalid {name}")
    if not math.isfinite(number):
        raise ValueError(f"invalid {name}")
    return number


def _percent_value(
    value: Any,
    current: float,
    *,
    floor: float = 0.0,
    clamp_above_100: bool = False,
) -> float:
    text = str(value) if not isinstance(value, bool) else ""
    relative = isinstance(value, str) and bool(text) and text[0] in "+-"
    number = _number(value, "percentage")
    if relative:
        if abs(number) > 100:
            raise ValueError("percentage step is too large")
        target = current + number
    else:
        if number < 0:
            raise ValueError("percentage must be non-negative")
        if number > 100 and not clamp_above_100:
            raise ValueError("percentage must be between 0 and 100")
        target = min(number, 100.0) if clamp_above_100 and number >= 0 else number
    return min(100.0, max(floor, target))


def _format_number(number: float) -> str:
    return f"{number:.2f}".rstrip("0").rstrip(".")


class GioSystemBus:
    """Minimal lazy system-bus facade backed by Gio."""

    def __init__(self):
        self._connection = None
        self._gio = None
        self._glib = None

    def _ensure(self):
        if self._connection is not None:
            return
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self._gio, self._glib = Gio, GLib
        self._connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def _result(self, reply: Any) -> Any:
        value = _plain(reply)
        if isinstance(value, list) and len(value) == 1:
            return value[0]
        return value

    def _call(
        self,
        name: str,
        path: str,
        interface: str,
        method: str,
        signature: str,
        arguments: tuple,
        reply_signature: str | None,
    ) -> Any:
        self._ensure()
        parameters = self._glib.Variant(signature, arguments) if signature else None
        reply_type = self._glib.VariantType(reply_signature) if reply_signature else None
        result = self._connection.call_sync(
            name,
            path,
            interface,
            method,
            parameters,
            reply_type,
            self._gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            None,
        )
        return None if reply_type is None else self._result(result)

    def get_all(self, name: str, path: str, interface: str) -> dict[str, Any]:
        result = self._call(name, path, PROPERTIES, "GetAll", "(s)", (interface,), "(a{sv})")
        properties = _mapping(result)
        if not properties:
            raise RuntimeError(f"no properties for {interface}")
        return properties

    def set_property(
        self,
        name: str,
        path: str,
        interface: str,
        property_name: str,
        signature: str,
        value: Any,
    ):
        self._ensure()
        variant = self._glib.Variant(signature, value)
        parameters = self._glib.Variant("(ssv)", (interface, property_name, variant))
        self._connection.call_sync(
            name,
            path,
            PROPERTIES,
            "Set",
            parameters,
            None,
            self._gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT_MS,
            None,
        )

    def call(
        self,
        name: str,
        path: str,
        interface: str,
        method: str,
        signature: str,
        arguments: tuple,
        reply_signature: str | None,
    ) -> Any:
        return self._call(name, path, interface, method, signature, arguments, reply_signature)

    def watch_name(self, name: str, appeared: Callable, vanished: Callable) -> int:
        self._gio_required()
        return self._gio.bus_watch_name(
            self._gio.BusType.SESSION,
            name,
            self._gio.BusNameWatcherFlags.NONE,
            lambda connection, bus_name, owner: appeared(owner),
            lambda connection, bus_name: vanished(),
        )

    def unwatch_name(self, token: int):
        self._gio_required()
        self._gio.bus_unwatch_name(token)

    def subscribe_signal(
        self,
        name: str,
        path: str,
        interface: str,
        member: str,
        callback: Callable,
    ) -> int:
        self._ensure()

        def dispatch(_connection, _sender, _path, _interface, _member, parameters, _data):
            callback(_plain(parameters))

        return self._connection.signal_subscribe(
            name,
            interface,
            member,
            path,
            None,
            self._gio.DBusSignalFlags.NONE,
            dispatch,
            None,
        )

    def unsubscribe_signal(self, token: int):
        self._ensure()
        self._connection.signal_unsubscribe(token)

    def _gio_required(self):
        self._ensure()


class PowerProfiles:
    """Read and safely control power-profiles-daemon."""

    def __init__(self, bus: Any | None = None):
        self.bus = bus or GioSystemBus()
        self._endpoint: tuple[str, str, str] | None = None
        self._hold_endpoint: tuple[str, str, str] | None = None
        self._hold_cookie: int | None = None
        self._hold_generation = 0
        self._holder_watch = None
        self._profile_signal = None

    @property
    def hold_cookie(self) -> int | None:
        return self._hold_cookie

    def _load(self) -> tuple[tuple[str, str, str], dict[str, Any]]:
        errors = []
        endpoints = (self._endpoint,) + PPD_ENDPOINTS if self._endpoint else PPD_ENDPOINTS
        seen = set()
        for endpoint in endpoints:
            if endpoint is None or endpoint in seen:
                continue
            seen.add(endpoint)
            name, path, interface = endpoint
            try:
                properties = self.bus.get_all(name, path, interface)
                self._endpoint = endpoint
                return endpoint, properties
            except Exception as error:
                errors.append(f"{name}: {_detail(error)}")
        self._endpoint = None
        raise RuntimeError("; ".join(errors) or "power profiles unavailable")

    @staticmethod
    def _profiles(value: Any) -> list[dict[str, Any]]:
        profiles = []
        for raw in _plain(value) if isinstance(_plain(value), list) else []:
            item = _mapping(raw)
            name = item.get("Profile")
            if not isinstance(name, str) or not name:
                continue
            profile = {"id": name, "label": PROFILE_LABELS.get(name, name)}
            for key, output in (("CpuDriver", "cpu_driver"), ("PlatformDriver", "platform_driver")):
                if isinstance(item.get(key), str):
                    profile[output] = item[key]
            profiles.append(profile)
        return profiles

    @staticmethod
    def _holds(value: Any) -> list[dict[str, Any]]:
        holds = []
        for raw in _plain(value) if isinstance(_plain(value), list) else []:
            holds.append(_json_value(_mapping(raw)))
        return holds

    def read(self) -> dict[str, Any]:
        try:
            endpoint, properties = self._load()
            active = properties.get("ActiveProfile")
            profiles = self._profiles(properties.get("Profiles", []))
            raw_holds = _plain(properties.get("ActiveProfileHolds"))
            holds = self._holds(raw_holds)
            owned = self._hold_cookie is not None
            if owned and isinstance(raw_holds, list) and not raw_holds:
                self._forget_hold()
                owned = False
            return {
                "available": True,
                "service": endpoint[0],
                "active": active if isinstance(active, str) else None,
                "active_label": PROFILE_LABELS.get(active, active) if isinstance(active, str) else None,
                "profiles": profiles,
                "performance_degraded": properties.get("PerformanceDegraded", ""),
                "holds": holds,
                "battery_aware": properties.get("BatteryAware"),
                "owned_hold": owned,
            }
        except Exception as error:
            return {
                "available": False,
                "service": None,
                "active": None,
                "active_label": None,
                "profiles": [],
                "performance_degraded": None,
                "holds": [],
                "battery_aware": None,
                "owned_hold": self._hold_cookie is not None,
                "error": _detail(error),
            }

    def set_profile(self, profile: Any) -> dict[str, Any]:
        if not isinstance(profile, str) or profile not in PROFILE_LABELS:
            return _failed("Unknown power profile")
        try:
            endpoint, properties = self._load()
            available = {item["id"] for item in self._profiles(properties.get("Profiles", []))}
            if profile not in available:
                return _failed(f"Power profile {profile} is unavailable")
            name, path, interface = endpoint
            self.bus.set_property(name, path, interface, "ActiveProfile", "s", profile)
            return _ok(f"Power profile set to {PROFILE_LABELS[profile]}")
        except Exception as error:
            return _failed(error)

    def set_active_profile(self, profile: Any) -> dict[str, Any]:
        return self.set_profile(profile)

    def hold_profile(
        self,
        profile: Any,
        reason: Any,
        application_id: Any = APP_ID,
        holder_name: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(profile, str) or profile not in PROFILE_LABELS:
            return _failed("Unknown power profile")
        try:
            reason_text = self._validate_text(reason, "hold reason", 160)
            app_id = self._validate_text(application_id, "application id", 128)
            holder = self._validate_bus_name(holder_name) if holder_name is not None else None
            endpoint, properties = self._load()
            available = {item["id"] for item in self._profiles(properties.get("Profiles", []))}
            if profile not in available:
                return _failed(f"Power profile {profile} is unavailable")
            if self._hold_cookie is not None:
                previous = self.release_profile()
                if not previous["ok"]:
                    return previous
            name, path, interface = endpoint
            cookie = self.bus.call(name, path, interface, "HoldProfile", "(sss)",
                                   (profile, reason_text, app_id), "(u)")
            if isinstance(cookie, bool) or not isinstance(cookie, int) or cookie < 1:
                raise RuntimeError("power profile daemon returned an invalid hold cookie")
            self._hold_cookie = cookie
            self._hold_endpoint = endpoint
            self._hold_generation += 1
            self._watch_profile_released(endpoint, self._hold_generation)
            if self._hold_cookie is None:
                return _failed("Power profile hold was already released")
            if holder is not None:
                self._watch_holder(holder, self._hold_generation)
                if self._hold_cookie is None:
                    return _failed("Power profile holder has vanished")
            return _ok(f"{PROFILE_LABELS[profile]} profile hold acquired")
        except Exception as error:
            if self._hold_cookie is not None:
                self.release_profile()
            return _failed(error)

    def hold_performance(
        self,
        reason: Any = "Game Mode performance session",
        application_id: Any = APP_ID,
        holder_name: Any = None,
    ) -> dict[str, Any]:
        return self.hold_profile("performance", reason, application_id, holder_name)

    def _watch_holder(self, holder: str, generation: int):
        def vanished():
            if generation != self._hold_generation:
                return
            self.release_profile()

        watch = self.bus.watch_name(holder, lambda _owner: None, vanished)
        if generation == self._hold_generation and self._hold_cookie is not None:
            self._holder_watch = watch
        else:
            try:
                self.bus.unwatch_name(watch)
            except Exception:
                pass

    def _watch_profile_released(self, endpoint: tuple[str, str, str], generation: int):
        def released(parameters):
            values = _plain(parameters)
            cookie = values[0] if isinstance(values, list) and len(values) == 1 else None
            if generation == self._hold_generation and cookie == self._hold_cookie:
                self._forget_hold()

        name, path, interface = endpoint
        try:
            token = self.bus.subscribe_signal(name, path, interface, "ProfileReleased", released)
        except Exception:
            return
        if generation == self._hold_generation and self._hold_cookie is not None:
            self._profile_signal = token
        else:
            try:
                self.bus.unsubscribe_signal(token)
            except Exception:
                pass

    def release_profile(self) -> dict[str, Any]:
        cookie, endpoint = self._hold_cookie, self._hold_endpoint
        if cookie is None:
            self._forget_hold()
            return _ok("No power profile hold active")
        failure = None
        try:
            if endpoint is None:
                raise RuntimeError("Power profile hold service is unavailable")
            name, path, interface = endpoint
            self.bus.call(name, path, interface, "ReleaseProfile", "(u)", (cookie,), None)
        except Exception as error:
            failure = error
        finally:
            self._forget_hold()
        if failure is not None:
            return _failed(failure)
        return _ok("Power profile hold released")

    def _forget_hold(self):
        self._hold_generation += 1
        self._hold_cookie = None
        self._hold_endpoint = None
        self._unwatch_holder()
        self._unsubscribe_profile_released()

    def release_hold(self) -> dict[str, Any]:
        return self.release_profile()

    def _unwatch_holder(self):
        if self._holder_watch is None:
            return
        try:
            self.bus.unwatch_name(self._holder_watch)
        except Exception:
            pass
        self._holder_watch = None

    def _unsubscribe_profile_released(self):
        if self._profile_signal is None:
            return
        try:
            self.bus.unsubscribe_signal(self._profile_signal)
        except Exception:
            pass
        self._profile_signal = None

    def close(self) -> dict[str, Any]:
        return self.release_profile()

    @staticmethod
    def _validate_text(value: Any, label: str, limit: int) -> str:
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError(f"{label} is invalid")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"{label} is invalid")
        return value

    @classmethod
    def _validate_bus_name(cls, value: Any) -> str:
        name = cls._validate_text(value, "holder name", 255)
        if not (DBUS_WELL_KNOWN_NAME.fullmatch(name) or DBUS_UNIQUE_NAME.fullmatch(name)):
            raise ValueError("holder name is not a valid D-Bus name")
        return name


class Battery:
    """Read UPower battery telemetry and the ASUS-specific charge limit."""

    def __init__(self, bus: Any | None = None, charge_limit_writable: bool = True):
        self.bus = bus or GioSystemBus()
        self.charge_limit_writable = bool(charge_limit_writable)

    def read(self) -> dict[str, Any]:
        battery, upower_error = self._read_upower()
        limit, asusd_error = self._read_charge_limit()
        available = battery is not None
        result = {
            "available": available,
            "path": battery.get("path") if battery else None,
            "percentage": battery.get("percentage") if battery else None,
            "state": battery.get("state") if battery else None,
            "energy_rate_w": battery.get("energy_rate_w") if battery else None,
            "time_to_empty_s": battery.get("time_to_empty_s") if battery else None,
            "time_to_full_s": battery.get("time_to_full_s") if battery else None,
            "energy_wh": battery.get("energy_wh") if battery else None,
            "energy_full_wh": battery.get("energy_full_wh") if battery else None,
            "energy_full_design_wh": battery.get("energy_full_design_wh") if battery else None,
            "health_percent": battery.get("health_percent") if battery else None,
            "charge_limit_percent": limit,
            "charge_limit_source": "asusd" if limit is not None else None,
            "charge_limit_writable": self.charge_limit_writable and limit is not None,
        }
        errors = {}
        if upower_error:
            errors["upower"] = upower_error
        if asusd_error:
            errors["asusd"] = asusd_error
        if errors:
            result["errors"] = errors
        return result

    def _read_upower(self) -> tuple[dict[str, Any] | None, str | None]:
        errors = []
        paths = []
        try:
            paths = self.bus.call(
                UPOWER,
                "/org/freedesktop/UPower",
                UPOWER,
                "EnumerateDevices",
                "",
                (),
                "(ao)",
            )
            if not isinstance(paths, list):
                paths = []
        except Exception as error:
            errors.append(_detail(error))
        preferred = [path for path in paths if isinstance(path, str) and "battery" in path.lower()]
        fallback = [path for path in paths if isinstance(path, str) and "DisplayDevice" in path]
        candidates = [
            *preferred,
            "/org/freedesktop/UPower/devices/battery_BAT1",
            *fallback,
            "/org/freedesktop/UPower/devices/DisplayDevice",
        ]
        seen = set()
        display = None
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            try:
                properties = self.bus.get_all(UPOWER, path, UPOWER_DEVICE)
                data = self._battery_values(path, properties)
                if not properties.get("IsPresent", True):
                    continue
                if properties.get("Type") in (None, 2) and properties.get("PowerSupply", True):
                    if properties.get("Type") == 2 or "battery" in path.lower():
                        return data, None
                    display = display or data
            except Exception as error:
                errors.append(f"{path}: {_detail(error)}")
        return (display, "; ".join(errors) if display and errors else None) if display else (
            None,
            "; ".join(errors) or "No UPower battery device",
        )

    @staticmethod
    def _battery_values(path: str, properties: Mapping[str, Any]) -> dict[str, Any]:
        energy_full = Battery._finite(properties.get("EnergyFull"))
        design_full = Battery._finite(properties.get("EnergyFullDesign"))
        health = energy_full * 100.0 / design_full if energy_full is not None and design_full else None
        state = properties.get("State")
        states = {
            0: "unknown",
            1: "charging",
            2: "discharging",
            3: "empty",
            4: "full",
            5: "pending-charge",
            6: "pending-discharge",
        }
        return {
            "path": path,
            "percentage": Battery._finite(properties.get("Percentage")),
            "state": states.get(state, "unknown"),
            "energy_rate_w": Battery._finite(properties.get("EnergyRate")),
            "time_to_empty_s": Battery._integer(properties.get("TimeToEmpty")),
            "time_to_full_s": Battery._integer(properties.get("TimeToFull")),
            "energy_wh": Battery._finite(properties.get("Energy")),
            "energy_full_wh": energy_full,
            "energy_full_design_wh": design_full,
            "health_percent": round(health, 2) if health is not None and math.isfinite(health) else None,
        }

    @staticmethod
    def _finite(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _read_charge_limit(self) -> tuple[int | None, str | None]:
        try:
            properties = self.bus.get_all(ASUSD, ASUSD_PATH, ASUSD_PLATFORM)
            limit = properties.get("ChargeControlEndThreshold")
            if isinstance(limit, bool) or not isinstance(limit, int):
                return None, "asusd did not report ChargeControlEndThreshold"
            return limit, None
        except Exception as error:
            return None, _detail(error)

    def set_charge_limit(self, value: Any) -> dict[str, Any]:
        if not self.charge_limit_writable:
            return _failed("ASUS charge limit is read-only")
        if isinstance(value, bool):
            return _failed("Charge limit must be an integer percentage")
        if isinstance(value, str):
            if not re.fullmatch(r"\d{2,3}", value):
                return _failed("Invalid charge limit")
            limit = int(value)
        elif isinstance(value, int):
            limit = value
        else:
            return _failed("Charge limit must be an integer percentage")
        if not 60 <= limit <= 100 or limit % 5:
            return _failed("Charge limit must be 60-100 in steps of 5")
        try:
            self.bus.set_property(
                ASUSD,
                ASUSD_PATH,
                ASUSD_PLATFORM,
                "ChargeControlEndThreshold",
                "y",
                limit,
            )
            return _ok(f"Charge limit set to {limit}%")
        except Exception as error:
            return _failed(error)


def _command(
    runner: Runner,
    argv: list[str],
    *,
    timeout: float = COMMAND_TIMEOUT_S,
) -> tuple[str | None, str | None]:
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as error:
        return None, _detail(error)
    if getattr(result, "returncode", 1) != 0:
        output = getattr(result, "stderr", "") or getattr(result, "stdout", "")
        return None, _detail(output) if output else f"{argv[0]} failed"
    output = getattr(result, "stdout", "")
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return str(output), None


def parse_brightness_output(output: Any) -> dict[str, Any]:
    if not isinstance(output, str):
        raise ValueError("invalid brightness output")
    fields = next((line.split(",") for line in output.splitlines() if line.count(",") >= 4), None)
    if fields is None or len(fields) < 5:
        raise ValueError("invalid brightness output")
    device, backlight_class, raw_text, percent_text, maximum_text = fields[:5]
    try:
        raw, maximum = int(raw_text), int(maximum_text)
        reported = float(percent_text.strip().rstrip("%"))
    except ValueError as error:
        raise ValueError("invalid brightness output") from error
    if not device or not backlight_class or maximum <= 0 or raw < 0 or not math.isfinite(reported):
        raise ValueError("invalid brightness output")
    try:
        percent = raw * 100.0 / maximum
    except OverflowError as error:
        raise ValueError("invalid brightness output") from error
    if not 0 <= percent <= 100:
        raise ValueError("invalid brightness output")
    return {
        "device": device,
        "class": backlight_class,
        "raw": raw,
        "maximum": maximum,
        "percent": round(percent, 2),
        "reported_percent": reported,
    }


class Brightness:
    """Bounded brightness control through brightnessctl."""

    def __init__(self, runner: Runner = subprocess.run, which: Callable[[str], Any] = shutil.which):
        self.runner = runner
        self.which = which

    def available(self) -> bool:
        return self.which("brightnessctl") is not None

    def read(self) -> dict[str, Any]:
        if not self.available():
            return {"available": False, "error": "brightnessctl is unavailable"}
        output, error = _command(self.runner, ["brightnessctl", "-m", "info"])
        if error:
            return {"available": False, "error": error}
        try:
            return {"available": True, **parse_brightness_output(output)}
        except ValueError as error:
            return {"available": False, "error": _detail(error)}

    def set_level(self, value: Any) -> dict[str, Any]:
        current = self.read()
        if not current["available"]:
            return _failed(current.get("error", "Brightness is unavailable"))
        try:
            target = _percent_value(value, current["percent"], floor=5.0)
        except ValueError as error:
            return _failed(error)
        _output, error = _command(
            self.runner,
            ["brightnessctl", "-m", "set", f"{_format_number(target)}%"],
        )
        if error:
            return _failed(error)
        return _ok(f"Brightness set to {_format_number(target)}%")

    def set(self, value: Any) -> dict[str, Any]:
        return self.set_level(value)


def parse_volume_output(output: Any) -> dict[str, Any]:
    if not isinstance(output, str):
        raise ValueError("invalid volume output")
    match = re.fullmatch(
        r"\s*Volume:\s*(\d+(?:\.\d+)?)((?:\s*\[[^\]\r\n]+\])*)\s*",
        output.strip(),
    )
    if not match:
        raise ValueError("invalid volume output")
    volume = float(match.group(1))
    if not math.isfinite(volume) or volume < 0:
        raise ValueError("invalid volume output")
    flags = re.findall(r"\[([^\]]+)\]", match.group(2))
    return {
        "volume": volume,
        "percent": volume * 100.0,
        "muted": any(flag.strip().upper() == "MUTED" for flag in flags),
    }


class Audio:
    """Read and control PipeWire's default sink and source."""

    def __init__(self, runner: Runner = subprocess.run, which: Callable[[str], Any] = shutil.which):
        self.runner = runner
        self.which = which

    def available(self) -> bool:
        return self.which("wpctl") is not None

    def _read_target(self, target: str) -> dict[str, Any]:
        output, error = _command(self.runner, ["wpctl", "get-volume", target])
        if error:
            return {"available": False, "error": error}
        try:
            return {"available": True, **parse_volume_output(output)}
        except ValueError as error:
            return {"available": False, "error": _detail(error)}

    def read(self) -> dict[str, Any]:
        if not self.available():
            return {
                "available": False,
                "volume": None,
                "muted": None,
                "mic_volume": None,
                "mic_muted": None,
                "error": "wpctl is unavailable",
            }
        sink = self._read_target(DEFAULT_SINK)
        source = self._read_target(DEFAULT_SOURCE)
        result = {
            "available": sink["available"] or source["available"],
            "volume": sink.get("percent"),
            "muted": sink.get("muted"),
            "mic_volume": source.get("percent"),
            "mic_muted": source.get("muted"),
            "sink": _json_value(sink),
            "source": _json_value(source),
        }
        if not sink["available"] or not source["available"]:
            result["errors"] = {
                key: data["error"]
                for key, data in (("sink", sink), ("source", source))
                if not data["available"]
            }
        return result

    def set_volume(self, value: Any) -> dict[str, Any]:
        text = str(value) if not isinstance(value, bool) else ""
        relative = isinstance(value, str) and bool(text) and text[0] in "+-"
        current = 0.0
        if relative:
            state = self._read_target(DEFAULT_SINK)
            if not state["available"]:
                return _failed(state.get("error", "Audio is unavailable"))
            current = state["percent"]
        try:
            target = _percent_value(value, current, clamp_above_100=True)
        except ValueError as error:
            return _failed(error)
        fraction = target / 100.0
        _output, error = _command(
            self.runner,
            ["wpctl", "set-volume", DEFAULT_SINK, f"{fraction:.4f}".rstrip("0").rstrip(".")],
        )
        if error:
            return _failed(error)
        return _ok(f"Volume set to {_format_number(target)}%")

    def set(self, value: Any) -> dict[str, Any]:
        return self.set_volume(value)

    def set_mute(self, muted: Any) -> dict[str, Any]:
        return self._set_mute(DEFAULT_SINK, muted, "Audio")

    def set_mic_mute(self, muted: Any) -> dict[str, Any]:
        return self._set_mute(DEFAULT_SOURCE, muted, "Microphone")

    def _set_mute(self, target: str, muted: Any, label: str) -> dict[str, Any]:
        if type(muted) is not bool:
            return _failed("Mute value must be true or false")
        _output, error = _command(
            self.runner,
            ["wpctl", "set-mute", target, "1" if muted else "0"],
        )
        if error:
            return _failed(error)
        return _ok(f"{label} {'muted' if muted else 'unmuted'}")


class PowerActions:
    """Explicitly confirmed session lock and power actions."""

    COMMANDS = {
        "lock": ["omarchy", "system", "lock"],
        "reboot": ["omarchy", "system", "reboot"],
        "poweroff": ["omarchy", "system", "shutdown"],
    }

    def __init__(
        self,
        bus: Any | None = None,
        runner: Runner = subprocess.run,
        which: Callable[[str], Any] = shutil.which,
    ):
        self.bus = bus or GioSystemBus()
        self.runner = runner
        self.which = which

    def read(self) -> dict[str, Any]:
        omarchy = self.which("omarchy") is not None
        return {
            "available": True,
            "confirmation_required": True,
            "actions": {
                "suspend": True,
                "reboot": omarchy,
                "poweroff": omarchy,
                "lock": omarchy,
            },
        }

    def perform(self, action: Any, *, confirm: bool = False) -> dict[str, Any]:
        if action not in ("suspend", "reboot", "poweroff", "lock"):
            return _failed("Unknown power action")
        if confirm is not True:
            return _failed("needs_confirmation")
        if action == "suspend":
            try:
                self.bus.call(
                    LOGIN1,
                    LOGIN1_PATH,
                    LOGIN1_MANAGER,
                    "Suspend",
                    "(b)",
                    (False,),
                    None,
                )
                return _ok("Suspend requested")
            except Exception as error:
                return _failed(error)
        if self.which("omarchy") is None:
            return _failed("omarchy is unavailable")
        _output, error = _command(self.runner, self.COMMANDS[action])
        return _failed(error) if error else _ok(f"{action.capitalize()} requested")


class OSD:
    """Best-effort feedback through Omarchy's existing OSD."""

    ICONS = {
        "brightness",
        "microphone",
        "microphone-muted",
        "volume-high",
        "volume-muted",
    }

    def __init__(
        self,
        runner: Runner = subprocess.run,
        which: Callable[[str], Any] = shutil.which,
    ):
        self.runner = runner
        self.which = which

    def show(
        self,
        *,
        icon: str = "",
        message: str = "",
        progress: int | float | None = None,
        duration_ms: int = 1200,
    ) -> None:
        if self.which("omarchy") is None:
            return
        if icon and icon not in self.ICONS:
            return
        if not isinstance(message, str) or len(message) > 120 or any(ord(char) < 32 for char in message):
            return
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 100 <= duration_ms <= 10000:
            return
        argv = ["omarchy", "osd"]
        if icon:
            argv.extend(["-i", icon])
        if message:
            argv.extend(["-m", message])
        if progress is not None:
            if isinstance(progress, bool):
                return
            try:
                bounded = round(min(100.0, max(0.0, _number(progress, "progress"))))
            except ValueError:
                return
            argv.extend(["-p", str(int(bounded))])
        argv.extend(["-d", str(duration_ms)])
        try:
            _command(self.runner, argv, timeout=OSD_TIMEOUT_S)
        except Exception:
            return

    def progress(self, icon: str, progress: int | float, duration_ms: int = 1200) -> None:
        self.show(icon=icon, progress=progress, duration_ms=duration_ms)


class QuickSettings:
    """Pull-on-demand aggregator with isolated providers."""

    ACTIONS = {
        "profile",
        "brightness",
        "volume",
        "mute",
        "mic_mute",
        "charge_limit",
        "hold_performance",
        "release_hold",
        "suspend",
        "reboot",
        "poweroff",
        "lock",
    }

    def __init__(
        self,
        *,
        bus: Any | None = None,
        runner: Runner = subprocess.run,
        power: PowerProfiles | None = None,
        battery: Battery | None = None,
        brightness: Brightness | None = None,
        audio: Audio | None = None,
        power_actions: PowerActions | None = None,
        osd: OSD | None = None,
    ):
        shared_bus = bus or GioSystemBus()
        self.power = power or PowerProfiles(shared_bus)
        self.battery = battery or Battery(shared_bus)
        self.brightness = brightness or Brightness(runner)
        self.audio = audio or Audio(runner)
        self.power_actions = power_actions or PowerActions(shared_bus, runner)
        self.osd = osd or OSD(runner)
        self._closed = False
        atexit.register(self.close)

    def snapshot(self) -> dict[str, Any]:
        result = {}
        for name, provider in (
            ("power", self.power),
            ("battery", self.battery),
            ("brightness", self.brightness),
            ("audio", self.audio),
            ("actions", self.power_actions),
        ):
            try:
                value = provider.read()
                if not isinstance(value, Mapping):
                    raise TypeError(f"{name} provider did not return a mapping")
                result[name] = _json_value(dict(value))
            except Exception as error:
                result[name] = {"available": False, "error": _detail(error)}
        return result

    def apply(self, action: Any, value: Any = None, *, confirm: bool = False) -> dict[str, Any]:
        try:
            return self._apply(action, value, confirm=confirm)
        except Exception as error:
            return _failed(error)

    def _apply(self, action: Any, value: Any, *, confirm: bool) -> dict[str, Any]:
        if not isinstance(action, str) or action not in self.ACTIONS:
            return _failed("Unknown quick setting action")
        if action == "profile":
            return self.power.set_profile(value)
        if action == "brightness":
            return self.brightness.set_level(value)
        if action == "volume":
            return self.audio.set_volume(value)
        if action == "mute":
            if type(value) is not bool:
                return _failed("Mute value must be true or false")
            return self.audio.set_mute(value)
        if action == "mic_mute":
            if type(value) is not bool:
                return _failed("Mute value must be true or false")
            return self.audio.set_mic_mute(value)
        if action == "charge_limit":
            return self.battery.set_charge_limit(value)
        if action == "hold_performance":
            return self._hold_performance(value)
        if action == "release_hold":
            if value is not None:
                return _failed("release_hold does not accept a value")
            return self.power.release_profile()
        if value is not None:
            return _failed("Power actions do not accept a value")
        return self.power_actions.perform(action, confirm=confirm is True)

    def _hold_performance(self, value: Any) -> dict[str, Any]:
        if value is None:
            return self.power.hold_performance()
        if isinstance(value, str):
            return self.power.hold_performance(value)
        if not isinstance(value, Mapping):
            return _failed("Performance hold value must be a mapping")
        allowed = {"reason", "application_id", "holder_name"}
        if set(value) - allowed:
            return _failed("Unknown performance hold field")
        return self.power.hold_performance(
            value.get("reason", "Game Mode performance session"),
            value.get("application_id", APP_ID),
            value.get("holder_name"),
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.power.release_profile()
        except Exception:
            pass
        try:
            atexit.unregister(self.close)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()


def _cli_percent(value: str) -> bool:
    if not re.fullmatch(r"[+-]?\d{1,3}", value):
        return False
    number = int(value)
    return (-100 <= number <= 100) if value[0] in "+-" else (0 <= number <= 100)


def cli(argv: list[str], qs: QuickSettings | None = None) -> tuple[int, str]:
    """Handle a Quick Settings command without starting the GTK runtime."""
    if not argv:
        return 2, QUICK_USAGE
    own = qs is None
    settings = qs or QuickSettings()
    command, rest = argv[0], argv[1:]
    try:
        if command == "status" and not rest:
            return 0, json.dumps(settings.snapshot(), ensure_ascii=False, sort_keys=True)
        if command == "profile" and len(rest) == 1 and rest[0] in PROFILE_LABELS:
            return _cli_result(settings.apply("profile", rest[0]))
        if command == "brightness" and len(rest) == 1 and _cli_percent(rest[0]):
            return _cli_result(settings.apply("brightness", rest[0]))
        if command == "volume" and len(rest) == 1:
            value = rest[0]
            if value in ("mute", "unmute"):
                return _cli_result(settings.apply("mute", value == "mute"))
            if _cli_percent(value):
                return _cli_result(settings.apply("volume", value))
        if command == "mic" and len(rest) == 1 and rest[0] in ("mute", "unmute"):
            return _cli_result(settings.apply("mic_mute", rest[0] == "mute"))
        return 2, QUICK_USAGE
    finally:
        if own:
            settings.close()


def _cli_result(result: Mapping[str, Any]) -> tuple[int, str]:
    ok = result.get("ok") is True
    detail = _detail(result.get("detail", "Unknown result"))
    return (0 if ok else 1), detail
