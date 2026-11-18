"""Authorized historical acquisition sessions, explicit recovery and explicit publication
(Step 16.17A, Tasks 2-6; RUNBOOK §16).

**Operations** — each is a separate, explicit invocation; none starts another:

* :func:`run_authorized_session` — ONE live session (the only operation that reads
  credentials or sends a request). It refuses unless the plan fingerprint equals the
  authorized one, the core probe gates passed, the staging store verifies, no earlier
  session was interrupted, the authorized session count is not used up and at least one
  eligible batch is still missing. Credentials are read only after all of that.
  Batches, budgets, retries, pagination and staging are
  :func:`~swing_trading_bot.research.staging.run_session`'s (120 attempts / 300 s, a
  batch starts only if its whole budget and 30 s fit, at most once per batch, nothing
  of a failed batch staged). It never runs a second session and never publishes.
* **Session limit (Step 16.17C.3).** ``--new-store`` seals the authorization's
  ``max_sessions`` into the store's write-once ``store.json`` (``session_limit``, covered
  by ``store_sha256``). ``--resume`` must name exactly that limit, every journal start
  must agree with it, and every started session counts — an interrupted one too, before
  and after :func:`recover` (which never touches ``store.json`` or a start record).
* **Session accounting (Step 16.17C.3.1).** Every started session also *consumes* one
  of the store's slot files (``StagingStore.consume_slot``: deleted and fsynced right
  after its journal start, before any request). Resume, recovery and publication
  require the journal and the consumed slots to agree
  (:func:`check_session_accounting`); deleting the newest start and report therefore
  leaves a consumed slot with no session (``SESSION_ACCOUNTING_INCONSISTENT``) instead
  of a free one, and recovery can only consume a slot, never restore one.
* :func:`recover` — explicit crash recovery: quarantines the lock and in-flight files
  (``StagingStore.recover``), re-verifies every staged batch and closes the journal
  entry of the interrupted session. Sends nothing.
* :func:`publish` — explicit publication: every eligible batch staged and verified, no
  ineligible batch, the journal closed and recorded under the same gates; then
  ``write_dataset`` (atomic, never overwriting) and ``load_dataset`` (schema and
  checksums). The result must be ``ACQUISITION_COMPLETE``. It never creates an
  attestation, never asserts research readiness, runs no backtest and opens no holdout
  view. Corporate-action date semantics are the 16.16D observation (unverified), so no
  provider-based completeness claim is made.
* :func:`status` — offline inspection of the store and journal.

**Locations.** Only ``<research root>/staging/<first 16 hex of the plan fingerprint>/``
and ``<research root>/<dataset_id>/`` are written; the CLI fixes the research root to
``<repo>/data/research``. No operation takes a ledger path or any other path argument.

**Ledger isolation.** While an operation runs, :func:`research_guard` arms a CPython
audit hook that refuses every ``sqlite3`` call and any open, listing, rename, removal or
permission change under the research root's parent (``data/``, which holds the ledger)
outside the research root itself — before the operating system is asked.

**Journal.** ``<staging>/sessions/NN.start.json`` is written (write-once, 0444) just
before a session's first request and ``NN.report.json`` when it ends. A start without a
report is an interrupted session: every further session and publication is refused
until :func:`recover`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from swing_trading_bot.research.acquisition import (
    CAPABILITY_PROBE_PLAN_ID,
    AcquisitionPlan,
    build_acquisition_plan,
    ineligible_reason,
    request_accounting,
)
from swing_trading_bot.research.corporate_actions import OBSERVED_16_16D
from swing_trading_bot.research.dataset import (
    DatasetError,
    DatasetState,
    assess_readiness,
    load_dataset,
    sha256,
)
from swing_trading_bot.research.staging import (
    SESSION_LIMIT_RANGE,
    Fetch,
    SessionReport,
    StagingError,
    StagingStore,
    publish_dataset,
    run_session,
)
from swing_trading_bot.research.universe import PILOT_UNIVERSE, ResearchUniverse

#: The acquisition plan reviewed in Step 16.16E.1 (RESEARCH_DATA §3.0a).
APPROVED_ACQUISITION_PLAN_FINGERPRINT: Final = (
    "6ca96c0601a404b19bd5adac44bc1e46141a9febd9c66db7b9438a7380ae5a3f"
)
#: The capability probe executed in Step 16.16D (RESEARCH_DATA §3.0).
APPROVED_PROBE_PLAN_FINGERPRINT: Final = (
    "1bfd4451e9df58e7a7b6de64c072bafc8298c3e509db05af15cd592386909931"
)
ENABLE_VARIABLE: Final = "RUN_ALPACA_HISTORY_ACQUISITION"
STAGING_DIR: Final = "staging"
JOURNAL_DIR: Final = "sessions"
#: Checks whose failure stops acquisition (their ``on_failure`` says stop).
CORE_GATES: Final = ("CAL-DEPTH", "SETTLE-CYCLE", "SIP-DEPTH", "PAGINATION")
#: Checks that decide conditional batches; each must have a definite outcome.
CONDITIONAL_GATES: Final = ("CA-SHAPE", "TICKER-IDENTITY")
_LABEL: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DATASET_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_EVIDENCE_NAME: Final = re.compile(r"^history-probe-[0-9]{8}T[0-9]{6}Z$")
_VERIFIED: Final = "VERIFIED"
_FAILED: Final = "FAILED"
_META_MISSING: Final = "META_PRE_CHANGE_BARS_MISSING"


class RunnerRefusedError(RuntimeError):
    """The operation was refused before (or instead of) doing anything. ``code`` is fixed
    text; no path, credential or response content is part of it."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ===================================================================== ledger guard


class LedgerAccessRefused(BaseException):
    """A guarded operation was attempted (``BaseException``: nothing may swallow it)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


GUARDED_PATH_EVENTS: Final = frozenset({
    "open", "os.listdir", "os.scandir", "os.remove", "os.rename", "os.replace", "os.chmod",
    "os.chown", "os.chflags", "os.truncate", "os.utime", "os.mkdir", "os.rmdir", "os.link",
    "os.symlink", "shutil.rmtree", "shutil.copyfile", "shutil.copymode", "shutil.copystat",
    "shutil.move", "glob.glob", "os.walk",
})  # fmt: skip


@dataclass(frozen=True, slots=True)
class ResearchGuard:
    """Refuses ``sqlite3`` and path operations under ``protected`` outside ``allowed``."""

    protected: str
    allowed: str

    def check(self, event: str, args: tuple[object, ...]) -> None:
        if event.startswith("sqlite3."):
            raise LedgerAccessRefused("SQLITE_REFUSED")
        if event not in GUARDED_PATH_EVENTS:
            return
        for value in args[:2]:
            if not isinstance(value, (str, bytes, os.PathLike)):
                continue
            try:
                path = os.path.abspath(os.fsdecode(value))  # string arithmetic, no syscall
            except (TypeError, ValueError):
                continue
            inside = path == self.protected or path.startswith(self.protected + os.sep)
            allowed = path == self.allowed or path.startswith(self.allowed + os.sep)
            if inside and not allowed:
                raise LedgerAccessRefused("PROTECTED_PATH_REFUSED")


_ARMED: list[ResearchGuard] = []
_HOOKED: list[bool] = []


def _audit(event: str, args: tuple[object, ...]) -> None:
    for guard in tuple(_ARMED):
        guard.check(event, args)


@contextlib.contextmanager
def research_guard(guard: ResearchGuard) -> Iterator[ResearchGuard]:
    """Arm ``guard`` for the ``with`` body (the process-wide hook is installed once)."""
    if not _HOOKED:
        sys.addaudithook(_audit)
        _HOOKED.append(True)
    _ARMED.append(guard)
    try:
        yield guard
    finally:
        _ARMED.remove(guard)


# ===================================================================== locations


@dataclass(frozen=True, slots=True)
class ResearchPaths:
    """The approved research root. Its parent (``data/``, holding the ledger) is guarded
    during every operation; only the root's subtree may be touched."""

    root: Path

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or os.path.normpath(self.root) != str(self.root):
            raise RunnerRefusedError("RESEARCH_ROOT_NOT_ABSOLUTE")
        if self.root.name != "research":
            raise RunnerRefusedError("RESEARCH_ROOT_NOT_APPROVED")

    @property
    def guard(self) -> ResearchGuard:
        return ResearchGuard(str(self.root.parent), str(self.root))

    def check(self) -> None:
        """The root (if present) and the staging directory are real directories."""
        for p in (self.root, self.root / STAGING_DIR):
            if p.is_symlink() or (p.exists() and not p.is_dir()):
                raise RunnerRefusedError("RESEARCH_PATH_NOT_A_DIRECTORY", p.name)

    def staging(self, plan: AcquisitionPlan) -> Path:
        return self.root / STAGING_DIR / plan.fingerprint[:16]

    def dataset(self, dataset_id: str) -> Path:
        if not _DATASET_ID.match(dataset_id) or dataset_id == STAGING_DIR:
            raise RunnerRefusedError("INVALID_DATASET_ID")
        return self.root / dataset_id


# ===================================================================== probe gates


@dataclass(frozen=True, slots=True)
class ProbeGates:
    evidence: str
    """The probe evidence directory name (``history-probe-<UTC>Z``)."""
    summary_sha256: str
    gates: Mapping[str, bool]


def gates_from_probe_summary(summary: Mapping[str, Any], *,
                             probe_fingerprint: str) -> dict[str, bool]:  # fmt: skip
    """Gate outcomes from a capability-probe ``summary.json``. A check that is not
    clearly passed or failed is left out (``ineligible_reason`` then fails closed)."""
    plan = summary.get("plan")
    if (not isinstance(plan, dict) or plan.get("id") != CAPABILITY_PROBE_PLAN_ID
            or plan.get("fingerprint") != probe_fingerprint):  # fmt: skip
        raise RunnerRefusedError("PROBE_PLAN_MISMATCH")
    execution = summary.get("execution")
    if not isinstance(execution, dict) or execution.get("status") != "COMPLETED":
        raise RunnerRefusedError("PROBE_NOT_COMPLETED")
    acquisition = summary.get("acquisition")
    if not isinstance(acquisition, dict) or acquisition.get("started") is not False:
        raise RunnerRefusedError("PROBE_SUMMARY_INVALID")
    checks = summary.get("checks")
    if not isinstance(checks, dict):
        raise RunnerRefusedError("PROBE_SUMMARY_INVALID")
    gates: dict[str, bool] = {}
    for name in (*CORE_GATES, *CONDITIONAL_GATES):
        result = checks.get(name)
        if not isinstance(result, dict):
            continue
        outcome, codes = result.get("outcome"), result.get("codes")
        if name == "TICKER-IDENTITY":  # evaluated as VERIFIED, with a code when FB is needed
            if outcome == _VERIFIED and codes == []:
                gates[name] = True
            elif outcome == _VERIFIED and codes == [_META_MISSING]:
                gates[name] = False
        elif outcome == _VERIFIED and codes == []:
            gates[name] = True
        elif outcome == _FAILED:
            gates[name] = False
    return gates


def load_probe_gates(evidence_root: Path, name: str, plan: AcquisitionPlan) -> ProbeGates:
    """Read ``<evidence_root>/<name>/summary.json`` (no symlinks) and derive the gates."""
    if not _EVIDENCE_NAME.match(name):
        raise RunnerRefusedError("INVALID_PROBE_EVIDENCE_NAME")
    directory = evidence_root / name
    path = directory / "summary.json"
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise RunnerRefusedError("PROBE_EVIDENCE_UNAVAILABLE")
    blob = path.read_bytes()
    try:
        summary = json.loads(blob)
    except ValueError:
        raise RunnerRefusedError("PROBE_SUMMARY_INVALID") from None
    if not isinstance(summary, dict):
        raise RunnerRefusedError("PROBE_SUMMARY_INVALID")
    gates = gates_from_probe_summary(summary, probe_fingerprint=plan.capability_probe_fingerprint)
    return ProbeGates(name, sha256(blob), gates)


def check_gates(gates: Mapping[str, bool]) -> None:
    """Acquisition may start only when every core gate passed and each conditional gate
    has a definite outcome."""
    failed = [g for g in CORE_GATES if gates.get(g) is not True]
    if failed:
        raise RunnerRefusedError("CORE_GATE_NOT_PASSED", ",".join(failed))
    unknown = [g for g in CONDITIONAL_GATES if not isinstance(gates.get(g), bool)]
    if unknown:
        raise RunnerRefusedError("CONDITIONAL_GATE_UNDETERMINED", ",".join(unknown))


# ===================================================================== journal


def _write_once(path: Path, doc: Mapping[str, object]) -> None:
    blob = (json.dumps(doc, sort_keys=True, indent=1) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    path.chmod(0o444)
    fd = os.open(path.parent, os.O_RDONLY)  # the new entry itself is durable too
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class Journal:
    directory: Path

    def ensure(self) -> None:
        if self.directory.is_symlink():
            raise RunnerRefusedError("JOURNAL_NOT_A_DIRECTORY")
        self.directory.mkdir(mode=0o700, exist_ok=True)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            raise RunnerRefusedError("JOURNAL_UNREADABLE", path.name) from None
        if not isinstance(doc, dict):
            raise RunnerRefusedError("JOURNAL_UNREADABLE", path.name)
        return doc

    def entries(self) -> list[tuple[int, dict[str, Any], dict[str, Any] | None]]:
        """(session number, start record, report or None), in order."""
        if not self.directory.is_dir():
            return []
        names = sorted(p.name for p in self.directory.iterdir())
        starts: dict[int, Path] = {}
        reports: dict[int, Path] = {}
        for name in names:
            m = re.fullmatch(r"([0-9]{2})\.(start|report)\.json", name)
            if m is None:
                raise RunnerRefusedError("JOURNAL_UNKNOWN_FILE", name)
            (starts if m.group(2) == "start" else reports)[int(m.group(1))] = self.directory / name
        if set(reports) - set(starts) or sorted(starts) != list(range(1, len(starts) + 1)):
            raise RunnerRefusedError("JOURNAL_INCONSISTENT")
        return [(n, self._read(starts[n]), self._read(reports[n]) if n in reports else None)
                for n in sorted(starts)]  # fmt: skip

    def unreported(self) -> list[int]:
        return [n for n, _, report in self.entries() if report is None]

    def start(self, n: int, doc: Mapping[str, object]) -> None:
        _write_once(self.directory / f"{n:02d}.start.json", doc)

    def report(self, n: int, doc: Mapping[str, object]) -> None:
        _write_once(self.directory / f"{n:02d}.report.json", doc)


# ===================================================================== sessions


@dataclass(frozen=True, slots=True)
class Authorization:
    """One explicitly authorized session: the plan it names, a label recorded in the
    journal (used once) and the total number of sessions the authorization allows."""

    plan_fingerprint: str
    label: str
    max_sessions: int

    def __post_init__(self) -> None:
        if not _LABEL.match(self.label):
            raise RunnerRefusedError("INVALID_AUTHORIZATION_LABEL")
        low, high = SESSION_LIMIT_RANGE
        if type(self.max_sessions) is not int or not low <= self.max_sessions <= high:
            raise RunnerRefusedError("INVALID_MAX_SESSIONS")


#: ``connect(remaining_seconds)`` -> (fetch, transport summary callable). Called only
#: after every offline check passed; the live wiring reads credentials here.
Connect = Callable[[Callable[[], timedelta]], tuple[Fetch, Callable[[], Mapping[str, object]]]]


def _report_dict(report: SessionReport) -> dict[str, object]:
    return {"status": report.status, "stop_code": report.stop_code,
            "stop_reason": None if report.stop_reason is None else report.stop_reason.value,
            "stop_detail": report.stop_detail,
            "resumable": report.status != "COMPLETE",
            "attempts_used": report.attempts_used,
            "elapsed_s": round(report.elapsed.total_seconds(), 3),
            "committed": list(report.committed),
            "already_committed": list(report.already_committed),
            "skipped": [list(s) for s in report.skipped], "pending": list(report.pending),
            "failed_batch": report.failed_batch}  # fmt: skip


def _eligible(plan: AcquisitionPlan, gates: Mapping[str, bool]) -> list[str]:
    return [b.batch_id for b in plan.batches if ineligible_reason(b, gates) is None]


def _check_plan(plan: AcquisitionPlan, fingerprint: str) -> None:
    if plan.fingerprint != fingerprint:
        raise RunnerRefusedError("PLAN_FINGERPRINT_MISMATCH")


def _check_journal_gates(entries: Sequence[tuple[int, dict[str, Any], Any]],
                         probe: ProbeGates) -> None:  # fmt: skip
    for _, start, _ in entries:
        if (start.get("probe_summary_sha256") != probe.summary_sha256
                or start.get("gates") != dict(sorted(probe.gates.items()))):  # fmt: skip
            raise RunnerRefusedError("GATES_CHANGED_BETWEEN_SESSIONS")


def check_session_limit(store: StagingStore,
                        entries: Sequence[tuple[int, dict[str, Any], Any]],
                        authorization: Authorization) -> int:  # fmt: skip
    """The store's sealed session limit, enforced: the request must name it exactly,
    every journal start must agree with it, and the started sessions (interrupted ones
    included) must leave room for one more. Returns the limit."""
    limit = store.session_limit
    if limit is None:
        raise RunnerRefusedError("SESSION_LIMIT_MISSING")
    if authorization.max_sessions != limit:
        raise RunnerRefusedError("SESSION_LIMIT_MISMATCH")
    if any(start.get("max_sessions") != limit for _, start, _ in entries):
        raise RunnerRefusedError("JOURNAL_SESSION_LIMIT_MISMATCH")
    used = check_session_accounting(store, entries)
    if max(len(entries), len(used)) >= limit:
        raise RunnerRefusedError("SESSION_LIMIT_REACHED")
    return limit


def check_session_accounting(
    store: StagingStore, entries: Sequence[tuple[int, dict[str, Any], Any]]
) -> list[int]:
    """The journal and the store's consumed slots must tell the same story: no more
    sessions than slots, no consumed slot without a journal start (the newest session's
    records deleted), and no closed session whose slot is still present (a restored
    slot). Only an unreported (interrupted) session may still hold its slot: it was
    interrupted between its start record and the consumption. Returns the consumed
    slots."""
    if store.session_limit is None:
        raise RunnerRefusedError("SESSION_LIMIT_MISSING")
    used = set(store.consumed_slots())
    started = {n for n, _, _ in entries}
    closed = {n for n, _, report in entries if report is not None}
    if len(entries) > store.session_limit or not used <= started or not closed <= used:
        raise RunnerRefusedError("SESSION_ACCOUNTING_INCONSISTENT")
    return sorted(used)


def _open_for_resume(
    root: Path, plan: AcquisitionPlan, authorization: Authorization, probe: ProbeGates
) -> tuple[StagingStore, list[tuple[int, dict[str, Any], dict[str, Any] | None]],
           Mapping[str, object]]:  # fmt: skip
    """The verified store, journal and staged batches a resumed session builds on."""
    if not root.is_dir():
        raise RunnerRefusedError("STORE_MISSING", "a resumed session needs the store")
    store = StagingStore.open(root, plan)
    entries = Journal(root / JOURNAL_DIR).entries()
    if any(report is None for _, _, report in entries):
        raise RunnerRefusedError("PREVIOUS_SESSION_INTERRUPTED", "recover explicitly first")
    if store.interruption():
        raise RunnerRefusedError("INTERRUPTED_SESSION", "recover explicitly first")
    if not entries:
        raise RunnerRefusedError("NO_SESSION_JOURNAL", "store not made by a session")
    check_session_limit(store, entries, authorization)
    if any(start.get("authorization") == authorization.label for _, start, _ in entries):
        raise RunnerRefusedError("AUTHORIZATION_LABEL_REUSED")
    _check_journal_gates(entries, probe)
    return store, entries, store.committed()  # re-verifies every staged batch


def run_authorized_session(
    *,
    paths: ResearchPaths,
    plan: AcquisitionPlan,
    authorization: Authorization,
    probe: ProbeGates,
    resume: bool,
    connect: Connect,
    monotonic: Callable[[], timedelta],
    sleep: Callable[[timedelta], None],
    now: Callable[[], datetime],
) -> dict[str, object]:
    """One authorized session (see the module docstring). Returns its journal report."""
    _check_plan(plan, authorization.plan_fingerprint)
    check_gates(probe.gates)
    gates = dict(sorted(probe.gates.items()))
    with research_guard(paths.guard):
        paths.check()
        root = paths.staging(plan)
        store: StagingStore | None = None
        entries: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
        committed: Mapping[str, object] = {}
        if resume:
            store, entries, committed = _open_for_resume(root, plan, authorization, probe)
        elif root.exists():
            raise RunnerRefusedError("STORE_EXISTS", "resume it explicitly or review it")
        pending = [b for b in _eligible(plan, gates) if b not in committed]
        if not pending:
            raise RunnerRefusedError("NOTHING_TO_FETCH")
        deadline = timedelta(seconds=plan.session_deadline_s)
        clock_start: list[timedelta] = []

        def remaining() -> timedelta:
            """Seconds left, measured from just before ``run_session`` starts its own
            budget clock (so the guard is never more lenient than the budget)."""
            if not clock_start:
                return timedelta(0)
            return deadline - (monotonic() - clock_start[0])

        fetch, transport_summary = connect(remaining)  # credentials are read here, last
        if store is None:
            paths.root.mkdir(mode=0o700, exist_ok=True)
            (paths.root / STAGING_DIR).mkdir(mode=0o700, exist_ok=True)
            store = StagingStore.create(root, plan, created_at=now(),
                                        session_limit=authorization.max_sessions)  # fmt: skip
        journal = Journal(root / JOURNAL_DIR)
        journal.ensure()
        n = len(entries) + 1
        journal.start(n, {
            "session": n, "authorization": authorization.label,
            "max_sessions": authorization.max_sessions, "plan_fingerprint": plan.fingerprint,
            "probe_evidence": probe.evidence, "probe_summary_sha256": probe.summary_sha256,
            "gates": gates, "started_at": now().isoformat(),
            "pending_batches": len(pending), "verified_batches": len(committed),
        })  # fmt: skip
        store.consume_slot(n)  # durable before the first request; never given back
        clock_start.append(monotonic())
        report = run_session(store, fetch, gates=gates, monotonic=monotonic, sleep=sleep,
                             now=now)  # fmt: skip
        transport = dict(transport_summary())
        out: dict[str, object] = {
            "session": n, "authorization": authorization.label,
            "ended_at": now().isoformat(), **_report_dict(report),
            "remaining_after": len(pending) - len(report.committed),
            "transport": transport, "published": False,
        }  # fmt: skip
        journal.report(n, out)
    return out


def recover(*, paths: ResearchPaths, plan: AcquisitionPlan, fingerprint: str, label: str,
            now: Callable[[], datetime]) -> dict[str, object]:  # fmt: skip
    """Explicit recovery of an interrupted session (sends nothing)."""
    _check_plan(plan, fingerprint)
    if not _LABEL.match(label):
        raise RunnerRefusedError("INVALID_RECOVERY_LABEL")
    with research_guard(paths.guard):
        paths.check()
        root = paths.staging(plan)
        if not root.is_dir():
            raise RunnerRefusedError("STORE_MISSING")
        store = StagingStore.open(root, plan)
        journal = Journal(root / JOURNAL_DIR)
        entries = journal.entries()
        unreported = [n for n, _, report in entries if report is None]
        if not store.interruption() and not unreported:
            raise RunnerRefusedError("NOTHING_TO_RECOVER")
        used = check_session_accounting(store, entries)
        if (root / "quarantine" / label).exists():
            raise RunnerRefusedError("RECOVERY_LABEL_USED")
        moved, verified = store.recover(label=label)
        for n in unreported:
            if n not in used:  # interrupted before its slot was consumed: consume it now
                store.consume_slot(n)
            journal.report(n, {"session": n, "status": "INTERRUPTED_RECOVERED",
                               "recovery_label": label, "recovered_at": now().isoformat(),
                               "quarantined": moved, "verified_batches": len(verified),
                               "published": False})  # fmt: skip
    return {"recovered": True, "label": label, "quarantined": moved,
            "verified_batches": len(verified), "sessions_closed": unreported}  # fmt: skip


def publish(*, paths: ResearchPaths, plan: AcquisitionPlan, fingerprint: str,
            universe: ResearchUniverse, probe: ProbeGates, dataset_id: str,
            now: Callable[[], datetime]) -> dict[str, object]:  # fmt: skip
    """Explicit publication (see the module docstring). No attestation is created, no
    readiness is asserted, no backtest runs and no holdout view is opened."""
    _check_plan(plan, fingerprint)
    check_gates(probe.gates)
    gates = dict(sorted(probe.gates.items()))
    destination = paths.dataset(dataset_id)
    with research_guard(paths.guard):
        paths.check()
        root = paths.staging(plan)
        if not root.is_dir():
            raise RunnerRefusedError("STORE_MISSING")
        if destination.exists():
            raise RunnerRefusedError("DATASET_EXISTS")
        store = StagingStore.open(root, plan)
        entries = Journal(root / JOURNAL_DIR).entries()
        if not entries:
            raise RunnerRefusedError("NO_SESSION_JOURNAL")
        if any(report is None for _, _, report in entries):
            raise RunnerRefusedError("PREVIOUS_SESSION_INTERRUPTED", "recover explicitly first")
        check_session_accounting(store, entries)
        _check_journal_gates(entries, probe)
        published = publish_dataset(
            store, universe, destination, gates=gates, semantics=OBSERVED_16_16D,
            dataset_id=dataset_id, created_at=now(), attestations=None,
        )  # fmt: skip
        readiness = assess_readiness(load_dataset(destination))
        if readiness.state is DatasetState.ACQUISITION_INCOMPLETE:
            raise RunnerRefusedError("PUBLISHED_DATASET_NOT_ACQUISITION_COMPLETE",
                                     "do not use it; review the manifest")  # fmt: skip
    report = published.report
    return {
        "dataset_id": dataset_id, "digest": published.digest,
        "state": readiness.state.value, "research_ready": readiness.research_ready,
        "readiness": readiness.to_dict(),
        "accepted": report.get("accepted"), "rejected": report.get("rejected"),
        "corporate_action_claims": report.get("corporate_action_claims"),
        "ca_date_semantics": OBSERVED_16_16D.status.value,
        "attestations_created": False, "backtest_run": False, "holdout_opened": False,
    }  # fmt: skip


def status(*, paths: ResearchPaths, plan: AcquisitionPlan, fingerprint: str,
           probe: ProbeGates | None) -> dict[str, object]:  # fmt: skip
    """Offline: store verification, interruption, journal, pending batches."""
    _check_plan(plan, fingerprint)
    with research_guard(paths.guard):
        paths.check()
        root = paths.staging(plan)
        if not root.is_dir():
            return {"store": "ABSENT", "plan_fingerprint": plan.fingerprint}
        store = StagingStore.open(root, plan)
        interruption = store.interruption()
        committed = store.committed()
        entries = Journal(root / JOURNAL_DIR).entries()
        consumed = store.consumed_slots()
        try:
            check_session_accounting(store, entries)
            accounting = "CONSISTENT"
        except RunnerRefusedError as exc:
            accounting = exc.code
    out: dict[str, object] = {
        "store": "PRESENT", "plan_fingerprint": plan.fingerprint,
        "session_limit": store.session_limit, "sessions_started": len(entries),
        "slots_consumed": consumed, "session_accounting": accounting,
        "verified_batches": sorted(committed), "interruption": interruption,
        "sessions": [{"session": n, "authorization": s.get("authorization"),
                      "status": None if r is None else r.get("status"),
                      "stop_code": None if r is None else r.get("stop_code"),
                      "attempts_used": None if r is None else r.get("attempts_used")}
                     for n, s, r in entries],
    }  # fmt: skip
    if probe is not None:
        eligible = _eligible(plan, probe.gates)
        out["pending_batches"] = [b for b in eligible if b not in committed]
        out["ineligible_staged"] = sorted(set(committed) - set(eligible))
        acct = request_accounting(plan, probe.gates)
        out["accounting"] = {"batches": acct.batches, "expected": acct.expected_requests,
                             "page_cap": acct.page_cap_requests,
                             "worst_case": acct.worst_case_attempts,
                             "sessions_expected": acct.sessions_expected,
                             "sessions_worst_case": acct.sessions_worst_case}  # fmt: skip
    return out


# ===================================================================== CLI

REPO: Final = Path(__file__).resolve().parents[3]


def _repo_paths() -> tuple[ResearchPaths, Path]:
    if not (REPO / "pyproject.toml").is_file() or not (REPO / ".git").exists():
        raise RunnerRefusedError("REPOSITORY_NOT_FOUND")
    return ResearchPaths(REPO / "data" / "research"), REPO / "contract-evidence"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m swing_trading_bot.history_acquisition",
        description="Historical acquisition (authorized use only).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the plan fingerprint and accounting (offline)")
    for name in ("status", "session", "recover", "publish"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--plan-fingerprint", required=True)
        if name in ("status", "session", "publish"):
            cmd.add_argument("--probe-evidence", required=name != "status")
        if name == "session":
            cmd.add_argument("--authorization", required=True)
            cmd.add_argument("--max-sessions", type=int, required=True)
            mode = cmd.add_mutually_exclusive_group(required=True)
            mode.add_argument("--new-store", action="store_true")
            mode.add_argument("--resume", action="store_true")
        if name == "recover":
            cmd.add_argument("--label", required=True)
        if name == "publish":
            cmd.add_argument("--dataset-id", required=True)
    return p


def _live_connect(plan: AcquisitionPlan, environ: Mapping[str, str]) -> Connect:
    def connect(remaining: Callable[[], timedelta]
                ) -> tuple[Fetch, Callable[[], Mapping[str, object]]]:  # fmt: skip
        from swing_trading_bot.history_acquisition.transport import (  # noqa: PLC0415
            LiveConnectionRefusedError,
            connect_live,
            transport_summary,
        )

        try:
            fetch, guarded = connect_live(plan, environ, remaining=remaining,
                                          monotonic=_monotonic, wall_clock=_now)  # fmt: skip
        except LiveConnectionRefusedError as exc:
            raise RunnerRefusedError(exc.code) from None
        return fetch, lambda: transport_summary(fetch, guarded)

    return connect


def _monotonic() -> timedelta:  # pragma: no cover - live clock
    import time  # noqa: PLC0415

    return timedelta(seconds=time.monotonic())


def _sleep(delay: timedelta) -> None:  # pragma: no cover - live clock
    import time  # noqa: PLC0415

    time.sleep(delay.total_seconds())


def _now() -> datetime:
    return datetime.now(UTC)


def main(argv: Sequence[str] | None = None,
         environ: Mapping[str, str] | None = None) -> int:  # fmt: skip
    """CLI entry point. Exit 0: done; 1: the session stopped or is incomplete; 2: refused."""
    env = os.environ if environ is None else environ
    args = _parser().parse_args(argv)
    plan = build_acquisition_plan()
    try:
        if plan.fingerprint != APPROVED_ACQUISITION_PLAN_FINGERPRINT:
            raise RunnerRefusedError("PLAN_DRIFT", "the code no longer builds the approved plan")
        if plan.capability_probe_fingerprint != APPROVED_PROBE_PLAN_FINGERPRINT:
            raise RunnerRefusedError("PROBE_PLAN_DRIFT")
        if args.command == "plan":
            acct = request_accounting(plan)
            out: dict[str, object] = {
                "plan_fingerprint": plan.fingerprint, "batches": len(plan.batches),
                "expected": acct.expected_requests, "page_cap": acct.page_cap_requests,
                "worst_case": acct.worst_case_attempts,
                "session_max_attempts": plan.session_max_attempts,
                "session_deadline_s": plan.session_deadline_s}  # fmt: skip
            sys.stdout.write(json.dumps(out, indent=1) + "\n")
            return 0
        if args.plan_fingerprint != APPROVED_ACQUISITION_PLAN_FINGERPRINT:
            raise RunnerRefusedError("PLAN_FINGERPRINT_NOT_AUTHORIZED")
        if args.command == "session" and env.get(ENABLE_VARIABLE) != "1":
            raise RunnerRefusedError("LIVE_SESSION_NOT_ENABLED")
        paths, evidence_root = _repo_paths()
        probe = (load_probe_gates(evidence_root, args.probe_evidence, plan)
                 if getattr(args, "probe_evidence", None) else None)  # fmt: skip
        code = 0
        if args.command == "status":
            out = status(paths=paths, plan=plan, fingerprint=args.plan_fingerprint, probe=probe)
        elif args.command == "recover":
            out = recover(paths=paths, plan=plan, fingerprint=args.plan_fingerprint,
                          label=args.label, now=_now)  # fmt: skip
        elif args.command == "publish":
            assert probe is not None  # noqa: S101 - argparse requires it
            out = publish(paths=paths, plan=plan, fingerprint=args.plan_fingerprint,
                          universe=PILOT_UNIVERSE, probe=probe, dataset_id=args.dataset_id,
                          now=_now)  # fmt: skip
        else:
            assert probe is not None  # noqa: S101 - argparse requires it
            out = run_authorized_session(
                paths=paths, plan=plan,
                authorization=Authorization(args.plan_fingerprint, args.authorization,
                                            args.max_sessions),
                probe=probe, resume=bool(args.resume), connect=_live_connect(plan, env),
                monotonic=_monotonic, sleep=_sleep, now=_now)  # fmt: skip
            code = 0 if out.get("status") == "COMPLETE" else 1
    except RunnerRefusedError as exc:
        sys.stdout.write(json.dumps({"refused": exc.code}) + "\n")
        return 2
    except (StagingError, DatasetError) as exc:
        code_name = getattr(exc, "code", type(exc).__name__)
        sys.stdout.write(json.dumps({"refused": str(code_name)}) + "\n")
        return 2
    sys.stdout.write(json.dumps(out, indent=1, default=str) + "\n")
    return code


__all__ = [
    "APPROVED_ACQUISITION_PLAN_FINGERPRINT",
    "APPROVED_PROBE_PLAN_FINGERPRINT",
    "CONDITIONAL_GATES",
    "CORE_GATES",
    "ENABLE_VARIABLE",
    "Authorization",
    "Journal",
    "LedgerAccessRefused",
    "ProbeGates",
    "ResearchGuard",
    "ResearchPaths",
    "RunnerRefusedError",
    "check_gates",
    "check_session_accounting",
    "check_session_limit",
    "gates_from_probe_summary",
    "load_probe_gates",
    "main",
    "publish",
    "recover",
    "research_guard",
    "run_authorized_session",
    "status",
]
