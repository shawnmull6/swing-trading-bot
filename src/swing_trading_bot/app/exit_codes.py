"""CLI exit codes and their precedence (ARCHITECTURE §12.20).

When several conditions apply to one command, the code earliest in :data:`PRECEDENCE`
wins: an internal error over everything; then usage, lock, configuration and ledger
problems (the command could not start safely); then a persistent halt raised during
this run or a reconciliation MISMATCH; then broker unavailability; then INCONCLUSIVE
evidence; then a plain refusal; OK only when nothing else applies.

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


PRECEDENCE: Final = (
    ExitCode.INTERNAL,
    ExitCode.USAGE,
    ExitCode.LOCK_HELD,
    ExitCode.CONFIG,
    ExitCode.LEDGER,
    ExitCode.HALT_OR_MISMATCH,
    ExitCode.BROKER_UNAVAILABLE,
    ExitCode.INCONCLUSIVE,
    ExitCode.REFUSED,
    ExitCode.OK,
)


def combine(*codes: ExitCode) -> ExitCode:
    """The highest-precedence code among ``codes`` (OK when empty)."""
    present = set(codes)
    return next((c for c in PRECEDENCE if c in present), ExitCode.OK)
