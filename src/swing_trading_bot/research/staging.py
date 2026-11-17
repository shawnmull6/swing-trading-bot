"""Resumable, budgeted historical acquisition and atomic dataset publication (Step
16.16E; RESEARCH_DATA §3.0b).

**Batches.** The acquisition plan is cut into :class:`AcquisitionBatch` units (one
logical request each, deterministic id, explicit symbol and date boundaries, its own
page cap and attempt budget). A batch is fetched to its last page and staged as one
unit, or not at all.

**Staging store** (one directory per acquisition-plan fingerprint)::

    <root>/store.json              plan id, fingerprint and batch ids (write-once)
    <root>/blobs/<sha256>.json     response bodies, content-addressed (write-once, 0444)
    <root>/batches/<batch_id>.json committed batch records (write-once, 0444)
    <root>/tmp/                    in-flight files; never trusted
    <root>/session.lock            present while a session runs
    <root>/quarantine/<label>/     where an explicit recovery moves a crashed session's files

Every file is written into ``tmp/``, fsynced and then *linked* to its final name: a link
fails if the name exists, so nothing is ever overwritten. A batch exists only once its
record is linked; its record carries its own digest and every page's SHA-256, and is
re-verified on every open, resume and assembly (a changed byte is ``TAMPERED_BATCH``;
nothing is repaired or re-fetched automatically). A store refuses a plan with another
fingerprint, and a record naming another plan or an unknown batch.

**Interrupted sessions.** A session holds ``session.lock``. A lock or in-flight file
left by a crash makes the store refuse new sessions and publication until
:meth:`StagingStore.recover` has moved them to ``quarantine/`` (never deleted, never
reused) and re-verified every committed batch.

**Budgets** (never raised here): the session's attempt cap and deadline are the plan's
(``REQUEST_BUDGET`` / ``SESSION_DEADLINE_S``); every attempt — first pages, later pages
and retries — is counted before it is sent. A batch starts only if its whole attempt
budget and :data:`BATCH_TIME_RESERVE_S` still fit, so a session never stops halfway
through a batch for budget reasons; it ends ``INCOMPLETE`` and a later, separately
authorized session resumes. Retries: 429, 503 or a timeout only, at most
``READ_ATTEMPTS`` per page. 401/403 or an entitlement refusal stop at once.

**Publication.** :func:`publish_dataset` assembles the dataset only when every batch the
gates make eligible is staged and verified (and no ineligible one is), then calls
``write_dataset`` (stage, fsync, one rename, never overwrite) and re-loads it. It never
runs a backtest, a walk-forward or a holdout evaluation, and selects no parameter.

Requests go through an injected ``fetch`` callable; this module imports no transport,
reads no credential and never opens a ledger.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    InvalidBrokerDataError,
    MarketDataEntitlementError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.mapping import session_from_raw
from swing_trading_bot.broker.models import BarAdjustment, DailyBar
from swing_trading_bot.domain.calendar import TradingSession
from swing_trading_bot.research.acquisition import (
    BATCH_TIME_RESERVE_S,
    READ_ATTEMPTS,
    AcquisitionBatch,
    AcquisitionPlan,
    BatchKind,
    CorporateActionWindow,
    LogicalRequest,
    ineligible_reason,
    request_accounting,
)
from swing_trading_bot.research.corporate_actions import (
    CorporateActionImport,
    CorporateActionParseError,
    FilterSemantics,
    build_records,
    decode,
    parse_page,
    select_window,
)
from swing_trading_bot.research.dataset import (
    ACQUISITION_COMPLETE,
    DatasetFormatError,
    Evidence,
    SymbolIdentity,
    TickerSpan,
    bars_from_alpaca_pages,
    canonical,
    encode_bar,
    encode_calendar,
    encode_corporate_actions,
    encode_identity,
    encode_json,
    encode_jsonl,
    load_dataset,
    quality_report,
    refuse_ledger_location,
    sha256,
    write_dataset,
)
from swing_trading_bot.research.universe import ResearchUniverse

STORE_FILE: Final = "store.json"
LOCK_FILE: Final = "session.lock"
BACKOFF_BASE: Final = timedelta(milliseconds=500)
BACKOFF_MAX: Final = timedelta(seconds=4)
ATTEMPTS_PER_PAGE: Final = READ_ATTEMPTS
_TICKER_CHANGES: Final = {"META": ("FB", date(2022, 6, 9))}


class StagingError(RuntimeError):
    """The store cannot be used safely. ``code`` names the reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class IncompatiblePlanError(StagingError):
    """The store, or a record in it, belongs to a different acquisition plan."""


class InterruptedSessionError(StagingError):
    """A session lock or in-flight file is present: recover explicitly first."""


class StagingIntegrityError(StagingError):
    """A committed batch no longer verifies (tampered, truncated or foreign)."""


class IncompleteAcquisitionError(StagingError):
    """Publication refused: an eligible batch is missing or an ineligible one is staged."""


# ===================================================================== eligibility


def _request(plan: AcquisitionPlan, batch: AcquisitionBatch) -> LogicalRequest:
    return next(r for r in plan.requests if r.request_id == batch.request_id)


# ===================================================================== files


def _page_token(kind: BatchKind, body: bytes) -> str | None:
    """The next-page token of a staged body; any unexpected shape raises."""
    try:
        doc = decode(body)
    except CorporateActionParseError as exc:
        raise InvalidBrokerDataError(f"response is not JSON: {exc.code}") from None
    if kind is BatchKind.CALENDAR:
        if not isinstance(doc, list):
            raise InvalidBrokerDataError("a calendar response must be a JSON list")
        return None
    if not isinstance(doc, dict) or "next_page_token" not in doc:
        raise InvalidBrokerDataError("response has no next_page_token")
    token = doc["next_page_token"]
    if token is not None and (not isinstance(token, str) or not token):
        raise InvalidBrokerDataError("next_page_token must be null or a non-empty string")
    return token


@dataclass(frozen=True, slots=True)
class BatchRecord:
    batch_id: str
    pages: tuple[str, ...]
    """Page body SHA-256s, in order."""
    attempts: int
    requested_at: datetime
    received_at: datetime
    raw: Mapping[str, Any] = field(repr=False, compare=False)


class StagingStore:
    """A verified handle on one plan's staging directory (see the module docstring)."""

    def __init__(self, root: Path, plan: AcquisitionPlan) -> None:
        self.root = root
        self.plan = plan
        self._batches = {b.batch_id: b for b in plan.batches}

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def create(cls, root: Path, plan: AcquisitionPlan, *, created_at: datetime) -> StagingStore:
        refuse_ledger_location(root)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            raise StagingError("STORE_EXISTS", str(root)) from None
        for name in ("blobs", "batches", "tmp", "quarantine"):
            (root / name).mkdir(mode=0o700)
        store = cls(root, plan)
        store._write_new(root / STORE_FILE, encode_json({
            "kind": "acquisition-staging", "plan_fingerprint": plan.fingerprint,
            "capability_probe_fingerprint": plan.capability_probe_fingerprint,
            "universe_id": plan.universe_id, "created_at": created_at.isoformat(),
            "batches": sorted(store._batches)}))  # fmt: skip
        return store

    @classmethod
    def open(cls, root: Path, plan: AcquisitionPlan) -> StagingStore:
        refuse_ledger_location(root)
        try:
            doc = json.loads((root / STORE_FILE).read_bytes())
        except (OSError, ValueError):
            raise StagingError("STORE_UNREADABLE", str(root)) from None
        if not isinstance(doc, dict) or doc.get("plan_fingerprint") != plan.fingerprint:
            raise IncompatiblePlanError("INCOMPATIBLE_PLAN", "the store was made for another "
                                        "acquisition plan; never mixed")  # fmt: skip
        if doc.get("batches") != sorted(b.batch_id for b in plan.batches):
            raise IncompatiblePlanError("INCOMPATIBLE_PLAN", "batch list differs")
        return cls(root, plan)

    def interruption(self) -> list[str]:
        """Names left by a session that did not finish cleanly (empty when none)."""
        left = sorted(p.name for p in (self.root / "tmp").iterdir())
        if (self.root / LOCK_FILE).exists():
            left.insert(0, LOCK_FILE)
        return left

    def recover(self, *, label: str) -> tuple[list[str], list[str]]:
        """Explicit recovery: move the lock and in-flight files to ``quarantine/<label>``
        (never deleted, never reused) and re-verify every committed batch (raises on any
        tampering). Returns (moved names, verified batch ids)."""
        if not label or "/" in label or label.startswith("."):
            raise StagingError("INVALID_LABEL", label)
        moved = self.interruption()
        target = self.root / "quarantine" / label
        target.mkdir(mode=0o700)  # FileExistsError: a label is used once
        for name in moved:
            src = self.root / LOCK_FILE if name == LOCK_FILE else self.root / "tmp" / name
            os.rename(src, target / name)
        return moved, sorted(self.committed())

    # ---------------------------------------------------------------- writing
    def _write_new(self, final: Path, data: bytes) -> bool:
        """Write ``data`` to ``final`` without ever replacing it. Returns False when an
        identical file already exists (content-addressed blobs); raises otherwise."""
        tmp = self.root / "tmp" / f"{final.name}.{os.urandom(8).hex()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.chmod(0o444)
            try:
                os.link(tmp, final)
            except FileExistsError:
                if final.parent.name == "blobs" and final.read_bytes() == data:
                    return False
                raise StagingError("REFUSED_OVERWRITE", final.name) from None
            return True
        finally:
            tmp.unlink(missing_ok=True)

    def _commit(self, batch: AcquisitionBatch, pages: Sequence[bytes], attempts: int,
                requested_at: datetime, received_at: datetime) -> None:  # fmt: skip
        req = _request(self.plan, batch)
        entries = []
        for body in pages:
            digest = sha256(body)
            self._write_new(self.root / "blobs" / f"{digest}.json", body)
            entries.append({"sha256": digest, "bytes": len(body)})
        record: dict[str, object] = {
            "batch_id": batch.batch_id, "request_id": batch.request_id,
            "kind": batch.kind.value, "plan_fingerprint": self.plan.fingerprint,
            "host": req.host.value, "path": req.path, "params": dict(req.params),
            "symbols": list(batch.symbols), "request_start": str(batch.request_start),
            "request_end": str(batch.request_end), "coverage_start": str(batch.coverage_start),
            "coverage_end": str(batch.coverage_end), "pages": entries, "attempts": attempts,
            "requested_at": requested_at.isoformat(), "received_at": received_at.isoformat(),
        }  # fmt: skip
        record["record_sha256"] = sha256(canonical(record).encode())
        self._write_new(self.root / "batches" / f"{batch.batch_id}.json", encode_json(record))

    # ---------------------------------------------------------------- reading
    def _tampered(self, batch_id: str, why: str) -> StagingIntegrityError:
        return StagingIntegrityError("TAMPERED_BATCH", f"{batch_id}: {why}")

    def _verify(self, batch: AcquisitionBatch, path: Path) -> BatchRecord:
        try:
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            raise self._tampered(batch.batch_id, "record unreadable") from None
        if not isinstance(doc, dict):
            raise self._tampered(batch.batch_id, "record is not an object")
        body = {k: v for k, v in doc.items() if k != "record_sha256"}
        if doc.get("record_sha256") != sha256(canonical(body).encode()):
            raise self._tampered(batch.batch_id, "record digest mismatch")
        if doc.get("plan_fingerprint") != self.plan.fingerprint:
            raise IncompatiblePlanError("INCOMPATIBLE_PLAN", f"{batch.batch_id} record")
        req = _request(self.plan, batch)
        expected = {"batch_id": batch.batch_id, "request_id": batch.request_id,
                    "kind": batch.kind.value, "host": req.host.value, "path": req.path,
                    "params": dict(req.params), "symbols": list(batch.symbols),
                    "request_start": str(batch.request_start),
                    "request_end": str(batch.request_end),
                    "coverage_start": str(batch.coverage_start),
                    "coverage_end": str(batch.coverage_end)}  # fmt: skip
        if any(doc.get(k) != v for k, v in expected.items()):
            raise self._tampered(batch.batch_id, "record differs from the plan")
        pages = doc.get("pages")
        attempts = doc.get("attempts")
        if (not isinstance(pages, list) or not 1 <= len(pages) <= batch.max_pages
                or not isinstance(attempts, int) or isinstance(attempts, bool)
                or not len(pages) <= attempts <= batch.max_attempts):  # fmt: skip
            raise self._tampered(batch.batch_id, "page or attempt count out of bounds")
        digests: list[str] = []
        tokens: list[str | None] = []
        for entry in pages:
            digest = entry.get("sha256") if isinstance(entry, dict) else None
            blob_path = self.root / "blobs" / f"{digest}.json"
            if not isinstance(digest, str) or not blob_path.is_file():
                raise self._tampered(batch.batch_id, "page missing")
            blob = blob_path.read_bytes()
            if sha256(blob) != digest or len(blob) != entry.get("bytes"):
                raise self._tampered(batch.batch_id, f"page {digest[:12]} changed")
            try:
                tokens.append(_page_token(batch.kind, blob))
            except InvalidBrokerDataError:
                raise self._tampered(batch.batch_id, "page unparseable") from None
            digests.append(digest)
        chain = tokens[:-1]
        if tokens[-1] is not None or None in chain or len(set(chain)) != len(chain):
            raise self._tampered(batch.batch_id, "page chain broken")
        try:
            requested = datetime.fromisoformat(str(doc.get("requested_at")))
            received = datetime.fromisoformat(str(doc.get("received_at")))
        except ValueError:
            raise self._tampered(batch.batch_id, "timestamps invalid") from None
        return BatchRecord(batch.batch_id, tuple(digests), attempts, requested, received, doc)

    def committed(self) -> dict[str, BatchRecord]:
        """Every committed batch, verified. An unknown record fails closed."""
        out: dict[str, BatchRecord] = {}
        for path in sorted((self.root / "batches").iterdir()):
            batch = self._batches.get(path.name.removesuffix(".json"))
            if batch is None or not path.name.endswith(".json"):
                raise IncompatiblePlanError("UNKNOWN_BATCH_RECORD", path.name)
            out[batch.batch_id] = self._verify(batch, path)
        return out

    def pages(self, record: BatchRecord) -> list[bytes]:
        return [(self.root / "blobs" / f"{d}.json").read_bytes() for d in record.pages]


# ===================================================================== sessions

#: ``fetch(batch, request, page_token)`` -> the raw response body. Raises the broker
#: read errors (``RateLimitedError``, ``ServiceUnavailableError``, ...).
Fetch = Callable[[AcquisitionBatch, LogicalRequest, "str | None"], bytes]


class _BatchStop(Exception):  # noqa: N818 - control flow
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Budget:
    def __init__(self, max_attempts: int, deadline: timedelta,
                 monotonic: Callable[[], timedelta]) -> None:  # fmt: skip
        self.max_attempts, self.deadline, self.monotonic = max_attempts, deadline, monotonic
        self.started = monotonic()
        self.used = 0

    def remaining(self) -> timedelta:
        return self.deadline - (self.monotonic() - self.started)

    def take(self) -> None:
        """Count one attempt BEFORE it is sent; refuse it when nothing is left."""
        if self.remaining() <= timedelta(0):
            raise _BatchStop("SESSION_DEADLINE_EXHAUSTED")
        if self.used >= self.max_attempts:
            raise _BatchStop("SESSION_BUDGET_EXHAUSTED")
        self.used += 1


@dataclass(frozen=True, slots=True)
class SessionReport:
    status: str
    """COMPLETE (every eligible batch staged), INCOMPLETE (resumable) or STOPPED."""
    stop_code: str | None
    attempts_used: int
    elapsed: timedelta
    committed: tuple[str, ...]
    already_committed: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    pending: tuple[str, ...]
    failed_batch: str | None = None


def _retryable(exc: BrokerError) -> bool:
    if isinstance(exc, RateLimitedError):
        return True
    return isinstance(exc, ServiceUnavailableError) and (exc.kind == "timeout"
                                                         or exc.status_code == 503)  # fmt: skip


def _fetch_page(batch: AcquisitionBatch, req: LogicalRequest, token: str | None, *,
                fetch: Fetch, budget: _Budget, sleep: Callable[[timedelta], None],
                spent: int) -> tuple[bytes, int]:  # fmt: skip
    """One page with bounded retries. Returns (body, attempts used by the batch so far)."""
    for page_attempt in range(1, ATTEMPTS_PER_PAGE + 1):
        if spent >= batch.max_attempts:
            raise _BatchStop("BATCH_BUDGET_EXHAUSTED")
        budget.take()
        spent += 1
        try:
            body = fetch(batch, req, token)
        except (AuthenticationError, MarketDataEntitlementError):
            raise _BatchStop("AUTH_OR_ENTITLEMENT_REFUSED") from None
        except InvalidBrokerDataError:
            raise _BatchStop("INVALID_RESPONSE") from None
        except BrokerError as exc:
            if not _retryable(exc):
                raise _BatchStop(f"HTTP_{exc.status_code}" if exc.status_code
                                 else "BROKER_ERROR") from None  # fmt: skip
            if page_attempt == ATTEMPTS_PER_PAGE:
                raise _BatchStop("RETRIES_EXHAUSTED") from None
            delay = min(BACKOFF_BASE * 2 ** (page_attempt - 1), BACKOFF_MAX)
            if budget.remaining() <= delay:
                raise _BatchStop("SESSION_DEADLINE_EXHAUSTED") from None
            sleep(delay)
            continue
        if not isinstance(body, bytes):
            raise _BatchStop("INVALID_RESPONSE")
        return body, spent
    raise _BatchStop("RETRIES_EXHAUSTED")  # pragma: no cover - loop always returns/raises


def _fetch_batch(batch: AcquisitionBatch, req: LogicalRequest, fetch: Fetch, budget: _Budget,
                 sleep: Callable[[timedelta], None]) -> tuple[list[bytes], int]:  # fmt: skip
    """Every page of one batch, or ``_BatchStop`` (nothing of the batch is kept)."""
    pages: list[bytes] = []
    seen: set[str] = set()
    token: str | None = None
    attempts = 0
    while True:
        if len(pages) >= batch.max_pages:
            raise _BatchStop("PAGE_CAP_EXCEEDED")
        body, attempts = _fetch_page(batch, req, token, fetch=fetch, budget=budget,
                                     sleep=sleep, spent=attempts)  # fmt: skip
        try:
            nxt = _page_token(batch.kind, body)
        except InvalidBrokerDataError:
            raise _BatchStop("INVALID_RESPONSE") from None
        pages.append(body)
        if nxt is None:
            return pages, attempts
        if nxt in seen:
            raise _BatchStop("REPEATED_PAGE_TOKEN")
        seen.add(nxt)
        token = nxt


def run_session(
    store: StagingStore,
    fetch: Fetch,
    *,
    gates: Mapping[str, bool],
    monotonic: Callable[[], timedelta],
    sleep: Callable[[timedelta], None],
    now: Callable[[], datetime],
    max_attempts: int | None = None,
    deadline_s: int | None = None,
) -> SessionReport:
    """Stage every eligible batch not yet staged, in plan order, within one session's
    budget. Never raises a budget above the plan's; never publishes."""
    plan = store.plan
    attempts_cap = plan.session_max_attempts if max_attempts is None else max_attempts
    deadline = plan.session_deadline_s if deadline_s is None else deadline_s
    if attempts_cap > plan.session_max_attempts or deadline > plan.session_deadline_s:
        raise StagingError("BUDGET_ABOVE_PLAN", "session budgets are never raised")
    left = store.interruption()
    if left:
        raise InterruptedSessionError("INTERRUPTED_SESSION", ", ".join(left))
    lock = store.root / LOCK_FILE
    try:
        os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:  # pragma: no cover - raced another session
        raise InterruptedSessionError("SESSION_LOCKED") from None
    # From here an unexpected exception leaves the lock: recovery is then explicit.
    try:
        done = store.committed()
    except StagingError:
        lock.unlink()
        raise
    budget = _Budget(attempts_cap, timedelta(seconds=deadline), monotonic)
    committed: list[str] = []
    skipped: list[tuple[str, str]] = []
    pending: list[str] = []
    stop: str | None = None
    failed: str | None = None
    for batch in plan.batches:
        if batch.batch_id in done:
            continue
        reason = ineligible_reason(batch, gates)
        if reason is not None:
            skipped.append((batch.batch_id, reason))
            continue
        if stop is not None:
            pending.append(batch.batch_id)
            continue
        if (attempts_cap - budget.used < batch.max_attempts
                or budget.remaining() < timedelta(seconds=BATCH_TIME_RESERVE_S)):  # fmt: skip
            stop = "SESSION_BUDGET_RESERVED"
            pending.append(batch.batch_id)
            continue
        requested_at = now()
        try:
            pages, attempts = _fetch_batch(batch, _request(plan, batch), fetch, budget, sleep)
        except _BatchStop as exc:
            stop, failed = exc.code, batch.batch_id
            pending.append(batch.batch_id)
            continue
        store._commit(batch, pages, attempts, requested_at, now())
        committed.append(batch.batch_id)
    lock.unlink()
    status = ("COMPLETE" if not pending else "INCOMPLETE"
              if stop == "SESSION_BUDGET_RESERVED" else "STOPPED")  # fmt: skip
    elapsed = max(timedelta(0), timedelta(seconds=deadline) - budget.remaining())
    return SessionReport(status, stop, budget.used, elapsed,
                         tuple(committed), tuple(sorted(done)), tuple(skipped),
                         tuple(pending), failed)  # fmt: skip


# ===================================================================== assembly


@dataclass(frozen=True, slots=True)
class PublishedDataset:
    root: Path
    digest: str
    report: Mapping[str, Any]
    corporate_actions: CorporateActionImport


def _calendar(trading: Sequence[bytes], settlement: Sequence[bytes], start: date,
              end: date) -> tuple[TradingSession, ...]:  # fmt: skip
    """TRADING rows with their settlement dates, cross-checked against the SETTLEMENT
    calendar (rows selected by settlement date). Never derived by weekday arithmetic."""
    rows = [r for body in trading for r in decode(body)]
    try:
        sessions = [s for s in (session_from_raw(r) for r in rows)
                    if start <= s.session_date <= end]  # fmt: skip
        settles: dict[date, date] = {}
        for body in settlement:
            for r in decode(body):
                trade = date.fromisoformat(str(r["date"]))
                settles[trade] = date.fromisoformat(str(r["settlement_date"]))
    except (InvalidBrokerDataError, KeyError, TypeError, ValueError) as exc:
        raise StagingError("CALENDAR_INVALID", str(exc)) from None
    known = {s.session_date for s in sessions}
    for s in sessions:
        if s.settlement_date is None:
            raise StagingError("SETTLEMENT_DATE_MISSING", str(s.session_date))
        if start <= s.settlement_date <= end and settles.get(s.session_date) != s.settlement_date:
            raise StagingError("SETTLEMENT_MISMATCH", str(s.session_date))
    stray = sorted(d for d in settles if start <= d <= end and d not in known)
    if stray:
        raise StagingError("SETTLEMENT_MISMATCH", f"not trading sessions: {stray[:3]}")
    if not sessions:
        raise StagingError("CALENDAR_INVALID", "no sessions")
    return tuple(sessions)


def _bars(store: StagingStore, plan: AcquisitionPlan, records: Mapping[str, BatchRecord],
          symbol: str, adjustment: BarAdjustment
          ) -> tuple[list[tuple[DailyBar, str, str]], str | None]:  # fmt: skip
    """(bar, source symbol, batch id) rows for one research symbol, or a reason."""
    rows: list[tuple[DailyBar, str, str]] = []
    for batch in plan.batches:
        if (batch.kind is not BatchKind.BARS or batch.symbols != (symbol,)
                or batch.batch_id not in records
                or dict(_request(plan, batch).params)["adjustment"] != adjustment):  # fmt: skip
            continue
        rec = records[batch.batch_id]
        source = dict(_request(plan, batch).params)["symbols"]
        try:
            got = bars_from_alpaca_pages(
                [decode(p) for p in store.pages(rec)], symbols=[source],
                start=batch.request_start, end=batch.request_end, feed="sip",
                adjustment=adjustment, received_at=rec.received_at)  # fmt: skip
        except (DatasetFormatError, CorporateActionParseError) as exc:
            return [], f"{batch.batch_id}: {exc}"
        rows += [(replace(b, symbol=symbol), source, batch.batch_id) for b in got[source]]
    rows.sort(key=lambda r: r[0].session_date)
    days = [r[0].session_date for r in rows]
    if len(set(days)) != len(days):
        return [], "the same session was returned under two tickers"
    return rows, None


def _identity(symbol: str, plan: AcquisitionPlan, *, used_fallback: bool) -> SymbolIdentity:
    spans: tuple[TickerSpan, ...]
    if used_fallback and symbol in _TICKER_CHANGES:
        old, changed = _TICKER_CHANGES[symbol]
        spans = (TickerSpan(old, plan.warmup_start, date.fromordinal(changed.toordinal() - 1)),
                 TickerSpan(symbol, changed, None))  # fmt: skip
    else:
        spans = (TickerSpan(symbol, plan.warmup_start, None),)
    return SymbolIdentity(symbol, spans, None, None)


def _corporate_actions(store: StagingStore, plan: AcquisitionPlan, *,
                       records: Mapping[str, BatchRecord], symbols: Sequence[str],
                       semantics: FilterSemantics,
                       attestations: Mapping[tuple[str, str], Sequence[Evidence]] | None,
                       ) -> CorporateActionImport:  # fmt: skip
    selections = []
    for batch in plan.batches:
        if batch.kind is not BatchKind.CORPORATE_ACTIONS or batch.batch_id not in records:
            continue
        window = CorporateActionWindow(
            batch.coverage_start, batch.coverage_end,
            (batch.coverage_start - batch.request_start).days,
            (batch.request_end - batch.coverage_end).days)  # fmt: skip
        try:
            events = [e for body in store.pages(records[batch.batch_id])
                      for e in parse_page(body, symbols).events]  # fmt: skip
        except CorporateActionParseError as exc:
            raise StagingError("CORPORATE_ACTIONS_UNPARSEABLE",
                               f"{batch.batch_id}: {exc.code}") from None  # fmt: skip
        selections.append(select_window(batch.batch_id, window, events, semantics,
                                        margin_days=plan.ca_pad_margin_days))  # fmt: skip
    return build_records(selections, symbols=symbols,
                         target=(plan.warmup_start, plan.research_end), semantics=semantics,
                         attestations=attestations)  # fmt: skip


def assemble_dataset(
    store: StagingStore,
    universe: ResearchUniverse,
    *,
    gates: Mapping[str, bool],
    semantics: FilterSemantics,
    dataset_id: str,
    created_at: datetime,
    attestations: Mapping[tuple[str, str], Sequence[Evidence]] | None = None,
) -> tuple[dict[str, object], dict[str, bytes], CorporateActionImport]:
    """The dataset's manifest fields and files, built only from verified batches.
    Raises :class:`IncompleteAcquisitionError` (nothing built) unless every eligible
    batch is staged and no ineligible batch is."""
    plan = store.plan
    if (universe.universe_id, universe.fingerprint) != (
        plan.universe_id,
        plan.universe_fingerprint,
    ):
        raise IncompatiblePlanError("INCOMPATIBLE_PLAN", "universe differs from the plan")
    left = store.interruption()
    if left:
        raise InterruptedSessionError("INTERRUPTED_SESSION", ", ".join(left))
    records = store.committed()
    eligible = {b.batch_id for b in plan.batches if ineligible_reason(b, gates) is None}
    missing = sorted(eligible - set(records))
    foreign = sorted(set(records) - eligible)
    if missing or foreign:
        raise IncompleteAcquisitionError(
            "INCOMPLETE_ACQUISITION", f"missing {missing}; ineligible staged {foreign}"
        )
    accounting = request_accounting(plan, gates)
    cal = {dict(_request(plan, b).params).get("date_type"): b for b in plan.batches
           if b.kind is BatchKind.CALENDAR}  # fmt: skip
    trading, settlement = cal["TRADING"], cal["SETTLEMENT"]
    sessions = _calendar(store.pages(records[trading.batch_id]),
                         store.pages(records[settlement.batch_id]),
                         plan.warmup_start, plan.research_end)  # fmt: skip
    files: dict[str, bytes] = {
        "calendar.json": encode_calendar(sessions, start=plan.warmup_start,
                                         end=plan.research_end, source="alpaca:/v2/calendar",
                                         batch_id=trading.batch_id)}  # fmt: skip
    findings: list[dict[str, str]] = []
    identities: list[SymbolIdentity] = []
    symbols = universe.symbols
    for sym in symbols:
        fallback = any(b.batch_id in records and b.symbols == (sym,) and b.requires_failed
                       for b in plan.batches)  # fmt: skip
        identities.append(_identity(sym, plan, used_fallback=fallback))
        for adjustment in ("raw", "split"):
            rows, problem = _bars(store, plan, records, sym, adjustment)
            if problem is not None:
                findings.append({"symbol": sym, "issue": f"BARS_{adjustment.upper()}_INVALID",
                                 "detail": problem})  # fmt: skip
            if rows:
                files[f"bars/{sym}.{adjustment}.jsonl"] = encode_jsonl(
                    [encode_bar(b, source_symbol=src, batch_id=bid) for b, src, bid in rows]
                )
    files["identity.json"] = encode_identity(identities)
    imported = _corporate_actions(
        store,
        plan,
        records=records,
        symbols=symbols,
        semantics=semantics,
        attestations=attestations,
    )
    for sym, rec in imported.records.items():
        files[f"corporate_actions/{sym}.json"] = encode_corporate_actions(rec)
    acquisitions = []
    for batch in plan.batches:
        if batch.batch_id not in records:
            continue
        staged, req = records[batch.batch_id], _request(plan, batch)
        acquisitions.append(
            {
                "batch_id": batch.batch_id,
                "kind": batch.kind.value.lower(),
                "provider": "alpaca",
                "endpoint": f"GET {req.path}",
                "params": dict(req.params),
                "requested_at": staged.requested_at.isoformat(),
                "received_at": staged.received_at.isoformat(),
                "request_count": staged.attempts,
            }
        )
    meta: dict[str, object] = {
        "dataset_id": dataset_id, "created_at": created_at.isoformat(),
        "description": "Alpaca SIP daily bars, calendars and provider-reported corporate "
        "actions (Step 16.16E pipeline)",
        "market_timezone": "America/New_York", "feed": "sip",
        "research_window": {"start": str(plan.research_start), "end": str(plan.research_end)},
        "warmup_start": str(plan.warmup_start),
        "universe": {"id": universe.universe_id, "policy": universe.policy.value,
                     "symbols": list(symbols),
                     "survivorship_bias_free": universe.survivorship_bias_free},
        "acquisitions": acquisitions,
        "acquisition_report": {
            "acquisition_state": ACQUISITION_COMPLETE,
            "required_batches": {
                bid: {"pages_sha256": list(records[bid].pages),
                      "attempts": records[bid].attempts}
                for bid in sorted(eligible)},
            "request_accounting": {
                "expected_requests": accounting.expected_requests,
                "page_cap_requests": accounting.page_cap_requests,
                "worst_case_attempts": accounting.worst_case_attempts,
                "attempts_used": sum(records[bid].attempts for bid in eligible)},
            "plan_fingerprint": plan.fingerprint,
            "capability_probe_fingerprint": plan.capability_probe_fingerprint,
            "gates": dict(sorted(gates.items())),
            "ca_date_semantics": {"status": imported.semantics.status.value,
                                  "candidates": [r.value for r in imported.semantics.candidates],
                                  "verified_for": list(imported.semantics.scope),
                                  "evidence": imported.semantics.evidence},
            "ca_padding_days": {"before": plan.ca_pad_before_days,
                                "after": plan.ca_pad_after_days,
                                "margin": plan.ca_pad_margin_days},
            "corporate_action_claims": {s: dict(v) for s, v in imported.claims.items()},
            "findings": findings + [f.to_dict() for f in imported.findings],
            "limitations": list(plan.limitations),
        },
    }  # fmt: skip
    return meta, files, imported


def publish_dataset(
    store: StagingStore,
    universe: ResearchUniverse,
    destination: Path,
    *,
    gates: Mapping[str, bool],
    semantics: FilterSemantics,
    dataset_id: str,
    created_at: datetime,
    attestations: Mapping[tuple[str, str], Sequence[Evidence]] | None = None,
) -> PublishedDataset:
    """Assemble from verified batches, publish atomically (never overwriting), re-load
    and report quality. Runs no backtest and touches no holdout view."""
    meta, files, imported = assemble_dataset(
        store, universe, gates=gates, semantics=semantics, dataset_id=dataset_id,
        created_at=created_at, attestations=attestations)  # fmt: skip
    digest = write_dataset(destination, meta, files)
    report = quality_report(load_dataset(destination))
    return PublishedDataset(destination, digest, report, imported)


__all__ = [
    "ATTEMPTS_PER_PAGE",
    "LOCK_FILE",
    "STORE_FILE",
    "BatchRecord",
    "Fetch",
    "IncompatiblePlanError",
    "IncompleteAcquisitionError",
    "InterruptedSessionError",
    "PublishedDataset",
    "SessionReport",
    "StagingError",
    "StagingIntegrityError",
    "StagingStore",
    "assemble_dataset",
    "ineligible_reason",
    "publish_dataset",
    "run_session",
]
