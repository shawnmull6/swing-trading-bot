"""Step 3: order state machine — every pair of statuses is checked."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from swing_trading_bot.domain.enums import OrderStatus as S
from swing_trading_bot.domain.state_machine import (
    AMBIGUOUS_STATUSES,
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    IllegalTransitionError,
    allowed_targets,
    is_terminal,
    transition,
)

EXPECTED_LEGAL: set[tuple[S, S]] = {
    *((S.PENDING_SUBMIT, t) for t in (
        S.SUBMIT_AMBIGUOUS, S.ACCEPTED, S.PARTIALLY_FILLED, S.PENDING_CANCEL,
        S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED,
    )),
    *((S.SUBMIT_AMBIGUOUS, t) for t in (
        S.CONFIRMED_ABSENT, S.ACCEPTED, S.PARTIALLY_FILLED, S.PENDING_CANCEL,
        S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED,
    )),
    *((S.ACCEPTED, t) for t in (
        S.PARTIALLY_FILLED, S.PENDING_CANCEL, S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED,
    )),
    *((S.PARTIALLY_FILLED, t) for t in (S.PENDING_CANCEL, S.FILLED, S.CANCELED, S.EXPIRED)),
    *((S.PENDING_CANCEL, t) for t in (
        S.ACCEPTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED,
    )),
    *((S.CONFIRMED_ABSENT, t) for t in (
        S.ACCEPTED, S.PARTIALLY_FILLED, S.PENDING_CANCEL, S.FILLED, S.CANCELED,
        S.EXPIRED, S.REJECTED,
    )),
}  # fmt: skip


@pytest.mark.parametrize(("current", "target"), list(itertools.product(S, S)))
def test_every_pair(current: S, target: S) -> None:
    if current is target:
        t = transition(current, target)
        assert t.is_noop
        assert not t.releases_reservation
    elif (current, target) in EXPECTED_LEGAL:
        t = transition(current, target)
        assert t.target is target
        assert t.requires_halt is (current is S.CONFIRMED_ABSENT)
    else:
        with pytest.raises(IllegalTransitionError) as info:
            transition(current, target)
        assert info.value.current is current
        assert info.value.target is target


def test_table_matches_expected_exactly() -> None:
    actual = {(c, t) for c in S for t in allowed_targets(c)}
    assert actual == EXPECTED_LEGAL


def test_status_partitions() -> None:
    assert set(S) == TERMINAL_STATUSES | WORKING_STATUSES
    assert not TERMINAL_STATUSES & WORKING_STATUSES
    assert AMBIGUOUS_STATUSES <= WORKING_STATUSES
    assert is_terminal(S.FILLED)
    assert not is_terminal(S.SUBMIT_AMBIGUOUS)


def test_ambiguous_order_cannot_be_declared_absent_from_pending_submit() -> None:
    """Absence requires the ambiguous state first (A§10.3); never a direct shortcut."""
    with pytest.raises(IllegalTransitionError):
        transition(S.PENDING_SUBMIT, S.CONFIRMED_ABSENT)


def test_no_transition_out_of_filled_canceled_expired_rejected_orphan() -> None:
    for status in (S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED, S.ORPHAN):
        assert allowed_targets(status) == frozenset()


def test_late_appearance_after_confirmed_absent_requires_halt() -> None:
    t = transition(S.CONFIRMED_ABSENT, S.ACCEPTED)
    assert t.requires_halt
    assert not t.releases_reservation  # it was already released; caller re-reserves/halts


def test_reservation_released_only_entering_terminal() -> None:
    assert transition(S.PARTIALLY_FILLED, S.CANCELED).releases_reservation
    assert transition(S.ACCEPTED, S.FILLED).releases_reservation
    assert not transition(S.ACCEPTED, S.PARTIALLY_FILLED).releases_reservation
    assert not transition(S.PENDING_SUBMIT, S.SUBMIT_AMBIGUOUS).releases_reservation


@given(st.lists(st.sampled_from(list(S)), min_size=1, max_size=30))
def test_random_walks_never_leave_a_terminal_state(path: list[S]) -> None:
    """Applying arbitrary targets: once FILLED/CANCELED/EXPIRED/REJECTED/ORPHAN, stuck."""
    current = S.PENDING_SUBMIT
    for target in path:
        try:
            current = transition(current, target).target
        except IllegalTransitionError:
            continue
    sealed = {S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED, S.ORPHAN}
    if current in sealed:
        for target in S:
            if target is not current:
                with pytest.raises(IllegalTransitionError):
                    transition(current, target)
