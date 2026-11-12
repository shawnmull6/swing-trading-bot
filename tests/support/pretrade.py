"""A healthy simulate-mode environment with a fresh gate and account, for risk tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from swing_trading_bot.broker.models import AccountSnapshot
from swing_trading_bot.broker.sim import SimConfig
from swing_trading_bot.config.fees import load_fee_model
from swing_trading_bot.config.risk_limits import load_risk_limits
from swing_trading_bot.domain.enums import InstrumentType, OrderType, RuntimeMode, Side, TimeInForce
from swing_trading_bot.risk.gates import GateStatus
from swing_trading_bot.risk.pretrade import (
    PreTradeRiskEngine,
    RiskDecision,
    RiskEngineConfig,
    TradeProposal,
)
from tests.support.gates import Env, healthy_env
from tests.support.portfolio import CONFIG as PF_CONFIG
from tests.support.portfolio import NOW, mark

ROOT = Path(__file__).resolve().parents[2]
LIMITS = load_risk_limits(ROOT / "config" / "risk.yaml")
FEES = load_fee_model(ROOT / "config" / "fees.yaml")
ENGINE_CONFIG = RiskEngineConfig(
    limits=LIMITS,
    mode=RuntimeMode.SIMULATE,
    fees=FEES,
    marks=PF_CONFIG.marks,
    calendar_max_age=timedelta(hours=36),
)
D = Decimal


def proposal(
    symbol: str = "MSFT",
    entry: str = "50.00",
    stop: str = "48.00",
    *,
    qty: str | None = None,
    intent: str | None = None,
    **changes: object,
) -> TradeProposal:
    p = TradeProposal(
        intent_key=intent or f"entry:{symbol}:test",
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        entry_price=D(entry),
        stop_price=D(stop),
        instrument_type=InstrumentType.EQUITY,
        requested_qty=None if qty is None else D(qty),
    )
    return replace(p, **changes)  # type: ignore[arg-type]


@dataclass
class RiskEnv:
    env: Env
    engine: PreTradeRiskEngine

    @property
    def db(self):  # type: ignore[no-untyped-def]
        return self.env.db

    def set_marks(self, prices: dict[str, str], *, at: datetime = NOW) -> None:
        with self.env.db.transaction() as tx:
            for symbol, price in prices.items():
                self.env.pf.record_mark(
                    tx, mark(symbol, price, as_of=at - timedelta(minutes=1), received=at)
                )

    def gate(self, at: datetime = NOW) -> GateStatus:
        return self.env.gates.evaluate(now=at)

    def account(self) -> AccountSnapshot:
        return self.env.sim.get_account()

    def evaluate(
        self,
        p: TradeProposal,
        *,
        at: datetime = NOW,
        gate: GateStatus | None = None,
        account: AccountSnapshot | None = None,
        no_gate: bool = False,
        no_account: bool = False,
    ) -> RiskDecision:
        return self.engine.evaluate(
            p,
            gate=None if no_gate else (gate or self.gate(at)),
            account=None if no_account else (account or self.account()),
            now=at,
        )


def risk_env(
    tmp_path: Path,
    config: RiskEngineConfig = ENGINE_CONFIG,
    *,
    sim_config: SimConfig | None = None,
) -> RiskEnv:
    env = healthy_env(tmp_path, sim_config=sim_config)
    status = env.gates.run_startup(now=NOW)
    assert status.new_entries_allowed, status.render()
    engine = PreTradeRiskEngine(env.db, env.pf, env.monitor, config)
    renv = RiskEnv(env, engine)
    renv.set_marks({"MSFT": "50.00"})
    return renv


def codes(decision: RiskDecision) -> set[str]:
    return set(decision.rejection_codes)
