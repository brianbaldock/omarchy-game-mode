"""Render Omarchy Game Mode with fake IO and save PNG screenshots.

Usage (headless, touches no live desktop state):
  gtk4-broadwayd :7 &
  GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python3 tests/ui_smoke.py OUTDIR [lang] [WxH] [word ...]

The words after the size choose what the fakes show. ``inputplumber`` renders a handheld where InputPlumber
owns the controller: Game Mode reads its virtual gamepad and Settings says so. ``charging``, ``low``,
``critical``, ``full`` and ``nobattery`` pick the battery (78 % and in use when none is named). The clock
always reads 9:41 PM on a 12-hour clock, whatever the machine's locale; the battery and the clock are fixed
fakes, so nothing reads UPower, sysfs or the time.

Bluetooth is a scripted ``FakeBluetooth`` too, and never the machine's. Without a ``bt`` word it is on, with three
remembered devices and the controller connected. A ``bt`` word renders Home and then the Bluetooth sheet in one of
these states, and checks what the sheet does, so a wrong behaviour stops the run with an error:

  bt         on, three remembered devices, the controller connected at 80 %
  btoff      off                            bterror    the service could not be read
  btnone     no adapter: no button          btscan     a search under way with three devices nearby
  btempty    a search that found nothing    btmany     more devices than fit, and more than are listed
  btdupes    devices with the same name     btforget   Forget asking first
  btconfirm  a number to compare            btcode     a code to type on the device, half typed
  btpin      a PIN half entered on the pad  btpasskey  a passkey half entered on the pad
  btpairing  a pairing that is connecting   btfail     a connection that failed
  btpaired   a device paired that did not connect

Every ``bt`` word also checks that each way out of the sheet ends its search, and its pairing when there is one:
Menu and B close it, Settings replacing it, the window being built again and the window being hidden.
"""

import datetime
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from fakes.fake_bluetooth import FakeBluetooth  # noqa: E402
from gaminghome import application, bluetooth as bt, bluetoothsheet as sheet, core, settings as settings_mod  # noqa: E402
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
BLUETOOTH = ("bt", "btoff", "bterror", "btnone", "btscan", "btempty", "btmany", "btdupes", "btforget", "btconfirm",
             "btcode", "btpin", "btpasskey", "btpairing", "btfail", "btpaired")
SEARCHED = ("btscan", "btempty", "btmany", "btdupes", "btconfirm", "btcode", "btpin", "btpasskey", "btpairing",
            "btpaired")
PAIRING = ("btconfirm", "btcode", "btpin", "btpasskey", "btpairing", "btpaired")


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


def bluetooth_world(word, on_change):
    """What Bluetooth looks like before the sheet is opened. The names are made up and no address appears."""
    source = FakeBluetooth(on_change, {"btoff": bt.OFF, "bterror": bt.ERROR, "btnone": bt.ABSENT}.get(word, bt.ON))
    if word in ("bterror", "btnone"):
        return source
    if word == "btdupes":
        source.add_device("Buds", "headphones", paired=True, connected=True)
        source.add_device("Buds", "headphones", paired=True)
        source.add_device("", "other", paired=True)
        return source
    source.add_device("Wireless Controller", "gamepad", paired=True, connected=word != "btoff", battery=80)
    source.add_device("Studio Headphones", "headphones", paired=True)
    source.add_device("Desk Keyboard", "keyboard", paired=True)
    if word == "btmany":
        for number in range(1, 9):
            source.add_device(f"Shelf Speaker {number}", "speaker", paired=True)
    return source


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def press(window, name):
    """Select a control by its name and press A on it, as the controller does. GTK clicks a button a moment after
    it is activated, so this runs the main loop until it has."""
    check(window._focus_by_name(name), f"{name} cannot be selected; the sheet has {sorted(window._bt_buttons)}")
    button, clicked = window.get_focus(), []
    handler = button.connect("clicked", lambda *_: clicked.append(True))
    window.handle_action(core.ACTIVATE)
    context, deadline = GLib.MainContext.default(), time.monotonic() + 2
    while not clicked and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)
    button.disconnect(handler)
    check(clicked, f"pressing A on {name} did not click it")


def play(word, window, source):
    """Do in the open sheet what the scenario shows."""
    if word in SEARCHED:
        press(window, sheet.SEARCH)
    if word == "btscan":
        for name, kind in (("Travel Speaker", "speaker"), ("Pocket Mouse", "mouse"), ("Phone", "phone")):
            source.add_device(name, kind)
    elif word == "btempty":
        source.set_scanning(False)
    elif word == "btmany":
        for number in range(1, 71):
            source.add_device(f"Room Sensor {number}", "other")
    elif word == "btdupes":
        source.add_device("Buds", "headphones")
        source.add_device("Buds (2)", "headphones")
    elif word == "btfail":
        press(window, f"{sheet.CONNECT}2")
        source.fail(bt.UNREACHABLE)
    elif word == "btforget":
        press(window, f"{sheet.FORGET}3")
    elif word in PAIRING:
        press(window, f"{sheet.PAIR}{source.add_device('Travel Speaker', 'speaker')}")
        if word == "btconfirm":
            source.show_prompt(bt.CONFIRM, code="042917")
        elif word == "btcode":
            source.show_prompt(bt.CODE, code="314159")
            source.type_digits(2)
        elif word in ("btpin", "btpasskey"):
            source.show_prompt(bt.PIN if word == "btpin" else bt.PASSKEY, max_length=16 if word == "btpin" else 6)
            press(window, f"{sheet.PAD}0")
            press(window, f"{sheet.PAD}4")
            window._on_key(None, Gdk.KEY_2, 0, Gdk.ModifierType(0))       # and one from a keyboard
        elif word == "btpairing":
            source.stage(bt.STAGE_CONNECT)
        elif word == "btpaired":
            source.stage(bt.STAGE_CONNECT)
            source.fail(bt.UNREACHABLE, paired=True)


FIRST_FOCUS = {"bt": sheet.SEARCH, "btoff": sheet.POWER, "bterror": sheet.RETRY, "btforget": f"{sheet.FORGET_NO}3",
               "btconfirm": sheet.NO, "btcode": sheet.CANCEL, "btpin": sheet.PAD_DONE, "btpasskey": sheet.PAD_DONE,
               "btpairing": sheet.CANCEL, "btfail": f"{sheet.CONNECT}2", "btpaired": sheet.SEARCH}


def scroll_to_end(window):
    """Show the end of a long sheet. (Headless, the window is never active, so GTK does not scroll to the focus.)"""
    scroller = window.scrim.get_first_child()
    adjustment = scroller.get_vadjustment()
    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())


def check_shown(word, window, source):
    """What the sheet must look like before its picture is taken."""
    check(window.sheet == "bluetooth", f"the Bluetooth sheet is not open: {window.sheet!r}")
    focus = window.selected_name()
    check(focus in window._bt_buttons, f"the focus is outside the sheet: {focus!r}")
    if word in FIRST_FOCUS:
        check(focus == FIRST_FOCUS[word], f"the focus is on {focus}, not on {FIRST_FOCUS[word]}")
    if word in ("btpin", "btpasskey"):
        check(window._bluetooth.entry.digits == "042", "the pad and the keyboard did not type 0, 4 and 2")
    if word == "btmany":
        check(source.snapshot.truncated, "the source lists every device, so nothing says that more were found")


def middle(window, widget):
    ok, bounds = widget.compute_bounds(window)
    check(ok and bounds.get_width() > 0, f"{widget.get_name()} has not been laid out")
    return bounds.get_x() + bounds.get_width() / 2


def check_layout(language, window):
    """Once the sheet is laid out: the pad reads from the left in every language, and Arabic mirrors the rest."""
    buttons = window._bt_buttons
    if f"{sheet.PAD}1" in buttons:
        keys = [middle(window, buttons[f"{sheet.PAD}{key}"]) for key in ("1", "2", "3", "erase", "0", "done")]
        check(keys[0] < keys[1] < keys[2] and keys[3] < keys[4] < keys[5],
              f"the pad is not laid out from the left in {language}")
    if f"{sheet.CONNECT}1" in buttons:
        button = buttons[f"{sheet.CONNECT}1"]
        badge, title = button.get_parent().get_first_child(), button.get_parent().get_first_child().get_next_sibling()
        mirrored = middle(window, badge) > middle(window, title) > middle(window, button)
        check(mirrored == (language == "ar"), f"a device row is {'not ' if language == 'ar' else ''}mirrored in "
                                              f"{language}")


def check_behaviour(word, language, window, source):
    """After the picture: what pressing things does. Each scenario leaves the sheet open for the next check."""
    t = window.runtime.t
    if word != "btnone":
        check_layout(language, window)
    if word == "bt":
        press(window, f"{sheet.CONNECT}2")
        busy = [name for name in (f"{sheet.CONNECT}1", f"{sheet.FORGET}2", sheet.POWER)
                if window._bt_buttons[name].get_sensitive()]
        check(not busy, f"{busy} can be pressed while a device connects")
        check(window._bt_buttons[sheet.SEARCH].get_sensitive(), "Search is disabled while a device connects")
        source.finish()
        check(window.selected_name() == f"{sheet.CONNECT}2",
              f"the focus went to {window.selected_name()} after connecting, not back to the device")
        check(window._bt_buttons[f"{sheet.CONNECT}2"].get_label() == t("bluetooth.disconnect"), "no Disconnect")
    elif word == "btscan":
        check(window._focus_by_name(f"{sheet.PAIR}5"), "a nearby device cannot be selected")
        source.remove_device(5)
        check(window.selected_name() == sheet.SEARCH,
              f"the focus went to {window.selected_name()} when the selected device vanished, not to Search")
    elif word == "btforget":
        window.handle_action(core.BACK)
        check(window.sheet == "bluetooth" and window.selected_name() == f"{sheet.FORGET}3",
              "B did not take the question back and return to Forget")
        check(("forget", 3) not in source.calls, "a device was forgotten without being confirmed")
    elif word in ("btconfirm", "btcode", "btpairing"):
        cancels = source.calls.count(("cancel",))
        window.sheet = None                                         # as a change of language does
        window.build()
        check(source.calls.count(("cancel",)) > cancels and source.snapshot.activity is None,
              "building the window again during a pairing did not abandon it")
    elif word in ("btpin", "btpasskey"):
        cancels = source.calls.count(("cancel",))
        window.set_visible(False)
        check(window.sheet is None, "hiding the window left the Bluetooth sheet open")
        check(source.calls.count(("cancel",)) > cancels and not source.running,
              "hiding the window during a pairing did not abandon it and stop Bluetooth")
        check(window._bluetooth.entry.digits == "", "the digits typed outlived the prompt")
        window.set_visible(True)
    if word != "btnone":
        check_exits(window, source)


def check_exits(window, source):
    """Each way out of the sheet ends its search."""
    def ended():
        return source.calls.count(("scan", False))

    def reopen():
        if window.sheet is not None:
            window.close_sheet()
        window.open_bluetooth()
        check(window.sheet == "bluetooth", "the Bluetooth sheet did not open")
        return ended()

    before = reopen()
    window.handle_action(core.MENU)
    check(window.sheet is None, f"Menu left {window.sheet!r} open instead of closing the Bluetooth sheet")
    check(ended() > before, "Menu closed the sheet and its search went on")
    before = reopen()
    window.handle_action(core.BACK)
    check(window.sheet is None and ended() > before, "B did not close the sheet and end its search")
    before = reopen()
    window.open_settings()
    check(window.sheet == "settings" and ended() > before, "Settings took the sheet's place and its search went on")
    before = reopen()
    window.sheet = None                                             # as a change of language does
    window.build()
    check(ended() > before and not window._bluetooth.visiting, "building the window again left the sheet open")
    before = reopen()
    window.set_visible(False)
    check(window.sheet is None, "hiding the window left the Bluetooth sheet open")
    check(ended() > before and not source.running and source.calls[-1] == ("stop",),
          "hiding the window did not end the search and stop Bluetooth")
    window.set_visible(True)
    check(source.running, "showing the window again did not start Bluetooth")


class FakeRuntime(application.Runtime):
    def __init__(self, app, language, inputplumber=False, battery="", bluetooth="bt"):
        self.app = app
        self.battery = BATTERIES[battery]
        self.bluetooth_word = bluetooth
        self.bluetooth_source = None
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

    def bluetooth(self, on_change):
        self.bluetooth_source = bluetooth_world(self.bluetooth_word, on_change)
        return self.bluetooth_source


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    language = sys.argv[2] if len(sys.argv) > 2 else "en"
    width, height = (int(v) for v in (sys.argv[3] if len(sys.argv) > 3 else "1280x720").split("x"))
    words = sys.argv[4:]
    inputplumber = "inputplumber" in words
    battery = next((word for word in words if word in BATTERIES), "")
    bluetooth = next((word for word in words if word in BLUETOOTH), "")
    suffix = "".join(f"-{word}" for word in words)
    app = Gtk.Application(application_id="io.github.brianbaldock.OmarchyGameModeSmoke")
    failures = []

    def shoot(window, name):
        paintable = Gtk.WidgetPaintable.new(window)
        snapshot = Gtk.Snapshot()
        w, h = window.get_width() or width, window.get_height() or height
        paintable.snapshot(snapshot, w, h)
        node = snapshot.to_node()
        if node is None:
            return False
        texture = window.get_renderer().render_texture(node, None)
        texture.save_to_png(str(out / f"{name}.png"))
        return True

    def activate(app):
        display = Gdk.Display.get_default()
        # Headless broadway has no frame clock driving transitions.
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        Gtk.IconTheme.get_for_display(display).add_search_path(str(application.DATA_DIR / "icons"))
        from gaminghome.ui import HomeWindow, load_css
        load_css(display, settings_mod.theme_colors())
        runtime = FakeRuntime(app, language, inputplumber, battery, bluetooth or "bt")
        runtime._apply_direction()
        window = HomeWindow(app, runtime)
        runtime.window = window
        window.set_default_size(width, height)
        # Broadway's screen is 1024x768 until a browser says otherwise, and a default size gives way to the
        # screen; a size request does not, so the picture has the size asked for.
        window.set_size_request(width, height)
        window.present()
        source = runtime.bluetooth_source
        if bluetooth == "btnone":
            steps = [("home", lambda: (window._focus_default(), check(
                not window.bluetooth_button.get_visible() and window.bluetooth_button not in window.nav_rows()[0],
                "the header offers Bluetooth without an adapter")))]
        elif bluetooth:
            steps = [("home", lambda: window._focus_default()),
                     ("bluetooth", lambda: (window.open_bluetooth(), play(bluetooth, window, source),
                                            check_shown(bluetooth, window, source)))]
            if bluetooth in ("btmany", "btscan"):
                steps.append(("bluetooth-end", lambda: scroll_to_end(window)))
        else:
            steps = [
                ("home", lambda: window._focus_default()),
                ("discover", lambda: window.show_page("discover")),
                ("app-sheet", lambda: window.open_app("geforce-now")),
                ("settings", lambda: (window.close_sheet(), window.open_settings(focus="mode"))),
            ]

        def fail():
            import traceback
            failures.append(traceback.format_exc())
            app.quit()
            return False

        def run(index=0, tries=0):
            try:
                if index > 0:
                    name = f"{language}-{width}x{height}-{steps[index - 1][0]}{suffix}"
                    if not shoot(window, name):
                        if tries < 10:                              # the first frame can come late
                            GLib.timeout_add(300, run, index, tries + 1)
                            return False
                        print(f"{name}: nothing rendered", file=sys.stderr)
                if index == len(steps):
                    if bluetooth:
                        check_behaviour(bluetooth, language, window, source)
                    app.quit()
                    return False
                steps[index][1]()
            except Exception:
                return fail()
            GLib.timeout_add(800, run, index + 1)
            return False

        GLib.timeout_add(1500, run)

    app.connect("activate", activate)
    app.run([])
    if failures:
        print(failures[0], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
