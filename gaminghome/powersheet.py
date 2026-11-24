"""What the power profile button and sheet of Gaming Home show, and what their controls do, apart from GTK.

``PowerSheet`` holds the window's power profile source (``power.Power``) and whether the sheet is being visited.
Every way out of the sheet ends in ``leave``, and a press that arrives after it does nothing: GTK clicks a button a
moment after A or Enter, and that click may come after the sheet went away. Entering the sheet dismisses a failure
or a note left from before, so that what happened while the user was away does not greet the next visit.

``blocks`` describes the sheet from top to bottom as plain data. Each block has a stable key and each control a
stable name that carries nothing another program chose (``pw:<profile>``, ``pw:dismiss``, ``pw:retry``,
``pw:close``), so the window can update the sheet in place and keep the focus where it was, and the status command
can name the focused control. A choice applies at once with A, a click or a tap; nothing asks to confirm it, and
nothing applies on hover or focus. While a request is on its way the other choices are dimmed and do nothing, but
they keep the focus.

Nothing here imports GTK or GLib or owns a timer.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import power
from .power import BALANCED, ERROR, FAILURES, HEAT, LAP, LIMITED, NOTES, PERFORMANCE, PROFILES, SAVER

BUTTON = "power"                      # the header button
OPTION = "pw:"                        # the start of an option's name: pw:power-saver, pw:balanced, pw:performance
DISMISS, RETRY, CLOSE = "pw:dismiss", "pw:retry", "pw:close"
NAMES = (BUTTON, *(OPTION + profile for profile in PROFILES), DISMISS, RETRY, CLOSE)   # every name there is

ICONS = {SAVER: "power-saver", BALANCED: "power-balanced", PERFORMANCE: "power-performance"}
MAX_HOLDS_SHOWN = 3

# The words of the firmware's platform profile that go with each profile. Any other word, "custom" among them,
# disagrees with it. The CPU may boost in every profile but power-saver.
FIRMWARE_WORDS = {SAVER: ("low-power", "quiet", "power-saver"), BALANCED: ("balanced", "balanced-performance"),
                  PERFORMANCE: ("performance", "balanced-performance")}
DEGRADED_REASONS = (HEAT, LAP, LIMITED)


def option_name(profile: str) -> str:
    return OPTION + profile


def option_profile(name: str) -> str:
    """The profile an option's name carries, or "" when the name is not an option's."""
    profile = name[len(OPTION):] if isinstance(name, str) and name.startswith(OPTION) else ""
    return profile if profile in PROFILES else ""


def profile_title(profile: str, t) -> str:
    return t(f"power.profile.{profile}") if profile in PROFILES else ""


def failure_text(failure: power.Failure, t) -> str:
    """One line from the failure's code and the profile asked for: nothing the system said is in the model."""
    reason = t(f"power.reason.{failure.code}" if failure.code in FAILURES else "power.reason.failed")
    if failure.profile not in PROFILES:
        return reason
    return t("power.failure", action=t("power.failed", profile=profile_title(failure.profile, t)), reason=reason)


def degraded_text(reasons, t) -> str:
    """One sentence for every reason the service holds performance back; "" for none, which promises nothing."""
    parts = [t(f"power.degraded.{reason}") for reason in DEGRADED_REASONS if reason in reasons]
    if not parts:
        return ""
    words = parts[0]
    for part in parts[1:]:
        words = t("power.degraded.and", first=words, second=part)
    return t("power.degraded", reasons=words)


def hold_lines(holds, t) -> list[str]:
    """What the other programs that hold a profile ask for, each program and profile once, at most
    ``MAX_HOLDS_SHOWN``. A hold is in force only while its profile is the active one, so none is said to be."""
    lines, seen = [], set()
    for hold in holds:
        if hold.ours or hold.profile not in PROFILES or (hold.name, hold.profile) in seen:
            continue
        seen.add((hold.name, hold.profile))
        profile = profile_title(hold.profile, t)
        lines.append(t("power.hold", name=hold.name, profile=profile) if hold.name else
                     t("power.holdUnnamed", profile=profile))
        if len(lines) == MAX_HOLDS_SHOWN:
            break
    return lines


def evidence_text(snapshot: power.Snapshot, t) -> str:
    """A sentence when what the kernel was told disagrees with the active profile, else "". It is what the kernel
    was told, not a measurement, and it never says whether a change worked."""
    active = snapshot.active
    if active not in PROFILES:
        return ""
    firmware, boost = snapshot.evidence.firmware, snapshot.evidence.boost
    profile = profile_title(active, t)
    if firmware and firmware not in FIRMWARE_WORDS[active]:
        return t("power.evidence.firmware", firmware=firmware, profile=profile)
    if boost == "off" and active != SAVER:
        return t("power.evidence.boostOff", profile=profile)
    if boost == "on" and active == SAVER:
        return t("power.evidence.boostOn", profile=profile)
    return ""


# ---- what the window shows ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ButtonView:
    """The header button: whether it exists, its icon (``gh-<icon>-symbolic``), its tone, and its tooltip, which is
    also its accessible name."""
    visible: bool
    icon: str = "power"
    tone: str = ""               # "" or "warn"
    tooltip: str = ""


def button_view(snapshot: power.Snapshot, t) -> ButtonView:
    if not snapshot.visible:
        return ButtonView(False)
    if snapshot.state == ERROR:
        return ButtonView(True, "power-error", "warn", t("power.tooltipError"))
    if snapshot.active in PROFILES:
        return ButtonView(True, ICONS[snapshot.active], "",
                          t("power.tooltip", profile=profile_title(snapshot.active, t)))
    return ButtonView(True, "power", "", t("power.title"))


@dataclass(frozen=True)
class Control:
    name: str
    text: str = ""
    style: str = ""              # "primary" or ""


@dataclass(frozen=True)
class Block:
    """One part of the sheet. ``key`` stays the same while the part shows the same thing; ``shape`` changes only
    when its controls do, so the window rebuilds a part only then and otherwise updates it in place."""
    key: str
    controls: tuple[Control, ...] = ()

    @property
    def shape(self) -> tuple:
        return (type(self).__name__,) + tuple(control.name for control in self.controls)


@dataclass(frozen=True)
class Text(Block):
    text: str = ""
    tone: str = "body"           # "title", "body" or "muted"

    @property
    def shape(self) -> tuple:
        return (type(self).__name__, self.tone)


@dataclass(frozen=True)
class Option(Block):
    """A profile to choose. Its one control has the option's name."""
    profile: str = ""
    title: str = ""
    body: str = ""
    current: bool = False        # the active profile: checked
    pending: bool = False        # asked for, and the service has not answered yet
    status: str = ""             # "Switching…" while pending
    dimmed: bool = False         # another profile is being asked for: a press does nothing now

    @property
    def icon(self) -> str:
        return ICONS[self.profile]


@dataclass(frozen=True)
class Notice(Block):
    """A sentence about the profile: a failure (``warn``) or something worth knowing. The first one that a failure
    or a note puts up carries Dismiss."""
    text: str = ""
    tone: str = ""               # "warn" or ""


@dataclass(frozen=True)
class Actions(Block):
    text: str = ""               # what the buttons are about, before them on the same row
    end: bool = False            # the buttons sit at the end of the row, as Close does in every sheet


def control_names(blocks) -> list[str]:
    """Every control of these blocks, in order from the top."""
    return [control.name for block in blocks for control in block.controls]


def current_option(snapshot: power.Snapshot, blocks) -> str | None:
    """The name of the active profile's option, when the sheet shows it."""
    name = option_name(snapshot.active) if snapshot.active in PROFILES else None
    return name if name in control_names(blocks) else None


def choose_focus(wanted: str | None, names, current: str | None) -> str | None:
    """The control to focus after the sheet changed: the one that had the focus while it is there, else a control
    that changes nothing: the active profile's option, Try again or Close. None when the sheet has no control."""
    names = list(names)
    if wanted in names:
        return wanted
    for name in (current, RETRY, CLOSE):
        if name in names:
            return name
    return names[0] if names else None


def opening_focus(names, current: str | None) -> str | None:
    """Where the focus starts when the sheet opens: on the active profile, so that A changes nothing."""
    return choose_focus(None, names, current)


# ---- the sheet --------------------------------------------------------------------------------------------------
class PowerSheet:
    """The window's power profile source and whether the sheet is being visited.

    The window calls ``start`` when it is mapped and ``stop`` when it is unmapped, destroyed or the app quits;
    ``enter`` when the sheet opens and ``leave`` on every way it goes away; ``press`` with a control's name when it
    is pressed. A control acts on the snapshot as it is when it is pressed.
    """

    def __init__(self, source: power.Power):
        self.source = source
        self.visiting = False

    @property
    def snapshot(self) -> power.Snapshot:
        return self.source.snapshot

    # -- lifecycle
    def start(self):
        self.source.start()

    def stop(self):
        self.leave()
        self.source.stop()

    def enter(self):
        """The sheet opens. A failure or a note that is still there came from before, perhaps after the user left:
        it is dismissed, so that it does not greet this visit."""
        if self.visiting:
            return
        self.visiting = True
        snapshot = self.source.snapshot
        if snapshot.failure is not None or snapshot.note:
            self.source.dismiss()

    def leave(self) -> bool:
        """The sheet goes away, whichever way. A request on its way goes on: it was asked for. False when the sheet
        was not open."""
        if not self.visiting:
            return False
        self.visiting = False
        return True

    # -- controls
    def applies(self, profile: str) -> bool:
        """Choosing ``profile`` would ask for it now: the service can be asked, nothing else is on its way, the
        machine offers it and it is not the active one already."""
        snapshot = self.source.snapshot
        return (snapshot.usable and not snapshot.pending and profile in snapshot.available
                and profile != snapshot.active)

    def press(self, name: str) -> str | None:
        """Do what the control of this name does; there is never another control to focus afterwards. Nothing
        happens once the sheet has left: GTK may still deliver a click it was about to make. Close is the window's."""
        if not self.visiting:
            return None
        snapshot = self.source.snapshot
        if name == DISMISS:
            if snapshot.failure is not None or snapshot.note:
                self.source.dismiss()
        elif name == RETRY:
            if snapshot.state == ERROR:
                self.source.retry()
        else:
            profile = option_profile(name)
            if profile and self.applies(profile):
                self.source.set_profile(profile)
        return None

    # -- what the sheet shows
    def blocks(self, t, battery=None) -> list[Block]:
        """The sheet from top to bottom. ``battery`` is what the header's battery chip shows, None for none."""
        snapshot = self.source.snapshot
        blocks: list[Block] = [Text("title", text=t("power.title"), tone="title"), Text("intro", text=t("power.intro"))]
        if snapshot.usable:
            blocks.extend(self._options(snapshot, t))
        elif snapshot.state == ERROR:
            blocks.append(Actions("retry", (Control(RETRY, t("power.tryAgain"), "primary"),),
                                  text=t("power.readFailed")))
        else:
            blocks.append(Text("unavailable", text=t("power.unavailable")))
        blocks.extend(self._notices(snapshot, t))
        if snapshot.usable and battery is not None:
            blocks.append(Text("sources", text=t("power.sources"), tone="muted"))
        blocks.append(Actions("close", (Control(CLOSE, t("button.close")),), end=True))
        return blocks

    @staticmethod
    def _options(snapshot: power.Snapshot, t) -> list[Block]:
        options: list[Block] = []
        for profile in PROFILES:
            if profile not in snapshot.available:
                continue
            pending = snapshot.pending == profile
            options.append(Option(f"option:{profile}", (Control(option_name(profile)),), profile=profile,
                                  title=profile_title(profile, t), body=t(f"power.about.{profile}"),
                                  current=snapshot.active == profile, pending=pending,
                                  status=t("power.switching") if pending else "",
                                  dimmed=bool(snapshot.pending) and not pending))
        return options

    @staticmethod
    def _notices(snapshot: power.Snapshot, t) -> list[Block]:
        notices: list[Block] = []
        dismiss = (Control(DISMISS, t("power.dismiss")),)
        if snapshot.failure is not None:
            notices.append(Notice("failure", dismiss, text=failure_text(snapshot.failure, t), tone="warn"))
        if snapshot.note in NOTES:
            notices.append(Notice("note", () if notices else dismiss, text=t(f"power.note.{snapshot.note}")))
        if not snapshot.usable:
            return notices
        if PERFORMANCE in snapshot.available:
            degraded = degraded_text(snapshot.degraded, t)
            if degraded:
                notices.append(Notice("degraded", text=degraded))
        holds = hold_lines(snapshot.holds, t)
        if holds:
            notices.append(Notice("holds", text="\n".join(holds)))
        evidence = evidence_text(snapshot, t)
        if evidence:
            notices.append(Notice("evidence", text=evidence))
        return notices
