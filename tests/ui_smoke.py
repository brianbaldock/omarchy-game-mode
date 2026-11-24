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
  btcancelpaired  a pairing given up just after the device had paired: it stays, and the sheet says so
  btconnecting    a device connecting, with the focus on its Connect, dimmed with what waits for it

Every ``bt`` word also checks that each way out of the sheet ends its search (Menu and B closing it, Settings taking
its place, the window being built again and the window being hidden), and that a click GTK delivers after the sheet
has closed does nothing (A then B on Search, A then Menu on Pair, Enter then Escape on Search). Giving up a pairing
is checked once for each way: B in ``btcode``, the window built again in ``btconfirm``, hidden in ``btpin`` and Menu
in ``btpasskey``; ``btpairing`` checks that a device paired already goes on connecting when the window is built again,
and ``btcancelpaired`` that a pairing given up after the device had paired is told about, then and at the next visit.
A window hidden with the sheet open must have the focus on the Bluetooth button once it is shown again and has drawn a
frame. ``btconnecting`` and ``btcode`` check that the focus ring on a dimmed control is drawn in the theme's accent,
as strong as on any other control: on Connect, Search and the switch while a device connects, and on "Canceling…".
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
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

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
             "btcode", "btpin", "btpasskey", "btpairing", "btfail", "btpaired", "btcancelpaired", "btconnecting")
SEARCHED = ("btscan", "btempty", "btmany", "btdupes", "btconfirm", "btcode", "btpin", "btpasskey", "btpairing",
            "btpaired", "btcancelpaired")
PAIRING = ("btconfirm", "btcode", "btpin", "btpasskey", "btpairing", "btpaired", "btcancelpaired")


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


def later(callback):
    """Report a change from the main loop, as the BlueZ client does, and never from inside the call that made it."""
    GLib.timeout_add(0, lambda: callback() and False)


def bluetooth_world(word, on_change):
    """What Bluetooth looks like before the sheet is opened. The names are made up and no address appears. Changes
    are reported later and a cancelled pairing waits for BlueZ to answer, as with the BlueZ client."""
    source = FakeBluetooth(on_change, {"btoff": bt.OFF, "bterror": bt.ERROR, "btnone": bt.ABSENT}.get(word, bt.ON),
                           defer=later, cancel_waits=True)
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
    settle()


def settle(milliseconds=60):
    """Run the main loop for a moment, as it runs between two presses."""
    context, deadline = GLib.MainContext.default(), time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)


def frame(window, action=lambda: None):
    """Do this and run the main loop until the window has drawn a frame since. GTK applies a change of style, and moves
    a focus that was left in removed widgets, only in a frame."""
    clock, drawn = window.get_frame_clock(), []
    handler = clock.connect("after-paint", lambda *_: drawn.append(True))
    action()
    window.queue_draw()
    context, deadline = GLib.MainContext.default(), time.monotonic() + 3
    while not drawn and time.monotonic() < deadline:
        if not context.iteration(False):
            time.sleep(0.005)
    clock.disconnect(handler)
    check(drawn, "the window drew no frame")


def ring(window, name):
    """The colour in the middle of the top border of the control of this name, drawn with the focus on it: the colour
    of its focus ring, as the window shows it."""
    check(window._focus_by_name(name), f"{name} cannot be selected")
    frame(window)
    ok, bounds = window._bt_buttons[name].compute_bounds(window)
    check(ok, f"{name} has not been laid out")
    x, y = int(bounds.get_x() + bounds.get_width() / 2), int(bounds.get_y()) + 1
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(window).snapshot(snapshot, window.get_width(), window.get_height())
    downloader = Gdk.TextureDownloader.new(window.get_renderer().render_texture(snapshot.to_node(), None))
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    pixels, stride = downloader.download_bytes()
    return tuple(pixels.get_data()[y * stride + x * 4:y * stride + x * 4 + 3])


def check_ring(window, *names):
    """The focus ring on each of these dimmed controls is the theme's accent at full strength, as on a plain control
    that is not dimmed."""
    accent = settings_mod.theme_colors()["accent"]
    wanted = tuple(int(accent[i:i + 2], 16) for i in (1, 3, 5))
    for name in names:
        seen = ring(window, name)
        check(all(abs(a - b) <= 3 for a, b in zip(seen, wanted)),
              f"the focus ring on {name} is {seen}, not the accent {wanted} it has on a plain control")


def texts_in(widget):
    """Every text that the labels under this widget hold, shown or not."""
    found = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child is not None:
        found.extend(texts_in(child))
        child = child.get_next_sibling()
    return found


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
    elif word == "btconnecting":
        press(window, f"{sheet.CONNECT}2")
    elif word == "btforget":
        press(window, f"{sheet.FORGET}3")
    elif word in PAIRING:
        speaker = source.add_device("Travel Speaker", "speaker")
        settle()
        press(window, f"{sheet.PAIR}{speaker}")
        if word == "btconfirm":
            window._focus_by_name(sheet.POWER)                       # the focus is still on the switch when the number
            source.show_prompt(bt.CONFIRM, code="042917")              # comes, and goes to No all the same
        elif word == "btcode":
            source.show_prompt(bt.CODE, code="314159")
            source.type_digits(2)
        elif word in ("btpin", "btpasskey"):
            source.show_prompt(bt.PIN if word == "btpin" else bt.PASSKEY, max_length=16 if word == "btpin" else 6)
            settle()
            press(window, f"{sheet.PAD}0")
            press(window, f"{sheet.PAD}4")
            window._on_key(None, Gdk.KEY_2, 0, Gdk.ModifierType(0))       # and one from a keyboard
        elif word == "btpairing":
            source.stage(bt.STAGE_CONNECT)
        elif word == "btpaired":
            source.stage(bt.STAGE_CONNECT)
            source.fail(bt.UNREACHABLE, paired=True)
        elif word == "btcancelpaired":
            window.handle_action(core.BACK)                         # given up while BlueZ still looks at the device,
            settle()
            source.finish_cancel(paired=True)                       # which had paired by then
    settle()


FIRST_FOCUS = {"bt": sheet.SEARCH, "btoff": sheet.POWER, "bterror": sheet.RETRY, "btforget": f"{sheet.FORGET_NO}3",
               "btconfirm": sheet.NO, "btcode": sheet.CANCEL, "btpin": sheet.PAD_DONE, "btpasskey": sheet.PAD_DONE,
               "btpairing": sheet.CLOSE, "btfail": sheet.DISMISS, "btpaired": sheet.DISMISS,
               "btcancelpaired": sheet.DISMISS, "btconnecting": f"{sheet.CONNECT}2"}


def scroll_to_end(window):
    """Show the end of a long sheet. (Headless, the window is never active, so GTK does not scroll to the focus.)"""
    scroller = window.scrim.get_first_child()
    adjustment = scroller.get_vadjustment()
    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())


def check_shown(word, window, source):
    """What the sheet must look like before its picture is taken."""
    check(window.sheet == "bluetooth", f"the Bluetooth sheet is not open: {window.sheet!r}")
    focus = window.get_focus() and window.get_focus().get_name()
    check(focus in window._bt_buttons, f"the focus is outside the sheet: {focus!r}")
    if word in FIRST_FOCUS:                                         # a name, or the start of one that ends in an id
        check(focus.startswith(FIRST_FOCUS[word]), f"the focus is on {focus}, not on {FIRST_FOCUS[word]}")
    if word in ("btpin", "btpasskey"):
        check(window._bluetooth.entry.digits == "042", "the pad and the keyboard did not type 0, 4 and 2")
    if word == "btmany":
        check(source.snapshot.truncated, "the source lists every device, so nothing says that more were found")
    if word == "btcancelpaired":
        check_cancel_told(window, source)
    if word in ("btempty", "btpaired", "btcancelpaired"):          # found nothing, or a device that was paired since
        said = window.runtime.t("bluetooth.nothingFound") in texts_in(window._bt_content)
        check(said == (word == "btempty"), f"'Nothing found' is {'missing' if not said else 'said'} in {word}")


def check_cancel_told(window, source):
    """A pairing given up after the device had paired leaves it paired: the sheet says so, and lists it."""
    t, shown = window.runtime.t, texts_in(window._bt_content)
    check(t("bluetooth.failed.canceledPaired", name="Travel Speaker") in shown
          and t("bluetooth.failed.canceledPairedHint") in shown,
          f"nothing says that the device had paired when the pairing was given up: {shown}")
    speaker = next(device.id for device in source.snapshot.devices if device.name == "Travel Speaker")
    check(f"{sheet.CONNECT}{speaker}" in window._bt_buttons, "the device that had paired is not in My devices")


def middle(window, widget):
    ok, bounds = widget.compute_bounds(window)
    check(ok and bounds.get_width() > 0, f"{widget.get_name()} has not been laid out")
    return bounds.get_x() + bounds.get_width() / 2


def check_layout(language, window):
    """Once the sheet is laid out: the pad reads from the left in every language, and Arabic mirrors the rest. Below
    1400 pixels the header shows the logo without the app's name."""
    settle()
    narrow = window.get_width() < 1400
    check(window.brand_title.get_visible() != narrow,
          f"the app's name is {'shown' if narrow else 'hidden'} in a window {window.get_width()} pixels wide")
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
        lit = [name for name in (f"{sheet.CONNECT}1", f"{sheet.FORGET}2", sheet.POWER, sheet.SEARCH)
               if not window._bt_buttons[name].has_css_class("dimmed")]
        check(not lit, f"{lit} are not dimmed while a device connects")
        check(window.get_focus() is window._bt_buttons[f"{sheet.CONNECT}2"],
              f"the focus left the device it connects for {window.get_focus() and window.get_focus().get_name()}")
        before = len(source.calls)
        press(window, f"{sheet.FORGET}2")                           # dimmed: pressing it does nothing
        check(len(source.calls) == before and f"{sheet.FORGET_YES}2" not in window._bt_buttons,
              "a dimmed control acted while a device connects")
        window.handle_action(core.UP)                               # the D-pad still moves over dimmed controls
        window.handle_action(core.DOWN)
        window._focus_by_name(f"{sheet.CONNECT}2")
        source.finish()
        settle()
        check(window.selected_name() == f"{sheet.CONNECT}2",
              f"the focus went to {window.selected_name()} after connecting, not back to the device")
        check(window._bt_buttons[f"{sheet.CONNECT}2"].get_label() == t("bluetooth.disconnect"), "no Disconnect")
    elif word == "btscan":
        check(window._focus_by_name(f"{sheet.PAIR}5"), "a nearby device cannot be selected")
        source.remove_device(5)
        settle()
        check(window.selected_name() == sheet.SEARCH,
              f"the focus went to {window.selected_name()} when the selected device vanished, not to Search")
    elif word == "btforget":
        window.handle_action(core.BACK)
        check(window.sheet == "bluetooth" and window.selected_name() == f"{sheet.FORGET}3",
              "B did not take the question back and return to Forget")
        check(("forget", 3) not in source.calls, "a device was forgotten without being confirmed")
    elif word == "btcode":
        window.handle_action(core.BACK)                             # B gives the pairing up ...
        settle()
        check(source.calls.count(("cancel",)) == 1 and window.sheet == "bluetooth"
              and window._bt_buttons[sheet.CANCEL].get_label() == t("bluetooth.canceling"),
              "B did not give the pairing up, or Cancel does not say that it is being canceled")
        check(window.selected_name() == sheet.CANCEL, f"the focus left Canceling… for {window.selected_name()}")
        check_ring(window, sheet.CANCEL)                            # dimmed until Bluetooth lets go
        window.handle_action(core.BACK)                             # ... and closes the sheet while BlueZ answers
        check(window.sheet is None and source.calls.count(("cancel",)) == 1,
              "B is stuck on a pairing given up already")
        source.finish_cancel()
        settle()
    elif word == "btconfirm":
        cancels = source.calls.count(("cancel",))
        window.sheet = None                                         # as a change of language does
        window.build()
        check(source.calls.count(("cancel",)) > cancels, "building the window again during a pairing did not abandon it")
        source.finish_cancel()                                      # BlueZ answers the cancel after the sheet went
        settle()
        window.open_bluetooth()
        settle()
        check(sheet.DISMISS not in window._bt_buttons and source.snapshot.failure is None,
              "the next visit is greeted with the pairing given up")
        window.close_sheet()
    elif word == "btpairing":
        window.sheet = None                                         # paired already: it connects as Connect does
        window.build()
        check(("cancel",) not in source.calls and source.snapshot.activity is not None,
              "building the window again cut short a pairing that was connecting")
        source.finish()
        settle()
    elif word == "btpin":
        prompt = source.snapshot.prompt.id
        window._focus_by_name(f"{sheet.PAD}5")                      # Enter answers from any key of the pad
        check(window.runtime.status()["selected"] == "bt:pad",
              f"the status tells which key of the pad is selected: {window.runtime.status()['selected']}")
        window._on_key(None, Gdk.KEY_Return, 0, Gdk.ModifierType(0))
        check(source.calls[-1] == ("answer", prompt, "042"), "Enter on the pad did not answer what was typed")
        settle()
        cancels = source.calls.count(("cancel",))
        window.set_visible(False)
        check(window.sheet is None, "hiding the window left the Bluetooth sheet open")
        check(source.calls.count(("cancel",)) > cancels and not source.running,
              "hiding the window during a pairing did not abandon it and stop Bluetooth")
        check(window._bluetooth.entry.digits == "", "the digits typed outlived the prompt")
        check(not any("042" in text for text in texts_in(window)), "the digits typed stay in a hidden label")
        window.set_visible(True)
    elif word == "btfail":
        press(window, sheet.DISMISS)
        check(window.get_focus() is window._bt_buttons.get(f"{sheet.CONNECT}2"),
              "dismissing the failure did not take the focus back to the device that failed")
    elif word == "btcancelpaired":
        press(window, sheet.CLOSE)
        window.open_bluetooth()
        settle()
        check_cancel_told(window, source)                           # still told at the next visit,
        check(window.get_focus() is window._bt_buttons[sheet.DISMISS], "the sheet opened away from its failure")
        press(window, sheet.DISMISS)
        check(sheet.DISMISS not in window._bt_buttons and source.snapshot.failure is None, "Dismiss left the failure")
        speaker = next(device.id for device in source.snapshot.devices if device.name == "Travel Speaker")
        check(window.get_focus() is window._bt_buttons.get(f"{sheet.CONNECT}{speaker}"),
              "dismissing the failure did not take the focus to the device that had paired")
    elif word == "btpasskey":
        window.handle_action(core.MENU)
        check(window.sheet is None and source.calls[-1] == ("cancel",), "Menu did not give up the pairing")
        check(not any("042" in text for text in texts_in(window)), "the digits typed stay in a hidden label")
        source.finish_cancel()
        settle()
    elif word == "btconnecting":
        check_ring(window, f"{sheet.CONNECT}2", sheet.SEARCH, sheet.POWER)    # a pill, a primary one and the switch
        source.finish()
        settle()
    if word != "btnone":
        check_exits(window, source)
        check_late_clicks(window, source)


def check_late_clicks(window, source):
    """GTK clicks a button 250 ms after A or Enter. A click on its way when the sheet goes must do nothing: A then
    B on Search, A then Menu on Pair, Enter then Escape on Search."""
    if source.snapshot.state != bt.ON:
        return

    def attempt(name, press_it, close, sent):
        window.open_bluetooth()
        check(window._focus_by_name(name), f"{name} cannot be selected")
        press_it()
        close()
        check(window.sheet is None, "the sheet did not close")
        after = len(source.calls)
        settle(400)
        late = [call for call in source.calls[after:] if call[:len(sent)] == sent]
        check(not late and not source.snapshot.scanning and source.snapshot.activity is None,
              f"{late or sent} reached Bluetooth after the sheet had closed")

    if source.snapshot.scanning:
        source.scan(False)
        settle()
    attempt(sheet.SEARCH, lambda: window.handle_action(core.ACTIVATE), lambda: window.handle_action(core.BACK),
            ("scan", True))
    window.open_bluetooth()
    press(window, sheet.SEARCH)                                     # the nearby list shows once Search was pressed
    nearby = next((device.id for device in bt.nearby(source.snapshot.devices)), None)
    if nearby is None:
        nearby = source.add_device("Late Speaker", "speaker")
        settle()
    attempt(f"{sheet.PAIR}{nearby}", lambda: window.handle_action(core.ACTIVATE),
            lambda: window.handle_action(core.MENU), ("pair", nearby))
    attempt(sheet.SEARCH, lambda: window.get_focus().activate(),
            lambda: window._on_key(None, Gdk.KEY_Escape, 0, Gdk.ModifierType(0)), ("scan", True))


def check_exits(window, source):
    """Each way out of the sheet ends its search, and hiding the window with the sheet open leaves the focus on the
    Bluetooth button that opened it."""
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
    frame(window, lambda: window.set_visible(True))
    settle()
    check(source.running, "showing the window again did not start Bluetooth")
    check(window.get_focus() is window.bluetooth_button,
          f"shown again, the window has the focus on {window.get_focus() and window.get_focus().get_name()}, "
          "not on the Bluetooth button that opened the sheet")


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
    # Not unique: a run that found another one on the session bus would hand its work over and exit at once, passing.
    app = Gtk.Application(application_id="io.github.brianbaldock.OmarchyGameModeSmoke",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
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

        stepped = [-1]                  # the window's frame counter when the last step had run

        def drawn():
            """A frame was drawn after the last step. Broadway draws few frames without a browser, and a picture
            taken before that frame shows the styles from before the step, such as the focus ring where it was."""
            clock = window.get_frame_clock()
            return clock is not None and clock.get_frame_counter() > stepped[0]

        def run(index=0, tries=0):
            try:
                if index > 0:
                    name = f"{language}-{width}x{height}-{steps[index - 1][0]}{suffix}"
                    fresh = drawn()
                    if not fresh and tries < 10:
                        GLib.timeout_add(300, run, index, tries + 1)
                        return False
                    if not shoot(window, name):
                        if tries < 10:                              # the first frame can come late
                            GLib.timeout_add(300, run, index, tries + 1)
                            return False
                        print(f"{name}: nothing rendered", file=sys.stderr)
                    elif not fresh:
                        print(f"{name}: no frame was drawn after the step, so the picture may show the one before",
                              file=sys.stderr)
                if index == len(steps):
                    if bluetooth:
                        check_behaviour(bluetooth, language, window, source)
                    app.quit()
                    return False
                steps[index][1]()
                clock = window.get_frame_clock()
                stepped[0] = clock.get_frame_counter() if clock is not None else -1
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
