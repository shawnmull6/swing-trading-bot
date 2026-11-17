"""Long-only support/resistance swing setup (Step 16.14, Phase B).

Pure and deterministic: the only input is a :class:`~.market_data.BarWindow` (completed
sessions up to and including the decision session, point-in-time split-adjusted). The
output is a :class:`SetupEvaluation`: an accepted setup with prices, or explicit
rejection reasons. Nothing here sizes, places orders or touches a ledger.

**Rules** (every threshold is a :class:`SupportResistanceParams` field):

1. *Volatility* — ``ATR`` is the simple mean of the true range over ``atr_period``
   sessions. ``ATR / close`` must lie in ``[min_atr_pct, max_atr_pct]`` (meaningful daily
   movement, but not erratic).
2. *Liquidity and price* — mean ``close x volume`` over ``liquidity_period`` sessions
   ≥ ``min_avg_dollar_volume``; close ≥ ``min_price``.
3. *Swing points* — bar ``i`` of the last ``level_lookback`` bars is a swing low when its
   low is ≤ every low within ``pivot_span`` bars on both sides and < every low on its
   left (the first of equal lows wins); swing highs mirror this. The last ``pivot_span``
   bars can never be swing points: a swing point needs ``pivot_span`` *completed* bars
   after it, so its confirmation never uses data after the decision session.
4. *Zones* — swing prices sorted ascending are grouped greedily: a price more than
   ``zone_atr x ATR`` above the group's lowest member starts a new group. A zone has a
   floor (lowest), top (highest) and touch count.
5. *Support* — the highest zone with ≥ ``min_support_touches`` swing lows whose floor is
   at or below the close. Proximity: ``max(0, close - top) / ATR`` ≤
   ``max_entry_distance_atr``.
6. *Confirmation* (``confirmation``) on the decision bar:
   ``BULLISH_REJECTION`` — the low reached the zone top, the bar closed up
   (close > open) and in the upper half of its range; ``CLOSE_ABOVE_PRIOR_HIGH`` — the
   zone was tested by this or the prior bar and the close exceeds the prior bar's high;
   ``NONE`` — no confirmation.
7. *Prices* — entry limit = close rounded down to a valid tick; protective stop =
   support floor - ``stop_buffer_atr x ATR`` rounded down; resistance = the lowest
   swing-high zone whose floor is above the entry; first target = that floor -
   ``target_buffer_atr x ATR`` rounded down.
8. *Reward/risk* — ``(target - entry) / (entry - stop)`` ≥ ``min_reward_risk``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.domain.money import (
    DECIMAL_CONTEXT,
    PENNY,
    PRICE_QUANTUM,
    SUB_PENNY_THRESHOLD,
    ZERO,
)
from swing_trading_bot.strategy.market_data import BarWindow, StrategyBar

STRATEGY_ID: Final = "sr-swing"
STRATEGY_VERSION: Final = "1"
HALF: Final = Decimal("0.5")


class Confirmation(StrEnum):
    NONE = "NONE"
    BULLISH_REJECTION = "BULLISH_REJECTION"
    CLOSE_ABOVE_PRIOR_HIGH = "CLOSE_ABOVE_PRIOR_HIGH"


class RejectionReason(StrEnum):
    """Declaration order is report order."""

    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    VOLATILITY_TOO_LOW = "VOLATILITY_TOO_LOW"
    VOLATILITY_TOO_HIGH = "VOLATILITY_TOO_HIGH"
    NO_SUPPORT = "NO_SUPPORT"
    NOT_NEAR_SUPPORT = "NOT_NEAR_SUPPORT"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    INVALID_STOP = "INVALID_STOP"
    NO_RESISTANCE = "NO_RESISTANCE"
    TARGET_NOT_ABOVE_ENTRY = "TARGET_NOT_ABOVE_ENTRY"
    REWARD_RISK_TOO_LOW = "REWARD_RISK_TOO_LOW"


_REASON_ORDER: Final = {r: i for i, r in enumerate(RejectionReason)}


class StrategyParamsError(ValueError):
    pass


def _dec(value: object, name: str, *, positive: bool = True) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StrategyParamsError(f"{name} must be a finite Decimal")
    if value < ZERO or (positive and value == ZERO):
        raise StrategyParamsError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return value


def _int(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StrategyParamsError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class SupportResistanceParams:
    """A small set of interpretable parameters. Defaults are a starting point for
    research, not tuned or approved values."""

    level_lookback: int = 60
    pivot_span: int = 3
    atr_period: int = 14
    zone_atr: Decimal = Decimal("0.5")
    min_support_touches: int = 2
    max_entry_distance_atr: Decimal = Decimal("0.75")
    confirmation: Confirmation = Confirmation.BULLISH_REJECTION
    stop_buffer_atr: Decimal = Decimal("0.25")
    target_buffer_atr: Decimal = Decimal("0.10")
    min_reward_risk: Decimal = Decimal("2")
    min_price: Decimal = Decimal("10")
    liquidity_period: int = 20
    min_avg_dollar_volume: Decimal = Decimal("20000000")
    min_atr_pct: Decimal = Decimal("0.015")
    max_atr_pct: Decimal = Decimal("0.08")

    def __post_init__(self) -> None:
        span = _int(self.pivot_span, "pivot_span", 1)
        _int(self.level_lookback, "level_lookback", 2 * span + 2)
        _int(self.atr_period, "atr_period", 1)
        _int(self.min_support_touches, "min_support_touches", 1)
        _int(self.liquidity_period, "liquidity_period", 1)
        for name in ("zone_atr", "min_reward_risk", "min_price", "min_avg_dollar_volume",
                     "max_atr_pct"):  # fmt: skip
            _dec(getattr(self, name), name)
        for name in ("max_entry_distance_atr", "stop_buffer_atr", "target_buffer_atr",
                     "min_atr_pct"):  # fmt: skip
            _dec(getattr(self, name), name, positive=False)
        if self.min_atr_pct > self.max_atr_pct:
            raise StrategyParamsError("min_atr_pct must not exceed max_atr_pct")
        if not isinstance(self.confirmation, Confirmation):
            raise StrategyParamsError("confirmation must be a Confirmation")

    @property
    def required_sessions(self) -> int:
        """Bars a window must contain (the ATR needs one extra prior close)."""
        return max(self.level_lookback, self.atr_period + 1, self.liquidity_period, 2)

    def canonical(self) -> dict[str, str | int]:
        return {k: (v.value if isinstance(v, StrEnum) else v if isinstance(v, int) else str(v))
                for k, v in asdict(self).items()}  # fmt: skip

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(
            {"strategy": STRATEGY_ID, "version": STRATEGY_VERSION, **self.canonical()},
            sort_keys=True, separators=(",", ":"),
        )  # fmt: skip
        return hashlib.sha256(blob.encode()).hexdigest()


# ===================================================================== results


@dataclass(frozen=True, slots=True)
class Zone:
    floor: Decimal
    top: Decimal
    touches: int
    last_touch: date


@dataclass(frozen=True, slots=True)
class SetupMetrics:
    """Every computed quantity, for audit. ``None`` where a stage was not reached."""

    close: Decimal | None = None
    atr: Decimal | None = None
    atr_pct: Decimal | None = None
    avg_dollar_volume: Decimal | None = None
    support: Zone | None = None
    resistance: Zone | None = None
    distance_atr: Decimal | None = None
    entry_limit: Decimal | None = None
    protective_stop: Decimal | None = None
    partial_target: Decimal | None = None
    reward_risk: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SetupEvaluation:
    symbol: str
    decision_session: date
    params_fingerprint: str
    reasons: tuple[RejectionReason, ...]
    metrics: SetupMetrics

    @property
    def accepted(self) -> bool:
        return not self.reasons


# ===================================================================== math


def round_price_down(value: Decimal) -> Decimal:
    """Round down to a valid order tick (whole cents at/above $1, 0.0001 below)."""
    quantum = PENNY if value >= SUB_PENNY_THRESHOLD else PRICE_QUANTUM
    return value.quantize(quantum, rounding=ROUND_FLOOR, context=DECIMAL_CONTEXT)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return DECIMAL_CONTEXT.divide(sum(values, ZERO), Decimal(len(values)))


def average_true_range(bars: Sequence[StrategyBar], period: int) -> Decimal:
    """Simple mean of the last ``period`` true ranges (needs ``period + 1`` bars)."""
    if len(bars) < period + 1:
        raise StrategyParamsError("not enough bars for the ATR period")
    ranges = []
    for prev, bar in zip(bars[-period - 1 : -1], bars[-period:], strict=True):
        ranges.append(max(bar.high - bar.low, abs(bar.high - prev.close),
                          abs(bar.low - prev.close)))  # fmt: skip
    return _mean(ranges)


def swing_lows(bars: Sequence[StrategyBar], span: int) -> list[tuple[date, Decimal]]:
    out = []
    for i in range(span, len(bars) - span):
        lo = bars[i].low
        left = bars[i - span : i]
        right = bars[i + 1 : i + span + 1]
        if all(lo < b.low for b in left) and all(lo <= b.low for b in right):
            out.append((bars[i].session_date, lo))
    return out


def swing_highs(bars: Sequence[StrategyBar], span: int) -> list[tuple[date, Decimal]]:
    out = []
    for i in range(span, len(bars) - span):
        hi = bars[i].high
        left = bars[i - span : i]
        right = bars[i + 1 : i + span + 1]
        if all(hi > b.high for b in left) and all(hi >= b.high for b in right):
            out.append((bars[i].session_date, hi))
    return out


def cluster_zones(points: Sequence[tuple[date, Decimal]], width: Decimal) -> list[Zone]:
    """Greedy, order-independent grouping of swing prices (ascending)."""
    ordered = sorted(points, key=lambda p: (p[1], p[0]))
    zones: list[Zone] = []
    group: list[tuple[date, Decimal]] = []
    for point in ordered:
        if group and point[1] - group[0][1] > width:
            zones.append(_zone(group))
            group = []
        group.append(point)
    if group:
        zones.append(_zone(group))
    return zones


def _zone(group: Sequence[tuple[date, Decimal]]) -> Zone:
    return Zone(floor=group[0][1], top=group[-1][1], touches=len(group),
                last_touch=max(d for d, _ in group))  # fmt: skip


def _confirmed(bars: Sequence[StrategyBar], zone_top: Decimal, mode: Confirmation) -> bool:
    bar = bars[-1]
    if mode is Confirmation.NONE:
        return True
    if mode is Confirmation.BULLISH_REJECTION:
        rng = bar.high - bar.low
        return (bar.low <= zone_top and bar.close > bar.open and rng > ZERO
                and bar.close - bar.low >= rng * HALF)  # fmt: skip
    prior = bars[-2]
    tested = bar.low <= zone_top or prior.low <= zone_top
    return tested and bar.close > prior.high


# ===================================================================== evaluate


def evaluate_setup(window: BarWindow, params: SupportResistanceParams) -> SetupEvaluation:
    """Evaluate one symbol after the decision session's close. Deterministic."""
    fp = params.fingerprint
    if len(window.bars) < params.required_sessions:
        return SetupEvaluation(window.symbol, window.decision_session, fp,
                               (RejectionReason.INSUFFICIENT_HISTORY,), SetupMetrics())  # fmt: skip
    bars = window.bars[-params.required_sessions :]
    reasons, metrics = _filters(bars, params)
    if metrics.atr is not None and metrics.atr > ZERO:
        level_reasons, metrics = _levels(bars, params, metrics)
        reasons += level_reasons
    return _done(window, fp, reasons, metrics)


def _filters(
    bars: Sequence[StrategyBar], params: SupportResistanceParams
) -> tuple[list[RejectionReason], SetupMetrics]:
    reasons: list[RejectionReason] = []
    close = bars[-1].close
    atr = average_true_range(bars, params.atr_period)
    atr_pct = DECIMAL_CONTEXT.divide(atr, close)
    adv = _mean([b.dollar_volume for b in bars[-params.liquidity_period :]])
    if close < params.min_price:
        reasons.append(RejectionReason.PRICE_BELOW_MINIMUM)
    if adv < params.min_avg_dollar_volume:
        reasons.append(RejectionReason.INSUFFICIENT_LIQUIDITY)
    if atr_pct < params.min_atr_pct or atr == ZERO:
        reasons.append(RejectionReason.VOLATILITY_TOO_LOW)
    if atr_pct > params.max_atr_pct:
        reasons.append(RejectionReason.VOLATILITY_TOO_HIGH)
    return reasons, SetupMetrics(close=close, atr=atr, atr_pct=atr_pct, avg_dollar_volume=adv)


def _levels(
    bars: Sequence[StrategyBar], params: SupportResistanceParams, base: SetupMetrics
) -> tuple[list[RejectionReason], SetupMetrics]:
    reasons: list[RejectionReason] = []
    close, atr = bars[-1].close, base.atr
    if atr is None:  # pragma: no cover - guarded by the caller
        raise StrategyParamsError("ATR required")
    width = params.zone_atr * atr
    level_bars = bars[-params.level_lookback :]
    supports = [z for z in cluster_zones(swing_lows(level_bars, params.pivot_span), width)
                if z.touches >= params.min_support_touches and z.floor <= close]  # fmt: skip
    if not supports:
        return [RejectionReason.NO_SUPPORT], base
    support = max(supports, key=lambda z: (z.top, z.floor))
    distance = DECIMAL_CONTEXT.divide(max(ZERO, close - support.top), atr)
    if distance > params.max_entry_distance_atr:
        reasons.append(RejectionReason.NOT_NEAR_SUPPORT)
    if not _confirmed(bars, support.top, params.confirmation):
        reasons.append(RejectionReason.NOT_CONFIRMED)
    entry = round_price_down(close)
    stop = _below(support.floor - params.stop_buffer_atr * atr, entry)
    if stop is None:
        reasons.append(RejectionReason.INVALID_STOP)
    above = [z for z in cluster_zones(swing_highs(level_bars, params.pivot_span), width)
             if z.floor > entry]  # fmt: skip
    resistance = min(above, key=lambda z: (z.floor, z.top)) if above else None
    target: Decimal | None = None
    if resistance is None:
        reasons.append(RejectionReason.NO_RESISTANCE)
    else:
        raw_target = resistance.floor - params.target_buffer_atr * atr
        target = round_price_down(raw_target) if raw_target > ZERO else None
        if target is None or target <= entry:
            reasons.append(RejectionReason.TARGET_NOT_ABOVE_ENTRY)
            target = None
    rr: Decimal | None = None
    if stop is not None and target is not None:
        rr = DECIMAL_CONTEXT.divide(target - entry, entry - stop)
        if rr < params.min_reward_risk:
            reasons.append(RejectionReason.REWARD_RISK_TOO_LOW)
    metrics = replace(base, support=support, resistance=resistance, distance_atr=distance,
                      entry_limit=entry, protective_stop=stop, partial_target=target,
                      reward_risk=rr)  # fmt: skip
    return reasons, metrics


def _below(raw_stop: Decimal, entry: Decimal) -> Decimal | None:
    """The rounded-down stop, or None when it is not a positive price below ``entry``."""
    if raw_stop <= ZERO:
        return None
    stop = round_price_down(raw_stop)
    return stop if ZERO < stop < entry else None


def _done(
    window: BarWindow, fp: str, reasons: Sequence[RejectionReason], metrics: SetupMetrics
) -> SetupEvaluation:
    ordered = tuple(sorted(set(reasons), key=_REASON_ORDER.__getitem__))
    return SetupEvaluation(window.symbol, window.decision_session, fp, ordered, metrics)


__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "Confirmation",
    "RejectionReason",
    "SetupEvaluation",
    "SetupMetrics",
    "StrategyParamsError",
    "SupportResistanceParams",
    "Zone",
    "average_true_range",
    "cluster_zones",
    "evaluate_setup",
    "round_price_down",
    "swing_highs",
    "swing_lows",
]
