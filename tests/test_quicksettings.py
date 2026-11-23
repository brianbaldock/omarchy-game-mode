import gc
import io
import json
import math
import os
import unittest
import weakref
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from gaminghome import quicksettings as quick


class Variant:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class FakeBus:
    def __init__(
        self,
        properties=None,
        devices=None,
        hold_cookie=41,
        *,
        release_error=None,
        synchronous_vanish=False,
    ):
        self.properties = properties or {}
        self.devices = devices or []
        self.hold_cookie = hold_cookie
        self.release_error = release_error
        self.synchronous_vanish = synchronous_vanish
        self.calls = []
        self.sets = []
        self.watches = []
        self.signals = []
        self.next_watch = 1
        self.next_signal = 1
        self.fail_names = set()

    def get_all(self, name, path, interface):
        self.calls.append(("get_all", name, path, interface))
        if name in self.fail_names:
            raise RuntimeError(f"{name} unavailable")
        value = self.properties.get((name, path, interface))
        if value is None:
            raise RuntimeError("missing interface")
        return value

    def set_property(self, name, path, interface, property_name, signature, value):
        self.sets.append((name, path, interface, property_name, signature, value))

    def call(self, name, path, interface, method, signature, arguments, reply_signature):
        self.calls.append(("call", name, path, interface, method, signature, arguments, reply_signature))
        if method == "EnumerateDevices":
            return list(self.devices)
        if method == "HoldProfile":
            return self.hold_cookie
        if method == "ReleaseProfile" and self.release_error is not None:
            raise self.release_error
        return None

    def watch_name(self, name, appeared, vanished):
        token = self.next_watch
        self.next_watch += 1
        self.watches.append((name, appeared, vanished, token))
        if self.synchronous_vanish:
            vanished()
        return token

    def unwatch_name(self, token):
        self.watches = [watch for watch in self.watches if watch[3] != token]

    def subscribe_signal(self, name, path, interface, member, callback):
        token = self.next_signal
        self.next_signal += 1
        self.signals.append((name, path, interface, member, callback, token))
        return token

    def unsubscribe_signal(self, token):
        self.signals = [signal for signal in self.signals if signal[5] != token]

    def emit_profile_released(self, cookie):
        for _name, _path, _interface, member, callback, _token in list(self.signals):
            if member == "ProfileReleased":
                callback([cookie])


class FakeRunner:
    def __init__(self, responder=None):
        self.responder = responder or (lambda _argv: SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        reply = self.responder(list(argv))
        return reply if reply is not None else SimpleNamespace(returncode=0, stdout="", stderr="")


def available(command):
    return f"/usr/bin/{command}"


def ppd_properties():
    return {
        "ActiveProfile": "balanced",
        "Profiles": Variant([
            Variant({"Profile": Variant("power-saver"), "CpuDriver": Variant("amd_pstate")}),
            Variant({"Profile": Variant("balanced")}),
            Variant({"Profile": Variant("performance")}),
        ]),
        "PerformanceDegraded": "",
        "ActiveProfileHolds": Variant([]),
        "BatteryAware": True,
    }


class PowerProfilesTest(unittest.TestCase):
    def setUp(self):
        self.endpoint = quick.PPD_ENDPOINTS[0]
        self.bus = FakeBus({(*self.endpoint,): ppd_properties()})
        self.power = quick.PowerProfiles(self.bus)

    def test_reads_and_labels_variant_profiles(self):
        status = self.power.read()
        self.assertTrue(status["available"])
        self.assertEqual(status["active"], "balanced")
        self.assertEqual(
            [profile["label"] for profile in status["profiles"]],
            ["Quiet", "Balanced", "Performance"],
        )
        self.assertEqual(status["service"], self.endpoint[0])

    def test_falls_back_to_legacy_service(self):
        legacy = quick.PPD_ENDPOINTS[1]
        self.bus.fail_names.add(self.endpoint[0])
        self.bus.properties[(*legacy,)] = ppd_properties()
        status = self.power.read()
        self.assertTrue(status["available"])
        self.assertEqual(status["service"], legacy[0])

    def test_sets_only_advertised_known_profile(self):
        result = self.power.set_profile("performance")
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.bus.sets[0][3:],
            ("ActiveProfile", "s", "performance"),
        )
        self.assertFalse(self.power.set_profile("turbo")["ok"])
        self.assertFalse(self.power.set_profile("performance;shutdown")["ok"])
        self.assertEqual(len(self.bus.sets), 1)

    def test_rejects_missing_profile_even_if_name_is_known(self):
        self.bus.properties[(*self.endpoint,)]["Profiles"] = [{"Profile": "balanced"}]
        self.assertFalse(self.power.set_profile("performance")["ok"])
        self.assertEqual(self.bus.sets, [])

    def test_hold_and_holder_exit_release_exact_cookie(self):
        result = self.power.hold_performance(
            "Game active",
            "com.example.Game",
            "com.example.Game",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.power.hold_cookie, 41)
        hold_call = next(call for call in self.bus.calls if len(call) > 4 and call[4] == "HoldProfile")
        self.assertEqual(hold_call[5:7], ("(sss)", ("performance", "Game active", "com.example.Game")))
        self.assertEqual(len(self.bus.watches), 1)
        self.bus.watches[0][2]()
        release_call = next(call for call in self.bus.calls if len(call) > 4 and call[4] == "ReleaseProfile")
        self.assertEqual(release_call[5:7], ("(u)", (41,)))
        self.assertIsNone(self.power.hold_cookie)
        self.assertTrue(self.power.close()["ok"])

    def test_realistic_hold_listing_preserves_owned_cookie(self):
        self.assertTrue(self.power.hold_performance()["ok"])
        self.bus.properties[(*self.endpoint,)]["ActiveProfileHolds"] = [{
            "ApplicationId": quick.APP_ID,
            "Profile": "performance",
            "Reason": "Game Mode performance session",
        }]
        status = self.power.read()
        self.assertTrue(status["owned_hold"])
        self.assertEqual(self.power.hold_cookie, 41)
        self.assertTrue(self.power.release_profile()["ok"])

    def test_empty_hold_listing_forgets_auto_released_cookie(self):
        self.assertTrue(self.power.hold_performance()["ok"])
        self.bus.properties[(*self.endpoint,)]["ActiveProfileHolds"] = []
        status = self.power.read()
        self.assertFalse(status["owned_hold"])
        self.assertIsNone(self.power.hold_cookie)
        self.assertFalse(any(
            len(call) > 4 and call[4] == "ReleaseProfile"
            for call in self.bus.calls
        ))

    def test_matching_profile_released_signal_forgets_cookie(self):
        self.assertTrue(self.power.hold_performance()["ok"])
        self.bus.emit_profile_released(40)
        self.assertEqual(self.power.hold_cookie, 41)
        self.bus.emit_profile_released(41)
        self.assertIsNone(self.power.hold_cookie)
        self.assertEqual(self.bus.signals, [])

    def test_failed_release_clears_state_before_a_new_hold(self):
        self.assertTrue(self.power.hold_performance()["ok"])
        self.bus.release_error = RuntimeError("PPD restarted")
        self.assertFalse(self.power.release_profile()["ok"])
        self.assertIsNone(self.power.hold_cookie)
        self.assertEqual(self.bus.watches, [])
        self.assertEqual(self.bus.signals, [])
        self.bus.release_error = None
        self.bus.hold_cookie = 42
        self.assertTrue(self.power.hold_performance("Fresh game")["ok"])
        self.assertEqual(self.power.hold_cookie, 42)

    def test_synchronous_holder_vanish_cannot_leave_a_stale_watch(self):
        bus = FakeBus({(*self.endpoint,): ppd_properties()}, synchronous_vanish=True)
        power = quick.PowerProfiles(bus)
        self.assertFalse(power.hold_performance(holder_name="com.example.Game")["ok"])
        self.assertIsNone(power.hold_cookie)
        self.assertIsNone(power._holder_watch)
        self.assertEqual(bus.watches, [])
        self.assertEqual(bus.signals, [])

    def test_holder_name_must_be_a_valid_dbus_name(self):
        for invalid in ("org..example", "1org.example", "has spaces", ":1.", "a\nb"):
            with self.subTest(invalid=invalid):
                bus = FakeBus({(*self.endpoint,): ppd_properties()})
                result = quick.PowerProfiles(bus).hold_performance(holder_name=invalid)
                self.assertFalse(result["ok"])
                self.assertFalse(any(
                    len(call) > 4 and call[4] == "HoldProfile"
                    for call in bus.calls
                ))

    def test_rejects_unbounded_hold_strings(self):
        self.assertFalse(self.power.hold_performance("x" * 161)["ok"])
        self.assertFalse(self.power.hold_performance("bad\nreason")["ok"])
        self.assertFalse(self.power.hold_performance("reason", object())["ok"])
        self.assertFalse(any(call[4] == "HoldProfile" for call in self.bus.calls))

    def test_read_failure_is_serializable(self):
        self.bus.fail_names.update((self.endpoint[0], quick.PPD_ENDPOINTS[1][0]))
        status = self.power.read()
        self.assertFalse(status["available"])
        json.dumps(status)


class BatteryTest(unittest.TestCase):
    def setUp(self):
        battery_path = "/org/freedesktop/UPower/devices/battery_BAT1"
        properties = {
            (quick.UPOWER, battery_path, quick.UPOWER_DEVICE): {
                "Type": 2,
                "IsPresent": True,
                "PowerSupply": True,
                "Percentage": 82.5,
                "State": 1,
                "Energy": 50.0,
                "EnergyFull": 54.0,
                "EnergyFullDesign": 60.0,
                "EnergyRate": 18.75,
                "TimeToEmpty": 0,
                "TimeToFull": 1200,
                "ChargeEndThreshold": 80,
            },
            (quick.ASUSD, quick.ASUSD_PATH, quick.ASUSD_PLATFORM): {
                "ChargeControlEndThreshold": 90,
            },
        }
        self.bus = FakeBus(properties, [battery_path])
        self.battery = quick.Battery(self.bus)

    def test_reads_upower_health_and_asusd_not_upower_limit(self):
        status = self.battery.read()
        self.assertTrue(status["available"])
        self.assertEqual(status["percentage"], 82.5)
        self.assertEqual(status["state"], "charging")
        self.assertEqual(status["health_percent"], 90.0)
        self.assertEqual(status["energy_rate_w"], 18.75)
        self.assertEqual(status["time_to_full_s"], 1200)
        self.assertEqual(status["charge_limit_percent"], 90)
        self.assertEqual(status["charge_limit_source"], "asusd")

    def test_display_device_fills_in_without_design_health(self):
        path = "/org/freedesktop/UPower/devices/DisplayDevice"
        self.bus.devices = [path]
        self.bus.properties = {
            (quick.UPOWER, path, quick.UPOWER_DEVICE): {
                "Type": 2,
                "IsPresent": True,
                "PowerSupply": True,
                "Percentage": 42.0,
                "State": 2,
                "EnergyFull": 50.0,
                "EnergyFullDesign": 0.0,
            },
            (quick.ASUSD, quick.ASUSD_PATH, quick.ASUSD_PLATFORM): {},
        }
        status = self.battery.read()
        self.assertTrue(status["available"])
        self.assertIsNone(status["health_percent"])

    def test_charge_limit_is_bounded_and_uses_byte_property(self):
        self.assertTrue(self.battery.set_charge_limit(85)["ok"])
        self.assertTrue(self.battery.set_charge_limit("100")["ok"])
        self.assertEqual(self.bus.sets[-1][3:], ("ChargeControlEndThreshold", "y", 100))
        for invalid in (59, 61, 101, 85.0, True, "80%", "NaN", "9" * 50, "80;reboot"):
            with self.subTest(invalid=invalid):
                self.assertFalse(self.battery.set_charge_limit(invalid)["ok"])

    def test_charge_limit_can_be_explicitly_read_only(self):
        battery = quick.Battery(self.bus, charge_limit_writable=False)
        status = battery.read()
        self.assertFalse(status["charge_limit_writable"])
        self.assertFalse(battery.set_charge_limit(80)["ok"])
        self.assertEqual(self.bus.sets, [])


class GioSystemBusTest(unittest.TestCase):
    def test_set_property_constructs_scalar_variant_without_a_bus_call(self):
        from gi.repository import Gio, GLib

        class Connection:
            def __init__(self):
                self.arguments = None

            def call_sync(self, *arguments):
                self.arguments = arguments

        for signature, expected in (("s", "balanced"), ("y", 80)):
            with self.subTest(signature=signature):
                connection = Connection()
                bus = quick.GioSystemBus()
                bus._connection = connection
                bus._gio, bus._glib = Gio, GLib
                bus.set_property(
                    "service",
                    "/path",
                    "interface",
                    "Property",
                    signature,
                    expected,
                )
                parameters = connection.arguments[4]
                value = parameters.get_child_value(2).get_variant()
                self.assertEqual(value.unpack(), expected)


class BrightnessTest(unittest.TestCase):
    def test_parse_machine_output_matrix(self):
        cases = (
            ("amdgpu_bl1,backlight,0,0%,65535", (0.0, 0, 65535)),
            ("amdgpu_bl1,backlight,32768,50%,65535", (50.0, 32768, 65535)),
            ("noise\namdgpu_bl1,backlight,65535,100%,65535\n", (100.0, 65535, 65535)),
        )
        for output, expected in cases:
            with self.subTest(output=output):
                value = quick.parse_brightness_output(output)
                self.assertEqual((value["percent"], value["raw"], value["maximum"]), expected)
        for invalid in ("", "32768", "x,backlight,1,NaN,10", "x,backlight,2,20%,0"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    quick.parse_brightness_output(invalid)

    def test_absolute_and_relative_sets_use_fixed_argv_and_floor(self):
        def respond(argv):
            if argv[1:] == ["-m", "info"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="amdgpu_bl1,backlight,32768,50%,65535\n",
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        runner = FakeRunner(respond)
        brightness = quick.Brightness(runner, available)
        self.assertTrue(brightness.set(0)["ok"])
        self.assertTrue(brightness.set_level("-100")["ok"])
        self.assertTrue(brightness.set_level("+75")["ok"])
        self.assertEqual(
            [call[0] for call in runner.calls if call[0][1:3] == ["-m", "set"]],
            [
                ["brightnessctl", "-m", "set", "5%"],
                ["brightnessctl", "-m", "set", "5%"],
                ["brightnessctl", "-m", "set", "100%"],
            ],
        )
        for _argv, kwargs in runner.calls:
            self.assertEqual(kwargs["timeout"], quick.COMMAND_TIMEOUT_S)
            self.assertNotIn("shell", kwargs)

    def test_rejects_nan_huge_and_injection_values(self):
        runner = FakeRunner(lambda _argv: SimpleNamespace(
            returncode=0,
            stdout="amdgpu_bl1,backlight,32768,50%,65535\n",
            stderr="",
        ))
        brightness = quick.Brightness(runner, available)
        for value in ("nan", "inf", "1e999", "20;touch x", "9" * 40, 10_000):
            with self.subTest(value=value):
                self.assertFalse(brightness.set(value)["ok"])
        self.assertEqual(len(runner.calls), 6)
        self.assertFalse(any("set" in call[0] for call in runner.calls))


class AudioTest(unittest.TestCase):
    def test_parse_volume_matrix(self):
        normal = quick.parse_volume_output("Volume: 0.45\n")
        self.assertEqual(normal, {"volume": 0.45, "percent": 45.0, "muted": False})
        localized = quick.parse_volume_output("Volume: 0,45 [MUTED]\n")
        self.assertEqual(localized, {"volume": 0.45, "percent": 45.0, "muted": True})
        muted = quick.parse_volume_output(" Volume: 1.50 [MUTED] ")
        self.assertEqual(muted["percent"], 150.0)
        self.assertTrue(muted["muted"])
        for invalid in ("garbage", "Volume: nan", "Volume: -0.2", "Volume: 1.0 MUTED"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    quick.parse_volume_output(invalid)

    def test_reads_sink_and_source_separately(self):
        outputs = {
            quick.DEFAULT_SINK: "Volume: 0.45 [MUTED]\n",
            quick.DEFAULT_SOURCE: "Volume: 1.00\n",
        }
        runner = FakeRunner(lambda argv: SimpleNamespace(
            returncode=0,
            stdout=outputs[argv[-1]],
            stderr="",
        ))
        with patch.dict(os.environ, {"LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8"}):
            status = quick.Audio(runner, available).read()
        self.assertEqual((status["volume"], status["muted"]), (45.0, True))
        self.assertEqual((status["mic_volume"], status["mic_muted"]), (100.0, False))
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ["wpctl", "get-volume", quick.DEFAULT_SINK],
                ["wpctl", "get-volume", quick.DEFAULT_SOURCE],
            ],
        )
        for _argv, kwargs in runner.calls:
            self.assertEqual(kwargs["env"]["LC_ALL"], "C")
            self.assertEqual(kwargs["env"]["LANG"], "C")

    def test_volume_is_clamped_and_mute_argv_is_fixed(self):
        runner = FakeRunner()
        audio = quick.Audio(runner, available)
        self.assertTrue(audio.set_volume(150)["ok"])
        self.assertTrue(audio.set_mute(True)["ok"])
        self.assertTrue(audio.set_mic_mute(False)["ok"])
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ["wpctl", "set-volume", quick.DEFAULT_SINK, "1"],
                ["wpctl", "set-mute", quick.DEFAULT_SINK, "1"],
                ["wpctl", "set-mute", quick.DEFAULT_SOURCE, "0"],
            ],
        )
        self.assertFalse(audio.set_mute(1)["ok"])
        self.assertFalse(audio.set_volume("50;amixer")["ok"])
        self.assertFalse(audio.set_volume(math.nan)["ok"])
        self.assertEqual(len(runner.calls), 3)

    def test_relative_volume_caps_at_100(self):
        def respond(argv):
            stdout = "Volume: 0.90\n" if argv[1] == "get-volume" else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        runner = FakeRunner(respond)
        self.assertTrue(quick.Audio(runner, available).set_volume("+30")["ok"])
        self.assertEqual(runner.calls[-1][0], ["wpctl", "set-volume", quick.DEFAULT_SINK, "1"])


class PowerActionsTest(unittest.TestCase):
    def test_actions_require_literal_confirmation_and_do_not_run(self):
        bus = FakeBus()
        runner = FakeRunner()
        actions = quick.PowerActions(bus, runner, available)
        for action in ("suspend", "reboot", "poweroff", "lock"):
            with self.subTest(action=action):
                result = actions.perform(action)
                self.assertFalse(result["ok"])
                self.assertEqual(result["detail"], "needs_confirmation")
        self.assertEqual(bus.calls, [])
        self.assertEqual(runner.calls, [])

    def test_confirmed_actions_use_logind_or_fixed_omarchy_argv(self):
        bus = FakeBus()
        runner = FakeRunner()
        actions = quick.PowerActions(bus, runner, available)
        self.assertTrue(actions.perform("suspend", confirm=True)["ok"])
        suspend = bus.calls[-1]
        self.assertEqual(suspend[4:7], ("Suspend", "(b)", (False,)))
        self.assertTrue(actions.perform("poweroff", confirm=True)["ok"])
        self.assertEqual(runner.calls[0][0], ["omarchy", "system", "shutdown"])
        self.assertEqual(runner.calls[0][1]["timeout"], quick.COMMAND_TIMEOUT_S)

    def test_unknown_action_is_rejected(self):
        runner = FakeRunner()
        self.assertFalse(quick.PowerActions(FakeBus(), runner, available).perform("delete")["ok"])
        self.assertEqual(runner.calls, [])


class OSDTest(unittest.TestCase):
    def test_feedback_is_fixed_bounded_argv_and_best_effort(self):
        runner = FakeRunner(lambda _argv: SimpleNamespace(returncode=1, stdout="", stderr="not shown"))
        osd = quick.OSD(runner, available)
        osd.progress("brightness", 33)
        self.assertEqual(
            runner.calls[0][0],
            ["omarchy", "osd", "-i", "brightness", "-p", "33", "-d", "1200"],
        )
        self.assertEqual(runner.calls[0][1]["timeout"], quick.OSD_TIMEOUT_S)
        osd.show(icon="brightness;reboot", progress=50)
        osd.show(message="bad\nmessage", progress=50)
        osd.show(progress=math.nan)
        self.assertEqual(len(runner.calls), 1)


class StubProvider:
    def __init__(self, value=None, error=None):
        self.value = value or {"available": True}
        self.error = error

    def read(self):
        if self.error:
            raise self.error
        return dict(self.value)

    def release_profile(self):
        return quick._ok("released")

    def set_profile(self, value):
        return quick._ok(f"profile {value}")

    def set_level(self, value):
        return quick._ok(f"brightness {value}")

    def set_volume(self, value):
        return quick._ok(f"volume {value}")

    def set_mute(self, value):
        return quick._ok(f"mute {value}")

    def set_mic_mute(self, value):
        return quick._ok(f"mic {value}")

    def set_charge_limit(self, value):
        return quick._ok(f"charge {value}")

    def hold_performance(self, *args):
        return quick._ok("held")

    def perform(self, _action, *, confirm=False):
        return quick._failed("needs_confirmation") if not confirm else quick._ok("action")


class AggregatorTest(unittest.TestCase):
    def settings(self, brightness=None):
        return quick.QuickSettings(
            power=StubProvider(),
            battery=StubProvider(),
            brightness=brightness or StubProvider(),
            audio=StubProvider(),
            power_actions=StubProvider(),
            osd=StubProvider(),
        )

    def test_provider_failure_does_not_break_snapshot(self):
        settings = self.settings(StubProvider(error=RuntimeError("broken sensor")))
        self.addCleanup(settings.close)
        snapshot = settings.snapshot()
        self.assertEqual(snapshot["brightness"]["available"], False)
        self.assertIn("broken sensor", snapshot["brightness"]["error"])
        self.assertTrue(snapshot["power"]["available"])
        self.assertTrue(snapshot["audio"]["available"])
        json.dumps(snapshot)

    def test_dispatch_allowlist_and_confirmation_are_strict(self):
        settings = self.settings()
        self.addCleanup(settings.close)
        for action, value in (
            ("unknown", None),
            ("mute", "yes"),
            ("release_hold", "cookie"),
            ("suspend", True),
            ("suspend", {"confirm": True}),
            ("hold_performance", {"reason": "x", "extra": "y"}),
        ):
            with self.subTest(action=action):
                self.assertFalse(settings.apply(action, value)["ok"])
        self.assertFalse(settings.apply("suspend")["ok"])
        self.assertTrue(settings.apply("suspend", confirm=True)["ok"])


class QuickCliTest(unittest.TestCase):
    class FakeSettings:
        def __init__(self, failure=False):
            self.failure = failure
            self.calls = []

        def snapshot(self):
            return {"power": {"active": "balanced"}, "battery": {"percentage": 80}}

        def apply(self, action, value=None, **kwargs):
            self.calls.append((action, value, kwargs))
            return {"ok": not self.failure, "detail": "done" if not self.failure else "provider failed"}

    def test_status_is_json(self):
        code, output = quick.cli(["status"], self.FakeSettings())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["battery"]["percentage"], 80)

    def test_valid_commands_and_one_line_output(self):
        cases = (
            (["profile", "performance"], ("profile", "performance")),
            (["brightness", "+10"], ("brightness", "+10")),
            (["volume", "-5"], ("volume", "-5")),
            (["volume", "mute"], ("mute", True)),
            (["volume", "unmute"], ("mute", False)),
            (["mic", "mute"], ("mic_mute", True)),
            (["mic", "unmute"], ("mic_mute", False)),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                settings = self.FakeSettings()
                self.assertEqual(quick.cli(argv, settings), (0, "done"))
                self.assertEqual(settings.calls[0][:2], expected)

    def test_usage_and_provider_failures_have_distinct_codes(self):
        settings = self.FakeSettings()
        for argv in (
            [],
            ["profile"],
            ["profile", "turbo"],
            ["profile", "performance;reboot"],
            ["brightness", "nan"],
            ["brightness", "999"],
            ["brightness", "20;id"],
            ["volume", "101"],
            ["volume", "toggle"],
            ["mic", "set"],
            ["status", "extra"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(quick.cli(argv, settings)[0], 2)
        self.assertEqual(quick.cli(["profile", "balanced"], self.FakeSettings(failure=True)), (1, "provider failed"))


class RuntimeQuickTest(unittest.TestCase):
    def test_runtime_quick_attribute_is_created_once_lazily(self):
        from gaminghome.application import Runtime

        runtime = object.__new__(Runtime)
        sentinel = StubProvider()
        with patch("gaminghome.quicksettings.QuickSettings", return_value=sentinel) as constructor:
            self.assertIs(runtime.quick, sentinel)
            self.assertIs(runtime.quick, sentinel)
        constructor.assert_called_once_with()

    def test_main_quick_answers_without_registering_a_resident_application(self):
        from gaminghome.application import main

        class LocalOnlyApplication:
            def __init__(self):
                self.registered = False
                self.arguments = None

            def register(self, _cancellable):
                self.registered = True

            def dispatch(self, arguments):
                self.arguments = arguments
                return 0, '{"battery":{"percentage":80}}'

            def get_is_remote(self):
                raise AssertionError("quick should not inspect the resident application")

        application = LocalOnlyApplication()
        output = io.StringIO()
        with patch("gaminghome.application.GameModeApplication", return_value=application):
            with redirect_stdout(output):
                self.assertEqual(main(["omarchy-game-mode", "quick", "status"]), 0)
        self.assertFalse(application.registered)
        self.assertEqual(application.arguments, ["quick", "status"])
        self.assertIn('"percentage":80', output.getvalue())


if __name__ == "__main__":
    unittest.main()
