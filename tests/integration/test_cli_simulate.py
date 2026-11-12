"""Step 14: offline simulation scenarios through the CLI (hypothetical, SimBroker only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from swing_trading_bot.app.simulation import HYPOTHETICAL, SCENARIOS
from tests.support.cli_env import cli_env


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_scenario_passes_end_to_end(tmp_path: Path, scenario: str) -> None:
    e = cli_env(tmp_path)
    code, env = e.run("simulate", scenario, "--run-id", "t")
    (result,) = env["result"]["scenarios"]
    assert code == 0, result
    assert env["hypothetical"] is True and result["hypothetical"] is True
    assert result["notice"] == HYPOTHETICAL
    assert result["passed"] and all(result["checks"].values()), result["checks"]
    final = result["final"]
    assert final["invariants_ok"] is True
    assert all(o["broker_orders_created"] <= 1 for o in final["orders"])  # never resent
    assert result["ledger"] == str(e.data / "sim" / "t.sqlite3")
    assert not e.ledger.exists()
    assert e.account.transport.calls == []  # the Alpaca adapter is never touched


def test_all_scenarios_and_human_output_is_labelled(tmp_path: Path) -> None:
    e = cli_env(tmp_path)
    code, env = e.run("simulate", "all", "--run-id", "batch")
    assert code == 0 and env["result"]["passed"] is True
    assert len(env["result"]["scenarios"]) == len(SCENARIOS)
    assert len(list((e.data / "sim").glob("batch-*.sqlite3"))) == len(SCENARIOS)
    import io  # noqa: PLC0415

    from swing_trading_bot.app.cli import main  # noqa: PLC0415

    out = io.StringIO()
    main(["orders", "--sim-run", "batch-happy-path", "--data-dir", str(e.data)], stdout=out)
    assert out.getvalue().startswith("*** HYPOTHETICAL SIMULATION")
