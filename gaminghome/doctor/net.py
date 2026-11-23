"""Network path checks: routes, IPv6 state, VPN/proxy, DNS, TLS, clock, MTU and gateway loss.

Everything is read from /proc and /sys or asked of a tool with a timeout; the only
packets sent are DNS lookups, TCP+TLS handshakes to Microsoft's public service names
(no HTTP request, no credentials), one SNTP query and ICMP echo to the default
gateway and one public anycast address. Nothing here needs root.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import struct
import sys
import threading
from dataclasses import dataclass
from typing import Any

from .context import Context
from .model import Outcome, fail, info, ok, skip, warn

RTF_UP, RTF_REJECT = 0x0001, 0x0200
IFF_UP = 0x1
IFA_F_DADFAILED, IFA_F_TENTATIVE = 0x08, 0x40
ULA = ipaddress.IPv6Network("fc00::/7")
ARPHRD_ETHER = 1
INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
# VPN and tunnel interface names: WireGuard, OpenVPN, Tailscale, ZeroTier, PPP and common vendor clients.
TUNNEL_NAME = re.compile(r"^(tun|tap|wg|tailscale|zt|ppp|nordlynx|proton|pvpn|mullvad|ipsec|vti|vpn|cscotun|gpd)", re.I)
MAC_NAMED = re.compile(r"^((?:wl|en|ww)x)[0-9A-Fa-f]{12}$")
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")
STANDARD_RULES = {(0, "local"), (32766, "main"), (32767, "default")}
TABLE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

NTP_HOST = "time.cloudflare.com"
NTP_EPOCH_OFFSET = 2208988800
PMTU_TARGET = "1.1.1.1"


@dataclass(frozen=True)
class ServiceHost:
    name: str
    role: str
    https: bool = True


# Names the Xbox web client and Greenlight use before a stream starts. Regional
# streaming hosts are chosen at sign-in and are deliberately not guessed here.
REQUIRED_HOSTS = (
    ServiceHost("login.live.com", "sign-in"),
    ServiceHost("user.auth.xboxlive.com", "xbox-auth"),
    ServiceHost("xsts.auth.xboxlive.com", "xbox-auth"),
    ServiceHost("www.xbox.com", "web-client"),
    ServiceHost("xhome.gssv-play-prod.xboxlive.com", "streaming-service"),
    ServiceHost("gssv-play-prod.xboxlive.com", "streaming-service"),
    ServiceHost("relay.communication.microsoft.com", "relay", https=False),
)

# Public resolvers that filter content. A filter can sinkhole a gaming or streaming name,
# so the doctor names it; it is a heuristic lookup of well-known addresses, nothing more.
# Sources: Cloudflare "1.1.1.1 for Families" docs; AdGuard's public DNS provider list.
FILTERING_RESOLVERS = {
    "1.1.1.2": "Cloudflare for Families (malware filter)",
    "1.0.0.2": "Cloudflare for Families (malware filter)",
    "2606:4700:4700::1112": "Cloudflare for Families (malware filter)",
    "2606:4700:4700::1002": "Cloudflare for Families (malware filter)",
    "1.1.1.3": "Cloudflare for Families (malware and adult-content filter)",
    "1.0.0.3": "Cloudflare for Families (malware and adult-content filter)",
    "2606:4700:4700::1113": "Cloudflare for Families (malware and adult-content filter)",
    "2606:4700:4700::1003": "Cloudflare for Families (malware and adult-content filter)",
    "208.67.222.123": "OpenDNS FamilyShield",
    "208.67.220.123": "OpenDNS FamilyShield",
    "185.228.168.168": "CleanBrowsing Family filter",
    "185.228.169.168": "CleanBrowsing Family filter",
    "185.228.168.10": "CleanBrowsing Adult filter",
    "185.228.169.11": "CleanBrowsing Adult filter",
    "94.140.14.14": "AdGuard DNS (ads and trackers filter)",
    "94.140.15.15": "AdGuard DNS (ads and trackers filter)",
    "94.140.14.15": "AdGuard DNS Family Protection",
    "94.140.15.16": "AdGuard DNS Family Protection",
}
KNOWN_RESOLVERS = {
    "1.1.1.1": "Cloudflare", "1.0.0.1": "Cloudflare", "8.8.8.8": "Google", "8.8.4.4": "Google",
    "9.9.9.9": "Quad9", "149.112.112.112": "Quad9", "208.67.222.222": "OpenDNS", "208.67.220.220": "OpenDNS",
}


def display_interface(name: str) -> str:
    """Interface name safe to print: adapters named after their MAC lose the MAC."""
    return MAC_NAMED.sub(r"\1<mac>", name)


# ---- routes -------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    interface: str
    destination: int
    mask: int
    gateway: int
    metric: int

    @property
    def prefix(self) -> int:
        return bin(self.mask).count("1")

    @property
    def gateway_address(self) -> str:
        return str(ipaddress.IPv4Address(self.gateway)) if self.gateway else ""


def _kernel_ipv4(text: str) -> int:
    """/proc/net/route prints the raw 32-bit value, so the byte order is the host's."""
    return int.from_bytes(bytes.fromhex(text), sys.byteorder)


def ipv4_routes(ctx: Context) -> list[Route]:
    routes = []
    for line in (ctx.fs.read("/proc/net/route") or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            if not int(fields[3], 16) & RTF_UP:
                continue
            routes.append(Route(fields[0], _kernel_ipv4(fields[1]), _kernel_ipv4(fields[7]), _kernel_ipv4(fields[2]),
                                int(fields[6])))
        except ValueError:
            continue
    return routes


def default_routes(ctx: Context) -> list[Route]:
    return sorted((route for route in ipv4_routes(ctx) if route.mask == 0 and route.destination == 0),
                  key=lambda route: route.metric)


def route_for(routes: list[Route], address: str) -> Route | None:
    """Longest-prefix match, then lowest metric: what the kernel would pick from the main table."""
    target = int(ipaddress.IPv4Address(address))
    matches = [route for route in routes if target & route.mask == route.destination]
    return min(matches, key=lambda route: (-route.prefix, route.metric)) if matches else None


def interface_kind(ctx: Context, name: str) -> str:
    if ctx.fs.exists(f"/sys/class/net/{name}/wireless"):
        return "wifi"
    if name.startswith(("wwan", "rmnet")):
        return "cellular"
    if TUNNEL_NAME.match(name):
        return "tunnel"
    kind = ctx.fs.line(f"/sys/class/net/{name}/type")
    if kind and kind != str(ARPHRD_ETHER):
        return "tunnel"
    return "ethernet"


def local_ipv4(ctx: Context, destination: str) -> str:
    """Source address the kernel would use towards `destination`; connecting a UDP socket sends nothing."""
    try:
        sock = ctx.udp_socket()
    except OSError:
        return ""
    try:
        sock.connect((destination, 9))
        return str(sock.getsockname()[0])
    except OSError:
        return ""
    finally:
        sock.close()


def check_default_route(ctx: Context) -> Outcome:
    defaults = default_routes(ctx)
    if not defaults:
        return fail("No IPv4 default route: this device has no IPv4 path to the internet.",
                    "Reconnect Wi-Fi or Ethernet, then run the doctor again.", default_routes=0)
    best = defaults[0]
    kind = interface_kind(ctx, best.interface)
    source = local_ipv4(ctx, best.gateway_address) if best.gateway else ""
    detail = f"via {display_interface(best.interface)} ({kind})"
    if best.gateway:
        detail += f", gateway {best.gateway_address}"
    if source:
        detail += f", this device {source}"
    if len(defaults) > 1:
        detail += f"; {len(defaults)} default routes, the lowest metric wins"
    return ok(detail, interface=display_interface(best.interface), interface_type=kind,
              gateway=best.gateway_address, source=source, default_routes=len(defaults))


# ---- IPv6 ---------------------------------------------------------------------

def ipv6_state(ctx: Context) -> dict[str, Any]:
    global_count = local_count = 0
    for line in (ctx.fs.read("/proc/net/if_inet6") or "").splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[5] == "lo":
            continue
        try:
            address, flags = ipaddress.IPv6Address(int(fields[0], 16)), int(fields[4], 16)
        except ValueError:
            continue
        if flags & (IFA_F_TENTATIVE | IFA_F_DADFAILED):
            continue
        if address.is_global:
            global_count += 1
        elif address in ULA:
            local_count += 1
    default_route = False
    for line in (ctx.fs.read("/proc/net/ipv6_route") or "").splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[9] == "lo":
            continue
        try:
            flags, is_default = int(fields[8], 16), int(fields[0], 16) == 0 and int(fields[1], 16) == 0
        except ValueError:
            continue
        if is_default and flags & RTF_UP and not flags & RTF_REJECT:
            default_route = True
    if global_count and default_route:
        state = "dual-stack"
    elif not global_count and not default_route:
        state = "local-only" if local_count else "absent"
    else:
        state = "half-configured"
    return {"ipv6_state": state, "global_addresses": global_count, "local_addresses": local_count,
            "default_route": default_route}


def check_ipv6(ctx: Context) -> Outcome:
    state = ipv6_state(ctx)
    name = state["ipv6_state"]
    if name == "dual-stack":
        return ok("Dual-stack: a global IPv6 address and an IPv6 default route are both present.", **state)
    if name == "absent":
        return ok("IPv4-only: no global IPv6 address and no IPv6 default route, which is a consistent setup.", **state)
    if name == "local-only":
        return ok("IPv4-only for the internet: IPv6 has local (ULA) addresses but no global address and no "
                  "default route, which is consistent.", **state)
    half = ("a global IPv6 address but no IPv6 default route" if state["global_addresses"]
            else "an IPv6 default route but no global IPv6 address")
    return warn(f"Half-configured IPv6: {half}. Apps may try IPv6 first and stall before falling back to IPv4.",
                "Fix IPv6 on the router (router advertisements or DHCPv6), or turn IPv6 off for this connection "
                "consistently: `nmcli connection modify '<connection name>' ipv6.method disabled`", **state)


# ---- VPN, policy routing, proxy -------------------------------------------------

def _tunnel_interfaces(ctx: Context) -> list[str]:
    names = []
    for line in (ctx.fs.read("/proc/net/dev") or "").splitlines()[2:]:
        name = line.split(":", 1)[0].strip()
        if not INTERFACE_NAME.match(name) or name == "lo" or interface_kind(ctx, name) != "tunnel":
            continue
        try:
            up = int(ctx.fs.line(f"/sys/class/net/{name}/flags") or "0x1", 16) & IFF_UP
        except ValueError:
            up = True
        if up:
            names.append(name)
    return names


def parse_ip_rules(text: str) -> list[tuple[int, str]]:
    """(priority, table) for every `ip rule` line; selectors are not kept (they can hold addresses)."""
    rules = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+):.*\blookup\s+(\S+)", line)
        if match:
            rules.append((int(match.group(1)), match.group(2)))
    return rules


def _policy_routing(ctx: Context) -> tuple[int | None, list[str]]:
    """Number of non-standard routing rules, and the extra tables that carry a default route."""
    listed = ctx.command(["ip", "-4", "rule", "show"], 3)
    if not listed.ok:
        return None, []
    extra = [rule for rule in parse_ip_rules(listed.stdout) if rule not in STANDARD_RULES]
    tables = list(dict.fromkeys(table for _priority, table in extra
                                if table not in ("local", "main", "default") and TABLE_NAME.match(table)))
    full = []
    for table in tables[:4]:
        routes = ctx.command(["ip", "-4", "route", "show", "table", table], 3)
        if routes.ok and re.search(r"(?m)^(default|0\.0\.0\.0/1)\b", routes.stdout):
            full.append(table)
    return len(extra), full


def check_vpn_proxy(ctx: Context) -> Outcome:
    tunnels = [display_interface(name) for name in _tunnel_interfaces(ctx)]
    defaults = default_routes(ctx)
    via_tunnel = bool(defaults) and interface_kind(ctx, defaults[0].interface) == "tunnel"
    rules, full_tables = _policy_routing(ctx)
    proxies = sorted(name for name in (*PROXY_VARIABLES, *(v.upper() for v in PROXY_VARIABLES)) if ctx.env.get(name))
    evidence = dict(tunnel_interfaces=tunnels, default_route_via_tunnel=via_tunnel, policy_rules=rules,
                    policy_tables_with_default_route=full_tables, proxy_variables=proxies)
    problems = []
    if via_tunnel or full_tables:
        problems.append("internet traffic is routed through a VPN or tunnel"
                        + (f" ({', '.join(tunnels)})" if tunnels else ""))
    elif tunnels and rules is None:
        problems.append(f"a VPN or tunnel interface is up ({', '.join(tunnels)}) and policy routing could not be "
                        "read, so it may carry internet traffic")
    if proxies:
        problems.append(f"proxy variables are set ({', '.join(proxies)}; values not shown)")
    if problems:
        summary = "; ".join(problems)
        return warn(summary[0].upper() + summary[1:] + ". A VPN changes latency, MTU and NAT behaviour, and "
                    "stream media usually cannot use an HTTP proxy.",
                    "Retry once with the VPN disconnected and the proxy variables unset, to see whether the "
                    "result changes.", **evidence)
    note = f" ({', '.join(tunnels)} is up but does not carry internet traffic)" if tunnels else ""
    return ok(f"No VPN, tunnel or proxy in the default path{note}.", **evidence)


# ---- DNS ----------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    host: str
    addresses: tuple[str, ...] = ()
    error: str = ""
    milliseconds: int = 0

    @property
    def usable(self) -> tuple[str, ...]:
        """Routable answers only: a sinkhole (0.0.0.0, loopback, private) is not a working lookup."""
        return tuple(address for address in self.addresses if ipaddress.ip_address(address).is_global)


def resolve(ctx: Context, host: str, family: int = socket.AF_INET, timeout: float = 2.0) -> Resolution:
    """Look a name up with a deadline. getaddrinfo has none, so it runs on an abandonable thread."""
    box: list[Any] = []
    started = ctx.monotonic()

    def lookup() -> None:
        try:
            infos = ctx.getaddrinfo(host, None, family, socket.SOCK_DGRAM)
            box.append(tuple(dict.fromkeys(str(entry[4][0]) for entry in infos)))
        except socket.gaierror as exc:
            box.append("not-found" if exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)) else "failed")
        except OSError:
            box.append("failed")

    thread = threading.Thread(target=lookup, name="doctor-dns", daemon=True)
    thread.start()
    thread.join(timeout)
    elapsed = round((ctx.monotonic() - started) * 1000)
    if not box:
        return Resolution(host, error="timeout", milliseconds=elapsed)
    if isinstance(box[0], str):
        return Resolution(host, error=box[0], milliseconds=elapsed)
    return Resolution(host, box[0], milliseconds=elapsed)


def resolve_cached(ctx: Context, host: str, family: int = socket.AF_INET) -> Resolution:
    return ctx.once(f"dns:{family}:{host}", lambda: resolve(ctx, host, family))


def parse_resolv_conf(text: str) -> tuple[list[str], list[str]]:
    servers, domains = [], []
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            servers.append(fields[1].split("%", 1)[0])
        elif len(fields) >= 2 and fields[0] in ("search", "domain"):
            domains.extend(fields[1:])
    return servers, domains


def _server_address(token: str) -> str:
    """Strip resolvectl decorations: `addr#name`, `addr%iface`, `[addr]:port`, `addr:port`."""
    token = token.split("#", 1)[0].split("%", 1)[0]
    if token.startswith("["):
        token = token[1:].split("]", 1)[0]
    elif token.count(":") == 1:
        token = token.split(":", 1)[0]
    try:
        return ipaddress.ip_address(token).compressed
    except ValueError:
        return ""


def parse_resolvectl_status(text: str) -> tuple[list[str], list[str], list[str]]:
    """(link DNS servers, fallback servers, search domains) from `resolvectl status`."""
    values: dict[str, list[str]] = {}
    key = ""
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9. ]*?):\s+(.*)$", line)
        if match:
            key = match.group(1)
            values.setdefault(key, []).extend(match.group(2).split())
        elif line.strip() and key and line.startswith(" "):
            values[key].extend(line.split())
        else:
            key = ""

    def addresses(name: str) -> list[str]:
        return list(dict.fromkeys(filter(None, (_server_address(token) for token in values.get(name, [])))))

    servers = list(dict.fromkeys(addresses("Current DNS Server") + addresses("DNS Servers")))
    domains = [token.lstrip("~") for token in values.get("DNS Domain", []) if token.lstrip("~").strip(".")]
    return servers, addresses("Fallback DNS Servers"), domains


def classify_resolver(address: str) -> tuple[str, str]:
    """(kind, provider) for one resolver address; kind is filtering, public, lan or local."""
    if address in FILTERING_RESOLVERS:
        return "filtering", FILTERING_RESOLVERS[address]
    parsed = ipaddress.ip_address(address)
    if parsed.is_loopback:
        return "local", "a resolver on this device"
    if not parsed.is_global:
        return "lan", "the router or a resolver on the local network"
    return "public", KNOWN_RESOLVERS.get(address, "a public resolver")


def check_resolver(ctx: Context) -> Outcome:
    servers, domains = parse_resolv_conf(ctx.fs.read("/etc/resolv.conf") or "")
    mode = "direct"
    if any(server.startswith("127.0.0.5") for server in servers):
        mode = "systemd-resolved"
        status = ctx.command(["resolvectl", "status"], 4)
        if status.ok:
            upstream, fallback, link_domains = parse_resolvectl_status(status.stdout)
            domains += link_domains
            servers = upstream or fallback
        else:
            servers = []
    for domain in domains:
        ctx.secret(domain)
    rows = []
    for address in dict.fromkeys(servers):
        try:
            kind, provider = classify_resolver(address)
        except ValueError:
            continue
        rows.append({"address": address, "kind": kind, "provider": provider})
    if not rows:
        return skip("Could not identify the DNS resolver in use.", mode=mode, servers=[], filtering=False)
    providers = ", ".join(dict.fromkeys(row["provider"] for row in rows))
    filtering = [row for row in rows if row["kind"] == "filtering"]
    evidence = dict(mode=mode, servers=rows, filtering=bool(filtering))
    if filtering:
        return warn(f"DNS goes through a content-filtering resolver: {providers}. A filter can block or sinkhole "
                    "a gaming or streaming name that this doctor does not test.",
                    "Nothing to change while the service-name check passes. If a name fails, compare it with an "
                    "unfiltered resolver before changing DNS.", **evidence)
    if any(row["kind"] == "lan" for row in rows):
        providers += " (its upstream and any filtering cannot be seen from here)"
    return info(f"DNS resolver: {providers}.", **evidence)


def check_dns_required(ctx: Context) -> Outcome:
    def lookup(host: ServiceHost) -> dict[str, Any]:
        ipv4 = resolve_cached(ctx, host.name, socket.AF_INET)
        ipv6 = resolve_cached(ctx, host.name, socket.AF_INET6)
        error = ipv4.error or ("" if ipv4.usable else "non-routable-answer")
        return {"host": host.name, "role": host.role, "resolved": not error, "a_records": len(ipv4.addresses),
                "aaaa_records": len(ipv6.addresses), "error": error, "milliseconds": ipv4.milliseconds}

    rows = ctx.parallel(lookup, REQUIRED_HOSTS)
    failed = [row for row in rows if not row["resolved"]]
    evidence = dict(hosts=rows, resolved=len(rows) - len(failed), total=len(rows))
    if not failed:
        return ok(f"All {len(rows)} Xbox and Microsoft service names resolve.", **evidence)
    names = ", ".join(f"{row['host']} ({row['error']})" for row in failed)
    blocked = any(row["error"] == "non-routable-answer" for row in failed)
    hint = ("A non-routable answer means the resolver or a captive portal is blocking the name. " if blocked else "")
    remedy = (hint + "Check the connection and the resolver; compare with: `resolvectl query www.xbox.com`")
    streaming = [row for row in rows if row["role"] == "streaming-service"]
    if len(failed) == len(rows):
        return fail("No Xbox or Microsoft service name resolves: DNS is not working.", remedy, **evidence)
    if all(not row["resolved"] for row in streaming) or len({row["role"] for row in failed}) >= 3:
        return fail(f"{len(failed)} of {len(rows)} service names do not resolve: {names}.", remedy, **evidence)
    return warn(f"{len(failed)} of {len(rows)} service names do not resolve: {names}.", remedy, **evidence)


# ---- TLS ----------------------------------------------------------------------

def describe_os_error(exc: BaseException) -> str:
    """Short, address-free reason for a socket failure."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, ConnectionResetError):
        return "reset"
    reason = getattr(exc, "strerror", None)
    return str(reason).lower() if reason else type(exc).__name__


def tls_probe(ctx: Context, host: str, address: str, port: int = 443, timeout: float = 4.0) -> dict[str, Any]:
    """TCP connect then a verified TLS handshake for `host`; no request is sent."""
    row: dict[str, Any] = {"host": host, "ok": False, "stage": "connect", "error": "", "connect_ms": None,
                           "handshake_ms": None, "tls_version": ""}
    started = ctx.monotonic()
    try:
        sock = ctx.tcp_connect((address, port), timeout)
    except OSError as exc:
        row["error"] = describe_os_error(exc)
        return row
    connected = ctx.monotonic()
    row["connect_ms"] = round((connected - started) * 1000)
    try:
        sock.settimeout(max(0.5, timeout - (connected - started)))
        secure = ctx.tls_context().wrap_socket(sock, server_hostname=host)
    except ssl.SSLCertVerificationError as exc:
        sock.close()
        row.update(stage="certificate",
                   error=str(getattr(exc, "verify_message", "") or "certificate verification failed"))
        return row
    except (ssl.SSLError, OSError) as exc:
        sock.close()
        row.update(stage="tls", error=describe_os_error(exc))
        return row
    try:
        row.update(ok=True, stage="", handshake_ms=round((ctx.monotonic() - connected) * 1000),
                   tls_version=str(secure.version() or ""))
    finally:
        secure.close()
    return row


def check_tls(ctx: Context) -> Outcome:
    def probe(host: ServiceHost) -> dict[str, Any]:
        resolution = resolve_cached(ctx, host.name, socket.AF_INET)
        if not resolution.usable:
            return {"host": host.name, "role": host.role, "ok": False, "stage": "dns", "error": "no address",
                    "connect_ms": None, "handshake_ms": None, "tls_version": ""}
        return {**tls_probe(ctx, host.name, resolution.usable[0]), "role": host.role}

    rows = ctx.parallel(probe, [host for host in REQUIRED_HOSTS if host.https])
    failed = [row for row in rows if not row["ok"]]
    evidence = dict(hosts=rows, completed=len(rows) - len(failed), total=len(rows))
    if not failed:
        slowest = max((row["connect_ms"] or 0) + (row["handshake_ms"] or 0) for row in rows)
        return ok(f"All {len(rows)} TLS handshakes with Xbox and Microsoft services completed "
                  f"(slowest {slowest} ms).", **evidence)
    names = ", ".join(f"{row['host']} ({row['stage']}: {row['error']})" for row in failed)
    remedy = "Check for a captive portal, a filtering proxy or a firewall rule that blocks outbound TCP 443."
    if any(row["stage"] == "certificate" for row in failed):
        remedy = ("A certificate failure means something between this device and Microsoft is intercepting TLS "
                  "(captive portal, filtering proxy, security appliance), or the clock is wrong. " + remedy)
    streaming_down = all(not row["ok"] for row in rows if row["role"] == "streaming-service")
    summary = f"{len(failed)} of {len(rows)} TLS handshakes failed: {names}."
    return (fail if len(failed) == len(rows) or streaming_down else warn)(summary, remedy, **evidence)


# ---- clock --------------------------------------------------------------------

def _ntp_timestamp(seconds: float) -> bytes:
    whole = int(seconds)
    return struct.pack("!II", (whole + NTP_EPOCH_OFFSET) & 0xFFFFFFFF, int((seconds - whole) * 2 ** 32) & 0xFFFFFFFF)


def _from_ntp(data: bytes) -> float:
    whole, fraction = struct.unpack("!II", data)
    if whole < 0x80000000:  # NTP era 1, after February 2036
        whole += 2 ** 32
    return whole - NTP_EPOCH_OFFSET + fraction / 2 ** 32


def sntp_offset(ctx: Context, address: str, timeout: float = 2.0) -> float | None:
    """Seconds this clock is behind (+) or ahead (-) of an NTP server, from one SNTP exchange."""
    try:
        sock = ctx.udp_socket()
    except OSError:
        return None
    try:
        sent = ctx.wall_time()
        transmit = _ntp_timestamp(sent)
        sock.settimeout(timeout)
        sock.sendto(b"\x23" + bytes(39) + transmit, (address, 123))
        data, _source = sock.recvfrom(512)
        received = ctx.wall_time()
    except OSError:
        return None
    finally:
        sock.close()
    # Mode 4 (server), a real stratum, and our own timestamp echoed back.
    if len(data) < 48 or data[0] & 0x07 != 4 or data[1] == 0 or data[24:32] != transmit:
        return None
    return ((_from_ntp(data[32:40]) - sent) + (_from_ntp(data[40:48]) - received)) / 2


def _time_sync_state(ctx: Context) -> dict[str, bool]:
    shown = ctx.command(["timedatectl", "show", "-p", "NTP", "-p", "NTPSynchronized"], 3)
    if not shown.ok:
        return {}
    pairs = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    return {key: pairs[key].strip() == "yes" for key in ("NTP", "NTPSynchronized") if key in pairs}


def check_clock(ctx: Context) -> Outcome:
    state = _time_sync_state(ctx)
    resolution = resolve_cached(ctx, NTP_HOST, socket.AF_INET)
    offset = sntp_offset(ctx, resolution.usable[0]) if resolution.usable else None
    synced = state.get("NTPSynchronized")
    evidence = dict(ntp_enabled=state.get("NTP"), ntp_synchronized=synced,
                    offset_ms=None if offset is None else round(offset * 1000), reference=NTP_HOST)
    fix = "Turn on time synchronisation: `sudo timedatectl set-ntp true`"
    if offset is None and synced is None:
        return skip("Could not read the time-sync state or reach an NTP server.", **evidence)
    if offset is not None and abs(offset) > 30:
        return fail(f"The system clock is off by about {abs(offset):.0f} s. Sign-in tokens and TLS certificates "
                    "depend on the correct time.", fix, **evidence)
    if offset is not None and abs(offset) > 5:
        return warn(f"The system clock is off by about {abs(offset):.0f} s.", fix, **evidence)
    measured = "not measured" if offset is None else f"within {max(abs(offset), 0.1):.1f} s of {NTP_HOST}"
    if synced is False:
        return warn(f"The clock is not being synchronised by NTP (offset {measured}).", fix, **evidence)
    if offset is None:
        return ok("NTP synchronisation is active (the offset was not measured: no answer from the NTP server).",
                  **evidence)
    return ok(f"The clock is {measured}" + (" and NTP synchronisation is active." if synced else "."), **evidence)


# ---- ping: gateway loss and path MTU --------------------------------------------

def parse_ping(text: str) -> dict[str, Any] | None:
    counts = re.search(r"(\d+) packets transmitted, (\d+)(?: packets)? received", text)
    if not counts:
        return None
    sent, received = int(counts.group(1)), int(counts.group(2))
    timing = re.search(r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = ([\d.]+)/([\d.]+)/([\d.]+)", text)
    return {"sent": sent, "received": received,
            "loss_percent": round(100 * (sent - received) / sent, 1) if sent else 100.0,
            "rtt_avg_ms": float(timing.group(2)) if timing else None,
            "rtt_max_ms": float(timing.group(3)) if timing else None}


def check_gateway_loss(ctx: Context) -> Outcome:
    defaults = default_routes(ctx)
    if not defaults or not defaults[0].gateway:
        return skip("No default gateway to probe.")
    result = ctx.command(["ping", "-4", "-n", "-q", "-c", "20", "-i", "0.2", "-W", "1", "-w", "7",
                          defaults[0].gateway_address], 9)
    if result.error == "not-found":
        return skip("The ping tool is not installed.")
    stats = parse_ping(result.stdout)
    if stats is None:
        return skip("The gateway probe did not produce a result.")
    if not stats["received"]:
        return skip("The gateway does not answer ping, so local loss could not be measured.", **stats)
    summary = (f"{stats['received']}/{stats['sent']} replies from the gateway, average {stats['rtt_avg_ms']} ms, "
               f"worst {stats['rtt_max_ms']} ms")
    slow = (stats["rtt_max_ms"] or 0) >= 100 or (stats["rtt_avg_ms"] or 0) >= 30
    if stats["received"] < stats["sent"] or slow:
        return warn(f"Unsteady local link: {summary}. Loss or spikes this close to the device show up as stutter "
                    "and dropped streams.",
                    "Move closer to the access point, prefer 5 GHz or 6 GHz, or use wired Ethernet.", **stats)
    return ok(f"Steady local link: {summary}.", **stats)


def check_mtu_interface(ctx: Context) -> Outcome:
    defaults = default_routes(ctx)
    if not defaults:
        return skip("No default route, so there is no interface MTU to judge.")
    name = defaults[0].interface
    try:
        mtu = int(ctx.fs.line(f"/sys/class/net/{name}/mtu"))
    except ValueError:
        return skip("Could not read the interface MTU.")
    shown = display_interface(name)
    if mtu < 1400:
        return warn(f"MTU on {shown} is {mtu}, unusually low. Large stream packets may be fragmented or dropped.",
                    "A low MTU usually comes from a VPN or tunnel. Fix it there instead of lowering it further.",
                    interface=shown, mtu=mtu)
    return ok(f"MTU on {shown} is {mtu}.", interface=shown, mtu=mtu)


def _ping_size(ctx: Context, size: int) -> bool | None:
    result = ctx.command(["ping", "-4", "-n", "-q", "-c", "2", "-i", "0.2", "-W", "1", "-w", "3", "-M", "do",
                          "-s", str(size), PMTU_TARGET], 5)
    if result.error:
        return None
    stats = parse_ping(result.stdout)
    return bool(stats and stats["received"])


def check_pmtu(ctx: Context) -> Outcome:
    small = _ping_size(ctx, 1200)
    if small is None:
        return skip("The ping tool is not installed or did not run.")
    if not small:
        return skip(f"{PMTU_TARGET} does not answer ping from here, so the path MTU could not be probed.")
    if _ping_size(ctx, 1472):
        return ok("Full-size 1500-byte packets pass without fragmentation.", passes_1228=True, passes_1500=True)
    return warn("1228-byte packets pass but full-size 1500-byte packets do not: something on the path has a "
                "smaller MTU and may silently drop large stream packets.",
                "This usually points at a VPN, a tunnel or PPPoE. Fix the MTU on that link; do not lower the "
                "Wi-Fi MTU by guesswork.", passes_1228=True, passes_1500=False)
