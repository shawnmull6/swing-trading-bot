"""Step 15B.13A: activities already reflected in the INIT baseline are attributed by
identity, never by timestamp.

``init-ledger`` reads the complete (single-page, tail-confirmed) 7-day activity window
before it records the broker cash. Those activities are inside ``baseline.cash``. The
first reconciliation's periodic re-scan reaches back 7 days and ingests them *after*
the baseline: before this step they were counted again as cash movements
(``CASH_UNEXPLAINED``) and, being unclassified, flagged ``UNEXPLAINED_CASH_ACTIVITY``.

Now an activity is *baseline-reflected* only if its id — and, when recorded, its exact
payload — was observed by ``init-ledger``. Anything else, including a late-posted
activity with an earlier timestamp, is post-baseline and fails closed as before.
Offline only: the real read adapter over a fake HTTP account, temporary directories.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app import init_ledger
from swing_trading_bot.ledger.baselines import reflected_activities
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.ledger.repositories import SyncStateRepository, TradingStateRepository
from swing_trading_bot.ledger.sync_keys import (
    ACTIVITIES_COMPLETE_THROUGH,
    BASELINE_REFLECTED_ACTIVITIES,
)
from tests.support.alpaca_fake import fill_activity
from tests.support.cli_env import CliEnv, cli_env


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def historical_jnlc(e: CliEnv, amount: str = "100.00", *, days: int = 2) -> str:
    """A journal entry posted ``days`` before initialization (already in broker cash)."""
    aid = e.account.deposit(amount, kind="JNLC")
    e.account.clock.advance(days=days)
    return aid


def appear(e: CliEnv, at: datetime, amount: str, *, kind: str = "JNLC", tag: str) -> str:
    """An activity that appears at the broker NOW, carrying the earlier time ``at``:
    it was not there when ``init-ledger`` read the window. Its cash is new."""
    aid = f"{at:%Y%m%d%H%M%S%f}::{tag}"
    row = {"id": aid, "activity_type": kind, "date": at.date().isoformat(),
           "net_amount": amount, "description": kind, "status": "executed"}  # fmt: skip
    e.account.activities.append((at, row))
    e.account.cash += Decimal(amount)
    return aid


def late_posted(e: CliEnv, amount: str, *, kind: str = "JNLC", hours_back: int = 30) -> str:
    """Posted NOW (after the baseline) but timestamped before it: new broker cash."""
    return appear(e, e.account.clock.now - timedelta(hours=hours_back), amount, kind=kind,
                  tag="late")  # fmt: skip


def reconcile(e: CliEnv) -> tuple[int, dict[str, Any]]:
    e.account.clock.advance(minutes=20)  # let the first window close
    code, env = e.run("reconcile")
    return code, env["result"]["reconciliation"]


def codes(recon: dict[str, Any]) -> list[str]:
    return sorted(f["code"] for f in recon["findings"])


def state(e: CliEnv) -> tuple[bool, str | None]:
    with e.open() as db, db.read() as s:
        st = TradingStateRepository().get(s)
    return st.halted, st.reason_code


def stored(e: CliEnv) -> dict[str, tuple[str, str]]:
    with e.open() as db, db.read() as s:
        rows = s.fetch_all("SELECT activity_id, attribution, ingested_at FROM broker_activities")
    return {str(r[0]): (str(r[1]), str(r[2])) for r in rows}


def cash_entries(e: CliEnv) -> int:
    return int(e.scalar("SELECT count(*) FROM cash_entries"))


def assert_clean_and_still_initialized(code: int, recon: dict[str, Any], e: CliEnv) -> None:
    assert recon["outcome"] == "CLEAN" and recon["findings"] == [], recon
    assert recon["halted"] is None and code == 0
    assert state(e) == (True, "INITIALIZED")  # reconciliation never clears the halt
    assert cash_entries(e) == 1  # only the virtual $5,000 deposit: nothing booked


# ============================================================ 1, 2, 5, 10: the live case
def test_prebaseline_jnlc_found_by_first_reconcile_is_clean(e: CliEnv) -> None:
    aid = historical_jnlc(e)
    e.init()
    with e.open() as db, db.read() as s:
        reflected = json.loads(SyncStateRepository().get(s, BASELINE_REFLECTED_ACTIVITIES) or "")
        taken_at = s.scalar("SELECT taken_at FROM broker_baselines")
    assert list(reflected) == [aid]  # recorded with its exact payload
    code, recon = reconcile(e)
    assert_clean_and_still_initialized(code, recon, e)
    attribution, ingested_at = stored(e)[aid]
    assert attribution == "EXTERNAL"  # explained by the baseline, never booked
    assert ingested_at > str(taken_at)  # ingested after the baseline, effective before it


def test_prebaseline_activity_found_by_periodic_rescan_only(e: CliEnv) -> None:
    """The sync window starts at the portfolio's creation; only the 7-day PERIODIC
    re-scan of reconciliation reaches the historical activity."""
    aid = historical_jnlc(e, days=3)
    e.init()
    e.account.clock.advance(minutes=20)
    code, env = e.run("sync")
    assert code == 0 and aid not in stored(e)  # sync alone never sees it
    code, env = e.run("reconcile")
    recon = env["result"]["reconciliation"]
    assert_clean_and_still_initialized(code, recon, e)
    assert aid in stored(e)


def test_prebaseline_unattributable_fill_is_not_booked(e: CliEnv) -> None:
    """A historical fill (the account was flat at init again) is reflected too: it is
    preserved as EXTERNAL, never booked into the virtual ledger, never halts."""
    t = (e.account.clock.now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    e.account.post(fill_activity("x", order_id="not-a-bot-order", t=t))
    e.account.clock.advance(days=1)
    e.init()
    code, recon = reconcile(e)
    assert_clean_and_still_initialized(code, recon, e)
    assert int(e.scalar("SELECT count(*) FROM fills")) == 0
    assert {a for a, _ in stored(e).values()} == {"EXTERNAL"}


# ============================================================ 3: genuine post-baseline
def test_post_baseline_unexplained_cash_activity_still_halts(e: CliEnv) -> None:
    historical_jnlc(e)
    e.init()
    e.account.clock.advance(minutes=5)
    e.account.deposit("250.00", kind="JNLC")  # posted after the baseline
    code, recon = reconcile(e)
    assert code == 4 and recon["outcome"] == "MISMATCH" and recon["halted"] == "RECON_CASH"
    assert codes(recon) == ["UNEXPLAINED_CASH_ACTIVITY"]  # the historical one is not flagged


def test_post_baseline_cash_without_activity_still_halts(e: CliEnv) -> None:
    historical_jnlc(e)
    e.init()
    e.account.cash += 7  # broker cash moved with no activity at all
    code, recon = reconcile(e)
    assert code == 4 and recon["halted"] == "RECON_CASH"
    assert codes(recon) == ["CASH_UNEXPLAINED"]


# ============================================================ 4: late-posted / changed history
def test_late_posted_backdated_activity_fails_closed(e: CliEnv) -> None:
    """Timestamped before the baseline but posted after it: NOT in the init observation,
    so it is new broker cash — counted once, and unclassified, so it halts."""
    historical_jnlc(e)
    e.init()
    aid = late_posted(e, "-12.34")
    code, recon = reconcile(e)
    assert code == 4 and recon["halted"] == "RECON_CASH"
    assert codes(recon) == ["UNEXPLAINED_CASH_ACTIVITY"]
    assert stored(e)[aid][0] == "UNEXPLAINED"


def test_late_posted_external_credit_is_counted_once(e: CliEnv) -> None:
    """A backdated, explained (INT) credit posted after the baseline is a real movement:
    counted exactly once, so the cash identity balances."""
    e.init()
    late_posted(e, "3.21", kind="INT")
    code, recon = reconcile(e)
    assert_clean_and_still_initialized(code, recon, e)


def test_historical_activity_redelivered_with_changed_content_fails_closed(e: CliEnv) -> None:
    """Same id, different payload than init observed: its effect is not established, so
    it is not baseline-reflected — it counts as a movement and is flagged."""
    aid = historical_jnlc(e)
    e.init()
    row = next(r for _p, r in e.account.activities if r["id"] == aid)
    row["net_amount"] = "90.00"  # a silent correction of the same activity
    code, recon = reconcile(e)
    assert code == 4 and recon["outcome"] == "MISMATCH"
    assert "UNEXPLAINED_CASH_ACTIVITY" in codes(recon) and "CASH_UNEXPLAINED" in codes(recon)
    assert stored(e)[aid][0] == "UNEXPLAINED"


# ============================================================ 6: overlapping windows
def test_duplicate_retrieval_across_windows_stays_clean(e: CliEnv) -> None:
    aid = historical_jnlc(e)
    e.init()
    code, recon = reconcile(e)
    assert_clean_and_still_initialized(code, recon, e)
    e.account.clock.advance(days=1)  # the daily PERIODIC re-scan reads it again
    code, recon = reconcile(e)
    assert_clean_and_still_initialized(code, recon, e)
    assert list(stored(e)) == [aid]


def test_historical_replay_with_changed_content_after_storage_conflicts(e: CliEnv) -> None:
    aid = historical_jnlc(e)
    e.init()
    code, _recon = reconcile(e)
    assert code == 0
    row = next(r for _p, r in e.account.activities if r["id"] == aid)
    row["description"] = "rewritten"
    e.account.clock.advance(days=1)  # the next daily PERIODIC re-scan re-reads it
    code, _recon = reconcile(e)
    assert code == 4 and state(e) == (True, "RECON_EXECUTION")  # existing CONFLICT path


# ============================================================ 7: coverage marker
def test_initial_coverage_marker_unset(e: CliEnv) -> None:
    historical_jnlc(e)
    e.init()
    with e.open() as db, db.read() as s:
        assert SyncStateRepository().get(s, ACTIVITIES_COMPLETE_THROUGH) is None


# ============================================================ 8: rebaseline safeguards
def test_rebaseline_lists_only_post_baseline_activity(e: CliEnv) -> None:
    historical_jnlc(e)
    e.init()
    e.account.clock.advance(minutes=5)
    new = e.account.deposit("250.00", kind="JNLC")
    code, _recon = reconcile(e)
    assert code == 4
    code, env = e.run("rebaseline", "propose", "--reason", "owner journal")
    assert code == 0, env
    assert env["result"]["unexplained_amount_to_acknowledge"] == "250.00"
    listed = [a["ref_id"] for a in env["result"]["preview"]["activities"]]
    assert listed == [new]


# ============================================================ 9: boundary timestamp
def test_activity_at_the_baseline_instant_observed_by_init_is_reflected(e: CliEnv) -> None:
    aid = e.account.deposit("100.00", kind="JNLC")  # posted at the observation instant
    e.init()
    with e.open() as db, db.read() as s:
        assert aid in json.loads(SyncStateRepository().get(s, BASELINE_REFLECTED_ACTIVITIES) or "")
    code, recon = reconcile(e)
    assert_clean_and_still_initialized(code, recon, e)


def test_activity_at_the_baseline_instant_not_observed_by_init_fails_closed(e: CliEnv) -> None:
    """Same timestamp as the boundary, but posted after init read the window: the
    timestamp alone never makes it historical."""
    e.init()
    with e.open() as db, db.read() as s:
        taken_at = s.scalar("SELECT taken_at FROM broker_baselines")
    appear(e, datetime.fromisoformat(str(taken_at)), "5.00", tag="boundary")
    code, recon = reconcile(e)
    assert code == 4 and codes(recon) == ["UNEXPLAINED_CASH_ACTIVITY"]


# ============================================================ ledgers initialized before 15B.13A
def legacy_init(e: CliEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """Initialize as before Step 15B.13A: the reflected set is not recorded (sync_state
    rows are append/update only, so the key is diverted instead of deleted)."""
    monkeypatch.setattr(init_ledger, "BASELINE_REFLECTED_ACTIVITIES", "test.not_recorded")
    e.init()
    with e.open() as db, db.read() as s:
        assert SyncStateRepository().get(s, BASELINE_REFLECTED_ACTIVITIES) is None


def test_legacy_ledger_uses_the_recorded_initial_cursor(
    e: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger created before this step has only ``activity_cursor_id`` (the last id of
    the init window). With a single historical activity that id is the whole set."""
    historical_jnlc(e)
    legacy_init(e, monkeypatch)
    code, recon = reconcile(e)
    assert_clean_and_still_initialized(code, recon, e)


def test_legacy_ledger_with_more_history_fails_closed(
    e: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the recorded set, only the cursor activity is reflected: any other
    historical activity is flagged (a false MISMATCH, never a silent acceptance)."""
    historical_jnlc(e, "100.00", days=0)
    e.account.clock.advance(minutes=1)
    historical_jnlc(e, "50.00", days=2)
    legacy_init(e, monkeypatch)
    code, recon = reconcile(e)
    assert code == 4 and recon["outcome"] == "MISMATCH"
    assert "UNEXPLAINED_CASH_ACTIVITY" in codes(recon)


def test_legacy_reflected_activity_replayed_with_new_content_conflicts(
    e: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Id-only (legacy) reflection carries no payload, but once stored, a replay with
    different content is still a CONFLICT and halts."""
    aid = historical_jnlc(e)
    legacy_init(e, monkeypatch)
    code, _recon = reconcile(e)
    assert code == 0 and stored(e)[aid][0] == "EXTERNAL"
    row = next(r for _p, r in e.account.activities if r["id"] == aid)
    row["description"] = "rewritten"
    e.account.clock.advance(days=1)
    code, _recon = reconcile(e)
    assert code == 4 and state(e) == (True, "RECON_EXECUTION")


@pytest.mark.parametrize("value", ["not json", "[]", '{"a": 1}'])
def test_malformed_reflected_set_fails_closed(e: CliEnv, value: str) -> None:
    e.init()
    with e.open() as db, db.transaction() as tx:
        SyncStateRepository().set(tx, BASELINE_REFLECTED_ACTIVITIES, value, e.account.clock.now)
    with e.open() as db, db.read() as s, pytest.raises(PersistenceError, match="reflected"):
        reflected_activities(s)
