"""CLI exit codes and their precedence (ARCHITECTURE §12.20).

When several conditions apply to one command, the code earliest in :data:`PRECEDENCE`
wins: an internal error over everything; then usage, lock, configuration and ledger
problems (the command could not start safely); then a persistent halt raised during
this run or a reconciliation MISMATCH; then a broker payload that broke the parsing
contract; then broker unavailability; then INCONCLUSIVE evidence; then a plain refusal;
OK only when nothing else applies.

``BROKER_PAYLOAD`` (Step 15B.2) is a fail-closed schema/contract mismatch
(``InvalidBrokerDataError``): retrying will not help, unlike ``BROKER_UNAVAILABLE``.
Inside the runtime a malformed read still raises the persistent ``BROKER_PAYLOAD`` halt
and never counts toward a broker outage (Step 15B.3): the operation that raised it from
an un-halted ledger, with no other new halt, reports ``BROKER_PAYLOAD``; an operation
governed by an already-recorded persistent halt reports ``HALT_OR_MISMATCH``.

* Informational commands (``status``, ``portfolio``, ``orders``, ``halts``, ``audit``)
  return OK while *reporting* an existing halt.
* Operational commands return non-zero whenever the requested operation was refused or
  could not be completed safely. An INCONCLUSIVE or MISMATCH reconciliation is never OK.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final


class ExitCode(IntEnum):
    OK = 0
    INTERNAL = 1
    USAGE = 2
    REFUSED = 3
    HALT_OR_MISMATCH = 4
    INCONCLUSIVE = 5
    LOCK_HELD = 6
    CONFIG = 7
    BROKER_UNAVAILABLE = 8
    LEDGER = 9
    BROKER_PAYLOAD = 10


PRECEDENCE: Final = (
    ExitCode.INTERNAL,
    ExitCode.USAGE,
    ExitCode.LOCK_HELD,
    ExitCode.CONFIG,
    ExitCode.LEDGER,
    ExitCode.HALT_OR_MISMATCH,
    ExitCode.BROKER_PAYLOAD,
    ExitCode.BROKER_UNAVAILABLE,
    ExitCode.INCONCLUSIVE,
    ExitCode.REFUSED,
    ExitCode.OK,
)


def combine(*codes: ExitCode) -> ExitCode:
    """The highest-precedence code among ``codes`` (OK when empty)."""
    present = set(codes)
    return next((c for c in PRECEDENCE if c in present), ExitCode.OK)
