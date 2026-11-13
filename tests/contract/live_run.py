"""PYTEST_DONT_REWRITE

The supervised live read-only contract run (Step 15B, prepared in Step 15A.1).

**Do not run without explicit operator authorization.** One process, one shared budget:

1. install the redacting logger (before any credential is touched);
2. load credentials through the explicit loader (environment **or** ``STB_ENV_FILE``);
   register their values with the redactor;
3. ``setup`` phase: the real CLI ``broker-check`` through the budgeted session;
4. ``contract`` phase: every check in :data:`checks.CHECKS`, each sanitized;
5. classify C1-C13, summarize rate-limit evidence, write raw evidence (local, Git-ignored)
   and a sanitized summary.

The run stops — and every later check is ``NOT_RUN`` — on budget or deadline exhaustion,
a write attempt, an authentication failure, persisting rate limiting, or an invalid account
identity. The summary separates *execution* (COMPLETED / STOPPED) from each row's
*verification* outcome.

Usage (authorized operator only)::

    RUN_ALPACA_PAPER_TESTS=1 STB_ENV_FILE=/abs/path uv run python -m tests.contract.live_run
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from swing_trading_bot.app.cli import main as cli_main
from swing_trading_bot.app.commands import production_dependencies
from swing_trading_bot.app.credentials import CredentialSourceError, load_credentials
from swing_trading_bot.broker.alpaca_http import AlpacaReadTransport
from swing_trading_bot.broker.alpaca_read import AlpacaPaperReadAdapter
from swing_trading_bot.observability.logging import (
    SecretRedactor,
    configure_logging,
    register_secrets,
)
from tests.contract.checks import CHECKS, ContractContext
from tests.contract.outcomes import (
    STOP_CODES,
    CheckResult,
    CheckStatus,
    classify_rows,
    run_check,
)
from tests.contract.supervisor import (
    EVIDENCE_ROOT,
    BudgetedSession,
    BudgetExhausted,
    EvidenceError,
    EvidenceWriter,
    RequestBudget,
    WriteAttemptError,
    bounded_sleep,
    forbidden_values,
    rate_limit_evidence,
)

ENABLE_VARIABLE = "RUN_ALPACA_PAPER_TESTS"


def _default_session() -> Any:
    import requests  # noqa: PLC0415

    return requests.Session()


def run_supervised(  # noqa: PLR0915 - injectable dependencies; one linear script
    environ: Mapping[str, str],
    *,
    budget: RequestBudget | None = None,
    session_factory: Callable[[], Any] = _default_session,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    evidence: EvidenceWriter | None = None,
    log_stream: TextIO | None = None,
    log_target: logging.Logger | None = None,
) -> dict[str, Any]:
    """Run the whole supervised contract suite and return the sanitized summary."""
    budget = budget or RequestBudget()
    configure_logging(secrets=(), stream=log_stream or sys.stderr, logger=log_target)
    execution: dict[str, Any] = {"status": "COMPLETED", "stop_code": None}
    results: dict[str, CheckResult] = {name: CheckResult(CheckStatus.NOT_RUN, "NOT_RUN")
                                       for name in CHECKS}  # fmt: skip
    secrets: tuple[str, ...] = ()
    redactor = SecretRedactor()
    session: BudgetedSession | None = None
    setup: dict[str, Any] = {"broker_check_exit": None}

    def stop(code: str) -> None:
        execution.update(status="STOPPED", stop_code=code)

    try:
        loaded = load_credentials(environ)
        secrets = loaded.settings.secret_values()
        register_secrets(secrets)
        redactor.add(secrets)
        execution["credential_source"] = loaded.source
    except CredentialSourceError:
        stop("CREDENTIALS_UNAVAILABLE")
    if execution["status"] == "COMPLETED":
        budget.start()
        session = BudgetedSession(session_factory(), budget, redactor)
        wait = bounded_sleep(budget, *(() if sleep is None else (sleep,)))
        try:
            budget.phase = "setup"
            out = io.StringIO()
            deps = production_dependencies(environ, session_factory=lambda: session,  # type: ignore[arg-type,return-value]
                                           sleep=wait)  # fmt: skip
            code = cli_main(["broker-check", "--json"], deps=deps, stdout=out)
            setup["broker_check_exit"] = code
            if code != 0:
                stop(f"BROKER_CHECK_EXIT_{code}")
            else:
                budget.phase = "contract"
                transport = AlpacaReadTransport(loaded.settings, session=session)  # type: ignore[arg-type]
                adapter = AlpacaPaperReadAdapter(transport, sleep=wait)
                ctx = ContractContext(adapter, transport, now=now())
                for name, check in CHECKS.items():
                    results[name] = run_check(check, ctx)
                    if results[name].code in STOP_CODES:
                        stop(results[name].code)
                        break
        except BudgetExhausted as exc:
            stop(exc.code)
        except WriteAttemptError as exc:
            stop(exc.code)
    exchanges = [] if session is None else list(session.exchanges)
    summary: dict[str, Any] = {
        "synthetic": False,
        "execution": {
            **execution,
            "requests_used": budget.used,
            "requests_by_phase": dict(budget.by_phase),
            "max_requests": budget.max_requests,
            "elapsed_s": round(budget.elapsed(), 1) if session is not None else 0.0,
            "deadline_s": budget.deadline_s,
            "setup": setup,
        },
        "checks": {name: r.to_dict() for name, r in results.items()},
        "rows": classify_rows(results),
        "rate_limits": rate_limit_evidence(exchanges),
        "methods_seen": sorted({e.method for e in exchanges}),
    }
    writer = evidence or EvidenceWriter(EVIDENCE_ROOT)
    try:
        path = writer.write(exchanges, summary, forbidden_values(exchanges, secrets))
        summary["evidence"] = {"written": True, "summary": path.name}
    except AssertionError:
        summary = {"execution": {"status": "STOPPED", "stop_code": "SUMMARY_SANITIZATION_FAILED"}}
    except EvidenceError as exc:
        summary["evidence"] = {"written": False, "code": str(exc)}
    logging.getLogger(__name__).info("contract run finished",
                                     extra={"status": summary["execution"]["status"]})  # fmt: skip
    return summary


def render(summary: Mapping[str, Any], secrets: tuple[str, ...] = ()) -> str:
    """Operator-facing text: execution status and the row table only."""
    lines = [f"execution: {summary['execution']['status']} "
             f"(stop: {summary['execution'].get('stop_code')})"]  # fmt: skip
    for row, value in summary.get("rows", {}).items():
        lines.append(f"{row:4s} {value['outcome']:13s} {value['code']}")
    return SecretRedactor(secrets).redact("\n".join(lines))


def fail_if_not_ok(summary: Mapping[str, Any]) -> None:
    """The pytest verdict: fixed codes only, no traceback (``pytrace=False``)."""
    import pytest  # noqa: PLC0415

    status = summary["execution"]["status"]
    failed = sorted(r for r, v in summary.get("rows", {}).items() if v["outcome"] == "FAILED")
    if status != "COMPLETED" or failed:
        pytest.fail(f"contract run {status}; failed rows: {failed}\n{render(summary)}",
                    pytrace=False)  # fmt: skip


def main() -> int:  # pragma: no cover - live entry point
    if os.environ.get(ENABLE_VARIABLE) != "1":
        sys.stdout.write(f"refused: set {ENABLE_VARIABLE}=1 (authorized operator run only)\n")
        return 2
    summary = run_supervised(os.environ)
    sys.stdout.write(render(summary) + "\n")
    sys.stdout.write(json.dumps({"evidence": summary.get("evidence")}) + "\n")
    ok = summary["execution"]["status"] == "COMPLETED"
    failed = any(v["outcome"] == "FAILED" for v in summary.get("rows", {}).values())
    return 0 if ok and not failed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["Path", "main", "render", "run_supervised"]
