"""Resumable acquisition: batches, budgets, staging, recovery, tampering and atomic
publication (Step 16.16E, Tasks 2-6). A SYNTHETIC provider stands in for every request;
nothing here reaches a network, a credential or a ledger."""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    DataEntitlementKind,
    InvalidBrokerDataError,
    MarketDataEntitlementError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.research import staging
from swing_trading_bot.research.acquisition import (
    REQUEST_BUDGET,
    SESSION_DEADLINE_S,
    AcquisitionBatch,
    AcquisitionPlan,
    LogicalRequest,
    build_acquisition_plan,
    ineligible_reason,
    request_accounting,
    sessions_needed,
)
from swing_trading_bot.research.corporate_actions import (
    ACTION_TYPES,
    OBSERVED_16_16D,
    DateRole,
    FilterSemantics,
    SemanticsStatus,
)
from swing_trading_bot.research.dataset import (
    DatasetError,
    DatasetNotResearchReadyError,
    DatasetState,
    Evidence,
    EvidenceKind,
    load_dataset,
)
from swing_trading_bot.research.staging import (
    LOCK_FILE,
    Fetch,
    FetchRefusedError,
    IncompatiblePlanError,
    IncompleteAcquisitionError,
    InterruptedSessionError,
    PublishedDataset,
    SessionReport,
    StagingError,
    StagingIntegrityError,
    StagingStore,
    StopReason,
    assemble_dataset,
    publish_dataset,
    run_session,
)
from swing_trading_bot.research.universe import (
    AssetClass,
    MembershipPolicy,
    ResearchUniverse,
    UniverseMember,
)
from swing_trading_bot.strategy.walk_forward import HoldoutAccessError, SealedResearchData
from tests.support.research_fixture import synthetic_bars, synthetic_sessions

RESEARCH = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "research"
UNIVERSE = ResearchUniverse(
    "synthetic-2", MembershipPolicy.FIXED_PRESENT_DAY_PILOT,
    (UniverseMember("AAA", "Synthetic", AssetClass.EQUITY),
     UniverseMember("META", "Synthetic", AssetClass.EQUITY)), False, ("SYNTHETIC",))  # fmt: skip
WARMUP, START, END = date(2020, 7, 1), date(2021, 1, 1), date(2021, 6, 30)
SESSIONS = synthetic_sessions(date(2020, 5, 1), date(2021, 8, 31))
GATES = {"CAL-DEPTH": True, "SETTLE-CYCLE": True, "SIP-DEPTH": True, "PAGINATION": True,
         "CA-SHAPE": True, "TICKER-IDENTITY": True}  # fmt: skip
#: Verified for every action type (a hypothetical fully-discriminating probe).
VERIFIED = FilterSemantics(
    SemanticsStatus.LIVE_VERIFIED, (DateRole.PROCESS_DATE,), "test", tuple(sorted(ACTION_TYPES))
)
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def plan() -> AcquisitionPlan:
    return build_acquisition_plan(UNIVERSE, warmup_start=WARMUP, research_start=START,
                                  research_end=END)  # fmt: skip


def _json(obj: object) -> bytes:
    def enc(o: object) -> str:
        if isinstance(o, Decimal):
            return f"@@{o}@@"
        raise TypeError(o)

    return json.dumps(obj, default=enc).replace('"@@', "").replace('@@"', "").encode()


def dividend(pid: str, sym: str, ex: date, pay_lag: int = 6) -> dict[str, object]:
    pay = ex + timedelta(days=pay_lag)
    return {"id": pid, "symbol": sym, "cusip": "0", "ex_date": str(ex),
            "record_date": str(ex + timedelta(days=1)), "payable_date": str(pay),
            "process_date": str(pay), "rate": Decimal("0.25"), "special": False,
            "foreign": False}  # fmt: skip


@dataclass
class Clock:
    t: timedelta = timedelta(0)
    per_call: timedelta = timedelta(milliseconds=10)
    slept: list[timedelta] = field(default_factory=list)

    def monotonic(self) -> timedelta:
        self.t += self.per_call
        return self.t

    def sleep(self, d: timedelta) -> None:
        self.slept.append(d)
        self.t += d


@dataclass
class Provider:
    """Serves calendars, bars and corporate actions like the documented endpoints. The
    corporate-action filter uses the process date (the hypothesis being tested)."""

    page_size: int = 10_000
    ca_events: list[dict[str, object]] = field(default_factory=lambda: [
        dividend("aaa-1", "AAA", date(2020, 12, 10), pay_lag=30),  # seen by both CA batches
        dividend("aaa-2", "AAA", date(2021, 2, 5))])  # fmt: skip
    script: dict[str, list[BaseException]] = field(default_factory=dict)
    """request id -> errors raised by its next attempts, in order."""
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    on_call: Callable[[], None] | None = None

    def __call__(self, batch: AcquisitionBatch, req: LogicalRequest, token: str | None) -> bytes:
        self.calls.append((req.request_id, token))
        if self.on_call is not None:
            self.on_call()
        queued = self.script.get(req.request_id)
        if queued:
            raise queued.pop(0)
        p = dict(req.params)
        if req.path == "/v2/calendar":
            lo, hi = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
            key = "date" if p["date_type"] == "TRADING" else "settlement"
            rows = [{"date": str(s.session_date), "open": "09:30", "close": "16:00",
                     "session_open": "0400", "session_close": "2000",
                     "settlement_date": str(s.settlement_date)} for s in SESSIONS
                    if s.settlement_date is not None and lo <= (
                        s.session_date if key == "date" else s.settlement_date) <= hi]  # fmt: skip
            return _json(rows)
        if req.path == "/v2/stocks/bars":
            sym = p["symbols"]
            days = [s.session_date for s in SESSIONS
                    if batch.request_start <= s.session_date <= batch.request_end]  # fmt: skip
            raw = [{"t": b.as_of.isoformat().replace("+00:00", "Z"), "o": b.open, "h": b.high,
                    "l": b.low, "c": b.close, "v": b.volume, "n": 100, "vw": b.close}
                   for b in synthetic_bars(sym, dates=days)]  # fmt: skip
            return self._page({"bars": {sym: raw}}, "bars", raw, token)
        lo_s, hi_s = p["start"], p["end"]
        items = [
            e
            for e in self.ca_events
            if lo_s <= str(e["process_date"]) <= hi_s and str(e["symbol"]) in p["symbols"]
        ]
        return self._page({"corporate_actions": {"cash_dividends": items}}, "ca", items, token)

    def _page(self, doc: dict[str, Any], kind: str, items: list[Any], token: str | None) -> bytes:
        start = int(token.split(":")[1]) if token else 0
        chunk = items[start : start + self.page_size]
        nxt = start + self.page_size
        if kind == "bars":
            doc = {"bars": dict.fromkeys(doc["bars"], chunk)}
        else:
            doc = {"corporate_actions": {"cash_dividends": chunk}}
        doc["next_page_token"] = f"{kind}:{nxt}" if nxt < len(items) else None
        return _json(doc)


def session(
    store: StagingStore, provider: Fetch, clock: Clock | None = None, **kw: Any
) -> SessionReport:
    c = clock or Clock()
    return run_session(store, provider, gates=kw.pop("gates", GATES), monotonic=c.monotonic,
                       sleep=c.sleep, now=lambda: NOW, **kw)  # fmt: skip


@pytest.fixture
def store(tmp_path: Path) -> StagingStore:
    return StagingStore.create(tmp_path / "staging", plan(), created_at=NOW)


def attest_aaa() -> dict[tuple[str, str], list[Evidence]]:
    review = Evidence(EvidenceKind.OPERATOR_REVIEW, "reviewer", "synthetic-review-1")
    return {("AAA", n): [review] for n in ("splits", "dividends", "other_events")}


def attest_all() -> dict[tuple[str, str], list[Evidence]]:
    review = Evidence(EvidenceKind.OPERATOR_REVIEW, "reviewer", "synthetic-review-2")
    return {(s, n): [review] for s in UNIVERSE.symbols
            for n in ("splits", "dividends", "other_events")}  # fmt: skip


def publish(store: StagingStore, dest: Path, **kw: Any) -> PublishedDataset:
    return publish_dataset(store, UNIVERSE, dest, gates=kw.pop("gates", GATES),
                           semantics=kw.pop("semantics", VERIFIED), dataset_id="synthetic-ds",
                           created_at=NOW, **kw)  # fmt: skip


# ============================================================ plan and batches
def test_batches_are_deterministic_bounded_and_explicit() -> None:
    a, b = plan(), plan()
    assert a.fingerprint == b.fingerprint and a.batches == b.batches
    ids = [x.batch_id for x in a.batches]
    assert len(set(ids)) == len(ids) == len(a.requests)
    for batch in a.batches:
        assert batch.batch_id.startswith(batch.request_id + ".")
        assert batch.request_start <= batch.coverage_start <= batch.coverage_end <= \
            batch.request_end  # fmt: skip
        assert batch.max_attempts <= batch.max_pages * a.max_attempts_per_page
        assert batch.max_attempts == batch.max_pages + a.batch_retry_reserve
    assert (a.session_max_attempts, a.session_deadline_s) == (REQUEST_BUDGET, SESSION_DEADLINE_S)
    fb = [x for x in a.batches if x.requires_failed]
    assert [x.requires_failed for x in fb] == [("TICKER-IDENTITY",)] * 2
    assert all("TICKER-IDENTITY" not in x.requires_passed for x in fb)


# ============================================================ sessions
def test_a_full_session_stages_every_eligible_batch(store: StagingStore) -> None:
    provider = Provider()
    report = session(store, provider)
    assert report.status == "COMPLETE" and report.stop_code is None
    eligible = [b for b in store.plan.batches if not b.requires_failed]
    assert sorted(report.committed) == sorted(b.batch_id for b in eligible)
    assert {r for _, r in report.skipped} == {"only if TICKER-IDENTITY failed"}
    assert report.attempts_used == len(provider.calls) == len(eligible)
    assert not (store.root / LOCK_FILE).exists() and store.interruption() == []
    for path in (store.root / "batches").iterdir():
        assert path.stat().st_mode & 0o777 == 0o444
    again = session(store, provider)  # nothing left: no request is sent
    assert again.committed == () and len(provider.calls) == len(eligible)


def test_pagination_is_counted_and_capped(store: StagingStore) -> None:
    ok = Provider(page_size=200)  # ~250 bars: 2 pages, within the cap of 2
    report = session(store, ok)
    assert report.status == "COMPLETE"
    bars = [c for c in ok.calls if c[0].startswith("C-BARS")]
    assert len(bars) == 8 and report.attempts_used == len(ok.calls)
    record = json.loads(next((store.root / "batches").glob("C-BARS-AAA-RAW.*")).read_bytes())
    assert len(record["pages"]) == 2 and record["attempts"] == 2


def test_a_page_beyond_the_cap_stops_and_stages_nothing_of_that_batch(
    store: StagingStore,
) -> None:
    report = session(store, Provider(page_size=100))  # 3 pages > cap 2
    assert report.status == "STOPPED" and report.stop_code == "PAGE_CAP_EXCEEDED"
    assert report.failed_batch is not None and report.failed_batch.startswith("C-BARS-AAA-RAW")
    assert not list((store.root / "batches").glob("C-BARS-*"))
    assert len(list((store.root / "batches").iterdir())) == 2  # the calendars only
    assert store.interruption() == []  # a handled stop leaves nothing in flight


def test_retries_only_for_429_503_timeout_and_are_counted(store: StagingStore) -> None:
    clock = Clock()
    provider = Provider(
        script={
            "C-CAL-TRADING": [
                RateLimitedError("429"),
                ServiceUnavailableError("503", status_code=503),
            ]
        }
    )
    report = session(store, provider, clock)
    assert report.status == "COMPLETE"
    assert provider.calls[:3] == [("C-CAL-TRADING", None)] * 3
    assert clock.slept == [timedelta(milliseconds=500), timedelta(seconds=1)]
    assert report.attempts_used == len(provider.calls)


@pytest.mark.parametrize(
    ("errors", "code"),
    [
        ([ServiceUnavailableError("t", kind="timeout")] * 3, "RETRIES_EXHAUSTED"),
        ([AuthenticationError("401", status_code=401)], "AUTH_OR_ENTITLEMENT_REFUSED"),
        ([ServiceUnavailableError("500", status_code=500)], "HTTP_500"),
        ([BrokerError("400", status_code=400)], "HTTP_400"),
    ],
)
def test_failures_stop_the_session(store: StagingStore, errors: list[BrokerError],
                                   code: str) -> None:  # fmt: skip
    provider = Provider(script={"C-CAL-TRADING": list(errors)})
    report = session(store, provider)
    assert report.status == "STOPPED" and report.stop_code == code
    assert report.committed == () and len(provider.calls) == len(errors)


@pytest.mark.parametrize(
    ("error", "code", "reason", "detail"),
    [
        (ServiceUnavailableError("t", kind="timeout"), "RETRIES_EXHAUSTED",
         StopReason.PAGE_RETRIES_EXHAUSTED, "TIMEOUT"),
        (AuthenticationError("401", status_code=401), "AUTH_OR_ENTITLEMENT_REFUSED",
         StopReason.CREDENTIALS_REJECTED, "HTTP_401"),
        (MarketDataEntitlementError("403", kind=DataEntitlementKind.RECENT_DATA_WINDOW),
         "AUTH_OR_ENTITLEMENT_REFUSED", StopReason.ENTITLEMENT_REFUSED, "HTTP_403"),
        (InvalidBrokerDataError("bad"), "INVALID_RESPONSE",
         StopReason.INVALID_PROVIDER_RESPONSE, "UNMAPPABLE_RESPONSE"),
        (BrokerError("404", status_code=404), "HTTP_404", StopReason.HTTP_ERROR, "HTTP_404"),
        (ServiceUnavailableError("n", kind="network"), "BROKER_ERROR",
         StopReason.TRANSPORT_ERROR, "ServiceUnavailableError"),
    ],
)  # fmt: skip
def test_every_stop_keeps_its_compatible_code_and_adds_a_specific_reason(
    store: StagingStore, error: BrokerError, code: str, reason: StopReason, detail: str
) -> None:
    provider = Provider(script={"C-CAL-TRADING": [error] * 3})
    report = session(store, provider)
    assert (report.stop_code, report.stop_reason, report.stop_detail) == (code, reason, detail)


def test_bare_byte_fetches_record_no_received_digest(store: StagingStore) -> None:
    session(store, Provider())
    for record in store.committed().values():
        assert set(StagingStore.received(record)) == {(None, None)}


def test_a_batch_never_exceeds_its_own_attempt_budget(store: StagingStore) -> None:
    """Bars: cap 2 pages, budget 4 attempts. Page 1 needs 3 attempts, page 2 fails once:
    the 5th attempt is refused before it is sent."""
    rid = "C-BARS-AAA-RAW"
    p = Provider(page_size=200, script={rid: [RateLimitedError("x"), RateLimitedError("x")]})
    original = p.__call__

    def fetch(batch: AcquisitionBatch, req: LogicalRequest, token: str | None) -> bytes:
        if req.request_id == rid and token is not None and not p.script.get("page2-done"):
            p.script["page2-done"] = []
            raise RateLimitedError("page 2")
        return original(batch, req, token)

    c = Clock()
    report = run_session(store, fetch, gates=GATES, monotonic=c.monotonic, sleep=c.sleep,
                         now=lambda: NOW)  # fmt: skip
    assert report.stop_code == "BATCH_BUDGET_EXHAUSTED"
    assert sum(1 for r, _ in p.calls if r == rid) == 3  # 4 attempts counted, 1 failed early


def test_budget_exhaustion_reserves_and_resume_completes(store: StagingStore) -> None:
    provider = Provider()
    first = session(store, provider, max_attempts=5)  # 2 calendars, then a 4-attempt batch
    assert first.status == "INCOMPLETE" and first.stop_code == "SESSION_BUDGET_RESERVED"
    assert first.attempts_used == 2 and len(first.committed) == 2
    assert first.pending  # resumable, nothing half-staged
    second = session(store, provider)
    assert second.status == "COMPLETE"
    assert set(second.already_committed) == set(first.committed)
    assert sum(1 for r, _ in provider.calls if r.startswith("C-CAL")) == 2  # not refetched


def test_the_deadline_is_enforced_before_a_batch_and_before_a_retry(store: StagingStore) -> None:
    slow = Clock(per_call=timedelta(0))
    provider = Provider()
    provider.on_call = lambda: setattr(slow, "t", slow.t + timedelta(seconds=140))
    report = session(store, provider, slow)
    assert report.status == "INCOMPLETE" and len(report.committed) == 2  # 280 s used

    clock = Clock(per_call=timedelta(0))
    retry = Provider(script={"C-CAL-TRADING": [ServiceUnavailableError("503", status_code=503)]})
    retry.on_call = lambda: setattr(clock, "t", clock.t + timedelta(seconds=299, milliseconds=900))
    other = StagingStore.create(store.root.parent / "other", plan(), created_at=NOW)
    report = session(other, retry, clock)
    assert report.stop_code == "SESSION_DEADLINE_EXHAUSTED" and clock.slept == []


# ============================================================ Step 16.17A.3: late responses
DEADLINE = timedelta(seconds=SESSION_DEADLINE_S)
LATE = "RESPONSE_AFTER_DEADLINE"


def _late_on(request_id: str, at: timedelta, clock: Clock, provider: Provider) -> Provider:
    """``provider`` whose ``request_id`` response completes at ``at`` (session time)."""
    inner = provider.on_call

    def on_call() -> None:
        if inner is not None:
            inner()
        if provider.calls[-1][0] == request_id:
            clock.t = at

    provider.on_call = on_call
    return provider


def _check_intact(store: StagingStore, committed: tuple[str, ...]) -> None:
    """Earlier batches still verify; nothing of the expired batch was written."""
    assert set(store.committed()) == set(committed)
    assert store.interruption() == [] and not (store.root / LOCK_FILE).exists()


@pytest.mark.parametrize(
    ("at", "accepted"),
    [(DEADLINE - timedelta(milliseconds=1), True),  # completes before: accepted
     (DEADLINE, False),  # exactly at the deadline: expired
     (DEADLINE + timedelta(seconds=1), False)],  # after: expired
)  # fmt: skip
def test_a_response_completing_at_or_after_the_deadline_is_never_accepted(
    store: StagingStore, at: timedelta, accepted: bool
) -> None:
    clock = Clock(per_call=timedelta(0))
    provider = _late_on("C-CAL-SETTLEMENT", at, clock, Provider())
    report = session(store, provider, clock)
    assert len(provider.calls) == report.attempts_used == 2  # the late attempt is counted
    assert clock.slept == []
    if accepted:
        assert report.status == "INCOMPLETE" and len(report.committed) == 2
        assert report.stop_reason is StopReason.SESSION_TIME_RESERVE_INSUFFICIENT
    else:
        assert report.status == "STOPPED"
        assert [b.split(".")[0] for b in report.committed] == ["C-CAL-TRADING"]
        assert report.stop_code == "SESSION_DEADLINE_EXHAUSTED"  # the compatible code
        assert report.stop_reason is StopReason.SESSION_DEADLINE_EXHAUSTED
        assert report.stop_detail == LATE
        assert str(report.failed_batch).startswith("C-CAL-SETTLEMENT")
    _check_intact(store, report.committed)


def test_a_later_page_after_the_deadline_stages_nothing_of_its_batch(
    store: StagingStore,
) -> None:
    clock = Clock(per_call=timedelta(0))
    provider = Provider(page_size=200)

    def second_page() -> None:
        if provider.calls[-1][1] is not None:  # a page token: page 2
            clock.t = DEADLINE

    provider.on_call = second_page
    report = session(store, provider, clock)
    assert (report.stop_reason, report.stop_detail) == (StopReason.SESSION_DEADLINE_EXHAUSTED, LATE)
    assert str(report.failed_batch).startswith("C-BARS")
    assert sum(1 for _, t in provider.calls if t is not None) == 1
    assert not any(b.startswith("C-BARS") for b in store.committed())
    _check_intact(store, report.committed)


@pytest.mark.parametrize(
    "error",
    [RateLimitedError("429"), ServiceUnavailableError("503", status_code=503),
     ServiceUnavailableError("t", kind="timeout")],
)  # fmt: skip
def test_no_retry_is_started_after_the_deadline(store: StagingStore, error: BrokerError) -> None:
    clock = Clock(per_call=timedelta(0))
    provider = _late_on("C-CAL-TRADING", DEADLINE, clock,
                        Provider(script={"C-CAL-TRADING": [error]}))  # fmt: skip
    report = session(store, provider, clock)
    assert len(provider.calls) == report.attempts_used == 1 and clock.slept == []
    assert (report.stop_reason, report.stop_detail) == (StopReason.SESSION_DEADLINE_EXHAUSTED, LATE)


@pytest.mark.parametrize(
    "error",
    [InvalidBrokerDataError("shape"),
     FetchRefusedError("UNEXPECTED_SHAPE", StopReason.INVALID_PROVIDER_RESPONSE)],
)  # fmt: skip
def test_validation_finishing_after_the_deadline_is_the_deadline_not_invalid_data(
    store: StagingStore, error: BrokerError
) -> None:
    """The response arrived in time; its validation ran past the deadline and failed."""
    clock = Clock(per_call=timedelta(0))
    provider = _late_on("C-CAL-SETTLEMENT", DEADLINE + timedelta(milliseconds=1), clock,
                        Provider(script={"C-CAL-SETTLEMENT": [error]}))  # fmt: skip
    report = session(store, provider, clock)
    assert report.stop_reason is StopReason.SESSION_DEADLINE_EXHAUSTED
    assert report.stop_code == "SESSION_DEADLINE_EXHAUSTED" and report.stop_detail == LATE
    _check_intact(store, report.committed)


def test_validation_and_canonicalization_running_past_the_deadline_are_rejected(
    store: StagingStore,
) -> None:
    """A fetch that received its response in time but returned (after validating and
    canonicalizing it) at the deadline: the page is not accepted."""
    clock = Clock(per_call=timedelta(0))
    provider = Provider()

    def fetch(batch: AcquisitionBatch, req: LogicalRequest, token: str | None) -> bytes:
        body = provider(batch, req, token)  # received with time to spare
        if req.request_id == "C-CAL-SETTLEMENT":
            clock.t = DEADLINE  # validation + canonicalization took the rest
        return body

    report = session(store, fetch, clock)
    assert (report.stop_reason, report.stop_detail) == (StopReason.SESSION_DEADLINE_EXHAUSTED, LATE)
    assert report.attempts_used == 2
    _check_intact(store, report.committed)


def test_authorization_refusals_keep_their_reason_even_when_late(store: StagingStore) -> None:
    clock = Clock(per_call=timedelta(0))
    provider = _late_on("C-CAL-TRADING", DEADLINE, clock, Provider(
        script={"C-CAL-TRADING": [AuthenticationError("401", status_code=401)]}))  # fmt: skip
    report = session(store, provider, clock)
    assert report.stop_reason is StopReason.CREDENTIALS_REJECTED


def test_a_batch_reaching_its_commit_after_the_deadline_is_not_staged(
    store: StagingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last page arrived in time; reading it (page-token parsing) ran past the
    deadline, so the completed batch is refused at the commit boundary."""
    clock = Clock(per_call=timedelta(0))
    real = staging._page_token
    seen: list[int] = []

    def slow_parse(kind: Any, body: bytes) -> str | None:
        seen.append(1)
        if len(seen) == 2:  # the second batch's (only) page
            clock.t = DEADLINE
        return real(kind, body)

    monkeypatch.setattr(staging, "_page_token", slow_parse)
    provider = Provider()
    report = session(store, provider, clock)
    assert report.status == "STOPPED" and report.stop_code == "SESSION_DEADLINE_EXHAUSTED"
    assert report.stop_reason is StopReason.SESSION_DEADLINE_EXHAUSTED
    assert report.stop_detail == "BATCH_COMMIT_AFTER_DEADLINE"
    assert len(report.committed) == 1 and str(report.failed_batch).startswith("C-CAL-SETTLEMENT")
    assert report.attempts_used == len(provider.calls) == 2
    _check_intact(store, report.committed)
    resumed = session(store, Provider())  # a later session resumes from where it stopped
    assert resumed.status == "COMPLETE" and resumed.already_committed == report.committed


def test_budgets_are_never_raised(store: StagingStore) -> None:
    with pytest.raises(StagingError, match="BUDGET_ABOVE_PLAN"):
        session(store, Provider(), max_attempts=REQUEST_BUDGET + 1)
    with pytest.raises(StagingError, match="BUDGET_ABOVE_PLAN"):
        session(store, Provider(), deadline_s=SESSION_DEADLINE_S + 1)


# ============================================================ interruption and recovery
def test_an_interrupted_session_fails_closed_until_explicit_recovery(
    store: StagingStore, tmp_path: Path,
) -> None:  # fmt: skip
    provider = Provider(script={"C-BARS-AAA-RAW": [KeyboardInterrupt()]})
    with pytest.raises(KeyboardInterrupt):
        session(store, provider)
    (store.root / "tmp" / "half-written.json.abc").write_bytes(b"{")  # a crash mid-write
    assert store.interruption() == [LOCK_FILE, "half-written.json.abc"]
    with pytest.raises(InterruptedSessionError):
        session(store, Provider())
    with pytest.raises(InterruptedSessionError):
        publish(store, tmp_path / "ds")
    moved, verified = store.recover(label="crash-1")
    assert moved == [LOCK_FILE, "half-written.json.abc"]
    assert len(verified) == 2  # the calendars, re-verified
    assert sorted(p.name for p in (store.root / "quarantine" / "crash-1").iterdir()) == \
        sorted(moved)  # fmt: skip
    with pytest.raises(FileExistsError):
        store.recover(label="crash-1")
    assert session(store, Provider()).status == "COMPLETE"
    assert not (tmp_path / "ds").exists()


# ============================================================ tampering and plan identity
def _complete(store: StagingStore) -> None:
    assert session(store, Provider()).status == "COMPLETE"


def _writable(path: Path) -> None:
    path.chmod(0o644)


def test_a_changed_page_is_rejected(store: StagingStore, tmp_path: Path) -> None:
    _complete(store)
    blob = next((store.root / "blobs").iterdir())
    _writable(blob)
    blob.write_bytes(blob.read_bytes().replace(b"09:30", b"09:31", 1) + b" ")
    with pytest.raises(StagingIntegrityError, match="TAMPERED_BATCH"):
        store.committed()
    with pytest.raises(StagingIntegrityError):
        publish(store, tmp_path / "ds")
    with pytest.raises(StagingIntegrityError):
        session(store, Provider())
    assert not (store.root / LOCK_FILE).exists()  # refused cleanly, nothing sent
    assert not (tmp_path / "ds").exists()


def test_a_rewritten_record_is_rejected(store: StagingStore) -> None:
    _complete(store)
    path = next((store.root / "batches").glob("C-CAL-TRADING.*"))
    doc = json.loads(path.read_bytes())
    doc["params"]["start"] = "2020-01-01"  # even with a recomputed digest
    body = {k: v for k, v in doc.items() if k != "record_sha256"}
    from swing_trading_bot.research.dataset import canonical, sha256  # noqa: PLC0415

    doc["record_sha256"] = sha256(canonical(body).encode())
    _writable(path)
    path.write_text(json.dumps(doc))
    with pytest.raises(StagingIntegrityError, match="differs from the plan"):
        store.committed()


def test_a_missing_page_or_foreign_record_is_rejected(store: StagingStore) -> None:
    _complete(store)
    (store.root / "batches" / "X-UNKNOWN.000000000000.json").write_text("{}")
    with pytest.raises(IncompatiblePlanError, match="UNKNOWN_BATCH_RECORD"):
        store.committed()
    (store.root / "batches" / "X-UNKNOWN.000000000000.json").unlink()
    next((store.root / "blobs").iterdir()).unlink()
    with pytest.raises(StagingIntegrityError, match="page missing"):
        store.committed()


def test_incompatible_plans_and_stores_are_never_mixed(store: StagingStore) -> None:
    other = build_acquisition_plan(UNIVERSE, warmup_start=WARMUP, research_start=START,
                                   research_end=date(2021, 5, 31))  # fmt: skip
    with pytest.raises(IncompatiblePlanError, match="INCOMPATIBLE_PLAN"):
        StagingStore.open(store.root, other)
    with pytest.raises(StagingError, match="STORE_EXISTS"):
        StagingStore.create(store.root, plan(), created_at=NOW)
    assert StagingStore.open(store.root, plan()).plan.fingerprint == store.plan.fingerprint
    _complete(store)
    foreign = ResearchUniverse("synthetic-x", UNIVERSE.policy, UNIVERSE.members, False, ())
    with pytest.raises(IncompatiblePlanError):
        assemble_dataset(store, foreign, gates=GATES, semantics=VERIFIED,
                         dataset_id="x", created_at=NOW)  # fmt: skip


def test_a_batch_staged_under_other_gate_outcomes_blocks_publication(
    store: StagingStore, tmp_path: Path,
) -> None:  # fmt: skip
    failed_identity = {**GATES, "TICKER-IDENTITY": False}
    assert session(store, Provider(), gates=failed_identity).status == "COMPLETE"
    with pytest.raises(IncompleteAcquisitionError, match="ineligible staged"):
        publish(store, tmp_path / "ds")  # FB batches staged; gates now say not needed
    assert not (tmp_path / "ds").exists()


# ============================================================ publication
def test_publication_needs_every_eligible_batch(store: StagingStore, tmp_path: Path) -> None:
    session(store, Provider(), max_attempts=5)
    with pytest.raises(IncompleteAcquisitionError, match="missing"):
        publish(store, tmp_path / "ds")
    assert not (tmp_path / "ds").exists()
    assert not list(tmp_path.glob(".ds*"))


def test_atomic_publication_and_visible_rejections(store: StagingStore, tmp_path: Path) -> None:
    _complete(store)
    result = publish(store, tmp_path / "ds", attestations=attest_aaa())
    ds = load_dataset(tmp_path / "ds")
    assert ds.digest == result.digest
    assert result.report["accepted"] == ["AAA"]
    # META has no attested coverage: rejected, and the reasons stay in the report
    assert "META" in result.report["rejected"]
    assert any("COVERAGE_EVIDENCE_INSUFFICIENT" in r or "OTHER_EVENT_COVERAGE_MISSING" in r
               for r in result.report["rejected"]["META"])  # fmt: skip
    rec = ds.records["AAA"]
    assert rec is not None and [d.ex_session for d in rec.dividends] == [
        date(2020, 12, 10),
        date(2021, 2, 5),
    ]  # de-duplicated across padded windows
    assert all(s.settlement_date is not None for s in ds.sessions)
    assert ds.sessions[0].session_date == WARMUP
    acq = ds.manifest.raw["acquisition_report"]
    assert acq["plan_fingerprint"] == store.plan.fingerprint
    assert acq["ca_date_semantics"]["status"] == "LIVE_VERIFIED"
    for path in (tmp_path / "ds").rglob("*"):
        if path.is_file():
            assert path.stat().st_mode & 0o222 == 0  # read-only
    with pytest.raises(DatasetError, match="immutable"):
        publish(store, tmp_path / "ds", attestations=attest_aaa())
    assert not list(tmp_path.glob(".ds.staging"))


def test_unverified_semantics_withhold_provider_claims(store: StagingStore,
                                                      tmp_path: Path) -> None:  # fmt: skip
    _complete(store)
    result = publish(store, tmp_path / "ds", semantics=OBSERVED_16_16D)
    claims = result.corporate_actions.claims
    assert all(not kinds for sym in claims for kinds in claims[sym].values())
    assert set(result.report["rejected"]) == {"AAA", "META"}
    symbols: list[dict[str, Any]] = result.report["symbols"]
    rows = {r["symbol"]: r for r in symbols}
    assert any(f["issue"] == "DATE_SEMANTICS_UNVERIFIED"
               for f in rows["AAA"]["acquisition_findings"])  # fmt: skip


def test_provider_only_coverage_is_never_accepted_by_default(store: StagingStore,
                                                            tmp_path: Path) -> None:  # fmt: skip
    _complete(store)
    result = publish(store, tmp_path / "ds")  # verified semantics, but provider-only evidence
    assert result.corporate_actions.claims["AAA"]["dividends"] == ("PROVIDER_REPORTED",)
    assert set(result.report["rejected"]) == {"AAA", "META"}


# ============================================================ isolation
def _names(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


@pytest.mark.parametrize("module", ["staging.py", "corporate_actions.py"])
def test_acquisition_runs_no_backtest_tuning_or_holdout(module: str) -> None:
    names = _names(RESEARCH / module)
    assert not names & {"swing_trading_bot.strategy.walk_forward",
                        "swing_trading_bot.strategy.backtest",
                        "swing_trading_bot.strategy.evaluation", "run_walk_forward",
                        "run_holdout", "run_backtest", "HoldoutRelease",
                        "SealedResearchData", "FixedDefaultsSelector"}  # fmt: skip


def test_a_published_dataset_keeps_the_holdout_sealed(store: StagingStore,
                                                      tmp_path: Path) -> None:  # fmt: skip
    _complete(store)
    publish(store, tmp_path / "ds", attestations=attest_all())
    inputs = load_dataset(tmp_path / "ds").research_inputs()
    sealed = SealedResearchData(inputs, holdout_start=date(2021, 4, 1))
    sealed.view(date(2021, 3, 31))
    with pytest.raises(HoldoutAccessError):
        sealed.view(date(2021, 4, 1))


def test_staging_refuses_a_ledger_directory(tmp_path: Path) -> None:
    """Names are listed, never opened: an empty placeholder triggers it."""
    (tmp_path / "ledger.sqlite3").write_bytes(b"")
    with pytest.raises(DatasetError, match="ledger"):
        StagingStore.create(tmp_path / "staging", plan(), created_at=NOW)
    with pytest.raises(DatasetError, match="ledger"):
        StagingStore.open(tmp_path / "staging", plan())
    assert not (tmp_path / "staging").exists()
    assert (tmp_path / "ledger.sqlite3").read_bytes() == b""


def test_staged_files_are_private(store: StagingStore) -> None:
    _complete(store)
    assert store.root.stat().st_mode & 0o777 == 0o700
    for name in ("blobs", "batches", "tmp", "quarantine"):
        assert (store.root / name).stat().st_mode & 0o077 == 0
    assert os.listdir(store.root / "tmp") == []


# ============================================================ accounting vs the runner
def test_the_runner_spends_exactly_what_the_planner_accounts_for(store: StagingStore) -> None:
    """Every eligible batch hits its page cap where it can (bars: 2 pages) and spends both
    retries on 503s; small sessions force resumption. Attempts per batch never exceed the
    budget, the total equals the sum of actual use, and the number of sessions equals the
    planner's simulation of the runner's reservation rule."""
    p = store.plan
    eligible = [b for b in p.batches if ineligible_reason(b, GATES) is None]
    script: dict[str, list[BaseException]] = {
        b.request_id: [ServiceUnavailableError("503", status_code=503)] * 2 for b in eligible
    }
    provider = Provider(page_size=200, script=script)
    reports: list[SessionReport] = []
    while not reports or reports[-1].status == "INCOMPLETE":
        reports.append(session(store, provider, max_attempts=12))
        assert len(reports) < 20
    assert reports[-1].status == "COMPLETE"
    staged = store.committed()
    used = {b.batch_id: staged[b.batch_id].attempts for b in eligible}
    for b in eligible:
        assert used[b.batch_id] <= b.max_attempts
        assert used[b.batch_id] == len(staged[b.batch_id].pages) + 2
    assert sum(r.attempts_used for r in reports) == len(provider.calls) == sum(used.values())
    planned = sessions_needed([(b.max_attempts, used[b.batch_id]) for b in eligible], 12)
    assert len(reports) == planned > 1  # resumption really happened
    acc = request_accounting(p, GATES)
    assert sum(used.values()) <= acc.worst_case_attempts
    assert acc.batches == len(eligible)


# ============================================================ readiness after publication
def test_publication_records_acquisition_complete_separately_from_readiness(
    store: StagingStore, tmp_path: Path
) -> None:
    _complete(store)
    publish(store, tmp_path / "partial", attestations=attest_aaa())
    ds = load_dataset(tmp_path / "partial")
    acq = ds.manifest.raw["acquisition_report"]
    eligible = sorted(b.batch_id for b in store.plan.batches
                      if ineligible_reason(b, GATES) is None)  # fmt: skip
    assert acq["acquisition_state"] == "ACQUISITION_COMPLETE"
    assert sorted(acq["required_batches"]) == eligible == sorted(ds.manifest.batches)
    assert all(len(v["pages_sha256"]) >= 1 for v in acq["required_batches"].values())
    assert acq["request_accounting"]["attempts_used"] == len(eligible)
    r = ds.readiness()
    assert r.state is DatasetState.ACQUISITION_COMPLETE  # META is not attested
    assert any(b.startswith("SYMBOL_REJECTED META") for b in r.research_blockers)
    with pytest.raises(DatasetNotResearchReadyError):
        ds.research_inputs()
    publish(store, tmp_path / "ready", attestations=attest_all())
    assert load_dataset(tmp_path / "ready").readiness().state is DatasetState.RESEARCH_READY
