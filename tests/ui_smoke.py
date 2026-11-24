"""Render Omarchy Game Mode with fake IO and save PNG screenshots.

Usage (headless, touches no live desktop state):
  gtk4-broadwayd :7 &
  GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 tests/ui_smoke.py OUTDIR [lang] [WxH] [word ...]

The words after the size choose what the fakes show. ``inputplumber`` renders a handheld where InputPlumber
owns the controller: Game Mode reads its virtual gamepad and Settings says so. ``charging``, ``low``,
``critical``, ``full`` and ``nobattery`` pick the battery (78 % and in use when none is named). The clock
always reads 9:41 PM on a 12-hour clock, whatever the machine's locale; the battery and the clock are fixed
fakes, so nothing reads UPower, sysfs or the time.
"""

import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from gaminghome import application, core, settings as settings_mod  # noqa: E402
from gaminghome.battery import CHARGING, DISCHARGING, FULL, Battery  # noqa: E402
from gaminghome.catalog import Availability, load_catalog, machine_arch  # noqa: E402
from gaminghome.clock import ClockStyle  # noqa: E402
from gaminghome.headerstatus import HeaderStatus  # noqa: E402
from gaminghome.i18n import Translator  # noqa: E402
from gaminghome.inputplumber import Composite, InputPlumberClient  # noqa: E402

BATTERIES = {
    "": Battery(78, DISCHARGING, 28173),
    "charging": Battery(64, CHARGING, 3900),
    "low": Battery(15, DISCHARGING, 4380),
    "critical": Battery(7, DISCHARGING, 1500),
    "full": Battery(100, FULL),
    "nobattery": None,
}


class Stub:
    available = False
    confirmed = None
    error = None
    connected = True
    nav_enabled = True
    managed = False
    external = False

    def status(self):
        return {"devices": [{"path": "/dev/input/event6", "name": "Demo Gamepad"}], "errors": []}

    def __getattr__(self, name):
        return lambda *a, **k: None


class VirtualPadStub(Stub):
    def status(self):
        return {"devices": [{"path": "/dev/input/event21", "name": "Microsoft X-Box One Elite 2 pad", "virtual": True}],
                "errors": [], "ambiguous": []}


def demo_inputplumber():
    """The client as it looks on a ROG Xbox Ally with evdev sources only; it is never started."""
    client = InputPlumberClient(lambda: None)
    client.owner, client.version, client.serial = ":demo", "0.82.0", 1
    client.composites = (Composite(
        path="/org/shadowblip/InputPlumber/CompositeDevice0", name="ASUS ROG Xbox Ally", profile="Default",
        targets=("mouse", "xbox-elite", "keyboard"), dbus_targets=1, sources=5, hidraw_sources=False,
        event_nodes=frozenset(), right_stick_button=False),)
    return client


class FakeBattery:
    """Stands in for the battery monitor: one fixed battery, reported when it starts, nothing read from the machine."""

    def __init__(self, on_change, battery):
        self.on_change, self.battery, self.running = on_change, battery, False

    def start(self):
        self.running = True
        self.on_change()

    def stop(self):
        self.running = False

    def poll(self):
        pass


class FakeRuntime(application.Runtime):
    def __init__(self, app, language, inputplumber=False, battery=""):
        self.app = app
        self.battery = BATTERIES[battery]
        self.settings = settings_mod.Settings(Path(os.devnull))
        self.settings.save = lambda: None
        self.catalog = load_catalog(application.DATA_DIR / "catalog.json")
        self.translator = Translator(application.DATA_DIR / "locales", language)
        self.arch = machine_arch()
        self.availability = Availability()
        self.installed = {"steam", "remote", "cloud", "moonlight"}
        self.installing = set()
        self.model = core.HomeModel(core.Classifier(self.catalog))
        self.model.ipc_connected = True
        steam = core.Window("0x1", "steam", "Steam Big Picture Mode", "steam")
        self.model.update_windows([steam], steam)
        self.window = None
        self._ui_dirty = None
        self._launch_timer = None
        self.interpreter = self.input = self.hypr = self.mapper = Stub()
        self.inputplumber = InputPlumberClient(lambda: None)
        if inputplumber:
            self.inputplumber, self.input = demo_inputplumber(), VirtualPadStub()

    def autostart_enabled(self):
        return False

    def header_status(self, on_clock, on_battery):
        return HeaderStatus(on_clock, on_battery, now=lambda: datetime.datetime(2026, 11, 23, 21, 41, 7),
                            style=ClockStyle(twelve_hour=True),
                            make_battery=lambda on_change: FakeBattery(on_change, self.battery))


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    language = sys.argv[2] if len(sys.argv) > 2 else "en"
    width, height = (int(v) for v in (sys.argv[3] if len(sys.argv) > 3 else "1280x720").split("x"))
    words = sys.argv[4:]
    inputplumber = "inputplumber" in words
    battery = next((word for word in words if word in BATTERIES), "")
    suffix = "".join(f"-{word}" for word in words)
    app = Gtk.Application(application_id="io.github.brianbaldock.OmarchyGameModeSmoke")

    def shoot(window, name):
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        w, h = window.get_width() or width, window.get_height() or height
        paintable.snapshot(snapshot, w, h)
        node = snapshot.to_node()
        if node is None:
            print(f"{name}: nothing rendered ({w}x{h})", file=sys.stderr)
            return
        texture = window.get_renderer().render_texture(node, None)
        texture.save_to_png(str(out / f"{name}.png"))

    def activate(app):
        display = Gdk.Display.get_default()
        # Headless broadway has no frame clock driving transitions.
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        Gtk.IconTheme.get_for_display(display).add_search_path(str(application.DATA_DIR / "icons"))
        from gaminghome.ui import HomeWindow, load_css
        load_css(display, settings_mod.theme_colors())
        runtime = FakeRuntime(app, language, inputplumber, battery)
        runtime._apply_direction()
        window = HomeWindow(app, runtime)
        runtime.window = window
        window.set_default_size(width, height)
        window.present()
        steps = [
            ("home", lambda: window._focus_default()),
            ("discover", lambda: window.show_page("discover")),
            ("app-sheet", lambda: window.open_app("geforce-now")),
            ("settings", lambda: (window.close_sheet(), window.open_settings(focus="mode"))),
        ]

        def run(index=0):
            if index > 0:
                shoot(window, f"{language}-{width}x{height}-{steps[index - 1][0]}{suffix}")
            if index == len(steps):
                app.quit()
                return False
            steps[index][1]()
            GLib.timeout_add(800, run, index + 1)
            return False

        GLib.timeout_add(1500, run)

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
