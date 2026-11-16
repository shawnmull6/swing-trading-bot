"""Step 16.12: migration 0009 (durable stop prices, stop levels, protection gaps).

Every database is a disposable file under pytest's tmp_path; ``data/ledger.sqlite3`` is
never touched. An observe ledger is never migrated implicitly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import MigrationError, MigrationRequiredError
from swing_trading_bot.ledger.migrator import (
    apply_pending,
    apply_pending_atomic,
    packaged_migrations,
    verify_applied,
)
from swing_trading_bot.ledger.repositories import PortfolioRecord, PortfoliosRepository

NOW = datetime(2026, 11, 16, 21, tzinfo=UTC)
TS = "2026-11-16T15:00:00.000000+00:00"
PLAN = "tp-019a8c3e5f2b7d4c8a1e2f3a4b5c6d7e"
UP_TO_8 = packaged_migrations()[:8]
COLS = (
    "client_order_id, portfolio_id, intent_key, symbol, side, order_type, time_in_force, "
    "purpose, qty, limit_price, planned_stop, status, filled_qty, trade_plan_id, order_role, "
    "created_at, updated_at"
)


def _row(coid: str, *, side: str = "BUY", otype: str = "LIMIT", limit: str | None = "50",
         plan: str | None = None, role: str | None = None, purpose: str = "ENTRY",
         status: str = "FILLED") -> tuple[object, ...]:  # fmt: skip
    return (coid, 1, f"k:{coid}", "MSFT", side, otype, "DAY", purpose, "12", limit, None,
            status, "0", plan, role, TS, TS)  # fmt: skip


LEGACY = [
    _row("stb-entry"),  # an M1 limit entry
    _row(
        "stb-orphan", side="SELL", otype="MARKET", limit=None, purpose="RISK_EXIT", status="ORPHAN"
    ),
    _row("stb-plan-entry", plan=PLAN, role="ENTRY"),
    _row(
        "stb-plan-target",
        side="SELL",
        plan=PLAN,
        role="TARGET",
        purpose="DISCRETIONARY_EXIT",
        status="CANCELED",
    ),
]


def v8_database(path: Path, rows: list[tuple[object, ...]]) -> sqlite3.Connection:
    """A disposable simulate ledger at schema version 8 holding ``rows`` as orders."""
    db = Database.create(path, RuntimeMode.SIMULATE, now=NOW, migrations=UP_TO_8)
    with db.transaction() as tx:
        PortfoliosRepository().insert(tx, PortfolioRecord(
            name="main", mode=RuntimeMode.SIMULATE, initial_capital=Decimal(5000),
            max_capital_allocation=Decimal(5000), account_profile="cash_conservative",
            created_at=NOW))  # fmt: skip
    db.close()
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    c.executemany(f"INSERT INTO orders ({COLS}) VALUES ({', '.join('?' * 17)})", rows)
    return c


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = v8_database(tmp_path / "disposable.sqlite3", LEGACY)
    yield c
    c.close()


def migrate(c: sqlite3.Connection) -> None:
    assert apply_pending(c, packaged_migrations(), now=NOW) == [9]


def insert_order(c: sqlite3.Connection, coid: str, *, otype: str, limit: str | None,
                 stop: str | None, side: str = "SELL", plan: str | None = PLAN,
                 role: str | None = "PROTECTIVE_STOP") -> None:  # fmt: skip
    c.execute(
        f"INSERT INTO orders ({COLS}, stop_price) VALUES ({', '.join('?' * 18)})",
        (*_row(coid, side=side, otype=otype, limit=limit, plan=plan, role=role,
               purpose="RISK_EXIT" if side == "SELL" else "ENTRY", status="ACCEPTED"), stop),
    )  # fmt: skip


def test_existing_rows_survive_unchanged(conn: sqlite3.Connection) -> None:
    before = conn.execute(f"SELECT {COLS} FROM orders ORDER BY client_order_id").fetchall()
    migrate(conn)
    after = conn.execute(f"SELECT {COLS}, stop_price FROM orders ORDER BY client_order_id")
    assert [r[:-1] for r in after] == before
    assert {r[-1] for r in conn.execute("SELECT stop_price FROM orders")} == {None}
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    verify_applied(conn, packaged_migrations())
    assert apply_pending(conn, packaged_migrations(), now=NOW) == []  # idempotent


def test_valid_stop_orders_are_accepted(conn: sqlite3.Connection) -> None:
    migrate(conn)
    insert_order(conn, "stb-stop", otype="STOP", limit=None, stop="48.00")
    insert_order(conn, "stb-stop-limit", otype="STOP_LIMIT", limit="47.50", stop="48.00")
    rows = conn.execute(
        "SELECT order_type, limit_price, stop_price FROM orders WHERE stop_price IS NOT NULL "
        "ORDER BY client_order_id"
    ).fetchall()
    assert rows == [("STOP", None, "48.00"), ("STOP_LIMIT", "47.50", "48.00")]


@pytest.mark.parametrize(
    ("otype", "limit", "stop", "side", "plan", "role"),
    [("STOP", None, None, "SELL", PLAN, "PROTECTIVE_STOP"),  # stop without a stop price
     ("STOP", "47.00", "48.00", "SELL", PLAN, "PROTECTIVE_STOP"),  # stop with a limit
     ("LIMIT", "54.00", "48.00", "SELL", PLAN, "TARGET"),  # limit with a stop price
     ("LIMIT", None, None, "SELL", PLAN, "TARGET"),  # limit without a limit price
     ("STOP_LIMIT", None, "48.00", "SELL", PLAN, "PROTECTIVE_STOP"),
     ("MARKET", None, "48.00", "SELL", None, None),
     ("STOP", None, "-48.00", "SELL", PLAN, "PROTECTIVE_STOP"),  # malformed decimal
     ("STOP", None, "48.00", "SELL", PLAN, None),  # plan without a role
     ("STOP", None, "48.00", "SELL", None, "PROTECTIVE_STOP"),  # role without a plan
     ("STOP", None, "48.00", "BUY", PLAN, "PROTECTIVE_STOP"),  # a sell role that buys
     ("LIMIT", "50.00", None, "SELL", PLAN, "ENTRY")],  # an entry that sells
)  # fmt: skip
def test_invalid_orders_are_rejected(  # noqa: PLR0917 - parametrized columns
    conn: sqlite3.Connection, otype: str, limit: str | None, stop: str | None, side: str,
    plan: str | None, role: str | None,
) -> None:  # fmt: skip
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_order(conn, "stb-bad", otype=otype, limit=limit, stop=stop, side=side, plan=plan,
                     role=role)  # fmt: skip


@pytest.mark.parametrize(
    ("column", "value"),
    [("stop_price", "47.00"), ("trade_plan_id", None), ("order_role", "EXIT"),
     ("limit_price", "1"), ("order_type", "LIMIT")],
)  # fmt: skip
def test_order_identity_columns_are_immutable(
    conn: sqlite3.Connection, column: str, value: str | None
) -> None:
    migrate(conn)
    insert_order(conn, "stb-stop", otype="STOP", limit=None, stop="48.00")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"UPDATE orders SET {column} = ? WHERE client_order_id = 'stb-stop'", (value,))
    conn.execute("UPDATE orders SET status = 'CANCELED' WHERE client_order_id = 'stb-stop'")


def level(c: sqlite3.Connection, seq: int, stop: str, high: str | None,
          source: str = "TRAILING") -> None:  # fmt: skip
    c.execute(
        "INSERT INTO plan_stop_levels (trade_plan_id, seq, stop_price, high_water, source, "
        "evidence_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (PLAN, seq, stop, high, source, '{"source": "test"}', TS),
    )


def test_stop_levels_only_ratchet_up_in_sequence(conn: sqlite3.Connection) -> None:
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError, match="ratchet"):
        level(conn, 2, "48.00", None, "PLAN_INTENT")  # must start at 1
    level(conn, 1, "48.00", None, "PLAN_INTENT")
    level(conn, 2, "50.00", "52.00")
    for seq, stop, high in [(3, "49.99", "53.00"), (3, "51.00", "51.99"), (3, "51.00", None),
                            (4, "51.00", "53.00")]:  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError, match="ratchet"):
            level(conn, seq, stop, high, "BREAKEVEN")
    level(conn, 3, "50.00", "52.00", "BREAKEVEN")  # equal is allowed
    with pytest.raises(sqlite3.IntegrityError):
        level(conn, 4, "51.00", None)  # TRAILING needs a high
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE plan_stop_levels SET stop_price = '60.00'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM plan_stop_levels")


def test_protection_gaps_open_once_and_close_once(conn: sqlite3.Connection) -> None:
    migrate(conn)
    insert_order(conn, "stb-stop", otype="STOP", limit=None, stop="48.00")
    insert_order(conn, "stb-stop-2", otype="STOP", limit=None, stop="49.00")

    def open_gap() -> None:
        conn.execute("INSERT INTO protection_gaps (trade_plan_id, opened_at, reason, "
                     "prior_client_order_id) VALUES (?, ?, 'STOP_CANCEL_REQUESTED', 'stb-stop')",
                     (PLAN, TS))  # fmt: skip

    open_gap()
    with pytest.raises(sqlite3.IntegrityError):
        open_gap()  # one open gap per plan
    with pytest.raises(sqlite3.IntegrityError):  # acknowledged needs the closing order
        conn.execute("UPDATE protection_gaps SET closed_at = ?, close_reason = "
                     "'STOP_ACKNOWLEDGED'", (TS,))  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):  # cannot close before it opened
        conn.execute("UPDATE protection_gaps SET closed_at = '2026-11-15T15:00:00.000000+00:00',"
                     " close_reason = 'POSITION_FLAT'")  # fmt: skip
    conn.execute("UPDATE protection_gaps SET closed_at = ?, close_reason = 'STOP_ACKNOWLEDGED', "
                 "closed_by_client_order_id = 'stb-stop-2'", (TS,))  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError, match="set once"):
        conn.execute("UPDATE protection_gaps SET close_reason = 'POSITION_FLAT'")
    open_gap()  # a new interval may open after the previous one closed
    with pytest.raises(sqlite3.IntegrityError, match="set once"):
        conn.execute("UPDATE protection_gaps SET reason = 'STOP_ENDED_UNFILLED' "
                     "WHERE closed_at IS NULL")  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM protection_gaps")


def test_invalid_legacy_row_aborts_the_whole_migration(tmp_path: Path) -> None:
    """A v8 LIMIT order without a limit price (never written by the application, but
    allowed by the v8 schema) violates the new CHECK: nothing changes, version stays 8."""
    c = v8_database(tmp_path / "bad.sqlite3", [_row("stb-bad", limit=None)])
    with pytest.raises(MigrationError, match="rolled back"):
        apply_pending(c, packaged_migrations(), now=NOW)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 8
    cols = [r[1] for r in c.execute("PRAGMA table_info(orders)")]
    assert "stop_price" not in cols
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'plan_stop_levels'"
                     ).fetchone()[0] == 0  # fmt: skip
    c.close()


@pytest.mark.parametrize("atomic", [False, True], ids=["apply_pending", "migrate_workflow"])
@pytest.mark.parametrize("index", [1, 2, 5, 9])
def test_failure_mid_migration_rolls_everything_back(
    conn: sqlite3.Connection, atomic: bool, index: int
) -> None:
    before = conn.execute(f"SELECT {COLS} FROM orders ORDER BY client_order_id").fetchall()
    triggers = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'orders_immutable'"
    ).fetchone()

    def explode(version: int, at: int) -> None:
        if version == 9 and at == index:
            raise RuntimeError("simulated crash")

    run = apply_pending_atomic if atomic else apply_pending
    with pytest.raises(MigrationError):
        run(conn, packaged_migrations(), now=NOW, before_statement=explode)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 8
    assert "stop_price" not in [r[1] for r in conn.execute("PRAGMA table_info(orders)")]
    assert conn.execute(f"SELECT {COLS} FROM orders ORDER BY client_order_id").fetchall() == before
    assert (
        conn.execute("SELECT sql FROM sqlite_master WHERE name = 'orders_immutable'").fetchone()
        == triggers
    )  # the original trigger is back
    migrate(conn)  # and a later attempt still succeeds


def test_observe_ledger_at_version_8_is_refused_not_migrated(tmp_path: Path) -> None:
    """The real (observe) ledger is at version 8: with 0009 packaged, opening it refuses
    until the explicit, backed-up ``migrate`` workflow runs. It is never migrated here."""
    path = tmp_path / "observe.sqlite3"
    Database.create(path, RuntimeMode.OBSERVE, now=NOW, migrations=UP_TO_8).close()
    with pytest.raises(MigrationRequiredError):
        Database.open(path, RuntimeMode.OBSERVE, now=NOW)
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 8
    c.close()


def test_version_name_and_checksum_are_tracked(conn: sqlite3.Connection) -> None:
    migrate(conn)
    (m9,) = [m for m in packaged_migrations() if m.version == 9]
    row = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations WHERE version = 9"
    ).fetchone()
    assert row == (9, "protective_stops", m9.checksum)


def test_ratchet_compares_numbers_exactly_not_text(conn: sqlite3.Connection) -> None:
    migrate(conn)
    level(conn, 1, "9.50", None, "PLAN_INTENT")
    level(conn, 2, "10", "10.0001", "BREAKEVEN")  # text order would call "10" < "9.50"
    level(conn, 3, "10.0000", "10.0001", "BREAKEVEN")  # numerically equal: allowed
    with pytest.raises(sqlite3.IntegrityError, match="ratchet"):
        level(conn, 4, "9.9999", "10.0001", "BREAKEVEN")
    with pytest.raises(sqlite3.IntegrityError, match="ratchet"):
        level(conn, 4, "10.00", "10.00009", "BREAKEVEN")  # high 10.00009 < 10.0001
    level(conn, 4, "100", "100", "TRAILING")


# ------------------------------------------------- ownership and evidence (16.12A)

OTHER = "tp-019a8c3e5f2b7d4c8a1e2f3a4b5c6d7f"


def gap_row(
    c: sqlite3.Connection, plan: str, prior: str | None, *,
    reason: str = "STOP_CANCEL_REQUESTED", confirmed: str | None = None,
) -> None:  # fmt: skip
    c.execute("INSERT INTO protection_gaps (trade_plan_id, opened_at, reason, "
              "prior_client_order_id, confirmed_at) VALUES (?, ?, ?, ?, ?)",
              (plan, TS, reason, prior, confirmed))  # fmt: skip


@pytest.fixture
def owned(conn: sqlite3.Connection) -> sqlite3.Connection:
    migrate(conn)
    insert_order(conn, "stb-stop", otype="STOP", limit=None, stop="48.00")
    insert_order(conn, "stb-other-stop", otype="STOP", limit=None, stop="40.00", plan=OTHER)
    return conn


@pytest.mark.parametrize(
    ("plan", "prior"),
    [(OTHER, "stb-other-stop"),  # a plan without an ENTRY order
     (PLAN, "stb-other-stop"),  # another plan's stop
     (PLAN, "stb-plan-entry"),  # not a protective stop
     (PLAN, "stb-missing"),  # unknown order
     (PLAN, None)],  # no prior order
)  # fmt: skip
def test_gap_ownership_is_enforced(owned: sqlite3.Connection, plan: str, prior: str | None) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        gap_row(owned, plan, prior)


def test_gap_closer_must_be_a_stop_of_the_same_plan(owned: sqlite3.Connection) -> None:
    gap_row(owned, PLAN, "stb-stop")
    for closer in ("stb-other-stop", "stb-plan-entry"):
        with pytest.raises(sqlite3.IntegrityError):
            owned.execute("UPDATE protection_gaps SET closed_at = ?, close_reason = "
                          "'STOP_ACKNOWLEDGED', closed_by_client_order_id = ?",
                          (TS, closer))  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError):  # flat closes name no order
        owned.execute("UPDATE protection_gaps SET closed_at = ?, close_reason = 'POSITION_FLAT', "
                      "closed_by_client_order_id = 'stb-stop'", (TS,))  # fmt: skip


def test_gap_confirmation_rules(owned: sqlite3.Connection) -> None:
    early = "2026-11-16T14:00:00.000000+00:00"
    later = "2026-11-16T16:00:00.000000+00:00"
    with pytest.raises(sqlite3.IntegrityError):  # ended unfilled opens confirmed
        gap_row(owned, PLAN, "stb-stop", reason="STOP_ENDED_UNFILLED")
    with pytest.raises(sqlite3.IntegrityError):
        gap_row(owned, PLAN, "stb-stop", confirmed=early)  # before it opened
    gap_row(owned, PLAN, "stb-stop")
    with pytest.raises(sqlite3.IntegrityError):
        owned.execute("UPDATE protection_gaps SET confirmed_at = ?", (early,))
    owned.execute("UPDATE protection_gaps SET confirmed_at = ?", (later,))
    with pytest.raises(sqlite3.IntegrityError, match="set once"):
        owned.execute("UPDATE protection_gaps SET confirmed_at = ?", (TS,))
    with pytest.raises(sqlite3.IntegrityError):  # cannot close before the confirmation
        owned.execute("UPDATE protection_gaps SET closed_at = ?, close_reason = 'POSITION_FLAT'",
                      (TS,))  # fmt: skip
    owned.execute("UPDATE protection_gaps SET closed_at = ?, close_reason = 'POSITION_FLAT'",
                  (later,))  # fmt: skip
    with pytest.raises(sqlite3.IntegrityError, match="set once"):
        owned.execute("UPDATE protection_gaps SET confirmed_at = NULL")
    with pytest.raises(sqlite3.IntegrityError):  # cannot insert a closed gap
        owned.execute("INSERT INTO protection_gaps (trade_plan_id, opened_at, reason, "
                      "prior_client_order_id, closed_at, close_reason) VALUES "
                      "(?, ?, 'STOP_CANCEL_REQUESTED', 'stb-stop', ?, 'POSITION_FLAT')",
                      (PLAN, TS, later))  # fmt: skip
    gap_row(owned, PLAN, "stb-stop", reason="STOP_ENDED_UNFILLED", confirmed=TS)


@pytest.mark.parametrize(
    ("plan", "evidence"),
    [(OTHER, '{"source": "x"}'),  # unknown plan (no ENTRY)
     (PLAN, "{}"), (PLAN, "[1]"), (PLAN, '"text"'), (PLAN, "not json"),
     (PLAN, '{"client_order_id": "stb-other-stop"}'),  # another plan's order
     (PLAN, '{"client_order_id": "stb-missing"}')],
)  # fmt: skip
def test_stop_level_ownership_and_evidence(
    owned: sqlite3.Connection, plan: str, evidence: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        owned.execute(
            "INSERT INTO plan_stop_levels (trade_plan_id, seq, stop_price, high_water, source, "
            "evidence_json, recorded_at) VALUES (?, 1, '48.00', NULL, 'PLAN_INTENT', ?, ?)",
            (plan, evidence, TS),
        )
    owned.execute(
        "INSERT INTO plan_stop_levels (trade_plan_id, seq, stop_price, high_water, source, "
        "evidence_json, recorded_at) VALUES (?, 1, '48.00', NULL, 'PLAN_INTENT', ?, ?)",
        (PLAN, '{"client_order_id": "stb-stop"}', TS),
    )
