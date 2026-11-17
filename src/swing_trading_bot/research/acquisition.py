"""Deterministic historical-data acquisition PLAN (Steps 16.15-16.15A, Phase 3).

This module describes what a separately authorized acquisition session would request.
It performs no request: no transport, credential or network module is imported
(``tests/unit/test_research_isolation.py``). The plan is fingerprinted so the authorized
run can prove it executed exactly what was reviewed.

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

**Phases.** ``PROBE`` requests run first and decide whether the ``CORE`` requests run at
all (:data:`CAPABILITY_CHECKS`). Corporate-action requests are ``CONDITIONAL`` on their
probe; their result is at most ``PROVIDER_REPORTED`` evidence and never establishes
completeness by itself (dataset module).

Verification status in this repository:

* ``GET /v2/stocks/bars`` — live-verified only for a few 2026-11 sessions and one 2024-06
  NVDA window (raw and split); multi-year depth, page size and pagination on historical
  windows are not verified.
* ``GET /v2/calendar`` — live-verified for recent dates only.
* ``GET /v1/corporate-actions`` — **never exercised here**; parameter names, type names,
  pagination, history depth and completeness are hypotheses.
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

CORPORATE_ACTION_TYPES: Final = (
    "forward_split", "reverse_split", "unit_split", "cash_dividend", "stock_dividend",
    "spin_off", "cash_merger", "stock_merger", "stock_and_cash_merger", "name_change",
    "worthless_removal", "rights_distribution",
)  # fmt: skip


class Host(StrEnum):
    DATA = "data"
    TRADING = "trading"


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
        "CAL-DEPTH", "Does the TRADING calendar serve 2020 sessions with open/close times?",
        "P-CAL-2020", "every weekday row parses (session_from_raw); first session 2020-07-01",
        "stop: no dataset without an authoritative historical calendar"),
    CapabilityCheck(
        "SETTLE-CYCLE", "Does the SETTLEMENT calendar show the 2024-05-28 T+2 -> T+1 change?",
        "P-SETTLE-2024", "trades before 2024-05-28 settle 2 settlement days later, from "
        "2024-05-28 one", "stop: settlement dates cannot be derived by weekday arithmetic"),
    CapabilityCheck(
        "SIP-DEPTH", "Is SIP history available for the oldest planned window?",
        "P-SIP-2020", "HTTP 200 with SPY bars for every session 2020-07-01..2020-07-10, "
        "t = midnight America/New_York", "stop on 403 (entitlement, no retry); re-plan the "
        "window or feed in a new reviewed plan"),
    CapabilityCheck(
        "PAGINATION", "Does next_page_token pagination work on a historical window?",
        "P-PAGES", "limit=50 over 2020-H2 AAPL returns 3 pages, then a null token, with no "
        "duplicate or out-of-order bar", "stop: core requests rely on pagination for safety"),
    CapabilityCheck(
        "SPLIT-ADJ", "Do raw vs split-adjusted bars reflect a known split?",
        "P-SPLIT-RAW+P-SPLIT-ADJ", "derive_split_events finds NVDA 4:1 effective 2021-07-20 "
        "(same-provider consistency only)", "keep raw bars; split coverage then needs an "
        "independent source or operator review"),
    CapabilityCheck(
        "CA-SHAPE", "What does /v1/corporate-actions return (shape, type names, pagination)?",
        "P-CA", "a documented JSON shape with ex/pay dates and amounts as exact numbers or "
        "strings; record the shape before any importer is written",
        "skip every CONDITIONAL corporate-action request; corporate-action coverage stays "
        "unattested and affected symbols are rejected (fail closed)"),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
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
    """Probes + core + conditional at expected pages, no retries."""
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
                       span: tuple[date, date], purpose: str, cap: int,
                       requires: tuple[str, ...] = ()) -> LogicalRequest:  # fmt: skip
    params = (("symbols", symbols), ("types", types), ("start", str(span[0])),
              ("end", str(span[1])), ("limit", "1000"), ("sort", "asc"))  # fmt: skip
    return LogicalRequest(rid, phase, Host.DATA, "/v1/corporate-actions", params, purpose, 1,
                          cap, live_verified=False, requires=requires)  # fmt: skip


def _probes() -> list[LogicalRequest]:
    p = Phase.PROBE
    return [
        _calendar("P-CAL-2020", p, "TRADING", (date(2020, 7, 1), date(2020, 7, 31)),
                  purpose="historical trading-calendar depth"),
        _calendar("P-SETTLE-2024", p, "SETTLEMENT", (date(2024, 5, 20), date(2024, 6, 7)),
                  purpose="settlement-cycle change visible in the SETTLEMENT calendar"),
        _bars("P-SIP-2020", p, "SPY", "raw", (date(2020, 7, 1), date(2020, 7, 10)),
              purpose="SIP entitlement and depth for the oldest window", cap=1),
        _bars("P-PAGES", p, "AAPL", "raw", (date(2020, 7, 1), date(2020, 12, 31)), limit=50,
              expected=3, cap=4, purpose="pagination on a historical window"),
        _bars("P-SPLIT-RAW", p, "NVDA", "raw", (date(2021, 7, 14), date(2021, 7, 23)),
              purpose="known 2021-07 split, raw", cap=1),
        _bars("P-SPLIT-ADJ", p, "NVDA", "split", (date(2021, 7, 14), date(2021, 7, 23)),
              purpose="known 2021-07 split, split-adjusted", cap=1),
        _corporate_actions("P-CA", p, "AAPL", types="cash_dividend,forward_split",
                           span=(date(2020, 7, 1), date(2021, 6, 30)),
                           purpose="corporate-action endpoint shape (UNVERIFIED)", cap=1),
    ]  # fmt: skip


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
    reqs = _probes()
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
                    requires=core,
                    purpose="only if META bars before the 2022-06 ticker change are missing",
                )
            )
    days = (research_end - warmup_start).days  # integer estimate: 1461 days per 4 years
    sessions = (days * 4 * SESSIONS_PER_YEAR_ESTIMATE + 730) // 1461
    expected = sum(r.expected_pages for r in reqs)
    return AcquisitionPlan(
        universe_id=universe.universe_id, universe_fingerprint=universe.fingerprint,
        warmup_start=warmup_start, research_start=research_start, research_end=research_end,
        requests=tuple(reqs), checks=CAPABILITY_CHECKS, estimated_sessions=sessions,
        estimated_bars_per_adjustment=sessions * len(universe.symbols),
        logical_requests=len(reqs), expected_http_requests=expected,
        max_http_without_retries=sum(r.planning_max_pages for r in reqs),
        worst_case_http_requests=sum(r.worst_case_http for r in reqs),
        request_budget=REQUEST_BUDGET, expected_within_budget=expected <= REQUEST_BUDGET,
        stop_conditions=(
            "any failed capability check whose on_failure says stop",
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
    "CORPORATE_ACTION_TYPES",
    "DEFAULT_RESEARCH_END",
    "DEFAULT_RESEARCH_START",
    "DEFAULT_WARMUP_START",
    "READ_ATTEMPTS",
    "REQUEST_BUDGET",
    "AcquisitionPlan",
    "CapabilityCheck",
    "Host",
    "LogicalRequest",
    "Phase",
    "bar_bounds",
    "build_acquisition_plan",
]
