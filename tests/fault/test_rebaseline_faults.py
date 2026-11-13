"""Step 15A fault injection: rebaseline and acknowledgement integrity."""

from __future__ import annotations

from pathlib import Path

import pytest

from swing_trading_bot.ledger.errors import PersistenceError
from tests.support.cli_env import CliEnv, cli_env


class Crash(BaseException):
    pass


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    env = cli_env(tmp_path)
    env.ready()
    env.account.deposit("40.00")
    env.account.clock.advance(minutes=20)
    assert env.run("reconcile")[0] == 4
    return env


def propose(e: CliEnv) -> str:
    code, env = e.run("rebaseline", "propose", "--reason", "owner deposit")
    assert code == 0
    e.account.clock.advance(minutes=1)
    return str(env["result"]["request_id"])


def approve(e: CliEnv, rid: str, amount: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    return e.run("rebaseline", "approve", rid, "--acknowledge-unexplained", amount,
                 "--reason", "checked")  # fmt: skip


@pytest.mark.parametrize("typed", ["40", "40.0", "40.00", "+40.00"])
def test_equal_decimal_amounts_are_accepted(e: CliEnv, typed: str) -> None:
    code, env = approve(e, propose(e), typed)
    assert code == 0 and env["result"]["decision"] == "APPROVED"


@pytest.mark.parametrize("typed", ["40.001", "39.99", "-40", "forty", "4e1x", ""])
def test_inexact_amounts_are_refused_and_correctable(e: CliEnv, typed: str) -> None:
    rid = propose(e)
    code, _ = approve(e, rid, typed)
    assert code in (2, 3)  # an empty value is a usage error; the rest are refusals
    assert approve(e, rid, "40.00")[0] == 0  # the request stayed PROPOSED


def test_crash_inside_approval_commits_nothing(e: CliEnv) -> None:
    rid = propose(e)
    baselines = e.scalar("SELECT count(*) FROM broker_baselines")

    def hook(point: str) -> None:
        if point == "rebaseline:before_commit":
            raise Crash(point)

    e.crash = hook
    with pytest.raises(Crash):
        approve(e, rid, "40.00")
    e.crash = None
    assert e.scalar("SELECT count(*) FROM broker_baselines") == baselines
    assert e.scalar(f"SELECT status FROM rebaseline_requests WHERE id = {int(rid)}") == "PROPOSED"
    assert approve(e, rid, "40.00")[0] == 0


def test_acknowledgements_and_evidence_are_immutable(e: CliEnv) -> None:
    assert approve(e, propose(e), "40.00")[0] == 0
    statements = (
        "UPDATE rebaseline_acknowledgements SET payload = 'x'",
        "DELETE FROM rebaseline_acknowledgements",
        "UPDATE broker_activities SET raw_json = '{}'",
        "DELETE FROM broker_baselines",
        "UPDATE rebaseline_requests SET status = 'PROPOSED'",
        "DELETE FROM halt_events",
    )
    for sql in statements:
        with pytest.raises(PersistenceError), e.open() as db, db.transaction() as tx:
            tx.execute(sql)
    code, env = e.run("verify-ledger", str(e.ledger))
    assert code == 0 and env["result"]["verified"] is True
