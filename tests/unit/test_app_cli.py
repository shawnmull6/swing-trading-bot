"""Step 14: CLI parsing, exit-code precedence and error mapping (no ledger needed)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from swing_trading_bot.app.cli import build_parser, exit_code_for, main
from swing_trading_bot.app.commands import BrokerNotConfiguredError
from swing_trading_bot.app.exit_codes import PRECEDENCE, ExitCode, combine
from swing_trading_bot.app.lock import LockHeldError
from swing_trading_bot.app.paths import DataPaths, PathError
from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.config.yaml_loader import ConfigFileError
from swing_trading_bot.ledger.errors import MigrationRequiredError

E = ExitCode


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(list(argv), stdout=out), out.getvalue()


def test_precedence_is_total_and_ordered() -> None:
    assert set(PRECEDENCE) == set(ExitCode)
    assert combine() is E.OK
    assert combine(E.REFUSED, E.INCONCLUSIVE) is E.INCONCLUSIVE
    assert combine(E.INCONCLUSIVE, E.HALT_OR_MISMATCH) is E.HALT_OR_MISMATCH
    assert combine(E.BROKER_UNAVAILABLE, E.HALT_OR_MISMATCH) is E.HALT_OR_MISMATCH
    assert combine(E.HALT_OR_MISMATCH, E.LEDGER, E.LOCK_HELD) is E.LOCK_HELD
    assert combine(E.OK, E.INTERNAL) is E.INTERNAL


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (LockHeldError("x"), E.LOCK_HELD),
        (BrokerNotConfiguredError("x"), E.CONFIG),
        (ConfigFileError("x"), E.CONFIG),
        (PathError("x"), E.USAGE),
        (MigrationRequiredError("x"), E.LEDGER),
        (ServiceUnavailableError("x"), E.BROKER_UNAVAILABLE),
        (RuntimeError("x"), E.INTERNAL),
    ],
)
def test_exception_mapping(exc: Exception, code: ExitCode) -> None:
    assert exit_code_for(exc) is code


@pytest.mark.parametrize(
    "argv",
    [
        ("halt",),  # --reason required
        ("halt", "--reason", "  "),  # blank reason
        ("resolve-order", "stb-1"),  # --confirm-absent required
        ("rebaseline", "approve", "1", "--reason", "r"),  # amount required
        ("simulate", "make-money"),  # unknown scenario
        ("sync", "--env-file", ".env"),  # credential files are not a CLI input
        ("frobnicate",),
    ],
)
def test_usage_errors(argv: tuple[str, ...]) -> None:
    code, text = run(*argv)
    assert code == E.USAGE and "usage error" in text


def test_usage_error_as_json() -> None:
    code, text = run("halt", "--json")
    assert code == E.USAGE and json.loads(text)["exit_meaning"] == "USAGE"


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "init-ledger" in capsys.readouterr().out


def test_every_command_has_a_safety_row() -> None:
    from swing_trading_bot.app.safety import COMMAND_SAFETY  # noqa: PLC0415

    sub = next(a for a in build_parser()._actions if a.dest == "command")
    names = set(sub.choices) - {"rebaseline"} | {"rebaseline propose", "rebaseline approve"}  # type: ignore[arg-type]
    assert names <= {c.command for c in COMMAND_SAFETY}


def test_default_actor_and_text_output(tmp_path: Path) -> None:
    code, text = run("status", "--data-dir", str(tmp_path))
    assert code == E.LEDGER
    assert "exit_meaning: LEDGER" in text and "DatabaseNotFoundError" in text


def test_paths_keep_simulation_ledgers_apart(tmp_path: Path) -> None:
    paths = DataPaths.at(tmp_path)
    assert paths.sim_ledger("abc").parent == paths.sim_dir
    assert paths.observe_ledger.parent == paths.root
    for bad in ("../x", "", "a/b", "x" * 80):
        with pytest.raises(PathError):
            paths.sim_ledger(bad)
