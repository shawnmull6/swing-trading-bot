"""Step 4: schema-level guarantees (foreign keys, TEXT decimals, enums, append-only)."""

from __future__ import annotations

import pytest

from swing_trading_bot.domain.enums import OrderStatus
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import ConstraintViolationError

from .conftest import OrderFactory

TS = "2026-11-09T15:30:00.000000+00:00"


def _write(db: Database, sql: str, params: tuple[object, ...] = ()) -> None:
    with db.transaction() as tx:
        tx.execute(sql, params)


def test_foreign_keys_enforced(db: Database) -> None:
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"):
        _write(
            db,
            "INSERT INTO order_events (client_order_id, ts, source) VALUES (?, ?, 'LOCAL')",
            ("stb-does-not-exist", TS),
        )
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"):
        _write(
            db,
            "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on) "
            "VALUES (999, ?, 'DEPOSIT', '1', '2026-11-09', '2026-11-09')",
            (TS,),
        )


@pytest.mark.parametrize(
    "amount",
    ["1e5", "1E5", "NaN", "Infinity", "", " 1", "1.", ".5", "-.5", "1-2", "1.2.3", "--1",
     "+1", "1,000", "$5"],
)  # fmt: skip
def test_decimal_columns_reject_non_canonical_values(
    db: Database, portfolio_id: int, amount: object
) -> None:
    with pytest.raises(ConstraintViolationError, match="CHECK"):
        _write(
            db,
            "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on) "
            "VALUES (?, ?, 'DEPOSIT', ?, '2026-11-09', '2026-11-09')",
            (portfolio_id, TS, amount),
        )


def test_strict_text_columns_coerce_numbers_so_codec_is_the_float_barrier(
    db: Database, portfolio_id: int
) -> None:
    """STRICT TEXT columns convert a bound REAL/INTEGER to text *before* CHECKs run, so
    SQLite cannot tell 1.5 from '1.5'. Floats are therefore stopped in Python: the codec
    accepts only Decimal (see test_codec.test_only_decimal_can_be_written)."""
    _write(
        db,
        "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on) "
        "VALUES (?, ?, 'DEPOSIT', ?, '2026-11-09', '2026-11-09')",
        (portfolio_id, TS, 1.5),
    )
    with db.read() as s:
        assert s.fetch_one("SELECT amount, typeof(amount) FROM cash_entries") == ("1.5", "text")


@pytest.mark.parametrize("amount", ["0", "5000", "-12.3456", "0.0001", "123456789012.5"])
def test_decimal_columns_accept_canonical_text(
    db: Database, portfolio_id: int, amount: str
) -> None:
    _write(
        db,
        "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on) "
        "VALUES (?, ?, 'DEPOSIT', ?, '2026-11-09', '2026-11-09')",
        (portfolio_id, TS, amount),
    )
    with db.read() as s:
        assert s.fetch_one(
            "SELECT amount, typeof(amount) FROM cash_entries ORDER BY id DESC LIMIT 1"
        ) == (amount, "text")


@pytest.mark.parametrize("ts", ["2026-11-09 15:30:00", "2026-11-09T15:30:00+00:00", "now"])
def test_timestamp_format_enforced(db: Database, ts: str) -> None:
    with pytest.raises(ConstraintViolationError, match="CHECK"):
        _write(db, "INSERT INTO sync_state VALUES ('k', 'v', ?)", (ts,))


def test_no_defaults_for_safety_critical_columns(db: Database, make_order: OrderFactory) -> None:
    order = make_order()
    # Omitting filled_qty / status / qty must fail rather than silently default.
    with pytest.raises(ConstraintViolationError, match="NOT NULL"):
        _write(
            db,
            "INSERT INTO cash_reservations (client_order_id, original, status, updated_at) "
            "VALUES (?, '10', 'ACTIVE', ?)",
            (order.client_order_id, TS),
        )
    with pytest.raises(ConstraintViolationError, match="NOT NULL"):
        _write(
            db, "INSERT INTO trading_state (id, changed_at, changed_by) VALUES (1, ?, 'x')", (TS,)
        )


def test_enum_checks(db: Database, make_order: OrderFactory) -> None:
    order = make_order()
    with pytest.raises(ConstraintViolationError, match="CHECK"):
        _write(
            db,
            "UPDATE orders SET status = 'DONE' WHERE client_order_id = ?",
            (order.client_order_id,),
        )
    for status in OrderStatus:
        _write(
            db,
            "UPDATE orders SET status = ? WHERE client_order_id = ?",
            (status.value, order.client_order_id),
        )


@pytest.mark.parametrize(
    "table",
    ["fills", "order_events", "halt_events", "reconciliation_runs", "ledger_adjustments",
     "order_resolution_attempts", "equity_snapshots", "loss_limit_events", "portfolios",
     "database_identity"],
)  # fmt: skip
def test_append_only_tables_have_guard_triggers(db: Database, table: str) -> None:
    """Row triggers only fire on existing rows, so presence is asserted for every table;
    behaviour is exercised on populated tables in the tests below."""
    with db.read() as s:
        triggers = {
            str(r[0])
            for r in s.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
            )
        }
    assert {f"{table}_no_update", f"{table}_no_delete"} <= triggers


@pytest.mark.parametrize(
    "table",
    ["orders", "cash_reservations", "broker_activities", "cash_entries", "positions",
     "broker_baselines", "rebaseline_requests", "trading_state", "sync_state"],
)  # fmt: skip
def test_audited_tables_cannot_be_deleted_from(db: Database, table: str) -> None:
    with db.read() as s:
        names = {
            str(r[0])
            for r in s.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
            )
        }
    assert f"{table}_no_delete" in names


def test_halt_history_cannot_be_rewritten(db: Database) -> None:
    with pytest.raises(ConstraintViolationError, match="append-only"):
        _write(db, "UPDATE halt_events SET action = 'RESUME'")
    with pytest.raises(ConstraintViolationError, match="cannot be deleted"):
        _write(db, "DELETE FROM halt_events")
    with pytest.raises(ConstraintViolationError, match="cannot be deleted"):
        _write(db, "DELETE FROM trading_state")


def test_order_immutable_fields_and_broker_id_set_once(
    db: Database, make_order: OrderFactory
) -> None:
    order = make_order()
    coid = order.client_order_id
    for column, value in [("qty", "999"), ("side", "SELL"), ("symbol", "MSFT"),
                          ("limit_price", "1.00"), ("intent_key", "x")]:  # fmt: skip
        with pytest.raises(ConstraintViolationError, match="immutable"):
            _write(db, f"UPDATE orders SET {column} = ? WHERE client_order_id = ?", (value, coid))
    _write(db, "UPDATE orders SET broker_order_id = 'b-1' WHERE client_order_id = ?", (coid,))
    _write(db, "UPDATE orders SET broker_order_id = 'b-1' WHERE client_order_id = ?", (coid,))
    with pytest.raises(ConstraintViolationError, match="cannot change once set"):
        _write(db, "UPDATE orders SET broker_order_id = 'b-2' WHERE client_order_id = ?", (coid,))
    with pytest.raises(ConstraintViolationError, match="cannot be deleted"):
        _write(db, "DELETE FROM orders")


def test_unique_intent_key_and_client_order_id(db: Database, make_order: OrderFactory) -> None:
    order = make_order()
    with pytest.raises(ConstraintViolationError, match="UNIQUE"):
        make_order(intent_key=order.intent_key)
    with pytest.raises(ConstraintViolationError, match="UNIQUE"):
        make_order(client_order_id=order.client_order_id, intent_key="other")


def test_reservations_are_final_once_closed(db: Database, make_order: OrderFactory) -> None:
    order = make_order()
    _write(
        db,
        "INSERT INTO cash_reservations VALUES (?, '100', '100', 'ACTIVE', ?)",
        (order.client_order_id, TS),
    )
    with pytest.raises(ConstraintViolationError, match="immutable"):
        _write(db, "UPDATE cash_reservations SET original = '1'")
    _write(db, "UPDATE cash_reservations SET status = 'RELEASED'")
    with pytest.raises(ConstraintViolationError, match="final"):
        _write(db, "UPDATE cash_reservations SET status = 'ACTIVE'")
    with pytest.raises(ConstraintViolationError, match="final"):
        _write(db, "UPDATE cash_reservations SET remaining = '0'")


def test_settlement_resolves_once(db: Database, portfolio_id: int) -> None:
    _write(
        db,
        "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on) "
        "VALUES (?, ?, 'DEPOSIT', '5000', '2026-11-09', NULL)",
        (portfolio_id, TS),
    )
    _write(db, "UPDATE cash_entries SET settles_on = '2026-11-10'")
    with pytest.raises(ConstraintViolationError, match="only be resolved once"):
        _write(db, "UPDATE cash_entries SET settles_on = '2026-11-11'")
    with pytest.raises(ConstraintViolationError, match="immutable"):
        _write(db, "UPDATE cash_entries SET amount = '1'")


def test_buy_sell_cash_entries_need_a_fill(db: Database, portfolio_id: int) -> None:
    with pytest.raises(ConstraintViolationError, match="CHECK"):
        _write(
            db,
            "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on) "
            "VALUES (?, ?, 'BUY', '-100', '2026-11-09', '2026-11-09')",
            (portfolio_id, TS),
        )


def test_portfolio_mode_must_match_database(db: Database) -> None:
    with pytest.raises(ConstraintViolationError, match="does not match database mode"):
        _write(
            db,
            "INSERT INTO portfolios (name, mode, initial_capital, max_capital_allocation, "
            "account_profile, created_at) VALUES ('main', 'observe', '5000', '5000', 'p', ?)",
            (TS,),
        )


def test_only_one_active_broker_baseline(db: Database, portfolio_id: int) -> None:
    sql = (
        "INSERT INTO broker_baselines (portfolio_id, taken_at, cash, equity, activity_cursor_id, "
        "account_config_json, reason, rebaseline_request_id, superseded_at) "
        "VALUES (?, ?, '100000', '100000', NULL, '{}', 'INIT', NULL, NULL)"
    )
    _write(db, sql, (portfolio_id, TS))
    with pytest.raises(ConstraintViolationError, match="UNIQUE"):
        _write(db, sql, (portfolio_id, TS))
    with pytest.raises(ConstraintViolationError, match="immutable"):
        _write(db, "UPDATE broker_baselines SET cash = '1'")
    _write(db, "UPDATE broker_baselines SET superseded_at = ?", (TS,))
    with pytest.raises(ConstraintViolationError, match="already superseded"):
        _write(db, "UPDATE broker_baselines SET superseded_at = ?", (TS,))


def test_json_columns_must_be_valid(db: Database) -> None:
    with pytest.raises(ConstraintViolationError, match="CHECK"):
        _write(
            db,
            "INSERT INTO reconciliation_runs (ts, result, diff_json) VALUES (?, 'OK', '{')",
            (TS,),
        )


def test_persistent_halt_requires_reason(db: Database) -> None:
    with pytest.raises(ConstraintViolationError, match="CHECK"):
        _write(db, "UPDATE trading_state SET reason_code = NULL")


def test_constraint_errors_never_contain_values(db: Database, portfolio_id: int) -> None:
    secret_like = "PKFAKESECRETVALUE-123456789"
    with pytest.raises(ConstraintViolationError) as info:
        _write(
            db,
            "INSERT INTO cash_entries (portfolio_id, ts, kind, amount, trade_date, settles_on, "
            "memo) VALUES (?, ?, 'DEPOSIT', ?, '2026-11-09', '2026-11-09', ?)",
            (portfolio_id, TS, secret_like, secret_like),
        )
    assert secret_like not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
