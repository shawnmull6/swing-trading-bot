"""Step 11 verification items (requested with the step 11 approval).

1. Fees enter the risk budget at the actual candidate quantity, rounded up, and the
   boundary where one more share breaks the 0.5% budget.
2. The broker cash backstop only ever lowers a size, never uses margin buying power,
   never stands in for virtual settled cash, never bypasses the $5,000 ceiling, and is
   conservative when broker evidence is missing, stale or negative.
3. The ledger fingerprint covers every table and every mutable fact that sizing,
   commitments, loss limits, unresolved orders and reconciliation readiness read.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.broker.models import AccountSnapshot
from swing_trading_bot.domain.enums import HaltReason, OrderStatus, ReconciliationResult, Side
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.ledger.repositories import (
    CashEntriesRepository,
    CashEntryKind,
    CashEntryRecord,
    OrdersRepository,
    ReconciliationRunsRepository,
    ReservationsRepository,
    ResolutionAttempt,
    ResolutionAttemptsRepository,
    ResolutionMethod,
    SyncStateRepository,
    TradingStateRepository,
)
from swing_trading_bot.ledger.state_fingerprint import (
    EXCLUDED_TABLES,
    fingerprinted_tables,
    ledger_fingerprint,
)
from swing_trading_bot.risk.pretrade import (
    Constraint,
    RejectionCode,
    SizingInputs,
    broker_cash_backstop,
    size,
)
from tests.support.loss_limits import hold
from tests.support.portfolio import NOW, finish, make_order, reserve
from tests.support.portfolio import fill as pf_fill
from tests.support.pretrade import FEES, LIMITS, RiskEnv, codes, proposal, risk_env

D = Decimal
ZERO_FEES = FeeModel(D(0), D(0), D(0), D(0), D(0))


def inputs(**kw: object) -> SizingInputs:
    base = SizingInputs(entry=D("50.00"), stop=D("48.00"), equity=D(5000), baseline_equity=D(5000),
                        held_market_value=D(0), pending_symbol_commitment=D(0),
                        capital_headroom=D(5000), available_cash=D(5000),
                        portfolio_stressed=D(0), broker_cash=D(100000))  # fmt: skip
    return replace(base, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------- 1. fees in the risk budget


def test_fees_are_charged_at_the_candidate_quantity() -> None:
    per_share = FeeModel(D(0), D("0.09"), D(0), D(0), D(0))
    # 12 shares: 24.00 + 1.08 = 25.08 > 25 ; 11 shares: 22.00 + 0.99 = 22.99
    assert size(inputs(), LIMITS, ZERO_FEES).cap(Constraint.RISK_BUDGET).max_qty == 12
    assert size(inputs(), LIMITS, per_share).cap(Constraint.RISK_BUDGET).max_qty == 11


def test_fee_rounding_up_decides_the_boundary() -> None:
    # Budget 0.5% x 5000.10 = 25.0005. At 12 shares the raw fee 1.0004 would fit
    # (25.0004) but the estimate rounds UP to 1.01 (25.01): only 11 shares.
    fee = FeeModel(D("1.0004"), D(0), D(0), D(0), D(0))
    assert fee.buy_fee(D(12)) == D("1.01")
    cap = size(inputs(equity=D("5000.10")), LIMITS, fee).cap(Constraint.RISK_BUDGET)
    assert cap.max_qty == 11


def test_exact_budget_is_allowed_and_one_more_share_is_not() -> None:
    fee = FeeModel(D(0), D("0.0833"), D(0), D(0), D(0))  # 12 x 0.0833 = 0.9996 -> 1.00
    assert size(inputs(), LIMITS, fee).cap(Constraint.RISK_BUDGET).max_qty == 12  # 25.00 == 25
    # 13 shares: 26.00 + 1.09 > 25
    assert D(13) * D(2) + fee.buy_fee(D(13)) > D(25)


def test_shipped_exit_fee_estimate_at_quantity(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    d = r.evaluate(proposal("MSFT", "50.00", "48.00"))
    expected = D(12) * D(2) + FEES.buy_fee(D(12)) + FEES.sell_fee(D(12), D(48))
    assert d.planned_risk == expected == D("24.03")


# --------------------------------------------------------- 2. broker cash backstop


def test_backstop_ignores_margin_and_uses_the_smaller_cash_figure(tmp_path: Path) -> None:
    fields = {f.name for f in dataclasses.fields(AccountSnapshot)}
    assert "buying_power" not in fields  # margin buying power is not even modelled
    r = risk_env(tmp_path)
    acct = r.account()
    assert acct.multiplier == D(4)  # a margin-enabled simulated account ...
    small_cash = replace(acct, cash=D(150), non_marginable_buying_power=D(100000))
    assert broker_cash_backstop(small_cash) == D(150)
    d = r.evaluate(proposal(), account=small_cash)
    assert d.approved_qty == D(3)  # ... still limited by min(cash, non-marginable BP)
    assert d.sizing is not None and d.sizing.binding == (Constraint.BROKER_CASH_BACKSTOP,)


def test_backstop_never_replaces_virtual_settled_cash(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    # Buy and sell $4,400 today: the proceeds are unsettled, so virtual settled cash is
    # $600 while the broker reports ten million dollars of cash.
    hold(r.db, r.env.pf, "ZZZ", "44", "100.00", at=NOW)
    sale = make_order(r.db, side=Side.SELL, symbol="ZZZ", qty="44", limit="100.00")
    r.env.pf.process_fill(pf_fill(sale, "44", "100.00", at=NOW), now=NOW)
    finish(r.env.pf, r.db, sale, OrderStatus.FILLED)
    rich = replace(r.account(), cash=D(10**7), non_marginable_buying_power=D(10**7))
    d = r.evaluate(proposal(qty="12"), account=rich)
    assert d.approved and d.reservation == D(600) and d.evidence.available_cash == D(600)
    over = r.evaluate(proposal(qty="13"), account=rich)
    assert RejectionCode.QUANTITY_EXCEEDS_LIMIT in codes(over)
    assert over.sizing is not None and over.sizing.cap(Constraint.AVAILABLE_CASH).max_qty == 12
    assert over.sizing.cap(Constraint.BROKER_CASH_BACKSTOP).max_qty > 1000


def test_backstop_with_huge_broker_cash_cannot_lift_the_ceiling() -> None:
    capped = size(inputs(capital_headroom=D(300), broker_cash=D(10**9)), LIMITS, ZERO_FEES)
    assert capped.max_qty == 6 and capped.binding == (Constraint.CAPITAL_CEILING,)


def test_missing_stale_or_negative_broker_cash_is_conservative(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    missing = r.evaluate(proposal(), no_account=True)
    assert RejectionCode.ACCOUNT_EVIDENCE_MISSING in codes(missing)
    assert missing.sizing is None and not missing.approved  # nothing is sized without it
    stale = r.evaluate(proposal(), account=replace(r.account(), as_of=NOW - timedelta(minutes=5)))
    assert RejectionCode.ACCOUNT_EVIDENCE_STALE in codes(stale) and not stale.approved
    negative = replace(r.account(), cash=D(-50), non_marginable_buying_power=D(0))
    assert broker_cash_backstop(negative) == 0
    d = r.evaluate(proposal(), account=negative)
    assert RejectionCode.QUANTITY_ZERO in codes(d) and d.approved_qty == 0


money = st.decimals(D(0), D(200000), places=2, allow_nan=False, allow_infinity=False)


@given(cash=money, nmbp=money, virtual=money, headroom=money)
def test_backstop_only_ever_reduces(cash: Decimal, nmbp: Decimal, virtual: Decimal,
                                    headroom: Decimal) -> None:  # fmt: skip
    backstop = max(D(0), min(cash, nmbp))
    with_backstop = size(inputs(available_cash=virtual, capital_headroom=headroom,
                                broker_cash=backstop), LIMITS, FEES)  # fmt: skip
    without = size(inputs(available_cash=virtual, capital_headroom=headroom,
                          broker_cash=D(10**12)), LIMITS, FEES)  # fmt: skip
    assert with_backstop.max_qty <= without.max_qty
    if with_backstop.max_qty < without.max_qty:
        assert Constraint.BROKER_CASH_BACKSTOP in with_backstop.binding
    for name in (Constraint.AVAILABLE_CASH, Constraint.CAPITAL_CEILING):
        assert with_backstop.cap(name) == without.cap(name)  # never substituted


# --------------------------------------------------------- 3. fingerprint coverage


def test_every_table_is_fingerprinted_or_explicitly_excluded(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    with r.db.read() as s:
        rows = s.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        all_tables = {str(row[0]) for row in rows}
        covered = set(fingerprinted_tables(s))
    assert covered | set(EXCLUDED_TABLES) == all_tables
    assert covered.isdisjoint(EXCLUDED_TABLES)
    assert {"orders", "cash_reservations", "cash_entries", "fills", "positions", "price_marks",
            "equity_snapshots", "loss_limit_events", "session_closes", "trading_state",
            "halt_events", "reconciliation_runs", "order_resolution_attempts", "sync_state",
            "broker_baselines", "market_calendar", "settlement_days"} <= covered  # fmt: skip


def _changes(r: RiskEnv, mutate: object) -> bool:
    before = r.engine.fingerprint()
    with r.db.transaction() as tx:
        mutate(tx)  # type: ignore[operator]
    return r.engine.fingerprint() != before


def test_every_decision_relevant_mutation_changes_the_fingerprint(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    order = make_order(r.db, symbol="ZZZ", qty="2", limit="20.00")
    reserve(r.env.pf, r.db, order)
    entries = CashEntriesRepository()
    with r.db.transaction() as tx:
        entries.append(tx, CashEntryRecord(1, NOW, CashEntryKind.DEPOSIT, D("1.00"),
                                           date(2026, 11, 9), None, memo="unsettled"))  # fmt: skip
    with r.db.read() as s:
        pending = list(entries.list_unresolved_settlements(s, 1))
    assert pending and pending[-1].id is not None
    entry_id = pending[-1].id
    coid = order.client_order_id
    mutations = {
        # settlement resolved later: a mutable column the old count/max hash missed
        "settlement date": lambda tx: entries.resolve_settlement(tx, entry_id, date(2026, 11, 10)),
        "reservation remaining": lambda tx: ReservationsRepository().set_remaining(
            tx, coid, D("1.00"), NOW),
        "order status": lambda tx: OrdersRepository().transition_status(
            tx, coid, expected=OrderStatus.PENDING_SUBMIT,
            target=OrderStatus.SUBMIT_AMBIGUOUS, at=NOW),
        "resolution attempt": lambda tx: ResolutionAttemptsRepository().append(
            tx, ResolutionAttempt(coid, NOW, ResolutionMethod.BY_CLIENT_ID, "NOT_FOUND")),
        "order event": lambda tx: tx.insert(
            "INSERT INTO order_events (client_order_id, ts, source) VALUES (?, ?, 'LOCAL')",
            (coid, "2026-11-09T15:00:00.000000+00:00")),
        "sync state": lambda tx: SyncStateRepository().set(tx, "x.test", "1", NOW),
        "mark": lambda tx: r.env.pf.record_mark(tx, __import__(
            "tests.support.portfolio", fromlist=["mark"]).mark("MSFT", "50.02", as_of=NOW)),
        "reconciliation": lambda tx: ReconciliationRunsRepository().append(
            tx, ReconciliationResult.INCONCLUSIVE, None, NOW),
        "halt": lambda tx: TradingStateRepository().halt(
            tx, HaltReason.OPERATOR, detail="t", actor="t", at=NOW),
    }  # fmt: skip
    for name, mutate in mutations.items():
        assert _changes(r, mutate), name


def test_fingerprint_is_stable_for_unchanged_state(tmp_path: Path) -> None:
    r = risk_env(tmp_path)
    assert r.engine.fingerprint() == r.engine.fingerprint()
    with r.db.read() as s:
        direct = ledger_fingerprint(s)
    assert direct == r.engine.fingerprint()
