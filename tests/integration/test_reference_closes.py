"""Step 16.9 (M2-2b): consolidated SIP reference closes — admission, timing, immutable
recording, conflicts, the default-off activation gate and discontinuity diagnostics.

Offline: a scripted ``BarSource`` (no network) and disposable simulation ledgers.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app.config import AppConfig
from swing_trading_bot.broker.errors import BrokerError, ServiceUnavailableError
from swing_trading_bot.broker.mapping import daily_bar_from_raw
from swing_trading_bot.broker.models import BarAdjustment, BarFeed, DailyBar
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.ledger.db import Database
from swing_trading_bot.ledger.loss_limit_store import (
    CloseSource,
    SessionClose,
    SessionCloseConflictsRepository,
    SessionClosesRepository,
)
from swing_trading_bot.ledger.loss_limits import LossBlocker, LossLimitConfig, LossLimitMonitor
from swing_trading_bot.ledger.portfolio import VirtualPortfolio
from swing_trading_bot.risk.reference_closes import (
    OutcomeStatus,
    ReferenceCloseRecorder,
    Rejection,
    admit,
    recording_eligible_at,
)
from tests.support.bars_fixture import t_of
from tests.support.cli_env import CONFIG_DIR
from tests.support.loss_limits import CONFIG, close, et, evaluate_at, hold
from tests.support.portfolio import new_ledger, seed_calendar

D = Decimal
MON, TUE, THU, FRI = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 12), date(2026, 11, 13)
SAT, NEXT_MON = date(2026, 11, 14), date(2026, 11, 16)
CANDIDATE = (CloseSource.OFFICIAL, CloseSource.CONSOLIDATED_BAR)
ENABLED = replace(CONFIG, consolidated_closes_enabled=True, baseline_close_sources=CANDIDATE)


def bar(symbol: str, day: date, close_px: str, *, received: datetime, volume: int = 1_000_000,
        vwap: str | None = "default", feed: BarFeed = "sip",
        adjustment: BarAdjustment = "raw") -> DailyBar:  # fmt: skip
    c = D(close_px)
    row: dict[str, Any] = {"t": t_of(day), "o": c, "h": c + 1, "l": c - 1, "c": c, "v": volume,
                           "n": 10}  # fmt: skip
    if vwap is not None:
        row["vw"] = c if vwap == "default" else D(vwap)
    return daily_bar_from_raw(row, symbol, feed=feed, adjustment=adjustment, received_at=received)


class Bars:
    """A scripted BarSource: queued responses (a mapping or a BrokerError), every call kept."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], date, date, str, str]] = []
        self.queue: deque[Mapping[str, tuple[DailyBar, ...]] | BrokerError] = deque()

    def get_daily_bars(self, symbols: Sequence[str], start: date, end: date, *,
                       feed: BarFeed, adjustment: BarAdjustment,
                       ) -> Mapping[str, tuple[DailyBar, ...]]:  # fmt: skip
        self.calls.append((tuple(symbols), start, end, feed, adjustment))
        item = self.queue.popleft()
        if isinstance(item, BrokerError):
            raise item
        return item


Env = tuple[Database, VirtualPortfolio, Bars]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    db, pf = new_ledger(tmp_path / "ref.sqlite3")  # created 2026-10-10
    yield db, pf, Bars()
    db.close()


def recorder(env: Env, config: LossLimitConfig = ENABLED) -> ReferenceCloseRecorder:
    db, pf, bars = env
    return ReferenceCloseRecorder(db, pf, bars, config)


def run(env: Env, now: datetime, config: LossLimitConfig = ENABLED) -> list[Any]:
    seed_calendar(env[0], now - timedelta(minutes=5))
    return list(recorder(env, config).run(now=now))


def stored(db: Database, symbol: str, day: date) -> SessionClose | None:
    with db.read() as s:
        return SessionClosesRepository().get(s, symbol, day, CloseSource.CONSOLIDATED_BAR)


# ============================================================ admission (pure)
GOOD_AT = recording_eligible_at(MON)


def test_admitted_close_is_raw_sip_with_source_identity() -> None:
    b = bar("AAPL", MON, "150.25", received=GOOD_AT)
    close_ = admit("AAPL", [b], session_date=MON)
    assert isinstance(close_, SessionClose)
    assert (close_.source, close_.feed, close_.adjustment) == (CloseSource.CONSOLIDATED_BAR,
                                                               "sip", "raw")  # fmt: skip
    assert (close_.close_price, close_.as_of, close_.received_at) == (D("150.25"), b.as_of,
                                                                      GOOD_AT)  # fmt: skip
    assert close_.session_date == MON and close_.source is not CloseSource.OFFICIAL


@pytest.mark.parametrize(
    ("bars", "expected"),
    [
        ([], Rejection.MISSING_BAR),
        ([bar("AAPL", MON, "10", received=GOOD_AT)] * 2, Rejection.DUPLICATE_BAR),
        ([bar("AAPL", TUE, "10", received=GOOD_AT)], Rejection.WRONG_SESSION),
        ([bar("MSFT", MON, "10", received=GOOD_AT)], Rejection.WRONG_SESSION),
        ([bar("AAPL", MON, "10", received=GOOD_AT, feed="iex")], Rejection.WRONG_FEED),
        ([bar("AAPL", MON, "10", received=GOOD_AT, adjustment="split")],
         Rejection.WRONG_ADJUSTMENT),
        ([bar("AAPL", MON, "10", received=GOOD_AT, volume=0)], Rejection.ZERO_VOLUME),
        ([bar("AAPL", MON, "10", received=GOOD_AT, vwap=None)], Rejection.VWAP_MISSING),
        ([bar("AAPL", MON, "10.00", received=GOOD_AT, vwap="8.90")],
         Rejection.CLOSE_VWAP_DEVIATION),  # 12.4% above VWAP
        ([bar("AAPL", MON, "10.00", received=GOOD_AT, vwap="11.20")],
         Rejection.CLOSE_VWAP_DEVIATION),  # 10.7% below VWAP
        ([bar("AAPL", MON, "10", received=GOOD_AT - timedelta(microseconds=1))],
         Rejection.OBSERVED_TOO_EARLY),
    ],
)  # fmt: skip
def test_admission_rejections(bars: list[DailyBar], expected: Rejection) -> None:
    assert admit("AAPL", bars, session_date=MON) is expected


def test_vwap_deviation_boundary_is_inclusive() -> None:
    at_limit = bar("AAPL", MON, "11.00", received=GOOD_AT, vwap="10.00")  # exactly 10%
    assert isinstance(admit("AAPL", [at_limit], session_date=MON), SessionClose)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"feed": "iex"}, "sip feed"),
        ({"adjustment": "split"}, "unadjusted"),
        ({"adjustment": None}, "unadjusted"),
    ],
)
def test_session_close_model_guards(changes: dict[str, Any], message: str) -> None:
    base = SessionClose("AAPL", MON, D(1), CloseSource.CONSOLIDATED_BAR, GOOD_AT, GOOD_AT,
                        feed="sip", adjustment="raw")  # fmt: skip
    with pytest.raises(ValueError, match=message):
        replace(base, **changes)
    with pytest.raises(ValueError, match="only a CONSOLIDATED_BAR"):
        SessionClose("AAPL", MON, D(1), CloseSource.OFFICIAL, GOOD_AT, GOOD_AT, adjustment="raw")


# ============================================================ activation gate
def test_disabled_by_default_and_makes_no_request(env: Env) -> None:
    db, pf, bars = env
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    assert CONFIG.consolidated_closes_enabled is False
    assert run(env, et(TUE, 9), CONFIG) == []
    assert bars.calls == []


def test_real_observe_policy_stays_official_only() -> None:
    config = AppConfig.load(CONFIG_DIR)
    observe = config.loss_limits(RuntimeMode.OBSERVE)
    assert observe.baseline_close_sources == (CloseSource.OFFICIAL,)
    assert observe.consolidated_closes_enabled is False
    assert config.loss_limits(RuntimeMode.SIMULATE).consolidated_closes_enabled is False
    with pytest.raises(ValueError, match="consolidated_closes_enabled"):
        replace(observe, baseline_close_sources=(CloseSource.OFFICIAL,
                                                 CloseSource.CONSOLIDATED_BAR))  # fmt: skip
    with pytest.raises(ValueError, match="daily-bar"):
        replace(observe, consolidated_closes_enabled=True,
                baseline_close_sources=(CloseSource.DAILY_BAR,))  # fmt: skip


def test_consolidated_close_values_a_baseline_only_when_enabled(env: Env) -> None:
    db, pf, bars = env
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    bars.queue.append({"AAPL": (bar("AAPL", MON, "98.00", received=et(MON, 20)),)})
    run(env, et(MON, 20))
    # default (OFFICIAL/SIM) policy: the consolidated close is not substituted
    off = evaluate_at(db, pf, LossLimitMonitor(db, pf, CONFIG), et(TUE, 10), {"AAPL": "98"})
    assert LossBlocker.SESSION_BASELINE_MISSING in off.blockers
    assert any("CONSOLIDATED_BAR/sip" in p for p in off.problems)
    # the explicitly enabled candidate policy values it, labelled as such
    on = evaluate_at(db, pf, LossLimitMonitor(db, pf, ENABLED), et(TUE, 10), {"AAPL": "98"})
    assert on.daily_baseline is not None and on.daily_baseline.equity == D("4960.00")
    (m,) = on.daily_baseline.marks
    assert (m["source"], m["feed"], D(m["close"])) == ("CONSOLIDATED_BAR", "sip", D("98.00"))


# ============================================================ timing
def test_no_request_without_holdings(env: Env) -> None:
    out = run(env, et(TUE, 10))
    assert {o.status for o in out} == {OutcomeStatus.NOT_NEEDED}
    assert env[2].calls == []


@pytest.mark.parametrize(
    ("now", "fetched"),
    [
        (et(MON, 16, 30), False),  # after the close, before the quiet threshold
        (et(MON, 19, 59), False),
        (et(MON, 20, 0), True),  # threshold: 20:00 ET on the session date
        (et(TUE, 9, 0), True),  # first cycle the following morning
    ],
)
def test_recording_threshold(env: Env, now: datetime, fetched: bool) -> None:
    db, pf, bars = env
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    bars.queue.append({"AAPL": (bar("AAPL", MON, "99.50", received=now),)})
    out = run(env, now)
    statuses = {o.session_date: o.status for o in out}
    assert statuses[MON] is (OutcomeStatus.FETCHED if fetched else OutcomeStatus.NOT_YET_ELIGIBLE)
    assert bars.calls == ([(("AAPL",), MON, MON, "sip", "raw")] if fetched else [])
    assert (stored(db, "AAPL", MON) is not None) is fetched


def test_weekend_fetches_friday_once(env: Env) -> None:
    db, pf, bars = env
    hold(db, pf, "AAPL", "20", "100.00", at=et(THU, 10))
    bars.queue.append({"AAPL": (bar("AAPL", FRI, "101", received=et(SAT, 10)),)})
    out = run(env, et(SAT, 10))  # current session Monday; daily and weekly reference Friday
    assert [o.session_date for o in out] == [FRI]
    assert bars.calls == [(("AAPL",), FRI, FRI, "sip", "raw")]
    assert run(env, et(NEXT_MON, 9)) and bars.calls == [(("AAPL",), FRI, FRI, "sip", "raw")]


def test_early_close_and_holiday(env: Env) -> None:
    db, pf, bars = env
    wed, thanksgiving, early = date(2026, 11, 25), date(2026, 11, 26), date(2026, 11, 27)
    hold(db, pf, "AAPL", "20", "100.00", at=et(date(2026, 11, 23), 10))
    # Thanksgiving (holiday): the reference is Wednesday, eligible since Wed 20:00
    bars.queue.append({"AAPL": (bar("AAPL", wed, "100", received=et(thanksgiving, 10)),)})
    out = run(env, et(thanksgiving, 10))
    assert [(o.session_date, o.status) for o in out if o.status is OutcomeStatus.FETCHED] == [
        (wed, OutcomeStatus.FETCHED)]  # fmt: skip
    # early close (13:00): still the same 20:00 threshold on the session date
    after_close = run(env, et(early, 15))
    assert {o.session_date: o.status for o in after_close}[early] is OutcomeStatus.NOT_YET_ELIGIBLE
    bars.queue.append({"AAPL": (bar("AAPL", early, "101", received=et(early, 20)),)})
    run(env, et(early, 20))
    assert stored(db, "AAPL", early) is not None
    assert bars.calls[-1] == (("AAPL",), early, early, "sip", "raw")


# ============================================================ missing data
def test_missing_bar_or_fetch_failure_records_nothing(env: Env) -> None:
    db, pf, bars = env
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    bars.queue.append(ServiceUnavailableError("503", status_code=503))
    (out,) = [o for o in run(env, et(MON, 20)) if o.session_date == MON]
    assert out.status is OutcomeStatus.FETCH_FAILED and "ServiceUnavailableError" in str(out.error)
    bars.queue.append({})  # the provider has no bar for the symbol
    (out,) = [o for o in run(env, et(MON, 21)) if o.session_date == MON]
    assert out.rejected == {"AAPL": Rejection.MISSING_BAR}
    assert stored(db, "AAPL", MON) is None
    r = evaluate_at(db, pf, LossLimitMonitor(db, pf, ENABLED), et(TUE, 10), {"AAPL": "99"})
    assert LossBlocker.SESSION_BASELINE_MISSING in r.blockers  # entries blocked, no guess


# ============================================================ immutability & conflicts
def test_conflicting_later_observation_is_kept_and_blocks(tmp_path: Path) -> None:
    path = tmp_path / "conflict.sqlite3"
    db, pf = new_ledger(path)
    bars = Bars()
    env = (db, pf, bars)
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    hold(db, pf, "MSFT", "5", "200.00", at=et(MON, 10))
    bars.queue.append({"AAPL": (bar("AAPL", MON, "99.00", received=et(MON, 20)),)})
    run(env, et(MON, 20))  # AAPL recorded, MSFT missing
    bars.queue.append({"AAPL": (bar("AAPL", MON, "99.40", received=et(MON, 21)),),
                       "MSFT": (bar("MSFT", MON, "201.00", received=et(MON, 21)),)})  # fmt: skip
    (out,) = [o for o in run(env, et(MON, 21)) if o.session_date == MON]
    assert (out.conflicts, out.recorded) == (("AAPL",), ("MSFT",))
    original = stored(db, "AAPL", MON)
    assert original is not None and original.close_price == D("99.00")  # never overwritten
    with db.read() as s:
        (conflict,) = SessionCloseConflictsRepository().for_sessions(s, [MON])
    assert (conflict.recorded_close, conflict.observed_close) == (D("99.00"), D("99.40"))
    mon = LossLimitMonitor(db, pf, ENABLED)
    r = evaluate_at(db, pf, mon, et(TUE, 10), {"AAPL": "99", "MSFT": "201"})
    assert LossBlocker.REFERENCE_CLOSE_CONFLICT in r.blockers
    assert any("later observed 99.40" in p for p in r.problems)
    db.close()

    # a restart keeps the conflict, the block and the original close
    db = Database.open(path, RuntimeMode.SIMULATE, auto_migrate=False)
    pf = VirtualPortfolio(db, pf._config)
    mon = LossLimitMonitor(db, pf, ENABLED)
    with db.read() as s:
        assert LossBlocker.REFERENCE_CLOSE_CONFLICT in mon.report(s, now=et(TUE, 10, 5)).blockers
        assert len(SessionCloseConflictsRepository().for_sessions(s, [MON])) == 1
    assert stored(db, "AAPL", MON).close_price == D("99.00")  # type: ignore[union-attr]
    db.close()


def test_conflict_block_ends_only_when_the_session_is_no_longer_referenced(env: Env) -> None:
    db, pf, bars = env
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    hold(db, pf, "MSFT", "5", "200.00", at=et(MON, 10))
    bars.queue.append({"AAPL": (bar("AAPL", MON, "99.00", received=et(MON, 20)),)})
    run(env, et(MON, 20))
    bars.queue.append({"AAPL": (bar("AAPL", MON, "98.00", received=et(MON, 21)),),
                       "MSFT": (bar("MSFT", MON, "201.00", received=et(MON, 21)),)})  # fmt: skip
    run(env, et(MON, 21))
    mon = LossLimitMonitor(db, pf, ENABLED)
    tue = evaluate_at(db, pf, mon, et(TUE, 10), {"AAPL": "99", "MSFT": "201"})
    assert LossBlocker.REFERENCE_CLOSE_CONFLICT in tue.blockers
    for sym in ("AAPL", "MSFT"):  # Tuesday's own closes, recorded normally
        close(mon, sym, TUE, "100.00")
    wed = evaluate_at(db, pf, LossLimitMonitor(db, pf, replace(
        ENABLED, baseline_close_sources=(CloseSource.OFFICIAL, CloseSource.CONSOLIDATED_BAR,
                                         CloseSource.SIM))), et(date(2026, 11, 11), 10),
        {"AAPL": "100", "MSFT": "201"})  # fmt: skip
    # Wednesday's daily baseline references Tuesday; the week still references the prior
    # Friday, untouched by Monday's conflict
    assert LossBlocker.REFERENCE_CLOSE_CONFLICT not in wed.blockers
    with db.read() as s:  # the audit row is permanent
        assert len(SessionCloseConflictsRepository().for_sessions(s, [MON])) == 1


# ============================================================ diagnostics
@pytest.mark.parametrize(("mark", "flagged"), [("65.00", True), ("75.00", False),
                                                ("130.00", True)])  # fmt: skip
def test_discontinuity_is_a_diagnostic_not_a_limit(env: Env, mark: str, flagged: bool) -> None:
    db, pf, _ = env
    hold(db, pf, "AAPL", "20", "100.00", at=et(MON, 10))
    mon = LossLimitMonitor(db, pf, CONFIG)
    close(mon, "AAPL", MON, "100.00")
    r = evaluate_at(db, pf, mon, et(TUE, 10), {"AAPL": mark})
    assert ([g.symbol for g in r.discontinuities] == ["AAPL"]) is flagged
    assert not any("DISCONTINUITY" in b.value for b in r.blockers)
    if mark == "65.00":  # a genuine -35% loss is still a loss when quantities verify
        assert r.lockout_active and r.weekly_breach_active
