"""TradePlan contract (M2-3 foundation, RTH-01; ARCHITECTURE §12.38).

Pure domain code: no I/O, no broker, no ledger. Four concerns are kept apart:

1. **Strategy intent** — :class:`TradePlanIntent`: what a strategy wants (entry limit
   near support, protective stop, partial-profit target near resistance, breakeven
   rule, trailing rule). Immutable and validated. Its ``fingerprint`` is a hash of its
   canonical content: it identifies *what* is intended, never *which trade*.
   A :class:`TradePlan` binds an intent to a generated, unique trade-instance id
   (``plan_id``, UUIDv7). Two independent trades with identical parameters get distinct
   ids; a retry of one trade reuses its id, so the plan's UNIQUE order intent keys
   (``{plan_id}:{ROLE}:{generation}``) make every step at-most-once. A strategy is
   replaceable: anything that produces a valid intent plugs in, and nothing here knows
   how support or resistance were found.
2. **Risk-approved sizing** stays in :mod:`swing_trading_bot.risk.pretrade` (not
   duplicated here); :func:`allocate_exit_quantities` only splits an *actual* filled
   position into whole-share partial and runner buckets.
3. **OMS order lifecycle and share reservations** live in :mod:`swing_trading_bot.oms`.
4. **Exit-management state** — :class:`ExitState`, a pure reducer over explicit,
   idempotent events (fills and observed highs). Replaying the same events yields the
   same state; the protective stop only ever ratchets up.

Nothing here decides a policy that has not been approved (ARCHITECTURE §12.38,
"Unresolved policy decisions"): a position too small to split enters
``SMALL_POSITION_UNRESOLVED`` instead of inventing a rule, and the stop/target share
conflict is reported (:attr:`ExitOrderPlan.share_conflict`), not resolved.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Final

from swing_trading_bot.domain.enums import InstrumentType, OrderRole, TimeInForce
from swing_trading_bot.domain.ids import uuid7
from swing_trading_bot.domain.money import (
    DECIMAL_CONTEXT,
    ONE,
    PENNY,
    ZERO,
    MoneyError,
    is_whole,
    valid_order_price,
)

PLAN_ID_PREFIX: Final = "tp-"
_PLAN_ID_RE: Final = re.compile(r"^tp-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$")
"""``tp-`` + the 32 hex digits of a UUIDv7 (version and variant nibbles checked)."""
FINGERPRINT_PREFIX: Final = "tpf-"
_FINGERPRINT_RE: Final = re.compile(r"^tpf-[0-9a-f]{64}$")
_SYMBOL_RE: Final = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class TradePlanError(ValueError):
    """An invalid plan, allocation or exit event. Callers treat it as fail-closed."""


class ExitStateError(TradePlanError):
    """An event that cannot be applied (e.g. an oversell): the caller must halt."""


def is_plan_id(value: str) -> bool:
    return bool(_PLAN_ID_RE.match(value))


def is_fingerprint(value: str) -> bool:
    return bool(_FINGERPRINT_RE.match(value))


def new_plan_id() -> str:
    """A new, unique trade-instance id. Never derived from the intent's content."""
    return PLAN_ID_PREFIX + uuid7().hex


def _is_decimal(value: object) -> bool:
    """Runtime check: dataclass annotations are not enforced, floats must not slip in."""
    return isinstance(value, Decimal)


def _price(value: Decimal, name: str) -> Decimal:
    try:
        return valid_order_price(value)
    except MoneyError as exc:
        raise TradePlanError(f"{name}: {exc}") from exc


def _floor_penny(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_FLOOR, context=DECIMAL_CONTEXT)


def _ceil_penny(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_CEILING, context=DECIMAL_CONTEXT)


# ===================================================================== intent


class TrailMode(StrEnum):
    ABSOLUTE = "ABSOLUTE"
    """Trail a fixed dollar amount below the high-water mark."""
    PERCENT = "PERCENT"
    """Trail a fraction (0, 1) of the high-water mark below it."""


@dataclass(frozen=True, slots=True)
class TrailingStopIntent:
    mode: TrailMode
    offset: Decimal

    def __post_init__(self) -> None:
        if not _is_decimal(self.offset) or not self.offset.is_finite():
            raise TradePlanError("trailing offset must be a finite Decimal")
        if self.mode is TrailMode.PERCENT:
            if not ZERO < self.offset < ONE:
                raise TradePlanError("percent trailing offset must be in (0, 1)")
        elif self.offset <= ZERO or self.offset != _floor_penny(self.offset):
            raise TradePlanError("absolute trailing offset must be a positive whole-cent amount")

    def stop_for(self, high_water: Decimal) -> Decimal | None:
        """The trailing stop for ``high_water``, rounded DOWN to a valid cent (never
        tighter than the specified distance), or None when it would not be positive."""
        if self.mode is TrailMode.ABSOLUTE:
            raw = high_water - self.offset
        else:
            raw = high_water * (ONE - self.offset)
        stop = _floor_penny(raw)
        return stop if stop > ZERO else None


@dataclass(frozen=True, slots=True)
class ExitAllocationPolicy:
    """How a filled position is split into the partial-profit and runner buckets."""

    partial_fraction: Decimal
    min_partial_qty: int = 1
    min_runner_qty: int = 1

    def __post_init__(self) -> None:
        if not _is_decimal(self.partial_fraction) or not (ZERO < self.partial_fraction < ONE):
            raise TradePlanError("partial_fraction must be a Decimal in (0, 1)")
        if self.min_partial_qty < 1 or self.min_runner_qty < 1:
            raise TradePlanError("minimum bucket sizes must be at least one share")


@dataclass(frozen=True, slots=True)
class ExitAllocation:
    total_qty: Decimal
    partial_qty: Decimal
    runner_qty: Decimal
    feasible: bool
    reason: str | None = None


def allocate_exit_quantities(total_qty: Decimal, policy: ExitAllocationPolicy) -> ExitAllocation:
    """Split ``total_qty`` whole shares: partial = floor(total x fraction), runner = rest.

    Never rounds a bucket up. When either bucket would fall below its minimum the split is
    **infeasible**: the result carries ``partial_qty = 0`` and the whole position in
    ``runner_qty``, and what to do about it is an unresolved policy decision.
    """
    if not _is_decimal(total_qty) or total_qty < ZERO or not is_whole(total_qty):
        raise TradePlanError("total_qty must be a non-negative whole number of shares")
    partial = (total_qty * policy.partial_fraction).to_integral_value(
        rounding=ROUND_FLOOR, context=DECIMAL_CONTEXT
    )
    runner = total_qty - partial
    if partial < policy.min_partial_qty or runner < policy.min_runner_qty:
        return ExitAllocation(
            total_qty, ZERO, total_qty, feasible=False,
            reason=f"{total_qty} share(s) cannot be split into a partial of at least "
            f"{policy.min_partial_qty} and a runner of at least {policy.min_runner_qty}",
        )  # fmt: skip
    return ExitAllocation(total_qty, partial, runner, feasible=True)


@dataclass(frozen=True, slots=True)
class TradePlanIntent:
    """Strategy-generated intent for one long swing trade. Immutable and validated.

    ``signal_ref`` names the signal instance (e.g. the session and level it came from) so
    two distinct signals with identical prices still get distinct plan ids.
    """

    strategy_id: str
    strategy_version: str
    signal_ref: str
    symbol: str
    instrument_type: InstrumentType
    entry_limit: Decimal
    protective_stop: Decimal
    partial_target: Decimal
    allocation: ExitAllocationPolicy
    trailing: TrailingStopIntent
    breakeven_after_partial: bool = True
    entry_time_in_force: TimeInForce = TimeInForce.DAY

    def __post_init__(self) -> None:
        for name in ("strategy_id", "strategy_version", "signal_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _TOKEN_RE.match(value):
                raise TradePlanError(f"{name} must match {_TOKEN_RE.pattern}")
        if not _SYMBOL_RE.match(self.symbol):
            raise TradePlanError(f"invalid symbol {self.symbol!r}")
        entry = _price(self.entry_limit, "entry_limit")
        stop = _price(self.protective_stop, "protective_stop")
        target = _price(self.partial_target, "partial_target")
        if not stop < entry:
            raise TradePlanError("protective_stop must be below entry_limit (long only)")
        if not entry < target:
            raise TradePlanError("partial_target must be above entry_limit")

    # ----------------------------------------------------------------- identity
    def canonical(self) -> dict[str, str | bool]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "signal_ref": self.signal_ref,
            "symbol": self.symbol,
            "instrument_type": self.instrument_type.value,
            "entry_limit": str(self.entry_limit),
            "protective_stop": str(self.protective_stop),
            "partial_target": str(self.partial_target),
            "partial_fraction": str(self.allocation.partial_fraction),
            "min_partial_qty": str(self.allocation.min_partial_qty),
            "min_runner_qty": str(self.allocation.min_runner_qty),
            "trail_mode": self.trailing.mode.value,
            "trail_offset": str(self.trailing.offset),
            "breakeven_after_partial": self.breakeven_after_partial,
            "entry_time_in_force": self.entry_time_in_force.value,
        }

    @property
    def fingerprint(self) -> str:
        """Deterministic hash of the canonical content (``tpf-`` + 64 hex digits). Equal
        fingerprints mean equal intents, not the same trade."""
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return FINGERPRINT_PREFIX + hashlib.sha256(blob.encode()).hexdigest()

    @classmethod
    def from_canonical(cls, data: Mapping[str, object]) -> TradePlanIntent:
        """Rebuild an intent from :meth:`canonical` output (e.g. the durable copy in the
        entry's audit event). Unknown or missing keys, or a non-string value where a
        string is expected, raise :class:`TradePlanError`."""
        expected = set(_CANONICAL_KEYS)
        if set(data) != expected:
            raise TradePlanError(f"canonical intent keys differ: {sorted(set(data) ^ expected)}")

        def text(key: str) -> str:
            value = data[key]
            if not isinstance(value, str):
                raise TradePlanError(f"canonical {key} must be a string")
            return value

        flag = data["breakeven_after_partial"]
        if not isinstance(flag, bool):
            raise TradePlanError("canonical breakeven_after_partial must be a boolean")
        try:
            return cls(
                strategy_id=text("strategy_id"),
                strategy_version=text("strategy_version"),
                signal_ref=text("signal_ref"),
                symbol=text("symbol"),
                instrument_type=InstrumentType(text("instrument_type")),
                entry_limit=Decimal(text("entry_limit")),
                protective_stop=Decimal(text("protective_stop")),
                partial_target=Decimal(text("partial_target")),
                allocation=ExitAllocationPolicy(
                    Decimal(text("partial_fraction")),
                    int(text("min_partial_qty")),
                    int(text("min_runner_qty")),
                ),
                trailing=TrailingStopIntent(
                    TrailMode(text("trail_mode")), Decimal(text("trail_offset"))
                ),
                breakeven_after_partial=flag,
                entry_time_in_force=TimeInForce(text("entry_time_in_force")),
            )
        except (ValueError, ArithmeticError) as exc:
            if isinstance(exc, TradePlanError):
                raise
            raise TradePlanError(f"invalid canonical intent: {exc}") from exc

    @property
    def planned_risk_per_share(self) -> Decimal:
        return self.entry_limit - self.protective_stop


_CANONICAL_KEYS: Final = (
    "strategy_id", "strategy_version", "signal_ref", "symbol", "instrument_type",
    "entry_limit", "protective_stop", "partial_target", "partial_fraction",
    "min_partial_qty", "min_runner_qty", "trail_mode", "trail_offset",
    "breakeven_after_partial", "entry_time_in_force",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class TradePlan:
    """One trade instance: a unique ``plan_id`` bound to an immutable intent.

    * New trade → :meth:`new` (fresh UUIDv7 id), even if the intent equals another's.
    * Retry or restart of the same trade → reuse the same ``TradePlan`` (or rebuild it
      from the ledger); its intent keys collide with the orders already written.
    """

    plan_id: str
    intent: TradePlanIntent

    def __post_init__(self) -> None:
        if not is_plan_id(self.plan_id):
            raise TradePlanError(f"invalid plan id {self.plan_id!r}")

    @classmethod
    def new(cls, intent: TradePlanIntent, *, new_id: Callable[[], str] = new_plan_id) -> TradePlan:
        return cls(new_id(), intent)

    @property
    def fingerprint(self) -> str:
        return self.intent.fingerprint

    @property
    def entry_intent_key(self) -> str:
        return exit_intent_key(self.plan_id, OrderRole.ENTRY, 0)


def exit_intent_key(plan_id: str, role: OrderRole, generation: int) -> str:
    """Deterministic, durable intent key for a plan order. ``generation`` increases for
    each replacement, so a re-derived intent for the same step collides with the UNIQUE
    ``orders.intent_key`` instead of creating a second order."""
    if not is_plan_id(plan_id):
        raise TradePlanError(f"invalid plan id {plan_id!r}")
    if generation < 0:
        raise TradePlanError("generation must be >= 0")
    return f"{plan_id}:{role.value}:{generation}"


# ============================================================= exit state


class ExitPhase(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    """No shares yet; the entry may be working."""
    ENTRY_PARTIAL = "ENTRY_PARTIAL"
    """The entry has fills but is still working."""
    PROTECTED = "PROTECTED"
    """Entry complete; initial stop; the partial target has not been fully taken."""
    RUNNER = "RUNNER"
    """Partial taken; stop at or above breakeven; the runner trails."""
    SMALL_POSITION_UNRESOLVED = "SMALL_POSITION_UNRESOLVED"
    """Too few shares to split; only the protective stop applies until a policy exists."""
    CLOSED = "CLOSED"
    ENTRY_ABANDONED = "ENTRY_ABANDONED"
    """The entry ended without any fill."""


class ExitEventKind(StrEnum):
    ENTRY_FILL = "ENTRY_FILL"
    ENTRY_CLOSED = "ENTRY_CLOSED"
    TARGET_FILL = "TARGET_FILL"
    STOP_FILL = "STOP_FILL"
    """Any risk-reducing sell that is not the partial target (stop or exit)."""
    PRICE_HIGH = "PRICE_HIGH"


@dataclass(frozen=True, slots=True)
class ExitEvent:
    """One idempotent input. ``event_id`` is the broker activity id for fills, or a
    stable observation id (e.g. ``bar:<session>``) for highs."""

    event_id: str
    kind: ExitEventKind
    qty: Decimal = ZERO
    price: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.event_id:
            raise TradePlanError("event_id is required")
        fill = self.kind in (ExitEventKind.ENTRY_FILL, ExitEventKind.TARGET_FILL,
                             ExitEventKind.STOP_FILL)  # fmt: skip
        if fill and (self.qty <= ZERO or not is_whole(self.qty) or self.price <= ZERO):
            raise TradePlanError(f"{self.kind.value} needs a positive whole qty and price")
        if self.kind is ExitEventKind.PRICE_HIGH and self.price <= ZERO:
            raise TradePlanError("PRICE_HIGH needs a positive price")


@dataclass(frozen=True, slots=True)
class StopOrderIntent:
    qty: Decimal
    stop_price: Decimal


@dataclass(frozen=True, slots=True)
class TargetOrderIntent:
    qty: Decimal
    limit_price: Decimal


@dataclass(frozen=True, slots=True)
class ExitOrderPlan:
    """What exit orders the state calls for. Not an instruction to place them."""

    protective_stop: StopOrderIntent | None
    partial_target: TargetOrderIntent | None
    share_conflict: bool
    """True when stop and target together need more shares than are held: they would
    compete for the same shares (an unresolved execution policy, ARCHITECTURE §12.38)."""
    policy_required: str | None = None


@dataclass(frozen=True, slots=True)
class GapAssessment:
    stop_breached: bool
    open_price: Decimal
    stop_price: Decimal
    loss_at_open: Decimal
    planned_loss: Decimal
    r_multiple: Decimal | None
    """(open - avg entry) / planned risk per share; below -1 means a loss beyond 1R."""


@dataclass(frozen=True, slots=True)
class ExitState:
    intent: TradePlanIntent
    phase: ExitPhase = ExitPhase.PENDING_ENTRY
    entry_qty: Decimal = ZERO
    entry_notional: Decimal = ZERO
    target_sold: Decimal = ZERO
    other_sold: Decimal = ZERO
    stop_price: Decimal | None = None
    high_water: Decimal | None = None
    allocation: ExitAllocation | None = None
    applied: frozenset[str] = field(default_factory=frozenset)
    history: tuple[ExitEvent, ...] = ()

    @classmethod
    def start(cls, intent: TradePlanIntent) -> ExitState:
        return cls(intent=intent, stop_price=intent.protective_stop)

    # ------------------------------------------------------------- derived
    @property
    def held_qty(self) -> Decimal:
        return self.entry_qty - self.target_sold - self.other_sold

    @property
    def avg_entry(self) -> Decimal | None:
        if self.entry_qty == ZERO:
            return None
        return DECIMAL_CONTEXT.divide(self.entry_notional, self.entry_qty)

    @property
    def breakeven(self) -> Decimal | None:
        """Average entry fill price rounded UP to a cent (fees excluded)."""
        avg = self.avg_entry
        return None if avg is None else _ceil_penny(avg)

    @property
    def partial_remaining(self) -> Decimal:
        if self.allocation is None or not self.allocation.feasible:
            return ZERO
        return max(ZERO, self.allocation.partial_qty - self.target_sold)

    # ------------------------------------------------------------- reducer
    def apply(self, event: ExitEvent) -> ExitState:
        """Apply one event. A repeated ``event_id`` with identical content is a no-op;
        the same id with different content raises (conflicting replay)."""
        if event.event_id in self.applied:
            prior = next(e for e in self.history if e.event_id == event.event_id)
            if prior != event:
                raise ExitStateError(f"event {event.event_id} replayed with different content")
            return self
        nxt = self._reduce(event)
        return replace(nxt, applied=self.applied | {event.event_id},
                       history=(*self.history, event))  # fmt: skip

    def _reduce(self, ev: ExitEvent) -> ExitState:
        k = ExitEventKind
        if ev.kind is k.ENTRY_FILL:
            if self.phase not in (ExitPhase.PENDING_ENTRY, ExitPhase.ENTRY_PARTIAL):
                raise ExitStateError(f"entry fill in phase {self.phase.value}")
            return replace(self, phase=ExitPhase.ENTRY_PARTIAL, entry_qty=self.entry_qty + ev.qty,
                           entry_notional=self.entry_notional + ev.qty * ev.price)  # fmt: skip
        if ev.kind is k.ENTRY_CLOSED:
            return self._close_entry()
        if ev.kind is k.PRICE_HIGH:
            return self._observe_high(ev.price)
        if self.phase not in (ExitPhase.PROTECTED, ExitPhase.RUNNER,
                              ExitPhase.SMALL_POSITION_UNRESOLVED):  # fmt: skip
            raise ExitStateError(f"{ev.kind.value} in phase {self.phase.value}")
        if ev.qty > self.held_qty:
            raise ExitStateError(f"sell of {ev.qty} exceeds the {self.held_qty} held (oversell)")
        if ev.kind is k.TARGET_FILL:
            if ev.qty > self.partial_remaining:
                raise ExitStateError(
                    f"target fill {ev.qty} exceeds the remaining partial {self.partial_remaining}"
                )
            state = replace(self, target_sold=self.target_sold + ev.qty)
            if state.partial_remaining == ZERO and state.phase is ExitPhase.PROTECTED:
                state = state._enter_runner()
            return state._closed_if_flat()
        state = replace(self, other_sold=self.other_sold + ev.qty)
        return state._closed_if_flat()

    def _close_entry(self) -> ExitState:
        if self.phase is ExitPhase.PENDING_ENTRY:
            return replace(self, phase=ExitPhase.ENTRY_ABANDONED)
        if self.phase is not ExitPhase.ENTRY_PARTIAL:
            raise ExitStateError(f"entry closed in phase {self.phase.value}")
        alloc = allocate_exit_quantities(self.entry_qty, self.intent.allocation)
        phase = ExitPhase.PROTECTED if alloc.feasible else ExitPhase.SMALL_POSITION_UNRESOLVED
        return replace(self, phase=phase, allocation=alloc)

    def _enter_runner(self) -> ExitState:
        stop = self.stop_price
        be = self.breakeven
        if self.intent.breakeven_after_partial and be is not None:
            stop = be if stop is None else max(stop, be)
        state = replace(self, phase=ExitPhase.RUNNER, stop_price=stop)
        return state._trail()

    def _observe_high(self, price: Decimal) -> ExitState:
        if self.phase in (ExitPhase.CLOSED, ExitPhase.ENTRY_ABANDONED):
            return self
        high = price if self.high_water is None else max(self.high_water, price)
        return replace(self, high_water=high)._trail()

    def _trail(self) -> ExitState:
        """Ratchet the runner's stop; never lowers it."""
        if self.phase is not ExitPhase.RUNNER or self.high_water is None:
            return self
        trailed = self.intent.trailing.stop_for(self.high_water)
        if trailed is None or (self.stop_price is not None and trailed <= self.stop_price):
            return self
        return replace(self, stop_price=trailed)

    def _closed_if_flat(self) -> ExitState:
        return replace(self, phase=ExitPhase.CLOSED) if self.held_qty == ZERO else self

    # -------------------------------------------------------------- queries
    def exit_orders(self) -> ExitOrderPlan:
        held = self.held_qty
        if self.phase not in (ExitPhase.PROTECTED, ExitPhase.RUNNER,
                              ExitPhase.SMALL_POSITION_UNRESOLVED) or held == ZERO:  # fmt: skip
            return ExitOrderPlan(None, None, share_conflict=False)
        stop = None if self.stop_price is None else StopOrderIntent(held, self.stop_price)
        target: TargetOrderIntent | None = None
        if self.phase is ExitPhase.PROTECTED and self.partial_remaining > ZERO:
            target = TargetOrderIntent(self.partial_remaining, self.intent.partial_target)
        conflict = (stop.qty if stop else ZERO) + (target.qty if target else ZERO) > held
        policy = None
        if self.phase is ExitPhase.SMALL_POSITION_UNRESOLVED:
            policy = "SMALL_POSITION: partial-profit allocation impossible"
        elif conflict:
            policy = "STOP_TARGET_SHARE_CONFLICT: stop and target compete for the same shares"
        return ExitOrderPlan(stop, target, share_conflict=conflict, policy_required=policy)

    def assess_open(self, open_price: Decimal) -> GapAssessment:
        """Evaluate a session open against the current stop (overnight gap). A stop is a
        trigger, not a price guarantee: a gap through it executes at or near the open."""
        avg = self.avg_entry
        if avg is None or self.stop_price is None or self.held_qty == ZERO:
            raise TradePlanError("no open position to assess")
        risk = avg - self.intent.protective_stop
        planned = max(ZERO, risk) * self.held_qty
        loss = max(ZERO, avg - open_price) * self.held_qty
        r_mult = None if risk <= ZERO else DECIMAL_CONTEXT.divide(open_price - avg, risk)
        return GapAssessment(open_price <= self.stop_price, open_price, self.stop_price,
                             loss, planned, r_mult)  # fmt: skip


def replay(intent: TradePlanIntent, events: Iterable[ExitEvent]) -> ExitState:
    """Fold ``events`` from a fresh state (restart and replay)."""
    state = ExitState.start(intent)
    for ev in events:
        state = state.apply(ev)
    return state


__all__ = [
    "FINGERPRINT_PREFIX",
    "PLAN_ID_PREFIX",
    "ExitAllocation",
    "ExitAllocationPolicy",
    "ExitEvent",
    "ExitEventKind",
    "ExitOrderPlan",
    "ExitPhase",
    "ExitState",
    "ExitStateError",
    "GapAssessment",
    "StopOrderIntent",
    "TargetOrderIntent",
    "TradePlan",
    "TradePlanError",
    "TradePlanIntent",
    "TrailMode",
    "TrailingStopIntent",
    "allocate_exit_quantities",
    "exit_intent_key",
    "is_fingerprint",
    "is_plan_id",
    "new_plan_id",
    "replay",
]
