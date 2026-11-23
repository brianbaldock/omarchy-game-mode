"""What the runtime shows and reports about InputPlumber, without windows, devices or a bus."""

import json
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gaminghome.application import Runtime
from gaminghome.i18n import Translator
from gaminghome.inputplumber import Composite, InputPlumberClient

DATA = Path(__file__).resolve().parent.parent / "data"
SCRATCH = Path(__file__).resolve().parent / "scratch" / "surface"
ROOT = "/org/shadowblip/InputPlumber"
PAD = {"path": "/dev/input/event21", "name": "Microsoft X-Box One Elite 2 pad", "virtual": True}
HIDDEN = {"path": "/dev/input/event6", "name": "ASUSTeK Computer Inc. N-KEY Device"}
DENIED = {"path": "/dev/input/event6", "error": "permission denied reading ASUSTeK Computer Inc. N-KEY Device"}
RC73YA_STATUS = {"name": "ASUS ROG Xbox Ally", "profile": "Default", "targets": ["mouse", "xbox-elite", "keyboard"],
                 "sources": 5, "hidraw_sources": False, "dbus_targets": 1, "right_stick_button": False}


def client(targets=("mouse", "xbox-elite", "keyboard"), state="ready") -> InputPlumberClient:
    """A client that is never started, in one of its states; "ready" is a ROG Xbox Ally after a discovery."""
    result = InputPlumberClient(lambda: None)
    result.name_known = state != "unknown"
    if state in ("pending", "failed", "empty", "ready"):
        result.owner = ":1.5"
    if state == "failed":
        result.error = "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: not allowed"
    if state in ("empty", "ready"):
        result.serial, result.version = 1, "0.82.0"
    if state == "ready":
        result.composites = (Composite(
            path=f"{ROOT}/CompositeDevice0", name="ASUS ROG Xbox Ally", profile="Default", targets=tuple(targets),
            dbus_targets=1, sources=5, hidraw_sources=False, event_nodes=frozenset({"/dev/input/event6"}),
            right_stick_button=False),)
    assert result.state == state
    return result


class SurfaceTest(unittest.TestCase):
    def runtime(self, inputplumber=None, **controllers):
        status = {"devices": [], "errors": [], "ambiguous": [], "unreadable": [], **controllers}
        translator = Translator(DATA / "locales", "en", {"LANG": "C"}, Path("/nonexistent/language"))
        runtime = SimpleNamespace(inputplumber=inputplumber or client(), input=SimpleNamespace(status=lambda: status),
                                  mapper=SimpleNamespace(external=False),
                                  t=lambda key, **params: translator(key, **params))
        runtime._inputplumber_gap = lambda current: Runtime._inputplumber_gap(runtime, current)
        return runtime

    def test_virtual_pad_reads_as_a_connected_controller(self):
        runtime = self.runtime(devices=[PAD])
        self.assertEqual(Runtime.controller_summary(runtime), {
            "label": "Controller connected", "state": "ok",
            "tooltip": "Microsoft X-Box One Elite 2 pad (InputPlumber)"})
        self.assertEqual(Runtime.inputplumber_summary(runtime), (
            "InputPlumber 0.82.0 manages ASUS ROG Xbox Ally with the profile Default. Game Mode reads its virtual "
            "gamepad and changes nothing in InputPlumber. It reads this controller through evdev only. It reports "
            "no right stick click (R3)."))

    def test_status_section_has_a_fixed_shape_without_identifiers(self):
        report = Runtime.inputplumber_status(self.runtime(devices=[PAD]))
        self.assertEqual(report, {
            "available": True, "state": "ready", "version": "0.82.0", "settled": True, "composites": [RC73YA_STATUS],
            "notes": ["evdev-only", "no-r3"], "error": None,
            "gamepad_nodes": [{"path": "/dev/input/event21", "name": "Microsoft X-Box One Elite 2 pad"}]})
        text = json.dumps(report).lower()
        for word in ("persistent", "serial", "uniq", "/org/shadowblip"):
            self.assertNotIn(word, text)

    def test_hidden_pad_without_a_usable_virtual_pad_is_information_not_a_warning(self):
        runtime = self.runtime(client(targets=("deck-uhid", "mouse")), unreadable=[HIDDEN])
        summary = Runtime.controller_summary(runtime)
        self.assertEqual((summary["label"], summary["state"]), ("Controller not readable", None))
        self.assertEqual(summary["tooltip"], (
            "ASUSTeK Computer Inc. N-KEY Device is present, but InputPlumber hides it and Game Mode cannot read it. "
            "Game Mode does not recognize its virtual controller type (deck-uhid), so use keyboard or touch here."))
        self.assertIn(summary["tooltip"], Runtime.inputplumber_summary(runtime))
        self.assertEqual(Runtime.inputplumber_status(runtime)["notes"],
                         ["unrecognized-target", "evdev-only", "no-r3", "hidden-unreadable"])
        runtime = self.runtime(ambiguous=["Microsoft X-Box One Elite 2 pad"], unreadable=[HIDDEN])
        self.assertIn("Game Mode uses none of them", Runtime.controller_summary(runtime)["tooltip"])
        self.assertIn("ambiguous-gamepad", Runtime.inputplumber_status(runtime)["notes"])

    def test_permission_warning_waits_until_inputplumber_has_answered(self):
        for state in ("unknown", "pending"):
            runtime = self.runtime(client(state=state), errors=[DENIED])
            self.assertEqual(Runtime.controller_summary(runtime),
                             {"label": "Keyboard and touch ready", "state": None, "tooltip": ""}, state)
            self.assertIsNone(Runtime.inputplumber_summary(runtime), state)
        runtime = self.runtime(client(state="absent"), errors=[DENIED])
        summary = Runtime.controller_summary(runtime)
        self.assertEqual((summary["label"], summary["state"], summary["tooltip"]),
                         ("Controller not readable", "warn", DENIED["error"]))
        self.assertIsNone(Runtime.inputplumber_summary(runtime))
        self.assertEqual(Runtime.inputplumber_status(runtime), {
            "available": False, "state": "absent", "version": None, "settled": True, "composites": [], "notes": [],
            "error": None, "gamepad_nodes": []})

    def test_only_a_successful_read_says_that_no_controller_is_managed(self):
        runtime = self.runtime(client(state="empty"))
        self.assertEqual(Runtime.inputplumber_summary(runtime), "InputPlumber is running but manages no controller.")
        self.assertEqual(Runtime.inputplumber_status(runtime)["notes"], ["no-device"])

        failed = "InputPlumber is running, but Game Mode could not read its state."
        runtime = self.runtime(client(state="failed"), errors=[DENIED])
        self.assertEqual(Runtime.inputplumber_summary(runtime), failed)
        self.assertEqual(Runtime.controller_summary(runtime), {
            "label": "Controller not readable", "state": "warn", "tooltip": f"{DENIED['error']}; {failed}"})
        report = Runtime.inputplumber_status(runtime)
        self.assertEqual((report["state"], report["settled"], report["notes"]), ("failed", False, []))
        self.assertIn("AccessDenied", report["error"])
        # Its virtual gamepad may be right there, but nothing says that it may be read.
        self.assertEqual(Runtime.controller_summary(self.runtime(client(state="failed"))),
                         {"label": "Keyboard and touch ready", "state": None, "tooltip": failed})


class RuntimeWiringTest(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        isolated = {"XDG_CONFIG_HOME": str(SCRATCH), "XDG_STATE_HOME": str(SCRATCH)}
        with mock.patch.dict(os.environ, isolated):
            self.runtime = Runtime(app=None)  # constructed only: nothing is started
        self.addCleanup(self.runtime.stop)

    def test_monitor_takes_its_allowlist_and_hints_from_the_client(self):
        runtime = self.runtime
        self.assertEqual(runtime.input.extra_gamepads, runtime.inputplumber.virtual_gamepads)
        self.assertEqual(runtime.input.on_candidate, runtime.inputplumber.hint)
        rescans = []
        runtime.input.rescan = lambda: rescans.append(1)
        runtime.inputplumber.on_change()
        self.assertEqual(rescans, [1])

    def test_status_reports_inputplumber_and_leaves_the_mapper_report_alone(self):
        status = self.runtime.status()
        self.assertEqual(status["inputplumber"], {"available": False, "state": "unknown", "version": None,
                                                  "settled": False, "composites": [], "notes": [], "error": None,
                                                  "gamepad_nodes": []})
        self.assertEqual(sorted(status["mode"]["mapper"]), ["available", "confirmed", "error", "external",
                                                            "initial_sets", "managed", "requested"])
        self.assertEqual(os.listdir(SCRATCH), [])


if __name__ == "__main__":
    unittest.main()
