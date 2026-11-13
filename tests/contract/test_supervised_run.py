"""Step 15A.1: the supervised live-run harness, exercised fully OFFLINE.

Synthetic sentinel credentials, a fake monotonic clock, and the synthetic account served
through the real HTTPS GET transport and the real CLI ``broker-check``."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from swing_trading_bot.broker.errors import AuthenticationError, ServiceUnavailableError
from tests.contract import checks as contract_checks
from tests.contract.live_run import fail_if_not_ok, render, run_supervised
from tests.contract.supervisor import (
    EVIDENCE_ROOT,
    REPO,
    BudgetedSession,
    BudgetExhausted,
    EvidenceError,
    EvidenceWriter,
    Exchange,
    RequestBudget,
    _git_ignored,
    bounded_sleep,
    rate_limit_evidence,
)
from tests.contract.test_contract_replay import synthetic_account
from tests.support.alpaca_account import ACCOUNT_ID, AccountSession

KEY = "PKSENTINELKEY4242424242"
SECRET = "sentinel/Secret/Value/4242424242424242"
ENV = {"ALPACA_API_KEY": KEY, "ALPACA_SECRET_KEY": SECRET, "TRADING_MODE": "paper"}
GOOD_HEADERS = {"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "190",
                "X-RateLimit-Reset": "1795000000"}  # fmt: skip


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class HarnessSession(AccountSession):
    """The synthetic account plus scripted latency, headers and echoing error bodies."""

    def __init__(self, clock: FakeTime, *, cost_s: float = 0.01,
                 headers: dict[str, str] | None = None) -> None:  # fmt: skip
        super().__init__(synthetic_account())
        self.clock = clock
        self.cost_s = cost_s
        self.headers = GOOD_HEADERS if headers is None else headers
        self.timeouts: list[Any] = []
        self.echo_paths: set[str] = set()

    def get(self, url: str, **kw: Any) -> Any:
        self.timeouts.append(kw.get("timeout"))
        self.clock.t += self.cost_s
        if any(url.endswith(p) for p in self.echo_paths):
            r = requests.Response()
            r.url, r.status_code = url, 500
            r._content = f'{{"message":"key {KEY} secret {SECRET} acct {ACCOUNT_ID}"}}'.encode()
            return r
        response = super().get(url, **kw)
        response.headers.update(self.headers)
        return response


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    target = logging.getLogger("swing_trading_bot")
    saved = (list(target.handlers), target.propagate, target.level)
    stream = io.StringIO()
    yield stream
    target.handlers[:] = saved[0]
    target.propagate, target.level = saved[1], saved[2]


def run(tmp_path: Path, logs: io.StringIO, session: HarnessSession, clock: FakeTime,
        **budget: Any) -> dict[str, Any]:  # fmt: skip
    return run_supervised(
        ENV,
        budget=RequestBudget(monotonic=clock, **budget),
        session_factory=lambda: session,
        sleep=clock.sleep,
        now=lambda: session.account.clock.now,
        evidence=EvidenceWriter(tmp_path / "evidence", run="run", require_git_ignored=False),
        log_stream=logs,
        log_target=logging.getLogger("swing_trading_bot"),
    )


def leaks(text: str) -> list[str]:
    return [v for v in (KEY, SECRET, ACCOUNT_ID, "100000.00", "PA0000") if v in text]


# ============================================================ complete run
def test_offline_supervised_run_classifies_every_row(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    summary = run(tmp_path, logs, session, clock)
    ex = summary["execution"]
    assert ex["status"] == "COMPLETED" and ex["stop_code"] is None
    assert ex["requests_by_phase"]["setup"] >= 3 and ex["requests_by_phase"]["contract"] > 0
    assert ex["requests_used"] == sum(ex["requests_by_phase"].values()) <= 120
    assert summary["methods_seen"] == ["GET"]
    rows = {r: v["outcome"] for r, v in summary["rows"].items()}
    assert rows == {
        "C1": "VERIFIED", "C2": "VERIFIED", "C3": "VERIFIED", "C4": "VERIFIED",
        "C5": "INCONCLUSIVE", "C6": "VERIFIED", "C7": "VERIFIED", "C8": "VERIFIED",
        "C9": "INCONCLUSIVE", "C10": "DEFERRED", "C11": "DEFERRED", "C12": "DEFERRED",
        "C13": "DEFERRED",
    }  # fmt: skip
    assert summary["rows"]["C5"]["code"] == "FLAT_ACCOUNT_ORDER_SEMANTICS_UNEXERCISED"
    rl = summary["rate_limits"]["paper-api.alpaca.markets"]
    assert rl["status"] == "OBSERVED" and rl["limit"] == 200
    text = (tmp_path / "evidence" / "run" / "summary.json").read_text()
    assert leaks(text) == [] and leaks(logs.getvalue()) == [] and leaks(render(summary)) == []


def test_evidence_permissions(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    run(tmp_path, logs, HarnessSession(clock), clock)
    root = tmp_path / "evidence"
    for d in (root, root / "run", root / "run" / "raw"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    files = [root / "run" / "summary.json", *(root / "run" / "raw").iterdir()]
    assert len(files) > 5
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
    for f in files:  # raw bodies are local-only but still never carry the secrets
        assert KEY not in f.read_text() and SECRET not in f.read_text()


# ============================================================ budgets (R1)
def test_budget_exceeded_during_retries_stops_everything(tmp_path: Path, logs: io.StringIO
                                                          ) -> None:  # fmt: skip
    clock = FakeTime()
    session = HarnessSession(clock)
    session.account.down["/account/configurations"] = lambda: ServiceUnavailableError(
        "503", status_code=503)  # fmt: skip
    summary = run(tmp_path, logs, session, clock, max_requests=3)
    ex = summary["execution"]
    assert (ex["status"], ex["stop_code"]) == ("STOPPED", "REQUEST_BUDGET_EXCEEDED")
    assert ex["requests_used"] == 3 and len(session.requests) == 3  # the retry hit the cap
    assert all(v["status"] == "NOT_RUN" for v in summary["checks"].values())
    testable = {f"C{i}" for i in range(1, 10)}
    assert {v["outcome"] for r, v in summary["rows"].items() if r in testable} == {"INCONCLUSIVE"}
    assert (tmp_path / "evidence" / "run" / "summary.json").exists()  # partial summary


def test_setup_requests_consume_the_shared_budget(tmp_path: Path, logs: io.StringIO) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    clock = FakeTime()
    first = HarnessSession(clock)
    full = run(tmp_path / "a", logs, first, clock)
    setup = full["execution"]["requests_by_phase"]["setup"]
    clock2 = FakeTime()
    second = HarnessSession(clock2)
    summary = run(tmp_path / "b", logs, second, clock2, max_requests=setup + 2)
    ex = summary["execution"]
    assert ex["stop_code"] == "REQUEST_BUDGET_EXCEEDED"
    assert ex["requests_by_phase"] == {"setup": setup, "contract": 2}
    assert summary["checks"]["account_identity"]["status"] == "PASSED"
    assert summary["checks"]["market_data"]["status"] == "NOT_RUN"


def test_deadline_exceeded_during_backoff(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    session.account.down["/account"] = lambda: ServiceUnavailableError("503", status_code=503)
    summary = run(tmp_path, logs, session, clock, deadline_s=0.4)
    assert summary["execution"]["stop_code"] == "DEADLINE_EXCEEDED"
    assert clock.t < 0.4  # it refused to sleep past the deadline


def test_deadline_exceeded_during_a_request(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    session = HarnessSession(clock, cost_s=50.0)
    summary = run(tmp_path, logs, session, clock, deadline_s=120)
    assert summary["execution"]["stop_code"] == "DEADLINE_EXCEEDED"
    assert len(session.requests) == 3  # 3 x 50 s: the third crossed the deadline
    assert all(max(t) <= 120 for t in session.timeouts)
    assert session.timeouts[-1] == (5.0, 20.0)  # never longer than the transport's own


def test_timeouts_are_cut_to_the_remaining_deadline() -> None:
    clock = FakeTime()
    inner = HarnessSession(clock)
    budget = RequestBudget(monotonic=clock, deadline_s=3)
    spy = BudgetedSession(inner, budget)
    spy.get("https://paper-api.alpaca.markets/v2/clock", params={}, headers={},
            timeout=(5, 20), allow_redirects=False)  # fmt: skip
    assert inner.timeouts[0] == (3.0, 3.0)
    wait = bounded_sleep(budget, clock.sleep)
    with pytest.raises(BudgetExhausted, match="DEADLINE_EXCEEDED"):
        wait(__import__("datetime").timedelta(seconds=10))
    with pytest.raises(BudgetExhausted):  # once exhausted, nothing more is sent
        spy.get("https://paper-api.alpaca.markets/v2/clock", params={}, headers={},
                timeout=(5, 20), allow_redirects=False)  # fmt: skip
    assert budget.used == 1


# ============================================================ stops and failures
def test_auth_failure_in_setup_prevents_contract_requests(tmp_path: Path, logs: io.StringIO
                                                          ) -> None:  # fmt: skip
    clock = FakeTime()
    session = HarnessSession(clock)
    session.account.down["/account"] = lambda: AuthenticationError("401", status_code=401)
    summary = run(tmp_path, logs, session, clock)
    assert summary["execution"]["stop_code"] == "BROKER_CHECK_EXIT_8"
    assert "contract" not in summary["execution"]["requests_by_phase"]


def test_critical_failure_mid_run_stops_later_checks(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    sent = []

    def auth_on_calendar(_p: Any) -> object:
        sent.append(1)
        raise AuthenticationError("401", status_code=401)

    session.account.transport.always("trading", "/calendar", auth_on_calendar)
    summary = run(tmp_path, logs, session, clock)
    assert summary["execution"]["stop_code"] == "BROKER_AUTH_FAILED"
    assert summary["checks"]["clock"]["code"] == "BROKER_AUTH_FAILED"
    assert summary["checks"]["calendars"]["status"] == "NOT_RUN"
    assert sent == [1]  # auth is never retried and nothing followed it


def test_identity_violation_is_failed_and_stops(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    session.account.account_overrides["status"] = "ACCOUNT_CLOSED"
    summary = run(tmp_path, logs, session, clock)
    assert summary["execution"]["stop_code"] == "BROKER_CHECK_EXIT_3"  # setup refused first


def test_echoed_error_bodies_are_sanitized(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    session.echo_paths = {"/stocks/trades/latest"}
    summary = run(tmp_path, logs, session, clock)
    assert summary["checks"]["market_data"]["code"] == "BROKER_UNAVAILABLE_HTTP"
    assert summary["rows"]["C8"]["outcome"] == "INCONCLUSIVE"
    corpus = json.dumps(summary) + logs.getvalue() + render(summary)
    corpus += "".join(p.read_text() for p in (tmp_path / "evidence").rglob("*.json"))
    assert KEY not in corpus and SECRET not in corpus
    assert ACCOUNT_ID not in json.dumps(summary) + logs.getvalue()


def test_unexpected_harness_exceptions_carry_no_data(
    tmp_path: Path, logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_c: Any) -> dict[str, Any]:
        raise ValueError(f"{SECRET} {ACCOUNT_ID}")

    monkeypatch.setitem(contract_checks.CHECKS, "clock", boom)
    clock = FakeTime()
    summary = run(tmp_path, logs, HarnessSession(clock), clock)
    expected = {"status": "HARNESS_ERROR", "code": "HARNESS_VALUEERROR", "observation": {}}
    assert summary["checks"]["clock"] == expected
    assert summary["rows"]["C3"]["outcome"] == "INCONCLUSIVE"
    assert leaks(json.dumps(summary)) == []


# ============================================================ rate-limit evidence (R2)
def ex(headers: dict[str, str], status: int = 200) -> Exchange:
    return Exchange("GET", "paper-api.alpaca.markets", "/v2/x", status, headers, "")


@pytest.mark.parametrize(
    ("items", "status", "reason"),
    [
        ([ex(GOOD_HEADERS)] * 3, "OBSERVED", None),
        ([ex(GOOD_HEADERS), ex({})], "UNVERIFIED", "HEADERS_MISSING"),
        ([ex({**GOOD_HEADERS, "X-RateLimit-Limit": "lots"})], "UNVERIFIED", "HEADERS_MALFORMED"),
        ([ex({**GOOD_HEADERS, "X-RateLimit-Remaining": "999"})], "UNVERIFIED",
         "HEADERS_MALFORMED"),
        ([ex(GOOD_HEADERS), ex({**GOOD_HEADERS, "X-RateLimit-Limit": "100",
                                 "X-RateLimit-Remaining": "50"})], "UNVERIFIED",
         "LIMIT_INCONSISTENT"),
        ([], None, None),
    ],
)  # fmt: skip
def test_rate_limit_evidence(items: list[Exchange], status: str | None, reason: str | None
                             ) -> None:  # fmt: skip
    out = rate_limit_evidence(items)
    if status is None:
        assert out == {}  # no responses: nothing claimed, no limit assumed
        return
    host = out["paper-api.alpaca.markets"]
    assert (host["status"], host["reason"]) == (status, reason)
    assert host["limit"] == (200 if status == "OBSERVED" else None)


def test_missing_headers_in_a_run_stay_unverified(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    summary = run(tmp_path, logs, HarnessSession(clock, headers={}), clock)
    rl = summary["rate_limits"]["paper-api.alpaca.markets"]
    assert rl["status"] == "UNVERIFIED" and rl["reason"] == "HEADERS_MISSING"
    assert rl["limit"] is None


def test_retry_after_and_429_are_counted() -> None:
    out = rate_limit_evidence([ex({**GOOD_HEADERS, "Retry-After": "3"}, 429)])
    host = out["paper-api.alpaca.markets"]
    assert host["retry_after_seen"] == 1 and host["http_429_seen"] == 1


# ============================================================ evidence paths (R5)
def test_evidence_refuses_symlinks_existing_and_unignored_paths(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(EvidenceError, match="NOT_A_DIRECTORY"):
        EvidenceWriter(link, run="r", require_git_ignored=False).write([], {}, [])
    (real / "r").mkdir()
    with pytest.raises(EvidenceError, match="DESTINATION_EXISTS"):
        EvidenceWriter(real, run="r", require_git_ignored=False).write([], {}, [])
    with pytest.raises(EvidenceError, match="INVALID"):
        EvidenceWriter(real, run="../escape", require_git_ignored=False).write([], {}, [])
    with pytest.raises(EvidenceError, match="NOT_GIT_IGNORED"):
        EvidenceWriter(REPO / "tests" / "not-ignored-evidence", run="r").write([], {}, [])
    assert not (REPO / "tests" / "not-ignored-evidence").exists()


def test_default_evidence_root_is_git_ignored() -> None:
    assert _git_ignored(EVIDENCE_ROOT / "probe")


# ============================================================ pytest output (R3, R6)
def test_pytest_failure_output_is_sanitized(tmp_path: Path) -> None:
    module = tmp_path / "test_live_failure.py"
    module.write_text(
        textwrap.dedent(f"""
        import logging
        from pathlib import Path
        from tests.contract import checks
        from tests.contract.live_run import fail_if_not_ok, run_supervised
        from tests.contract.supervisor import EvidenceWriter, RequestBudget
        from tests.contract.test_supervised_run import ENV, FakeTime, HarnessSession

        def test_live(monkeypatch, tmp_path):
            def boom(_c):
                assert {SECRET!r} == {ACCOUNT_ID!r}, {KEY!r}
            monkeypatch.setitem(checks.CHECKS, "clock", boom)
            clock = FakeTime()
            session = HarnessSession(clock)
            session.echo_paths = {{"/stocks/trades/latest"}}
            summary = run_supervised(ENV, budget=RequestBudget(monotonic=clock),
                session_factory=lambda: session, sleep=clock.sleep,
                now=lambda: session.account.clock.now,
                evidence=EvidenceWriter(tmp_path / "ev", run="r", require_git_ignored=False),
                log_target=logging.getLogger("swing_trading_bot"))
            summary["rows"]["C3"]["outcome"] = "FAILED"
            fail_if_not_ok(summary)
    """)
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ALPACA_", "APCA_"))}
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(module), "-p", "no:cacheprovider", "-q",
         "--rootdir", str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "failed rows: ['C3']" in output
    assert leaks(output) == [], leaks(output)


def test_fail_if_not_ok_passes_a_clean_summary() -> None:
    fail_if_not_ok({"execution": {"status": "COMPLETED"}, "evidence": {"written": True},
                    "rows": {"C1": {"outcome": "VERIFIED"}}})  # fmt: skip


# ============================================================ Step 15B.2 account schema
def test_account_without_pdt_completes_and_verifies_identity(
    tmp_path: Path, logs: io.StringIO
) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    assert "pattern_day_trader" not in session.account._account({})  # the observed shape
    summary = run(tmp_path, logs, session, clock)
    assert summary["execution"]["status"] == "COMPLETED"
    assert summary["execution"]["setup"]["broker_check_exit"] == 0
    assert summary["rows"]["C1"] == {"outcome": "VERIFIED", "code": "OK"}


def test_account_payload_violation_in_setup_stops_with_exit_10(
    tmp_path: Path, logs: io.StringIO
) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    session.account.account_overrides["pattern_day_trader"] = "false"
    summary = run(tmp_path, logs, session, clock)
    assert summary["execution"]["status"] == "STOPPED"
    assert summary["execution"]["stop_code"] == "BROKER_CHECK_EXIT_10"
    assert summary["execution"]["requests_by_phase"] == {"setup": 1}  # no retry, no checks
    assert all(v["code"] == "NOT_RUN" for r, v in summary["rows"].items()
               if r not in {"C10", "C11", "C12", "C13"})  # fmt: skip


def test_observed_config_shape_completes_setup_and_verifies_c2(
    tmp_path: Path, logs: io.StringIO
) -> None:
    from tests.support.alpaca_fake import CONFIG  # noqa: PLC0415

    assert "pdt_check" not in CONFIG and "dtbp_check" not in CONFIG  # Step 15B run #2 shape
    clock = FakeTime()
    session = HarnessSession(clock)
    summary = run(tmp_path, logs, session, clock)
    assert summary["execution"]["status"] == "COMPLETED"
    assert summary["execution"]["setup"]["broker_check_exit"] == 0
    assert summary["rows"]["C2"] == {"outcome": "VERIFIED", "code": "OK"}


def test_malformed_config_in_setup_stops_with_exit_10(tmp_path: Path, logs: io.StringIO) -> None:
    clock = FakeTime()
    session = HarnessSession(clock)
    session.account.config_overrides["pdt_check"] = ""
    summary = run(tmp_path, logs, session, clock)
    assert summary["execution"]["stop_code"] == "BROKER_CHECK_EXIT_10"
    assert summary["execution"]["requests_by_phase"] == {"setup": 2}
