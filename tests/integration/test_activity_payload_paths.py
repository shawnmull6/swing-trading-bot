"""Step 15B.5B: activity ``description`` compatibility and wrapped payload errors through
the collector, the runtime, reconciliation, init-ledger, fee attribution and contract C7 —
offline, synthetic data only.

Live run #3 saw a non-trade activity whose ``description`` was ``""``; the mapper
rejected it, ``collect_window`` wrapped that in ``IncompleteRetrievalError`` and init /
CLI paths reported "broker unavailable" (exit 8). Now ``""`` is a valid description, and a
genuinely malformed activity is a payload error (exit 10) wherever it surfaces, while
completeness guarantees are unchanged: a window that failed part-way is never complete.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.activity_feed import collect_window
from swing_trading_bot.broker.errors import (
    IncompleteRetrievalError,
    InvalidBrokerDataError,
    ServiceUnavailableError,
    payload_cause,
)
from swing_trading_bot.domain.enums import ActivityAttribution
from swing_trading_bot.ledger.activity_coverage import complete_through
from swing_trading_bot.ledger.broker_outage import load_streak
from swing_trading_bot.ledger.repositories import TradingStateRepository
from tests.contract import checks as contract_checks
from tests.contract.outcomes import CheckStatus, classify_rows, run_check
from tests.contract.test_contract_replay import synthetic_account, wire
from tests.support.alpaca_account import START, Clock, FakeAlpacaAccount
from tests.support.cli_env import CliEnv, cli_env
from tests.support.recon import ReconEnv, recon_env

D = Decimal
LAG = timedelta(minutes=15)


def cash_row(description: object = "", kind: str = "CSD", amount: str = "10") -> dict[str, Any]:
    row: dict[str, Any] = {"activity_type": kind, "date": "2026-11-09", "net_amount": amount,
                           "status": "executed", "currency": "USD"}  # fmt: skip
    if description is not MISSING:
        row["description"] = description
    return row


MISSING = object()


def account_with(*descriptions: object, hours_ago: float = 3) -> FakeAlpacaAccount:
    acct = FakeAlpacaAccount(clock=Clock(START))
    for n, desc in enumerate(descriptions):
        acct.clock.now = START - timedelta(hours=hours_ago) + timedelta(minutes=n)
        acct.post(cash_row(desc))
    acct.clock.now = START
    return acct


def window(acct: FakeAlpacaAccount, page_size: int = 100) -> Any:
    return collect_window(acct.adapter(), after=START - timedelta(days=1),
                          until=START - timedelta(minutes=20), now=START, posting_lag=LAG,
                          page_size=page_size)  # fmt: skip


# ============================================================ collector
def test_empty_descriptions_are_collected_and_the_tail_is_confirmed() -> None:
    w = window(account_with("", "", "REG", MISSING, None))
    assert w.tail_confirmed and w.closed
    assert [a.description for a in w.activities] == ["", "", "REG", None, None]


@pytest.mark.parametrize("bad", [5, True, [], {}])
def test_malformed_description_on_the_first_page(bad: object) -> None:
    with pytest.raises(IncompleteRetrievalError) as info:
        window(account_with("ok", bad, "ok"))
    assert isinstance(payload_cause(info.value), InvalidBrokerDataError)
    assert info.value.partial == ()  # the bad page contributes nothing


def test_malformed_description_on_a_later_page_keeps_partial_incomplete() -> None:
    descs: list[object] = ["" for _ in range(25)]
    descs[17] = 1.5  # third page with page_size=8
    with pytest.raises(IncompleteRetrievalError) as info:
        window(account_with(*descs), page_size=8)
    assert isinstance(payload_cause(info.value), InvalidBrokerDataError)
    assert len(info.value.partial) == 16  # two good pages read, never reported complete


def test_genuine_interruption_is_not_a_payload_error() -> None:
    acct = account_with("", "")
    acct.fail("/account/activities", *[ServiceUnavailableError("t", kind="timeout")] * 3)
    with pytest.raises(IncompleteRetrievalError) as info:
        window(acct)
    assert payload_cause(info.value) is None


# ============================================================ runtime (CLI)
@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def post_now(e: CliEnv, description: object, amount: str = "10") -> None:
    e.account.cash += D(amount)
    e.account.post(cash_row(description, amount=amount))
    e.account.clock.advance(minutes=20)


def payload_halts(e: CliEnv) -> int:
    with e.open() as db, db.read() as s:
        return sum(1 for x in TradingStateRepository().events(s)
                   if x.action == "HALT" and x.reason_code == "BROKER_PAYLOAD")  # fmt: skip


def test_valid_empty_description_does_not_halt(e: CliEnv) -> None:
    e.ready()
    post_now(e, "")
    code, env = e.run("sync")
    assert env["result"]["preflight"]["halted"] is None
    assert env["result"]["sync"]["status"] != "HALTED"
    assert payload_halts(e) == 0
    with e.open() as db, db.read() as s:
        raw = s.fetch_all("SELECT raw_json FROM broker_activities WHERE activity_type = 'CSD'")
    assert any(json.loads(str(r[0])).get("description") == "" for r in raw)
    assert code in (0, 4, 5)  # the deposit itself is a reconciliation matter, not a payload one


def test_malformed_activity_halts_once_without_outage_or_coverage(e: CliEnv) -> None:
    e.ready()
    with e.open() as db, db.read() as s:
        marker, streak = complete_through(s), load_streak(s)
    post_now(e, 7)
    code, env = e.run("sync")
    assert code == 10 and env["result"]["halts_raised"] == ["BROKER_PAYLOAD"]
    assert env["result"]["sync"]["status"] == "HALTED"
    assert env["result"]["broker_outage"]["event"] == "UNCHANGED"
    assert env["result"]["broker_outage"]["cycle_failures"] == []
    code, env = e.run("sync")
    assert code == 4  # governed by the recorded persistent halt
    assert payload_halts(e) >= 1  # (each mid-sync recurrence is re-recorded: existing rule)
    with e.open() as db, db.read() as s:
        state = TradingStateRepository().get(s)
        assert state.halted and state.reason_code == "BROKER_PAYLOAD"  # never auto-cleared
        assert complete_through(s) == marker  # the window was never accepted as complete
        assert load_streak(s) == streak
        bad = s.fetch_all("SELECT count(*) FROM broker_activities WHERE json_extract("
                          "raw_json, '$.description') = 7")  # fmt: skip
        assert bad[0][0] == 0


def test_reconciliation_stays_fail_closed_on_a_malformed_activity(e: CliEnv) -> None:
    e.ready()
    post_now(e, {"nested": "object"})
    code, env = e.run("reconcile")
    assert code == 10
    recon = env["result"]["reconciliation"]
    assert recon["outcome"] in ("INCONCLUSIVE", "MISMATCH") and recon["outcome"] != "CLEAN"
    assert "PERSISTENT_HALT" in env["result"]["gates"]["reason_codes"]
    assert env["result"]["gates"]["new_entries_allowed"] is False


# ============================================================ init-ledger
def confirm(e: CliEnv) -> tuple[str, ...]:
    return ("--confirm-path", str(e.ledger), "--confirm-account", e.account.account_id,
            "--confirm-environment", "paper")  # fmt: skip


def post_before_start(e: CliEnv, description: object) -> None:
    now = e.account.clock.now
    e.account.clock.now = now - timedelta(days=1)
    e.account.post(cash_row(description, amount="0.01", kind="INT"))
    e.account.clock.now = now


def test_init_accepts_an_empty_description(e: CliEnv) -> None:
    post_before_start(e, "")
    e.init()
    assert e.ledger.exists()


@pytest.mark.parametrize("bad", [0, False, ["x"]])
def test_init_refuses_a_malformed_activity_with_exit_10(e: CliEnv, bad: object) -> None:
    post_before_start(e, bad)
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 10 and env["result"]["status"] == "BROKER_PAYLOAD"
    assert env["result"]["problems"][0].startswith("InvalidBrokerDataError:")
    assert not e.ledger.exists()
    assert sorted(p.name for p in e.data.iterdir()) == ["ledger.sqlite3.lock"]


def test_init_interrupted_by_a_timeout_is_still_exit_8(e: CliEnv) -> None:
    e.account.fail("/account/activities", *[ServiceUnavailableError("t", kind="timeout")] * 3)
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 8 and env["result"]["status"] == "BROKER_UNAVAILABLE"
    assert not e.ledger.exists()


# ============================================================ FEE twins with ""
@pytest.fixture
def r(tmp_path: Path) -> ReconEnv:
    return recon_env(tmp_path)


def fees(r: ReconEnv, *descriptions: str | None) -> list[str]:
    r.o.advance(timedelta(0))
    coid = r.o.place().client_order_id
    assert coid is not None
    r.o.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5))
    for desc in descriptions:
        r.o.sim.schedule_cash_activity("FEE", D("-0.02"), order_client_id=coid,
                                       description=desc, after=timedelta(seconds=30))  # fmt: skip
    r.run(timedelta(minutes=20))
    with r.o.db.read() as s:
        rows = s.fetch_all("SELECT attribution FROM broker_activities WHERE activity_type = "
                           "'FEE' ORDER BY ts, activity_id")  # fmt: skip
    return sorted(str(x[0]) for x in rows)


A = ActivityAttribution


def test_empty_description_fee_twins_are_booked_once(r: ReconEnv) -> None:
    assert fees(r, "", "", "REG") == ["ATTRIBUTABLE", "ATTRIBUTABLE", "UNEXPLAINED"]
    assert r.o.balances().cash == D(4400) - D("0.04")  # "" once + REG once


def test_empty_description_behaves_exactly_like_any_other_description(tmp_path: Path) -> None:
    named = fees(recon_env(tmp_path / "a"), "TAF", "TAF", "REG")
    empty = fees(recon_env(tmp_path / "b"), "", "", "REG")
    assert named == empty


def test_empty_and_missing_descriptions_stay_distinct(tmp_path: Path) -> None:
    """Unchanged rule: twins need an equal description; "" is not None (as "TAF" is not
    None). Each is a distinct fee, exactly as two different named fees would be."""
    r1 = recon_env(tmp_path / "c")
    assert fees(r1, "", None) == fees(recon_env(tmp_path / "d"), "TAF", None)


# ============================================================ contract C7
def c7(acct: FakeAlpacaAccount) -> tuple[Any, dict[str, str]]:
    ctx, _ = wire(acct)
    result = run_check(contract_checks.check_activity_pagination, ctx)
    row = classify_rows({"activity_pagination": result})["C7"]
    return result, row


def non_fill_rows(acct: FakeAlpacaAccount) -> list[dict[str, Any]]:
    rows = [row for _, row in sorted(acct.activities, key=lambda x: x[1]["id"])]
    return [r for r in rows if r["activity_type"] != "FILL"]


def test_c7_verified_with_empty_descriptions() -> None:
    acct = synthetic_account()
    for row in non_fill_rows(acct)[:5]:
        row["description"] = ""
    result, row = c7(acct)
    assert result.status is CheckStatus.PASSED
    assert row == {"outcome": "VERIFIED", "code": "OK"}


def test_c7_malformed_payload_is_failed() -> None:
    acct = synthetic_account()
    non_fill_rows(acct)[15]["description"] = 42  # on a later page of the size-10 scan
    result, row = c7(acct)
    assert result.status is CheckStatus.VIOLATION
    assert row == {"outcome": "FAILED", "code": "BROKER_PAYLOAD_CONTRACT_BROKEN"}


def test_c7_genuine_incomplete_retrieval_stays_inconclusive() -> None:
    acct = synthetic_account()
    acct.fail("/account/activities", *[ServiceUnavailableError("t", kind="timeout")] * 3)
    _, row = c7(acct)
    assert row == {"outcome": "INCONCLUSIVE", "code": "BROKER_INCOMPLETE_RETRIEVAL"}


def test_c7_too_few_activities_stays_inconclusive() -> None:
    acct = FakeAlpacaAccount(clock=Clock(START))
    acct.clock.now = START - timedelta(hours=2)
    acct.post(cash_row(""))  # one valid activity, as on the real flat account
    acct.clock.now = START
    result, row = c7(acct)
    assert result.status is CheckStatus.PASSED
    assert row == {"outcome": "INCONCLUSIVE", "code": "TOO_FEW_ACTIVITIES_TO_EXERCISE_PAGINATION"}
