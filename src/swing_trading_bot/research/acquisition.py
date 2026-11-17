"""Deterministic historical-data PLANS (Steps 16.15-16.16B).

Two separate plans, each fingerprinted so an authorized run can prove it executed exactly
what was reviewed:

* :func:`build_capability_probe_plan` — the minimal capability probe (Step 16.16B):
  six probes, P1-P6, at most :data:`PROBE_MAX_HTTP_ATTEMPTS` HTTP attempts in total. Its
  runner (``tests/contract/history_probe.py``) executes only these requests, writes only
  sanitized evidence and **never continues into acquisition**.
* :func:`build_acquisition_plan` — the full acquisition (``CORE`` and ``CONDITIONAL``
  requests). It names the probe plan's fingerprint and is gated by the probe's
  :data:`CAPABILITY_CHECKS`; it needs its own, later authorization.

This module describes requests; it performs none: no transport, credential or network
module is imported (``tests/unit/test_research_isolation.py``).

**Logical vs HTTP requests.** A :class:`LogicalRequest` is one query (one endpoint, one
parameter set). It becomes one or more HTTP GETs through *pagination* (``next_page_token``
until null) and *retries* (the existing read stack retries only HTTP 429, 503 and
timeouts, at most :data:`READ_ATTEMPTS` attempts per page, same page token). Estimates:

* ``expected`` — pages the planner expects, no retries;
* ``planning_max_pages`` — the page cap the session enforces for that request (stop and
  publish nothing if exceeded; for bars never above the adapter's 20-page cap);
* worst case — ``planning_max_pages x READ_ATTEMPTS``.

The session's shared read budget (:data:`REQUEST_BUDGET`) binds first: exhausting it
stops the session with nothing published. Nothing is retried after an *entitlement
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
    def worst_case_http(self) -> int:
        return self.planning_max_pages * READ_ATTEMPTS


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


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    capability_probe_plan_id: str
    capability_probe_fingerprint: str
    """The probe whose checks gate this plan; a different probe is a different plan."""
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
    core = ("CAL-DEPTH", "SETTLE-CYCLE", "SIP-DEPTH", "PAGINATION")
    for kind in ("TRADING", "SETTLEMENT"):
        reqs.append(_calendar(f"C-CAL-{kind}", Phase.CORE, kind, span,
                              purpose=f"{kind} calendar for every session",
                              requires=core[:2]))  # fmt: skip
    for sym in universe.symbols:
        reqs.append(
            _bars(
                f"C-BARS-{sym}-RAW",
                Phase.CORE,
                sym,
                "raw",
                span,
                requires=core,
                purpose=f"{sym} raw SIP daily bars (decision and execution data)",
            )
        )
        reqs.append(
            _bars(
                f"C-BARS-{sym}-SPLIT",
                Phase.CORE,
                sym,
                "split",
                span,
                requires=core,
                purpose=f"{sym} split-adjusted bars (consistency check only)",
            )
        )
    for year in range(warmup_start.year, research_end.year + 1):
        window = (max(warmup_start, date(year, 1, 1)), min(research_end, date(year, 12, 31)))
        reqs.append(_corporate_actions(
            f"X-CA-{year}", Phase.CONDITIONAL, ",".join(universe.symbols),
            types=",".join(CORPORATE_ACTION_TYPES), span=window,
            purpose=f"corporate actions {year}: PROVIDER_REPORTED evidence only", cap=5,
            requires=("CA-SHAPE",)))  # fmt: skip
    if "META" in universe.symbols:
        pre_change = (warmup_start, date(2022, 6, 8))
        for adjustment in ("raw", "split"):
            reqs.append(
                _bars(
                    f"X-BARS-FB-{adjustment.upper()}",
                    Phase.CONDITIONAL,
                    "FB",
                    adjustment,
                    pre_change,
                    requires=(*core, "TICKER-IDENTITY"),
                    purpose="only if META bars before the 2022-06 ticker change are missing",
                )
            )
    days = (research_end - warmup_start).days  # integer estimate: 1461 days per 4 years
    sessions = (days * 4 * SESSIONS_PER_YEAR_ESTIMATE + 730) // 1461
    expected = sum(r.expected_pages for r in reqs)
    probe = build_capability_probe_plan()
    return AcquisitionPlan(
        capability_probe_plan_id=probe.plan_id,
        capability_probe_fingerprint=probe.fingerprint,
        universe_id=universe.universe_id, universe_fingerprint=universe.fingerprint,
        warmup_start=warmup_start, research_start=research_start, research_end=research_end,
        requests=tuple(reqs), checks=CAPABILITY_CHECKS, estimated_sessions=sessions,
        estimated_bars_per_adjustment=sessions * len(universe.symbols),
        logical_requests=len(reqs), expected_http_requests=expected,
        max_http_without_retries=sum(r.planning_max_pages for r in reqs),
        worst_case_http_requests=sum(r.worst_case_http for r in reqs),
        request_budget=REQUEST_BUDGET, expected_within_budget=expected <= REQUEST_BUDGET,
        stop_conditions=(
            "the capability probe not run, or any of its checks whose on_failure says "
            "stop failed",
            "HTTP 401 or 403 on either host (entitlement failures are never retried)",
            "a page beyond a request's planning_max_pages, a repeated or non-string page "
            "token, or any InvalidBrokerDataError",
            "a retryable error (429/503/timeout) still failing after 3 attempts on one page",
            "the shared request budget exhausted",
            "in every case: nothing is published; a partial dataset is never written",
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
        ),
    )  # fmt: skip


__all__ = [
    "ADAPTER_MAX_BAR_PAGES",
    "CAPABILITY_CHECKS",
    "CAPABILITY_PROBE_PLAN_ID",
    "CORPORATE_ACTION_TYPES",
    "DEFAULT_RESEARCH_END",
    "DEFAULT_RESEARCH_START",
    "DEFAULT_WARMUP_START",
    "HOST_ORIGINS",
    "PROBE_ENDPOINTS",
    "PROBE_MAX_HTTP_ATTEMPTS",
    "READ_ATTEMPTS",
    "REQUEST_BUDGET",
    "AcquisitionPlan",
    "CapabilityCheck",
    "CapabilityProbePlan",
    "Host",
    "LogicalRequest",
    "Phase",
    "bar_bounds",
    "build_acquisition_plan",
    "build_capability_probe_plan",
]
