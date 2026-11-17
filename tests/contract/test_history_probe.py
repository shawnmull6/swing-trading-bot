"""Step 16.16B: the historical capability probe runner, exercised fully OFFLINE.

A synthetic provider serves the calendars, bars and corporate actions through the real
GET transport; canary credentials come from a synthetic ``STB_ENV_FILE`` in a temporary
directory; the clock is fake. No network, no real credentials, no real ledger: the ledger
guard is armed on a temporary ``data`` directory (and the default guard is tested by
string comparison only)."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests

from swing_trading_bot.broker.alpaca_http import ALLOWED_ORIGINS
from swing_trading_bot.domain.calendar import MARKET_TZ
from swing_trading_bot.research.acquisition import build_capability_probe_plan
from tests.contract import history_probe
from tests.contract.history_probe import (
    PRODUCTION_DATA_DIR,
    LedgerAccessRefused,
    LedgerGuard,
    ProbeSession,
    ledger_guard,
    probe_verdict,
    render,
    run_probe,
)
from tests.contract.supervisor import (
    REPO,
    BudgetedSession,
    RequestBudget,
    WriteAttemptError,
)

KEY = "PKCANARYHISTORYKEY4242"
SECRET = "canary/History/Secret/4242424242424242"
NOW = datetime(2026, 11, 17, 1, 2, 3, tzinfo=UTC)
RUN_DIR = "history-probe-20261117T010203Z"
HOLIDAYS = frozenset({date(2020, 7, 3), date(2020, 9, 7), date(2020, 11, 26),
                      date(2020, 12, 25), date(2022, 6, 20), date(2024, 5, 27),
                      date(2025, 1, 9)})  # fmt: skip
T1 = date(2024, 5, 28)
SPLIT_DAY = date(2020, 8, 31)
RATE = {"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "199",
        "X-RateLimit-Reset": "1795000000"}  # fmt: skip
CA_ITEMS: list[tuple[str, dict[str, Any]]] = [
    ("cash_dividends", {"id": "d1", "symbol": "AAPL", "rate": Decimal("0.82"),
                        "ex_date": "2020-08-07", "payable_date": "2020-08-13"}),
    ("forward_splits", {"id": "s1", "symbol": "AAPL", "new_rate": 4, "old_rate": 1,
                        "ex_date": "2020-08-31", "payable_date": "2020-08-28"}),
    ("cash_dividends", {"id": "d2", "symbol": "SPY", "rate": Decimal("1.339"),
                        "ex_date": "2020-09-18", "payable_date": "2020-10-30"}),
    ("cash_dividends", {"id": "d3", "symbol": "AAPL", "rate": Decimal("0.205"),
                        "ex_date": "2020-11-06", "payable_date": "2020-11-12"}),
    ("cash_dividends", {"id": "d4", "symbol": "SPY", "rate": Decimal("1.58"),
                        "ex_date": "2020-12-18", "payable_date": "2021-01-29"}),
    ("cash_dividends", {"id": "d5", "symbol": "AAPL", "rate": Decimal("0.205"),
                        "ex_date": "2021-02-05", "payable_date": "2021-02-11"}),
    ("cash_dividends", {"id": "d6", "symbol": "SPY", "rate": Decimal("1.278"),
                        "ex_date": "2021-03-19", "payable_date": "2021-04-30"}),
    ("cash_dividends", {"id": "d7", "symbol": "AAPL", "rate": Decimal("0.22"),
                        "ex_date": "2021-05-07", "payable_date": "2021-05-13"}),
    ("cash_dividends", {"id": "d8", "symbol": "SPY", "rate": Decimal("1.376"),
                        "ex_date": "2021-06-18", "payable_date": "2021-07-30"}),
]  # fmt: skip


def dumps(value: object) -> str:
    """JSON with exact decimals written as numbers (as the provider does)."""

    def enc(v: object) -> object:
        if isinstance(v, Decimal):
            return f"__NUM__{v}__"
        raise TypeError(type(v).__name__)

    return re.sub(r'"__NUM__([^"]*)__"', r"\1", json.dumps(value, default=enc))


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


Scripted = Callable[[str, dict[str, Any]], requests.Response] | BaseException


def reply(url: str, status: int, body: str, headers: dict[str, str] | None = None,
          n: int = 0) -> requests.Response:  # fmt: skip
    r = requests.Response()
    r.url, r.status_code, r._content = url, status, body.encode()
    r.headers.update({**RATE, "X-Request-ID": f"req-{n:04d}", **(headers or {})})
    return r


def status(code: int, body: str = "{}", headers: dict[str, str] | None = None) -> Scripted:
    return lambda url, _kw: reply(url, code, body, headers)


class Provider:
    """A synthetic Alpaca: calendars, SIP bars, corporate actions, page tokens."""

    def __init__(self, clock: FakeTime, *, extra_holidays: frozenset[date] = frozenset(),
                 t1_from: date | None = T1, early_close: bool = True,
                 page_size: int | None = None, ca_page_size: int | None = None,
                 adjust: bool = True, meta_from: date | None = None,
                 calendar_end: date | None = None, cost_s: float = 0.01,
                 script: dict[int, Scripted] | None = None) -> None:  # fmt: skip
        self.clock = clock
        holidays = HOLIDAYS | extra_holidays
        d, self.days = date(2020, 6, 1), []
        while d <= date(2026, 12, 31):
            if d.weekday() < 5 and d not in holidays:
                self.days.append(d)
            d += timedelta(days=1)
        self.t1_from, self.early_close = t1_from, early_close
        self.page_size, self.ca_page_size = page_size, ca_page_size
        self.adjust, self.meta_from, self.calendar_end = adjust, meta_from, calendar_end
        self.cost_s = cost_s
        self.script = dict(script or {})
        self.calls: list[dict[str, Any]] = []
        self.trust_env = True

    # ---- session protocol
    def get(self, url: str, **kw: Any) -> requests.Response:
        self.calls.append({"url": url, **kw})
        self.clock.t += self.cost_s
        item = self.script.get(len(self.calls))
        if isinstance(item, BaseException):
            raise item
        if item is not None:
            return item(url, kw)
        return self.route(url, dict(kw.get("params") or {}))

    def route(self, url: str, params: dict[str, str]) -> requests.Response:
        path = urlsplit(url).path
        n = len(self.calls)
        if path == "/v2/calendar":
            return reply(url, 200, json.dumps(self.calendar(params)), n=n)
        if path == "/v2/stocks/bars":
            return reply(url, 200, dumps(self.bars(params)), n=n)
        if path == "/v1/corporate-actions":
            return reply(url, 200, dumps(self.actions(params)), n=n)
        return reply(url, 404, '{"message": "not found"}', n=n)

    # ---- data
    def settles(self, i: int) -> date:
        t1 = self.t1_from is not None and self.days[i] >= self.t1_from
        return self.days[i + (1 if t1 else 2)]

    def row(self, i: int) -> dict[str, str]:
        d = self.days[i]
        close = "13:00" if self.early_close and d == date(2020, 11, 27) else "16:00"
        return {"date": str(d), "open": "09:30", "close": close, "session_open": "0400",
                "session_close": "2000", "settlement_date": str(self.settles(i))}  # fmt: skip

    def calendar(self, params: dict[str, str]) -> list[dict[str, str]]:
        start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
        end = min(end, self.calendar_end or end)
        if params["date_type"] == "TRADING":
            return [self.row(i) for i, d in enumerate(self.days) if start <= d <= end]
        return [self.row(i) for i in range(len(self.days) - 2)
                if start <= self.settles(i) <= end]  # fmt: skip

    def bars(self, params: dict[str, str]) -> dict[str, Any]:
        sym, adj = params["symbols"], params["adjustment"]
        lo = datetime.fromisoformat(params["start"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(params["end"].replace("Z", "+00:00"))
        rows = []
        for d in self.days:
            t = datetime.combine(d, time(0), MARKET_TZ).astimezone(UTC)
            if not lo <= t <= hi or (sym == "META" and self.meta_from and d < self.meta_from):
                continue
            close = 200 if sym == "META" else (400 if d < SPLIT_DAY else 100)
            if sym == "AAPL" and adj == "split" and self.adjust:
                close = 100
            rows.append({"t": t.isoformat().replace("+00:00", "Z"), "o": close,
                         "h": close + 1, "l": close - 1, "c": close, "v": 1000, "n": 10,
                         "vw": close})  # fmt: skip
        size = self.page_size or int(params["limit"])
        offset = int(params.get("page_token", "tok-0").removeprefix("tok-"))
        page = rows[offset : offset + size]
        nxt = f"tok-{offset + size}" if offset + size < len(rows) else None
        return {"bars": {sym: page} if page else {}, "next_page_token": nxt}

    def actions(self, params: dict[str, str]) -> dict[str, Any]:
        size = self.ca_page_size or int(params["limit"])
        offset = int(params.get("page_token", "tok-0").removeprefix("tok-"))
        grouped: dict[str, list[dict[str, Any]]] = {}
        for kind, item in CA_ITEMS[offset : offset + size]:
            grouped.setdefault(kind, []).append(item)
        nxt = f"tok-{offset + size}" if offset + size < len(CA_ITEMS) else None
        return {"corporate_actions": grouped, "next_page_token": nxt}


def env(tmp_path: Path, extra: str = "", folder_name: str = "creds") -> dict[str, str]:
    folder = tmp_path / folder_name
    folder.mkdir(exist_ok=True)
    path = folder / "paper.env"
    path.write_text(f"ALPACA_API_KEY={KEY}\nALPACA_SECRET_KEY={SECRET}\nTRADING_MODE=paper\n"
                    + extra)  # fmt: skip
    path.chmod(0o600)
    return {"STB_ENV_FILE": str(path)}


def run(
    tmp_path: Path,
    provider: Provider,
    clock: FakeTime,
    *,
    environ: dict[str, str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    return run_probe(
        environ if environ is not None else env(tmp_path),
        session_factory=lambda: provider,
        sleep=clock.sleep,
        monotonic=clock,
        now=lambda: NOW,
        evidence_root=root or tmp_path / "evidence",
        require_git_ignored=False,
        guard=LedgerGuard((os.path.abspath(tmp_path / "data"),)),
    )


def go(tmp_path: Path, **provider: Any) -> tuple[dict[str, Any], Provider, FakeTime]:
    clock = FakeTime()
    p = Provider(clock, **provider)
    return run(tmp_path, p, clock), p, clock


def outcomes(summary: dict[str, Any]) -> dict[str, str]:
    return {k: v["outcome"] for k, v in summary["checks"].items()}


def evidence_text(root: Path) -> str:
    return "".join(p.read_text() for p in sorted(root.rglob("*")) if p.is_file())


ALL_VERIFIED = {"CAL-DEPTH": "VERIFIED", "SETTLE-CYCLE": "VERIFIED", "SIP-DEPTH": "VERIFIED",
                "PAGINATION": "VERIFIED", "SPLIT-ADJ": "VERIFIED",
                "TICKER-IDENTITY": "VERIFIED", "CA-SHAPE": "VERIFIED"}  # fmt: skip


# ============================================================ complete run
def test_complete_run_sends_exactly_the_reviewed_requests(tmp_path: Path) -> None:
    summary, provider, _ = go(tmp_path)
    assert summary["execution"]["status"] == "COMPLETED"
    assert summary["execution"]["attempts_used"] == 8 == len(provider.calls)
    assert outcomes(summary) == ALL_VERIFIED
    assert summary["verdict"] == {"status": "PASSED", "code": "OK"}
    plan = build_capability_probe_plan()
    expected = []
    for req in plan.requests:
        origin = dict(plan.origins)[str(req.host)]
        expected.extend([(origin + req.path, dict(req.params))] * req.expected_pages)
    sent = [(c["url"], {k: v for k, v in c["params"].items() if k != "page_token"})
            for c in provider.calls]  # fmt: skip
    assert sent == expected
    assert [("page_token" in c["params"]) for c in provider.calls] == [
        False, False, False, True, False, False, False, True]  # fmt: skip
    for call in provider.calls:
        assert call["allow_redirects"] is False
        assert call["timeout"] == (5, 20)
        assert set(call["headers"]) == {"APCA-API-KEY-ID", "APCA-API-SECRET-KEY", "User-Agent"}
    assert provider.trust_env is False  # no environment proxies or .netrc
    assert summary["methods_seen"] == ["GET"]
    assert summary["acquisition"] == {"started": False, "dataset_written": False}
    p = summary["probes"]
    assert p["P1"]["observations"]["first"] == "2020-07-01"
    assert p["P1"]["observations"]["last"] == "2026-10-30"
    assert p["P2"]["observations"]["transition"] == {"2024-05-24": "2024-05-29",
                                                     "2024-05-28": "2024-05-29"}  # fmt: skip
    assert p["P4"]["observations"]["derived"] == ["AAPL 2020-08-31 4:1"]
    assert p["P5"]["observations"]["fb_requests_needed"] is False
    ca = p["P6"]["observations"]
    assert ca["items_per_page"] == [{"cash_dividends": 4, "forward_splits": 1},
                                    {"cash_dividends": 4}]  # fmt: skip
    assert ca["limit_semantics"] == "TOTAL_ITEMS" and ca["aapl_split_2020_08_31_listed"]
    assert ca["shape"]["cash_dividends"]["rate"] == ["number"]
    assert ca["shape"]["forward_splits"]["new_rate"] == ["integer"]
    assert ca["symbols_seen"] == ["AAPL", "SPY"]


def test_p3_expected_sessions_come_from_the_calendar(tmp_path: Path) -> None:
    summary, _, _ = go(tmp_path)
    paging = summary["checks"]["PAGINATION"]["observations"]
    assert summary["checks"]["SIP-DEPTH"]["observations"]["expected_sessions"] == 128
    assert paging["expected_bars_per_page"] == [100, 28] == paging["bars_per_page"]
    # one more synthetic holiday: the expectation follows the calendar, not a constant
    other = tmp_path / "other"
    other.mkdir()
    summary, _, _ = go(other, extra_holidays=frozenset({date(2020, 10, 12)}))
    assert outcomes(summary) == ALL_VERIFIED
    assert summary["checks"]["SIP-DEPTH"]["observations"]["expected_sessions"] == 127
    assert summary["checks"]["PAGINATION"]["observations"]["bars_per_page"] == [100, 27]


def test_without_a_calendar_p3_counts_are_inconclusive(tmp_path: Path) -> None:
    summary, _, _ = go(tmp_path, script={1: status(200, "{}")})
    checks = outcomes(summary)
    assert checks["CAL-DEPTH"] == "FAILED"
    assert summary["probes"]["P1"]["codes"] == ["UNEXPECTED_SHAPE"]
    assert checks["SETTLE-CYCLE"] == "INCONCLUSIVE"
    assert checks["SIP-DEPTH"] == "INCONCLUSIVE" and checks["TICKER-IDENTITY"] == "INCONCLUSIVE"
    assert checks["SPLIT-ADJ"] == "VERIFIED"


# ============================================================ request limits
def test_eleventh_attempt_is_refused_before_transmission(tmp_path: Path) -> None:
    script = {n: status(429) for n in (1, 3, 5, 7, 9)}
    summary, provider, clock = go(tmp_path, script=script)
    assert len(provider.calls) == 10  # P1..P4 each needed a retry; P5 was never sent
    ex = summary["execution"]
    assert ex["status"] == "STOPPED" and ex["stop_code"] == "REQUEST_BUDGET_EXCEEDED"
    assert ex["attempts_used"] == ex["max_http_attempts"] == 10
    assert summary["probes"]["P5"]["outcome"] == "NOT_RUN"
    assert summary["probes"]["P6"]["outcome"] == "NOT_RUN"
    assert len(summary["attempts"]) == 10
    assert clock.sleeps == [0.5] * 5


def test_at_most_one_retry_per_page_and_only_for_429_503_timeout(tmp_path: Path) -> None:
    cases: list[tuple[dict[int, Scripted], str, int]] = [
        ({1: status(429), 2: status(429)}, "RETRY_EXHAUSTED_HTTP_429", 2),
        ({1: status(503), 2: status(503)}, "RETRY_EXHAUSTED_HTTP_503", 2),
        ({1: requests.Timeout(), 2: requests.Timeout()}, "RETRY_EXHAUSTED_TIMEOUT", 2),
        ({1: status(500)}, "HTTP_500", 1),
        ({1: status(502)}, "HTTP_502", 1),
        ({1: requests.ConnectionError()}, "NETWORK_ERROR", 1),
        ({1: status(400)}, "HTTP_400", 1),
        ({1: status(401)}, "HTTP_401", 1),
        ({1: status(403, '{"message": "forbidden"}')}, "HTTP_403", 1),
        ({3: status(403, '{"message": "subscription does not permit querying recent '
                         'SIP data"}')}, "HTTP_403_ENTITLEMENT", 3),
        ({3: status(403, '{"message": "forbidden"}')}, "HTTP_403", 3),
        ({1: status(404)}, "HTTP_404", 1),
        ({5: status(404)}, "HTTP_404", 5),
    ]  # fmt: skip
    for n, (script, code, calls) in enumerate(cases):
        root = tmp_path / f"case{n}"
        root.mkdir()
        summary, provider, _ = go(root, script=script)
        assert summary["execution"]["stop_code"] == code, code
        assert len(provider.calls) == calls, code
        assert summary["verdict"]["status"] == "FAILED"
        assert all(a["error"] is None or a["error"] in {"Timeout", "ConnectionError"}
                   for a in summary["attempts"])  # fmt: skip
    recoverable: list[dict[int, Scripted]] = [
        {1: status(503)}, {1: requests.Timeout()}, {4: status(429)}]  # fmt: skip
    for n, script in enumerate(recoverable):
        root = tmp_path / f"ok{n}"
        root.mkdir()
        summary, provider, _ = go(root, script=script)
        assert summary["execution"]["status"] == "COMPLETED"
        assert len(provider.calls) == 9 and outcomes(summary) == ALL_VERIFIED


def test_retry_after_is_honoured_up_to_the_cap(tmp_path: Path) -> None:
    _, _, clock = go(tmp_path, script={1: status(429, headers={"Retry-After": "3"})})
    assert clock.sleeps == [3]
    other = tmp_path / "x"
    other.mkdir()
    _, _, clock = go(other, script={1: status(429, headers={"Retry-After": "60"})})
    assert clock.sleeps == [4]


def test_deadline_and_timeouts_bounded_by_remaining_time(tmp_path: Path) -> None:
    summary, provider, _ = go(tmp_path, cost_s=110.0)
    assert provider.calls[0]["timeout"] == (5, 20)
    assert provider.calls[1]["timeout"] == (5, 10.0)  # 10 s were left
    assert summary["execution"]["stop_code"] == "DEADLINE_EXCEEDED"
    assert len(provider.calls) == 2


def test_redirects_and_foreign_response_hosts_stop_the_run(tmp_path: Path) -> None:
    moved = status(302, "", {"Location": "https://example.invalid/x"})
    summary, provider, _ = go(tmp_path, script={1: moved})
    assert summary["execution"]["stop_code"] == "REDIRECT_REFUSED" and len(provider.calls) == 1
    other = tmp_path / "x"
    other.mkdir()
    foreign: Scripted = lambda url, _kw: reply("https://example.invalid/v2/calendar", 200, "[]")  # noqa: E731
    summary, _, _ = go(other, script={1: foreign})
    assert summary["execution"]["stop_code"] == "UNEXPECTED_RESPONSE_HOST"


def test_probe_session_enforces_the_exact_endpoint_allowlist() -> None:
    clock = FakeTime()
    provider = Provider(clock)
    budget = RequestBudget(max_requests=10, deadline_s=120, monotonic=clock)
    plan = build_capability_probe_plan()
    endpoints = frozenset((dict(plan.origins)[h], p) for h, p in plan.endpoints)
    session = ProbeSession(BudgetedSession(provider, budget), endpoints)
    with pytest.raises(WriteAttemptError, match="ATTEMPT_WITHOUT_PROBE_CONTEXT"):
        session.get("https://paper-api.alpaca.markets/v2/calendar", allow_redirects=False)
    session.context = ("P1", 1, 1)
    refused = ("https://paper-api.alpaca.markets/v2/account",
               "https://paper-api.alpaca.markets/v2/orders",
               "https://paper-api.alpaca.markets/v2/positions",
               "https://paper-api.alpaca.markets/v2/account/activities",
               "https://data.alpaca.markets/v1beta1/corporate-actions",
               "https://paper-api.alpaca.markets/v2/corporate_actions/announcements",
               "https://data.alpaca.markets/v2/calendar",
               "https://paper-api.alpaca.markets/v1/corporate-actions",
               "https://api.alpaca.markets/v2/calendar",
               "http://paper-api.alpaca.markets/v2/calendar",
               "https://paper-api.alpaca.markets/v2/calendar?date_type=TRADING")  # fmt: skip
    for url in refused:
        with pytest.raises(WriteAttemptError, match="ENDPOINT_NOT_ALLOWLISTED"):
            session.get(url, allow_redirects=False)
    for verb in ("post", "put", "patch", "delete", "request", "head", "options"):
        with pytest.raises(WriteAttemptError):
            getattr(session, verb)
    assert budget.used == 0 and provider.calls == []
    assert {o for o, _ in endpoints} == set(ALLOWED_ORIGINS.values())


# ============================================================ responses
def test_invalid_json_fails_the_probe_and_is_never_stored(tmp_path: Path) -> None:
    summary, provider, _ = go(tmp_path, script={1: status(200, "not json {")})
    assert summary["probes"]["P1"] == {"outcome": "FAILED", "codes": ["INVALID_RESPONSE"],
                                       "observations": {}}  # fmt: skip
    assert summary["execution"]["status"] == "COMPLETED" and len(provider.calls) == 8
    first = summary["attempts"][0]
    assert first["body_stored"] is False and first["body_bytes"] == len("not json {")
    assert first["body_sha256"] == hashlib.sha256(b"not json {").hexdigest()
    stored = json.loads((tmp_path / "evidence" / RUN_DIR / "raw" /
                         "000-paper-api_v2_calendar.json").read_text())  # fmt: skip
    assert stored == {"status": 200, "body": ""}
    assert "not json" not in evidence_text(tmp_path / "evidence")


def test_unexpected_shapes_fail_closed(tmp_path: Path) -> None:
    def bars_with(symbol: str) -> Scripted:
        body: dict[str, Any] = {"bars": {symbol: []}, "next_page_token": None}
        return lambda url, _kw: reply(url, 200, json.dumps(body))

    cases: list[tuple[dict[int, Scripted], str, str]] = [
        ({2: status(200, '{"rows": []}')}, "P2", "UNEXPECTED_SHAPE"),
        ({3: bars_with("MSFT")}, "P3", "UNEXPECTED_SYMBOL_OR_SHAPE"),
        ({3: status(200, '{"bars": []}')}, "P3", "UNEXPECTED_SHAPE"),
        ({5: status(200, '{"bars": {}}')}, "P4", "UNEXPECTED_SHAPE"),
        ({7: status(200, "[]")}, "P6", "UNEXPECTED_SHAPE"),
        ({7: status(200, '{"corporate_actions": [], "next_page_token": null}')}, "P6",
         "UNEXPECTED_SHAPE"),
        ({1: status(200, '[{"date": "2020-07-01"}]')}, "P1", "ROW_INVALID"),
    ]  # fmt: skip
    for n, (script, probe, code) in enumerate(cases):
        root = tmp_path / f"c{n}"
        root.mkdir()
        summary, _, _ = go(root, script=script)
        assert summary["probes"][probe]["outcome"] == "FAILED", (probe, code)
        assert code in summary["probes"][probe]["codes"], (probe, summary["probes"][probe])
        assert summary["execution"]["status"] == "COMPLETED"
        assert summary["verdict"]["code"] == "CHECKS_FAILED"


def test_pagination_tokens_and_caps(tmp_path: Path) -> None:
    def page(token: object) -> Scripted:
        body = {"bars": {}, "next_page_token": token}
        return lambda url, _kw: reply(url, 200, json.dumps(body))

    cases: list[tuple[dict[int, Scripted], str]] = [
        ({4: page("tok-100")}, "REPEATED_PAGE_TOKEN"),
        ({3: page("")}, "INVALID_PAGE_TOKEN"),
        ({3: page(7)}, "INVALID_PAGE_TOKEN"),
    ]
    for n, (script, code) in enumerate(cases):
        root = tmp_path / f"t{n}"
        root.mkdir()
        summary, _, _ = go(root, script=script)
        assert summary["probes"]["P3"]["codes"] == [code]
        assert summary["checks"]["PAGINATION"]["outcome"] == "FAILED"
    root = tmp_path / "cap"
    root.mkdir()
    summary, provider, _ = go(root, page_size=50)  # the provider ignores limit=100
    assert "PAGE_CAP_EXCEEDED" in summary["checks"]["PAGINATION"]["codes"]
    assert summary["checks"]["SIP-DEPTH"]["codes"] == ["SESSIONS_MISSING"]
    assert len(provider.calls) == 8  # never a third P3 page
    root = tmp_path / "ca"
    root.mkdir()
    summary, provider, _ = go(root, ca_page_size=3)
    assert summary["checks"]["CA-SHAPE"]["outcome"] == "VERIFIED"
    assert summary["probes"]["P6"]["observations"]["finding"] == "PAGINATION_INCOMPLETE_AT_CAP"
    assert len(provider.calls) == 8
    root = tmp_path / "ca-all"
    root.mkdir()
    summary, provider, _ = go(root, ca_page_size=9)
    assert summary["probes"]["P6"]["observations"]["limit_semantics"] == "NOT_TOTAL_ITEMS"
    assert len(provider.calls) == 7


def test_corporate_action_404_is_unavailable_without_fallback(tmp_path: Path) -> None:
    summary, provider, _ = go(tmp_path, script={7: status(404, '{"message": "Not Found"}')})
    assert summary["checks"]["CA-SHAPE"]["outcome"] == "UNAVAILABLE"
    assert summary["probes"]["P6"]["codes"] == ["ENDPOINT_NOT_FOUND"]
    assert summary["execution"]["status"] == "COMPLETED" and len(provider.calls) == 7
    assert [urlsplit(c["url"]).path for c in provider.calls].count("/v1/corporate-actions") == 1


def test_provider_disagreements_are_reported(tmp_path: Path) -> None:
    variants: list[tuple[dict[str, Any], str, str]] = [
        ({"t1_from": None}, "SETTLE-CYCLE", "TRANSITION_NOT_AS_EXPECTED"),
        ({"t1_from": None}, "SETTLE-CYCLE", "SETTLEMENT_DISTANCE_UNEXPECTED"),
        ({"early_close": False}, "CAL-DEPTH", "EXPECTED_EARLY_CLOSE_MISSING"),
        ({"calendar_end": date(2026, 6, 30)}, "CAL-DEPTH", "LAST_SESSION_UNEXPECTED"),
        ({"extra_holidays": frozenset()}, "CAL-DEPTH", ""),
        ({"adjust": False}, "SPLIT-ADJ", "SPLIT_NOT_AS_EXPECTED"),
    ]  # fmt: skip
    for n, (kw, check, code) in enumerate(variants):
        root = tmp_path / f"v{n}"
        root.mkdir()
        summary, _, _ = go(root, **kw)
        if code:
            assert summary["checks"][check]["outcome"] == "FAILED", (kw, check)
            assert code in summary["checks"][check]["codes"]
    root = tmp_path / "meta"
    root.mkdir()
    summary, _, _ = go(root, meta_from=date(2022, 6, 9))
    identity = summary["checks"]["TICKER-IDENTITY"]
    assert identity["outcome"] == "VERIFIED"
    assert identity["codes"] == ["META_PRE_CHANGE_BARS_MISSING"]
    assert identity["observations"]["fb_requests_needed"] is True


# ============================================================ credentials and evidence
def test_credentials_only_from_an_explicit_file_with_trading_disabled(tmp_path: Path) -> None:
    def never() -> Provider:
        raise AssertionError("no session may be created")

    cases = [
        ({"ALPACA_API_KEY": KEY, "ALPACA_SECRET_KEY": SECRET, "TRADING_MODE": "paper"},
         "CREDENTIAL_SOURCE_NOT_FILE"),
        (env(tmp_path, "TRADING_ENABLED=true\n", "creds-enabled"), "TRADING_ENABLED_REFUSED"),
        ({}, "CREDENTIALS_UNAVAILABLE"),
        ({**env(tmp_path), "ALPACA_API_KEY": KEY}, "CREDENTIALS_UNAVAILABLE"),
    ]  # fmt: skip
    for n, (environ, code) in enumerate(cases):
        summary = run_probe(environ, session_factory=never, now=lambda: NOW,
                            evidence_root=tmp_path / f"e{n}", require_git_ignored=False,
                            guard=LedgerGuard((str(tmp_path / "data"),)))  # fmt: skip
        assert summary["execution"]["stop_code"] == code
        assert summary["execution"]["attempts_used"] == 0
        assert KEY not in json.dumps(summary) and SECRET not in json.dumps(summary)
    bad = tmp_path / "bad"
    bad.mkdir()
    path = bad / "paper.env"
    path.write_text(f"ALPACA_API_KEY={KEY}\nALPACA_SECRET_KEY={SECRET}\n")
    path.chmod(0o600)
    summary = run_probe({"STB_ENV_FILE": str(path)}, session_factory=never, now=lambda: NOW,
                        evidence_root=tmp_path / "e-bad", require_git_ignored=False,
                        guard=LedgerGuard((str(tmp_path / "data"),)))  # fmt: skip
    assert summary["execution"]["stop_code"] == "CONFIGURATION_INVALID"


def test_canary_credentials_never_reach_evidence_or_output(tmp_path: Path) -> None:
    def calendar_with_canaries(url: str, kw: dict[str, Any]) -> requests.Response:
        rows = provider.calendar(dict(kw["params"]))
        rows[0] = {**rows[0], "note": f"key {KEY} secret {SECRET}"}
        return reply(url, 200, json.dumps(rows), {"X-Request-ID": KEY})

    clock = FakeTime()
    echo = f'{{"message": "key {KEY} secret {SECRET}"}}'
    provider = Provider(clock, script={1: calendar_with_canaries, 7: status(404, echo)})
    summary = run(tmp_path, provider, clock)
    assert summary["evidence"]["written"] is True
    text = evidence_text(tmp_path / "evidence")
    for needle in (KEY, SECRET, "APCA-API-KEY-ID", "APCA-API-SECRET-KEY", "User-Agent"):
        assert needle not in text, needle
    assert "***REDACTED***" in text
    assert KEY not in render(summary, (KEY, SECRET)) and SECRET not in json.dumps(summary)
    stored = json.loads((tmp_path / "evidence" / RUN_DIR / "summary.json").read_text())
    assert stored["attempts"][0]["request_id"] == "***REDACTED***"  # a header echoing a key


def test_evidence_layout_permissions_and_write_once(tmp_path: Path) -> None:
    clock = FakeTime()
    provider = Provider(clock)
    summary = run(tmp_path, provider, clock)
    root = tmp_path / "evidence"
    run_dir = root / RUN_DIR
    assert summary["evidence"] == {"written": True, "directory": RUN_DIR}
    assert sorted(p.name for p in root.iterdir()) == [RUN_DIR]  # no staging left
    for d in (root, run_dir, run_dir / "raw"):
        assert stat.S_IMODE(d.lstat().st_mode) == 0o700 and not d.is_symlink()
    files = sorted(p for p in run_dir.rglob("*") if p.is_file())
    assert len(files) == 9  # 8 bodies + summary.json
    for f in files:
        assert stat.S_IMODE(f.lstat().st_mode) == 0o600 and not f.is_symlink()
    stored = json.loads((run_dir / "summary.json").read_text())
    for name, digest in stored["evidence_files"].items():
        assert hashlib.sha256((run_dir / "raw" / name).read_bytes()).hexdigest() == digest
    a = stored["attempts"][3]
    assert (a["probe"], a["page"], a["attempt"], a["status"]) == ("P3", 2, 1, 200)
    assert a["request_id"] == "req-0004" and a["elapsed_ms"] == 10
    assert a["params"]["page_token_sha256"] == hashlib.sha256(b"tok-100").hexdigest()[:16]
    assert "page_token" not in a["params"]
    before = {p: p.read_bytes() for p in files}
    again = run(tmp_path, Provider(FakeTime()), FakeTime())
    assert again["evidence"] == {"written": False, "code": "EVIDENCE_DESTINATION_EXISTS"}
    assert {p: p.read_bytes() for p in files} == before


def test_evidence_and_fingerprints_are_deterministic(tmp_path: Path) -> None:
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        clock = FakeTime()
        run(tmp_path, Provider(clock), clock, root=root)
    trees = [{p.relative_to(r): p.read_bytes() for p in sorted(r.rglob("*")) if p.is_file()}
             for r in roots]  # fmt: skip
    assert trees[0] == trees[1] and len(trees[0]) == 9
    summary = json.loads((roots[0] / RUN_DIR / "summary.json").read_text())
    assert summary["plan"]["fingerprint"] == build_capability_probe_plan().fingerprint


def test_failed_sanitization_publishes_nothing(tmp_path: Path) -> None:
    def colliding(url: str, kw: dict[str, Any]) -> requests.Response:
        rows: list[dict[str, Any]] = list(provider.calendar(dict(kw["params"])))
        rows[0] = {**rows[0], KEY: 1, "***REDACTED***": 2}  # keys collide after redaction
        return reply(url, 200, json.dumps(rows))

    clock = FakeTime()
    provider = Provider(clock, script={1: colliding})
    summary = run(tmp_path, provider, clock)
    assert summary["execution"]["stop_code"] == "EVIDENCE_SANITIZATION_FAILED"
    assert summary["evidence"] == {"written": False, "code": "EVIDENCE_KEY_COLLISION"}
    root = tmp_path / "evidence"
    assert not root.exists() or list(root.iterdir()) == []
    assert "checks" not in summary and probe_verdict(summary) == (
        False, "EVIDENCE_SANITIZATION_FAILED")  # fmt: skip


# ============================================================ ledger isolation
def test_ledger_guard_refuses_sqlite_and_protected_paths(tmp_path: Path) -> None:
    protected = tmp_path / "data"
    protected.mkdir()
    target = protected / "ledger.sqlite3"
    outside = tmp_path / "outside.txt"
    with ledger_guard(LedgerGuard((str(protected),))):
        for attempt in (lambda: sqlite3.connect(":memory:"),
                        lambda: sqlite3.connect(str(tmp_path / "elsewhere.db")),
                        lambda: target.open("w"),
                        lambda: os.listdir(protected),
                        lambda: list(os.scandir(protected)),
                        lambda: os.rename(outside, protected / "x"),
                        lambda: os.chmod(protected, 0o700),
                        lambda: os.remove(protected / "ledger.sqlite3-wal"),
                        lambda: os.mkdir(protected / "sub")):  # fmt: skip
            with pytest.raises(LedgerAccessRefused):
                attempt()
        outside.write_text("fine")  # outside the protected directory
    assert not target.exists() and not (protected / "sub").exists()
    assert list(protected.iterdir()) == []
    sqlite3.connect(":memory:").close()  # disarmed after the block


def test_default_guard_names_the_production_data_directory_without_touching_it() -> None:
    guard = LedgerGuard()
    assert guard.protected == (os.path.abspath(REPO / "data"),)
    assert PRODUCTION_DATA_DIR == REPO / "data"
    synthetic = str(REPO / "data" / "nonexistent-probe-guard-check")
    with pytest.raises(LedgerAccessRefused):
        guard.check("open", (synthetic, "r", 0))  # a pure string check, no syscall
    with pytest.raises(LedgerAccessRefused):
        guard.check("sqlite3.connect", (":memory:",))
    guard.check("open", (str(REPO / "contract-evidence" / "x"), "r", 0))
    guard.check("open", (3, "r", 0))  # file descriptors are not paths


def test_runner_refuses_ledger_access_during_a_run(tmp_path: Path) -> None:
    class Sneaky(Provider):
        def get(self, url: str, **kw: Any) -> requests.Response:
            sqlite3.connect(":memory:")
            return super().get(url, **kw)

    clock = FakeTime()
    with pytest.raises(LedgerAccessRefused):
        run(tmp_path, Sneaky(clock), clock)
    assert not (tmp_path / "evidence").exists()


def test_evidence_root_beside_a_ledger_is_refused(tmp_path: Path) -> None:
    parent = tmp_path / "site"
    parent.mkdir()
    (parent / "ledger.sqlite3").write_bytes(b"")  # synthetic name only
    clock = FakeTime()
    summary = run(tmp_path, Provider(clock), clock, root=parent / "evidence")
    assert summary["evidence"] == {"written": False, "code": "EVIDENCE_ROOT_NEAR_LEDGER"}
    assert not (parent / "evidence").exists()


def test_runner_imports_no_ledger_persistence_or_dataset_code() -> None:
    code = (
        "import sys\n"
        "import tests.contract.history_probe\n"
        "bad = sorted(m for m in sys.modules if m == 'sqlite3' or m.startswith(("
        "'_sqlite3', 'swing_trading_bot.ledger', 'swing_trading_bot.oms', "
        "'swing_trading_bot.app.cli', 'swing_trading_bot.app.commands', "
        "'swing_trading_bot.app.runtime', 'swing_trading_bot.research.dataset')))\n"
        "print(bad)\n"
    )  # fmt: skip
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True,  # noqa: S603
                         text=True, timeout=60, check=True)  # fmt: skip
    assert out.stdout.strip() == "[]"
    source = Path(history_probe.__file__).read_text()
    tree = ast.parse(source)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    banned = ("sqlite3", "swing_trading_bot.ledger", "swing_trading_bot.research.dataset")
    assert not any(m.startswith(banned) for m in imported)
    for needle in ("ledger.sqlite3", "Database", "write_dataset", "build_acquisition_plan",
                   "submit_order", "TradingClient"):  # fmt: skip
        assert needle not in source, needle
    params = set(inspect.signature(run_probe).parameters)
    assert not any(word in p for p in params for word in ("ledger", "db", "database"))


def test_a_run_publishes_no_dataset_and_writes_only_evidence(tmp_path: Path) -> None:
    clock = FakeTime()
    summary = run(tmp_path, Provider(clock), clock)
    assert summary["acquisition"] == {"started": False, "dataset_written": False}
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert all(p.startswith(("creds/", f"evidence/{RUN_DIR}/")) for p in written)
    assert not (tmp_path / "data").exists()


def test_verdict_and_render(tmp_path: Path) -> None:
    summary, _, _ = go(tmp_path)
    text = render(summary, (KEY, SECRET))
    assert text.splitlines()[0] == "probe verdict: PASSED (OK)"
    assert "acquisition started: False" in text
    assert build_capability_probe_plan().fingerprint in text
    failed = {"execution": {"status": "COMPLETED"}, "evidence": {"written": True},
              "checks": {"X": {"outcome": "FAILED"}}}  # fmt: skip
    assert probe_verdict(failed) == (False, "CHECKS_FAILED")
