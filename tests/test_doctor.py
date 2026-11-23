"""Tests for `omarchy-game-mode doctor`: no network, no devices, no root, no GTK."""

import json
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path

from gaminghome.doctor import context, model, redact, runner
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


if __name__ == "__main__":
    unittest.main()
