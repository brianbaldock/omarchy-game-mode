from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from gaminghome.keyboard import (
    FALLBACK_MONITOR_HEIGHT,
    KeyboardController,
    KeyboardLockError,
    NO_BACKEND,
    STILL_STARTING,
    Visibility,
    focused_monitor_logical_height,
    hyprland_visibility,
    keyboard_action_lock,
    keyboard_sizing,
    layers,
    validate_keyboard_args,
    visibility_from_layers,
)
from gaminghome.settings import SETTINGS_MAX_BYTES, Settings

SCRATCH = Path(__file__).resolve().parent / "scratch" / "keyboard"
EXECUTABLES = {
    "wvkbd-mobintl": "/usr/bin/wvkbd-mobintl",
    "wvkbd": "/usr/bin/wvkbd",
    "hyprctl": "/usr/bin/hyprctl",
}
IDENTITIES = {path: (1, index + 100) for index, path in enumerate(EXECUTABLES.values())}
SESSION = {
    "WAYLAND_DISPLAY": "wayland-fixture",
    "HYPRLAND_INSTANCE_SIGNATURE": "hyprland-fixture",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}
USR1_MASK = 1 << (signal.SIGUSR1 - 1)
USR2_MASK = 1 << (signal.SIGUSR2 - 1)
RTMIN = getattr(signal, "SIGRTMIN", None)
RTMIN_MASK = 1 << (RTMIN - 1) if RTMIN is not None else 0

VISIBLE_LAYERS = {
    "eDP-1": {
        "levels": {
            "0": [{"namespace": "omarchy-background", "w": 1920, "h": 1080}],
            "2": [{"namespace": "omarchy-bar", "w": 1920, "h": 44}],
            "3": [{"namespace": "wvkbd", "pid": 51, "w": 1280, "h": 260}],
        }
    }
}
HIDDEN_LAYERS = {
    "eDP-1": {
        "levels": {
            "0": [{"namespace": "omarchy-background", "w": 1920, "h": 1080}],
            "2": [{"namespace": "omarchy-bar", "w": 1920, "h": 44}],
        }
    }
}
VISIBLE_FLAT_LAYERS = {
    "eDP-1": {
        "0": [{"namespace": "omarchy-background", "w": 1920, "h": 1080}],
        "2": [{"namespace": "omarchy-bar", "w": 1920, "h": 44}],
        "3": [{"namespace": "wvkbd-mobintl", "pid": 51, "w": 1280, "h": 260}],
    }
}


class ProcFixture:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True)
        self.identities = {}

    def add(
        self,
        pid: int,
        *,
        backend="wvkbd-mobintl",
        uid=1000,
        state="S",
        start=990,
        sigblk=0,
        sigcgt=0,
        identity=None,
        environment=None,
        extra_threads=0,
    ):
        directory = self.root / str(pid)
        directory.mkdir(parents=True)
        (directory / "comm").write_text(f"{backend}\n", encoding="utf-8")
        (directory / "status").write_text(
            self.status(backend, uid, state, sigblk, sigcgt),
            encoding="ascii",
        )
        fields = [state, *(["0"] * 18), str(start), "0", "0"]
        (directory / "stat").write_text(f"{pid} ({backend}) {' '.join(fields)}\n", encoding="utf-8")
        environ = dict(SESSION if environment is None else environment)
        raw = b"\x00".join(
            os.fsencode(f"{key}={value}") for key, value in environ.items()
        ) + b"\x00"
        (directory / "environ").write_bytes(raw)
        task_dir = directory / "task"
        task_dir.mkdir()
        self.add_task(pid, pid, sigblk=sigblk, sigcgt=sigcgt)
        for offset in range(1, extra_threads + 1):
            self.add_task(pid, pid + offset, sigblk=sigblk, sigcgt=sigcgt)
        self.identities[directory / "exe"] = identity or IDENTITIES[EXECUTABLES[backend]]

    @staticmethod
    def status(backend, uid, state, sigblk, sigcgt):
        return "\n".join(
            [
                f"Name:\t{backend}",
                f"State:\t{state} (state)",
                f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}",
                f"SigBlk:\t{sigblk:016x}",
                f"SigCgt:\t{sigcgt:016x}",
                "",
            ]
        )

    def add_task(self, pid: int, tid: int, *, sigblk=0, sigcgt=0):
        directory = self.root / str(pid) / "task" / str(tid)
        directory.mkdir()
        (directory / "status").write_text(
            self.status("wvkbd-mobintl", 1000, "S", sigblk, sigcgt),
            encoding="ascii",
        )

    def break_uid(self, pid: int):
        path = self.root / str(pid) / "status"
        path.write_text(path.read_text().replace("\t1000\t1000\t1000\t1000", "\t1000\t1000\t1000\t0"))

    def remove(self, pid: int):
        shutil.rmtree(self.root / str(pid), ignore_errors=True)

    def stat(self, path):
        target = Path(path)
        if str(target) in IDENTITIES:
            device, inode = IDENTITIES[str(target)]
            return types.SimpleNamespace(st_dev=device, st_ino=inode, st_mode=stat.S_IFREG | 0o755)
        if target in self.identities:
            device, inode = self.identities[target]
            return types.SimpleNamespace(st_dev=device, st_ino=inode, st_mode=stat.S_IFREG | 0o755)
        raise FileNotFoundError(path)


class ControllerFixture(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.proc = ProcFixture(SCRATCH / "proc")
        self.sent = []
        self.closed = []
        self.spawned = []
        self.settings = {}
        self.visibility = []
        self.pid_counter = 0
        self.environment = dict(SESSION)

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def unexpected_spawn(self, argv):
        self.fail(f"unexpected spawn: {argv}")

    def open_pidfd(self, pid):
        self.pid_counter += 1
        return 1000 + self.pid_counter

    def send_signal(self, fd, sig, siginfo=None, flags=0):
        self.sent.append((fd, sig, siginfo, flags))

    def visibility_provider(self, expected_pid):
        if self.visibility:
            value = self.visibility.pop(0)
            return Visibility(value, None)
        return Visibility("unknown")

    def fake_spawn(self, argv):
        self.spawned.append(argv)
        return types.SimpleNamespace()

    def controller(self, **overrides):
        options = {
            "proc_root": self.proc.root,
            "pidfd_open": self.open_pidfd,
            "pidfd_send_signal": self.send_signal,
            "close_fd": self.closed.append,
            "stat_result": self.proc.stat,
            "realpath": lambda path: path,
            "which": lambda name: EXECUTABLES.get(name),
            "run": lambda argv, timeout, limit: (_ for _ in ()).throw(OSError("unexpected helper")),
            "spawn": self.unexpected_spawn,
            "boot_clock": lambda: 1000.0,
            "sleep": lambda _delay: None,
            "monitor_height": lambda: 720,
            "visibility_provider": self.visibility_provider,
            "settings_accessor": lambda: self.settings,
            "environment": self.environment,
            "uid": 1000,
            "clock_ticks": 1,
        }
        options.update(overrides)
        return KeyboardController(**options)

    def assert_closed(self):
        self.assertTrue(self.closed)
        self.assertEqual(len(self.closed), len(set(self.closed)))


class ProcessIdentityTest(ControllerFixture):
    def test_backend_must_resolve_from_a_trusted_absolute_path(self):
        candidates = ("/home/user/bin/wvkbd-mobintl", "wvkbd-mobintl", "bin/wvkbd-mobintl")
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                status = self.controller(which=lambda _name, c=candidate: c).status()
                self.assertFalse(status["installed"])
                self.assertEqual(status["command_error"], NO_BACKEND)

    def test_symlink_outside_trusted_directory_is_rejected(self):
        controller = self.controller(
            which=lambda name: "/usr/bin/wvkbd-mobintl" if name == "wvkbd-mobintl" else None,
            realpath=lambda _path: "/home/user/wvkbd-mobintl",
        )
        self.assertFalse(controller.status()["installed"])

    def test_comm_prefilter_cannot_substitute_wrong_executable(self):
        self.proc.add(10, identity=(9, 999))
        result = self.controller().show()
        self.assertFalse(result["ok"])
        self.assertIn("trusted wvkbd executable", result["detail"])
        self.assertEqual(self.sent, [])
        self.assert_closed()

    def test_all_four_uids_and_state_are_checked(self):
        self.proc.add(11)
        self.proc.break_uid(11)
        result = self.controller().show()
        self.assertFalse(result["ok"])
        self.assertIn("current uid", result["detail"])
        self.assertEqual(self.sent, [])

    def test_zombie_is_ignored_while_dead_state_is_not_started(self):
        self.proc.add(12, state="Z")
        result = self.controller().hide()
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "already hidden/not running")

    def test_other_session_process_is_refused_and_untouched(self):
        self.proc.add(13, environment={**SESSION, "HYPRLAND_INSTANCE_SIGNATURE": "other"})
        result = self.controller().show()
        self.assertFalse(result["ok"])
        self.assertIn("different Wayland/Hyprland", result["detail"])
        self.assertEqual(self.sent, [])

    def test_sole_foreign_candidate_never_triggers_a_spawn(self):
        self.proc.add(14, environment={"WAYLAND_DISPLAY": "other", "HYPRLAND_INSTANCE_SIGNATURE": "other"})
        controller = self.controller(spawn=self.unexpected_spawn)
        self.assertFalse(controller.toggle()["ok"])
        self.assertEqual(self.spawned, [])

    def test_different_xdg_runtime_dir_is_refused_and_untouched(self):
        self.proc.add(140, environment={**SESSION, "XDG_RUNTIME_DIR": "/run/user/2000"},
                      start=1, sigblk=USR2_MASK)
        self.visibility = ["hidden"]
        result = self.controller().show()
        self.assertFalse(result["ok"])
        self.assertIn("different Wayland/Hyprland", result["detail"])
        self.assertEqual(self.sent, [])

    def test_wayland_socket_candidate_is_refused(self):
        self.proc.add(141, environment={**SESSION, "WAYLAND_SOCKET": "42"},
                      start=1, sigblk=USR2_MASK)
        self.visibility = ["hidden"]
        result = self.controller().show()
        self.assertFalse(result["ok"])
        self.assertIn("WAYLAND_SOCKET", result["detail"])
        self.assertEqual(self.sent, [])

    def test_missing_own_or_candidate_session_environment_refuses(self):
        self.proc.add(15)
        self.environment = {"WAYLAND_DISPLAY": "wayland-fixture"}
        self.assertIn("session variables", self.controller().hide()["detail"])
        self.environment = dict(SESSION)
        (self.proc.root / "15" / "environ").unlink()
        self.assertIn("unreadable", self.controller().show()["detail"])

    def test_multiple_valid_candidates_are_ambiguous(self):
        self.proc.add(16)
        self.proc.add(17, backend="wvkbd")
        result = self.controller().toggle()
        self.assertFalse(result["ok"])
        self.assertIn("2 wvkbd instances", result["detail"])
        self.assertEqual(self.sent, [])
        self.assert_closed()

    def test_valid_plus_unverifiable_candidate_refuses_everyone(self):
        self.proc.add(18)
        self.proc.add(19, identity=(9, 999))
        result = self.controller().hide()
        self.assertFalse(result["ok"])
        self.assertIn("trusted wvkbd executable", result["detail"])
        self.assertEqual(self.sent, [])

    def test_truncated_scan_is_failure_not_absence(self):
        for pid in range(20, 24):
            self.proc.add(pid)
        result = self.controller(scan_limit=2).hide()
        self.assertFalse(result["ok"])
        self.assertIn("exceeded 2 entries", result["detail"])

    def test_pidfd_unavailable_never_falls_back_to_numeric_kill(self):
        self.proc.add(25)
        result = self.controller(pidfd_open=None).show()
        self.assertFalse(result["ok"])
        self.assertIn("pidfd support", result["detail"])
        self.assertEqual(self.sent, [])

    def test_pid_exit_or_reuse_between_validation_and_send_is_reported(self):
        self.proc.add(26, start=1, sigblk=USR2_MASK)
        self.visibility = ["hidden"]

        def vanished(fd, sig, siginfo=None, flags=0):
            raise ProcessLookupError("process exited")

        result = self.controller(pidfd_send_signal=vanished).show()
        self.assertFalse(result["ok"])
        self.assertIn("process exited", result["detail"])
        self.assert_closed()


class ReadinessTest(ControllerFixture):
    def test_authentic_candidate_is_unready_for_every_action(self):
        for pid, action, state in ((30, "show", "hidden"), (31, "hide", "visible"),
                                   (32, "toggle", "hidden")):
            with self.subTest(action=action):
                self.proc.add(pid, start=999, sigblk=0, sigcgt=0)
                self.visibility = [state]
                result = getattr(self.controller(proc_root=self.proc.root), action)()
                self.assertFalse(result["ok"])
                self.assertEqual(result["detail"], STILL_STARTING)
                self.assertEqual(self.sent, [])
                self.proc.remove(pid)

    def test_age_and_mask_must_both_be_ready(self):
        self.proc.add(33, start=999, sigblk=USR2_MASK | RTMIN_MASK)
        self.visibility = ["hidden"]
        self.assertFalse(self.controller().show()["ok"])
        self.proc.remove(33)
        self.proc.add(34, start=1, sigblk=0, sigcgt=0)
        self.visibility = ["hidden"]
        self.assertFalse(self.controller().show()["ok"])

    def test_live_sigblk_and_sigcgt_masks_are_both_accepted(self):
        self.proc.add(
            35,
            start=1,
            sigblk=0x0000000200001A00,
            sigcgt=0x0000000100000000,
        )
        self.visibility = ["hidden", "visible"]
        self.assertTrue(self.controller().show()["ok"])
        self.assertEqual(self.sent[-1][1], signal.SIGUSR2)

    def test_every_thread_must_have_requested_signal_mask(self):
        self.proc.add(36, start=1, sigblk=USR2_MASK, extra_threads=1)
        missing = self.proc.root / "36" / "task" / "37" / "status"
        missing.write_text(self.proc.status("wvkbd-mobintl", 1000, "S", 0, 0))
        self.visibility = ["hidden"]
        result = self.controller().show()
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], STILL_STARTING)
        self.assertEqual(self.sent, [])

    def test_missing_task_status_is_not_ready(self):
        self.proc.add(37, start=1, sigblk=USR2_MASK)
        (self.proc.root / "37" / "task" / "37" / "status").unlink()
        self.visibility = ["hidden"]
        self.assertFalse(self.controller().show()["ok"])

    def test_more_than_64_tasks_is_not_ready(self):
        self.proc.add(370, start=1, sigblk=USR2_MASK, extra_threads=64)
        self.visibility = ["hidden"]
        result = self.controller().show()
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], STILL_STARTING)

    def test_executable_is_revalidated_immediately_before_send(self):
        self.proc.add(38, start=1, sigblk=USR2_MASK)
        pid_exe = self.proc.root / "38" / "exe"
        checks = 0

        def stat_result(path):
            nonlocal checks
            if Path(path) == pid_exe:
                checks += 1
                if checks >= 3:
                    return types.SimpleNamespace(st_dev=9, st_ino=999, st_mode=stat.S_IFREG | 0o755)
                return types.SimpleNamespace(
                    st_dev=IDENTITIES[EXECUTABLES["wvkbd-mobintl"]][0],
                    st_ino=IDENTITIES[EXECUTABLES["wvkbd-mobintl"]][1],
                    st_mode=stat.S_IFREG | 0o755,
                )
            return self.proc.stat(path)

        self.visibility = ["hidden"]
        result = self.controller(stat_result=stat_result).show()
        self.assertFalse(result["ok"])
        self.assertIn("trusted wvkbd executable", result["detail"])
        self.assertEqual(self.sent, [])


class VisibilityTest(ControllerFixture):
    def test_wrapped_and_flat_hyprland_shapes(self):
        self.assertEqual(visibility_from_layers(VISIBLE_LAYERS, 51), "visible")
        self.assertEqual(visibility_from_layers(VISIBLE_FLAT_LAYERS, 51), "visible")
        self.assertEqual(visibility_from_layers(HIDDEN_LAYERS), "hidden")

    def test_malformed_layers_are_unknown_never_hidden(self):
        malformed = (
            None,
            [],
            {},
            {"eDP-1": []},
            {"eDP-1": {"levels": []}},
            {"eDP-1": {"3": {}}},
            {"eDP-1": {"3": [{"namespace": 1}]}},
            {"eDP-1": {"3": [{"namespace": "wvkbd", "pid": "51"}]}},
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertEqual(visibility_from_layers(payload), "unknown")

    def test_wvkbd_layer_pid_must_match_candidate(self):
        observation = hyprland_visibility(
            lambda argv, timeout, limit: json.dumps(VISIBLE_LAYERS).encode(),
            "/usr/bin/hyprctl",
            99,
        )
        self.assertEqual(observation.value, "unknown")
        self.assertIn("not candidate 99", observation.error)

    def test_pid_mismatch_refuses_signal_and_closes_pidfd(self):
        self.proc.add(51, start=1, sigblk=USR1_MASK)
        provider = lambda _pid: Visibility("unknown", "wvkbd layer belongs to pid 99")
        result = self.controller(visibility_provider=provider).hide()
        self.assertFalse(result["ok"])
        self.assertIn("refusing to signal", result["detail"])
        self.assertEqual(self.sent, [])
        self.assert_closed()

    def test_toggle_uses_show_or_hide_signal_when_visibility_known(self):
        self.proc.add(52, start=1, sigblk=USR1_MASK | USR2_MASK | RTMIN_MASK)
        cases = (("visible", signal.SIGUSR1, "hidden"), ("hidden", signal.SIGUSR2, "visible"))
        for before, expected, after in cases:
            with self.subTest(before=before):
                self.sent.clear()
                self.visibility = [before, after]
                result = self.controller().toggle()
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.sent[-1][1], expected)

    @unittest.skipUnless(RTMIN is not None, "needs SIGRTMIN")
    def test_toggle_falls_back_to_rtmin_only_when_visibility_unknown(self):
        self.proc.add(53, start=1, sigblk=RTMIN_MASK)
        self.visibility = ["unknown", "unknown"]
        result = self.controller().toggle()
        self.assertTrue(result["ok"])
        self.assertEqual(self.sent[-1][1], signal.SIGRTMIN)
        self.assertIn("could not confirm", result["detail"])

    def test_show_and_hide_are_noops_without_signalling(self):
        self.proc.add(54, start=999, sigblk=0)
        self.visibility = ["visible"]
        self.assertEqual(self.controller().show()["detail"], "already visible")
        self.visibility = ["hidden"]
        self.assertEqual(self.controller().hide()["detail"], "already hidden")
        self.assertEqual(self.sent, [])

    def test_signal_confirmation_is_requeried_once(self):
        self.proc.add(55, start=1, sigblk=USR2_MASK)
        self.visibility = ["hidden", "visible"]
        result = self.controller().show()
        self.assertTrue(result["ok"])
        self.assertIn("confirmed visible", result["detail"])
        self.assertEqual(len(self.sent), 1)

    def test_visibility_provider_failure_still_closes_pidfd(self):
        self.proc.add(56, start=1, sigblk=USR2_MASK)

        def broken(_pid):
            raise RuntimeError("malformed helper")

        self.visibility = []
        result = self.controller(visibility_provider=broken).show()
        self.assertTrue(result["ok"])
        self.assert_closed()


class HelperAndSizingTest(ControllerFixture):
    def test_monitor_json_uses_real_scaled_dimensions(self):
        calls = []

        def run(argv, timeout, limit):
            calls.append((argv, timeout, limit))
            return json.dumps([
                {"name": "DP-1", "focused": False, "width": 1600, "height": 1200, "scale": 1},
                {"name": "eDP-1", "focused": True, "width": 1920, "height": 1080, "scale": 1.5},
            ]).encode()

        self.assertEqual(focused_monitor_logical_height(run, "/usr/bin/hyprctl"), 720)
        self.assertEqual(calls, [(["/usr/bin/hyprctl", "monitors", "-j"], 1.0, 64 * 1024)])

    def test_monitor_height_and_scale_are_validated_before_division(self):
        invalid = (
            {"height": 199, "scale": 1},
            {"height": 8001, "scale": 1},
            {"height": 1080, "scale": 0},
            {"height": 1080, "scale": -1},
            {"height": 1080, "scale": 0.49},
            {"height": 1080, "scale": 8.01},
            {"height": 1080, "scale": float("nan")},
            {"height": 1080, "scale": float("inf")},
            {"height": 10**400, "scale": 1},
            {"height": 1080, "scale": 10**400},
            {"height": float("-inf"), "scale": 1},
        )
        for monitor in invalid:
            with self.subTest(monitor=monitor):
                raw = json.dumps([{"focused": True, **monitor}]).encode()
                self.assertEqual(
                    focused_monitor_logical_height(lambda *_args, raw=raw: raw, "/usr/bin/hyprctl"),
                    FALLBACK_MONITOR_HEIGHT,
                )

    def test_missing_or_failed_hyprctl_falls_back_to_720(self):
        self.assertEqual(focused_monitor_logical_height(lambda *_args: b"bad", None), 720)
        self.assertEqual(
            focused_monitor_logical_height(lambda *_args: (_ for _ in ()).throw(OSError("no")),
                                            "/usr/bin/hyprctl"),
            FALLBACK_MONITOR_HEIGHT,
        )

    def test_sizing_is_pure_and_clamped(self):
        self.assertEqual(keyboard_sizing(720), {"landscape_height": 220, "font_size": 16})
        self.assertEqual(keyboard_sizing(930), {"landscape_height": 260, "font_size": 18})
        self.assertEqual(keyboard_sizing(2000), {"landscape_height": 420, "font_size": 24})

    def test_hyprctl_is_never_executed_bare_or_untrusted(self):
        executed = []

        def which(name):
            if name == "hyprctl":
                return "/home/user/bin/hyprctl"
            return EXECUTABLES.get(name)

        def run(argv, _timeout, _limit):
            executed.append(argv)
            return b"{}"

        controller = self.controller(which=which, run=run, monitor_height=None, spawn=self.fake_spawn)
        self.assertTrue(controller.show()["ok"])
        self.assertEqual(executed, [])
        self.assertEqual(self.spawned[0][0], "/usr/bin/wvkbd-mobintl")

    def test_untrusted_or_failed_hyprctl_never_publishes_hidden(self):
        self.proc.add(58)

        def which(name):
            if name == "hyprctl":
                return "/home/user/bin/hyprctl"
            return EXECUTABLES.get(name)

        status = self.controller(
            which=which,
            run=lambda *_args: self.fail("untrusted hyprctl was executed"),
            visibility_provider=None,
        ).status()
        self.assertEqual(status["visibility"], "unknown")

        def failed_run(*_args):
            raise subprocess.CalledProcessError(1, "hyprctl")

        status = self.controller(
            which=lambda name: EXECUTABLES.get(name),
            run=failed_run,
            visibility_provider=None,
        ).status()
        self.assertEqual(status["visibility"], "unknown")

    def test_trusted_helpers_are_called_by_absolute_path(self):
        calls = []

        def run(argv, timeout, limit):
            calls.append(argv)
            if "layers" in argv:
                return json.dumps(HIDDEN_LAYERS).encode()
            return json.dumps([{"focused": True, "height": 1080, "scale": 1.5}]).encode()

        self.proc.add(57, start=1, sigblk=USR2_MASK)
        controller = self.controller(
            which=lambda name: EXECUTABLES.get(name),
            run=run,
            monitor_height=None,
            visibility_provider=None,
        )
        self.visibility = ["hidden", "visible"]
        self.assertTrue(controller.show()["ok"])
        self.assertTrue(all(call[0] == "/usr/bin/hyprctl" for call in calls))


class StartAndArgsTest(ControllerFixture):
    def test_cold_hide_is_noop_without_spawn(self):
        result = self.controller().hide()
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "already hidden/not running")
        self.assertEqual(self.spawned, [])

    def test_cold_show_and_toggle_start_visible_without_signals(self):
        for action in ("show", "toggle"):
            with self.subTest(action=action):
                self.spawned.clear()
                controller = self.controller(spawn=self.fake_spawn)
                result = getattr(controller, action)()
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["visibility"], "visible")
                self.assertNotIn("--hidden", self.spawned[-1])
                self.assertEqual(self.sent, [])

    def test_cold_start_has_default_size_only_and_no_font(self):
        self.controller(spawn=self.fake_spawn, monitor_height=lambda: 1080).show()
        self.assertEqual(
            self.spawned[0],
            ["/usr/bin/wvkbd-mobintl", "-H", "302", "-L", "302"],
        )

    def test_detached_spawn_is_direct_and_does_not_inherit_file_descriptors(self):
        with mock.patch("gaminghome.keyboard.subprocess.Popen") as popen:
            self.controller()._spawn_detached(["/usr/bin/wvkbd-mobintl"])
        self.assertEqual(popen.call_args.args, (["/usr/bin/wvkbd-mobintl"],))
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("pass_fds", popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs["env"], dict(os.environ))

    def test_second_cli_invocation_gets_lock_while_cold_start_child_lives(self):
        real_popen = subprocess.Popen
        child = None

        def fixture_popen(_argv, **kwargs):
            nonlocal child
            child = real_popen(
                [sys.executable, "-u", "-c", "import time; print('ready', flush=True); time.sleep(5)"],
                **kwargs,
            )
            return child

        holder = {}

        def spawn(argv):
            return holder["controller"]._spawn_detached(argv)

        try:
            with mock.patch("gaminghome.keyboard.subprocess.Popen", side_effect=fixture_popen):
                with keyboard_action_lock(str(SCRATCH), 0.15) as lock_fd:
                    self.assertFalse(os.get_inheritable(lock_fd))
                    controller = self.controller(
                        spawn=spawn,
                        which=lambda name: EXECUTABLES.get(name),
                    )
                    holder["controller"] = controller
                    self.assertTrue(controller.show()["ok"])
                    self.assertIsNotNone(child)
                    self.assertIsNone(child.poll())
                with keyboard_action_lock(str(SCRATCH), 0.15):
                    self.assertIsNone(child.poll())
                    self.assertTrue(self.controller().hide()["ok"])
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=1)

    def test_valid_args_are_forwarded_exactly(self):
        value = [
            "-H", "160", "-L", "600", "--fn", "JetBrainsMono Nerd Font 18",
            "--no-popup", "--no-highlight", "--no-feedback", "--non-exclusive",
        ]
        self.assertEqual(validate_keyboard_args(value), (value, None))
        self.settings["keyboardArgs"] = value
        self.controller(spawn=self.fake_spawn, monitor_height=self.fail).show()
        self.assertEqual(self.spawned[0], ["/usr/bin/wvkbd-mobintl", *value])

    def test_layer_options_and_executable_overrides_are_rejected(self):
        forbidden = (
            "-l", "--landscape-layers", "-landscape-layers", "--hidden", "-hidden",
            "--auto", "-auto", "-o", "-O", "-D", "--version", "wvkbd-mobintl",
        )
        for option in forbidden:
            with self.subTest(option=option):
                self.assertIsNone(validate_keyboard_args([option])[0])

    def test_types_missing_values_repeats_and_flags_are_strict(self):
        bad = (
            "wvkbd", None, {}, ["wvkbd"],
            ["-H"], ["-H", "--no-popup"], ["-H", "260", "-H", "280"],
            ["--no-popup", "--no-popup"], ["--fn"], ["--fn", "-x"],
            ["-H", 260], ["-H", ""], ["-H", "260\x00"], ["-H", "260\n"],
            ["--no-popup"] * 17, ["--fn", "x" * 4097],
        )
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(validate_keyboard_args(value)[0])

    def test_height_font_and_lone_surrogates_are_validated(self):
        for value in ("159", "601", "+200", "2e2", " 200"):
            self.assertIsNone(validate_keyboard_args(["-H", value])[0])
        for value in ("Font 11", "Font 33", "Fönt 18", " 18", "Font\t18"):
            self.assertIsNone(validate_keyboard_args(["--fn", value])[0])
        self.assertIsNone(validate_keyboard_args(["--fn", "Font \ud800 18"])[0])

    def test_invalid_args_are_never_executed_and_status_reports_error(self):
        self.settings = {"keyboardArgs": ["--auto"], "keyboardCommand": ["/bin/sh"]}
        controller = self.controller(spawn=self.fake_spawn)
        self.assertTrue(controller.show()["ok"])
        self.assertNotIn("--auto", self.spawned[0])
        status = controller.status()
        self.assertTrue(status["args_error"])
        self.assertTrue(status["ignored_legacy_override"])
        self.assertNotIn("--auto", status["args_error"])


class StatusTest(ControllerFixture):
    def test_status_contract_and_honest_visibility(self):
        self.proc.add(60, start=1, sigblk=USR2_MASK)
        self.settings = {"keyboardArgs": ["--fn", "Font 10"], "keyboardCommand": ["ignored"]}
        self.visibility = ["visible"]
        status = self.controller().status()
        self.assertEqual(
            set(status),
            {
                "installed", "running", "backend", "last_command", "visibility",
                "command_error", "args_error", "ignored_legacy_override",
            },
        )
        self.assertTrue(status["installed"])
        self.assertTrue(status["running"])
        self.assertEqual(status["backend"], "wvkbd-mobintl")
        self.assertEqual(status["visibility"], "visible")
        self.assertTrue(status["args_error"])
        self.assertTrue(status["ignored_legacy_override"])

    def test_not_installed_status_is_clear(self):
        status = self.controller(which=lambda _name: None).status()
        self.assertFalse(status["installed"])
        self.assertFalse(status["running"])
        self.assertEqual(status["command_error"], NO_BACKEND)


class LockTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_lock_directory_and_file_are_private(self):
        with keyboard_action_lock(str(SCRATCH)):
            lock_path = SCRATCH / "omarchy-game-mode" / "keyboard.lock"
            self.assertEqual((lock_path.parent.stat().st_mode & 0o777), 0o700)
            self.assertEqual((lock_path.stat().st_mode & 0o777), 0o600)

    def test_contended_lock_fails_within_bounded_time(self):
        directory = SCRATCH / "omarchy-game-mode"
        directory.mkdir(mode=0o700)
        path = directory / "keyboard.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(KeyboardLockError, "already in progress"):
                with keyboard_action_lock(str(SCRATCH), 0.05):
                    self.fail("lock should be contended")
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(descriptor)

    def test_invalid_runtime_directory_fails_clearly(self):
        with self.assertRaisesRegex(KeyboardLockError, "XDG_RUNTIME_DIR"):
            with keyboard_action_lock(""):
                self.fail("lock should not be available")


class SettingsRobustnessTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        self.path = SCRATCH / "settings.json"

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_unhashable_library_entries_are_filtered_before_deduplication(self):
        self.path.write_text(json.dumps({"library": [{"bad": 1}, ["bad"], "moonlight", "moonlight"]}))
        self.assertEqual(Settings(self.path).library, ["moonlight"])

    def test_fifo_read_has_a_hard_deadline_and_never_blocks(self):
        fifo = SCRATCH / "fifo"
        os.mkfifo(fifo)
        finished = threading.Event()
        result = []

        def load():
            result.append(Settings(fifo))
            finished.set()

        thread = threading.Thread(target=load, daemon=True)
        thread.start()
        self.assertTrue(finished.wait(0.5))
        self.assertTrue(result[0].load_error)
        self.assertTrue(result[0].load_failed)
        self.assertIn("regular file", result[0].load_error)
        saved = threading.Event()
        save_result = []

        def save():
            save_result.append(Settings(fifo).save())
            saved.set()

        saver = threading.Thread(target=save, daemon=True)
        saver.start()
        self.assertTrue(saved.wait(0.5))
        self.assertFalse(save_result[0])
        self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode))

    def test_nested_json_and_lone_surrogate_do_not_crash_load_or_save(self):
        self.path.write_text("[" * 2000 + "0" + "]" * 2000)
        settings = Settings(self.path)
        self.assertTrue(settings.load_error)
        settings.language = "en"
        self.assertFalse(settings.save())
        self.assertEqual(self.path.read_text(), "[" * 2000 + "0" + "]" * 2000)
        self.path.write_text(json.dumps({"future": "\ud800", "keyboardCommand": "\udfff"}))
        settings = Settings(self.path)
        settings.language = "fr"
        self.assertTrue(settings.save())
        raw = self.path.read_text(encoding="ascii")
        self.assertIn("\\ud800", raw)
        self.assertEqual(json.loads(raw)["future"], "\ud800")

        deep = "[" * 400 + "\"leaf\"" + "]" * 400
        self.path.write_text('{"version":1,"keyboardArgs":' + deep + '}')
        settings = Settings(self.path)
        self.assertIsNone(settings.load_error)
        settings.language = "de"
        self.assertTrue(settings.save())

    def test_every_initial_load_failure_is_fail_closed_on_save(self):
        cases = (
            ("bad-json", b"{broken", None),
            ("oversize", b"x" * (SETTINGS_MAX_BYTES + 1), "bytes"),
            ("future", json.dumps({"version": 999, "future": True}).encode(), "newer"),
        )
        for name, raw, fragment in cases:
            with self.subTest(name=name):
                self.path.write_bytes(raw)
                settings = Settings(self.path)
                self.assertTrue(settings.load_failed)
                if fragment:
                    self.assertIn(fragment, settings.load_error)
                settings.language = "fr"
                with mock.patch("gaminghome.settings.LOGGER.warning") as warning:
                    self.assertFalse(settings.save())
                    self.assertFalse(settings.save())
                    warning.assert_called_once()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_permission_load_failure_is_fail_closed_on_save(self):
        raw = json.dumps({"version": 1, "language": "ja", "future": True}).encode()
        self.path.write_bytes(raw)
        with mock.patch(
            "gaminghome.settings._read_settings_document",
            side_effect=PermissionError("permission denied"),
        ):
            settings = Settings(self.path)
        self.assertTrue(settings.load_failed)
        settings.language = "en"
        self.assertFalse(settings.save())
        self.assertEqual(self.path.read_bytes(), raw)

    def test_save_error_redacts_home_directory(self):
        self.path.write_text("{}")
        settings = Settings(self.path)
        with mock.patch(
            "gaminghome.settings.atomic_write",
            side_effect=OSError(f"{os.path.expanduser('~')}/private/settings.json denied"),
        ):
            self.assertFalse(settings.save())
        self.assertIn("~/private/settings.json denied", settings.save_error)
        self.assertNotIn(os.path.expanduser("~"), settings.save_error)

    def test_pin_save_failure_restores_library(self):
        self.path.write_text(json.dumps({"library": ["steam"]}))
        settings = Settings(self.path)
        settings.save = mock.Mock(return_value=False)
        catalog = types.SimpleNamespace(
            ids=["steam", "moonlight"],
            apps=[],
        )
        self.assertFalse(settings.set_pinned(catalog, "moonlight", True))
        self.assertEqual(settings.library, ["steam"])

    def test_external_corruption_between_load_and_save_is_never_replaced(self):
        self.path.write_text(json.dumps({"version": 1, "language": "en", "future": {"kept": True}}))
        settings = Settings(self.path)
        corrupt = b"{bad json"
        self.path.write_bytes(corrupt)
        settings.language = "de"
        self.assertFalse(settings.save())
        self.assertEqual(self.path.read_bytes(), corrupt)

    def test_save_refuses_serialized_document_over_read_cap(self):
        original = json.dumps({"version": 1, "language": "en"}).encode()
        self.path.write_bytes(original)
        settings = Settings(self.path)
        settings.keyboard_args = ["x" * SETTINGS_MAX_BYTES]
        settings.keyboard_args_present = True
        self.assertFalse(settings.save())
        self.assertIn("exceeds", settings.save_error)
        self.assertEqual(self.path.read_bytes(), original)

    def test_symlink_is_preserved_and_atomic_replace_targets_real_file(self):
        target = SCRATCH / "dotfiles" / "settings.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"future": 1, "language": "en"}))
        self.path.symlink_to(target)
        settings = Settings(self.path)
        settings.language = "de"
        self.assertTrue(settings.save())
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(target.read_text())["language"], "de")
        self.assertEqual(json.loads(self.path.read_text())["future"], 1)

    def test_save_merges_external_unknown_and_keyboard_args_edits(self):
        self.path.write_text(json.dumps({
            "language": "en", "keyboardArgs": ["-L", "240"], "future": {"v": 1},
            "keyboardCommand": ["/bin/sh"],
        }))
        settings = Settings(self.path)
        self.path.write_text(json.dumps({
            "language": "en", "keyboardArgs": ["-L", "300"], "future": {"v": 2},
            "keyboardCommand": ["wvkbd", "--hidden"],
        }))
        settings.language = "fr"
        self.assertTrue(settings.save())
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["language"], "fr")
        self.assertEqual(saved["keyboardArgs"], ["-L", "300"])
        self.assertEqual(saved["future"], {"v": 2})
        self.assertEqual(saved["keyboardCommand"], ["wvkbd", "--hidden"])

    def test_in_memory_keyboard_args_change_wins_over_external_value(self):
        self.path.write_text(json.dumps({"keyboardArgs": ["-L", "240"]}))
        settings = Settings(self.path)
        self.path.write_text(json.dumps({"keyboardArgs": ["-L", "300"]}))
        settings.keyboard_args = ["-L", "280"]
        settings.keyboard_args_present = True
        self.assertTrue(settings.save())
        self.assertEqual(json.loads(self.path.read_text())["keyboardArgs"], ["-L", "280"])

    def test_threaded_savers_preserve_each_others_owned_changes(self):
        self.path.write_text(json.dumps({"version": 1, "language": "system", "manageAntimicrox": False}))
        first = Settings(self.path)
        second = Settings(self.path)
        first.language = "fr"
        second.mapper_managed = True
        barrier = threading.Barrier(3)
        outcomes = []

        def save(settings):
            barrier.wait()
            outcomes.append(settings.save())

        threads = [
            threading.Thread(target=save, args=(first,)),
            threading.Thread(target=save, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [True, True])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["language"], "fr")
        self.assertTrue(saved["manageAntimicrox"])

    def test_stalled_settings_writer_times_out_without_blocking(self):
        self.path.write_text(json.dumps({"version": 1, "language": "en"}))
        settings = Settings(self.path)
        lock_path = SCRATCH / ".settings.json.lock"
        held = threading.Event()
        release = threading.Event()

        def hold_lock():
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held.set()
            release.wait(2)
            os.close(descriptor)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.assertTrue(held.wait(0.5))
        outcomes = []
        with (
            mock.patch("gaminghome.settings.SETTINGS_LOCK_TIMEOUT_S", 0.05, create=True),
            mock.patch("gaminghome.settings.SETTINGS_LOCK_RETRY_S", 0.01, create=True),
            mock.patch("gaminghome.settings.LOGGER.warning") as warning,
        ):
            try:
                for _attempt in range(2):
                    done = threading.Event()

                    def save():
                        settings.language = "fr"
                        outcomes.append(settings.save())
                        done.set()

                    saver = threading.Thread(target=save, daemon=True)
                    saver.start()
                    self.assertTrue(done.wait(0.4), "settings save blocked on its lock")
                    saver.join(0.2)
                    self.assertFalse(saver.is_alive())
            finally:
                release.set()
                holder.join(1)
            warning.assert_called_once()
        self.assertEqual(outcomes, [False, False])
        self.assertEqual(json.loads(self.path.read_text())["language"], "en")

    def test_future_version_is_not_downgraded_or_overwritten(self):
        self.path.write_text(json.dumps({"version": 1, "language": "ja"}))
        settings = Settings(self.path)
        original = {"version": 999, "language": "ja", "future": {"kept": True}}
        self.path.write_text(json.dumps(original))
        settings.language = "en"
        with mock.patch("gaminghome.settings.LOGGER.warning") as warning:
            self.assertFalse(settings.save())
            self.assertFalse(settings.save())
            warning.assert_called_once()
        self.assertEqual(json.loads(self.path.read_text()), original)

    def test_oversize_file_is_refused_and_atomic_file_is_private(self):
        self.path.write_text("x" * (SETTINGS_MAX_BYTES + 1))
        settings = Settings(self.path)
        self.assertIn("bytes", settings.load_error)
        self.assertTrue(settings.load_failed)
        self.assertFalse(settings.save())
        self.assertEqual(self.path.stat().st_size, SETTINGS_MAX_BYTES + 1)
        self.path.write_text("{}")
        settings = Settings(self.path)
        settings.language = "es"
        self.assertTrue(settings.save())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(SCRATCH.glob(".settings.json.*.tmp")), [])

    def test_keyboard_status_tolerates_settings_load_failure(self):
        self.path.write_text("{broken")
        settings = Settings(self.path)
        controller = KeyboardController(
            proc_root=SCRATCH / "missing-proc",
            pidfd_open=lambda _pid: (_ for _ in ()).throw(AssertionError("pidfd")),
            spawn=lambda argv: (_ for _ in ()).throw(AssertionError(argv)),
            which=lambda _name: None,
            visibility_provider=lambda _pid: Visibility("unknown"),
            settings_accessor=lambda: settings,
        )
        status = controller.status()
        self.assertIn("settings load error", status["command_error"])


class RealSignalPathTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux") and hasattr(signal, "SIGRTMIN")
                         and hasattr(os, "pidfd_open"), "needs Linux pidfd signals")
    def test_real_pidfd_delivery_uses_pipe_handshake_and_hard_deadlines(self):
        code = r"""
import ctypes
import signal
import sys

libc = ctypes.CDLL(None)
libc.prctl(15, b"wvkbd-mobintl", 0, 0, 0)
print("cold", flush=True)
command = sys.stdin.readline()
if command.strip() != "arm":
    raise SystemExit(2)
for name in ("SIGUSR1", "SIGUSR2", "SIGRTMIN"):
    signal.signal(getattr(signal, name), lambda _signum, _frame, name=name: print(name, flush=True))
print("armed", flush=True)
while True:
    signal.pause()
"""
        environment = dict(os.environ, **SESSION)
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

        def read_line(pipe, timeout=1.5):
            ready, _, _ = select.select([pipe], [], [], timeout)
            if not ready:
                self.fail(f"timed out reading child output after {timeout} seconds")
            line = pipe.readline()
            if not line:
                self.fail("child output closed")
            return line.strip()

        try:
            self.assertEqual(read_line(child.stdout), "cold")
            proc = SCRATCH / "real-proc"
            proc.mkdir(parents=True)
            (proc / str(child.pid)).symlink_to(Path("/proc") / str(child.pid), target_is_directory=True)
            identity = os.stat(f"/proc/{child.pid}/exe")

            def fake_stat(path):
                if path == "/usr/bin/wvkbd-mobintl":
                    return types.SimpleNamespace(
                        st_dev=identity.st_dev,
                        st_ino=identity.st_ino,
                        st_mode=stat.S_IFREG | 0o755,
                    )
                if path == str(proc / str(child.pid) / "exe"):
                    return identity
                raise FileNotFoundError(path)

            visibility = iter(("hidden", "hidden", "visible", "visible", "hidden", "hidden"))
            controller = KeyboardController(
                proc_root=proc,
                pidfd_open=os.pidfd_open,
                pidfd_send_signal=signal.pidfd_send_signal,
                stat_result=fake_stat,
                realpath=lambda path: path,
                which=lambda name: "/usr/bin/wvkbd-mobintl" if name == "wvkbd-mobintl" else None,
                run=lambda *_args: self.fail("helper should not be queried"),
                spawn=lambda argv: self.fail(f"unexpected spawn: {argv}"),
                boot_clock=lambda: time.clock_gettime(time.CLOCK_BOOTTIME) + 5,
                visibility_provider=lambda _pid: Visibility(next(visibility)),
                settings_accessor={},
                environment=SESSION,
            )
            cold = controller.show()
            self.assertFalse(cold["ok"])
            self.assertEqual(cold["detail"], STILL_STARTING)
            self.assertIsNone(child.poll())
            child.stdin.write("arm\n")
            child.stdin.flush()
            self.assertEqual(read_line(child.stdout), "armed")
            self.assertTrue(controller.show()["ok"])
            self.assertEqual(read_line(child.stdout), "SIGUSR2")
            self.assertTrue(controller.hide()["ok"])
            self.assertEqual(read_line(child.stdout), "SIGUSR1")
            self.assertTrue(controller.toggle()["ok"])
            self.assertEqual(read_line(child.stdout), "SIGUSR2")
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=1.5)
            if child.stdin:
                child.stdin.close()
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
            shutil.rmtree(SCRATCH, ignore_errors=True)


class KeyboardCliTest(unittest.TestCase):
    class FakeController:
        def status(self):
            return {
                "installed": True, "running": True, "backend": "wvkbd-mobintl",
                "last_command": None, "visibility": "visible", "command_error": None,
                "args_error": None, "ignored_legacy_override": False,
            }

        def show(self):
            return {"ok": True, "action": "show", "detail": "shown"}

        def hide(self):
            return {"ok": False, "action": "hide", "detail": "still starting"}

        def toggle(self):
            return {"ok": True, "action": "toggle", "detail": "toggled"}

    def run_main(self, arguments, controller, lock=None):
        from gaminghome.application import main

        stdout, stderr = io.StringIO(), io.StringIO()
        lock_patch = lock or contextlib.nullcontext()
        with (
            mock.patch("gaminghome.keyboard.KeyboardController", return_value=controller),
            mock.patch("gaminghome.keyboard.keyboard_action_lock", return_value=lock_patch) as lock_factory,
            mock.patch("gaminghome.application.GameModeApplication") as application,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["omarchy-game-mode", "keyboard", *arguments])
        application.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue(), lock_factory

    def test_status_bypasses_app_registration_and_action_lock(self):
        code, stdout, stderr, lock = self.run_main(["status"], self.FakeController())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["visibility"], "visible")
        self.assertEqual(stderr, "")
        lock.assert_not_called()

    def test_action_streams_and_exit_codes_are_correct(self):
        code, stdout, stderr, lock = self.run_main(["show"], self.FakeController())
        self.assertEqual((code, stdout, stderr), (0, "shown\n", ""))
        lock.assert_called_once()
        code, stdout, stderr, _ = self.run_main(["hide"], self.FakeController())
        self.assertEqual((code, stdout, stderr), (1, "", "still starting\n"))

    def test_usage_error_precedes_controller_and_app_creation(self):
        from gaminghome.application import main

        with (
            mock.patch("gaminghome.keyboard.KeyboardController") as controller,
            mock.patch("gaminghome.application.GameModeApplication") as application,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(main(["omarchy-game-mode", "keyboard", "bogus"]), 2)
        controller.assert_not_called()
        application.assert_not_called()
        self.assertIn("keyboard [toggle|show|hide|status]", stderr.getvalue())

    def test_lock_error_is_printed_and_no_action_runs(self):
        from gaminghome.application import main

        controller = self.FakeController()
        controller.show = mock.Mock()
        with (
            mock.patch("gaminghome.keyboard.KeyboardController", return_value=controller),
            mock.patch(
                "gaminghome.keyboard.keyboard_action_lock",
                side_effect=KeyboardLockError("another keyboard action is already in progress"),
            ),
            mock.patch("gaminghome.application.GameModeApplication"),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(["omarchy-game-mode", "keyboard", "show"])
        self.assertEqual(code, 1)
        self.assertIn("already in progress", stderr.getvalue())
        controller.show.assert_not_called()

    def test_async_request_api_is_removed(self):
        self.assertFalse(hasattr(KeyboardController, "request"))

    def test_mapper_cli_failed_save_is_nonzero_and_never_reports_new_value(self):
        from gaminghome.application import GameModeApplication

        stored = types.SimpleNamespace(
            mapper_managed=True,
            save_error=f"{os.path.expanduser('~')}/config denied",
            save=lambda: False,
            sanitize_error=lambda reason: reason.replace(os.path.expanduser("~"), "~"),
        )
        with mock.patch("gaminghome.application.settings_mod.Settings", return_value=stored):
            code, output = GameModeApplication().dispatch(["mapper", "off"])
        self.assertEqual(code, 1)
        self.assertIn("could not save settings: ~/config denied", output)
        self.assertNotIn("\noff", output)
        self.assertTrue(stored.mapper_managed)

    def test_mapper_main_reports_save_failure_on_stderr(self):
        from gaminghome.application import GameModeApplication, main

        stored = types.SimpleNamespace(
            mapper_managed=True,
            save_error=f"{os.path.expanduser('~')}/config denied",
            save=lambda: False,
            sanitize_error=lambda reason: reason.replace(os.path.expanduser("~"), "~"),
        )
        app = types.SimpleNamespace(runtime=None)
        app.dispatch = lambda args: GameModeApplication.dispatch(app, args)
        app.register = mock.Mock()
        app.get_is_remote = mock.Mock(return_value=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch("gaminghome.application.GameModeApplication", return_value=app),
            mock.patch("gaminghome.application.settings_mod.Settings", return_value=stored),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["omarchy-game-mode", "mapper", "off"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("could not save settings: ~/config denied", stderr.getvalue())
        self.assertNotIn("\noff", stderr.getvalue())

    def test_autostart_failure_is_nonzero_and_not_reported_as_set(self):
        from gaminghome.application import GameModeApplication

        with mock.patch(
            "gaminghome.application.settings_mod.set_autostart",
            return_value=(False, f"{os.path.expanduser('~')}/.config denied"),
        ):
            code, output = GameModeApplication().dispatch(["autostart", "on"])
        self.assertEqual(code, 1)
        self.assertIn("could not save settings", output)
        self.assertIn("~/.config denied", output)
        self.assertNotEqual(output, "on")

    def test_mapper_cli_success_reports_new_value(self):
        from gaminghome.application import GameModeApplication

        stored = types.SimpleNamespace(
            mapper_managed=True,
            save_error=None,
            save=mock.Mock(return_value=True),
            sanitize_error=lambda reason: reason,
        )
        with mock.patch("gaminghome.application.settings_mod.Settings", return_value=stored):
            self.assertEqual(
                GameModeApplication().dispatch(["mapper", "off"]),
                (0, "off"),
            )
        stored.save.assert_called_once()

    def test_mapper_cli_with_resident_runtime_does_not_report_failed_change(self):
        from gaminghome.application import GameModeApplication

        runtime = types.SimpleNamespace(
            set_mapper_managed=mock.Mock(return_value=False),
            settings=types.SimpleNamespace(save_error="lock timeout"),
        )
        app = GameModeApplication()
        app.runtime = runtime
        code, output = app.dispatch(["mapper", "off"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "could not save settings: lock timeout")
        runtime.set_mapper_managed.assert_called_once_with(False)

    def test_mapper_gui_save_failure_reverts_state_and_surfaces_warning(self):
        from gaminghome.application import Runtime

        save = mock.Mock(return_value=False)
        settings = types.SimpleNamespace(
            mapper_managed=True,
            save=save,
            save_error="lock timeout",
        )
        mapper = types.SimpleNamespace(managed=True, set_managed=mock.Mock())
        runtime = Runtime.__new__(Runtime)
        runtime.settings = settings
        runtime.mapper = mapper
        runtime.model = types.SimpleNamespace(requested_mode=None, message=None)
        runtime.t = lambda key: key
        runtime._sync_nav = mock.Mock()
        runtime.on_ui_changed = mock.Mock()
        self.assertFalse(runtime.set_mapper_managed(False))
        self.assertTrue(settings.mapper_managed)
        mapper.set_managed.assert_not_called()
        self.assertEqual(runtime.model.message[0], "error")
        self.assertIn("lock timeout", runtime.model.message[2]["reason"])
        runtime.on_ui_changed.assert_called_once()

    def test_language_gui_save_failure_keeps_previous_language(self):
        from gaminghome.application import Runtime

        settings = types.SimpleNamespace(
            language="en",
            save=mock.Mock(return_value=False),
            save_error="future version",
        )
        runtime = Runtime.__new__(Runtime)
        runtime.settings = settings
        runtime.model = types.SimpleNamespace(message=None)
        runtime.translator = object()
        original_translator = runtime.translator
        runtime.window = None
        runtime.t = lambda key: key
        runtime._apply_direction = mock.Mock()
        runtime.on_ui_changed = mock.Mock()
        self.assertFalse(runtime.set_language("fr"))
        self.assertEqual(settings.language, "en")
        self.assertIs(runtime.translator, original_translator)
        runtime._apply_direction.assert_not_called()
        self.assertEqual(runtime.model.message[0], "error")

    def test_pin_gui_save_failure_stays_unsaved_and_warns(self):
        from gaminghome.application import Runtime

        runtime = Runtime.__new__(Runtime)
        runtime.settings = types.SimpleNamespace(
            set_pinned=mock.Mock(return_value=False),
            save_error="read failed",
        )
        runtime.catalog = types.SimpleNamespace(get=lambda _app_id: types.SimpleNamespace(name="Steam"))
        runtime.model = types.SimpleNamespace(message=None)
        runtime.on_ui_changed = mock.Mock()
        self.assertFalse(runtime.set_pinned("steam", True))
        self.assertEqual(runtime.model.message[0], "error")
        runtime.on_ui_changed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
