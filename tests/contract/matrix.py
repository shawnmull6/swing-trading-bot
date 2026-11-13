"""The read-only broker contract verification matrix (ARCHITECTURE §12.22).

Each row separates three things:

1. ``offline`` — what the code's handling is verified to do, offline, against synthetic
   payloads (``test_contract_replay.py``). This proves our parsing and fail-closed
   behaviour, **not** Alpaca's behaviour.
2. ``live_read`` — what only a real read-only Alpaca response can establish (the opt-in
   ``test_alpaca_paper_read.py`` check named in ``check``). Not yet run.
3. ``paper_orders`` — what cannot be verified until authorized paper-order execution
   (M2 pre-execution suite).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    id: str
    behaviour: str
    offline: str
    live_read: str
    paper_orders: str
    check: str | None
    replay_tests: tuple[str, ...]


MATRIX: tuple[Row, ...] = (
    Row("C1", "Account identity and environment",
        "strict identity validation (UUID id, ACTIVE, USD, paper label) fails closed",
        "real id shape, status, identity stability; PA prefix reported only", "-",
        "account_identity", ("test_checks_pass_on_synthetic_account",
                             "test_identity_violations_fail_closed")),
    Row("C2", "Account configuration and restrictions",
        "restriction flags parsed; U6 protections reported",
        "real no_shorting / max_margin_multiplier / suspend_trade values", "-",
        "account_restrictions", ("test_checks_pass_on_synthetic_account",)),
    Row("C3", "Broker clock",
        "offset-aware timestamps required; is_open checked against the calendar",
        "real clock agrees with the real calendar", "-", "clock",
        ("test_checks_pass_on_synthetic_account",)),
    Row("C4", "Trading and settlement calendars",
        "internal consistency, settlement dates are settlement days",
        "real rows vs the fixture's ASSUMPTION holidays/settlement days; horizon", "-",
        "calendars", ("test_checks_pass_on_synthetic_account",)),
    Row("C5", "Positions and orders",
        "list parsing; float or malformed rows rejected",
        "flat-account responses; /orders after/limit semantics",
        "partial-fill order shapes, qty_available", "positions_and_orders",
        ("test_checks_pass_on_synthetic_account", "test_float_money_is_rejected")),
    Row("C6", "Unknown client-order-id lookup",
        "404 maps to None (not proof of absence)", "real status is 404 (not 422)",
        "visibility latency after a submit", "unknown_client_order_id",
        ("test_error_classification",)),
    Row("C7", "Activity retrieval and pagination",
        "page-size independence, tail confirmation, window partition, exact-100 boundary, "
        "duplicate and non-advancing cursors fail closed",
        "real page_token semantics, after/until inclusivity, id ordering, non-trade types",
        "FILL granularity, FEE order_id/subtype, DIV qty/per_share_amount",
        "activity_pagination", ("test_checks_pass_on_synthetic_account",
                                "test_exact_page_boundary_needs_one_more_request",
                                "test_non_advancing_cursor_fails_closed")),
    Row("C8", "Latest trades and daily bars",
        "UTC timestamps, exact Decimal prices, midnight-ET bar dates",
        "feed entitlement (IEX/SIP); real timestamp semantics; IEX close is not official",
        "-", "market_data", ("test_checks_pass_on_synthetic_account",)),
    Row("C9", "Decimal precision", "trading money fields must be strings; floats rejected; "
        "market-data numbers decoded as Decimal", "no float in real trading payloads",
        "fill price precision", "raw_decimals",
        ("test_float_money_is_rejected", "test_checks_pass_on_synthetic_account")),
    Row("C10", "Malformed payloads", "non-JSON, missing fields, naive timestamps, unknown "
        "statuses rejected", "- (cannot be provoked read-only)", "-", None,
        ("test_malformed_payloads_fail_closed",)),
    Row("C11", "Error classification", "401/403→auth, 404→not found, 429→rate limited with "
        "Retry-After, 5xx/timeout/network→unavailable with category", "real 429/5xx shapes "
        "only if they occur naturally; deliberate bad-key 401 check NOT authorized (D15-7)",
        "504 on submit", None, ("test_error_classification",)),
    Row("C12", "Posting lag and completeness", "short page ends a window; closed windows "
        "respect the lag; late activities found by re-scans",
        "not observable on a flat account (no timestamped fills)",
        "measured fill-activity lag vs order status", None,
        ("test_exact_page_boundary_needs_one_more_request",)),
    Row("C13", "Official close", "daily bars are observations; official closes unsupported "
        "(fail closed)", "SIP entitlement and whether any official source exists", "-",
        "market_data", ("test_official_closes_are_unsupported",)),
)  # fmt: skip
