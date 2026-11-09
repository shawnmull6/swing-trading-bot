"""OPTIONAL read-only contract checks against the Alpaca PAPER API (M1 CT items).

Skipped unless ``RUN_ALPACA_PAPER_TESTS=1`` (and then only these tests may open
sockets). Credentials are loaded with the approved loader (``load_settings`` on the
repository ``.env``) inside each test, never at import, and are never printed or logged.
Every call goes through the GET-only transport; nothing here can write.

Do not run without explicit operator authorization.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from swing_trading_bot.broker.activity_feed import collect_activities
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.config.settings import load_settings
from swing_trading_bot.domain.calendar import CalendarKind

pytestmark = pytest.mark.alpaca_paper
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@pytest.fixture
def adapter() -> AlpacaPaperReadAdapter:
    return AlpacaPaperReadAdapter.from_settings(load_settings(ENV_FILE, environ={}))


def test_ct1_account_and_config(adapter: AlpacaPaperReadAdapter) -> None:
    account = adapter.get_account()
    assert account.status
    config = adapter.get_account_config()
    assert config.max_margin_multiplier >= 1  # U6 reported, not required, in M1


def test_ct3_calendars(adapter: AlpacaPaperReadAdapter) -> None:
    start, end = date(2026, 11, 23), date(2026, 12, 4)
    trading = adapter.get_calendar(CalendarKind.TRADING, start, end)
    assert trading.problems() == []
    settlement = adapter.get_calendar(CalendarKind.SETTLEMENT, start, end)
    assert settlement.problems() == []
    assert date(2026, 11, 26) not in {s.session_date for s in trading.sessions}


def test_ct4_unknown_client_order_id_is_404(adapter: AlpacaPaperReadAdapter) -> None:
    assert adapter.get_order_by_client_id("stb-contract-test-does-not-exist") is None


def test_ct5_ct8_activities_paginate(adapter: AlpacaPaperReadAdapter) -> None:
    now = datetime.now(tz=UTC)
    collect_activities(adapter, after=now - timedelta(days=30), until=now, page_size=10)


def test_ct6_positions_and_orders(adapter: AlpacaPaperReadAdapter) -> None:
    adapter.get_positions()
    adapter.get_open_orders()


def test_ct9_market_data(adapter: AlpacaPaperReadAdapter) -> None:
    marks = adapter.get_marks(["SPY"])
    assert "SPY" in marks and marks["SPY"].as_of.tzinfo is not None
