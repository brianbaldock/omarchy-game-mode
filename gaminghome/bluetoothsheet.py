"""What the Bluetooth button and sheet of Gaming Home show, and what their controls do, apart from GTK.

``BluetoothSheet`` holds the window's Bluetooth source and what one visit to the sheet remembers: whether Search
was pressed, which device asks to be forgotten, the digits typed on the pad. Every way out of the sheet ends in
``leave``, which stops the search and abandons a pairing, so neither outlives the sheet. ``blocks`` describes the
sheet from top to bottom as plain data; each block has a stable key and each control a stable name that carries
the device's id, so the window can update what it shows in place and keep the focus where it was.

Nothing here imports GTK or GLib or owns a timer. Digits typed for a PIN or a passkey stay only as long as the
prompt that asked for them, and they appear in no ``repr``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import bluetooth as bt

# The names of the controls. A device's controls end with its id, never with a position in a list.
POWER, SEARCH, RETRY, DISMISS, CLOSE = "bt:power", "bt:search", "bt:retry", "bt:dismiss", "bt:close"
CANCEL, YES, NO = "bt:cancel", "bt:yes", "bt:no"
CONNECT, FORGET, FORGET_YES, FORGET_NO, PAIR = (
    "bt:connect:", "bt:forget:", "bt:forget-yes:", "bt:forget-no:", "bt:pair:")
PAD = "bt:pad:"
PAD_ERASE, PAD_DONE = PAD + "erase", PAD + "done"
PAD_KEYS = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("erase", "0", "done"))

# Where the focus goes when its control is gone: never to a control that forgets or pairs a device.
SAFE_FOCUS = (SEARCH, CLOSE)
DESTRUCTIVE = (FORGET, FORGET_YES, PAIR)
# Where the focus starts when the sheet opens. None of them changes a connection: the power switch is first only
# while Bluetooth is off, because while it is on, Search is there before it.
OPENING_FOCUS = (SEARCH, RETRY, POWER, CLOSE)

KIND_ICONS = {"gamepad": "controller", "headphones": "headphones", "headset": "headset", "speaker": "speaker",
              "keyboard": "keyboard", "mouse": "mouse", "phone": "phone", "computer": "desktop"}
ACTIONS = (bt.CONNECTING, bt.DISCONNECTING, bt.PAIRING, bt.FORGETTING, bt.POWERING, bt.SEARCHING)


def device_icon(kind: str) -> str:
    return KIND_ICONS.get(kind, "bluetooth")


def device_id(name: str, prefix: str) -> int | None:
    """The device a control of this name acts on, or None when the name is not one of that kind."""
    if not name.startswith(prefix):
        return None
    try:
        return int(name[len(prefix):])
    except ValueError:
        return None


def device_labels(snapshot: bt.Snapshot, unnamed: str) -> dict[int, str]:
    """What to call each device shown: the remembered ones keep plain names before nearby ones get a number."""
    return bt.labels(bt.remembered(snapshot.devices) + bt.nearby(snapshot.devices), unnamed)


def pairing(snapshot: bt.Snapshot) -> bool:
    """A pairing is under way or asks something: the sheet shows the panel for it."""
    activity = snapshot.activity
    return snapshot.prompt is not None or (activity is not None and activity.kind == bt.PAIRING)


class DigitEntry:
    """The digits typed on the pad for one PIN or passkey prompt: a string, so leading zeros stay."""

    def __init__(self):
        self.prompt = None       # the id of the prompt these digits answer
        self.kind = ""
        self.limit = 0
        self.digits = ""

    def __repr__(self):
        return f"DigitEntry(prompt={self.prompt}, typed={len(self.digits)}, limit={self.limit})"

    @staticmethod
    def limit_for(prompt: bt.Prompt) -> int:
        """At most what the prompt allows, and never more than a PIN or a passkey can have."""
        most = bt.PASSKEY_MAX if prompt.kind == bt.PASSKEY else bt.PIN_MAX
        return min(prompt.max_length, most) if prompt.max_length > 0 else most

    def begin(self, prompt: bt.Prompt):
        self.prompt, self.kind, self.limit, self.digits = prompt.id, prompt.kind, self.limit_for(prompt), ""

    def clear(self):
        self.prompt, self.kind, self.limit, self.digits = None, "", 0, ""

    @property
    def shown(self) -> str:
        """The display above the pad: the digits so far and, for a passkey, a blank for each of its six still to
        come. A PIN has no fixed length, so it gets no blanks."""
        if self.kind == bt.PASSKEY:
            return self.digits + "_" * (self.limit - len(self.digits))
        return self.digits

    @property
    def ready(self) -> bool:
        """Done can be pressed: there is at least one digit."""
        return bool(self.digits)

    def press(self, digit: str) -> bool:
        if len(digit) != 1 or digit not in "0123456789" or len(self.digits) >= self.limit:
            return False
        self.digits += digit
        return True

    def erase(self) -> bool:
        if not self.digits:
            return False
        self.digits = self.digits[:-1]
        return True


@dataclass(frozen=True)
class FailureMessage:
    """The keys of a failure's words: what failed, why, and what to do next when there is something to say."""
    action: str
    reason: str
    hint: str = ""
    name: str = ""

    def text(self, t) -> str:
        return t("bluetooth.failure", action=t(self.action, name=self.name), reason=t(self.reason))


def failure_message(failure: bt.Failure, name: str) -> FailureMessage:
    """Words for a failure from the contract's codes alone: nothing that BlueZ said is shown, because none of it is
    in the model. ``name`` is what to call the device."""
    reason = f"bluetooth.reason.{failure.code}" if failure.code in bt.FAILURES else "bluetooth.reason.failed"
    if failure.paired:
        return FailureMessage("bluetooth.failed.paired", reason, "bluetooth.failed.pairedHint", name)
    action = f"bluetooth.failed.{failure.action}" if failure.action in ACTIONS else "bluetooth.failed.other"
    return FailureMessage(action, reason, "", name)


def choose_focus(wanted: str | None, controls, first: str | None = None) -> str | None:
    """The control to focus after the sheet changed, or None to leave the focus as it is.

    ``controls`` are the sheet's controls in order, as ``(name, enabled)``. ``first`` is where a part that has just
    appeared wants the focus to start (No for a number to compare, Cancel for a question). Otherwise the focus stays
    on the control of the same name; when that one is only disabled while something runs, it keeps its claim (None)
    and gets the focus back once it is enabled again. When it is gone, the focus goes to Search or Close, or else to
    the first control that neither forgets nor pairs a device.
    """
    enabled = {name: on for name, on in controls}
    if first and enabled.get(first):
        return first
    if wanted in enabled:
        return wanted if enabled[wanted] else None
    for name in SAFE_FOCUS:
        if enabled.get(name):
            return name
    return next((name for name, on in controls if on and not name.startswith(DESTRUCTIVE)), None)


def opening_focus(controls) -> str | None:
    """Where the focus starts when the sheet opens: on a control that does not change a connection."""
    enabled = {name: on for name, on in controls}
    return next((name for name in OPENING_FOCUS if enabled.get(name)), None)


# ---- what the window shows ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ButtonView:
    """The header button: whether it exists, its icon (``gh-<icon>-symbolic``), its tone and its tooltip."""
    visible: bool
    icon: str = "bluetooth"
    tone: str = ""               # "", "off", "connected" or "warn"
    tooltip: str = ""


def button_view(snapshot: bt.Snapshot, t) -> ButtonView:
    if not snapshot.visible:
        return ButtonView(False)
    if snapshot.state == bt.ERROR:
        return ButtonView(True, "bluetooth-error", "warn", t("bluetooth.tooltipError"))
    if snapshot.state == bt.OFF:
        return ButtonView(True, "bluetooth-off", "off", t("bluetooth.tooltipOff"))
    linked = bt.connected(snapshot.devices)
    if not linked:
        return ButtonView(True, "bluetooth", "", t("bluetooth.tooltipOn"))
    names = device_labels(snapshot, t("bluetooth.unnamed"))
    return ButtonView(True, "bluetooth-connected", "connected",
                      "\n".join([t("bluetooth.tooltipConnected")] + [names[d.id] for d in linked]))


@dataclass(frozen=True)
class Control:
    name: str
    text: str = ""
    enabled: bool = True
    style: str = ""              # "primary", "danger" or ""
    icon: str = ""               # shown instead of the text, which is then the tooltip


@dataclass(frozen=True)
class Block:
    """One part of the sheet. ``key`` stays the same while the part shows the same thing; ``shape`` changes only
    when its controls do, so the window rebuilds a part only then and otherwise updates it in place."""
    key: str
    controls: tuple[Control, ...] = ()
    first: str = ""              # where the focus starts when this part appears

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        """The controls as rows of the D-pad's navigation."""
        return (tuple(c.name for c in self.controls),) if self.controls else ()

    @property
    def shape(self) -> tuple:
        return (type(self).__name__,) + tuple(c.name for c in self.controls)


@dataclass(frozen=True)
class Text(Block):
    text: str = ""
    tone: str = "body"           # "title", "section", "body" or "muted"
    status: str = ""             # a section's static state line, at the other end of its heading

    @property
    def shape(self) -> tuple:
        return (type(self).__name__, self.tone)


@dataclass(frozen=True)
class Problem(Block):
    """The failure line and its Dismiss."""
    text: str = ""
    hint: str = ""


@dataclass(frozen=True)
class Power(Block):
    title: str = ""
    hint: str = ""
    status: str = ""
    on: bool = False


@dataclass(frozen=True)
class Row(Block):
    """A device: remembered (Connect or Disconnect, Forget) or nearby (Pair)."""
    icon: str = "bluetooth"
    title: str = ""
    status: str = ""
    connected: bool = False


@dataclass(frozen=True)
class Question(Block):
    """A sentence with its answers: forgetting a device, a number to compare or to type, a pairing under way."""
    text: str = ""
    icon: str = ""               # the device's icon, for a question that stands in a device's place
    code: str = ""               # digits shown large, always left to right
    typed: int = -1              # how many of them the device has received, -1 when that is not the question
    danger: bool = False

    @property
    def shape(self) -> tuple:
        return super().shape + (len(self.code), self.typed >= 0, bool(self.icon), self.danger)


@dataclass(frozen=True)
class Pad(Block):
    """The digit pad of a PIN or passkey prompt. Its controls are the twelve keys and then Cancel."""
    text: str = ""
    display: str = field(default="", repr=False)

    @property
    def rows(self) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(PAD + key for key in row) for row in PAD_KEYS) + ((CANCEL,),)


@dataclass(frozen=True)
class Actions(Block):
    text: str = ""               # what the buttons are about, before them on the same row
    end: bool = False            # the buttons sit at the end of the row, as Close does in every sheet


# ---- the sheet ----------------------------------------------------------------------------------------------------
class BluetoothSheet:
    """The window's Bluetooth source and what a visit to the sheet remembers.

    The window calls ``start`` when it is mapped and ``stop`` when it is unmapped, destroyed or the app quits;
    ``enter`` when the sheet opens and ``leave`` on every way it goes away; ``sync`` whenever the source reports a
    change; ``press`` with a control's name when it is pressed. A control acts on the snapshot as it is when it is
    pressed, and on the device its name carries.
    """

    def __init__(self, source: bt.Bluetooth):
        self.source = source
        self.visiting = False
        self.searched = False        # Search was pressed during this visit: the nearby list stays until it ends
        self.forgetting = 0          # the device whose Forget asks for confirmation
        self.entry = DigitEntry()
        self._asked_search = False   # Search was pressed and the source has not said yet that it searches
        self._power_target = None    # what the power switch was last asked for, while that runs

    @property
    def snapshot(self) -> bt.Snapshot:
        return self.source.snapshot

    @property
    def searching(self) -> bool:
        return self.snapshot.scanning or self._asked_search

    @property
    def typing(self) -> bool:
        """The pad is shown, so the keyboard's digits, Backspace and Enter are the pad's."""
        prompt = self.snapshot.prompt
        return (self.visiting and prompt is not None and prompt.kind in (bt.PIN, bt.PASSKEY)
                and prompt.id == self.entry.prompt)

    # -- lifecycle
    def start(self):
        self.source.start()

    def stop(self):
        self.leave()
        self.source.stop()

    def enter(self):
        if self.visiting:
            return
        self.visiting = True
        self.searched = self._asked_search = False
        self.forgetting = 0
        self.entry.clear()
        self.sync()

    def leave(self) -> bool:
        """The sheet goes away, whichever way: end the search and abandon a pairing. False when it was not open."""
        if not self.visiting:
            return False
        self.visiting = False
        self.searched = self._asked_search = False
        self.forgetting = 0
        self._power_target = None
        self.entry.clear()
        busy_pairing = pairing(self.source.snapshot)
        self.source.scan(False)
        if busy_pairing:
            self.source.cancel()
        return True

    def sync(self):
        """The source changed: forget what no longer applies, and the digits of a prompt that has ended."""
        snapshot = self.source.snapshot
        prompt = snapshot.prompt
        if prompt is None or prompt.kind not in (bt.PIN, bt.PASSKEY) or not self.visiting:
            self.entry.clear()
        elif prompt.id != self.entry.prompt:
            self.entry.begin(prompt)
        if self.forgetting and not any(d.id == self.forgetting for d in bt.remembered(snapshot.devices)):
            self.forgetting = 0
        failure = snapshot.failure
        if snapshot.scanning or (failure is not None and failure.action == bt.SEARCHING):
            self._asked_search = False
        if snapshot.activity is None or snapshot.activity.kind != bt.POWERING:
            self._power_target = None

    # -- controls
    def press(self, name: str) -> str | None:
        """Do what the control of this name does. Returns the control to focus next when that is another one.
        Nothing happens once the sheet has left: GTK may still deliver a click it was about to make."""
        if not self.visiting:
            return None
        snapshot = self.source.snapshot
        idle = snapshot.activity is None
        prompt = snapshot.prompt
        if name == POWER:
            if snapshot.usable and idle:
                self._power_target = snapshot.state != bt.ON
                self.source.set_power(self._power_target)
        elif name == SEARCH:
            if self.searching:
                self._asked_search = False
                self.source.scan(False)
            elif snapshot.state == bt.ON:
                self.searched = self._asked_search = True
                self.source.scan(True)
        elif name == RETRY:
            self.source.retry()
        elif name == DISMISS:
            self.source.dismiss()
        elif name in (YES, NO):
            if prompt is not None and prompt.kind == bt.CONFIRM:
                self.source.answer(prompt.id, name == YES)
        elif name == CANCEL:
            self.entry.clear()
            self.source.cancel()
        elif name.startswith(PAD):
            return self._pad(name[len(PAD):])
        elif (device := device_id(name, CONNECT)) is not None:
            if idle:
                known = next((d for d in snapshot.devices if d.id == device), None)
                (self.source.disconnect if known is not None and known.connected else self.source.connect)(device)
        elif (device := device_id(name, FORGET)) is not None:
            if idle:
                self.forgetting = device
        elif (device := device_id(name, FORGET_YES)) is not None:
            self.forgetting = 0
            if idle:
                self.source.forget(device)
        elif (device := device_id(name, FORGET_NO)) is not None:
            self.forgetting = 0
            return f"{FORGET}{device}"
        elif (device := device_id(name, PAIR)) is not None:
            if idle:
                self.source.pair(device)
        return None

    def back(self) -> tuple[bool, str | None]:
        """B, or Escape: close a question first and give up a pairing next. (False, None) lets the sheet close."""
        if self.forgetting:
            return True, self.press(f"{FORGET_NO}{self.forgetting}")
        if pairing(self.source.snapshot):
            self.press(CANCEL)
            return True, None
        return False, None

    def key(self, key: str) -> str | None:
        """A digit or "erase" from a keyboard while the pad is shown. The focus moves to Done after a digit, so
        that Enter answers; to Erase when nothing is left, because Done is then disabled."""
        if not self.typing:
            return None
        if key == "erase":
            return self._pad("erase")
        return PAD_DONE if self.entry.press(key) else None

    def _pad(self, key: str) -> str | None:
        prompt = self.source.snapshot.prompt
        if prompt is None or prompt.id != self.entry.prompt:
            return None
        if key == "erase":
            self.entry.erase()
            return None if self.entry.ready else PAD_ERASE
        if key == "done":
            if self.entry.ready:
                digits = self.entry.digits
                self.entry.clear()
                self.source.answer(prompt.id, digits)
            return None
        self.entry.press(key)
        return None

    # -- what the sheet shows
    def power_status(self, t) -> str:
        snapshot = self.snapshot
        activity = snapshot.activity
        if activity is not None and activity.kind == bt.POWERING:
            turning_on = self._power_target if self._power_target is not None else snapshot.state != bt.ON
            return t("bluetooth.turningOn" if turning_on else "bluetooth.turningOff")
        return t("settings.on" if snapshot.state == bt.ON else "settings.off")

    def blocks(self, t) -> list[Block]:
        """The sheet from top to bottom."""
        snapshot = self.snapshot
        names = device_labels(snapshot, t("bluetooth.unnamed"))

        def called(device: int, name: str) -> str:
            return names.get(device) or name or t("bluetooth.unnamed")

        blocks: list[Block] = [Text("title", text=t("bluetooth.title"), tone="title")]
        failure = snapshot.failure
        if failure is not None:
            message = failure_message(failure, called(failure.device, failure.name))
            blocks.append(Problem("failure", (Control(DISMISS, t("bluetooth.dismiss")),), text=message.text(t),
                                  hint=t(message.hint) if message.hint else ""))
        if not snapshot.visible:
            blocks.append(Text("unavailable", text=t("bluetooth.unavailable")))
        elif snapshot.state == bt.ERROR:
            blocks.append(Actions("retry", (Control(RETRY, t("bluetooth.tryAgain"), style="primary"),),
                                  text=t("bluetooth.readFailed")))
        else:
            idle = snapshot.activity is None
            blocks.append(Power("power", (Control(POWER, enabled=idle),), title=t("bluetooth.title"),
                                hint=t("bluetooth.powerHint"), status=self.power_status(t),
                                on=snapshot.state == bt.ON))
            if pairing(snapshot):
                blocks.extend(self._panel(snapshot, called, t))
            elif snapshot.state == bt.OFF:
                blocks.append(Text("off", text=t("bluetooth.offBody")))
            else:
                blocks.extend(self._devices(snapshot, names, idle, t))
        blocks.append(Actions("close", (Control(CLOSE, t("button.close")),), end=True))
        return blocks

    def _devices(self, snapshot, names, idle, t) -> list[Block]:
        activity = snapshot.activity
        blocks: list[Block] = [Text("mine", text=t("bluetooth.myDevices").upper(), tone="section")]
        mine = bt.remembered(snapshot.devices)
        for device in mine:
            key, icon = f"device:{device.id}", device_icon(device.kind)
            if device.id == self.forgetting:
                blocks.append(Question(key, (
                    Control(f"{FORGET_YES}{device.id}", t("bluetooth.forget"), idle, "danger"),
                    Control(f"{FORGET_NO}{device.id}", t("bluetooth.cancel"))),
                    first=f"{FORGET_NO}{device.id}", text=t("bluetooth.forgetQuestion", name=names[device.id]),
                    icon=icon, danger=True))
                continue
            if activity is not None and activity.device == device.id and activity.kind in (
                    bt.CONNECTING, bt.DISCONNECTING, bt.FORGETTING):
                status = t(f"bluetooth.{activity.kind}")
            elif device.connected and device.battery is not None:
                status = t("bluetooth.connectedBattery", percent=device.battery)
            else:
                status = t("bluetooth.connected" if device.connected else "bluetooth.notConnected")
            blocks.append(Row(key, (
                Control(f"{CONNECT}{device.id}", t("bluetooth.disconnect" if device.connected else "bluetooth.connect"),
                        idle),
                Control(f"{FORGET}{device.id}", t("bluetooth.forget"), idle)),
                icon=icon, title=names[device.id], status=status, connected=device.connected))
        if not mine:
            blocks.append(Text("mine-empty", text=t("bluetooth.noDevices"), tone="muted"))
        searching = self.searching
        blocks.append(Text("add", text=t("bluetooth.addDevice").upper(), tone="section",
                           status=t("bluetooth.searching") if searching else ""))
        blocks.append(Actions("search", (Control(SEARCH, t("bluetooth.stopSearch" if searching else "bluetooth.search"),
                                                 style="" if searching else "primary"),),
                              text=t("bluetooth.addHint")))
        if self.searched:
            found = bt.nearby(snapshot.devices)
            for device in found:
                blocks.append(Row(f"nearby:{device.id}", (Control(f"{PAIR}{device.id}", t("bluetooth.pair"), idle),),
                                  icon=device_icon(device.kind), title=names[device.id]))
            if not found:
                blocks.append(Text("nearby-empty", tone="muted",
                                   text=t("bluetooth.looking" if searching else "bluetooth.nothingFound")))
        if snapshot.truncated:
            blocks.append(Text("truncated", text=t("bluetooth.truncated"), tone="muted"))
        return blocks

    def _panel(self, snapshot, called, t) -> list[Block]:
        prompt = snapshot.prompt
        cancel = Control(CANCEL, t("bluetooth.cancel"))
        if prompt is not None:
            name = called(prompt.device, prompt.name)
            if prompt.kind == bt.CONFIRM:
                return [Question(f"prompt:{prompt.id}", (Control(YES, t("bluetooth.matches"), style="primary"),
                                                         Control(NO, t("bluetooth.no"))),
                                 first=NO, text=t("bluetooth.confirm", name=name), code=prompt.code)]
            if prompt.kind == bt.CODE:
                return [Question(f"prompt:{prompt.id}", (cancel,), first=CANCEL,
                                 text=t("bluetooth.typeCode", name=name, code=prompt.code), code=prompt.code,
                                 typed=max(0, min(prompt.typed, len(prompt.code))))]
            if prompt.kind in (bt.PIN, bt.PASSKEY) and prompt.id == self.entry.prompt:
                return [Pad(f"prompt:{prompt.id}", self._pad_controls(t) + (cancel,), first=f"{PAD}1",
                            text=t("bluetooth.enterPin" if prompt.kind == bt.PIN else "bluetooth.enterPasskey",
                                   name=name),
                            display=self.entry.shown)]
        activity = snapshot.activity
        if activity is not None and activity.kind == bt.PAIRING:
            name = called(activity.device, activity.name)
            words = "bluetooth.connectingTo" if activity.stage in (bt.STAGE_TRUST, bt.STAGE_CONNECT) else \
                "bluetooth.pairingWith"
        else:
            name = called(prompt.device, prompt.name) if prompt is not None else t("bluetooth.unnamed")
            words = "bluetooth.pairingWith"
        return [Question("pairing", (cancel,), first=CANCEL, text=t(words, name=name))]

    def _pad_controls(self, t) -> tuple[Control, ...]:
        keys = []
        for row in PAD_KEYS:
            for key in row:
                if key == "erase":
                    keys.append(Control(PAD_ERASE, t("bluetooth.erase"), icon="backspace"))
                elif key == "done":
                    keys.append(Control(PAD_DONE, t("bluetooth.done"), self.entry.ready, "primary", icon="check"))
                else:
                    keys.append(Control(PAD + key, key))
        return tuple(keys)


def control_states(blocks) -> list[tuple[str, bool]]:
    """Every control of these blocks in order, as ``(name, enabled)``."""
    return [(control.name, control.enabled) for block in blocks for control in block.controls]
