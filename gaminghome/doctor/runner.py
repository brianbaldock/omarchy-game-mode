"""Bounded, isolated execution of doctor checks.

Workers are daemon threads rather than a ThreadPoolExecutor on purpose: an executor
joins its workers when the interpreter exits, so a single check stuck past its
timeout would hang the command on the way out. An overrunning daemon worker is
abandoned instead, and a replacement keeps the queue moving.

A check that raises or overruns becomes `skip` ("result unknown"), never `fail`:
a doctor bug or a stalled tool is not evidence about the user's system. The report
names the exception class and the line that raised it, never its message, which can
quote a path, an address or a line of tool output.

Evidence is allowlisted here, at the single point where an `Outcome` becomes part
of a report: a check declares the evidence fields it may emit and anything else is
dropped, so a parser that picks up too much cannot leak it.
"""

from __future__ import annotations

import collections
import errno
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar

from .model import CheckResult, Outcome, skip

T = TypeVar("T")
R = TypeVar("R")

_PENDING, _RUNNING, _DONE = "pending", "running", "done"
_PACKAGE, _THIS_FILE = os.path.split(os.path.abspath(__file__))
BASE_FIELDS = ("reason",)
REPORT_HINT = "Please report this with the output of `omarchy-game-mode doctor --json`."


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    category: str
    run: Callable[[Any], Outcome]
    proves: str = ""
    does_not_prove: str = ""
    fields: tuple[str, ...] = ()
    decisive: bool = False
    timeout: float | None = None


def allowlisted(evidence: Any, fields: Iterable[str], dropped: list[str] | None = None) -> Any:
    """Keep declared evidence fields only. `servers.rtt_ms` declares a key inside the rows of `servers`."""
    allowed = frozenset(fields)

    def walk(value: Any, prefix: str) -> Any:
        if isinstance(value, Mapping):
            kept = {}
            for key, item in value.items():
                path = f"{prefix}{key}"
                if path in allowed:
                    kept[str(key)] = walk(item, f"{path}.")
                elif dropped is not None:
                    dropped.append(path)
            return kept
        if isinstance(value, (list, tuple)):
            return [walk(item, prefix) for item in value]
        return value

    return walk(evidence, "")


class _Task:
    __slots__ = ("check", "state", "started", "result")

    def __init__(self, check: Check):
        self.check = check
        self.state = _PENDING
        self.started = 0.0
        self.result: CheckResult | None = None


def _result(check: Check, outcome: Outcome, seconds: float) -> CheckResult:
    return CheckResult(
        id=check.id, title=check.title, status=outcome.status, detail=outcome.detail, category=check.category,
        proves=check.proves if outcome.proves is None else outcome.proves,
        does_not_prove=check.does_not_prove if outcome.does_not_prove is None else outcome.does_not_prove,
        remediation=outcome.remediation, evidence=allowlisted(outcome.evidence, (*BASE_FIELDS, *check.fields)),
        root_command=outcome.root_command, duration_ms=max(0, round(seconds * 1000)), decisive=check.decisive)


def describe_exception(exc: BaseException) -> str:
    """`PermissionError EACCES at net:123`: enough to find the bug, nothing the exception quoted."""
    parts = [type(exc).__name__]
    number = getattr(exc, "errno", None)
    if isinstance(exc, OSError) and isinstance(number, int) and number in errno.errorcode:
        parts.append(errno.errorcode[number])
    own, innermost, trace = "", "", exc.__traceback__
    while trace is not None:
        directory, filename = os.path.split(trace.tb_frame.f_code.co_filename)
        innermost = f"at {os.path.splitext(filename)[0]}:{trace.tb_lineno}"
        if directory == _PACKAGE and filename != _THIS_FILE:
            own = innermost  # the deepest line of the doctor itself says more than one inside the library it called
        trace = trace.tb_next
    return " ".join(filter(None, (*parts, own or innermost)))


def _execute(check: Check, ctx: Any) -> Outcome:
    try:
        outcome = check.run(ctx)
        if not isinstance(outcome, Outcome):
            raise TypeError(f"check returned {type(outcome).__name__}, not an Outcome")
        return outcome
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        return skip(f"internal error in this check ({describe_exception(exc)}); result unknown",
                    REPORT_HINT, reason="internal-error")


def run_checks(checks: Sequence[Check], ctx: Any, *, timeout: float = 6.0, deadline: float = 45.0,
               workers: int = 8, monotonic: Callable[[], float] = time.monotonic) -> list[CheckResult]:
    """Run every check; results come back in the order the checks were given."""
    tasks = [_Task(check) for check in checks]
    pending = collections.deque(tasks)
    changed = threading.Condition()
    overall = monotonic() + deadline

    def worker() -> None:
        while True:
            with changed:
                if not pending:
                    return
                task = pending.popleft()
                task.state, task.started = _RUNNING, monotonic()
                changed.notify_all()
            outcome = _execute(task.check, ctx)
            with changed:
                if task.state == _RUNNING:
                    task.result = _result(task.check, outcome, monotonic() - task.started)
                    task.state = _DONE
                changed.notify_all()

    def spawn() -> None:
        threading.Thread(target=worker, name="doctor-check", daemon=True).start()

    def abandon(task: _Task, now: float, detail: str) -> None:
        started = task.started if task.state == _RUNNING else now
        task.result = _result(task.check, skip(detail, reason="timeout"), now - started)
        task.state = _DONE

    for _ in range(max(1, min(workers, len(tasks)))):
        spawn()
    with changed:
        while any(task.state != _DONE for task in tasks):
            now = monotonic()
            if now >= overall:
                pending.clear()
                for task in tasks:
                    if task.state == _PENDING:
                        abandon(task, now, "not run: the doctor reached its overall time limit")
                    elif task.state == _RUNNING:
                        abandon(task, now, "stopped at the doctor's overall time limit; result unknown")
                break
            wake = overall
            for task in tasks:
                if task.state != _RUNNING:
                    continue
                limit = task.check.timeout if task.check.timeout is not None else timeout
                if now >= task.started + limit:
                    abandon(task, now, f"timed out after {limit:g} s; result unknown")
                    spawn()
                else:
                    wake = min(wake, task.started + limit)
            changed.wait(max(0.0, wake - monotonic()))
    return [task.result for task in tasks if task.result is not None]


def parallel_map(function: Callable[[T], R], items: Iterable[T], workers: int = 8) -> list[R]:
    """Run short, internally bounded probes side by side; results keep the input order."""
    work = list(items)
    results: list[Any] = [None] * len(work)
    errors: list[BaseException] = []
    queue = collections.deque(enumerate(work))
    lock = threading.Lock()

    def worker() -> None:
        while True:
            with lock:
                if not queue:
                    return
                index, item = queue.popleft()
            try:
                results[index] = function(item)
            except Exception as exc:  # noqa: BLE001 - re-raised in the caller's thread below
                with lock:
                    errors.append(exc)

    threads = [threading.Thread(target=worker, name="doctor-probe", daemon=True)
               for _ in range(min(workers, len(work)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return results
