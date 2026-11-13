"""PYTEST_DONT_REWRITE

The supervised live read-only contract run (Step 15B, prepared in Step 15A.1).

**Do not run without explicit operator authorization.** One process, one shared budget:

1. install the redacting logger (before any credential is touched);
2. load credentials through the explicit loader (environment **or** ``STB_ENV_FILE``);
   register their values with the redactor;
3. ``setup`` phase: the real CLI ``broker-check`` through the budgeted session;
4. ``contract`` phase: every check in :data:`checks.CHECKS`, each sanitized;
5. classify C1-C13, summarize rate-limit evidence, write raw evidence (local, Git-ignored)
   and a sanitized summary. Raw bodies are structurally sanitized (Step 15B.5C); if any
   evidence cannot be proven sanitized, nothing is written and the summary is reduced to
   ``STOPPED`` / ``SUMMARY_SANITIZATION_FAILED`` with a fixed reason code (no rows, so
   nothing can be VERIFIED). Evidence is published atomically (Step 15B.5D); a filesystem
   failure is ``STOPPED`` / ``EVIDENCE_WRITE_FAILED``, likewise without rows.
6. the final **run verdict** (:func:`run_verdict`) is separate from the individual C1-C13
   rows: it passes only if execution COMPLETED, the evidence was published
   (``evidence.written`` is true) and no row FAILED. A refusal before writing (e.g.
   ``EVIDENCE_DESTINATION_EXISTS``) keeps the rows for diagnosis but fails the run.

The run stops — and every later check is ``NOT_RUN`` — on budget or deadline exhaustion,
a write attempt, an authentication failure, persisting rate limiting, or an invalid account
identity. Before any request it stops with a fixed code when no credential source is
selected (``CREDENTIALS_UNAVAILABLE``) or the selected settings are invalid
(``CONFIGURATION_INVALID``, Step 15B.6A — e.g. ``TRADING_MODE`` missing or not ``paper``);
the exception message is discarded, no session is created and the budget never starts.
The summary separates *execution* (COMPLETED / STOPPED) from each row's
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
from swing_trading_bot.config.settings import SettingsError
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
    EvidenceWriteError,
    EvidenceWriter,
    RequestBudget,
    SanitizationError,
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
    session: BudgetedSession | None = None
    setup: dict[str, Any] = {"broker_check_exit": None}

    def stop(code: str) -> None:
        execution.update(status="STOPPED", stop_code=code)

    try:
        loaded = load_credentials(environ)
        secrets = loaded.settings.secret_values()
        register_secrets(secrets)
        execution["credential_source"] = loaded.source
    except CredentialSourceError:  # no/ambiguous/unsafe source (a SettingsError subclass)
        stop("CREDENTIALS_UNAVAILABLE")
    except SettingsError:  # Step 15B.6A: invalid or missing setting; message discarded
        stop("CONFIGURATION_INVALID")
    if execution["status"] == "COMPLETED":
        budget.start()
        session = BudgetedSession(session_factory(), budget)
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
        forbidden = forbidden_values(exchanges, secrets)
        path = writer.write(exchanges, summary, forbidden, secrets=secrets)
        summary["evidence"] = {"written": True, "summary": path.name}
    except SanitizationError as exc:  # fail closed: no rows, nothing written
        summary = {"execution": {"status": "STOPPED", "stop_code": "SUMMARY_SANITIZATION_FAILED"},
                   "evidence": {"written": False, "code": exc.code}}  # fmt: skip
    except EvidenceWriteError as exc:  # fail closed: no rows; never "written"
        summary = {"execution": {"status": "STOPPED", "stop_code": exc.code},
                   "evidence": {"written": False, "code": exc.code,
                                "staging_removed": exc.staging_removed}}  # fmt: skip
    except EvidenceError as exc:  # a refusal before any write (unchanged)
        summary["evidence"] = {"written": False, "code": str(exc)}
    passed, verdict_code = run_verdict(summary)
    summary["verdict"] = {"status": "PASSED" if passed else "FAILED", "code": verdict_code}
    logging.getLogger(__name__).info("contract run finished",
                                     extra={"status": summary["execution"]["status"],
                                            "verdict": summary["verdict"]["status"]})  # fmt: skip
    return summary


def run_verdict(summary: Mapping[str, Any]) -> tuple[bool, str]:
    """The final supervised-run verdict, recomputed from the summary (never trusted from a
    stored field): ``(passed, fixed code)``. Precedence: a stopped execution (its stop
    code), unpublished evidence (the evidence code — a refusal or a write failure), a FAILED
    contract row (``CONTRACT_ROWS_FAILED``); otherwise ``OK``."""
    execution = summary.get("execution") or {}
    if execution.get("status") != "COMPLETED":
        return False, str(execution.get("stop_code") or "EXECUTION_NOT_COMPLETED")
    evidence = summary.get("evidence") or {}
    if evidence.get("written") is not True:
        return False, str(evidence.get("code") or "EVIDENCE_NOT_WRITTEN")
    if any(v.get("outcome") == "FAILED" for v in (summary.get("rows") or {}).values()):
        return False, "CONTRACT_ROWS_FAILED"
    return True, "OK"


def render(summary: Mapping[str, Any], secrets: tuple[str, ...] = ()) -> str:
    """Operator-facing text: the run verdict, execution status and the row table only."""
    passed, code = run_verdict(summary)
    evidence = summary.get("evidence") or {}
    lines = [f"run verdict: {'PASSED' if passed else 'FAILED'} ({code})",
             f"execution: {summary['execution']['status']} "
             f"(stop: {summary['execution'].get('stop_code')})",
             f"evidence written: {evidence.get('written') is True}"]  # fmt: skip
    if summary.get("rows"):
        lines.append("contract rows (individual results, not the run verdict):")
    for row, value in summary.get("rows", {}).items():
        lines.append(f"{row:4s} {value['outcome']:13s} {value['code']}")
    return SecretRedactor(secrets).redact("\n".join(lines))


def fail_if_not_ok(summary: Mapping[str, Any]) -> None:
    """The pytest verdict: fixed codes only, no traceback (``pytrace=False``)."""
    import pytest  # noqa: PLC0415

    passed, code = run_verdict(summary)
    if not passed:
        failed = sorted(r for r, v in summary.get("rows", {}).items() if v["outcome"] == "FAILED")
        pytest.fail(f"contract run FAILED ({code}); failed rows: {failed}\n{render(summary)}",
                    pytrace=False)  # fmt: skip


def main() -> int:  # pragma: no cover - live entry point
    if os.environ.get(ENABLE_VARIABLE) != "1":
        sys.stdout.write(f"refused: set {ENABLE_VARIABLE}=1 (authorized operator run only)\n")
        return 2
    summary = run_supervised(os.environ)
    sys.stdout.write(render(summary) + "\n")
    sys.stdout.write(json.dumps({"evidence": summary.get("evidence")}) + "\n")
    return 0 if run_verdict(summary)[0] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["Path", "main", "render", "run_supervised", "run_verdict"]
