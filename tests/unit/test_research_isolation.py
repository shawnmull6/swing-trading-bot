"""The research package is offline infrastructure: no network, credentials, environment,
ledger, OMS or broker transport can be reached from it (Step 16.15)."""

from __future__ import annotations

import ast
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from swing_trading_bot.research.dataset import DatasetError, load_dataset, write_dataset
from tests.conftest import PRODUCTION_DATA_DIR, ProductionLedgerAccessError

RESEARCH = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "research"

ALLOWED_INTERNAL = (
    "swing_trading_bot.research",
    "swing_trading_bot.strategy",
    "swing_trading_bot.domain",
    "swing_trading_bot.broker.models",
    "swing_trading_bot.broker.mapping",  # pure payload mapping, shared with the adapter
    "swing_trading_bot.broker.errors",
)
ALLOWED_STDLIB = frozenset(
    {"__future__", "collections", "dataclasses", "datetime", "decimal", "enum", "hashlib",
     "itertools", "json", "os", "pathlib", "re", "shutil", "typing"}
)  # fmt: skip
FORBIDDEN_TEXT = (
    "os.environ", "getenv", "dotenv", "ALPACA_", "SecretStr", "ledger.sqlite3",
    "import socket", "urllib", "http.client", "httpx", "import requests", "urlopen",
    "submit_order", "OrderManager", "sqlite3", "TRADING_ENABLED", "load_credentials",
)  # fmt: skip
#: Names that would reach a transport, a credential or the environment if called.
FORBIDDEN_CALLS = frozenset({"exec", "eval", "__import__", "input", "print", "getenv"})


def _modules() -> list[Path]:
    files = sorted(RESEARCH.glob("*.py"))
    assert {p.name for p in files} >= {"universe.py", "dataset.py", "acquisition.py"}
    return files


def test_research_imports_only_offline_modules() -> None:
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.startswith("swing_trading_bot"):
                    assert name.startswith(ALLOWED_INTERNAL), f"{path.name} imports {name}"
                else:
                    assert name.split(".")[0] in ALLOWED_STDLIB, f"{path.name} imports {name}"


def test_research_source_mentions_no_network_or_credential_paths() -> None:
    for path in _modules():
        text = path.read_text()
        for needle in FORBIDDEN_TEXT:
            assert needle not in text, f"{path.name} mentions {needle}"
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, f"{path.name} calls {node.func.id}"


def test_importing_research_loads_no_transport_ledger_or_oms() -> None:
    code = (
        "import sys\n"
        "import swing_trading_bot.research.dataset\n"
        "import swing_trading_bot.research.acquisition\n"
        "import swing_trading_bot.research.universe\n"
        "import swing_trading_bot.strategy.walk_forward\n"
        "import swing_trading_bot.strategy.evaluation\n"
        "bad = sorted(m for m in sys.modules if m.startswith(("
        "'swing_trading_bot.ledger', 'swing_trading_bot.oms', 'swing_trading_bot.app',"
        "'swing_trading_bot.broker.alpaca', 'swing_trading_bot.broker.sim',"
        "'swing_trading_bot.risk.pretrade', 'swing_trading_bot.config.settings',"
        "'alpaca', 'sqlite3', 'requests', 'http.client', 'urllib3', 'httpx', 'dotenv')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and inline code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == ""


def test_strategy_does_not_depend_on_research() -> None:
    strategy = RESEARCH.parent / "strategy"
    for path in sorted(strategy.glob("*.py")):
        assert "swing_trading_bot.research" not in path.read_text(), path.name


def test_tests_cannot_open_the_production_ledger_in_any_mode() -> None:
    """The conftest guard refuses raw sqlite3 access under data/, read-only URIs
    included. Only paths inside a directory that does not exist are used, so even a
    broken guard could neither open the real ledger nor create a file."""
    missing = PRODUCTION_DATA_DIR / "no-such-dir-guard-probe"
    assert not missing.exists()
    target = missing / "probe.sqlite3"
    for database in (str(target), target, f"file:{target}?mode=ro"):
        with pytest.raises(ProductionLedgerAccessError):
            sqlite3.connect(database, uri=str(database).startswith("file:"))
    assert not missing.exists()
    sqlite3.connect(":memory:").close()


def test_datasets_refuse_a_directory_that_holds_a_ledger(tmp_path: Path) -> None:
    """Names are listed, never opened: an empty placeholder file triggers it."""
    (tmp_path / "ledger.sqlite3").write_bytes(b"")
    with pytest.raises(DatasetError):
        write_dataset(tmp_path / "ds", {}, {})
    with pytest.raises(DatasetError):
        load_dataset(tmp_path)
