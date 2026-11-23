"""Tests for `omarchy-game-mode doctor`: no network, no devices, no root, no GTK."""

import collections
import ipaddress
import json
import shutil
import socket
import ssl
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

from gaminghome.doctor import context, model, net, redact, runner, stun, udp
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
        self.assertEqual(outcome.detail, "cannot verify without root - run: sudo ufw status numbered verbose")


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


if __name__ == "__main__":
    unittest.main()
