"""The acquisition store's sealed session limit (Step 16.17C.3).

``--new-store`` seals ``max_sessions`` into the write-once ``store.json``
(``session_limit``, covered by ``store_sha256``); ``--resume`` must name exactly that
limit; every started session counts, interrupted ones included, and recovery never
resets the count. Everything here is SYNTHETIC: temporary stores, the mocked HTTP of the
runner tests, a placeholder ledger name that is never opened, no credential and no
network (refusals use ``never_connect``, which fails if credentials would be read)."""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.history_acquisition import runner as runner_mod
from swing_trading_bot.history_acquisition.runner import (
    Authorization,
    ResearchPaths,
    RunnerRefusedError,
    recover,
    status,
)
from swing_trading_bot.history_acquisition.supervisor import (
    PRE_SESSION_REFUSALS,
    SessionArgs,
    run_child,
    summarize,
)
from swing_trading_bot.research.dataset import canonical, sha256
from swing_trading_bot.research.staging import (
    STORE_FILE,
    StagingError,
    StagingIntegrityError,
    StagingStore,
)
from tests.unit import test_history_acquisition_runner as rt

CHILD = Path(__file__).resolve().parents[1] / "support" / "acquisition_child.py"
NEW_CODES = ("SESSION_LIMIT_MISSING", "SESSION_LIMIT_MISMATCH",
             "JOURNAL_SESSION_LIMIT_MISMATCH", "SESSION_LIMIT_REACHED")  # fmt: skip


class Crash(BaseException):
    """Simulates a process kill."""


@pytest.fixture
def paths(tmp_path: Path) -> ResearchPaths:
    data = tmp_path / "data"
    data.mkdir()
    (data / "ledger.sqlite3").write_bytes(b"")  # a placeholder: names only, never opened
    return ResearchPaths(data / "research")


def root(paths: ResearchPaths) -> Path:
    return rt.staging_root(paths)


def store_doc(paths: ResearchPaths) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((root(paths) / STORE_FILE).read_bytes())
    return doc


def rewrite_store(paths: ResearchPaths, doc: dict[str, Any], *, reseal: bool) -> None:
    """Tamper with ``store.json`` (it is 0444; the tamperer chmods it first)."""
    path = root(paths) / STORE_FILE
    if reseal:
        body = {k: v for k, v in doc.items() if k != "store_sha256"}
        doc = {**body, "store_sha256": sha256(canonical(body).encode())}
    path.chmod(0o644)
    path.write_text(json.dumps(doc))
    path.chmod(0o444)


def refused(paths: ResearchPaths, code: str, **kw: Any) -> None:
    with pytest.raises((RunnerRefusedError, StagingError), match=code):
        rt.session(paths, resume=True, connect=rt.never_connect, **kw)


def incomplete_first_session(paths: ResearchPaths, max_sessions: int = 2) -> None:
    first = rt.slow_session(paths, max_sessions=max_sessions)
    assert first["status"] == "INCOMPLETE" and first["resumable"]


def crash_on_call(n: int) -> Any:
    calls = {"n": 0}

    def crash(_url: str, _p: dict[str, str]) -> BaseException | None:
        calls["n"] += 1
        return Crash() if calls["n"] == n else None

    return crash


# ============================================================ 1-5: persistence and matching
def test_a_new_store_seals_max_sessions_2(paths: ResearchPaths) -> None:
    incomplete_first_session(paths, max_sessions=2)
    doc = store_doc(paths)
    assert doc["session_limit"] == 2
    body = {k: v for k, v in doc.items() if k != "store_sha256"}
    assert doc["store_sha256"] == sha256(canonical(body).encode())
    assert (root(paths) / STORE_FILE).stat().st_mode & 0o777 == 0o444
    start = json.loads((root(paths) / "sessions" / "01.start.json").read_bytes())
    assert start["max_sessions"] == 2
    p = rt.plan()
    assert StagingStore.open(root(paths), p).session_limit == 2
    st_ = status(paths=paths, plan=p, fingerprint=p.fingerprint, probe=rt.PROBE)
    assert st_["session_limit"] == 2 and st_["sessions_started"] == 1


def test_resume_with_the_sealed_limit_succeeds(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    second = rt.session(paths, resume=True, label="auth-2", max_sessions=2)
    assert second["status"] == "COMPLETE"


@pytest.mark.parametrize("requested", [9, 3, 1])
def test_resume_must_name_exactly_the_sealed_limit(paths: ResearchPaths,
                                                   requested: int) -> None:  # fmt: skip
    incomplete_first_session(paths)
    before = (root(paths) / STORE_FILE).read_bytes()
    refused(paths, "SESSION_LIMIT_MISMATCH", label="auth-2", max_sessions=requested)
    assert (root(paths) / STORE_FILE).read_bytes() == before
    assert sorted(x.name for x in (root(paths) / "sessions").iterdir()) == [
        "01.report.json", "01.start.json"]  # nothing started  # fmt: skip


def test_a_third_session_is_refused_after_two_started(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    second = rt.session(paths, rt.FakeHttp(page_size=100), resume=True, label="auth-2",
                        max_sessions=2)  # stops on a page cap  # fmt: skip
    assert second["status"] == "STOPPED" and second["pending"]  # work remains
    refused(paths, "SESSION_LIMIT_REACHED", label="auth-3", max_sessions=2)
    refused(paths, "SESSION_LIMIT_MISMATCH", label="auth-3", max_sessions=3)
    refused(paths, "SESSION_LIMIT_MISMATCH", label="auth-3", max_sessions=9)


# ============================================================ 6-7: interruption and recovery
def _recover(paths: ResearchPaths, label: str) -> dict[str, Any]:
    p = rt.plan()
    return dict(recover(paths=paths, plan=p, fingerprint=p.fingerprint, label=label,
                        now=lambda: rt.NOW))  # fmt: skip


def test_interrupted_sessions_count_and_recovery_never_resets_the_limit(
    paths: ResearchPaths,
) -> None:
    with pytest.raises(Crash):
        rt.session(paths, rt.FakeHttp(override=crash_on_call(3)), max_sessions=2)
    sealed = (root(paths) / STORE_FILE).read_bytes()
    refused(paths, "PREVIOUS_SESSION_INTERRUPTED", label="auth-2", max_sessions=2)
    assert _recover(paths, "2026-11-17-crash-1")["sessions_closed"] == [1]
    # the interrupted session still counts: one more fits under 2, but never under 1 or 9
    refused(paths, "SESSION_LIMIT_MISMATCH", label="auth-2", max_sessions=9)
    with pytest.raises(Crash):
        rt.session(paths, rt.FakeHttp(override=crash_on_call(2)), resume=True,
                   label="auth-2", max_sessions=2)  # fmt: skip
    assert _recover(paths, "2026-11-17-crash-2")["sessions_closed"] == [2]
    assert (root(paths) / STORE_FILE).read_bytes() == sealed  # recovery never touches it
    refused(paths, "SESSION_LIMIT_REACHED", label="auth-3", max_sessions=2)
    p = rt.plan()
    st_ = status(paths=paths, plan=p, fingerprint=p.fingerprint, probe=rt.PROBE)
    assert st_["sessions_started"] == 2 and st_["session_limit"] == 2
    assert st_["pending_batches"]  # refused although batches are still missing


# ============================================================ 8-9: fail closed
def test_a_store_without_a_limit_is_never_resumed(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    doc = store_doc(paths)
    rewrite_store(paths, {**doc, "session_limit": None, "session_slots": []}, reseal=True)
    for n in (1, 2, 9):
        refused(paths, "SESSION_LIMIT_MISSING", label="auth-2", max_sessions=n)


def test_absent_metadata_fails_closed(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    doc = store_doc(paths)
    rewrite_store(paths, {k: v for k, v in doc.items() if k != "session_limit"}, reseal=True)
    refused(paths, "STORE_METADATA_TAMPERED", label="auth-2", max_sessions=2)
    (root(paths) / STORE_FILE).unlink()
    refused(paths, "STORE_UNREADABLE", label="auth-2", max_sessions=2)


@pytest.mark.parametrize("bad", [0, 10, -2, "2", 2.0, True, [2], {"n": 2}])
def test_a_malformed_limit_fails_closed(paths: ResearchPaths, bad: object) -> None:
    incomplete_first_session(paths)
    rewrite_store(paths, {**store_doc(paths), "session_limit": bad}, reseal=True)
    refused(paths, "STORE_METADATA_INVALID", label="auth-2", max_sessions=2)
    p = rt.plan()
    with pytest.raises(StagingIntegrityError, match="STORE_METADATA_INVALID"):
        status(paths=paths, plan=p, fingerprint=p.fingerprint, probe=rt.PROBE)


def test_tampered_metadata_fails_integrity(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    doc = store_doc(paths)
    rewrite_store(paths, {**doc, "session_limit": 9}, reseal=False)  # digest now stale
    for n in (2, 9):
        refused(paths, "STORE_METADATA_TAMPERED", label="auth-2", max_sessions=n)
    rewrite_store(paths, {**doc, "store_sha256": "0" * 64}, reseal=False)
    refused(paths, "STORE_METADATA_TAMPERED", label="auth-2", max_sessions=2)
    p = rt.plan()
    for op in (lambda: status(paths=paths, plan=p, fingerprint=p.fingerprint, probe=None),
               lambda: _recover(paths, "x"), lambda: rt.do_publish(paths)):  # fmt: skip
        with pytest.raises(StagingIntegrityError, match="STORE_METADATA_TAMPERED"):
            op()


def test_a_resealed_raised_limit_is_caught_by_the_journal(paths: ResearchPaths) -> None:
    """Someone who also recomputes the (unkeyed) digest still meets the write-once
    journal starts, which record the limit each session was authorized under."""
    incomplete_first_session(paths)
    doc = store_doc(paths)
    slots = doc["session_slots"] + [f"{n:064x}" for n in range(7)]  # 9 commitments
    rewrite_store(paths, {**doc, "session_limit": 9, "session_slots": slots}, reseal=True)
    refused(paths, "JOURNAL_SESSION_LIMIT_MISMATCH", label="auth-2", max_sessions=9)
    refused(paths, "SESSION_LIMIT_MISMATCH", label="auth-2", max_sessions=2)


# ============================================================ 10: crash during initialization
def test_a_crash_before_store_json_is_linked_leaves_no_usable_store(
    paths: ResearchPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = os.link

    def link(src: Any, dst: Any, **kw: Any) -> None:
        if Path(dst).name == STORE_FILE:
            raise Crash
        real(src, dst, **kw)

    monkeypatch.setattr(os, "link", link)
    with pytest.raises(Crash):
        rt.session(paths, max_sessions=2)
    monkeypatch.setattr(os, "link", real)
    assert root(paths).is_dir() and not (root(paths) / STORE_FILE).exists()
    for n in (1, 2, 9):
        refused(paths, "STORE_UNREADABLE", label="auth-2", max_sessions=n)
    with pytest.raises(RunnerRefusedError, match="STORE_EXISTS"):
        rt.session(paths, max_sessions=9, connect=rt.never_connect)


def test_a_crash_after_sealing_but_before_the_journal_start(
    paths: ResearchPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def start(*_a: Any, **_k: Any) -> None:
        raise Crash

    monkeypatch.setattr(runner_mod.Journal, "start", start)
    with pytest.raises(Crash):
        rt.session(paths, max_sessions=2)
    monkeypatch.undo()
    assert store_doc(paths)["session_limit"] == 2
    for n in (1, 2, 9):
        refused(paths, "NO_SESSION_JOURNAL", label="auth-2", max_sessions=n)


def test_create_refuses_an_invalid_limit(tmp_path: Path) -> None:
    for bad in (0, 10, True):
        with pytest.raises(StagingError, match="SESSION_LIMIT_INVALID"):
            StagingStore.create(tmp_path / "s", rt.plan(), created_at=rt.NOW,
                                session_limit=bad)  # fmt: skip
        assert not (tmp_path / "s").exists()
    for bad in (0, 10, True):
        with pytest.raises(RunnerRefusedError, match="INVALID_MAX_SESSIONS"):
            Authorization(rt.plan().fingerprint, "a", bad)


# ============================================================ 11-12: no bypass
def test_the_runner_enforces_the_sealed_limit_itself(paths: ResearchPaths) -> None:
    """``run_authorized_session`` (what the runner CLI calls with the parsed
    ``--max-sessions``) checks the store, not just the caller's value."""
    incomplete_first_session(paths)
    p = rt.plan()
    with pytest.raises(RunnerRefusedError, match="SESSION_LIMIT_MISMATCH"):
        runner_mod.run_authorized_session(
            paths=paths, plan=p, authorization=Authorization(p.fingerprint, "direct", 9),
            probe=rt.PROBE, resume=True, connect=rt.never_connect,
            monotonic=rt.Clock().monotonic, sleep=rt.Clock().sleep, now=lambda: rt.NOW)  # fmt: skip


def test_the_supervisor_passes_the_value_unchanged_and_cannot_bypass(
    tmp_path: Path,
    paths: ResearchPaths,
) -> None:
    for n in (1, 2, 9):
        args = SessionArgs(runner_mod.APPROVED_ACQUISITION_PLAN_FINGERPRINT,
                           "history-probe-20261117T025839Z", "auth-2", n, True)  # fmt: skip
        argv = args.argv()
        assert argv[argv.index("--max-sessions") + 1] == str(n) and "--resume" in argv
    assert set(NEW_CODES) <= PRE_SESSION_REFUSALS
    incomplete_first_session(paths)
    log = tmp_path / "log"
    data = paths.root.parent
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(CHILD.parents[2])}
    out = run_child([sys.executable, str(CHILD), "runner_resume", str(log), str(data),
                     "auth-2", "9"], env, limit=timedelta(seconds=60))  # fmt: skip
    code, result = summarize(out)
    assert code == 2 and result["result"] == "REFUSED"
    report = result["session_report"]
    assert isinstance(report, dict) and report["refused"] == "SESSION_LIMIT_MISMATCH"
    assert all(v is None for k, v in report.items() if k != "refused")
    assert result["staging_state"] == "NOT_STARTED" and result["recovery_required"] is False
    assert result["sessions_launched"] == 1 and len(log.read_text().splitlines()) == 1
    assert sorted(x.name for x in (root(paths) / "sessions").iterdir()) == [
        "01.report.json", "01.start.json"]  # fmt: skip
    assert (data / "ledger.sqlite3").read_bytes() == b""
