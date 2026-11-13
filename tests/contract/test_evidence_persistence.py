"""Step 15B.5D: atomic evidence publication and write-failure handling — OFFLINE fault
injection with synthetic evidence only.

Evidence is sanitized in memory, written and fsynced into a private
``<root>/.incomplete-<run>-<hex>/`` staging directory, verified, then published with one
``rename`` to ``<root>/<run>``. Any filesystem failure is ``EVIDENCE_WRITE_FAILED``
(fixed code, no path/errno/content), staging is removed where possible, and the run
verdict fails. Every injected ``OSError`` below carries a synthetic secret in its text
to prove that text never escapes.
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.contract import live_run
from tests.contract.live_run import fail_if_not_ok, render, run_supervised
from tests.contract.supervisor import (
    STAGING_PREFIX,
    EvidenceCollisionError,
    EvidenceError,
    EvidenceWriteError,
    EvidenceWriter,
    Exchange,
    FileOps,
    RequestBudget,
    SanitizationError,
    _git_ignored,
)
from tests.contract.test_supervised_run import ENV, KEY, SECRET, FakeTime, HarnessSession
from tests.support.alpaca_account import ACCOUNT_ID

IDENTITIES = (KEY, SECRET, ACCOUNT_ID, "PA0000")
STAGING_NAME = re.compile(r"^\.incomplete-run-[0-9a-f]{16}$")


def exchanges() -> list[Exchange]:
    def ex(path: str, body: object) -> Exchange:
        return Exchange("GET", "paper-api.alpaca.markets", path, 200, {}, json.dumps(body))

    return [
        ex("/v2/account", {"id": ACCOUNT_ID, "account_number": "PA0000", "cash": "1000.00"}),
        ex("/v2/clock", {"is_open": True, "next_page_token": None, "note": f"k {SECRET}"}),
        ex("/v2/x", {"big": "y" * 50_000}),  # large enough for a meaningful mid-file failure
    ]


class FaultyFS(FileOps):
    """Fails the ``nth`` call of ``op`` whose path matches ``match``."""

    def __init__(self, op: str, *, nth: int = 1, match: Callable[[Path], bool] = lambda _p: True,
                 errno_: int = errno.ENOSPC) -> None:  # fmt: skip
        self.op, self.nth, self.match, self.errno = op, nth, match, errno_
        self.seen = 0
        self.calls: list[tuple[str, str]] = []
        self.paths: dict[int, Path] = {}

    def _maybe_fail(self, op: str, path: Path) -> None:
        self.calls.append((op, path.name))
        if op == self.op and self.match(path):
            self.seen += 1
            if self.seen == self.nth:
                raise OSError(self.errno, f"injected {SECRET} {ACCOUNT_ID}", f"/x/{KEY}")

    def mkdir(self, path: Path) -> None:
        self._maybe_fail("mkdir", path)
        super().mkdir(path)

    def chmod(self, path: Path, mode: int) -> None:
        self._maybe_fail("chmod", path)
        super().chmod(path, mode)

    def open_new(self, path: Path) -> int:
        self._maybe_fail("open_new", path)
        fd = super().open_new(path)
        self.paths[fd] = path
        return fd

    def write(self, fd: int, data: bytes) -> int:
        path = self.paths[fd]
        if self.op == "write_mid" and self.match(path):
            if self.seen == 0:
                self.seen = 1
                return super().write(fd, data[: max(1, len(data) // 2)])  # a short write ...
            raise OSError(self.errno, f"injected {SECRET}", f"/x/{KEY}")  # ... then failure
        if self.op == "write_lie" and self.match(path):
            super().write(fd, data[: len(data) // 2])
            return len(data)  # claims success: only verification can notice
        self._maybe_fail("write", path)
        return super().write(fd, data)

    def fsync(self, fd: int) -> None:
        self._maybe_fail("fsync", self.paths[fd])
        super().fsync(fd)

    def close(self, fd: int) -> None:
        path = self.paths.pop(fd)
        super().close(fd)  # the descriptor is released either way
        self._maybe_fail("close", path)

    def fsync_dir(self, path: Path) -> None:
        self._maybe_fail("fsync_dir", path)
        super().fsync_dir(path)

    def rename(self, src: Path, dst: Path) -> None:
        self._maybe_fail("rename", dst)
        super().rename(src, dst)

    def rmtree(self, path: Path) -> None:
        self._maybe_fail("rmtree", path)
        super().rmtree(path)


class CleanupFails(FaultyFS):
    """Fails ``op`` once and then also fails the cleanup."""

    def rmtree(self, path: Path) -> None:
        raise OSError(errno.EACCES, f"no {SECRET}", str(path))


def writer(tmp_path: Path, fs: FileOps | None = None, run: str = "run") -> EvidenceWriter:
    return EvidenceWriter(tmp_path / "evidence", run=run, require_git_ignored=False, fs=fs)


def write(w: EvidenceWriter) -> Path:
    return w.write(exchanges(), {"ok": True}, [ACCOUNT_ID], secrets=(KEY, SECRET))


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def assert_clean_failure(info: pytest.ExceptionInfo[EvidenceWriteError], root: Path) -> None:
    exc = info.value
    assert exc.code == "EVIDENCE_WRITE_FAILED" and str(exc) == "EVIDENCE_WRITE_FAILED"
    assert exc.__cause__ is None and exc.__suppress_context__  # no errno text, no path
    assert all(v not in repr(exc) + str(exc.args) for v in IDENTITIES)
    assert not (root / "run").exists()  # never a completed-looking run directory


def every_file_text(root: Path) -> str:
    return "".join(p.read_text() for p in root.rglob("*") if p.is_file()) if root.exists() else ""


# ============================================================ success: atomic publication
class Watcher(FileOps):
    """Asserts, at the moment of publication, that the final directory does not exist yet
    and the staging directory already holds every file."""

    def __init__(self) -> None:
        self.checked = False

    def rename(self, src: Path, dst: Path) -> None:
        assert STAGING_NAME.match(src.name) and src.parent == dst.parent
        assert not os.path.lexists(dst)
        assert (src / "summary.json").is_file() and len(list((src / "raw").iterdir())) == 3
        self.checked = True
        super().rename(src, dst)


def test_successful_publication_is_atomic_private_and_complete(tmp_path: Path) -> None:
    fs = Watcher()
    path = write(writer(tmp_path, fs))
    root = tmp_path / "evidence"
    assert fs.checked and path == root / "run" / "summary.json"
    assert tree(root) == ["run", "run/raw", "run/raw/000-paper-api_v2_account.json",
                          "run/raw/001-paper-api_v2_clock.json", "run/raw/002-paper-api_v2_x.json",
                          "run/summary.json"]  # fmt: skip  # no staging leftovers
    for d in (root, root / "run", root / "run" / "raw"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in root.rglob("*") if p.is_file())
    assert all(v not in every_file_text(root) for v in IDENTITIES)
    clock = json.loads((root / "run" / "raw" / "001-paper-api_v2_clock.json").read_text())
    assert json.loads(clock["body"])["next_page_token"] is None


def test_an_existing_completed_run_is_never_overwritten(tmp_path: Path) -> None:
    write(writer(tmp_path))
    before = (tmp_path / "evidence" / "run" / "summary.json").read_bytes()
    with pytest.raises(EvidenceError, match="EVIDENCE_DESTINATION_EXISTS"):
        write(writer(tmp_path))
    assert (tmp_path / "evidence" / "run" / "summary.json").read_bytes() == before
    assert not any(p.name.startswith(STAGING_PREFIX) for p in (tmp_path / "evidence").iterdir())


def test_a_run_published_concurrently_is_not_replaced(tmp_path: Path) -> None:
    """The final name appears between the pre-check and publication: rename(2) refuses a
    non-empty destination, so the other run stays intact."""

    class Racer(FileOps):
        def rename(self, src: Path, dst: Path) -> None:
            os.mkdir(dst, 0o700)
            (dst / "summary.json").write_text("other run")
            super().rename(src, dst)

    with pytest.raises(EvidenceCollisionError, match="EVIDENCE_DESTINATION_EXISTS") as info:
        write(writer(tmp_path, Racer()))
    assert not isinstance(info.value, EvidenceWriteError)  # a collision, not an I/O fault
    assert info.value.staging_removed
    root = tmp_path / "evidence"
    assert (root / "run" / "summary.json").read_text() == "other run"
    assert tree(root) == ["run", "run/summary.json"]


def test_staging_names_are_unique_and_cannot_collide_with_runs(tmp_path: Path) -> None:
    seen: list[str] = []

    class Names(FileOps):
        def mkdir(self, path: Path) -> None:
            seen.append(path.name)
            super().mkdir(path)

    write(writer(tmp_path, Names(), run="run"))
    write(writer(tmp_path, Names(), run="run2"))
    staging = [n for n in seen if n.startswith(STAGING_PREFIX)]
    assert len(staging) == 2 and len(set(staging)) == 2
    assert STAGING_NAME.match(staging[0]) and staging[1].startswith(".incomplete-run2-")
    assert sorted(p.name for p in (tmp_path / "evidence").iterdir()) == ["run", "run2"]
    for bad in (".incomplete-run-0123", ".hidden"):
        with pytest.raises(EvidenceError, match="EVIDENCE_PATH_INVALID"):
            write(writer(tmp_path, run=bad))


# ============================================================ fault injection
def staging(p: Path) -> bool:
    return p.name.startswith(STAGING_PREFIX)


def under_staging(p: Path) -> bool:
    return any(part.startswith(STAGING_PREFIX) for part in p.parts)


CASES = [
    pytest.param("mkdir", 1, lambda p: p.name == "evidence", id="evidence-root-creation"),
    pytest.param("chmod", 1, lambda p: p.name == "evidence", id="evidence-root-chmod"),
    pytest.param("mkdir", 1, staging, id="staging-directory-creation"),
    pytest.param("mkdir", 1, lambda p: p.name == "raw", id="raw-directory-creation"),
    pytest.param("open_new", 1, under_staging, id="first-file-open"),
    pytest.param("write", 1, under_staging, id="first-file-write"),
    pytest.param("open_new", 3, under_staging, id="later-file-open"),
    pytest.param("write", 3, under_staging, id="later-file-write"),
    pytest.param("write_mid", 1, lambda p: p.name.endswith("_x.json"), id="mid-file-write"),
    pytest.param("open_new", 1, lambda p: p.name == "summary.json", id="summary-open"),
    pytest.param("write", 1, lambda p: p.name == "summary.json", id="summary-write"),
    pytest.param("fsync", 2, under_staging, id="file-flush-fsync"),
    pytest.param("close", 1, lambda p: p.name == "summary.json", id="file-close"),
    pytest.param("chmod", 1, lambda p: p.suffix == ".json", id="file-chmod"),
    pytest.param("fsync_dir", 1, staging, id="staging-directory-fsync"),
    pytest.param("rename", 1, lambda _p: True, id="final-publication"),
]  # fmt: skip


@pytest.mark.parametrize(("op", "nth", "match"), CASES)
def test_write_failure_is_fixed_code_and_leaves_no_run(
    tmp_path: Path, op: str, nth: int, match: Callable[[Path], bool]
) -> None:
    fs = FaultyFS(op, nth=nth, match=match)
    with pytest.raises(EvidenceWriteError) as info:
        write(writer(tmp_path, fs))
    assert fs.seen >= 1, "the fault was never reached"
    root = tmp_path / "evidence"
    assert_clean_failure(info, root)
    assert info.value.staging_removed is True
    assert tree(root) == []  # staging removed; nothing else was ever created


def test_unverifiable_short_write_is_caught_before_publication(tmp_path: Path) -> None:
    fs = FaultyFS("write_lie", match=lambda p: p.name == "summary.json")
    with pytest.raises(EvidenceWriteError) as info:
        write(writer(tmp_path, fs))
    assert_clean_failure(info, tmp_path / "evidence")
    assert tree(tmp_path / "evidence") == []


def test_failure_after_publication_still_fails_the_run(tmp_path: Path) -> None:
    """The root's fsync fails after the rename: the run directory is complete (it was
    verified before the rename) but durability is not confirmed, so the run fails."""
    fs = FaultyFS("fsync_dir", match=lambda p: p.name == "evidence")
    with pytest.raises(EvidenceWriteError) as info:
        write(writer(tmp_path, fs))
    assert info.value.staging_removed is True
    assert (tmp_path / "evidence" / "run" / "summary.json").is_file()


@pytest.mark.parametrize(("op", "nth"), [("open_new", 2), ("write_mid", 1), ("rename", 1)])
def test_cleanup_failure_leaves_only_a_distinguishable_incomplete_artifact(
    tmp_path: Path, op: str, nth: int
) -> None:
    fs = CleanupFails(op, nth=nth, match=lambda p: p.name.endswith(".json") or op == "rename")
    with pytest.raises(EvidenceWriteError) as info:
        write(writer(tmp_path, fs))
    root = tmp_path / "evidence"
    assert_clean_failure(info, root)
    assert info.value.staging_removed is False
    (left,) = list(root.iterdir())
    assert STAGING_NAME.match(left.name)  # dot-prefixed: never a run name
    assert stat.S_IMODE(left.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in left.rglob("*") if p.is_file())
    assert all(v not in every_file_text(root) for v in IDENTITIES)  # sanitized, even partial


def test_sanitization_failure_writes_nothing_and_is_not_a_write_failure(tmp_path: Path) -> None:
    fs = FaultyFS("never")
    bad = [*exchanges(), Exchange("GET", "paper-api.alpaca.markets", "/v2/y", 200, {}, "{oops")]
    with pytest.raises(SanitizationError) as info:
        writer(tmp_path, fs).write(bad, {"ok": True}, [], secrets=(SECRET,))
    assert not isinstance(info.value, EvidenceError)
    assert fs.calls == [] and not (tmp_path / "evidence").exists()


def test_git_failure_counts_as_not_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert _git_ignored(tmp_path) is False
    with pytest.raises(EvidenceError, match="EVIDENCE_ROOT_NOT_GIT_IGNORED"):
        write(EvidenceWriter(tmp_path / "evidence", run="run"))
    assert not (tmp_path / "evidence").exists()


# ============================================================ supervised run verdicts
@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    target = logging.getLogger("swing_trading_bot")
    saved = (list(target.handlers), target.propagate, target.level)
    stream = io.StringIO()
    yield stream
    target.handlers[:] = saved[0]
    target.propagate, target.level = saved[1], saved[2]


def supervised(tmp_path: Path, logs: io.StringIO, fs: FileOps | None = None) -> dict[str, Any]:
    clock = FakeTime()
    session = HarnessSession(clock)
    return run_supervised(
        ENV, budget=RequestBudget(monotonic=clock), session_factory=lambda: session,
        sleep=clock.sleep, now=lambda: session.account.clock.now,
        evidence=writer(tmp_path, fs), log_stream=logs,
        log_target=logging.getLogger("swing_trading_bot"),
    )  # fmt: skip


def test_successful_run_keeps_every_contract_outcome(tmp_path: Path, logs: io.StringIO) -> None:
    summary = supervised(tmp_path, logs)
    assert summary["evidence"] == {"written": True, "summary": "summary.json"}
    assert summary["verdict"] == {"status": "PASSED", "code": "OK"}
    rows = {r: v["outcome"] for r, v in summary["rows"].items()}
    assert rows == {
        "C1": "VERIFIED", "C2": "VERIFIED", "C3": "VERIFIED", "C4": "VERIFIED",
        "C5": "INCONCLUSIVE", "C6": "VERIFIED", "C7": "VERIFIED", "C8": "VERIFIED",
        "C9": "INCONCLUSIVE", "C10": "DEFERRED", "C11": "DEFERRED", "C12": "DEFERRED",
        "C13": "DEFERRED",
    }  # fmt: skip
    fail_if_not_ok(summary)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_write_failure_fails_the_run_without_rows_or_leaks(
    tmp_path: Path, logs: io.StringIO, cleanup_fails: bool
) -> None:
    fs_type = CleanupFails if cleanup_fails else FaultyFS
    summary = supervised(tmp_path, logs, fs_type("open_new", nth=4, match=under_staging))
    assert summary == {
        "execution": {"status": "STOPPED", "stop_code": "EVIDENCE_WRITE_FAILED"},
        "evidence": {"written": False, "code": "EVIDENCE_WRITE_FAILED",
                     "staging_removed": not cleanup_fails},
        "verdict": {"status": "FAILED", "code": "EVIDENCE_WRITE_FAILED"},
    }  # fmt: skip
    assert "VERIFIED" not in json.dumps(summary)
    assert not (tmp_path / "evidence" / "run").exists()
    corpus = json.dumps(summary) + logs.getvalue() + render(summary)
    assert all(v not in corpus for v in IDENTITIES) and "injected" not in corpus
    with pytest.raises(pytest.fail.Exception):
        fail_if_not_ok(summary)


def test_write_failure_gives_a_failing_cli_exit(
    tmp_path: Path, logs: io.StringIO, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    summary = supervised(tmp_path, logs, FaultyFS("rename"))
    monkeypatch.setenv(live_run.ENABLE_VARIABLE, "1")
    monkeypatch.setattr(live_run, "run_supervised", lambda _env: summary)
    assert live_run.main() == 1
    out = capsys.readouterr().out
    assert "EVIDENCE_WRITE_FAILED" in out and '"written": false' in out
    assert all(v not in out for v in IDENTITIES)


def test_a_zero_byte_write_is_a_write_failure(tmp_path: Path) -> None:
    class Stalls(FileOps):
        def write(self, fd: int, data: bytes) -> int:
            return 0  # the device accepts nothing: never loop forever

    with pytest.raises(EvidenceWriteError) as info:
        write(writer(tmp_path, Stalls()))
    assert_clean_failure(info, tmp_path / "evidence")
    assert tree(tmp_path / "evidence") == []


def test_a_staging_directory_that_is_not_private_is_never_published(tmp_path: Path) -> None:
    class Loose(FileOps):
        def chmod(self, path: Path, mode: int) -> None:
            super().chmod(path, 0o755 if path.name == "raw" else mode)

    with pytest.raises(EvidenceWriteError) as info:
        write(writer(tmp_path, Loose()))
    assert_clean_failure(info, tmp_path / "evidence")
    assert tree(tmp_path / "evidence") == []
