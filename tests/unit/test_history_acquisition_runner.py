"""The historical acquisition runner (Step 16.17A): the guarded read-only transport, the
fetch adapter, authorized sessions, recovery, publication and isolation.

Every HTTP response is MOCKED (``FakeHttp`` stands in for ``requests.Session`` underneath
the real ``AlpacaReadTransport``); every directory is a synthetic temporary one with a
placeholder ``ledger.sqlite3`` that is never opened. No credential is read except
synthetic files created by the tests themselves."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.broker.alpaca_http import AlpacaReadTransport
from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.config.settings import settings_from_mapping
from swing_trading_bot.history_acquisition import runner as runner_mod
from swing_trading_bot.history_acquisition.runner import (
    APPROVED_ACQUISITION_PLAN_FINGERPRINT,
    APPROVED_PROBE_PLAN_FINGERPRINT,
    ENABLE_VARIABLE,
    Authorization,
    Connect,
    LedgerAccessRefused,
    ProbeGates,
    ResearchPaths,
    RunnerRefusedError,
    check_gates,
    gates_from_probe_summary,
    load_probe_gates,
    main,
    publish,
    recover,
    research_guard,
    run_authorized_session,
    status,
)
from swing_trading_bot.history_acquisition.transport import (
    ACQUISITION_ENDPOINTS,
    MAX_RETRY_AFTER,
    AcquisitionFetch,
    GuardedHttpSession,
    LiveConnectionRefusedError,
    TransportRefusedError,
    canonical_body,
    connect_live,
    parse_retry_after,
    plan_endpoints,
    refusal_reason,
    transport_summary,
)
from swing_trading_bot.research.acquisition import (
    AcquisitionPlan,
    BatchKind,
    build_acquisition_plan,
    build_capability_probe_plan,
    request_accounting,
)
from swing_trading_bot.research.corporate_actions import decode
from swing_trading_bot.research.dataset import DatasetState, load_dataset
from swing_trading_bot.research.staging import (
    MIN_ATTEMPT_TIME,
    RECEIVED_DIGEST_SCOPE,
    STORED_DIGEST_SCOPE,
    FetchedPage,
    IncompatiblePlanError,
    IncompleteAcquisitionError,
    StagingIntegrityError,
    StagingStore,
    StopReason,
    retry_wait,
)
from swing_trading_bot.research.universe import (
    AssetClass,
    MembershipPolicy,
    ResearchUniverse,
    UniverseMember,
)
from tests.support.research_fixture import synthetic_bars, synthetic_sessions

PKG = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "history_acquisition"
UNIVERSE = ResearchUniverse(
    "synthetic-2", MembershipPolicy.FIXED_PRESENT_DAY_PILOT,
    (UniverseMember("AAA", "Synthetic", AssetClass.EQUITY),
     UniverseMember("META", "Synthetic", AssetClass.EQUITY)), False, ("SYNTHETIC",))  # fmt: skip
WARMUP, START, END = date(2020, 7, 1), date(2021, 1, 1), date(2021, 6, 30)
SESSIONS = synthetic_sessions(date(2020, 5, 1), date(2021, 8, 31))
GATES = {"CAL-DEPTH": True, "SETTLE-CYCLE": True, "SIP-DEPTH": True, "PAGINATION": True,
         "CA-SHAPE": True, "TICKER-IDENTITY": True}  # fmt: skip
PROBE = ProbeGates("history-probe-20261117T025839Z", "a" * 64, GATES)
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
FAKE = {"ALPACA_API_KEY": "PKFAKETESTKEY000000001",
        "ALPACA_SECRET_KEY": "fakeTestSecretValue/DoNotUse/0000000000001",
        "TRADING_MODE": "paper"}  # fmt: skip
SETTINGS = settings_from_mapping(FAKE)
DATA, TRADING = "https://data.alpaca.markets", "https://paper-api.alpaca.markets"
FULL = timedelta(seconds=300)


def plan() -> AcquisitionPlan:
    return build_acquisition_plan(UNIVERSE, warmup_start=WARMUP, research_start=START,
                                  research_end=END)  # fmt: skip


def _json(obj: object) -> str:
    def enc(o: object) -> str:
        if isinstance(o, Decimal):
            return f"@@{o}@@"
        raise TypeError(o)

    return json.dumps(obj, default=enc).replace('"@@', "").replace('@@"', "")


def dividend(pid: str, sym: str, ex: date, pay_lag: int = 6,
             rate: str = "0.25") -> dict[str, object]:  # fmt: skip
    pay = ex + timedelta(days=pay_lag)
    return {"id": pid, "symbol": sym, "cusip": "0", "ex_date": str(ex),
            "record_date": str(ex + timedelta(days=1)), "payable_date": str(pay),
            "process_date": str(pay), "rate": Decimal(rate), "special": False,
            "foreign": False}  # fmt: skip


def response(status: int, body: str, url: str,
             headers: Mapping[str, str] | None = None) -> requests.Response:  # fmt: skip
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.url = url
    r.headers.update(headers or {})
    return r


Override = Callable[[str, dict[str, str]], "requests.Response | BaseException | None"]


@dataclass
class FakeHttp:
    """A mocked ``requests.Session``: serves calendars, bars and corporate actions like
    the documented endpoints; the corporate-action filter uses the process date."""

    page_size: int = 10_000
    ca_events: list[dict[str, object]] = field(default_factory=lambda: [
        dividend("aaa-1", "AAA", date(2020, 12, 10), pay_lag=30),  # in both padded windows
        dividend("aaa-2", "AAA", date(2021, 2, 5))])  # fmt: skip
    override: Override | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    trust_env: bool = True
    proxies: dict[str, str] = field(default_factory=lambda: {"https": "http://proxy.invalid"})

    def get(self, url: str, **kw: Any) -> requests.Response:
        params = dict(kw.get("params") or {})
        self.calls.append({"url": url, **kw})
        if self.override is not None:
            got = self.override(url, params)
            if isinstance(got, BaseException):
                raise got
            if got is not None:
                return got
        full = f"{url}?{urlencode(params)}"
        return response(200, self.body(url, params), full)

    def body(self, url: str, p: dict[str, str]) -> str:
        if url.endswith("/v2/calendar"):
            lo, hi = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
            by_settle = p["date_type"] == "SETTLEMENT"
            rows = [{"date": str(s.session_date), "open": "09:30", "close": "16:00",
                     "session_open": "0400", "session_close": "2000",
                     "settlement_date": str(s.settlement_date)} for s in SESSIONS
                    if s.settlement_date is not None and lo <= (
                        s.settlement_date if by_settle else s.session_date) <= hi]  # fmt: skip
            return _json(rows)
        token = p.get("page_token")
        if url.endswith("/v2/stocks/bars"):
            sym = p["symbols"]
            lo = datetime.fromisoformat(p["start"]).date()
            hi = datetime.fromisoformat(p["end"]).date()
            days = [s.session_date for s in SESSIONS if lo <= s.session_date <= hi]
            raw = [{"t": b.as_of.isoformat().replace("+00:00", "Z"), "o": b.open, "h": b.high,
                    "l": b.low, "c": b.close, "v": b.volume, "n": 100, "vw": b.close}
                   for b in synthetic_bars(sym, dates=days)]  # fmt: skip
            chunk, nxt = self._page(raw, token)
            return _json({"bars": {sym: chunk}, "next_page_token": nxt})
        wanted = p["symbols"].split(",")
        items = [e for e in self.ca_events if p["start"] <= str(e["process_date"]) <= p["end"]
                 and e["symbol"] in wanted]  # fmt: skip
        chunk, nxt = self._page(items, token)
        return _json({"corporate_actions": {"cash_dividends": chunk}, "next_page_token": nxt})

    def _page(self, items: list[Any], token: str | None) -> tuple[list[Any], str | None]:
        start = int(token.split(":")[1]) if token else 0
        nxt = start + self.page_size
        return items[start:nxt], (f"tok:{nxt}" if nxt < len(items) else None)


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


def connect_with(http: FakeHttp, p: AcquisitionPlan,
                 guards: list[GuardedHttpSession] | None = None, *,
                 max_attempts: int | None = None, shift: timedelta = timedelta(0),
                 endpoints: Mapping[tuple[str, str], frozenset[str]] | None = None,
                 ) -> Connect:  # fmt: skip
    """The live wiring over ``http``. ``max_attempts`` / ``shift`` make the guard's own
    cap or deadline tighter than the staging budget (to exercise the guard backstop)."""

    def connect(remaining: Callable[[], timedelta]
                ) -> tuple[AcquisitionFetch, Callable[[], Mapping[str, object]]]:  # fmt: skip
        guarded = GuardedHttpSession(
            http, endpoints=plan_endpoints(p) if endpoints is None else endpoints,
            max_attempts=max_attempts or p.session_max_attempts,
            remaining=lambda: remaining() - shift, wall_clock=lambda: NOW)  # fmt: skip
        if guards is not None:
            guards.append(guarded)
        transport = AlpacaReadTransport(SETTINGS, session=guarded)  # type: ignore[arg-type]
        fetch = AcquisitionFetch(p, transport, guard=guarded)
        return fetch, lambda: transport_summary(fetch, guarded)

    return connect


def never_connect(remaining: Callable[[], timedelta]) -> Any:
    raise AssertionError("credentials must not be read / no request may be sent")


@pytest.fixture
def paths(tmp_path: Path) -> ResearchPaths:
    data = tmp_path / "data"
    data.mkdir()
    (data / "ledger.sqlite3").write_bytes(b"")  # a placeholder: names only, never opened
    return ResearchPaths(data / "research")


def session(paths: ResearchPaths, http: FakeHttp | None = None, *, resume: bool = False,
            label: str = "auth-1", max_sessions: int = 2, clock: Clock | None = None,
            p: AcquisitionPlan | None = None, probe: ProbeGates = PROBE,
            connect: Connect | None = None) -> dict[str, Any]:  # fmt: skip
    p = p or plan()
    c = clock or Clock()
    return dict(run_authorized_session(
        paths=paths, plan=p, authorization=Authorization(p.fingerprint, label, max_sessions),
        probe=probe, resume=resume,
        connect=connect or connect_with(http or FakeHttp(), p),
        monotonic=c.monotonic, sleep=c.sleep, now=lambda: NOW))  # fmt: skip


def do_publish(paths: ResearchPaths, dataset_id: str = "synthetic-ds",
               probe: ProbeGates = PROBE) -> dict[str, Any]:  # fmt: skip
    p = plan()
    return dict(publish(paths=paths, plan=p, fingerprint=p.fingerprint, universe=UNIVERSE,
                        probe=probe, dataset_id=dataset_id, now=lambda: NOW))  # fmt: skip


def slow_session(paths: ResearchPaths, **kw: Any) -> dict[str, Any]:
    """A provider that takes 70 s per response: after four batches fewer than 30 s of the
    300 s session are left, so the next batch is reserved for a later session."""
    clock = Clock(per_call=timedelta(0))

    def tick(_url: str, _p: dict[str, str]) -> None:
        clock.t += timedelta(seconds=70)

    return session(paths, FakeHttp(override=tick), clock=clock, **kw)


def eligible(p: AcquisitionPlan) -> list[str]:
    return [b.batch_id for b in p.batches if not b.requires_failed]


def staging_root(paths: ResearchPaths) -> Path:
    return paths.staging(plan())


# ============================================================ the approved plan
def test_the_runner_is_bound_to_the_approved_plan_and_probe() -> None:
    p = build_acquisition_plan()
    assert p.fingerprint == APPROVED_ACQUISITION_PLAN_FINGERPRINT
    assert build_capability_probe_plan().fingerprint == APPROVED_PROBE_PLAN_FINGERPRINT
    assert p.capability_probe_fingerprint == APPROVED_PROBE_PLAN_FINGERPRINT
    acct = request_accounting(p, GATES)
    assert (acct.batches, acct.expected_requests, acct.page_cap_requests,
            acct.worst_case_attempts) == (49, 49, 117, 215)  # fmt: skip
    assert (acct.sessions_expected, acct.sessions_worst_case) == (1, 2)
    assert (p.session_max_attempts, p.session_deadline_s) == (120, 300)
    assert set(plan_endpoints(p)) == ACQUISITION_ENDPOINTS


# ============================================================ guarded session
def guard(http: FakeHttp | None = None, *, remaining: int | timedelta = 300,
          max_attempts: int = 120) -> tuple[GuardedHttpSession, FakeHttp]:  # fmt: skip
    h = http or FakeHttp()
    left = remaining if isinstance(remaining, timedelta) else timedelta(seconds=remaining)
    g = GuardedHttpSession(h, endpoints=plan_endpoints(plan()), max_attempts=max_attempts,
                           remaining=lambda: left, wall_clock=lambda: NOW)  # fmt: skip
    return g, h


CAL_PARAMS = {"start": "2020-07-01", "end": "2021-06-30", "date_type": "TRADING"}


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (f"{TRADING}/v2/account", "ENDPOINT_NOT_ALLOWLISTED"),
        (f"{TRADING}/v2/orders", "ENDPOINT_NOT_ALLOWLISTED"),
        (f"{TRADING}/v2/positions", "ENDPOINT_NOT_ALLOWLISTED"),
        (f"{TRADING}/v2/account/activities", "ENDPOINT_NOT_ALLOWLISTED"),
        (f"{TRADING}/v2/account/configurations", "ENDPOINT_NOT_ALLOWLISTED"),
        (f"{TRADING}/v2/stocks/bars", "ENDPOINT_NOT_ALLOWLISTED"),
        (f"{DATA}/v2/calendar", "ENDPOINT_NOT_ALLOWLISTED"),
        (f"{DATA}/v1beta1/corporate-actions", "ENDPOINT_NOT_ALLOWLISTED"),
        ("https://api.alpaca.markets/v2/calendar", "ENDPOINT_NOT_ALLOWLISTED"),
        ("http://paper-api.alpaca.markets/v2/calendar", "ENDPOINT_NOT_ALLOWLISTED"),
        ("https://paper-api.alpaca.markets:8443/v2/calendar", "URL_REFUSED"),
        ("https://user:pw@paper-api.alpaca.markets/v2/calendar", "URL_REFUSED"),
        (f"{TRADING}/v2/calendar?start=2020-01-01", "URL_REFUSED"),
        (f"{TRADING}/v2/calendar#x", "URL_REFUSED"),
    ],
)
def test_the_guard_allows_only_the_exact_planned_endpoints(url: str, code: str) -> None:
    g, h = guard()
    with pytest.raises(TransportRefusedError) as err:
        g.get(url, params={}, headers={}, timeout=(5, 20), allow_redirects=False)
    assert err.value.code == code
    assert h.calls == [] and g.sent == 0 and g.refusals == [code]


def test_the_guard_refuses_unplanned_query_keys_options_and_redirect_following() -> None:
    g, h = guard()
    url = f"{TRADING}/v2/calendar"
    for kwargs, code in (
        ({"params": {**CAL_PARAMS, "status": "all"}, "allow_redirects": False}, "QUERY_REFUSED"),
        ({"params": CAL_PARAMS}, "REDIRECTS_NOT_DISABLED"),
        ({"params": CAL_PARAMS, "allow_redirects": False, "data": b"x"},
         "REQUEST_OPTION_REFUSED"),
        ({"params": CAL_PARAMS, "allow_redirects": False, "proxies": {}},
         "REQUEST_OPTION_REFUSED"),
    ):  # fmt: skip
        with pytest.raises(TransportRefusedError) as err:
            g.get(url, **kwargs)
        assert err.value.code == code
    assert h.calls == []


def test_the_guard_has_no_write_path_and_disables_environment_settings() -> None:
    g, h = guard()
    assert h.trust_env is False and h.proxies == {}
    for name in ("post", "put", "patch", "delete", "head", "options", "request", "send",
                 "mount", "auth", "cookies"):  # fmt: skip
        with pytest.raises(TransportRefusedError):
            getattr(g, name)
    with pytest.raises(TransportRefusedError):
        g.trust_env = True  # type: ignore[misc]
    with pytest.raises(TransportRefusedError):
        g.max_redirects = 5
    g.trust_env = False  # type: ignore[misc]  # what AlpacaReadTransport does: accepted
    assert g.trust_env is False and h.calls == []


def test_the_guard_refuses_redirects_and_foreign_response_urls() -> None:
    url = f"{TRADING}/v2/calendar"
    for resp, code in (
        (response(302, "", url), "REDIRECT_REFUSED"),
        (response(200, "[]", "https://evil.invalid/v2/calendar"), "UNEXPECTED_RESPONSE_URL"),
        (response(200, "[]", f"{TRADING}/v2/account"), "UNEXPECTED_RESPONSE_URL"),
    ):
        g, h = guard(FakeHttp(override=lambda _u, _p, r=resp: r))  # type: ignore[misc]
        with pytest.raises(TransportRefusedError) as err:
            g.get(url, params=CAL_PARAMS, headers={}, timeout=(5, 20), allow_redirects=False)
        assert err.value.code == code and len(h.calls) == 1


def test_the_guard_cuts_timeouts_to_the_deadline_and_caps_attempts() -> None:
    url = f"{TRADING}/v2/calendar"
    g, h = guard(remaining=3)
    g.get(url, params=CAL_PARAMS, headers={}, timeout=(5, 20), allow_redirects=False)
    assert h.calls[0]["timeout"] == (3, 3) and h.calls[0]["allow_redirects"] is False
    g, h = guard(remaining=0)
    with pytest.raises(TransportRefusedError, match="SESSION_DEADLINE"):
        g.get(url, params=CAL_PARAMS, allow_redirects=False)
    assert h.calls == []
    g, h = guard(max_attempts=2)
    for _ in range(2):
        g.get(url, params=CAL_PARAMS, allow_redirects=False)
    with pytest.raises(TransportRefusedError, match="SESSION_ATTEMPT_CAP"):
        g.get(url, params=CAL_PARAMS, allow_redirects=False)
    assert len(h.calls) == 2 and g.sent == 2


def test_the_guard_records_no_query_values_or_headers() -> None:
    g, _ = guard(FakeHttp(override=lambda u, _p: response(200, "{}", u)))
    params = {"symbols": "AAA", "page_token": "secret-looking-token"}
    with pytest.raises(TransportRefusedError):  # not every key is planned for bars
        g.get(f"{DATA}/v2/stocks/bars", params={**params, "x": "1"}, allow_redirects=False)
    g.get(f"{DATA}/v2/stocks/bars", params=params, headers={"APCA-API-KEY-ID": "k"},
          allow_redirects=False)  # fmt: skip
    text = json.dumps(g.summary())
    assert "secret-looking-token" not in text and "APCA" not in text and "AAA" not in text


# ============================================================ canonical bytes
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(10**20), 10**20) | st.text(max_size=8)
    | st.decimals(allow_nan=False, allow_infinity=False, places=4).filter(
        lambda d: "." in str(d) or "E" in str(d)),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=5), inner,
                                                                 max_size=4),
    max_leaves=20,
)  # fmt: skip


@given(_json_values)
def test_canonical_bodies_round_trip_exactly(doc: object) -> None:
    body = canonical_body(doc)
    again = decode(body)
    assert again == doc
    assert canonical_body(again) == body  # idempotent: one canonical form

    def types(v: object) -> object:
        if isinstance(v, dict):
            return {k: types(x) for k, x in v.items()}
        if isinstance(v, list):
            return [types(x) for x in v]
        return (type(v).__name__, str(v))

    assert types(again) == types(doc)  # Decimal stays Decimal with the same exponent


@pytest.mark.parametrize(
    ("doc", "code"),
    [({"p": 1.5}, "FLOAT_IN_RESPONSE"), ({"p": Decimal("NaN")}, "NON_FINITE_NUMBER"),
     ({"p": Decimal("Infinity")}, "NON_FINITE_NUMBER"),
     ({"p": Decimal(5)}, "NON_CANONICAL_DECIMAL"),
     ({1: "x"}, "NON_STRING_KEY"), ({"p": {1, 2}}, "UNSUPPORTED_JSON_VALUE")],
)  # fmt: skip
def test_canonical_bodies_refuse_inexact_values(doc: object, code: str) -> None:
    with pytest.raises(TransportRefusedError) as err:
        canonical_body(doc)
    assert err.value.code == code


def test_a_transport_nan_constant_never_reaches_staging(paths: ResearchPaths) -> None:
    """``decode_json`` maps a JSON ``NaN`` to a float: the adapter refuses it."""

    def nan(url: str, p: dict[str, str]) -> requests.Response | None:
        if url.endswith("/v2/calendar") and p["date_type"] == "TRADING":
            return response(200, '[{"date": NaN}]', f"{url}?x")
        return None

    out = session(paths, FakeHttp(override=nan))
    assert out["status"] == "STOPPED" and out["stop_code"] == "INVALID_RESPONSE"
    assert out["committed"] == []
    assert "FLOAT_IN_RESPONSE" in out["transport"]["refusals"]


def test_staged_prices_keep_their_exact_decimal_text(paths: ResearchPaths) -> None:
    def exact(url: str, p: dict[str, str]) -> requests.Response | None:
        if url.endswith("/v2/stocks/bars") and p["symbols"] == "AAA" and p["adjustment"] == "raw":
            body = json.loads(FakeHttp().body(url, p).replace('"o": ', '"o": "').replace(
                ', "h"', '", "h"'))  # fmt: skip
            rows = body["bars"]["AAA"]
            for r in rows:
                r["o"] = r["h"] = r["l"] = r["c"] = r["vw"] = "@@150.25010@@"
            return response(200, _json(body).replace('"@@', "").replace('@@"', ""), f"{url}?x")
        return None

    out = session(paths, FakeHttp(override=exact))
    assert out["status"] == "COMPLETE"
    blobs = [b.read_bytes() for b in (staging_root(paths) / "blobs").iterdir()]
    assert any(b'"c":150.25010' in b for b in blobs)
    assert not any(b"150.2501," in b for b in blobs)


# ============================================================ fetch adapter
def test_the_adapter_sends_only_requests_identical_to_the_plan() -> None:
    p = plan()
    fake = FakeHttp()
    fetch = AcquisitionFetch(p, AlpacaReadTransport(SETTINGS, session=fake))  # type: ignore[arg-type]
    batch = p.batches[0]
    req = next(r for r in p.requests if r.request_id == batch.request_id)
    for b, r in (
        (batch, replace(req, params=(*req.params[:-1], ("date_type", "SETTLEMENT")))),
        (replace(batch, max_attempts=99), req),
        (batch, replace(req, path="/v2/account")),
        (p.batches[1], req),
    ):
        with pytest.raises(TransportRefusedError, match="REQUEST_NOT_IN_PLAN"):
            fetch(b, r, None)
    with pytest.raises(TransportRefusedError, match="CALENDAR_IS_NOT_PAGINATED"):
        fetch(batch, req, "tok:1")
    other = build_acquisition_plan(UNIVERSE, warmup_start=WARMUP, research_start=START,
                                   research_end=date(2021, 5, 31))  # fmt: skip
    with pytest.raises(TransportRefusedError, match="REQUEST_NOT_IN_PLAN"):
        fetch(other.batches[2], other.requests[2], None)


BAD_SHAPES: list[tuple[str, Callable[[str, dict[str, str]], bool], str, str]] = [
    ("calendar object", lambda u, p: u.endswith("/v2/calendar"), "{}", "CALENDAR_NOT_A_LIST"),
    ("calendar extra key", lambda u, p: u.endswith("/v2/calendar"),
     '[{"date":"2020-07-01","open":"09:30","close":"16:00","session_open":"0400",'
     '"session_close":"2000","settlement_date":"2020-07-03","x":"y"}]', "CALENDAR_ROW_SHAPE"),
    ("bars extra key", lambda u, p: u.endswith("/bars"),
     '{"bars":{},"next_page_token":null,"currency":"USD"}', "BARS_PAGE_SHAPE"),
    ("bars no token", lambda u, p: u.endswith("/bars"), '{"bars":{}}', "BARS_PAGE_SHAPE"),
    ("bars other symbol", lambda u, p: u.endswith("/bars"),
     '{"bars":{"ZZZ":[]},"next_page_token":null}', "BARS_UNEXPECTED_SYMBOL"),
    ("bar missing close", lambda u, p: u.endswith("/bars"),
     '{"bars":{"AAA":[{"t":"2020-07-01T04:00:00Z","o":1.0,"h":1.0,"l":1.0,"v":1}]},'
     '"next_page_token":null}', "BAR_ROW_SHAPE"),
    ("bar high below low", lambda u, p: u.endswith("/bars"),
     '{"bars":{"AAA":[{"t":"2020-07-01T04:00:00Z","o":1.0,"h":1.0,"l":2.0,"c":1.5,"v":1}]},'
     '"next_page_token":null}', "BAR_ROW_INVALID"),
    ("empty page token", lambda u, p: u.endswith("/bars"),
     '{"bars":{},"next_page_token":""}', "INVALID_PAGE_TOKEN"),
    ("unknown action type", lambda u, p: u.endswith("corporate-actions"),
     '{"corporate_actions":{"mystery_events":[]},"next_page_token":null}',
     "CORPORATE_ACTIONS_UNKNOWN_ACTION_TYPE"),
    ("dividend without dates", lambda u, p: u.endswith("corporate-actions"),
     '{"corporate_actions":{"cash_dividends":[{"id":"x","symbol":"AAA","rate":0.1}]},'
     '"next_page_token":null}', "CORPORATE_ACTIONS_UNRECOGNIZED_EVENT_SHAPE"),
    ("ca extra key", lambda u, p: u.endswith("corporate-actions"),
     '{"corporate_actions":{},"next_page_token":null,"x":1}',
     "CORPORATE_ACTIONS_UNEXPECTED_SHAPE"),
]  # fmt: skip


@pytest.mark.parametrize(("name", "match", "body", "code"), BAD_SHAPES,
                         ids=[b[0] for b in BAD_SHAPES])  # fmt: skip
def test_unexpected_response_shapes_fail_closed(
    paths: ResearchPaths,
    name: str,
    match: Callable[[str, dict[str, str]], bool],
    body: str,
    code: str,
) -> None:
    del name

    def bad(url: str, p: dict[str, str]) -> requests.Response | None:
        return response(200, body, f"{url}?x") if match(url, p) else None

    out = session(paths, FakeHttp(override=bad))
    assert out["status"] == "STOPPED" and out["stop_code"] == "INVALID_RESPONSE"
    assert code in out["transport"]["refusals"]
    failed = str(out["failed_batch"])
    assert not (staging_root(paths) / "batches" / f"{failed}.json").exists()
    assert out["attempts_used"] == len(out["committed"]) + 1


# ============================================================ sessions end to end
def test_a_session_stages_every_eligible_batch_then_publication_is_explicit(
    paths: ResearchPaths,
) -> None:
    http = FakeHttp()
    out = session(paths, http)
    p = plan()
    assert out["status"] == "COMPLETE" and out["stop_code"] is None
    assert sorted(out["committed"]) == sorted(eligible(p))
    assert out["attempts_used"] == len(http.calls) == len(eligible(p)) and out["published"] is False
    assert all(c["allow_redirects"] is False for c in http.calls)
    assert {c["url"] for c in http.calls} == {f"{TRADING}/v2/calendar", f"{DATA}/v2/stocks/bars",
                                              f"{DATA}/v1/corporate-actions"}  # fmt: skip
    root = staging_root(paths)
    assert root.parent.parent == paths.root and root.name == p.fingerprint[:16]
    journal = sorted(x.name for x in (root / "sessions").iterdir())
    assert journal == ["01.report.json", "01.start.json"]
    for f in (root / "sessions").iterdir():
        assert f.stat().st_mode & 0o777 == 0o444
    start = json.loads((root / "sessions" / "01.start.json").read_bytes())
    assert start["plan_fingerprint"] == p.fingerprint and start["gates"] == dict(
        sorted(GATES.items())
    )
    assert not (paths.root / "synthetic-ds").exists()  # a session never publishes
    pub = do_publish(paths)
    assert pub["state"] == DatasetState.ACQUISITION_COMPLETE.value
    assert pub["research_ready"] is False
    assert pub["attestations_created"] is False and pub["holdout_opened"] is False
    assert pub["backtest_run"] is False
    ds = load_dataset(paths.root / "synthetic-ds")
    report = ds.manifest.raw["acquisition_report"]
    assert report["plan_fingerprint"] == p.fingerprint
    assert sorted(report["required_batches"]) == sorted(eligible(p))
    assert report["ca_date_semantics"]["status"] == "CONSISTENT_WITH_OBSERVATIONS"
    assert report["ca_date_semantics"]["verified_for"] == []
    # no provider-based completeness claim while the date-filter gate is unverified
    issues = {f["issue"] for f in report["findings"]}
    assert "DATE_SEMANTICS_UNVERIFIED" in issues
    for claims in report["corporate_action_claims"].values():
        assert not any(claims.values()), claims  # no list carries a claim
    # the event seen by both padded windows is stored once, by ex-date
    aaa = json.loads((paths.root / "synthetic-ds" / "corporate_actions" / "AAA.json").read_bytes())
    assert [(d["ex_session"], d["pay_date"]) for d in aaa["dividends"]] == [
        ("2020-12-10", "2021-01-09"), ("2021-02-05", "2021-02-11")]  # fmt: skip
    assert aaa["dividend_coverage"] is None  # provider lists alone establish nothing
    with pytest.raises(RunnerRefusedError, match="DATASET_EXISTS"):
        do_publish(paths)


def test_nothing_left_to_fetch_is_refused_before_credentials(paths: ResearchPaths) -> None:
    session(paths)
    with pytest.raises(RunnerRefusedError, match="NOTHING_TO_FETCH"):
        session(paths, resume=True, label="auth-2", connect=never_connect)


@pytest.mark.parametrize(
    ("status_code", "body", "url_end", "code"),
    [
        (401, "{}", "/v2/calendar", "AUTH_OR_ENTITLEMENT_REFUSED"),
        (403, "{}", "/v2/calendar", "AUTH_OR_ENTITLEMENT_REFUSED"),
        (403, '{"message":"subscription does not permit querying recent SIP data"}',
         "/v2/stocks/bars", "AUTH_OR_ENTITLEMENT_REFUSED"),
        (403, '{"message":"forbidden"}', "/v2/stocks/bars", "AUTH_OR_ENTITLEMENT_REFUSED"),
        (404, "{}", "/v1/corporate-actions", "HTTP_404"),
        (400, "{}", "/v2/stocks/bars", "HTTP_400"),
        (500, "{}", "/v2/stocks/bars", "HTTP_500"),
    ],
)  # fmt: skip
def test_refusals_stop_at_once_without_retry_or_fallback(
    paths: ResearchPaths, status_code: int, body: str, url_end: str, code: str
) -> None:
    def fail(url: str, p: dict[str, str]) -> requests.Response | None:
        return response(status_code, body, f"{url}?x") if url.endswith(url_end) else None

    http = FakeHttp(override=fail)
    out = session(paths, http)
    assert out["status"] == "STOPPED" and out["stop_code"] == code
    hits = [c for c in http.calls if c["url"].endswith(url_end)]
    assert len(hits) == 1  # never retried, no fallback endpoint
    assert out["attempts_used"] == len(http.calls)
    assert len(out["committed"]) == len(http.calls) - 1


def test_retries_only_after_429_503_or_timeout_and_are_counted(paths: ResearchPaths) -> None:
    queue: list[requests.Response | BaseException] = [
        response(429, "{}", f"{TRADING}/v2/calendar"),
        requests.Timeout("slow"),
    ]

    def flaky(url: str, p: dict[str, str]) -> requests.Response | BaseException | None:
        if url.endswith("/v2/calendar") and p["date_type"] == "TRADING" and queue:
            return queue.pop(0)
        return None

    http, clock = FakeHttp(override=flaky), Clock()
    out = session(paths, http, clock=clock)
    assert out["status"] == "COMPLETE"
    assert out["attempts_used"] == len(http.calls) == len(eligible(plan())) + 2
    assert clock.slept == [timedelta(milliseconds=500), timedelta(seconds=1)]


def test_retry_exhaustion_stops_the_session(paths: ResearchPaths) -> None:
    def down(url: str, p: dict[str, str]) -> requests.Response | None:
        return response(503, "{}", f"{url}?x") if url.endswith("/v2/calendar") else None

    http = FakeHttp(override=down)
    out = session(paths, http)
    assert out["status"] == "STOPPED" and out["stop_code"] == "RETRIES_EXHAUSTED"
    assert len(http.calls) == 3 and out["committed"] == []


def test_pagination_follows_tokens_to_null_within_the_page_cap(paths: ResearchPaths) -> None:
    http = FakeHttp(page_size=200)  # ~250 bars per symbol: 2 pages, cap 2
    out = session(paths, http)
    assert out["status"] == "COMPLETE"
    bar_calls = [c for c in http.calls if c["url"].endswith("/bars")]
    assert len(bar_calls) == 8
    assert [("page_token" in c["params"]) for c in bar_calls[:2]] == [False, True]
    assert out["attempts_used"] == len(http.calls)


def test_pagination_beyond_the_cap_or_with_a_repeated_token_stops(tmp_path: Path) -> None:
    for i, (http, code) in enumerate((
        (FakeHttp(page_size=100), "PAGE_CAP_EXCEEDED"),
        (FakeHttp(override=lambda u, p: response(
            200, '{"bars":{},"next_page_token":"same"}', f"{u}?x")
            if u.endswith("/bars") else None), "REPEATED_PAGE_TOKEN"),
    )):  # fmt: skip
        data = tmp_path / f"d{i}" / "data"
        data.mkdir(parents=True)
        out = session(ResearchPaths(data / "research"), http)
        assert out["status"] == "STOPPED" and out["stop_code"] == code
        assert str(out["failed_batch"]).startswith("C-BARS-AAA-RAW")


def test_a_batch_starts_only_if_its_budget_and_30s_fit_then_resumes_later(
    paths: ResearchPaths,
) -> None:
    """A slow provider: the session reserves the rest once fewer than 30 s are left,
    leaving nothing half-staged. A second authorized session resumes, re-verifies staged
    batches and never refetches them."""
    first = slow_session(paths)
    assert first["status"] == "INCOMPLETE" and first["stop_code"] == "SESSION_BUDGET_RESERVED"
    done = set(first["committed"])
    assert len(done) == 4 and first["pending"] and first["failed_batch"] is None
    assert first["attempts_used"] == 4 and first["stop_detail"] == "BATCH_TIME_RESERVE"
    assert first["stop_reason"] == "SESSION_TIME_RESERVE_INSUFFICIENT" and first["resumable"]
    assert first["remaining_after"] == len(first["pending"])
    with pytest.raises(RunnerRefusedError, match="STORE_EXISTS"):
        session(paths, connect=never_connect)
    http2 = FakeHttp()
    second = session(paths, http2, resume=True, label="auth-2")
    assert second["status"] == "COMPLETE"
    assert set(second["already_committed"]) == done
    assert done.isdisjoint(second["committed"])
    assert len(http2.calls) == len(second["committed"])
    with pytest.raises(RunnerRefusedError, match="SESSION_LIMIT_REACHED"):
        session(paths, resume=True, label="auth-3", connect=never_connect)


def test_the_guard_cuts_later_attempts_to_the_session_time_left(paths: ResearchPaths) -> None:
    """The first attempt takes 285 s and returns 429; after the 0.5 s backoff only 14.5 s
    of the 300 s session are left, so the retry's read timeout is cut to 14 whole seconds. Then no
    further batch fits (30 s reserve): the session ends INCOMPLETE, nothing half-staged."""
    clock = Clock(per_call=timedelta(0))
    first = [True]

    def slow_429(url: str, _p: dict[str, str]) -> requests.Response | None:
        if first[0]:
            first[0] = False
            clock.t += timedelta(seconds=285)
            return response(429, "{}", f"{url}?x")
        return None

    http = FakeHttp(override=slow_429)
    out = session(paths, http, clock=clock)
    assert [c["timeout"] for c in http.calls] == [(5, 20), (5, 14)]
    assert out["status"] == "INCOMPLETE" and out["stop_code"] == "SESSION_BUDGET_RESERVED"
    assert out["stop_reason"] == "SESSION_TIME_RESERVE_INSUFFICIENT"
    assert out["attempts_used"] == 2 and len(out["committed"]) == 1


def test_session_authorization_rules(paths: ResearchPaths) -> None:
    p = plan()
    with pytest.raises(RunnerRefusedError, match="STORE_MISSING"):
        session(paths, resume=True, connect=never_connect)
    with pytest.raises(RunnerRefusedError, match="PLAN_FINGERPRINT_MISMATCH"):
        run_authorized_session(paths=paths, plan=p, authorization=Authorization(
            APPROVED_ACQUISITION_PLAN_FINGERPRINT, "a", 1), probe=PROBE, resume=False,
            connect=never_connect, monotonic=Clock().monotonic, sleep=Clock().sleep,
            now=lambda: NOW)  # fmt: skip
    for bad in ("", "../x", ".hidden", "a b"):
        with pytest.raises(RunnerRefusedError, match="INVALID_AUTHORIZATION_LABEL"):
            Authorization(p.fingerprint, bad, 1)
    for n in (0, 10):
        with pytest.raises(RunnerRefusedError, match="INVALID_MAX_SESSIONS"):
            Authorization(p.fingerprint, "a", n)
    session(paths, http=FakeHttp(page_size=100), max_sessions=3)  # stops on the page cap
    with pytest.raises(RunnerRefusedError, match="AUTHORIZATION_LABEL_REUSED"):
        session(paths, resume=True, label="auth-1", max_sessions=3, connect=never_connect)
    assert not (paths.root / "synthetic-ds").exists()


@pytest.mark.parametrize(
    ("gates", "code"),
    [
        ({**GATES, "SIP-DEPTH": False}, "CORE_GATE_NOT_PASSED"),
        ({k: v for k, v in GATES.items() if k != "PAGINATION"}, "CORE_GATE_NOT_PASSED"),
        ({k: v for k, v in GATES.items() if k != "TICKER-IDENTITY"},
         "CONDITIONAL_GATE_UNDETERMINED"),
        ({k: v for k, v in GATES.items() if k != "CA-SHAPE"}, "CONDITIONAL_GATE_UNDETERMINED"),
    ],
)  # fmt: skip
def test_sessions_need_passed_core_gates(paths: ResearchPaths, gates: dict[str, bool],
                                         code: str) -> None:  # fmt: skip
    with pytest.raises(RunnerRefusedError, match=code):
        session(paths, probe=ProbeGates(PROBE.evidence, PROBE.summary_sha256, gates),
                connect=never_connect)  # fmt: skip
    assert not paths.root.exists()


def test_gates_cannot_change_between_sessions(paths: ResearchPaths) -> None:
    slow_session(paths)
    other = ProbeGates(PROBE.evidence, "b" * 64, GATES)
    with pytest.raises(RunnerRefusedError, match="GATES_CHANGED_BETWEEN_SESSIONS"):
        session(paths, resume=True, label="auth-2", probe=other, connect=never_connect)
    with pytest.raises(RunnerRefusedError, match="GATES_CHANGED_BETWEEN_SESSIONS"):
        do_publish(paths, probe=other)


def test_ca_shape_failure_skips_corporate_actions_and_publication_stays_complete(
    paths: ResearchPaths,
) -> None:
    probe = ProbeGates(PROBE.evidence, PROBE.summary_sha256, {**GATES, "CA-SHAPE": False})
    http = FakeHttp()
    out = session(paths, http, probe=probe)
    assert out["status"] == "COMPLETE"
    assert not any(c["url"].endswith("corporate-actions") for c in http.calls)
    pub = do_publish(paths, probe=probe)
    assert pub["state"] == "ACQUISITION_COMPLETE" and pub["research_ready"] is False
    assert set(pub["rejected"]) == {"AAA", "META"}


def test_ticker_identity_failure_adds_the_fb_batches(paths: ResearchPaths) -> None:
    probe = ProbeGates(PROBE.evidence, PROBE.summary_sha256,
                       {**GATES, "TICKER-IDENTITY": False})  # fmt: skip
    http = FakeHttp()
    out = session(paths, http, probe=probe)
    assert out["status"] == "COMPLETE"
    assert {c["params"]["symbols"] for c in http.calls if c["url"].endswith("/bars")} == {
        "AAA", "META", "FB"}  # fmt: skip


# ============================================================ corporate actions
def test_conflicting_duplicate_corporate_actions_fail_closed_for_their_symbol(
    paths: ResearchPaths,
) -> None:
    """The same provider id with different content in two overlapping windows."""
    seen: list[str] = []

    def conflict(url: str, p: dict[str, str]) -> requests.Response | None:
        if not url.endswith("corporate-actions"):
            return None
        seen.append(p["start"])
        rate = "0.25" if len(seen) == 1 else "0.30"
        doc = {"corporate_actions": {"cash_dividends": [
            dividend("aaa-1", "AAA", date(2020, 12, 10), pay_lag=30, rate=rate)]},
            "next_page_token": None}  # fmt: skip
        return response(200, _json(doc), f"{url}?x")

    assert session(paths, FakeHttp(override=conflict))["status"] == "COMPLETE"
    do_publish(paths)
    report = load_dataset(paths.root / "synthetic-ds").manifest.raw["acquisition_report"]
    assert any(f["issue"] == "DUPLICATE_EVENT_CONFLICT" and f["symbol"] == "AAA"
               for f in report["findings"])  # fmt: skip


def test_payable_and_process_dates_are_kept_apart_and_requests_are_padded(
    paths: ResearchPaths,
) -> None:
    http = FakeHttp()
    session(paths, http)
    ca = [c["params"] for c in http.calls if c["url"].endswith("corporate-actions")]
    assert [(x["start"], x["end"]) for x in ca] == [("2020-05-31", "2021-04-02"),
                                                    ("2020-12-01", "2021-09-30")]  # fmt: skip
    blobs = [decode(b.read_bytes()) for b in (staging_root(paths) / "blobs").iterdir()]
    events = [e for b in blobs if isinstance(b, dict) and "corporate_actions" in b
              for e in b["corporate_actions"].get("cash_dividends", [])]  # fmt: skip
    assert events and all({"payable_date", "process_date", "ex_date"} <= set(e) for e in events)


# ============================================================ crash recovery
class Crash(BaseException):
    """Simulates a process kill mid-session."""


def test_a_crash_blocks_everything_until_explicit_recovery(paths: ResearchPaths) -> None:
    calls = {"n": 0}

    def crash(url: str, p: dict[str, str]) -> BaseException | None:
        calls["n"] += 1
        return Crash() if calls["n"] == 4 else None

    with pytest.raises(Crash):
        session(paths, FakeHttp(override=crash), max_sessions=3)
    root = staging_root(paths)
    assert (root / "session.lock").exists()
    assert sorted(x.name for x in (root / "sessions").iterdir()) == ["01.start.json"]
    with pytest.raises(RunnerRefusedError, match="PREVIOUS_SESSION_INTERRUPTED"):
        session(paths, resume=True, label="auth-2", max_sessions=3, connect=never_connect)
    with pytest.raises(RunnerRefusedError, match="PREVIOUS_SESSION_INTERRUPTED"):
        do_publish(paths)
    p = plan()
    st_ = status(paths=paths, plan=p, fingerprint=p.fingerprint, probe=PROBE)
    assert st_["interruption"] == ["session.lock"]
    out = recover(paths=paths, plan=p, fingerprint=p.fingerprint, label="2026-11-16-crash",
                  now=lambda: NOW)  # fmt: skip
    assert out["quarantined"] == ["session.lock"] and out["sessions_closed"] == [1]
    assert (root / "quarantine" / "2026-11-16-crash" / "session.lock").exists()
    closed = json.loads((root / "sessions" / "01.report.json").read_bytes())
    assert closed["status"] == "INTERRUPTED_RECOVERED"
    with pytest.raises(RunnerRefusedError, match="NOTHING_TO_RECOVER"):
        recover(paths=paths, plan=p, fingerprint=p.fingerprint, label="again", now=lambda: NOW)
    resumed = session(paths, resume=True, label="auth-2", max_sessions=3)
    assert resumed["status"] == "COMPLETE"
    assert len(resumed["already_committed"]) == 3
    assert do_publish(paths)["state"] == "ACQUISITION_COMPLETE"


def test_recovery_labels_are_validated_and_used_once(paths: ResearchPaths) -> None:
    p = plan()
    with pytest.raises(RunnerRefusedError, match="STORE_MISSING"):
        recover(paths=paths, plan=p, fingerprint=p.fingerprint, label="x", now=lambda: NOW)
    session(paths)
    (staging_root(paths) / "session.lock").write_bytes(b"")
    for bad in ("", "../q", ".q"):
        with pytest.raises(RunnerRefusedError, match="INVALID_RECOVERY_LABEL"):
            recover(paths=paths, plan=p, fingerprint=p.fingerprint, label=bad, now=lambda: NOW)
    (staging_root(paths) / "quarantine" / "used").mkdir()
    with pytest.raises(RunnerRefusedError, match="RECOVERY_LABEL_USED"):
        recover(paths=paths, plan=p, fingerprint=p.fingerprint, label="used", now=lambda: NOW)


# ============================================================ tampering and plan binding
def _make_writable_and_edit(path: Path, edit: Callable[[bytes], bytes]) -> None:
    path.chmod(0o644)
    path.write_bytes(edit(path.read_bytes()))


def test_tampered_staging_is_refused_by_resume_and_publication(paths: ResearchPaths) -> None:
    slow_session(paths)
    root = staging_root(paths)
    blob = sorted((root / "blobs").iterdir())[0]
    _make_writable_and_edit(blob, lambda b: b.replace(b"0", b"1", 1))
    with pytest.raises(StagingIntegrityError, match="TAMPERED_BATCH"):
        session(paths, resume=True, label="auth-2", connect=never_connect)
    with pytest.raises(StagingIntegrityError, match="TAMPERED_BATCH"):
        do_publish(paths)
    assert not (paths.root / "synthetic-ds").exists()


def test_tampered_record_and_unknown_record_are_refused(tmp_path: Path) -> None:
    for i, mutate in enumerate(("record", "unknown")):
        data = tmp_path / f"d{i}" / "data"
        data.mkdir(parents=True)
        paths = ResearchPaths(data / "research")
        session(paths)
        batches = staging_root(paths) / "batches"
        if mutate == "record":
            rec = sorted(batches.iterdir())[0]
            _make_writable_and_edit(rec, lambda b: b.replace(b'"attempts":1', b'"attempts":2'))
            expected: type[Exception] = StagingIntegrityError
        else:
            (batches / "C-BARS-ZZZ-RAW.000000000000.json").write_bytes(b"{}")
            expected = IncompatiblePlanError
        with pytest.raises(expected):
            do_publish(paths)


def test_a_store_of_another_plan_is_never_used(paths: ResearchPaths) -> None:
    session(paths)
    other = build_acquisition_plan(UNIVERSE, warmup_start=WARMUP, research_start=START,
                                   research_end=date(2021, 5, 31))  # fmt: skip
    assert other.fingerprint != plan().fingerprint
    shutil.copytree(staging_root(paths), paths.staging(other))
    with pytest.raises(IncompatiblePlanError, match="INCOMPATIBLE_PLAN"):
        session(paths, resume=True, label="auth-2", p=other, connect=never_connect)


# ============================================================ publication gates
def test_publication_requires_every_eligible_batch(paths: ResearchPaths) -> None:
    assert slow_session(paths)["status"] == "INCOMPLETE"
    with pytest.raises(IncompleteAcquisitionError, match="INCOMPLETE_ACQUISITION"):
        do_publish(paths)
    assert not (paths.root / "synthetic-ds").exists()


def test_publication_refusals(paths: ResearchPaths) -> None:
    with pytest.raises(RunnerRefusedError, match="STORE_MISSING"):
        do_publish(paths)
    for bad in ("staging", "UPPER", "../x", "a", ".hidden"):
        with pytest.raises(RunnerRefusedError, match="INVALID_DATASET_ID"):
            do_publish(paths, dataset_id=bad)
    p = plan()
    with pytest.raises(RunnerRefusedError, match="PLAN_FINGERPRINT_MISMATCH"):
        publish(paths=paths, plan=p, fingerprint="0" * 64, universe=UNIVERSE, probe=PROBE,
                dataset_id="synthetic-ds", now=lambda: NOW)  # fmt: skip
    with pytest.raises(RunnerRefusedError, match="CORE_GATE_NOT_PASSED"):
        do_publish(paths, probe=ProbeGates(PROBE.evidence, PROBE.summary_sha256, {}))


def test_publication_never_opens_the_holdout_or_runs_evaluation(
    paths: ResearchPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swing_trading_bot.research import dataset as dataset_mod  # noqa: PLC0415
    from swing_trading_bot.strategy import backtest, walk_forward  # noqa: PLC0415

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("evaluation or holdout access during acquisition")

    for owner, name in ((walk_forward, "run_walk_forward"), (walk_forward, "run_holdout"),
                        (walk_forward, "run_continuous"), (walk_forward, "freeze_protocol"),
                        (walk_forward.SealedResearchData, "view"),
                        (walk_forward.SealedResearchData, "__init__"),
                        (dataset_mod.ResearchDataset, "research_inputs"),
                        (backtest, "run_backtest")):  # fmt: skip
        if hasattr(owner, name):
            monkeypatch.setattr(owner, name, forbidden)
    assert session(paths)["status"] == "COMPLETE"
    pub = do_publish(paths)
    assert pub["holdout_opened"] is False and pub["research_ready"] is False


# ============================================================ probe gates
def summary(**over: Any) -> dict[str, Any]:
    checks = {c: {"outcome": "VERIFIED", "codes": [], "observations": {}}
              for c in ("CAL-DEPTH", "SETTLE-CYCLE", "SIP-DEPTH", "PAGINATION", "SPLIT-ADJ",
                        "TICKER-IDENTITY", "CA-SHAPE")}  # fmt: skip
    doc: dict[str, Any] = {
        "plan": {"id": "history-capability-probe-v1",
                 "fingerprint": APPROVED_PROBE_PLAN_FINGERPRINT},
        "execution": {"status": "COMPLETED", "stop_code": None},
        "acquisition": {"started": False, "dataset_written": False}, "checks": checks}  # fmt: skip
    doc.update(over)
    return doc


def test_gates_come_from_the_reviewed_probe_summary() -> None:
    fp = APPROVED_PROBE_PLAN_FINGERPRINT
    assert gates_from_probe_summary(summary(), probe_fingerprint=fp) == GATES
    s = summary()
    s["checks"]["TICKER-IDENTITY"]["codes"] = ["META_PRE_CHANGE_BARS_MISSING"]
    s["checks"]["CA-SHAPE"] = {"outcome": "UNAVAILABLE", "codes": ["ENDPOINT_NOT_FOUND"]}
    s["checks"]["SIP-DEPTH"] = {"outcome": "FAILED", "codes": ["SESSIONS_MISSING"]}
    s["checks"]["PAGINATION"] = {"outcome": "INCONCLUSIVE", "codes": ["X"]}
    gates = gates_from_probe_summary(s, probe_fingerprint=fp)
    assert gates["TICKER-IDENTITY"] is False and gates["SIP-DEPTH"] is False
    assert "CA-SHAPE" not in gates and "PAGINATION" not in gates
    with pytest.raises(RunnerRefusedError, match="CORE_GATE_NOT_PASSED"):
        check_gates(gates)
    for bad, code in ((summary(plan={"id": "x", "fingerprint": fp}), "PROBE_PLAN_MISMATCH"),
                      (summary(execution={"status": "STOPPED"}), "PROBE_NOT_COMPLETED"),
                      (summary(acquisition={"started": True}), "PROBE_SUMMARY_INVALID"),
                      (summary(checks=[]), "PROBE_SUMMARY_INVALID")):  # fmt: skip
        with pytest.raises(RunnerRefusedError, match=code):
            gates_from_probe_summary(bad, probe_fingerprint=fp)


def test_probe_evidence_is_read_by_name_without_symlinks(tmp_path: Path) -> None:
    p = build_acquisition_plan()
    name = "history-probe-20261117T025839Z"
    (tmp_path / name).mkdir()
    blob = json.dumps(summary()).encode()
    (tmp_path / name / "summary.json").write_bytes(blob)
    got = load_probe_gates(tmp_path, name, p)
    assert got.gates == GATES and got.evidence == name and len(got.summary_sha256) == 64
    for bad in ("../x", "history-probe-x", f"{name}/..", "summary.json"):
        with pytest.raises(RunnerRefusedError, match="INVALID_PROBE_EVIDENCE_NAME"):
            load_probe_gates(tmp_path, bad, p)
    link = "history-probe-20261117T000000Z"
    (tmp_path / link).symlink_to(tmp_path / name)
    with pytest.raises(RunnerRefusedError, match="PROBE_EVIDENCE_UNAVAILABLE"):
        load_probe_gates(tmp_path, link, p)


# ============================================================ credentials
def _credential_file(tmp_path: Path, extra: str = "") -> Path:
    f = tmp_path / "synthetic-paper.env"
    f.write_text("".join(f"{k}={v}\n" for k, v in FAKE.items()) + extra)
    f.chmod(0o600)
    return f


def test_live_connection_refuses_conflicting_credential_sources_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swing_trading_bot.app import credentials  # noqa: PLC0415

    def no_read(*_a: object, **_k: object) -> None:
        raise AssertionError("the credential file must not be read")

    monkeypatch.setattr(credentials, "dotenv_values", no_read)
    f = _credential_file(tmp_path)
    p = plan()
    for env, code in (
        ({"STB_ENV_FILE": str(f), "ALPACA_API_KEY": "x"}, "CREDENTIALS_UNAVAILABLE"),
        ({"STB_ENV_FILE": str(f), "TRADING_MODE": "paper"}, "CREDENTIALS_UNAVAILABLE"),
        ({}, "CREDENTIALS_UNAVAILABLE"),
        (dict(FAKE), "CREDENTIAL_SOURCE_NOT_FILE"),
    ):
        http = FakeHttp()
        with pytest.raises(LiveConnectionRefusedError) as err:
            connect_live(p, env, remaining=lambda: FULL, http_session_factory=lambda: http)  # noqa: B023 - used at once
        assert err.value.code == code and http.calls == []


def test_live_connection_refuses_trading_enabled_and_wires_the_guard(tmp_path: Path) -> None:
    p = plan()
    enabled = tmp_path / "on"
    enabled.mkdir()
    f = _credential_file(enabled, "TRADING_ENABLED=true\n")
    with pytest.raises(LiveConnectionRefusedError, match="TRADING_ENABLED_REFUSED"):
        connect_live(p, {"STB_ENV_FILE": str(f)}, remaining=lambda: FULL,
                     http_session_factory=FakeHttp)  # fmt: skip
    ok = _credential_file(tmp_path)
    http = FakeHttp()
    fetch, guarded = connect_live(p, {"STB_ENV_FILE": str(ok)}, remaining=lambda: FULL,
                                  http_session_factory=lambda: http)  # fmt: skip
    batch = p.batches[0]
    page = fetch(batch, next(r for r in p.requests if r.request_id == batch.request_id), None)
    assert isinstance(decode(page.body), list) and guarded.sent == 1 and http.trust_env is False
    assert fetch.guard is guarded and page.received_sha256 is not None
    assert http.calls[0]["url"] == f"{TRADING}/v2/calendar"


# ============================================================ isolation
def test_operations_cannot_touch_the_ledger_directory(paths: ResearchPaths) -> None:
    ledger = paths.root.parent / "ledger.sqlite3"
    with research_guard(paths.guard):
        with pytest.raises(LedgerAccessRefused):
            ledger.open("rb")
        with pytest.raises(LedgerAccessRefused):
            os.listdir(paths.root.parent)
        with pytest.raises(LedgerAccessRefused):
            sqlite3.connect(":memory:")
        with pytest.raises(LedgerAccessRefused):
            (paths.root.parent / "other").mkdir()
        paths.root.mkdir()
        (paths.root / "ok.txt").write_bytes(b"inside the research root is allowed")
    sqlite3.connect(":memory:").close()  # disarmed afterwards
    assert ledger.read_bytes() == b""


def test_research_paths_are_fixed_and_take_no_ledger_path(tmp_path: Path) -> None:
    with pytest.raises(RunnerRefusedError, match="RESEARCH_ROOT_NOT_APPROVED"):
        ResearchPaths(tmp_path / "data")
    with pytest.raises(RunnerRefusedError, match="RESEARCH_ROOT_NOT_ABSOLUTE"):
        ResearchPaths(Path("data/research"))
    with pytest.raises(RunnerRefusedError, match="RESEARCH_ROOT_NOT_ABSOLUTE"):
        ResearchPaths(tmp_path / "x" / ".." / "research")
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (data / "research").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RunnerRefusedError, match="RESEARCH_PATH_NOT_A_DIRECTORY"):
        session(ResearchPaths(data / "research"), connect=never_connect)
    for fn in (run_authorized_session, publish, recover, status):
        assert not any("ledger" in p or p.endswith("db") for p in fn.__code__.co_varnames)


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_the_runner_imports_no_oms_execution_ledger_or_evaluation_module() -> None:
    allowed = ("swing_trading_bot.research.", "swing_trading_bot.history_acquisition",
               "swing_trading_bot.broker.alpaca_http", "swing_trading_bot.broker.errors",
               "swing_trading_bot.broker.mapping", "swing_trading_bot.broker.models",
               "swing_trading_bot.app.credentials",
               "swing_trading_bot.config.settings")  # fmt: skip
    for path in sorted(PKG.glob("*.py")):
        text = path.read_text()
        for name in _imports(path):
            if name.startswith("swing_trading_bot"):
                assert name.startswith(allowed), f"{path.name} imports {name}"
            else:
                banned = {"sqlite3", "socket", "http", "urllib3", "httpx", "subprocess"}
                if path.name == "supervisor.py":  # Step 16.17B: its one job is a child process
                    banned -= {"subprocess"}
                assert name.split(".")[0] not in banned, name
        for needle in ("submit_order", "OrderManager", ".post(", ".put(", ".patch(",
                       ".delete(", "TRADING_ENABLED=", "ledger.sqlite3", "walk_forward",
                       "run_backtest", "HoldoutRelease", "research_inputs"):  # fmt: skip
            assert needle not in text, f"{path.name} mentions {needle}"


def test_importing_the_runner_loads_no_ledger_oms_or_trading_module() -> None:
    code = (
        "import sys\n"
        "import swing_trading_bot.history_acquisition.runner\n"
        "import swing_trading_bot.history_acquisition.transport\n"
        "bad = sorted(m for m in sys.modules if m.startswith(("
        "'swing_trading_bot.ledger', 'swing_trading_bot.oms', 'swing_trading_bot.app.runtime',"
        "'swing_trading_bot.app.commands', 'swing_trading_bot.app.cli',"
        "'swing_trading_bot.app.broker_factory', 'swing_trading_bot.app.simulation',"
        "'swing_trading_bot.broker.alpaca_read', 'swing_trading_bot.broker.sim',"
        "'swing_trading_bot.risk.pretrade', 'sqlite3')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and inline code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == ""


def test_the_research_package_stays_free_of_the_transport() -> None:
    research = PKG.parent / "research"
    for path in sorted(research.glob("*.py")):
        assert "history_acquisition" not in path.read_text(), path.name


# ============================================================ CLI
def test_cli_plan_is_offline_and_reports_the_approved_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["plan"], environ={}) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan_fingerprint"] == APPROVED_ACQUISITION_PLAN_FINGERPRINT
    assert (out["session_max_attempts"], out["session_deadline_s"]) == (120, 300)


def test_cli_refuses_live_sessions_without_the_enable_flag_or_the_fingerprint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_paths() -> None:
        raise AssertionError("no repository path may be touched")

    monkeypatch.setattr(runner_mod, "_repo_paths", no_paths)
    base = ["session", "--probe-evidence", "history-probe-20261117T025839Z",
            "--authorization", "a", "--max-sessions", "2", "--new-store"]  # fmt: skip
    fp = ["--plan-fingerprint", APPROVED_ACQUISITION_PLAN_FINGERPRINT]
    assert main([*base, *fp], environ={}) == 2
    assert json.loads(capsys.readouterr().out) == {"refused": "LIVE_SESSION_NOT_ENABLED"}
    assert main([*base, "--plan-fingerprint", "0" * 64], environ={ENABLE_VARIABLE: "1"}) == 2
    assert json.loads(capsys.readouterr().out) == {"refused": "PLAN_FINGERPRINT_NOT_AUTHORIZED"}
    for cmd in (["status"], ["recover", "--label", "x"],
                ["publish", "--probe-evidence", "x", "--dataset-id", "abc"]):  # fmt: skip
        assert main([*cmd, "--plan-fingerprint", "f" * 64], environ={}) == 2
        assert json.loads(capsys.readouterr().out) == {
            "refused": "PLAN_FINGERPRINT_NOT_AUTHORIZED"}  # fmt: skip
    with pytest.raises(SystemExit):
        main(["session", *fp], environ={})  # missing required authorization arguments
    capsys.readouterr()


def test_cli_has_no_path_or_ledger_arguments() -> None:
    parser = runner_mod._parser()
    flags = {opt for action in parser._subparsers._group_actions  # type: ignore[union-attr]
             for sub in action.choices.values() for a in sub._actions  # type: ignore[union-attr]
             for opt in a.option_strings}  # fmt: skip
    assert not any(("path" in f or "ledger" in f or "dir" in f or "db" in f) for f in flags)


@pytest.fixture(autouse=True)
def _no_live_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENABLE_VARIABLE, raising=False)


def test_batch_kinds_cover_every_endpoint() -> None:
    p = plan()
    kinds = {b.kind for b in p.batches}
    assert kinds == set(BatchKind)


# ============================================================ Step 16.17A.1: Retry-After
def http_date(at: datetime) -> str:
    return format_datetime(at, usegmt=True)


@pytest.mark.parametrize(
    ("value", "wait", "fmt"),
    [
        (None, None, "ABSENT"),
        ("7", timedelta(seconds=7), "DELTA_SECONDS"),
        (" 120 ", timedelta(seconds=120), "DELTA_SECONDS"),
        ("0", timedelta(0), "DELTA_SECONDS"),
        ("999999999999", MAX_RETRY_AFTER, "DELTA_SECONDS"),  # clamped, never overflows
        (http_date(NOW + timedelta(seconds=90)), timedelta(seconds=90), "HTTP_DATE"),
        (http_date(NOW - timedelta(hours=1)), timedelta(0), "HTTP_DATE"),  # past: no wait
        (http_date(NOW + timedelta(days=30)), MAX_RETRY_AFTER, "HTTP_DATE"),
        ("", None, "INVALID"),
        ("soon", None, "INVALID"),
        ("1.5", None, "INVALID"),
        ("-3", None, "INVALID"),
        ("+3", None, "INVALID"),
        ("120, 60", None, "INVALID"),
        ("Tue, 16 Nov 2026 12:01:30 GMT", None, "INVALID"),  # 16 Nov 2026 is a Monday
        ("Mon, 31 Feb 2026 12:00:00 GMT", None, "INVALID"),
        ("Mon, 16 Nov 2026 12:00:00 UTC", None, "INVALID"),
        ("Monday, 16-Nov-26 12:01:30 GMT", None, "INVALID"),  # RFC 850 (obsolete)
        ("Mon Nov 16 12:01:30 2026", None, "INVALID"),  # asctime (obsolete)
    ],
)  # fmt: skip
def test_retry_after_parsing(value: str | None, wait: timedelta | None, fmt: str) -> None:
    assert parse_retry_after(value, NOW) == (wait, fmt)


def test_retry_after_dates_need_an_aware_wall_clock() -> None:
    naive = NOW.replace(tzinfo=None)
    assert parse_retry_after(http_date(NOW), naive) == (None, "INVALID")
    assert parse_retry_after("5", naive) == (timedelta(seconds=5), "DELTA_SECONDS")


def test_retry_after_lengthens_the_fallback_backoff_but_never_shortens_it() -> None:
    def err(after: timedelta | None) -> Exception:
        e = Exception()
        e.retry_after = after  # type: ignore[attr-defined]
        return e

    half = timedelta(milliseconds=500)
    assert retry_wait(1, err(None)) == (half, False)
    assert retry_wait(1, err(timedelta(0))) == (half, False)
    assert retry_wait(2, err(timedelta(seconds=1))) == (timedelta(seconds=1), False)
    assert retry_wait(1, err(timedelta(seconds=9))) == (timedelta(seconds=9), True)
    assert retry_wait(1, err(timedelta(seconds=-9))) == (half, False)  # invalid: ignored
    assert retry_wait(1, Exception()) == (half, False)


def throttled(
    status_code: int, after: str | None, times: int = 1, body: str = "{}"
) -> Callable[[str, dict[str, str]], requests.Response | None]:
    """The first ``times`` trading-calendar requests answer ``status_code`` (+ header)."""
    left = [times]

    def override(url: str, p: dict[str, str]) -> requests.Response | None:
        if url.endswith("/v2/calendar") and p["date_type"] == "TRADING" and left[0]:
            left[0] -= 1
            return response(status_code, body, f"{url}?x",
                            {} if after is None else {"Retry-After": after})  # fmt: skip
        return None

    return override


def attempt_log(out: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(out["transport"]["log"])


@pytest.mark.parametrize(
    ("status_code", "after", "slept", "fmt"),
    [
        (429, "7", timedelta(seconds=7), "DELTA_SECONDS"),
        (503, "7", timedelta(seconds=7), "DELTA_SECONDS"),
        (429, http_date(NOW + timedelta(seconds=12)), timedelta(seconds=12), "HTTP_DATE"),
        (503, http_date(NOW + timedelta(seconds=12)), timedelta(seconds=12), "HTTP_DATE"),
        (429, "soon", timedelta(milliseconds=500), "INVALID"),
        (503, "1.5", timedelta(milliseconds=500), "INVALID"),
        (429, None, timedelta(milliseconds=500), "ABSENT"),
        (429, "0", timedelta(milliseconds=500), "DELTA_SECONDS"),  # never below the fallback
    ],
)  # fmt: skip
def test_sessions_respect_retry_after_or_fall_back_to_the_backoff(
    paths: ResearchPaths, status_code: int, after: str | None, slept: timedelta, fmt: str
) -> None:
    clock = Clock()
    http = FakeHttp(override=throttled(status_code, after))
    out = session(paths, http, clock=clock)
    assert out["status"] == "COMPLETE" and out["stop_reason"] is None
    assert clock.slept == [slept]
    assert out["attempts_used"] == len(http.calls) == len(eligible(plan())) + 1
    first = attempt_log(out)[0]
    assert (first["status"], first["retry_after_format"]) == (status_code, fmt)


def test_a_retry_after_beyond_the_deadline_stops_safely_and_stays_resumable(
    paths: ResearchPaths,
) -> None:
    """Retry-After 600 s cannot fit the 300 s session: no wait at all, nothing of the batch
    staged, a machine-readable reason, and a later authorized session resumes."""
    clock = Clock()
    out = session(paths, FakeHttp(override=throttled(429, "600")), clock=clock)
    assert clock.slept == []
    assert out["status"] == "STOPPED" and out["resumable"] is True
    assert out["stop_reason"] == "RATE_LIMIT_WAIT_EXCEEDS_DEADLINE"
    assert out["stop_code"] == "SESSION_DEADLINE_EXHAUSTED"  # compatible code, not INVALID
    assert out["stop_detail"] == "HTTP_429_WAIT_600000MS"
    assert out["committed"] == [] and str(out["failed_batch"]).startswith("C-CAL-TRADING")
    second = session(paths, FakeHttp(), resume=True, label="auth-2")
    assert second["status"] == "COMPLETE" and second["already_committed"] == []


@pytest.mark.parametrize(
    ("after", "slept", "reason"),
    [("9", [timedelta(seconds=9)], None),  # leaves exactly 1 s: the retry is sent
     ("10", [], "RATE_LIMIT_WAIT_EXCEEDS_DEADLINE"),  # leaves 0 s: never waited for
     (http_date(NOW + timedelta(seconds=10)), [], "RATE_LIMIT_WAIT_EXCEEDS_DEADLINE")],
)  # fmt: skip
def test_a_wait_must_leave_one_second_for_the_retry_itself(
    paths: ResearchPaths, after: str, slept: list[timedelta], reason: str | None
) -> None:
    """The throttled response takes 290 s, leaving 10 s of the session."""
    clock = Clock(per_call=timedelta(0))
    inner = throttled(503, after)

    def slow(url: str, p: dict[str, str]) -> requests.Response | None:
        got = inner(url, p)
        if got is not None:
            clock.t += timedelta(seconds=290)
        return got

    http = FakeHttp(override=slow)
    out = session(paths, http, clock=clock)
    assert timedelta(seconds=1) == MIN_ATTEMPT_TIME and clock.slept == slept
    assert out["stop_reason"] == (reason or "SESSION_TIME_RESERVE_INSUFFICIENT")
    if reason is None:  # the retry got the last whole second, then no batch fits
        assert http.calls[1]["timeout"] == (1, 1) and len(out["committed"]) == 1
    else:
        assert len(http.calls) == 1 and out["committed"] == []


def test_retries_never_exceed_the_per_page_limit_even_with_short_retry_after(
    paths: ResearchPaths,
) -> None:
    clock = Clock()
    http = FakeHttp(override=throttled(429, "1", times=5))
    out = session(paths, http, clock=clock)
    assert out["stop_reason"] == "PAGE_RETRIES_EXHAUSTED"
    assert out["stop_code"] == "RETRIES_EXHAUSTED" and out["stop_detail"] == "HTTP_429"
    assert len(http.calls) == 3 and clock.slept == [timedelta(seconds=1)] * 2


def test_retries_stop_before_waiting_when_the_batch_budget_is_spent(
    paths: ResearchPaths,
) -> None:
    """Bars: 2 pages, budget 4. Page 1 takes 3 attempts; page 2's first attempt is the 4th
    and fails: the retry would be a 5th, so the session stops without sleeping."""
    seen = [0]

    def bars(url: str, p: dict[str, str]) -> requests.Response | None:
        if url.endswith("/bars") and p["symbols"] == "AAA" and p["adjustment"] == "raw":
            seen[0] += 1
            if seen[0] in (1, 2, 4):
                return response(429, "{}", f"{url}?x", {"Retry-After": "2"})
        return None

    clock = Clock()
    out = session(paths, FakeHttp(page_size=200, override=bars), clock=clock)
    assert out["stop_reason"] == "BATCH_ATTEMPT_BUDGET_EXHAUSTED"
    assert out["stop_code"] == "BATCH_BUDGET_EXHAUSTED" and seen[0] == 4
    assert clock.slept == [timedelta(seconds=2)] * 2  # none after the 4th attempt
    assert len(out["committed"]) == 2  # the calendars; nothing of the bars batch


@pytest.mark.parametrize(
    ("status_code", "body", "reason"),
    [
        (401, "{}", "CREDENTIALS_REJECTED"),
        (403, "{}", "CREDENTIALS_REJECTED"),
        (403, '{"message":"subscription does not permit querying recent SIP data"}',
         "ENTITLEMENT_REFUSED"),
    ],
)  # fmt: skip
def test_auth_and_entitlement_refusals_are_never_retried_despite_retry_after(
    paths: ResearchPaths, status_code: int, body: str, reason: str
) -> None:
    def deny(url: str, p: dict[str, str]) -> requests.Response | None:
        if url.endswith("/bars"):
            return response(status_code, body, f"{url}?x", {"Retry-After": "1"})
        return None

    clock = Clock()
    http = FakeHttp(override=deny)
    out = session(paths, http, clock=clock)
    assert out["stop_reason"] == reason and out["stop_code"] == "AUTH_OR_ENTITLEMENT_REFUSED"
    assert out["stop_detail"] == f"HTTP_{status_code}"
    assert clock.slept == [] and len([c for c in http.calls if c["url"].endswith("/bars")]) == 1


# ============================================================ Step 16.17A.1: stop reasons
def test_refusal_codes_map_to_specific_reasons() -> None:
    assert refusal_reason("ENDPOINT_NOT_ALLOWLISTED") is StopReason.UNAUTHORIZED_ENDPOINT
    assert refusal_reason("QUERY_REFUSED") is StopReason.UNAUTHORIZED_ENDPOINT
    assert refusal_reason("REQUEST_NOT_IN_PLAN") is StopReason.UNAUTHORIZED_ENDPOINT
    assert refusal_reason("ATTRIBUTE_REFUSED_POST") is StopReason.UNAUTHORIZED_ENDPOINT
    assert refusal_reason("SESSION_DEADLINE") is StopReason.SESSION_DEADLINE_EXHAUSTED
    assert refusal_reason("SESSION_ATTEMPT_CAP") is StopReason.SESSION_ATTEMPT_BUDGET_EXHAUSTED
    assert refusal_reason("REDIRECT_REFUSED") is StopReason.REDIRECT_REFUSED
    assert refusal_reason("UNEXPECTED_RESPONSE_URL") is StopReason.REDIRECT_REFUSED
    assert refusal_reason("RECEIVED_BODY_UNAVAILABLE") is StopReason.PROVENANCE_UNAVAILABLE
    assert refusal_reason("FLOAT_IN_RESPONSE") is StopReason.INVALID_PROVIDER_RESPONSE
    assert refusal_reason("BARS_PAGE_SHAPE") is StopReason.INVALID_PROVIDER_RESPONSE


def _endpoints_without_calendar() -> dict[tuple[str, str], frozenset[str]]:
    return {k: v for k, v in plan_endpoints(plan()).items() if k[1] != "/v2/calendar"}


@pytest.mark.parametrize(
    ("kw", "override", "expect"),
    [
        ({"shift": timedelta(seconds=299, milliseconds=500)}, None,
         ("SESSION_DEADLINE_EXHAUSTED", "SESSION_DEADLINE_EXHAUSTED", "SESSION_DEADLINE", 0, 0)),
        ({"max_attempts": 1}, None, ("SESSION_ATTEMPT_BUDGET_EXHAUSTED",
         "SESSION_BUDGET_EXHAUSTED", "SESSION_ATTEMPT_CAP", 1, 1)),
        ({"endpoints": _endpoints_without_calendar()}, None, ("UNAUTHORIZED_ENDPOINT",
         "REQUEST_REFUSED", "ENDPOINT_NOT_ALLOWLISTED", 0, 0)),
        ({}, lambda u, p: response(302, "", f"{u}?x"), ("REDIRECT_REFUSED", "REQUEST_REFUSED",
         "REDIRECT_REFUSED", 1, 0)),
        ({}, lambda u, p: response(200, '{"a": 1}', f"{u}?x"), ("INVALID_PROVIDER_RESPONSE",
         "INVALID_RESPONSE", "CALENDAR_NOT_A_LIST", 1, 0)),
    ],
)  # fmt: skip
def test_guard_and_adapter_refusals_propagate_their_specific_reason(
    paths: ResearchPaths, kw: dict[str, Any], override: Override | None,
    expect: tuple[str, str, str, int, int],
) -> None:  # fmt: skip
    """A deadline, attempt-cap or authorization refusal is never reported as malformed
    provider data; each keeps its reason and fixed-text detail."""
    reason, code, detail, sent, staged = expect
    p = plan()
    http = FakeHttp(override=override)
    out = session(paths, connect=connect_with(http, p, **kw))
    assert (out["stop_reason"], out["stop_code"], out["stop_detail"]) == (reason, code, detail)
    assert out["status"] == "STOPPED" and out["resumable"] is True
    assert len(http.calls) == sent and len(out["committed"]) == staged


# ============================================================ Step 16.17A.1: deadlines
@pytest.mark.parametrize(
    ("left", "timeouts"),
    [
        (timedelta(seconds=1), (1, 1)),  # exactly one second: allowed, never more
        (timedelta(seconds=1, milliseconds=999), (1, 1)),
        (timedelta(seconds=3, milliseconds=200), (3, 3)),
        (timedelta(seconds=14, milliseconds=900), (5, 14)),  # read timeout cut, not rounded up
        (timedelta(seconds=300), (5, 20)),
    ],
)  # fmt: skip
def test_request_timeouts_never_exceed_the_time_left(
    left: timedelta, timeouts: tuple[int, int]
) -> None:
    g, h = guard(remaining=left)
    g.get(f"{TRADING}/v2/calendar", params=CAL_PARAMS, allow_redirects=False)
    assert h.calls[0]["timeout"] == timeouts
    assert all(t <= left.total_seconds() for t in h.calls[0]["timeout"])


@pytest.mark.parametrize("left", [timedelta(milliseconds=999), timedelta(microseconds=1),
                                  timedelta(0), timedelta(seconds=-5)])  # fmt: skip
def test_less_than_one_second_left_sends_nothing(left: timedelta) -> None:
    g, h = guard(remaining=left)
    with pytest.raises(TransportRefusedError) as err:
        g.get(f"{TRADING}/v2/calendar", params=CAL_PARAMS, allow_redirects=False)
    assert err.value.reason is StopReason.SESSION_DEADLINE_EXHAUSTED and h.calls == []


def test_the_session_deadline_running_out_during_pagination_stages_nothing_of_the_batch(
    paths: ResearchPaths,
) -> None:
    clock = Clock(per_call=timedelta(0))

    def slow_first_page(url: str, p: dict[str, str]) -> None:
        if url.endswith("/bars") and "page_token" not in p and p["adjustment"] == "raw":
            clock.t = timedelta(seconds=299, milliseconds=500)

    http = FakeHttp(page_size=200, override=slow_first_page)
    out = session(paths, http, clock=clock)
    assert out["stop_reason"] == "SESSION_DEADLINE_EXHAUSTED"
    assert out["stop_detail"] == "SESSION_TIME_LEFT"
    assert len(out["committed"]) == 2 and str(out["failed_batch"]).startswith("C-BARS-AAA-RAW")
    assert not any("page_token" in c["params"] for c in http.calls)  # page 2 never sent


def test_the_session_attempt_budget_running_out_during_pagination(paths: ResearchPaths) -> None:
    """The guard's hard cap (3) is reached on page 2 of the first bars batch."""
    p = plan()
    http = FakeHttp(page_size=200)
    out = session(paths, connect=connect_with(http, p, max_attempts=3))
    assert out["stop_reason"] == "SESSION_ATTEMPT_BUDGET_EXHAUSTED"
    assert out["stop_detail"] == "SESSION_ATTEMPT_CAP" and len(http.calls) == 3
    assert len(out["committed"]) == 2 and str(out["failed_batch"]).startswith("C-BARS-AAA-RAW")
    staged = StagingStore.open(staging_root(paths), p).committed()
    assert not any(b.startswith("C-BARS") for b in staged)


# ============================================================ Step 16.17A.1: provenance
RAW_CALENDAR = (' [ {"settlement_date": "2020-07-06", "date": "2020-07-01",\n'
                '   "open": "09:30", "close": "16:00", "session_open": "0400",\n'
                '   "session_close": "2000"} ] ')  # fmt: skip


def test_received_and_canonical_digests_are_kept_apart(paths: ResearchPaths) -> None:
    def raw_calendar(url: str, p: dict[str, str]) -> requests.Response | None:
        if url.endswith("/v2/calendar") and p["date_type"] == "TRADING":
            return response(200, RAW_CALENDAR, f"{url}?x")
        return None

    p = plan()
    out = session(paths, FakeHttp(override=raw_calendar))
    store = StagingStore.open(staging_root(paths), p)
    records = store.committed()
    cal = next(r for b, r in records.items() if b.startswith("C-CAL-TRADING"))
    ((received, size),) = StagingStore.received(cal)
    assert received == hashlib.sha256(RAW_CALENDAR.encode()).hexdigest()
    assert size == len(RAW_CALENDAR.encode())
    stored = store.pages(cal)[0]
    assert cal.pages[0] == hashlib.sha256(stored).hexdigest() != received
    assert stored == canonical_body(decode(RAW_CALENDAR.encode()))
    assert cal.raw["provenance"] == {"sha256": STORED_DIGEST_SCOPE,
                                     "received_sha256": RECEIVED_DIGEST_SCOPE}  # fmt: skip
    for record in records.values():  # every staged page carries both, never equal
        for (got, n), digest in zip(StagingStore.received(record), record.pages, strict=True):
            assert got is not None and n is not None and got != digest
    log = [a["received_sha256"] for a in attempt_log(out) if a["status"] == 200]
    assert received in log and len(log) == len(out["committed"])
    blobs = [f.read_bytes() for f in (staging_root(paths) / "blobs").iterdir()]
    assert RAW_CALENDAR.encode() not in blobs  # the received bytes are never stored


def test_without_received_bytes_no_raw_digest_is_claimed() -> None:
    p = plan()
    batch = p.batches[0]
    req = next(r for r in p.requests if r.request_id == batch.request_id)
    bare = AcquisitionFetch(p, AlpacaReadTransport(SETTINGS, session=FakeHttp()))  # type: ignore[arg-type]
    page = bare(batch, req, None)
    assert (page.received_sha256, page.received_bytes) == (None, None)

    class NoContent(FakeHttp):
        def get(self, url: str, **kw: Any) -> Any:
            r = super().get(url, **kw)
            return SimpleNamespace(status_code=200, url=r.url, text=r.text, headers={})

    g = GuardedHttpSession(NoContent(), endpoints=plan_endpoints(p), max_attempts=5,
                           remaining=lambda: FULL)  # fmt: skip
    guarded = AcquisitionFetch(p, AlpacaReadTransport(SETTINGS, session=g),  # type: ignore[arg-type]
                               guard=g)  # fmt: skip
    with pytest.raises(TransportRefusedError) as err:
        guarded(batch, req, None)
    assert err.value.code == "RECEIVED_BODY_UNAVAILABLE"
    assert err.value.reason is StopReason.PROVENANCE_UNAVAILABLE
    with pytest.raises(InvalidBrokerDataError):
        FetchedPage(b"{}", "not-a-digest", 2)
    with pytest.raises(InvalidBrokerDataError):
        FetchedPage(b"{}", "0" * 64, None)


def test_a_tampered_received_digest_is_detected(paths: ResearchPaths) -> None:
    session(paths)
    p = plan()
    path = next((staging_root(paths) / "batches").iterdir())
    doc = json.loads(path.read_bytes())
    doc["pages"][0]["received_sha256"] = "f" * 64
    path.chmod(0o644)
    path.write_text(json.dumps(doc))
    with pytest.raises(StagingIntegrityError, match="record digest mismatch"):
        StagingStore.open(staging_root(paths), p).committed()
    # even with a recomputed record digest, a malformed received digest fails closed
    from swing_trading_bot.research.dataset import canonical, sha256  # noqa: PLC0415

    doc["pages"][0]["received_sha256"] = "XYZ"
    body = {k: v for k, v in doc.items() if k != "record_sha256"}
    doc["record_sha256"] = sha256(canonical(body).encode())
    path.write_text(json.dumps(doc))
    with pytest.raises(StagingIntegrityError, match="received-body digest invalid"):
        StagingStore.open(staging_root(paths), p).committed()


def test_reports_redact_credentials_tokens_and_bodies(paths: ResearchPaths) -> None:
    marker = "BODY-MARKER-should-never-be-logged"
    fired: set[int] = set()

    def noisy(url: str, p: dict[str, str]) -> requests.Response | None:
        if url.endswith("/bars") and "page_token" in p and 429 not in fired:
            fired.add(429)
            return response(429, json.dumps({"message": marker}), f"{url}?x",
                            {"Retry-After": "1"})  # fmt: skip
        if url.endswith("/v2/calendar") and p["date_type"] == "SETTLEMENT" and 503 not in fired:
            fired.add(503)
            return response(503, marker, f"{url}?x", {"Retry-After": "nonsense " + marker})
        return None

    http = FakeHttp(page_size=200, override=noisy)
    out = session(paths, http)
    assert out["status"] == "COMPLETE" and fired == {429, 503}
    formats = {a["retry_after_format"] for a in attempt_log(out)} - {None}
    assert formats == {"DELTA_SECONDS", "INVALID"}
    tokens = {c["params"]["page_token"] for c in http.calls if "page_token" in c["params"]}
    assert tokens  # pagination happened
    texts = [json.dumps(out)] + [f.read_text() for f in
                                 (staging_root(paths) / "sessions").iterdir()]  # fmt: skip
    for text in texts:
        for secret in (*FAKE.values(), *tokens, marker, "APCA-API"):
            if secret == "paper":
                continue
            assert secret not in text
    hashed = {a["page_token_sha256"] for a in attempt_log(out)} - {None}
    assert hashed == {hashlib.sha256(t.encode()).hexdigest()[:16] for t in tokens}


# ============================================================ Step 16.17A.3: late responses
LATE = "RESPONSE_AFTER_DEADLINE"


def late_at(clock: Clock, at: timedelta, *, page2: bool = False,
            status: int | None = None) -> Override:  # fmt: skip
    """The settlement calendar (or, with ``page2``, the first page-2 request) completes at
    session time ``at``; with ``status`` it answers that status instead of a page."""

    def override(url: str, p: dict[str, str]) -> requests.Response | None:
        hit = "page_token" in p if page2 else p.get("date_type") == "SETTLEMENT"
        if not hit:
            return None
        clock.t = at
        if status is None:
            return None
        return response(status, "{}", f"{url}?{urlencode(p)}", {"Retry-After": "1"})

    return override


@pytest.mark.parametrize(
    ("at", "accepted"),
    [(FULL - timedelta(milliseconds=1), True), (FULL, False),
     (FULL + timedelta(seconds=1), False)],
)  # fmt: skip
def test_the_guard_refuses_a_response_completing_at_or_after_the_deadline(
    paths: ResearchPaths, at: timedelta, accepted: bool
) -> None:
    clock = Clock(per_call=timedelta(0))
    http = FakeHttp(override=late_at(clock, at))
    out = session(paths, http, clock=clock)
    assert len(http.calls) == out["attempts_used"] == out["transport"]["attempts_sent"] == 2
    if accepted:
        assert out["status"] == "INCOMPLETE" and len(out["committed"]) == 2
        return
    assert out["status"] == "STOPPED" and out["resumable"] is True
    assert out["stop_code"] == "SESSION_DEADLINE_EXHAUSTED"  # compatible, never INVALID
    assert (out["stop_reason"], out["stop_detail"]) == ("SESSION_DEADLINE_EXHAUSTED", LATE)
    assert len(out["committed"]) == 1
    assert str(out["failed_batch"]).startswith("C-CAL-SETTLEMENT")
    last = attempt_log(out)[-1]
    assert last["status"] == 200 and last["error"] == "REFUSED_RESPONSE_AFTER_DEADLINE"
    assert last["received_sha256"] is None  # never read, never digested
    assert set(out["transport"]["refusals"]) == {LATE}  # guard and adapter each record it
    store = StagingStore.open(staging_root(paths), plan())
    assert set(store.committed()) == set(out["committed"])  # the earlier batch verifies
    second = session(paths, FakeHttp(), resume=True, label="auth-2")
    assert second["status"] == "COMPLETE"
    assert second["already_committed"] == out["committed"]
    assert (paths.root.parent / "ledger.sqlite3").read_bytes() == b""


def test_a_second_page_after_the_deadline_stages_nothing_of_the_batch(
    paths: ResearchPaths,
) -> None:
    clock = Clock(per_call=timedelta(0))
    http = FakeHttp(page_size=200, override=late_at(clock, FULL, page2=True))
    out = session(paths, http, clock=clock)
    assert (out["stop_reason"], out["stop_detail"]) == ("SESSION_DEADLINE_EXHAUSTED", LATE)
    assert str(out["failed_batch"]).startswith("C-BARS-AAA-RAW")
    assert sum(1 for c in http.calls if "page_token" in c["params"]) == 1
    staged = StagingStore.open(staging_root(paths), plan()).committed()
    assert len(staged) == 2 and not any(b.startswith("C-BARS") for b in staged)


@pytest.mark.parametrize("status", [429, 503])
def test_a_retryable_response_after_the_deadline_is_never_retried(
    paths: ResearchPaths, status: int
) -> None:
    clock = Clock(per_call=timedelta(0))
    http = FakeHttp(override=late_at(clock, FULL, status=status))
    out = session(paths, http, clock=clock)
    assert clock.slept == [] and len(http.calls) == out["attempts_used"] == 2
    assert (out["stop_reason"], out["stop_detail"]) == ("SESSION_DEADLINE_EXHAUSTED", LATE)
    assert attempt_log(out)[-1]["retry_after_format"] is None  # not read once expired


def test_a_timeout_after_the_deadline_is_never_retried(paths: ResearchPaths) -> None:
    clock = Clock(per_call=timedelta(0))

    def timeout(url: str, p: dict[str, str]) -> BaseException | None:
        if p.get("date_type") != "SETTLEMENT":
            return None
        clock.t = FULL
        return requests.Timeout("slow")

    http = FakeHttp(override=timeout)
    out = session(paths, http, clock=clock)
    assert clock.slept == [] and len(http.calls) == 2
    assert (out["stop_reason"], out["stop_detail"]) == ("SESSION_DEADLINE_EXHAUSTED", LATE)


def test_the_guard_counts_the_late_attempt_and_records_nothing_of_its_body() -> None:
    left = [timedelta(seconds=300)]

    def finish_late(_url: str, _p: dict[str, str]) -> None:
        left[0] = timedelta(0)

    h = FakeHttp(override=finish_late)
    g = GuardedHttpSession(h, endpoints=plan_endpoints(plan()), max_attempts=120,
                           remaining=lambda: left[0], wall_clock=lambda: NOW)  # fmt: skip
    with pytest.raises(TransportRefusedError) as err:
        g.get(f"{TRADING}/v2/calendar", params=CAL_PARAMS, allow_redirects=False)
    assert (err.value.code, err.value.reason) == (LATE, StopReason.SESSION_DEADLINE_EXHAUSTED)
    assert g.sent == 1 and len(h.calls) == 1
    assert g.pop_received() is None and g.pop_retry_after() is None
    assert refusal_reason(LATE) is StopReason.SESSION_DEADLINE_EXHAUSTED
