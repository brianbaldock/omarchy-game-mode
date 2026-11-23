"""Result model for the doctor: plain, JSON-serialisable data with no I/O.

A check produces an `Outcome`; the runner turns it into a `CheckResult` by adding
the identity and timing; a `Report` is the full run. `fail` is reserved for direct
contradictory evidence. "Could not test" is always `skip` (or `warn` when the
missing answer is itself suggestive), so a clean exit code never hides a guess.

A check marked `decisive` guards evidence the verdict depends on (firewall rules,
the console, a live failed attempt). While any of those is unverified the run is
`inconclusive`, however many local checks passed.

Other commands (`buttons`, a future `capture`) reuse `Report`: they set `mode` and
attach their own allowlisted blocks through `sections`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Any, Callable

SCHEMA = "omarchy-game-mode.doctor/v1"
SCHEMA_VERSION = 1
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class Status(enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    INFO = "info"

    @property
    def marker(self) -> str:
        return f"[{self.name}]"


@dataclass
class Outcome:
    """What one check found. `proves`/`does_not_prove` override the check's static text."""

    status: Status
    detail: str = ""
    remediation: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    root_command: str = ""
    proves: str | None = None
    does_not_prove: str | None = None


def ok(detail: str, **evidence: Any) -> Outcome:
    return Outcome(Status.PASS, detail, evidence=evidence)


def info(detail: str, **evidence: Any) -> Outcome:
    return Outcome(Status.INFO, detail, evidence=evidence)


def warn(detail: str, remediation: str = "", **evidence: Any) -> Outcome:
    return Outcome(Status.WARN, detail, remediation, evidence)


def fail(detail: str, remediation: str = "", **evidence: Any) -> Outcome:
    return Outcome(Status.FAIL, detail, remediation, evidence)


def skip(detail: str, remediation: str = "", **evidence: Any) -> Outcome:
    return Outcome(Status.SKIP, detail, remediation, evidence)


def needs_root(command: str, remediation: str = "", **evidence: Any) -> Outcome:
    """A check only root can answer. The doctor prints the command; it never runs it."""
    return Outcome(Status.SKIP, f"cannot verify without root - run: {command}", remediation, evidence,
                   root_command=command)


@dataclass
class CheckResult:
    id: str
    title: str
    status: Status
    detail: str = ""
    category: str = ""
    proves: str = ""
    does_not_prove: str = ""
    remediation: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    root_command: str = ""
    duration_ms: int = 0
    decisive: bool = False

    def root(self) -> dict[str, Any]:
        if not self.root_command:
            return {"required": False}
        return {"required": True, "status": "not_run", "cannot_verify_without_root": True,
                "command": self.root_command}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "category": self.category, "title": self.title, "status": self.status.value,
            "detail": self.detail, "duration_ms": self.duration_ms, "proves": self.proves,
            "does_not_prove": self.does_not_prove, "evidence": self.evidence, "root": self.root(),
            "remediation": self.remediation, "decisive": self.decisive,
        }


@dataclass
class Report:
    scope: str
    checks: list[CheckResult]
    environment: dict[str, Any] = field(default_factory=dict)
    privacy: dict[str, Any] = field(default_factory=dict)
    sections: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    mode: str = "doctor"

    def counts(self) -> dict[str, int]:
        totals = {status.value: 0 for status in Status}
        for check in self.checks:
            totals[check.status.value] += 1
        return totals

    def unverified(self) -> list[CheckResult]:
        """Decisive checks this run could not answer."""
        return [check for check in self.checks if check.decisive and check.status is Status.SKIP]

    def overall(self) -> str:
        counts = self.counts()
        if counts["fail"]:
            return "fail"
        if self.unverified():
            return "inconclusive"
        if counts["warn"]:
            return "warn"
        if counts["pass"]:
            return "pass"
        return "inconclusive" if counts["skip"] else "info"

    def exit_code(self) -> int:
        return EXIT_FAILED if self.counts()["fail"] else EXIT_OK

    def headline(self) -> str:
        counts = self.counts()
        overall = self.overall()
        warnings = f"{counts['warn']} warning{'' if counts['warn'] == 1 else 's'}"
        if overall == "fail":
            first = next(check for check in self.checks if check.status is Status.FAIL)
            return f"FAIL: {counts['fail']} check{'' if counts['fail'] == 1 else 's'} failed, first: {first.title}."
        if overall == "inconclusive":
            missing = ", ".join(check.title for check in self.unverified()) or "everything that was skipped"
            tested = "nothing was verified" if not counts["pass"] else "no failures in what was tested"
            extra = f", {warnings}" if counts["warn"] else ""
            return f"INCONCLUSIVE: {tested}{extra}. Not verified: {missing}."
        if overall == "warn":
            first = next(check for check in self.checks if check.status is Status.WARN)
            return f"No failures; {warnings}, first: {first.title}."
        if overall == "pass":
            return "No failures or warnings in what this run tested."
        return "Environment facts only; nothing was tested."

    def summary(self) -> dict[str, Any]:
        return {"status": self.overall(), **self.counts(), "exit_code": self.exit_code(),
                "headline": self.headline(), "unverified": [check.id for check in self.unverified()]}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA, "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
            "started_at": self.started_at, "finished_at": self.finished_at, "duration_ms": self.duration_ms,
            "mode": self.mode, "scope": self.scope, "summary": self.summary(),
            "environment": self.environment, "privacy": self.privacy,
            "checks": [check.to_dict() for check in self.checks], **self.sections,
        }

    def transformed(self, text: Callable[[str], str], data: Callable[[Any], Any]) -> Report:
        """Copy with every string passed through `text` and every structure through `data`."""
        checks = [replace(check, title=text(check.title), detail=text(check.detail), proves=text(check.proves),
                          does_not_prove=text(check.does_not_prove), remediation=text(check.remediation),
                          evidence=data(check.evidence), root_command=text(check.root_command))
                  for check in self.checks]
        return replace(self, checks=checks, environment=data(self.environment), sections=data(self.sections))
