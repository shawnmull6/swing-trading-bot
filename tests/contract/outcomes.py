"""PYTEST_DONT_REWRITE

Contract outcome classification (Step 15A.1, R3/R4).

Every check runs through :func:`run_check`, which never lets an exception message or
traceback escape: it records a status and a **fixed diagnostic code** only. Rows C1-C13 are
then classified from those results:

* ``VERIFIED`` — the check passed **and** the real response exercised what the row needs;
* ``INCONCLUSIVE`` — attempted but not established (broker error, insufficient evidence,
  not run because the run stopped, or the account could not exercise the behaviour);
* ``FAILED`` — an observed response contradicts the contract;
* ``DEFERRED`` — cannot be tested in this phase.

A check that did not run is never VERIFIED.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from swing_trading_bot.broker.errors import (
    AuthenticationError,
    BrokerError,
    IncompleteRetrievalError,
    OrderNotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    payload_cause,
)
from tests.contract.checks import ContractContext, ContractViolation, InsufficientEvidence


class CheckStatus(StrEnum):
    PASSED = "PASSED"
    VIOLATION = "VIOLATION"
    INSUFFICIENT = "INSUFFICIENT"
    BROKER_ERROR = "BROKER_ERROR"
    HARNESS_ERROR = "HARNESS_ERROR"
    NOT_RUN = "NOT_RUN"


class Outcome(StrEnum):
    VERIFIED = "VERIFIED"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"


@dataclass
class CheckResult:
    status: CheckStatus
    code: str
    observation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "code": self.code, "observation": self.observation}


#: Stops the whole run after the check (no further requests).
STOP_CODES = frozenset({"BROKER_AUTH_FAILED", "BROKER_RATE_LIMITED", "ACCOUNT_IDENTITY_INVALID"})


def classify_exception(exc: Exception) -> CheckResult:  # noqa: PLR0911 - one code per class
    """Fixed codes only; the exception's message is never read."""
    if isinstance(exc, ContractViolation):
        return CheckResult(CheckStatus.VIOLATION, exc.code)
    if isinstance(exc, InsufficientEvidence):
        return CheckResult(CheckStatus.INSUFFICIENT, exc.code)
    if isinstance(exc, AuthenticationError):
        return CheckResult(CheckStatus.BROKER_ERROR, "BROKER_AUTH_FAILED")
    if isinstance(exc, RateLimitedError):
        return CheckResult(CheckStatus.BROKER_ERROR, "BROKER_RATE_LIMITED")
    if isinstance(exc, ServiceUnavailableError):
        return CheckResult(CheckStatus.BROKER_ERROR, f"BROKER_UNAVAILABLE_{exc.kind.upper()}")
    if payload_cause(exc) is not None:
        # The broker's response broke the parsing contract the code relies on — directly,
        # or on a page inside an interrupted retrieval (Step 15B.5B; e.g. C7).
        return CheckResult(CheckStatus.VIOLATION, "BROKER_PAYLOAD_CONTRACT_BROKEN")
    if isinstance(exc, IncompleteRetrievalError):
        return CheckResult(CheckStatus.BROKER_ERROR, "BROKER_INCOMPLETE_RETRIEVAL")
    if isinstance(exc, OrderNotFoundError):
        return CheckResult(CheckStatus.BROKER_ERROR, "BROKER_NOT_FOUND")
    if isinstance(exc, BrokerError):
        return CheckResult(CheckStatus.BROKER_ERROR, f"BROKER_{type(exc).__name__.upper()}")
    return CheckResult(CheckStatus.HARNESS_ERROR, f"HARNESS_{type(exc).__name__.upper()}")


def run_check(check: Callable[[ContractContext], dict[str, Any]], ctx: ContractContext
              ) -> CheckResult:  # fmt: skip
    """Run one check. ``BaseException`` (budget exhaustion, write attempts) propagates so
    the supervisor can stop the run; every ``Exception`` becomes a sanitized result."""
    try:
        return CheckResult(CheckStatus.PASSED, "OK", check(ctx))
    except Exception as exc:  # noqa: BLE001 - classified to a fixed code, message discarded
        return classify_exception(exc)


# ============================================================ row classification
Rule = Callable[[dict[str, CheckResult]], tuple[Outcome, str]]


def _from(check: str, exercised: Callable[[dict[str, Any]], bool] | None = None,
          unexercised_code: str = "") -> Rule:  # fmt: skip
    def rule(results: dict[str, CheckResult]) -> tuple[Outcome, str]:
        r = results.get(check) or CheckResult(CheckStatus.NOT_RUN, "NOT_RUN")
        if r.status is CheckStatus.VIOLATION:
            return Outcome.FAILED, r.code
        if r.status is not CheckStatus.PASSED:
            return Outcome.INCONCLUSIVE, r.code
        if exercised is not None and not exercised(r.observation):
            return Outcome.INCONCLUSIVE, unexercised_code
        return Outcome.VERIFIED, "OK"

    return rule


def _deferred(code: str) -> Rule:
    return lambda _results: (Outcome.DEFERRED, code)


RULES: dict[str, Rule] = {
    "C1": _from("account_identity"),
    "C2": _from("account_restrictions"),
    "C3": _from("clock"),
    "C4": _from("calendars"),
    "C5": _from("positions_and_orders", lambda o: bool(o.get("order_list_semantics_exercised")),
                "FLAT_ACCOUNT_ORDER_SEMANTICS_UNEXERCISED"),
    "C6": _from("unknown_client_order_id"),
    "C7": _from("activity_pagination", lambda o: bool(o.get("multi_page_exercised")),
                "TOO_FEW_ACTIVITIES_TO_EXERCISE_PAGINATION"),
    "C8": _from("market_data"),
    "C9": _from("raw_decimals", lambda o: bool(o.get("non_account_numerics_checked")),
                "ONLY_ACCOUNT_NUMERICS_OBSERVED"),
    "C10": _deferred("MALFORMED_PAYLOADS_CANNOT_BE_PROVOKED_READ_ONLY"),
    "C11": _deferred("AUTH_ERROR_CHECK_NOT_AUTHORIZED_D15_7"),
    "C12": _deferred("POSTING_LAG_NEEDS_FILLS"),
    "C13": _deferred("NO_OFFICIAL_CLOSE_SOURCE_READ_ONLY"),
}  # fmt: skip


def classify_rows(results: dict[str, CheckResult]) -> dict[str, dict[str, str]]:
    return {row: dict(zip(("outcome", "code"), (o.value, c), strict=True))
            for row, (o, c) in ((row, rule(results)) for row, rule in RULES.items())}  # fmt: skip
