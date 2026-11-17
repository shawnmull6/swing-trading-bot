"""PYTEST_DONT_REWRITE

The historical-provider capability probe runner (Step 16.16B; plan: Step 16.16A).

**Do not run without explicit operator authorization naming the probe-plan
fingerprint.** It executes exactly the six reviewed probes of
:func:`~swing_trading_bot.research.acquisition.build_capability_probe_plan` and nothing
else:

* P1 TRADING calendar, P2 SETTLEMENT calendar (2020-07-01..2026-10-31);
* P3 AAPL raw SIP bars 2020-H2 at ``limit=100`` (SIP depth and pagination; the expected
  session count comes from the P1 calendar, never a constant);
* P4 AAPL split-adjusted bars around 2020-08-31 (compared with P3);
* P5 META raw bars across the 2022-06-09 ticker change;
* P6 ``/v1/corporate-actions`` for AAPL/SPY 2020-07..2021-06 at ``limit=5``.

**Request controls** (one :class:`~tests.contract.supervisor.RequestBudget` for the
whole session, never injectable): GET only; the exact (origin, path) allowlist of the
plan, checked before the budget is touched; at most 10 actual HTTP attempts (pages and
retries together; the 11th is refused before it is sent); a 120 s deadline with connect
and read timeouts cut to the time left; at most one retry per page and only after 429,
503 or a timeout; 401/403 stop the session; redirects and responses from another host
stop it; no proxies or ``.netrc`` (``trust_env`` off); no fallback endpoint.

**Evidence** goes only to a new ``contract-evidence/history-probe-<UTC>Z/`` directory
through :class:`~tests.contract.supervisor.EvidenceWriter` (Git-ignored, 0700/0600, no
symlinks, write-once, atomic publication). Bodies are structurally sanitized before
anything is written; a body that cannot be proven sanitized is never stored (its length
and SHA-256 are). Request headers are never stored; of the response headers only the
rate-limit allowlist and ``X-Request-ID`` are. Error messages of the transport (which may
quote a body) are never recorded: only fixed codes are.

**Ledger isolation.** No persistence, ledger or ``sqlite3`` module is imported (tested in a
fresh interpreter); there is no ledger argument; while the run is active an audit hook
(:class:`LedgerGuard`) refuses every ``sqlite3`` connection and any open, listing,
rename, removal or permission change under the production data directory, before the
operating system is asked. ``stat`` has no audit event: it is prevented by construction
(no code path names a ledger file).

**No acquisition.** The runner writes no research dataset and never continues into the
acquisition plan; it reports and stops.

Usage (authorized operator only)::

    RUN_ALPACA_HISTORY_PROBE=1 STB_ENV_FILE=/abs/path/paper.env \\
        uv run python -m tests.contract.history_probe --plan-fingerprint <fingerprint>
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import itertools
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from swing_trading_bot.app.credentials import CredentialSourceError, load_credentials
from swing_trading_bot.broker.alpaca_http import (
    ALLOWED_ORIGINS,
    AlpacaReadTransport,
    ReadRetryPolicy,
)
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    BrokerRequestError,
    InvalidBrokerDataError,
    MarketDataEntitlementError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.broker.mapping import (
    calendar_date,
    collect_daily_bars,
    session_from_raw,
    settlement_days_from_raw,
)
from swing_trading_bot.broker.models import DailyBar
from swing_trading_bot.config.settings import SettingsError
from swing_trading_bot.domain.calendar import MARKET_TZ, TradingSession
from swing_trading_bot.observability.logging import SecretRedactor
from swing_trading_bot.research.acquisition import (
    CapabilityProbePlan,
    Host,
    LogicalRequest,
    build_capability_probe_plan,
)
from swing_trading_bot.strategy.market_data import derive_split_events
from tests.contract.supervisor import (
    EVIDENCE_ROOT,
    REDACTED,
    REPO,
    BudgetedSession,
    BudgetExhausted,
    EvidenceError,
    EvidenceWriteError,
    EvidenceWriter,
    Exchange,
    RequestBudget,
    SanitizationError,
    WriteAttemptError,
    _raw_name,
    assert_sanitized,
    bounded_sleep,
    identity_values,
    parse_body,
    rate_limit_evidence,
    sanitize_body,
)

ENABLE_VARIABLE: Final = "RUN_ALPACA_HISTORY_PROBE"
#: The production data directory (it holds the ledger). Named, never opened.
PRODUCTION_DATA_DIR: Final = REPO / "data"
CORPORATE_ACTIONS: Final = "/v1/corporate-actions"
REQUEST_ID_HEADER: Final = "X-Request-ID"

# ------------------------------------------------------------------ expectations
# General-knowledge expectations the probe checks (not attested data).
EXPECTED_FIRST_SESSION: Final = date(2020, 7, 1)
EXPECTED_LAST_SESSION: Final = date(2026, 10, 30)
EXPECTED_CLOSURES: Final = (date(2020, 11, 26), date(2022, 6, 20), date(2025, 1, 9))
EXPECTED_EARLY_CLOSES: Final = {date(2020, 11, 27): time(13, 0)}
T1_EFFECTIVE: Final = date(2024, 5, 28)
EXPECTED_TRANSITION: Final = {date(2024, 5, 24): date(2024, 5, 29),
                              date(2024, 5, 28): date(2024, 5, 29)}  # fmt: skip
P3_WINDOW: Final = (date(2020, 7, 1), date(2020, 12, 31))
P4_WINDOW: Final = (date(2020, 8, 24), date(2020, 9, 4))
P5_WINDOW: Final = (date(2022, 6, 6), date(2022, 6, 10))
TICKER_CHANGE: Final = date(2022, 6, 9)
EXPECTED_SPLIT: Final = ("AAPL", date(2020, 8, 31), 4, 1)
MAX_LISTED_MISMATCHES: Final = 20

VERIFIED: Final = "VERIFIED"
FAILED: Final = "FAILED"
INCONCLUSIVE: Final = "INCONCLUSIVE"
UNAVAILABLE: Final = "UNAVAILABLE"
NOT_RUN: Final = "NOT_RUN"


# ============================================================ ledger guard (Phase 5)
class LedgerAccessRefused(BaseException):
    """A guarded operation was attempted while a probe ran. A ``BaseException`` so no
    ``except Exception`` on the way can swallow it. ``code`` is fixed; no path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


#: Audit events (CPython) whose first one or two arguments are filesystem paths.
GUARDED_PATH_EVENTS: Final = frozenset({
    "open", "os.listdir", "os.scandir", "os.remove", "os.rename", "os.replace", "os.chmod",
    "os.chown", "os.chflags", "os.truncate", "os.utime", "os.mkdir", "os.rmdir", "os.link",
    "os.symlink", "shutil.rmtree", "shutil.copyfile", "shutil.copymode", "shutil.copystat",
    "shutil.move", "glob.glob", "os.walk",
})  # fmt: skip


def _event_path(value: object) -> str | None:
    if isinstance(value, (str, bytes, os.PathLike)):
        try:
            return os.path.abspath(os.fsdecode(value))  # string arithmetic, no syscall
        except (TypeError, ValueError):
            return None
    return None


@dataclass(frozen=True, slots=True)
class LedgerGuard:
    """Refuses ``sqlite3`` entirely and path operations under ``protected``."""

    protected: tuple[str, ...] = (os.path.abspath(PRODUCTION_DATA_DIR),)

    def check(self, event: str, args: tuple[object, ...]) -> None:
        if event.startswith("sqlite3."):
            raise LedgerAccessRefused("SQLITE_REFUSED")
        if event not in GUARDED_PATH_EVENTS:
            return
        for value in args[:2]:
            path = _event_path(value)
            if path is not None and any(path == root or path.startswith(root + os.sep)
                                        for root in self.protected):  # fmt: skip
                raise LedgerAccessRefused("PROTECTED_PATH_REFUSED")


_ACTIVE_GUARDS: list[LedgerGuard] = []
_HOOK_INSTALLED: list[bool] = []


def _audit(event: str, args: tuple[object, ...]) -> None:
    for guard in tuple(_ACTIVE_GUARDS):
        guard.check(event, args)


@contextlib.contextmanager
def ledger_guard(guard: LedgerGuard | None = None) -> Iterator[LedgerGuard]:
    """Arm ``guard`` (default: the production data directory) for the ``with`` body. The
    process-wide audit hook is installed once (hooks cannot be removed) and enforces only
    the guards armed at the moment."""
    if not _HOOK_INSTALLED:
        sys.addaudithook(_audit)
        _HOOK_INSTALLED.append(True)
    armed = guard or LedgerGuard()
    _ACTIVE_GUARDS.append(armed)
    try:
        yield armed
    finally:
        _ACTIVE_GUARDS.remove(armed)


# ============================================================ probe session (Phase 3)
class ProbeRefusal(BaseException):
    """A request or response outside the probe contract (fixed ``code``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Attempt:
    probe: str
    page: int
    attempt: int
    host: str
    path: str
    params: dict[str, str]
    started_ms: int
    elapsed_ms: int | None = None
    status: int | None = None
    request_id: str | None = None
    error: str | None = None
    body_bytes: int | None = None
    body_sha256: str | None = None
    body_stored: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _safe_params(params: Mapping[str, Any]) -> dict[str, str]:
    out = {k: str(v) for k, v in params.items() if k != "page_token"}
    if "page_token" in params:
        out["page_token_sha256"] = _token_digest(str(params["page_token"]))
    return dict(sorted(out.items()))


class ProbeSession:
    """The outermost session: the exact endpoint allowlist (checked before the budget is
    touched, so a refused request is never sent nor counted), per-attempt metadata, and
    the redirect / response-host refusals. Everything else is :class:`BudgetedSession`."""

    def __init__(self, inner: BudgetedSession, endpoints: frozenset[tuple[str, str]]) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_endpoints", endpoints)
        object.__setattr__(self, "attempts", [])
        object.__setattr__(self, "context", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)  # BudgetedSession refuses every write verb

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "context":
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    def get(self, url: str, **kwargs: Any) -> Any:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if (origin, parts.path) not in self._endpoints or parts.query or parts.fragment:
            raise WriteAttemptError("ENDPOINT_NOT_ALLOWLISTED")
        if self.context is None:
            raise WriteAttemptError("ATTEMPT_WITHOUT_PROBE_CONTEXT")
        probe, page, attempt = self.context
        budget: RequestBudget = self._inner.budget

        def clock_ms() -> int:
            return round(budget.elapsed() * 1000)

        sent_before = budget.used
        started = clock_ms()
        record = Attempt(probe, page, attempt, parts.netloc, parts.path,
                         _safe_params(kwargs.get("params") or {}), started)  # fmt: skip
        try:
            response = self._inner.get(url, **kwargs)
        except BaseException as exc:
            if budget.used > sent_before:  # transmitted (or attempted) and failed
                record.elapsed_ms = clock_ms() - started
                record.error = type(exc).__name__
                self.attempts.append(record)
            raise
        record.elapsed_ms = clock_ms() - started
        record.status = int(response.status_code)
        headers = getattr(response, "headers", {}) or {}
        rid = headers.get(REQUEST_ID_HEADER)
        record.request_id = str(rid)[:128] if rid is not None else None
        body = (response.text or "").encode()
        record.body_bytes = len(body)
        record.body_sha256 = hashlib.sha256(body).hexdigest()
        self.attempts.append(record)
        if 300 <= record.status < 400:
            raise ProbeRefusal("REDIRECT_REFUSED")
        final = getattr(response, "url", None)
        if final:
            fp = urlsplit(str(final))
            if f"{fp.scheme}://{fp.netloc}" != origin:
                raise ProbeRefusal("UNEXPECTED_RESPONSE_HOST")
        return response


# ============================================================ execution (Phase 2)
class _Stop(BaseException):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _NotFound(Exception):  # noqa: N818 - control flow
    pass


class ProbeDataError(Exception):
    """A response that is not the expected shape (fixed ``code``, never content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Fetched:
    pages: list[object] = field(default_factory=list)
    complete: bool = False
    """``next_page_token`` reached null within the request's page cap."""


def _classify(exc: BrokerError) -> tuple[str, bool]:  # noqa: PLR0911 - a flat table
    """(fixed code, retryable). Retryable: 429, 503, timeout only."""
    if isinstance(exc, RateLimitedError):
        return "HTTP_429", True
    if isinstance(exc, ServiceUnavailableError):
        if exc.kind == "timeout":
            return "TIMEOUT", True
        if exc.status_code == 503:
            return "HTTP_503", True
        if exc.status_code is not None:
            return f"HTTP_{exc.status_code}", False
        return "NETWORK_ERROR", False
    if isinstance(exc, MarketDataEntitlementError):
        return "HTTP_403_ENTITLEMENT", False
    if isinstance(exc, AuthenticationError):
        return ("HTTP_401" if exc.status_code == 401 else "HTTP_403"), False
    if isinstance(exc, BrokerRequestError):
        return f"HTTP_{exc.status_code}", False
    return type(exc).__name__, False


class ProbeExecutor:
    def __init__(self, plan: CapabilityProbePlan, transport: AlpacaReadTransport,
                 session: ProbeSession, sleep: Callable[[timedelta], None]) -> None:  # fmt: skip
        self.plan = plan
        self.transport = transport
        self.session = session
        self.sleep = sleep
        self.policy = ReadRetryPolicy(plan.max_attempts_per_page,
                                      timedelta(milliseconds=plan.backoff_base_ms),
                                      timedelta(milliseconds=plan.backoff_max_ms))  # fmt: skip

    def _call(self, req: LogicalRequest, params: Mapping[str, str]) -> object:
        if req.path == CORPORATE_ACTIONS:
            return self.transport.get_corporate_actions_json(params)
        service: Literal["trading", "data"] = "trading" if req.host is Host.TRADING else "data"
        return self.transport.get_json(service, req.path.removeprefix("/v2"), params)

    def _page(self, req: LogicalRequest, page: int, params: Mapping[str, str]) -> object:
        attempt = 1
        while True:
            self.session.context = (req.request_id, page, attempt)
            try:
                return self._call(req, params)
            except OrderNotFoundError:
                raise _NotFound from None
            except (RateLimitedError, ServiceUnavailableError, AuthenticationError,
                    MarketDataEntitlementError, BrokerRequestError) as exc:  # fmt: skip
                code, retryable = _classify(exc)
                if not retryable:
                    raise _Stop(code) from None
                if attempt >= self.policy.max_attempts:
                    raise _Stop(f"RETRY_EXHAUSTED_{code}") from None
                self.sleep(self.policy.delay(attempt, exc))
                attempt += 1
            finally:
                self.session.context = None

    def fetch(self, req: LogicalRequest) -> Fetched:
        out = Fetched()
        paginated = req.path != "/v2/calendar"
        token: str | None = None
        seen: set[str] = set()
        for page in range(1, req.planning_max_pages + 1):
            params = dict(req.params)
            if token is not None:
                params["page_token"] = token
            payload = self._page(req, page, params)
            out.pages.append(payload)
            if not paginated:
                out.complete = True
                return out
            if not isinstance(payload, dict) or "next_page_token" not in payload:
                raise ProbeDataError("UNEXPECTED_SHAPE")
            nxt = payload["next_page_token"]
            if nxt is None:
                out.complete = True
                return out
            if not isinstance(nxt, str) or not nxt:
                raise ProbeDataError("INVALID_PAGE_TOKEN")
            if nxt in seen:
                raise ProbeDataError("REPEATED_PAGE_TOKEN")
            seen.add(nxt)
            token = nxt
        return out  # cap reached with a non-null token: complete stays False


# ============================================================ evaluation (pure)
@dataclass
class Result:
    outcome: str
    codes: list[str] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "codes": sorted(set(self.codes)),
                "observations": self.observations}  # fmt: skip


def _rows(pages: Sequence[object]) -> list[Mapping[str, Any]]:
    if len(pages) != 1 or not isinstance(pages[0], list):
        raise ProbeDataError("UNEXPECTED_SHAPE")
    rows = pages[0]
    if not all(isinstance(r, dict) for r in rows):
        raise ProbeDataError("UNEXPECTED_SHAPE")
    return rows


def evaluate_trading_calendar(pages: Sequence[object]) -> tuple[Result, list[TradingSession]]:
    """CAL-DEPTH. Returns the parsed sessions (empty on failure)."""
    try:
        rows = _rows(pages)
        sessions = [session_from_raw(r) for r in rows]
    except (ProbeDataError, InvalidBrokerDataError) as exc:
        code = exc.code if isinstance(exc, ProbeDataError) else "ROW_INVALID"
        return Result(FAILED, [code]), []
    days = [s.session_date for s in sessions]
    codes: list[str] = []
    if not days:
        return Result(FAILED, ["EMPTY"]), []
    if any(b <= a for a, b in itertools.pairwise(days)):
        codes.append("NOT_STRICTLY_INCREASING")
    if any(d.weekday() >= 5 for d in days):
        codes.append("WEEKEND_SESSION")
    if days[0] != EXPECTED_FIRST_SESSION:
        codes.append("FIRST_SESSION_UNEXPECTED")
    if days[-1] != EXPECTED_LAST_SESSION:
        codes.append("LAST_SESSION_UNEXPECTED")  # incl. a silently truncated response
    present = set(days)
    closures_listed = [str(d) for d in EXPECTED_CLOSURES if d in present]
    if closures_listed:
        codes.append("EXPECTED_CLOSURE_LISTED")
    by_day = {s.session_date: s for s in sessions}
    early_ok = all(d in by_day and by_day[d].close_at.astimezone(MARKET_TZ).time() == t
                   for d, t in EXPECTED_EARLY_CLOSES.items())  # fmt: skip
    if not early_ok:
        codes.append("EXPECTED_EARLY_CLOSE_MISSING")
    obs = {
        "rows": len(rows), "first": str(days[0]), "last": str(days[-1]),
        "rows_with_settlement_date": sum(1 for s in sessions if s.settlement_date is not None),
        "expected_closures_listed": closures_listed, "early_closes_as_expected": early_ok,
    }  # fmt: skip
    return Result(FAILED if codes else VERIFIED, codes, obs), sessions


def evaluate_settlement(pages: Sequence[object], sessions: Sequence[TradingSession]) -> Result:
    """SETTLE-CYCLE: the settlement-day distance of every trade, the 2024-05-28 change,
    and agreement with the TRADING rows' own ``settlement_date``."""
    try:
        rows = _rows(pages)
        settlement_days = list(settlement_days_from_raw(rows))  # validates every row
        pairs = sorted((calendar_date(r, "date"), calendar_date(r, "settlement_date"))
                       for r in rows)  # fmt: skip
    except (ProbeDataError, InvalidBrokerDataError) as exc:
        code = exc.code if isinstance(exc, ProbeDataError) else "ROW_INVALID"
        return Result(FAILED, [code])
    if not pairs:
        return Result(FAILED, ["EMPTY"])
    first_day = settlement_days[0]
    mismatches: list[str] = []
    t2 = t1 = 0
    for trade, settles in pairs:
        if trade < first_day:  # its earlier settlement days precede the range
            continue
        distance = (bisect.bisect_right(settlement_days, settles)
                    - bisect.bisect_right(settlement_days, trade))  # fmt: skip
        expected = 2 if trade < T1_EFFECTIVE else 1
        if distance == expected:
            t2, t1 = (t2 + 1, t1) if expected == 2 else (t2, t1 + 1)
        else:
            mismatches.append(f"{trade}:{distance}")
    observed = dict(pairs)
    transition = {str(t): (str(observed[t]) if t in observed else None)
                  for t in EXPECTED_TRANSITION}  # fmt: skip
    codes: list[str] = []
    if mismatches:
        codes.append("SETTLEMENT_DISTANCE_UNEXPECTED")
    if any(observed.get(t) != s for t, s in EXPECTED_TRANSITION.items()):
        codes.append("TRANSITION_NOT_AS_EXPECTED")
    disagreements: int | None = None
    if sessions:
        by_day = {s.session_date: s.settlement_date for s in sessions}
        disagreements = sum(1 for trade, settles in pairs
                            if trade in by_day and by_day[trade] is not None
                            and by_day[trade] != settles)  # fmt: skip
        if disagreements:
            codes.append("TRADING_SETTLEMENT_DISAGREE")
    else:
        codes.append("TRADING_CALENDAR_UNAVAILABLE")
    obs = {
        "rows": len(rows), "settlement_days": len(settlement_days),
        "first_trade": str(pairs[0][0]), "last_trade": str(pairs[-1][0]),
        "t_plus_2_rows": t2, "t_plus_1_rows": t1, "mismatch_count": len(mismatches),
        "mismatches": mismatches[:MAX_LISTED_MISMATCHES], "transition": transition,
        "trading_settlement_disagreements": disagreements,
    }  # fmt: skip
    hard = [c for c in codes if c != "TRADING_CALENDAR_UNAVAILABLE"]
    outcome = FAILED if hard else (INCONCLUSIVE if codes else VERIFIED)
    return Result(outcome, codes, obs)


def _bar_rows(pages: Sequence[object], symbol: str) -> Iterator[tuple[str, Any, datetime]]:
    received = datetime(1970, 1, 1, tzinfo=UTC)  # probe evidence: receipt time not modelled
    for page in pages:
        if not isinstance(page, dict):
            raise ProbeDataError("UNEXPECTED_SHAPE")
        bars = page.get("bars") or {}
        if not isinstance(bars, dict):
            raise ProbeDataError("UNEXPECTED_SHAPE")
        for sym, rows in bars.items():
            if sym != symbol or not isinstance(rows, list):
                raise ProbeDataError("UNEXPECTED_SYMBOL_OR_SHAPE")
            for row in rows:
                yield sym, row, received


def parse_bars(pages: Sequence[object], symbol: str, window: tuple[date, date],
               adjustment: str) -> list[DailyBar]:  # fmt: skip
    try:
        out = collect_daily_bars(_bar_rows(pages, symbol), [symbol], *window, feed="sip",
                                 adjustment=adjustment)  # type: ignore[arg-type]  # fmt: skip
    except InvalidBrokerDataError:
        raise ProbeDataError("INVALID_BARS") from None
    bars = list(out[symbol])
    if any(b.as_of.astimezone(MARKET_TZ).time() != time(0, 0) for b in bars):
        raise ProbeDataError("BAR_TIME_NOT_MARKET_MIDNIGHT")
    return bars


def calendar_sessions(sessions: Sequence[TradingSession], window: tuple[date, date]) -> list[date]:
    return [s.session_date for s in sessions if window[0] <= s.session_date <= window[1]]


def evaluate_p3(fetched: Fetched, sessions: Sequence[TradingSession], page_limit: int
                ) -> tuple[Result, Result, list[DailyBar]]:  # fmt: skip
    """(SIP-DEPTH, PAGINATION, bars). Expected counts come from the P1 calendar."""
    try:
        bars = parse_bars(fetched.pages, "AAPL", P3_WINDOW, "raw")
    except ProbeDataError as exc:
        return Result(FAILED, [exc.code]), Result(FAILED, [exc.code]), []
    per_page = []
    for page in fetched.pages:
        rows = (page.get("bars") or {}).get("AAPL") if isinstance(page, dict) else None
        per_page.append(len(rows) if isinstance(rows, list) else 0)
    got = [b.session_date for b in bars]
    depth_obs: dict[str, Any] = {"bars": len(got), "first": str(got[0]) if got else None,
                                 "last": str(got[-1]) if got else None}  # fmt: skip
    page_obs: dict[str, Any] = {"pages": len(fetched.pages), "bars_per_page": per_page,
                                "final_token_null": fetched.complete}  # fmt: skip
    page_codes = [] if fetched.complete else ["PAGE_CAP_EXCEEDED"]
    if not sessions:
        return (Result(INCONCLUSIVE, ["TRADING_CALENDAR_UNAVAILABLE"], depth_obs),
                Result(FAILED if page_codes else INCONCLUSIVE,
                       [*page_codes, "TRADING_CALENDAR_UNAVAILABLE"], page_obs), bars)  # fmt: skip
    expected = calendar_sessions(sessions, P3_WINDOW)
    missing = sorted(set(expected) - set(got))
    extra = sorted(set(got) - set(expected))
    depth_codes = (["SESSIONS_MISSING"] if missing else []) + (["UNEXPECTED_SESSIONS"]
                                                               if extra else [])  # fmt: skip
    depth_obs.update(expected_sessions=len(expected), missing=[str(d) for d in missing][:20],
                     unexpected=[str(d) for d in extra][:20])  # fmt: skip
    expected_pages = max(1, -(-len(expected) // page_limit))
    expected_sizes = [min(page_limit, len(expected) - i * page_limit)
                      for i in range(expected_pages)]  # fmt: skip
    page_obs.update(expected_pages=expected_pages, expected_bars_per_page=expected_sizes)
    if len(fetched.pages) != expected_pages or per_page != expected_sizes:
        page_codes.append("PAGE_LAYOUT_UNEXPECTED")
    return (Result(FAILED if depth_codes else VERIFIED, depth_codes, depth_obs),
            Result(FAILED if page_codes else VERIFIED, page_codes, page_obs), bars)  # fmt: skip


def evaluate_split(raw: Sequence[DailyBar], fetched: Fetched) -> Result:
    try:
        adjusted = parse_bars(fetched.pages, "AAPL", P4_WINDOW, "split")
    except ProbeDataError as exc:
        return Result(FAILED, [exc.code])
    if not raw:
        return Result(INCONCLUSIVE, ["RAW_BARS_UNAVAILABLE"], {"adjusted_bars": len(adjusted)})
    window_raw = [b for b in raw if P4_WINDOW[0] <= b.session_date <= P4_WINDOW[1]]
    derived = derive_split_events("AAPL", window_raw, adjusted)
    events = [(e.symbol, e.effective_session, e.new_shares, e.old_shares)
              for e in derived.events]  # fmt: skip
    obs = {"raw_bars": len(window_raw), "adjusted_bars": len(adjusted),
           "derived": [f"{s} {d} {n}:{o}" for s, d, n, o in events],
           "findings": sorted(str(f.issue) for f in derived.findings),
           "evidence_kind": "SAME_PROVIDER_CONSISTENCY"}  # fmt: skip
    ok = derived.resolved and events == [EXPECTED_SPLIT]
    return Result(VERIFIED if ok else FAILED, [] if ok else ["SPLIT_NOT_AS_EXPECTED"], obs)


def evaluate_identity(fetched: Fetched, sessions: Sequence[TradingSession]) -> Result:
    try:
        bars = parse_bars(fetched.pages, "META", P5_WINDOW, "raw")
    except ProbeDataError as exc:
        return Result(FAILED, [exc.code])
    got = [b.session_date for b in bars]
    obs: dict[str, Any] = {"bars": len(got), "sessions": [str(d) for d in got],
                           "pre_change_bars": sum(1 for d in got if d < TICKER_CHANGE)}  # fmt: skip
    if not sessions:
        return Result(INCONCLUSIVE, ["TRADING_CALENDAR_UNAVAILABLE"], obs)
    expected = calendar_sessions(sessions, P5_WINDOW)
    obs["expected_sessions"] = len(expected)
    complete = got == expected
    obs["fb_requests_needed"] = not complete
    return Result(VERIFIED, [] if complete else ["META_PRE_CHANGE_BARS_MISSING"], obs)


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, Decimal):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def evaluate_corporate_actions(fetched: Fetched, limit: int) -> Result:
    """CA-SHAPE: record the shape; never import. Expectations are observations only."""
    shape: dict[str, dict[str, set[str]]] = {}
    per_page: list[dict[str, int]] = []
    ids: list[str] = []
    found_split = False
    symbols: set[str] = set()
    for page in fetched.pages:
        if (not isinstance(page, dict) or not isinstance(page.get("corporate_actions"), dict)
                or "next_page_token" not in page):  # fmt: skip
            return Result(FAILED, ["UNEXPECTED_SHAPE"])
        counts: dict[str, int] = {}
        for kind, items in page["corporate_actions"].items():
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                return Result(FAILED, ["UNEXPECTED_SHAPE"])
            counts[kind] = len(items)
            fields = shape.setdefault(kind, {})
            for item in items:
                for key, value in item.items():
                    fields.setdefault(key, set()).add(_json_type(value))
                if isinstance(item.get("id"), str):
                    ids.append(item["id"])
                if isinstance(item.get("symbol"), str):
                    symbols.add(item["symbol"])
                if (kind == "forward_splits" and item.get("symbol") == EXPECTED_SPLIT[0]
                        and str(EXPECTED_SPLIT[1]) in [v for v in item.values()
                                                       if isinstance(v, str)]):  # fmt: skip
                    found_split = True
        per_page.append(dict(sorted(counts.items())))
    codes: list[str] = []
    if len(ids) != len(set(ids)):
        codes.append("DUPLICATE_ITEM_ID")
    totals = [sum(c.values()) for c in per_page]
    obs = {
        "pages": len(fetched.pages), "final_token_null": fetched.complete,
        "items_per_page": per_page,
        "limit_semantics": ("TOTAL_ITEMS" if all(t <= limit for t in totals)
                            else "NOT_TOTAL_ITEMS"),
        "shape": {k: {f: sorted(t) for f, t in sorted(v.items())}
                  for k, v in sorted(shape.items())},
        "symbols_seen": sorted(symbols), "aapl_split_2020_08_31_listed": found_split,
        "evidence_kind": "PROVIDER_REPORTED",
    }  # fmt: skip
    if not fetched.complete:
        obs["finding"] = "PAGINATION_INCOMPLETE_AT_CAP"
    return Result(FAILED if codes else VERIFIED, codes, obs)


# ============================================================ the run
def _default_session() -> Any:
    import requests  # noqa: PLC0415

    return requests.Session()


def _storable(ex: Exchange) -> tuple[Exchange, bool]:
    """An exchange whose body can be proven sanitized, or the same exchange with the body
    withheld (``False``): an unparseable success body is never written."""
    try:
        parse_body(ex)
    except SanitizationError:
        return Exchange(ex.method, ex.host, ex.path, ex.status, ex.rate_headers, ""), False
    return ex, True


def _near_ledger(root: Path) -> bool:
    """A ledger file beside or above the evidence root (names only; nothing opened)."""
    for directory in (root, root.parent):
        with contextlib.suppress(OSError):
            if any(name.startswith("ledger.") for name in os.listdir(directory)):
                return True
    return False


def run_probe(  # noqa: PLR0912, PLR0915 - one linear, audited script
    environ: Mapping[str, str],
    *,
    session_factory: Callable[[], Any] = _default_session,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    evidence_root: Path = EVIDENCE_ROOT,
    require_git_ignored: bool = True,
    guard: LedgerGuard | None = None,
) -> dict[str, Any]:
    """Run P1-P6 once and return the sanitized summary. Never starts acquisition."""
    plan = build_capability_probe_plan()
    if plan.continues_into_acquisition:  # pragma: no cover - plan invariant (tested)
        raise AssertionError("the capability probe must never continue into acquisition")
    budget = RequestBudget(max_requests=plan.max_http_attempts, deadline_s=plan.deadline_s)
    if monotonic is not None:
        budget.monotonic = monotonic
    started_at = now().astimezone(UTC)
    run_name = f"{plan.evidence_prefix}{started_at:%Y%m%dT%H%M%SZ}"
    execution: dict[str, Any] = {"status": "COMPLETED", "stop_code": None,
                                 "credential_source": None}  # fmt: skip
    results: dict[str, Result] = {r.request_id: Result(NOT_RUN) for r in plan.requests}
    checks: dict[str, Result] = {c.check_id: Result(NOT_RUN) for c in plan.checks}
    secrets: tuple[str, ...] = ()
    session: ProbeSession | None = None

    def stop(code: str) -> None:
        if execution["status"] == "COMPLETED":
            execution.update(status="STOPPED", stop_code=code)

    with ledger_guard(guard):
        try:
            loaded = load_credentials(environ)
            secrets = loaded.settings.secret_values()
            execution["credential_source"] = loaded.source
            if loaded.source != "file":
                stop("CREDENTIAL_SOURCE_NOT_FILE")
            elif loaded.settings.trading_enabled:
                stop("TRADING_ENABLED_REFUSED")
        except CredentialSourceError:
            stop("CREDENTIALS_UNAVAILABLE")
        except SettingsError:
            stop("CONFIGURATION_INVALID")
        if execution["status"] == "COMPLETED":
            budget.start()
            endpoints = frozenset((dict(plan.origins)[h], p) for h, p in plan.endpoints)
            inner = session_factory()
            session = ProbeSession(BudgetedSession(inner, budget), endpoints)
            transport = AlpacaReadTransport(
                loaded.settings,
                session=session,  # type: ignore[arg-type]
                connect_timeout_s=plan.connect_timeout_s,
                read_timeout_s=plan.read_timeout_s,
            )
            if getattr(inner, "trust_env", None) is not False:  # pragma: no cover - invariant
                stop("TRUST_ENV_NOT_DISABLED")
            wait = bounded_sleep(budget, *(() if sleep is None else (sleep,)))
            executor = ProbeExecutor(plan, transport, session, wait)
            fetched: dict[str, Fetched] = {}
            for req in plan.requests:
                if execution["status"] != "COMPLETED":
                    break
                try:
                    fetched[req.request_id] = executor.fetch(req)
                except _NotFound:
                    if req.path == CORPORATE_ACTIONS:
                        results[req.request_id] = Result(UNAVAILABLE, ["ENDPOINT_NOT_FOUND"])
                    else:
                        stop("HTTP_404")
                except ProbeDataError as exc:
                    results[req.request_id] = Result(FAILED, [exc.code])
                except InvalidBrokerDataError:
                    results[req.request_id] = Result(FAILED, ["INVALID_RESPONSE"])
                except _Stop as exc:
                    stop(exc.code)
                except (BudgetExhausted, WriteAttemptError, ProbeRefusal) as exc:
                    stop(exc.code)
            # ---- evaluate what was received (pure; no request) ----
            sessions: list[TradingSession] = []
            if "P1" in fetched:
                results["P1"], sessions = evaluate_trading_calendar(fetched["P1"].pages)
            if "P2" in fetched:
                results["P2"] = evaluate_settlement(fetched["P2"].pages, sessions)
            raw: list[DailyBar] = []
            if "P3" in fetched:
                depth, paging, raw = evaluate_p3(fetched["P3"], sessions, plan.page_limit)
                results["P3"] = Result(
                    FAILED if FAILED in (depth.outcome, paging.outcome) else depth.outcome,
                    depth.codes + paging.codes, {"sip_depth": depth.observations,
                                                 "pagination": paging.observations})  # fmt: skip
                checks["SIP-DEPTH"], checks["PAGINATION"] = depth, paging
            if "P4" in fetched:
                results["P4"] = evaluate_split(raw, fetched["P4"])
            if "P5" in fetched:
                results["P5"] = evaluate_identity(fetched["P5"], sessions)
            if "P6" in fetched:
                results["P6"] = evaluate_corporate_actions(
                    fetched["P6"], int(dict(plan.request("P6").params)["limit"])
                )
            for check, probe in (("CAL-DEPTH", "P1"), ("SETTLE-CYCLE", "P2"),
                                 ("SPLIT-ADJ", "P4"), ("TICKER-IDENTITY", "P5"),
                                 ("CA-SHAPE", "P6")):  # fmt: skip
                checks[check] = results[probe]
            if "P3" not in fetched and results["P3"].outcome != NOT_RUN:
                checks["SIP-DEPTH"] = checks["PAGINATION"] = results["P3"]
        exchanges = [] if session is None else list(session.exchanges)
        attempts = [] if session is None else list(session.attempts)
        stored: list[Exchange] = []
        for ex, attempt in zip(exchanges, attempts, strict=True):
            kept, ok = _storable(ex)
            attempt.body_stored = ok and attempt.status is not None
            stored.append(kept)
        summary: dict[str, Any] = {
            "plan": {"id": plan.plan_id, "fingerprint": plan.fingerprint},
            "execution": {
                **execution,
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
                "attempts_used": budget.used,
                "max_http_attempts": budget.max_requests,
                "deadline_s": plan.deadline_s,
                "elapsed_ms": round(budget.elapsed() * 1000) if session else 0,
            },
            "attempts": [a.to_dict() for a in attempts],
            "probes": {k: v.to_dict() for k, v in results.items()},
            "checks": {k: v.to_dict() for k, v in checks.items()},
            "rate_limits": rate_limit_evidence(exchanges),
            "methods_seen": sorted({e.method for e in exchanges}),
            "acquisition": {"started": False, "dataset_written": False},
        }
        try:
            identities = identity_values(stored, secrets)
            for record in summary["attempts"]:  # a header could echo a credential
                rid = record["request_id"]
                if rid is not None and any(v in rid for v in identities):
                    record["request_id"] = REDACTED
            summary["evidence_files"] = {
                _raw_name(n, ex): hashlib.sha256(sanitize_body(ex, identities).encode()
                                                 ).hexdigest()
                for n, ex in enumerate(stored)}  # fmt: skip
            if _near_ledger(evidence_root):
                raise EvidenceError("EVIDENCE_ROOT_NEAR_LEDGER")
            writer = EvidenceWriter(evidence_root, run=run_name,
                                    require_git_ignored=require_git_ignored)  # fmt: skip
            assert_sanitized(summary, identities)
            path = writer.write(stored, summary, identities, secrets=secrets)
            summary["evidence"] = {"written": True, "directory": path.parent.name}
        except SanitizationError as exc:
            summary = {"plan": summary["plan"],
                       "execution": {"status": "STOPPED",
                                     "stop_code": "EVIDENCE_SANITIZATION_FAILED"},
                       "evidence": {"written": False, "code": exc.code},
                       "acquisition": {"started": False, "dataset_written": False}}  # fmt: skip
        except EvidenceWriteError as exc:
            summary = {"plan": summary["plan"],
                       "execution": {"status": "STOPPED", "stop_code": exc.code},
                       "evidence": {"written": False, "code": exc.code,
                                    "staging_removed": exc.staging_removed},
                       "acquisition": {"started": False, "dataset_written": False}}  # fmt: skip
        except EvidenceError as exc:
            summary["evidence"] = {"written": False, "code": str(exc)}
    passed, code = probe_verdict(summary)
    summary["verdict"] = {"status": "PASSED" if passed else "FAILED", "code": code}
    return summary


def probe_verdict(summary: Mapping[str, Any]) -> tuple[bool, str]:
    """Recomputed from the summary: a completed run, published evidence, no FAILED check.
    Passing permits nothing by itself: acquisition needs its own authorization."""
    execution = summary.get("execution") or {}
    if execution.get("status") != "COMPLETED":
        return False, str(execution.get("stop_code") or "EXECUTION_NOT_COMPLETED")
    evidence = summary.get("evidence") or {}
    if evidence.get("written") is not True:
        return False, str(evidence.get("code") or "EVIDENCE_NOT_WRITTEN")
    if any(v.get("outcome") == FAILED for v in (summary.get("checks") or {}).values()):
        return False, "CHECKS_FAILED"
    return True, "OK"


def render(summary: Mapping[str, Any], secrets: Sequence[str] = ()) -> str:
    passed, code = probe_verdict(summary)
    execution = summary.get("execution") or {}
    lines = [f"probe verdict: {'PASSED' if passed else 'FAILED'} ({code})",
             f"plan: {summary['plan']['id']} {summary['plan']['fingerprint']}",
             f"execution: {execution.get('status')} (stop: {execution.get('stop_code')}); "
             f"attempts {execution.get('attempts_used')}/{execution.get('max_http_attempts')}",
             f"evidence written: {(summary.get('evidence') or {}).get('written') is True}",
             "acquisition started: False"]  # fmt: skip
    for name, value in (summary.get("checks") or {}).items():
        lines.append(f"{name:16s} {value['outcome']:13s} {','.join(value['codes'])}")
    return SecretRedactor(secrets).redact("\n".join(lines))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - live entry point
    args = list(sys.argv[1:] if argv is None else argv)
    plan = build_capability_probe_plan()
    if os.environ.get(ENABLE_VARIABLE) != "1":
        sys.stdout.write(f"refused: set {ENABLE_VARIABLE}=1 (authorized operator run only)\n")
        return 2
    if args != ["--plan-fingerprint", plan.fingerprint]:
        sys.stdout.write("refused: pass --plan-fingerprint <the authorized fingerprint>; "
                         "it does not match this plan\n")  # fmt: skip
        return 2
    summary = run_probe(os.environ)
    sys.stdout.write(render(summary) + "\n")
    sys.stdout.write(json.dumps({"evidence": summary.get("evidence")}) + "\n")
    return 0 if probe_verdict(summary)[0] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ALLOWED_ORIGINS",
    "LedgerAccessRefused",
    "LedgerGuard",
    "ProbeRefusal",
    "ProbeSession",
    "evaluate_corporate_actions",
    "evaluate_settlement",
    "evaluate_trading_calendar",
    "ledger_guard",
    "main",
    "probe_verdict",
    "render",
    "run_probe",
]
