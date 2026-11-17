"""Deterministic historical-data PLANS (Steps 16.15-16.16B).

Two separate plans, each fingerprinted so an authorized run can prove it executed exactly
what was reviewed:

* :func:`build_capability_probe_plan` — the minimal capability probe (Step 16.16B):
  six probes, P1-P6, at most :data:`PROBE_MAX_HTTP_ATTEMPTS` HTTP attempts in total. Its
  runner (``tests/contract/history_probe.py``) executes only these requests, writes only
  sanitized evidence and **never continues into acquisition**.
* :func:`build_acquisition_plan` — the full acquisition (``CORE`` and ``CONDITIONAL``
  requests), split into deterministic, resumable :class:`AcquisitionBatch` units (Step
  16.16E; executed only through ``research.staging``). It names the probe plan's
  fingerprint and is gated by the probe's :data:`CAPABILITY_CHECKS`; it needs its own,
  later authorization.
* :func:`build_ca_date_semantics_plan` — the PROPOSED three-request check for the
  :data:`ACQUISITION_GATES` gate ``CA-DATE-SEMANTICS`` (which date the corporate-action
  endpoint filters on). Defined for review only; no runner executes it.

This module describes requests; it performs none: no transport, credential or network
module is imported (``tests/unit/test_research_isolation.py``).

**Logical vs HTTP requests.** A :class:`LogicalRequest` is one query (one endpoint, one
parameter set). It becomes one or more HTTP GETs through *pagination* (``next_page_token``
until null) and *retries* (the existing read stack retries only HTTP 429, 503 and
timeouts, at most :data:`READ_ATTEMPTS` attempts per page, same page token). Estimates:

* ``expected`` — pages the planner expects, no retries;
* ``planning_max_pages`` — the page cap the session enforces for that request (stop and
  publish nothing if exceeded; for bars never above the adapter's 20-page cap);
* attempt budget — ``min(planning_max_pages + BATCH_RETRY_RESERVE,
  planning_max_pages x READ_ATTEMPTS)`` (:attr:`LogicalRequest.attempt_budget`): every
  page at its cap plus at most two retries for the whole batch, each page still limited
  to :data:`READ_ATTEMPTS`. The session runner enforces exactly this per batch, so the
  plan's worst case is the sum of the batch budgets (:func:`request_accounting`).

The session's shared read budget (:data:`REQUEST_BUDGET`) binds as well: a batch whose
whole budget no longer fits is not started, and exhausting it stops the session with
nothing published. Nothing is retried after an *entitlement
failure*: a data-host 403 with the known recent-SIP message is a
``MarketDataEntitlementError`` (A§12.36) and stops the session; any other 403 or a 401
also stops it (in the runtime it would be an ``AUTH`` halt).

**Phases.** ``PROBE`` requests belong only to the capability probe plan; their checks
(:data:`CAPABILITY_CHECKS`) decide whether the acquisition's ``CORE`` requests may run.
Corporate-action requests are ``CONDITIONAL`` on their probe; their result is at most
``PROVIDER_REPORTED`` evidence and never establishes completeness by itself (dataset
module).

Verification status in this repository:

* ``GET /v2/stocks/bars`` — live-verified only for a few 2026-11 sessions and one 2024-06
  NVDA window (raw and split); multi-year depth, page size and pagination on historical
  windows are not verified.
* ``GET /v2/calendar`` — live-verified for recent dates only.
* ``GET /v1/corporate-actions`` — **never exercised here**; parameter names, type names,
  pagination, history depth and completeness are hypotheses. The transport reaches it
  only through its single fixed method (Step 16.16B).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Final

from swing_trading_bot.domain.calendar import MARKET_TZ
from swing_trading_bot.research.universe import PILOT_UNIVERSE, ResearchUniverse

DEFAULT_WARMUP_START: Final = date(2020, 7, 1)
DEFAULT_RESEARCH_START: Final = date(2021, 1, 1)
DEFAULT_RESEARCH_END: Final = date(2026, 10, 31)
#: Shared read budget of the existing read stack (RUNBOOK §3): 120 requests / 300 s.
REQUEST_BUDGET: Final = 120
#: ``daily_bars`` / ``calendar`` read policy: 3 attempts per page (429, 503, timeout only).
READ_ATTEMPTS: Final = 3
#: The adapter's page cap for bars (``alpaca_read.MAX_BAR_PAGES``).
ADAPTER_MAX_BAR_PAGES: Final = 20
SESSIONS_PER_YEAR_ESTIMATE: Final = 252

CAPABILITY_PROBE_PLAN_ID: Final = "history-capability-probe-v1"
#: Hard cap on actual HTTP attempts in one probe session: pages and retries together.
PROBE_MAX_HTTP_ATTEMPTS: Final = 10
#: One retry per page at most (two attempts), only after 429, 503 or a timeout.
PROBE_MAX_ATTEMPTS_PER_PAGE: Final = 2
PROBE_DEADLINE_S: Final = 120
PROBE_CONNECT_TIMEOUT_S: Final = 5
PROBE_READ_TIMEOUT_S: Final = 20
PROBE_BACKOFF_BASE_MS: Final = 500
PROBE_BACKOFF_MAX_MS: Final = 4000
PROBE_RETRY_ON: Final = ("HTTP_429", "HTTP_503", "TIMEOUT")
PROBE_STOP_ON: Final = ("HTTP_401", "HTTP_403")
#: P3's page size: small enough that the 2020-H2 window needs more than one page.
PROBE_PAGE_LIMIT: Final = 100
PROBE_EVIDENCE_PREFIX: Final = "history-probe-"

# ---- acquisition sessions and batches (Step 16.16E) ----
#: The existing read stack's session deadline (RUNBOOK §3); the attempt cap is
#: :data:`REQUEST_BUDGET`. Neither is raised for acquisition.
SESSION_DEADLINE_S: Final = 300
#: A batch may use at most ``planning_max_pages + BATCH_RETRY_RESERVE`` HTTP attempts
#: (never more than ``planning_max_pages x READ_ATTEMPTS``).
BATCH_RETRY_RESERVE: Final = 2
#: A batch is started only with at least this much session time left.
BATCH_TIME_RESERVE_S: Final = 30

# ---- corporate-action windows (Step 16.16E) ----
#: The 16.16D probe showed that ``/v1/corporate-actions`` does NOT filter on the ex-date
#: (SPY's 2020-06-19 ex-date dividend was returned for start=2020-07-01; its payable and
#: process dates were 2020-07-31). Which date it filters on is not verified, so requests
#: are padded and the research coverage is selected by ex-date afterwards.
CA_PAD_BEFORE_DAYS: Final = 31
"""The filter date may precede the ex-date (a split's payable date: AAPL 2020, 3 days)."""
CA_PAD_AFTER_DAYS: Final = 92
"""The filter date may follow the ex-date (SPY's payable/process date: 42 days)."""
CA_PAD_MARGIN_DAYS: Final = 14
"""An observed lag closer than this to a pad edge shows the padding is not safe."""
CA_PAGE_LIMIT: Final = 1000
CA_DATE_SEMANTICS_PLAN_ID: Final = "ca-date-semantics-probe-v1"
CA_SEMANTICS_MAX_HTTP_ATTEMPTS: Final = 5
CA_SEMANTICS_DEADLINE_S: Final = 60

CORPORATE_ACTION_TYPES: Final = (
    "forward_split", "reverse_split", "unit_split", "cash_dividend", "stock_dividend",
    "spin_off", "cash_merger", "stock_merger", "stock_and_cash_merger", "name_change",
    "worthless_removal", "rights_distribution",
)  # fmt: skip


class Host(StrEnum):
    DATA = "data"
    TRADING = "trading"


#: The exact HTTPS origins (equal to the transport's allowlist; tested).
HOST_ORIGINS: Final = {
    Host.TRADING: "https://paper-api.alpaca.markets",
    Host.DATA: "https://data.alpaca.markets",
}
#: The only (host, path) pairs the capability probe may request.
PROBE_ENDPOINTS: Final = (
    (Host.TRADING, "/v2/calendar"),
    (Host.DATA, "/v2/stocks/bars"),
    (Host.DATA, "/v1/corporate-actions"),
)


class Phase(StrEnum):
    PROBE = "PROBE"
    CORE = "CORE"
    CONDITIONAL = "CONDITIONAL"


@dataclass(frozen=True, slots=True)
class LogicalRequest:
    request_id: str
    phase: Phase
    host: Host
    path: str
    params: tuple[tuple[str, str], ...]
    purpose: str
    expected_pages: int
    planning_max_pages: int
    live_verified: bool
    requires: tuple[str, ...] = ()
    """Capability checks that must pass before this request is sent."""

    @property
    def attempt_budget(self) -> int:
        """HTTP attempts the batch of this request may use: pages and retries together."""
        return min(self.planning_max_pages + BATCH_RETRY_RESERVE,
                   self.planning_max_pages * READ_ATTEMPTS)  # fmt: skip


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    check_id: str
    question: str
    probe: str
    """The probe request id that answers it."""
    passes_when: str
    on_failure: str


CAPABILITY_CHECKS: Final = (
    CapabilityCheck(
        "CAL-DEPTH", "Does the TRADING calendar serve every session of the research window?",
        "P1", "every row parses (session_from_raw); dates strictly increasing, no weekend; "
        "first 2020-07-01, last 2026-10-30; listed closures absent, listed early close at "
        "13:00", "stop: no dataset without an authoritative historical calendar"),
    CapabilityCheck(
        "SETTLE-CYCLE", "Does the SETTLEMENT calendar show the 2024-05-28 T+2 -> T+1 change?",
        "P2", "trades before 2024-05-28 settle on the 2nd listed settlement day after the "
        "trade, from 2024-05-28 on the 1st; 2024-05-24 and 2024-05-28 both settle "
        "2024-05-29; TRADING and SETTLEMENT agree on shared trade dates",
        "stop: settlement dates cannot be derived by weekday arithmetic"),
    CapabilityCheck(
        "SIP-DEPTH", "Is SIP history available for the oldest planned window?",
        "P3", "HTTP 200 with one AAPL bar for every calendar session 2020-07-01..2020-12-31 "
        "(count derived from P1), t = midnight America/New_York", "stop on 403 "
        "(entitlement, no retry); re-plan the window or feed in a new reviewed plan"),
    CapabilityCheck(
        "PAGINATION", "Does next_page_token pagination work on a historical window?",
        "P3", "limit=100 returns ceil(sessions/100) pages (from P1), then a null token; no "
        "repeated token, duplicate or out-of-order bar", "stop: core requests rely on "
        "pagination for safety"),
    CapabilityCheck(
        "SPLIT-ADJ", "Do raw vs split-adjusted bars reflect a known split?",
        "P3+P4", "derive_split_events finds AAPL 4:1 effective 2020-08-31 (same-provider "
        "consistency only)", "keep raw bars; split coverage then needs an independent "
        "source or operator review"),
    CapabilityCheck(
        "TICKER-IDENTITY", "Does META return bars from before the 2022-06-09 FB -> META change?",
        "P5", "a META bar for every calendar session 2022-06-06..2022-06-10", "the FB "
        "conditional requests are needed (META's early bars are keyed as FB)"),
    CapabilityCheck(
        "CA-SHAPE", "What does /v1/corporate-actions return (shape, type names, pagination)?",
        "P6", "an object with a corporate_actions object of lists and a next_page_token; "
        "the shape (keys, value types) is recorded before any importer is written",
        "skip every CONDITIONAL corporate-action request; corporate-action coverage stays "
        "unattested and affected symbols are rejected (fail closed)"),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class CapabilityProbePlan:
    """The exact capability probe: what is sent, how often, and when it stops."""

    plan_id: str
    requests: tuple[LogicalRequest, ...]
    checks: tuple[CapabilityCheck, ...]
    origins: tuple[tuple[str, str], ...]
    endpoints: tuple[tuple[str, str], ...]
    page_limit: int
    expected_http_requests: int
    """Pages the planner expects, no retries."""
    max_pages: int
    """Every request at its page cap, no retries."""
    max_http_attempts: int
    """The hard cap on actual attempts (pages and retries); the next one is refused."""
    retry_reserve: int
    max_attempts_per_page: int
    retry_on: tuple[str, ...]
    stop_on: tuple[str, ...]
    deadline_s: int
    connect_timeout_s: int
    read_timeout_s: int
    backoff_base_ms: int
    backoff_max_ms: int
    evidence_prefix: str
    continues_into_acquisition: bool
    stop_conditions: tuple[str, ...]
    credential_requirement: str

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def request(self, request_id: str) -> LogicalRequest:
        for r in self.requests:
            if r.request_id == request_id:
                return r
        raise KeyError(request_id)


#: Gates added after the 16.16D probe. They are NOT part of the probe plan (whose
#: fingerprint is fixed); each needs its own separately authorized live request.
ACQUISITION_GATES: Final = (
    CapabilityCheck(
        "CA-DATE-SEMANTICS",
        "Which date do /v1/corporate-actions start/end filter on (ex, record, payable or "
        "process date)?",
        "Q1+Q2+Q3", "exactly one date role explains which single-day requests return the "
        "reference events (ca-date-semantics-probe-v1)", "corporate actions are still "
        "fetched over padded windows and selected by ex-date, but no PROVIDER_REPORTED "
        "coverage claim is made: coverage then needs INDEPENDENT_SOURCE or "
        "OPERATOR_REVIEW evidence, or the symbol is rejected"),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class CorporateActionWindow:
    """One corporate-action request. ``coverage_*`` is the research coverage it supplies
    (events are kept by **ex-date** inside it); the request window is padded on both sides
    because the provider's filter date differs from the ex-date."""

    coverage_start: date
    coverage_end: date
    pad_before_days: int = CA_PAD_BEFORE_DAYS
    pad_after_days: int = CA_PAD_AFTER_DAYS

    def __post_init__(self) -> None:
        if self.coverage_end < self.coverage_start:
            raise ValueError("coverage_end precedes coverage_start")
        if self.pad_before_days < 0 or self.pad_after_days < 0:
            raise ValueError("padding cannot be negative")

    @property
    def request_start(self) -> date:
        return self.coverage_start - timedelta(days=self.pad_before_days)

    @property
    def request_end(self) -> date:
        return self.coverage_end + timedelta(days=self.pad_after_days)


def ca_windows(
    start: date,
    end: date,
    *,
    pad_before_days: int = CA_PAD_BEFORE_DAYS,
    pad_after_days: int = CA_PAD_AFTER_DAYS,
) -> tuple[CorporateActionWindow, ...]:
    """Contiguous, non-overlapping calendar-year coverage slices over ``start..end``; the
    padded *request* windows overlap, so neighbouring batches see the same events."""
    if end < start:
        raise ValueError("end precedes start")
    return tuple(
        CorporateActionWindow(max(start, date(y, 1, 1)), min(end, date(y, 12, 31)),
                              pad_before_days, pad_after_days)
        for y in range(start.year, end.year + 1)
    )  # fmt: skip


class BatchKind(StrEnum):
    CALENDAR = "CALENDAR"
    BARS = "BARS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"


@dataclass(frozen=True, slots=True)
class AcquisitionBatch:
    """The unit of resumable acquisition: one logical request, fetched to its last page
    within its own attempt budget, then staged atomically (``research.staging``)."""

    batch_id: str
    """``<request_id>.<12 hex>`` over the request and its boundaries: deterministic."""
    request_id: str
    kind: BatchKind
    symbols: tuple[str, ...]
    request_start: date
    request_end: date
    coverage_start: date
    """The research dates this batch supplies (for corporate actions: by ex-date)."""
    coverage_end: date
    max_pages: int
    max_attempts: int
    """Per-batch HTTP budget: pages and retries together."""
    requires_passed: tuple[str, ...]
    requires_failed: tuple[str, ...] = ()
    """Run only when these checks FAILED (the FB fallback)."""


def _batch(req: LogicalRequest, kind: BatchKind, symbols: tuple[str, ...],
           request_span: tuple[date, date], coverage: tuple[date, date], *,
           requires_failed: tuple[str, ...] = ()) -> AcquisitionBatch:  # fmt: skip
    body = json.dumps({"request": asdict(req), "request_span": request_span,
                       "coverage": coverage, "requires_failed": requires_failed},
                      sort_keys=True, separators=(",", ":"), default=str)  # fmt: skip
    return AcquisitionBatch(
        batch_id=f"{req.request_id}.{hashlib.sha256(body.encode()).hexdigest()[:12]}",
        request_id=req.request_id, kind=kind, symbols=symbols,
        request_start=request_span[0], request_end=request_span[1],
        coverage_start=coverage[0], coverage_end=coverage[1],
        max_pages=req.planning_max_pages,
        max_attempts=req.attempt_budget,
        requires_passed=tuple(c for c in req.requires if c not in requires_failed),
        requires_failed=requires_failed,
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    capability_probe_plan_id: str
    capability_probe_fingerprint: str
    """The probe whose checks gate this plan; a different probe is a different plan."""
    ca_date_semantics_plan_id: str
    ca_date_semantics_fingerprint: str
    """The proposed verification request for CA-DATE-SEMANTICS (not yet authorized)."""
    gates: tuple[CapabilityCheck, ...]
    batches: tuple[AcquisitionBatch, ...]
    session_max_attempts: int
    session_deadline_s: int
    batch_retry_reserve: int
    max_attempts_per_page: int
    batch_attempt_total: int
    """Sum of every batch's attempt budget (the bound with retries)."""
    max_sessions_without_stop: int
    """Sessions needed when every batch uses its whole budget and nothing stops."""
    ca_pad_before_days: int
    ca_pad_after_days: int
    ca_pad_margin_days: int
    ca_date_filter_observation: str
    universe_id: str
    universe_fingerprint: str
    warmup_start: date
    research_start: date
    research_end: date
    requests: tuple[LogicalRequest, ...]
    checks: tuple[CapabilityCheck, ...]
    estimated_sessions: int
    estimated_bars_per_adjustment: int
    logical_requests: int
    expected_http_requests: int
    """Core + conditional at expected pages, no retries (the probe is separate)."""
    max_http_without_retries: int
    """Every request at its planning page cap, no retries."""
    worst_case_http_requests: int
    """Every page at its cap and every attempt used (bounded first by the budget)."""
    request_budget: int
    expected_within_budget: bool
    stop_conditions: tuple[str, ...]
    credential_requirement: str
    limitations: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def _rfc3339(d: date, delta: timedelta) -> str:
    instant = datetime.combine(d, time(0, 0), tzinfo=MARKET_TZ).astimezone(UTC) + delta
    return instant.isoformat().replace("+00:00", "Z")


def bar_bounds(start: date, end: date) -> tuple[str, str]:
    """The adapter's ``bar_window`` bounds (A§12.36): one second around the market-time
    midnights of ``start`` and ``end``."""
    return _rfc3339(start, timedelta(seconds=-1)), _rfc3339(end, timedelta(seconds=1))


def _bars(rid: str, phase: Phase, symbol: str, adjustment: str, span: tuple[date, date], *,
          purpose: str, limit: int = 10000, expected: int = 1, cap: int = 2,
          requires: tuple[str, ...] = ()) -> LogicalRequest:  # fmt: skip
    lo, hi = bar_bounds(*span)
    params = (("symbols", symbol), ("timeframe", "1Day"), ("start", lo), ("end", hi),
              ("feed", "sip"), ("adjustment", adjustment), ("limit", str(limit)),
              ("sort", "asc"))  # fmt: skip
    return LogicalRequest(rid, phase, Host.DATA, "/v2/stocks/bars", params, purpose, expected,
                          min(cap, ADAPTER_MAX_BAR_PAGES), live_verified=False,
                          requires=requires)  # fmt: skip


def _calendar(rid: str, phase: Phase, kind: str, span: tuple[date, date], *, purpose: str,
              requires: tuple[str, ...] = ()) -> LogicalRequest:  # fmt: skip
    params = (("start", str(span[0])), ("end", str(span[1])), ("date_type", kind))
    return LogicalRequest(rid, phase, Host.TRADING, "/v2/calendar", params, purpose, 1, 1,
                          live_verified=False, requires=requires)  # fmt: skip


def _corporate_actions(rid: str, phase: Phase, symbols: str, *, types: str,
                       span: tuple[date, date], purpose: str, cap: int, limit: int = 1000,
                       expected: int = 1, requires: tuple[str, ...] = ()
                       ) -> LogicalRequest:  # fmt: skip
    params = (("symbols", symbols), ("types", types), ("start", str(span[0])),
              ("end", str(span[1])), ("limit", str(limit)), ("sort", "asc"))  # fmt: skip
    return LogicalRequest(rid, phase, Host.DATA, "/v1/corporate-actions", params, purpose,
                          expected, cap, live_verified=False, requires=requires)  # fmt: skip


def _probes() -> tuple[LogicalRequest, ...]:
    """P1-P6 exactly as reviewed in Step 16.16A. No probe reads a 2025-2026 price."""
    p = Phase.PROBE
    window = (DEFAULT_WARMUP_START, DEFAULT_RESEARCH_END)
    return (
        _calendar("P1", p, "TRADING", window,
                  purpose="trading-calendar depth and format over the research window"),
        _calendar("P2", p, "SETTLEMENT", window,
                  purpose="settlement calendar incl. the 2024-05-28 T+2 -> T+1 change"),
        _bars("P3", p, "AAPL", "raw", (date(2020, 7, 1), date(2020, 12, 31)),
              limit=PROBE_PAGE_LIMIT, expected=2, cap=2,
              purpose="SIP depth for the oldest window and small-page pagination"),
        _bars("P4", p, "AAPL", "split", (date(2020, 8, 24), date(2020, 9, 4)), cap=1,
              purpose="split-adjusted bars around the 2020-08-31 4:1 split (vs P3 raw)"),
        _bars("P5", p, "META", "raw", (date(2022, 6, 6), date(2022, 6, 10)), cap=1,
              purpose="ticker identity across the 2022-06-09 FB -> META change"),
        _corporate_actions("P6", p, "AAPL,SPY", types="forward_split,cash_dividend",
                           span=(date(2020, 7, 1), date(2021, 6, 30)), limit=5, expected=2,
                           cap=2, purpose="corporate-action endpoint shape and pagination "
                           "(UNVERIFIED endpoint)"),
    )  # fmt: skip


def build_capability_probe_plan() -> CapabilityProbePlan:
    """The Step 16.16B capability probe: 8 expected HTTP requests, hard cap 10."""
    reqs = _probes()
    expected = sum(r.expected_pages for r in reqs)
    pages = sum(r.planning_max_pages for r in reqs)
    return CapabilityProbePlan(
        plan_id=CAPABILITY_PROBE_PLAN_ID, requests=reqs, checks=CAPABILITY_CHECKS,
        origins=tuple((str(h), o) for h, o in HOST_ORIGINS.items()),
        endpoints=tuple((str(h), path) for h, path in PROBE_ENDPOINTS),
        page_limit=PROBE_PAGE_LIMIT, expected_http_requests=expected, max_pages=pages,
        max_http_attempts=PROBE_MAX_HTTP_ATTEMPTS,
        retry_reserve=PROBE_MAX_HTTP_ATTEMPTS - pages,
        max_attempts_per_page=PROBE_MAX_ATTEMPTS_PER_PAGE, retry_on=PROBE_RETRY_ON,
        stop_on=PROBE_STOP_ON, deadline_s=PROBE_DEADLINE_S,
        connect_timeout_s=PROBE_CONNECT_TIMEOUT_S, read_timeout_s=PROBE_READ_TIMEOUT_S,
        backoff_base_ms=PROBE_BACKOFF_BASE_MS, backoff_max_ms=PROBE_BACKOFF_MAX_MS,
        evidence_prefix=PROBE_EVIDENCE_PREFIX, continues_into_acquisition=False,
        stop_conditions=(
            "HTTP 401 or 403 on either host: stop the session, never retried",
            "HTTP 404 on P1-P5, any other 4xx, a 5xx other than 503, a network error, a "
            "redirect or a response from another host: stop the session",
            "HTTP 404 on P6: CA-SHAPE unavailable; no fallback endpoint is tried",
            "429, 503 or a timeout still failing after one retry on a page: stop",
            f"the {PROBE_MAX_HTTP_ATTEMPTS}-attempt budget or the {PROBE_DEADLINE_S} s "
            "deadline exhausted: the next attempt is refused before it is sent",
            "a repeated, empty or non-string page token, or a page beyond a request's cap: "
            "that probe fails (P6: recorded as a finding)",
            "in every case: sanitized evidence of what was received is written; no dataset "
            "is written and acquisition never starts",
        ),
        credential_requirement=(
            "Paper-account key from STB_ENV_FILE only (no credential environment variables), "
            "TRADING_MODE=paper, trading disabled; GET only; no account, order, position "
            "or activity endpoint; no ledger access."),
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class SemanticsReference:
    """One reference event of ca-date-semantics-probe-v1 and the single day requested.
    Every date is copied from the 16.16D evidence (PROVIDER_REPORTED); a date the
    evidence does not contain is None and is never inferred."""

    symbol: str
    list_name: str
    """Response list (and action type) of the event."""
    ex_date: date
    record_date: date | None
    payable_date: date | None
    process_date: date | None
    requested_day: date
    evidence: str

    def dates(self) -> dict[str, date | None]:
        return {"ex_date": self.ex_date, "record_date": self.record_date,
                "payable_date": self.payable_date, "process_date": self.process_date}  # fmt: skip


_P6: Final = "history-probe-20261117T025839Z P6"


def _aapl_split(requested_day: date) -> SemanticsReference:
    return SemanticsReference("AAPL", "forward_splits", date(2020, 8, 31), date(2020, 8, 24),
                              date(2020, 8, 28), date(2020, 8, 31), requested_day,
                              f"{_P6} raw/006 (4:1, due bill 2020-09-01)")  # fmt: skip


#: Reference events from the 16.16D probe evidence. A single-day request returns its
#: event only if the provider's filter date equals that day (the expected response under
#: every hypothesis is derived from these dates in ``corporate_actions``).
CA_SEMANTICS_REFERENCES: Final = {
    "Q1": _aapl_split(date(2020, 8, 28)),
    "Q2": _aapl_split(date(2020, 8, 31)),
    "Q3": SemanticsReference(
        "SPY", "cash_dividends", date(2020, 6, 19), date(2020, 6, 22), date(2020, 7, 31),
        date(2020, 7, 31), date(2020, 7, 31), f"{_P6} raw/007 (rate 1.3662419)"),
}  # fmt: skip
"""Q1/Q2 discriminate payable (08-28) from process (08-31) on the only observed event
whose two dates differ (AAPL's split). Q3 is a positive control: SPY's dividend has
payable == process (07-31), so it separates {payable, process} from {ex, record} and
checks that a single-day window returns anything, but it cannot separate payable from
process for cash dividends. No observed cash dividend has payable != process."""


def build_ca_date_semantics_plan() -> CapabilityProbePlan:
    """The PROPOSED live check for CA-DATE-SEMANTICS: three single-day requests, at most
    5 attempts and 60 s. Defined and fingerprinted for review only: no runner executes it
    and it needs its own authorization."""
    types = {"forward_splits": "forward_split", "cash_dividends": "cash_dividend"}
    reqs = tuple(
        _corporate_actions(rid, Phase.PROBE, sym, types=types[kind], span=(day, day),
                           limit=10, expected=1, cap=1,
                           purpose=f"{sym} {kind} ex {ex}: single-day filter on {day}")
        for rid, (sym, kind, ex, day) in (
            (k, (r.symbol, r.list_name, r.ex_date, r.requested_day))
            for k, r in CA_SEMANTICS_REFERENCES.items())
    )  # fmt: skip
    return CapabilityProbePlan(
        plan_id=CA_DATE_SEMANTICS_PLAN_ID, requests=reqs, checks=ACQUISITION_GATES,
        origins=((str(Host.DATA), HOST_ORIGINS[Host.DATA]),),
        endpoints=((str(Host.DATA), "/v1/corporate-actions"),), page_limit=10,
        expected_http_requests=len(reqs), max_pages=len(reqs),
        max_http_attempts=CA_SEMANTICS_MAX_HTTP_ATTEMPTS,
        retry_reserve=CA_SEMANTICS_MAX_HTTP_ATTEMPTS - len(reqs),
        max_attempts_per_page=PROBE_MAX_ATTEMPTS_PER_PAGE, retry_on=PROBE_RETRY_ON,
        stop_on=PROBE_STOP_ON, deadline_s=CA_SEMANTICS_DEADLINE_S,
        connect_timeout_s=PROBE_CONNECT_TIMEOUT_S, read_timeout_s=PROBE_READ_TIMEOUT_S,
        backoff_base_ms=PROBE_BACKOFF_BASE_MS, backoff_max_ms=PROBE_BACKOFF_MAX_MS,
        evidence_prefix="ca-semantics-probe-", continues_into_acquisition=False,
        stop_conditions=(
            "HTTP 401 or 403: stop, never retried",
            "any other 4xx, a 5xx other than 503, a redirect or another host: stop",
            f"the {CA_SEMANTICS_MAX_HTTP_ATTEMPTS}-attempt budget or the "
            f"{CA_SEMANTICS_DEADLINE_S} s deadline exhausted: the next attempt is refused",
            "any pattern other than exactly one date role: INCONCLUSIVE (fail closed)",
        ),
        credential_requirement=(
            "Paper-account key from STB_ENV_FILE only (no credential environment variables), "
            "TRADING_MODE=paper, trading disabled; GET only; data host only; no ledger "
            "access."),
    )  # fmt: skip


def ineligible_reason(batch: AcquisitionBatch, gates: Mapping[str, bool]) -> str | None:
    """Why ``batch`` must not run under ``gates`` (check id -> passed), or None. A
    missing gate counts as not passed and not failed (fail closed)."""
    for check in batch.requires_passed:
        if gates.get(check) is not True:
            return f"requires {check} passed"
    for check in batch.requires_failed:
        if gates.get(check) is not False:
            return f"only if {check} failed"
    return None


def sessions_needed(batches: Sequence[tuple[int, int]],
                    session_max_attempts: int = REQUEST_BUDGET) -> int:  # fmt: skip
    """Sessions the runner needs for ``(attempt budget, attempts used)`` batches in plan
    order when nothing stops. Mirrors the runner's rule exactly: a batch starts only if
    its whole budget still fits in the session (``run_session``). Session time is not
    modelled (see the plan's limitations)."""
    sessions, used = (1, 0) if batches else (0, 0)
    for budget, use in batches:
        if not 0 <= use <= budget <= session_max_attempts:
            raise ValueError(f"batch budget {budget} / use {use} cannot fit a session")
        if session_max_attempts - used < budget:
            sessions, used = sessions + 1, 0
        used += use
    return sessions


@dataclass(frozen=True, slots=True)
class BatchKindAccounting:
    kind: BatchKind
    batches: int
    expected_pages: int
    """Per batch (every batch of a kind has the same request shape)."""
    max_pages: int
    retry_allowance: int
    """Attempts beyond the page cap a batch may spend on retries (429/503/timeout)."""
    max_attempts: int
    """Per batch: ``max_pages + retry_allowance``, enforced by the runner."""
    total_expected: int
    total_page_cap: int
    total_worst_case: int


@dataclass(frozen=True, slots=True)
class RequestAccounting:
    """Reconciled request totals for one set of batches (all, or those eligible under
    given gate outcomes). Every number is derived from the batches the runner enforces."""

    scope: str
    rows: tuple[BatchKindAccounting, ...]
    batches: int
    expected_requests: int
    """Every batch at its expected pages, no retries."""
    page_cap_requests: int
    """Every batch at its page cap, no retries."""
    worst_case_attempts: int
    """Every batch using its whole attempt budget (pages + retries)."""
    session_max_attempts: int
    session_deadline_s: int
    sessions_expected: int
    sessions_worst_case: int
    skipped: tuple[tuple[str, str], ...]


def request_accounting(plan: AcquisitionPlan,
                       gates: Mapping[str, bool] | None = None) -> RequestAccounting:  # fmt: skip
    """Totals for every batch of ``plan`` (``gates`` None) or only those eligible under
    ``gates``. Raises if any batch's shape is inconsistent with its budget rule."""
    expected_of = {r.request_id: r.expected_pages for r in plan.requests}
    chosen: list[AcquisitionBatch] = []
    skipped: list[tuple[str, str]] = []
    for b in plan.batches:
        reason = None if gates is None else ineligible_reason(b, gates)
        if reason is None:
            chosen.append(b)
        else:
            skipped.append((b.batch_id, reason))
    rows = []
    for kind in BatchKind:
        group = [b for b in chosen if b.kind is kind]
        if not group:
            continue
        shapes = {(expected_of[b.request_id], b.max_pages, b.max_attempts) for b in group}
        if len(shapes) != 1:
            raise ValueError(f"{kind.value} batches differ in shape: {sorted(shapes)}")
        ((expected, pages, attempts),) = shapes
        if attempts != min(pages + plan.batch_retry_reserve, pages * plan.max_attempts_per_page):
            raise ValueError(f"{kind.value} budget {attempts} breaks the budget rule")
        n = len(group)
        rows.append(BatchKindAccounting(kind, n, expected, pages, attempts - pages, attempts,
                                        n * expected, n * pages, n * attempts))  # fmt: skip
    return RequestAccounting(
        scope="all batches" if gates is None else f"eligible under {dict(sorted(gates.items()))}",
        rows=tuple(rows), batches=len(chosen),
        expected_requests=sum(r.total_expected for r in rows),
        page_cap_requests=sum(r.total_page_cap for r in rows),
        worst_case_attempts=sum(r.total_worst_case for r in rows),
        session_max_attempts=plan.session_max_attempts,
        session_deadline_s=plan.session_deadline_s,
        sessions_expected=sessions_needed(
            [(b.max_attempts, expected_of[b.request_id]) for b in chosen],
            plan.session_max_attempts),
        sessions_worst_case=sessions_needed([(b.max_attempts, b.max_attempts) for b in chosen],
                                            plan.session_max_attempts),
        skipped=tuple(skipped),
    )  # fmt: skip


def build_acquisition_plan(
    universe: ResearchUniverse = PILOT_UNIVERSE,
    *,
    warmup_start: date = DEFAULT_WARMUP_START,
    research_start: date = DEFAULT_RESEARCH_START,
    research_end: date = DEFAULT_RESEARCH_END,
) -> AcquisitionPlan:
    if not warmup_start <= research_start <= research_end:
        raise ValueError("need warmup_start <= research_start <= research_end")
    span = (warmup_start, research_end)
    reqs: list[LogicalRequest] = []
    batches: list[AcquisitionBatch] = []
    symbols = tuple(universe.symbols)
    core = ("CAL-DEPTH", "SETTLE-CYCLE", "SIP-DEPTH", "PAGINATION")
    for kind in ("TRADING", "SETTLEMENT"):
        req = _calendar(
            f"C-CAL-{kind}",
            Phase.CORE,
            kind,
            span,
            purpose=f"{kind} calendar for every session",
            requires=core[:2],
        )
        reqs.append(req)
        batches.append(_batch(req, BatchKind.CALENDAR, (), span, span))
    for sym in symbols:
        for adjustment, purpose in (
            ("raw", f"{sym} raw SIP daily bars (decision and execution data)"),
            ("split", f"{sym} split-adjusted bars (consistency check only)"),
        ):
            req = _bars(
                f"C-BARS-{sym}-{adjustment.upper()}",
                Phase.CORE,
                sym,
                adjustment,
                span,
                requires=core,
                purpose=purpose,
            )
            reqs.append(req)
            batches.append(_batch(req, BatchKind.BARS, (sym,), span, span))
    for w in ca_windows(warmup_start, research_end):
        req = _corporate_actions(
            f"X-CA-{w.coverage_start.year}",
            Phase.CONDITIONAL,
            ",".join(symbols),
            types=",".join(CORPORATE_ACTION_TYPES),
            span=(w.request_start, w.request_end),
            limit=CA_PAGE_LIMIT,
            cap=5,
            requires=("CA-SHAPE",),
            purpose=f"corporate actions with ex-date {w.coverage_start}..{w.coverage_end} "
            f"(request padded -{w.pad_before_days}/+{w.pad_after_days} days, selected by "
            "ex-date): PROVIDER_REPORTED evidence only",
        )
        reqs.append(req)
        batches.append(
            _batch(
                req,
                BatchKind.CORPORATE_ACTIONS,
                symbols,
                (w.request_start, w.request_end),
                (w.coverage_start, w.coverage_end),
            )
        )
    if "META" in symbols:
        pre_change = (warmup_start, date(2022, 6, 8))
        for adjustment in ("raw", "split"):
            req = _bars(
                f"X-BARS-FB-{adjustment.upper()}",
                Phase.CONDITIONAL,
                "FB",
                adjustment,
                pre_change,
                requires=(*core, "TICKER-IDENTITY"),
                purpose="only if META bars before the 2022-06 ticker change are missing",
            )
            reqs.append(req)
            batches.append(
                _batch(
                    req,
                    BatchKind.BARS,
                    ("META",),
                    pre_change,
                    pre_change,
                    requires_failed=("TICKER-IDENTITY",),
                )
            )
    days = (research_end - warmup_start).days  # integer estimate: 1461 days per 4 years
    sessions = (days * 4 * SESSIONS_PER_YEAR_ESTIMATE + 730) // 1461
    expected = sum(r.expected_pages for r in reqs)
    attempt_total = sum(b.max_attempts for b in batches)
    probe = build_capability_probe_plan()
    semantics = build_ca_date_semantics_plan()
    return AcquisitionPlan(
        capability_probe_plan_id=probe.plan_id,
        capability_probe_fingerprint=probe.fingerprint,
        ca_date_semantics_plan_id=semantics.plan_id,
        ca_date_semantics_fingerprint=semantics.fingerprint,
        gates=ACQUISITION_GATES, batches=tuple(batches),
        session_max_attempts=REQUEST_BUDGET, session_deadline_s=SESSION_DEADLINE_S,
        batch_retry_reserve=BATCH_RETRY_RESERVE, max_attempts_per_page=READ_ATTEMPTS,
        batch_attempt_total=attempt_total,
        max_sessions_without_stop=sessions_needed([(b.max_attempts, b.max_attempts)
                                                   for b in batches]),
        ca_pad_before_days=CA_PAD_BEFORE_DAYS, ca_pad_after_days=CA_PAD_AFTER_DAYS,
        ca_pad_margin_days=CA_PAD_MARGIN_DAYS,
        ca_date_filter_observation=(
            "history-probe-20261117T025839Z P6: not the ex-date (SPY ex 2020-06-19 returned "
            "for start 2020-07-01) and not the record date (06-22); consistent with the "
            "payable or process date (07-31). UNVERIFIED: gate CA-DATE-SEMANTICS"),
        universe_id=universe.universe_id, universe_fingerprint=universe.fingerprint,
        warmup_start=warmup_start, research_start=research_start, research_end=research_end,
        requests=tuple(reqs), checks=CAPABILITY_CHECKS, estimated_sessions=sessions,
        estimated_bars_per_adjustment=sessions * len(universe.symbols),
        logical_requests=len(reqs), expected_http_requests=expected,
        max_http_without_retries=sum(r.planning_max_pages for r in reqs),
        worst_case_http_requests=attempt_total,
        request_budget=REQUEST_BUDGET, expected_within_budget=expected <= REQUEST_BUDGET,
        stop_conditions=(
            "the capability probe not run, or any of its checks whose on_failure says "
            "stop failed",
            "HTTP 401 or 403 on either host (entitlement failures are never retried)",
            "a page beyond a batch's max_pages, a repeated or non-string page "
            "token, or any InvalidBrokerDataError",
            "a retryable error (429/503/timeout) still failing after 3 attempts on one page",
            "a batch exceeding its own attempt budget",
            "the shared request budget or the session deadline exhausted; a batch whose "
            "whole budget no longer fits is not started (resume in a later, authorized "
            "session)",
            "an interrupted session (lock or in-flight files present): no further request "
            "until an explicit recovery has quarantined them and re-verified every batch",
            "in every case: only complete, verified batches are staged; a dataset is "
            "published only when every eligible batch is staged and verified",
        ),
        credential_requirement=(
            "Read-only paper-account API key selected explicitly via STB_ENV_FILE (RUNBOOK "
            "§2); GET only; data and trading hosts; no ledger opened."),
        limitations=(
            "Bars before research_start are warm-up history only and are never traded.",
            "Historical SIP depth before 2024 is not live-verified for this account.",
            "The corporate-action endpoint is unverified; its lists are PROVIDER_REPORTED "
            "and never establish completeness without independent or reviewed evidence.",
            "Raw vs split-adjusted bars come from one provider: an internal consistency "
            "check, not independent verification.",
            "Delisted symbols are not requested: the universe is a present-day pilot.",
            "SIP daily bars may be revised by the provider after acquisition; the dataset "
            "freezes what was received and records when.",
            "The corporate-action filter date is unverified (CA-DATE-SEMANTICS); padding "
            "is a mitigation, never proof of completeness.",
            "Response shapes of corporate-action types other than forward splits and cash "
            "dividends are unverified; an unrecognized shape fails closed.",
            "The 16.16D probe verified SIP depth and pagination for AAPL 2020-H2 only, "
            "not for every symbol or year.",
        ),
    )  # fmt: skip


__all__ = [
    "ACQUISITION_GATES",
    "ADAPTER_MAX_BAR_PAGES",
    "BATCH_RETRY_RESERVE",
    "BATCH_TIME_RESERVE_S",
    "CAPABILITY_CHECKS",
    "CAPABILITY_PROBE_PLAN_ID",
    "CA_DATE_SEMANTICS_PLAN_ID",
    "CA_PAD_AFTER_DAYS",
    "CA_PAD_BEFORE_DAYS",
    "CA_PAD_MARGIN_DAYS",
    "CA_PAGE_LIMIT",
    "CA_SEMANTICS_REFERENCES",
    "CORPORATE_ACTION_TYPES",
    "DEFAULT_RESEARCH_END",
    "DEFAULT_RESEARCH_START",
    "DEFAULT_WARMUP_START",
    "HOST_ORIGINS",
    "PROBE_ENDPOINTS",
    "PROBE_MAX_HTTP_ATTEMPTS",
    "READ_ATTEMPTS",
    "REQUEST_BUDGET",
    "SESSION_DEADLINE_S",
    "AcquisitionBatch",
    "AcquisitionPlan",
    "BatchKind",
    "BatchKindAccounting",
    "CapabilityCheck",
    "CapabilityProbePlan",
    "CorporateActionWindow",
    "Host",
    "LogicalRequest",
    "Phase",
    "RequestAccounting",
    "SemanticsReference",
    "bar_bounds",
    "build_acquisition_plan",
    "build_ca_date_semantics_plan",
    "build_capability_probe_plan",
    "ca_windows",
    "ineligible_reason",
    "request_accounting",
    "sessions_needed",
]
