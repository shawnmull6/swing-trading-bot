"""OFFLINE contract replay (always runs). Every check of the live contract suite runs here
against SYNTHETIC payloads served through the real HTTPS GET transport, plus the
fail-closed behaviours that cannot be provoked read-only. This verifies the code's
handling only; it is not evidence about Alpaca's behaviour (see ``matrix.py``)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app.credentials import load_credentials
from swing_trading_bot.broker.activity_feed import collect_window
from swing_trading_bot.broker.alpaca_http import AlpacaReadTransport
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    IncompleteRetrievalError,
    InvalidBrokerDataError,
    RateLimitedError,
    ServiceUnavailableError,
    UnknownBrokerStatusError,
    UnsupportedBrokerOperationError,
)
from swing_trading_bot.observability.logging import SecretRedactor
from swing_trading_bot.oms.lifecycle import read_error_halt, read_error_outage_category
from tests.contract import checks as contract_checks
from tests.contract.checks import CHECKS, ContractContext
from tests.contract.matrix import MATRIX
from tests.contract.supervisor import (
    BudgetedSession,
    EvidenceWriter,
    RequestBudget,
    WriteAttemptError,
    assert_sanitized,
)
from tests.support.alpaca_account import (
    ACCOUNT_ID,
    START,
    AccountSession,
    Clock,
    FakeAlpacaAccount,
)
from tests.support.alpaca_fake import order

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_paper_account.json"
ENV = {
    "ALPACA_API_KEY": "PKSYNTHETICKEY0000000001",
    "ALPACA_SECRET_KEY": "synthetic/Secret/Value/NotReal/0000000000001",
    "TRADING_MODE": "paper",
}
SECRETS = [ENV["ALPACA_API_KEY"], ENV["ALPACA_SECRET_KEY"]]


def synthetic_account() -> FakeAlpacaAccount:
    acct = FakeAlpacaAccount(clock=Clock(START))
    doc = json.loads(FIXTURE.read_text())
    assert "SYNTHETIC" in doc["_notice"]
    for raw in doc["activities"]:
        row = dict(raw)
        posted = START - timedelta(hours=row.pop("_posted_hours_ago"))
        acct.clock.now = posted
        acct.post(row)
    acct.clock.now = START
    acct.prices["SPY"] = Decimal("512.34")
    return acct


def wire(acct: FakeAlpacaAccount) -> tuple[ContractContext, BudgetedSession]:
    spy = BudgetedSession(AccountSession(acct), RequestBudget(), SecretRedactor(SECRETS))
    transport = AlpacaReadTransport(load_credentials(ENV).settings, session=spy)  # type: ignore[arg-type]
    adapter = AlpacaPaperReadAdapter(transport, clock=acct.clock, sleep=lambda _d: None)
    return ContractContext(adapter, transport, now=START), spy


def forbidden(acct: FakeAlpacaAccount) -> list[str]:
    return [*SECRETS, acct.account_id, "PA0000", str(acct.cash)]


# ============================================================ every live check, offline
def test_checks_pass_on_synthetic_account(tmp_path: Path) -> None:
    acct = synthetic_account()
    ctx, spy = wire(acct)
    summary: dict[str, Any] = {"synthetic": True, "checks": {}}
    for name, check in sorted(CHECKS.items()):
        summary["checks"][name] = check(ctx)
    obs = summary["checks"]
    assert obs["activity_pagination"]["page_size_10_equals_100"] is True
    assert obs["activity_pagination"]["adjacent_windows_partition_the_whole"] is True
    assert {"INT", "CSD", "FILL"} <= set(obs["activity_pagination"]["activity_types_seen"])
    assert obs["calendars"]["sessions_differing_from_fixture"] == []
    assert obs["market_data"]["daily_bar_t_is_midnight_et"] is True
    assert obs["raw_decimals"]["account_money_fields_are_strings"] is True
    assert spy.exchanges and all(e.method == "GET" for e in spy.exchanges)
    written = EvidenceWriter(tmp_path, run="replay", require_git_ignored=False).write(
        spy.exchanges, summary, forbidden(acct)
    )
    text = written.read_text()
    for value in forbidden(acct):
        assert value not in text  # the sanitized summary carries no identifiers/balances
    assert (written.parent / "raw").is_dir()


def test_identity_violations_fail_closed() -> None:
    for override in ({"id": "PA123456"}, {"status": "ACCOUNT_CLOSED"}, {"currency": "CAD"}):
        acct = synthetic_account()
        acct.account_overrides.update(override)
        ctx, _ = wire(acct)
        with pytest.raises(contract_checks.ContractViolation, match="ACCOUNT_IDENTITY"):
            contract_checks.check_account_identity(ctx)


def test_float_money_is_rejected() -> None:
    acct = synthetic_account()
    acct.account_overrides["cash"] = 100000.5  # a JSON number, not a string
    ctx, _ = wire(acct)
    with pytest.raises(InvalidBrokerDataError):
        ctx.adapter.get_account()
    acct = synthetic_account()
    acct.open_orders.append(order("stb-x", qty=10))  # type: ignore[arg-type]
    ctx, _ = wire(acct)
    with pytest.raises(InvalidBrokerDataError):
        ctx.adapter.get_open_orders()


@pytest.mark.parametrize(
    ("path", "body", "error"),
    [
        ("/account", "not json", InvalidBrokerDataError),
        ("/clock", json.dumps({"timestamp": "2026-11-09T10:00:00", "is_open": True,
                               "next_open": "x", "next_close": "y"}), InvalidBrokerDataError),
        ("/orders", json.dumps([order("stb-1", status="teleported")]), UnknownBrokerStatusError),
        ("/positions", json.dumps([{"symbol": "AAPL"}]), InvalidBrokerDataError),
    ],
)  # fmt: skip
def test_malformed_payloads_fail_closed(path: str, body: str, error: type[Exception]) -> None:
    acct = synthetic_account()
    acct.transport.always("trading", path, body)
    ctx, _ = wire(acct)
    call: dict[str, Callable[[], object]] = {
        "/account": ctx.adapter.get_account,
        "/clock": ctx.adapter.get_clock,
        "/orders": ctx.adapter.get_open_orders,
        "/positions": ctx.adapter.get_positions,
    }
    with pytest.raises(error):
        call[path]()


@pytest.mark.parametrize(
    ("raised", "expected", "category", "halt"),
    [
        (AuthenticationError("401", status_code=401), AuthenticationError, None, "AUTH"),
        (RateLimitedError("429"), RateLimitedError, "RATE_LIMITED", None),
        (ServiceUnavailableError("503", status_code=503), ServiceUnavailableError,
         "SERVER_ERROR", None),
        (ServiceUnavailableError("t", kind="timeout"), ServiceUnavailableError, "TIMEOUT", None),
    ],
)  # fmt: skip
def test_error_classification(
    raised: BrokerError, expected: type[BrokerError], category: str | None, halt: str | None
) -> None:
    acct = synthetic_account()
    acct.down["/account"] = lambda: raised
    ctx, spy = wire(acct)
    with pytest.raises(expected) as info:
        ctx.adapter.get_account()
    assert read_error_outage_category(info.value) == category
    reason = read_error_halt(info.value)
    assert (reason.value if reason else None) == halt
    retried = sum(e.path.endswith("/account") for e in spy.exchanges)
    assert retried == (1 if halt else 3)  # auth is never retried; transient reads 3x
    assert ctx.adapter.get_order_by_client_id("stb-missing") is None  # 404 → None


def test_exact_page_boundary_needs_one_more_request() -> None:
    acct = synthetic_account()
    acct.activities.clear()
    for _ in range(100):
        acct.clock.now = START - timedelta(hours=2)
        acct.deposit("1.00")
    acct.clock.now = START
    ctx, spy = wire(acct)
    window = collect_window(ctx.adapter, after=START - timedelta(days=1),
                            until=START - timedelta(minutes=20), now=START,
                            posting_lag=timedelta(minutes=15))  # fmt: skip
    pages = [e for e in spy.exchanges if e.path.endswith("/account/activities")]
    assert len(window.activities) == 100 and window.tail_confirmed and len(pages) == 2


def test_non_advancing_cursor_fails_closed() -> None:
    acct = synthetic_account()
    page = [r for _p, r in acct.activities][:10]
    acct.transport.always("trading", "/account/activities", json.dumps(page))
    ctx, _ = wire(acct)
    with pytest.raises((IncompleteRetrievalError, InvalidBrokerDataError)):
        collect_window(ctx.adapter, after=START - timedelta(days=60), until=START,
                       now=START, posting_lag=timedelta(minutes=15), page_size=10)  # fmt: skip


def test_official_closes_are_unsupported() -> None:
    ctx, spy = wire(synthetic_account())
    with pytest.raises(UnsupportedBrokerOperationError):
        ctx.adapter.get_official_closes(["SPY"], date(2026, 11, 6))
    assert spy.exchanges == []  # refused without a request


# ============================================================ harness integrity
def test_spy_refuses_writes_and_foreign_hosts() -> None:
    spy = BudgetedSession(AccountSession(synthetic_account()), RequestBudget(), SecretRedactor())
    for verb in ("post", "put", "patch", "delete", "request", "send"):
        with pytest.raises(WriteAttemptError):
            getattr(spy, verb)
    with pytest.raises(WriteAttemptError):
        spy.get("https://api.alpaca.markets/v2/account", allow_redirects=False)
    with pytest.raises(WriteAttemptError):
        spy.get("http://paper-api.alpaca.markets/v2/account", allow_redirects=False)
    with pytest.raises(WriteAttemptError):  # redirects must be disabled by the caller
        spy.get("https://paper-api.alpaca.markets/v2/account")
    assert spy.budget.used == 0  # refused before counting or sending


def test_evidence_writer_refuses_unsanitized_summaries(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="forbidden"):
        EvidenceWriter(tmp_path, run="leak", require_git_ignored=False).write(
            [], {"id": ACCOUNT_ID}, [ACCOUNT_ID]
        )
    assert not (tmp_path / "leak").exists()
    assert_sanitized({"ok": True}, [ACCOUNT_ID])


def test_matrix_references_real_checks_and_tests() -> None:
    names = set(globals())
    for row in MATRIX:
        assert row.check is None or row.check in CHECKS, row.id
        assert row.replay_tests, row.id
        for test in row.replay_tests:
            assert test in names, (row.id, test)
    assert {r.id for r in MATRIX} == {f"C{i}" for i in range(1, 14)}
