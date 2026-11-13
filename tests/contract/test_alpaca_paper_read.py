"""PYTEST_DONT_REWRITE

OPTIONAL supervised read-only contract run against the Alpaca PAPER API (Step 15B).

Skipped unless ``RUN_ALPACA_PAPER_TESTS=1``. **Do not run without explicit operator
authorization.** The single test delegates to :func:`tests.contract.live_run.run_supervised`
(one shared 120-request / 300-second budget; setup + broker-check + every check). A failure
is reported with ``pytrace=False`` and only fixed codes, so no broker data, identifier or
credential can appear in pytest output. Prefer the module entry point for the supervised
run: ``python -m tests.contract.live_run``.
"""

from __future__ import annotations

import os

import pytest

from tests.contract.live_run import fail_if_not_ok, run_supervised

pytestmark = pytest.mark.alpaca_paper


def test_supervised_read_only_contract_run() -> None:
    fail_if_not_ok(run_supervised(os.environ))
