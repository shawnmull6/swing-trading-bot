"""The fixed pilot research universe (Step 16.15, Phase 1).

Twenty present-day, large and liquid US listings chosen by hand. **This is a pilot
dataset, not a survivorship-bias-free sample**: every member exists and is liquid today,
so results on it overstate what a rule applied to the universe as it looked in 2021
would have achieved (no delisted, acquired or collapsed names can appear). Members are
research candidates only: nothing here makes a symbol eligible to trade.

**Membership policy** (``FIXED_PRESENT_DAY_PILOT``): the list is fixed for the whole
research window, never re-selected by performance, and never silently substituted. A
member whose data cannot be validated is reported and excluded from results, not
replaced. Changing the list creates a new universe id (and a new fingerprint).

**Identity.** The research identity is a stable key (the current ticker). Ticker changes
inside the window are recorded in the dataset's identity file, which every bar row is
checked against. Known ticker changes and corporate events below are *expectations to
verify during acquisition* (they come from general knowledge, not from an attested
source), and are never used as corporate-action data.

**ETF treatment.** ETFs are evaluated with the same rules and filters as equities.
Their distributions are booked as cash on the ex-date (ordinary dividends, capital-gain
distributions and return of capital alike; tax character is ignored). An ETF with an
unattested distribution history is rejected like any equity.

**Trading eligibility.** Not established here. Asset status (``tradable``, ``status``,
``fractionable``) is an M2-V2 pre-execution check against ``GET /v2/assets/{symbol}`` and
is out of scope for offline research.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Final


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    BROAD_MARKET_ETF = "BROAD_MARKET_ETF"
    SECTOR_ETF = "SECTOR_ETF"


class MembershipPolicy(StrEnum):
    FIXED_PRESENT_DAY_PILOT = "FIXED_PRESENT_DAY_PILOT"


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    sector: str
    asset_class: AssetClass
    expectations: tuple[str, ...] = ()
    """Unverified notes to check during acquisition (ticker changes, splits)."""


@dataclass(frozen=True, slots=True)
class ResearchUniverse:
    universe_id: str
    policy: MembershipPolicy
    members: tuple[UniverseMember, ...]
    survivorship_bias_free: bool
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        symbols = [m.symbol for m in self.members]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate universe member")
        if symbols != sorted(symbols):
            raise ValueError("members must be sorted by symbol")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(m.symbol for m in self.members)

    @property
    def sectors(self) -> dict[str, str]:
        return {m.symbol: m.sector for m in self.members}

    def member(self, symbol: str) -> UniverseMember:
        for m in self.members:
            if m.symbol == symbol:
                return m
        raise KeyError(symbol)

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


_E = AssetClass.EQUITY
_B = AssetClass.BROAD_MARKET_ETF
_S = AssetClass.SECTOR_ETF
_VERIFY = "verify during acquisition"

PILOT_UNIVERSE: Final = ResearchUniverse(
    universe_id="pilot-20-v1",
    policy=MembershipPolicy.FIXED_PRESENT_DAY_PILOT,
    members=tuple(sorted((
        UniverseMember("NVDA", "Technology", _E,
                       (f"forward splits expected 2021-07 (4:1) and 2024-06 (10:1); {_VERIFY}",)),
        UniverseMember("AMD", "Technology", _E),
        UniverseMember("MSFT", "Technology", _E),
        UniverseMember("AAPL", "Technology", _E),
        UniverseMember("META", "Communication", _E,
                       (f"traded as FB before a 2022-06 ticker change; {_VERIFY} how the "
                        "provider keys pre-change bars",)),
        UniverseMember("GOOGL", "Communication", _E,
                       (f"forward split expected 2022-07 (20:1); {_VERIFY}",)),
        UniverseMember("AMZN", "Consumer discretionary", _E,
                       (f"forward split expected 2022-06 (20:1); {_VERIFY}",)),
        UniverseMember("TSLA", "Consumer discretionary", _E,
                       (f"forward split expected 2022-08 (3:1); {_VERIFY}",)),
        UniverseMember("JPM", "Financials", _E),
        UniverseMember("GS", "Financials", _E),
        UniverseMember("CAT", "Industrials", _E),
        UniverseMember("BA", "Industrials", _E),
        UniverseMember("UNH", "Healthcare", _E),
        UniverseMember("ABBV", "Healthcare", _E),
        UniverseMember("XOM", "Energy", _E),
        UniverseMember("CVX", "Energy", _E),
        UniverseMember("SPY", "Broad market", _B),
        UniverseMember("QQQ", "Broad market", _B),
        UniverseMember("XLF", "Financials", _S),
        UniverseMember("XLE", "Energy", _S),
    ), key=lambda m: m.symbol)),
    survivorship_bias_free=False,
    limitations=(
        "Present-day survivors chosen with hindsight: no delisted, acquired or failed "
        "names; results are optimistic relative to a point-in-time universe.",
        "Large-cap, highly liquid names only: fills and slippage are more favourable than "
        "for the broader market.",
        "Sector ETFs overlap their constituents (XLF with JPM/GS, XLE with XOM/CVX): "
        "signals are correlated, so 20 symbols are fewer than 20 independent bets.",
        "Trading eligibility at Alpaca is not verified (M2-V2).",
    ),
)  # fmt: skip


__all__ = [
    "PILOT_UNIVERSE",
    "AssetClass",
    "MembershipPolicy",
    "ResearchUniverse",
    "UniverseMember",
]
