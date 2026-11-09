"""Step 1: scaffolding guards that apply to the whole code base."""

from __future__ import annotations

import ast
import os
import socket
import tomllib
from pathlib import Path

import pytest

import swing_trading_bot
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.db import Database
from tests.conftest import ProductionLedgerAccessError

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "swing_trading_bot"


def _source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def test_package_is_typed() -> None:
    assert (SRC_ROOT / "py.typed").is_file()
    assert swing_trading_bot.__name__ == "swing_trading_bot"


def test_network_is_blocked_by_default() -> None:
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        socket.create_connection(("paper-api.alpaca.markets", 443), timeout=1)


def test_credential_environment_is_scrubbed() -> None:
    assert not [k for k in os.environ if k.startswith(("ALPACA_", "APCA_"))]
    assert "TRADING_MODE" not in os.environ


def test_hypothesis_is_a_dev_dependency_only() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    runtime = " ".join(pyproject["project"]["dependencies"])
    dev = " ".join(pyproject["dependency-groups"]["dev"])
    assert "hypothesis" in dev
    assert "hypothesis" not in runtime


def test_no_float_usage_in_source() -> None:
    """Money and quantities must never pass through binary floating point."""
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "float":
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} (float literal)")
    # money.py and broker/mapping.py may *name* float in order to reject it.
    allowed = (
        str(Path("src/swing_trading_bot/domain/money.py")),
        str(Path("src/swing_trading_bot/broker/mapping.py")),
    )
    offenders = [o for o in offenders if not o.startswith(allowed)]
    assert offenders == []


def test_alpaca_sdk_is_imported_only_by_the_read_transport() -> None:
    """Domain, config, ledger and observability stay broker-agnostic; the SDK is confined
    to broker/alpaca_http.py (step 9)."""
    for path in _source_files():
        if path.name == "alpaca_http.py" and path.parent.name == "broker":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n == "alpaca" or n.startswith("alpaca.") for n in names), path


def test_source_never_references_dotenv_file_or_alpaca_txt_directly() -> None:
    for path in _source_files():
        text = path.read_text()
        assert "alpaca.txt" not in text, path


def test_ledger_package_has_no_network_or_broker_imports() -> None:
    """No network I/O can happen inside a DB transaction if the ledger cannot do I/O."""
    banned = ("socket", "http", "urllib", "requests", "httpx", "websocket", "alpaca", "ssl")
    for path in sorted((SRC_ROOT / "ledger").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in banned, f"{path}: imports {name}"


def test_tests_cannot_touch_the_production_ledger(tmp_path: Path) -> None:
    for target in (REPO_ROOT / "data" / "ledger.sqlite3", REPO_ROOT / "data" / "x" / "y.db"):
        with pytest.raises(ProductionLedgerAccessError):
            Database.create(target, RuntimeMode.SIMULATE)
        with pytest.raises(ProductionLedgerAccessError):
            Database.open(target, RuntimeMode.OBSERVE)
    assert not (REPO_ROOT / "data" / "ledger.sqlite3").exists()
    Database.create(tmp_path / "ok.sqlite3", RuntimeMode.SIMULATE).close()  # tmp is fine
