"""Step 13.1: conservative attribution of fees and dividends (§12.18)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.broker.models import BrokerActivity, SubmitOrderRequest
from swing_trading_bot.domain.enums import (
    ActivityAttribution,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from swing_trading_bot.ledger.repositories import OrdersRepository
from swing_trading_bot.oms.classify import classify_cash_activity
from swing_trading_bot.oms.reconciler import Outcome, Severity
from tests.support.loss_limits import hold
from tests.support.portfolio import NOW, fill, finish, make_order, new_ledger
from tests.support.recon import ReconEnv, recon_env

D = Decimal
A = ActivityAttribution
QUIET = timedelta(minutes=20)


@pytest.fixture
def r(tmp_path: Path) -> ReconEnv:
    return recon_env(tmp_path)


def bot_fill(r: ReconEnv) -> str:
    r.o.advance(timedelta(0))
    coid = r.o.place().client_order_id
    assert coid is not None
    r.o.sim.schedule_fill(coid, D(12), D(50), after=timedelta(seconds=5))
    return coid


def attributions(r: ReconEnv, kind: str) -> list[str]:
    with r.o.db.read() as s:
        rows = s.fetch_all(
            "SELECT attribution FROM broker_activities WHERE activity_type = ? "
            "ORDER BY ts, activity_id",
            (kind,),
        )
    return [str(x[0]) for x in rows]


# ------------------------------------------------------------------- fees


def test_unrelated_manual_trading_on_the_same_day(r: ReconEnv) -> None:
    bot_fill(r)
    r.o.sim.submit_order(
        SubmitOrderRequest(
            "manual-7", "AAPL", Side.BUY, D(1), OrderType.LIMIT, TimeInForce.DAY, D("100.00")
        )
    )
    r.o.sim.schedule_fill("manual-7", D(1), D(100), after=timedelta(seconds=5))
    r.o.sim.schedule_cash_activity(
        "FEE", D("-0.01"), order_client_id="manual-7", after=timedelta(seconds=30)
    )
    r.o.sim.schedule_cash_activity("FEE", D("-0.02"), after=timedelta(seconds=31))  # no ref
    rep = r.run(QUIET)
    assert attributions(r, "FEE") == ["UNEXPLAINED", "UNEXPLAINED"]  # same day is not proof
    assert rep.outcome is Outcome.MISMATCH
    assert {"UNEXPLAINED_CASH_ACTIVITY", "UNEXPLAINED_FILL", "EXTERNAL_ORDER"} <= rep.codes(
        Severity.MISMATCH
    )
    assert r.o.balances().cash == D(4400)  # neither fee touched the virtual ledger


def test_delayed_regulatory_fee_with_order_reference(r: ReconEnv) -> None:
    coid = bot_fill(r)
    r.o.sim.schedule_cash_activity(
        "FEE",
        D("-0.02"),
        order_client_id=coid,
        description="REG",
        after=timedelta(days=2),
        activity_delay=timedelta(hours=6),
    )
    r.run(QUIET)
    r.o.advance(timedelta(days=2, hours=6, minutes=1))  # posted two days later
    rep = r.run(QUIET)
    assert attributions(r, "FEE") == ["ATTRIBUTABLE"]
    assert rep.outcome is Outcome.CLEAN, rep.findings
    assert r.o.balances().cash == D(4400) - D("0.02")


def test_duplicate_fee_representations_are_never_booked_twice(r: ReconEnv) -> None:
    coid = bot_fill(r)
    for desc in ("TAF", "TAF", "REG"):  # TAF reported twice; REG is a distinct fee
        r.o.sim.schedule_cash_activity(
            "FEE", D("-0.02"), order_client_id=coid, description=desc, after=timedelta(seconds=30)
        )
    rep = r.run(QUIET)
    assert sorted(attributions(r, "FEE")) == ["ATTRIBUTABLE", "ATTRIBUTABLE", "UNEXPLAINED"]
    assert r.o.balances().cash == D(4400) - D("0.04")  # TAF once + REG once
    assert "UNEXPLAINED_CASH_ACTIVITY" in rep.codes(Severity.MISMATCH)  # operator reviews


def _fee(order_id: str | None, amount: str = "-0.05", aid: str = "fee-1") -> BrokerActivity:
    raw = {"id": aid, "net_amount": amount, "order_id": order_id}
    return BrokerActivity(
        aid, "FEE", NOW.date(), NOW, json.dumps(raw), net_amount=D(amount), order_id=order_id
    )


def test_fee_attribution_requires_execution_evidence(tmp_path: Path) -> None:
    db, pf = new_ledger(tmp_path / "fees.sqlite3")
    unfilled = make_order(db, symbol="AAA", qty="1", limit="20.00")
    embedded = make_order(db, symbol="BBB", qty="1", limit="20.00")
    pf.process_fill(fill(embedded, "1", "20.00", fees="0.05"), now=NOW)
    finish(pf, db, embedded, OrderStatus.FILLED)
    plain = make_order(db, symbol="CCC", qty="1", limit="20.00")
    pf.process_fill(fill(plain, "1", "20.00"), now=NOW)
    finish(pf, db, plain, OrderStatus.FILLED)
    with db.transaction() as tx:
        repo = OrdersRepository()
        for order, bid in ((unfilled, "b-unfilled"), (embedded, "b-embedded"), (plain, "b-plain")):
            repo.set_broker_order_id(tx, order.client_order_id, bid, NOW)
    with db.read() as s:
        verdicts = {
            "no reference": classify_cash_activity(s, _fee(None)),
            "unknown order": classify_cash_activity(s, _fee("b-someone-else")),
            "no executions": classify_cash_activity(s, _fee("b-unfilled")),
            "fee on the fill already": classify_cash_activity(s, _fee("b-embedded")),
            "refund": classify_cash_activity(s, _fee("b-plain", amount="0.05")),
            "proven": classify_cash_activity(s, _fee("b-plain")),
        }
    assert {k: v.attribution for k, v in verdicts.items()} == {
        "no reference": A.UNEXPLAINED,
        "unknown order": A.UNEXPLAINED,
        "no executions": A.UNEXPLAINED,
        "fee on the fill already": A.UNEXPLAINED,
        "refund": A.UNEXPLAINED,
        "proven": A.ATTRIBUTABLE,
    }
    db.close()


# --------------------------------------------------------------- dividends


def _div(
    qty: str | None, per_share: str | None, amount: str, *, days_after: int, symbol: str = "AAPL"
) -> BrokerActivity:
    paid = NOW + timedelta(days=days_after)
    return BrokerActivity(
        f"div-{days_after}",
        "DIV",
        paid.date(),
        paid,
        '{"id":"d"}',
        symbol=symbol,
        net_amount=D(amount),
        qty=None if qty is None else D(qty),
        per_share_amount=None if per_share is None else D(per_share),
    )


def test_dividends_are_unresolved_without_verified_entitlement(tmp_path: Path) -> None:
    """No M1 source reports a verified ex-date with the eligible quantity, so holdings on
    the payment date — however long, however exact — never prove entitlement."""
    db, pf = new_ledger(tmp_path / "div.sqlite3")
    hold(db, pf, "AAPL", "10", "100.00", at=NOW)
    sale = make_order(db, side=Side.SELL, symbol="AAPL", qty="10", limit="101.00")
    with db.read() as s:
        cases = {
            "held 40 days, exact qty and amount": _div("10", "0.24", "2.40", days_after=40),
            "recent purchase": _div("10", "0.24", "2.40", days_after=10),
            "qty differs": _div("12", "0.20", "2.40", days_after=40),
            "no per-share": _div("10", None, "2.40", days_after=40),
            "never held": _div("10", "0.24", "2.40", days_after=40, symbol="MSFT"),
        }
        verdicts = {k: classify_cash_activity(s, v) for k, v in cases.items()}
    assert {v.attribution for v in verdicts.values()} == {A.UNEXPLAINED}
    assert all("entitlement unverified" in v.reason for v in verdicts.values())
    closed_at = NOW + timedelta(days=41)
    pf.process_fill(fill(sale, "10", "101.00", at=closed_at), now=closed_at)
    finish(pf, db, sale, OrderStatus.FILLED)
    with db.read() as s:  # paid after the position was closed: still unresolved
        after_close = classify_cash_activity(s, _div("10", "0.24", "2.40", days_after=45))
    assert after_close.attribution is A.UNEXPLAINED
    db.close()


def test_dividend_on_a_bot_position_is_preserved_blocks_and_is_not_booked(r: ReconEnv) -> None:
    bot_fill(r)
    r.o.sim.schedule_cash_activity(
        "DIV",
        D("2.40"),
        symbol="MSFT",
        qty=D(12),
        per_share_amount=D("0.20"),
        after=timedelta(seconds=30),
    )
    rep = r.run(QUIET)
    assert attributions(r, "DIV") == ["UNEXPLAINED"]
    assert "DIVIDEND_ENTITLEMENT_UNVERIFIED" in rep.codes(Severity.MISMATCH)
    assert r.o.balances().cash == D(4400)  # not booked
    assert "UNEXPLAINED_ACTIVITY" in r.o.r.gate(r.o.now()).reason_codes


def test_every_activity_is_preserved_whatever_its_attribution(r: ReconEnv) -> None:
    coid = bot_fill(r)
    r.o.sim.schedule_cash_activity(
        "FEE", D("-0.02"), order_client_id=coid, after=timedelta(seconds=30)
    )
    r.o.sim.schedule_cash_activity("INT", D("1.00"), after=timedelta(seconds=31))
    r.o.sim.schedule_cash_activity("CSD", D("50.00"), after=timedelta(seconds=32))
    r.o.sim.schedule_cash_activity(
        "DIV",
        D("2.40"),
        symbol="MSFT",
        qty=D(12),
        per_share_amount=D("0.20"),
        after=timedelta(seconds=33),
    )
    r.run(QUIET)
    with r.o.db.read() as s:
        rows = dict(
            s.fetch_all(
                "SELECT activity_type, attribution FROM broker_activities "
                "WHERE activity_type <> 'FILL'"
            )
        )
    assert rows == {
        "FEE": "ATTRIBUTABLE",
        "INT": "EXTERNAL",
        "CSD": "UNEXPLAINED",
        "DIV": "UNEXPLAINED",
    }
    assert r.o.balances().cash == D(4400) - D("0.02")  # only the proven fee is booked
