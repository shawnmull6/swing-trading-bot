"""OPT-IN benchmarks (Step 15A, D15-9). Run with ``RUN_PERF=1``; results are written as
JSON to ``$PERF_OUT`` (default: the test's tmp dir). Targets are *reported*, never
enforced by weakening anything; no scope change to the fingerprint is made here.

Workload model (documented in docs/PERFORMANCE.md): one reconciliation every 10 minutes
around the clock = 144/day ≈ 52,560/year; 1-2 sync cycles per reconciliation; M1/M2
activity volume about 5,000/year (fills, fees, interest), stressed up to 20,000.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.ledger.codec import ts_to_db
from swing_trading_bot.ledger.state_fingerprint import ledger_fingerprint
from tests.support.cli_env import CliEnv, cli_env

pytestmark = pytest.mark.perf
RESULTS: dict[str, Any] = {}
TARGETS = {
    "fingerprint_1y_ms": 250,
    "reconcile_1k_s": 5,
    "reconcile_10k_s": 15,
    "cli_status_cold_s": 1.0,
    "sync_s": 2.0,
}


@pytest.fixture(scope="module", autouse=True)
def report(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    yield
    out = Path(os.environ.get("PERF_OUT") or tmp_path_factory.mktemp("perf") / "perf.json")
    out.write_text(json.dumps({"results": RESULTS, "targets": TARGETS}, indent=2))


def timed(fn: Callable[[], Any], repeat: int = 3) -> float:
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def grow_runs(e: CliEnv, n: int, diff_bytes: int = 812) -> None:
    """Append ``n`` CLEAN reconciliation runs with realistic evidence payloads."""
    diff = json.dumps({"evidence": "x" * (diff_bytes - 20)})
    start = e.account.clock.now
    with e.open() as db, db.transaction() as tx:
        for i in range(n):
            tx.execute("INSERT INTO reconciliation_runs (ts, result, diff_json) VALUES (?, ?, ?)",
                       (ts_to_db(start - timedelta(minutes=10 * (n - i))), "OK", diff))  # fmt: skip


@pytest.mark.parametrize("runs", [0, 10_000, 52_560, 105_120])
def test_full_ledger_fingerprint(tmp_path: Path, runs: int) -> None:
    e = cli_env(tmp_path)
    e.ready()
    grow_runs(e, runs)
    with e.open() as db, db.read() as s:
        seconds = timed(lambda: ledger_fingerprint(s))
    size = e.ledger.stat().st_size
    RESULTS[f"fingerprint_{runs}_runs"] = {"ms": round(seconds * 1000, 1), "db_bytes": size}


@pytest.mark.parametrize("count", [1_000, 5_000, 20_000])
def test_reconcile_with_activity_history(tmp_path: Path, count: int) -> None:
    e = cli_env(tmp_path)
    e.ready()
    for i in range(count):  # external interest credits spread over the past ~30 days
        e.account.clock.now += timedelta(seconds=max(1, 2_592_000 // count))
        e.account.deposit("0.01", kind="INT")
        del i
    e.account.clock.advance(minutes=20)
    t0 = time.perf_counter()
    code, _ = e.run("sync")
    sync_s = time.perf_counter() - t0
    calls = len(e.account.transport.calls)
    e.account.clock.advance(minutes=1)
    t0 = time.perf_counter()
    code, env = e.run("reconcile")
    recon_s = time.perf_counter() - t0
    assert env["result"]["reconciliation"]["outcome"] == "CLEAN", code
    recon_calls = len(e.account.transport.calls) - calls
    e.account.clock.advance(minutes=1)
    t0 = time.perf_counter()
    e.run("sync")
    steady_sync_s = time.perf_counter() - t0
    RESULTS[f"activities_{count}"] = {
        "first_sync_s": round(sync_s, 2),
        "reconcile_s": round(recon_s, 2),
        "reconcile_requests": recon_calls,
        "steady_state_sync_s": round(steady_sync_s, 3),
        "db_bytes": e.ledger.stat().st_size,
    }


def test_database_growth_per_reconciliation(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    e.ready()
    before = e.ledger.stat().st_size
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        e.account.clock.advance(minutes=10)
        e.run("reconcile")
    elapsed = time.perf_counter() - t0
    with e.open() as db:
        db.pragma("wal_checkpoint")
    grown = e.ledger.stat().st_size - before
    RESULTS["growth"] = {
        "runs": n,
        "bytes_per_reconcile_cycle": grown // n,
        "projected_bytes_per_year_at_144_per_day": grown // n * 52_560,
        "mean_reconcile_s": round(elapsed / n, 3),
    }


def test_cli_cold_start(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    e.init()
    argv = [sys.executable, "-m", "swing_trading_bot.app.cli", "status", "--data-dir",
            str(e.data), "--json"]  # fmt: skip
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALPACA_", "APCA_"))}
    env.pop("STB_ENV_FILE", None)

    def once() -> None:
        subprocess.run(argv, check=True, capture_output=True, env=env)  # noqa: S603

    RESULTS["cli_status_cold_s"] = round(timed(once, repeat=5), 3)
    help_argv = [sys.executable, "-m", "swing_trading_bot.app.cli", "--help"]
    RESULTS["cli_help_cold_s"] = round(
        timed(lambda: subprocess.run(help_argv, check=True, capture_output=True, env=env),  # noqa: S603
              repeat=5), 3)  # fmt: skip
