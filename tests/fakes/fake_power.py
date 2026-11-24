"""A power profile source that a test scripts by hand, keeping to the rules of ``gaminghome.power``.

It stands where the power-profiles-daemon client (``gaminghome/powerprofiles.py``) stands in the app and touches
nothing of the machine: no bus, no timer, no process, no file. The test says what the service looks like (the
keyword arguments, ``change``) and how a request ends (``finish``, ``fail``). That world outlives the source, as the
service outlives the client.

Where a window could be fooled, it does what the client does:

* the snapshot is ``UNKNOWN`` until the first ``start()``; what was last read stays across ``stop()`` and the next
  ``start()``, so the header does not blink, while ``pending``, ``failure`` and ``note`` go at ``stop()``;
* ``stop()`` forgets the request on its way: the service may still carry it out (``finish`` then changes the world,
  and the next reading shows it), but nothing is reported about it, and nothing at all is reported after ``stop()``;
* ``set_profile`` is ignored while stopped and while a request is on its way. It ends at once as ``UNAVAILABLE``
  while the service is not ``READY`` and before this visit's first reading (with ``read_at_start=False`` that is
  ``read()``; until then the last visit's reading is shown), and as ``UNSUPPORTED`` for a profile that is not
  offered. For the active profile it asks the service nothing (``sent`` stays as it is) and only tells Omarchy
  (``remembered``). Otherwise the service is asked: with ``answers="now"`` it answers at once, with
  ``answers="later"`` when the test calls ``finish()`` or ``fail(code)``;
* another program's change after a request of this visit succeeded is noted (``CHANGED_ELSEWHERE``), and so is
  Omarchy's command failing (``not_remembered``); a new request and ``dismiss`` clear the failure and the note.

When it reports: ``on_change`` only between ``start()`` and ``stop()``, and a burst of changes once. The client calls
``on_change`` from inside ``start``, ``set_profile``, ``dismiss`` and ``retry``; this stand-in does that only with
``reentrant=True``, so that a window cannot rely on it by accident. Otherwise a change made inside one of those calls
goes through ``defer`` when one is given (the harness hands it the main loop), or waits for ``flush()``. A change the
test scripts is reported at once, or through ``defer``. Every call of the protocol, ignored or not, is in ``calls``.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from dataclasses import replace

from gaminghome.power import (ABSENT, CHANGED_ELSEWHERE, ERROR, NOT_REMEMBERED, PERFORMANCE, PROFILES, READY,
                              UNAVAILABLE, UNSUPPORTED, Evidence, Failure, Snapshot)


class FakePower:
    def __init__(self, on_change=lambda: None, *, state=READY, active=PERFORMANCE, available=PROFILES, degraded=(),
                 holds=(), evidence=Evidence(), answers="now", read_at_start=True, reentrant=False, defer=None):
        self.on_change = on_change
        self.answers = answers                # "now", or "later" for finish() and fail()
        self.read_at_start = read_at_start    # False: the visit's first reading comes with read()
        self.reentrant = reentrant
        self.defer = defer
        self.world = {"state": state, "active": active, "available": tuple(available), "degraded": tuple(degraded),
                      "holds": tuple(holds), "evidence": evidence}
        self.calls: list[tuple] = []          # every call of the protocol, ignored or not
        self.sent: list[str] = []             # the profiles the service was asked for
        self.remembered: list[str] = []       # the profiles Omarchy was told to keep
        self.running = False
        self.changes = 0                      # how often on_change was called
        self._snapshot = Snapshot()
        self._asked = ""                      # the request the service is working on, whether the source waits or not
        self._live = False                    # the service was read in this visit
        self._armed = False                   # a request of this visit succeeded: another program's change is noted
        self._epoch = 0                       # one more at each start() and stop(): a late report of another is dropped
        self._inside = 0                      # calls of the protocol under way
        self._owed = False                    # a change made inside one of them, for flush()
        self._scheduled = None                # the epoch of the report handed to defer and not delivered yet

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def asked(self) -> str:
        """The profile the service is still working on, "" when none."""
        return self._asked

    # ---- the protocol --------------------------------------------------------------------------------------------
    def start(self):
        self.calls.append(("start",))
        if self.running:
            return
        self.running, self._live = True, False
        self._epoch += 1
        if self.read_at_start:
            with self._inside_call():
                self._read()

    def stop(self):
        self.calls.append(("stop",))
        if not self.running:
            return
        self.running, self._live, self._armed, self._owed = False, False, False, False
        self._epoch += 1
        self._snapshot = replace(self._snapshot, pending="", failure=None, note="")

    def retry(self):
        self.calls.append(("retry",))
        if self.running and self._snapshot.state == ERROR:
            with self._inside_call():
                self._read()

    def dismiss(self):
        self.calls.append(("dismiss",))
        if self.running:
            self._armed = False
            with self._inside_call():
                self._update(failure=None, note="")

    def set_profile(self, profile):
        self.calls.append(("set_profile", profile))
        snapshot = self._snapshot
        if not self.running or snapshot.pending:
            return
        self._armed = False
        asked = profile if profile in PROFILES else ""
        with self._inside_call():
            if snapshot.state != READY or not self._live:
                self._update(failure=Failure(UNAVAILABLE, asked), note="")
            elif profile not in snapshot.available:
                self._update(failure=Failure(UNSUPPORTED, asked), note="")
            elif profile == snapshot.active:
                self._armed = True                # asked nothing of the service; Omarchy is told all the same
                self.remembered.append(profile)
                self._update(failure=None, note="")
            else:
                self.sent.append(profile)
                self._asked = profile
                self._update(pending=profile, failure=None, note="")
                if self.answers == "now":
                    self.finish()

    # ---- what the test scripts -----------------------------------------------------------------------------------
    def read(self):
        """The service answers this visit's first reading (with ``read_at_start=False``)."""
        if self.running:
            self._read()

    def finish(self):
        """The service carries out the request on its way and answers. A source stopped meanwhile forgot it, and
        sees the profile at its next reading."""
        profile, self._asked = self._asked, ""
        assert profile, "nothing was asked"
        self.world["active"] = profile
        if not (self.running and self._live):
            return
        if self._snapshot.pending == profile:
            self._armed = True
            self.remembered.append(profile)
            self._update(active=profile, pending="")
        else:
            self._update(active=profile)          # asked in an earlier visit: the service says what it did

    def fail(self, code):
        """The service refuses the request on its way, or does not answer it."""
        profile, self._asked = self._asked, ""
        assert profile, "nothing was asked"
        if self.running and self._snapshot.pending == profile:
            self._update(pending="", failure=Failure(code, profile))

    def change(self, **world):
        """The service changes: another program switches the profile, the service restarts with other profiles,
        holds or reasons, the kernel is told something else, or the service goes, comes back or cannot be read. As
        with the client, a service that could not be read is read again only by ``retry``, and a change before this
        visit's first reading shows with that reading."""
        self.world.update(world)
        if not self.running:
            return
        gone, back = self.world["state"] == ABSENT, self._snapshot.state == ABSENT
        if self._live or gone or back:
            self._read()

    def not_remembered(self):
        """Omarchy's command could not keep the choice of this visit."""
        if self.running and self._armed:
            self._update(note=NOT_REMEMBERED)

    def flush(self):
        """What the main loop does once the window's call has returned: report a change made inside it."""
        if self._owed:
            self._deliver(self._epoch)

    # ---- inside ----------------------------------------------------------------------------------------------------
    def _read(self):
        world, snapshot = self.world, self._snapshot
        self._live = world["state"] == READY
        if self._live:
            reading = {"state": READY, "active": world["active"],
                       "available": tuple(p for p in PROFILES if p in world["available"]),
                       "degraded": world["degraded"], "holds": world["holds"]}
        else:
            reading = {"state": world["state"], "active": "", "available": (), "degraded": (), "holds": ()}
        changes = {**reading, "evidence": world["evidence"]}
        if world["state"] == ABSENT:
            self._armed, self._asked = False, ""    # the service went, and the request on its way with it
            if snapshot.pending:
                changes.update(pending="", failure=Failure(UNAVAILABLE, snapshot.pending))
        elif (self._armed and not snapshot.pending and snapshot.state == READY and reading["active"]
              and reading["active"] != snapshot.active):
            changes["note"] = CHANGED_ELSEWHERE
        self._update(**changes)

    @contextmanager
    def _inside_call(self):
        self._inside += 1
        try:
            yield
        finally:
            self._inside -= 1

    def _update(self, **changes):
        snapshot = replace(self._snapshot, **changes)
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self._report()

    def _report(self):
        if not self.running:
            return
        if self._inside and self.reentrant:
            self._deliver(self._epoch)
        elif self.defer is not None:
            if self._scheduled != self._epoch:
                self._scheduled = self._epoch
                self.defer(functools.partial(self._deliver, self._epoch))
        elif self._inside:
            self._owed = True
        else:
            self._deliver(self._epoch)

    def _deliver(self, epoch):
        if self._scheduled == epoch:
            self._scheduled = None
        if self.running and epoch == self._epoch:
            self._owed = False
            self.changes += 1
            self.on_change()
