"""Host firewall checks that need no root, and an honest marker for what does.

Without root the doctor can see which firewall manager systemd runs and, for ufw,
the saved default policy and (where the files are world-readable) the saved rules,
which it judges for IPv4 and IPv6 separately and words as what they would do once
loaded. It cannot see the live kernel ruleset, so that part is always reported as "cannot
verify without root" together with the exact command for the user to run. The
doctor never runs sudo itself and never changes a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .context import Context
from .model import Outcome, fail, info, needs_root, ok, skip, warn

MANAGERS = ("ufw", "firewalld", "nftables", "iptables")
ANYWHERE = ("0.0.0.0/0", "::/0")
# ufw saves the rules of each address family in a file of its own and loads the IPv6 one only with IPV6=yes.
UFW_RULE_FILES = (("IPv4", "/etc/ufw/user.rules"), ("IPv6", "/etc/ufw/user6.rules"))
SAVED_NOT_LIVE = "Saved rules are not the live state, which only root can read (see Live firewall rules)."
PROTOCOL = re.compile(r"[a-z0-9]{1,12}")
PORTS = re.compile(r"any|[0-9][0-9,:]{0,79}")
ROOT_COMMANDS = {
    "ufw": "sudo ufw status numbered verbose",
    "firewalld": "sudo firewall-cmd --list-all",
}
NFT_COMMAND = "sudo nft -a list ruleset"
DROP_LOG_COMMAND = "sudo journalctl -k --since '5 minutes ago' --no-pager -o short-iso -g 'UFW (BLOCK|ALLOW)'"


@dataclass(frozen=True)
class UfwRule:
    action: str
    protocol: str
    port: str
    direction: str
    everywhere: bool
    family: str = "IPv4"

    @property
    def outbound_udp(self) -> bool:
        return self.direction == "out" and self.protocol in ("udp", "any")

    def describe(self) -> str:
        return f"{self.action} {self.direction} {self.protocol} port {self.port} ({self.family})"


def active_managers(ctx: Context) -> dict[str, bool] | None:
    """Which firewall services systemd reports as active; None when systemd cannot be asked."""
    def ask() -> dict[str, bool] | None:
        result = ctx.command(["systemctl", "is-active", *(f"{name}.service" for name in MANAGERS)], 4)
        states = result.stdout.split()
        if result.error or len(states) != len(MANAGERS):
            return None
        return {name: state == "active" for name, state in zip(MANAGERS, states)}
    return ctx.once("firewall-managers", ask)


def parse_ufw_defaults(text: str) -> dict[str, str]:
    """DEFAULT_INPUT_POLICY/DEFAULT_OUTPUT_POLICY from /etc/default/ufw as allow, deny or reject."""
    names = {"ACCEPT": "allow", "DROP": "deny", "REJECT": "reject"}
    found = {}
    for key, label in (("DEFAULT_INPUT_POLICY", "incoming"), ("DEFAULT_OUTPUT_POLICY", "outgoing")):
        match = re.search(rf'(?m)^{key}="?([A-Za-z]+)"?', text)
        if match and match.group(1).upper() in names:
            found[label] = names[match.group(1).upper()]
    return found


def parse_ufw_rules(text: str, family: str = "IPv4") -> list[UfwRule]:
    """Saved rules from the `### tuple ###` lines of one of ufw's rule files; addresses and comments are not kept."""
    rules = []
    for line in text.splitlines():
        fields = line.split()
        if fields[:3] != ["###", "tuple", "###"] or len(fields) < 10:
            continue
        action, protocol, port, destination = fields[3].split("_")[0], fields[4], fields[5], fields[6]
        direction = next((token.split("_")[0] for token in fields[9:] if re.fullmatch(r"(in|out)(_\S+)?", token)), "")
        if (action in ("allow", "deny", "reject", "limit") and direction and PROTOCOL.fullmatch(protocol)
                and PORTS.fullmatch(port)):
            rules.append(UfwRule(action, protocol, port, direction, destination in ANYWHERE, family))
    return rules


def stops_all_udp(rules: list[UfwRule]) -> bool:
    """Whether these rules of one family, in their saved order, would stop every outbound UDP packet.

    That takes a deny or reject for any port to anywhere with no earlier rule letting some
    outbound UDP through: ufw applies the first rule that matches.
    """
    for rule in rules:
        if not rule.outbound_udp:
            continue
        if rule.action not in ("deny", "reject"):
            return False
        if rule.port == "any" and rule.everywhere:
            return True
    return False


def _ufw_saved_policy(ctx: Context) -> dict[str, Any]:
    settings = ctx.fs.read("/etc/default/ufw") or ""
    defaults = parse_ufw_defaults(settings)
    ipv6 = not re.search(r'(?mi)^IPV6="?no"?\s*$', settings)
    saved = {family: ctx.fs.read(path) for family, path in UFW_RULE_FILES if ipv6 or family == "IPv4"}
    readable = any(text is not None for text in saved.values())
    by_family = {family: parse_ufw_rules(text, family) if text else [] for family, text in saved.items()}
    rules = [rule for family_rules in by_family.values() for rule in family_rules]
    blocks = [rule for rule in rules if rule.outbound_udp and rule.action in ("deny", "reject")]
    closed = [family for family, family_rules in by_family.items() if stops_all_udp(family_rules)]
    return {"defaults": defaults, "readable": readable, "rules": rules, "blocks": blocks, "closed": closed}


def check_active_policy(ctx: Context) -> Outcome:
    managers = active_managers(ctx)
    if managers is None:
        return skip("Could not ask systemd which firewall is active.")
    active = [name for name, running in managers.items() if running]
    evidence: dict[str, Any] = dict(active=active)
    if not active:
        return info("No firewall manager (ufw, firewalld, nftables, iptables) is active. Rules loaded by other "
                    "tools cannot be seen without root.", **evidence)
    if "ufw" not in active:
        return info(f"{' and '.join(active)} is active; its rules can only be read with root.", **evidence)
    policy = _ufw_saved_policy(ctx)
    defaults, blocks, closed = policy["defaults"], policy["blocks"], policy["closed"]
    evidence.update(ufw_incoming=defaults.get("incoming"), ufw_outgoing=defaults.get("outgoing"),
                    ufw_rules_readable=policy["readable"], ufw_saved_rules=len(policy["rules"]),
                    ufw_outbound_blocks=[rule.describe() for rule in blocks], ufw_all_udp_blocked=closed)
    if not defaults:
        return info("ufw is active; its saved policy could not be read without root.", **evidence)
    summary = f"ufw is active; saved default policy: {defaults.get('incoming', 'unknown')} incoming, " \
              f"{defaults.get('outgoing', 'unknown')} outgoing"
    fix = f"Review the outbound rules with: `{ROOT_COMMANDS['ufw']}`"
    listed = ", ".join(rule.describe() for rule in blocks)
    if "IPv4" in closed:
        families = " and ".join(closed)
        return fail(f"{summary}. The saved rules would block all outbound UDP over {families} ({listed}); once "
                    f"loaded, they stop stream media from leaving this device. {SAVED_NOT_LIVE}", fix, **evidence)
    if closed:
        return warn(f"{summary}. The saved rules would block all outbound UDP over IPv6 but not over IPv4, which "
                    f"streams can use instead ({listed}). {SAVED_NOT_LIVE}", fix, **evidence)
    outgoing = defaults.get("outgoing")
    if blocks or outgoing != "allow":
        reasons = [f"{len(blocks)} saved rule(s) would block some outbound UDP ({listed})"] if blocks else []
        if outgoing in ("deny", "reject"):
            reasons.append("the saved default policy would stop outgoing traffic that no rule allows, so "
                           "streaming needs explicit allow rules")
        elif outgoing is None:
            reasons.append("the saved default policy for outgoing traffic could not be read")
        return warn(f"{summary}; {'; '.join(reasons)}. {SAVED_NOT_LIVE}", fix, **evidence)
    if not policy["readable"]:
        return info(f"{summary}. The saved rules need root to read.", **evidence)
    inbound = (" Unsolicited inbound packets are dropped, which is normal: replies to traffic this device starts "
               "are let through." if defaults.get("incoming") in ("deny", "reject") else "")
    return ok(f"{summary}; {len(policy['rules'])} saved rule(s), none of which would block outbound UDP.{inbound}",
              **evidence)


def check_rules(ctx: Context) -> Outcome:
    """The live ruleset is root-only: say so, and hand over the command instead of guessing."""
    managers = active_managers(ctx) or {}
    command = next((ROOT_COMMANDS[name] for name in ROOT_COMMANDS if managers.get(name)), NFT_COMMAND)
    extra = []
    if command != NFT_COMMAND:
        extra.append(f"The full kernel ruleset: `{NFT_COMMAND}`.")
    if managers.get("ufw"):
        extra.append(f"Right after a failed attempt, the packets ufw dropped: `{DROP_LOG_COMMAND}`.")
    return needs_root(command, " ".join(extra), active=[name for name, running in managers.items() if running])
