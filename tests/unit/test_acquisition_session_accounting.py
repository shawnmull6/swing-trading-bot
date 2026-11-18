"""Durable session accounting (Step 16.17C.3.1).

A store sealed with ``session_limit`` *L* holds *L* slot files whose SHA-256s are
committed in ``store.json``; starting session *n* deletes slot *n* (fsynced, before any
request). Deleting journal records can therefore never give a slot back: a consumed slot
with no journal start is ``SESSION_ACCOUNTING_INCONSISTENT``. Everything here is
SYNTHETIC: temporary stores, the mocked HTTP of the runner tests, a placeholder ledger
name that is never opened, no credential and no network (refusals use
``never_connect``, which fails if credentials would be read)."""

from __future__ import annotations

import os
import shutil
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
    status,
)
from swing_trading_bot.history_acquisition.supervisor import (
    PRE_SESSION_REFUSALS,
    run_child,
    summarize,
)
from swing_trading_bot.research.acquisition import (
    REQUEST_BUDGET,
    SESSION_DEADLINE_S,
    build_acquisition_plan,
)
from swing_trading_bot.research.staging import (
    SLOTS_DIR,
    STORE_FILE,
    StagingError,
    StagingIntegrityError,
    StagingStore,
)
from tests.unit import test_history_acquisition_runner as rt
from tests.unit.test_acquisition_session_cap import (
    CHILD,
    Crash,
    _recover,
    crash_on_call,
    incomplete_first_session,
    refused,
    rewrite_store,
    root,
    store_doc,
)

INCONSISTENT = "SESSION_ACCOUNTING_INCONSISTENT"


@pytest.fixture
def paths(tmp_path: Path) -> ResearchPaths:
    data = tmp_path / "data"
    data.mkdir()
    (data / "ledger.sqlite3").write_bytes(b"")  # a placeholder: names only, never opened
    return ResearchPaths(data / "research")


def slots(paths: ResearchPaths) -> list[str]:
    return sorted(p.name for p in (root(paths) / SLOTS_DIR).iterdir())


def journal(paths: ResearchPaths) -> list[str]:
    return sorted(p.name for p in (root(paths) / "sessions").iterdir())


def delete(paths: ResearchPaths, *names: str) -> None:
    for name in names:
        (root(paths) / "sessions" / name).unlink()


def two_sessions(paths: ResearchPaths) -> None:
    """Session 1 INCOMPLETE, session 2 STOPPED on a page cap: work remains, 2 of 2 used."""
    incomplete_first_session(paths)
    second = rt.session(paths, rt.FakeHttp(page_size=100), resume=True, label="auth-2",
                        max_sessions=2)  # fmt: skip
    assert second["status"] == "STOPPED" and second["pending"]


def accounting(paths: ResearchPaths) -> dict[str, Any]:
    p = rt.plan()
    return dict(status(paths=paths, plan=p, fingerprint=p.fingerprint, probe=rt.PROBE))


# ============================================================ slots: creation and consumption
def test_a_new_store_commits_one_slot_per_authorized_session(paths: ResearchPaths) -> None:
    incomplete_first_session(paths, max_sessions=3)
    doc = store_doc(paths)
    assert len(doc["session_slots"]) == 3 and len(set(doc["session_slots"])) == 3
    assert slots(paths) == ["02.slot", "03.slot"]  # session 1 consumed slot 1
    for name in slots(paths):
        assert (root(paths) / SLOTS_DIR / name).stat().st_mode & 0o777 == 0o444
    st_ = accounting(paths)
    assert st_["slots_consumed"] == [1] and st_["session_accounting"] == "CONSISTENT"


def test_the_slot_is_consumed_before_the_first_request(paths: ResearchPaths) -> None:
    seen: list[bool] = []

    def look(_url: str, _p: dict[str, str]) -> None:
        if not seen:
            seen.append((root(paths) / SLOTS_DIR / "01.slot").exists())

    rt.session(paths, rt.FakeHttp(override=look), max_sessions=2)
    assert seen == [False]
    assert (root(paths) / "sessions" / "01.start.json").exists()


# ============================================================ deleted journal records
def test_deleting_the_latest_start_record_fails_closed(paths: ResearchPaths) -> None:
    two_sessions(paths)
    delete(paths, "02.start.json")
    refused(paths, "JOURNAL_INCONSISTENT", label="auth-3", max_sessions=2)
    with pytest.raises(RunnerRefusedError, match="JOURNAL_INCONSISTENT"):
        _recover(paths, "after-delete")
    with pytest.raises(RunnerRefusedError, match="JOURNAL_INCONSISTENT"):
        rt.do_publish(paths)


def test_deleting_the_latest_report_never_restores_its_slot(paths: ResearchPaths) -> None:
    two_sessions(paths)
    delete(paths, "02.report.json")
    refused(paths, "PREVIOUS_SESSION_INTERRUPTED", label="auth-3", max_sessions=2)
    assert _recover(paths, "lost-report")["sessions_closed"] == [2]
    assert slots(paths) == []  # still consumed: recovery gave nothing back
    refused(paths, "SESSION_LIMIT_REACHED", label="auth-3", max_sessions=2)


def test_deleting_both_latest_records_fails_closed(paths: ResearchPaths) -> None:
    """Before 16.17C.3.1 this restored a session: the journal said 1 of 2."""
    two_sessions(paths)
    delete(paths, "02.start.json", "02.report.json")
    assert journal(paths) == ["01.report.json", "01.start.json"]
    refused(paths, INCONSISTENT, label="auth-3", max_sessions=2)
    with pytest.raises(RunnerRefusedError, match=INCONSISTENT):
        rt.do_publish(paths)
    st_ = accounting(paths)
    assert st_["session_accounting"] == INCONSISTENT and st_["slots_consumed"] == [1, 2]
    assert st_["sessions_started"] == 1


def test_deleting_every_journal_record_fails_closed(paths: ResearchPaths) -> None:
    two_sessions(paths)
    delete(paths, *journal(paths))
    refused(paths, "NO_SESSION_JOURNAL", label="auth-3", max_sessions=2)
    shutil.rmtree(root(paths) / "sessions")
    refused(paths, "NO_SESSION_JOURNAL", label="auth-3", max_sessions=2)
    assert accounting(paths)["session_accounting"] == INCONSISTENT


# ============================================================ interruption and recovery
def test_a_crash_between_the_start_record_and_the_slot(
    paths: ResearchPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupted session creation: the start is durable, the slot not yet consumed and
    nothing was sent. It is an interrupted session; recovery consumes its slot."""
    http = rt.FakeHttp()

    def crash(*_a: Any, **_k: Any) -> None:
        raise Crash

    monkeypatch.setattr(StagingStore, "consume_slot", crash)
    with pytest.raises(Crash):
        rt.session(paths, http, max_sessions=2)
    monkeypatch.undo()
    assert http.calls == []  # no request before the slot is consumed
    assert slots(paths) == ["01.slot", "02.slot"] and journal(paths) == ["01.start.json"]
    assert accounting(paths)["session_accounting"] == "CONSISTENT"
    refused(paths, "PREVIOUS_SESSION_INTERRUPTED", label="auth-2", max_sessions=2)
    assert _recover(paths, "crash-before-slot")["sessions_closed"] == [1]
    assert slots(paths) == ["02.slot"]
    second = rt.session(paths, resume=True, label="auth-2", max_sessions=2)
    assert second["status"] == "COMPLETE" and slots(paths) == []


def test_a_crash_while_creating_slots_leaves_no_usable_store(
    paths: ResearchPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = os.open
    calls = {"n": 0}

    def opener(path: Any, flags: int, *a: Any, **k: Any) -> int:
        if str(path).endswith(".slot"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise Crash
        return real(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", opener)
    with pytest.raises(Crash):
        rt.session(paths, max_sessions=2)
    monkeypatch.undo()
    assert not (root(paths) / STORE_FILE).exists()
    refused(paths, "STORE_UNREADABLE", label="auth-2", max_sessions=2)
    with pytest.raises(RunnerRefusedError, match="STORE_EXISTS"):
        rt.session(paths, max_sessions=9, connect=rt.never_connect)


def test_recovery_after_interruption_keeps_both_slots_used(paths: ResearchPaths) -> None:
    with pytest.raises(Crash):
        rt.session(paths, rt.FakeHttp(override=crash_on_call(3)), max_sessions=2)
    assert slots(paths) == ["02.slot"]  # consumed before the crash
    _recover(paths, "crash-1")
    with pytest.raises(Crash):
        rt.session(paths, rt.FakeHttp(override=crash_on_call(2)), resume=True,
                   label="auth-2", max_sessions=2)  # fmt: skip
    _recover(paths, "crash-2")
    assert slots(paths) == []
    refused(paths, "SESSION_LIMIT_REACHED", label="auth-3", max_sessions=2)
    delete(paths, "02.start.json", "02.report.json")  # hide the second crash
    refused(paths, INCONSISTENT, label="auth-3", max_sessions=2)


def test_a_third_session_is_refused_after_two_starts(paths: ResearchPaths) -> None:
    two_sessions(paths)
    assert slots(paths) == []
    refused(paths, "SESSION_LIMIT_REACHED", label="auth-3", max_sessions=2)
    st_ = accounting(paths)
    assert st_["slots_consumed"] == [1, 2] and st_["session_accounting"] == "CONSISTENT"


# ============================================================ missing or inconsistent metadata
def test_absent_slot_commitments_fail_closed(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    doc = {k: v for k, v in store_doc(paths).items() if k != "session_slots"}
    rewrite_store(paths, doc, reseal=True)
    refused(paths, "STORE_METADATA_TAMPERED", label="auth-2", max_sessions=2)


@pytest.mark.parametrize("bad", [
    [], ["0" * 64], ["0" * 64] * 2, ["0" * 64, "x" * 64], ["0" * 64, 7], "00", None,
    ["0" * 64, "1" * 64, "2" * 64],
])  # fmt: skip
def test_malformed_slot_commitments_fail_closed(paths: ResearchPaths, bad: object) -> None:
    incomplete_first_session(paths)
    rewrite_store(paths, {**store_doc(paths), "session_slots": bad}, reseal=True)
    refused(paths, "STORE_METADATA_INVALID", label="auth-2", max_sessions=2)


def test_an_unsealed_slot_commitment_change_fails_integrity(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    doc = store_doc(paths)
    rewrite_store(paths, {**doc, "session_slots": ["0" * 64, "1" * 64]}, reseal=False)
    refused(paths, "STORE_METADATA_TAMPERED", label="auth-2", max_sessions=2)


def test_a_forged_slot_file_is_tampering(paths: ResearchPaths) -> None:
    two_sessions(paths)
    delete(paths, "02.start.json", "02.report.json")
    forged = root(paths) / SLOTS_DIR / "02.slot"
    forged.write_bytes(os.urandom(32).hex().encode())  # no secret, no matching commitment
    refused(paths, "SESSION_SLOT_TAMPERED", label="auth-3", max_sessions=2)
    with pytest.raises(StagingIntegrityError, match="SESSION_SLOT_TAMPERED"):
        accounting(paths)


@pytest.mark.parametrize("name", ["03.slot", "notes.txt", ".hidden"])
def test_an_unknown_file_among_slots_is_tampering(paths: ResearchPaths, name: str) -> None:
    incomplete_first_session(paths)
    (root(paths) / SLOTS_DIR / name).write_bytes(b"x")
    refused(paths, "SESSION_SLOT_TAMPERED", label="auth-2", max_sessions=2)


def test_a_symlinked_slot_is_tampering(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    slot = root(paths) / SLOTS_DIR / "02.slot"
    copy = slot.parent.parent / "elsewhere.slot"
    shutil.copy(slot, copy)
    slot.unlink()
    slot.symlink_to(copy)
    refused(paths, "SESSION_SLOT_TAMPERED", label="auth-2", max_sessions=2)


def test_losing_slot_files_only_uses_up_capacity(paths: ResearchPaths) -> None:
    incomplete_first_session(paths)
    shutil.rmtree(root(paths) / SLOTS_DIR)
    refused(paths, INCONSISTENT, label="auth-2", max_sessions=2)  # slot 2 gone, no session 2
    assert accounting(paths)["slots_consumed"] == [1, 2]


def test_a_closed_session_whose_slot_reappears_is_inconsistent(
    paths: ResearchPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot file copied just before session 1 consumed it, restored afterwards while
    session 1's records remain."""
    before = paths.root / "slot-copy"  # inside the research root the guard allows
    real_consume = StagingStore.consume_slot

    def copy_then_consume(self: StagingStore, n: int) -> None:
        shutil.copy(self.root / SLOTS_DIR / f"{n:02d}.slot", before)
        real_consume(self, n)

    monkeypatch.setattr(StagingStore, "consume_slot", copy_then_consume)
    incomplete_first_session(paths)
    monkeypatch.undo()
    shutil.copy(before, root(paths) / SLOTS_DIR / "01.slot")
    refused(paths, INCONSISTENT, label="auth-2", max_sessions=2)


def test_trust_boundary_a_restored_backup_with_deleted_records_is_not_detected(
    paths: ResearchPaths,
) -> None:
    """Documented limitation: an actor who kept a copy of a slot file from before it was
    consumed, restores it AND deletes that session's journal records leaves a store that
    is indistinguishable from one where the session never ran. The slots defend against
    deletion and loss, not against restoring a pre-consumption backup."""
    backup = paths.root.parent / "slots-backup"
    incomplete_first_session(paths)
    shutil.copytree(root(paths) / SLOTS_DIR, backup)  # slot 2 still unconsumed here
    second = rt.session(paths, rt.FakeHttp(page_size=100), resume=True, label="auth-2",
                        max_sessions=2)  # fmt: skip
    assert second["status"] == "STOPPED" and slots(paths) == []
    shutil.copy(backup / "02.slot", root(paths) / SLOTS_DIR / "02.slot")
    delete(paths, "02.start.json", "02.report.json")
    assert accounting(paths)["session_accounting"] == "CONSISTENT"  # not detected


# ============================================================ no bypass
def test_the_runner_enforces_accounting_itself(paths: ResearchPaths) -> None:
    two_sessions(paths)
    delete(paths, "02.start.json", "02.report.json")
    p = rt.plan()
    with pytest.raises(RunnerRefusedError, match=INCONSISTENT):
        runner_mod.run_authorized_session(
            paths=paths, plan=p, authorization=Authorization(p.fingerprint, "direct", 2),
            probe=rt.PROBE, resume=True, connect=rt.never_connect,
            monotonic=rt.Clock().monotonic, sleep=rt.Clock().sleep, now=lambda: rt.NOW)  # fmt: skip
    with pytest.raises(StagingError, match="SESSION_SLOT_CONSUMED"):
        StagingStore.open(root(paths), p).consume_slot(2)
    with pytest.raises(StagingError, match="SESSION_SLOT_UNAVAILABLE"):
        StagingStore.open(root(paths), p).consume_slot(3)


def test_the_supervisor_cannot_bypass_accounting(tmp_path: Path, paths: ResearchPaths) -> None:
    assert INCONSISTENT in PRE_SESSION_REFUSALS
    two_sessions(paths)
    delete(paths, "02.start.json", "02.report.json")
    log = tmp_path / "log"
    data = paths.root.parent
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(CHILD.parents[2])}
    out = run_child([sys.executable, str(CHILD), "runner_resume", str(log), str(data),
                     "auth-3", "2"], env, limit=timedelta(seconds=60))  # fmt: skip
    code, result = summarize(out)
    assert code == 2 and result["result"] == "REFUSED"
    report = result["session_report"]
    assert isinstance(report, dict) and report["refused"] == INCONSISTENT
    assert result["sessions_launched"] == 1 and len(log.read_text().splitlines()) == 1
    assert journal(paths) == ["01.report.json", "01.start.json"] and slots(paths) == []
    assert (data / "ledger.sqlite3").read_bytes() == b""


# ============================================================ budgets and plan unchanged
def test_budgets_and_the_approved_plan_are_unchanged() -> None:
    plan = build_acquisition_plan()
    assert plan.fingerprint == runner_mod.APPROVED_ACQUISITION_PLAN_FINGERPRINT == (
        "6ca96c0601a404b19bd5adac44bc1e46141a9febd9c66db7b9438a7380ae5a3f")  # fmt: skip
    assert (REQUEST_BUDGET, SESSION_DEADLINE_S) == (120, 300)
    assert (plan.session_max_attempts, plan.session_deadline_s) == (120, 300)
    assert len(plan.batches) == 51
