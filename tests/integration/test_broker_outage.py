"""Step 14.1: the broker-outage circuit breaker through the real CLI (offline)."""

from __future__ import annotations

import multiprocessing as mp
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerRequestError,
    InvalidBrokerDataError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.ledger.broker_outage import last_reset, load_streak
from swing_trading_bot.ledger.repositories import TradingStateRepository
from tests.support.cli_crash import run_during_outage
from tests.support.cli_env import CliEnv, cli_env


def http503() -> ServiceUnavailableError:
    return ServiceUnavailableError("HTTP 503", status_code=503)


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    env = cli_env(tmp_path)
    env.ready()
    return env


def down(e: CliEnv, factory: Any = http503, path: str = "*") -> None:
    e.account.down[path] = factory


def up(e: CliEnv) -> None:
    e.account.down.clear()


def sync_at(e: CliEnv, minute: float, *, start: Any = None) -> tuple[int, dict[str, Any]]:
    base = start if start is not None else e.account.clock.now
    e.account.clock.now = base + timedelta(minutes=minute)
    return e.run("sync")


def outage(env: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = env["result"]["broker_outage"]
    return out


def streak(e: CliEnv) -> Any:
    with e.open() as db, db.read() as s:
        return load_streak(s)


def halts(e: CliEnv, reason: str = "BROKER_OUTAGE") -> int:
    with e.open() as db, db.read() as s:
        return sum(
            1
            for x in TradingStateRepository().events(s)
            if x.action == "HALT" and x.reason_code == reason
        )


def state(e: CliEnv) -> tuple[bool, str | None]:
    with e.open() as db, db.read() as s:
        st = TradingStateRepository().get(s)
    return st.halted, st.reason_code


# ============================================================ escalation policy
def test_single_5xx_then_thirty_minutes_of_silence_does_not_halt(e: CliEnv) -> None:
    down(e)
    t0 = e.account.clock.now
    code, env = sync_at(e, 0, start=t0)
    assert code == 8 and outage(env)["event"] == "STARTED"
    up(e)
    e.account.clock.now = t0 + timedelta(minutes=45)
    assert e.run("status")[0] == 0  # time passing alone never escalates
    assert state(e) == (False, None) and halts(e) == 0
    down(e)
    code, env = sync_at(e, 0)  # a second failure 45 minutes later: still only 2 cycles
    assert code == 8 and outage(env)["streak"]["counted_cycles"] == 2
    assert halts(e) == 0


def test_three_failures_inside_thirty_minutes_do_not_halt(e: CliEnv) -> None:
    down(e)
    t0 = e.account.clock.now
    for minute in (0, 10, 29):
        code, _ = sync_at(e, minute, start=t0)
        assert code == 8
    assert streak(e).counted_cycles == 3 and halts(e) == 0


def test_three_failures_spanning_thirty_minutes_halt_once(e: CliEnv) -> None:
    down(e, lambda: RateLimitedError("429"))
    t0 = e.account.clock.now
    sync_at(e, 0, start=t0)
    sync_at(e, 15, start=t0)
    code, env = sync_at(e, 30, start=t0)
    assert code == 4 and outage(env)["event"] == "ESCALATED"
    assert outage(env)["streak"]["category"] == "RATE_LIMITED"
    assert state(e) == (True, "BROKER_OUTAGE") and halts(e) == 1
    for minute in (31, 45, 60):  # repeated escalation checks
        code, env = sync_at(e, minute, start=t0)
        assert code == 8 and outage(env)["escalated_now"] is False
    assert halts(e) == 1


def test_many_http_failures_in_one_cycle_count_once(e: CliEnv) -> None:
    e.account.clock.advance(hours=25)  # calendar refresh due: two broker steps will fail
    down(e, path="/calendar")
    down(e, path="/account/activities")
    code, env = e.run("sync")
    assert code == 8  # preflight passed; the cycle failed on broker unavailability
    assert len(outage(env)["cycle_failures"]) >= 2
    assert outage(env)["streak"]["counted_cycles"] == 1


def test_rapid_retries_are_not_distinct_cycles(e: CliEnv) -> None:
    down(e)
    t0 = e.account.clock.now
    for seconds in (0, 20, 40, 59):
        sync_at(e, seconds / 60, start=t0)
    assert streak(e).counted_cycles == 1
    sync_at(e, 1, start=t0)
    assert streak(e).counted_cycles == 2


def test_same_cycle_reported_twice_is_idempotent(e: CliEnv) -> None:
    down(e)
    e.run("sync")
    e.run("sync")  # same timestamp: the same cycle re-reported
    assert streak(e).counted_cycles == 1


# ============================================================ restart and reset
def test_outage_streak_survives_process_restarts(e: CliEnv, tmp_path: Path) -> None:
    down(e)
    t0 = e.account.clock.now
    sync_at(e, 0, start=t0)
    child_time = t0 + timedelta(minutes=15)
    proc = mp.get_context("spawn").Process(
        target=run_during_outage, args=(str(tmp_path), child_time.isoformat(), "sync")
    )
    proc.start()
    proc.join(60)
    assert proc.exitcode == 8  # another process recorded the second cycle durably
    assert streak(e).counted_cycles == 2
    code, _ = sync_at(e, 30, start=t0)
    assert code == 4 and state(e) == (True, "BROKER_OUTAGE")


def test_complete_sync_resets_the_streak(e: CliEnv) -> None:
    down(e)
    t0 = e.account.clock.now
    sync_at(e, 0, start=t0)
    sync_at(e, 15, start=t0)
    up(e)
    code, env = sync_at(e, 20, start=t0)
    assert code == 0 and outage(env)["event"] == "RESET"
    with e.open() as db, db.read() as s:
        assert load_streak(s) is None
        assert last_reset(s)["previous"]["counted_cycles"] == 2  # type: ignore[index]
    down(e)
    code, env = sync_at(e, 31, start=t0)  # a new streak, not the third cycle of the old
    assert code == 8 and outage(env)["event"] == "STARTED" and halts(e) == 0


def test_partial_sync_does_not_reset_the_streak(e: CliEnv) -> None:
    down(e)
    sync_at(e, 0)
    up(e)
    e.account.clock.advance(hours=25)  # calendar refresh due ...
    e.account.down["/calendar"] = lambda: BrokerRequestError("HTTP 400", status_code=400)
    code, env = e.run("sync")  # ... and fails with a non-outage error: incomplete
    assert outage(env)["event"] == "UNCHANGED" and env["result"]["calendar"]["error"]
    assert code == 5  # an incomplete cycle is never reported as success
    assert streak(e) is not None and streak(e).counted_cycles == 1


def test_halt_survives_reconnection_and_needs_clean_resume(e: CliEnv) -> None:
    down(e)
    t0 = e.account.clock.now
    for minute in (0, 15, 30):
        sync_at(e, minute, start=t0)
    assert state(e) == (True, "BROKER_OUTAGE")
    up(e)
    code, env = sync_at(e, 35, start=t0)
    assert code == 0 and outage(env)["event"] == "RESET"
    assert state(e) == (True, "BROKER_OUTAGE")  # restored connectivity clears nothing
    code, env = e.run("status", "--broker")
    assert "PERSISTENT_HALT" in env["result"]["gates"]["reason_codes"]
    code, env = e.run("resume", "--reason", "broker back, reconciled")
    assert code == 0 and env["result"]["resumed"] is True
    assert env["result"]["reconciliation"]["outcome"] == "CLEAN"
    assert state(e) == (False, None)


def test_new_entries_block_immediately_on_the_first_failure(e: CliEnv) -> None:
    _code, env = e.run("status", "--broker")
    assert "BROKER_OUTAGE_ACTIVE" not in env["result"]["gates"]["reason_codes"]
    down(e, path="/account/activities")  # preflight works; sync cannot complete
    e.account.clock.advance(seconds=5)
    e.run("sync")
    _code, env = e.run("status", "--broker")  # last good sync is only seconds old
    assert "BROKER_OUTAGE_ACTIVE" in env["result"]["gates"]["reason_codes"]
    assert env["result"]["broker"]["outage_streak"]["counted_cycles"] == 1


# ============================================================ dedicated paths
def test_auth_identity_and_payload_errors_keep_their_own_paths(e: CliEnv) -> None:
    down(e)
    sync_at(e, 0)
    up(e)
    baseline = streak(e)
    e.account.fail("/account", AuthenticationError("401", status_code=401))
    code, env = sync_at(e, 5)
    assert code == 4 and env["result"]["preflight"]["halted"] == "AUTH"
    assert outage(env)["event"] == "UNCHANGED"
    e.account.fail("/account", InvalidBrokerDataError("float in cash"))
    code, env = sync_at(e, 5)
    assert env["result"]["preflight"]["halted"] == "BROKER_PAYLOAD"
    assert outage(env)["event"] == "UNCHANGED"
    e.account.account_id = "someone-else"
    code, env = sync_at(e, 5)
    assert env["result"]["preflight"]["halted"] == "RECON_ACCOUNT"
    assert outage(env)["event"] == "UNCHANGED"
    assert streak(e) == baseline and halts(e) == 0
