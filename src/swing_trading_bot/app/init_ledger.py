"""``init-ledger``: explicit, confirmed creation of the account-backed observe ledger.

Safety properties (ARCHITECTURE §8.1, §12.20; D15):

* Only this command creates an observe ledger; ``status``, ``sync``, ``reconcile`` and
  startup never do (``Database.open`` refuses a missing file).
* The target must not exist (neither the ledger nor any SQLite sidecar), and the data
  directory may contain only expected entries.
* The exclusive ledger lock is taken before anything is read or written.
* Broker evidence is gathered first, outside any transaction: account, account
  configuration, clock, positions, open orders, a tail-confirmed activity window and
  both calendars, then account, positions and open orders again. Initialization is
  refused unless the account is flat, unrestricted, USD, without pending fees, and the
  two observations agree.
* The operator must confirm the exact target path, broker account id and environment.
  Without matching confirmations nothing is created; the preview lists the values.
* The ledger is built under a temporary name and linked into place only when complete
  (no-clobber), so an interrupted initialization never leaves a half-built ledger at
  the target path.
* The broker baseline (broker cash/equity) and the $5,000 virtual portfolio are
  separate records; broker cash is never virtual capital. The INITIALIZED persistent
  halt written at database creation is kept: the first ``resume`` is a deliberate act.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from swing_trading_bot.app.config import (
    ACTIVITY_POSTING_LAG,
    CALENDAR_FETCH_AHEAD,
    CALENDAR_FETCH_BACK,
    PORTFOLIO_NAME,
    AppConfig,
)
from swing_trading_bot.app.exit_codes import ExitCode
from swing_trading_bot.app.paths import SQLITE_SIDECARS, DataPaths
from swing_trading_bot.app.runtime import ObserveBroker
from swing_trading_bot.broker.activity_feed import DEFAULT_RESCAN_POLICY, collect_window
from swing_trading_bot.broker.errors import BrokerError
from swing_trading_bot.broker.models import AccountConfigSnapshot, AccountSnapshot
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarFetch, CalendarKind
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.baselines import BrokerBaselinesRepository
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.db import Database, assert_no_transaction
from swing_trading_bot.ledger.integrity import integrity_report
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import SyncStateRepository, TradingStateRepository
from swing_trading_bot.ledger.sync_keys import BROKER_ACCOUNT_ID, BROKER_ENVIRONMENT

ZERO = Decimal(0)


class InitStatus(StrEnum):
    INITIALIZED = "INITIALIZED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    REFUSED = "REFUSED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"


@dataclass(slots=True)
class InitEvidence:
    observed_at: datetime
    account: AccountSnapshot | None = None
    account_config: AccountConfigSnapshot | None = None
    positions: int | None = None
    open_orders: int | None = None
    activities: int | None = None
    activity_types: tuple[str, ...] = ()
    activity_tail_confirmed: bool = False
    last_activity_id: str | None = None
    calendars: tuple[CalendarFetch, ...] = ()
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        a, c = self.account, self.account_config
        return {
            "observed_at": self.observed_at.isoformat(),
            "account_id": None if a is None else a.account_id,
            "environment": None if a is None else a.environment,
            "account_status": None if a is None else a.status,
            "currency": None if a is None else a.currency,
            "trading_blocked": None if a is None else a.trading_blocked,
            "account_blocked": None if a is None else a.account_blocked,
            "positions": self.positions,
            "open_orders": self.open_orders,
            "activities_in_lookback": self.activities,
            "activity_types": list(self.activity_types),
            "activity_tail_confirmed": self.activity_tail_confirmed,
            "m2_account_protections": None if c is None else c.m2_protections_in_place,
            "calendars": [f"{f.kind.value} {f.start}..{f.end}" for f in self.calendars],
            "problems": list(self.problems),
        }


@dataclass(slots=True)
class InitResult:
    status: InitStatus
    ledger: str
    evidence: InitEvidence | None = None
    problems: list[str] = field(default_factory=list)
    required_confirmation: dict[str, str] | None = None
    created: dict[str, Any] | None = None

    @property
    def exit_code(self) -> ExitCode:
        return {
            InitStatus.INITIALIZED: ExitCode.OK,
            InitStatus.CONFIRMATION_REQUIRED: ExitCode.REFUSED,
            InitStatus.REFUSED: ExitCode.REFUSED,
            InitStatus.BROKER_UNAVAILABLE: ExitCode.BROKER_UNAVAILABLE,
        }[self.status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "ledger": self.ledger,
            "problems": list(self.problems),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "required_confirmation": self.required_confirmation,
            "created": self.created,
        }


def check_target(paths: DataPaths) -> list[str]:
    problems: list[str] = []
    if not paths.root.is_dir():
        return [f"data directory does not exist: {paths.root}"]
    existing = paths.existing_artifacts(paths.observe_ledger)
    if existing:
        problems.append("ledger already exists: " + ", ".join(str(p) for p in existing))
    unexpected = paths.unexpected_entries()
    if unexpected:
        problems.append("data directory has unexpected entries: " + ", ".join(unexpected))
    return problems


def gather_evidence(broker: ObserveBroker, now: datetime) -> InitEvidence:
    """Read-only broker evidence for an initial baseline. Raises BrokerError."""
    assert_no_transaction("init-ledger evidence")
    ev = InitEvidence(observed_at=now)
    ev.account = broker.get_account()
    ev.account_config = broker.get_account_config()
    broker.get_clock()
    positions = broker.get_positions()
    open_orders = broker.get_open_orders()
    window = collect_window(
        broker,
        after=now - DEFAULT_RESCAN_POLICY.periodic_lookback,
        until=now,
        now=now,
        posting_lag=ACTIVITY_POSTING_LAG,
    )
    today = now.astimezone(MARKET_TZ).date()
    ev.calendars = tuple(
        broker.get_calendar(kind, today - CALENDAR_FETCH_BACK, today + CALENDAR_FETCH_AHEAD)
        for kind in CalendarKind
    )
    again = broker.get_account()
    positions_again = broker.get_positions()
    open_orders_again = broker.get_open_orders()

    ev.positions, ev.open_orders = len(positions), len(open_orders)
    ev.activities = len(window.activities)
    ev.activity_types = tuple(sorted({a.activity_type for a in window.activities}))
    ev.activity_tail_confirmed = window.tail_confirmed
    ev.last_activity_id = window.activities[-1].activity_id if window.activities else None
    a, p = ev.account, ev.problems
    if a.environment != "paper":
        p.append(f"broker environment is {a.environment!r}, not 'paper'")
    if not a.account_id.strip():
        p.append("broker account id is blank")
    if a.currency != "USD":
        p.append(f"account currency {a.currency} is not supported")
    if a.account_blocked or a.trading_blocked:
        p.append("broker account is restricted (account_blocked / trading_blocked)")
    if a.accrued_fees != ZERO or a.pending_reg_taf_fees != ZERO:
        p.append("broker fees are pending; initialize once they have posted")
    if positions or positions_again:
        p.append(f"account is not flat: {len(positions)} position(s)")
    if open_orders or open_orders_again:
        p.append(f"account has {len(open_orders)} open order(s)")
    if not window.tail_confirmed:
        p.append("activity history could not be read to its end")
    if (again.account_id, again.cash) != (a.account_id, a.cash):
        p.append("account changed while evidence was being gathered; retry")
    return ev


def _confirmations(
    paths: DataPaths, ev: InitEvidence, path: str | None, account: str | None, env: str | None
) -> dict[str, str] | None:
    """None when every confirmation matches; otherwise the values that must be typed."""
    if ev.account is None:  # pragma: no cover - guarded by the caller
        raise RuntimeError("no account evidence")
    required = {
        "confirm_path": str(paths.observe_ledger),
        "confirm_account": ev.account.account_id,
        "confirm_environment": ev.account.environment,
    }
    given = {"confirm_path": path, "confirm_account": account, "confirm_environment": env}
    return None if given == required else required


def _remove_temp(path: os.PathLike[str] | str) -> None:
    for suffix in ("", *SQLITE_SIDECARS):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f"{os.fspath(path)}{suffix}")


def initialize_observe_ledger(  # noqa: PLR0911 - one return per audited outcome
    paths: DataPaths,
    broker: ObserveBroker,
    config: AppConfig,
    *,
    now: datetime,
    confirm_path: str | None,
    confirm_account: str | None,
    confirm_environment: str | None,
    initial_capital: Decimal,
    max_capital_allocation: Decimal,
) -> InitResult:
    """The caller holds the exclusive lock on ``paths.observe_ledger``."""
    ledger = paths.observe_ledger
    result = InitResult(InitStatus.REFUSED, str(ledger))
    result.problems = check_target(paths)
    if result.problems:
        return result
    try:
        ev = gather_evidence(broker, now)
    except BrokerError as exc:
        result.status = InitStatus.BROKER_UNAVAILABLE
        result.problems = [f"{type(exc).__name__}: {exc}"]
        return result
    result.evidence = ev
    if ev.problems:
        result.problems = list(ev.problems)
        return result
    required = _confirmations(paths, ev, confirm_path, confirm_account, confirm_environment)
    if required is not None:
        result.status = InitStatus.CONFIRMATION_REQUIRED
        result.required_confirmation = required
        result.problems = ["operator confirmation of path, account and environment required"]
        return result
    temp = ledger.with_name(f".{ledger.name}.init-{uuid.uuid4().hex}")
    try:
        created = _build(
            temp, ev, config, now=now, initial_capital=initial_capital,
            max_capital_allocation=max_capital_allocation,
        )  # fmt: skip
        os.link(temp, ledger)  # no-clobber: fails if the target appeared meanwhile
    except InitRefusedError as exc:
        _remove_temp(temp)
        result.problems = [str(exc)]
        return result
    except BaseException:
        _remove_temp(temp)
        raise
    _remove_temp(temp)
    with (
        Database.open(ledger, RuntimeMode.OBSERVE, now=now, auto_migrate=False) as db,
        db.read() as s,
    ):
        integrity = integrity_report(s)
        state = TradingStateRepository().get(s)
    if not integrity.ok:  # pragma: no cover - a freshly built ledger
        result.problems = integrity.problems()
        return result
    created["halted"] = state.halted
    created["halt_reason"] = state.reason_code
    result.status = InitStatus.INITIALIZED
    result.created = created
    return result


def _build(
    temp: Path,
    ev: InitEvidence,
    config: AppConfig,
    *,
    now: datetime,
    initial_capital: Decimal,
    max_capital_allocation: Decimal,
) -> dict[str, Any]:
    account, account_config = ev.account, ev.account_config
    if account is None or account_config is None:  # pragma: no cover - caller guards
        raise RuntimeError("incomplete account evidence")
    db = Database.create(temp, RuntimeMode.OBSERVE, now=now)
    try:
        with db.transaction() as tx:
            store = CalendarStore()
            for fetch in ev.calendars:
                outcome = store.record_fetch(tx, fetch)
                if not outcome.accepted:
                    raise InitRefusedError(
                        f"{fetch.kind.value} calendar rejected: {'; '.join(outcome.problems)}"
                    )
            pid = VirtualPortfolio.initialize(
                tx,
                name=PORTFOLIO_NAME,
                mode=RuntimeMode.OBSERVE,
                initial_capital=initial_capital,
                max_capital_allocation=max_capital_allocation,
                account_profile=config.policy.active_profile,
                at=now,
            )
            sync = SyncStateRepository()
            sync.set(tx, BROKER_ACCOUNT_ID, account.account_id, now)
            sync.set(tx, BROKER_ENVIRONMENT, account.environment, now)
            baseline_id = BrokerBaselinesRepository().record_initial(
                tx,
                portfolio_id=pid,
                taken_at=ev.observed_at,
                cash=account.cash,
                equity=account.cash,  # flat account: equity = cash
                account_config={
                    "no_shorting": account_config.no_shorting,
                    "max_margin_multiplier": str(account_config.max_margin_multiplier),
                    "pdt_check": account_config.pdt_check,
                    "dtbp_check": account_config.dtbp_check,
                    "suspend_trade": account_config.suspend_trade,
                    "fractional_trading": account_config.fractional_trading,
                },
                activity_cursor_id=ev.last_activity_id,
            )
        db.pragma("wal_checkpoint")
    finally:
        db.close()
    return {
        "portfolio_id": pid,
        "virtual_initial_capital": str(initial_capital),
        "virtual_max_capital_allocation": str(max_capital_allocation),
        "account_profile": config.policy.active_profile,
        "broker_baseline_id": baseline_id,
        "broker_account_id": account.account_id,
        "broker_environment": account.environment,
    }


class InitRefusedError(RuntimeError):
    """Raised inside the build transaction to roll it back (temp file is removed)."""
