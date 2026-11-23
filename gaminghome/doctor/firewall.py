"""Host firewall checks that need no root, and an honest marker for what does.

Without root the doctor can see which firewall manager systemd runs and, for ufw,
the saved default policy and (where the files are world-readable) the saved rules.
It cannot see the live kernel ruleset, so that part is always reported as "cannot
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

    def describe(self) -> str:
        return f"{self.action} {self.direction} {self.protocol} port {self.port}"


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


def parse_ufw_rules(text: str) -> list[UfwRule]:
    """Saved rules from the `### tuple ###` lines of ufw's user.rules; addresses and comments are not kept."""
    rules = []
    for line in text.splitlines():
        fields = line.split()
        if fields[:3] != ["###", "tuple", "###"] or len(fields) < 10:
            continue
        action, protocol, port, destination = fields[3].split("_")[0], fields[4], fields[5], fields[6]
        direction = next((token.split("_")[0] for token in fields[9:] if re.fullmatch(r"(in|out)(_\S+)?", token)), "")
        if (action in ("allow", "deny", "reject", "limit") and direction and PROTOCOL.fullmatch(protocol)
                and PORTS.fullmatch(port)):
            rules.append(UfwRule(action, protocol, port, direction, destination in ANYWHERE))
    return rules


def _ufw_saved_policy(ctx: Context) -> dict[str, Any]:
    defaults = parse_ufw_defaults(ctx.fs.read("/etc/default/ufw") or "")
    saved = [ctx.fs.read(path) for path in ("/etc/ufw/user.rules", "/etc/ufw/user6.rules")]
    readable = any(text is not None for text in saved)
    rules = [rule for text in saved if text for rule in parse_ufw_rules(text)]
    blocks = [rule for rule in rules if rule.direction == "out" and rule.action in ("deny", "reject")
              and rule.protocol in ("udp", "any")]
    return {"defaults": defaults, "readable": readable, "rules": rules, "blocks": blocks}


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
    defaults, blocks = policy["defaults"], policy["blocks"]
    evidence.update(ufw_incoming=defaults.get("incoming"), ufw_outgoing=defaults.get("outgoing"),
                    ufw_rules_readable=policy["readable"], ufw_saved_rules=len(policy["rules"]),
                    ufw_outbound_blocks=[rule.describe() for rule in blocks])
    if not defaults:
        return info("ufw is active; its saved policy could not be read without root.", **evidence)
    summary = f"ufw is active; saved default policy: {defaults.get('incoming', 'unknown')} incoming, " \
              f"{defaults.get('outgoing', 'unknown')} outgoing"
    fix = f"Review the outbound rules with: `{ROOT_COMMANDS['ufw']}`"
    if any(rule.port == "any" and rule.everywhere for rule in blocks):
        return fail(f"{summary}. A saved rule blocks all outbound UDP ({blocks[0].describe()}), which stops stream "
                    "media from leaving this device.", fix, **evidence)
    if blocks or defaults.get("outgoing") != "allow":
        reason = (f"{len(blocks)} saved rule(s) block outbound UDP ({', '.join(rule.describe() for rule in blocks)})"
                  if blocks else "outgoing traffic is not allowed by default, so streaming needs explicit allow rules")
        return warn(f"{summary}; {reason}.", fix, **evidence)
    if not policy["readable"]:
        return info(f"{summary}. The saved rules need root to read.", **evidence)
    return ok(f"{summary}; {len(policy['rules'])} saved rule(s), none blocks outbound traffic. Unsolicited inbound "
              "packets are dropped, which is normal: replies to traffic this device starts are let through.",
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
