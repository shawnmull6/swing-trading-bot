"""Broker account restrictions and M2 account protections (M2-1, ARCHITECTURE §12.34).

Pure functions over the two broker snapshots; no I/O. One policy, used by the entry
gates, the pre-trade engine, exit readiness and reconciliation, so the restriction
fields are interpreted the same way everywhere.

Each restriction field has three states — **true** (restriction active), **false**
(restriction reported absent) and **unknown** (the broker did not report it, or the
snapshot carrying it could not be read). Unknown is never treated as false. A field
with a malformed value never reaches this module: the mapper raises
``InvalidBrokerDataError`` (exit 10 / ``BROKER_PAYLOAD`` halt).

Policy (offline; field semantics from the installed alpaca-py 0.45.0 docstrings, not
live-verified):

==========================  ==========================  =============  ===============
field                       source                      true blocks    unknown
==========================  ==========================  =============  ===============
status != ACTIVE            ``/v2/account``             entries+writes (required field)
trading_blocked             ``/v2/account``             entries+writes (required field)
account_blocked             ``/v2/account``             entries+writes (required field)
trade_suspended_by_user     ``/v2/account``             entries+writes blocks entries
suspend_trade               ``/account/configurations`` entries+writes (required field)
closing_transactions_only   ``/account/configurations`` entries only   blocks entries
transfers_blocked           ``/v2/account``             entries only   reported only*
==========================  ==========================  =============  ===============

``transfers_blocked``: the installed SDK documents it as concerning money transfers only.
Blocking new entries on it is a **conservative M2 policy, not a verified Alpaca order
restriction**: a wider account hold is not ruled out, and an unneeded entry block is
harmless. It never affects exits.

"writes" = the field is documented as preventing *any* order placement, so a
risk-reducing exit could not be submitted either; this is reported, never used to cancel
or suppress anything. "entries only" = new entries are refused while exits stay
permitted in principle. *For M2 paper execution every field must be known and clear
(:func:`m2_entry_problems`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from swing_trading_bot.broker.models import AccountConfigSnapshot, AccountSnapshot

ACTIVE_STATUS: Final = "ACTIVE"


class RestrictionField(StrEnum):
    STATUS = "status"
    TRADING_BLOCKED = "trading_blocked"
    ACCOUNT_BLOCKED = "account_blocked"
    TRADE_SUSPENDED_BY_USER = "trade_suspended_by_user"
    TRANSFERS_BLOCKED = "transfers_blocked"
    SUSPEND_TRADE = "suspend_trade"
    CLOSING_TRANSACTIONS_ONLY = "closing_transactions_only"


ACCOUNT_FIELDS: Final = (
    RestrictionField.STATUS,
    RestrictionField.TRADING_BLOCKED,
    RestrictionField.ACCOUNT_BLOCKED,
    RestrictionField.TRADE_SUSPENDED_BY_USER,
    RestrictionField.TRANSFERS_BLOCKED,
)
CONFIG_FIELDS: Final = (
    RestrictionField.SUSPEND_TRADE,
    RestrictionField.CLOSING_TRANSACTIONS_ONLY,
)
ORDER_WRITE_RESTRICTIONS: Final = frozenset(
    {
        RestrictionField.STATUS,
        RestrictionField.TRADING_BLOCKED,
        RestrictionField.ACCOUNT_BLOCKED,
        RestrictionField.TRADE_SUSPENDED_BY_USER,
        RestrictionField.SUSPEND_TRADE,
    }
)
"""Documented (SDK) as preventing any order placement, exits included."""
ENTRY_CRITICAL: Final = frozenset(
    {RestrictionField.TRADE_SUSPENDED_BY_USER, RestrictionField.CLOSING_TRANSACTIONS_ONLY}
)
"""Fields whose *unknown* state leaves new-entry eligibility unknown (blocks entries)."""


@dataclass(frozen=True, slots=True)
class RestrictionAssessment:
    active_fields: tuple[RestrictionField, ...]
    """Known restrictions in force."""
    active: tuple[str, ...]
    """The same, as text, e.g. ``"trade_suspended_by_user=true"``."""
    order_writes_prevented: tuple[str, ...]
    """Subset of :attr:`active` documented as preventing every order write."""
    unreported: tuple[RestrictionField, ...]
    """Fields the broker did not report (absent or ``null``) in a snapshot that was read."""
    unavailable: tuple[RestrictionField, ...]
    """Fields whose snapshot was not available (read failed or not supplied)."""

    @property
    def entry_unknown(self) -> tuple[RestrictionField, ...]:
        """Unknown fields that leave new-entry eligibility unknown: unreported
        entry-critical fields, and every field of an unavailable snapshot."""
        return (*(f for f in self.unreported if f in ENTRY_CRITICAL), *self.unavailable)

    @property
    def entries_permitted(self) -> bool:
        """True only when no restriction is active and entry eligibility is fully known."""
        return not self.active and not self.entry_unknown

    @property
    def fully_known(self) -> bool:
        return not self.unreported and not self.unavailable

    def describe(self) -> str:
        parts = list(self.active)
        parts += [f"{f.value}=unknown (not reported)" for f in self.unreported]
        parts += [f"{f.value}=unknown (snapshot unavailable)" for f in self.unavailable]
        return ", ".join(parts)


def assess_restrictions(
    account: AccountSnapshot | None, config: AccountConfigSnapshot | None
) -> RestrictionAssessment:
    values: list[tuple[RestrictionField, bool | None]] = []
    active: list[tuple[RestrictionField, str]] = []
    unavailable: list[RestrictionField] = []
    if account is None:
        unavailable.extend(ACCOUNT_FIELDS)
    else:
        if account.status != ACTIVE_STATUS:
            active.append((RestrictionField.STATUS, f"status={account.status}"))
        values += [
            (RestrictionField.TRADING_BLOCKED, account.trading_blocked),
            (RestrictionField.ACCOUNT_BLOCKED, account.account_blocked),
            (RestrictionField.TRADE_SUSPENDED_BY_USER, account.trade_suspended_by_user),
            (RestrictionField.TRANSFERS_BLOCKED, account.transfers_blocked),
        ]
    if config is None:
        unavailable.extend(CONFIG_FIELDS)
    else:
        values += [
            (RestrictionField.SUSPEND_TRADE, config.suspend_trade),
            (RestrictionField.CLOSING_TRANSACTIONS_ONLY, config.closing_transactions_only),
        ]
    active += [(name, f"{name.value}=true") for name, value in values if value is True]
    return RestrictionAssessment(
        active_fields=tuple(name for name, _ in active),
        active=tuple(text for _, text in active),
        order_writes_prevented=tuple(
            text for name, text in active if name in ORDER_WRITE_RESTRICTIONS
        ),
        unreported=tuple(name for name, value in values if value is None),
        unavailable=tuple(unavailable),
    )


def u6_problems(config: AccountConfigSnapshot | None) -> tuple[str, ...]:
    """U6 account protections: ``no_shorting=true`` and ``max_margin_multiplier=1``."""
    if config is None:
        return ("account configuration unavailable: U6 protections unverified",)
    out: list[str] = []
    if not config.no_shorting:
        out.append("no_shorting=false (shorting enabled)")
    if config.max_margin_multiplier != 1:
        out.append(f"max_margin_multiplier={config.max_margin_multiplier} (must be 1)")
    return tuple(out)


def m2_account_state_problems(
    account: AccountSnapshot | None, config: AccountConfigSnapshot | None
) -> tuple[str, ...]:
    """M2 requires *verified* account state: every restriction field reported and clear
    (including ``transfers_blocked``) and both snapshots available."""
    r = assess_restrictions(account, config)
    out = [f"restriction active: {a}" for a in r.active]
    out += [f"restriction not reported: {f.value}" for f in r.unreported]
    out += [f"restriction unverified (snapshot unavailable): {f.value}" for f in r.unavailable]
    return tuple(out)


def m2_entry_problems(
    account: AccountSnapshot | None, config: AccountConfigSnapshot | None
) -> tuple[str, ...]:
    """Everything that makes a prospective PAPER_EXECUTE entry ineligible on account
    grounds: U6 protections plus verified, unrestricted account state. Empty = eligible
    on account grounds only (every other gate still applies)."""
    return (*u6_problems(config), *m2_account_state_problems(account, config))


__all__ = [
    "ACCOUNT_FIELDS",
    "CONFIG_FIELDS",
    "ENTRY_CRITICAL",
    "ORDER_WRITE_RESTRICTIONS",
    "RestrictionAssessment",
    "RestrictionField",
    "assess_restrictions",
    "m2_account_state_problems",
    "m2_entry_problems",
    "u6_problems",
]
