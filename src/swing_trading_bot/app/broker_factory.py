"""The production read-only broker factory and account-identity validation (§12.22).

The factory builds exactly one thing: :class:`AlpacaPaperReadAdapter` over
:class:`AlpacaReadTransport`. That transport has a single method (HTTP GET), contacts only
the allowlisted HTTPS paper-trading and market-data hosts, never follows redirects and
ignores environment proxies. No SDK ``TradingClient`` is constructed and nothing here
can submit, replace or cancel an order (structural tests in
``tests/unit/test_broker_factory.py``).

**What proves the paper environment.** Paper and live accounts have separate API keys
and separate hosts. The transport can reach only the paper host, so any authenticated
response is from a paper account; the adapter labels it ``environment = "paper"``. The
``PA`` account-number prefix is *not* used as proof (it is undocumented); it is only
reported by contract checks.

**Strict identity.** :func:`account_identity_problems` fails closed on an account whose
id is not a canonical UUID, whose status is not ``ACTIVE``, whose currency is not USD, or
whose environment label is not the expected one. ``init-ledger`` refuses on any problem;
the runtime preflight treats one as an identity failure (persistent ``RECON_ACCOUNT``
halt) before anything is ingested, in addition to the existing recorded-identity match.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final

import requests

from swing_trading_bot.app.credentials import LoadedCredentials, load_credentials
from swing_trading_bot.broker.alpaca_http import AlpacaReadTransport, Sleeper
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.broker.models import AccountSnapshot
from swing_trading_bot.observability.logging import register_secrets

_UUID: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ACCEPTED_STATUS: Final = frozenset({"ACTIVE"})


def account_identity_problems(account: AccountSnapshot, *, environment: str) -> list[str]:
    """Reasons the connected account's identity is uncertain (empty when strict-valid).
    Messages never include the account id itself."""
    problems: list[str] = []
    if account.environment != environment:
        problems.append(f"broker environment is {account.environment!r}, not {environment!r}")
    if environment == "paper" and not _UUID.match(account.account_id):
        problems.append("broker account id is not a canonical UUID")
    if not account.account_id.strip():
        problems.append("broker account id is blank")
    if account.status not in ACCEPTED_STATUS:
        problems.append(f"broker account status is {account.status!r}, not ACTIVE")
    if account.currency != "USD":
        problems.append(f"account currency {account.currency} is not supported")
    return problems


def mask_account_id(account_id: str) -> str:
    """For operator output only: the last 4 characters, never the full id."""
    return "…" + account_id[-4:] if len(account_id) > 4 else "…"


SessionFactory = Callable[[], requests.Session]


def production_broker_factory(
    environ: Mapping[str, str] | None = None,
    *,
    session_factory: SessionFactory | None = None,
    on_loaded: Callable[[LoadedCredentials], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Sleeper | None = None,
) -> Callable[[], AlpacaPaperReadAdapter]:
    """A factory that loads credentials lazily (only when a broker command runs), registers
    the secret values with the log redactor, and returns the read-only adapter."""
    source = os.environ if environ is None else environ

    def make() -> AlpacaPaperReadAdapter:
        loaded = load_credentials(source)
        register_secrets(loaded.settings.secret_values())
        if on_loaded is not None:
            on_loaded(loaded)
        session = session_factory() if session_factory is not None else None
        transport = AlpacaReadTransport(loaded.settings, session=session)
        options: dict[str, Any] = {}
        if clock is not None:
            options["clock"] = clock
        if sleep is not None:
            options["sleep"] = sleep  # e.g. a supervisor that bounds backoff by a deadline
        return AlpacaPaperReadAdapter(transport, **options)

    return make
