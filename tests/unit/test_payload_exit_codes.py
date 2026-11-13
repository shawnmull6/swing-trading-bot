"""Step 15B.3: runtime exit-code classification of malformed broker payloads.

* A malformed read that raised the persistent BROKER_PAYLOAD halt in this operation, from
  an unhalted ledger, with no other halt raised -> ``BROKER_PAYLOAD`` (10).
* An operation blocked by an already-recorded persistent halt -> ``HALT_OR_MISMATCH`` (4).
* Any other halt raised alongside it -> ``HALT_OR_MISMATCH`` (precedence unchanged).
* A malformed payload is never ``BROKER_UNAVAILABLE`` (8).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from swing_trading_bot.app.exit_codes import ExitCode
from swing_trading_bot.app.runtime import CycleReport, PreflightReport
from swing_trading_bot.domain.enums import HaltReason
from swing_trading_bot.ledger.repositories import TradingState

T0 = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
PAYLOAD = HaltReason.BROKER_PAYLOAD


def state(halted: bool, reason: str | None = None, at: datetime = T0) -> TradingState:
    return TradingState(halted, reason, None, at, "runtime")


def payload_preflight(*, halted: HaltReason | None, already: bool) -> PreflightReport:
    return PreflightReport(broker_error="InvalidBrokerDataError: x", halted=halted,
                           payload_error=True, already_halted=already)  # fmt: skip


@pytest.mark.parametrize(
    ("halted", "already", "code"),
    [
        (PAYLOAD, False, ExitCode.BROKER_PAYLOAD),  # raised by this operation
        (None, True, ExitCode.HALT_OR_MISMATCH),  # same halt already recorded
        (PAYLOAD, True, ExitCode.HALT_OR_MISMATCH),  # replaced an existing other halt
    ],
)
def test_preflight_payload_codes(halted: HaltReason | None, already: bool, code: ExitCode
                                 ) -> None:  # fmt: skip
    assert payload_preflight(halted=halted, already=already).exit_code is code


def test_non_payload_preflight_codes_unchanged() -> None:
    assert PreflightReport(broker_error="ServiceUnavailableError: x").exit_code is (
        ExitCode.BROKER_UNAVAILABLE
    )
    auth = PreflightReport(broker_error="AuthenticationError: x", halted=HaltReason.AUTH)
    assert auth.exit_code is ExitCode.HALT_OR_MISMATCH
    assert PreflightReport(identity_problem="x", identity_mismatch=True).exit_code is (
        ExitCode.HALT_OR_MISMATCH
    )


def cycle(before: TradingState, after: TradingState, raised: tuple[str, ...],
          pre: PreflightReport | None = None) -> CycleReport:  # fmt: skip
    return CycleReport(before=before, after=after, preflight=pre or PreflightReport(),
                       halts_raised=raised)  # fmt: skip


def test_cycle_with_only_a_new_payload_halt_is_10() -> None:
    later = T0 + timedelta(minutes=1)
    pre = payload_preflight(halted=PAYLOAD, already=False)
    c = cycle(state(False), state(True, PAYLOAD.value, later), (PAYLOAD.value,), pre)
    assert c.payload_halt_only and c.exit_code is ExitCode.BROKER_PAYLOAD


def test_cycle_with_payload_and_another_halt_is_4() -> None:
    later = T0 + timedelta(minutes=1)
    raised = (HaltReason.LEDGER_INVARIANT.value, PAYLOAD.value)
    c = cycle(state(False), state(True, PAYLOAD.value, later), raised)
    assert not c.payload_halt_only and c.exit_code is ExitCode.HALT_OR_MISMATCH


def test_cycle_starting_halted_is_4() -> None:
    later = T0 + timedelta(minutes=1)
    c = cycle(state(True, "OPERATOR"), state(True, PAYLOAD.value, later), (PAYLOAD.value,))
    assert not c.payload_halt_only and c.exit_code is ExitCode.HALT_OR_MISMATCH


def test_cycle_with_another_new_halt_is_still_4() -> None:
    later = T0 + timedelta(minutes=1)
    c = cycle(state(False), state(True, "AUTH", later), ("AUTH",))
    assert c.exit_code is ExitCode.HALT_OR_MISMATCH
