"""Strategy signal contract (Step 16.14, Phase C).

A :class:`SetupSignal` is the immutable, auditable output of an accepted setup: the
decision it was made at, the single session it is valid for, its prices and the
evidence behind them. :func:`to_trade_plan_intent` translates it into the existing
:class:`~swing_trading_bot.domain.trade_plan.TradePlanIntent` using an
:class:`ExitTemplate` (partial fraction, trailing rule) — the only bridge to the OMS.

Responsibilities stay separate:

* market-data validation — :mod:`.market_data`;
* signal generation — :mod:`.support_resistance` and :func:`generate_signal`;
* risk sizing — :mod:`swing_trading_bot.risk.sizing` / the pre-trade engine;
* TradePlan creation — ``TradePlan.new(intent)`` by the caller;
* execution — the OMS.

Nothing here sizes, persists, or submits anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal

from swing_trading_bot.domain.enums import InstrumentType, TimeInForce
from swing_trading_bot.domain.money import DECIMAL_CONTEXT, PENNY, ZERO
from swing_trading_bot.domain.trade_plan import (
    ExitAllocationPolicy,
    TradePlanIntent,
    TrailingStopIntent,
    TrailMode,
)
from swing_trading_bot.strategy.market_data import (
    BarWindow,
    PriceBasis,
    SessionCalendar,
    SplitEvent,
    SymbolSeries,
    WindowIssue,
    WindowStatus,
)
from swing_trading_bot.strategy.support_resistance import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    RejectionReason,
    SetupEvaluation,
    SupportResistanceParams,
    Zone,
    evaluate_setup,
)


class SignalError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SignalProvenance:
    feed: str
    basis: PriceBasis
    splits_applied: tuple[SplitEvent, ...]
    bars_used: int
    series_digest: str | None


@dataclass(frozen=True, slots=True)
class SetupSignal:
    strategy_id: str
    strategy_version: str
    params_fingerprint: str
    symbol: str
    decision_session: date
    decision_at: datetime
    """The decision session's close: no input to this signal is later than this."""
    entry_session: date
    """The only session the entry may be worked in (a DAY limit order)."""
    entry_limit: Decimal
    protective_stop: Decimal
    partial_target: Decimal
    support: Zone
    resistance: Zone
    atr: Decimal
    reward_risk: Decimal
    provenance: SignalProvenance = field(compare=True)

    def __post_init__(self) -> None:
        if not ZERO < self.protective_stop < self.entry_limit < self.partial_target:
            raise SignalError("need 0 < protective_stop < entry_limit < partial_target")
        if self.entry_session <= self.decision_session:
            raise SignalError("entry_session must follow decision_session")

    @property
    def risk_per_share(self) -> Decimal:
        return self.entry_limit - self.protective_stop

    @property
    def signal_ref(self) -> str:
        """Stable identity of this signal instance (fits the TradePlan token rule)."""
        return f"{self.symbol}:{self.decision_session:%Y%m%d}:{self.params_fingerprint[:12]}"

    def valid_for(self, session: date) -> bool:
        return session == self.entry_session


@dataclass(frozen=True, slots=True)
class ExitTemplate:
    """How every signal maps onto TradePlan exit fields. Not strategy logic: it encodes
    the exit *policy* parameters the OMS already understands."""

    partial_fraction: Decimal = Decimal("0.5")
    min_partial_qty: int = 1
    min_runner_qty: int = 1
    trail_atr_multiple: Decimal | None = Decimal("2")
    """ABSOLUTE trail of ``multiple x ATR`` rounded UP to a cent; None → ``trail_percent``."""
    trail_percent: Decimal = Decimal("0.08")
    breakeven_after_partial: bool = True
    instrument_type: InstrumentType = InstrumentType.EQUITY

    def allocation(self) -> ExitAllocationPolicy:
        return ExitAllocationPolicy(self.partial_fraction, self.min_partial_qty,
                                    self.min_runner_qty)  # fmt: skip

    def trailing(self, atr: Decimal) -> TrailingStopIntent:
        if self.trail_atr_multiple is None:
            return TrailingStopIntent(TrailMode.PERCENT, self.trail_percent)
        offset = (self.trail_atr_multiple * atr).quantize(
            PENNY, rounding=ROUND_CEILING, context=DECIMAL_CONTEXT
        )
        return TrailingStopIntent(TrailMode.ABSOLUTE, max(offset, PENNY))


def to_trade_plan_intent(signal: SetupSignal, template: ExitTemplate) -> TradePlanIntent:
    """The TradePlan intent for ``signal``. Raises ``TradePlanError`` on invalid prices."""
    return TradePlanIntent(
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        signal_ref=signal.signal_ref,
        symbol=signal.symbol,
        instrument_type=template.instrument_type,
        entry_limit=signal.entry_limit,
        protective_stop=signal.protective_stop,
        partial_target=signal.partial_target,
        allocation=template.allocation(),
        trailing=template.trailing(signal.atr),
        breakeven_after_partial=template.breakeven_after_partial,
        entry_time_in_force=TimeInForce.DAY,
    )


@dataclass(frozen=True, slots=True)
class SignalResult:
    symbol: str
    decision_session: date
    evaluation: SetupEvaluation | None
    signal: SetupSignal | None
    window_issue: WindowIssue | None = None
    detail: str = ""

    @property
    def reasons(self) -> tuple[RejectionReason, ...]:
        if self.evaluation is None:
            return (RejectionReason.INSUFFICIENT_HISTORY,)
        return self.evaluation.reasons


def signal_from_window(
    window: BarWindow,
    params: SupportResistanceParams,
    *,
    entry_session: date,
    series_digest: str | None = None,
) -> SignalResult:
    """Evaluate ``window`` and build the signal when the setup is accepted."""
    ev = evaluate_setup(window, params)
    m = ev.metrics
    if not ev.accepted:
        return SignalResult(window.symbol, window.decision_session, ev, None)
    if (m.support is None or m.resistance is None or m.entry_limit is None
            or m.protective_stop is None or m.partial_target is None
            or m.reward_risk is None or m.atr is None):  # fmt: skip
        raise SignalError("an accepted setup must carry every price")  # pragma: no cover
    signal = SetupSignal(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        params_fingerprint=ev.params_fingerprint,
        symbol=window.symbol,
        decision_session=window.decision_session,
        decision_at=window.decision_at,
        entry_session=entry_session,
        entry_limit=m.entry_limit,
        protective_stop=m.protective_stop,
        partial_target=m.partial_target,
        support=m.support,
        resistance=m.resistance,
        atr=m.atr,
        reward_risk=m.reward_risk,
        provenance=SignalProvenance(
            window.feed, window.basis, window.splits_applied, len(window.bars), series_digest
        ),
    )
    return SignalResult(window.symbol, window.decision_session, ev, signal)


def generate_signal(
    series: SymbolSeries,
    calendar: SessionCalendar,
    decision_session: date,
    params: SupportResistanceParams,
) -> SignalResult:
    """The signal decided after ``decision_session`` closes, for the next session.

    Uses only bars on or before ``decision_session`` (point-in-time adjusted). A missing
    session in the window, or no next session in the calendar, yields no signal."""
    result = series.window(calendar, decision_session, params.required_sessions)
    if result.status is not WindowStatus.COMPLETE or result.window is None:
        return SignalResult(series.symbol, decision_session, None, None,
                            result.issue, result.detail)  # fmt: skip
    nxt = calendar.next_session(decision_session)
    if nxt is None:
        return SignalResult(series.symbol, decision_session, None, None,
                            WindowIssue.CALENDAR_TOO_SHORT, "no next session")  # fmt: skip
    return signal_from_window(result.window, params, entry_session=nxt,
                              series_digest=series.provenance.digest)  # fmt: skip


__all__ = [
    "ExitTemplate",
    "SetupSignal",
    "SignalError",
    "SignalProvenance",
    "SignalResult",
    "generate_signal",
    "signal_from_window",
    "to_trade_plan_intent",
]
