"""Conservative fee estimates used for pre-trade sizing (M1 step 11). Pure logic.

Estimates, not broker truth: actual fees are booked from broker activities (§12.8).
Each component is rounded **up** to the cent so estimates never understate cost.

* Buys: per-order and per-share commission (Alpaca equities: zero).
* Sells (used for the exit leg of the risk budget): FINRA TAF per share (capped per
  order) and the SEC Section 31 fee per notional dollar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from swing_trading_bot.domain.money import DECIMAL_CONTEXT, PENNY, ZERO


def _cents_up(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_CEILING, context=DECIMAL_CONTEXT)


@dataclass(frozen=True, slots=True)
class FeeModel:
    buy_per_order: Decimal
    buy_per_share: Decimal
    sell_taf_per_share: Decimal
    sell_taf_max_per_order: Decimal
    sell_sec_per_notional: Decimal

    def __post_init__(self) -> None:
        for name in ("buy_per_order", "buy_per_share", "sell_taf_per_share",
                     "sell_taf_max_per_order", "sell_sec_per_notional"):  # fmt: skip
            if getattr(self, name) < ZERO:
                raise ValueError(f"{name} must not be negative")

    def buy_fee(self, qty: Decimal) -> Decimal:
        if qty <= ZERO:
            return ZERO
        return _cents_up(self.buy_per_order + self.buy_per_share * qty)

    def sell_fee(self, qty: Decimal, price: Decimal) -> Decimal:
        if qty <= ZERO:
            return ZERO
        taf = min(_cents_up(self.sell_taf_per_share * qty), self.sell_taf_max_per_order)
        return taf + _cents_up(self.sell_sec_per_notional * qty * price)
