"""Step 15B.4: the observed ``/v2/account/configurations`` shape (no ``pdt_check`` /
``dtbp_check``) through the real CLI, offline, plus broker-baseline audit compatibility.

* New baselines keep both keys, as JSON ``null`` when the broker did not report them.
* Historical baselines holding string values still load, verify, reconcile and survive a
  rebaseline verbatim; existing rows (and their fingerprint) never change.
* A malformed present value still fails closed with exit 10.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.cli_env import CliEnv, cli_env

IMMUTABLE = ("id, portfolio_id, taken_at, cash, equity, activity_cursor_id, "
             "account_config_json, reason, rebaseline_request_id")  # fmt: skip
HISTORICAL = {"pdt_check": "entry", "dtbp_check": "entry"}
MALFORMED: list[dict[str, Any]] = [
    {"pdt_check": ""},
    {"pdt_check": True},
    {"dtbp_check": 0},
    {"dtbp_check": ["entry"]},
    {"pdt_check": {}},
    {"suspend_trade": None},  # a required field: null is still rejected
    {"no_shorting": "true"},
]


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def baselines(e: CliEnv) -> list[tuple[Any, ...]]:
    with e.open() as db, db.read() as s:
        return [tuple(r) for r in s.fetch_all(
            f"SELECT {IMMUTABLE} FROM broker_baselines ORDER BY id"
        )]  # fmt: skip


def configs(e: CliEnv) -> list[dict[str, Any]]:
    return [json.loads(row[6]) for row in baselines(e)]


def row_digest(row: tuple[Any, ...]) -> str:
    return hashlib.sha256(repr(row).encode()).hexdigest()


def confirm(e: CliEnv) -> tuple[str, ...]:
    return ("--confirm-path", str(e.ledger), "--confirm-account", e.account.account_id,
            "--confirm-environment", "paper")  # fmt: skip


def rebaseline_after_deposit(e: CliEnv) -> None:
    e.account.deposit("250.00")
    e.account.clock.advance(minutes=20)
    assert e.run("reconcile")[0] == 4  # RECON_CASH mismatch
    code, env = e.run("rebaseline", "propose", "--reason", "owner deposit")
    assert code == 0, env
    rid = str(env["result"]["request_id"])
    e.account.clock.advance(minutes=1)
    code, env = e.run("rebaseline", "approve", rid, "--acknowledge-unexplained", "250.00",
                      "--reason", "statement checked")  # fmt: skip
    assert code == 0 and env["result"]["decision"] == "APPROVED", env


# ============================================================ broker-check
def test_broker_check_with_observed_config_shape(e: CliEnv) -> None:
    code, env = e.run("broker-check")
    assert code == 0, env
    result = env["result"]
    assert result["identity_problems"] == [] and result["no_shorting"] is True
    assert result["max_margin_multiplier"] == "1" and result["m2_account_protections"] is True
    assert "pdt_check" not in result and "dtbp_check" not in result  # never displayed


@pytest.mark.parametrize("override", MALFORMED)
def test_malformed_config_still_exits_10(e: CliEnv, override: dict[str, Any]) -> None:
    e.account.config_overrides.update(override)
    code, env = e.run("broker-check")
    assert code == 10 and env["result"]["error"] == "InvalidBrokerDataError"
    code, env = e.run("init-ledger", *confirm(e))
    assert code == 10 and env["result"]["status"] == "BROKER_PAYLOAD"
    assert not e.ledger.exists()


# ============================================================ new baselines
def test_init_records_json_null_for_unreported_fields(e: CliEnv) -> None:
    e.init()
    (cfg,) = configs(e)
    assert set(cfg) == {"no_shorting", "max_margin_multiplier", "pdt_check", "dtbp_check",
                        "suspend_trade", "fractional_trading"}  # fmt: skip
    assert cfg["pdt_check"] is None and cfg["dtbp_check"] is None
    assert e.run("verify-ledger", str(e.ledger))[0] == 0


@pytest.mark.parametrize("override", [{"pdt_check": None}, {"pdt_check": "entry"},
                                      {"pdt_check": "exit", "dtbp_check": None}])  # fmt: skip
def test_init_records_reported_values_verbatim(e: CliEnv, override: dict[str, Any]) -> None:
    e.account.config_overrides.update(override)
    e.init()
    (cfg,) = configs(e)
    assert cfg["pdt_check"] == override.get("pdt_check")
    assert cfg["dtbp_check"] == override.get("dtbp_check")


def gate_codes(env: dict[str, Any]) -> list[str]:
    codes: list[str] = env["result"]["gates"]["reason_codes"]
    return codes


@pytest.mark.parametrize("still_reported", [False, True])
def test_unknown_config_through_startup_reconcile_and_sync(e: CliEnv, still_reported: bool) -> None:
    e.account.config_overrides.update(HISTORICAL)
    e.ready()  # init -> CLEAN reconcile -> resume, all exit 0
    e.account.clock.advance(minutes=1)
    code, env = e.run("reconcile")
    assert code == 0, env
    known = gate_codes(env)
    if not still_reported:
        e.account.config_overrides.clear()  # the broker now omits both fields
    e.account.clock.advance(minutes=1)
    code, env = e.run("reconcile")
    assert code == 0 and env["result"]["reconciliation"]["outcome"] == "CLEAN", env
    assert gate_codes(env) == known  # unknown values change no gate outcome
    assert "BROKER_DATA_INVALID" not in gate_codes(env)
    code, env = e.run("sync")
    assert code == 0 and env["result"]["preflight"]["ok"] is True
    assert e.run("verify-ledger", str(e.ledger))[0] == 0


# ============================================================ historical baselines
def test_historical_string_baseline_is_untouched_and_fingerprint_stable(e: CliEnv) -> None:
    e.account.config_overrides.update(HISTORICAL)
    e.ready()
    (before,) = baselines(e)
    assert json.loads(before[6])["pdt_check"] == "entry"
    fp = e.fingerprint(("broker_baselines",))
    e.account.config_overrides.clear()  # new code, broker omits the fields
    e.account.clock.advance(minutes=1)
    for argv in (("sync",), ("reconcile",), ("status",), ("audit",)):
        e.run(*argv)
    assert e.run("verify-ledger", str(e.ledger))[0] == 0
    assert baselines(e) == [before]
    assert e.fingerprint(("broker_baselines",)) == fp


def test_rebaseline_copies_the_stored_dict_and_keeps_old_rows(e: CliEnv) -> None:
    e.account.config_overrides.update(HISTORICAL)
    e.ready()
    (old,) = baselines(e)
    digest = row_digest(old)
    e.account.config_overrides.clear()
    rebaseline_after_deposit(e)
    rows = baselines(e)
    assert len(rows) == 2
    assert row_digest(rows[0]) == digest  # the historical row is immutable
    assert json.loads(rows[1][6]) == json.loads(old[6])  # copied verbatim, strings kept
    assert e.run("verify-ledger", str(e.ledger))[0] == 0


def test_rebaseline_of_a_null_baseline_keeps_null(e: CliEnv) -> None:
    e.ready()
    rebaseline_after_deposit(e)
    cfgs = configs(e)
    assert cfgs[0] == cfgs[1] and cfgs[1]["pdt_check"] is None


# ============================================================ runtime payload violation
def test_malformed_config_at_runtime_is_exit_10_then_4(e: CliEnv) -> None:
    e.ready()
    e.account.config_overrides["dtbp_check"] = False
    code, env = e.run("sync")
    assert code == 10 and env["result"]["preflight"]["halted"] == "BROKER_PAYLOAD"
    assert env["result"]["broker_outage"]["event"] == "UNCHANGED"
    assert e.run("sync")[0] == 4
