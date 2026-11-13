"""Step 15B.5D: atomic **no-replace** evidence publication through the native platform call
(macOS ``renamex_np(RENAME_EXCL)``, Linux ``renameat2(RENAME_NOREPLACE)``) — OFFLINE.

The real native operation is exercised wherever the platform supports it; mocked natives
are used only for the fail-closed paths (unsupported OS, missing symbol, unsupported
filesystem). There is no fallback to ``os.rename``/``os.replace``.
"""

from __future__ import annotations

import ast
import ctypes
import errno
import os
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.contract import supervisor
from tests.contract.supervisor import (
    AT_FDCWD,
    RENAME_EXCL,
    RENAME_NOREPLACE,
    STAGING_PREFIX,
    EvidenceCollisionError,
    EvidenceWriteError,
    EvidenceWriter,
    Exchange,
    FileOps,
    noreplace_rename,
    platform_noreplace_rename,
    resolve_noreplace_rename,
)
from tests.contract.test_supervised_run import KEY, SECRET
from tests.support.alpaca_account import ACCOUNT_ID

SUPPORTED = sys.platform == "darwin" or sys.platform.startswith("linux")
native_only = pytest.mark.skipif(not SUPPORTED, reason="no native no-replace rename here")
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS renamex_np")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2")


def native() -> Any:
    fn = platform_noreplace_rename()
    assert fn is not None
    return fn


def staged(tmp_path: Path, name: str = "staging") -> Path:
    src = tmp_path / name
    src.mkdir()
    (src / "summary.json").write_text(name)
    return src


def exchanges() -> list[Exchange]:
    body = f'{{"id": "{ACCOUNT_ID}", "note": "{SECRET}"}}'
    return [Exchange("GET", "paper-api.alpaca.markets", "/v2/account", 200, {}, body)]


def write(root: Path, fs: FileOps | None = None, summary: Any = None) -> Path:
    w = EvidenceWriter(root, run="run", require_git_ignored=False, fs=fs)
    return w.write(exchanges(), summary or {"ok": True}, [], secrets=(KEY, SECRET))


# ============================================================ constants and signatures
def test_reviewed_platform_constants() -> None:
    assert RENAME_EXCL == 0x4  # macOS <stdio.h>
    assert RENAME_NOREPLACE == 0x1  # Linux <linux/fs.h>
    assert AT_FDCWD == -100  # Linux <fcntl.h>


@macos_only
def test_macos_uses_renamex_np_with_explicit_signature() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    assert resolve_noreplace_rename("darwin", libc) is not None
    fn = libc.renamex_np
    assert fn.argtypes == [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    assert fn.restype is ctypes.c_int


@linux_only
def test_linux_uses_renameat2_with_explicit_signature() -> None:  # pragma: no cover - Linux
    libc = ctypes.CDLL(None, use_errno=True)
    if resolve_noreplace_rename("linux", libc) is None:
        pytest.skip("glibc < 2.28: renameat2 not exported (publication fails closed)")
    fn = libc.renameat2
    assert fn.argtypes == [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_uint]  # fmt: skip
    assert fn.restype is ctypes.c_int


# ============================================================ the real native operation
@native_only
def test_publishes_when_the_destination_is_absent(tmp_path: Path) -> None:
    src = staged(tmp_path)
    noreplace_rename(src, tmp_path / "run", native())
    assert not src.exists() and (tmp_path / "run" / "summary.json").read_text() == "staging"


@native_only
@pytest.mark.parametrize("kind", ["empty-dir", "non-empty-dir", "file", "symlink"])
def test_never_replaces_an_existing_destination(tmp_path: Path, kind: str) -> None:
    src, dst = staged(tmp_path), tmp_path / "run"
    if kind == "empty-dir":
        dst.mkdir()
    elif kind == "non-empty-dir":
        dst.mkdir()
        (dst / "summary.json").write_text("other")
    elif kind == "file":
        dst.write_text("file")
    else:
        dst.symlink_to(tmp_path / "nowhere")
    before = sorted(os.listdir(dst)) if dst.is_dir() and not dst.is_symlink() else None
    with pytest.raises(FileExistsError) as info:
        noreplace_rename(src, dst, native())
    assert info.value.errno == errno.EEXIST
    assert (src / "summary.json").read_text() == "staging"  # source untouched
    assert os.path.lexists(dst)
    if before is not None:
        assert sorted(os.listdir(dst)) == before


@native_only
def test_native_errors_carry_errno_and_no_path(tmp_path: Path) -> None:
    secret_dir = tmp_path / f"x{SECRET.replace('/', '_')}"
    with pytest.raises(FileNotFoundError) as info:
        noreplace_rename(secret_dir / "missing", tmp_path / "run", native())
    assert info.value.errno == errno.ENOENT
    assert info.value.filename is None and SECRET.replace("/", "_") not in str(info.value)


@pytest.mark.parametrize("bad", ["a\0b", "x\0"])
def test_embedded_nul_is_refused_before_any_native_call(tmp_path: Path, bad: str) -> None:
    def must_not_run(_s: bytes, _d: bytes) -> int:
        raise AssertionError("native called")

    with pytest.raises(OSError, match=os.strerror(errno.EINVAL)) as info:
        noreplace_rename(tmp_path / bad, tmp_path / "run", must_not_run)
    assert info.value.errno == errno.EINVAL


@native_only
def test_successful_atomic_publication_with_private_permissions(tmp_path: Path) -> None:
    path = write(tmp_path / "ev")
    root = tmp_path / "ev"
    assert path == root / "run" / "summary.json"
    assert sorted(p.name for p in root.iterdir()) == ["run"]
    for d in (root, root / "run", root / "run" / "raw"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    files = [p for p in root.rglob("*") if p.is_file()]
    assert files and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
    text = "".join(p.read_text() for p in files)
    assert SECRET not in text and ACCOUNT_ID not in text  # sanitization unchanged


@native_only
def test_concurrent_publications_of_one_run_name_exactly_one_wins(tmp_path: Path) -> None:
    """Two writers stage completely, then publish at the same moment with the real
    native call: exactly one publishes, the other is a collision; nothing is mixed."""
    gate = threading.Barrier(2)

    class Synchronized(FileOps):
        def rename(self, src: Path, dst: Path) -> None:
            gate.wait(timeout=10)
            super().rename(src, dst)

    outcomes: dict[str, object] = {}

    def attempt(label: str) -> None:
        try:
            outcomes[label] = write(tmp_path / "ev", Synchronized(), {"writer": label})
        except EvidenceCollisionError as exc:
            outcomes[label] = exc

    (tmp_path / "ev").mkdir(mode=0o700)
    threads = [threading.Thread(target=attempt, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    winners = [k for k, v in outcomes.items() if isinstance(v, Path)]
    losers = [k for k, v in outcomes.items() if isinstance(v, EvidenceCollisionError)]
    assert len(winners) == 1 and len(losers) == 1
    loser = outcomes[losers[0]]
    assert isinstance(loser, EvidenceCollisionError) and loser.staging_removed
    published = (tmp_path / "ev" / "run" / "summary.json").read_text()
    assert f'"writer": "{winners[0]}"' in published
    assert sorted(p.name for p in (tmp_path / "ev").iterdir()) == ["run"]


# ============================================================ fail closed, no fallback
@pytest.mark.parametrize("platform", ["win32", "cygwin", "freebsd14", "aix", ""])
def test_unsupported_os_has_no_native_rename(platform: str) -> None:
    assert resolve_noreplace_rename(platform) is None


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_missing_native_symbol_has_no_native_rename(platform: str) -> None:
    class NoSymbols:
        pass

    assert resolve_noreplace_rename(platform, NoSymbols()) is None


def test_unavailable_native_rename_fails_closed_as_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "platform_noreplace_rename", lambda: None)
    with pytest.raises(EvidenceWriteError) as info:
        write(tmp_path / "ev")
    assert not isinstance(info.value, EvidenceCollisionError)
    assert info.value.code == "EVIDENCE_WRITE_FAILED" and info.value.staging_removed
    assert list((tmp_path / "ev").iterdir()) == []  # nothing published, staging removed


@pytest.mark.parametrize("err", [errno.EINVAL, errno.ENOTSUP, errno.ENOSYS, errno.EXDEV,
                                 errno.EACCES, 0])  # fmt: skip
def test_unsupported_filesystem_and_native_errors_are_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    def failing(_src: bytes, _dst: bytes) -> int:
        ctypes.set_errno(err)  # as the C function would; 0 = failure without errno
        return -1

    monkeypatch.setattr(supervisor, "platform_noreplace_rename", lambda: failing)
    with pytest.raises(EvidenceWriteError) as info:
        write(tmp_path / "ev")
    assert info.value.code == "EVIDENCE_WRITE_FAILED" and info.value.staging_removed
    assert list((tmp_path / "ev").iterdir()) == []


def test_native_eexist_is_a_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def exists(_src: bytes, _dst: bytes) -> int:
        ctypes.set_errno(errno.EEXIST)
        return -1

    monkeypatch.setattr(supervisor, "platform_noreplace_rename", lambda: exists)
    with pytest.raises(EvidenceCollisionError, match="EVIDENCE_DESTINATION_EXISTS"):
        write(tmp_path / "ev")
    assert list((tmp_path / "ev").iterdir()) == []


def test_ordinary_rename_is_never_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("ordinary rename used")

    for name in ("rename", "replace", "renames"):
        monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(Path, "rename", forbidden)
    monkeypatch.setattr(Path, "replace", forbidden)
    if SUPPORTED:
        assert write(tmp_path / "ev").is_file()  # published by the native call alone
    monkeypatch.setattr(supervisor, "platform_noreplace_rename", lambda: None)
    with pytest.raises(EvidenceWriteError):  # unsupported: fails closed, no fallback
        EvidenceWriter(tmp_path / "ev2", run="run", require_git_ignored=False).write(
            exchanges(), {"ok": True}, [], secrets=(SECRET,)
        )


def test_harness_source_has_no_ordinary_rename_call() -> None:
    tree = ast.parse(Path(supervisor.__file__).read_text())
    calls = {
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    }  # fmt: skip
    assert not calls & {"os.rename", "os.replace", "os.renames", "shutil.move"}


def test_staging_is_never_published_under_its_own_name(tmp_path: Path) -> None:
    class Spy(FileOps):
        def rename(self, src: Path, dst: Path) -> None:
            assert src.name.startswith(STAGING_PREFIX) and not dst.name.startswith(".")
            super().rename(src, dst)

    if not SUPPORTED:  # pragma: no cover - unsupported platforms fail closed
        pytest.skip("no native no-replace rename here")
    assert write(tmp_path / "ev", Spy()).is_file()


class FakeFn:
    """Records the signature set on it and the arguments of each call."""

    def __init__(self) -> None:
        self.argtypes: list[Any] = []
        self.restype: Any = None
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> int:
        self.calls.append(args)
        return 0


@pytest.mark.parametrize(
    ("platform", "symbol", "argtypes", "expected"),
    [
        ("darwin", "renamex_np", [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint],
         (b"/s", b"/d", RENAME_EXCL)),
        ("linux", "renameat2",
         [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint],
         (AT_FDCWD, b"/s", AT_FDCWD, b"/d", RENAME_NOREPLACE)),
    ],
)  # fmt: skip
def test_binding_passes_the_reviewed_arguments_on_each_platform(
    platform: str, symbol: str, argtypes: list[Any], expected: tuple[Any, ...]
) -> None:
    """Portable check of both bindings (the Linux one cannot run natively on macOS)."""
    fn = FakeFn()
    lib = type("Lib", (), {symbol: fn})()
    bound = resolve_noreplace_rename(platform, lib)
    assert bound is not None and bound(b"/s", b"/d") == 0
    assert fn.argtypes == argtypes and fn.restype is ctypes.c_int
    assert fn.calls == [expected]
