"""Step 15A: secret redaction and audit completeness through the PRODUCTION wiring
(``production_dependencies``: explicit credential loading → real HTTPS GET transport),
with synthetic credentials and an offline session. Also ``broker-check``."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

from swing_trading_bot.app.cli import main
from swing_trading_bot.app.commands import production_dependencies
from swing_trading_bot.broker.errors import ServiceUnavailableError
from swing_trading_bot.ledger.repositories import TradingStateRepository
from swing_trading_bot.observability.logging import configure_logging
from tests.support.alpaca_account import ACCOUNT_ID, AccountSession, FakeAlpacaAccount
from tests.support.cli_env import CONFIG_DIR

KEY = "PKSYNTHETICKEY7777777777"
SECRET = "synthetic/Secret/Echo/Value/7777777777777"
ENV = {"ALPACA_API_KEY": KEY, "ALPACA_SECRET_KEY": SECRET, "TRADING_MODE": "paper"}


class EchoingSession(AccountSession):
    """Returns 500s whose body echoes the credentials, as a misbehaving proxy might
    (``echo`` = how many consecutive /clock requests; reads are retried 3 times)."""

    echo = 0

    def get(self, url: str, **kw: Any) -> Any:
        if self.echo and url.endswith("/clock"):
            self.echo -= 1
            r = requests.Response()
            r.url, r.status_code = url, 500
            r._content = f'{{"message": "bad key {KEY} secret {SECRET}"}}'.encode()
            return r
        return super().get(url, **kw)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    target = logging.getLogger("swing_trading_bot")
    previous = (list(target.handlers), target.propagate, target.level)
    configure_logging(secrets=(), stream=stream, logger=target)
    target.propagate = False
    yield stream
    target.handlers[:] = previous[0]
    target.propagate, target.level = previous[1], previous[2]


class Prod:
    def __init__(self, root: Path, environ: dict[str, str] | None = None) -> None:
        self.data = root / "data"
        self.data.mkdir(exist_ok=True)
        self.account = FakeAlpacaAccount()
        self.session = EchoingSession(self.account)
        self.environ = ENV if environ is None else environ
        self.outputs: list[str] = []

    def run(self, *argv: str) -> tuple[int, dict[str, Any]]:
        deps = production_dependencies(self.environ, session_factory=lambda: self.session,  # type: ignore[arg-type,return-value]
                                       clock=self.account.clock)  # fmt: skip
        out = io.StringIO()
        code = main([*argv, "--data-dir", str(self.data), "--config-dir", str(CONFIG_DIR),
                     "--json", "--actor", "operator:audit"], deps=deps, stdout=out)  # fmt: skip
        self.outputs.append(out.getvalue())
        return code, json.loads(out.getvalue())

    def ledger_bytes(self) -> bytes:
        return b"".join(p.read_bytes() for p in self.data.rglob("*") if p.is_file())


def test_secrets_never_reach_output_logs_or_ledger(tmp_path: Path, log_stream: io.StringIO) -> None:
    p = Prod(tmp_path)
    ledger = str(p.data / "ledger.sqlite3")
    assert p.run("init-ledger", "--confirm-path", ledger, "--confirm-account", ACCOUNT_ID,
                 "--confirm-environment", "paper")[0] == 0  # fmt: skip
    p.account.clock.advance(minutes=20)
    assert p.run("reconcile")[0] == 0
    assert p.run("resume", "--reason", "go")[0] == 0
    p.session.echo = 3  # the broker echoes the credentials in an error body, persistently
    p.account.clock.advance(minutes=1)
    code, env = p.run("sync")
    assert code == 8 and "REDACTED" in env["result"]["preflight"]["broker_error"]
    p.account.down["/account"] = lambda: ServiceUnavailableError("down", status_code=503)
    p.account.clock.advance(minutes=1)
    assert p.run("sync")[0] == 8
    del p.account.down["/account"]
    p.account.clock.advance(minutes=1)
    assert p.run("broker-check")[0] == 0
    corpus = "\n".join(p.outputs) + log_stream.getvalue()
    for secret in (KEY, SECRET):
        assert secret not in corpus
        assert secret.encode() not in p.ledger_bytes()  # ledger, WAL and lock files
    assert '"credential' not in p.ledger_bytes().decode("latin-1")
    assert "APCA-API-SECRET-KEY" in p.session.header_names  # the secret WAS sent, by header


def test_audit_trail_is_complete_after_an_incident(tmp_path: Path) -> None:
    p = Prod(tmp_path)
    ledger = p.data / "ledger.sqlite3"
    p.run("init-ledger", "--confirm-path", str(ledger), "--confirm-account", ACCOUNT_ID,
          "--confirm-environment", "paper")  # fmt: skip
    p.account.clock.advance(minutes=20)
    p.run("reconcile")
    p.run("resume", "--reason", "go")
    p.account.deposit("40.00")
    p.account.clock.advance(minutes=20)
    p.run("reconcile")  # MISMATCH → halt
    p.run("resume", "--reason", "refused")  # audited refusal
    _code, env = p.run("rebaseline", "propose", "--reason", "owner deposit")
    rid = str(env["result"]["request_id"])
    p.account.clock.advance(minutes=1)
    p.run("rebaseline", "approve", rid, "--acknowledge-unexplained", "40", "--reason", "ok")
    p.account.clock.advance(minutes=2)
    assert p.run("resume", "--reason", "clean")[0] == 0
    p.account.down["*"] = lambda: ServiceUnavailableError("down", status_code=503)
    start = p.account.clock.now
    for minute in (0, 15, 30):
        p.account.clock.now = start + timedelta(minutes=minute)
        p.run("sync")
    from swing_trading_bot.domain.enums import RuntimeMode  # noqa: PLC0415
    from swing_trading_bot.ledger.db import Database  # noqa: PLC0415

    with Database.open(ledger, RuntimeMode.OBSERVE, auto_migrate=False) as db, db.read() as s:
        events = TradingStateRepository().events(s)
        rebaseline = s.fetch_all("SELECT action, actor FROM rebaseline_events ORDER BY id")
    halts = [e.reason_code for e in events if e.action == "HALT"]
    distinct = [r for i, r in enumerate(halts) if i == 0 or halts[i - 1] != r]
    assert distinct == ["INITIALIZED", "RECON_CASH", "REBASELINE", "BROKER_OUTAGE"]
    for e in events:
        assert e.actor.strip() and e.reason_code.strip()
        if e.action == "RESUME":
            assert e.reconciliation_run_id is not None and e.actor == "operator:audit"
    assert any(e.action == "RESUME_REFUSED" for e in events)
    assert next(e for e in events if e.reason_code == "BROKER_OUTAGE").actor == "outage_breaker"
    assert [r[0] for r in rebaseline] == ["PROPOSED", "APPROVED"]
    assert all(r[1] == "operator:audit" for r in rebaseline)


# ============================================================ broker-check
def test_broker_check_is_ledger_free_and_masks_identity(tmp_path: Path) -> None:
    p = Prod(tmp_path)
    code, env = p.run("broker-check")
    assert code == 0, env
    result = env["result"]
    assert result["environment"] == "paper" and result["identity_problems"] == []
    assert result["account_id"] == "…" + ACCOUNT_ID[-4:] and ACCOUNT_ID not in p.outputs[-1]
    assert "100000" not in p.outputs[-1]  # no balances
    assert result["execution_available"] is False
    assert list(p.data.iterdir()) == []  # no ledger, no lock file
    assert all(m == "GET" for m, _ in p.session.requests)


def test_broker_check_refuses_uncertain_identity_and_missing_credentials(tmp_path: Path) -> None:
    p = Prod(tmp_path)
    p.account.account_id = "PA0000001"
    code, env = p.run("broker-check")
    assert code == 3 and any("UUID" in x for x in env["result"]["identity_problems"])
    for environ in ({}, {**ENV, "STB_ENV_FILE": "/tmp/x.env"}):  # noqa: S108
        q = Prod(tmp_path, environ)
        code, env = q.run("broker-check")
        assert code == 7 and env["result"]["error"] == "CredentialSourceError"
        assert q.session.requests == []  # refused before any request
