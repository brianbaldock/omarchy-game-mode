"""STUN/TURN message codec (RFC 8489, RFC 8656): just enough for anonymous probes.

Pure functions, no sockets. The doctor sends only two kinds of request: an anonymous
Binding request, and an Allocate request without credentials whose expected answer
is the 401 challenge. It never authenticates and never retries with credentials.
"""

from __future__ import annotations

import ipaddress
import os
import struct
from dataclasses import dataclass
from typing import Iterable

MAGIC_COOKIE = 0x2112A442
HEADER = struct.Struct("!HHI12s")
MAX_DATAGRAM = 2048

BINDING, ALLOCATE = 0x001, 0x003
REQUEST, INDICATION, SUCCESS, ERROR = 0, 1, 2, 3

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_ERROR_CODE = 0x0009
ATTR_REALM = 0x0014
ATTR_NONCE = 0x0015
ATTR_REQUESTED_TRANSPORT = 0x0019
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_ALTERNATE_SERVER = 0x8023
TRANSPORT_UDP = 17
FAMILY_IPV4, FAMILY_IPV6 = 0x01, 0x02


class StunError(ValueError):
    """The datagram is not a well-formed STUN message."""


@dataclass(frozen=True)
class Message:
    method: int
    cls: int
    transaction_id: bytes
    attributes: tuple[tuple[int, bytes], ...] = ()

    def first(self, kind: int) -> bytes | None:
        return next((value for attribute, value in self.attributes if attribute == kind), None)


def new_transaction_id() -> bytes:
    return os.urandom(12)


def message_type(method: int, cls: int) -> int:
    """Interleave the 12 method bits with the two class bits (RFC 8489 section 5)."""
    return (((method & 0x0F80) << 2) | ((method & 0x0070) << 1) | (method & 0x000F)
            | ((cls & 0x2) << 7) | ((cls & 0x1) << 4))


def split_type(value: int) -> tuple[int, int]:
    method = ((value & 0x3E00) >> 2) | ((value & 0x00E0) >> 1) | (value & 0x000F)
    return method, ((value & 0x0100) >> 7) | ((value & 0x0010) >> 4)


def encode(method: int, cls: int, transaction_id: bytes, attributes: Iterable[tuple[int, bytes]] = ()) -> bytes:
    if len(transaction_id) != 12:
        raise StunError("transaction id must be 12 bytes")
    body = b"".join(struct.pack("!HH", kind, len(value)) + value + b"\x00" * (-len(value) % 4)
                    for kind, value in attributes)
    return HEADER.pack(message_type(method, cls), len(body), MAGIC_COOKIE, transaction_id) + body


def decode(data: bytes) -> Message:
    if len(data) < HEADER.size:
        raise StunError("shorter than a STUN header")
    kind, length, cookie, transaction_id = HEADER.unpack_from(data)
    if kind & 0xC000 or cookie != MAGIC_COOKIE:
        raise StunError("not a STUN message")
    if length % 4 or HEADER.size + length > len(data):
        raise StunError("length field does not match the datagram")
    attributes = []
    offset, end = HEADER.size, HEADER.size + length
    while offset < end:
        if offset + 4 > end:
            raise StunError("truncated attribute header")
        attribute, size = struct.unpack_from("!HH", data, offset)
        offset += 4
        if offset + size > end:
            raise StunError("truncated attribute value")
        attributes.append((attribute, data[offset:offset + size]))
        offset += size + (-size % 4)
    method, cls = split_type(kind)
    return Message(method, cls, transaction_id, tuple(attributes))


def binding_request(transaction_id: bytes) -> bytes:
    return encode(BINDING, REQUEST, transaction_id)


def allocate_request(transaction_id: bytes) -> bytes:
    """TURN Allocate asking for a UDP relay, deliberately without credentials."""
    return encode(ALLOCATE, REQUEST, transaction_id, [(ATTR_REQUESTED_TRANSPORT, bytes((TRANSPORT_UDP, 0, 0, 0)))])


def _xor_key(transaction_id: bytes) -> bytes:
    return struct.pack("!I", MAGIC_COOKIE) + transaction_id


def encode_xor_address(address: str, port: int, transaction_id: bytes) -> bytes:
    parsed = ipaddress.ip_address(address)
    family = FAMILY_IPV4 if parsed.version == 4 else FAMILY_IPV6
    masked = bytes(a ^ b for a, b in zip(parsed.packed, _xor_key(transaction_id)))
    return struct.pack("!BBH", 0, family, port ^ (MAGIC_COOKIE >> 16)) + masked


def decode_xor_address(value: bytes, transaction_id: bytes) -> tuple[str, int]:
    """XOR-MAPPED-ADDRESS: port XOR the cookie's top half; address XOR cookie (+ transaction id for IPv6)."""
    if len(value) < 4:
        raise StunError("truncated address attribute")
    size = {FAMILY_IPV4: 4, FAMILY_IPV6: 16}.get(value[1])
    if size is None or len(value) < 4 + size:
        raise StunError("unknown address family or truncated address")
    port = struct.unpack_from("!H", value, 2)[0] ^ (MAGIC_COOKIE >> 16)
    raw = bytes(a ^ b for a, b in zip(value[4:4 + size], _xor_key(transaction_id)))
    return str(ipaddress.ip_address(raw)), port


def decode_plain_address(value: bytes) -> tuple[str, int]:
    """MAPPED-ADDRESS, the unobfuscated form older servers still send."""
    if len(value) < 4:
        raise StunError("truncated address attribute")
    size = {FAMILY_IPV4: 4, FAMILY_IPV6: 16}.get(value[1])
    if size is None or len(value) < 4 + size:
        raise StunError("unknown address family or truncated address")
    return str(ipaddress.ip_address(value[4:4 + size])), struct.unpack_from("!H", value, 2)[0]


def mapped_address(message: Message) -> tuple[str, int] | None:
    """The public address and port the server saw this socket come from."""
    xored = message.first(ATTR_XOR_MAPPED_ADDRESS)
    if xored is not None:
        return decode_xor_address(xored, message.transaction_id)
    plain = message.first(ATTR_MAPPED_ADDRESS)
    return decode_plain_address(plain) if plain is not None else None


def encode_plain_address(address: str, port: int) -> bytes:
    parsed = ipaddress.ip_address(address)
    return struct.pack("!BBH", 0, FAMILY_IPV4 if parsed.version == 4 else FAMILY_IPV6, port) + parsed.packed


def alternate_server(message: Message) -> tuple[str, int] | None:
    """Where a `300 Try Alternate` answer says to ask instead."""
    value = message.first(ATTR_ALTERNATE_SERVER)
    return decode_plain_address(value) if value is not None else None


def encode_error_code(code: int, reason: str = "") -> bytes:
    return struct.pack("!HBB", 0, code // 100, code % 100) + reason.encode("utf-8")


def error_code(message: Message) -> tuple[int, str] | None:
    value = message.first(ATTR_ERROR_CODE)
    if value is None or len(value) < 4:
        return None
    return (value[2] & 0x07) * 100 + value[3], value[4:].decode("utf-8", "replace")


def realm(message: Message) -> str:
    value = message.first(ATTR_REALM)
    return value.decode("utf-8", "replace") if value else ""
