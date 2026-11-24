"""A Bluetooth source that a test scripts by hand, keeping to the rules of ``gaminghome.bluetooth``.

It stands where the BlueZ client stands in the app and touches nothing of the machine: no bus, no timer of its own, no
thread. The test says what Bluetooth looks like (``set_state``, ``add_device``, ``show_prompt``) and how an
operation ends (``finish``, ``fail``). That world outlives the source, as Bluetooth outlives the client: ``stop()``
forgets what the source had read and done, ``start()`` reads the world again, and outside the two the snapshot is
the contract's ``UNKNOWN``, as the BlueZ client's is.

The rules of the contract hold by themselves: one activity at a time, during which ``set_power``, ``connect``,
``disconnect``, ``pair``, ``forget`` and ``scan(True)`` are ignored while ``scan(False)``, ``cancel`` and ``answer``
still work; a new operation clears the failure; an answer to a prompt that is no longer asked is ignored; a pairing
ends the search; at most ``MAX_DEVICES`` devices are listed, remembered ones first; ``start`` and ``stop`` are
idempotent, and ``on_change`` is called only between them. Every call, ignored or not, is recorded in ``calls``.

Two options make it behave the way the BlueZ client does where a window can go wrong. ``defer(callback)`` reports a
change later instead of from inside the call that made it (the client reports from a timer on the main loop), and a
burst of changes once. With ``cancel_waits``, cancelling a pairing takes the question away at once and ends the
pairing only when the test calls ``finish_cancel()``, as BlueZ answers ``CancelPairing`` up to ten seconds later.
"""

from __future__ import annotations

from dataclasses import replace

from gaminghome import bluetooth as bt

FRESH = {"scanning": False, "activity": None, "prompt": None, "failure": None}


class FakeBluetooth:
    def __init__(self, on_change=lambda: None, state=bt.ON, *, defer=None, cancel_waits=False):
        self.on_change = on_change
        self.defer = defer
        self.cancel_waits = cancel_waits
        self.calls: list[tuple] = []
        self.running = False
        self.changes = 0                     # how often on_change was called
        self._state = state                  # the world: what Bluetooth is, whether the source runs or not
        self._devices: dict[int, bt.Device] = {}
        self._own = dict(FRESH)              # what the source itself does: its search, activity, prompt, failure
        self._snapshot = bt.Snapshot()
        self._last_id = self._last_prompt = self._last_order = 0
        self._power = None                   # what set_power asked for, while it runs
        self._cancelling = False             # a cancelled pairing waits for BlueZ to answer
        self._epoch = 0                      # one per start() and stop(): a late report of an earlier one is dropped
        self._pending = False

    @property
    def snapshot(self) -> bt.Snapshot:
        return self._snapshot if self.running else bt.Snapshot()

    # ---- the protocol --------------------------------------------------------------------------------------------
    def start(self):
        self.calls.append(("start",))
        if not self.running:
            self.running = True
            self._epoch += 1
            self._update()                   # as a source does once it has read Bluetooth

    def stop(self):
        """Everything the source read and did is let go: the search, the pairing and every call in flight."""
        self.calls.append(("stop",))
        if self.running:
            self.running = False
            self._epoch += 1
            self._own, self._power, self._cancelling, self._pending = dict(FRESH), None, False, False

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
            if self._own["scanning"]:
                self._update(scanning=False)
        elif self._busy():
            return                           # ignored while something runs
        elif self._state != bt.ON:
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
        self._begin(bt.PAIRING, device, scanning=False)

    def forget(self, device):
        self.calls.append(("forget", device))
        if self._state != bt.ON and not self._busy():
            known = self._devices.get(device)
            self._update(failure=bt.Failure(bt.NOT_READY, bt.FORGETTING, device=device,
                                            name=known.name if known else ""))
            return
        self._begin(bt.FORGETTING, device)

    def cancel(self):
        self.calls.append(("cancel",))
        activity, prompt = self._own["activity"], self._own["prompt"]
        if self._cancelling or (prompt is None and (activity is None or activity.kind != bt.PAIRING)):
            return
        if self.cancel_waits:
            self._cancelling = True
            self._update(prompt=None)        # the question goes at once, the pairing when BlueZ answers
        else:
            self.finish_cancel()

    def answer(self, prompt, reply):
        self.calls.append(("answer", prompt, reply))
        current = self._own["prompt"]
        if current is not None and current.id == prompt:
            self._update(prompt=None)

    def dismiss(self):
        self.calls.append(("dismiss",))
        if self._own["failure"] is not None:
            self._update(failure=None)

    # ---- what the test scripts -----------------------------------------------------------------------------------
    def set_state(self, state):
        self._state = state
        self._update()

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
        self._update(activity=replace(self._own["activity"], stage=stage))

    def show_prompt(self, kind, code="", typed=0, max_length=0) -> int:
        """The device being paired asks something; a new prompt always has a new id."""
        activity = self._own["activity"]
        assert activity is not None and activity.kind == bt.PAIRING, "only a pairing asks anything"
        self._last_prompt += 1
        self._update(prompt=bt.Prompt(self._last_prompt, kind, activity.device, activity.name, code, typed,
                                      max_length))
        return self._last_prompt

    def type_digits(self, typed):
        """The device of a CODE prompt has received this many digits."""
        self._update(prompt=replace(self._own["prompt"], typed=typed))

    def finish_cancel(self):
        """BlueZ answers the cancelled pairing: it ends, cancelled."""
        activity, prompt = self._own["activity"], self._own["prompt"]
        self._cancelling = False
        device = activity.device if activity is not None else prompt.device
        name = activity.name if activity is not None else prompt.name
        stage = activity.stage if activity is not None else bt.STAGE_PAIR
        self._update(prompt=None, activity=None, failure=bt.Failure(bt.CANCELED, bt.PAIRING, stage, device, name))

    def finish(self):
        """The activity under way succeeds."""
        activity = self._own["activity"]
        assert activity is not None, "nothing runs"
        changes = {"activity": None, "prompt": None}
        if activity.kind == bt.POWERING:
            self._state = bt.ON if self._power else bt.OFF
            if not self._power:
                changes["scanning"] = False
                for device in self._devices:
                    self._devices[device] = replace(self._devices[device], connected=False)
        elif activity.kind == bt.FORGETTING:
            self._devices.pop(activity.device, None)
        elif activity.device in self._devices:
            linked = activity.kind != bt.DISCONNECTING
            extra = {"paired": True, "trusted": True} if activity.kind == bt.PAIRING else {}
            self._devices[activity.device] = replace(self._devices[activity.device], connected=linked, **extra)
        self._cancelling = False
        self._update(**changes)

    def fail(self, code, action=None, paired=False):
        """The activity under way fails (or, with no activity, the operation ``action``: a search that could not
        start after all stops searching, as the BlueZ client says it searches before BlueZ has answered).
        ``paired``: the device was paired before connecting failed, so it is now among the remembered."""
        activity = self._own["activity"]
        if activity is None:
            self._update(failure=bt.Failure(code, action or ""), **({"scanning": False} if action == bt.SEARCHING
                                                                     else {}))
            return
        if paired and activity.device in self._devices:
            self._devices[activity.device] = replace(self._devices[activity.device], paired=True, trusted=True)
        self._cancelling = False
        self._update(activity=None, prompt=None,
                     failure=bt.Failure(code, action or activity.kind, activity.stage, activity.device, activity.name,
                                        paired))

    # ---- inside ----------------------------------------------------------------------------------------------------
    def _busy(self) -> bool:
        return self._own["activity"] is not None

    def _begin(self, kind, device, **fields) -> bool:
        if self._busy():
            return False
        known = self._devices.get(device)
        if known is None:
            self._update(failure=bt.Failure(bt.GONE, kind, device=device))
            return False
        stage = bt.STAGE_PAIR if kind == bt.PAIRING else ""
        self._update(activity=bt.Activity(kind, device, known.name, stage), failure=None, **fields)
        return True

    def _update(self, **own):
        if not self.running:
            return                           # nothing is read or done while stopped; the world keeps what changed
        self._own.update(own)
        ordered = sorted(self._devices.values(), key=lambda d: (not d.paired, d.order))
        self._snapshot = bt.Snapshot(self._state, devices=tuple(ordered[:bt.MAX_DEVICES]),
                                     truncated=len(ordered) > bt.MAX_DEVICES, **self._own)
        self._notify()

    def _notify(self):
        if not self.running:
            return
        if self.defer is None:
            self._deliver(self._epoch)
        elif not self._pending:
            self._pending = True
            self.defer(lambda epoch=self._epoch: self._deliver(epoch))

    def _deliver(self, epoch):
        self._pending = False
        if self.running and epoch == self._epoch:
            self.changes += 1
            self.on_change()
