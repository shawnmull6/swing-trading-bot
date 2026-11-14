"""Step 16.5A: HTTP 401/403 classification in the GET-only transport.

Only a data-host 403 whose body names a subscription restriction becomes a
``MarketDataEntitlementError``; every other 403 stays a blocking ``AuthenticationError``
explicitly marked unresolved, and 401 is always rejected credentials. Bodies are
bounded, never echoed, and the entitlement wording is a fixture (not live-verified).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from swing_trading_bot.app.cli import exit_code_for
from swing_trading_bot.app.exit_codes import ExitCode
from swing_trading_bot.broker.alpaca_http import (
    MAX_FORBIDDEN_BODY_BYTES,
    AlpacaReadTransport,
    Service,
    raise_for_status,
)
from swing_trading_bot.broker.errors import (
    AuthenticationError,
    AuthFailureKind,
    BrokerError,
    DataEntitlementKind,
    MarketDataEntitlementError,
    OrderState,
    submit_failure_state,
)
from swing_trading_bot.config.settings import settings_from_mapping
from swing_trading_bot.domain.enums import HaltReason
from swing_trading_bot.oms.lifecycle import read_error_halt, read_error_outage_category

FAKE_KEY = "PKFAKETESTKEY000000001"
FAKE_SECRET = "fakeTestSecretValue/DoNotUse/0000000000001"
SETTINGS = settings_from_mapping(
    {"ALPACA_API_KEY": FAKE_KEY, "ALPACA_SECRET_KEY": FAKE_SECRET, "TRADING_MODE": "paper"}
)
#: Publicly reported wording of Alpaca's data-host 403 for recent SIP data on a plan that
#: does not include it. Step 16.5 did not capture the body: a fixture, not live evidence.
RECENT_SIP = '{"message": "subscription does not permit querying recent SIP data"}'


def response(status: int, body: bytes | str = b"") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode() if isinstance(body, str) else body
    return r


def error_for(status: int, body: bytes | str, service: Service | None = "data") -> BrokerError:
    with pytest.raises(BrokerError) as info:
        raise_for_status(response(status, body), "GET data/stocks/bars", service)
    return info.value


class FakeSession:
    def __init__(self, *results: requests.Response) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        return self.results.pop(0)


# ============================================================ 401 vs 403
@pytest.mark.parametrize("service", ["data", "trading", None])
@pytest.mark.parametrize("body", ["", RECENT_SIP, '{"message": "unauthorized."}'])
def test_401_is_always_rejected_credentials(service: Service | None, body: str) -> None:
    err = error_for(401, body, service)
    assert type(err) is AuthenticationError
    assert err.kind is AuthFailureKind.CREDENTIALS_REJECTED and err.status_code == 401
    assert read_error_halt(err) is HaltReason.AUTH


@pytest.mark.parametrize("service", ["trading", None])
def test_403_off_the_data_host_is_unresolved_auth(service: Service | None) -> None:
    """The trading host keeps the pre-16.5A 403 -> AUTH rule, whatever the body says."""
    err = error_for(403, RECENT_SIP, service)
    assert type(err) is AuthenticationError
    assert err.kind is AuthFailureKind.FORBIDDEN_UNRESOLVED
    assert err.evidence == "trading_host"
    assert read_error_halt(err) is HaltReason.AUTH


# ============================================================ known entitlement bodies
@pytest.mark.parametrize(
    ("body", "kind", "feed"),
    [
        (RECENT_SIP, DataEntitlementKind.RECENT_DATA_WINDOW, "sip"),
        ('{"message": "subscription does not permit querying recent SIP data."}',
         DataEntitlementKind.RECENT_DATA_WINDOW, "sip"),
        ('{"code": 40310000, "message": "Subscription  does not permit querying\\nrecent '
         'SIP data"}', DataEntitlementKind.RECENT_DATA_WINDOW, "sip"),
    ],
)  # fmt: skip
def test_recognized_data_host_403_is_an_entitlement_error(
    body: str, kind: DataEntitlementKind, feed: str
) -> None:
    err = error_for(403, body)
    assert isinstance(err, MarketDataEntitlementError)
    assert AuthenticationError not in type(err).__mro__
    assert (err.kind, err.feed, err.status_code) == (kind, feed, 403)
    assert not err.read_retryable and err.order_state is OrderState.NOT_APPLICABLE
    assert (
        str(err)
        == f"GET data/stocks/bars: HTTP 403: market data not permitted ({kind}, feed {feed})"
    )


def test_entitlement_error_blocks_without_claiming_bad_credentials() -> None:
    err = error_for(403, RECENT_SIP)
    assert read_error_halt(err) is None  # no AUTH (or any) persistent halt
    assert read_error_outage_category(err) is None  # not a broker outage either
    assert exit_code_for(err) is ExitCode.BROKER_UNAVAILABLE  # the read still fails
    assert submit_failure_state(err) is OrderState.UNKNOWN  # write semantics unchanged


# ============================================================ unknown / malformed
@pytest.mark.parametrize(
    ("body", "evidence"),
    [
        ("", "no_body"),
        ("   \n", "no_body"),
        ("forbidden", "malformed_body"),
        (b"\xff\xfe{}", "malformed_body"),
        ("{" * 3, "malformed_body"),
        ("[" * 100_000 + "]" * 100_000, "oversized_body"),
        ("[]", "no_message"),
        ('"subscription does not permit querying recent SIP data"', "no_message"),
        ('{"msg": "subscription does not permit querying recent SIP data"}', "no_message"),
        ('{"message": 403}', "no_message"),
        ('{"message": null}', "no_message"),
        ('{"message": "forbidden."}', "unrecognized_message"),
        ('{"message": "request is not authorized"}', "unrecognized_message"),
        ('{"message": "subscription does not permit querying recent SIP data for PK123"}',
         "unrecognized_message"),
        ('{"message": "your subscription does not permit querying recent SIP data"}',
         "unrecognized_message"),
        ('{"message": "subscription does not permit querying recent s1p data"}',
         "unrecognized_message"),
        # plausible variants that were never observed stay unresolved (no speculation)
        ('{"message": "subscription does not permit querying SIP data"}',
         "unrecognized_message"),
        ('{"message": "subscription does not permit querying recent IEX data"}',
         "unrecognized_message"),
        ('{"message": "subscription does not permit querying OPRA data"}',
         "unrecognized_message"),
    ],
)  # fmt: skip
def test_unknown_or_malformed_403_stays_blocking_and_unresolved(
    body: str | bytes, evidence: str
) -> None:
    err = error_for(403, body)
    assert type(err) is AuthenticationError
    assert err.kind is AuthFailureKind.FORBIDDEN_UNRESOLVED and err.evidence == evidence
    assert read_error_halt(err) is HaltReason.AUTH  # fail closed exactly as before
    assert str(err) == f"GET data/stocks/bars: HTTP 403 (unresolved: {evidence})"


def test_deeply_nested_small_body_is_malformed_not_a_crash() -> None:
    body = "[" * 1000 + "]" * 1000
    assert len(body) <= MAX_FORBIDDEN_BODY_BYTES
    err = error_for(403, '{"message": ' + body + "}")
    assert isinstance(err, AuthenticationError) and err.evidence == "no_message"
    err = error_for(403, "[" * 2000)  # within the cap, unterminated
    assert isinstance(err, AuthenticationError) and err.evidence == "malformed_body"


def test_oversized_body_is_not_inspected_even_if_recognizable() -> None:
    padded = json.dumps({"message": "subscription does not permit querying recent SIP data",
                         "pad": "x" * MAX_FORBIDDEN_BODY_BYTES})  # fmt: skip
    err = error_for(403, padded)
    assert isinstance(err, AuthenticationError) and err.evidence == "oversized_body"


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("service", ["data", "trading"])
def test_error_text_never_carries_the_body(status: int, service: Service) -> None:
    leaky = json.dumps({"message": f"bad key {FAKE_KEY} secret {FAKE_SECRET}",
                        "account_number": "PA0000FAKE00"})  # fmt: skip
    err = error_for(status, leaky, service)
    for value in (FAKE_KEY, FAKE_SECRET, "PA0000FAKE00", "bad key"):
        assert value not in str(err) and value not in repr(err.args)


# ============================================================ through the transport
def test_transport_passes_the_service_to_classification() -> None:
    session = FakeSession(response(403, RECENT_SIP), response(403, RECENT_SIP))
    t = AlpacaReadTransport(SETTINGS, session=session)  # type: ignore[arg-type]
    with pytest.raises(MarketDataEntitlementError):
        t.get_json("data", "/stocks/bars", {"symbols": "SPY"})
    with pytest.raises(AuthenticationError) as info:
        t.get_json("trading", "/account", {})
    assert info.value.kind is AuthFailureKind.FORBIDDEN_UNRESOLVED
    assert len(session.calls) == 2  # one GET each; nothing retried


def test_authentication_error_kind_defaults_follow_status() -> None:
    assert AuthenticationError("x", status_code=401).kind is AuthFailureKind.CREDENTIALS_REJECTED
    assert AuthenticationError("x", status_code=403).kind is AuthFailureKind.FORBIDDEN_UNRESOLVED
    assert AuthenticationError("x").kind is AuthFailureKind.FORBIDDEN_UNRESOLVED
