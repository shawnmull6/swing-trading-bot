"""Step 15B.5D (review): the final supervised-run verdict requires published evidence —
OFFLINE, synthetic data only.

Individual C1-C13 rows are contract results; the **run verdict** (``run_verdict``) is
separate and passes only if execution COMPLETED, evidence was published
(``evidence.written`` is true) and no row FAILED. A refusal before writing keeps its
diagnostic code and the rows, but fails the run (pytest and CLI).
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.contract import live_run
from tests.contract.live_run import fail_if_not_ok, render, run_supervised, run_verdict
from tests.contract.supervisor import EvidenceWriter, FileOps, RequestBudget
from tests.contract.test_supervised_run import ENV, KEY, SECRET, FakeTime, HarnessSession
from tests.support.alpaca_account import ACCOUNT_ID

IDENTITIES = (KEY, SECRET, ACCOUNT_ID, "PA0000")


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    target = logging.getLogger("swing_trading_bot")
    saved = (list(target.handlers), target.propagate, target.level)
    stream = io.StringIO()
    yield stream
    target.handlers[:] = saved[0]
    target.propagate, target.level = saved[1], saved[2]


def supervised(logs: io.StringIO, evidence: EvidenceWriter) -> dict[str, Any]:
    clock = FakeTime()
    session = HarnessSession(clock)
    return run_supervised(
        ENV, budget=RequestBudget(monotonic=clock), session_factory=lambda: session,
        sleep=clock.sleep, now=lambda: session.account.clock.now, evidence=evidence,
        log_stream=logs, log_target=logging.getLogger("swing_trading_bot"),
    )  # fmt: skip


def assert_refused(summary: dict[str, Any], code: str, logs: io.StringIO) -> None:
    assert summary["execution"]["status"] == "COMPLETED"  # the checks themselves ran
    assert summary["evidence"] == {"written": False, "code": code}  # diagnosable refusal
    assert summary["verdict"] == {"status": "FAILED", "code": code}
    assert run_verdict(summary) == (False, code)
    assert summary["rows"]["C1"]["outcome"] == "VERIFIED"  # individual result kept ...
    text = render(summary)
    assert text.startswith(f"run verdict: FAILED ({code})")  # ... but not the run verdict
    assert "evidence written: False" in text
    assert "individual results, not the run verdict" in text
    with pytest.raises(pytest.fail.Exception, match=f"contract run FAILED \\({code}\\)"):
        fail_if_not_ok(summary)
    corpus = json.dumps(summary) + logs.getvalue() + text
    assert all(v not in corpus for v in IDENTITIES)


def ev(root: Path, **kw: Any) -> EvidenceWriter:
    return EvidenceWriter(root, require_git_ignored=kw.pop("require_git_ignored", False), **kw)


# ============================================================ refusals fail the run
def test_existing_evidence_destination(tmp_path: Path, logs: io.StringIO) -> None:
    (tmp_path / "ev" / "run").mkdir(parents=True)
    (tmp_path / "ev" / "run" / "summary.json").write_text("earlier run")
    summary = supervised(logs, ev(tmp_path / "ev", run="run"))
    assert_refused(summary, "EVIDENCE_DESTINATION_EXISTS", logs)
    assert (tmp_path / "ev" / "run" / "summary.json").read_text() == "earlier run"
    assert sorted(p.name for p in (tmp_path / "ev").iterdir()) == ["run"]


def test_empty_existing_destination_is_refused_too(tmp_path: Path, logs: io.StringIO) -> None:
    (tmp_path / "ev" / "run").mkdir(parents=True)
    summary = supervised(logs, ev(tmp_path / "ev", run="run"))
    assert_refused(summary, "EVIDENCE_DESTINATION_EXISTS", logs)
    assert list((tmp_path / "ev" / "run").iterdir()) == []


@pytest.mark.parametrize("kind", ["relative", "file", "symlink"])
def test_invalid_evidence_root(tmp_path: Path, logs: io.StringIO, kind: str) -> None:
    if kind == "relative":
        root, code = Path("relative-evidence"), "EVIDENCE_PATH_INVALID"
    elif kind == "file":
        root, code = tmp_path / "ev", "EVIDENCE_ROOT_NOT_A_DIRECTORY"
        root.write_text("not a directory")
    else:
        (tmp_path / "real").mkdir()
        root, code = tmp_path / "ev", "EVIDENCE_ROOT_NOT_A_DIRECTORY"
        root.symlink_to(tmp_path / "real")
    summary = supervised(logs, ev(root, run="run"))
    assert_refused(summary, code, logs)
    assert not Path("relative-evidence").exists() and not (tmp_path / "real" / "run").exists()


def test_root_not_git_ignored(tmp_path: Path, logs: io.StringIO) -> None:
    summary = supervised(logs, ev(tmp_path / "ev", run="run", require_git_ignored=True))
    assert_refused(summary, "EVIDENCE_ROOT_NOT_GIT_IGNORED", logs)
    assert not (tmp_path / "ev").exists()


@pytest.mark.parametrize("run", [".hidden", ".incomplete-run-0123", "a/b", "..", "."])
def test_invalid_run_name(tmp_path: Path, logs: io.StringIO, run: str) -> None:
    summary = supervised(logs, ev(tmp_path / "ev", run=run))
    assert_refused(summary, "EVIDENCE_PATH_INVALID", logs)
    assert not (tmp_path / "ev").exists()


def test_destination_collision_at_publication(tmp_path: Path, logs: io.StringIO) -> None:
    """The run name appears (non-empty) between the pre-check and the rename: a
    collision, reported as such — not as a filesystem write failure."""

    class Racer(FileOps):
        def rename(self, src: Path, dst: Path) -> None:
            os.mkdir(dst, 0o700)
            (dst / "summary.json").write_text("other run")
            super().rename(src, dst)

    summary = supervised(logs, ev(tmp_path / "ev", run="run", fs=Racer()))
    assert_refused(summary, "EVIDENCE_DESTINATION_EXISTS", logs)
    assert (tmp_path / "ev" / "run" / "summary.json").read_text() == "other run"
    assert sorted(p.name for p in (tmp_path / "ev").iterdir()) == ["run"]  # staging removed


def test_an_empty_destination_created_during_publication_is_not_replaced(
    tmp_path: Path, logs: io.StringIO
) -> None:
    """The former open race (A§12.30 ¶7): an EMPTY ``<run>`` directory created between the
    pre-check and publication. The native no-replace rename refuses it atomically."""

    class EmptyRacer(FileOps):
        def rename(self, src: Path, dst: Path) -> None:
            os.mkdir(dst, 0o700)
            super().rename(src, dst)

    summary = supervised(logs, ev(tmp_path / "ev", run="run", fs=EmptyRacer()))
    assert_refused(summary, "EVIDENCE_DESTINATION_EXISTS", logs)
    assert list((tmp_path / "ev" / "run").iterdir()) == []  # left exactly as it was
    assert sorted(p.name for p in (tmp_path / "ev").iterdir()) == ["run"]  # staging removed


# ============================================================ entry points
VERIFIED_ROW = {"C1": {"outcome": "VERIFIED", "code": "OK"}}
REFUSED = {"written": False, "code": "EVIDENCE_DESTINATION_EXISTS"}


@pytest.mark.parametrize(
    "summary",
    [
        {"execution": {"status": "COMPLETED"}, "evidence": REFUSED, "rows": VERIFIED_ROW},
        {"execution": {"status": "COMPLETED"}, "rows": VERIFIED_ROW},
        {"execution": {"status": "COMPLETED"}, "evidence": {"written": "yes"}, "rows": {}},
        {"execution": {"status": "STOPPED", "stop_code": "EVIDENCE_WRITE_FAILED"}},
        {"execution": {"status": "COMPLETED"}, "evidence": {"written": True},
         "rows": {"C4": {"outcome": "FAILED", "code": "X"}}},
    ],
)  # fmt: skip
def test_no_unpublished_or_failed_run_passes_pytest_or_cli(
    summary: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_verdict(summary)[0] is False
    with pytest.raises(pytest.fail.Exception):
        fail_if_not_ok(summary)
    monkeypatch.setenv(live_run.ENABLE_VARIABLE, "1")
    monkeypatch.setattr(live_run, "run_supervised", lambda _env: summary)
    assert live_run.main() == 1
    assert "run verdict: FAILED" in capsys.readouterr().out


def test_published_successful_run_passes(tmp_path: Path, logs: io.StringIO,
                                         monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    summary = supervised(logs, ev(tmp_path / "ev", run="run"))
    assert summary["evidence"]["written"] is True
    assert summary["verdict"] == {"status": "PASSED", "code": "OK"}
    fail_if_not_ok(summary)
    monkeypatch.setenv(live_run.ENABLE_VARIABLE, "1")
    monkeypatch.setattr(live_run, "run_supervised", lambda _env: summary)
    assert live_run.main() == 0


def test_verdict_codes_and_precedence() -> None:
    stopped = {"execution": {"status": "STOPPED", "stop_code": "BUDGET"}, "evidence": {}}
    assert run_verdict(stopped) == (False, "BUDGET")
    assert run_verdict({"execution": {"status": "STOPPED"}}) == (False, "EXECUTION_NOT_COMPLETED")
    unwritten = {"execution": {"status": "COMPLETED"}, "evidence": {"written": False},
                 "rows": {"C4": {"outcome": "FAILED", "code": "X"}}}  # fmt: skip
    assert run_verdict(unwritten) == (False, "EVIDENCE_NOT_WRITTEN")  # evidence first
    ok = {"execution": {"status": "COMPLETED"}, "evidence": {"written": True},
          "rows": {"C5": {"outcome": "INCONCLUSIVE"}, "C10": {"outcome": "DEFERRED"}}}  # fmt: skip
    assert run_verdict(ok) == (True, "OK")
    forged = {**unwritten, "verdict": {"status": "PASSED", "code": "OK"}}
    assert run_verdict(forged)[0] is False  # recomputed, never trusted from the summary
