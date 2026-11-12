"""Runtime configuration assembled from ``config/*.yaml`` (no secrets, no ``.env``).

Every threshold comes from the approved configuration files; this module only maps
them onto the constructor configs of the existing services. The few values that are
not risk thresholds (activity posting lag, reconciliation freshness, calendar refresh
cadence) mirror the values the services already use by default.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from swing_trading_bot.broker.activity_feed import DEFAULT_RESCAN_POLICY
from swing_trading_bot.config.fees import load_fee_model
from swing_trading_bot.config.operating_policy import OperatingPolicy, load_operating_policy
from swing_trading_bot.config.risk_limits import RiskLimits, load_risk_limits
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.domain.fees import FeeModel
from swing_trading_bot.domain.valuation import MarkFreshnessPolicy
from swing_trading_bot.ledger.broker_outage import OutagePolicy
from swing_trading_bot.ledger.loss_limit_store import CloseSource
from swing_trading_bot.ledger.loss_limits import LossLimitConfig
from swing_trading_bot.ledger.portfolio import PortfolioConfig
from swing_trading_bot.oms.order_manager import OmsConfig
from swing_trading_bot.oms.reconciler import ReconConfig
from swing_trading_bot.risk.gates import GateConfig
from swing_trading_bot.risk.pretrade import RiskEngineConfig

PORTFOLIO_NAME: Final = "main"
ACTIVITY_POSTING_LAG: Final = DEFAULT_RESCAN_POLICY.posting_lag
RECONCILIATION_MAX_AGE: Final = timedelta(minutes=10)
CALENDAR_REFRESH_AFTER: Final = timedelta(hours=24)  # §6.3: "re-fetched daily"
CALENDAR_FETCH_BACK: Final = timedelta(days=7)
CALENDAR_FETCH_AHEAD: Final = timedelta(days=60)
CALENDAR_REQUIRED_AHEAD: Final = timedelta(days=30)
D8_CAPITAL: Final = Decimal("5000")  # D8: INITIAL_CAPITAL = MAX_CAPITAL_ALLOCATION


@dataclass(frozen=True, slots=True)
class AppConfig:
    risk: RiskLimits
    policy: OperatingPolicy
    fees: FeeModel
    config_dir: Path

    @classmethod
    def load(cls, config_dir: Path) -> AppConfig:
        return cls(
            risk=load_risk_limits(config_dir / "risk.yaml"),
            policy=load_operating_policy(config_dir / "operating_policy.yaml"),
            fees=load_fee_model(config_dir / "fees.yaml"),
            config_dir=config_dir,
        )

    @property
    def calendar_max_age(self) -> timedelta:
        return timedelta(hours=self.policy.profile.settlement.calendar_max_age_hours)

    @property
    def max_sync_age(self) -> timedelta:
        return timedelta(seconds=self.risk.max_sync_age_seconds)

    def marks(self) -> MarkFreshnessPolicy:
        m = self.risk.mark_freshness
        return MarkFreshnessPolicy.from_seconds(
            in_session=m.max_age_in_session_seconds,
            future_skew=m.max_future_skew_seconds,
            receipt_age=self.risk.max_sync_age_seconds,
        )

    def portfolio(self) -> PortfolioConfig:
        return PortfolioConfig(PORTFOLIO_NAME, self.calendar_max_age, self.marks())

    def loss_limits(self, mode: RuntimeMode) -> LossLimitConfig:
        cfg = LossLimitConfig.from_risk_limits(self.risk, calendar_max_age=self.calendar_max_age)
        if mode is RuntimeMode.OBSERVE:
            # D14-8: account-backed baselines accept verified official closes only;
            # simulated closes exist only in simulation ledgers.
            cfg = replace(cfg, baseline_close_sources=(CloseSource.OFFICIAL,))
        return cfg

    def gates(self, mode: RuntimeMode, *, trading_enabled: bool) -> GateConfig:
        settlement = self.policy.profile.settlement
        return GateConfig(
            mode=mode,
            trading_enabled=trading_enabled,
            max_sync_age=self.max_sync_age,
            calendar_max_age=self.calendar_max_age,
            calendar_coverage_days=settlement.min_calendar_coverage_days,
            calendar_horizon_warning_days=settlement.calendar_horizon_warning_days,
            activity_posting_lag=ACTIVITY_POSTING_LAG,
            reconciliation_max_age=RECONCILIATION_MAX_AGE,
        )

    def oms(self, mode: RuntimeMode) -> OmsConfig:
        return OmsConfig(
            mode=mode,
            ambiguous=self.risk.ambiguous_orders,
            posting_lag=ACTIVITY_POSTING_LAG,
            reconciliation_max_age=RECONCILIATION_MAX_AGE,
        )

    def reconciliation(self, mode: RuntimeMode) -> ReconConfig:
        return ReconConfig(
            oms=self.oms(mode),
            rescan=DEFAULT_RESCAN_POLICY,
            max_snapshot_retries=self.risk.reconciliation.max_snapshot_retries,
            max_consecutive_inconclusive=self.risk.reconciliation.max_consecutive_inconclusive,
            clean_max_age=RECONCILIATION_MAX_AGE,
        )

    def outage(self) -> OutagePolicy:
        b = self.risk.broker_outage
        return OutagePolicy(
            min_duration=timedelta(seconds=b.min_outage_seconds),
            min_counted_cycles=b.min_failed_cycles,
            min_cycle_spacing=timedelta(seconds=b.min_cycle_spacing_seconds),
        )

    def engine(self) -> RiskEngineConfig:
        return RiskEngineConfig(
            limits=self.risk,
            mode=RuntimeMode.SIMULATE,
            fees=self.fees,
            marks=self.marks(),
            calendar_max_age=self.calendar_max_age,
            reconciliation_max_age=RECONCILIATION_MAX_AGE,
        )
