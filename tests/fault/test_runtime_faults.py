"""Step 15A fault injection: concurrency, partial/malformed broker responses, replay
conflicts, late activities, corrupted outage state, and ambiguous-order recovery across
every send boundary. Offline; synthetic broker; temporary ledgers only."""

from __future__ import annotations

import multiprocessing as mp
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app.config import AppConfig
from swing_trading_bot.app.paths import DataPaths
from swing_trading_bot.app.runtime import resolve_order
from swing_trading_bot.app.simulation import (
    SIM_ACTOR,
    ScenarioCrash,
    bring_up,
    crash_at,
    create_harness,
)
from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.domain.enums import OrderStatus
from swing_trading_bot.ledger.activity_coverage import complete_through
from swing_trading_bot.ledger.broker_outage import OUTAGE_STATE_KEY
from swing_trading_bot.ledger.repositories import (
    OrdersRepository,
    ReservationsRepository,
    SyncStateRepository,
)
from swing_trading_bot.oms.order_manager import CRASH_POINTS
from tests.support.cli_crash import run_paused
from tests.support.cli_env import CONFIG_DIR, CliEnv, cli_env


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    env = cli_env(tmp_path)
    env.ready()
    return env


def activities(e: CliEnv) -> int:
    return int(e.scalar("SELECT count(*) FROM broker_activities"))


# ============================================================ concurrency
def test_concurrent_operations_serialize_on_the_lock(e: CliEnv) -> None:
    runs = e.scalar("SELECT count(*) FROM reconciliation_runs")
    e.account.clock.advance(minutes=1)
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(3)
    procs = [
        ctx.Process(target=run_paused, args=(str(e.root), e.account.clock.now.isoformat(),
                                             "reconcile", "recon:after_observation", barrier))
        for _ in range(3)
    ]  # fmt: skip
    for p in procs:
        p.start()
    for p in procs:
        p.join(90)
    assert sorted(p.exitcode or 0 for p in procs) == [0, 6, 6]  # one ran; the others refused
    assert e.scalar("SELECT count(*) FROM reconciliation_runs") == runs + 1
    assert e.run("verify-ledger", str(e.ledger))[0] == 0


# ============================================================ partial / malformed responses
def flaky_second_page(e: CliEnv) -> list[bool]:
    original = e.account.transport._defaults[("trading", "/account/activities")]
    failing = [True]

    def handler(params: Mapping[str, str]) -> object:
        if failing[0] and "page_token" in params:
            raise ServiceUnavailableError("HTTP 503", status_code=503)
        return original(params)  # type: ignore[operator]

    e.account.transport.always("trading", "/account/activities", handler)
    return failing


def test_partial_activity_pages_never_advance_coverage_or_duplicate(e: CliEnv) -> None:
    for _ in range(150):
        e.account.deposit("1.00")
    e.account.clock.advance(minutes=20)
    with e.open() as db, db.read() as s:
        covered_before = complete_through(s)
    failing = flaky_second_page(e)
    code, env = e.run("sync")
    assert code == 8 and env["result"]["sync"]["status"] == "INCOMPLETE"
    assert activities(e) == 100  # page 1 preserved, idempotently
    with e.open() as db, db.read() as s:
        assert complete_through(s) == covered_before  # no gap is ever marked complete
    failing[0] = False
    e.account.clock.advance(minutes=1)
    code, env = e.run("sync")
    assert env["result"]["sync"]["status"] == "OK"
    assert activities(e) == 150  # nothing booked twice


def test_truncated_json_halts_broker_payload(e: CliEnv) -> None:
    e.account.transport.always("trading", "/account/activities", '[{"id": "x", "activ')
    e.account.clock.advance(minutes=1)
    code, env = e.run("sync")
    assert code == 4
    assert e.run("halts")[1]["result"]["trading_state"]["reason_code"] == "BROKER_PAYLOAD"
    assert env["result"]["broker_outage"]["event"] == "UNCHANGED"  # not an outage


def test_replayed_activity_with_changed_content_is_a_conflict(e: CliEnv) -> None:
    aid = e.account.deposit("25.00")
    e.account.clock.advance(minutes=20)
    e.run("sync")
    with e.open() as db, db.read() as s:
        stored = s.scalar("SELECT raw_json FROM broker_activities WHERE activity_id = ?",
                          (aid,))  # fmt: skip
    for _posted, row in e.account.activities:
        if row["id"] == aid:
            row["net_amount"] = "2500.00"  # same id, different content
    e.account.clock.advance(minutes=1)
    code, env = e.run("reconcile")
    assert code == 4
    codes = {f["code"] for f in env["result"]["reconciliation"]["findings"]}
    assert "ACTIVITY_CONFLICT" in codes
    with e.open() as db, db.read() as s:  # the stored row is never rewritten
        assert s.scalar("SELECT raw_json FROM broker_activities WHERE activity_id = ?",
                        (aid,)) == stored  # fmt: skip


def test_late_back_dated_activity_is_found_by_the_history_rescan(e: CliEnv) -> None:
    e.account.clock.advance(hours=2)
    assert e.run("sync")[0] == 0
    before = activities(e)
    now = e.account.clock.now
    e.account.clock.now = now - timedelta(hours=1, minutes=50)  # inside covered history
    e.account.deposit("7.00")
    e.account.clock.now = now + timedelta(minutes=1)
    e.run("sync")
    assert activities(e) == before  # forward window + overlap cannot see it
    code, env = e.run("reconcile")
    assert activities(e) == before + 1  # the full-history re-scan does
    assert code == 4 and env["result"]["reconciliation"]["outcome"] == "MISMATCH"


def test_corrupted_outage_state_fails_closed(e: CliEnv) -> None:
    with e.open() as db, db.transaction() as tx:
        SyncStateRepository().set(tx, OUTAGE_STATE_KEY, '{"active": true, "streak": {}',
                                  e.account.clock.now)  # fmt: skip
    for argv in (("sync",), ("status",), ("status", "--broker")):
        code, env = e.run(*argv)
        assert code == 9, argv
        assert "unreadable" in env["result"]["detail"]


# ============================================================ ambiguous-order recovery
SEND_POINTS = [p for p in CRASH_POINTS if p != "after_cancel_marker"]


@pytest.mark.parametrize("point", SEND_POINTS)
def test_crash_at_every_send_boundary_converges(tmp_path: Path, point: str) -> None:
    (tmp_path / "data").mkdir()
    paths = DataPaths.at(tmp_path / "data")
    paths.sim_dir.mkdir()
    h = create_harness(paths, "fault", AppConfig.load(CONFIG_DIR))
    try:
        bring_up(h)
        h.build(crash=crash_at(point))
        h.rt.gates.run_startup(now=h.tick())
        with pytest.raises(ScenarioCrash):
            h.place()
        h.restart()
        with h.db.read() as s:
            (order,) = OrdersRepository().list_all(s)
        coid = order.client_order_id
        start = order.send_attempted_at or order.created_at
        for minute in (1, 5):
            h.clock.advance_to(start + timedelta(minutes=minute))
            h.cycle(f"+{minute}m")
        status = h.order(coid).status
        if status in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMIT_AMBIGUOUS):
            h.clock.advance_to(start + timedelta(minutes=26))
            resolved = resolve_order(h.rt, coid, actor=SIM_ACTOR)
            assert resolved.confirmed, resolved.to_dict()
            final: Any = OrderStatus.CONFIRMED_ABSENT
        else:
            final = OrderStatus.ACCEPTED
        assert h.order(coid).status is final
        assert h.broker_orders(coid) == (0 if final is OrderStatus.CONFIRMED_ABSENT else 1)
        with h.db.read() as s:
            res = ReservationsRepository().get(s, coid)
        assert res is not None
        assert (res.status.value == "ACTIVE") == (final is OrderStatus.ACCEPTED)
        assert h.rt.pf.check_invariants(now=h.tick()).ok
    finally:
        h.db.close()
