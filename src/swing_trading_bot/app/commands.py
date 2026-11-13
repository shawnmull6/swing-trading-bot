"""Command handlers: lock → open → operation → close, one function per command.

Every handler follows the order recorded in :mod:`swing_trading_bot.app.safety`:
dependency and configuration checks first (nothing touched), then the ledger lock
(exclusive for operations, shared for inspection and backup), then the database open
(never migrating), then the operation, then close; the lock is released last.

Broker access comes only from ``Dependencies.broker_factory``. The production wiring
(:func:`production_dependencies`) uses the read-only Alpaca factory with explicit
credential loading (``app/credentials.py``): process environment *or* ``STB_ENV_FILE``,
never an implicit ``.env``. Credentials are loaded only when a broker command runs.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from swing_trading_bot.app.broker_factory import (
    SessionFactory,
    account_identity_problems,
    mask_account_id,
    production_broker_factory,
)
from swing_trading_bot.app.config import D8_CAPITAL, AppConfig
from swing_trading_bot.app.exit_codes import ExitCode
from swing_trading_bot.app.init_ledger import initialize_observe_ledger
from swing_trading_bot.app.lock import LedgerLock, LockMode
from swing_trading_bot.app.paths import DataPaths
from swing_trading_bot.app.runtime import (
    ObserveBroker,
    Runtime,
    build_runtime,
    operator_halt,
    rebaseline_approve,
    rebaseline_propose,
    reconcile,
    resolve_order,
    resume,
    state_dict,
    sync_cycle,
)
from swing_trading_bot.app.views import (
    audit_view,
    halts_view,
    orders_view,
    portfolio_view,
    status_view,
)
from swing_trading_bot.broker.alpaca_http import Sleeper
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.accounting_invariants import accounting_checker
from swing_trading_bot.ledger.backup import backup_database
from swing_trading_bot.ledger.db import Database, utc_now
from swing_trading_bot.ledger.errors import DatabaseNotFoundError
from swing_trading_bot.ledger.integrity import integrity_report
from swing_trading_bot.ledger.migration_guard import detect_mode, migrate_ledger, plan_migration
from swing_trading_bot.ledger.migrator import Migration, packaged_migrations
from swing_trading_bot.ledger.portfolio import market_date
from swing_trading_bot.ledger.repositories import SyncStateRepository, TradingStateRepository
from swing_trading_bot.ledger.sync_keys import BROKER_ACCOUNT_ID
from swing_trading_bot.oms.order_manager import CrashHook


class BrokerNotConfiguredError(RuntimeError):
    """No broker factory: credential loading / live read-only access is Step 15 work."""


def _new_run_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass(frozen=True, slots=True)
class Dependencies:
    """Everything a command may touch outside the ledger, injected (tests use fakes)."""

    clock: Callable[[], datetime] = utc_now
    broker_factory: Callable[[], ObserveBroker] | None = None
    migrations: tuple[Migration, ...] | None = None
    initial_capital: Decimal = D8_CAPITAL
    max_capital_allocation: Decimal = D8_CAPITAL
    crash: CrashHook | None = None
    new_run_id: Callable[[], str] = _new_run_id
    secret_values: list[str] = field(default_factory=list)
    """Secret values loaded during this command, redacted from CLI output."""


def production_dependencies(
    environ: Mapping[str, str] | None = None,
    *,
    session_factory: SessionFactory | None = None,
    clock: Callable[[], datetime] = utc_now,
    sleep: Sleeper | None = None,
) -> Dependencies:
    """The real wiring: the read-only Alpaca factory with explicit credential loading.
    Nothing is read until a broker-backed command actually needs the broker.
    (``session_factory``/``clock``/``sleep`` exist for tests and the contract supervisor.)"""
    secrets: list[str] = []
    factory = production_broker_factory(
        environ,
        session_factory=session_factory,
        on_loaded=lambda loaded: secrets.extend(loaded.settings.secret_values()),
        clock=None if clock is utc_now else clock,
        sleep=sleep,
    )
    return Dependencies(clock=clock, broker_factory=factory, secret_values=secrets)


@dataclass(frozen=True, slots=True)
class Context:
    command: str
    command_id: str
    paths: DataPaths
    config_dir: Path
    actor: str
    deps: Dependencies

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return self.deps.migrations if self.deps.migrations is not None else packaged_migrations()

    def config(self) -> AppConfig:
        return AppConfig.load(self.config_dir)

    def broker(self) -> ObserveBroker:
        if self.deps.broker_factory is None:
            raise BrokerNotConfiguredError(
                "broker access is not configured in this build: credential loading and live "
                "read-only connectivity are separately authorized Step 15 work"
            )
        return self.deps.broker_factory()


@dataclass(slots=True)
class Outcome:
    code: ExitCode
    result: dict[str, Any]
    hypothetical: bool = False
    notes: list[str] = field(default_factory=list)


Handler = Callable[[Context, argparse.Namespace], Outcome]


# ===================================================================== helpers
def _target(ctx: Context, args: argparse.Namespace) -> tuple[Path, RuntimeMode]:
    run = getattr(args, "sim_run", None)
    if run:
        return ctx.paths.sim_ledger(run), RuntimeMode.SIMULATE
    return ctx.paths.observe_ledger, RuntimeMode.OBSERVE


def _require_ledger(path: Path) -> None:
    if not path.is_file():
        hint = " (create it with init-ledger)" if path.name == "ledger.sqlite3" else ""
        raise DatabaseNotFoundError(f"ledger not found: {path}{hint}")


def _open(ctx: Context, path: Path, mode: RuntimeMode) -> Database:
    return Database.open(
        path, mode, now=ctx.deps.clock(), migrations=ctx.migrations, auto_migrate=False
    )


def _with_ledger[T](
    ctx: Context, path: Path, mode: RuntimeMode, lock: LockMode, fn: Callable[[Database], T]
) -> T:
    _require_ledger(path)
    with LedgerLock(path, lock, command=ctx.command):
        db = _open(ctx, path, mode)
        try:
            return fn(db)
        finally:
            db.close()


def _operational[T](ctx: Context, fn: Callable[[Runtime], T]) -> tuple[T, bool]:
    """Observe-ledger operation under the exclusive lock. Returns the result and whether
    the §10.1 step-2 invariant check passed (a violation is halted by the checker)."""
    broker = ctx.broker()
    config = ctx.config()

    def run(db: Database) -> tuple[T, bool]:
        rt = build_runtime(db, broker, config, clock=ctx.deps.clock, trading_enabled=False,
                           crash=ctx.deps.crash)  # fmt: skip
        invariants = rt.pf.check_invariants(now=rt.now())
        return fn(rt), invariants.ok

    return _with_ledger(ctx, ctx.paths.observe_ledger, RuntimeMode.OBSERVE, LockMode.EXCLUSIVE,
                        run)  # fmt: skip


# ===================================================================== inspection
def cmd_status(ctx: Context, args: argparse.Namespace) -> Outcome:
    path, mode = _target(ctx, args)
    config = ctx.config()
    broker = ctx.broker() if args.broker else None

    def view(db: Database) -> dict[str, Any]:
        out = status_view(db, config, ctx.deps.clock())
        if broker is not None:
            rt = build_runtime(db, broker, config, clock=ctx.deps.clock, trading_enabled=False)
            out["gates"] = rt.gates.evaluate(now=ctx.deps.clock()).to_dict()
        return out

    result = _with_ledger(ctx, path, mode, LockMode.SHARED, view)
    return Outcome(ExitCode.OK, result, hypothetical=mode is RuntimeMode.SIMULATE)


def cmd_portfolio(ctx: Context, args: argparse.Namespace) -> Outcome:
    path, mode = _target(ctx, args)
    config = ctx.config()
    result = _with_ledger(ctx, path, mode, LockMode.SHARED,
                          lambda db: portfolio_view(db, config, ctx.deps.clock()))  # fmt: skip
    return Outcome(ExitCode.OK, result, hypothetical=mode is RuntimeMode.SIMULATE)


def cmd_orders(ctx: Context, args: argparse.Namespace) -> Outcome:
    path, mode = _target(ctx, args)
    result = _with_ledger(ctx, path, mode, LockMode.SHARED, orders_view)
    return Outcome(ExitCode.OK, result, hypothetical=mode is RuntimeMode.SIMULATE)


def cmd_halts(ctx: Context, args: argparse.Namespace) -> Outcome:
    path, mode = _target(ctx, args)
    result = _with_ledger(ctx, path, mode, LockMode.SHARED, halts_view)
    return Outcome(ExitCode.OK, result, hypothetical=mode is RuntimeMode.SIMULATE)


def cmd_audit(ctx: Context, args: argparse.Namespace) -> Outcome:
    path, mode = _target(ctx, args)
    result = _with_ledger(ctx, path, mode, LockMode.SHARED,
                          lambda db: audit_view(db, limit=args.limit))  # fmt: skip
    return Outcome(ExitCode.OK, result, hypothetical=mode is RuntimeMode.SIMULATE)


# ===================================================================== lifecycle
def cmd_init_ledger(ctx: Context, args: argparse.Namespace) -> Outcome:
    broker = ctx.broker()
    config = ctx.config()
    if not ctx.paths.root.is_dir():
        return Outcome(ExitCode.REFUSED, {"status": "REFUSED", "problems": [
            f"data directory does not exist: {ctx.paths.root}"]})  # fmt: skip
    with LedgerLock(ctx.paths.observe_ledger, LockMode.EXCLUSIVE, command=ctx.command):
        result = initialize_observe_ledger(
            ctx.paths,
            broker,
            config,
            now=ctx.deps.clock(),
            confirm_path=args.confirm_path,
            confirm_account=args.confirm_account,
            confirm_environment=args.confirm_environment,
            initial_capital=ctx.deps.initial_capital,
            max_capital_allocation=ctx.deps.max_capital_allocation,
        )
    return Outcome(result.exit_code, result.to_dict())


def cmd_migrate(ctx: Context, args: argparse.Namespace) -> Outcome:
    path, mode = _target(ctx, args)
    _require_ledger(path)
    with LedgerLock(path, LockMode.EXCLUSIVE, command=ctx.command):
        report = migrate_ledger(path, mode=mode, backup_path=Path(args.backup),
                                migrations=ctx.migrations, now=ctx.deps.clock())  # fmt: skip
    return Outcome(ExitCode.OK, report.to_dict())


def cmd_backup(ctx: Context, args: argparse.Namespace) -> Outcome:
    path, mode = _target(ctx, args)
    _require_ledger(path)
    with LedgerLock(path, LockMode.SHARED, command=ctx.command):
        plan = plan_migration(path, mode, ctx.migrations)  # identity, history, integrity
        report = backup_database(path, Path(args.path))
    return Outcome(ExitCode.OK, {**report.to_dict(), "schema_pending": list(plan.pending)})


# ===================================================================== operations
def _with_invariants(result: dict[str, Any], *, ok: bool) -> dict[str, Any]:
    return {"startup_invariants_ok": ok, **result}


def cmd_sync(ctx: Context, _args: argparse.Namespace) -> Outcome:
    report, ok = _operational(ctx, sync_cycle)
    return Outcome(report.exit_code, _with_invariants(report.to_dict(), ok=ok))


def cmd_reconcile(ctx: Context, _args: argparse.Namespace) -> Outcome:
    result, ok = _operational(ctx, reconcile)
    return Outcome(result.exit_code, _with_invariants(result.to_dict(), ok=ok))


def cmd_halt(ctx: Context, args: argparse.Namespace) -> Outcome:
    def run(db: Database) -> dict[str, Any]:
        state = operator_halt(db, actor=ctx.actor, reason=args.reason, at=ctx.deps.clock())
        return {"trading_state": state_dict(state)}

    result = _with_ledger(ctx, ctx.paths.observe_ledger, RuntimeMode.OBSERVE,
                          LockMode.EXCLUSIVE, run)  # fmt: skip
    return Outcome(ExitCode.OK, result)


def cmd_resume(ctx: Context, args: argparse.Namespace) -> Outcome:
    result, ok = _operational(ctx, lambda rt: resume(rt, actor=ctx.actor, reason=args.reason))
    return Outcome(result.exit_code, _with_invariants(result.to_dict(), ok=ok))


def cmd_resolve_order(ctx: Context, args: argparse.Namespace) -> Outcome:
    result, ok = _operational(
        ctx, lambda rt: resolve_order(rt, args.client_order_id, actor=ctx.actor)
    )
    return Outcome(result.exit_code, _with_invariants(result.to_dict(), ok=ok))


def cmd_rebaseline_propose(ctx: Context, args: argparse.Namespace) -> Outcome:
    result, ok = _operational(
        ctx, lambda rt: rebaseline_propose(rt, actor=ctx.actor, reason=args.reason)
    )
    return Outcome(result.exit_code, _with_invariants(result.to_dict(), ok=ok))


def cmd_rebaseline_approve(ctx: Context, args: argparse.Namespace) -> Outcome:
    result, ok = _operational(
        ctx,
        lambda rt: rebaseline_approve(
            rt,
            args.request_id,
            acknowledged=args.acknowledge_unexplained,
            actor=ctx.actor,
            reason=args.reason,
        ),
    )
    return Outcome(result.exit_code, _with_invariants(result.to_dict(), ok=ok))


# ===================================================================== verify
def cmd_verify_ledger(ctx: Context, args: argparse.Namespace) -> Outcome:
    """Read-only verification of any ledger file — the live ledger or a backup before a
    manual restore (D15-8). Never migrates, never writes to the ledger; takes a shared
    lock (which may create ``<path>.lock`` beside the file)."""
    path = Path(args.path).expanduser().absolute()
    _require_ledger(path)
    with LedgerLock(path, LockMode.SHARED, command=ctx.command):
        mode = detect_mode(path)
        plan = plan_migration(path, mode, ctx.migrations)  # app id, history, quick_check
        result: dict[str, Any] = {
            "ledger": str(path),
            "mode": mode.value,
            "schema_version": plan.current_version,
            "pending_migrations": list(plan.pending),
        }
        if plan.pending:
            result["note"] = "schema is older than this application: run migrate --backup"
            return Outcome(ExitCode.REFUSED, result, hypothetical=mode is RuntimeMode.SIMULATE)
        db = Database.open(path, mode, now=ctx.deps.clock(), migrations=ctx.migrations,
                           auto_migrate=False)  # fmt: skip
        try:
            with db.read() as s:
                integrity = integrity_report(s, full=True)
                invariants = accounting_checker(lambda: market_date(ctx.deps.clock())).run(s)
                state = TradingStateRepository().get(s)
                recorded = SyncStateRepository().get(s, BROKER_ACCOUNT_ID)
        finally:
            db.close()
    ok = integrity.ok and invariants.ok
    result.update(
        integrity_check_ok=integrity.ok,
        integrity_problems=integrity.problems(),
        invariants_ok=invariants.ok,
        invariant_violations=sorted({v.invariant for v in invariants.violations}),
        trading_state=state_dict(state),
        recorded_account=None if recorded is None else mask_account_id(recorded),
        verified=ok,
    )
    return Outcome(ExitCode.OK if ok else ExitCode.LEDGER, result,
                   hypothetical=mode is RuntimeMode.SIMULATE)  # fmt: skip


# ===================================================================== broker check
def cmd_broker_check(ctx: Context, _args: argparse.Namespace) -> Outcome:
    """Ledger-free, GET-only connectivity and identity check (replaces the old
    ``scripts/check_connection.py``). Never shows balances or the full account id."""
    broker = ctx.broker()
    account = broker.get_account()
    config = broker.get_account_config()
    clock = broker.get_clock()
    problems = account_identity_problems(account, environment="paper")
    result = {
        "environment": account.environment,
        "account_id": mask_account_id(account.account_id),
        "status": account.status,
        "currency": account.currency,
        "trading_blocked": account.trading_blocked,
        "account_blocked": account.account_blocked,
        "pattern_day_trader": account.pattern_day_trader,
        "no_shorting": config.no_shorting,
        "max_margin_multiplier": str(config.max_margin_multiplier),
        "m2_account_protections": config.m2_protections_in_place,
        "market_open": clock.is_open,
        "identity_problems": problems,
        "execution_available": False,
    }
    return Outcome(ExitCode.REFUSED if problems else ExitCode.OK, result)


# ===================================================================== simulation
def cmd_simulate(ctx: Context, args: argparse.Namespace) -> Outcome:
    # Imported here so observe-mode command paths never load the execution module.
    from swing_trading_bot.app.simulation import HYPOTHETICAL, SCENARIOS, run_scenario  # noqa: PLC0415

    config = ctx.config()
    if not ctx.paths.root.is_dir():
        raise DatabaseNotFoundError(f"data directory does not exist: {ctx.paths.root}")
    ctx.paths.sim_dir.mkdir(exist_ok=True)
    names: Sequence[str] = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    base = args.run_id or ctx.deps.new_run_id()
    results = []
    for name in names:
        run_id = base if len(names) == 1 else f"{base}-{name}"
        path = ctx.paths.sim_ledger(run_id)
        with LedgerLock(path, LockMode.EXCLUSIVE, command=ctx.command):
            results.append(run_scenario(ctx.paths, name, run_id, config).to_dict())
    passed = all(r["passed"] for r in results)
    return Outcome(
        ExitCode.OK if passed else ExitCode.INTERNAL,
        {"notice": HYPOTHETICAL, "passed": passed, "scenarios": results},
        hypothetical=True,
    )
