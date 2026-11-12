"""Domain enumerations. Broker-agnostic: nothing here imports a broker SDK."""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"
    GTC = "GTC"


class OrderStatus(StrEnum):
    """Local order lifecycle states (ARCHITECTURE §6.4)."""

    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMIT_AMBIGUOUS = "SUBMIT_AMBIGUOUS"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    CONFIRMED_ABSENT = "CONFIRMED_ABSENT"
    ORPHAN = "ORPHAN"


class IntentPurpose(StrEnum):
    """Why an order exists. Drives the same-session policy (U5)."""

    ENTRY = "ENTRY"
    DISCRETIONARY_EXIT = "DISCRETIONARY_EXIT"
    RISK_EXIT = "RISK_EXIT"


class OrderRole(StrEnum):
    """Reserved for the RTH-01 TradePlan aggregate (U9). Always NULL in M1."""

    ENTRY = "ENTRY"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TARGET = "TARGET"
    EXIT = "EXIT"


class AssetClass(StrEnum):
    US_EQUITY = "US_EQUITY"


class InstrumentType(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"


class AccountType(StrEnum):
    CASH = "CASH"
    MARGIN = "MARGIN"


class RuntimeMode(StrEnum):
    OBSERVE = "observe"
    SIMULATE = "simulate"
    PAPER_EXECUTE = "paper_execute"  # M2+, not constructible in M1


class MarkSource(StrEnum):
    LATEST_TRADE = "LATEST_TRADE"
    SESSION_CLOSE_BAR = "SESSION_CLOSE_BAR"
    SIM = "SIM"


class ActivityAttribution(StrEnum):
    ATTRIBUTABLE = "ATTRIBUTABLE"
    EXTERNAL = "EXTERNAL"
    UNEXPLAINED = "UNEXPLAINED"


class ReconciliationResult(StrEnum):
    OK = "OK"
    MISMATCH = "MISMATCH"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


class HaltReason(StrEnum):
    """Reasons for a *persistent* halt (ARCHITECTURE §7)."""

    INITIALIZED = "INITIALIZED"
    OPERATOR = "OPERATOR"
    RECON_ACCOUNT = "RECON_ACCOUNT"
    RECON_POSITION = "RECON_POSITION"
    RECON_ORDER = "RECON_ORDER"
    RECON_EXECUTION = "RECON_EXECUTION"
    RECON_CASH = "RECON_CASH"
    RECON_INCONCLUSIVE_REPEAT = "RECON_INCONCLUSIVE_REPEAT"
    LEDGER_INVARIANT = "LEDGER_INVARIANT"
    AUTH = "AUTH"
    UNMAPPED_BROKER_STATUS = "UNMAPPED_BROKER_STATUS"
    UNEXPECTED_BROKER_STATUS = "UNEXPECTED_BROKER_STATUS"
    BROKER_PAYLOAD = "BROKER_PAYLOAD"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    AMBIGUOUS_ORDER_TIMEOUT = "AMBIGUOUS_ORDER_TIMEOUT"
    BROKER_OUTAGE = "BROKER_OUTAGE"
    DAILY_LOSS_REPEAT = "DAILY_LOSS_REPEAT"
    WEEKLY_DRAWDOWN = "WEEKLY_DRAWDOWN"
    REBASELINE = "REBASELINE"


class BlockReason(StrEnum):
    """Reasons for a *transient* block, re-evaluated continuously (ARCHITECTURE §7)."""

    STARTUP_GATE = "STARTUP_GATE"
    STALE_SYNC = "STALE_SYNC"
    MARKET_CLOSED = "MARKET_CLOSED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    STALE_MARKS = "STALE_MARKS"
    AMBIGUOUS_ORDER = "AMBIGUOUS_ORDER"
    RECON_INCONCLUSIVE = "RECON_INCONCLUSIVE"
    CALENDAR_MISSING = "CALENDAR_MISSING"
    BASELINE_MISSING = "BASELINE_MISSING"
    BROKER_TRADING_BLOCKED = "BROKER_TRADING_BLOCKED"
    DAILY_LOSS_LOCKOUT = "DAILY_LOSS_LOCKOUT"
