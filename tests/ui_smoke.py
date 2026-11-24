"""Render Omarchy Game Mode with fake IO and save PNG screenshots.

Usage (headless, touches no live desktop state):
  gtk4-broadwayd --address 127.0.0.1 :7 &
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

Every ``bt`` word also checks that each way out of the sheet ends its search (Menu and B closing it, Settings taking
its place, the window being built again and the window being hidden), and that a click GTK delivers after the sheet
has closed does nothing (A then B on Search, A then Menu on Pair, Enter then Escape on Search). Giving up a pairing
is checked once for each way: B in ``btcode``, the window built again in ``btconfirm``, hidden in ``btpin`` and Menu
in ``btpasskey``; ``btpairing`` checks that a device paired already goes on connecting when the window is built again.

The power profile is a scripted ``FakePower``, never the machine's: nothing reaches power-profiles-daemon. Without a
``pw`` word it offers all three profiles with performance active. It answers the way the client does: from inside
the window's own calls, and from the main loop for what the service does; a request is answered when the script
says so. A ``pw`` word (use one ``bt`` or ``pw`` word at a time) renders Home and then the power sheet:

  pw          performance active, three profiles          pwsaver      power-saver active
  pwbalanced  balanced active                              pwpending    performance asked for, no answer yet
  pwfail      performance refused (denied)                 pwerror      the service could not be read
  pwnone      only one real profile: no button             pwheld       two programs ask for performance
  pwdegraded  performance held back because of heat        pwelsewhere  another program changed it afterwards
  pwnotremembered  Omarchy could not keep the choice       pwmismatch   the firmware says quiet, not performance

Every ``pw`` word but ``pwnone`` checks where the focus starts and that the status command names it neutrally, that
each way out of the sheet (B, Escape, Menu, Close, Settings or Bluetooth taking its place, the window built again or
hidden) leaves it and takes its widgets out of the window, and that a click GTK delivers after the sheet has closed
chooses nothing (A then B, A then Menu, Enter then Escape on another profile). ``pwpending`` checks that the focus
stays on the option asked for, which stays sensitive, until the answer and after it; ``pwfail`` that a failure is
not there at the next visit, also one that arrives after the sheet closed; ``pwerror`` that Try again reads the
service; ``pw`` that a profile vanishing from the list does not leave the focus on a dead button; ``pwnone`` that
there is no power button until a second profile is offered.
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
from fakes.fake_power import FakePower  # noqa: E402
from gaminghome import application, bluetooth as bt, bluetoothsheet as sheet, core, settings as settings_mod  # noqa: E402
from gaminghome import power, powersheet as pw  # noqa: E402
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
POWER = ("pw", "pwsaver", "pwbalanced", "pwpending", "pwfail", "pwerror", "pwnone", "pwheld", "pwdegraded",
         "pwelsewhere", "pwnotremembered", "pwmismatch")
CHOOSING = ("pwpending", "pwfail", "pwelsewhere", "pwnotremembered")     # performance is chosen from balanced


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
    check(window._focus_by_name(name),
          f"{name} cannot be selected; the sheet has {sorted(window._bt_buttons or window._pw_buttons)}")
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
    settle()


FIRST_FOCUS = {"bt": sheet.SEARCH, "btoff": sheet.POWER, "bterror": sheet.RETRY, "btforget": f"{sheet.FORGET_NO}3",
               "btconfirm": sheet.NO, "btcode": sheet.CANCEL, "btpin": sheet.PAD_DONE, "btpasskey": sheet.PAD_DONE,
               "btpairing": sheet.CLOSE, "btfail": f"{sheet.CONNECT}2", "btpaired": sheet.DISMISS}


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
    elif word == "btpasskey":
        window.handle_action(core.MENU)
        check(window.sheet is None and source.calls[-1] == ("cancel",), "Menu did not give up the pairing")
        check(not any("042" in text for text in texts_in(window)), "the digits typed stay in a hidden label")
        source.finish_cancel()
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
    settle()


# ---- the power profile ------------------------------------------------------------------------------------------
def power_world(word, on_change):
    """What the power service looks like before the sheet is opened; the programs' names are made up. Like the
    client, the source reports from inside the window's own calls, and reports what the service does from the main
    loop; a request is answered when the script says so."""
    active = {"pwsaver": power.SAVER, "pwbalanced": power.BALANCED}.get(word, power.PERFORMANCE)
    options = {}
    if word in CHOOSING or word == "pwnone":
        active = power.BALANCED
    if word == "pwerror":
        options["state"] = power.ERROR
    elif word == "pwnone":
        options["available"] = (power.BALANCED,)
    elif word == "pwheld":
        options["holds"] = (power.Hold(power.PERFORMANCE, "Racing Game"), power.Hold(power.PERFORMANCE))
    elif word == "pwdegraded":
        options["degraded"] = (power.HEAT,)
    elif word == "pwmismatch":
        options["evidence"] = power.Evidence("quiet", "off")
    return FakePower(on_change, active=active, answers="later", reentrant=True, defer=later, **options)


def play_power(word, window, source):
    """Do in the open sheet what the scenario shows."""
    if word in CHOOSING:
        press(window, pw.option_name(power.PERFORMANCE))
        check(source.snapshot.pending == power.PERFORMANCE, "pressing A on Performance asked for nothing")
    if word == "pwfail":
        source.fail(power.DENIED)
    elif word in ("pwelsewhere", "pwnotremembered"):
        source.finish()
        settle()
        if word == "pwelsewhere":
            source.change(active=power.SAVER)
        else:
            source.not_remembered()
    settle()


def keys(window):
    return [block.key for block in window._power.blocks(window.runtime.t, window._battery)]


def named(widget, prefix):
    """Whether this widget or one under it has a name that starts with ``prefix``."""
    if (widget.get_name() or "").startswith(prefix):
        return True
    child = widget.get_first_child()
    while child is not None:
        if named(child, prefix):
            return True
        child = child.get_next_sibling()
    return False


def check_power_shown(word, window, source):
    """What the sheet must look like before its picture is taken."""
    t = window.runtime.t
    check(window.sheet == "power", f"the power sheet is not open: {window.sheet!r}")
    focus = window.get_focus()
    name = focus.get_name() if focus is not None else None
    check(window._pw_buttons.get(name) is focus, f"the focus is outside the sheet: {name!r}")
    wanted = pw.RETRY if word == "pwerror" else pw.option_name(
        {"pwsaver": power.SAVER, "pwbalanced": power.BALANCED}.get(word, power.PERFORMANCE))
    check(name == wanted, f"the focus is on {name}, not on {wanted}")
    told = window.runtime.status()["selected"]
    check(told == name and told in pw.NAMES, f"the status command names the focus {told!r}")
    shown = keys(window)
    if source.snapshot.usable:
        current = window._pw_buttons[pw.option_name(source.snapshot.active)]
        check(current.has_css_class("current") and sum(button.has_css_class("current") for button in
                                                       window._pw_buttons.values()) == 1,
              "the active profile is not the one marked")
        has_battery = window._battery is not None
        check(("sources" in shown) == has_battery,
              f"the line about plugged in and battery is {'missing' if has_battery else 'there'} on a machine "
              f"{'with' if has_battery else 'without'} a battery")
    if word == "pwpending":
        asked = window._pw_buttons[wanted]
        others = [button for key, button in window._pw_buttons.items() if pw.option_profile(key) and key != wanted]
        check(asked.has_css_class("pending") and not asked.has_css_class("dimmed")
              and t("power.switching") in texts_in(asked), "the profile asked for does not say that it is switching")
        check(all(button.has_css_class("dimmed") and button.get_sensitive() for button in others),
              "the other profiles are not dimmed, or not sensitive, while one is asked for")
    expected = {"pwfail": "failure", "pwelsewhere": "note", "pwnotremembered": "note", "pwheld": "holds",
                "pwdegraded": "degraded", "pwmismatch": "evidence", "pwerror": "retry"}.get(word)
    if expected:
        check(expected in shown, f"the sheet does not show {expected}: {shown}")
    if word in ("pwfail", "pwelsewhere", "pwnotremembered"):
        check(pw.DISMISS in window._pw_buttons, "what happened to the choice has no Dismiss")
    if word == "pwheld":
        check(any("Racing Game" in text for text in texts_in(window._pw_content)), "the program asking is not named")
    if word == "pwerror":
        check(window.power_button.has_css_class("warn"), "the power button does not warn that it could not read")


def check_header(language, window, width):
    """The header: the power button between Bluetooth and Settings (mirrored in Arabic), every chip and button
    inside the window, and no chip's text cut short."""
    settle()
    check(window.get_width() == width, f"the header made the window {window.get_width()} pixels wide, not {width}")
    shown = [button for button in (window.bluetooth_button, window.power_button, window.settings_button)
             if button.get_visible()]
    places = [middle(window, button) for button in shown]
    check(places == sorted(places, reverse=language == "ar"), f"the header's buttons are out of order in {language}")
    for widget in shown + [window.controller_chip, window.mode_chip, window.battery_chip, window.clock_label]:
        if widget.get_visible():
            ok, bounds = widget.compute_bounds(window)
            check(ok and bounds.get_x() >= 0 and bounds.get_x() + bounds.get_width() <= width,
                  f"{widget.get_name() or type(widget).__name__} does not fit in the header in {language}")
    for text in (window.controller_label, window.mode_label, window.battery_label, window.clock_label):
        if text.get_mapped():
            check(not text.get_layout().is_ellipsized(), f"{text.get_text()!r} is cut short in {language}")


def check_power_layout(language, window):
    """An option reads glyph, words, mark from the start of the line: mirrored in Arabic."""
    settle()
    option = next((window._pw_buttons[pw.option_name(profile)] for profile in power.PROFILES
                   if pw.option_name(profile) in window._pw_buttons), None)
    if option is None:
        return
    row = option.get_child()
    glyph, mark = row.get_first_child(), row.get_last_child()
    texts = glyph.get_next_sibling()
    mirrored = middle(window, glyph) > middle(window, texts) > middle(window, mark)
    check(mirrored == (language == "ar"), f"a profile is {'not ' if language == 'ar' else ''}mirrored in {language}")


def check_power_behaviour(word, language, window, source):
    """After the picture: what pressing things does. Each scenario leaves the sheet open for the next check."""
    check_header(language, window, window.get_size_request()[0])
    if word == "pwnone":
        check(not window.power_button.get_visible() and window.power_button not in window.nav_rows()[0],
              "the header offers a power profile with only one to choose")
        source.change(available=power.PROFILES)
        settle()
        check(window.power_button.get_visible() and window.power_button in window.nav_rows()[0],
              "a second profile did not bring the power button")
        return
    check_power_layout(language, window)
    option = pw.option_name
    if word == "pwpending":
        asked = window._pw_buttons[option(power.PERFORMANCE)]
        window.handle_action(core.UP)                               # the D-pad still moves over dimmed profiles
        check(window.get_focus() is window._pw_buttons[option(power.BALANCED)], "the D-pad did not move up")
        window.handle_action(core.DOWN)
        check(window.get_focus() is asked, "the D-pad did not come back to the profile asked for")
        source.finish()
        settle()
        check(window.get_focus() is window._pw_buttons[option(power.PERFORMANCE)] and asked.get_root() is window,
              f"the focus left the profile asked for when the answer came: {window.selected_name()}")
        check(asked.has_css_class("current") and not any(button.has_css_class("dimmed")
                                                         for button in window._pw_buttons.values()),
              "the profile asked for is not the active one after the answer, or something stays dimmed")
    elif word == "pwfail":
        window.handle_action(core.BACK)
        window.open_power()
        settle()
        check(pw.DISMISS not in window._pw_buttons and source.snapshot.failure is None,
              "the failure is still there at the next visit")
        press(window, option(power.PERFORMANCE))                    # asked again, and the sheet closes first
        window.handle_action(core.BACK)
        source.fail(power.TIMEOUT)
        settle()
        window.open_power()
        settle()
        check(pw.DISMISS not in window._pw_buttons and source.snapshot.failure is None,
              "a failure that came after the sheet closed greets the next visit")
    elif word == "pwerror":
        source.change(state=power.READY)
        press(window, pw.RETRY)
        settle()
        check(source.snapshot.usable and window.selected_name() == option(source.snapshot.active),
              f"Try again did not read the service, or the focus is on {window.selected_name()}")
        check(not window.power_button.has_css_class("warn"), "the power button still warns after a good reading")
    elif word == "pw":
        check(window._focus_by_name(option(power.SAVER)), "power-saver cannot be selected")
        source.change(available=(power.BALANCED, power.PERFORMANCE))     # the service restarted without it
        settle()
        focus = window.get_focus()
        check(focus is window._pw_buttons.get(option(power.PERFORMANCE)) and focus.get_root() is window,
              f"the focus stayed on a profile that went away, or went to {window.selected_name()}")
        source.change(available=power.PROFILES)
        settle()
    check_power_exits(window, source)
    check_power_late_clicks(window, source)


def check_power_exits(window, source):
    """Each way out of the sheet leaves it, and takes its widgets and words out of the window."""
    t = window.runtime.t

    def reopen():
        if window.sheet is not None:
            window.close_sheet()
        window.open_power()
        settle()
        check(window.sheet == "power" and window._power.visiting and window._pw_buttons, "the power sheet did not open")

    def left(way):
        check(window.sheet != "power" and not window._power.visiting, f"{way} left the power sheet open")
        check(not window._pw_buttons and window._pw_content is None and not named(window, pw.OPTION),
              f"{way} left the power sheet's controls in the window")
        check(t("power.intro") not in texts_in(window), f"{way} left the power sheet's words in a hidden label")

    ways = {"Menu": lambda: window.handle_action(core.MENU), "B": lambda: window.handle_action(core.BACK),
            "Escape": lambda: window._on_key(None, Gdk.KEY_Escape, 0, Gdk.ModifierType(0)),
            "Close": lambda: press(window, pw.CLOSE), "Settings": window.open_settings,
            "Bluetooth": window.open_bluetooth,
            "a new language": lambda: (setattr(window, "sheet", None), window.build())}
    for way, leave in ways.items():
        reopen()
        leave()
        left(way)
    reopen()
    window.set_visible(False)
    left("hiding the window")
    check(window.sheet is None and not source.running and source.calls[-1] == ("stop",),
          "hiding the window did not close the sheet and stop the power profile")
    window.set_visible(True)
    settle()
    check(source.running, "showing the window again did not start the power profile")
    if window.sheet is not None:
        window.close_sheet()


def check_power_late_clicks(window, source):
    """GTK clicks a button 250 ms after A or Enter. A click on its way when the sheet goes must choose nothing: A then
    B, A then Menu, Enter then Escape, each on a profile that is not the active one. Then A alone, which chooses."""
    def target():
        snapshot = source.snapshot
        return next(profile for profile in snapshot.available if profile != snapshot.active)

    def attempt(way, press_it, close):
        window.open_power()
        settle()
        chosen, active = target(), source.snapshot.active
        check(window._focus_by_name(pw.option_name(chosen)), f"{chosen} cannot be selected")
        before = len(source.calls)
        press_it()
        close()
        check(window.sheet is None, f"{way} did not close the sheet")
        settle(400)
        late = [call for call in source.calls[before:] if call[0] == "set_profile"]
        check(not late and source.snapshot.active == active and not source.snapshot.pending,
              f"{late or chosen} reached the power profile after {way} had closed the sheet")

    def escape():
        window._on_key(None, Gdk.KEY_Escape, 0, Gdk.ModifierType(0))

    attempt("A then B", lambda: window.handle_action(core.ACTIVATE), lambda: window.handle_action(core.BACK))
    attempt("A then Menu", lambda: window.handle_action(core.ACTIVATE), lambda: window.handle_action(core.MENU))
    attempt("Enter then Escape", lambda: window.get_focus().activate(), escape)
    window.open_power()
    settle()
    chosen = target()
    press(window, pw.option_name(chosen))
    check(source.calls[-1] == ("set_profile", chosen) and source.snapshot.pending == chosen,
          "A on another profile did not ask for it")
    source.finish()
    settle()
    window.close_sheet()


class FakeRuntime(application.Runtime):
    def __init__(self, app, language, inputplumber=False, battery="", bluetooth="bt", power_word="pw"):
        self.app = app
        self.battery = BATTERIES[battery]
        self.bluetooth_word = bluetooth
        self.bluetooth_source = None
        self.power_word = power_word
        self.power_source = None
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

    def power(self, on_change):
        self.power_source = power_world(self.power_word, on_change)
        return self.power_source


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    language = sys.argv[2] if len(sys.argv) > 2 else "en"
    width, height = (int(v) for v in (sys.argv[3] if len(sys.argv) > 3 else "1280x720").split("x"))
    words = sys.argv[4:]
    inputplumber = "inputplumber" in words
    battery = next((word for word in words if word in BATTERIES), "")
    bluetooth = next((word for word in words if word in BLUETOOTH), "")
    power_word = "" if bluetooth else next((word for word in words if word in POWER), "")
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
        runtime = FakeRuntime(app, language, inputplumber, battery, bluetooth or "bt", power_word or "pw")
        runtime._apply_direction()
        window = HomeWindow(app, runtime)
        runtime.window = window
        window.set_default_size(width, height)
        # Broadway's screen is 1024x768 until a browser says otherwise, and a default size gives way to the
        # screen; a size request does not, so the picture has the size asked for.
        window.set_size_request(width, height)
        window.present()
        source, power_source = runtime.bluetooth_source, runtime.power_source
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
        elif power_word == "pwnone":
            steps = [("home", lambda: window._focus_default())]
        elif power_word:
            steps = [("home", lambda: window._focus_default()),
                     ("power", lambda: (window.open_power(), play_power(power_word, window, power_source),
                                        check_power_shown(power_word, window, power_source)))]
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
                    elif power_word:
                        check_power_behaviour(power_word, language, window, power_source)
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

    def start(app):
        """A window that cannot be shown (no display, a broken step before the first picture) fails the run too."""
        try:
            activate(app)
        except Exception:
            import traceback
            failures.append(traceback.format_exc())
            app.quit()

    app.connect("activate", start)
    app.run([])
    if failures:
        print(failures[0], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
