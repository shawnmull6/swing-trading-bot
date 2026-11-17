"""SYNTHETIC multi-year research fixtures (Step 16.15). No real market value is
reproduced and the calendar is not authoritative: weekdays minus three fixed holidays,
settlement on the next session. For tests of the dataset, walk-forward and evaluation
infrastructure only.

Prices follow a deterministic sawtooth range (as in ``strategy_fixture``) with a per-symbol
scale and phase, so support/resistance setups recur and symbols compete for capacity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from swing_trading_bot.broker.models import BarAdjustment, DailyBar
from swing_trading_bot.domain.calendar import MARKET_TZ, TradingSession
from swing_trading_bot.research.dataset import (
    ACQUISITION_COMPLETE,
    CorporateActionRecord,
    Coverage,
    DistributionKind,
    Evidence,
    EvidenceKind,
    OtherEvent,
    SymbolIdentity,
    TickerSpan,
    encode_bar,
    encode_calendar,
    encode_corporate_actions,
    encode_identity,
    encode_jsonl,
    sha256,
    write_dataset,
)
from swing_trading_bot.strategy.market_data import CashDividend, SplitEvent

D = Decimal
SYN_START = date(2020, 7, 1)
SYN_END = date(2026, 10, 31)
RESEARCH_START = date(2021, 1, 1)
CYCLE = 12
VOLUME = 1_000_000
WICK = D("0.40")
UNIT = D(1)
BATCH = "fixture-batch-1"
CAL_BATCH = "fixture-calendar-1"
ACQUIRED = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _holiday(d: date) -> bool:
    return (d.month, d.day) in {(1, 1), (7, 4), (12, 25)}


def synthetic_sessions(start: date = SYN_START, end: date = SYN_END) -> tuple[TradingSession, ...]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and not _holiday(d):
            days.append(d)
        d += timedelta(days=1)
    out = []
    for i, day in enumerate(days):
        settle = days[i + 1] if i + 1 < len(days) else day + timedelta(days=3)
        while settle.weekday() >= 5:
            settle += timedelta(days=1)
        out.append(TradingSession(
            session_date=day,
            open_at=datetime.combine(day, time(9, 30), tzinfo=MARKET_TZ).astimezone(UTC),
            close_at=datetime.combine(day, time(16, 0), tzinfo=MARKET_TZ).astimezone(UTC),
            settlement_date=settle,
        ))  # fmt: skip
    return tuple(out)


SESSIONS = synthetic_sessions()
DATES = tuple(s.session_date for s in SESSIONS)


def _close(i: int, phase: int) -> Decimal:
    p = (i + phase) % CYCLE
    if p == 7:  # the bar after the trough: a bullish rejection close to support
        return D("49.0000")
    step = D(8) / 6
    level = D(56) - step * p if p <= 6 else D(48) + step * (p - 6)
    return level.quantize(D("0.0001"))


def sawtooth(n: int, *, scale: Decimal = UNIT, phase: int = 0) -> list[tuple[Decimal, ...]]:
    """``n`` OHLC rows on a 56..48 sawtooth (times ``scale``)."""
    rows = []
    prev = _close(0, phase)
    for i in range(n):
        c = _close(i, phase)
        o = prev
        rows.append(tuple((x * scale).quantize(D("0.0001")) for x in
                          (o, max(o, c) + WICK, min(o, c) - WICK, c)))  # fmt: skip
        prev = c
    return rows


def daily_bar(symbol: str, day: date, ohlc: Sequence[Decimal], *, volume: int = VOLUME,
              adjustment: BarAdjustment = "raw") -> DailyBar:  # fmt: skip
    o, h, lo, c = ohlc
    return DailyBar(symbol=symbol, session_date=day, open=o, high=h, low=lo, close=c,
                    volume=volume, vwap=None, trade_count=None, feed="sip",
                    adjustment=adjustment,
                    as_of=datetime.combine(day, time(0, 0), tzinfo=MARKET_TZ).astimezone(UTC),
                    received_at=ACQUIRED)  # fmt: skip


def synthetic_bars(symbol: str, *, scale: Decimal = UNIT, phase: int = 0,
                   dates: Sequence[date] = DATES) -> tuple[DailyBar, ...]:  # fmt: skip
    rows = sawtooth(len(dates), scale=scale, phase=phase)
    return tuple(daily_bar(symbol, d, r) for d, r in zip(dates, rows, strict=True))


def evidence(kind: EvidenceKind = EvidenceKind.OPERATOR_REVIEW) -> Evidence:
    return Evidence(kind, "fixture:reviewer", f"fixture-{kind.value.lower()}-1")


def full_coverage(*kinds: EvidenceKind, start: date = SYN_START,
                  end: date = SYN_END) -> Coverage:  # fmt: skip
    """Coverage of the whole synthetic window, by default with operator-review evidence."""
    chosen = kinds or (EvidenceKind.OPERATOR_REVIEW,)
    return Coverage(start, end, tuple(evidence(k) for k in chosen))


def record(
    symbol: str,
    *,
    splits: Sequence[SplitEvent] = (),
    dividends: Sequence[CashDividend] = (),
    others: Sequence[OtherEvent] = (),
    split_cov: Coverage | None = None,
    div_cov: Coverage | None = None,
    other_cov: Coverage | None = None,
    attest: bool = True,
) -> CorporateActionRecord:
    cov = full_coverage() if attest else None
    return CorporateActionRecord(
        symbol, tuple(splits), (), tuple(dividends),
        tuple((d.ex_session, DistributionKind.CASH_DIVIDEND) for d in dividends), tuple(others),
        split_cov or cov, div_cov or cov, other_cov or cov,
    )  # fmt: skip


def identity(symbol: str, spans: Sequence[TickerSpan] | None = None) -> SymbolIdentity:
    return SymbolIdentity(symbol, tuple(spans or (TickerSpan(symbol, SYN_START, None),)),
                          None, None)  # fmt: skip


@dataclass
class FixtureSpec:
    bars: dict[str, Sequence[DailyBar]]
    records: dict[str, CorporateActionRecord | None]
    identities: dict[str, SymbolIdentity] = field(default_factory=dict)
    split_bars: dict[str, Sequence[DailyBar]] = field(default_factory=dict)
    source_symbols: dict[str, Mapping[date, str]] = field(default_factory=dict)
    symbols: tuple[str, ...] | None = None
    sessions: Sequence[TradingSession] = SESSIONS


def default_spec(symbols: Sequence[str] = ("AAA", "BBB", "CCC")) -> FixtureSpec:
    bars: dict[str, Sequence[DailyBar]] = {
        s: synthetic_bars(s, scale=D(1) + D(i) / 2, phase=i * 3) for i, s in enumerate(symbols)
    }
    return FixtureSpec(bars=bars, records={s: record(s) for s in symbols},
                       identities={s: identity(s) for s in symbols})  # fmt: skip


def manifest_meta(symbols: Sequence[str]) -> dict[str, object]:
    return {
        "dataset_id": "fixture-synthetic-v1",
        "created_at": ACQUIRED.isoformat(),
        "description": "SYNTHETIC test dataset",
        "market_timezone": "America/New_York",
        "feed": "sip",
        "research_window": {"start": str(RESEARCH_START), "end": str(SYN_END)},
        "warmup_start": str(SYN_START),
        "universe": {"id": "fixture", "policy": "FIXED_PRESENT_DAY_PILOT",
                     "symbols": sorted(symbols), "survivorship_bias_free": False},
        "acquisitions": [
            {"batch_id": BATCH, "kind": "bars", "provider": "fixture",
             "endpoint": "GET /v2/stocks/bars", "params": {"feed": "sip"},
             "requested_at": ACQUIRED.isoformat(), "received_at": ACQUIRED.isoformat(),
             "request_count": 0},
            {"batch_id": CAL_BATCH, "kind": "calendar", "provider": "fixture",
             "endpoint": "GET /v2/calendar", "params": {},
             "requested_at": ACQUIRED.isoformat(), "received_at": ACQUIRED.isoformat(),
             "request_count": 0},
        ],
        "acquisition_report": acquisition_report(),
    }  # fmt: skip


def acquisition_report(
    state: str = ACQUISITION_COMPLETE, batches: Sequence[str] = (BATCH, CAL_BATCH)
) -> dict[str, object]:
    """A fixture's record of a complete acquisition (synthetic page checksums)."""
    return {"acquisition_state": state,
            "required_batches": {b: {"pages_sha256": [sha256(b.encode())], "attempts": 1}
                                 for b in batches}}  # fmt: skip


def build_files(spec: FixtureSpec) -> tuple[dict[str, object], dict[str, bytes]]:
    symbols = spec.symbols or tuple(sorted(spec.bars))
    files: dict[str, bytes] = {
        "calendar.json": encode_calendar(spec.sessions, start=SYN_START, end=SYN_END,
                                         source="fixture:synthetic", batch_id=CAL_BATCH),
        "identity.json": encode_identity([spec.identities[s] for s in sorted(spec.identities)]),
    }  # fmt: skip
    for sym, bars in spec.bars.items():
        names = spec.source_symbols.get(sym, {})
        files[f"bars/{sym}.raw.jsonl"] = encode_jsonl(
            [encode_bar(b, source_symbol=names.get(b.session_date, sym), batch_id=BATCH)
             for b in bars])  # fmt: skip
    for sym, bars in spec.split_bars.items():
        names = spec.source_symbols.get(sym, {})
        files[f"bars/{sym}.split.jsonl"] = encode_jsonl(
            [encode_bar(b, source_symbol=names.get(b.session_date, sym), batch_id=BATCH)
             for b in bars])  # fmt: skip
    for sym, rec in spec.records.items():
        if rec is not None:
            files[f"corporate_actions/{sym}.json"] = encode_corporate_actions(rec)
    return manifest_meta(symbols), files


def write_fixture(root: Path, spec: FixtureSpec | None = None) -> str:
    meta, files = build_files(spec or default_spec())
    return write_dataset(root, meta, files)
