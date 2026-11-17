"""The strategy package is research code: no OMS, ledger, broker transport, credentials
or order submission can be reached from it (Step 16.14, Phase E)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

STRATEGY = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot" / "strategy"

ALLOWED_INTERNAL = (
    "swing_trading_bot.strategy",
    "swing_trading_bot.domain",
    "swing_trading_bot.broker.models",  # pure DailyBar model
    "swing_trading_bot.risk.sizing",  # pure sizing math
    "swing_trading_bot.config.risk_limits",  # the limits model (no file read here)
)
ALLOWED_STDLIB = frozenset(
    {"__future__", "bisect", "collections", "dataclasses", "datetime", "decimal", "enum",
     "fractions", "hashlib", "itertools", "json", "typing"}
)  # fmt: skip
FORBIDDEN_NAMES = (
    "submit_order", "cancel_order", "place_entry", "place_plan_exit", "OrderManager",
    "Database", "sqlite3", "TRADING_ENABLED", "ALPACA_", "SecretStr", "ledger.sqlite3",
    "BrokerExecutionPort",
)  # fmt: skip
FORBIDDEN_CALLS = frozenset({"open", "exec", "eval", "__import__", "input", "print"})


def _modules() -> list[Path]:
    files = sorted(STRATEGY.glob("*.py"))
    assert {p.name for p in files} >= {"market_data.py", "support_resistance.py",
                                       "signals.py", "backtest.py"}  # fmt: skip
    return files


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_strategy_imports_only_pure_modules() -> None:
    for path in _modules():
        for name in _imports(path):
            if name.startswith("swing_trading_bot"):
                assert name.startswith(ALLOWED_INTERNAL), f"{path.name} imports {name}"
            else:
                assert name.split(".")[0] in ALLOWED_STDLIB, f"{path.name} imports {name}"


def test_strategy_source_has_no_execution_or_credential_paths() -> None:
    for path in _modules():
        text = path.read_text()
        for needle in FORBIDDEN_NAMES:
            assert needle not in text, f"{path.name} mentions {needle}"
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, f"{path.name} calls {node.func.id}"


def test_importing_the_strategy_loads_no_ledger_oms_or_broker_transport() -> None:
    code = (
        "import sys\n"
        "import swing_trading_bot.strategy.backtest\n"
        "import swing_trading_bot.strategy.signals\n"
        "bad = sorted(m for m in sys.modules if m.startswith(("
        "'swing_trading_bot.ledger', 'swing_trading_bot.oms', 'swing_trading_bot.app',"
        "'swing_trading_bot.broker.alpaca', 'swing_trading_bot.broker.sim',"
        "'swing_trading_bot.risk.pretrade', 'swing_trading_bot.config.settings',"
        "'swing_trading_bot.app', 'alpaca', 'sqlite3', 'requests', 'http.client', 'urllib3',"
        "'httpx', 'dotenv')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and inline code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == ""


def test_oms_does_not_depend_on_the_strategy() -> None:
    """The OMS must stay strategy-agnostic: no OMS/risk/ledger/app module imports it."""
    root = STRATEGY.parent
    for package in ("oms", "risk", "ledger", "app", "broker", "domain", "config"):
        for path in sorted((root / package).rglob("*.py")):
            for name in _imports(path):
                assert not name.startswith("swing_trading_bot.strategy"), f"{path} imports {name}"
