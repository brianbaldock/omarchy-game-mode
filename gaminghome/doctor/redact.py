"""Redaction applied to every doctor result, and again to the rendered output.

The doctor is built so that private values never enter a report in the first place
(checks emit only allowlisted evidence fields; see `runner.allowlisted`). A list of
field names says nothing about what a value holds, though: a controller can be named
after its serial number. So this module scrubs values, whatever field they arrive in.
`Redactor.data`/`text` run over every string of a report and over everything the
button test prints, and `text` runs once more over the final text, JSON or Markdown.

Never printed, whatever the options:
- MAC addresses in any notation (so BSSIDs), UUIDs, and tokens that look like a serial
  number (eight or more letters and digits with at least one digit), unless the doctor
  vouched for them itself with `trust` (model names, versions, its own run id);
- three or more groups of two to four hex digits joined by `-`, `_`, `:` or `.`, even
  glued to a word (`Controller-02-AA-BB-CC-DD-EE`, `AB12_CD34_EF56`), apart from IP
  addresses, times, dates and short version numbers, and apart from groups inside a value
  the doctor vouched for with `trust` (a kernel release such as `6.12.12-200.fc41.x86_64`)
  where that value stands whole, unless they are shaped like a MAC address;
- the home directory, other users' homes and the per-user runtime and media
  directories, each together with everything below them;
- user and host names, e-mail addresses, and the user info, query and fragment of URLs;
- credential- and identity-looking keys with their values, and any literal a check
  registered with `add_secret` (the Wi-Fi SSID, DNS search domains).

Addresses, unless `--no-redact` asked for them in full:
- public IPv4/IPv6 become opaque tokens (`<public-ip-1>`), stable within one report
  so that "the same mapped address twice" is still visible;
- private IPv4 keeps its first two octets (`192.168.x.x`); private IPv6 is `<lan-ipv6>`.
  An IPv6 zone id (`%wlan0`) goes with its address, and an IPv4-mapped IPv6 address is
  treated as the IPv4 address it carries;
- loopback, unspecified, broadcast and multicast addresses identify nobody and stay.

Every rule is idempotent, and `text` never raises: if redaction itself fails, the
whole value is replaced, so a bug here cannot turn into a leak.
"""

from __future__ import annotations

import ipaddress
import re
import threading
from typing import Any, Mapping

REDACTED = "<redacted>"
UNCLASSIFIED = "<redacted-unclassified>"
MASKED_MAC = "<mac>"
MASKED_ID = "<id>"
MASKED_UUID = "<uuid>"
LAN_IPV6 = "<lan-ipv6>"
HOME_PATH = "<home-path>"
RUNTIME_PATH = "<runtime-path>"
MEDIA_PATH = "<media-path>"

# Exact names from the streaming research, names of values that identify a person, a
# network or a device, plus any key ending in a credential word.
_SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "setcookie", "accesstoken", "refreshtoken", "idtoken", "gstoken",
    "gamestreamtoken", "xststoken", "token", "usertoken", "clientsecret", "password", "code", "state",
    "ssid", "essid", "bssid", "serial", "serialnumber", "uniq", "phys", "hostname", "username", "gamertag", "xuid",
    "userhash", "uhs", "liveid", "consoleid", "consolename", "deviceid", "sessionid", "macaddress", "hwaddr",
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
# Every repetition below that could meet a long run of word characters is bounded, so that the
# final pass over a whole report stays linear in its length whatever a value looks like.
_KEY_VALUE = re.compile(
    r"(?i)([a-z0-9_-]{0,64}(?:token|secret|password|passwd))\b(\"?'?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;\"']+)")
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
# `serial`, `HID_UNIQ`, `console name` and the like, as a whole word of the key: the value goes, up to the
# end of the line or the next separator, because such values (a network name) can contain spaces.
_IDENTITY_WORDS = (r"serial(?:[ _-]?(?:number|no))?|s/n|uniq|e?ssid|bssid|gamertag|xuid|userhash|uhs|hostname|username"
                   r"|live[ _-]?id|console[ _-]?(?:id|name)|device[ _-]?id|session[ _-]?id")
_IDENTITY_VALUE = re.compile(
    rf"(?i)(?<![A-Za-z0-9])((?:{_IDENTITY_WORDS})(?:[_-][A-Za-z0-9]{{1,32}}){{0,3}})(\"?'?[ \t]*[:=][ \t]*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\n\r&;,|\"']+)")
# The scheme and what follows it; an already scrubbed query or fragment is part of the match so that
# scrubbing twice changes nothing.
_URL = re.compile(rf"(?i)(?<![A-Za-z0-9+.-])([a-z][a-z0-9+.-]{{1,15}})://((?:[^\s\"'<>`|\\]|{REDACTED})+)")
_QUERY = re.compile(r"(?<=[A-Za-z0-9/_.~-])\?[A-Za-z0-9_.~%\[\]-]+=[^\s\"'<>`|#]*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}")
_XUID = re.compile(r"(?<!\d)253[35]\d{12}(?!\d)")

# A private directory takes the rest of its line with it: file names contain spaces, and no doctor
# message puts such a path in the middle of a sentence on purpose.
# It must start a path, though: `/v5/sessions/home/play` is an API route, not a home directory.
_PATH_START = r"(?<![A-Za-z0-9_.~-])"
_PATH_TAIL = r"[^\n\r\t\"`|<>]*"
_PATH_END = r"(?=[/\s\"'`:,;)]|$)"
_HOME_DIRS = re.compile(rf"{_PATH_START}(?:(?:/var)?/home/|/root{_PATH_END}){_PATH_TAIL}")
_RUNTIME_DIRS = re.compile(rf"{_PATH_START}/run/user/\d+{_PATH_TAIL}")
_MEDIA_DIRS = re.compile(rf"{_PATH_START}(?:/run)?/media/{_PATH_TAIL}")

_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}(?![0-9A-Za-z])")
_HEX_PAIR = "[0-9A-Fa-f]{2}"
# Six pairs (or eight, EUI-64) joined by one of `:`, `-` or `.`; three dotted groups of four; twelve bare digits.
_MAC = re.compile(
    rf"(?<![0-9A-Za-z:.-]){_HEX_PAIR}([:.-]){_HEX_PAIR}(?:\1{_HEX_PAIR}){{4}}(?:(?:\1{_HEX_PAIR}){{2}})?"
    rf"(?![0-9A-Za-z])(?!\1[0-9A-Fa-f])"
    r"|(?<![0-9A-Za-z.])[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?![0-9A-Za-z])(?!\.[0-9A-Fa-f])"
    r"|(?<![0-9A-Za-z])[0-9A-Fa-f]{12}(?![0-9A-Za-z])")
_IPV4 = re.compile(r"(?<!\d)(?<!\d\.)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d)(?!\.\d)")
# One left-to-right pass so tokens are numbered in reading order: either a run of address
# characters holding two to eight colons (an IPv6 candidate, validated by ipaddress) with its
# zone id, or an IPv4 address. No valid IPv6 address has a longer group or more colons.
_ADDRESS = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f.]{0,16}(?::[0-9A-Fa-f.]{0,16}){2,8}(?![0-9A-Za-z])"
                      r"(?:%(?:25)?[0-9A-Za-z_.~-]{1,32})?|" + _IPV4.pattern)
# Hex groups joined into one token: a MAC address or an asset tag. The MAC rule above wants a clean
# start, so it misses one glued to a word by a joiner; this one only refuses to start inside a word.
_JOINED_HEX = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{2,4}(?:[-_:.][0-9A-Fa-f]{2,4}){2,15}(?![0-9A-Za-z])")
_JOINER = re.compile(r"[-_:.]")
ALIAS_KINDS = ("vpn", "if")
# What alias text is made of: a kind, a number or both (`vpn`, `vpn-2`, `12`). A name like that is
# generic, and replacing it with its alias would rewrite the aliases themselves (`vpn` in `vpn-1`).
_ALIAS_TEXT = re.compile(rf"(?i)(?:{'|'.join(ALIAS_KINDS)})(?:-\d+)?|\d+")
_VOUCHED_LIMIT = 64  # values kept for the rule above; a report vouches for a handful
_ADDRESS_CHARS = frozenset("0123456789abcdefABCDEF:.")
_SERIAL = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,}(?![A-Za-z0-9])")
# Kernel device nodes are numbered, not named after anything: `event100` is not a serial number.
_DEVICE_NODE = re.compile(r"(?:event|hidraw|input|js|mouse|card|renderD|controlD)\d{1,6}")


def _sensitive_key(key: object) -> bool:
    name = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return name in _SENSITIVE_KEYS or name.endswith(_SENSITIVE_SUFFIXES)


def _word(literal: str) -> re.Pattern[str]:
    return re.compile(rf"(?i)(?<![A-Za-z0-9_]){re.escape(literal)}(?![A-Za-z0-9_])")


def _identifying(name: str) -> bool:
    return len(name) >= 3 and name.lower() not in _COMMON_WORDS


def _inside_address(match: re.Match[str]) -> bool:
    """Whether the run of address characters around a match is an IP address (with a port, perhaps)."""
    text, start, end = match.string, match.start(), match.end()
    while start > 0 and text[start - 1] in _ADDRESS_CHARS and match.start() - start < 48:
        start -= 1
    while end < len(text) and text[end] in _ADDRESS_CHARS and end - match.end() < 48:
        end += 1
    run = text[start:end].strip(".")
    host, colon, port = run.rpartition(":")
    for candidate in (run, host if colon and port.isdigit() and "." in host else ""):
        try:
            ipaddress.ip_address(candidate)
            return True
        except ValueError:
            continue
    return False


def _standing_whole(text: str, literals: list[str]) -> list[tuple[int, int]]:
    """Where each literal stands in `text` as a whole, not run together with a letter or digit."""
    found = []
    for literal in literals:
        start = text.find(literal)
        while start >= 0:
            end = start + len(literal)
            if not (start and text[start - 1].isalnum()) and not (end < len(text) and text[end].isalnum()):
                found.append((start, end))
            start = text.find(literal, start + 1)
    return found


def _joined_hex(match: re.Match[str], vouched: list[tuple[int, int]] | None = None) -> str:
    token = match.group(0)
    groups = _JOINER.split(token)
    if len(groups) in (6, 8) and all(len(group) == 2 for group in groups):
        return MASKED_MAC
    # Times (`00:46:05`), dates and short version numbers: three groups of decimal digits.
    if len(groups) == 3 and "".join(groups).isdigit():
        return token
    for start, end in vouched or ():
        if start <= match.start() < end:
            # Kept up to the end of the vouched value; whatever runs on past it is judged on its own.
            cut = end - match.start()
            return token[:cut] + _JOINED_HEX.sub(_joined_hex, token[cut:])
    return token if _inside_address(match) else MASKED_ID


def _url(match: re.Match[str]) -> str:
    """`scheme://host/path`, with a marker where a query or a fragment was."""
    rest, hash_mark, _fragment = match.group(2).partition("#")
    rest, question_mark, _query = rest.partition("?")
    authority, slash, path = rest.partition("/")
    host = authority.rpartition("@")[2]
    return (f"{match.group(1)}://{host}{slash}{path}" + (f"?{REDACTED}" if question_mark else "")
            + (f"#{REDACTED}" if hash_mark else ""))


class Redactor:
    def __init__(self, *, home: str = "", user: str = "", host: str = "", mask_addresses: bool = True):
        self.mask_addresses = mask_addresses
        home = home.rstrip("/")
        self._home = re.compile(re.escape(home) + _PATH_END + _PATH_TAIL) if home else None
        self._user = _word(user) if _identifying(user) else None
        self._host = _word(host) if _identifying(host) else None
        self._secrets: dict[str, tuple[re.Pattern[str], str]] = {}
        self._trusted: set[str] = set()
        self._vouched: set[str] = set()
        self._public: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._lock = threading.Lock()

    def add_secret(self, value: str, label: str = REDACTED) -> None:
        """Register a literal (SSID, search domain) that must never be printed."""
        value = value.strip()
        if _identifying(value):
            with self._lock:
                self._secrets[value] = (_word(value), label)

    def alias(self, name: str, kind: str) -> str:
        """A stand-in such as `vpn-1` for a name someone chose, the same for the same name in one report.

        The name itself is registered as a secret with that alias as its label, so a copy of it
        anywhere else in the report is replaced as well. A name that alias text is made of
        (`vpn`, `vpn-1`, `1`) is not: it identifies nobody, and replacing it would rewrite every
        alias that contains it, again on every pass.
        """
        if kind not in ALIAS_KINDS:
            raise ValueError(f"unknown alias kind {kind!r}")
        with self._lock:
            shown = self._aliases.get(name)
            if shown is None:
                taken = sum(1 for value in self._aliases.values() if value.startswith(f"{kind}-"))
                shown = self._aliases[name] = f"{kind}-{taken + 1}"
                if _identifying(name) and not _ALIAS_TEXT.fullmatch(name):
                    self._secrets[name] = (_word(name), shown)
        return shown

    def trust(self, value: str) -> None:
        """Vouch for a value the doctor produced or validated itself.

        A board name, a version or the run id looks like a serial number to the rule above
        and would be masked, and a kernel release such as `6.12.12-200.fc41.x86_64` holds
        joined hex groups. Its long tokens are kept wherever they appear; its joined hex
        groups only where the whole value stands, so the same groups next to it, or a MAC
        address, are still masked. Only ever call this with such values, never with
        free-form input: a device name, tool output or anything the user typed.
        """
        whole = str(value).strip()
        tokens = _SERIAL.findall(whole)
        vouched = bool(_JOINED_HEX.search(whole)) and len(whole) <= 200
        if tokens or vouched:
            with self._lock:
                self._trusted.update(tokens)
                if vouched and len(self._vouched) < _VOUCHED_LIMIT:
                    self._vouched.add(whole)

    def text(self, value: str) -> str:
        try:
            return self._text(value)
        except Exception:  # noqa: BLE001 - a redaction bug must not become a leak
            return UNCLASSIFIED

    def data(self, value: Any) -> Any:
        """Redact a JSON-like structure; values under credential- or identity-looking keys are dropped."""
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
            trusted = frozenset(self._trusted)
            vouched = sorted(self._vouched, key=len, reverse=True)
        for _literal, (pattern, label) in secrets:
            value = pattern.sub(label, value)
        value = _HEADER.sub(rf"\1\2{REDACTED}", value)
        value = _KEY_VALUE.sub(rf"\1\2{REDACTED}", value)
        value = _BEARER.sub(f"Bearer {REDACTED}", value)
        value = _JWT.sub(REDACTED, value)
        value = _URL.sub(_url, value)
        value = _QUERY.sub(f"?{REDACTED}", value)
        value = _IDENTITY_VALUE.sub(rf"\1\2{REDACTED}", value)
        value = _EMAIL.sub("<email-redacted>", value)
        value = _XUID.sub("<xuid-redacted>", value)
        value = _HOME_DIRS.sub(HOME_PATH, value)
        if self._home:
            value = self._home.sub(HOME_PATH, value)
        value = _RUNTIME_DIRS.sub(RUNTIME_PATH, value)
        value = _MEDIA_DIRS.sub(MEDIA_PATH, value)
        value = _UUID.sub(MASKED_UUID, value)
        value = _MAC.sub(MASKED_MAC, value)
        value = _ADDRESS.sub(self._address, value)
        spans = _standing_whole(value, vouched)
        value = _JOINED_HEX.sub(lambda match: _joined_hex(match, spans), value)
        if self._user:
            value = self._user.sub("<user>", value)
        if self._host:
            value = self._host.sub("<host>", value)
        return _SERIAL.sub(lambda match: self._serial(match.group(0), trusted), value)

    @staticmethod
    def _serial(token: str, trusted: frozenset[str]) -> str:
        return token if token in trusted or _DEVICE_NODE.fullmatch(token) else MASKED_ID

    def _token(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
        key = address.compressed
        with self._lock:
            return self._public.setdefault(key, f"<public-ip-{len(self._public) + 1}>")

    def _mask_ipv4(self, address: ipaddress.IPv4Address, shown: str) -> str:
        """What to print for an IPv4 address that was written as `shown`."""
        if (not self.mask_addresses or address.is_loopback or address.is_unspecified or address.is_multicast
                or int(address) == 0xFFFFFFFF):
            return shown
        if address.is_global:
            return self._token(address)
        first, second = str(address).split(".")[:2]
        return f"{first}.{second}.x.x"

    def _ipv4(self, match: re.Match[str]) -> str:
        octets = [int(part) for part in match.groups()]
        if any(octet > 255 for octet in octets):
            return match.group(0)
        return self._mask_ipv4(ipaddress.IPv4Address(".".join(str(octet) for octet in octets)), match.group(0))

    def _address(self, match: re.Match[str]) -> str:
        token = match.group(0)
        if ":" not in token:
            return self._ipv4(match)
        run, percent, zone = token.partition("%")
        masked = self._ipv6(run)
        if masked is None:
            # Not IPv6 after all (a time, "host:port:"): addresses inside the run still count.
            return _IPV4.sub(self._ipv4, run) + percent + zone
        # The zone id names a network interface; it is only shown with an address that is shown in full.
        return masked + percent + zone if masked == run else masked

    def _ipv6(self, run: str) -> str | None:
        # Sentence punctuation can trail the address; "fe80::" itself is valid, so try as-is first.
        for candidate in (run, run.rstrip("."), run.rstrip(".").removesuffix(":")):
            try:
                address = ipaddress.IPv6Address(candidate)
            except ValueError:
                continue
            rest = run[len(candidate):]
            if address.ipv4_mapped is not None:
                return self._mask_ipv4(address.ipv4_mapped, candidate) + rest
            if not self.mask_addresses or address.is_loopback or address.is_unspecified:
                return run
            return (self._token(address) if address.is_global else LAN_IPV6) + rest
        return None
