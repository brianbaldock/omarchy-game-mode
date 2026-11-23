"""The doctor's check plan: identity, wording and allowlisted evidence fields of every check.

Each entry states in one short line what a pass proves and what it does not, so
that a report cannot be read as more than it is. `fields` is the complete list of
evidence keys a check may emit; the runner drops anything else.
"""

from __future__ import annotations

from . import browser, console, firewall, inputstack, net, system, udp, wifi
from .context import Context, Options
from .model import Outcome, skip
from .runner import Check

SCOPES = ("streaming", "input", "system", "all")
CATEGORY_TITLES = {
    "system": "System", "network": "Network path", "udp": "Streaming transport (UDP)", "wifi": "Wi-Fi",
    "firewall": "Firewall", "clients": "Clients and video", "console": "Console",
    "unverified": "Not verifiable from this device", "input": "Controller",
}
CONSOLE_CHECKLIST = "the console checklist in the README (\"Xbox Remote Play: before you troubleshoot\")"


def _console_settings(_ctx: Context) -> Outcome:
    return skip("Not checked: remote features, the power mode, the NAT type and the signed-in profile can only be "
                "seen on the console itself.", f"Go through {CONSOLE_CHECKLIST}.")


def _failed_attempt(_ctx: Context) -> Outcome:
    return skip("Not observed: this run did not watch a real connection attempt, so it cannot say which step "
                "fails (sign-in, session start, connection setup or video).",
                "Reproduce the failure once in Chromium with chrome://webrtc-internals open in a second tab and "
                "note the last connection state it shows.")


SYSTEM = (
    Check("system.platform", "Platform", "system", system.check_platform,
          "Which OS, kernel and session this report describes.", "Anything about streaming or controllers.",
          ("os", "os_version", "kernel", "architecture", "omarchy", "hyprland", "session_type", "desktop",
           "game_mode", "python")),
    Check("system.device", "Device model", "system", system.check_device,
          "The model named by the firmware.", "That this unit behaves like the one that was tested.",
          ("vendor", "product", "family", "board", "bios", "handheld", "handheld_verified")),
    Check("system.gpu", "GPU and Mesa", "system", system.check_gpu,
          "Which GPU driver and Mesa version are in use.", "That video decode works (see hardware video decode).",
          ("gpus", "gpus.vendor", "gpus.id", "gpus.driver", "mesa")),
)

STREAMING = (
    Check("network.ipv4.default_route", "IPv4 default route", "network", net.check_default_route,
          "Packets have an IPv4 way out of this device.", "That the internet answers on it.",
          ("interface", "interface_type", "gateway", "source", "default_routes")),
    Check("network.ipv6.consistency", "IPv6 consistency", "network", net.check_ipv6,
          "IPv6 is either usable or cleanly absent.", "Which address family a stream ends up using.",
          ("ipv6_state", "global_addresses", "local_addresses", "default_route")),
    Check("network.vpn_proxy", "VPN and proxy", "network", net.check_vpn_proxy,
          "Whether a tunnel, policy routing or proxy variable is in the path.",
          "That stream media really takes that path.",
          ("tunnel_interfaces", "default_route_via_tunnel", "policy_rules", "policy_tables_with_default_route",
           "proxy_variables")),
    Check("dns.resolver", "DNS resolver", "network", net.check_resolver,
          "Which resolver answers this device's lookups.", "That it never blocks a name the doctor does not test.",
          ("mode", "servers", "servers.address", "servers.kind", "servers.provider", "filtering")),
    Check("dns.required", "Xbox service names", "network", net.check_dns_required,
          "The sign-in, Xbox and relay names resolve to routable addresses.",
          "That the regional streaming host picked at sign-in resolves, or that sign-in succeeds.",
          ("hosts", "hosts.host", "hosts.role", "hosts.resolved", "hosts.a_records", "hosts.aaaa_records",
           "hosts.error", "hosts.milliseconds", "resolved", "total")),
    Check("tls443.required", "TLS to Xbox services", "network", net.check_tls,
          "TCP 443 and an untampered TLS path to Microsoft exist.",
          "That the account, region or a stream session is accepted.",
          ("hosts", "hosts.host", "hosts.role", "hosts.ok", "hosts.stage", "hosts.error", "hosts.connect_ms",
           "hosts.handshake_ms", "hosts.tls_version", "completed", "total")),
    Check("host.clock", "System clock", "network", net.check_clock,
          "The clock is close enough for sign-in tokens and certificates.", "Anything about the account itself.",
          ("ntp_enabled", "ntp_synchronized", "offset_ms", "reference")),
    Check("stun.binding", "UDP and STUN", "udp", udp.check_stun_binding,
          "Outbound UDP works and replies come back.",
          "That the ports a real stream negotiates are open, or that the console is reachable.",
          ("servers", "servers.server", "servers.port", "servers.answered", "servers.error", "servers.rtt_ms",
           "servers.attempts", "servers.mapped_address", "servers.mapped_port", "answered", "total",
           "public_addresses")),
    Check("turn.challenge", "Microsoft relay (TURN)", "udp", udp.check_turn_challenge,
          "Microsoft's relay is reachable over UDP 3478.",
          "That a relay allocation with real Xbox credentials succeeds.",
          ("answered", "redirected", "rtt_ms", "error_code", "realm")),
    Check("nat.mapping_stability", "NAT mapping", "udp", udp.check_nat_mapping,
          "How this network maps one socket towards different servers.",
          "The console's NAT type, or any formal NAT classification.",
          ("verdict", "local_port", "mappings", "mappings.server", "mappings.mapped_address",
           "mappings.mapped_port", "address_translated", "port_preserved")),
    Check("wifi.link", "Wi-Fi link", "wifi", wifi.check_wifi_link,
          "Band, channel width and signal right now.", "Throughput or stability over a whole session.",
          ("interface", "band", "frequency_mhz", "channel", "width_mhz", "dfs", "signal_dbm", "rx_mbps", "tx_mbps",
           "generation")),
    Check("wifi.powersave", "Wi-Fi power saving", "wifi", wifi.check_wifi_powersave,
          "Whether the radio's power saving is on.", "That latency is low all the way to the console or cloud.",
          ("interface", "power_save")),
    Check("wifi.events", "Recent Wi-Fi events", "wifi", wifi.check_wifi_events,
          "Roams, disconnects and channel switches logged in the last 10 minutes.",
          "Older events, or ones the network daemon did not log.",
          ("window_minutes", "roams", "disconnects", "channel_switches")),
    Check("wifi.loss_gateway", "Local link loss", "wifi", net.check_gateway_loss,
          "Loss and delay between this device and the router right now.", "Loss beyond the router.",
          ("sent", "received", "loss_percent", "rtt_avg_ms", "rtt_max_ms"), timeout=11.0),
    Check("mtu.interface", "Interface MTU", "network", net.check_mtu_interface,
          "The configured MTU of the internet interface.", "The MTU of the whole path.", ("interface", "mtu")),
    Check("mtu.pmtu_probe", "Path MTU", "network", net.check_pmtu,
          "Full-size packets reach a public address unfragmented.",
          "The same for the path to a console or to Microsoft's servers.", ("passes_1228", "passes_1500"),
          timeout=11.0),
    Check("firewall.active_policy", "Host firewall", "firewall", firewall.check_active_policy,
          "Which firewall runs and what its saved policy allows.",
          "The live kernel rules, or what was dropped during an attempt.",
          ("active", "ufw_incoming", "ufw_outgoing", "ufw_rules_readable", "ufw_saved_rules", "ufw_outbound_blocks")),
    Check("firewall.rules", "Live firewall rules", "unverified", firewall.check_rules,
          "Nothing: the live rules can only be read with root.", "Whether a rule blocks stream traffic.",
          ("active",), decisive=True),
    Check("client.browsers", "Browsers", "clients", browser.check_browsers,
          "Which Chromium-family browsers are installed.", "That Microsoft supports them on Linux.",
          ("browsers", "browsers.name", "browsers.version", "browsers.source")),
    Check("client.greenlight_version", "Greenlight", "clients", browser.check_greenlight,
          "Whether the Linux Remote Play client is installed.", "That it is signed in or that its session works.",
          ("installed", "version")),
    Check("browser.support_policy", "Xbox web client support", "clients", browser.check_support_policy,
          "Microsoft's published browser list leaves out desktop Linux.",
          "Whether the web client happens to work today.", ("platform", "supported", "greenlight_installed")),
    Check("browser.flags", "Browser flags", "clients", browser.check_browser_flags,
          "Which launch flags the browser flag files add.",
          "Flags set inside the browser or by policy; the paths of loaded extensions are not shown.",
          ("files", "files.file", "files.flags", "concerns", "unpacked_extensions")),
    Check("video.vaapi", "Hardware video decode", "clients", browser.check_vaapi,
          "The GPU driver offers hardware H.264 decode.", "That the browser or Greenlight uses it for a stream.",
          ("drivers", "driver", "h264_high", "h264_any", "hevc", "av1", "decode_profiles")),
)

CONSOLE_ROUTE = Check(
    "console.route", "Route to the console", "console", console.check_console_route,
    "Whether the console's address is on this device's own subnet.", "That Remote Play works.",
    ("on_link", "routed", "interface"))
CONSOLE = (
    Check("console.reachability", "Console address", "console", console.check_console_reachability,
          "Whether a device answers at the address you gave.", "That it is the console, or that Remote Play works.",
          ("stun_udp_9002", "tcp_3074", "neighbour")),
    Check("manual.console_account_region", "Console settings and account", "unverified", _console_settings,
          "Nothing: these settings are only visible on the console.",
          "That remote features, power mode, NAT and profile are right.", decisive=True),
    # Extension point: a `capture` command will replace this marker with what it observed.
    Check("capture.failed_attempt", "Live failed attempt", "unverified", _failed_attempt,
          "Nothing: no connection attempt was observed.", "Which step of a real attempt fails.", decisive=True),
)

INPUT = (
    Check("input.gamepads", "Controllers", "input", inputstack.check_gamepads,
          "Which controls each controller can report, according to the kernel.",
          "That each physical button sends the code its label suggests.",
          ("gamepads", "gamepads.name", "gamepads.node", "gamepads.kind", "gamepads.bus", "gamepads.id",
           "gamepads.access", "gamepads.missing", "gamepads.guide")),
    Check("input.stack", "Input daemons", "input", inputstack.check_input_stack,
          "Which input daemons and mappers are running.", "That their profiles are right for your games.",
          ("inputplumber", "inputplumber_bus", "suspend_hook", "hhd", "antimicrox", "steam")),
    Check("input.handheld", "Handheld controller setup", "input", inputstack.check_handheld,
          "Whether a handheld daemon presents a complete controller.",
          "That games and Steam act on every button.", ("handheld", "verified", "daemon")),
    Check("input.hidraw-access", "Controller HID interfaces", "input", inputstack.check_hidraw_access,
          "Whether this user can read the built-in controller's raw HID interfaces.",
          "Which program changed their permissions, or that the controller is faulty.",
          ("interfaces", "interfaces.node", "interfaces.interface", "interfaces.mode", "interfaces.access",
           "hide_rules", "inputplumber", "hhd")),
)


def plan(options: Options) -> list[Check]:
    """Checks for a scope, in report order. Environment facts are part of every scope."""
    checks = list(SYSTEM)
    if options.scope in ("streaming", "all"):
        checks += STREAMING
        if options.console_ip:
            checks.append(CONSOLE_ROUTE)
        checks += CONSOLE
    if options.scope in ("input", "all"):
        checks += INPUT
    return checks
