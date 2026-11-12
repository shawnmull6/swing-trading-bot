"""The command side-effect matrix (ARCHITECTURE §12.20).

This table is the contract for every CLI command; ``tests/integration/test_cli_safety``
checks each row against the command's observable behaviour (broker calls, ledger
fingerprint, halt events, lock mode).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from swing_trading_bot.app.lock import LockMode


class Broker(StrEnum):
    NONE = "none"
    READ_ONLY = "read-only"
    SIMBROKER = "SimBroker only (in memory)"


class LedgerWrites(StrEnum):
    NONE = "none"
    CREATE = "creates a new ledger"
    SCHEMA = "schema migration (after verified backup)"
    NEW_FILE = "writes a new backup file only"
    OPERATIONAL = "sync / reconciliation / audit records"
    OPERATOR = "operator audit records"
    SIM_ONLY = "new simulation ledger only"


class Recovery(StrEnum):
    YES = "yes"
    NO = "no"


class Halts(StrEnum):
    NONE = "none"
    CREATES_INITIALIZED = "creates INITIALIZED"
    MAY_CREATE = "may create (never clears)"
    CREATES_OPERATOR = "creates OPERATOR"
    MAY_CLEAR = "may create; clears only after a CLEAN reconciliation"
    REBASELINE = "re-records REBASELINE (halt kept)"
    SIM_ONLY = "inside the simulation ledger only"


@dataclass(frozen=True, slots=True)
class CommandSafety:
    command: str
    broker: Broker
    ledger_writes: LedgerWrites
    oms_recovery: Recovery
    halts: Halts
    confirmation: str
    lock: LockMode | None
    sequence: str


_SH, _EX = LockMode.SHARED, LockMode.EXCLUSIVE
_INSPECT = "open (no migration) → one read snapshot"
COMMAND_SAFETY: Final = (
    CommandSafety("status", Broker.NONE, LedgerWrites.NONE, Recovery.NO, Halts.NONE, "none", _SH,
                  _INSPECT),
    CommandSafety("status --broker", Broker.READ_ONLY, LedgerWrites.NONE, Recovery.NO, Halts.NONE,
                  "none", _SH, "broker reads (account, config, clock) → read-only gate "
                  "evaluation"),
    CommandSafety("portfolio", Broker.NONE, LedgerWrites.NONE, Recovery.NO, Halts.NONE, "none", _SH,
                  _INSPECT),
    CommandSafety("orders", Broker.NONE, LedgerWrites.NONE, Recovery.NO, Halts.NONE, "none", _SH,
                  _INSPECT),
    CommandSafety("halts", Broker.NONE, LedgerWrites.NONE, Recovery.NO, Halts.NONE, "none", _SH,
                  _INSPECT),
    CommandSafety("audit", Broker.NONE, LedgerWrites.NONE, Recovery.NO, Halts.NONE, "none", _SH,
                  _INSPECT),
    CommandSafety("init-ledger", Broker.READ_ONLY, LedgerWrites.CREATE, Recovery.NO,
                  Halts.CREATES_INITIALIZED,
                  "--confirm-path, --confirm-account, --confirm-environment must match", _EX,
                  "target checks → broker evidence → confirmations → build under a temp name "
                  "→ no-clobber link"),
    CommandSafety("migrate", Broker.NONE, LedgerWrites.SCHEMA, Recovery.NO, Halts.NONE,
                  "--backup PATH (new file) required", _EX,
                  "verify history → integrity → verified backup → all-or-nothing migration "
                  "→ reopen"),
    CommandSafety("backup", Broker.NONE, LedgerWrites.NEW_FILE, Recovery.NO, Halts.NONE,
                  "PATH must not exist", _SH, "open (no migration) → online backup → verify"),
    CommandSafety("sync", Broker.READ_ONLY, LedgerWrites.OPERATIONAL, Recovery.YES,
                  Halts.MAY_CREATE,
                  "none", _EX, "preflight → calendar → recovery lookups → sync → escalate "
                  "→ marks → loss limits → last-sync marker"),
    CommandSafety("reconcile", Broker.READ_ONLY, LedgerWrites.OPERATIONAL, Recovery.YES,
                  Halts.MAY_CREATE, "none", _EX, "sync sequence → reconciliation → startup "
                  "readiness"),
    CommandSafety("halt", Broker.NONE, LedgerWrites.OPERATOR, Recovery.NO, Halts.CREATES_OPERATOR,
                  "--reason required", _EX, "one transaction: halt + halt_events"),
    CommandSafety("resume", Broker.READ_ONLY, LedgerWrites.OPERATIONAL, Recovery.YES,
                  Halts.MAY_CLEAR,
                  "--reason required", _EX, "reconcile sequence → guarded resume (refusal "
                  "audited)"),
    CommandSafety("resolve-order", Broker.READ_ONLY, LedgerWrites.OPERATIONAL, Recovery.YES,
                  Halts.MAY_CREATE, "--confirm-absent required", _EX,
                  "preflight → lookup → list scan → activity scan → reconciliation → "
                  "evidence-gated confirm_absent"),
    CommandSafety("rebaseline propose", Broker.READ_ONLY, LedgerWrites.OPERATIONAL, Recovery.NO,
                  Halts.MAY_CREATE, "--reason required; ledger must be halted", _EX,
                  "preflight → propose (runs its own reconciliation)"),
    CommandSafety("rebaseline approve", Broker.READ_ONLY, LedgerWrites.OPERATIONAL, Recovery.NO,
                  Halts.REBASELINE, "--acknowledge-unexplained exact amount and --reason", _EX,
                  "preflight → confirm (second reconciliation; bound to the proposal)"),
    CommandSafety("simulate", Broker.SIMBROKER, LedgerWrites.SIM_ONLY, Recovery.YES, Halts.SIM_ONLY,
                  "none", _EX, "new sim ledger → scripted scenario → summary (hypothetical)"),
)  # fmt: skip

INSPECTION_COMMANDS: Final = frozenset({"status", "portfolio", "orders", "halts", "audit"})


def safety(command: str) -> CommandSafety:
    return next(c for c in COMMAND_SAFETY if c.command == command)


def render_markdown() -> str:
    head = (
        "| Command | Broker | Ledger writes | OMS recovery | Halts | Confirmation | Lock |\n"
        "|---|---|---|---|---|---|---|"
    )
    rows = [
        f"| `{c.command}` | {c.broker.value} | {c.ledger_writes.value} | "
        f"{c.oms_recovery.value} | {c.halts.value} | {c.confirmation} | "
        f"{c.lock.value if c.lock else '-'} |"
        for c in COMMAND_SAFETY
    ]
    return "\n".join([head, *rows])
