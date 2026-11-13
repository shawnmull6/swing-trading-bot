"""Step 15A.1 (O2): a failed activity-head re-check must not repeat the whole observation.

Only a head re-check that *succeeds and finds new activity* keeps the existing bounded
retry. A head read that fails (429, 5xx, timeout) ends the observation as INCONCLUSIVE:
one full-history re-scan, no completeness advance, no false CLEAN, no duplicate booking,
and the repeated-INCONCLUSIVE persistent halt still applies."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.errors import (
    BrokerError,
    RateLimitedError,
    ServiceUnavailableError,
)
from swing_trading_bot.ledger.activity_coverage import complete_through
from tests.support.cli_env import CliEnv, cli_env

PATH = "/account/activities"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Probe:
    """Wraps the activities route: tags each request, optionally fails the head re-check
    or posts new activity when the head is first read."""

    def __init__(self, e: CliEnv, *, fail: Callable[[], BrokerError] | None = None,
                 change_once: bool = False) -> None:  # fmt: skip
        self.e = e
        self.fail = fail
        self.change_once = change_once
        self.kinds: list[str] = []
        with e.open() as db, db.read() as s:
            self.inception = _ts(str(s.scalar(
                "SELECT taken_at FROM broker_baselines WHERE reason = 'INIT'")))  # fmt: skip
        self.original = e.account.transport._defaults[("trading", PATH)]
        e.account.transport.always("trading", PATH, self.handler)

    def kind(self, p: Mapping[str, str]) -> str:
        now = self.e.account.clock.now
        after = _ts(p["after"]) if "after" in p else None
        until = _ts(p["until"]) if "until" in p else None
        if after == self.inception:
            return "FULL_HISTORY"
        if after == now - timedelta(hours=1) and until == now:
            return "HEAD"
        return "OTHER"

    def handler(self, p: Mapping[str, str]) -> object:
        kind = self.kind(p)
        if "page_token" not in p:
            self.kinds.append(kind)
        if kind == "HEAD" and self.fail is not None:
            raise self.fail()
        if kind == "HEAD" and self.change_once:
            self.change_once = False
            now = self.e.account.clock.now
            self.e.account.clock.now = now - timedelta(minutes=1)
            self.e.account.deposit("0.07", kind="INT")  # appears while we reconcile
            self.e.account.clock.now = now
        return self.original(p)  # type: ignore[operator]

    def count(self, kind: str) -> int:
        return self.kinds.count(kind)


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    env = cli_env(tmp_path)
    env.ready()
    env.account.clock.advance(minutes=1)
    assert env.run("sync")[0] == 0  # coverage marker set for this instant
    return env


def marker(e: CliEnv) -> Any:
    with e.open() as db, db.read() as s:
        return complete_through(s)


@pytest.mark.parametrize(
    "failure",
    [
        lambda: RateLimitedError("429"),
        lambda: ServiceUnavailableError("503", status_code=503),
        lambda: ServiceUnavailableError("timed out", kind="timeout"),
    ],
    ids=["429", "5xx", "timeout"],
)
def test_failed_head_read_stops_after_one_observation(
    e: CliEnv, failure: Callable[[], BrokerError]
) -> None:
    before_marker = marker(e)
    activities = e.scalar("SELECT count(*) FROM broker_activities")
    probe = Probe(e, fail=failure)
    code, env = e.run("reconcile")
    recon = env["result"]["reconciliation"]
    assert code == 5 and recon["outcome"] == "INCONCLUSIVE"  # never a false CLEAN
    codes = {(f["code"], f["subject"]) for f in recon["findings"]}
    assert ("READ_FAILED", "activity head re-check") in codes
    assert any(c == "ACTIVITY_HEAD_UNSTABLE" for c, _ in codes)
    assert probe.count("FULL_HISTORY") == 1  # not repeated up to 3 times
    assert probe.count("HEAD") == 3  # ONE head read = its 3 bounded read attempts
    with e.open() as db, db.read() as s:
        run = s.fetch_one("SELECT result, diff_json FROM reconciliation_runs ORDER BY id DESC")
    assert run is not None and run[0] == "INCONCLUSIVE"
    evidence = json.loads(str(run[1]))
    assert evidence["attempts"] == 1 and evidence["head_check"] == "READ_FAILED"
    assert marker(e) == before_marker  # no completeness advanced by the failed check
    assert e.scalar("SELECT count(*) FROM broker_activities") == activities


def test_genuine_head_change_keeps_the_bounded_retry(e: CliEnv) -> None:
    probe = Probe(e, change_once=True)
    code, env = e.run("reconcile")
    assert code == 0 and env["result"]["reconciliation"]["outcome"] == "CLEAN"
    assert probe.count("FULL_HISTORY") == 2 and probe.count("HEAD") == 2  # one repeat
    ids = e.scalar("SELECT count(*) FROM broker_activities")
    distinct = e.scalar("SELECT count(DISTINCT activity_id) FROM broker_activities")
    assert ids == distinct  # the new activity was booked exactly once


def test_head_that_keeps_changing_is_still_bounded(e: CliEnv) -> None:
    probe = Probe(e)

    def always_new(p: Mapping[str, str]) -> object:
        if probe.kind(p) == "HEAD" and "page_token" not in p:
            now = e.account.clock.now
            e.account.clock.now = now - timedelta(seconds=30)
            e.account.deposit("0.01", kind="INT")
            e.account.clock.now = now
        return probe.handler(p)

    e.account.transport.always("trading", PATH, always_new)
    code, env = e.run("reconcile")
    assert code == 5
    assert probe.count("FULL_HISTORY") == 3  # max_snapshot_retries, unchanged policy
    codes = {f["code"] for f in env["result"]["reconciliation"]["findings"]}
    assert "ACTIVITY_HEAD_UNSTABLE" in codes and "READ_FAILED" not in codes


def test_repeated_inconclusive_policy_still_halts(e: CliEnv) -> None:
    Probe(e, fail=lambda: RateLimitedError("429"))
    for _ in range(5):
        e.account.clock.advance(minutes=2)
        code, _env = e.run("reconcile")
    assert code == 4
    halts = e.run("halts")[1]["result"]["trading_state"]
    assert halts["halted"] is True and halts["reason_code"] == "RECON_INCONCLUSIVE_REPEAT"
