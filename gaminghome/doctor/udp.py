"""UDP reachability for streaming: anonymous STUN, the TURN challenge and NAT mapping behaviour.

Xbox Remote Play and cloud gaming carry media over UDP negotiated with ICE. These
checks send the same first packets a WebRTC client would, to Microsoft's relay and
two public STUN servers, and stop there: no credentials, no allocation, no media.
Microsoft's relay answers the TURN request with a redirect to a regional relay; that
one redirect is followed, and only to a public unicast IPv4 address on UDP 3478.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Sequence

from . import stun
from .context import Context
from .model import Outcome, fail, ok, skip, warn
from .net import local_ipv4, public_unicast, resolve_cached


@dataclass(frozen=True)
class StunServer:
    name: str
    host: str
    port: int


MICROSOFT_RELAY = StunServer("Microsoft relay", "relay.communication.microsoft.com", 3478)
STUN_SERVERS = (
    MICROSOFT_RELAY,
    StunServer("Cloudflare", "stun.cloudflare.com", 3478),
    StunServer("Google", "stun.l.google.com", 19302),
)
# The one port a Microsoft relay redirect may point to: TURN over UDP.
TURN_PORTS = frozenset({3478})
UDP_BLOCKED_FIX = ("Look for a VPN, a captive portal, or a router or firewall rule that blocks outbound UDP "
                   "(ports 3478 and 19302 here).")


@dataclass(frozen=True)
class StunReply:
    message: stun.Message
    rtt_ms: int
    attempts: int


def stun_exchange(ctx: Context, sock: Any, destination: tuple[str, int], request: bytes, transaction_id: bytes,
                  *, timeout: float = 1.2, attempts: int = 2) -> StunReply | None:
    """Send one request (retransmitting once) and wait for the reply to this exact transaction."""
    for attempt in range(1, attempts + 1):
        sent = ctx.monotonic()
        try:
            sock.sendto(request, destination)
        except OSError:
            return None
        while True:
            remaining = sent + timeout - ctx.monotonic()
            if remaining <= 0:
                break
            try:
                sock.settimeout(remaining)
                data, source = sock.recvfrom(stun.MAX_DATAGRAM)
            except OSError:
                break
            if tuple(source[:2]) != destination:
                continue
            try:
                message = stun.decode(data)
            except stun.StunError:
                continue
            if message.transaction_id == transaction_id:
                return StunReply(message, round((ctx.monotonic() - sent) * 1000), attempt)
    return None


def _addresses(ctx: Context, server: StunServer) -> tuple[str, ...]:
    return resolve_cached(ctx, server.host, socket.AF_INET).usable[:2]


def binding(ctx: Context, sock: Any, address: str, port: int) -> tuple[StunReply, tuple[str, int]] | None:
    """Anonymous Binding on an existing socket: the reply and the public address it reports."""
    transaction_id = stun.new_transaction_id()
    reply = stun_exchange(ctx, sock, (address, port), stun.binding_request(transaction_id), transaction_id)
    if reply is None or reply.message.method != stun.BINDING or reply.message.cls != stun.SUCCESS:
        return None
    try:
        mapped = stun.mapped_address(reply.message)
    except stun.StunError:
        return None
    return (reply, mapped) if mapped else None


def _probe_server(ctx: Context, server: StunServer) -> dict[str, Any]:
    row: dict[str, Any] = {"server": server.name, "port": server.port, "answered": False, "error": "",
                           "rtt_ms": None, "attempts": None, "mapped_address": "", "mapped_port": None}
    addresses = _addresses(ctx, server)
    if not addresses:
        row["error"] = "name did not resolve"
        return row
    for address in addresses:
        try:
            sock = ctx.udp_socket()
        except OSError:
            break
        try:
            answer = binding(ctx, sock, address, server.port)
        finally:
            sock.close()
        if answer:
            reply, (mapped_address, mapped_port) = answer
            row.update(answered=True, rtt_ms=reply.rtt_ms, attempts=reply.attempts, mapped_address=mapped_address,
                       mapped_port=mapped_port)
            return row
    row["error"] = "no reply"
    return row


def check_stun_binding(ctx: Context) -> Outcome:
    rows = ctx.parallel(lambda server: _probe_server(ctx, server), STUN_SERVERS)
    answered = [row for row in rows if row["answered"]]
    evidence = dict(servers=rows, answered=len(answered), total=len(rows),
                    public_addresses=len({row["mapped_address"] for row in answered}))
    if not answered:
        if all(row["error"] == "name did not resolve" for row in rows):
            return skip("No STUN server name resolved, so UDP could not be tested (see the DNS checks).", **evidence)
        return fail("No STUN server answered: outbound UDP looks blocked, so a stream cannot be set up.",
                    UDP_BLOCKED_FIX, **evidence)
    timing = ", ".join(f"{row['server']} {row['rtt_ms']} ms" for row in answered)
    public = answered[0]["mapped_address"]
    if len(answered) == len(rows):
        return ok(f"All {len(rows)} STUN servers answered ({timing}); public address {public}.", **evidence)
    silent = ", ".join(f"{row['server']} ({row['error']})" for row in rows if not row["answered"])
    relay_down = not rows[0]["answered"]
    note = " Microsoft's relay is the one Xbox streaming uses." if relay_down else ""
    return warn(f"{len(answered)} of {len(rows)} STUN servers answered ({timing}); no answer from {silent}.{note}",
                UDP_BLOCKED_FIX, **evidence)


def _allocate(ctx: Context, address: str, port: int) -> StunReply | None:
    """One TURN Allocate without credentials; the answer of interest is the error it comes back with."""
    try:
        sock = ctx.udp_socket()
    except OSError:
        return None
    try:
        transaction_id = stun.new_transaction_id()
        reply = stun_exchange(ctx, sock, (address, port), stun.allocate_request(transaction_id), transaction_id)
    finally:
        sock.close()
    return reply if reply is not None and reply.message.method == stun.ALLOCATE else None


def _regional_relay(message: stun.Message) -> tuple[str, int] | None:
    """The alternate server of a 300 answer, accepted only as a public unicast IPv4 address on the TURN port.

    The redirect comes from the network, so it is not trusted to point anywhere else: never into
    private, carrier-grade NAT, loopback, link-local, multicast or reserved space.
    """
    try:
        alternate = stun.alternate_server(message)
    except stun.StunError:
        return None
    if alternate is None:
        return None
    address, port = alternate
    public_ipv4 = ipaddress.ip_address(address).version == 4 and public_unicast(address)
    return alternate if public_ipv4 and port in TURN_PORTS else None


def check_turn_challenge(ctx: Context) -> Outcome:
    server = MICROSOFT_RELAY
    addresses = _addresses(ctx, server)
    if not addresses:
        return skip("The relay name did not resolve, so TURN could not be tested (see the DNS checks).",
                    answered=False)
    for address in addresses:
        reply = _allocate(ctx, address, server.port)
        if reply is None:
            continue
        error = stun.error_code(reply.message)
        redirected = False
        # Microsoft's anycast front door answers 300 and names a regional relay; ask that one once.
        if error and error[0] == 300 and (regional := _regional_relay(reply.message)):
            redirected = True
            second = _allocate(ctx, *regional)
            if second is None:
                return warn("Microsoft's relay answered and pointed to a regional relay, but that one did not "
                            "answer on UDP 3478.", UDP_BLOCKED_FIX, answered=True, redirected=True,
                            rtt_ms=reply.rtt_ms, error_code=300, challenge=bool(stun.realm(reply.message)))
            reply, error = second, stun.error_code(second.message)
        # Whether the answer carries a realm is a fact about the relay; the realm text itself is never reported.
        evidence = dict(answered=True, redirected=redirected, rtt_ms=reply.rtt_ms,
                        error_code=error[0] if error else None, challenge=bool(stun.realm(reply.message)))
        via = "regional relay (after the expected redirect)" if redirected else "relay"
        if error and error[0] == 401:
            return ok(f"Microsoft's {via} answered the unauthenticated TURN request with the expected 401 "
                      f"challenge ({reply.rtt_ms} ms).", **evidence)
        answer = f"error {error[0]}" if error else "an unexpected reply"
        return warn(f"Microsoft's {via} is reachable but answered the TURN request with {answer} instead of "
                    "the usual 401 challenge.", "Run the doctor again later; the relay may be busy.", **evidence)
    return fail("Microsoft's relay did not answer a TURN request on UDP 3478, so relayed streaming cannot work "
                "from this network.", UDP_BLOCKED_FIX, answered=False)


def classify_mapping(rounds: Sequence[Sequence[tuple[str, int] | None]]) -> str:
    """Compare the public address:port one socket got from several servers, asked twice.

    `rounds[r][s]` is what server `s` reported in round `r` (None: no answer).
    Returns `endpoint-independent`, `endpoint-dependent`, `unstable` or `inconclusive`.
    """
    first = rounds[0] if rounds else ()
    seen = [mapping for mapping in first if mapping is not None]
    if len(seen) < 2:
        return "inconclusive"
    if len(set(seen)) > 1:
        return "endpoint-dependent"
    for later in rounds[1:]:
        if any(before is not None and after is not None and before != after for before, after in zip(first, later)):
            return "unstable"
    return "endpoint-independent"


def check_nat_mapping(ctx: Context) -> Outcome:
    targets = [(server, addresses[0]) for server in STUN_SERVERS if (addresses := _addresses(ctx, server))]
    if len(targets) < 2:
        return skip("Fewer than two STUN servers resolved, so NAT mapping could not be compared.",
                    verdict="inconclusive")
    try:
        sock = ctx.udp_socket()
    except OSError:
        return skip("Could not open a UDP socket.", verdict="inconclusive")
    rounds: list[list[tuple[str, int] | None]] = []
    try:
        sock.bind(("", 0))
        local_port = int(sock.getsockname()[1])
        for _round in range(2):
            answers = []
            for server, address in targets:
                answer = binding(ctx, sock, address, server.port)
                answers.append(answer[1] if answer else None)
            rounds.append(answers)
    except OSError:
        return skip("The UDP socket failed before the comparison finished.", verdict="inconclusive")
    finally:
        sock.close()
    verdict = classify_mapping(rounds)
    mappings = [{"server": server.name, "mapped_address": mapping[0] if mapping else "",
                 "mapped_port": mapping[1] if mapping else None}
                for (server, _address), mapping in zip(targets, rounds[0])]
    seen = [mapping for mapping in rounds[0] if mapping]
    translated = bool(seen) and seen[0][0] != local_ipv4(ctx, targets[0][1])
    evidence = dict(verdict=verdict, local_port=local_port, mappings=mappings, address_translated=translated,
                    port_preserved=bool(seen) and all(mapping[1] == local_port for mapping in seen))
    if verdict == "inconclusive":
        return skip("Fewer than two STUN servers answered from one socket, so NAT mapping could not be compared.",
                    **evidence)
    if verdict == "endpoint-independent":
        kind = "one public port for every destination" if translated else "no address translation at all"
        return ok(f"Endpoint-independent mapping: this socket kept {kind} "
                  f"({seen[0][0]}:{seen[0][1]}), so direct peer-to-peer paths are possible.", **evidence)
    shown = ", ".join(f"{row['server']} saw {row['mapped_address']}:{row['mapped_port']}" for row in mappings
                      if row["mapped_port"] is not None)
    what = ("the public port changes with the destination" if verdict == "endpoint-dependent"
            else "the public port changed between two requests to the same server")
    return warn(f"Endpoint-dependent mapping: {what} ({shown}). Often called symmetric NAT; direct paths to a "
                "console or cloud server are harder and may need a relay. This is an observation, not a formal "
                "NAT type.",
                "Expected on mobile, hotel and carrier-grade NAT networks. At home, check the router for a "
                "second NAT or a 'symmetric NAT' setting; the console's own NAT test is separate.", **evidence)
