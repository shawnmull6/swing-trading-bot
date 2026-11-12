"""Offline, single-process simulation scenarios (D14-3, M1_PLAN §4.1 ``simulate``).

This is the ONLY module in ``app/`` that constructs an execution capability. It does so
only for a NEW simulation ledger under ``<data>/sim/`` and only around an in-memory
:class:`SimBroker` (``SimulationExecution`` refuses anything else, and ``OrderManager``
refuses a non-simulate ledger). Nothing here reaches a network or an account-backed
ledger. Every result is labelled hypothetical.

Scenario intents are fixed, scripted proposals that exercise the full gate → pre-trade
risk → OMS path; there is no signal or strategy logic. Simulated time is a
:class:`SimClock` that ticks one millisecond per reading (so successive evidence is
strictly ordered, as it is in wall-clock time) and is advanced explicitly between steps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from swing_trading_bot.app.config import (
    CALENDAR_FETCH_AHEAD,
    CALENDAR_FETCH_BACK,
    PORTFOLIO_NAME,
    AppConfig,
)
from swing_trading_bot.app.paths import DataPaths
from swing_trading_bot.app.runtime import (
    CycleReport,
    ReconcileResult,
    Runtime,
    build_runtime,
    rebaseline_approve,
    rebaseline_propose,
    reconcile,
    record_marks,
    resolve_order,
    resume,
    state_dict,
    sync_cycle,
)
from swing_trading_bot.app.sim_calendar import synthetic_calendars
from swing_trading_bot.broker.sim import AcceptThenFail, FailBeforeReceipt, SimBroker, SimClock
from swing_trading_bot.domain.calendar import MARKET_TZ, CalendarKind
from swing_trading_bot.domain.enums import (
    InstrumentType,
    OrderStatus,
    OrderType,
    RuntimeMode,
    Side,
    TimeInForce,
)
from swing_trading_bot.ledger.baselines import BrokerBaselinesRepository
from swing_trading_bot.ledger.calendar_store import CalendarStore
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.errors import DatabaseExistsError
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.ledger.repositories import (
    OrderRecord,
    OrdersRepository,
    PositionsRepository,
    ReservationRecord,
    ReservationsRepository,
    SyncStateRepository,
)
from swing_trading_bot.ledger.sync_keys import BROKER_ACCOUNT_ID, BROKER_ENVIRONMENT
from swing_trading_bot.oms.execution import SimulationExecution
from swing_trading_bot.oms.order_manager import CrashHook, OrderManager, PlacementResult
from swing_trading_bot.oms.reconciler import Outcome
from swing_trading_bot.risk.pretrade import PreTradeRiskEngine, TradeProposal

HYPOTHETICAL: Final = (
    "HYPOTHETICAL SIMULATION - in-memory SimBroker only; no real orders, no broker network "
    "calls; results are not account evidence"
)
SCENARIO_START: Final = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # Monday 10:00 ET
TICK: Final = timedelta(milliseconds=1)
SIM_ACTOR: Final = "operator:simulation"
SYMBOL: Final = "SIMA"
D = Decimal


class ScenarioCrash(BaseException):
    """Raised by a scenario crash hook; unwinds like a killed process."""


class ScenarioError(RuntimeError):
    pass


def crash_at(point: str) -> CrashHook:
    def hook(reached: str) -> None:
        if reached == point:
            raise ScenarioCrash(point)

    return hook


@dataclass(slots=True)
class Harness:
    paths: DataPaths
    run_id: str
    config: AppConfig
    clock: SimClock
    sim: SimBroker
    db: Database
    rt: Runtime = field(init=False)
    engine: PreTradeRiskEngine = field(init=False)
    om: OrderManager = field(init=False)
    steps: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    prices: dict[str, Decimal] = field(default_factory=dict)
    intents: int = 0

    @property
    def path(self) -> Path:
        return self.paths.sim_ledger(self.run_id)

    def now(self) -> datetime:
        return self.clock.now()

    def tick(self) -> datetime:
        return self.clock.advance(TICK)

    def build(self, crash: CrashHook | None = None) -> None:
        self.rt = build_runtime(
            self.db, self.sim, self.config, clock=self.tick, trading_enabled=True, crash=crash
        )
        self.engine = PreTradeRiskEngine(self.db, self.rt.pf, self.rt.monitor, self.config.engine())
        self.om = OrderManager(
            self.db, self.rt.pf, self.engine, self.rt.gates, self.sim,
            SimulationExecution(self.sim), self.config.oms(RuntimeMode.SIMULATE), crash=crash,
        )  # fmt: skip

    def restart(self, crash: CrashHook | None = None) -> None:
        """A process restart: new connection and new collaborators, same broker."""
        self.db.close()
        self.db = Database.open(self.path, RuntimeMode.SIMULATE, now=self.now(), auto_migrate=False)
        self.build(crash)

    # ------------------------------------------------------------- recording
    def step(self, name: str, **data: object) -> None:
        self.steps.append({"at": self.now().isoformat(), "step": name, **data})

    def check(self, name: str, ok: bool) -> None:  # noqa: FBT001
        self.checks[name] = bool(ok)

    def advance(self, delta: timedelta) -> None:
        self.clock.advance(delta)
        for symbol, price in self.prices.items():
            self.sim.set_price(symbol, price)

    # ------------------------------------------------------------- operations
    def cycle(self, label: str) -> CycleReport:
        report = sync_cycle(self.rt)
        synced = report.to_dict()["sync"]
        self.step(label, exit_code=int(report.exit_code),
                  sync=None if synced is None else synced["status"], looked_up=report.looked_up,
                  transitions={} if synced is None else synced["transitions"])  # fmt: skip
        return report

    def reconcile(self, label: str) -> ReconcileResult:
        result = reconcile(self.rt)
        recon = result.recon
        self.step(label, exit_code=int(result.exit_code),
                  outcome=None if recon is None else recon.outcome.value,
                  findings=[] if recon is None else sorted(recon.codes()),
                  halted=None if recon is None or recon.halted is None
                  else recon.halted.value)  # fmt: skip
        return result

    def place(self, qty: str = "10", entry: str = "50.00", stop: str = "48.00") -> PlacementResult:
        self.prices[SYMBOL] = D(entry)
        self.sim.set_price(SYMBOL, D(entry))
        record_marks(self.rt, [SYMBOL])
        self.intents += 1
        proposal = TradeProposal(
            intent_key=f"sim:{self.run_id}:{self.intents}",
            symbol=SYMBOL,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            entry_price=D(entry),
            stop_price=D(stop),
            instrument_type=InstrumentType.EQUITY,
            requested_qty=D(qty),
        )
        gate = self.rt.gates.evaluate(now=self.tick())
        decision = self.engine.evaluate(
            proposal, gate=gate, account=self.sim.get_account(), now=self.tick()
        )
        if not decision.approved:
            self.step("place: risk rejected", rejections=list(decision.rejection_codes))
            raise ScenarioError(f"proposal rejected: {decision.rejection_codes}")
        result = self.om.place_entry(decision, now=self.tick())
        self.step("place", outcome=result.outcome.value,
                  client_order_id=result.client_order_id,
                  status=None if result.status is None else result.status.value,
                  qty=str(decision.approved_qty),
                  reservation=str(decision.reservation))  # fmt: skip
        return result

    # ------------------------------------------------------------- observation
    def order(self, coid: str) -> OrderRecord:
        with self.db.read() as s:
            return OrdersRepository().require(s, coid)

    def reservation(self, coid: str) -> ReservationRecord | None:
        with self.db.read() as s:
            return ReservationsRepository().get(s, coid)

    def broker_orders(self, coid: str) -> int:
        return sum(f"SUBMIT {coid} accepted as" in line for line in self.sim.event_log)

    def position(self, symbol: str = SYMBOL) -> Decimal:
        with self.db.read() as s:
            pos = PositionsRepository().get(s, 1, symbol)
        return D(0) if pos is None else pos.qty

    def summary(self) -> dict[str, Any]:
        now = self.tick()
        invariants = self.rt.pf.check_invariants(now=now)
        with self.db.read() as s:
            balances = self.rt.pf.balances(s, now=now)
            orders = OrdersRepository().list_all(s)
            positions = PositionsRepository().list_for_portfolio(s, 1)
        state = self.rt.state()
        return {
            "trading_state": state_dict(state),
            "invariants_ok": invariants.ok,
            "virtual_cash": str(balances.cash),
            "buy_commitments": str(balances.buy_commitments),
            "positions": {p.symbol: str(p.qty) for p in positions if p.qty},
            "orders": [
                {
                    "client_order_id": o.client_order_id,
                    "status": o.status.value,
                    "qty": str(o.qty),
                    "filled_qty": str(o.filled_qty),
                    "broker_orders_created": self.broker_orders(o.client_order_id),
                }
                for o in orders
            ],
        }


# ===================================================================== setup
def create_harness(paths: DataPaths, run_id: str, config: AppConfig) -> Harness:
    """A NEW simulation ledger, initialized like an account-backed one but against the
    SimBroker: portfolio, recorded (sim) identity, INIT broker baseline, calendars."""
    path = paths.sim_ledger(run_id)
    if path.exists():
        raise DatabaseExistsError(f"simulation ledger already exists: {path}")
    clock = SimClock(SCENARIO_START)
    sim = SimBroker(clock)
    today = SCENARIO_START.astimezone(MARKET_TZ).date()
    for fetch in synthetic_calendars(
        today - timedelta(days=30), today + timedelta(days=120), fetched_at=clock.now()
    ):
        sim.load_calendar(fetch)
    fetches = [
        sim.get_calendar(kind, today - CALENDAR_FETCH_BACK, today + CALENDAR_FETCH_AHEAD)
        for kind in CalendarKind
    ]
    account, account_config = sim.get_account(), sim.get_account_config()
    db = Database.create(path, RuntimeMode.SIMULATE, now=clock.now())
    try:
        with db.transaction() as tx:
            store = CalendarStore()
            for fetch in fetches:
                if not store.record_fetch(tx, fetch).accepted:
                    raise ScenarioError("synthetic simulation calendar rejected")
            pid = VirtualPortfolio.initialize(
                tx,
                name=PORTFOLIO_NAME,
                mode=RuntimeMode.SIMULATE,
                initial_capital=D("5000"),
                max_capital_allocation=D("5000"),
                account_profile=config.policy.active_profile,
                at=clock.now(),
            )
            sync = SyncStateRepository()
            sync.set(tx, BROKER_ACCOUNT_ID, account.account_id, clock.now())
            sync.set(tx, BROKER_ENVIRONMENT, account.environment, clock.now())
            BrokerBaselinesRepository().record_initial(
                tx,
                portfolio_id=pid,
                taken_at=clock.now(),
                cash=account.cash,
                equity=account.cash,
                account_config={
                    "no_shorting": account_config.no_shorting,
                    "max_margin_multiplier": str(account_config.max_margin_multiplier),
                },
            )
    except BaseException:
        db.close()
        raise
    h = Harness(paths, run_id, config, clock, sim, db)
    h.build()
    h.step("ledger created", ledger=str(path), mode="simulate")
    return h


def bring_up(h: Harness) -> None:
    """Startup on the new sim ledger: let the first activity window close, reconcile,
    then the deliberate operator resume of the INITIALIZED halt."""
    h.advance(timedelta(minutes=20))
    first = h.reconcile("startup reconcile")
    h.check("startup reconciliation clean", first.recon is not None
            and first.recon.outcome is Outcome.CLEAN)  # fmt: skip
    res = resume(h.rt, actor=SIM_ACTOR, reason="simulation start")
    h.step("resume INITIALIZED halt", resumed=res.resumed, exit_code=int(res.exit_code))
    h.check("initial resume accepted", res.resumed)


# ===================================================================== scenarios
def scenario_happy_path(h: Harness) -> None:
    bring_up(h)
    placed = h.place()
    coid = placed.client_order_id or ""
    h.check("order accepted", placed.status is OrderStatus.ACCEPTED)
    h.sim.schedule_fill(coid, D(10), D("49.95"), after=timedelta(seconds=30))
    h.advance(timedelta(minutes=1))
    h.cycle("sync after fill")
    h.check("order filled", h.order(coid).status is OrderStatus.FILLED)
    res = h.reservation(coid)
    h.check("reservation consumed", res is not None and res.status.value == "CONSUMED")
    h.check("position booked", h.position() == D(10))
    h.advance(timedelta(minutes=20))
    final = h.reconcile("reconcile after fill")
    h.check("reconciliation clean", final.recon is not None
            and final.recon.outcome is Outcome.CLEAN)  # fmt: skip
    h.check("one broker order for the intent", h.broker_orders(coid) == 1)


def scenario_partial_cancel(h: Harness) -> None:
    bring_up(h)
    coid = h.place().client_order_id or ""
    h.sim.schedule_fill(coid, D(4), D("49.95"), after=timedelta(seconds=30))
    h.advance(timedelta(minutes=1))
    h.cycle("sync after partial fill")
    h.check("partially filled", h.order(coid).status is OrderStatus.PARTIALLY_FILLED)
    outcome = h.om.request_cancel(coid, now=h.tick(), reason="simulation cancel")
    h.step("cancel requested", outcome=outcome.value)
    res = h.reservation(coid)
    h.check("reservation held until broker confirms",
            res is not None and res.status.value == "ACTIVE")  # fmt: skip
    h.advance(timedelta(minutes=1))
    h.cycle("sync after cancel")
    h.check("order canceled", h.order(coid).status is OrderStatus.CANCELED)
    res = h.reservation(coid)
    h.check("remainder released", res is not None and res.status.value == "RELEASED")
    h.check("position equals broker fills", h.position() == D(4))
    h.advance(timedelta(minutes=20))
    final = h.reconcile("reconcile after cancel")
    h.check("reconciliation clean", final.recon is not None
            and final.recon.outcome is Outcome.CLEAN)  # fmt: skip


def scenario_ambiguous_adopt(h: Harness) -> None:
    bring_up(h)
    h.sim.script_submit(AcceptThenFail("gateway_timeout"))
    coid = h.place().client_order_id or ""
    h.check("504 after acceptance is ambiguous",
            h.order(coid).status is OrderStatus.SUBMIT_AMBIGUOUS)  # fmt: skip
    res = h.reservation(coid)
    h.check("reservation kept while ambiguous",
            res is not None and res.status.value == "ACTIVE")  # fmt: skip
    h.advance(timedelta(minutes=1))
    h.cycle("recovery lookup")
    h.check("adopted by client-id lookup", h.order(coid).status is OrderStatus.ACCEPTED)
    h.sim.schedule_fill(coid, D(10), D("49.90"), after=timedelta(seconds=10))
    h.advance(timedelta(minutes=1))
    h.cycle("sync after fill")
    h.check("order filled", h.order(coid).status is OrderStatus.FILLED)
    h.advance(timedelta(minutes=20))
    final = h.reconcile("reconcile")
    h.check("reconciliation clean", final.recon is not None
            and final.recon.outcome is Outcome.CLEAN)  # fmt: skip
    h.check("never resent: one broker order", h.broker_orders(coid) == 1)


def scenario_never_received(h: Harness) -> None:
    bring_up(h)
    h.sim.script_submit(FailBeforeReceipt())
    coid = h.place().client_order_id or ""
    sent = h.order(coid).send_attempted_at or h.now()
    h.check("lost request is ambiguous", h.order(coid).status is OrderStatus.SUBMIT_AMBIGUOUS)
    for minute in (1, 5):
        h.clock.advance_to(sent + timedelta(minutes=minute))
        h.cycle(f"recovery lookup +{minute}m")
    h.check("not released on lookups or a timer",
            h.order(coid).status is OrderStatus.SUBMIT_AMBIGUOUS)  # fmt: skip
    h.clock.advance_to(sent + timedelta(minutes=26))
    resolved = resolve_order(h.rt, coid, actor=SIM_ACTOR)
    h.step("operator resolve-order --confirm-absent", confirmed=resolved.confirmed,
           missing=[] if resolved.evidence is None
           else list(resolved.evidence.missing))  # fmt: skip
    h.check("operator confirmation accepted", resolved.confirmed)
    h.check("order confirmed absent", h.order(coid).status is OrderStatus.CONFIRMED_ABSENT)
    res = h.reservation(coid)
    h.check("reservation released only now", res is not None and res.status.value == "RELEASED")
    h.check("no broker order exists", h.broker_orders(coid) == 0)


def scenario_restart_recovery(h: Harness) -> None:
    bring_up(h)
    h.build(crash=crash_at("after_submit"))
    gate = h.rt.gates.run_startup(now=h.tick())  # rewired: startup readiness again
    h.check("startup readiness re-established", gate.startup_complete)
    try:
        h.place()
    except ScenarioCrash as crash:
        h.step("process crashed", point=str(crash))
    else:
        raise ScenarioError("crash point not reached")
    h.restart()
    with h.db.read() as s:
        pending = list(OrdersRepository().list_working(s))
    coid = pending[0].client_order_id
    h.check("after restart the order is unresolved locally",
            pending[0].status is OrderStatus.PENDING_SUBMIT)  # fmt: skip
    h.advance(timedelta(minutes=1))
    h.cycle("startup recovery")
    h.check("adopted after restart", h.order(coid).status is OrderStatus.ACCEPTED)
    h.sim.schedule_fill(coid, D(10), D("50.00"), after=timedelta(seconds=10))
    h.advance(timedelta(minutes=1))
    h.cycle("sync after fill")
    h.advance(timedelta(minutes=20))
    final = h.reconcile("reconcile")
    h.check("reconciliation clean", final.recon is not None
            and final.recon.outcome is Outcome.CLEAN)  # fmt: skip
    h.check("one broker order for the intent", h.broker_orders(coid) == 1)


def scenario_unexplained_deposit(h: Harness) -> None:
    bring_up(h)
    h.sim.schedule_cash_activity("CSD", D("250.00"), after=timedelta(seconds=5))
    h.advance(timedelta(minutes=20))
    first = h.reconcile("reconcile after external deposit")
    h.check("deposit is a MISMATCH", first.recon is not None
            and first.recon.outcome is Outcome.MISMATCH)  # fmt: skip
    h.check("persistent halt RECON_CASH", h.rt.state().reason_code == "RECON_CASH")
    refused = resume(h.rt, actor=SIM_ACTOR, reason="try to resume")
    h.step("resume attempt", resumed=refused.resumed, exit_code=int(refused.exit_code))
    h.check("resume refused while mismatched", not refused.resumed)
    proposal = rebaseline_propose(h.rt, actor=SIM_ACTOR, reason="owner deposit")
    amount = None if proposal.outcome is None else proposal.outcome.unexplained
    h.step("rebaseline propose", **proposal.to_dict())
    h.check("proposal records the exact amount", amount == D("250.00"))
    h.advance(timedelta(minutes=1))
    request_id = None if proposal.outcome is None else proposal.outcome.request_id
    approved = rebaseline_approve(h.rt, request_id or 0, acknowledged=str(amount),
                                  actor=SIM_ACTOR, reason="confirmed external deposit")  # fmt: skip
    h.step("rebaseline approve", decision=approved.to_dict()["decision"])
    h.check("rebaseline approved", approved.exit_code == 0)
    h.check("halt remains after approval", h.rt.state().reason_code == "REBASELINE")
    h.advance(timedelta(minutes=2))
    final = resume(h.rt, actor=SIM_ACTOR, reason="clean against new baseline")
    h.step("resume", resumed=final.resumed)
    h.check("resume accepted after clean reconciliation", final.resumed)
    with h.db.read() as s:
        cash = h.rt.pf.balances(s, now=h.tick()).cash
    h.check("virtual cash untouched by the deposit", cash == D("5000"))


SCENARIOS: Final[dict[str, Callable[[Harness], None]]] = {
    "happy-path": scenario_happy_path,
    "partial-cancel": scenario_partial_cancel,
    "ambiguous-adopt": scenario_ambiguous_adopt,
    "never-received": scenario_never_received,
    "restart-recovery": scenario_restart_recovery,
    "unexplained-deposit": scenario_unexplained_deposit,
}


@dataclass(slots=True)
class ScenarioResult:
    name: str
    run_id: str
    ledger: str
    steps: list[dict[str, Any]]
    checks: dict[str, bool]
    summary: dict[str, Any]
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.checks) and all(self.checks.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothetical": True,
            "notice": HYPOTHETICAL,
            "scenario": self.name,
            "run_id": self.run_id,
            "ledger": self.ledger,
            "passed": self.passed,
            "error": self.error,
            "checks": dict(self.checks),
            "steps": list(self.steps),
            "final": self.summary,
        }


def run_scenario(paths: DataPaths, name: str, run_id: str, config: AppConfig) -> ScenarioResult:
    """Run one scenario on a new simulation ledger (caller holds its exclusive lock)."""
    scenario = SCENARIOS[name]
    paths.sim_dir.mkdir(exist_ok=True)
    h = create_harness(paths, run_id, config)
    error: str | None = None
    try:
        scenario(h)
    except ScenarioError as exc:
        error = str(exc)
    try:
        summary = h.summary()
    finally:
        h.db.close()
    return ScenarioResult(name, run_id, str(h.path), h.steps, h.checks, summary, error)
