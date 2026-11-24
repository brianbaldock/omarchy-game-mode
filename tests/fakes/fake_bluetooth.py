"""A Bluetooth source that a test scripts by hand, keeping to the rules of ``gaminghome.bluetooth``.

It stands where the BlueZ client stands in the app and touches nothing of the machine: no bus, no timer, no thread.
The test says what Bluetooth looks like (``set_state``, ``add_device``, ``show_prompt``) and how an operation ends
(``finish``, ``fail``). The rules of the contract hold by themselves: one activity at a time, during which
``set_power``, ``connect``, ``disconnect``, ``pair`` and ``forget`` are ignored while ``scan(False)``, ``cancel`` and
``answer`` still work; a new operation clears the failure; an answer to a prompt that is no longer asked is ignored;
a pairing ends the search; at most ``MAX_DEVICES`` devices are listed, remembered ones first; ``start`` and
``stop`` are idempotent, and ``on_change`` is called only between them, right after the snapshot changed. Every
call, ignored or not, is recorded in ``calls``.
"""

from __future__ import annotations

from dataclasses import replace

from gaminghome import bluetooth as bt


class FakeBluetooth:
    def __init__(self, on_change=lambda: None, state=bt.ON):
        self.on_change = on_change
        self.calls: list[tuple] = []
        self.running = False
        self.changes = 0                     # how often on_change was called
        self._devices: dict[int, bt.Device] = {}
        self._snapshot = bt.Snapshot(state)
        self._last_id = self._last_prompt = self._last_order = 0
        self._power = None                   # what set_power asked for, while it runs

    @property
    def snapshot(self) -> bt.Snapshot:
        return self._snapshot

    # ---- the protocol --------------------------------------------------------------------------------------------
    def start(self):
        self.calls.append(("start",))
        if not self.running:
            self.running = True
            self._notify()                   # as a source does once it has read Bluetooth

    def stop(self):
        """Everything is let go: the search, the pairing and every call in flight."""
        self.calls.append(("stop",))
        if self.running:
            self.running = False
            self._snapshot = replace(self._snapshot, scanning=False, prompt=None, activity=None)

    def retry(self):
        self.calls.append(("retry",))
        self._update(failure=None)

    def set_power(self, on):
        self.calls.append(("set_power", on))
        if not self._busy():
            self._power = on
            self._update(activity=bt.Activity(bt.POWERING), failure=None)

    def scan(self, on):
        self.calls.append(("scan", on))
        if not on:
            if self._snapshot.scanning:
                self._update(scanning=False)
        elif self._snapshot.state != bt.ON:
            self._update(failure=bt.Failure(bt.NOT_READY, bt.SEARCHING))
        else:
            self._update(scanning=True, failure=None)

    def connect(self, device):
        self.calls.append(("connect", device))
        self._begin(bt.CONNECTING, device)

    def disconnect(self, device):
        self.calls.append(("disconnect", device))
        self._begin(bt.DISCONNECTING, device)

    def pair(self, device):
        self.calls.append(("pair", device))
        if self._begin(bt.PAIRING, device):
            self._update(scanning=False)

    def forget(self, device):
        self.calls.append(("forget", device))
        if self._snapshot.state != bt.ON and not self._busy():
            known = self._devices.get(device)
            self._update(failure=bt.Failure(bt.NOT_READY, bt.FORGETTING, device=device,
                                            name=known.name if known else ""))
            return
        self._begin(bt.FORGETTING, device)

    def cancel(self):
        self.calls.append(("cancel",))
        activity, prompt = self._snapshot.activity, self._snapshot.prompt
        if prompt is None and (activity is None or activity.kind != bt.PAIRING):
            return
        device = activity.device if activity is not None else prompt.device
        name = activity.name if activity is not None else prompt.name
        stage = activity.stage if activity is not None else bt.STAGE_PAIR
        self._update(prompt=None, activity=None, failure=bt.Failure(bt.CANCELED, bt.PAIRING, stage, device, name))

    def answer(self, prompt, reply):
        self.calls.append(("answer", prompt, reply))
        current = self._snapshot.prompt
        if current is not None and current.id == prompt:
            self._update(prompt=None)

    def dismiss(self):
        self.calls.append(("dismiss",))
        if self._snapshot.failure is not None:
            self._update(failure=None)

    # ---- what the test scripts -----------------------------------------------------------------------------------
    def set_state(self, state):
        self._update(state=state)

    def add_device(self, name="", kind="other", paired=False, connected=False, battery=None, trusted=None) -> int:
        self._last_id += 1
        self._last_order += 1
        self._devices[self._last_id] = bt.Device(self._last_id, name, kind, paired,
                                                 paired if trusted is None else trusted, connected, battery,
                                                 self._last_order)
        self._update()
        return self._last_id

    def change_device(self, device, **fields):
        self._devices[device] = replace(self._devices[device], **fields)
        self._update()

    def remove_device(self, device):
        del self._devices[device]
        self._update()

    def set_scanning(self, scanning):
        """The search ends by itself, as it does after ``SCAN_SECONDS``."""
        self._update(scanning=scanning)

    def stage(self, stage):
        """The pairing under way moves on to trusting or connecting."""
        self._update(activity=replace(self._snapshot.activity, stage=stage))

    def show_prompt(self, kind, code="", typed=0, max_length=0) -> int:
        """The device being paired asks something; a new prompt always has a new id."""
        activity = self._snapshot.activity
        assert activity is not None and activity.kind == bt.PAIRING, "only a pairing asks anything"
        self._last_prompt += 1
        self._update(prompt=bt.Prompt(self._last_prompt, kind, activity.device, activity.name, code, typed,
                                      max_length))
        return self._last_prompt

    def type_digits(self, typed):
        """The device of a CODE prompt has received this many digits."""
        self._update(prompt=replace(self._snapshot.prompt, typed=typed))

    def finish(self):
        """The activity under way succeeds."""
        activity = self._snapshot.activity
        changes = {"activity": None, "prompt": None}
        if activity.kind == bt.POWERING:
            on = self._power
            changes["state"] = bt.ON if on else bt.OFF
            if not on:
                changes["scanning"] = False
                for device in self._devices:
                    self._devices[device] = replace(self._devices[device], connected=False)
        elif activity.kind == bt.FORGETTING:
            self._devices.pop(activity.device, None)
        elif activity.device in self._devices:
            linked = activity.kind != bt.DISCONNECTING
            extra = {"paired": True, "trusted": True} if activity.kind == bt.PAIRING else {}
            self._devices[activity.device] = replace(self._devices[activity.device], connected=linked, **extra)
        self._update(**changes)

    def fail(self, code, action=None, paired=False):
        """The activity under way fails (or, with no activity, the operation ``action``). ``paired``: the device
        was paired before connecting failed, so it is now among the remembered."""
        activity = self._snapshot.activity
        if activity is None:
            self._update(failure=bt.Failure(code, action or ""))
            return
        if paired and activity.device in self._devices:
            self._devices[activity.device] = replace(self._devices[activity.device], paired=True, trusted=True)
        self._update(activity=None, prompt=None,
                     failure=bt.Failure(code, action or activity.kind, activity.stage, activity.device, activity.name,
                                        paired))

    # ---- inside ----------------------------------------------------------------------------------------------------
    def _busy(self) -> bool:
        return self._snapshot.activity is not None

    def _begin(self, kind, device) -> bool:
        if self._busy():
            return False
        known = self._devices.get(device)
        if known is None:
            self._update(failure=bt.Failure(bt.GONE, kind, device=device))
            return False
        stage = bt.STAGE_PAIR if kind == bt.PAIRING else ""
        self._update(activity=bt.Activity(kind, device, known.name, stage), failure=None)
        return True

    def _update(self, **fields):
        ordered = sorted(self._devices.values(), key=lambda d: (not d.paired, d.order))
        fields.update(devices=tuple(ordered[:bt.MAX_DEVICES]), truncated=len(ordered) > bt.MAX_DEVICES)
        self._snapshot = replace(self._snapshot, **fields)
        self._notify()

    def _notify(self):
        if self.running:
            self.changes += 1
            self.on_change()
