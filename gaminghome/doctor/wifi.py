"""Wi-Fi link checks from `iw` and the network daemon's journal.

Only radio facts leave this module: band, channel, width, signal, bitrates, power
saving and counts of recent roam/disconnect/channel-switch events. The SSID and
BSSID are parsed solely to register them as never-print literals; journal lines are
counted and never quoted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .context import Context
from .model import Outcome, ok, skip, warn
from .net import INTERFACE_NAME, default_routes, shown_interface

# Radar-shared 5 GHz spectrum (U-NII-2A and U-NII-2C) under FCC and ETSI rules. The
# regulatory domain has the last word; this is the common case, stated as a heuristic.
DFS_RANGES_MHZ = ((5250, 5350), (5470, 5730))
# Lower edges of the 6 GHz band and the 5 GHz sub-bands: wider channels are blocks counted from there.
BLOCK_STARTS_MHZ = (5945, 5735, 5490, 5170)
WEAK_SIGNAL_DBM = -70
EVENT_WINDOW_MINUTES = 10
JOURNAL = ("journalctl", "-b", "-u", "NetworkManager", "-u", "iwd", "-u", "wpa_supplicant",
           f"--since=-{EVENT_WINDOW_MINUTES}min", "--no-pager", "-o", "short-unix", "-n", "2000")
JOURNAL_LINE = re.compile(r"^(\d+(?:\.\d+)?) \S+ [^:]+: (.*)$")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# One real-world event is often logged by two daemons, or twice; merge what falls this close together.
EVENT_MERGE_SECONDS = 3.0
EVENT_PATTERNS = {
    "roams": re.compile(r"supplicant interface state: completed -> (?:authenticating|associating|associated)"
                        r"|\broam", re.I),
    "disconnects": re.compile(r"CTRL-EVENT-DISCONNECTED|deauth|disassoc|beacon[- ]?loss|connection lost"
                              r"|supplicant interface state: \S+ -> disconnected", re.I),
    "channel_switches": re.compile(r"CTRL-EVENT-CHANNEL-SWITCH\b|channel switch (?:announce|complete|done)"
                                   r"|radar", re.I),
}


@dataclass(frozen=True)
class WifiLink:
    connected: bool = False
    frequency_mhz: int | None = None
    signal_dbm: int | None = None
    rx_mbps: float | None = None
    tx_mbps: float | None = None
    width_mhz: int | None = None
    generation: str = ""
    private: tuple[str, ...] = field(default=(), repr=False)


def channel_for(frequency: int) -> int | None:
    if frequency == 2484:
        return 14
    if 2412 <= frequency <= 2472:
        return (frequency - 2407) // 5
    if 5160 <= frequency <= 5885:
        return (frequency - 5000) // 5
    if 5955 <= frequency <= 7115:
        return (frequency - 5950) // 5
    return None


def band_for(frequency: int) -> str:
    if 2400 <= frequency < 2500:
        return "2.4 GHz"
    if 5150 <= frequency < 5900:
        return "5 GHz"
    if 5925 <= frequency <= 7125:
        return "6 GHz"
    return "unknown band"


def channel_span(frequency: int, width: int, center: int | None = None) -> tuple[int, int]:
    """Lower and upper edge in MHz of the whole channel, not just its 20 MHz primary."""
    if center:
        return center - width // 2, center + width // 2
    lower = frequency - 10
    for start in BLOCK_STARTS_MHZ:
        if lower >= start:
            block = start + (lower - start) // width * width
            return block, block + width
    return lower, lower + max(width, 20)


def uses_dfs(frequency: int, width: int, center: int | None = None) -> bool:
    """True when any part of the channel lies in radar-shared spectrum: 36 at 160 MHz reaches into it."""
    if band_for(frequency) != "5 GHz":
        return False
    low, high = channel_span(frequency, width, center)
    return any(low < end and high > start for start, end in DFS_RANGES_MHZ)


def _bitrate(line: str) -> tuple[float | None, int, str]:
    rate = re.search(r"([\d.]+) MBit/s", line)
    width = re.search(r"\b(\d+)(P80)?MHz", line)
    generation = next((label for token, label in (("EHT-", "Wi-Fi 7"), ("HE-", "Wi-Fi 6"), ("VHT-", "Wi-Fi 5"),
                                                  ("MCS", "Wi-Fi 4")) if token in line), "")
    megahertz = (160 if width.group(2) else int(width.group(1))) if width else 20
    return (float(rate.group(1)) if rate else None), megahertz, generation


def parse_link(text: str) -> WifiLink:
    """Parse `iw dev IF link`. SSID and BSSID end up only in `private`, for the redactor."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    if not first.startswith("Connected to"):
        return WifiLink()
    values: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        key, _colon, value = line.strip().partition(":")
        values.setdefault(key.strip().lower(), value.strip())
    rx = _bitrate(values.get("rx bitrate", ""))
    tx = _bitrate(values.get("tx bitrate", ""))
    frequency = re.match(r"\d+", values.get("freq", ""))
    signal = re.match(r"-?\d+", values.get("signal", ""))
    bssid = first.split()[2] if len(first.split()) > 2 else ""
    widths = [width for rate, width, _generation in (rx, tx) if rate is not None]
    return WifiLink(True, int(frequency.group()) if frequency else None, int(signal.group()) if signal else None,
                    rx[0], tx[0], max(widths) if widths else None, tx[2] or rx[2],
                    tuple(filter(None, (values.get("ssid", ""), bssid))))


def parse_info(text: str) -> dict[str, Any]:
    """Channel line of `iw dev IF info`, the authoritative width and centre frequency."""
    match = re.search(r"channel (\d+) \((\d+) MHz\), width: (\d+)(\+\d+)? MHz(?:.*?center1: (\d+) MHz)?", text)
    ssid = re.search(r"(?m)^\s*ssid (.+)$", text)
    info: dict[str, Any] = {"private": (ssid.group(1).strip(),) if ssid else ()}
    if match:
        info.update(channel=int(match.group(1)), frequency_mhz=int(match.group(2)),
                    width_mhz=160 if match.group(4) else int(match.group(3)),
                    center_mhz=int(match.group(5)) if match.group(5) else None)
    return info


def parse_power_save(text: str) -> bool | None:
    match = re.search(r"Power save:\s*(on|off)", text, re.I)
    return match.group(1).lower() == "on" if match else None


def _wireless_interfaces(ctx: Context) -> list[str]:
    return [name for name in ctx.fs.listdir("/sys/class/net")
            if INTERFACE_NAME.match(name) and ctx.fs.exists(f"/sys/class/net/{name}/wireless")]


def _probe(ctx: Context) -> dict[str, Any]:
    """Pick the Wi-Fi interface that matters and read its state once for all Wi-Fi checks."""
    names = _wireless_interfaces(ctx)
    wanted = ctx.options.interface
    if wanted and wanted not in names:
        return {"skip": "The interface given with --interface is not a wireless interface on this device."}
    if not names:
        return {"skip": "No wireless interface: Wi-Fi checks do not apply."}
    defaults = default_routes(ctx)
    if not wanted and defaults and defaults[0].interface not in names:
        return {"skip": "The internet connection does not use Wi-Fi, so Wi-Fi checks do not apply."}
    name = wanted or (defaults[0].interface if defaults else names[0])
    link_result = ctx.command(["iw", "dev", name, "link"], 4)
    if link_result.error == "not-found":
        return {"skip": "The `iw` tool is not installed, so the Wi-Fi link could not be read.",
                "fix": "Install the `iw` package to enable the Wi-Fi checks."}
    if not link_result.ok:
        return {"skip": "`iw` could not read the wireless link."}
    link = parse_link(link_result.stdout)
    info = parse_info(ctx.command(["iw", "dev", name, "info"], 4).stdout)
    for private in (*link.private, *info["private"]):
        ctx.secret(private)
    if not link.connected:
        return {"skip": "The wireless interface is not connected to an access point."}
    power = parse_power_save(ctx.command(["iw", "dev", name, "get", "power_save"], 4).stdout)
    return {"interface": name, "link": link, "info": info, "power_save": power}


def wifi_state(ctx: Context) -> dict[str, Any]:
    return ctx.once("wifi", lambda: _probe(ctx))


def _skipped(state: dict[str, Any]) -> Outcome:
    return skip(state["skip"], state.get("fix", ""))


def check_wifi_link(ctx: Context) -> Outcome:
    state = wifi_state(ctx)
    if "skip" in state:
        return _skipped(state)
    link, info = state["link"], state["info"]
    frequency = info.get("frequency_mhz") or link.frequency_mhz
    if frequency is None:
        return skip("`iw` did not report the frequency of the wireless link.")
    width = info.get("width_mhz") or link.width_mhz or 20
    band = band_for(frequency)
    dfs = uses_dfs(frequency, width, info.get("center_mhz"))
    evidence = dict(interface=shown_interface(ctx, state["interface"]), band=band, frequency_mhz=frequency,
                    channel=info.get("channel") or channel_for(frequency), width_mhz=width, dfs=dfs,
                    signal_dbm=link.signal_dbm, rx_mbps=link.rx_mbps, tx_mbps=link.tx_mbps, generation=link.generation)
    if evidence["generation"] == "Wi-Fi 6" and band == "6 GHz":
        evidence["generation"] = "Wi-Fi 6E"
    parts = [f"{band} channel {evidence['channel']}", f"{width} MHz wide{' (DFS spectrum)' if dfs else ''}"]
    if link.signal_dbm is not None:
        parts.append(f"signal {link.signal_dbm} dBm")
    if link.rx_mbps is not None and link.tx_mbps is not None:
        parts.append(f"{link.rx_mbps:g} Mbit/s down / {link.tx_mbps:g} Mbit/s up")
    summary = ", ".join(parts) + (f" ({evidence['generation']})" if evidence["generation"] else "")
    concerns, fixes = [], []
    if band == "2.4 GHz":
        concerns.append("the 2.4 GHz band is crowded and slow for game streaming")
        fixes.append("Connect to the 5 GHz or 6 GHz network, or use wired Ethernet.")
    if link.signal_dbm is not None and link.signal_dbm <= WEAK_SIGNAL_DBM:
        concerns.append("the signal is weak")
        fixes.append("Move closer to the access point.")
    if dfs and width >= 160:
        concerns.append("a 160 MHz channel on radar-shared (DFS) spectrum can be forced to switch channel, which "
                        "interrupts a stream; it does not explain a stream that never connects")
        fixes.append("If streams stutter or drop, test the access point on a fixed non-DFS channel (36-48) at "
                     "80 MHz.")
    if concerns:
        return warn(f"{summary}. Note: {'; '.join(concerns)}.", " ".join(fixes), **evidence)
    return ok(f"{summary}.", **evidence)


def check_wifi_powersave(ctx: Context) -> Outcome:
    state = wifi_state(ctx)
    if "skip" in state:
        return _skipped(state)
    interface = shown_interface(ctx, state["interface"])
    if state["power_save"] is None:
        return skip("`iw` did not report the Wi-Fi power-save state.", interface=interface)
    if state["power_save"]:
        return warn("Wi-Fi power saving is on: the radio sleeps between packets, which adds latency spikes to "
                    "a stream.",
                    f"Turn it off until the next reconnect: `sudo iw dev {interface} set power_save off`. With "
                    "NetworkManager, `wifi.powersave = 2` in a conf.d file makes it permanent.",
                    interface=interface, power_save=True)
    return ok("Wi-Fi power saving is off.", interface=interface, power_save=False)


def count_events(journal: str) -> dict[str, int]:
    """Count roams, disconnects and channel switches in `journalctl -o short-unix` output."""
    counts = {}
    for name, pattern in EVENT_PATTERNS.items():
        last, total = None, 0
        for line in journal.splitlines():
            entry = JOURNAL_LINE.match(line)
            if not entry:
                continue
            moment = float(entry.group(1))
            # Network names are quoted in these logs; drop them so a name cannot look like an event.
            if pattern.search(QUOTED.sub("", entry.group(2))):
                if last is None or moment - last > EVENT_MERGE_SECONDS:
                    total += 1
                last = moment
        counts[name] = total
    return counts


def check_wifi_events(ctx: Context) -> Outcome:
    state = wifi_state(ctx)
    if "skip" in state:
        return _skipped(state)
    result = ctx.command(JOURNAL, 6)
    if result.error == "not-found":
        return skip("journalctl is not available, so recent Wi-Fi events could not be read.")
    if not result.ok or "not seeing messages from other users" in result.stderr:
        return skip("The system journal is not readable by this user, so recent Wi-Fi events could not be read.",
                    "Members of the `systemd-journal` or `wheel` group can read it.")
    counts = count_events(result.stdout)
    evidence = dict(window_minutes=EVENT_WINDOW_MINUTES, **counts)
    if not any(counts.values()):
        return ok(f"No Wi-Fi roam, disconnect or channel switch in the last {EVENT_WINDOW_MINUTES} minutes.",
                  **evidence)
    found = ", ".join(f"{count} {label}" for count, label in (
        (counts["roams"], "roam(s) between access points or bands"), (counts["disconnects"], "disconnect(s)"),
        (counts["channel_switches"], "channel switch(es)")) if count)
    return warn(f"In the last {EVENT_WINDOW_MINUTES} minutes: {found}. Each one interrupts the connection for a "
                "moment and can end a stream or a connection attempt.",
                "If this lines up with failed attempts, stay close to one access point, or give the 5 GHz and "
                "6 GHz networks separate names so the device stops moving between them.", **evidence)
