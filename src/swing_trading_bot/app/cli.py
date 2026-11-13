"""The M1 operator CLI (M1_PLAN §4.1, ARCHITECTURE §12.20). No command can create a real
order: the observe runtime has no execution capability, and ``simulate`` uses only an
in-memory SimBroker on a separate simulation ledger.

Exit codes and their precedence are documented in :mod:`swing_trading_bot.app.exit_codes`;
the per-command side effects in :mod:`swing_trading_bot.app.safety`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn, TextIO

from pydantic import ValidationError

from swing_trading_bot.app.commands import (
    BrokerNotConfiguredError,
    Context,
    Dependencies,
    Handler,
    Outcome,
    cmd_audit,
    cmd_backup,
    cmd_broker_check,
    cmd_halt,
    cmd_halts,
    cmd_init_ledger,
    cmd_migrate,
    cmd_orders,
    cmd_portfolio,
    cmd_rebaseline_approve,
    cmd_rebaseline_propose,
    cmd_reconcile,
    cmd_resolve_order,
    cmd_resume,
    cmd_simulate,
    cmd_status,
    cmd_sync,
    cmd_verify_ledger,
    production_dependencies,
)
from swing_trading_bot.app.exit_codes import ExitCode
from swing_trading_bot.app.lock import LockHeldError
from swing_trading_bot.app.paths import DataPaths, PathError
from swing_trading_bot.broker.errors import BrokerError, payload_cause
from swing_trading_bot.config.settings import SettingsError
from swing_trading_bot.config.yaml_loader import ConfigFileError
from swing_trading_bot.ledger.errors import PersistenceError
from swing_trading_bot.observability.logging import SecretRedactor, configure_logging

_log = logging.getLogger("swing_trading_bot.cli")
SCENARIO_CHOICES = (
    "happy-path", "partial-cancel", "ambiguous-adopt", "never-received", "restart-recovery",
    "unexplained-deposit", "all",
)  # fmt: skip
HYPOTHETICAL_BANNER = "*** HYPOTHETICAL SIMULATION - no real orders; not account evidence ***"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def _nonblank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be blank")
    return value


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - one declarative command table
    common = _Parser(add_help=False)
    common.add_argument("--data-dir", default="data", help="ledger directory (default: data)")
    common.add_argument("--config-dir", default="config", help="configuration directory")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--actor", type=_nonblank, default=None,
                        help="operator identity for audit records "
                        "(default operator:<user>)")  # fmt: skip
    sim_target = _Parser(add_help=False)
    sim_target.add_argument("--sim-run", metavar="RUN_ID",
                            help="inspect a simulation ledger, not the observe ledger")  # fmt: skip

    parser = _Parser(prog="swing-trading-bot", description=__doc__,
                     formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def add(name: str, handler: Handler, help_: str, *parents: _Parser) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_, parents=[common, *parents])
        p.set_defaults(handler=handler, command_name=name)
        return p

    p = add("status", cmd_status, "read-only ledger status", sim_target)
    p.add_argument("--broker", action="store_true", help="also evaluate gates (broker reads)")
    add("portfolio", cmd_portfolio, "read-only virtual portfolio", sim_target)
    add("orders", cmd_orders, "read-only order list", sim_target)
    add("halts", cmd_halts, "read-only halt state and history", sim_target)
    p = add("audit", cmd_audit, "read-only audit trail", sim_target)
    p.add_argument("--limit", type=int, default=20)

    p = add("init-ledger", cmd_init_ledger, "create the observe ledger (confirmed)")
    p.add_argument("--confirm-path")
    p.add_argument("--confirm-account")
    p.add_argument("--confirm-environment")
    p.add_argument("--confirm-file", metavar="PATH",
                   help="private JSON file (absolute path, owned by you, mode 0600) holding "
                   "confirm_path, confirm_account and confirm_environment; replaces the "
                   "three --confirm-* options so the account id stays out of argv")  # fmt: skip
    p = add("migrate", cmd_migrate, "explicit, backed-up schema migration", sim_target)
    p.add_argument("--backup", required=True, metavar="PATH", help="new backup file")
    p = add("backup", cmd_backup, "verified online backup", sim_target)
    p.add_argument("path", metavar="PATH")
    p = add("verify-ledger", cmd_verify_ledger, "read-only verification of a ledger/backup")
    p.add_argument("path", metavar="PATH")

    add("broker-check", cmd_broker_check, "ledger-free, GET-only broker identity check")
    add("sync", cmd_sync, "read-only broker sync and recovery")
    add("reconcile", cmd_reconcile, "full reconciliation")
    p = add("halt", cmd_halt, "set an operator persistent halt")
    p.add_argument("--reason", type=_nonblank, required=True)
    p = add("resume", cmd_resume, "resume after a CLEAN reconciliation")
    p.add_argument("--reason", type=_nonblank, required=True)
    p = add("resolve-order", cmd_resolve_order, "operator absence confirmation (U4)")
    p.add_argument("client_order_id")
    p.add_argument("--confirm-absent", action="store_true", required=True)

    rb = sub.add_parser("rebaseline", help="audited two-step rebaseline")
    rbs = rb.add_subparsers(dest="rebaseline_command", required=True, parser_class=_Parser)
    p = rbs.add_parser("propose", parents=[common])
    p.set_defaults(handler=cmd_rebaseline_propose, command_name="rebaseline propose")
    p.add_argument("--reason", type=_nonblank, required=True)
    p = rbs.add_parser("approve", parents=[common])
    p.set_defaults(handler=cmd_rebaseline_approve, command_name="rebaseline approve")
    p.add_argument("request_id", type=int)
    p.add_argument("--acknowledge-unexplained", required=True, metavar="AMOUNT")
    p.add_argument("--reason", type=_nonblank, required=True)

    p = add("simulate", cmd_simulate, "offline scenario on a new simulation ledger")
    p.add_argument("scenario", choices=SCENARIO_CHOICES)
    p.add_argument("--run-id", default=None)
    return parser


def exit_code_for(exc: BaseException) -> ExitCode:  # noqa: PLR0911 - one code per family
    if isinstance(exc, LockHeldError):
        return ExitCode.LOCK_HELD
    if isinstance(
        exc, BrokerNotConfiguredError | ConfigFileError | SettingsError | ValidationError
    ):
        return ExitCode.CONFIG
    if isinstance(exc, PathError):
        return ExitCode.USAGE
    if isinstance(exc, PersistenceError):
        return ExitCode.LEDGER
    if payload_cause(exc) is not None:  # before BrokerError: a schema mismatch, direct or
        return ExitCode.BROKER_PAYLOAD  # behind an incomplete retrieval; not an outage
    if isinstance(exc, BrokerError):
        return ExitCode.BROKER_UNAVAILABLE
    return ExitCode.INTERNAL


def _render(value: object, indent: int = 0) -> list[str]:
    pad = "  " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, dict | list) and item:
                lines.append(f"{pad}{key}:")
                lines.extend(_render(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict | list):
                lines.append(f"{pad}-")
                lines.extend(_render(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return lines
    return [f"{pad}{_scalar(value)}"]


def _scalar(value: object) -> str:
    if isinstance(value, dict | list):
        return "[]" if isinstance(value, list) else "{}"
    return "-" if value is None else str(value)


def main(
    argv: Sequence[str] | None = None,
    *,
    deps: Dependencies | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except _UsageError as exc:
        if "--json" in argv:
            usage = {
                "command": None,
                "exit_code": int(ExitCode.USAGE),
                "exit_meaning": ExitCode.USAGE.name,
                "result": {"error": str(exc)},
            }
            out.write(json.dumps(usage, indent=2) + "\n")
        else:
            out.write(f"usage error: {exc}\n")
        return int(ExitCode.USAGE)
    except SystemExit as exc:  # --help
        return int(exc.code) if isinstance(exc.code, int) else int(ExitCode.USAGE)
    ctx = Context(
        command=args.command_name,
        command_id=uuid.uuid4().hex,
        paths=DataPaths.at(Path(args.data_dir)),
        config_dir=Path(args.config_dir),
        actor=args.actor or f"operator:{getpass.getuser()}",
        deps=deps if deps is not None else production_dependencies(),
    )
    log = {"command": ctx.command, "command_id": ctx.command_id}
    _log.info("command started", extra=log)
    try:
        outcome: Outcome = args.handler(ctx, args)
    except Exception as exc:  # noqa: BLE001 - every failure maps to an exit code
        code = exit_code_for(exc)
        if code is ExitCode.INTERNAL:
            _log.exception("command failed", extra=log)
        else:
            _log.warning("command refused", extra={**log, "error": str(exc)})
        outcome = Outcome(code, {"error": type(exc).__name__, "detail": str(exc)})
    envelope: dict[str, Any] = {
        "command": ctx.command,
        "command_id": ctx.command_id,
        "exit_code": int(outcome.code),
        "exit_meaning": outcome.code.name,
    }
    if outcome.hypothetical:
        envelope["hypothetical"] = True
    envelope["result"] = outcome.result
    if args.json:
        text = json.dumps(envelope, indent=2, sort_keys=False, default=str) + "\n"
    else:
        banner = HYPOTHETICAL_BANNER + "\n" if outcome.hypothetical else ""
        text = banner + "\n".join(_render(envelope)) + "\n"
    out.write(SecretRedactor(ctx.deps.secret_values).redact(text))
    _log.info("command finished", extra={**log, "exit_code": int(outcome.code)})
    return int(outcome.code)


def entrypoint() -> None:  # pragma: no cover - process entry
    configure_logging(secrets=())
    sys.exit(main())


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
