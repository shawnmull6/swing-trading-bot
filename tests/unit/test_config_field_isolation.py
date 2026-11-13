"""Step 15B.4 structural guard: ``pdt_check`` / ``dtbp_check`` are audit-only.

They may be declared (``broker/models.py``), parsed (``broker/mapping.py``), simulated
(``broker/sim.py``) and recorded verbatim in a new broker baseline by ``init-ledger``
(``app/init_ledger.py``, inside ``_build`` only, which writes the baseline). No gate, risk,
pre-trade, reconciliation, rebaseline, order-management, runtime, ledger or domain code
may read them, so an unknown (``None``) value can neither grant a permission nor weaken
a restriction.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
TOKENS = ("pdt_check", "dtbp_check")
ALLOWED = {"broker/models.py", "broker/mapping.py", "broker/sim.py", "app/init_ledger.py"}
FORBIDDEN_AREAS = ("risk", "oms", "ledger", "domain", "observability", "config",
                   "app/runtime.py", "app/commands.py", "app/cli.py", "app/simulation.py",
                   "app/broker_factory.py", "broker/alpaca_read.py", "broker/alpaca_http.py",
                   "broker/activity_feed.py")  # fmt: skip


def _files(area: str) -> list[Path]:
    root = PKG / area
    if root.is_file():
        return [root]
    return [p for p in root.rglob("*") if p.suffix in {".py", ".sql"}]


def _mentions(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return any(t in text for t in TOKENS)


def test_tokens_appear_only_in_allowed_files() -> None:
    hits = {
        p.relative_to(PKG).as_posix()
        for p in PKG.rglob("*")
        if p.suffix in {".py", ".sql"} and _mentions(p)
    }
    assert hits <= ALLOWED, sorted(hits - ALLOWED)


@pytest.mark.parametrize("area", FORBIDDEN_AREAS)
def test_decision_areas_never_mention_the_fields(area: str) -> None:
    files = _files(area)
    assert files, area
    assert [p.relative_to(PKG).as_posix() for p in files if _mentions(p)] == []


def _functions_mentioning(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            attr = isinstance(inner, ast.Attribute) and inner.attr in TOKENS
            const = isinstance(inner, ast.Constant) and inner.value in TOKENS
            if attr or const:
                found.add(node.name)
    return found


def test_init_ledger_only_records_them_in_the_baseline() -> None:
    # ``_build`` writes the broker baseline; nothing else in init-ledger reads them.
    assert _functions_mentioning(PKG / "app" / "init_ledger.py") == {"_build"}


def test_mapping_reads_them_only_in_the_config_mapper() -> None:
    assert _functions_mentioning(PKG / "broker" / "mapping.py") == {"account_config_from_raw"}


def test_only_these_two_config_fields_use_opt_text() -> None:
    tree = ast.parse((PKG / "broker" / "mapping.py").read_text())
    mapper = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "account_config_from_raw"
    )
    optional = sorted(
        str(c.args[1].value)
        for c in ast.walk(mapper)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "opt_text"
        and isinstance(c.args[1], ast.Constant)
    )  # fmt: skip
    assert optional == sorted(TOKENS)
