"""Process-level crash harness for the OMS (M1 plan §5.4).

The parent test owns the :class:`SimBroker` (so broker state survives the "crash") and
serves its methods over a ``multiprocessing`` pipe. A spawned child process opens the
ledger file, wires the real OrderManager / OrderSync to a pipe-backed broker proxy, runs
one scenario and dies with ``os._exit`` at the requested crash point: no ``finally``
blocks, no rollback handlers, no clean close — exactly like a killed process. The
parent then reopens the ledger and runs restart recovery.

No sockets are used (the test suite blocks network connections); the pipe is an OS pipe.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from swing_trading_bot.broker.sim import SimBroker, SimClock
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.loss_limits import LossLimitMonitor
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.oms.execution import SimulationExecution, register_simulation_double
from swing_trading_bot.oms.order_manager import OrderManager
from swing_trading_bot.oms.sync import OrderSync
from swing_trading_bot.risk.gates import TradingGates
from swing_trading_bot.risk.pretrade import PreTradeRiskEngine
from tests.support.gates import GATE_CONFIG, Env
from tests.support.loss_limits import CONFIG as LOSS_CONFIG
from tests.support.oms import OMS_CONFIG, OmsEnv
from tests.support.portfolio import CONFIG as PF_CONFIG
from tests.support.pretrade import ENGINE_CONFIG, RiskEnv, proposal

CRASH_EXIT_CODE = 70


class PipeBroker:
    """Child-side proxy implementing both broker ports by forwarding every call. Methods
    are real attributes (protocol checks look them up statically). Registered as a
    controlled simulation double: it only ever reaches the parent's SimBroker."""

    SIMULATION_DOUBLE_PURPOSE = "process-level crash tests: pipe proxy to a SimBroker"

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self._conn.send((name, args, kwargs))
        ok, value = self._conn.recv()
        if not ok:
            raise value
        return value

    def now(self) -> Any:
        """The parent's simulated clock."""
        return self._call("now_")


def _forward(name: str) -> Callable[..., Any]:
    def method(self: PipeBroker, *args: Any, **kwargs: Any) -> Any:
        return self._call(name, *args, **kwargs)

    method.__name__ = name
    return method


for _name in ("get_account", "get_account_config", "get_clock", "get_calendar",
              "get_positions", "get_open_orders", "get_orders", "get_order_by_client_id",
              "get_activities", "get_marks", "submit_order", "cancel_order"):  # fmt: skip
    setattr(PipeBroker, _name, _forward(_name))
register_simulation_double(PipeBroker)


def _child(conn: Connection, db_path: str, scenario: str, crash_point: str) -> None:
    broker = PipeBroker(conn)

    def crash(point: str) -> None:
        if point == crash_point:
            os._exit(CRASH_EXIT_CODE)  # die immediately, mid-whatever

    now = broker.now()
    db = Database.open(Path(db_path), RuntimeMode.SIMULATE)
    pf = VirtualPortfolio(db, PF_CONFIG)
    monitor = LossLimitMonitor(db, pf, LOSS_CONFIG)
    gates = TradingGates(db, pf, monitor, broker, GATE_CONFIG)  # type: ignore[arg-type]
    gates.run_startup(now=now)
    engine = PreTradeRiskEngine(db, pf, monitor, ENGINE_CONFIG)
    if scenario == "place":
        om = OrderManager(db, pf, engine, gates, broker, SimulationExecution(broker),  # type: ignore[arg-type]
                          OMS_CONFIG, crash=crash)  # fmt: skip
        decision = engine.evaluate(proposal(), gate=gates.evaluate(now=now),
                                   account=broker._call("get_account"), now=now)  # fmt: skip
        om.place_entry(decision, now=now)
    elif scenario == "sync":
        OrderSync(db, pf, broker, OMS_CONFIG, crash=crash).sync(now=now)  # type: ignore[arg-type]
    os._exit(0)  # the crash point was never reached


@dataclass(frozen=True, slots=True)
class ChildResult:
    exitcode: int | None
    calls: list[str]


def run_child(sim: SimBroker, clock: SimClock, db_path: Path, scenario: str,
              crash_point: str, *, timeout: float = 60.0) -> ChildResult:  # fmt: skip
    """Run the scenario in a fresh interpreter, serving broker calls until it exits."""
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_child, args=(child, str(db_path), scenario, crash_point))
    proc.start()
    child.close()
    calls: list[str] = []
    while True:
        if parent.poll(0.05):
            try:
                name, args, kwargs = parent.recv()
            except EOFError:
                break
            calls.append(name)
            try:
                value = clock.now() if name == "now_" else getattr(sim, name)(*args, **kwargs)
                parent.send((True, value))
            except Exception as exc:  # noqa: BLE001 - relayed to the child verbatim
                parent.send((False, exc))
        elif not proc.is_alive():
            break
    proc.join(timeout)
    return ChildResult(proc.exitcode, calls)


def reopen(e: OmsEnv) -> OmsEnv:
    """A fresh 'process': reopen the ledger file and rebuild every collaborator over the
    same SimBroker and clock."""
    old = e.r.env
    db = Database.open(old.db.path, RuntimeMode.SIMULATE)
    pf = VirtualPortfolio(db, PF_CONFIG)
    monitor = LossLimitMonitor(db, pf, LOSS_CONFIG)
    gates = TradingGates(db, pf, monitor, old.sim, GATE_CONFIG)
    env = Env(db, pf, monitor, old.sim, old.clock, gates)
    return OmsEnv(RiskEnv(env, PreTradeRiskEngine(db, pf, monitor, ENGINE_CONFIG)))
