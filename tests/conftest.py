"""Test-wide safety rails.

* Network access is blocked for every test unless it carries the ``alpaca_paper``
  marker *and* ``RUN_ALPACA_PAPER_TESTS=1`` is set.
* Credential and trading environment variables are scrubbed so no test can pick up
  real values from the developer's shell. Tests build settings from explicit mappings.
* Hypothesis profiles: ``dev`` (default) and ``ci`` (derandomized, more examples).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest
from hypothesis import HealthCheck, settings

from swing_trading_bot.ledger.db import Database

settings.register_profile("dev", deadline=None, max_examples=200)
settings.register_profile(
    "ci",
    deadline=None,
    max_examples=1000,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

_SCRUBBED_PREFIXES = ("ALPACA_", "APCA_")
_SCRUBBED_NAMES = frozenset(
    {"TRADING_MODE", "TRADING_ENABLED", "INITIAL_CAPITAL", "MAX_CAPITAL_ALLOCATION", "STB_ENV_FILE"}
)

CONTRACT_TESTS_ENABLED = os.environ.get("RUN_ALPACA_PAPER_TESTS") == "1"
PERF_TESTS_ENABLED = os.environ.get("RUN_PERF") == "1"


class NetworkAccessBlockedError(RuntimeError):
    """Raised when a test attempts network I/O without opting in."""


def _blocked(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise NetworkAccessBlockedError(
        "Network access is disabled in tests. Mark the test with @pytest.mark.alpaca_paper "
        "and set RUN_ALPACA_PAPER_TESTS=1 to run read-only contract tests."
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    skip_contract = pytest.mark.skip(reason="contract test: set RUN_ALPACA_PAPER_TESTS=1 to run")
    skip_perf = pytest.mark.skip(reason="benchmark: set RUN_PERF=1 to run")
    for item in items:
        if "alpaca_paper" in item.keywords and not CONTRACT_TESTS_ENABLED:
            item.add_marker(skip_contract)
        if "perf" in item.keywords and not PERF_TESTS_ENABLED:
            item.add_marker(skip_perf)


PRODUCTION_DATA_DIR = (Path(__file__).resolve().parents[1] / "data").resolve()


class ProductionLedgerAccessError(RuntimeError):
    """A test tried to create or open a database under the repository's data/ dir."""


@pytest.fixture(autouse=True)
def _protect_production_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests may only use isolated temporary databases (simulation ledgers under
    tmp_path); the production ledger location is off limits."""
    original_create = Database.create.__func__  # type: ignore[attr-defined]
    original_open = Database.open.__func__  # type: ignore[attr-defined]

    def _guard(path: Path) -> None:
        resolved = Path(path).resolve()
        if resolved == PRODUCTION_DATA_DIR or PRODUCTION_DATA_DIR in resolved.parents:
            raise ProductionLedgerAccessError(f"tests must not touch {resolved}")

    def create(cls: type[Database], path: Path, *args: Any, **kwargs: Any) -> Database:
        _guard(path)
        return original_create(cls, path, *args, **kwargs)  # type: ignore[no-any-return]

    def open_(cls: type[Database], path: Path, *args: Any, **kwargs: Any) -> Database:
        _guard(path)
        return original_open(cls, path, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(Database, "create", classmethod(create))
    monkeypatch.setattr(Database, "open", classmethod(open_))


@pytest.fixture(autouse=True)
def _scrub_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("alpaca_paper") is not None and CONTRACT_TESTS_ENABLED:
        return  # an operator-authorized live run selects its credential source explicitly
    for name in list(os.environ):
        if name.startswith(_SCRUBBED_PREFIXES) or name in _SCRUBBED_NAMES:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _block_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("alpaca_paper") is not None and CONTRACT_TESTS_ENABLED:
        yield
        return
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    yield
