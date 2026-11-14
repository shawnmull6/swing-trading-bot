"""M2-2a: the DailyBar model and the Alpaca daily-bar mapper (exact, fail-closed)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from swing_trading_bot.broker.alpaca_http import decode_json
from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.broker.mapping import collect_daily_bars, daily_bar_from_raw
from swing_trading_bot.broker.models import DailyBar
from tests.support.bars_fixture import bar_row, bars_json, t_of

D = Decimal
DAY = date(2026, 11, 9)
RECEIVED = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)


def good(**changes: Any) -> DailyBar:
    bar = daily_bar_from_raw(bar_row(DAY, D("150.25")), "AAPL", feed="sip",
                             adjustment="raw", received_at=RECEIVED)  # fmt: skip
    return replace(bar, **changes) if changes else bar


def parse(row: dict[str, Any], **kw: Any) -> DailyBar:
    opts: dict[str, Any] = {"feed": "iex", "adjustment": "raw", "received_at": RECEIVED}
    opts.update(kw)
    return daily_bar_from_raw(row, "AAPL", **opts)


# ============================================================ model
def test_exact_decimal_values_from_json_numbers() -> None:
    text = bars_json({"bar": {**bar_row(DAY, D("150.25")), "c": D("150.123456789"),
                              "vw": D("150.30000000001")}})  # fmt: skip
    raw = decode_json(text)
    assert isinstance(raw, dict)
    bar = parse(raw["bar"])
    assert bar.close == D("150.123456789") and isinstance(bar.close, Decimal)
    assert bar.vwap == D("150.30000000001")
    assert (bar.open, bar.high, bar.low) == (D("150.25"), D("151.50"), D("149.50"))
    assert bar.volume == 1_000_000 and bar.trade_count == 5_000
    assert bar.session_date == DAY and bar.as_of.tzinfo is not None


def test_integer_prices_are_exact() -> None:
    row = {**bar_row(DAY, D(10)), "o": 10, "h": 12, "l": 9, "c": 11}
    bar = parse(row)
    assert (bar.open, bar.high, bar.low, bar.close) == (D(10), D(12), D(9), D(11))


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "vwap"])
@pytest.mark.parametrize("value", [D(0), D(-1), D("NaN"), D("Infinity"), 1.5, "1.5", None])
def test_model_rejects_bad_prices(field: str, value: object) -> None:
    if field == "vwap" and value is None:
        assert good(vwap=None).vwap is None  # optional
        return
    with pytest.raises(ValueError, match="finite, positive Decimal"):
        good(**{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"low": D("150.30")},  # low above open
        {"high": D("150.30")},  # high below close
        {"open": D("151.60")},  # open above high
        {"close": D("149.40")},  # close below low
        {"low": D("152"), "high": D("151")},
    ],
)
def test_model_rejects_inconsistent_ohlc(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="OHLC"):
        good(**changes)


def test_equal_ohlc_is_consistent() -> None:
    bar = good(open=D(5), high=D(5), low=D(5), close=D(5), vwap=D(5))
    assert bar.high == bar.low


@pytest.mark.parametrize(("field", "value"), [("volume", -1), ("volume", True),
                                              ("volume", D(1)), ("trade_count", -2),
                                              ("trade_count", False)])  # fmt: skip
def test_model_rejects_bad_counts(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        good(**{field: value})


@pytest.mark.parametrize(("field", "value"), [("feed", "delayed_sip"), ("feed", "otc"),
                                              ("adjustment", "all"), ("adjustment", "dividend"),
                                              ("symbol", ""), ("symbol", " AAPL")])  # fmt: skip
def test_model_rejects_unsupported_labels(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=r"unsupported bar|symbol must be"):
        good(**{field: value})


def test_model_requires_aware_timestamps_on_the_session_date() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        good(as_of=datetime(2026, 11, 9, 10, 0, tzinfo=UTC).replace(tzinfo=None))
    with pytest.raises(ValueError, match="session_date"):
        good(session_date=date(2026, 11, 10))
    with pytest.raises(ValueError, match="not a datetime"):
        good(session_date=datetime(2026, 11, 9, tzinfo=UTC))


# ============================================================ mapper
@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"o": 150.25}, "exact number"),  # binary float never accepted
        ({"c": "abc"}, "c"),
        ({"h": True}, "exact number"),
        ({"v": 10.5}, "JSON integer"),
        ({"v": D("10")}, "JSON integer"),
        ({"v": "1000"}, "JSON integer"),
        ({"n": 1.0}, "JSON integer"),
        ({"vw": 1.5}, "exact number"),
        ({"t": "2026-11-09T05:00:00"}, "offset"),
        ({"t": "yesterday"}, "ISO-8601"),
        ({"l": D("152")}, "OHLC"),
        ({"c": D("0")}, "positive"),
    ],
)
def test_mapper_rejects_malformed_fields(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match=message):
        parse({**bar_row(DAY, D("150.25")), **patch})


@pytest.mark.parametrize("key", ["t", "o", "h", "l", "c", "v"])
def test_mapper_requires_core_fields(key: str) -> None:
    row = {k: v for k, v in bar_row(DAY, D("150.25")).items() if k != key}
    with pytest.raises(InvalidBrokerDataError, match="missing"):
        parse(row)


def test_mapper_accepts_missing_or_null_optional_fields() -> None:
    bare = parse(bar_row(DAY, D("150.25"), optional=False))
    assert (bare.vwap, bare.trade_count) == (None, None)
    nulls = parse({**bar_row(DAY, D("150.25")), "vw": None, "n": None})
    assert (nulls.vwap, nulls.trade_count) == (None, None)


def test_mapper_labels_feed_and_adjustment_explicitly() -> None:
    bar = parse(bar_row(DAY, D("150.25")), feed="sip", adjustment="split")
    assert (bar.feed, bar.adjustment) == ("sip", "split")


def test_session_date_comes_from_t_in_market_time() -> None:
    assert t_of(DAY) == "2026-11-09T05:00:00Z"  # EST
    assert parse(bar_row(DAY, D(1))).session_date == DAY
    summer = date(2026, 7, 6)
    assert t_of(summer) == "2026-07-06T04:00:00Z"  # EDT
    assert parse(bar_row(summer, D(1))).session_date == summer


# ============================================================ sequencing
def rows(*days: date, symbol: str = "AAPL") -> list[tuple[str, dict[str, Any], datetime]]:
    return [(symbol, bar_row(d, D(10)), RECEIVED) for d in days]


def test_collect_returns_every_requested_symbol_in_order() -> None:
    a, b = date(2026, 11, 9), date(2026, 11, 10)
    out = collect_daily_bars(rows(a, b), ["AAPL", "MSFT"], a, b, feed="iex", adjustment="raw")
    assert [x.session_date for x in out["AAPL"]] == [a, b] and out["MSFT"] == ()


@pytest.mark.parametrize(
    ("days", "message"),
    [
        ((date(2026, 11, 10), date(2026, 11, 9)), "out-of-order"),
        ((date(2026, 11, 9), date(2026, 11, 9)), "duplicate"),
        ((date(2026, 11, 6),), "outside"),
        ((date(2026, 11, 11),), "outside"),
    ],
)
def test_collect_fails_closed(days: tuple[date, ...], message: str) -> None:
    with pytest.raises(InvalidBrokerDataError, match=message):
        collect_daily_bars(rows(*days), ["AAPL"], date(2026, 11, 9), date(2026, 11, 10),
                           feed="iex", adjustment="raw")  # fmt: skip


def test_collect_rejects_unrequested_symbol() -> None:
    with pytest.raises(InvalidBrokerDataError, match="unrequested"):
        collect_daily_bars(rows(DAY, symbol="TSLA"), ["AAPL"], DAY, DAY, feed="iex",
                           adjustment="raw")  # fmt: skip
