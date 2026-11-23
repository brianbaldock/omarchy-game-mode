"""Redaction applied to every doctor result, and again to the rendered output.

The doctor is built so that private values never enter a report in the first place
(checks emit only allowlisted evidence fields; see `runner.allowlisted`). This module
is the second and third line of defence: `Redactor.data`/`text` run over every field
of the report, and `text` runs once more over the final text, JSON or Markdown.

Never printed, whatever the options: MAC addresses (so BSSIDs), the home directory,
user and host names, e-mail addresses, credential-looking keys and values, and any
literal a check registered with `add_secret` (the Wi-Fi SSID, DNS search domains).

Addresses, unless `--no-redact` asked for them in full:
- public IPv4/IPv6 become opaque tokens (`<public-ip-1>`), stable within one report
  so that "the same mapped address twice" is still visible;
- private IPv4 keeps its first two octets (`192.168.x.x`); private IPv6 is `<lan-ipv6>`;
- loopback, unspecified, broadcast and multicast addresses identify nobody and stay.

`text` never raises: if redaction itself fails, the whole value is replaced, so a bug
here cannot turn into a leak.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from typing import Any, Mapping

REDACTED = "<redacted>"
UNCLASSIFIED = "<redacted-unclassified>"
MASKED_MAC = "<mac>"
LAN_IPV6 = "<lan-ipv6>"

# Exact names from the streaming research, plus any key ending in a credential word.
_SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "setcookie", "accesstoken", "refreshtoken", "idtoken", "gstoken",
    "gamestreamtoken", "xststoken", "token", "usertoken", "clientsecret", "password", "code", "state",
})
_SENSITIVE_SUFFIXES = ("token", "secret", "password", "passwd", "cookie")

# A user name, host name, SSID or search domain that is also an ordinary word in doctor
# output would mangle the report without hiding anything identifying. Such values are
# still never emitted on purpose, and paths are covered by the home-directory rule.
_COMMON_WORDS = frozenset({
    "admin", "ally", "arch", "archlinux", "check", "corp", "deck", "desktop", "doctor", "fail", "game", "gamer",
    "games", "guest", "home", "host", "info", "internal", "lan", "linux", "local", "localdomain", "localhost",
    "network", "omarchy", "pass", "root", "skip", "steam", "steamdeck", "system", "test", "user", "warn",
    "wifi", "wlan", "xbox",
})

_HEADER = re.compile(r"(?im)\b(authorization|set-cookie|cookie)\b(\s*[:=]\s*)[^\r\n]+")
_KEY_VALUE = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:token|secret|password|passwd))\b(\"?'?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;\"']+)")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_URL_QUERY = re.compile(
    r"(?i)([?&](?:access_token|id_token|refresh_token|token|code|state|session|client_id|sig|signature|key)=)[^&\s#]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_XUID = re.compile(r"(?<!\d)253[35]\d{12}(?!\d)")

_MAC = re.compile(r"(?i)(?<![0-9a-f:-])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f:-])"
                  r"|(?<![0-9a-f:-])(?:[0-9a-f]{2}-){5}[0-9a-f]{2}(?![0-9a-f:-])")
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d)(?!\.\d)")
# One left-to-right pass so tokens are numbered in reading order: either a maximal run of
# address characters holding at least two colons (an IPv6 candidate, validated by
# ipaddress) or an IPv4 address.
_ADDRESS = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f.]*(?::[0-9A-Fa-f.]*){2,}(?![0-9A-Za-z])|" + _IPV4.pattern)
_OTHER_HOME = re.compile(r"/home/[^/\s\"':,;)]+")


def _sensitive_key(key: object) -> bool:
    name = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return name in _SENSITIVE_KEYS or name.endswith(_SENSITIVE_SUFFIXES)


def _word(literal: str) -> re.Pattern[str]:
    return re.compile(rf"(?i)(?<![A-Za-z0-9_]){re.escape(literal)}(?![A-Za-z0-9_])")


def _identifying(name: str) -> bool:
    return len(name) >= 3 and name.lower() not in _COMMON_WORDS


class Redactor:
    def __init__(self, *, home: str = "", user: str = "", host: str = "", mask_addresses: bool = True):
        self.mask_addresses = mask_addresses
        self._home = home.rstrip("/") if home not in ("", "/") else ""
        self._user = _word(user) if _identifying(user) else None
        self._host = _word(host) if _identifying(host) else None
        self._secrets: dict[str, tuple[re.Pattern[str], str]] = {}
        self._public: dict[str, str] = {}
        self._lock = threading.Lock()

    def add_secret(self, value: str, label: str = REDACTED) -> None:
        """Register a literal (SSID, search domain) that must never be printed."""
        value = value.strip()
        if _identifying(value):
            with self._lock:
                self._secrets[value] = (_word(value), label)

    def text(self, value: str) -> str:
        try:
            return self._text(value)
        except Exception:  # noqa: BLE001 - a redaction bug must not become a leak
            return UNCLASSIFIED

    def data(self, value: Any) -> Any:
        """Redact a JSON-like structure; values under credential-looking keys are dropped."""
        if isinstance(value, Mapping):
            return {self.text(str(key)): (REDACTED if _sensitive_key(key) else self.data(item))
                    for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.data(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.text(str(value))

    def _text(self, value: str) -> str:
        if not value:
            return value
        with self._lock:
            secrets = sorted(self._secrets.items(), key=lambda item: -len(item[0]))
        for _literal, (pattern, label) in secrets:
            value = pattern.sub(label, value)
        value = _HEADER.sub(rf"\1\2{REDACTED}", value)
        value = _KEY_VALUE.sub(rf"\1\2{REDACTED}", value)
        value = _BEARER.sub(f"Bearer {REDACTED}", value)
        value = _JWT.sub(REDACTED, value)
        value = _URL_USERINFO.sub(rf"\1{REDACTED}@", value)
        value = _URL_QUERY.sub(rf"\1{REDACTED}", value)
        value = _EMAIL.sub("<email-redacted>", value)
        value = _XUID.sub("<xuid-redacted>", value)
        if self._home:
            value = re.sub(re.escape(self._home) + r"(?=$|[/\s\"':,;)])", "~", value)
        value = _OTHER_HOME.sub("/home/<user>", value)
        value = _MAC.sub(MASKED_MAC, value)
        value = _ADDRESS.sub(self._address, value)
        if self._user:
            value = self._user.sub("<user>", value)
        if self._host:
            value = self._host.sub("<host>", value)
        return value

    def _token(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
        key = address.compressed
        with self._lock:
            return self._public.setdefault(key, f"<public-ip-{len(self._public) + 1}>")

    def _ipv4(self, match: re.Match[str]) -> str:
        octets = [int(part) for part in match.groups()]
        if any(octet > 255 for octet in octets):
            return match.group(0)
        address = ipaddress.IPv4Address(".".join(str(octet) for octet in octets))
        if (not self.mask_addresses or address.is_loopback or address.is_unspecified or address.is_multicast
                or int(address) == 0xFFFFFFFF):
            return match.group(0)
        if address.is_global:
            return self._token(address)
        return f"{octets[0]}.{octets[1]}.x.x"

    def _address(self, match: re.Match[str]) -> str:
        token = match.group(0)
        if ":" not in token:
            return self._ipv4(match)
        masked = self._ipv6(token)
        # Not IPv6 after all (a time, "host:port:"): addresses inside the run still count.
        return _IPV4.sub(self._ipv4, token) if masked is None else masked

    def _ipv6(self, token: str) -> str | None:
        # Sentence punctuation can trail the address; "fe80::" itself is valid, so try as-is first.
        for candidate in (token, token.rstrip("."), token.rstrip(".").removesuffix(":")):
            try:
                address = ipaddress.IPv6Address(candidate)
            except ValueError:
                continue
            if not self.mask_addresses or address.is_loopback or address.is_unspecified:
                return token
            masked = self._token(address) if address.is_global else LAN_IPV6
            return masked + token[len(candidate):]
        return None
