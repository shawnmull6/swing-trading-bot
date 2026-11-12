"""M1 step 11: deterministic pre-trade risk checks on simulation ledgers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import (
    InstrumentType,
    OrderStatus,
    OrderType,
    ReconciliationResult,
    Side,
)
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.ledger.repositories import OrdersRepository, ReconciliationRunsRepository
from swing_trading_bot.risk.pretrade import Constraint, RejectionCode
from tests.support.loss_limits import et, hold
from tests.support.portfolio import NOW, fill, finish, make_order, reserve
from tests.support.pretrade import ENGINE_CONFIG, FEES, RiskEnv, codes, proposal, risk_env

D = Decimal
C = RejectionCode


@pytest.fixture
def renv(tmp_path: Path) -> RiskEnv:
    return risk_env(tmp_path)


def buy_and_hold(r: RiskEnv, symbol: str, qty: str, price: str) -> None:
    hold(r.db, r.env.pf, symbol, qty, price, at=NOW)
    r.set_marks({symbol: price})


def working_buy(r: RiskEnv, symbol: str, qty: str, limit: str) -> None:
    """A reserved buy the broker has accepted (not ambiguous, so gates stay open)."""
    order = make_order(r.db, symbol=symbol, qty=qty, limit=limit)
    reserve(r.env.pf, r.db, order)
    with r.db.transaction() as tx:
        OrdersRepository().transition_status(tx, order.client_order_id,
                                             expected=OrderStatus.PENDING_SUBMIT,
                                             target=OrderStatus.ACCEPTED, at=NOW)  # fmt: skip


# ------------------------------------------------------------ sizing on $5,000


def test_initial_portfolio_sizes_at_half_percent_risk(renv: RiskEnv) -> None:
    d = renv.evaluate(proposal("MSFT", "50.00", "48.00"))
    assert d.approved and d.rejections == ()
    # budget 0.5% x $5,000 = $25; $2.00/share + est. exit fees: 12 shares -> $24.03
    assert d.approved_qty == D(12)
    assert d.planned_risk == D("24.03") and d.planned_risk <= D(25)
    assert d.reservation == D(600)
    assert d.sizing is not None and d.sizing.binding == (Constraint.RISK_BUDGET,)
    assert d.sizing.cap(Constraint.RISK_BUDGET).limit == D(25)
    ev = d.evidence
    assert (ev.equity, ev.available_cash, ev.capital_headroom) == (D(5000), D(5000), D(5000))
    assert ev.mark_price == D(50) and ev.mark_feed == "iex" and ev.account_id == "sim-account-0001"
    assert ev.session_baseline_id is not None and ev.week_baseline_id is not None
    assert ev.ledger_fingerprint == renv.engine.fingerprint()


def test_whole_shares_always_round_down(renv: RiskEnv) -> None:
    d = renv.evaluate(proposal("MSFT", "50.00", "47.97"))  # $2.03/share: 25/2.03 = 12.3
    assert d.approved_qty == D(12)
    assert D(13) * D("2.03") > D(25)  # one more share would breach the budget


@pytest.mark.parametrize(
    ("entry", "stop", "code"),
    [
        ("50.00", "50.00", C.STOP_NOT_BELOW_ENTRY),  # zero distance
        ("50.00", "50.01", C.STOP_NOT_BELOW_ENTRY),  # negative distance
        ("50.00", "0", C.INVALID_STOP_PRICE),
        ("50.00", "-1", C.INVALID_STOP_PRICE),
        ("50.00", "48.001", C.INVALID_STOP_PRICE),  # sub-penny above $1
        ("50.005", "48.00", C.INVALID_ENTRY_PRICE),
        ("9.99", "9.00", C.PRICE_BELOW_MINIMUM),
    ],
)
def test_invalid_stop_and_entry_prices(renv: RiskEnv, entry: str, stop: str, code: str) -> None:
    d = renv.evaluate(proposal("MSFT", entry, stop))
    assert not d.approved and d.approved_qty == 0
    assert code in codes(d)


def test_one_share_exceeding_the_risk_budget(renv: RiskEnv) -> None:
    renv.set_marks({"COST": "400.00"})
    d = renv.evaluate(proposal("COST", "400.00", "370.00"))  # $30/share > $25 budget
    assert codes(d) == {C.ONE_SHARE_EXCEEDS_RISK_BUDGET}
    assert d.sizing is not None and d.sizing.cap(Constraint.RISK_BUDGET).max_qty == 0


def test_requested_quantity_is_never_reduced_silently(renv: RiskEnv) -> None:
    ok = renv.evaluate(proposal(qty="5"))
    assert ok.approved and ok.approved_qty == D(5)
    over = renv.evaluate(proposal(qty="13"))
    assert codes(over) == {C.QUANTITY_EXCEEDS_LIMIT} and over.approved_qty == 0
    for bad in ("0", "-2", "1.5"):
        assert C.INVALID_REQUESTED_QUANTITY in codes(renv.evaluate(proposal(qty=bad)))


def test_unsupported_proposal_shapes(renv: RiskEnv) -> None:
    cases = {
        C.NOT_LONG_ENTRY: proposal(side=Side.SELL),
        C.UNSUPPORTED_ORDER_TYPE: proposal(order_type=OrderType.MARKET),
        C.EXTENDED_HOURS_NOT_ALLOWED: proposal(extended_hours=True),
        C.INVALID_SYMBOL: proposal(symbol="msft$"),
        C.INVALID_INTENT_KEY: proposal(intent_key=""),
    }
    for code, p in cases.items():
        assert code in codes(renv.evaluate(p)), code
    equity_only = replace(ENGINE_CONFIG, allowed_instruments=frozenset({InstrumentType.EQUITY}))
    renv.engine._config = equity_only
    assert C.UNSUPPORTED_INSTRUMENT in codes(
        renv.evaluate(proposal(instrument_type=InstrumentType.ETF))
    )


# ------------------------------------------------------- concentration & count


def test_exposure_exactly_at_and_above_twenty_percent(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "5", "100.00")  # $500 held; limit 20% x $5,000 = $1,000
    at_limit = renv.evaluate(proposal("AAPL", "100.00", "99.00", qty="5"))
    assert at_limit.approved, at_limit.rejections  # 500 + 500 == 1,000: allowed
    over = renv.evaluate(proposal("AAPL", "100.00", "99.00", qty="6"))
    assert C.QUANTITY_EXCEEDS_LIMIT in codes(over)
    assert over.sizing is not None
    assert Constraint.POSITION_CONCENTRATION in over.sizing.binding
    assert over.sizing.cap(Constraint.POSITION_CONCENTRATION).used == D(500)


def test_position_already_at_or_over_concentration(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "10", "100.00")
    assert C.POSITION_AT_CONCENTRATION_LIMIT in codes(
        renv.evaluate(proposal("AAPL", "100.00", "99.00"))
    )
    renv.set_marks({"AAPL": "110.00"})  # market value 1,100 > 20% of 5,100
    d = renv.evaluate(proposal("AAPL", "110.00", "109.00"))
    assert C.POSITION_AT_CONCENTRATION_LIMIT in codes(d) and d.approved_qty == 0


def test_four_positions_counting_pending_entries(renv: RiskEnv) -> None:
    for sym in ("AAA", "BBB", "CCC"):
        buy_and_hold(renv, sym, "5", "20.00")
    renv.set_marks({"DDD": "20.00", "EEE": "20.00"})
    fourth = renv.evaluate(proposal("DDD", "20.00", "19.00"))
    assert fourth.approved, fourth.rejections
    working_buy(renv, "DDD", "5", "20.00")
    fifth = renv.evaluate(proposal("EEE", "20.00", "19.00"))
    assert C.MAX_POSITIONS_REACHED in codes(fifth)
    adding = renv.evaluate(proposal("AAA", "20.00", "19.00"))  # existing symbol: no new slot
    assert C.MAX_POSITIONS_REACHED not in codes(adding)
    assert adding.approved, adding.rejections


def test_existing_position_plus_additional_shares(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "3", "100.00")
    d = renv.evaluate(proposal("AAPL", "100.00", "98.00"))
    assert d.approved
    assert d.sizing is not None
    # concentration: (1,000 - 300) / 100 = 7; gap 15% x (300 + 100q) <= 150 -> 7
    assert d.approved_qty == D(7)
    assert set(d.sizing.binding) == {Constraint.POSITION_CONCENTRATION,
                                     Constraint.GAP_STRESS_POSITION}  # fmt: skip


def test_pending_commitments_and_duplicate_working_order(renv: RiskEnv) -> None:
    working_buy(renv, "MSFT", "6", "50.00")  # $300 committed, accepted by the broker
    d = renv.evaluate(proposal("MSFT", "50.00", "49.00"))
    assert C.DUPLICATE_WORKING_ORDER in codes(d) and not d.approved
    assert d.sizing is not None
    conc = d.sizing.cap(Constraint.POSITION_CONCENTRATION)
    assert conc.used == D(300) and conc.max_qty == 14  # (1,000 - 300) / 50
    assert d.evidence.available_cash == D(4700) and d.evidence.capital_headroom == D(4700)


def test_duplicate_intent(renv: RiskEnv) -> None:
    order = make_order(renv.db, symbol="AAPL", status=OrderStatus.PENDING_SUBMIT)
    finish(renv.env.pf, renv.db, order, OrderStatus.CANCELED)
    d = renv.evaluate(proposal("MSFT", intent=order.intent_key))
    assert codes(d) == {C.DUPLICATE_INTENT}


# ------------------------------------------------------- $1,000, cash, ceiling


def test_thousand_dollar_order_boundary(renv: RiskEnv) -> None:
    renv.set_marks({"XYZ": "100.00"})
    exact = renv.evaluate(proposal("XYZ", "100.00", "99.50", qty="10"))
    assert exact.approved and exact.reservation == D(1000)
    assert renv.evaluate(proposal("XYZ", "100.00", "99.50", qty="11")).approved_qty == 0
    fee = FeeModel(D(1), D(0), FEES.sell_taf_per_share, FEES.sell_taf_max_per_order,
                   FEES.sell_sec_per_notional)  # fmt: skip
    renv.engine._config = replace(ENGINE_CONFIG, fees=fee)
    with_fee = renv.evaluate(proposal("XYZ", "100.00", "99.50"))
    assert with_fee.approved_qty == D(9)  # $1,000 + $1 fee would exceed the cap
    assert with_fee.sizing is not None
    assert with_fee.sizing.binding == (Constraint.ORDER_COMMITMENT,)
    assert with_fee.reservation == D(901)


def round_trip(r: RiskEnv, symbol: str, qty: str, buy: str, sell: str) -> None:
    """Buy then sell the same session: sale proceeds stay unsettled until T+1."""
    hold(r.db, r.env.pf, symbol, qty, buy, at=NOW)
    sale = make_order(r.db, side=Side.SELL, symbol=symbol, qty=qty, limit=sell)
    r.env.pf.process_fill(fill(sale, qty, sell, at=NOW), now=NOW)
    finish(r.env.pf, r.db, sale, OrderStatus.FILLED)


def test_unsettled_proceeds_and_insufficient_settled_cash(renv: RiskEnv) -> None:
    round_trip(renv, "AAA", "45", "100.00", "100.00")  # $4,500 now unsettled
    d = renv.evaluate(proposal("MSFT", "50.00", "48.00"))
    assert d.evidence.available_cash == D(500)
    assert d.approved_qty == D(10)  # 500 / 50, below the 12-share risk size
    assert d.sizing is not None and d.sizing.binding == (Constraint.AVAILABLE_CASH,)
    round_trip(renv, "BBB", "10", "49.00", "49.00")  # settled cash now $10
    starved = renv.evaluate(proposal("MSFT", "50.00", "48.00"))
    assert codes(starved) >= {C.QUANTITY_ZERO} and starved.approved_qty == 0


def test_capital_ceiling_ignores_profits(renv: RiskEnv) -> None:
    round_trip(renv, "AAA", "10", "100.00", "150.00")  # +$500 realized (unsettled)
    d = renv.evaluate(proposal("MSFT", "50.00", "48.00"))
    assert d.evidence.equity == D(5500)
    assert d.evidence.capital_headroom == D(5000)  # the ceiling does not grow with profit
    assert d.sizing is not None
    assert d.sizing.cap(Constraint.CAPITAL_CEILING).limit == D(5000)


# ------------------------------------------------------------- loss limits


def test_daily_lockout_blocks_new_risk(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "10", "100.00")
    later = NOW + timedelta(minutes=5)
    renv.env.refresh(later)
    renv.set_marks({"AAPL": "85.00", "MSFT": "50.00"}, at=later)  # -$150 = -3% > 2%
    renv.env.monitor.evaluate(now=later)
    d = renv.evaluate(proposal(), at=later)
    assert {C.DAILY_LOSS_LOCKOUT, C.GATE_BLOCKED} <= codes(d)


def test_weekly_breach_halts_new_risk(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "20", "100.00")
    later = NOW + timedelta(minutes=5)
    renv.env.refresh(later)
    renv.set_marks({"AAPL": "85.00", "MSFT": "50.00"}, at=later)  # -$300 = -6% > 5%
    renv.env.monitor.evaluate(now=later)
    d = renv.evaluate(proposal(), at=later)
    assert {C.WEEKLY_LIMIT_ACTIVE, C.PERSISTENT_HALT} <= codes(d)


def test_missing_official_close_leaves_no_baseline(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "2", "100.00")  # held overnight with no recorded close
    tue = et(date(2026, 11, 10), 10)
    renv.env.refresh(tue)
    renv.set_marks({"AAPL": "100.00", "MSFT": "50.00"}, at=tue)
    renv.env.monitor.establish_baselines(now=tue)  # cannot: no official close for AAPL
    d = renv.evaluate(proposal(), at=tue)
    assert C.BASELINE_UNAVAILABLE in codes(d) and not d.approved


# ------------------------------------------------------------- gap stress


def test_gap_stress_portfolio_boundary(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAA", "20", "100.00")  # stressed $300
    buy_and_hold(renv, "BBB", "10", "100.00")  # stressed $150 -> $450 of $500
    d = renv.evaluate(proposal("MSFT", "50.00", "49.00"))
    assert d.approved_qty == D(6)  # 6 x $7.50 = $45 fits; 7 x $7.50 = $52.50 does not
    assert d.sizing is not None and d.sizing.binding == (Constraint.GAP_STRESS_PORTFOLIO,)
    assert renv.evaluate(proposal("MSFT", "50.00", "49.00", qty="7")).approved_qty == 0


def test_gap_stress_position_boundary(renv: RiskEnv) -> None:
    renv.set_marks({"XYZ": "25.00"})
    d = renv.evaluate(proposal("XYZ", "25.00", "24.50"))
    # 15% x 25 x q <= 3% x 5,000 = 150 -> q <= 40; also 20% concentration -> 40
    assert d.approved_qty == D(40)
    assert d.sizing is not None
    assert Constraint.GAP_STRESS_POSITION in d.sizing.binding


# ------------------------------------------------------ stale / missing data


def test_stale_and_missing_marks(renv: RiskEnv) -> None:
    assert C.MARK_MISSING_OR_STALE in codes(renv.evaluate(proposal("NOPE", "50.00", "48.00")))
    renv.set_marks({"MSFT": "50.00"}, at=NOW - timedelta(minutes=20))
    assert C.MARK_MISSING_OR_STALE in codes(renv.evaluate(proposal()))
    renv.set_marks({"LOW": "9.50"})
    assert C.MARK_BELOW_MINIMUM in codes(renv.evaluate(proposal("LOW", "10.00", "9.00")))


def test_stale_holding_mark_makes_portfolio_ineligible(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "2", "100.00")
    renv.set_marks({"AAPL": "100.00"}, at=NOW - timedelta(minutes=30))
    d = renv.evaluate(proposal())
    assert C.PORTFOLIO_NOT_ENTRY_ELIGIBLE in codes(d) and not d.approved


def test_gate_evidence_is_required_fresh_and_permissive(renv: RiskEnv) -> None:
    assert C.GATE_MISSING in codes(renv.evaluate(proposal(), no_gate=True))
    old = renv.gate(NOW)
    stale = renv.evaluate(proposal(), gate=old, at=NOW + timedelta(seconds=31))
    assert C.GATE_STALE in codes(stale)
    future = renv.evaluate(proposal(), gate=replace(old, evaluated_at=NOW + timedelta(seconds=5)))
    assert C.GATE_STALE in codes(future)
    with renv.db.transaction() as tx:
        ReconciliationRunsRepository().append(tx, ReconciliationResult.MISMATCH, None, NOW)
    blocked = renv.evaluate(proposal())
    assert {C.GATE_BLOCKED, C.RECONCILIATION_NOT_CLEAN} <= codes(blocked)


def test_reconciliation_must_be_recent(renv: RiskEnv) -> None:
    later = NOW + timedelta(minutes=11)  # last clean run is now stale
    renv.set_marks({"MSFT": "50.00"}, at=later)
    d = renv.evaluate(proposal(), at=later, gate=replace(renv.gate(NOW), evaluated_at=later))
    assert C.RECONCILIATION_NOT_CLEAN in codes(d)


def test_account_evidence(renv: RiskEnv) -> None:
    acct = renv.account()
    assert C.ACCOUNT_EVIDENCE_MISSING in codes(renv.evaluate(proposal(), no_account=True))
    stale = replace(acct, as_of=NOW - timedelta(seconds=121))
    assert C.ACCOUNT_EVIDENCE_STALE in codes(renv.evaluate(proposal(), account=stale))
    other = replace(acct, account_id="someone-else")
    assert C.ACCOUNT_MISMATCH in codes(renv.evaluate(proposal(), account=other))
    paper = replace(acct, environment="paper")
    assert C.ACCOUNT_MISMATCH in codes(renv.evaluate(proposal(), account=paper))
    blocked = replace(acct, trading_blocked=True)
    assert C.ACCOUNT_RESTRICTED in codes(renv.evaluate(proposal(), account=blocked))
    thin = replace(acct, non_marginable_buying_power=D(250))
    d = renv.evaluate(proposal(), account=thin)
    assert d.approved_qty == D(5)  # the broker's own cash is a backstop, never a source
    assert d.sizing is not None and d.sizing.binding == (Constraint.BROKER_CASH_BACKSTOP,)


def test_ledger_inconsistency_rejects(renv: RiskEnv) -> None:
    buy_and_hold(renv, "AAPL", "2", "100.00")
    with renv.db.transaction() as tx:
        tx.execute("UPDATE positions SET cost_basis = '1' WHERE symbol = 'AAPL'")
    assert C.LEDGER_INCONSISTENT in codes(renv.evaluate(proposal()))


# ----------------------------------------------- decisions are not reusable


def test_decision_expires_and_is_bound_to_ledger_state(renv: RiskEnv) -> None:
    d = renv.evaluate(proposal())
    assert d.approved
    fp = renv.engine.fingerprint()
    assert d.still_valid(now=NOW, current_fingerprint=fp)
    assert not d.still_valid(now=d.valid_until + timedelta(seconds=1), current_fingerprint=fp)
    assert not d.still_valid(now=NOW - timedelta(seconds=1), current_fingerprint=fp)
    reserve(renv.env.pf, renv.db, make_order(renv.db, symbol="ZZZ", qty="1", limit="20.00"))
    changed = renv.engine.fingerprint()
    assert changed != fp and not d.still_valid(now=NOW, current_fingerprint=changed)
    rejected = renv.evaluate(proposal(qty="99"))
    assert not rejected.still_valid(now=NOW, current_fingerprint=changed)


def test_evaluation_is_read_only_and_deterministic(renv: RiskEnv) -> None:
    before = renv.engine.fingerprint()
    gate, acct = renv.gate(), renv.account()
    a = renv.engine.evaluate(proposal(), gate=gate, account=acct, now=NOW)
    b = renv.engine.evaluate(proposal(), gate=gate, account=acct, now=NOW)
    preview = renv.engine.preview(proposal(), gate=gate, account=acct, now=NOW)
    assert renv.engine.fingerprint() == before
    assert a == b and a.decision_id == b.decision_id
    assert preview == a.sizing
