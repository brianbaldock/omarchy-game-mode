"""Streaming clients and video decode: which browsers and Greenlight are present, what
the browser flag files change, and whether VA-API offers hardware H.264 decode.

Browsers are only asked for `--version`. Greenlight is an Electron app that would
open its window, so it is never executed; its version comes from the package
database. Flag files are summarised by flag name: values that can hold a path, a
URL or anything personal are not printed. No browser profile is opened. `vainfo`
is pointed at a local GPU render node: through a display it could reach whatever
X11 server DISPLAY names, possibly on another machine.
"""

from __future__ import annotations

import re
from typing import Any

from .context import Context
from .model import Outcome, info, ok, skip, warn

BROWSERS = (
    ("Chromium", ("chromium", "chromium-browser")),
    ("Google Chrome", ("google-chrome-stable", "google-chrome")),
    ("Microsoft Edge", ("microsoft-edge-stable", "microsoft-edge")),
)
FLAG_FILES = ("chromium-flags.conf", "chrome-flags.conf", "microsoft-edge-stable-flags.conf")
APPLICATION_DIRS = ("/usr/share/applications", "/usr/local/share/applications",
                    "/var/lib/flatpak/exports/share/applications")
USER_APPLICATION_DIRS = (".local/share/applications", ".local/share/flatpak/exports/share/applications")
DRI_DIRS = ("/usr/lib/dri", "/usr/lib64/dri", "/usr/lib/x86_64-linux-gnu/dri", "/usr/lib/aarch64-linux-gnu/dri")
RENDER_NODES = "/dev/dri"
RENDER_NODE = re.compile(r"renderD\d{1,4}")
VERSION = re.compile(r"\d+(?:\.\d+){1,3}")
# Flags whose value is a plain setting worth showing. Every other value is withheld.
VALUE_FLAGS = frozenset({
    "--ozone-platform", "--ozone-platform-hint", "--enable-features", "--disable-features", "--password-store",
    "--use-gl", "--use-angle", "--use-vulkan", "--force-device-scale-factor", "--gtk-version",
    "--wayland-text-input-version", "--webrtc-ip-handling-policy", "--force-webrtc-ip-handling-policy",
})
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_,.+-]{1,200}$")
# Flags that change how a stream connects or decodes, and why that matters.
RISKY_FLAGS = {
    "--no-sandbox": "removes the browser sandbox; it is a testing switch, not a streaming fix",
    "--disable-gpu": "forces software rendering and video decode",
    "--disable-accelerated-video-decode": "turns hardware video decode off",
    "--proxy-server": "sends browser traffic through a proxy that stream media usually cannot use",
    "--disable-webrtc": "turns WebRTC off, which every Xbox stream needs",
}
UDP_BLOCKING_POLICY = "disable_non_proxied_udp"
SUPPORT_POLICY = ("Microsoft's supported-browser list for xbox.com/play and xbox.com/remoteplay does not include "
                  "desktop Linux, so the web client can fail here even when every network check passes.")


def _home(ctx: Context) -> str:
    return str(ctx.env.get("HOME") or "").rstrip("/")


def _config_dir(ctx: Context) -> str:
    return str(ctx.env.get("XDG_CONFIG_HOME") or "").rstrip("/") or (f"{_home(ctx)}/.config" if _home(ctx) else "")


def _desktop_entries(ctx: Context) -> list[str]:
    home = _home(ctx)
    dirs = [*APPLICATION_DIRS, *((f"{home}/{relative}" for relative in USER_APPLICATION_DIRS) if home else ())]
    return [name for directory in dirs for name in ctx.fs.listdir(directory) if name.endswith(".desktop")]


def check_browsers(ctx: Context) -> Outcome:
    found = []
    for label, binaries in BROWSERS:
        binary = next((name for name in binaries if ctx.which(name)), None)
        if binary is None:
            continue
        version = VERSION.search(ctx.command([binary, "--version"], 5).stdout)
        found.append({"name": label, "version": version.group() if version else "", "source": "system"})
    entries = " ".join(_desktop_entries(ctx)).lower()
    for label, flatpak_id in (("Chromium", "org.chromium.chromium"), ("Google Chrome", "com.google.chrome"),
                              ("Microsoft Edge", "com.microsoft.edge")):
        if flatpak_id in entries and not any(row["name"] == label for row in found):
            found.append({"name": label, "version": "", "source": "flatpak"})
    if not found:
        return skip("No Chromium-family browser (Chromium, Chrome, Edge) was found.", browsers=[])
    shown = ", ".join(f"{row['name']} {row['version']}".strip() + (" (Flatpak)" if row["source"] == "flatpak" else "")
                      for row in found)
    return info(f"{shown}.", browsers=found)


def greenlight_installed(ctx: Context) -> bool:
    return bool(ctx.which("greenlight") or ctx.fs.exists("/opt/Greenlight/greenlight-desktop")
                or any("greenlight" in name.lower() for name in _desktop_entries(ctx)))


def check_greenlight(ctx: Context) -> Outcome:
    if not greenlight_installed(ctx):
        return skip("Greenlight, the Xbox Remote Play client this launcher uses on Linux, is not installed.",
                    "Install Xbox Remote Play from Game Mode's Discover page.", installed=False, version="")
    listed = ctx.command(["pacman", "-Q", "greenlight-bin", "greenlight"], 5).stdout
    match = re.search(r"(?m)^greenlight(?:-bin)? (?:\d+:)?(\d[\w.]*)", listed)
    version = match.group(1) if match else ""
    return info(f"Greenlight {version} is installed." if version else "Greenlight is installed (version not read).",
                installed=True, version=version)


def check_support_policy(ctx: Context) -> Outcome:
    greenlight = greenlight_installed(ctx)
    advice = ("Use Greenlight for Remote Play on Linux" if greenlight else
              "Install Xbox Remote Play (Greenlight) from Game Mode's Discover page")
    return warn(SUPPORT_POLICY, f"{advice}, and compare with a supported device (a phone or a Windows PC) on the "
                "same network and account.", platform="linux", supported=False, greenlight_installed=greenlight)


def summarise_flags(text: str) -> tuple[list[str], list[str], int]:
    """(printable flags, concerns, unpacked-extension count) for one flags file."""
    shown, concerns, extensions = [], [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("--"):
            continue
        name, _equals, value = line.partition("=")
        name = name.split()[0]
        if name == "--load-extension":
            count = len([part for part in value.split(",") if part.strip()])
            extensions += count
            shown.append(f"{name}=<{count} path(s) not shown>")
        elif not value:
            shown.append(name)
        elif name in VALUE_FLAGS and SAFE_VALUE.match(value):
            shown.append(f"{name}={value}")
        else:
            shown.append(f"{name}=<value not shown>")
        if name in RISKY_FLAGS:
            concerns.append(f"{name} ({RISKY_FLAGS[name]})")
        elif name.endswith("webrtc-ip-handling-policy") and value == UDP_BLOCKING_POLICY:
            concerns.append(f"{name}={value} (stops WebRTC from using UDP, so a stream cannot connect)")
    return shown, concerns, extensions


def check_browser_flags(ctx: Context) -> Outcome:
    config = _config_dir(ctx)
    files, concerns, extensions = [], [], 0
    for name in FLAG_FILES if config else ():
        text = ctx.fs.read(f"{config}/{name}")
        if text is None:
            continue
        shown, risky, loaded = summarise_flags(text)
        if shown:
            files.append({"file": name, "flags": shown})
            concerns += [f"{item} in {name}" for item in risky]
            extensions += loaded
    evidence: dict[str, Any] = dict(files=files, concerns=concerns, unpacked_extensions=extensions)
    if not files:
        return info("No custom browser flag files.", **evidence)
    total = sum(len(row["flags"]) for row in files)
    where = ", ".join(row["file"] for row in files)
    note = (f" {extensions} unpacked extension(s) load into every browser window, including the Xbox web app."
            if extensions else "")
    if concerns:
        return warn(f"Browser flags that affect streaming: {'; '.join(concerns)}.{note}",
                    "Remove those flags unless you added them on purpose, then restart the browser.", **evidence)
    return info(f"{total} custom flag(s) in {where}; none is known to affect streaming.{note}", **evidence)


def parse_vainfo(text: str) -> dict[str, Any]:
    """Decode profiles (VAEntrypointVLD) and the driver line from `vainfo` output."""
    driver = re.search(r"Driver version:\s*(.+)", text)
    decode = set(re.findall(r"(VAProfile\w+)\s*:\s*VAEntrypointVLD", text))
    return {"driver": driver.group(1).strip()[:160] if driver else "",
            "h264_high": "VAProfileH264High" in decode,
            "h264_any": any(profile.startswith("VAProfileH264") for profile in decode),
            "hevc": any(profile.startswith("VAProfileHEVC") for profile in decode),
            "av1": any(profile.startswith("VAProfileAV1") for profile in decode),
            "decode_profiles": len(decode)}


def render_node(ctx: Context) -> str:
    """The first local GPU render node, as in `/dev/dri/renderD128`, or empty."""
    nodes = [name for name in ctx.fs.listdir(RENDER_NODES) if RENDER_NODE.fullmatch(name)]
    return f"{RENDER_NODES}/{min(nodes, key=lambda name: int(name[7:]))}" if nodes else ""


def check_vaapi(ctx: Context) -> Outcome:
    drivers = sorted({name[:-len("_drv_video.so")] for directory in DRI_DIRS for name in ctx.fs.listdir(directory)
                      if name.endswith("_drv_video.so")})
    node = render_node(ctx)
    if not node:
        return skip("No GPU render node (/dev/dri/renderD*) was found, so hardware video decode could not be "
                    "checked.", drivers=drivers)
    result = ctx.command(["vainfo", "--display", "drm", "--device", node], 5)
    if result.error == "not-found":
        if not drivers:
            return skip("No VA-API driver and no `vainfo` tool were found, so hardware video decode could not "
                        "be checked.", drivers=[])
        return skip(f"VA-API drivers are installed ({', '.join(drivers)}), but `vainfo` is not, so H.264 decode "
                    "could not be confirmed.", "Install libva-utils to let the doctor confirm it.", drivers=drivers)
    parsed = parse_vainfo(result.stdout + result.stderr)
    evidence = dict(drivers=drivers, **parsed)
    if not parsed["decode_profiles"]:
        return skip("`vainfo` did not report any decode profile, so hardware video decode could not be checked.",
                    **evidence)
    if parsed["h264_high"]:
        return ok(f"Hardware H.264 High decode is available through VA-API ({parsed['driver'] or 'driver unknown'}).",
                  **evidence)
    kind = "only below the High profile" if parsed["h264_any"] else "not at all"
    return warn(f"VA-API offers hardware H.264 decode {kind}. Xbox streams are H.264, so they may be decoded on "
                "the CPU, which costs battery and can stutter.",
                "Check that the VA-API driver for this GPU is installed (Mesa for AMD, intel-media-driver for "
                "Intel).", **evidence)
