"""M2-1: account restriction fields and M2 account protections (mapping and policy).

Three states per restriction field — true, false, unknown (absent or ``null``) — plus
malformed values, which never reach the policy: the mapper rejects them
(``InvalidBrokerDataError`` → exit 10 / ``BROKER_PAYLOAD``). Unknown is never false.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.errors import InvalidBrokerDataError
from swing_trading_bot.broker.mapping import account_config_from_raw, account_from_raw
from swing_trading_bot.broker.models import AccountConfigSnapshot, AccountSnapshot
from swing_trading_bot.broker.sim import SimBroker, SimClock, SimConfig
from swing_trading_bot.domain.enums import RuntimeMode
from swing_trading_bot.risk.account_restrictions import (
    ENTRY_CRITICAL,
    ORDER_WRITE_RESTRICTIONS,
    RestrictionField,
    assess_restrictions,
    m2_account_state_problems,
    m2_entry_problems,
    u6_problems,
)
from tests.support.alpaca_fake import ACCOUNT, CONFIG
from tests.support.gates import GATE_CONFIG
from tests.support.portfolio import NOW

F = RestrictionField
SRC = Path(__file__).resolve().parents[2] / "src" / "swing_trading_bot"
ACCOUNT_OPTIONAL = ("trade_suspended_by_user", "transfers_blocked")
CONFIG_OPTIONAL = ("closing_transactions_only",)
MALFORMED: list[object] = ["true", "false", "", 0, 1, 1.5, [], [True], {}, {"a": True}]


def acct(**changes: Any) -> AccountSnapshot:
    return account_from_raw({**ACCOUNT, **changes}, as_of=NOW, environment="paper")


def cfg(**changes: Any) -> AccountConfigSnapshot:
    return account_config_from_raw({**CONFIG, **changes})


def without(raw: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k != key}


def protected_cfg(**changes: Any) -> AccountConfigSnapshot:
    return cfg(no_shorting=True, max_margin_multiplier="1", **changes)


# ============================================================ mapping: observed shape
def test_observed_shapes_carry_every_restriction_field_as_a_boolean() -> None:
    for key in (*ACCOUNT_OPTIONAL, "trading_blocked", "account_blocked"):
        assert type(ACCOUNT[key]) is bool, key
    for key in (*CONFIG_OPTIONAL, "suspend_trade"):
        assert type(CONFIG[key]) is bool, key
    a, c = acct(), cfg()
    assert (a.trade_suspended_by_user, a.transfers_blocked) == (False, False)
    assert c.closing_transactions_only is False
    r = assess_restrictions(a, c)
    assert r.fully_known and r.entries_permitted and r.active == ()


# ============================================================ mapping: three states
@pytest.mark.parametrize("key", ACCOUNT_OPTIONAL)
@pytest.mark.parametrize("value", [True, False])
def test_account_restriction_explicit_values_are_kept(key: str, value: bool) -> None:
    assert getattr(acct(**{key: value}), key) is value


@pytest.mark.parametrize("key", CONFIG_OPTIONAL)
@pytest.mark.parametrize("value", [True, False])
def test_config_restriction_explicit_values_are_kept(key: str, value: bool) -> None:
    assert getattr(cfg(**{key: value}), key) is value


@pytest.mark.parametrize("key", ACCOUNT_OPTIONAL)
def test_account_restriction_absent_or_null_is_unknown_never_false(key: str) -> None:
    absent = account_from_raw(without(ACCOUNT, key), as_of=NOW, environment="paper")
    assert getattr(absent, key) is None
    assert getattr(acct(**{key: None}), key) is None


@pytest.mark.parametrize("key", CONFIG_OPTIONAL)
def test_config_restriction_absent_or_null_is_unknown_never_false(key: str) -> None:
    assert getattr(account_config_from_raw(without(CONFIG, key)), key) is None
    assert getattr(cfg(**{key: None}), key) is None


@pytest.mark.parametrize("key", ACCOUNT_OPTIONAL)
@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_account_restriction_is_a_payload_violation(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        acct(**{key: value})


@pytest.mark.parametrize("key", CONFIG_OPTIONAL)
@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_config_restriction_is_a_payload_violation(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        cfg(**{key: value})


@pytest.mark.parametrize("key", ["trading_blocked", "account_blocked"])
@pytest.mark.parametrize("value", ["absent", None, "false"])
def test_existing_required_account_restrictions_stay_strict(key: str, value: object) -> None:
    raw = without(ACCOUNT, key) if value == "absent" else {**ACCOUNT, key: value}
    with pytest.raises(InvalidBrokerDataError, match=key):
        account_from_raw(raw, as_of=NOW, environment="paper")


@pytest.mark.parametrize("key", ["suspend_trade", "no_shorting", "max_margin_multiplier"])
@pytest.mark.parametrize("value", ["absent", None])
def test_protection_fields_stay_required(key: str, value: object) -> None:
    raw = without(CONFIG, key) if value == "absent" else {**CONFIG, key: value}
    with pytest.raises(InvalidBrokerDataError, match=key):
        account_config_from_raw(raw)


@pytest.mark.parametrize(("key", "value"), [("no_shorting", "true"), ("no_shorting", 1),
                                            ("max_margin_multiplier", 1.0),
                                            ("max_margin_multiplier", "one")])  # fmt: skip
def test_malformed_protection_fields_are_payload_violations(key: str, value: object) -> None:
    with pytest.raises(InvalidBrokerDataError, match=key):
        cfg(**{key: value})


# ============================================================ policy: assessment
@pytest.mark.parametrize(
    ("field", "a_changes", "c_changes"),
    [
        (F.TRADING_BLOCKED, {"trading_blocked": True}, {}),
        (F.ACCOUNT_BLOCKED, {"account_blocked": True}, {}),
        (F.TRADE_SUSPENDED_BY_USER, {"trade_suspended_by_user": True}, {}),
        (F.TRANSFERS_BLOCKED, {"transfers_blocked": True}, {}),
        (F.SUSPEND_TRADE, {}, {"suspend_trade": True}),
        (F.CLOSING_TRANSACTIONS_ONLY, {}, {"closing_transactions_only": True}),
    ],
)
def test_each_known_restriction_blocks_entries(
    field: RestrictionField, a_changes: dict[str, Any], c_changes: dict[str, Any]
) -> None:
    r = assess_restrictions(acct(**a_changes), cfg(**c_changes))
    assert r.active_fields == (field,) and r.active == (f"{field.value}=true",)
    assert not r.entries_permitted
    assert (r.order_writes_prevented != ()) is (field in ORDER_WRITE_RESTRICTIONS)


def test_non_active_status_blocks_entries_and_writes() -> None:
    r = assess_restrictions(acct(status="ACCOUNT_CLOSED"), cfg())
    assert r.active == ("status=ACCOUNT_CLOSED",) and r.order_writes_prevented == r.active
    assert not r.entries_permitted


def test_write_classification_matches_the_documented_policy() -> None:
    assert frozenset({F.CLOSING_TRANSACTIONS_ONLY, F.TRANSFERS_BLOCKED}).isdisjoint(
        ORDER_WRITE_RESTRICTIONS
    )
    assert {F.TRADE_SUSPENDED_BY_USER, F.CLOSING_TRANSACTIONS_ONLY} == ENTRY_CRITICAL


@pytest.mark.parametrize(
    ("field", "a", "c", "blocks_entries"),
    [
        (F.TRADE_SUSPENDED_BY_USER, {"trade_suspended_by_user": None}, {}, True),
        (F.CLOSING_TRANSACTIONS_ONLY, {}, {"closing_transactions_only": None}, True),
        (F.TRANSFERS_BLOCKED, {"transfers_blocked": None}, {}, False),
    ],
)
def test_unreported_restriction_is_unknown(
    field: RestrictionField, a: dict[str, Any], c: dict[str, Any], blocks_entries: bool
) -> None:
    r = assess_restrictions(acct(**a), cfg(**c))
    assert r.unreported == (field,) and r.active == () and not r.fully_known
    assert r.entries_permitted is not blocks_entries
    assert "not reported" in r.describe()


@pytest.mark.parametrize("missing", ["account", "config", "both"])
def test_unavailable_snapshot_never_permits_entries(missing: str) -> None:
    a = None if missing in {"account", "both"} else acct()
    c = None if missing in {"config", "both"} else cfg()
    r = assess_restrictions(a, c)
    assert r.unavailable and not r.entries_permitted and not r.fully_known
    assert "snapshot unavailable" in r.describe()


def test_unknown_never_reads_as_false_even_when_other_fields_are_clear() -> None:
    r = assess_restrictions(acct(trade_suspended_by_user=None, transfers_blocked=None),
                            cfg(closing_transactions_only=None))  # fmt: skip
    assert set(r.unreported) == {F.TRADE_SUSPENDED_BY_USER, F.TRANSFERS_BLOCKED,
                                 F.CLOSING_TRANSACTIONS_ONLY}  # fmt: skip
    assert r.active == () and not r.entries_permitted


# ============================================================ M2 protections
def test_m2_eligible_only_with_protections_and_verified_clear_state() -> None:
    assert m2_entry_problems(acct(), protected_cfg()) == ()
    assert protected_cfg().m2_protections_in_place


def test_shorting_enabled_is_an_m2_problem() -> None:
    c = cfg(no_shorting=False, max_margin_multiplier="1")
    assert not c.m2_protections_in_place
    assert u6_problems(c) == ("no_shorting=false (shorting enabled)",)


@pytest.mark.parametrize("multiplier", ["2", "4", "1.5", "0"])
def test_margin_multiplier_other_than_one_is_an_m2_problem(multiplier: str) -> None:
    c = cfg(no_shorting=True, max_margin_multiplier=multiplier)
    assert not c.m2_protections_in_place
    assert any("max_margin_multiplier" in p for p in m2_entry_problems(acct(), c))


def test_multiplier_spelled_one_point_zero_is_one() -> None:
    assert u6_problems(cfg(no_shorting=True, max_margin_multiplier="1.0")) == ()


def test_missing_configuration_fails_closed_for_m2() -> None:
    problems = m2_entry_problems(acct(), None)
    assert "account configuration unavailable: U6 protections unverified" in problems
    assert any("snapshot unavailable" in p for p in problems)


@pytest.mark.parametrize(
    ("a", "c"),
    [
        ({"trade_suspended_by_user": True}, {}),
        ({"transfers_blocked": True}, {}),
        ({}, {"closing_transactions_only": True}),
        ({}, {"suspend_trade": True}),
        ({"trade_suspended_by_user": None}, {}),
        ({"transfers_blocked": None}, {}),  # unknown transfers is M2-blocking
        ({}, {"closing_transactions_only": None}),
        ({"status": "INACTIVE"}, {}),
    ],
)
def test_any_restriction_or_unknown_state_blocks_m2(a: dict[str, Any], c: dict[str, Any]) -> None:
    assert m2_account_state_problems(acct(**a), protected_cfg(**c)) != ()
    assert m2_entry_problems(acct(**a), protected_cfg(**c)) != ()


def test_missing_account_fails_closed_for_m2() -> None:
    assert m2_account_state_problems(None, protected_cfg()) != ()


# ============================================================ runtime configuration
def test_paper_execute_requires_m2_protections_and_stays_unavailable() -> None:
    with pytest.raises(ValueError, match="requires require_m2_account_protections"):
        replace(GATE_CONFIG, mode=RuntimeMode.PAPER_EXECUTE)
    with pytest.raises(ValueError, match="not available in M1"):
        replace(GATE_CONFIG, mode=RuntimeMode.PAPER_EXECUTE, require_m2_account_protections=True)
    for mode in (RuntimeMode.OBSERVE, RuntimeMode.SIMULATE):
        assert replace(GATE_CONFIG, mode=mode).require_m2_account_protections is False


def test_app_config_keeps_m1_modes_report_only() -> None:
    from swing_trading_bot.app.config import AppConfig  # noqa: PLC0415

    config = AppConfig.load(SRC.parents[1] / "config")
    for mode in (RuntimeMode.OBSERVE, RuntimeMode.SIMULATE):
        assert config.gates(mode, trading_enabled=False).require_m2_account_protections is False


# ============================================================ simulation payloads
@pytest.mark.parametrize("key", [*ACCOUNT_OPTIONAL, *CONFIG_OPTIONAL])
@pytest.mark.parametrize("value", [None, True, False])
def test_sim_reports_or_omits_restrictions(key: str, value: bool | None) -> None:
    sim = SimBroker(SimClock(NOW), SimConfig(**{key: value}))
    snap = sim.get_account_config() if key in CONFIG_OPTIONAL else sim.get_account()
    assert getattr(snap, key) is value


def test_sim_account_blocked_is_configurable() -> None:
    sim = SimBroker(SimClock(NOW), SimConfig(account_blocked=True))
    assert sim.get_account().account_blocked is True


# ============================================================ structural safety
def test_policy_module_is_pure_and_cannot_write() -> None:
    tree = ast.parse((SRC / "risk" / "account_restrictions.py").read_text())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert modules == {"__future__", "dataclasses", "enum", "typing",
                       "swing_trading_bot.broker.models"}  # fmt: skip
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"submit_order", "cancel_order", "replace_order", "patch", "post"}


def test_read_adapter_still_has_no_write_capability() -> None:
    for name in ("submit_order", "cancel_order", "replace_order", "patch_account_config"):
        assert not hasattr(AlpacaPaperReadAdapter, name), name


def test_snapshots_have_no_defaults_for_restriction_fields() -> None:
    """Constructing a snapshot must state the restriction state explicitly."""
    a = acct()
    with pytest.raises(TypeError):
        AccountSnapshot(**{k: getattr(a, k) for k in AccountSnapshot.__dataclass_fields__
                           if k != "trade_suspended_by_user"})  # fmt: skip
    assert replace(a, trade_suspended_by_user=None).trade_suspended_by_user is None
    assert Decimal(1) == protected_cfg().max_margin_multiplier
