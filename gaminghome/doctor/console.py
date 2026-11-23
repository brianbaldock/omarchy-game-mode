"""Optional checks against one console address the user names with `--console-ip`.

Nothing here discovers anything: no broadcast, no multicast, no SmartGlass packets,
no scan. Only the single private IPv4 address given on the command line is probed,
with an anonymous STUN Binding on UDP 9002 (the port Remote Play uses on the
console) and one TCP connection attempt to 3074 (the Xbox network port), plus a
look at the kernel's neighbour table. A reply shows the address is alive and
reachable; it cannot prove that Remote Play works. A cached neighbour (ARP) entry
only shows that a device used the address recently, and silence proves nothing.
"""

from __future__ import annotations

import ipaddress

from .context import Context
from .model import Outcome, ok, skip, warn
from .net import default_routes, describe_os_error, ipv4_routes, local_ipv4, route_for, shown_interface
from .runner import check_cancelled, time_left
from .udp import binding

# RFC 1918 only: not CGNAT, not link-local, never a public address.
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
REMOTE_PLAY_UDP_PORT = 9002
XBOX_NETWORK_TCP_PORT = 3074
ATF_COMPLETE = 0x2
NO_PROOF = "This cannot prove that Remote Play works."
FIND_ADDRESS = "The console shows its address under Settings > General > Network settings > Advanced settings."


def private_ipv4(text: str) -> str:
    """Validate a console address: a single RFC 1918 unicast IPv4 address, nothing else."""
    try:
        address = ipaddress.IPv4Address(text.strip())
    except ValueError:
        raise ValueError("not an IPv4 address") from None
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("not a private (RFC 1918) address: use the console's address on your home network")
    return str(address)


def _on_link_problem(ctx: Context, address: str) -> str:
    """Why an on-link address cannot be a console: it is this device, or the subnet's own address."""
    route = route_for(ipv4_routes(ctx), address)
    if route is None or route.gateway:
        return ""
    target = int(ipaddress.IPv4Address(address))
    broadcast = route.destination | (~route.mask & 0xFFFFFFFF)
    if route.prefix < 31 and target in (route.destination, broadcast):
        return "That is the network or broadcast address of this subnet, not a device."
    if local_ipv4(ctx, address) == address:
        return "That address belongs to this device, not to a console."
    return ""


def check_console_route(ctx: Context) -> Outcome:
    address = ctx.options.console_ip
    route = route_for(ipv4_routes(ctx), address)
    if route is None:
        return warn("There is no route to the console address from this device.",
                    f"Check the address and that both devices are on the same network. {FIND_ADDRESS}",
                    on_link=False, routed=False)
    problem = _on_link_problem(ctx, address)
    if problem:
        return skip(problem, FIND_ADDRESS, on_link=True, routed=True)
    interface = shown_interface(ctx, route.interface)
    if not route.gateway:
        return ok(f"The console address is on this device's own subnet (reached directly on {interface}, with "
                  f"no router in between). {NO_PROOF}", on_link=True, routed=True, interface=interface)
    defaults = default_routes(ctx)
    via = "the default gateway" if defaults and defaults[0].gateway == route.gateway else "a router"
    return warn(f"The console address is on a different subnet: traffic to it goes through {via}. A direct "
                "local stream then depends on the router allowing traffic between the two networks, which guest "
                f"networks and VLANs often do not. {NO_PROOF}",
                "Put the console and this device on the same trusted network, or allow traffic between the two "
                "networks on the router.", on_link=False, routed=True, interface=interface)


def neighbour_state(ctx: Context, address: str) -> str:
    """`resolved` when the kernel's neighbour (ARP) table holds a link-layer address for `address`.

    The kernel keeps such an entry for a while after the device last answered, so it shows that
    a device used the address on this network recently, not that it is awake now.
    """
    for line in (ctx.fs.read("/proc/net/arp") or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[0] == address:
            try:
                return "resolved" if int(fields[2], 16) & ATF_COMPLETE else "unresolved"
            except ValueError:
                return "unresolved"
    return "absent"


def _tcp_answer(ctx: Context, address: str, port: int, timeout: float = 1.5) -> str:
    """`accepted` or `refused` both mean a live host answered; anything else is silence."""
    check_cancelled()
    try:
        ctx.tcp_connect((address, port), time_left(timeout)).close()
        return "accepted"
    except OSError as exc:
        check_cancelled()
        return describe_os_error(exc)


def check_console_reachability(ctx: Context) -> Outcome:
    address = ctx.options.console_ip
    if not address:
        return skip("Not tested: no console address was given.",
                    f"Run the doctor with `--console-ip ADDRESS` to probe the console. {FIND_ADDRESS}")
    problem = _on_link_problem(ctx, address)
    if problem:
        return skip(problem, FIND_ADDRESS)
    try:
        sock = ctx.udp_socket()
    except OSError:
        sock = None
    stun_reply = False
    if sock is not None:
        try:
            stun_reply = binding(ctx, sock, address, REMOTE_PLAY_UDP_PORT) is not None
        finally:
            sock.close()
    tcp = _tcp_answer(ctx, address, XBOX_NETWORK_TCP_PORT)
    route = route_for(ipv4_routes(ctx), address)
    on_link = route is not None and not route.gateway
    neighbour = neighbour_state(ctx, address) if on_link else "not-on-link"
    evidence = dict(stun_udp_9002=stun_reply, tcp_3074=tcp, neighbour=neighbour)
    answers = []
    if stun_reply:
        answers.append(f"it answered a STUN request on UDP {REMOTE_PLAY_UDP_PORT}, the port Remote Play uses")
    if tcp in ("accepted", "refused"):
        answers.append(f"it answered on TCP {XBOX_NETWORK_TCP_PORT} (connection {tcp})")
    cached = neighbour == "resolved"
    if answers:
        note = " The kernel also holds its link-layer (ARP) address." if cached else ""
        return ok(f"A device answered at the console address: {'; '.join(answers)}.{note} {NO_PROOF}", **evidence)
    fix = ("Check that the console is on, confirm its address, and try with both devices on the same non-guest "
           f"network. {FIND_ADDRESS}")
    silence = f"no reply on UDP {REMOTE_PLAY_UDP_PORT} or TCP {XBOX_NETWORK_TCP_PORT}"
    unproven = "No reply does not prove that streaming from the console is impossible."
    if cached:
        return warn(f"No answer from the console address ({silence}), although the kernel still holds a "
                    "link-layer (ARP) address for it: a device used the address on this network recently. A "
                    "cached entry does not show that the console is awake now; it may be asleep or off, or not "
                    f"answer these probes. {unproven}", fix, **evidence)
    if on_link:
        silence += ", and the kernel holds no link-layer (ARP) address for it"
    return warn(f"No answer from the console address: {silence}. The console may be off or in its "
                "energy-saving power mode, the address may be wrong, or the access point may keep wireless "
                f"devices apart (client isolation). {unproven}", fix, **evidence)
