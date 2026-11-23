"""Tests for `omarchy-game-mode doctor`: no network, no devices, no root, no GTK."""

import collections
import heapq
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from gaminghome.doctor import (browser, buttons, buttonsio, checks, cli, console, context, devices, evdev, firewall,
                               hid, inputstack, model, net, redact, report, runner, stun, system, udp, wifi)
from gaminghome.doctor.context import CommandResult, Context, FileSystem, Options
from gaminghome.doctor.model import Status
from gaminghome.doctor.runner import Check

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(__file__).resolve().parent / "scratch" / "doctor"

# Canary values planted in fake inputs; none may ever reach any output.
CANARY_SSID = "CanaryNet-5G"
CANARY_BSSID = "de:ad:be:ef:00:01"
CANARY_MAC = "02:11:22:33:44:55"
CANARY_HOST = "canary-handheld"
CANARY_DOMAIN = "corp.canary.example"
CANARY_USER = "canaryuser"
CANARY_HOME = f"/home/{CANARY_USER}"
CANARY_PUBLIC_IP = "93.184.216.34"
CANARY_PUBLIC_IP6 = "2606:2800:220:1:248:1893:25c8:1946"
CANARY_LAN_IP = "192.168.77.23"
CANARY_SERIAL = "SN-CANARY-0042"
CANARIES = (CANARY_SSID, CANARY_BSSID, CANARY_MAC, CANARY_HOST, CANARY_DOMAIN, CANARY_USER, CANARY_PUBLIC_IP,
            CANARY_PUBLIC_IP6, CANARY_LAN_IP, CANARY_SERIAL)


def canary_redactor(**kw):
    return redact.Redactor(home=CANARY_HOME, user=CANARY_USER, host=CANARY_HOST, **kw)


class FakeCommands:
    """Canned tool output keyed by exact argv; an unexpected command fails the test."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def which(self, name):
        return f"/usr/bin/{name}" if any(key[0] == name for key in self.table) else None

    def run(self, argv, timeout):
        key = (Path(argv[0]).name, *argv[1:])
        self.calls.append(key)
        if key not in self.table:
            raise AssertionError(f"unexpected command: {key}")
        result = self.table[key]
        if isinstance(result, str):
            return CommandResult(tuple(argv), 0, result)
        return result


def forbidden(name):
    def call(*_args, **_kwargs):
        raise AssertionError(f"test touched the real {name}")
    return call


def make_context(*, commands=None, root=None, env=None, options=None, redactor=None, **overrides):
    """A Context whose every primitive is fake; anything not provided raises."""
    commands = commands or FakeCommands()
    defaults = dict(
        fs=FileSystem(root or SCRATCH / "empty"), env=env or {}, run=commands.run, which=commands.which,
        getaddrinfo=forbidden("resolver"), udp_socket=forbidden("UDP socket"), tcp_connect=forbidden("TCP socket"),
        tls_context=forbidden("TLS context"), monotonic=time.monotonic, wall_time=lambda: 1_800_000_000.0,
        sleep=lambda _s: None, parallel=lambda function, items, workers=8: [function(item) for item in items],
        redactor=redactor or canary_redactor())
    defaults.update(overrides)
    return Context(options or Options(), **defaults)


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class ScratchCase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)


class ModelTest(unittest.TestCase):
    def result(self, status, decisive=False, title="T"):
        return model.CheckResult("x.y", title, status, decisive=decisive)

    def test_fail_decides_exit_code_and_warn_does_not(self):
        report = model.Report("all", [self.result(Status.PASS), self.result(Status.WARN)])
        self.assertEqual((report.exit_code(), report.overall()), (0, "warn"))
        report.checks.append(self.result(Status.FAIL, title="Broken"))
        self.assertEqual((report.exit_code(), report.overall()), (1, "fail"))
        self.assertIn("Broken", report.headline())

    def test_unverified_decisive_evidence_is_inconclusive_even_when_everything_passes(self):
        report = model.Report("streaming", [self.result(Status.PASS), self.result(Status.PASS),
                                            self.result(Status.SKIP, decisive=True, title="Firewall rules")])
        self.assertEqual(report.overall(), "inconclusive")
        self.assertTrue(report.headline().startswith("INCONCLUSIVE"))
        self.assertIn("Firewall rules", report.headline())
        self.assertEqual(report.exit_code(), 0)
        self.assertEqual(report.summary()["unverified"], ["x.y"])

    def test_only_skips_are_not_a_healthy_result_and_facts_alone_are_not_a_test(self):
        self.assertEqual(model.Report("input", [self.result(Status.SKIP)]).overall(), "inconclusive")
        self.assertEqual(model.Report("system", [self.result(Status.INFO)]).overall(), "info")
        self.assertEqual(model.Report("input", [self.result(Status.PASS), self.result(Status.SKIP)]).overall(), "pass")

    def test_json_shape_and_root_block(self):
        check = model.CheckResult("firewall.rules", "Firewall rules", Status.SKIP, root_command="sudo nft list ruleset")
        document = model.Report("all", [check], run_id="r", sections={"capture": {"enabled": False}}).to_dict()
        self.assertEqual(document["schema"], "omarchy-game-mode.doctor/v1")
        self.assertEqual(document["schema_version"], 1)
        for key in ("run_id", "started_at", "finished_at", "duration_ms", "mode", "scope", "summary", "environment",
                    "privacy", "checks", "capture"):
            self.assertIn(key, document)
        for key in ("id", "category", "title", "status", "detail", "duration_ms", "proves", "does_not_prove",
                    "evidence", "root", "remediation", "decisive"):
            self.assertIn(key, document["checks"][0])
        self.assertEqual(document["checks"][0]["root"], {
            "required": True, "status": "not_run", "cannot_verify_without_root": True,
            "command": "sudo nft list ruleset"})
        self.assertEqual(model.CheckResult("a", "A", Status.PASS).root(), {"required": False})
        json.dumps(document)

    def test_needs_root_never_claims_a_result(self):
        outcome = model.needs_root("sudo ufw status numbered verbose")
        self.assertEqual(outcome.status, Status.SKIP)
        self.assertEqual(outcome.detail, "cannot verify without root \u2014 run: sudo ufw status numbered verbose")


class RedactionTest(unittest.TestCase):
    def setUp(self):
        self.r = canary_redactor()

    def test_public_addresses_become_stable_opaque_tokens(self):
        text = self.r.text(f"mapped {CANARY_PUBLIC_IP}:50000 then {CANARY_PUBLIC_IP} and 8.8.4.4 "
                           f"via [{CANARY_PUBLIC_IP6}]:3478")
        self.assertEqual(text, "mapped <public-ip-1>:50000 then <public-ip-1> and <public-ip-2> "
                               "via [<public-ip-3>]:3478")
        self.assertEqual(self.r.text(f"again {CANARY_PUBLIC_IP}"), "again <public-ip-1>")

    def test_lan_addresses_keep_two_octets_and_harmless_ones_stay(self):
        self.assertEqual(self.r.text(f"gw 192.168.77.1 me {CANARY_LAN_IP}/24 other 10.1.2.3, cgnat 100.64.3.4."),
                         "gw 192.168.x.x me 192.168.x.x/24 other 10.1.x.x, cgnat 100.64.x.x.")
        self.assertEqual(self.r.text("fe80::1c2:3ff:fe04:5%wlan0 fd10:b638::5 ::1 ::"),
                         "<lan-ipv6>%wlan0 <lan-ipv6> ::1 ::")
        keep = "127.0.0.53 0.0.0.0 255.255.255.255 239.255.255.250"
        self.assertEqual(self.r.text(keep), keep)

    def test_addresses_next_to_punctuation_are_still_caught(self):
        self.assertEqual(self.r.text(f"to {CANARY_LAN_IP}."), "to 192.168.x.x.")
        self.assertEqual(self.r.text(f"to {CANARY_PUBLIC_IP6}."), "to <public-ip-1>.")
        self.assertEqual(self.r.text("addr:fe80::aa:bb, next"), "addr:<lan-ipv6>, next")

    def test_versions_and_times_are_not_mistaken_for_addresses(self):
        text = "Chromium 155.0.8059.84, kernel 7.2.15-1-omarchy, Mesa 26.2.5, at 00:46:05, 1.2.3.4.5, 999.1.1.1"
        self.assertEqual(self.r.text(text), text)

    def test_mac_home_user_host_and_registered_secrets_never_survive(self):
        self.r.add_secret(CANARY_SSID)
        self.r.add_secret(CANARY_DOMAIN)
        text = self.r.text(f"Connected to {CANARY_BSSID} SSID: {CANARY_SSID} on {CANARY_HOST} as {CANARY_USER} "
                           f"{CANARY_HOME}/.config/x /home/someoneelse/y search {CANARY_DOMAIN} "
                           f"{CANARY_MAC.upper().replace(':', '-')}")
        for canary in CANARIES + ("someoneelse",):
            self.assertNotIn(canary, text)
        self.assertIn("~/.config/x", text)
        self.assertIn("<mac>", text)

    def test_identity_is_hidden_even_when_addresses_are_shown(self):
        r = canary_redactor(mask_addresses=False)
        r.add_secret(CANARY_SSID)
        text = r.text(f"{CANARY_LAN_IP} {CANARY_PUBLIC_IP} {CANARY_BSSID} {CANARY_HOME}/x {CANARY_SSID} {CANARY_HOST}")
        self.assertIn(CANARY_LAN_IP, text)
        self.assertIn(CANARY_PUBLIC_IP, text)
        for canary in (CANARY_BSSID, CANARY_HOME, CANARY_USER, CANARY_SSID, CANARY_HOST):
            self.assertNotIn(canary, text)

    def test_credentials_are_dropped_in_text_and_structures(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        text = self.r.text(f"Authorization: Bearer abc.def\nxsts_token=s3cr3t&x=1 password: hunter2 {jwt} "
                           "https://bob:pw@proxy.example/p?code=abc123&state=zzz9 mail someone@example.org "
                           "xuid 2535412345678901")
        for secret in ("abc.def", "s3cr3t", "hunter2", jwt, "bob:pw", "abc123", "zzz9", "someone@example.org",
                       "2535412345678901"):
            self.assertNotIn(secret, text)
        data = self.r.data({"Set-Cookie": "a=b", "userToken": "t", "code": "c", "nested": [{"client_secret": "s"}],
                            "error_code": 401, "gathering_state": "complete", "ok": True})
        self.assertEqual(data, {"Set-Cookie": "<redacted>", "userToken": "<redacted>", "code": "<redacted>",
                                "nested": [{"client_secret": "<redacted>"}], "error_code": 401,
                                "gathering_state": "complete", "ok": True})

    def test_ordinary_flags_and_words_are_left_alone(self):
        text = "--password-store=gnome-libsecret; no cookies, tokens or passwords are read"
        self.assertEqual(self.r.text(text), text)

    def test_common_word_identities_do_not_mangle_the_report(self):
        r = redact.Redactor(home="/home/user", user="user", host="omarchy")
        r.add_secret("home")
        self.assertEqual(r.text("omarchy pkg add: ask the user at home; /home/user/.config"),
                         "omarchy pkg add: ask the user at home; ~/.config")

    def test_redaction_is_idempotent(self):
        self.r.add_secret(CANARY_SSID)
        once = self.r.text(f"{CANARY_PUBLIC_IP} {CANARY_LAN_IP} {CANARY_BSSID} {CANARY_HOME}/a token=abc {CANARY_SSID} "
                           f"{CANARY_PUBLIC_IP6} fe80::1 /home/other/b")
        self.assertEqual(self.r.text(once), once)

    def test_a_redaction_bug_cannot_leak_the_value(self):
        self.r._text = lambda _value: 1 / 0
        self.assertEqual(self.r.text(f"secret {CANARY_PUBLIC_IP}"), redact.UNCLASSIFIED)


class RunnerTest(unittest.TestCase):
    def check(self, name, run, **kw):
        return Check(name, name.title(), "test", run, fields=("n",), **kw)

    def test_results_keep_the_given_order_whatever_finishes_first(self):
        def slow(_ctx):
            time.sleep(0.05)
            return model.ok("slow")
        checks = [self.check("a", slow), self.check("b", lambda _c: model.ok("fast")), self.check("c", slow)]
        results = runner.run_checks(checks, None, workers=3)
        self.assertEqual([r.id for r in results], ["a", "b", "c"])
        self.assertEqual([r.detail for r in results], ["slow", "fast", "slow"])

    def test_an_exception_is_isolated_and_reported_as_unknown_not_failed(self):
        def boom(_ctx):
            raise ValueError(f"cannot parse {CANARY_LAN_IP}")
        results = runner.run_checks([self.check("a", boom), self.check("b", lambda _c: model.ok("fine", n=1))], None)
        self.assertEqual([r.status for r in results], [Status.SKIP, Status.PASS])
        self.assertIn("internal error", results[0].detail)
        self.assertIn("ValueError", results[0].detail)
        self.assertEqual(results[0].evidence, {"reason": "internal-error"})

    def test_a_check_that_returns_the_wrong_type_is_an_internal_error(self):
        results = runner.run_checks([self.check("a", lambda _c: "nope")], None)
        self.assertEqual(results[0].status, Status.SKIP)

    def test_timeout_abandons_the_check_and_the_queue_keeps_moving(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(_ctx):
            release.wait(5)
            return model.ok("late")
        checks = [self.check("stuck", stuck, timeout=0.05), self.check("next", lambda _c: model.ok("ran"))]
        started = time.monotonic()
        results = runner.run_checks(checks, None, workers=1, timeout=2)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual([r.status for r in results], [Status.SKIP, Status.PASS])
        self.assertIn("timed out after 0.05 s", results[0].detail)
        self.assertEqual(results[0].evidence, {"reason": "timeout"})

    def test_global_deadline_marks_unstarted_checks_as_not_run(self):
        release = threading.Event()
        self.addCleanup(release.set)
        checks = [self.check("stuck", lambda _c: release.wait(5) and model.ok("x"), timeout=5),
                  self.check("never", lambda _c: model.ok("x"))]
        results = runner.run_checks(checks, None, workers=1, deadline=0.05)
        self.assertEqual([r.status for r in results], [Status.SKIP, Status.SKIP])
        self.assertIn("overall time limit", results[0].detail)
        self.assertIn("not run", results[1].detail)

    def test_static_scope_text_and_decisive_flag_travel_with_the_result(self):
        check = Check("a.b", "Title", "cat", lambda _c: model.ok("d"), proves="P", does_not_prove="N", decisive=True)
        result = runner.run_checks([check], None)[0]
        self.assertEqual((result.title, result.category, result.proves, result.does_not_prove, result.decisive),
                         ("Title", "cat", "P", "N", True))
        override = Check("a.b", "Title", "cat", lambda _c: model.Outcome(Status.WARN, "d", proves="other"), proves="P")
        self.assertEqual(runner.run_checks([override], None)[0].proves, "other")

    def test_evidence_outside_the_declared_fields_is_dropped(self):
        check = Check("a", "A", "t", lambda _c: model.ok("d", n=1, ssid=CANARY_SSID, rows=[{"keep": 1, "bssid": "x"}]),
                      fields=("n", "rows", "rows.keep"))
        self.assertEqual(runner.run_checks([check], None)[0].evidence, {"n": 1, "rows": [{"keep": 1}]})
        dropped = []
        runner.allowlisted({"a": 1, "b": {"c": 2, "d": 3}}, ("b", "b.c"), dropped)
        self.assertEqual(dropped, ["a", "b.d"])

    def test_parallel_map_keeps_order_and_surfaces_errors(self):
        self.assertEqual(runner.parallel_map(lambda n: n * 2, range(20), workers=4), [n * 2 for n in range(20)])
        self.assertEqual(runner.parallel_map(lambda n: n, []), [])
        with self.assertRaises(ZeroDivisionError):
            runner.parallel_map(lambda n: 1 // n, [1, 0])


class ContextTest(ScratchCase):
    def test_file_access_is_bounded_and_rooted(self):
        write(SCRATCH / "etc/os-release", "NAME=Omarchy\nSECOND=line\n")
        write(SCRATCH / "big", "x" * 10000)
        fs = FileSystem(SCRATCH)
        self.assertEqual(fs.line("/etc/os-release"), "NAME=Omarchy")
        self.assertEqual(len(fs.read("/big", limit=100)), 100)
        self.assertIsNone(fs.read("/missing"))
        self.assertEqual(fs.line("/missing"), "")
        self.assertEqual(fs.listdir("/etc"), ["os-release"])
        self.assertEqual(fs.listdir("/nope"), [])
        (SCRATCH / "link").symlink_to("etc/os-release")
        self.assertEqual(fs.resolved("/link"), "/etc/os-release")

    def test_run_command_captures_bounds_and_never_raises(self):
        python = sys.executable
        script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
        done = context.run_command([python, "-c", script])
        self.assertEqual((done.returncode, done.stdout.strip(), done.stderr.strip(), done.ok), (3, "out", "err", False))
        bounded = context.run_command([python, "-c", "print('x' * 100000)"], limit=1000)
        self.assertEqual(len(bounded.stdout), 1000)
        self.assertTrue(bounded.ok)
        self.assertEqual(context.run_command([str(SCRATCH / "no-such-tool")]).error, "not-found")

    def test_run_command_kills_a_command_that_overruns(self):
        started = time.monotonic()
        result = context.run_command([sys.executable, "-c", "import time; print('a', flush=True); time.sleep(30)"], 0.3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((result.error, result.returncode, result.ok), ("timeout", None, False))

    def test_commands_run_in_the_c_locale_without_a_shell(self):
        result = context.run_command([sys.executable, "-c", "import os; print(os.environ['LC_ALL'], '$HOME;id')"],
                                     env={"PATH": "/usr/bin"})
        self.assertEqual(result.stdout.strip(), "C $HOME;id")

    def test_missing_tool_is_a_result_and_memo_runs_once(self):
        ctx = make_context()
        self.assertEqual(ctx.command(["iw", "dev"]).error, "not-found")
        calls = []
        self.assertEqual([ctx.once("k", lambda: calls.append(1) or 7) for _ in range(3)], [7, 7, 7])
        self.assertEqual(calls, [1])


# ---- fakes for the network layer ------------------------------------------------

RELAY_IP, CLOUDFLARE_IP, GOOGLE_IP, REGIONAL_IP, NTP_IP = "20.202.24.10", "162.159.207.1", "74.125.250.129", \
    "20.202.99.7", "162.159.200.1"
SERVICE_IP = "40.90.8.20"
DNS_TABLE = {
    "relay.communication.microsoft.com": [RELAY_IP], "stun.cloudflare.com": [CLOUDFLARE_IP],
    "stun.l.google.com": [GOOGLE_IP], "time.cloudflare.com": [NTP_IP],
    **{host.name: [SERVICE_IP] for host in net.REQUIRED_HOSTS if host.https},
}


def fake_dns(table=None, ipv6=None):
    table = DNS_TABLE if table is None else table

    def getaddrinfo(host, _port, family=socket.AF_INET, kind=0):
        answers = (ipv6 or {}).get(host, []) if family == socket.AF_INET6 else table.get(host, [])
        if not answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(family, kind, 17, "", (address, 0)) for address in answers]
    return getaddrinfo


class FakeUdpSocket:
    """Scripted UDP peer: `responder(data, destination, sock)` returns the datagrams to deliver."""

    def __init__(self, responder, local):
        self.responder, self.local = responder, local
        self.queue, self.sent, self.closed = collections.deque(), [], False

    def settimeout(self, _seconds):
        pass

    def bind(self, _address):
        pass

    def connect(self, _address):
        pass

    def getsockname(self):
        return self.local

    def sendto(self, data, destination):
        self.sent.append((data, destination))
        self.queue.extend(self.responder(data, destination, self) or ())

    def recvfrom(self, _size):
        if not self.queue:
            raise socket.timeout("timed out")
        return self.queue.popleft()

    def close(self):
        self.closed = True


class UdpWorld:
    """Hands out fake sockets with increasing local ports and remembers them."""

    def __init__(self, responder, local_ip=CANARY_LAN_IP):
        self.responder, self.local_ip, self.sockets = responder, local_ip, []

    def socket(self, _family=socket.AF_INET):
        sock = FakeUdpSocket(self.responder, (self.local_ip, 40000 + len(self.sockets)))
        self.sockets.append(sock)
        return sock

    def sent(self):
        return [item for sock in self.sockets for item in sock.sent]


def binding_success(request, mapped):
    return stun.encode(stun.BINDING, stun.SUCCESS, request.transaction_id,
                       [(stun.ATTR_XOR_MAPPED_ADDRESS, stun.encode_xor_address(*mapped, request.transaction_id))])


def allocate_error(request, code, reason, extra=()):
    return stun.encode(stun.ALLOCATE, stun.ERROR, request.transaction_id,
                       [(stun.ATTR_ERROR_CODE, stun.encode_error_code(code, reason)), (stun.ATTR_REALM, b"rtcmedia"),
                        (stun.ATTR_NONCE, b"0123456789abcdef"), *extra])


def nat(public_ip=CANARY_PUBLIC_IP, port_for=lambda destination, sock: sock.local[1], silent=()):
    """A NAT plus STUN servers: every Binding is answered with the mapping `port_for` chooses."""
    def respond(data, destination, sock):
        request = stun.decode(data)
        if destination[0] in silent or request.method != stun.BINDING:
            return []
        return [(binding_success(request, (public_ip, port_for(destination, sock))), destination)]
    return respond


class StunCodecTest(unittest.TestCase):
    TID = bytes.fromhex("b7e7a701bc34d686fa87dfae")

    def test_requests_match_the_wire_format(self):
        self.assertEqual(stun.binding_request(self.TID), bytes.fromhex("000100002112a442") + self.TID)
        self.assertEqual(stun.allocate_request(self.TID),
                         bytes.fromhex("000300082112a442") + self.TID + bytes.fromhex("0019000411000000"))
        self.assertEqual(len(stun.new_transaction_id()), 12)
        self.assertNotEqual(stun.new_transaction_id(), stun.new_transaction_id())

    def test_method_and_class_bits_interleave(self):
        self.assertEqual(stun.message_type(stun.BINDING, stun.SUCCESS), 0x0101)
        self.assertEqual(stun.message_type(stun.BINDING, stun.ERROR), 0x0111)
        self.assertEqual(stun.message_type(stun.ALLOCATE, stun.ERROR), 0x0113)
        for method in (0x001, 0x003, 0x004, 0x0FF, 0xABC, 0xFFF):
            for cls in range(4):
                self.assertEqual(stun.split_type(stun.message_type(method, cls)), (method, cls))

    def test_rfc5769_sample_responses_decode(self):
        ipv4 = bytes.fromhex("0101003c2112a442b7e7a701bc34d686fa87dfae8022000b7465737420766563746f7220"
                             "002000080001a147e112a643000800142b91f599fd9e90c38c7489f92af9ba53f06be7d7"
                             "80280004c07d4c96")
        message = stun.decode(ipv4)
        self.assertEqual((message.method, message.cls, message.transaction_id), (stun.BINDING, stun.SUCCESS, self.TID))
        self.assertEqual(stun.mapped_address(message), ("192.0.2.1", 32853))
        ipv6 = bytes.fromhex("0002a1470113a9faa5d3f179bc25f4b5bed2b9d9")
        self.assertEqual(stun.decode_xor_address(ipv6, self.TID), ("2001:db8:1234:5678:11:2233:4455:6677", 32853))

    def test_xor_mapped_address_round_trip_v4_and_v6(self):
        for address, port in ((CANARY_PUBLIC_IP, 54321), ("192.168.1.2", 1), (CANARY_PUBLIC_IP6, 65535), ("::1", 3478)):
            tid = stun.new_transaction_id()
            encoded = stun.encode_xor_address(address, port, tid)
            self.assertNotIn(ipaddress.ip_address(address).packed, encoded)
            self.assertEqual(stun.decode_xor_address(encoded, tid), (ipaddress.ip_address(address).compressed, port))
            message = stun.decode(stun.encode(stun.BINDING, stun.SUCCESS, tid,
                                              [(stun.ATTR_XOR_MAPPED_ADDRESS, encoded)]))
            self.assertEqual(stun.mapped_address(message), (ipaddress.ip_address(address).compressed, port))

    def test_plain_mapped_address_and_alternate_server(self):
        message = stun.decode(stun.encode(stun.BINDING, stun.SUCCESS, self.TID, [
            (stun.ATTR_MAPPED_ADDRESS, stun.encode_plain_address("198.51.100.7", 4000)),
            (stun.ATTR_ALTERNATE_SERVER, stun.encode_plain_address(REGIONAL_IP, 3478))]))
        self.assertEqual(stun.mapped_address(message), ("198.51.100.7", 4000))
        self.assertEqual(stun.alternate_server(message), (REGIONAL_IP, 3478))
        self.assertIsNone(stun.mapped_address(stun.decode(stun.binding_request(self.TID))))

    def test_turn_401_challenge_parses(self):
        request = stun.decode(stun.allocate_request(self.TID))
        self.assertEqual((request.method, request.cls), (stun.ALLOCATE, stun.REQUEST))
        self.assertEqual(request.attributes, ((stun.ATTR_REQUESTED_TRANSPORT, bytes((17, 0, 0, 0))),))
        reply = stun.decode(allocate_error(request, 401, "Unauthorized"))
        self.assertEqual((reply.method, reply.cls), (stun.ALLOCATE, stun.ERROR))
        self.assertEqual(stun.error_code(reply), (401, "Unauthorized"))
        self.assertEqual(stun.realm(reply), "rtcmedia")
        self.assertIsNone(stun.error_code(stun.decode(stun.binding_request(self.TID))))
        self.assertEqual(stun.error_code(stun.decode(allocate_error(request, 300, "Try Alternate")))[0], 300)

    def test_malformed_datagrams_are_rejected(self):
        good = stun.encode(stun.BINDING, stun.SUCCESS, self.TID, [(stun.ATTR_REALM, b"abcde")])
        self.assertEqual(stun.decode(good).first(stun.ATTR_REALM), b"abcde")
        bad_cookie = good[:4] + b"\x00\x00\x00\x00" + good[8:]
        truncated_value = good[:20] + struct.pack("!HH", stun.ATTR_REALM, 400) + good[24:]
        for datagram in (b"", good[:19], bad_cookie, good[:-4], b"\xc0" + good[1:], truncated_value,
                         good[:2] + struct.pack("!H", 6) + good[4:26]):
            with self.assertRaises(stun.StunError):
                stun.decode(datagram)
        for value in (b"", b"\x00\x09\x00\x01\x01\x02\x03\x04", b"\x00\x01\x00\x01\x01"):
            with self.assertRaises(stun.StunError):
                stun.decode_xor_address(value, self.TID)
        with self.assertRaises(stun.StunError):
            stun.encode(stun.BINDING, stun.REQUEST, b"short")


class UdpChecksTest(unittest.TestCase):
    def ctx(self, responder, **kw):
        self.world = UdpWorld(responder)
        return make_context(udp_socket=self.world.socket, getaddrinfo=kw.pop("dns", fake_dns()), **kw)

    def test_exchange_ignores_strangers_garbage_and_other_transactions(self):
        def respond(data, destination, _sock):
            request = stun.decode(data)
            stranger = stun.encode(stun.BINDING, stun.SUCCESS, b"x" * 12)
            good = binding_success(request, (CANARY_PUBLIC_IP, 5000))
            return [(b"junk", destination), (stranger, destination), (good, ("203.0.113.9", 3478)), (good, destination)]
        ctx = self.ctx(respond)
        sock = self.world.socket()
        reply, mapped = udp.binding(ctx, sock, RELAY_IP, 3478)
        self.assertEqual((mapped, reply.attempts), ((CANARY_PUBLIC_IP, 5000), 1))

    def test_exchange_retransmits_once_then_gives_up(self):
        ctx = self.ctx(lambda *_a: [])
        sock = self.world.socket()
        self.assertIsNone(udp.binding(ctx, sock, RELAY_IP, 3478))
        self.assertEqual(len(sock.sent), 2)
        self.assertEqual(sock.sent[0], sock.sent[1])

    def test_binding_passes_when_every_server_answers(self):
        outcome = udp.check_stun_binding(self.ctx(nat()))
        self.assertEqual(outcome.status, Status.PASS)
        self.assertEqual((outcome.evidence["answered"], outcome.evidence["public_addresses"]), (3, 1))
        self.assertEqual([row["mapped_address"] for row in outcome.evidence["servers"]], [CANARY_PUBLIC_IP] * 3)
        self.assertTrue(all(sock.closed for sock in self.world.sockets))
        self.assertEqual({destination for _data, destination in self.world.sent()},
                         {(RELAY_IP, 3478), (CLOUDFLARE_IP, 3478), (GOOGLE_IP, 19302)})

    def test_binding_fails_only_on_direct_evidence(self):
        self.assertEqual(udp.check_stun_binding(self.ctx(nat(silent=(RELAY_IP, CLOUDFLARE_IP, GOOGLE_IP)))).status,
                         Status.FAIL)
        partial = udp.check_stun_binding(self.ctx(nat(silent=(RELAY_IP,))))
        self.assertEqual(partial.status, Status.WARN)
        self.assertIn("Microsoft's relay is the one Xbox streaming uses", partial.detail)
        unresolved = udp.check_stun_binding(self.ctx(nat(), dns=fake_dns({})))
        self.assertEqual(unresolved.status, Status.SKIP)
        self.assertEqual(self.world.sockets, [])

    def turn(self, answers):
        """`answers` maps a relay address to (code, reason, extra attributes) or None for silence."""
        def respond(data, destination, _sock):
            request = stun.decode(data)
            answer = answers.get(destination[0])
            return [(allocate_error(request, *answer), destination)] if answer else []
        return respond

    def test_turn_401_is_reachability_not_authentication(self):
        outcome = udp.check_turn_challenge(self.ctx(self.turn({RELAY_IP: (401, "Unauthorized")})))
        self.assertEqual((outcome.status, outcome.evidence["error_code"], outcome.evidence["redirected"]),
                         (Status.PASS, 401, False))
        for data, _destination in self.world.sent():
            request = stun.decode(data)
            self.assertEqual((request.method, request.cls), (stun.ALLOCATE, stun.REQUEST))
            self.assertEqual([kind for kind, _value in request.attributes], [stun.ATTR_REQUESTED_TRANSPORT])

    def test_turn_follows_one_redirect_to_a_public_regional_relay(self):
        alternate = [(stun.ATTR_ALTERNATE_SERVER, stun.encode_plain_address(REGIONAL_IP, 3478))]
        answers = {RELAY_IP: (300, "Try Alternate", alternate), REGIONAL_IP: (401, "Unauthorized")}
        outcome = udp.check_turn_challenge(self.ctx(self.turn(answers)))
        self.assertEqual((outcome.status, outcome.evidence["redirected"], outcome.evidence["error_code"]),
                         (Status.PASS, True, 401))
        self.assertEqual([destination for _data, destination in self.world.sent()],
                         [(RELAY_IP, 3478), (REGIONAL_IP, 3478)])
        silent = udp.check_turn_challenge(self.ctx(self.turn({RELAY_IP: (300, "Try Alternate", alternate)})))
        self.assertEqual(silent.status, Status.WARN)

    def test_turn_never_follows_a_redirect_into_private_address_space(self):
        alternate = [(stun.ATTR_ALTERNATE_SERVER, stun.encode_plain_address("192.168.77.1", 3478))]
        outcome = udp.check_turn_challenge(self.ctx(self.turn({RELAY_IP: (300, "Try Alternate", alternate)})))
        self.assertEqual((outcome.status, outcome.evidence["error_code"]), (Status.WARN, 300))
        self.assertEqual({destination[0] for _data, destination in self.world.sent()}, {RELAY_IP})

    def test_turn_silence_is_a_failure_and_no_name_is_a_skip(self):
        self.assertEqual(udp.check_turn_challenge(self.ctx(self.turn({}))).status, Status.FAIL)
        self.assertEqual(udp.check_turn_challenge(self.ctx(self.turn({}), dns=fake_dns({}))).status, Status.SKIP)

    def test_same_public_port_for_every_destination_is_endpoint_independent(self):
        outcome = udp.check_nat_mapping(self.ctx(nat()))
        self.assertEqual((outcome.status, outcome.evidence["verdict"]), (Status.PASS, "endpoint-independent"))
        self.assertTrue(outcome.evidence["address_translated"] and outcome.evidence["port_preserved"])
        probe = self.world.sockets[0]
        self.assertEqual(len({destination for _data, destination in probe.sent}), 3)
        self.assertEqual(len(probe.sent), 6)
        self.assertTrue(probe.closed)

    def test_public_port_that_depends_on_the_destination_is_flagged_without_a_formal_nat_type(self):
        ports = {RELAY_IP: 50001, CLOUDFLARE_IP: 50002, GOOGLE_IP: 50003}
        outcome = udp.check_nat_mapping(self.ctx(nat(port_for=lambda destination, _sock: ports[destination[0]])))
        self.assertEqual((outcome.status, outcome.evidence["verdict"]), (Status.WARN, "endpoint-dependent"))
        self.assertIn("not a formal NAT type", outcome.detail)
        self.assertNotIn("Strict", outcome.detail + outcome.remediation)
        self.assertFalse(outcome.evidence["port_preserved"])

    def test_mapping_that_changes_between_rounds_is_unstable(self):
        counter = iter(range(50000, 50100))
        seen = {}

        def rotating(destination, _sock):
            count = seen[destination] = seen.get(destination, 0) + 1
            return 40000 if count == 1 else next(counter)
        outcome = udp.check_nat_mapping(self.ctx(nat(port_for=rotating)))
        self.assertEqual((outcome.status, outcome.evidence["verdict"]), (Status.WARN, "unstable"))

    def test_one_answer_is_inconclusive_and_a_public_host_has_no_translation(self):
        outcome = udp.check_nat_mapping(self.ctx(nat(silent=(CLOUDFLARE_IP, GOOGLE_IP))))
        self.assertEqual((outcome.status, outcome.evidence["verdict"]), (Status.SKIP, "inconclusive"))
        self.world = UdpWorld(nat(public_ip=CANARY_PUBLIC_IP), local_ip=CANARY_PUBLIC_IP)
        direct = udp.check_nat_mapping(make_context(udp_socket=self.world.socket, getaddrinfo=fake_dns()))
        self.assertEqual(direct.status, Status.PASS)
        self.assertFalse(direct.evidence["address_translated"])
        self.assertIn("no address translation", direct.detail)

    def test_mapping_classification_is_a_pure_function(self):
        a, b = (CANARY_PUBLIC_IP, 5000), (CANARY_PUBLIC_IP, 5001)
        self.assertEqual(udp.classify_mapping([[a, a, a], [a, a, a]]), "endpoint-independent")
        self.assertEqual(udp.classify_mapping([[a, b], [a, b]]), "endpoint-dependent")
        self.assertEqual(udp.classify_mapping([[a, ("8.8.4.4", 5000)]]), "endpoint-dependent")
        self.assertEqual(udp.classify_mapping([[a, a], [a, b]]), "unstable")
        self.assertEqual(udp.classify_mapping([[a, None], [a, None]]), "inconclusive")
        self.assertEqual(udp.classify_mapping([[a, a, None], [a, a, None]]), "endpoint-independent")
        self.assertEqual(udp.classify_mapping([]), "inconclusive")


# ---- captured tool output and /proc samples (identifiers replaced by canaries) ---

def kernel_hex(address):
    """An IPv4 address the way /proc/net/route prints it on this machine."""
    return int(ipaddress.IPv4Address(address)).to_bytes(4, sys.byteorder).hex().upper()


def route_table(*rows):
    lines = ["Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT"]
    for interface, destination, gateway, flags, metric, mask in rows:
        lines.append(f"{interface}\t{kernel_hex(destination)}\t{kernel_hex(gateway)}\t{flags}\t0\t0\t{metric}\t"
                     f"{kernel_hex(mask)}\t0\t0\t0")
    return "\n".join(lines) + "\n"


WIFI_ROUTES = route_table(("wlp3s0", "0.0.0.0", "192.168.77.1", "0003", 600, "0.0.0.0"),
                          ("wlp3s0", "192.168.77.0", "0.0.0.0", "0001", 600, "255.255.255.0"))
IF_INET6_LOCAL = ("00000000000000000000000000000001 01 80 10 80       lo\n"
                  "fd10b63800000000a299a8fffe719247 02 40 00 00   wlp3s0\n"
                  "fe80000000000000a299a8fffe719247 02 40 20 80   wlp3s0\n")
IF_INET6_GLOBAL = IF_INET6_LOCAL + ipaddress.IPv6Address(CANARY_PUBLIC_IP6).exploded.replace(":", "") + \
    " 02 40 00 00   wlp3s0\n"
ZERO6 = "0" * 32
ULA_NET, LINK_NET = "fd10b638" + "0" * 24, "fe80" + "0" * 28
IPV6_ROUTE_NO_DEFAULT = (
    f"{ULA_NET} 40 {ZERO6} 00 {ZERO6} 00000258 00000002 00000000 00000001   wlp3s0\n"
    f"{ZERO6} 00 {ZERO6} 00 {ZERO6} ffffffff 00000001 00000000 00200200       lo\n"
    f"{LINK_NET} 40 {ZERO6} 00 {ZERO6} 00000400 00000002 00000000 00000001   wlp3s0\n")
IPV6_ROUTE_DEFAULT = IPV6_ROUTE_NO_DEFAULT + \
    f"{ZERO6} 00 {ZERO6} 00 {LINK_NET[:-1]}1 00000258 00000003 00000000 00000003   wlp3s0\n"
PROC_NET_DEV = ("Inter-|   Receive                                                |  Transmit\n"
                " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop"
                " fifo colls carrier compressed\n"
                "    lo: 1234 10 0 0 0 0 0 0 1234 10 0 0 0 0 0 0\n"
                "wlp3s0: 99 9 0 0 0 0 0 0 99 9 0 0 0 0 0 0\n")
RESOLV_STUB = ("# managed by systemd-resolved\nnameserver 127.0.0.53\noptions edns0 trust-ad\n"
               f"search {CANARY_DOMAIN} .\n")
RESOLVECTL_STATUS = f"""Global
           Protocols: -LLMNR -mDNS -DNSOverTLS DNSSEC=no/unsupported
    resolv.conf mode: stub
Fallback DNS Servers: 9.9.9.9#dns.quad9.net 2620:fe::9#dns.quad9.net
                      1.1.1.1#cloudflare-dns.com
                      2606:4700:4700::1111#cloudflare-dns.com 8.8.8.8#dns.google
                      2001:4860:4860::8888#dns.google

Link 2 (wlp3s0)
    Current Scopes: DNS
         Protocols: +DefaultRoute -LLMNR -mDNS -DNSOverTLS DNSSEC=no/unsupported
Current DNS Server: 1.1.1.3
       DNS Servers: 1.1.1.3 1.0.0.3
        DNS Domain: {CANARY_DOMAIN} ~.
     Default Route: yes
"""
IP_RULES_DEFAULT = "0:\tfrom all lookup local\n32766:\tfrom all lookup main\n32767:\tfrom all lookup default\n"
IP_RULES_WIREGUARD = ("0:\tfrom all lookup local\n32764:\tfrom all lookup main suppress_prefixlength 0\n"
                      "32765:\tnot from all fwmark 0xca6c lookup 51820\n32766:\tfrom all lookup main\n"
                      "32767:\tfrom all lookup default\n")
IP_RULES_TAILSCALE = ("0:\tfrom all lookup local\n5210:\tfrom all fwmark 0x80000/0xff0000 lookup main\n"
                      "5230:\tfrom all fwmark 0x80000/0xff0000 lookup default\n"
                      "5250:\tfrom all fwmark 0x80000/0xff0000 unreachable\n5270:\tfrom all lookup 52\n"
                      "32766:\tfrom all lookup main\n32767:\tfrom all lookup default\n")
PING_GATEWAY = ("ping", "-4", "-n", "-q", "-c", "20", "-i", "0.2", "-W", "1", "-w", "7", "192.168.77.1")
PING_CLEAN = ("PING 192.168.77.1 (192.168.77.1) 56(84) bytes of data.\n\n--- 192.168.77.1 ping statistics ---\n"
              "20 packets transmitted, 20 received, 0% packet loss, time 3811ms\n"
              "rtt min/avg/max/mdev = 1.234/2.345/10.456/1.789 ms\n")
PING_LOSSY = PING_CLEAN.replace("20 received, 0%", "17 received, 15%")
PING_SILENT = ("PING 192.168.77.1 (192.168.77.1) 56(84) bytes of data.\n\n--- 192.168.77.1 ping statistics ---\n"
               "20 packets transmitted, 0 received, 100% packet loss, time 6999ms\n")


def pmtu_argv(size):
    return ("ping", "-4", "-n", "-q", "-c", "2", "-i", "0.2", "-W", "1", "-w", "3", "-M", "do", "-s", str(size),
            "1.1.1.1")


def ping_reply(received):
    return CommandResult(("ping",), 0 if received else 1,
                         f"--- 1.1.1.1 ping statistics ---\n2 packets transmitted, {received} received, "
                         f"{'+2 errors, ' if not received else ''}{100 - 50 * received}% packet loss, time 201ms\n")


class NetCase(ScratchCase):
    def tree(self, files):
        """A fresh fake filesystem root holding exactly `files`."""
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        for path, text in files.items():
            write(SCRATCH / path.lstrip("/"), text)
        return SCRATCH

    def ctx(self, files=None, commands=None, **kw):
        world = UdpWorld(kw.pop("responder", lambda *_a: []))
        return make_context(root=self.tree(files or {}), commands=FakeCommands(commands), udp_socket=world.socket, **kw)


class RouteTest(NetCase):
    def test_default_route_is_decoded_from_the_kernel_table(self):
        if sys.byteorder == "little":
            self.assertEqual(net._kernel_ipv4("0104A8C0"), int(ipaddress.IPv4Address("192.168.4.1")))
        ctx = self.ctx({"/proc/net/route": WIFI_ROUTES, "/sys/class/net/wlp3s0/wireless/.keep": ""})
        outcome = net.check_default_route(ctx)
        self.assertEqual(outcome.status, Status.PASS)
        self.assertEqual(outcome.evidence, {"interface": "wlp3s0", "interface_type": "wifi", "gateway": "192.168.77.1",
                                            "source": CANARY_LAN_IP, "default_routes": 1})

    def test_no_default_route_is_a_failure(self):
        only_local = route_table(("wlp3s0", "192.168.77.0", "0.0.0.0", "0001", 600, "255.255.255.0"))
        for table in (only_local, "", route_table(("wlp3s0", "0.0.0.0", "192.168.77.1", "0002", 600, "0.0.0.0"))):
            self.assertEqual(net.check_default_route(self.ctx({"/proc/net/route": table})).status, Status.FAIL)

    def test_lowest_metric_wins_and_route_lookup_is_longest_prefix(self):
        table = route_table(("wlp3s0", "0.0.0.0", "192.168.77.1", "0003", 600, "0.0.0.0"),
                            ("enp5s0", "0.0.0.0", "10.0.0.1", "0003", 100, "0.0.0.0"),
                            ("enp5s0", "10.0.0.0", "0.0.0.0", "0001", 100, "255.255.255.0"),
                            ("wlp3s0", "192.168.77.0", "0.0.0.0", "0001", 600, "255.255.255.0"))
        ctx = self.ctx({"/proc/net/route": table})
        self.assertEqual(net.check_default_route(ctx).evidence["interface"], "enp5s0")
        routes = net.ipv4_routes(ctx)
        on_link = net.route_for(routes, "192.168.77.50")
        self.assertEqual((on_link.interface, on_link.gateway), ("wlp3s0", 0))
        self.assertEqual(net.route_for(routes, "172.16.9.9").gateway_address, "10.0.0.1")
        self.assertIsNone(net.route_for([], "10.0.0.1"))

    def test_adapter_named_after_its_mac_is_not_printed(self):
        self.assertEqual(net.display_interface("wlx0211223344ff"), "wlx<mac>")
        self.assertEqual(net.display_interface("wlp3s0"), "wlp3s0")

    def test_ipv6_states(self):
        cases = [(IF_INET6_LOCAL, IPV6_ROUTE_NO_DEFAULT, Status.PASS, "local-only"),
                 ("", "", Status.PASS, "absent"),
                 (IF_INET6_GLOBAL, IPV6_ROUTE_DEFAULT, Status.PASS, "dual-stack"),
                 (IF_INET6_GLOBAL, IPV6_ROUTE_NO_DEFAULT, Status.WARN, "half-configured"),
                 (IF_INET6_LOCAL, IPV6_ROUTE_DEFAULT, Status.WARN, "half-configured")]
        for addresses, routes, status, state in cases:
            outcome = net.check_ipv6(self.ctx({"/proc/net/if_inet6": addresses, "/proc/net/ipv6_route": routes}))
            self.assertEqual((outcome.status, outcome.evidence["ipv6_state"]), (status, state))
        self.assertIn("ipv6.method disabled", outcome.remediation)

    def test_mtu_of_the_default_interface(self):
        files = {"/proc/net/route": WIFI_ROUTES, "/sys/class/net/wlp3s0/mtu": "1500\n"}
        self.assertEqual(net.check_mtu_interface(self.ctx(files)).status, Status.PASS)
        files["/sys/class/net/wlp3s0/mtu"] = "1280\n"
        self.assertEqual(net.check_mtu_interface(self.ctx(files)).status, Status.WARN)
        self.assertEqual(net.check_mtu_interface(self.ctx({"/proc/net/route": WIFI_ROUTES})).status, Status.SKIP)


class VpnProxyTest(NetCase):
    FILES = {"/proc/net/route": WIFI_ROUTES, "/proc/net/dev": PROC_NET_DEV}

    def test_plain_wifi_has_no_vpn(self):
        outcome = net.check_vpn_proxy(self.ctx(self.FILES, {("ip", "-4", "rule", "show"): IP_RULES_DEFAULT}))
        self.assertEqual(outcome.status, Status.PASS)
        self.assertEqual(outcome.evidence["policy_rules"], 0)

    def test_wireguard_full_tunnel_is_found_through_policy_routing(self):
        files = {**self.FILES, "/proc/net/dev": PROC_NET_DEV + "   wg0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
                 "/sys/class/net/wg0/type": "65534\n", "/sys/class/net/wg0/flags": "0x10d1\n"}
        commands = {("ip", "-4", "rule", "show"): IP_RULES_WIREGUARD,
                    ("ip", "-4", "route", "show", "table", "51820"): "default dev wg0 scope link\n"}
        outcome = net.check_vpn_proxy(self.ctx(files, commands))
        self.assertEqual(outcome.status, Status.WARN)
        self.assertEqual((outcome.evidence["tunnel_interfaces"], outcome.evidence["policy_tables_with_default_route"],
                          outcome.evidence["policy_rules"]), (["wg0"], ["51820"], 2))

    def test_tailscale_without_exit_node_is_not_a_warning(self):
        files = {**self.FILES, "/proc/net/dev": PROC_NET_DEV + "tailscale0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"}
        commands = {("ip", "-4", "rule", "show"): IP_RULES_TAILSCALE,
                    ("ip", "-4", "route", "show", "table", "52"): "100.64.0.0/10 dev tailscale0\n"}
        outcome = net.check_vpn_proxy(self.ctx(files, commands))
        self.assertEqual(outcome.status, Status.PASS)
        self.assertIn("tailscale0 is up but does not carry internet traffic", outcome.detail)

    def test_default_route_through_a_tunnel_and_downed_tunnels(self):
        files = {"/proc/net/route": route_table(("tun0", "0.0.0.0", "10.8.0.1", "0003", 50, "0.0.0.0")),
                 "/proc/net/dev": PROC_NET_DEV + "  tun0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
                                                 "   wg9: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
                 "/sys/class/net/wg9/flags": "0x1090\n"}
        outcome = net.check_vpn_proxy(self.ctx(files, {("ip", "-4", "rule", "show"): IP_RULES_DEFAULT}))
        self.assertEqual(outcome.status, Status.WARN)
        self.assertTrue(outcome.evidence["default_route_via_tunnel"])
        self.assertEqual(outcome.evidence["tunnel_interfaces"], ["tun0"])

    def test_proxy_variables_are_named_but_their_values_never_appear(self):
        secret = f"http://bob:hunter2@proxy.{CANARY_DOMAIN}:3128"
        ctx = self.ctx(self.FILES, {("ip", "-4", "rule", "show"): IP_RULES_DEFAULT},
                       env={"HTTPS_PROXY": secret, "all_proxy": secret, "NO_PROXY": "localhost"})
        outcome = net.check_vpn_proxy(ctx)
        self.assertEqual(outcome.status, Status.WARN)
        self.assertEqual(outcome.evidence["proxy_variables"], ["HTTPS_PROXY", "all_proxy"])
        for fragment in ("hunter2", "bob", CANARY_DOMAIN, "3128"):
            self.assertNotIn(fragment, outcome.detail + outcome.remediation + json.dumps(outcome.evidence))

    def test_missing_ip_tool_with_a_tunnel_up_is_reported_as_unknown(self):
        files = {**self.FILES, "/proc/net/dev": PROC_NET_DEV + "   wg0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"}
        outcome = net.check_vpn_proxy(self.ctx(files))
        self.assertEqual(outcome.status, Status.WARN)
        self.assertIsNone(outcome.evidence["policy_rules"])
        self.assertEqual(net.parse_ip_rules("5270:\tfrom 192.168.77.23 lookup 52\ngarbage\n"), [(5270, "52")])


class DnsTest(NetCase):
    def test_resolver_behind_the_systemd_stub_is_identified_and_filtering_is_flagged(self):
        ctx = self.ctx({"/etc/resolv.conf": RESOLV_STUB}, {("resolvectl", "status"): RESOLVECTL_STATUS})
        outcome = net.check_resolver(ctx)
        self.assertEqual(outcome.status, Status.WARN)
        self.assertEqual(outcome.evidence["mode"], "systemd-resolved")
        self.assertEqual([(row["address"], row["kind"]) for row in outcome.evidence["servers"]],
                         [("1.1.1.3", "filtering"), ("1.0.0.3", "filtering")])
        self.assertIn("Cloudflare for Families", outcome.detail)
        self.assertNotIn(CANARY_DOMAIN, outcome.detail + json.dumps(outcome.evidence))
        self.assertEqual(ctx.redactor.text(f"search {CANARY_DOMAIN}"), "search <redacted>")

    def test_resolvectl_parser_handles_wrapped_lists_and_decorated_addresses(self):
        servers, fallback, domains = net.parse_resolvectl_status(RESOLVECTL_STATUS)
        self.assertEqual(servers, ["1.1.1.3", "1.0.0.3"])
        self.assertEqual(fallback, ["9.9.9.9", "2620:fe::9", "1.1.1.1", "2606:4700:4700::1111", "8.8.8.8",
                                    "2001:4860:4860::8888"])
        self.assertEqual(domains, [CANARY_DOMAIN])
        self.assertEqual([net._server_address(token) for token in
                          ("1.1.1.1:53#one.one", "[2606:4700:4700::1111]:853", "fe80::1%wlp3s0", "nonsense")],
                         ["1.1.1.1", "2606:4700:4700::1111", "fe80::1", ""])

    def test_plain_resolv_conf_router_and_public_resolvers(self):
        resolv = f"nameserver 192.168.77.1\nsearch {CANARY_DOMAIN}\n"
        router = net.check_resolver(self.ctx({"/etc/resolv.conf": resolv}))
        self.assertEqual((router.status, router.evidence["servers"][0]["kind"]), (Status.INFO, "lan"))
        self.assertIn("cannot be seen from here", router.detail)
        public = net.check_resolver(self.ctx({"/etc/resolv.conf": "nameserver 8.8.8.8\nnameserver 127.0.0.1\n"}))
        self.assertEqual([row["kind"] for row in public.evidence["servers"]], ["public", "local"])
        self.assertIn("Google", public.detail)
        self.assertEqual(net.check_resolver(self.ctx({})).status, Status.SKIP)
        self.assertEqual(net.check_resolver(self.ctx({"/etc/resolv.conf": RESOLV_STUB})).status, Status.SKIP)

    def test_every_filtering_resolver_entry_is_a_valid_address(self):
        for address in (*net.FILTERING_RESOLVERS, *net.KNOWN_RESOLVERS):
            self.assertEqual(ipaddress.ip_address(address).compressed, address)
            self.assertEqual(net.classify_resolver(address)[0],
                             "filtering" if address in net.FILTERING_RESOLVERS else "public")

    def test_lookup_has_its_own_deadline_and_reports_why_it_failed(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(*_args):
            release.wait(5)
            return []
        self.assertEqual(net.resolve(make_context(getaddrinfo=hang), "www.xbox.com", timeout=0.05).error, "timeout")
        self.assertEqual(net.resolve(make_context(getaddrinfo=fake_dns({})), "www.xbox.com").error, "not-found")

        def broken(*_args):
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        self.assertEqual(net.resolve(make_context(getaddrinfo=broken), "www.xbox.com").error, "failed")
        table = {"a.example": [SERVICE_IP, SERVICE_IP, "0.0.0.0"]}
        found = net.resolve(make_context(getaddrinfo=fake_dns(table)), "a.example")
        self.assertEqual((found.addresses, found.usable), ((SERVICE_IP, "0.0.0.0"), (SERVICE_IP,)))

    def test_required_names_all_resolve(self):
        outcome = net.check_dns_required(make_context(getaddrinfo=fake_dns(ipv6={"www.xbox.com": [CANARY_PUBLIC_IP6]})))
        self.assertEqual((outcome.status, outcome.evidence["resolved"], outcome.evidence["total"]), (Status.PASS, 7, 7))
        row = next(row for row in outcome.evidence["hosts"] if row["host"] == "www.xbox.com")
        self.assertEqual((row["a_records"], row["aaaa_records"]), (1, 1))
        self.assertNotIn(SERVICE_IP, json.dumps(outcome.evidence))

    def test_one_missing_name_warns_and_streaming_names_or_a_sinkhole_fail(self):
        table = dict(DNS_TABLE)
        del table["www.xbox.com"]
        self.assertEqual(net.check_dns_required(make_context(getaddrinfo=fake_dns(table))).status, Status.WARN)
        sinkholed = {**DNS_TABLE, "xhome.gssv-play-prod.xboxlive.com": ["0.0.0.0"],
                     "gssv-play-prod.xboxlive.com": ["0.0.0.0"]}
        outcome = net.check_dns_required(make_context(getaddrinfo=fake_dns(sinkholed)))
        self.assertEqual(outcome.status, Status.FAIL)
        self.assertIn("non-routable-answer", outcome.detail)
        self.assertIn("blocking the name", outcome.remediation)
        self.assertEqual(net.check_dns_required(make_context(getaddrinfo=fake_dns({}))).status, Status.FAIL)


class FakeTcp:
    def __init__(self):
        self.closed, self.sent = False, []

    def settimeout(self, _seconds):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


class FakeTls:
    """Stands in for ssl.SSLContext: records the server names and fails where told to."""

    def __init__(self, failures=None):
        self.failures, self.hosts, self.sockets = failures or {}, [], []

    def __call__(self):
        return self

    def wrap_socket(self, sock, server_hostname):
        self.hosts.append(server_hostname)
        self.sockets.append(sock)
        if server_hostname in self.failures:
            raise self.failures[server_hostname]
        return self

    def version(self):
        return "TLSv1.3"

    def close(self):
        for sock in self.sockets:
            sock.close()


class TlsTest(unittest.TestCase):
    def run_check(self, tls, connect=None):
        self.sockets = []

        def tcp_connect(address, timeout):
            self.targets.append((address, timeout))
            if connect:
                connect(address)
            self.sockets.append(FakeTcp())
            return self.sockets[-1]
        self.targets = []
        return net.check_tls(make_context(getaddrinfo=fake_dns(), tcp_connect=tcp_connect, tls_context=tls))

    def test_handshakes_verify_the_service_name_and_send_no_request(self):
        tls = FakeTls()
        outcome = self.run_check(tls)
        self.assertEqual((outcome.status, outcome.evidence["completed"]), (Status.PASS, 6))
        self.assertEqual(sorted(tls.hosts), sorted(host.name for host in net.REQUIRED_HOSTS if host.https))
        self.assertNotIn("relay.communication.microsoft.com", tls.hosts)
        self.assertEqual({target for target, _timeout in self.targets}, {(SERVICE_IP, 443)})
        self.assertTrue(all(sock.closed and not sock.sent for sock in self.sockets))
        self.assertEqual({row["tls_version"] for row in outcome.evidence["hosts"]}, {"TLSv1.3"})

    def test_certificate_failure_points_at_interception(self):
        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        error.verify_message = "self-signed certificate in certificate chain"
        outcome = self.run_check(FakeTls({"www.xbox.com": error}))
        self.assertEqual(outcome.status, Status.WARN)
        row = next(row for row in outcome.evidence["hosts"] if row["host"] == "www.xbox.com")
        self.assertEqual((row["stage"], row["error"]), ("certificate", "self-signed certificate in certificate chain"))
        self.assertIn("intercepting TLS", outcome.remediation)
        self.assertTrue(all(sock.closed for sock in self.sockets))

    def test_blocked_port_443_fails_without_printing_addresses(self):
        def refuse(_address):
            raise TimeoutError("timed out")
        outcome = self.run_check(FakeTls(), connect=refuse)
        self.assertEqual(outcome.status, Status.FAIL)
        self.assertIn("connect: timeout", outcome.detail)
        self.assertNotIn(SERVICE_IP, outcome.detail + json.dumps(outcome.evidence))

    def test_streaming_service_handshake_failures_are_decisive(self):
        failures = {name: ssl.SSLError("handshake failure") for name in
                    ("xhome.gssv-play-prod.xboxlive.com", "gssv-play-prod.xboxlive.com")}
        self.assertEqual(self.run_check(FakeTls(failures)).status, Status.FAIL)
        self.assertEqual(net.describe_os_error(ConnectionRefusedError()), "refused")
        self.assertEqual(net.describe_os_error(OSError(101, "Network is unreachable")), "network is unreachable")

    def test_unresolved_name_is_reported_at_the_dns_stage(self):
        outcome = net.check_tls(make_context(getaddrinfo=fake_dns({})))
        self.assertEqual(outcome.status, Status.FAIL)
        self.assertEqual({row["stage"] for row in outcome.evidence["hosts"]}, {"dns"})


def ntp_server(skew, mode=4, stratum=2, echo=True):
    def respond(data, destination, _sock):
        if destination[1] != 123:
            return []
        now = 1_800_000_000.0 + skew
        stamp = net._ntp_timestamp(now)
        origin = data[40:48] if echo else bytes(8)
        return [(bytes([0x20 | mode, stratum]) + bytes(22) + origin + stamp + stamp, destination)]
    return respond


class ClockTest(NetCase):
    SYNCED = {("timedatectl", "show", "-p", "NTP", "-p", "NTPSynchronized"): "NTP=yes\nNTPSynchronized=yes\n"}
    UNSYNCED = {("timedatectl", "show", "-p", "NTP", "-p", "NTPSynchronized"): "NTP=no\nNTPSynchronized=no\n"}

    def check(self, commands=None, **kw):
        return net.check_clock(self.ctx(commands=commands, getaddrinfo=fake_dns(), **kw))

    def test_sntp_request_is_a_client_packet_and_the_offset_is_measured(self):
        captured = []

        def respond(data, destination, sock):
            captured.append((data, destination))
            return ntp_server(2.5)(data, destination, sock)
        outcome = self.check(self.SYNCED, responder=respond)
        self.assertEqual((outcome.status, outcome.evidence["offset_ms"], outcome.evidence["ntp_synchronized"]),
                         (Status.PASS, 2500, True))
        packet, destination = captured[0]
        self.assertEqual((len(packet), packet[0], destination), (48, 0x23, (NTP_IP, 123)))

    def test_skew_thresholds(self):
        self.assertEqual(self.check(self.SYNCED, responder=ntp_server(-12)).status, Status.WARN)
        wrong = self.check(self.SYNCED, responder=ntp_server(3600))
        self.assertEqual((wrong.status, wrong.evidence["offset_ms"]), (Status.FAIL, 3_600_000))
        self.assertIn("timedatectl set-ntp true", wrong.remediation)

    def test_unsynchronised_clock_warns_and_missing_evidence_is_a_skip(self):
        self.assertEqual(self.check(self.UNSYNCED, responder=ntp_server(0.2)).status, Status.WARN)
        self.assertEqual(self.check(self.SYNCED).status, Status.PASS)
        self.assertEqual(self.check().status, Status.SKIP)

    def test_replies_that_are_not_ours_are_ignored(self):
        for responder in (ntp_server(100, mode=3), ntp_server(100, stratum=0), ntp_server(100, echo=False)):
            outcome = self.check(self.SYNCED, responder=responder)
            self.assertEqual((outcome.status, outcome.evidence["offset_ms"]), (Status.PASS, None))
        self.assertAlmostEqual(net._from_ntp(net._ntp_timestamp(1_800_000_000.25)), 1_800_000_000.25, places=6)
        self.assertGreater(net._from_ntp(struct.pack("!II", 5, 0)), 2_085_978_496)


class PingTest(NetCase):
    FILES = {"/proc/net/route": WIFI_ROUTES}

    def test_ping_summary_parser(self):
        self.assertEqual(net.parse_ping(PING_CLEAN), {"sent": 20, "received": 20, "loss_percent": 0.0,
                                                      "rtt_avg_ms": 2.345, "rtt_max_ms": 10.456})
        self.assertEqual(net.parse_ping(PING_SILENT)["loss_percent"], 100.0)
        self.assertIsNone(net.parse_ping(PING_SILENT)["rtt_avg_ms"])
        self.assertIsNone(net.parse_ping("ping: usage error"))

    def test_gateway_loss_verdicts(self):
        cases = [(PING_CLEAN, Status.PASS), (PING_LOSSY, Status.WARN), (PING_SILENT, Status.SKIP),
                 (PING_CLEAN.replace("10.456", "250.0"), Status.WARN), ("", Status.SKIP)]
        for output, status in cases:
            commands = {PING_GATEWAY: CommandResult(PING_GATEWAY, 0, output)}
            outcome = net.check_gateway_loss(self.ctx(self.FILES, commands))
            self.assertEqual(outcome.status, status, output)
        self.assertEqual(net.check_gateway_loss(self.ctx(self.FILES)).status, Status.SKIP)
        self.assertEqual(net.check_gateway_loss(self.ctx({})).status, Status.SKIP)

    def test_path_mtu_probe(self):
        cases = [(1, 1, Status.PASS), (1, 0, Status.WARN), (0, 0, Status.SKIP)]
        for small, large, status in cases:
            commands = {pmtu_argv(1200): ping_reply(small), pmtu_argv(1472): ping_reply(large)}
            self.assertEqual(net.check_pmtu(self.ctx(commands=commands)).status, status)
        self.assertEqual(net.check_pmtu(self.ctx()).status, Status.SKIP)


# ---- Wi-Fi ------------------------------------------------------------------------
# Radio facts as captured on the ROG Xbox Ally; the network name and access-point
# address are replaced by canaries so the real ones are never published.

IW_LINK = (f"Connected to {CANARY_BSSID} (on wlp3s0)\n\tSSID: {CANARY_SSID}\n\tfreq: 5640.0\n\tsignal: -44 dBm\n"
           "\trx bitrate: 1921.5 MBit/s 160MHz HE-MCS 9 HE-NSS 2 HE-GI 0 HE-DCM 0\n"
           "\ttx bitrate: 1729.6 MBit/s 160MHz HE-MCS 8 HE-NSS 2 HE-GI 0 HE-DCM 0\n")
IW_INFO = (f"Interface wlp3s0\n\tifindex 2\n\twdev 0x1\n\taddr {CANARY_MAC}\n\tssid {CANARY_SSID}\n\ttype managed\n"
           "\twiphy 0\n\tchannel 128 (5640 MHz), width: 160 MHz, center1: 5570 MHz\n\ttxpower 6.00 dBm\n")
IW_INFO_6GHZ = IW_INFO.replace("channel 128 (5640 MHz), width: 160 MHz, center1: 5570 MHz",
                               "channel 69 (6295 MHz), width: 160 MHz, center1: 6345 MHz")
JOURNAL_ARGV = ("journalctl", "-b", "-u", "NetworkManager", "-u", "iwd", "-u", "wpa_supplicant", "--since=-10min",
                "--no-pager", "-o", "short-unix", "-n", "2000")
JOURNAL_EVENTS = "\n".join(f"{stamp} {CANARY_HOST} {message}" for stamp, message in (
    ("1763887000.100000", "NetworkManager[700]: <info>  [1.1] device (wlp3s0): supplicant interface state: "
                          "completed -> authenticating"),
    ("1763887000.200000", f"wpa_supplicant[800]: wlp3s0: SME: Trying to authenticate with {CANARY_BSSID} "
                          f"(SSID='{CANARY_SSID}' freq=5640 MHz)"),
    ("1763887000.900000", "NetworkManager[700]: <info>  [1.2] device (p2p-dev-wlp3s0): supplicant management "
                          "interface state: completed -> authenticating"),
    ("1763887001.500000", f"wpa_supplicant[800]: wlp3s0: CTRL-EVENT-CONNECTED - Connection to {CANARY_BSSID} "
                          "completed [id=0 id_str=]"),
    ("1763887100.000000", f"wpa_supplicant[800]: wlp3s0: CTRL-EVENT-DISCONNECTED bssid={CANARY_BSSID} reason=3 "
                          "locally_generated=1"),
    ("1763887100.300000", "NetworkManager[700]: <info>  [1.3] device (wlp3s0): supplicant interface state: "
                          "completed -> disconnected"),
    ("1763887200.000000", "wpa_supplicant[800]: wlp3s0: CTRL-EVENT-STARTED-CHANNEL-SWITCH freq=5500 ht_enabled=1"),
    ("1763887200.400000", "wpa_supplicant[800]: wlp3s0: CTRL-EVENT-CHANNEL-SWITCH freq=5500 ht_enabled=1"),
    ("1763887300.000000", "NetworkManager[700]: <info>  [1.4] policy: set 'Roaming Deauth Radar' (wlp3s0) as "
                          "default for IPv4 routing and DNS"),
)) + "\n"
WIFI_FILES = {"/proc/net/route": WIFI_ROUTES, "/sys/class/net/wlp3s0/wireless/.keep": "", "/sys/class/net/lo/.keep": ""}


def iw_commands(link=IW_LINK, info=IW_INFO, power="Power save: off\n", journal=""):
    return {("iw", "dev", "wlp3s0", "link"): link, ("iw", "dev", "wlp3s0", "info"): info,
            ("iw", "dev", "wlp3s0", "get", "power_save"): power, JOURNAL_ARGV: journal}


class WifiTest(NetCase):
    def test_link_is_parsed_and_the_network_identity_is_dropped(self):
        link = wifi.parse_link(IW_LINK)
        self.assertEqual((link.connected, link.frequency_mhz, link.signal_dbm, link.rx_mbps, link.tx_mbps,
                          link.width_mhz, link.generation), (True, 5640, -44, 1921.5, 1729.6, 160, "Wi-Fi 6"))
        self.assertEqual(set(link.private), {CANARY_SSID, CANARY_BSSID})
        self.assertNotIn(CANARY_SSID, repr(link))
        self.assertFalse(wifi.parse_link("Not connected.\n").connected)
        self.assertFalse(wifi.parse_link("").connected)

    def test_sample_link_is_5ghz_160mhz_dfs_with_power_save_off_and_no_ssid(self):
        ctx = self.ctx(WIFI_FILES, iw_commands())
        link, power = wifi.check_wifi_link(ctx), wifi.check_wifi_powersave(ctx)
        self.assertEqual(link.evidence, {
            "interface": "wlp3s0", "band": "5 GHz", "frequency_mhz": 5640, "channel": 128, "width_mhz": 160,
            "dfs": True, "signal_dbm": -44, "rx_mbps": 1921.5, "tx_mbps": 1729.6, "generation": "Wi-Fi 6"})
        self.assertEqual(link.status, Status.WARN)
        self.assertIn("does not explain a stream that never connects", link.detail)
        self.assertEqual((power.status, power.evidence["power_save"]), (Status.PASS, False))
        for outcome in (link, power):
            text = outcome.detail + outcome.remediation + json.dumps(outcome.evidence)
            for canary in (CANARY_SSID, CANARY_BSSID, CANARY_MAC):
                self.assertNotIn(canary, text)
        self.assertEqual(ctx.redactor.text(f"ssid {CANARY_SSID}"), "ssid <redacted>")

    def test_6ghz_link_is_not_dfs_and_passes(self):
        outcome = wifi.check_wifi_link(self.ctx(WIFI_FILES, iw_commands(info=IW_INFO_6GHZ)))
        self.assertEqual((outcome.status, outcome.evidence["band"], outcome.evidence["channel"],
                          outcome.evidence["dfs"], outcome.evidence["generation"]),
                         (Status.PASS, "6 GHz", 69, False, "Wi-Fi 6E"))

    def test_channel_and_dfs_math(self):
        self.assertEqual([wifi.channel_for(f) for f in (2412, 2472, 2484, 5180, 5640, 5825, 5955, 6295, 900)],
                         [1, 13, 14, 36, 128, 165, 1, 69, None])
        self.assertEqual([wifi.band_for(f) for f in (2437, 5180, 5825, 5955, 7115, 60480)],
                         ["2.4 GHz", "5 GHz", "5 GHz", "6 GHz", "6 GHz", "unknown band"])
        self.assertEqual(wifi.channel_span(5640, 160), (5490, 5650))
        self.assertEqual(wifi.channel_span(5640, 160, 5570), (5490, 5650))
        self.assertEqual(wifi.channel_span(6295, 160), (6265, 6425))
        dfs = {(5180, 20): False, (5180, 80): False, (5180, 160): True, (5260, 20): True, (5500, 80): True,
               (5640, 160): True, (5720, 20): True, (5745, 80): False, (5825, 20): False, (2437, 20): False,
               (6295, 160): False}
        for (frequency, width), expected in dfs.items():
            self.assertEqual(wifi.uses_dfs(frequency, width), expected, (frequency, width))

    def test_bitrate_line_variants(self):
        self.assertEqual(wifi._bitrate("866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2"), (866.7, 80, "Wi-Fi 5"))
        self.assertEqual(wifi._bitrate("144.4 MBit/s MCS 15 short GI"), (144.4, 20, "Wi-Fi 4"))
        self.assertEqual(wifi._bitrate("2882.4 MBit/s 320MHz EHT-MCS 11 EHT-NSS 2"), (2882.4, 320, "Wi-Fi 7"))
        self.assertEqual(wifi._bitrate("1201.0 MBit/s 80P80MHz HE-MCS 11")[1], 160)
        legacy = wifi.parse_info("channel 6 (2437 MHz), width: 20 MHz (no HT), center1: 2437 MHz")
        self.assertEqual(legacy["width_mhz"], 20)

    def test_weak_2_4ghz_link_and_power_save_on_warn(self):
        link = IW_LINK.replace("5640.0", "2437").replace("-44", "-78").replace("160MHz ", "")
        info = IW_INFO.replace("channel 128 (5640 MHz), width: 160 MHz, center1: 5570 MHz",
                               "channel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz")
        ctx = self.ctx(WIFI_FILES, iw_commands(link, info, "Power save: on\n"))
        outcome = wifi.check_wifi_link(ctx)
        self.assertEqual((outcome.status, outcome.evidence["band"], outcome.evidence["dfs"]),
                         (Status.WARN, "2.4 GHz", False))
        self.assertIn("weak", outcome.detail)
        power = wifi.check_wifi_powersave(ctx)
        self.assertEqual(power.status, Status.WARN)
        self.assertIn("`sudo iw dev wlp3s0 set power_save off`", power.remediation)

    def test_wifi_checks_skip_cleanly_when_they_do_not_apply(self):
        wired = {"/proc/net/route": route_table(("enp5s0", "0.0.0.0", "10.0.0.1", "0003", 100, "0.0.0.0")),
                 "/sys/class/net/wlp3s0/wireless/.keep": "", "/sys/class/net/enp5s0/.keep": ""}
        cases = [({"/sys/class/net/enp5s0/.keep": ""}, {}, "No wireless interface"),
                 (wired, {}, "does not use Wi-Fi"),
                 (WIFI_FILES, {}, "`iw` tool is not installed"),
                 (WIFI_FILES, iw_commands(link="Not connected.\n"), "not connected")]
        for files, commands, phrase in cases:
            ctx = self.ctx(files, commands)
            for check in (wifi.check_wifi_link, wifi.check_wifi_powersave, wifi.check_wifi_events):
                outcome = check(ctx)
                self.assertEqual(outcome.status, Status.SKIP)
                self.assertIn(phrase, outcome.detail)

    def test_interface_option_selects_and_validates(self):
        files = {**WIFI_FILES, "/sys/class/net/wlan1/wireless/.keep": ""}
        commands = {("iw", "dev", "wlan1", "link"): IW_LINK.replace("wlp3s0", "wlan1"),
                    ("iw", "dev", "wlan1", "info"): IW_INFO_6GHZ, ("iw", "dev", "wlan1", "get", "power_save"): ""}
        ctx = self.ctx(files, commands, options=Options(interface="wlan1"))
        self.assertEqual(wifi.check_wifi_link(ctx).evidence["interface"], "wlan1")
        self.assertEqual(wifi.check_wifi_powersave(ctx).status, Status.SKIP)
        missing = wifi.check_wifi_link(self.ctx(WIFI_FILES, iw_commands(), options=Options(interface="eth9")))
        self.assertEqual(missing.status, Status.SKIP)

    def test_recent_events_are_counted_once_and_never_quoted(self):
        self.assertEqual(wifi.count_events(JOURNAL_EVENTS), {"roams": 1, "disconnects": 1, "channel_switches": 1})
        outcome = wifi.check_wifi_events(self.ctx(WIFI_FILES, iw_commands(journal=JOURNAL_EVENTS)))
        self.assertEqual(outcome.status, Status.WARN)
        self.assertEqual(outcome.evidence, {"window_minutes": 10, "roams": 1, "disconnects": 1, "channel_switches": 1})
        text = outcome.detail + outcome.remediation
        for canary in (CANARY_SSID, CANARY_BSSID, CANARY_HOST, "Roaming Deauth Radar"):
            self.assertNotIn(canary, text)

    def test_quiet_journal_passes_and_unreadable_journal_skips(self):
        quiet = wifi.check_wifi_events(self.ctx(WIFI_FILES, iw_commands(journal="-- No entries --\n")))
        self.assertEqual(quiet.status, Status.PASS)
        hint = CommandResult(JOURNAL_ARGV, 0, "", "Hint: You are currently not seeing messages from other users and "
                                                  "the system.\n")
        self.assertEqual(wifi.check_wifi_events(self.ctx(WIFI_FILES, iw_commands(journal=hint))).status, Status.SKIP)
        failed = CommandResult(JOURNAL_ARGV, 1, "", "No journal files were found.\n")
        self.assertEqual(wifi.check_wifi_events(self.ctx(WIFI_FILES, iw_commands(journal=failed))).status, Status.SKIP)
        no_tool = {key: value for key, value in iw_commands().items() if key != JOURNAL_ARGV}
        self.assertEqual(wifi.check_wifi_events(self.ctx(WIFI_FILES, no_tool)).status, Status.SKIP)


# ---- firewall ---------------------------------------------------------------------

SYSTEMCTL_FIREWALLS = ("systemctl", "is-active", "ufw.service", "firewalld.service", "nftables.service",
                       "iptables.service")
UFW_DEFAULTS = ('IPV6=yes\nDEFAULT_INPUT_POLICY="DROP"\nDEFAULT_OUTPUT_POLICY="ACCEPT"\n'
                'DEFAULT_FORWARD_POLICY="DROP"\n')
UFW_RULES = f"""*filter
:ufw-user-input - [0:0]
### RULES ###

### tuple ### allow udp 53317 0.0.0.0/0 any 0.0.0.0/0 in
-A ufw-user-input -p udp --dport 53317 -j ACCEPT

### tuple ### allow udp 53 172.17.0.1 any {CANARY_LAN_IP}/12 in comment=616c6c6f772d646f636b65722d646e73
-A ufw-user-input -p udp -d 172.17.0.1 --dport 53 -s {CANARY_LAN_IP}/12 -j ACCEPT

### tuple ### allow tcp 22 0.0.0.0/0 any 0.0.0.0/0 OpenSSH - in
-A ufw-user-input -p tcp --dport 22 -j ACCEPT

### END RULES ###
COMMIT
"""
UFW_BLOCK_PORT = "### tuple ### deny udp 3478 0.0.0.0/0 any 0.0.0.0/0 out\n"
UFW_BLOCK_ALL = "### tuple ### reject_log any any 0.0.0.0/0 any 0.0.0.0/0 out_wlp3s0\n"


def firewall_states(*states):
    return {SYSTEMCTL_FIREWALLS: CommandResult(SYSTEMCTL_FIREWALLS, 3, "\n".join(states) + "\n")}


UFW_ACTIVE = firewall_states("active", "inactive", "inactive", "inactive")


class FirewallTest(NetCase):
    def test_ufw_rule_tuples_are_parsed_without_addresses_or_comments(self):
        rules = firewall.parse_ufw_rules(UFW_RULES + UFW_BLOCK_ALL)
        self.assertEqual([rule.describe() for rule in rules], [
            "allow in udp port 53317", "allow in udp port 53", "allow in tcp port 22", "reject out any port any"])
        self.assertEqual([rule.everywhere for rule in rules], [True, False, True, True])
        self.assertEqual(firewall.parse_ufw_defaults(UFW_DEFAULTS), {"incoming": "deny", "outgoing": "allow"})
        self.assertEqual(firewall.parse_ufw_defaults("DEFAULT_OUTPUT_POLICY=REJECT\n"), {"outgoing": "reject"})

    def test_default_ufw_policy_read_without_root_does_not_block_streaming(self):
        files = {"/etc/default/ufw": UFW_DEFAULTS, "/etc/ufw/user.rules": UFW_RULES, "/etc/ufw/user6.rules": ""}
        outcome = firewall.check_active_policy(self.ctx(files, UFW_ACTIVE))
        self.assertEqual(outcome.status, Status.PASS)
        self.assertEqual(outcome.evidence, {
            "active": ["ufw"], "ufw_incoming": "deny", "ufw_outgoing": "allow", "ufw_rules_readable": True,
            "ufw_saved_rules": 3, "ufw_outbound_blocks": []})
        self.assertNotIn(CANARY_LAN_IP, outcome.detail + json.dumps(outcome.evidence))

    def test_outbound_udp_blocks_are_found_and_only_a_total_block_is_a_failure(self):
        base = {"/etc/default/ufw": UFW_DEFAULTS}
        port = firewall.check_active_policy(self.ctx({**base, "/etc/ufw/user.rules": UFW_RULES + UFW_BLOCK_PORT},
                                                     UFW_ACTIVE))
        self.assertEqual((port.status, port.evidence["ufw_outbound_blocks"]), (Status.WARN, ["deny out udp port 3478"]))
        everything = firewall.check_active_policy(self.ctx({**base, "/etc/ufw/user6.rules": UFW_BLOCK_ALL}, UFW_ACTIVE))
        self.assertEqual(everything.status, Status.FAIL)
        self.assertIn("`sudo ufw status numbered verbose`", everything.remediation)
        closed = {"/etc/default/ufw": UFW_DEFAULTS.replace('OUTPUT_POLICY="ACCEPT"', 'OUTPUT_POLICY="DROP"'),
                  "/etc/ufw/user.rules": UFW_RULES}
        self.assertEqual(firewall.check_active_policy(self.ctx(closed, UFW_ACTIVE)).status, Status.WARN)

    def test_what_cannot_be_read_is_reported_as_information_not_as_a_pass(self):
        unreadable = firewall.check_active_policy(self.ctx({"/etc/default/ufw": UFW_DEFAULTS}, UFW_ACTIVE))
        self.assertEqual((unreadable.status, unreadable.evidence["ufw_rules_readable"]), (Status.INFO, False))
        self.assertEqual(firewall.check_active_policy(self.ctx({}, UFW_ACTIVE)).status, Status.INFO)
        firewalld = firewall_states("inactive", "active", "inactive", "inactive")
        other = firewall.check_active_policy(self.ctx({}, firewalld))
        self.assertEqual((other.status, other.evidence["active"]), (Status.INFO, ["firewalld"]))
        nothing = firewall_states("inactive", "inactive", "inactive", "unknown")
        none = firewall.check_active_policy(self.ctx({}, nothing))
        self.assertEqual((none.status, none.evidence["active"]), (Status.INFO, []))
        self.assertEqual(firewall.check_active_policy(self.ctx({})).status, Status.SKIP)

    def test_live_rules_always_need_root_and_the_command_is_only_printed(self):
        commands = FakeCommands(UFW_ACTIVE)
        ufw = firewall.check_rules(make_context(commands=commands))
        self.assertEqual((ufw.status, ufw.root_command), (Status.SKIP, "sudo ufw status numbered verbose"))
        self.assertEqual(ufw.detail, "cannot verify without root \u2014 run: sudo ufw status numbered verbose")
        self.assertIn("`sudo nft -a list ruleset`", ufw.remediation)
        self.assertIn("-g 'UFW (BLOCK|ALLOW)'`", ufw.remediation)
        self.assertEqual(commands.calls, [SYSTEMCTL_FIREWALLS])
        firewalld = firewall.check_rules(self.ctx({}, firewall_states("inactive", "active", "inactive", "inactive")))
        self.assertEqual(firewalld.root_command, "sudo firewall-cmd --list-all")
        self.assertEqual(firewall.check_rules(self.ctx({})).root_command, "sudo nft -a list ruleset")


# ---- console address ----------------------------------------------------------------

CONSOLE_IP = "192.168.77.50"
ARP_TABLE = ("IP address       HW type     Flags       HW address            Mask     Device\n"
             f"192.168.77.1     0x1         0x2         {CANARY_BSSID}     *        wlp3s0\n")


def arp(flags):
    return ARP_TABLE + f"{CONSOLE_IP}    0x1         {flags}         {CANARY_MAC}     *        wlp3s0\n"


class ConsoleTest(NetCase):
    def console_ctx(self, files=None, tcp=None, responder=None, address=CONSOLE_IP):
        self.tcp_targets = []

        def tcp_connect(target, timeout):
            self.tcp_targets.append(target)
            if tcp is not None:
                raise tcp
            return FakeTcp()
        self.world = UdpWorld(responder or (lambda *_a: []))
        files = {"/proc/net/route": WIFI_ROUTES, **(files or {})}
        return make_context(root=self.tree(files), udp_socket=self.world.socket, tcp_connect=tcp_connect,
                            options=Options(console_ip=address))

    def test_only_private_rfc1918_unicast_addresses_are_accepted(self):
        for good in ("10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.10", " 192.168.1.10 "):
            self.assertEqual(console.private_ipv4(good), good.strip())
        for bad in ("8.8.8.8", CANARY_PUBLIC_IP, "100.64.1.1", "169.254.1.1", "127.0.0.1", "172.32.0.1", "224.0.0.1",
                    "255.255.255.255", "0.0.0.0", "192.168.1.300", "192.168.1.1/24", "192.168.01.5", "fe80::1",
                    "xbox.local", "", "192.168.1.1;id"):
            with self.assertRaises(ValueError, msg=bad):
                console.private_ipv4(bad)

    def test_console_on_the_same_subnet_is_reached_directly(self):
        outcome = console.check_console_route(self.console_ctx())
        self.assertEqual((outcome.status, outcome.evidence["on_link"]), (Status.PASS, True))
        self.assertIn("cannot prove that Remote Play works", outcome.detail)

    def test_console_on_another_subnet_or_without_a_route_warns(self):
        routed = console.check_console_route(self.console_ctx(address="192.168.90.50"))
        self.assertEqual((routed.status, routed.evidence["on_link"]), (Status.WARN, False))
        self.assertIn("default gateway", routed.detail)
        local_only = route_table(("wlp3s0", "192.168.77.0", "0.0.0.0", "0001", 600, "255.255.255.0"))
        lost = console.check_console_route(self.console_ctx({"/proc/net/route": local_only}, address="10.9.9.9"))
        self.assertEqual((lost.status, lost.evidence["routed"]), (Status.WARN, False))

    def test_own_broadcast_and_network_addresses_are_not_probed(self):
        for address in (CANARY_LAN_IP, "192.168.77.255", "192.168.77.0"):
            ctx = self.console_ctx(address=address)
            self.assertEqual(console.check_console_route(ctx).status, Status.SKIP)
            self.assertEqual(console.check_console_reachability(ctx).status, Status.SKIP)
            self.assertEqual(self.tcp_targets, [])
            self.assertEqual([sock.sent for sock in self.world.sockets], [[]] * len(self.world.sockets))

    def test_probe_is_one_stun_request_and_one_tcp_attempt_to_that_address_only(self):
        outcome = console.check_console_reachability(self.console_ctx(tcp=ConnectionRefusedError()))
        self.assertEqual(outcome.status, Status.PASS)
        self.assertEqual(outcome.evidence, {"stun_udp_9002": False, "tcp_3074": "refused", "neighbour": "absent"})
        self.assertEqual(self.tcp_targets, [(CONSOLE_IP, 3074)])
        sent = self.world.sent()
        self.assertEqual({destination for _data, destination in sent}, {(CONSOLE_IP, 9002)})
        for data, _destination in sent:
            request = stun.decode(data)
            self.assertEqual((request.method, request.cls, request.attributes), (stun.BINDING, stun.REQUEST, ()))
        self.assertIn("cannot prove that Remote Play works", outcome.detail)

    def test_stun_answer_or_arp_entry_counts_as_alive(self):
        answered = console.check_console_reachability(self.console_ctx(tcp=TimeoutError(), responder=nat()))
        self.assertEqual((answered.status, answered.evidence["stun_udp_9002"]), (Status.PASS, True))
        neighbour = console.check_console_reachability(self.console_ctx({"/proc/net/arp": arp("0x2")}, TimeoutError()))
        self.assertEqual((neighbour.status, neighbour.evidence["neighbour"]), (Status.PASS, "resolved"))
        self.assertNotIn(CANARY_MAC, neighbour.detail + json.dumps(neighbour.evidence))

    def test_silence_is_inconclusive_never_a_failure(self):
        outcome = console.check_console_reachability(self.console_ctx({"/proc/net/arp": arp("0x0")}, TimeoutError()))
        self.assertEqual(outcome.status, Status.WARN)
        self.assertEqual(outcome.evidence, {"stun_udp_9002": False, "tcp_3074": "timeout", "neighbour": "unresolved"})
        self.assertIn("Silence is not proof", outcome.detail)
        routed = console.check_console_reachability(self.console_ctx(tcp=TimeoutError(), address="192.168.90.50"))
        self.assertEqual(routed.evidence["neighbour"], "not-on-link")

    def test_without_an_address_nothing_is_sent(self):
        ctx = make_context()
        outcome = console.check_console_reachability(ctx)
        self.assertEqual(outcome.status, Status.SKIP)
        self.assertIn("--console-ip", outcome.remediation)


# ---- clients, video, system ---------------------------------------------------------

VAINFO = """Trying display: wayland
vainfo: VA-API version: 1.24 (libva 2.24.0)
vainfo: Driver version: Mesa Gallium driver 26.2.5-arch1.1 for AMD Ryzen Z2 A (radeonsi, vangogh, ACO, DRM 3.64)
vainfo: Supported profile and entrypoints
      VAProfileMPEG2Main              :	VAEntrypointVLD
      VAProfileH264ConstrainedBaseline:	VAEntrypointVLD
      VAProfileH264Main               :	VAEntrypointVLD
      VAProfileH264Main               :	VAEntrypointEncSlice
      VAProfileH264High               :	VAEntrypointVLD
      VAProfileH264High               :	VAEntrypointEncSlice
      VAProfileHEVCMain               :	VAEntrypointVLD
      VAProfileAV1Profile0            :	VAEntrypointVLD
      VAProfileNone                   :	VAEntrypointVideoProc
"""
CHROMIUM_FLAGS = (f"# flags\n--ozone-platform=wayland\n--password-store=gnome-libsecret\n"
                  f"--load-extension={CANARY_HOME}/ext/one,{CANARY_HOME}/ext/two\n--no-default-browser-check\n"
                  f"--user-data-dir={CANARY_HOME}/profile\n--some-future-flag=hunter2\n")
OS_RELEASE = 'NAME="Omarchy"\nPRETTY_NAME="Omarchy"\nID=omarchy\nVERSION_ID="4.0.6"\n'
HYPRCTL_VERSION = "Hyprland 0.56.2 built from branch v0.56.2 at commit efb5099 clean.\nDate: Wed Aug 5 2026\n"
CLIENT_ENV = {"HOME": CANARY_HOME, "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "Hyprland"}
CLIENT_COMMANDS = {
    ("chromium", "--version"): "Chromium 155.0.8059.84 Arch Linux\n",
    ("pacman", "-Q", "greenlight-bin", "greenlight"): CommandResult(
        ("pacman",), 1, "greenlight-bin 2.4.2-1\n", "error: package 'greenlight' was not found\n"),
    ("pacman", "-Q", "mesa"): "mesa 1:26.2.5-1\n",
    ("vainfo",): VAINFO,
    ("omarchy", "version"): "4.0.6-1\n",
    ("hyprctl", "version"): HYPRCTL_VERSION,
}
CLIENT_FILES = {
    "/usr/share/applications/chromium.desktop": "", "/usr/share/applications/greenlight-desktop.desktop": "",
    f"{CANARY_HOME}/.config/chromium-flags.conf": CHROMIUM_FLAGS,
    "/usr/lib/dri/radeonsi_drv_video.so": "", "/usr/lib/dri/radeonsi_dri.so": "",
    "/etc/os-release": OS_RELEASE, "/proc/sys/kernel/osrelease": "7.2.15-1-omarchy\n",
    "/proc/sys/kernel/arch": "x86_64\n",
}
ALLY_DMI = {f"/sys/class/dmi/id/{name}": value + "\n" for name, value in (
    ("sys_vendor", "ASUSTeK COMPUTER INC."), ("board_vendor", "ASUSTeK COMPUTER INC."),
    ("product_name", "ROG Xbox Ally RC73YA_RC73YA"), ("product_family", "ROG Xbox Ally"), ("board_name", "RC73YA"),
    ("bios_version", "RC73YA.318"), ("product_serial", CANARY_SERIAL), ("board_serial", CANARY_SERIAL),
    ("product_uuid", "c0ffee00-" + CANARY_SERIAL))}


class ClientsTest(NetCase):
    def client_ctx(self, files=None, commands=None, env=None):
        return self.ctx(CLIENT_FILES if files is None else files, CLIENT_COMMANDS if commands is None else commands,
                        env=CLIENT_ENV if env is None else env)

    def test_browser_version_is_read_with_a_bounded_version_call(self):
        outcome = browser.check_browsers(self.client_ctx())
        self.assertEqual(outcome.status, Status.INFO)
        self.assertEqual(outcome.evidence["browsers"], [{"name": "Chromium", "version": "155.0.8059.84",
                                                         "source": "system"}])
        flatpak = {"/var/lib/flatpak/exports/share/applications/com.microsoft.Edge.desktop": ""}
        edge = browser.check_browsers(self.client_ctx(flatpak, {}))
        self.assertEqual(edge.evidence["browsers"], [{"name": "Microsoft Edge", "version": "", "source": "flatpak"}])
        self.assertEqual(browser.check_browsers(self.client_ctx({}, {})).status, Status.SKIP)

    def test_greenlight_is_detected_without_ever_being_executed(self):
        commands = FakeCommands(CLIENT_COMMANDS)
        ctx = make_context(root=self.tree({**CLIENT_FILES, "/opt/Greenlight/greenlight-desktop": ""}),
                           commands=commands, env=CLIENT_ENV)
        outcome = browser.check_greenlight(ctx)
        self.assertEqual((outcome.status, outcome.evidence), (Status.INFO, {"installed": True, "version": "2.4.2"}))
        self.assertEqual(commands.calls, [("pacman", "-Q", "greenlight-bin", "greenlight")])
        user_entry = {f"{CANARY_HOME}/.local/share/applications/Greenlight.desktop": ""}
        self.assertTrue(browser.greenlight_installed(self.client_ctx(user_entry, {})))
        missing = browser.check_greenlight(self.client_ctx({}, {}))
        self.assertEqual((missing.status, missing.evidence["installed"]), (Status.SKIP, False))

    def test_linux_web_client_support_is_always_a_warning(self):
        for files, phrase in ((CLIENT_FILES, "Use Greenlight"), ({}, "Install Xbox Remote Play")):
            outcome = browser.check_support_policy(self.client_ctx(files, {}))
            self.assertEqual(outcome.status, Status.WARN)
            self.assertIn("does not include desktop Linux", outcome.detail)
            self.assertIn(phrase, outcome.remediation)

    def test_flag_summary_names_flags_but_never_paths_or_unknown_values(self):
        outcome = browser.check_browser_flags(self.client_ctx())
        self.assertEqual(outcome.status, Status.INFO)
        self.assertEqual(outcome.evidence["files"], [{"file": "chromium-flags.conf", "flags": [
            "--ozone-platform=wayland", "--password-store=gnome-libsecret", "--load-extension=<2 path(s) not shown>",
            "--no-default-browser-check", "--user-data-dir=<value not shown>",
            "--some-future-flag=<value not shown>"]}])
        self.assertEqual(outcome.evidence["unpacked_extensions"], 2)
        text = outcome.detail + json.dumps(outcome.evidence)
        for private in (CANARY_HOME, CANARY_USER, "ext/one", "hunter2"):
            self.assertNotIn(private, text)

    def test_flags_that_break_streaming_are_called_out(self):
        flags = ("--no-sandbox\n--disable-gpu\n--force-webrtc-ip-handling-policy=disable_non_proxied_udp\n"
                 f"--proxy-server=http://bob:hunter2@proxy.{CANARY_DOMAIN}:3128\n")
        files = {f"{CANARY_HOME}/.config/chromium-flags.conf": flags}
        outcome = browser.check_browser_flags(self.client_ctx(files, {}))
        self.assertEqual(outcome.status, Status.WARN)
        self.assertEqual(len(outcome.evidence["concerns"]), 4)
        self.assertIn("stops WebRTC from using UDP", outcome.detail)
        self.assertNotIn("hunter2", outcome.detail + json.dumps(outcome.evidence))
        self.assertNotIn(CANARY_DOMAIN, outcome.detail + json.dumps(outcome.evidence))
        xdg = self.client_ctx({"/cfg/chrome-flags.conf": "--disable-gpu\n"}, {}, env={"XDG_CONFIG_HOME": "/cfg"})
        self.assertEqual(browser.check_browser_flags(xdg).evidence["files"][0]["file"], "chrome-flags.conf")
        self.assertEqual(browser.check_browser_flags(self.client_ctx({}, {})).status, Status.INFO)
        self.assertEqual(browser.check_browser_flags(self.client_ctx({}, {}, env={})).status, Status.INFO)

    def test_vaapi_h264_high_decode(self):
        outcome = browser.check_vaapi(self.client_ctx())
        self.assertEqual(outcome.status, Status.PASS)
        self.assertEqual((outcome.evidence["drivers"], outcome.evidence["h264_high"], outcome.evidence["hevc"],
                          outcome.evidence["av1"], outcome.evidence["decode_profiles"]),
                         (["radeonsi"], True, True, True, 6))
        self.assertEqual(browser.parse_vainfo(VAINFO)["driver"][:19], "Mesa Gallium driver")

    def test_vaapi_without_h264_warns_and_missing_tools_skip(self):
        no_high = VAINFO.replace("      VAProfileH264High               :	VAEntrypointVLD\n", "")
        encode_only = "\n".join(line for line in VAINFO.splitlines() if "H264" not in line or "EncSlice" in line)
        for output, phrase in ((no_high, "only below the High profile"), (encode_only, "not at all")):
            outcome = browser.check_vaapi(self.client_ctx(commands={("vainfo",): output}))
            self.assertEqual(outcome.status, Status.WARN)
            self.assertIn(phrase, outcome.detail)
        no_tool = browser.check_vaapi(self.client_ctx(commands={}))
        self.assertEqual(no_tool.status, Status.SKIP)
        self.assertIn("radeonsi", no_tool.detail)
        self.assertEqual(browser.check_vaapi(self.client_ctx({}, {})).status, Status.SKIP)

    def test_vaapi_falls_back_to_the_render_node_without_a_display(self):
        commands = {("vainfo",): CommandResult(("vainfo",), 3, "", "error: can't connect to X server!\n"),
                    ("vainfo", "--display", "drm", "--device", "/dev/dri/renderD128"): VAINFO}
        outcome = browser.check_vaapi(self.client_ctx({"/dev/dri/renderD128": ""}, commands))
        self.assertEqual(outcome.status, Status.PASS)
        failed = browser.check_vaapi(self.client_ctx({}, {("vainfo",): commands[("vainfo",)]}))
        self.assertEqual(failed.status, Status.SKIP)


class SystemTest(NetCase):
    def test_platform_facts_do_not_include_the_host_name(self):
        files = {**CLIENT_FILES, "/etc/hostname": CANARY_HOST + "\n", "/proc/sys/kernel/hostname": CANARY_HOST + "\n"}
        outcome = system.check_platform(self.ctx(files, CLIENT_COMMANDS, env=CLIENT_ENV))
        self.assertEqual(outcome.status, Status.INFO)
        expected = {"os": "Omarchy", "os_version": "4.0.6", "kernel": "7.2.15-1-omarchy", "architecture": "x86_64",
                    "omarchy": "4.0.6-1", "hyprland": "0.56.2", "session_type": "wayland", "desktop": "Hyprland",
                    "game_mode": "0.1.0"}
        self.assertEqual({key: outcome.evidence[key] for key in expected}, expected)
        self.assertEqual(outcome.detail, "Omarchy 4.0.6, Linux 7.2.15-1-omarchy x86_64, Hyprland 0.56.2, wayland "
                                         "session, Game Mode 0.1.0.")
        self.assertNotIn(CANARY_HOST, outcome.detail + json.dumps(outcome.evidence))
        bare = system.check_platform(self.ctx({}, {}, env={}))
        self.assertEqual((bare.status, bare.evidence["os"], bare.evidence["omarchy"]), (Status.INFO, "Linux", ""))

    def test_device_model_comes_from_dmi_and_serials_are_never_read(self):
        outcome = system.check_device(self.ctx(ALLY_DMI))
        self.assertEqual(outcome.status, Status.INFO)
        evidence = outcome.evidence
        self.assertEqual((evidence["board"], evidence["handheld"], evidence["handheld_verified"]),
                         ("RC73YA", "ASUS ROG Xbox Ally", True))
        self.assertIn("Known handheld: ASUS ROG Xbox Ally.", outcome.detail)
        self.assertNotIn(CANARY_SERIAL, outcome.detail + json.dumps(outcome.evidence))
        self.assertFalse(any("serial" in name or "uuid" in name for name in devices.DMI_FIELDS))
        placeholder = {"/sys/class/dmi/id/product_name": "To Be Filled By O.E.M.\n",
                       "/sys/class/dmi/id/board_name": "Default string\n"}
        self.assertEqual(system.check_device(self.ctx(placeholder)).status, Status.SKIP)

    def test_known_handheld_table_matches_field_and_vendor(self):
        cases = [({"board_name": "RC73YA", "sys_vendor": "ASUSTeK COMPUTER INC."}, "ASUS ROG Xbox Ally", True),
                 ({"board_name": "RC73XA", "sys_vendor": "ASUSTeK COMPUTER INC."}, "ASUS ROG Xbox Ally X", False),
                 ({"board_name": "RC71L", "board_vendor": "ASUSTeK COMPUTER INC."}, "ASUS ROG Ally", False),
                 ({"board_name": "RC72LA", "sys_vendor": "ASUSTeK COMPUTER INC."}, "ASUS ROG Ally X", False),
                 ({"product_name": "83E1", "sys_vendor": "LENOVO"}, "Lenovo Legion Go", False),
                 ({"product_name": "83L3", "sys_vendor": "LENOVO"}, "Lenovo Legion Go S", False),
                 ({"product_name": "Galileo", "sys_vendor": "Valve"}, "Valve Steam Deck", False)]
        for values, name, verified in cases:
            handheld = devices.known_handheld(values)
            self.assertEqual((handheld.name, handheld.verified), (name, verified))
        self.assertIsNone(devices.known_handheld({"board_name": "RC73YA", "sys_vendor": "Someone Else"}))
        self.assertIsNone(devices.known_handheld({"product_name": "RC73YA", "sys_vendor": "ASUSTeK COMPUTER INC."}))
        self.assertIsNone(devices.known_handheld({}))
        self.assertEqual([handheld.name for handheld in devices.HANDHELDS if handheld.verified], ["ASUS ROG Xbox Ally"])

    def test_gpu_and_mesa(self):
        files = {"/sys/class/drm/card1/device/vendor": "0x1002\n", "/sys/class/drm/card1/device/device": "0x163f\n",
                 "/sys/class/drm/card1-eDP-1/status": "connected\n", "/sys/bus/pci/drivers/amdgpu/.keep": ""}
        root = self.tree(files)
        (root / "sys/class/drm/card1/device/driver").symlink_to("../../../../bus/pci/drivers/amdgpu")
        outcome = system.check_gpu(make_context(root=root, commands=FakeCommands(CLIENT_COMMANDS)))
        self.assertEqual(outcome.evidence, {"gpus": [{"vendor": "AMD", "id": "1002:163f", "driver": "amdgpu"}],
                                            "mesa": "26.2.5"})
        self.assertEqual(outcome.detail, "AMD 1002:163f (amdgpu), Mesa 26.2.5.")
        other = self.ctx({"/sys/class/drm/card0/device/vendor": "0x8086\n",
                          "/sys/class/drm/card0/device/device": "0x7d55\n", "/usr/lib/libgallium-25.1.4.so": ""})
        self.assertEqual(system.check_gpu(other).detail, "Intel 8086:7d55, Mesa 25.1.4.")
        self.assertEqual(system.check_gpu(self.ctx({})).status, Status.SKIP)


# ---- controllers: a fake sysfs/dev tree that reproduces the ROG Xbox Ally -------------

# Key bitmaps captured on the device: the raw pad has BTN_SOUTH..BTN_THUMBL but no
# BTN_THUMBR; InputPlumber's virtual pad has the full set.
ALLY_RAW_KEYS = "3fff000000000000 0 0 0 0"
ELITE_PAD_KEYS = "ff 0 0 0 0 0 0 7cdb000000000000 0 8000000000 0 0"
PAD_AXES = "3003f"
NKEY_KEYBOARD_KEYS = "1000000000007 ff9f207ac14057ff febeffdfffefffff fffffffffffffffe"
USB_PARENT = "sys/devices/pci0000:00/0000:00:08.1/0000:04:00.4/usb3/3-6"
STACK_ACTIVE = ("systemctl", "is-active", "inputplumber.service", "hhd.service")
STACK_HOOK = ("systemctl", "is-enabled", "inputplumber-suspend.service")
STACK_BUS = ("busctl", "--system", "status", "org.shadowblip.InputPlumber")


def add_input(root, number, name, keys, axes="0", vendor="0b05", product="1b4c", virtual=False, mode=0o660,
              bus="0003"):
    """One evdev device laid out like sysfs, with canary serials next to the fields the doctor reads."""
    parent = "sys/devices/virtual/input" if virtual else f"{USB_PARENT}/3-6:1.5/0003:0B05:1B4C.0006/input"
    base = root / parent / f"input{number}"
    for relative, text in (("name", name), ("id/bustype", bus), ("id/vendor", vendor), ("id/product", product),
                           ("id/version", "0110"), ("capabilities/key", keys), ("capabilities/abs", axes),
                           ("uniq", CANARY_SERIAL), ("phys", f"usb-0000:04:00.4-6/input5 {CANARY_MAC}")):
        write(base / relative, text + "\n")
    event = base / f"event{number}"
    event.mkdir()
    (event / "device").symlink_to("..")
    link = root / "sys/class/input" / f"event{number}"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(event, link.parent))
    write(root / "dev/input" / f"event{number}").chmod(mode)


def add_hidraw(root, number, interface, mode, vendor="0B05", product="1B4C"):
    device = root / USB_PARENT / f"3-6:1.{interface}" / f"0003:{vendor}:{product}.000{number + 1}"
    write(device / "uevent", f"DRIVER=hid-generic\nHID_ID=0003:0000{vendor}:0000{product}\n"
                             "HID_NAME=ASUSTeK Computer Inc. N-KEY Device\n"
                             f"HID_PHYS=usb-0000:04:00.4-6/input{interface}\nHID_UNIQ={CANARY_SERIAL}\n")
    write(device.parent / "bInterfaceNumber", f"{interface:02x}\n")
    node = device / "hidraw" / f"hidraw{number}"
    node.mkdir(parents=True)
    (node / "device").symlink_to("../..")
    link = root / "sys/class/hidraw" / f"hidraw{number}"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(node, link.parent))
    write(root / "dev" / f"hidraw{number}").chmod(mode)


def stack_commands(inputplumber=False, hhd=False, hook="enabled", bus=True):
    states = "\n".join("active" if on else "inactive" for on in (inputplumber, hhd)) + "\n"
    commands = {STACK_ACTIVE: CommandResult(STACK_ACTIVE, 0 if inputplumber and hhd else 3, states)}
    if inputplumber:
        commands[("pacman", "-Q", "inputplumber")] = "inputplumber 0.82.0-1\n"
        commands[STACK_HOOK] = CommandResult(STACK_HOOK, 0 if hook == "enabled" else 1, hook + "\n")
        commands[STACK_BUS] = CommandResult(STACK_BUS, 0 if bus else 1, f"PID=15029\nUID=0\nOwner={CANARY_USER}\n")
    return commands


def pad(node="event6", name="ASUSTeK Computer Inc. N-KEY Device", keys=ALLY_RAW_KEYS, virtual=False,
        identity="0b05:1b4c", access="readable"):
    return evdev.InputDevice(node, name, "USB", identity, virtual, frozenset(evdev.parse_bitmap(keys)),
                             frozenset(evdev.parse_bitmap(PAD_AXES)), access, 3, 0x0110)


def keyboard(node="event5", name="ASUSTeK Computer Inc. N-KEY Device", virtual=False, identity="0b05:1b4c"):
    return evdev.InputDevice(node, name, "USB", identity, virtual, frozenset(evdev.parse_bitmap(NKEY_KEYBOARD_KEYS)),
                             frozenset(), "readable", 3, 0x0110)


RAW_PAD, ELITE_PAD = pad(), pad("event21", "Microsoft X-Box One Elite 2 pad", ELITE_PAD_KEYS, True, "045e:0b00")
NKEY_KEYS, PLUMBER_KEYS = keyboard(), keyboard("event20", "InputPlumber Keyboard", True, "1234:5678")


class InputCase(ScratchCase):
    def ally(self, *, inputplumber=False, hide_raw=None, virtual_pad=None, processes=("antimicrox",), **stack):
        """The Ally's input devices: raw pad (hidden when InputPlumber runs) plus what the daemon adds."""
        hide_raw = inputplumber if hide_raw is None else hide_raw
        virtual_pad = inputplumber if virtual_pad is None else virtual_pad
        shutil.rmtree(SCRATCH, ignore_errors=True)
        root = SCRATCH
        for path, text in ALLY_DMI.items():
            write(root / path.lstrip("/"), text)
        add_input(root, 5, "ASUSTeK Computer Inc. N-KEY Device", NKEY_KEYBOARD_KEYS, mode=0o000 if hide_raw else 0o660)
        add_input(root, 6, "ASUSTeK Computer Inc. N-KEY Device", ALLY_RAW_KEYS, PAD_AXES,
                  mode=0o000 if hide_raw else 0o660)
        if virtual_pad:
            add_input(root, 21, "Microsoft X-Box One Elite 2 pad", ELITE_PAD_KEYS, PAD_AXES, "045e", "0b00", True)
        for pid, name in enumerate((*processes, *(("inputplumber",) if inputplumber else ())), 1100):
            write(root / f"proc/{pid}/comm", name + "\n")
        write(root / "proc/self/comm", "python3\n")
        self.commands = FakeCommands(stack_commands(inputplumber, **stack))
        return make_context(root=root, commands=self.commands)


class InputStackTest(InputCase):
    def test_capability_bitmaps_and_code_names(self):
        raw = evdev.parse_bitmap(ALLY_RAW_KEYS)
        self.assertEqual(raw, set(range(evdev.BTN_SOUTH, evdev.BTN_THUMBL + 1)))
        self.assertNotIn(evdev.BTN_THUMBR, raw)
        elite = evdev.parse_bitmap(ELITE_PAD_KEYS)
        self.assertTrue({evdev.BTN_THUMBR, evdev.BTN_MODE, evdev.BTN_TRIGGER_HAPPY1} <= elite)
        self.assertEqual(evdev.parse_bitmap("3003f"), {0, 1, 2, 3, 4, 5, 0x10, 0x11})
        self.assertEqual(evdev.parse_bitmap("zz 1"), {0})
        self.assertEqual([evdev.code_label(evdev.EV_KEY, code) for code in (314, 316, 318, 184, 191, 148, 0x2c1)],
                         ["BTN_SELECT (314)", "BTN_MODE (316)", "BTN_THUMBR (318)", "KEY_F14 (184)", "KEY_F21 (191)",
                          "KEY_PROG1 (148)", "BTN_TRIGGER_HAPPY2 (705)"])
        self.assertEqual(evdev.code_label(evdev.EV_ABS, 0x11), "ABS_HAT0Y (17)")
        self.assertEqual((evdev.key_name(0x150), evdev.key_name(250), evdev.abs_name(0x30)),
                         ("BTN_0x150", "KEY_250", "ABS_0x30"))
        self.assertEqual({evdev.KEY_NAMES[code] for code in range(183, 195)}, {f"KEY_F{n}" for n in range(13, 25)})

    def test_ally_raw_pad_is_enumerated_without_reading_serials(self):
        found = evdev.gamepads(self.ally())
        self.assertEqual(len(found), 1)
        pad = found[0]
        self.assertEqual((pad.node, pad.name, pad.bus, pad.vendor_product, pad.virtual, pad.access, pad.has_guide),
                         ("event6", "ASUSTeK Computer Inc. N-KEY Device", "USB", "0b05:1b4c", False, "readable", True))
        self.assertEqual(pad.missing_controls(), ["R3 (BTN_THUMBR)"])
        devices_seen = evdev.input_devices(self.ally())
        self.assertEqual([(device.node, device.is_gamepad, device.is_keyboard) for device in devices_seen],
                         [("event5", False, True), ("event6", True, False)])
        self.assertNotIn(CANARY_SERIAL, repr(devices_seen))
        self.assertNotIn(CANARY_MAC, repr(devices_seen))

    def test_raw_pad_without_a_daemon_warns_about_the_missing_r3(self):
        ctx = self.ally()
        pads = inputstack.check_gamepads(ctx)
        self.assertEqual(pads.status, Status.WARN)
        self.assertIn("does not list R3 (BTN_THUMBR)", pads.detail)
        self.assertIn("numbered by a generic driver, so a press can arrive under another button's code", pads.detail)
        self.assertIn("`omarchy-game-mode doctor buttons` shows what each press really sends", pads.remediation)
        self.assertEqual(pads.evidence["gamepads"], [{
            "name": "ASUSTeK Computer Inc. N-KEY Device", "node": "event6", "kind": "physical", "bus": "USB",
            "id": "0b05:1b4c", "access": "readable", "missing": ["R3 (BTN_THUMBR)"], "guide": True,
            "hid_order": True}])
        verdict = inputstack.check_handheld(ctx)
        self.assertEqual(verdict.status, Status.WARN)
        self.assertIn("ASUS ROG Xbox Ally: no handheld input daemon is running", verdict.detail)
        self.assertIn("does not list R3 (BTN_THUMBR)", verdict.detail)
        self.assertIn("`omarchy pkg add inputplumber` then `sudo systemctl enable --now inputplumber.service "
                      "inputplumber-suspend.service`", verdict.remediation)
        stack = inputstack.check_input_stack(ctx)
        self.assertEqual((stack.status, stack.evidence["inputplumber"], stack.evidence["antimicrox"]),
                         (Status.INFO, False, True))

    def test_generic_hid_numbering_is_recognised_from_the_capabilities_alone(self):
        self.assertTrue(RAW_PAD.hid_ordered)
        self.assertFalse(ELITE_PAD.hid_ordered)
        complete_but_generic = pad(keys="7fff000000000000 0 0 0 0")
        self.assertEqual((complete_but_generic.missing_controls(), complete_but_generic.hid_ordered), ([], True))
        self.assertIn("numbered by a generic driver", inputstack._describe(complete_but_generic))
        self.assertNotIn("generic driver", inputstack._describe(ELITE_PAD))

    def test_advice_is_printed_and_nothing_is_installed_or_started(self):
        ctx = self.ally()
        for check in (inputstack.check_gamepads, inputstack.check_input_stack, inputstack.check_handheld,
                      inputstack.check_hidraw_access):
            check(ctx)
        self.assertEqual(self.commands.calls, [STACK_ACTIVE])

    def test_inputplumber_with_hidden_raw_pad_and_virtual_pad_passes(self):
        ctx = self.ally(inputplumber=True)
        pads = inputstack.check_gamepads(ctx)
        self.assertEqual(pads.status, Status.PASS)
        rows = [(row["node"], row["kind"], row["access"], row["missing"]) for row in pads.evidence["gamepads"]]
        self.assertEqual(rows, [("event6", "physical", "hidden", ["R3 (BTN_THUMBR)"]),
                                ("event21", "virtual", "readable", [])])
        verdict = inputstack.check_handheld(ctx)
        self.assertEqual((verdict.status, verdict.evidence["daemon"]), (Status.PASS, "InputPlumber"))
        stack = inputstack.check_input_stack(ctx)
        self.assertEqual((stack.evidence["inputplumber_bus"], stack.evidence["suspend_hook"],
                          stack.evidence["inputplumber_version"]), (True, "enabled", "0.82.0"))
        self.assertIn("InputPlumber: running 0.82.0 (answers on the system bus", stack.detail)
        self.assertNotIn(CANARY_USER, stack.detail + json.dumps(stack.evidence))

    def test_handheld_verdicts(self):
        ally = devices.known_handheld({"board_name": "RC73YA", "sys_vendor": "ASUSTeK COMPUTER INC."})
        legion = devices.known_handheld({"product_name": "83L3", "sys_vendor": "LENOVO"})

        def pad(virtual, keys, access="readable"):
            return evdev.InputDevice("event1", "Pad", "USB", "0000:0000", virtual, frozenset(evdev.parse_bitmap(keys)),
                                     frozenset(evdev.parse_bitmap(PAD_AXES)), access)
        raw, hidden, full = pad(False, ALLY_RAW_KEYS), pad(False, ALLY_RAW_KEYS, "hidden"), pad(True, ELITE_PAD_KEYS)
        plumber = inputstack.Stack(inputplumber=True, inputplumber_bus=True, suspend_hook="enabled")
        cases = [
            (None, [raw], inputstack.Stack(), Status.INFO, "not a handheld"),
            (ally, [raw], inputstack.Stack(), Status.WARN, "no handheld input daemon"),
            (ally, [], inputstack.Stack(antimicrox=True), Status.WARN, "no handheld input daemon"),
            (ally, [hidden, full], plumber, Status.PASS, "complete virtual controller"),
            (ally, [hidden], plumber, Status.WARN, "no complete virtual controller"),
            (ally, [raw, full], plumber, Status.WARN, "still readable"),
            (ally, [hidden, full], inputstack.Stack(inputplumber=True, suspend_hook="disabled"), Status.WARN,
             "after sleep"),
            (ally, [hidden, full], inputstack.Stack(hhd=True), Status.PASS, "Handheld Daemon provides"),
        ]
        for handheld, pads, stack, status, phrase in cases:
            outcome = inputstack.handheld_verdict(handheld, pads, stack)
            self.assertEqual(outcome.status, status, phrase)
            self.assertIn(phrase, outcome.detail)
        untested = inputstack.handheld_verdict(legion, [raw], inputstack.Stack())
        self.assertIn("this project has not tested it", untested.remediation)
        self.assertNotIn("has not tested", inputstack.handheld_verdict(ally, [raw], inputstack.Stack()).remediation)

    def test_two_handheld_daemons_conflict_and_no_or_unreadable_pads_are_reported(self):
        both = inputstack.check_input_stack(self.ally(inputplumber=True, hhd=True))
        self.assertEqual(both.status, Status.WARN)
        self.assertIn("competing", both.detail)
        shutil.rmtree(SCRATCH)
        SCRATCH.mkdir()
        self.assertEqual(inputstack.check_gamepads(make_context(root=SCRATCH)).status, Status.SKIP)
        unreadable = inputstack.check_gamepads(self.ally(hide_raw=True))
        self.assertEqual(unreadable.status, Status.WARN)
        self.assertIn("No gamepad is readable", unreadable.detail)

    @unittest.skipIf(os.geteuid() == 0, "root can read every node")
    def test_node_without_read_permission_is_told_apart_from_a_hidden_one(self):
        ctx = self.ally()
        (SCRATCH / "dev/input/event6").chmod(0o200)
        self.assertEqual(evdev.gamepads(ctx)[0].access, "no-permission")
        (SCRATCH / "dev/input/event6").unlink()
        self.assertEqual(evdev.input_devices(ctx)[1].access, "missing")

    def hidraw_ctx(self, modes, rules=(), **ally):
        ctx = self.ally(**ally)
        for number, mode in enumerate(modes):
            add_hidraw(SCRATCH, number, number, mode)
        add_hidraw(SCRATCH, 9, 0, 0o600, vendor="0603", product="F200")
        for name in rules:
            write(SCRATCH / "run/udev/rules.d" / name, "")
        return ctx

    @unittest.skipIf(os.geteuid() == 0, "root can read every node")
    def test_hidden_hid_interfaces_are_reported_with_evidence_and_a_manual_command(self):
        # Write-only stands in for "root only": the test user owns these files.
        rules = ("50-inputplumber-hide-hidraw2-early.rules", "96-inputplumber-hide-hidraw5-late.rules",
                 "70-other.rules")
        outcome = inputstack.check_hidraw_access(self.hidraw_ctx((0o200, 0o200, 0o000, 0o200, 0o200, 0o000), rules,
                                                                 inputplumber=True))
        self.assertEqual(outcome.status, Status.WARN)
        self.assertIn("6 of 6 raw HID interfaces of the built-in controller are not readable by this user "
                      "(2 hidden with mode 000, 4 readable by root only)", outcome.detail)
        self.assertIn("Another input daemon may be hiding the controller interfaces: InputPlumber is running; "
                      "2 temporary udev rules from InputPlumber hide device nodes.", outcome.detail)
        self.assertIn("stop it yourself (`sudo systemctl stop inputplumber.service`)", outcome.remediation)
        self.assertIn("The doctor never does this for you.", outcome.remediation)
        self.assertEqual(outcome.evidence["interfaces"][2], {"node": "hidraw2", "interface": 2, "mode": "000",
                                                             "access": "hidden"})
        self.assertEqual((len(outcome.evidence["interfaces"]), outcome.evidence["hide_rules"],
                          outcome.evidence["inputplumber"]), (6, 2, True))
        self.assertNotIn(CANARY_SERIAL, outcome.detail + json.dumps(outcome.evidence))
        self.assertNotIn(("sudo",), [call[:1] for call in self.commands.calls])

    @unittest.skipIf(os.geteuid() == 0, "root can read every node")
    def test_default_root_only_interfaces_are_not_blamed_on_a_daemon(self):
        default = inputstack.check_hidraw_access(self.hidraw_ctx((0o200, 0o200)))
        self.assertEqual(default.status, Status.INFO)
        self.assertIn("system default", default.detail)
        shutil.rmtree(SCRATCH)
        SCRATCH.mkdir()
        readable = inputstack.check_hidraw_access(self.hidraw_ctx((0o660, 0o660)))
        self.assertEqual(readable.status, Status.PASS)

    def test_devices_without_asus_interfaces_skip(self):
        ctx = self.ally()
        add_hidraw(SCRATCH, 6, 0, 0o600, vendor="0603", product="F200")
        self.assertEqual(inputstack.check_hidraw_access(ctx).status, Status.SKIP)
        self.assertEqual([(node.node, node.vendor, node.interface) for node in evdev.hidraw_nodes(ctx)],
                         [("hidraw6", 0x0603, 0)])


# ---- a complete fake device: every input source carries canaries ---------------------

IP_RULES_WITH_ADDRESS = IP_RULES_DEFAULT.replace("32766:", f"100:\tfrom {CANARY_LAN_IP} lookup main\n32766:")
TIMEDATECTL = ("timedatectl", "show", "-p", "NTP", "-p", "NTPSynchronized")


class World(InputCase):
    """A healthy ROG Xbox Ally behind a home router, as the doctor's primitives would see it."""

    def world(self, options=None, *, console_answers=True, files=None, **ally):
        self.ally(inputplumber=True, **ally)
        overrides = files or {}
        files = {
            **CLIENT_FILES, "/proc/net/route": WIFI_ROUTES, "/proc/net/if_inet6": IF_INET6_LOCAL,
            "/proc/net/ipv6_route": IPV6_ROUTE_NO_DEFAULT, "/proc/net/dev": PROC_NET_DEV,
            "/proc/net/arp": arp("0x2"), "/etc/resolv.conf": RESOLV_STUB, "/etc/hostname": CANARY_HOST + "\n",
            "/proc/sys/kernel/hostname": CANARY_HOST + "\n", "/sys/class/net/wlp3s0/wireless/.keep": "",
            "/sys/class/net/wlp3s0/mtu": "1500\n", "/etc/default/ufw": UFW_DEFAULTS, "/etc/ufw/user.rules": UFW_RULES,
            "/sys/class/drm/card1/device/vendor": "0x1002\n", "/sys/class/drm/card1/device/device": "0x163f\n",
            "/run/udev/rules.d/50-inputplumber-hide-hidraw5-early.rules": "", **overrides,
        }
        for path, text in files.items():
            write(SCRATCH / path.lstrip("/"), text)
        for number in range(6):
            add_hidraw(SCRATCH, number, number, 0o000 if number in (2, 5) else 0o200)
        commands = {
            **self.commands.table, **CLIENT_COMMANDS, **UFW_ACTIVE, **iw_commands(journal=JOURNAL_EVENTS),
            ("resolvectl", "status"): RESOLVECTL_STATUS, ("ip", "-4", "rule", "show"): IP_RULES_WITH_ADDRESS,
            TIMEDATECTL: "NTP=yes\nNTPSynchronized=yes\n", PING_GATEWAY: PING_CLEAN,
            pmtu_argv(1200): ping_reply(2), pmtu_argv(1472): ping_reply(2),
        }
        self.commands = FakeCommands(commands)

        def respond(data, destination, sock):
            if destination[1] == 123:
                return ntp_server(0.2)(data, destination, sock)
            request = stun.decode(data)
            if request.method == stun.ALLOCATE:
                return [(allocate_error(request, 401, "Unauthorized"), destination)]
            if destination[0] == CONSOLE_IP and not console_answers:
                return []
            return [(binding_success(request, (CANARY_PUBLIC_IP, sock.local[1])), destination)]

        self.udp = UdpWorld(respond)
        self.tls = FakeTls()
        env = {**CLIENT_ENV, "USER": CANARY_USER, "HTTPS_PROXY": f"http://{CANARY_USER}:pw@proxy.{CANARY_DOMAIN}:3128"}
        options = options or Options()
        return make_context(
            root=SCRATCH, commands=self.commands, env=env, options=options,
            getaddrinfo=fake_dns(ipv6={"www.xbox.com": [CANARY_PUBLIC_IP6]}), udp_socket=self.udp.socket,
            tcp_connect=lambda address, timeout: FakeTcp(), tls_context=self.tls,
            redactor=canary_redactor(mask_addresses=options.redact))


class PlanTest(World):
    def test_scopes_select_checks_and_environment_facts_are_always_included(self):
        ids = {scope: [check.id for check in checks.plan(Options(scope=scope))] for scope in checks.SCOPES}
        self.assertEqual(ids["system"], ["system.platform", "system.device", "system.gpu"])
        self.assertEqual(ids["input"][:3], ids["system"])
        self.assertEqual(ids["input"][3:], ["input.gamepads", "input.stack", "input.handheld", "input.hidraw-access"])
        self.assertEqual(ids["all"], ids["streaming"] + ids["input"][3:])
        self.assertFalse(any(name.startswith("input.") for name in ids["streaming"]))
        for expected in ("stun.binding", "turn.challenge", "nat.mapping_stability", "dns.required", "tls443.required",
                         "wifi.link", "wifi.events", "firewall.rules", "browser.support_policy", "video.vaapi",
                         "console.reachability", "manual.console_account_region", "capture.failed_attempt"):
            self.assertIn(expected, ids["streaming"])
        self.assertEqual(len(ids["all"]), len(set(ids["all"])))

    def test_console_route_is_planned_only_with_an_address_and_nothing_discovers_or_launches(self):
        self.assertNotIn("console.route", [check.id for check in checks.plan(Options())])
        with_console = [check.id for check in checks.plan(Options(console_ip=CONSOLE_IP))]
        self.assertLess(with_console.index("console.route"), with_console.index("console.reachability"))
        everything = " ".join(check.id for check in checks.plan(Options(console_ip=CONSOLE_IP)))
        for removed in ("lan", "smartglass", "upnp", "webrtc", "deep"):
            self.assertNotIn(removed, everything)

    def test_decisive_evidence_is_exactly_firewall_console_and_a_live_attempt(self):
        decisive = [check.id for check in checks.plan(Options()) if check.decisive]
        self.assertEqual(decisive, ["firewall.rules", "manual.console_account_region", "capture.failed_attempt"])

    def test_every_check_states_what_it_proves_and_does_not_prove(self):
        for check in checks.plan(Options(console_ip=CONSOLE_IP)):
            for text in (check.title, check.proves, check.does_not_prove):
                self.assertTrue(text and len(text) <= 100, check.id)
            self.assertIn(check.category, checks.CATEGORY_TITLES, check.id)

    def test_every_evidence_field_a_check_emits_is_declared(self):
        worlds = [(Options(console_ip=CONSOLE_IP), {}), (Options(), {"hide_raw": False, "hook": "disabled"})]
        for options, ally in worlds:
            ctx = self.world(options, **ally)
            for check in checks.plan(options):
                dropped = []
                outcome = check.run(ctx)
                runner.allowlisted(outcome.evidence, (*runner.BASE_FIELDS, *check.fields), dropped)
                self.assertEqual(dropped, [], check.id)

    def test_healthy_world_has_no_failure_and_only_the_expected_warnings(self):
        options = Options(console_ip=CONSOLE_IP)
        results = runner.run_checks(checks.plan(options), self.world(options), workers=1)
        by_status = collections.defaultdict(list)
        for result in results:
            by_status[result.status].append(result.id)
        self.assertEqual(by_status[Status.FAIL], [])
        self.assertEqual(by_status[Status.WARN], ["network.vpn_proxy", "dns.resolver", "wifi.link", "wifi.events",
                                                  "browser.support_policy", "input.hidraw-access"])
        self.assertEqual(by_status[Status.SKIP], ["firewall.rules", "manual.console_account_region",
                                                  "capture.failed_attempt"])
        self.assertEqual([result.id for result in results], [check.id for check in checks.plan(options)])

    def test_the_doctor_runs_only_read_only_commands(self):
        options = Options(console_ip=CONSOLE_IP)
        runner.run_checks(checks.plan(options), self.world(options), workers=1)
        tools = {call[0] for call in self.commands.calls}
        self.assertEqual(tools, {"omarchy", "hyprctl", "pacman", "ip", "resolvectl", "timedatectl", "iw", "journalctl",
                                 "ping", "systemctl", "chromium", "vainfo", "busctl"})
        for call in self.commands.calls:
            self.assertNotIn(call[0], ("sudo", "pkexec", "su", "doas", "greenlight", "greenlight-desktop", "steam"))
            if call[0] == "systemctl":
                self.assertIn(call[1], ("is-active", "is-enabled"))
            if call[0] == "pacman":
                self.assertEqual(call[1], "-Q")
            if call[0] == "omarchy":
                self.assertEqual(call, ("omarchy", "version"))
            if call[0] == "busctl":
                self.assertEqual(call[:3], ("busctl", "--system", "status"))


# ---- command line, reports and privacy end to end ------------------------------------

RAW_MAC = re.compile(r"(?i)\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")
RAW_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


class CliCase(World):
    def run_cli(self, *argv, world=None):
        """Run the doctor command against the fake device; returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        self.contexts = []

        def factory(options):
            self.contexts.append(self.world(options, **(world or {})))
            return self.contexts[-1]
        code = cli.main(list(argv), stdout=out, stderr=err, context_factory=factory)
        return code, out.getvalue(), err.getvalue()


class CliTest(CliCase):
    def test_default_run_is_text_for_all_scopes_and_warnings_do_not_fail(self):
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.contexts[0].options, Options(scope="all", timeout=6.0, redact=True))
        for marker in ("[PASS]", "[WARN]", "[SKIP]", "[INFO]"):
            self.assertIn(marker, out)
        self.assertNotIn("[FAIL]", out)
        self.assertIn("Omarchy Game Mode doctor (all)", out)
        self.assertIn("Device:      ASUSTeK COMPUTER INC. ROG Xbox Ally RC73YA_RC73YA, board RC73YA", out)
        self.assertEqual(out.count("proves: "), out.count("  ["))

    def test_report_is_inconclusive_while_decisive_evidence_is_unverified(self):
        _code, out, _err = self.run_cli("streaming")
        self.assertIn("Result: INCONCLUSIVE: no failures in what was tested, 5 warnings. Not verified: Live", out)
        self.assertIn("Console settings and account, Live failed attempt.", out)
        self.assertIn("cannot verify without root \u2014 run: sudo ufw status numbered verbose", out)
        self.assertNotIn("Result: PASS", out)
        _code, out, _err = self.run_cli("input")
        self.assertIn("Result: No failures; 1 warning, first: Controller HID interfaces.", out)
        _code, out, _err = self.run_cli("system")
        self.assertIn("Result: Environment facts only; nothing was tested.", out)

    def test_a_failed_check_sets_exit_status_one(self):
        code, out, _err = self.run_cli("streaming", world={"files": {"/proc/net/route": route_table()}})
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] IPv4 default route", out)
        self.assertIn("Result: FAIL: 1 check failed, first: IPv4 default route.", out)
        self.assertEqual(json.loads(self.run_cli("--json", world={"files": {"/proc/net/route": route_table()}})[1])
                         ["summary"]["exit_code"], 1)

    def test_usage_errors_exit_two_and_help_exits_zero(self):
        for argv in (["nonsense"], ["--json", "--markdown"], ["--timeout", "0"], ["--timeout", "abc"],
                     ["--console-ip", CANARY_PUBLIC_IP], ["--console-ip", "not-an-ip"], ["--interface", "wl an;0"],
                     ["--no-redact", "--markdown"], ["--deep"], ["--lan"], ["all", "extra"]):
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, out), (2, ""), argv)
            self.assertIn("usage: omarchy-game-mode doctor", err)
            self.assertEqual(self.contexts, [], argv)
        code, out, err = self.run_cli("--help")
        self.assertEqual((code, err), (0, ""))
        for phrase in ("streaming", "input", "system", "--console-ip", "--markdown", "no root", "Exit status"):
            self.assertIn(phrase, out)

    def test_options_reach_the_checks(self):
        code, _out, _err = self.run_cli("streaming", "--console-ip", CONSOLE_IP, "--timeout", "9", "--interface",
                                        "wlp3s0", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.contexts[0].options, Options("streaming", 9.0, CONSOLE_IP, "wlp3s0", True))

    def test_interrupt_and_closed_pipe_are_not_tracebacks(self):
        def interrupted(_options):
            raise KeyboardInterrupt
        err = io.StringIO()
        self.assertEqual(cli.main([], stdout=io.StringIO(), stderr=err, context_factory=interrupted), 130)
        self.assertIn("Interrupted", err.getvalue())

        def closed(_options):
            raise BrokenPipeError
        self.assertEqual(cli.main([], stdout=io.StringIO(), stderr=io.StringIO(), context_factory=closed), 0)

    def test_report_survives_a_terminal_that_cannot_encode_it(self):
        class Latin1(io.StringIO):
            encoding = "latin-1"

            def write(self, text):
                text.encode(self.encoding)
                return super().write(text)
        stream = Latin1()
        cli.emit(stream, "cannot verify without root \u2014 run: sudo nft -a list ruleset\n")
        self.assertEqual(stream.getvalue(), "cannot verify without root ? run: sudo nft -a list ruleset\n")
        plain = io.StringIO()
        cli.emit(plain, "dash \u2014 kept\n")
        self.assertEqual(plain.getvalue(), "dash \u2014 kept\n")

    def test_json_report_schema(self):
        code, out, _err = self.run_cli("--json", "--console-ip", CONSOLE_IP)
        document = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(document), ["checks", "duration_ms", "environment", "finished_at", "mode", "privacy",
                                            "run_id", "schema", "schema_version", "scope", "started_at", "summary"])
        self.assertEqual((document["schema"], document["schema_version"], document["mode"], document["scope"]),
                         ("omarchy-game-mode.doctor/v1", 1, "doctor", "all"))
        self.assertRegex(document["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(document["run_id"], r"^[0-9a-f]{32}$")
        summary = document["summary"]
        self.assertEqual(sorted(summary), ["exit_code", "fail", "headline", "info", "pass", "skip", "status",
                                           "unverified", "warn"])
        self.assertEqual((summary["status"], summary["exit_code"], summary["fail"]), ("inconclusive", 0, 0))
        self.assertEqual(summary["unverified"], ["firewall.rules", "manual.console_account_region",
                                                 "capture.failed_attempt"])
        counted = sum(summary[name] for name in ("pass", "warn", "fail", "skip", "info"))
        self.assertEqual(counted, len(document["checks"]))
        for check in document["checks"]:
            self.assertEqual(sorted(check), ["category", "decisive", "detail", "does_not_prove", "duration_ms",
                                             "evidence", "id", "proves", "remediation", "root", "status", "title"])
            self.assertIn(check["status"], ("pass", "warn", "fail", "skip", "info"))
            self.assertTrue(check["proves"] and check["does_not_prove"], check["id"])
        by_id = {check["id"]: check for check in document["checks"]}
        self.assertEqual(by_id["firewall.rules"]["root"], {
            "required": True, "status": "not_run", "cannot_verify_without_root": True,
            "command": "sudo ufw status numbered verbose"})
        self.assertEqual(by_id["stun.binding"]["root"], {"required": False})
        self.assertEqual(document["privacy"], {
            "redacted": True, "addresses_masked": True, "root_used": False, "tokens_read": False,
            "accounts_read": False, "cookies_read": False, "browser_profiles_read": False, "lan_discovery": False,
            "console_address_probed": True})

    def test_environment_is_built_from_allowlisted_evidence(self):
        _code, out, _err = self.run_cli("--json")
        env = json.loads(out)["environment"]
        self.assertEqual((env["os"], env["kernel"], env["hyprland"], env["game_mode"]),
                         ("Omarchy", "7.2.15-1-omarchy", "0.56.2", "0.1.0"))
        self.assertEqual(env["device"]["board"], "RC73YA")
        self.assertEqual(env["network"], {"interface": "wlp3s0", "interface_type": "wifi", "ipv6_state": "local-only"})
        self.assertEqual(env["wireless"], {"band": "5 GHz", "frequency_mhz": 5640, "channel": 128, "width_mhz": 160,
                                           "dfs": True, "signal_dbm": -44, "generation": "Wi-Fi 6",
                                           "power_save": False})
        self.assertEqual(env["clients"], [{"name": "Chromium", "version": "155.0.8059.84"},
                                          {"name": "Greenlight", "version": "2.4.2"}])
        self.assertTrue(env["controllers"]["inputplumber"])
        for forbidden in ("ssid", "bssid", "hostname", "mac", "serial", "ipv4", "user"):
            self.assertNotIn(forbidden, json.dumps(env).lower())

    def test_text_output_is_plain_ascii_apart_from_the_dash_in_the_root_notice(self):
        _code, out, _err = self.run_cli()
        self.assertEqual({char for char in out if ord(char) > 126}, {"\u2014"})
        self.assertLessEqual(max(len(line) for line in out.splitlines()), report.TEXT_WIDTH)

    def test_markdown_report_is_issue_ready(self):
        code, out, _err = self.run_cli("--markdown", "--console-ip", CONSOLE_IP)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### Omarchy Game Mode doctor report\n\n**Result:** INCONCLUSIVE"))
        for line in ("| Environment |  |", "| Status | Check | Result |", "| Check | Proves | Does not prove |",
                     "<details><summary>What each check proves and does not prove</summary>", "</details>",
                     "**Next steps**"):
            self.assertIn(line, out)
        self.assertIn("| PASS | UDP and STUN | All 3 STUN servers answered", out)
        self.assertIn("public address &lt;public-ip-", out)
        self.assertNotIn("<public-ip-", out)
        self.assertIn("- **Live firewall rules** (SKIP): run `sudo ufw status numbered verbose`.", out)
        self.assertIn("-g 'UFW (BLOCK|ALLOW)'`", out)
        self.assertIn("| UDP and STUN (`stun.binding`) | Outbound UDP works and replies come back. |", out)
        table_rows = [line for line in out.splitlines() if line.startswith("| ")]
        self.assertTrue(all(line.endswith(" |") for line in table_rows))
        self.assertEqual({line.replace("\\|", "").count("|") for line in table_rows} - {3}, {4})

    def test_markdown_escaping(self):
        self.assertEqual(report.markdown_escape("a|b <tag> `x|<y>` & c\nd"), "a\\|b &lt;tag&gt; `x\\|<y>` &amp; c d")
        self.assertEqual(report.markdown_escape("run `a|b` <now>", table=False), "run `a|b` &lt;now&gt;")


class PrivacyTest(CliCase):
    STYLES = ((), ("--json",), ("--markdown",))

    def outputs(self, *extra, world=None):
        return {style: self.run_cli(*style, *extra, world=world)[1] for style in self.STYLES}

    def assert_private(self, text, label):
        for canary in CANARIES:
            self.assertNotIn(canary, text, f"{canary!r} leaked into {label}")
        for pattern, kind in ((RAW_MAC, "MAC address"), (RAW_IPV4, "IPv4 address")):
            leaked = [match for match in pattern.findall(text) if not match.startswith(("127.", "0.0.0.0"))]
            self.assertEqual(leaked, [], f"{kind} leaked into {label}")
        self.assertNotIn("/home/", text, label)

    def test_no_canary_survives_in_text_json_or_markdown(self):
        for style, output in self.outputs("--console-ip", CONSOLE_IP).items():
            self.assert_private(output, style or "text")
            self.assertGreater(len(output), 2000)

    def test_the_canaries_really_were_in_the_inputs(self):
        ctx = self.world(Options(console_ip=CONSOLE_IP))
        raw = cli.diagnose(ctx)
        evidence = json.dumps([check.evidence for check in raw.checks])
        self.assertIn(CANARY_PUBLIC_IP, evidence)
        self.assertIn(CANARY_LAN_IP, json.dumps([check.detail for check in raw.checks]))
        tools = "".join(str(value) for value in self.commands.table.values())
        for canary in (CANARY_SSID, CANARY_BSSID, CANARY_MAC, CANARY_HOST, CANARY_DOMAIN, CANARY_USER, CANARY_LAN_IP):
            self.assertIn(canary, tools)
        self.assertIn(CANARY_SERIAL, (SCRATCH / "sys/class/dmi/id/product_serial").read_text())
        self.assertIn(CANARY_SERIAL, (SCRATCH / "sys/class/hidraw/hidraw5/device/uevent").read_text())

    def test_public_address_gets_one_token_everywhere_in_a_report(self):
        document = json.loads(self.run_cli("--json")[1])
        by_id = {check["id"]: check for check in document["checks"]}
        mapped = {row["mapped_address"] for row in by_id["stun.binding"]["evidence"]["servers"]}
        mapped |= {row["mapped_address"] for row in by_id["nat.mapping_stability"]["evidence"]["mappings"]}
        self.assertEqual(len(mapped), 1)
        token = mapped.pop()
        self.assertRegex(token, r"^<public-ip-\d+>$")
        self.assertIn(token, by_id["stun.binding"]["detail"])
        self.assertEqual(by_id["network.ipv4.default_route"]["evidence"]["gateway"], "192.168.x.x")
        resolvers = {row["address"] for row in by_id["dns.resolver"]["evidence"]["servers"]}
        self.assertTrue(all(re.fullmatch(r"<public-ip-\d+>", address) for address in resolvers))
        self.assertNotIn(token, resolvers)

    def test_no_redact_shows_addresses_but_never_identities(self):
        code, out, _err = self.run_cli("--no-redact", "--console-ip", CONSOLE_IP)
        self.assertEqual(code, 0)
        self.assertFalse(self.contexts[0].options.redact)
        for visible in (CANARY_PUBLIC_IP, CANARY_LAN_IP, "192.168.77.1"):
            self.assertIn(visible, out)
        for hidden in (CANARY_SSID, CANARY_BSSID, CANARY_MAC, CANARY_HOST, CANARY_DOMAIN, CANARY_USER, CANARY_SERIAL):
            self.assertNotIn(hidden, out)
        self.assertIn("do not post this output", out)
        self.assertFalse(json.loads(self.run_cli("--no-redact", "--json")[1])["privacy"]["redacted"])

    def test_a_check_that_tries_to_leak_is_stopped_by_allowlist_redaction_and_final_scrub(self):
        def leaky(ctx):
            ctx.secret(CANARY_SSID)
            everything = (f"ssid {CANARY_SSID} bssid {CANARY_BSSID} host {CANARY_HOST} at {CANARY_PUBLIC_IP} and "
                          f"{CANARY_LAN_IP} in {CANARY_HOME}/x token=abc123secret serial")
            return model.warn(everything, everything, declared=everything, undeclared=CANARY_SERIAL,
                              rows=[{"ok": everything, "uniq": CANARY_SERIAL}])
        rogue = runner.Check("rogue.leak", f"Leak {CANARY_BSSID}", "system", leaky, "p", "n",
                             ("declared", "rows", "rows.ok"))
        self.addCleanup(setattr, cli, "plan", cli.plan)
        cli.plan = lambda options: [rogue]
        for style, output in self.outputs().items():
            self.assert_private(output, f"rogue {style}")
            self.assertNotIn("abc123secret", output)
            self.assertIn("rogue.leak" if style else "Leak", output)

    def test_final_scrub_catches_what_the_first_pass_never_saw(self):
        redactor = canary_redactor()
        clean = model.Report("all", [model.CheckResult("a.b", "T", Status.PASS, "fine")])

        def poisoned(_report):
            return f"late {CANARY_BSSID} {CANARY_PUBLIC_IP} {CANARY_HOME}\n"
        self.addCleanup(setattr, cli, "render_text", cli.render_text)
        self.addCleanup(setattr, cli, "render_markdown", cli.render_markdown)
        cli.render_text = cli.render_markdown = poisoned
        for style in ("text", "markdown"):
            self.assertEqual(cli.render(clean, redactor, style), "late <mac> <public-ip-1> ~\n")
        cli.render_json = lambda _report: json.dumps({"note": f"{CANARY_BSSID} {CANARY_HOME}", "token": "abc"})
        self.addCleanup(setattr, cli, "render_json", report.render_json)
        self.assertEqual(json.loads(cli.render(clean, redactor, "json")), {"note": "<mac> ~", "token": "<redacted>"})


class EntryPointTest(unittest.TestCase):
    """The doctor runs in the invoking process, without GTK and without a resident instance."""

    BLOCK_GI = (
        "import importlib.abc, sys\n"
        "class NoGtk(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'gi' or name.startswith('gi.'):\n"
        "            raise ImportError('gi is blocked in this test')\n"
        "sys.meta_path.insert(0, NoGtk())\n")

    def python(self, script, *argv):
        return subprocess.run([sys.executable, "-I", "-c", script, *argv], cwd=ROOT, capture_output=True, text=True,
                              timeout=60)

    def test_doctor_never_imports_gtk_from_any_entry_point(self):
        script = self.BLOCK_GI + (
            "import io, pkgutil, runpy, contextlib\n"
            "sys.path.insert(0, '.')\n"
            "import gaminghome.doctor as package\n"
            "names = [module.name for module in pkgutil.iter_modules(package.__path__)]\n"
            "assert {'cli', 'net', 'udp', 'wifi', 'inputstack', 'report'} <= set(names), names\n"
            "for name in names:\n"
            "    __import__('gaminghome.doctor.' + name)\n"
            "from gaminghome.doctor import cli\n"
            "assert cli.main(['--help'], stdout=io.StringIO()) == 0\n"
            "assert cli.main(['--nope'], stderr=io.StringIO()) == 2\n"
            "for entry, argv0 in (('omarchy-game-mode.py', 'omarchy-game-mode'), ('gaminghome/__main__.py', 'x')):\n"
            "    sys.argv = [argv0, 'doctor', '--help']\n"
            "    try:\n"
            "        with contextlib.redirect_stdout(io.StringIO()) as out:\n"
            "            runpy.run_path(entry, run_name='__main__') if entry.endswith('mode.py') else "
            "runpy.run_module('gaminghome', run_name='__main__')\n"
            "    except SystemExit as exit:\n"
            "        assert exit.code == 0, exit.code\n"
            "    assert 'usage: omarchy-game-mode doctor' in out.getvalue()\n"
            "loaded = sorted(name for name in sys.modules if name == 'gi' or name.startswith('gi.'))\n"
            "assert not loaded, loaded\n"
            "assert 'gaminghome.application' not in sys.modules\n"
            "print('no-gtk-ok')\n")
        result = self.python(script)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "no-gtk-ok"), result.stderr)

    def test_launcher_still_needs_gtk_for_everything_else(self):
        script = self.BLOCK_GI + ("import runpy\nsys.path.insert(0, '.')\nsys.argv = ['omarchy-game-mode', 'status']\n"
                                  "runpy.run_path('omarchy-game-mode.py', run_name='__main__')\n")
        result = self.python(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gi is blocked in this test", result.stderr)

    def test_application_main_answers_doctor_before_creating_the_application(self):
        from gaminghome import application

        class Forbidden:
            def __init__(self):
                raise AssertionError("doctor must not create or contact the GTK application")
        original, application.GameModeApplication = application.GameModeApplication, Forbidden
        self.addCleanup(setattr, application, "GameModeApplication", original)
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr, redirect_stdout
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(application.main(["omarchy-game-mode", "doctor", "--help"]), 0)
            self.assertEqual(application.main(["omarchy-game-mode", "doctor", "--nope"]), 2)
        self.assertIn("usage: omarchy-game-mode doctor", out.getvalue())
        self.assertIn("doctor [SCOPE|buttons]", application.USAGE)
        with self.assertRaises(AssertionError):
            application.main(["omarchy-game-mode", "status"])

    def test_importing_the_application_does_not_load_the_doctor(self):
        script = ("import sys\nsys.path.insert(0, '.')\nimport gaminghome.application\n"
                  "print(sorted(name for name in sys.modules if name.startswith('gaminghome.doctor')))\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "[]"), result.stderr)


# ---- guided button test: scripted events against a fake clock --------------------------

# Report descriptor of the ROG Xbox Ally's gamepad interface (identical on every unit):
# report 0x0b declares 14 buttons at bits 96-109, followed by 2 padding bits.
ALLY_REPORT_DESCRIPTOR = bytes.fromhex(
    "05010905a101850ba10009300931150026ffff350046ffff950275108102c0a10009330934150026ffff950275108102c0"
    "05010932150026ff039501750a81021500250075069501810305010935150026ff039501750a810215002500750695018103"
    "05091901290e150025017501950e810295017502810105010939150125083500463b016514750495018102950175048101"
    "050f0921850da102099715002501750495019102150025007504950191030970150025647508950491020950660110550e"
    "150026ff0075089501910209a7150026ff0075089501910265005500097c150026ff00750895019102c0c0")
STICK = buttons.AxisInfo(0, 65535, 32768)
TRIGGER = buttons.AxisInfo(0, 1023, 0)
PAD_AXIS_INFO = {evdev.ABS_X: STICK, evdev.ABS_Y: STICK, evdev.ABS_RX: STICK, evdev.ABS_RY: STICK,
                 evdev.ABS_Z: TRIGGER, evdev.ABS_RZ: TRIGGER}
KEY_F14, KEY_F15, KEY_F16, KEY_PROG1 = 184, 185, 186, 148


def key(code, device=0, at=0.5, hold=0.1):
    """A press and release, `at` seconds into the prompt."""
    return [(at, buttons.Event("input", device, evdev.EV_KEY, code, 1)),
            (at + hold, buttons.Event("input", device, evdev.EV_KEY, code, 0))]


def axis(code, value, device=0, at=0.5):
    return [(at, buttons.Event("input", device, evdev.EV_ABS, code, value))]


def typed(kind, at=0.5):
    return [(at, buttons.Event(kind))]


def raw_report(bits=(), at=0.5):
    payload = bytearray(15)
    for bit in bits:
        payload[bit // 8] |= 1 << bit % 8
    return [(at, buttons.Event("raw", data=bytes([0x0B]) + bytes(payload)))]


# What a standard Xbox-style pad sends for each control.
STANDARD = {
    "guide": key(evdev.BTN_MODE), "view": key(evdev.BTN_SELECT), "menu": key(evdev.BTN_START),
    "a": key(evdev.BTN_SOUTH), "b": key(evdev.BTN_EAST), "x": key(evdev.BTN_NORTH), "y": key(evdev.BTN_WEST),
    "lb": key(evdev.BTN_TL), "rb": key(evdev.BTN_TR), "l3": key(evdev.BTN_THUMBL), "r3": key(evdev.BTN_THUMBR),
    "lt": axis(evdev.ABS_Z, 1023) + axis(evdev.ABS_Z, 0, at=0.6),
    "rt": axis(evdev.ABS_RZ, 1023) + axis(evdev.ABS_RZ, 0, at=0.6),
    "dpad_up": axis(evdev.ABS_HAT0Y, -1) + axis(evdev.ABS_HAT0Y, 0, at=0.6),
    "dpad_down": axis(evdev.ABS_HAT0Y, 1) + axis(evdev.ABS_HAT0Y, 0, at=0.6),
    "dpad_left": axis(evdev.ABS_HAT0X, -1) + axis(evdev.ABS_HAT0X, 0, at=0.6),
    "dpad_right": axis(evdev.ABS_HAT0X, 1) + axis(evdev.ABS_HAT0X, 0, at=0.6),
    "ls_right": axis(evdev.ABS_X, 65535) + axis(evdev.ABS_X, 32768, at=0.6),
    "ls_down": axis(evdev.ABS_Y, 65535) + axis(evdev.ABS_Y, 32768, at=0.6),
    "rs_right": axis(evdev.ABS_RX, 65535) + axis(evdev.ABS_RX, 32768, at=0.6),
    "rs_down": axis(evdev.ABS_RY, 65535) + axis(evdev.ABS_RY, 32768, at=0.6),
}


class ScriptedSource:
    """Plays scripted events against a fake clock: `script[control]` is a list of (seconds into the prompt, event)."""

    def __init__(self, script, keys_down=None, axes=None, loose=()):
        self.script, self.now, self.queue, self.order = script, 0.0, [], 0
        self._keys, self._axes = keys_down or {}, axes if axes is not None else {0: PAD_AXIS_INFO}
        self.prompts, self.closed = [], False
        for at, event in loose:
            self._push(at, event)

    def _push(self, at, event):
        self.order += 1
        heapq.heappush(self.queue, (at, self.order, event))

    def clock(self):
        return self.now

    def keys_down(self):
        return self._keys

    def axes(self):
        return self._axes

    def begin_prompt(self, control):
        self.prompts.append((control.key, round(self.now, 3)))
        for delay, event in self.script.get(control.key, ()):
            self._push(self.now + delay, event)

    def poll(self, timeout):
        if self.queue and self.queue[0][0] <= self.now + timeout:
            at, _order, event = heapq.heappop(self.queue)
            self.now = max(self.now, at)
            return [event]
        self.now += timeout
        return []

    def close(self):
        self.closed = True


class Recorder(buttons.Listener):
    def __init__(self):
        self.lines = []

    def prompt(self, index, total, control):
        self.lines.append(f"ask {control.key}")

    def observed(self, control, observation):
        self.lines.append(f"{control.key}: {observation.label()}")

    def finished(self, result):
        self.lines.append(f"{result.control.key} {result.status}")


def run_session(script, *, timeout=12.0, controls=buttons.CONTROLS, watcher=None, **source):
    scripted = ScriptedSource(script, **source)
    recorder = Recorder()
    session = buttons.Session(scripted, buttons.Tracker(scripted.keys_down(), scripted.axes()), scripted.clock,
                              timeout=timeout, controls=controls, watcher=watcher, listener=recorder)
    return {result.control.key: result for result in session.run()}, scripted, recorder


def only(*keys):
    return [control for control in buttons.CONTROLS if control.key in keys]


class ButtonSessionTest(unittest.TestCase):
    def test_script_covers_every_requested_control_once(self):
        keys = [control.key for control in buttons.CONTROLS]
        self.assertEqual(keys, ["guide", "view", "menu", "a", "b", "x", "y", "lb", "rb", "lt", "rt", "l3", "r3",
                                "dpad_up", "dpad_down", "dpad_left", "dpad_right", "ls_right", "ls_down", "rs_right",
                                "rs_down", "paddle_left", "paddle_right", "armoury", "library", "armoury_hold",
                                "library_hold"])
        held = [control.key for control in buttons.CONTROLS if control.hold]
        self.assertEqual(held, ["armoury_hold", "library_hold"])
        self.assertTrue(all(control.name and control.prompt for control in buttons.CONTROLS))

    def test_standard_pad_passes_every_standard_control(self):
        results, source, recorder = run_session(STANDARD)
        standard = [control.key for control in buttons.CONTROLS if control.expected]
        self.assertEqual({key: buttons.outcome(results[key]) for key in standard}, {key: "pass" for key in standard})
        self.assertEqual(results["guide"].observations, [buttons.Observation(0, "key", evdev.BTN_MODE)])
        self.assertEqual(recorder.lines[:3], ["ask guide", "guide: BTN_MODE (316)", "guide observed"])
        self.assertEqual([buttons.outcome(results[key]) for key in sorted(buttons.EXTRA_BUTTONS)], ["no event"] * 6)
        self.assertEqual(len(source.prompts), len(buttons.CONTROLS))

    def test_a_press_ends_the_prompt_after_a_short_settle_and_silence_runs_into_the_timeout(self):
        results, source, _recorder = run_session({"guide": key(evdev.BTN_MODE, at=2.0)}, timeout=5.0,
                                                 controls=only("guide", "view", "menu"))
        self.assertEqual([results[name].status for name in ("guide", "view", "menu")],
                         ["observed", "timeout", "timeout"])
        self.assertEqual(source.prompts, [("guide", 0.0), ("view", 2.25), ("menu", 7.25)])
        self.assertEqual(buttons.outcome(results["view"]), "no event")

    def test_enter_skips_a_control_and_quit_leaves_the_rest_untested(self):
        script = {"guide": typed("skip"), "view": key(evdev.BTN_SELECT), "menu": typed("quit"),
                  "a": key(evdev.BTN_SOUTH)}
        results, _source, _recorder = run_session(script, controls=only("guide", "view", "menu", "a"))
        self.assertEqual([results[name].status for name in ("guide", "view", "menu", "a")],
                         ["skipped", "observed", "not tested", "not tested"])
        self.assertEqual([buttons.outcome(results[name]) for name in ("guide", "menu", "a")], ["not tested"] * 3)
        self.assertEqual(results["a"].observations, [])

    def test_enter_typed_by_a_button_mapper_right_after_a_press_is_not_a_skip(self):
        script = {"a": key(evdev.BTN_SOUTH, at=0.5) + typed("skip", at=0.52),
                  "b": typed("skip", at=0.05) + key(evdev.BTN_EAST, at=1.0),
                  "x": typed("skip", at=3.0)}
        results, _source, _recorder = run_session(script, controls=only("a", "b", "x"))
        self.assertEqual([results[name].status for name in ("a", "b", "x")], ["observed", "observed", "skipped"])

    def test_buttons_held_when_the_test_starts_or_across_prompts_are_ignored_until_released(self):
        held_at_start = {0: {evdev.BTN_SOUTH}}
        script = {"guide": [(0.2, buttons.Event("input", 0, evdev.EV_KEY, evdev.BTN_SOUTH, 2))],
                  "view": [(0.2, buttons.Event("input", 0, evdev.EV_KEY, evdev.BTN_SOUTH, 0)),
                           *key(evdev.BTN_SOUTH, at=0.4, hold=30)],
                  "menu": [(0.2, buttons.Event("input", 0, evdev.EV_KEY, evdev.BTN_SOUTH, 2)),
                           *key(evdev.BTN_START, at=0.5)]}
        results, _source, _recorder = run_session(script, controls=only("guide", "view", "menu"), timeout=3.0,
                                                  keys_down=held_at_start)
        self.assertEqual(results["guide"].status, "timeout")
        self.assertEqual([seen.code for seen in results["view"].observations], [evdev.BTN_SOUTH])
        self.assertEqual([seen.code for seen in results["menu"].observations], [evdev.BTN_START])

    def test_trigger_counts_only_past_the_threshold_and_once_per_pull(self):
        tracker = buttons.Tracker(axes={0: PAD_AXIS_INFO})
        feed = lambda value: tracker.feed(0, evdev.EV_ABS, evdev.ABS_Z, value)  # noqa: E731
        self.assertEqual([feed(value) for value in (40, 200, 357)], [None, None, None])
        self.assertEqual(feed(359), buttons.Observation(0, "axis", evdev.ABS_Z, 1))
        self.assertEqual([feed(value) for value in (700, 1023, 400, 200)], [None, None, None, None])
        self.assertIsNone(feed(150))
        self.assertEqual(feed(900), buttons.Observation(0, "axis", evdev.ABS_Z, 1))

    def test_stick_drift_is_ignored_and_direction_is_reported(self):
        tracker = buttons.Tracker(axes={0: PAD_AXIS_INFO})
        feed = lambda code, value: tracker.feed(0, evdev.EV_ABS, code, value)  # noqa: E731
        self.assertEqual([feed(evdev.ABS_X, value) for value in (32768, 33900, 31000, 40000, 26000)], [None] * 5)
        self.assertEqual(feed(evdev.ABS_X, 65000), buttons.Observation(0, "axis", evdev.ABS_X, 1))
        self.assertEqual(feed(evdev.ABS_X, 300), buttons.Observation(0, "axis", evdev.ABS_X, -1))
        self.assertIsNone(feed(evdev.ABS_X, 32768))
        self.assertIsNone(feed(0x28, 500))
        self.assertIsNone(buttons.Tracker().feed(0, evdev.EV_ABS, evdev.ABS_X, 65535))
        self.assertIsNone(buttons.Tracker().feed(0, 0x04, 4, 458756))

    def test_hat_edges(self):
        tracker = buttons.Tracker()
        feed = lambda code, value: tracker.feed(0, evdev.EV_ABS, code, value)  # noqa: E731
        self.assertEqual(feed(evdev.ABS_HAT0Y, -1), buttons.Observation(0, "hat", evdev.ABS_HAT0Y, -1))
        self.assertIsNone(feed(evdev.ABS_HAT0Y, -1))
        self.assertEqual(feed(evdev.ABS_HAT0Y, 1), buttons.Observation(0, "hat", evdev.ABS_HAT0Y, 1))
        self.assertIsNone(feed(evdev.ABS_HAT0Y, 0))
        self.assertEqual(feed(evdev.ABS_HAT0X, 1).label(), "ABS_HAT0X (16) +")
        dpad = only("dpad_up", "dpad_down", "dpad_left", "dpad_right")
        results, _source, _recorder = run_session(STANDARD, controls=dpad)
        self.assertEqual({buttons.outcome(result) for result in results.values()}, {"pass"})

    def test_one_press_seen_on_two_devices_and_a_long_press_that_changes_code_are_both_kept(self):
        script = {"paddle_left": key(KEY_F14, device=1) + key(KEY_F14, device=2, at=0.55),
                  "armoury_hold": key(KEY_F16, device=1, at=0.5, hold=2.0) + key(KEY_PROG1, device=1, at=1.9)}
        results, source, _recorder = run_session(script, controls=only("paddle_left", "armoury_hold"))
        self.assertEqual([(seen.device, seen.code) for seen in results["paddle_left"].observations],
                         [(1, KEY_F14), (2, KEY_F14)])
        self.assertEqual([seen.code for seen in results["armoury_hold"].observations], [KEY_F16, KEY_PROG1])
        self.assertEqual(source.prompts[1][1], 0.75)
        self.assertEqual(buttons.outcome(results["paddle_left"]), "recorded")


class ButtonVerdictTest(unittest.TestCase):
    def verdicts(self, script, devices=(RAW_PAD, NKEY_KEYS), primary=RAW_PAD, **kw):
        results, _source, _recorder = run_session(script, **kw)
        ordered = [results[control.key] for control in kw.get("controls", buttons.CONTROLS)]
        return {verdict.key: verdict for verdict in buttons.verdicts(ordered, list(devices), primary)}, ordered

    def test_xbox_button_reported_as_select_is_explained_with_a_text_only_suggestion(self):
        found, _results = self.verdicts({**STANDARD, "guide": key(evdev.BTN_SELECT)})
        guide = found["guide"]
        self.assertEqual(guide.status, Status.WARN)
        self.assertIn("The Xbox button reports BTN_SELECT (314), the code of View/Back, instead of BTN_MODE (316). "
                      "Steam will treat it as View/Back and will not open its menu; a handheld daemon profile can "
                      "remap it.", guide.detail)
        self.assertIn("View reports the same code", guide.detail)
        self.assertIn("nothing is written", guide.advice)
        self.assertIn("Xbox and View both report BTN_SELECT (314)", found["duplicates"].detail)

    def test_xbox_button_verdict_follows_only_what_was_observed(self):
        mode, _results = self.verdicts(STANDARD)
        self.assertEqual((mode["guide"].status, "duplicates" in mode), (Status.PASS, False))
        other, _results = self.verdicts({**STANDARD, "guide": key(KEY_F16, device=1), "view": typed("skip")})
        self.assertIn("reports KEY_F16 (186) instead of BTN_MODE (316)", other["guide"].detail)
        self.assertNotIn("View reports the same code", other["guide"].detail)
        silent, _results = self.verdicts({key_: value for key_, value in STANDARD.items() if key_ != "guide"})
        self.assertIn("sent nothing on the watched devices", silent["guide"].detail)
        skipped, _results = self.verdicts({**STANDARD, "guide": typed("skip")})
        self.assertNotIn("guide", skipped)

    def test_r3_missing_from_the_raw_ally_pad(self):
        script = {key_: value for key_, value in STANDARD.items() if key_ != "r3"}
        found, results = self.verdicts(script)
        self.assertEqual(found["r3"].status, Status.WARN)
        self.assertIn("R3 unavailable (needs the Ally HID kernel driver)", found["r3"].detail)
        self.assertIn("does not list BTN_THUMBR", found["r3"].detail)
        self.assertEqual(buttons.outcome(next(r for r in results if r.control.key == "r3")), "no event")
        virtual, _results = self.verdicts(script, devices=(ELITE_PAD,), primary=ELITE_PAD)
        self.assertIn("R3 was not seen, although the controller lists BTN_THUMBR", virtual["r3"].detail)
        other_vendor, _results = self.verdicts(script, primary=pad(identity="1234:0001"))
        self.assertIn("R3 unavailable (its driver does not expose it)", other_vendor["r3"].detail)
        ok, _results = self.verdicts(STANDARD, devices=(ELITE_PAD,), primary=ELITE_PAD)
        self.assertEqual(ok["r3"].status, Status.PASS)
        wrong, _results = self.verdicts({**STANDARD, "r3": key(evdev.BTN_THUMBL)})
        self.assertIn("R3 reports BTN_THUMBL (317) instead of BTN_THUMBR (318)", wrong["r3"].detail)

    def test_paddles_and_vendor_buttons_arriving_as_keys_on_a_readable_keyboard_leak(self):
        script = {**STANDARD, "paddle_left": key(KEY_F14, device=1), "paddle_right": key(KEY_F15, device=1),
                  "armoury": key(KEY_F16, device=1), "library": key(KEY_PROG1, device=1)}
        found, _results = self.verdicts(script)
        leak = found["extra_keys"]
        self.assertEqual(leak.status, Status.WARN)
        self.assertIn("the keys leak into applications; use InputPlumber", leak.detail)
        for expected in ("Left back paddle: KEY_F14 (184)", "Right back paddle: KEY_F15 (185)",
                         "Command Center button: KEY_F16 (186)", "Library button: KEY_PROG1 (148)"):
            self.assertIn(expected, leak.detail)
        routed, _results = self.verdicts(script, devices=(ELITE_PAD, PLUMBER_KEYS), primary=ELITE_PAD)
        self.assertEqual(routed["extra_keys"].status, Status.INFO)
        self.assertIn("virtual keyboard", routed["extra_keys"].detail)
        on_pad = {**STANDARD, "paddle_left": key(evdev.BTN_TRIGGER_HAPPY1), "paddle_right": key(0x2C1)}
        self.assertNotIn("extra_keys", self.verdicts(on_pad, devices=(ELITE_PAD,), primary=ELITE_PAD)[0])

    def test_two_buttons_with_one_code_are_ambiguous_but_a_short_and_long_press_of_one_button_are_not(self):
        script = {**STANDARD, "paddle_left": key(KEY_F14, device=1), "paddle_right": key(KEY_F14, device=1),
                  "armoury": key(KEY_F16, device=1), "armoury_hold": key(KEY_F16, device=1, hold=2.0)}
        found, _results = self.verdicts(script)
        self.assertEqual(found["duplicates"].status, Status.WARN)
        self.assertIn("Left back paddle and Right back paddle both report KEY_F14 (184)", found["duplicates"].detail)
        self.assertNotIn("Command Center", found["duplicates"].detail)
        same_code_other_device = {**STANDARD, "armoury": key(KEY_F16, device=1), "library": key(KEY_F16, device=0)}
        self.assertNotIn("duplicates", self.verdicts(same_code_other_device)[0])

    def test_unexpected_codes_and_silent_controls_are_listed(self):
        script = {**STANDARD, "lb": key(evdev.BTN_TR), "ls_right": axis(evdev.ABS_X, 0)}
        del script["menu"]
        found, results = self.verdicts(script)
        self.assertIn("LB reports BTN_TR (311), expected BTN_TL (310)", found["mismatch"].detail)
        self.assertIn("Left stick right reports ABS_X (0) -, expected ABS_X (0) +", found["mismatch"].detail)
        self.assertIn("Menu", found["silent"].detail)
        self.assertNotIn("held", found["silent"].detail)
        by_key = {result.control.key: buttons.outcome(result) for result in results}
        self.assertEqual((by_key["lb"], by_key["menu"], by_key["x"]), ("differs", "no event", "pass"))
        self.assertEqual(buttons.expected_label(buttons.CONTROLS[0]), "BTN_MODE (316)")
        self.assertEqual(buttons.expected_label(only("paddle_left")[0]), "no standard code")

    def test_buttons_numbered_in_hid_order_are_diagnosed_and_still_get_a_correct_sdl_line(self):
        order = ("a", "b", "x", "y", "lb", "rb", "view", "menu", "l3", "r3", "guide")
        script = {**STANDARD, **{name: key(evdev.BTN_SOUTH + index) for index, name in enumerate(order)}}
        found, results = self.verdicts(script)
        self.assertEqual(found["hid_order"].status, Status.WARN)
        self.assertIn("a generic driver numbered them without knowing the layout", found["hid_order"].detail)
        self.assertIn("The Xbox button reports BTN_SELECT (314), the code of View/Back", found["guide"].detail)
        self.assertNotIn("View reports the same code", found["guide"].detail)
        self.assertIn("R3 reports BTN_TR2 (313) instead of BTN_THUMBR (318)", found["r3"].detail)
        self.assertIn("X reports BTN_C (306), expected BTN_NORTH (307)", found["mismatch"].detail)
        self.assertNotIn("duplicates", found)
        line, missing = buttons.sdl_mapping(results, RAW_PAD, 0)
        self.assertIn(",guide:b10,back:b6,start:b7,a:b0,b:b1,x:b2,y:b3,leftshoulder:b4,rightshoulder:b5,", line)
        self.assertIn(",leftstick:b8,rightstick:b9,", line)
        self.assertEqual(missing, ["paddle2 (Left back paddle)", "paddle1 (Right back paddle)"])
        self.assertNotIn("hid_order", self.verdicts(STANDARD)[0])
        few = {name: key(evdev.BTN_SOUTH + index) for index, name in enumerate(order[:5])}
        self.assertNotIn("hid_order", self.verdicts(few, controls=only(*order[:5]))[0])

    def test_sdl_mapping_is_built_only_from_observed_presses(self):
        script = {key_: value for key_, value in STANDARD.items() if key_ not in ("r3", "menu")}
        _found, results = self.verdicts(script)
        line, missing = buttons.sdl_mapping(results, RAW_PAD, 0)
        self.assertEqual(line, "03000000050b00004c1b000010010000,ASUSTeK Computer Inc. N-KEY Device,guide:b12,"
                               "back:b10,a:b0,b:b1,x:b3,y:b4,leftshoulder:b6,rightshoulder:b7,lefttrigger:a2,"
                               "righttrigger:a5,leftstick:b13,dpup:h0.1,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,"
                               "leftx:a0,lefty:a1,rightx:a3,righty:a4,platform:Linux,")
        self.assertEqual(missing, ["start (Menu)", "rightstick (R3)", "paddle2 (Left back paddle)",
                                   "paddle1 (Right back paddle)"])
        self.assertNotIn("rightstick", line)
        self.assertEqual(buttons.sdl_guid(ELITE_PAD), "030000005e040000000b000010010000")

    def test_sdl_mapping_ignores_other_devices_and_marks_inverted_axes(self):
        script = {"a": key(evdev.BTN_SOUTH), "paddle_left": key(KEY_F14, device=1),
                  "paddle_right": key(evdev.BTN_TRIGGER_HAPPY1), "ls_right": axis(evdev.ABS_X, 0),
                  "lt": key(evdev.BTN_TL2)}
        results, _source, _recorder = run_session(script, controls=only("a", "lt", "ls_right", "paddle_left",
                                                                        "paddle_right"))
        ordered = [results[name] for name in ("a", "lt", "ls_right", "paddle_left", "paddle_right")]
        line, missing = buttons.sdl_mapping(ordered, ELITE_PAD, 0)
        self.assertEqual(line, "030000005e040000000b000010010000,Microsoft X-Box One Elite 2 pad,a:b0,leftx:a0~,"
                               "paddle1:b11,platform:Linux,")
        self.assertEqual(missing, ["lefttrigger (LT)", "paddle2 (Left back paddle)"])


class RawHidTest(unittest.TestCase):
    def test_descriptor_declares_fourteen_buttons_then_two_padding_bits(self):
        fields = hid.parse_report_descriptor(ALLY_REPORT_DESCRIPTOR)
        self.assertEqual([(item.offset, item.bits, item.constant) for item in fields], [
            (0, 32, False), (32, 32, False), (64, 10, False), (74, 6, True), (80, 10, False), (90, 6, True),
            (96, 14, False), (110, 2, True), (112, 4, False), (116, 4, True)])
        self.assertEqual({item.report_id for item in fields}, {0x0B})
        button_field = next(item for item in fields if item.is_buttons)
        self.assertEqual((button_field.usage_min, button_field.usage_max, button_field.bits), (1, 14, 14))
        self.assertTrue(hid.uses_report_ids(fields))
        self.assertEqual(hid.RawWatcher(fields).summary(), {"report_id": 0x0B, "declared_buttons": 14})

    def test_malformed_descriptors_do_not_raise(self):
        for data in (b"", b"\x05", b"\xfe", b"\xfe\x03\x00", ALLY_REPORT_DESCRIPTOR[:37], bytes(range(256)),
                     b"\x75\x08\x95\xff\x81\x02" * 2000):
            self.assertIsInstance(hid.parse_report_descriptor(data), list)
        four_buttons = bytes.fromhex("05091901290475019504810295048101")
        self.assertEqual(hid.parse_report_descriptor(four_buttons),
                         [hid.Field(0, 0, 4, False, 9, 1, 4), hid.Field(0, 4, 4, True, 9, 0, 0)])

    def test_only_bits_outside_the_declared_fields_are_reported_and_only_once(self):
        watcher = hid.RawWatcher(hid.parse_report_descriptor(ALLY_REPORT_DESCRIPTOR))

        def report(*bits):
            return raw_report(bits)[0][1].data
        self.assertEqual(watcher.feed(report()), [])
        self.assertEqual(watcher.feed(report(96, 109, 0, 40, 112)), [])
        found = watcher.feed(report(110))
        self.assertEqual([(bit.report_id, bit.bit) for bit in found], [(0x0B, 110)])
        self.assertEqual(found[0].where, "padding right after the 14 declared buttons (where button 15 would be)")
        self.assertEqual(watcher.feed(report(110)), [])
        self.assertEqual([bit.where for bit in watcher.feed(report(111, 75))],
                         ["padding between declared fields",
                          "padding right after the 14 declared buttons (where button 16 would be)"])
        self.assertEqual(watcher.feed(b""), [])
        unnumbered = hid.RawWatcher([hid.Field(0, 0, 4, False, 9, 1, 4), hid.Field(0, 4, 4, True, 9)])
        self.assertEqual(unnumbered.feed(b"\x00"), [])
        self.assertEqual([bit.bit for bit in unnumbered.feed(b"\x1f")], [4])

    def test_r3_found_in_a_padding_bit_of_the_raw_report_ends_the_prompt_and_explains_itself(self):
        watcher = hid.RawWatcher(hid.parse_report_descriptor(ALLY_REPORT_DESCRIPTOR))
        script = {"l3": raw_report(at=0.1) + key(evdev.BTN_THUMBL) + raw_report((109,), at=0.5),
                  "r3": raw_report((110,), at=0.7) + raw_report(at=0.9)}
        results, source, _recorder = run_session(script, controls=only("l3", "r3"), watcher=watcher)
        self.assertEqual(results["l3"].raw_bits, [])
        self.assertEqual([(bit.bit, bit.report_id) for bit in results["r3"].raw_bits], [(110, 0x0B)])
        self.assertEqual((results["r3"].status, buttons.outcome(results["r3"])), ("observed", "no event"))
        verdict = buttons.verdicts([results["l3"], results["r3"]], [RAW_PAD], RAW_PAD)[0]
        self.assertIn("pressing it changed bit 110 of raw HID report 0x0b, padding right after the 14 declared "
                      "buttons", verdict.detail)
        self.assertIn("The button exists in the hardware", verdict.detail)
        rows = buttons.control_rows([results["r3"]], [RAW_PAD])
        self.assertEqual(rows[0]["observed"], [{"node": "hidraw", "event": "raw HID bit 110 of report 0x0b",
                                                "type": "raw", "event_code": 110, "direction": 0}])

    def test_raw_reports_without_a_watcher_are_ignored(self):
        results, _source, _recorder = run_session({"r3": raw_report((110,))}, controls=only("r3"), timeout=2.0)
        self.assertEqual((results["r3"].status, results["r3"].raw_bits), ("timeout", []))


class FakeDevFS(FileSystem):
    """The scratch tree cannot hold real device nodes: treat its files under /dev as character devices."""

    def is_char_device(self, absolute):
        return absolute.startswith("/dev/") and self.mode(absolute) is not None


class ButtonCommandTest(InputCase):
    """`doctor buttons` end to end on a fake sysfs/dev tree, with scripted presses instead of hardware."""

    def device_ctx(self, **ally):
        self.ally(**ally)
        if ally.get("inputplumber"):
            add_input(SCRATCH, 20, "InputPlumber Keyboard", NKEY_KEYBOARD_KEYS, vendor="1234", product="5678",
                      virtual=True)
        add_input(SCRATCH, 1, "AT Translated Set 2 keyboard", NKEY_KEYBOARD_KEYS, vendor="0001", product="0001",
                  bus="0011")
        for path, text in CLIENT_FILES.items():
            write(SCRATCH / path.lstrip("/"), text)
        self.commands.table.update(CLIENT_COMMANDS)
        return make_context(fs=FakeDevFS(SCRATCH), commands=self.commands, env=CLIENT_ENV)

    def run_buttons(self, ctx, script, *argv, terminal=True, **source):
        out, err = io.StringIO(), io.StringIO()
        self.sources = []

        def factory(selection, raw_node):
            self.selection, self.raw_node = selection, raw_node
            self.sources.append(ScriptedSource(script, **source))
            return self.sources[-1]
        tty = (lambda _stream: True) if terminal is True else terminal
        code = buttonsio.main(list(argv), out, err, ctx=ctx, stdin=io.StringIO(), is_terminal=tty,
                              source_factory=factory)
        return code, out.getvalue(), err.getvalue()

    def test_refuses_to_run_without_a_terminal_and_opens_nothing(self):
        ctx = self.device_ctx()
        code, out, err = self.run_buttons(ctx, {}, terminal=lambda _stream: False)
        self.assertEqual((code, out, self.sources), (2, "", []))
        self.assertIn("is interactive: run it in a terminal", err)
        out_stream, err_stream = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["buttons"], stdout=out_stream, stderr=err_stream), 2)
        self.assertIn("is interactive", err_stream.getvalue())
        self.assertEqual(self.commands.calls, [])

    def test_report_can_be_redirected_only_when_the_prompts_still_reach_a_terminal(self):
        ctx = self.device_ctx()
        out, err, stdin = io.StringIO(), io.StringIO(), io.StringIO()
        sources = []

        def factory(_selection, _raw):
            sources.append(ScriptedSource(STANDARD))
            return sources[-1]

        def run(*argv, terminals):
            return buttonsio.main(list(argv), out, err, ctx=ctx, stdin=stdin, source_factory=factory,
                                  is_terminal=lambda stream: any(stream is terminal for terminal in terminals))
        self.assertEqual(run(terminals=(stdin, err)), 2)
        self.assertEqual(run("--json", terminals=(out, err)), 2)
        self.assertEqual((sources, out.getvalue()), ([], ""))
        self.assertEqual(run("--json", terminals=(stdin, err)), 0)
        self.assertIn("[1/27] Press: Xbox (center) button   [Enter = skip]", err.getvalue())
        self.assertNotIn("[1/27]", out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["mode"], "buttons")

    def test_usage_errors_and_help(self):
        ctx = self.device_ctx()
        for argv in (["--timeout", "1"], ["--timeout", "x"], ["--json", "--markdown"], ["--nope"], ["extra"]):
            code, out, err = self.run_buttons(ctx, {}, *argv)
            self.assertEqual((code, out, self.sources), (2, "", []), argv)
            self.assertIn("usage: omarchy-game-mode doctor buttons", err)
        code, out, _err = self.run_buttons(ctx, {}, "--help")
        self.assertEqual(code, 0)
        for phrase in ("--device", "--raw", "--timeout", "Read-only", "nothing is grabbed"):
            self.assertIn(phrase, out)
        out_stream = io.StringIO()
        self.assertEqual(cli.main(["buttons", "--help"], stdout=out_stream), 0)
        self.assertIn("usage: omarchy-game-mode doctor buttons", out_stream.getvalue())
        self.assertIn("doctor buttons", cli.EPILOG)

    def test_device_argument_must_be_an_evdev_character_device(self):
        ctx = self.device_ctx()
        write(SCRATCH / "dev/input/js0")
        write(SCRATCH / "etc/passwd", "root:x:0:0\n")
        (SCRATCH / "dev/input/by-id").mkdir()
        (SCRATCH / "dev/input/by-id/usb-pad-event-joystick").symlink_to("../event6")
        (SCRATCH / "dev/input/evil").symlink_to("../../etc/passwd")
        (SCRATCH / "dev/input/event77").symlink_to(SCRATCH / "etc/passwd")
        self.assertEqual(buttonsio.validate_device_path(ctx, "/dev/input/event6"), "event6")
        self.assertEqual(buttonsio.validate_device_path(ctx, "/dev/input/by-id/usb-pad-event-joystick"), "event6")
        for bad in ("/etc/passwd", "event6", "/dev/input/js0", "/dev/input/evil", "/dev/input/event77",
                    "/dev/input/../../etc/passwd", "/dev/input/event99", "/dev/input/", "/dev/input/event6/../js0",
                    "/dev/hidraw5", ""):
            with self.assertRaises(ValueError, msg=bad):
                buttonsio.validate_device_path(ctx, bad)
        plain_files = make_context(root=SCRATCH)
        with self.assertRaisesRegex(ValueError, "not a character device"):
            buttonsio.validate_device_path(plain_files, "/dev/input/event6")
        code, out, err = self.run_buttons(ctx, {}, "--device", "/dev/input/evil")
        self.assertEqual((code, out, self.sources), (2, "", []))
        self.assertIn("error: --device must resolve to /dev/input/eventN", err)

    def test_explicit_device_is_the_only_one_read_and_a_hidden_one_is_refused_plainly(self):
        ctx = self.device_ctx()
        selection = buttonsio.select_devices(ctx, "/dev/input/event5")
        self.assertEqual(([item.node for item in selection.watched], selection.primary), (["event5"], None))
        code, out, _err = self.run_buttons(ctx, {"paddle_left": key(KEY_F14)}, "--device", "/dev/input/event5")
        self.assertEqual(code, 0)
        self.assertIn("not a gamepad, so its codes are recorded but not judged", out)
        self.assertIn("Left back paddle             no standard code  KEY_F14 (184) on event5", out)
        self.assertNotIn("SDL mapping", out)
        hidden = self.device_ctx(inputplumber=True)
        with self.assertRaisesRegex(ValueError, "cannot be read: an input daemon hides it"):
            buttonsio.select_devices(hidden, "/dev/input/event6")
        with self.assertRaisesRegex(ValueError, "not a known input device"):
            write(SCRATCH / "dev/input/event88")
            buttonsio.select_devices(hidden, "/dev/input/event88")

    def test_physical_pad_and_its_key_interface_are_read_when_no_daemon_runs(self):
        selection = buttonsio.select_devices(self.device_ctx())
        self.assertEqual([item.node for item in selection.candidates], ["event6", "event5"])
        self.assertEqual(([item.node for item in selection.watched], selection.primary.node),
                         (["event6", "event5"], "event6"))
        self.assertEqual(selection.notes, [])

    def test_with_inputplumber_the_virtual_pad_is_judged_and_hidden_nodes_are_named(self):
        selection = buttonsio.select_devices(self.device_ctx(inputplumber=True))
        self.assertEqual(([item.node for item in selection.watched], selection.primary.node),
                         (["event21", "event20"], "event21"))
        self.assertEqual(selection.notes, [
            '/dev/input/event6 "ASUSTeK Computer Inc. N-KEY Device" (physical controller) cannot be read: an input '
            "daemon hides it (mode 000).",
            '/dev/input/event5 "ASUSTeK Computer Inc. N-KEY Device" (physical key device) cannot be read: an input '
            "daemon hides it (mode 000)."])
        self.assertNotIn("event1", [item.node for item in selection.candidates])

    def test_nothing_readable_ends_with_an_explanation(self):
        ctx = self.device_ctx(hide_raw=True)
        code, out, err = self.run_buttons(ctx, {})
        self.assertEqual((code, self.sources), (1, []))
        self.assertIn("cannot be read: an input daemon hides it", out)
        self.assertIn("No readable controller was found", out)

    def test_end_to_end_with_inputplumber_json_report(self):
        ctx = self.device_ctx(inputplumber=True)
        script = {**STANDARD, "guide": key(evdev.BTN_SELECT), "paddle_left": key(evdev.BTN_TRIGGER_HAPPY1),
                  "paddle_right": key(0x2C1), "armoury": key(KEY_F16, device=1), "library": typed("skip")}
        code, out, err = self.run_buttons(ctx, script, "--json")
        self.assertEqual(code, 0)
        self.assertTrue(self.sources[0].closed)
        self.assertIn('-> BTN_SELECT (314) on /dev/input/event21 "Microsoft X-Box One Elite 2 pad"', err)
        self.assertIn("read  /dev/input/event21  Microsoft X-Box One Elite 2 pad (virtual controller, USB 045e:0b00)",
                      err)
        self.assertIn("skip  /dev/input/event6  ASUSTeK Computer Inc. N-KEY Device (physical controller", err)
        self.assertIn("(skipped)", err)
        self.assertIn("(nothing arrived within 12 s)", err)
        document = json.loads(out)
        self.assertEqual((document["schema"], document["schema_version"], document["mode"], document["scope"]),
                         ("omarchy-game-mode.doctor/v1", 1, "buttons", "buttons"))
        self.assertEqual(sorted(document), [
            "checks", "controls", "devices", "duration_ms", "environment", "finished_at", "mode", "privacy", "raw",
            "run_id", "schema", "schema_version", "scope", "sdl_mapping", "started_at", "summary"])
        self.assertEqual([(row["node"], row["access"], row["watched"]) for row in document["devices"]], [
            ("event6", "hidden", False), ("event21", "readable", True), ("event5", "hidden", False),
            ("event20", "readable", True)])
        self.assertEqual(document["devices"][1], {
            "node": "event21", "name": "Microsoft X-Box One Elite 2 pad", "kind": "virtual", "role": "gamepad",
            "bus": "USB", "id": "045e:0b00", "access": "readable", "watched": True})
        for row in document["devices"]:
            self.assertEqual(sorted(row), ["access", "bus", "id", "kind", "name", "node", "role", "watched"])
        controls = {row["control"]: row for row in document["controls"]}
        self.assertEqual(len(controls), 27)
        self.assertEqual(controls["guide"], {
            "control": "guide", "label": "Xbox", "status": "observed", "result": "differs",
            "expected": "BTN_MODE (316)",
            "observed": [{"node": "event21", "event": "BTN_SELECT (314)", "type": "key", "event_code": 314,
                          "direction": 0}]})
        self.assertEqual((controls["r3"]["result"], controls["library"]["result"], controls["armoury"]["result"],
                          controls["library_hold"]["result"]), ("pass", "not tested", "recorded", "no event"))
        self.assertEqual(controls["armoury"]["observed"][0]["node"], "event20")
        mapping = document["sdl_mapping"]
        self.assertEqual(mapping["device"], "event21")
        self.assertTrue(mapping["line"].startswith("030000005e040000000b000010010000,Microsoft X-Box One Elite 2 pad,"))
        self.assertIn("rightstick:b10", mapping["line"])
        self.assertIn("guide:b6,back:b6", mapping["line"])
        self.assertEqual(mapping["missing"], [])
        self.assertEqual(document["raw"], {"status": "not requested"})
        verdicts_found = {check["id"]: check for check in document["checks"]}
        self.assertEqual(verdicts_found["buttons.guide"]["status"], "warn")
        self.assertEqual(verdicts_found["buttons.r3"]["status"], "pass")
        self.assertIn("virtual keyboard", verdicts_found["buttons.extra_keys"]["detail"])
        self.assertEqual(document["summary"]["exit_code"], 0)
        self.assertEqual(document["privacy"], {"redacted": True, "root_used": False, "devices_grabbed": False,
                                               "events_injected": False, "serials_read": False, "raw_hid_read": False})
        self.assertEqual(document["environment"]["device"]["board"], "RC73YA")
        self.assertEqual(document["environment"]["controllers"]["inputplumber_version"], "0.82.0")
        for canary in (CANARY_SERIAL, CANARY_MAC, CANARY_USER, CANARY_HOME, '"uniq"', '"phys"', '"serial"'):
            self.assertNotIn(canary, out)

    def test_end_to_end_raw_pad_markdown_report_with_raw_hid(self):
        ctx = self.device_ctx()
        add_hidraw(SCRATCH, 5, 5, 0o660)
        (SCRATCH / USB_PARENT / "3-6:1.5/0003:0B05:1B4C.0006/report_descriptor").write_bytes(ALLY_REPORT_DESCRIPTOR)
        add_hidraw(SCRATCH, 2, 2, 0o660)
        script = {key_: value for key_, value in STANDARD.items() if key_ != "r3"}
        script.update(r3=raw_report(at=0.1) + raw_report((110,), at=0.6), paddle_left=key(KEY_F14, device=1),
                      paddle_right=key(KEY_F15, device=1), view=key(evdev.BTN_SELECT) + raw_report(at=0.3))
        code, out, err = self.run_buttons(ctx, script, "--markdown", "--raw", "--timeout", "5")
        self.assertEqual(code, 0)
        self.assertEqual((self.raw_node.path, len(self.raw_node.fields)), ("/dev/hidraw5", 10))
        self.assertIn("-> raw HID report 0x0b: bit 110 changed (padding right after the 14 declared buttons", err)
        self.assertIn("(nothing arrived within 5 s)", err)
        self.assertTrue(out.startswith("### Omarchy Game Mode button test\n\n| Environment |  |"))
        for line in ("| Device | ASUSTeK COMPUTER INC. ROG Xbox Ally RC73YA_RC73YA, board RC73YA, BIOS RC73YA.318 |",
                     "| System | Omarchy 4.0.6, Linux 7.2.15-1-omarchy x86_64 |",
                     "| Node | Name | Kind | ID | Access | Read |",
                     "| event6 | ASUSTeK Computer Inc. N-KEY Device | physical gamepad | USB 0b05:1b4c | readable | "
                     "yes |",
                     "| Control | Expected | Observed | Result |",
                     "| Xbox | BTN_MODE (316) | BTN_MODE (316) on event6 | pass |",
                     "| R3 | BTN_THUMBR (318) | raw HID bit 110 of report 0x0b on hidraw | fail (no event) |",
                     "| Left back paddle | no standard code | KEY_F14 (184) on event5 | pass (recorded) |",
                     "| Library button | no standard code | - | fail (no event) |",
                     "**SDL mapping from the presses that were seen**", "```",
                     "Not seen and left out: rightstick (R3), paddle2 (Left back paddle), paddle1 (Right back paddle).",
                     "Raw HID: report 0x0b declares 14 buttons; changed: bit 110 during R3"):
            self.assertIn(line, out)
        self.assertIn("- **R3 (right stick click)** (WARN): R3 unavailable to apps: pressing it changed bit 110", out)
        self.assertIn("- **Paddles and vendor buttons** (WARN): These buttons arrive as keyboard keys", out)
        self.assertIn("03000000050b00004c1b000010010000,ASUSTeK Computer Inc. N-KEY Device,guide:b12,back:b10,", out)
        for canary in (CANARY_SERIAL, CANARY_MAC, CANARY_USER, CANARY_HOME):
            self.assertNotIn(canary, out)

    def test_text_report_and_silent_skip_of_unreadable_raw_hid(self):
        ctx = self.device_ctx()
        add_hidraw(SCRATCH, 5, 5, 0o000)
        script = {name: events for name, events in STANDARD.items() if name != "r3"}
        code, out, _err = self.run_buttons(ctx, {**script, "guide": key(evdev.BTN_SELECT)}, "--raw")
        self.assertEqual(code, 0)
        self.assertIsNone(self.raw_node)
        self.assertNotIn("Raw HID", out)
        self.assertNotIn("hidraw", out)
        for line in ("[1/27] Press: Xbox (center) button   [Enter = skip]", "Omarchy Game Mode button test",
                     "  Control", "  Xbox ", "BTN_SELECT (314) on event6", "Findings",
                     "  [WARN] Xbox button: The Xbox button reports BTN_SELECT (314), the code of View/Back",
                     "  [WARN] R3 (right stick click): R3 unavailable (needs the Ally HID kernel driver)",
                     "SDL mapping built only from the presses seen on event6:",
                     "  Not seen and left out: rightstick (R3), paddle2 (Left back paddle), paddle1 (Right back",
                     "Read-only test: no device was grabbed and no input was sent."):
            self.assertIn(line, out)
        self.assertIn("[26/27] Hold: Left button under the screen again, held for two seconds (optional)", out)

    def test_names_that_identify_the_user_are_scrubbed_from_the_report(self):
        ctx = self.device_ctx()
        add_input(SCRATCH, 30, f"{CANARY_USER}'s Controller", ELITE_PAD_KEYS, PAD_AXES, "045e", "0b13", bus="0005")
        ctx = make_context(fs=FakeDevFS(SCRATCH), commands=self.commands, env=CLIENT_ENV)
        code, out, _err = self.run_buttons(ctx, STANDARD, "--device", "/dev/input/event30", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["devices"][0]["name"], "<user>'s Controller")
        self.assertNotIn(CANARY_USER, out)

    def test_event_reader_parses_kernel_events_and_survives_a_node_that_is_not_evdev(self):
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        os.set_blocking(read_end, False)
        reader = object.__new__(buttonsio.EvdevReader)
        reader.fd = read_end
        self.assertEqual(reader.read(), [])
        events = [(evdev.EV_KEY, evdev.BTN_SELECT, 1), (0, 0, 0), (evdev.EV_ABS, evdev.ABS_Z, 900)]
        os.write(write_end, b"".join(buttonsio.EVENT.pack(1, 2, *event) for event in events))
        self.assertEqual(reader.read(), events)
        self.assertEqual((reader.keys_down(), reader.axes([evdev.ABS_X])), (set(), {}))
        os.close(write_end)
        self.assertEqual(reader.read(), [])
        self.assertEqual((buttonsio.EVENT.size, buttonsio.ABSINFO.size), (24, 24))
        self.assertEqual((buttonsio._eviocgkey(96), buttonsio._eviocgabs(0)), (0x80604518, 0x80184540))


if __name__ == "__main__":
    unittest.main()
