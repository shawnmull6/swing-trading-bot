"""Step 15B.2 structural guard: ``pattern_day_trader`` is informational only.

It may be declared (``broker/models.py``), parsed (``broker/mapping.py``), simulated
(``broker/sim.py``) and reported by ``broker-check`` (``app/commands.py``, inside
``cmd_broker_check`` only). Risk, pre-trade, gates, runtime/identity, reconciliation,
order management, ledger persistence and the domain must never read it, so an unknown
(``None``) value can neither grant a permission nor bypass a gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
TOKEN = "pattern_day_trader"
ALLOWED_FILES = {"broker/models.py", "broker/mapping.py", "broker/sim.py", "app/commands.py"}
SAFETY_CRITICAL = ("risk", "oms", "ledger", "domain", "app/runtime.py", "app/init_ledger.py",
                   "app/broker_factory.py", "app/simulation.py", "broker/activity_feed.py",
                   "broker/alpaca_read.py", "broker/alpaca_http.py")  # fmt: skip


def _rel(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


def _mentions(path: Path) -> bool:
    return TOKEN in path.read_text(encoding="utf-8")


def test_token_appears_only_in_allowed_files() -> None:
    hits = {_rel(p) for p in PKG.rglob("*") if p.suffix in {".py", ".sql"} and _mentions(p)}
    assert hits <= ALLOWED_FILES, sorted(hits - ALLOWED_FILES)


@pytest.mark.parametrize("area", SAFETY_CRITICAL)
def test_safety_critical_area_never_mentions_pdt(area: str) -> None:
    root = PKG / area
    files = [root] if root.is_file() else [p for p in root.rglob("*")
                                           if p.suffix in {".py", ".sql"}]  # fmt: skip
    assert files, area
    assert [_rel(p) for p in files if _mentions(p)] == []


def test_ledger_schema_does_not_persist_pdt() -> None:
    sql = "\n".join(p.read_text() for p in (PKG / "ledger" / "migrations").glob("*.sql"))
    assert sql and TOKEN not in sql and "day_trad" not in sql


def _functions_reading(tree: ast.AST) -> set[str]:
    """Names of top-level functions containing ``<expr>.pattern_day_trader`` or the
    string ``"pattern_day_trader"``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            attr = isinstance(inner, ast.Attribute) and inner.attr == TOKEN
            const = isinstance(inner, ast.Constant) and inner.value == TOKEN
            if attr or const:
                found.add(node.name)
    return found


def test_commands_reads_pdt_only_inside_broker_check() -> None:
    tree = ast.parse((PKG / "app" / "commands.py").read_text())
    assert _functions_reading(tree) == {"cmd_broker_check"}


def test_mapping_reads_pdt_only_in_the_account_mapper() -> None:
    tree = ast.parse((PKG / "broker" / "mapping.py").read_text())
    assert _functions_reading(tree) == {"account_from_raw"}


def test_mapper_uses_the_optional_boolean_for_pdt_only() -> None:
    """Only ``pattern_day_trader`` and the three M2-1 restriction fields (A§12.34; unknown
    there is fail-closed for entries, never false) go through ``opt_boolean``; nothing
    else was relaxed."""
    tree = ast.parse((PKG / "broker" / "mapping.py").read_text())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "opt_boolean"
    ]  # fmt: skip
    keys = [c.args[1].value for c in calls if isinstance(c.args[1], ast.Constant)]
    assert keys == [TOKEN, "trade_suspended_by_user", "transfers_blocked",
                    "closing_transactions_only"]  # fmt: skip
