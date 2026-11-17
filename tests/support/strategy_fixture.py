"""Deterministic, SYNTHETIC daily bars for strategy and backtest tests (Step 16.14).

No real market value is reproduced. The canonical setup is a sawtooth range: closes fall
from 56 to 48 and rise back in 12-session cycles, so swing lows cluster at 47.60
(support) and swing highs at 56.40 (resistance); the decision bar then tests support and
closes up (a bullish rejection). Sessions come from the offline fixture calendar.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from swing_trading_bot.broker.models import BarAdjustment, BarFeed, DailyBar
from swing_trading_bot.config.risk_limits import RiskLimits, load_risk_limits
from swing_trading_bot.domain.calendar import MARKET_TZ
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.strategy.market_data import (
    CashDividend,
    CorporateActions,
    CoverageWindow,
    DividendPolicy,
    SeriesPreparation,
    SessionCalendar,
    SplitEvent,
    prepare_series,
)
from tests.support.calendar_fixture import fixture_sessions

D = Decimal
ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
LIMITS: RiskLimits = load_risk_limits(ROOT / "config" / "risk.yaml")
ZERO_FEES = FeeModel(D(0), D(0), D(0), D(0), D(0))
REAL_FEES = FeeModel(D(0), D(0), D("0.000166"), D("8.30"), D("0.0000278"))
CAL_START = date(2026, 1, 2)
CAL_END = date(2026, 12, 31)
CALENDAR = SessionCalendar(fixture_sessions(CAL_START, CAL_END))
SESSIONS = CALENDAR.dates
VOLUME = 1_000_000
WICK = D("0.40")

OHLC = tuple[Decimal, Decimal, Decimal, Decimal]


def t_of(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=MARKET_TZ).astimezone(UTC)


def received(day: date) -> datetime:
    """An observation well after the session's close (complete)."""
    return datetime.combine(day, time(20, 0), tzinfo=MARKET_TZ).astimezone(UTC)


def bar(
    symbol: str,
    day: date,
    ohlc: OHLC,
    *,
    volume: int = VOLUME,
    feed: BarFeed = "sip",
    adjustment: BarAdjustment = "raw",
    received_at: datetime | None = None,
) -> DailyBar:
    o, h, lo, c = ohlc
    return DailyBar(symbol=symbol, session_date=day, open=o, high=h, low=lo, close=c,
                    volume=volume, vwap=None, trade_count=None, feed=feed,
                    adjustment=adjustment, as_of=t_of(day),
                    received_at=received_at or received(day))  # fmt: skip


def candle(open_: Decimal, close: Decimal, wick: Decimal = WICK) -> OHLC:
    return (open_, max(open_, close) + wick, min(open_, close) - wick, close)


def sawtooth_closes(cycles: int) -> list[Decimal]:
    """Closes 56 -> 48 -> 56 per 12-session cycle, then a final descent to 48."""
    step = D("8") / 6
    down = [D("56") - step * i for i in range(1, 7)]  # ... 48
    up = [D("48") + step * i for i in range(1, 7)]  # ... 56
    out: list[Decimal] = [D("56")]
    for _ in range(cycles):
        out += down + up
    out += down[:-1]  # stop one step above the trough: the decision bar tests support
    return [c.quantize(D("0.0001")) for c in out]


def range_ohlc(cycles: int = 6) -> list[OHLC]:
    closes = sawtooth_closes(cycles)
    rows = [candle(closes[0], closes[0])]
    for prev, c in itertools.pairwise(closes):
        rows.append(candle(prev, c))
    return rows


#: The decision bar: opens 48.20, dips to 47.50 (below the 47.60 zone), closes 49.00.
REJECTION_BAR: OHLC = (D("48.20"), D("49.20"), D("47.50"), D("49.00"))


@dataclass(frozen=True)
class Scenario:
    symbol: str
    ohlc: list[OHLC]
    start: date

    @property
    def days(self) -> tuple[date, ...]:
        i = SESSIONS.index(self.start)
        return SESSIONS[i : i + len(self.ohlc)]

    @property
    def decision_session(self) -> date:
        return self.days[self.decision_index]

    decision_index: int = -1

    def bars(self, *, skip: Iterable[date] = (), volume: int = VOLUME) -> list[DailyBar]:
        skipped = set(skip)
        rows = zip(self.days, self.ohlc, strict=True)
        return [bar(self.symbol, d, row, volume=volume) for d, row in rows if d not in skipped]


def setup_scenario(
    symbol: str = "SRX",
    *,
    future: Sequence[OHLC] = (),
    decision_bar: OHLC = REJECTION_BAR,
    start: date = date(2026, 3, 2),
    cycles: int = 6,
) -> Scenario:
    """The canonical accepted setup, decided on its ``decision_bar``, then ``future``."""
    rows = [*range_ohlc(cycles), decision_bar]
    sc = Scenario(symbol, [*rows, *future], start, decision_index=len(rows) - 1)
    if len(sc.days) != len(sc.ohlc):
        raise ValueError("scenario runs past the fixture calendar")
    return sc


def attested(
    symbol: str,
    splits: Sequence[SplitEvent] = (),
    dividends: Sequence[CashDividend] = (),
    *,
    start: date = CAL_START,
    end: date = CAL_END,
) -> CorporateActions:
    """A corporate-action record whose split and dividend lists are attested complete
    for ``start..end`` (the whole fixture calendar by default)."""
    window = CoverageWindow(start, end, "fixture:attested")
    return CorporateActions(symbol, tuple(splits), tuple(dividends), window, window)


def prepared(
    sc: Scenario,
    *,
    skip: Iterable[date] = (),
    splits: Sequence[SplitEvent] = (),
    dividends: Sequence[CashDividend] = (),
    volume: int = VOLUME,
    dividend_policy: DividendPolicy = DividendPolicy.REQUIRE_COVERAGE,
) -> SeriesPreparation:
    return prepare_series(sc.symbol, sc.bars(skip=skip, volume=volume), calendar=CALENDAR,
                          corporate_actions=attested(sc.symbol, splits, dividends),
                          dividend_policy=dividend_policy)  # fmt: skip


def next_session(d: date, n: int = 1) -> date:
    return SESSIONS[SESSIONS.index(d) + n]


def after(d: date, hours: int = 0) -> datetime:
    return datetime.combine(d, time(16, 0), tzinfo=MARKET_TZ).astimezone(UTC) + timedelta(
        hours=hours
    )
