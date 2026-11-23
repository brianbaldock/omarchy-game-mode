"""Environment facts for a report: operating system, session, device model and GPU.

Everything comes from /etc/os-release, /proc, /sys and two version commands. The
host name, serial numbers and UUIDs are never read.
"""

from __future__ import annotations

import platform
import re
from typing import Any

from .. import VERSION
from .context import Context
from .devices import dmi, known_handheld
from .model import Outcome, info, skip

GPU_VENDORS = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}


def os_release(ctx: Context) -> dict[str, str]:
    values = {}
    for line in (ctx.fs.read("/etc/os-release") or "").splitlines():
        key, equals, value = line.partition("=")
        if equals:
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def check_platform(ctx: Context) -> Outcome:
    release = os_release(ctx)
    evidence: dict[str, Any] = dict(
        os=release.get("PRETTY_NAME") or release.get("NAME") or "Linux",
        os_version=release.get("VERSION_ID") or release.get("BUILD_ID") or "",
        kernel=ctx.fs.line("/proc/sys/kernel/osrelease"),
        architecture=ctx.fs.line("/proc/sys/kernel/arch") or platform.machine(),
        omarchy=_first_match(r"(?m)^(\d[\w.+~-]*)\s*$", ctx.command(["omarchy", "version"], 5).stdout),
        hyprland=_first_match(r"Hyprland v?(\d[\w.]*)", ctx.command(["hyprctl", "version"], 5).stdout),
        session_type=str(ctx.env.get("XDG_SESSION_TYPE") or ""),
        desktop=str(ctx.env.get("XDG_CURRENT_DESKTOP") or ""),
        game_mode=VERSION, python=platform.python_version())
    parts = [f"{evidence['os']} {evidence['os_version']}".strip(),
             f"Linux {evidence['kernel']} {evidence['architecture']}".strip()]
    if evidence["omarchy"] and evidence["omarchy"].split("-")[0] != evidence["os_version"]:
        parts.append(f"Omarchy {evidence['omarchy']}")
    if evidence["hyprland"]:
        parts.append(f"Hyprland {evidence['hyprland']}")
    elif evidence["desktop"]:
        parts.append(evidence["desktop"])
    if evidence["session_type"]:
        parts.append(f"{evidence['session_type']} session")
    parts.append(f"Game Mode {VERSION}")
    return info(", ".join(parts) + ".", **evidence)


def check_device(ctx: Context) -> Outcome:
    values = dmi(ctx)
    if not any(values.values()):
        return skip("This machine does not expose DMI model information.")
    handheld = known_handheld(values)
    vendor = values["sys_vendor"] or values["board_vendor"]
    model = " ".join(filter(None, (vendor, values["product_name"] or values["product_family"])))
    extras = ", ".join(filter(None, (f"board {values['board_name']}" if values["board_name"] else "",
                                      f"BIOS {values['bios_version']}" if values["bios_version"] else "")))
    known = ""
    if handheld:
        known = f" Known handheld: {handheld.name}" + ("." if handheld.verified else
                                                       " (listed by InputPlumber, not tested by this project).")
    return info(f"{model or 'Unknown model'}{f' ({extras})' if extras else ''}.{known}",
                vendor=vendor, product=values["product_name"], family=values["product_family"],
                board=values["board_name"], bios=values["bios_version"],
                handheld=handheld.name if handheld else "", handheld_verified=bool(handheld and handheld.verified))


def _mesa_version(ctx: Context) -> str:
    listed = ctx.command(["pacman", "-Q", "mesa"], 5).stdout
    version = _first_match(r"(?m)^mesa (?:\d+:)?(\d+(?:\.\d+)+)", listed)
    if version:
        return version
    for directory in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu"):
        for name in ctx.fs.listdir(directory):
            version = _first_match(r"^libgallium-(\d+(?:\.\d+)+)", name)
            if version:
                return version
    return ""


def check_gpu(ctx: Context) -> Outcome:
    gpus = []
    for card in ctx.fs.listdir("/sys/class/drm"):
        if not re.fullmatch(r"card\d+", card):
            continue
        base = f"/sys/class/drm/{card}/device"
        vendor, device = ctx.fs.line(f"{base}/vendor").lower(), ctx.fs.line(f"{base}/device").lower()
        if not re.fullmatch(r"0x[0-9a-f]{4}", vendor) or not re.fullmatch(r"0x[0-9a-f]{4}", device):
            continue
        driver = ctx.fs.resolved(f"{base}/driver").rsplit("/", 1)[-1] if ctx.fs.exists(f"{base}/driver") else ""
        gpus.append({"vendor": GPU_VENDORS.get(vendor, vendor), "id": f"{vendor[2:]}:{device[2:]}",
                     "driver": driver if re.fullmatch(r"[\w-]{1,32}", driver) else ""})
    mesa = _mesa_version(ctx)
    if not gpus:
        return skip("No GPU was found under /sys/class/drm.", gpus=[], mesa=mesa)
    shown = "; ".join(f"{gpu['vendor']} {gpu['id']}" + (f" ({gpu['driver']})" if gpu["driver"] else "")
                      for gpu in gpus)
    return info(f"{shown}{f', Mesa {mesa}' if mesa else ''}.", gpus=gpus, mesa=mesa)
