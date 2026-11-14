"""Step 16.9: migration 0008 (CONSOLIDATED_BAR reference closes, close-conflict audit).

Every database here is a disposable file under pytest's tmp_path; nothing touches
``data/ledger.sqlite3``. An account-backed (observe) ledger is never migrated implicitly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import (
    MigrationChecksumError,
    MigrationError,
    MigrationRequiredError,
)
from swing_trading_bot.ledger.migrator import (
    apply_pending,
    apply_pending_atomic,
    packaged_migrations,
    verify_applied,
)

NOW = datetime(2026, 11, 14, 3, tzinfo=UTC)
TS = "2026-11-13T21:00:00.000000+00:00"
RX = "2026-11-14T01:30:00.000000+00:00"
COLS = "symbol, session_date, close_price, source, as_of, received_at, feed"
LEGACY = [
    ("AAPL", "2026-11-13", "356.27", "OFFICIAL", TS, RX, None),
    ("AAPL", "2026-11-13", "356.30", "DAILY_BAR", TS, RX, "iex"),
    ("MSFT", "2026-11-13", "523.88", "DAILY_BAR", TS, RX, "sip"),
    ("SIMA", "2026-11-13", "50", "SIM", TS, RX, "sim"),
]


def _noop(_conn: sqlite3.Connection) -> None:
    return None


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A disposable database at schema version 7 holding pre-0008 session closes."""
    path = tmp_path / "disposable.sqlite3"
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    assert apply_pending(c, packaged_migrations()[:7], now=NOW, bootstrap=_noop) == list(
        range(1, 8)
    )
    c.executemany(f"INSERT INTO session_closes ({COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)", LEGACY)
    yield c
    c.close()


def rows(c: sqlite3.Connection) -> list[tuple[object, ...]]:
    return c.execute(
        f"SELECT {COLS}, adjustment FROM session_closes ORDER BY symbol, source"
    ).fetchall()


def migrate(c: sqlite3.Connection) -> None:
    assert apply_pending(c, packaged_migrations(), now=NOW) == [8]


def insert_close(c: sqlite3.Connection, source: str, feed: str | None, adj: str | None) -> None:
    c.execute(
        f"INSERT INTO session_closes ({COLS}, adjustment) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("NVDA", "2026-11-12", "190.5", source, TS, RX, feed, adj),
    )


def test_existing_rows_survive_unchanged(conn: sqlite3.Connection) -> None:
    before = {r[:7] for r in conn.execute(f"SELECT {COLS} FROM session_closes")}
    migrate(conn)
    after = rows(conn)
    assert {r[:7] for r in after} == before  # OFFICIAL rows untouched, nothing relabelled
    assert all(r[7] is None for r in after)  # no adjustment on pre-existing sources


def test_migration_is_recorded_and_repeatable(conn: sqlite3.Connection) -> None:
    migrate(conn)
    assert apply_pending(conn, packaged_migrations(), now=NOW) == []  # idempotent
    verify_applied(conn, packaged_migrations())  # checksums and user_version agree
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8


def test_valid_consolidated_close_is_accepted(conn: sqlite3.Connection) -> None:
    migrate(conn)
    insert_close(conn, "CONSOLIDATED_BAR", "sip", "raw")
    stored = conn.execute(
        "SELECT source, feed, adjustment FROM session_closes WHERE symbol = 'NVDA'"
    ).fetchall()
    assert stored == [("CONSOLIDATED_BAR", "sip", "raw")]


@pytest.mark.parametrize(
    ("source", "feed", "adj"),
    [
        ("CONSOLIDATED_BAR", "iex", "raw"),  # not consolidated
        ("CONSOLIDATED_BAR", None, "raw"),
        ("CONSOLIDATED_BAR", "delayed_sip", "raw"),
        ("CONSOLIDATED_BAR", "sip", None),  # adjustment must be explicit
        ("CONSOLIDATED_BAR", "sip", "split"),  # split-adjusted never accepted
        ("OFFICIAL", None, "raw"),  # adjustment only for CONSOLIDATED_BAR
        ("DAILY_BAR", "sip", "raw"),
        ("DAILY_BAR", None, None),  # existing rule kept: DAILY_BAR names its feed
        ("REFERENCE", "sip", "raw"),  # unknown source
    ],
)
def test_invalid_source_feed_adjustment_rejected(
    conn: sqlite3.Connection, source: str, feed: str | None, adj: str | None
) -> None:
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        insert_close(conn, source, feed, adj)


def test_append_only_triggers_remain(conn: sqlite3.Connection) -> None:
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE session_closes SET close_price = '1' WHERE symbol = 'AAPL'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM session_closes")
    conn.execute(
        "INSERT INTO session_close_conflicts (symbol, session_date, source, recorded_close, "
        "observed_close, observed_as_of, observed_received_at, detected_at) "
        "VALUES ('AAPL', '2026-11-13', 'CONSOLIDATED_BAR', '356.27', '356.31', ?, ?, ?)",
        (TS, RX, RX),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE session_close_conflicts SET observed_close = '1'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM session_close_conflicts")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):  # one row per observation
        conn.execute(
            "INSERT INTO session_close_conflicts (symbol, session_date, source, recorded_close, "
            "observed_close, observed_as_of, observed_received_at, detected_at) "
            "VALUES ('AAPL', '2026-11-13', 'CONSOLIDATED_BAR', '356.27', '356.31', ?, ?, ?)",
            (TS, RX, RX),
        )


def test_invalid_legacy_row_aborts_the_whole_migration(tmp_path: Path) -> None:
    """A row violating the new constraints (impossible today, but fail closed) leaves the
    database at version 7 with its original table."""
    c = sqlite3.connect(tmp_path / "bad.sqlite3", isolation_level=None)
    apply_pending(c, packaged_migrations()[:7], now=NOW, bootstrap=_noop)
    c.execute("PRAGMA ignore_check_constraints = ON")
    c.execute(f"INSERT INTO session_closes ({COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ("X", "2026-11-13", "1", "DAILY_BAR", TS, RX, None))  # fmt: skip
    c.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(MigrationError, match="rolled back"):
        apply_pending(c, packaged_migrations(), now=NOW)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 7
    assert c.execute("SELECT count(*) FROM session_closes").fetchone()[0] == 1
    c.close()


def test_observe_ledger_is_never_migrated_implicitly(tmp_path: Path) -> None:
    """The real (observe) ledger can reach version 8 only through the explicit, backed-up
    ``migrate`` workflow; opening it refuses instead of migrating."""
    path = tmp_path / "observe.sqlite3"
    Database.create(
        path, RuntimeMode.OBSERVE, now=NOW, migrations=packaged_migrations()[:7]
    ).close()
    with pytest.raises(MigrationRequiredError):
        Database.open(path, RuntimeMode.OBSERVE, now=NOW)
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 7
    c.close()


def test_version_name_and_checksum_are_tracked(conn: sqlite3.Connection) -> None:
    migrate(conn)
    (m8,) = [m for m in packaged_migrations() if m.version == 8]
    row = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations WHERE version = 8"
    ).fetchone()
    assert row == (8, "consolidated_reference_closes", m8.checksum)
    tampered = [m if m.version != 8 else replace(m, sql=m.sql + "\n-- edited\n")
                for m in packaged_migrations()]  # fmt: skip
    with pytest.raises(MigrationChecksumError):
        verify_applied(conn, tampered)  # an edited 0008 is refused, never re-applied


@pytest.mark.parametrize("atomic", [False, True], ids=["apply_pending", "migrate_workflow"])
def test_failure_mid_migration_rolls_everything_back(
    conn: sqlite3.Connection,
    atomic: bool,
) -> None:
    """Every 0008 statement runs in ONE transaction with its schema_migrations row, both
    for simulation auto-migration and for the explicit (observe) migrate workflow."""
    before = rows_v7(conn)

    def explode(version: int, index: int) -> None:
        if version == 8 and index == 3:  # after the rebuild copied rows and dropped the old
            raise RuntimeError("simulated crash")

    run = apply_pending_atomic if atomic else apply_pending
    with pytest.raises(MigrationError):
        run(conn, packaged_migrations(), now=NOW, before_statement=explode)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 7
    assert [r[:7] for r in rows_v7(conn)] == [r[:7] for r in before]
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'session_close_conflicts'"
        ).fetchone()[0]
        == 0
    )
    migrate(conn)  # and a later attempt still succeeds


def rows_v7(c: sqlite3.Connection) -> list[tuple[object, ...]]:
    return c.execute(f"SELECT {COLS}, NULL FROM session_closes ORDER BY symbol, source").fetchall()
