"""Step 15B.5B.1: activity raw-JSON serialization through the collector, runtime,
reconciliation, init-ledger, FEE attribution and contract C7 — offline, synthetic data.

Before: a JSON number with a fraction/exponent anywhere in a non-trade activity (decoded
as ``Decimal``) made ``raw_json`` raise ``TypeError`` -> exit 1 INTERNAL, no
``BROKER_PAYLOAD`` halt, init-ledger reported the wrong category. Now a valid number in
an unmapped field is stored exactly; a numeric *money* field or a non-finite number is
an ``InvalidBrokerDataError`` handled by the existing payload paths (exit 10 / halt / 4).

Rows go through the fake's ``json.dumps`` and the production ``decode_json``: a Python
float becomes a ``Decimal``, ``math.nan`` arrives as the decoder's ``NaN`` constant.
"""

from __future__ import annotations

import json
import math
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import (
    IncompleteRetrievalError,
    InvalidBrokerDataError,
    ServiceUnavailableError,
    payload_cause,
)
from swing_trading_bot.ledger.activity_coverage import complete_through
from swing_trading_bot.ledger.broker_outage import load_streak
from swing_trading_bot.ledger.repositories import TradingStateRepository
from tests.contract.outcomes import CheckStatus
from tests.contract.test_contract_replay import synthetic_account
from tests.integration.test_activity_payload_paths import (
    c7,
    cash_row,
    confirm,
    fees,
    non_fill_rows,
    payload_halts,
    window,
)
from tests.support.alpaca_account import START, Clock, FakeAlpacaAccount
from tests.support.cli_env import CliEnv, cli_env
from tests.support.recon import recon_env

D = Decimal
NUMERIC_MONEY: dict[str, Any] = {"net_amount": 10.5}  # JSON number -> Decimal -> rejected
NON_FINITE: dict[str, Any] = {"unmapped": math.nan}  # NaN constant -> not JSON
VALID_EXTRA: dict[str, Any] = {"fee_rate": 0.000166, "nested": {"a": [1.50, 2, "x"]}}
MALFORMED = [pytest.param(NUMERIC_MONEY, id="numeric-money"),
             pytest.param(NON_FINITE, id="nan-unmapped"),
             pytest.param({"unmapped": {"deep": [math.inf]}}, id="inf-nested")]  # fmt: skip


def row(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**cash_row(""), **(extra or {})}


def account(rows: list[dict[str, Any]]) -> FakeAlpacaAccount:
    acct = FakeAlpacaAccount(clock=Clock(START))
    for n, r in enumerate(rows):
        acct.clock.now = START - timedelta(hours=3) + timedelta(minutes=n)
        acct.post(r)
    acct.clock.now = START
    return acct


# ============================================================ collector
def test_valid_numbers_in_unmapped_fields_are_collected_exactly() -> None:
    w = window(account([row(VALID_EXTRA), row()]))
    assert w.tail_confirmed and w.closed and len(w.activities) == 2
    stored = json.loads(w.activities[0].raw_json, parse_float=Decimal)
    assert stored["fee_rate"] == D("0.000166") and stored["nested"] == {"a": [D("1.50"), 2, "x"]}
    assert '"fee_rate":0.000166' in w.activities[0].raw_json


@pytest.mark.parametrize("bad", MALFORMED)
def test_malformed_first_page_fails_closed_as_a_payload_error(bad: dict[str, Any]) -> None:
    with pytest.raises(IncompleteRetrievalError) as info:
        window(account([row(), row(bad), row()]))
    assert isinstance(payload_cause(info.value), InvalidBrokerDataError)
    assert not isinstance(info.value.__cause__, TypeError)
    assert info.value.partial == ()


@pytest.mark.parametrize("bad", MALFORMED)
def test_malformed_later_page_keeps_partial_and_never_completes(bad: dict[str, Any]) -> None:
    rows = [row() for _ in range(25)]
    rows[17] = row(bad)  # third page with page_size=8
    with pytest.raises(IncompleteRetrievalError) as info:
        window(account(rows), page_size=8)
    assert isinstance(payload_cause(info.value), InvalidBrokerDataError)
    assert len(info.value.partial) == 16


def test_genuine_interruption_is_unchanged() -> None:
    acct = account([row(VALID_EXTRA)])
    acct.fail("/account/activities", *[ServiceUnavailableError("t", kind="timeout")] * 3)
    with pytest.raises(IncompleteRetrievalError) as info:
        window(acct)
    assert payload_cause(info.value) is None


# ============================================================ runtime (CLI)
@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def post(e: CliEnv, extra: dict[str, Any] | None = None) -> None:
    e.account.cash += D(10)
    e.account.post(row(extra))
    e.account.clock.advance(minutes=20)


def stored_csd(e: CliEnv) -> list[str]:
    with e.open() as db, db.read() as s:
        rows = s.fetch_all("SELECT raw_json FROM broker_activities WHERE activity_type='CSD'")
    return [str(r[0]) for r in rows]


def test_valid_unmapped_numbers_sync_without_a_halt(e: CliEnv) -> None:
    e.ready()
    post(e, VALID_EXTRA)
    code, env = e.run("sync")
    assert code in (0, 4, 5)  # never INTERNAL or BROKER_PAYLOAD
    assert env["result"]["preflight"]["halted"] is None and payload_halts(e) == 0
    raws = [r for r in stored_csd(e) if "fee_rate" in r]
    assert len(raws) == 1 and '"fee_rate":0.000166' in raws[0]
    with e.open() as db, db.read() as s:
        got = s.fetch_all("SELECT json_extract(raw_json, '$.nested.a[0]') FROM "
                          "broker_activities WHERE raw_json LIKE '%fee_rate%'")  # fmt: skip
    assert got[0][0] == 1.5  # valid JSON for SQLite too


@pytest.mark.parametrize("bad", MALFORMED)
def test_malformed_activity_is_a_payload_halt_not_internal(e: CliEnv, bad: dict[str, Any]) -> None:
    e.ready()
    with e.open() as db, db.read() as s:
        marker, streak = complete_through(s), load_streak(s)
    before = stored_csd(e)
    post(e, bad)
    code, env = e.run("sync")
    assert code == 10 and env["result"]["halts_raised"] == ["BROKER_PAYLOAD"]
    assert env["result"]["sync"]["status"] == "HALTED"
    assert env["result"]["broker_outage"]["event"] == "UNCHANGED"
    assert env["result"]["broker_outage"]["cycle_failures"] == []
    code, _ = e.run("sync")
    assert code == 4  # the recorded persistent halt now governs
    assert payload_halts(e) >= 1
    with e.open() as db, db.read() as s:
        state = TradingStateRepository().get(s)
        assert state.halted and state.reason_code == "BROKER_PAYLOAD"
        assert complete_through(s) == marker and load_streak(s) == streak
    assert stored_csd(e) == before  # the malformed activity was never booked


def test_an_existing_halt_keeps_exit_4_precedence(e: CliEnv) -> None:
    e.ready()
    assert e.run("halt", "--reason", "maintenance")[0] == 0
    post(e, NUMERIC_MONEY)
    code, _ = e.run("sync")
    assert code == 4


def test_reconcile_on_a_malformed_activity_is_exit_10_and_never_clean(e: CliEnv) -> None:
    e.ready()
    post(e, NON_FINITE)
    code, env = e.run("reconcile")
    assert code == 10
    assert env["result"]["reconciliation"]["outcome"] != "CLEAN"
    assert "PERSISTENT_HALT" in env["result"]["gates"]["reason_codes"]
    assert env["result"]["gates"]["new_entries_allowed"] is False


# ============================================================ init-ledger
def post_before_start(e: CliEnv, extra: dict[str, Any]) -> None:
    now = e.account.clock.now
    e.account.clock.now = now - timedelta(days=1)
    e.account.post({**cash_row("", amount="0.01", kind="INT"), **extra})
    e.account.clock.now = now


@pytest.mark.parametrize("bad", MALFORMED)
def test_init_refuses_with_exit_10_and_creates_nothing(e: CliEnv, bad: dict[str, Any]) -> None:
    post_before_start(e, bad)
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 10 and env["result"]["status"] == "BROKER_PAYLOAD"
    assert env["result"]["problems"][0].startswith("InvalidBrokerDataError:")
    assert not e.ledger.exists()
    assert sorted(p.name for p in e.data.iterdir()) == ["ledger.sqlite3.lock"]


def test_init_accepts_valid_unmapped_numbers(e: CliEnv) -> None:
    post_before_start(e, VALID_EXTRA)
    e.init()
    assert e.ledger.exists()


# ============================================================ FEE twins / persistence format
def test_fee_twins_and_stored_format_are_unchanged(tmp_path: Path) -> None:
    r = recon_env(tmp_path)
    assert fees(r, "", "", "REG") == ["ATTRIBUTABLE", "ATTRIBUTABLE", "UNEXPLAINED"]
    with r.o.db.read() as s:
        stored = [str(x[0]) for x in s.fetch_all("SELECT raw_json FROM broker_activities")]
    for text in stored:  # byte-identical to the pre-15B.5B.1 encoding
        assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))


# ============================================================ contract C7
def test_c7_with_valid_unmapped_numbers_is_verified() -> None:
    acct = synthetic_account()
    for r in non_fill_rows(acct)[:5]:
        r["fee_rate"] = 0.25
    result, outcome = c7(acct)
    assert result.status is CheckStatus.PASSED
    assert outcome == {"outcome": "VERIFIED", "code": "OK"}


@pytest.mark.parametrize("bad", MALFORMED)
def test_c7_malformed_serialization_is_failed(bad: dict[str, Any]) -> None:
    acct = synthetic_account()
    non_fill_rows(acct)[15].update(bad)  # on a later page of the size-10 scan
    result, outcome = c7(acct)
    assert result.status is CheckStatus.VIOLATION
    assert outcome == {"outcome": "FAILED", "code": "BROKER_PAYLOAD_CONTRACT_BROKEN"}


def test_c7_network_interruption_stays_inconclusive() -> None:
    acct = synthetic_account()
    acct.fail("/account/activities", *[ServiceUnavailableError("t", kind="timeout")] * 3)
    _, outcome = c7(acct)
    assert outcome == {"outcome": "INCONCLUSIVE", "code": "BROKER_INCOMPLETE_RETRIEVAL"}


def test_c7_too_few_activities_stays_inconclusive() -> None:
    _, outcome = c7(account([row(VALID_EXTRA)]))
    assert outcome == {"outcome": "INCONCLUSIVE",
                       "code": "TOO_FEW_ACTIVITIES_TO_EXERCISE_PAGINATION"}  # fmt: skip
