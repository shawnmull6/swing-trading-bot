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
from typing import Any, NoReturn

import pytest
from hypothesis import HealthCheck, settings

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
    {"TRADING_MODE", "TRADING_ENABLED", "INITIAL_CAPITAL", "MAX_CAPITAL_ALLOCATION"}
)

CONTRACT_TESTS_ENABLED = os.environ.get("RUN_ALPACA_PAPER_TESTS") == "1"


class NetworkAccessBlockedError(RuntimeError):
    """Raised when a test attempts network I/O without opting in."""


def _blocked(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise NetworkAccessBlockedError(
        "Network access is disabled in tests. Mark the test with @pytest.mark.alpaca_paper "
        "and set RUN_ALPACA_PAPER_TESTS=1 to run read-only contract tests."
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    if CONTRACT_TESTS_ENABLED:
        return
    skip = pytest.mark.skip(reason="contract test: set RUN_ALPACA_PAPER_TESTS=1 to run")
    for item in items:
        if "alpaca_paper" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _scrub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
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
