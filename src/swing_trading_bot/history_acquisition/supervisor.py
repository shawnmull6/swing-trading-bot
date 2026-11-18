"""External process supervisor for ONE historical acquisition session (Steps 16.17B,
16.17B.1; RUNBOOK §16).

``python -m swing_trading_bot.history_acquisition.supervisor --plan-fingerprint <fp>
--probe-evidence <name> --authorization <label> --max-sessions <n> (--new-store |
--resume)`` launches exactly one ``python -I -m swing_trading_bot.history_acquisition
session ...`` child with those same, validated arguments and waits for it, under a hard
wall-clock limit. It adds nothing to the runner's authority:

* **One child, once.** Exactly one process is launched per invocation. It is never
  retried, restarted or resumed automatically, and ``status``, ``recover`` and
  ``publish`` are never run: those stay explicit operator commands.
* **Fixed command.** The child's argv is built here from validated values (the approved
  plan fingerprint only, the runner's own label and evidence-name patterns, an integer
  session count, passed unchanged — the runner enforces it against the limit sealed in
  the store — one of ``--new-store`` / ``--resume``) and passed as a list; no shell,
  no operator-supplied executable, module or extra argument. The runner has no budget
  argument, so 120 attempts / 300 s cannot be raised from here.
* **Hard limit.** :data:`SUPERVISOR_LIMIT` (300 s, the plan's session deadline) is
  measured on the monotonic clock from launch. When it expires the child's process
  group receives ``SIGTERM``; after :data:`TERMINATION_GRACE` a survivor gets
  ``SIGKILL``. Nothing else is launched; the result is ``SUPERVISOR_TIMEOUT``.
* **Process-group safety (16.17B.1).** The child is a session leader, so its process
  group id equals its pid. The child is **not reaped** until every group signal has been
  sent: its exit is observed without reaping (``kqueue`` ``NOTE_EXIT`` on macOS, a
  ``pidfd`` on Linux), so while it is a zombie its pid — and therefore the group id —
  stays reserved by the kernel and cannot name an unrelated group. Live descendants left
  in the group after the child exits get ``SIGTERM``, then ``SIGKILL``; any still alive
  after that are reported (``process_group_survivors``), never silently ignored. After
  the reap the group id is never signaled again.
* **Environment.** An allowlist: ``PATH``, ``HOME``, locale and temp-dir names, plus
  ``STB_ENV_FILE`` (required, absolute; passed through unread) and
  ``RUN_ALPACA_HISTORY_ACQUISITION=1`` (set by the operator, never here on their behalf).
  Any ``ALPACA_*`` / ``APCA_*`` name or credential setting name (``TRADING_MODE``,
  ``TRADING_ENABLED``, ...) is refused before launch, so the credential file stays the
  only source and its paper mode and disabled trading cannot be overridden. ``-I`` makes
  the child ignore ``PYTHON*`` variables and the user site. The credential file is never
  opened, printed or copied.
* **Output.** One JSON document. The child's stdout (a private temporary file) is parsed
  as the runner's report and only fixed fields are kept; stderr is counted, never
  relayed. Failures are fixed codes: ``REFUSED`` (authorization or configuration) and
  ``SUPERVISOR_ERROR`` (an internal failure); no exception text, environment value or
  credential is ever part of a result.
* **Recovery.** ``recovery_required`` is false only when the supervisor can show that the
  staging session closed (a valid runner report) or never began (a runner refusal raised
  before its journal entry, or nothing launched). Every other case — a timeout, a crash,
  survivors, an unknown refusal — is ``staging_state: UNCERTAIN``: inspect with
  ``status``; ``recover --label`` only if it reports an interrupted session. Staging is
  write-once, so a kill never turns a partial batch into a committed one.

Supported operating systems: macOS and Linux (kernel 5.3 or later, for ``pidfd``). Any
other platform is refused (``SUPERVISOR_ERROR`` ``PLATFORM_UNSUPPORTED``) before launch.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, Any, Final, NoReturn, Protocol

from swing_trading_bot.app.credentials import ENV_FILE_VARIABLE
from swing_trading_bot.config.settings import ENV_TO_FIELD
from swing_trading_bot.history_acquisition.runner import (
    _EVIDENCE_NAME,
    _LABEL,
    APPROVED_ACQUISITION_PLAN_FINGERPRINT,
    ENABLE_VARIABLE,
    REPO,
)
from swing_trading_bot.research.acquisition import build_acquisition_plan
from swing_trading_bot.research.staging import SESSION_LIMIT_RANGE

RUNNER_MODULE: Final = "swing_trading_bot.history_acquisition"
SUPERVISOR_LIMIT: Final = timedelta(seconds=300)
"""Hard wall-clock limit from launch (the plan's session deadline; never raised)."""
TERMINATION_GRACE: Final = timedelta(seconds=10)
"""Bounded cleanup interval between ``SIGTERM`` and ``SIGKILL``."""
REAP_LIMIT: Final = timedelta(seconds=5)
"""Bounded wait for processes to disappear after ``SIGKILL``."""
POLL_INTERVAL: Final = timedelta(milliseconds=50)
MAX_REPORT_BYTES: Final = 1 << 20
CHILD_ENV_KEEP: Final = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
_FORBIDDEN_PREFIXES: Final = ("ALPACA_", "APCA_")
REPORT_FIELDS: Final = ("status", "stop_code", "stop_reason", "stop_detail", "resumable",
                        "attempts_used", "failed_batch", "refused", "session")  # fmt: skip
#: Runner refusals that mean an EARLIER session was interrupted (``recover`` needed).
RECOVERY_REFUSALS: Final = frozenset({"PREVIOUS_SESSION_INTERRUPTED", "INTERRUPTED_SESSION",
                                      "SESSION_LOCKED"})  # fmt: skip
#: Runner refusals (``RunnerRefusedError``) raised before a session writes its journal
#: start, i.e. proof that no staging session began. Staging/dataset error codes are not
#: here: some arise after the journal start, so they are treated as uncertain.
PRE_SESSION_REFUSALS: Final = frozenset({
    "AUTHORIZATION_LABEL_REUSED", "CONDITIONAL_GATE_UNDETERMINED", "CORE_GATE_NOT_PASSED",
    "CREDENTIALS_UNAVAILABLE", "CREDENTIAL_SOURCE_NOT_FILE", "GATES_CHANGED_BETWEEN_SESSIONS",
    "INVALID_AUTHORIZATION_LABEL", "INVALID_MAX_SESSIONS", "INVALID_PROBE_EVIDENCE_NAME",
    "JOURNAL_INCONSISTENT", "JOURNAL_NOT_A_DIRECTORY", "JOURNAL_SESSION_LIMIT_MISMATCH",
    "JOURNAL_UNKNOWN_FILE", "JOURNAL_UNREADABLE", "LIVE_SESSION_NOT_ENABLED", "NOTHING_TO_FETCH",
    "NO_SESSION_JOURNAL", "PLAN_DRIFT", "PLAN_FINGERPRINT_MISMATCH",
    "PLAN_FINGERPRINT_NOT_AUTHORIZED", "PROBE_EVIDENCE_UNAVAILABLE", "PROBE_NOT_COMPLETED",
    "PROBE_PLAN_DRIFT", "PROBE_PLAN_MISMATCH", "PROBE_SUMMARY_INVALID",
    "REPOSITORY_NOT_FOUND", "RESEARCH_PATH_NOT_A_DIRECTORY", "RESEARCH_ROOT_NOT_ABSOLUTE",
    "RESEARCH_ROOT_NOT_APPROVED", "SESSION_ACCOUNTING_INCONSISTENT", "SESSION_LIMIT_MISMATCH",
    "SESSION_LIMIT_MISSING",
    "SESSION_LIMIT_REACHED", "STORE_EXISTS", "STORE_MISSING", "TRADING_ENABLED_REFUSED",
})  # fmt: skip

EXIT_COMPLETE: Final = 0
EXIT_INCOMPLETE: Final = 1
EXIT_REFUSED: Final = 2
EXIT_TIMEOUT: Final = 3
EXIT_CHILD_FAILED: Final = 4
EXIT_SUPERVISOR_ERROR: Final = 5
EXIT_INTERRUPTED: Final = 130
NEXT_STEP_INSPECT: Final = ("inspect with status; run recover --label only if status "
                            "reports an interrupted session")  # fmt: skip


class SupervisorRefusedError(RuntimeError):
    """``REFUSED``: an authorization or configuration failure; nothing was launched.
    ``code`` is fixed text; ``names`` lists offending variable names (never values)."""

    def __init__(self, code: str, names: Sequence[str] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.names = tuple(names)


class SupervisorError(RuntimeError):
    """``SUPERVISOR_ERROR``: an internal failure. ``launched`` says whether the child was
    started (then the staging state is uncertain)."""

    def __init__(self, code: str, *, launched: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.launched = launched


@dataclass(frozen=True, slots=True)
class SessionArgs:
    """The runner's existing ``session`` arguments, validated."""

    plan_fingerprint: str
    probe_evidence: str
    authorization: str
    max_sessions: int
    resume: bool

    def __post_init__(self) -> None:
        if self.plan_fingerprint != APPROVED_ACQUISITION_PLAN_FINGERPRINT:
            raise SupervisorRefusedError("PLAN_FINGERPRINT_NOT_AUTHORIZED")
        if not _EVIDENCE_NAME.match(self.probe_evidence):
            raise SupervisorRefusedError("INVALID_PROBE_EVIDENCE_NAME")
        if not _LABEL.match(self.authorization):
            raise SupervisorRefusedError("INVALID_AUTHORIZATION_LABEL")
        low, high = SESSION_LIMIT_RANGE
        if type(self.max_sessions) is not int or not low <= self.max_sessions <= high:
            raise SupervisorRefusedError("INVALID_MAX_SESSIONS")
        if type(self.resume) is not bool:
            raise SupervisorRefusedError("INVALID_MODE")

    def argv(self, executable: str = sys.executable) -> list[str]:
        """The child's complete argv: the runner's ``session`` command, nothing else."""
        return [executable, "-I", "-m", RUNNER_MODULE, "session",
                "--plan-fingerprint", self.plan_fingerprint,
                "--probe-evidence", self.probe_evidence,
                "--authorization", self.authorization,
                "--max-sessions", str(self.max_sessions),
                "--resume" if self.resume else "--new-store"]  # fmt: skip


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The child's environment (allowlist), or a refusal naming the offending variable
    names only. The credential file is passed through as a path; it is never opened."""
    bad = sorted(k for k in environ
                 if k in ENV_TO_FIELD or k.startswith(_FORBIDDEN_PREFIXES))  # fmt: skip
    if bad:
        raise SupervisorRefusedError("CREDENTIAL_ENVIRONMENT_OVERRIDE", bad)
    if environ.get(ENABLE_VARIABLE) != "1":
        raise SupervisorRefusedError("LIVE_SESSION_NOT_ENABLED")
    selected = environ.get(ENV_FILE_VARIABLE, "")
    if not selected.strip() or not Path(selected).is_absolute():
        raise SupervisorRefusedError("CREDENTIAL_FILE_NOT_SELECTED")
    env = {k: environ[k] for k in CHILD_ENV_KEEP if k in environ}
    env[ENV_FILE_VARIABLE] = selected
    env[ENABLE_VARIABLE] = "1"
    return env


# ===================================================================== the platform


class ExitWatch(Protocol):
    def wait(self, timeout: timedelta) -> bool:
        """Block up to ``timeout``; True once the child has exited. Never reaps it."""

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Platform:
    """The OS operations the supervisor uses (injectable for tests)."""

    watch: Callable[[int], ExitWatch]
    live_members: Callable[[int], bool]
    """Whether the group has a LIVE member; asked only while the leader is unreaped."""
    kill_group: Callable[[int, int], None]
    sleep: Callable[[timedelta], None]


def _sleep(delay: timedelta) -> None:
    time.sleep(max(delay, timedelta(0)).total_seconds())


def _seconds(td: timedelta) -> Any:  # noqa: ANN401 - the OS APIs take seconds
    return max(td, timedelta(0)).total_seconds()


if sys.platform == "darwin":

    class _KqueueWatch:
        """macOS: ``EVFILT_PROC``/``NOTE_EXIT`` reports the exit without reaping (a child that
        is already a zombie is reported at once)."""

        def __init__(self, pid: int) -> None:
            self._kq = select.kqueue()
            self._event: Any = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            self._exited = False

        def wait(self, timeout: timedelta) -> bool:
            if not self._exited:
                changes = None if self._event is None else [self._event]
                self._event = None  # registered once
                try:
                    self._exited = bool(self._kq.control(changes, 1, _seconds(timeout)))
                except ProcessLookupError:  # pragma: no cover - only after a reap, never here
                    self._exited = True
            return self._exited

        def close(self) -> None:
            self._kq.close()

    def _darwin_live_members(pgid: int) -> bool:
        """macOS answers ``EPERM`` for a group whose only member is a zombie."""
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True


if sys.platform == "linux":  # pragma: no cover - exercised on Linux only

    class _PidfdWatch:
        """Linux: a ``pidfd`` becomes readable when the process exits (no reap)."""

        def __init__(self, pid: int) -> None:
            self._fd = os.pidfd_open(pid)
            self._poll = select.poll()
            self._poll.register(self._fd, select.POLLIN)

        def wait(self, timeout: timedelta) -> bool:
            ms = max(timeout, timedelta(0)) // timedelta(milliseconds=1)
            return bool(self._poll.poll(ms))

        def close(self) -> None:
            os.close(self._fd)

    def _linux_live_members(pgid: int) -> bool:
        """Scan ``/proc`` for a non-zombie process in the group (``kill(-pgid, 0)``
        succeeds on Linux for a zombie-only group, so it cannot tell)."""
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                stat = Path(entry.path, "stat").read_text()
            except OSError:
                continue
            fields = stat[stat.rfind(")") + 2 :].split()
            if len(fields) > 2 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
                return True
        return False


def native_platform() -> Platform:
    """The supported OS mechanism, or ``SUPERVISOR_ERROR PLATFORM_UNSUPPORTED``."""
    system = str(sys.platform)  # not narrowed: both branches are type-checked
    if system == "darwin":
        return Platform(globals()["_KqueueWatch"], globals()["_darwin_live_members"],
                        os.killpg, _sleep)  # fmt: skip
    if system == "linux" and hasattr(os, "pidfd_open"):  # pragma: no cover
        return Platform(globals()["_PidfdWatch"], globals()["_linux_live_members"],
                        os.killpg, _sleep)  # fmt: skip
    raise SupervisorError("PLATFORM_UNSUPPORTED")  # pragma: no cover


# ===================================================================== the child

#: ``subprocess.Popen`` (or a test double with ``pid`` and ``wait``).
Popen = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    exit_code: int | None
    """The child's exit status (negative: the signal that ended it); ``None`` if unknown."""
    termination_requested: bool
    forced_termination: bool
    interrupted: bool
    """The supervisor itself was interrupted (``SIGINT``/``SIGTERM``) and stopped the child."""
    elapsed: timedelta
    stdout: bytes
    stderr_bytes: int
    descendants_found: bool = False
    """Live processes were still in the group after the child exited (and were signaled)."""
    survivors: bool = False
    """Some group member, or the child itself, was still alive at the end: reported."""


def _monotonic() -> timedelta:
    return timedelta(microseconds=time.monotonic_ns() // 1000)


@dataclass
class _Group:
    """Signals for ONE process group whose leader is unreaped (so its id is reserved)."""

    pgid: int
    platform: Platform
    monotonic: Callable[[], timedelta]
    reaped: bool = False

    def signal(self, sig: int) -> None:
        if self.reaped:  # pragma: no cover - a programming error, never a runtime path
            raise SupervisorError("SIGNAL_AFTER_REAP", launched=True)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            self.platform.kill_group(self.pgid, sig)

    def gone_within(self, limit: timedelta) -> bool:
        end = self.monotonic() + limit
        while self.platform.live_members(self.pgid):
            if self.monotonic() >= end:
                return False
            self.platform.sleep(POLL_INTERVAL)
        return True

    def clear(self, grace: timedelta, reap: timedelta) -> tuple[bool, bool]:
        """After the leader exited (unreaped): (descendants found, survivors left)."""
        if not self.platform.live_members(self.pgid):
            return False, False
        self.signal(signal.SIGTERM)
        if self.gone_within(grace):
            return True, False
        self.signal(signal.SIGKILL)
        return True, not self.gone_within(reap)


def _read(fh: IO[bytes]) -> tuple[bytes, int]:
    size = os.fstat(fh.fileno()).st_size
    fh.seek(0)
    return fh.read(MAX_REPORT_BYTES), size


def run_child(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    limit: timedelta = SUPERVISOR_LIMIT,
    grace: timedelta = TERMINATION_GRACE,
    reap: timedelta = REAP_LIMIT,
    cwd: Path = REPO,
    popen: Popen = subprocess.Popen,
    platform: Platform | None = None,
    monotonic: Callable[[], timedelta] = _monotonic,
) -> ChildOutcome:
    """Launch ONE child in its own process group and wait at most ``limit`` (never more
    than :data:`SUPERVISOR_LIMIT`). Not retried, not restarted."""
    if limit > SUPERVISOR_LIMIT or limit <= timedelta(0):
        raise SupervisorRefusedError("SUPERVISOR_LIMIT_NOT_ALLOWED")
    if grace <= timedelta(0) or grace > TERMINATION_GRACE or reap <= timedelta(0):
        raise SupervisorRefusedError("CLEANUP_INTERVAL_NOT_ALLOWED")
    ops = platform or native_platform()
    try:
        out_f = tempfile.TemporaryFile()  # noqa: SIM115 - closed below
        err_f = tempfile.TemporaryFile()  # noqa: SIM115 - closed below
    except OSError:
        raise SupervisorError("OUTPUT_CAPTURE_FAILED") from None
    with out_f, err_f:
        started = monotonic()
        try:
            proc = popen(list(argv), env=dict(env), cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=out_f, stderr=err_f, start_new_session=True,
                         close_fds=True, shell=False)  # fmt: skip
        except (OSError, ValueError, subprocess.SubprocessError):
            raise SupervisorError("CHILD_LAUNCH_FAILED") from None
        return _supervise_child(proc, ops, started, limit, grace, reap, monotonic,
                                out_f, err_f)  # fmt: skip


def _supervise_child(proc: Any, ops: Platform, started: timedelta, limit: timedelta,  # noqa: ANN401, PLR0917
                     grace: timedelta, reap: timedelta, monotonic: Callable[[], timedelta],
                     out_f: IO[bytes], err_f: IO[bytes]) -> ChildOutcome:  # fmt: skip
    group = _Group(proc.pid, ops, monotonic)  # a session leader: pid == process group id
    requested = forced = interrupted = found = survivors = False
    try:
        watch = ops.watch(proc.pid)
    except OSError:
        group.signal(signal.SIGKILL)
        raise SupervisorError("CHILD_WATCH_FAILED", launched=True) from None
    try:
        try:
            exited = watch.wait(limit - (monotonic() - started))
        except KeyboardInterrupt:
            exited, interrupted = False, True
        if not exited:
            requested = True
            group.signal(signal.SIGTERM)
            exited = watch.wait(grace)
            if not exited:
                forced = True
                group.signal(signal.SIGKILL)
                exited = watch.wait(reap)
        if exited:  # the leader is a zombie: the group id is still reserved
            found, survivors = group.clear(grace, reap)
        else:  # stuck even after SIGKILL (e.g. uninterruptible I/O): reported
            survivors = True
    except BaseException:
        group.signal(signal.SIGKILL)  # never leave the group behind on an internal error
        raise
    finally:
        watch.close()
    group.reaped = True  # from here on the group id is never signaled again
    try:
        code: int | None = proc.wait(timeout=_seconds(reap))
    except subprocess.TimeoutExpired:
        code = None
    out, _ = _read(out_f)
    _, err_size = _read(err_f)
    return ChildOutcome(code, requested, forced, interrupted, monotonic() - started, out,
                        err_size, found, survivors)  # fmt: skip


# ===================================================================== the result


def _report(stdout: bytes) -> dict[str, object] | None:
    try:
        doc = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    kept: dict[str, object] = {}
    for key in REPORT_FIELDS:
        value = doc.get(key)
        if value is None or isinstance(value, bool | int) or (
                isinstance(value, str) and len(value) <= 128):  # fmt: skip
            kept[key] = value
    return kept


def _classify(outcome: ChildOutcome, report: dict[str, object] | None
              ) -> tuple[str, int, str]:  # fmt: skip
    """(result, exit code, staging state)."""
    status = None if report is None else report.get("status")
    refused = None if report is None else report.get("refused")
    code = outcome.exit_code
    if outcome.interrupted:
        return "SUPERVISOR_INTERRUPTED", EXIT_INTERRUPTED, "UNCERTAIN"
    if outcome.termination_requested:
        return "SUPERVISOR_TIMEOUT", EXIT_TIMEOUT, "UNCERTAIN"
    if code == EXIT_COMPLETE and status == "COMPLETE":
        return "COMPLETE", EXIT_COMPLETE, "CLOSED"
    if code == EXIT_INCOMPLETE and status in ("INCOMPLETE", "STOPPED"):
        return str(status), EXIT_INCOMPLETE, "CLOSED"
    if code == EXIT_REFUSED and isinstance(refused, str):
        state = ("NOT_STARTED" if refused in PRE_SESSION_REFUSALS
                 else "INTERRUPTED_EARLIER" if refused in RECOVERY_REFUSALS
                 else "UNCERTAIN")  # fmt: skip
        return "REFUSED", EXIT_REFUSED, state
    return "CHILD_FAILED", EXIT_CHILD_FAILED, "UNCERTAIN"


def summarize(outcome: ChildOutcome) -> tuple[int, dict[str, object]]:
    """(supervisor exit code, sanitized result). Recovery is never ruled out unless the
    staging session provably closed or never began."""
    report = None if outcome.termination_requested else _report(outcome.stdout)
    result, exit_code, state = _classify(outcome, report)
    if outcome.survivors:
        state = "UNCERTAIN"  # a live process may still touch staging
    recovery = state not in {"NOT_STARTED", "CLOSED"}
    return exit_code, {
        "supervisor": "history-acquisition", "result": result, "code": None,
        "launched": True, "sessions_launched": 1, "staging_state": state,
        "child_exit_status": outcome.exit_code,
        "termination_requested": outcome.termination_requested,
        "forced_termination": outcome.forced_termination,
        "descendants_terminated": outcome.descendants_found,
        "process_group_survivors": outcome.survivors,
        "elapsed_s": round(outcome.elapsed.total_seconds(), 3),
        "limit_s": int(SUPERVISOR_LIMIT.total_seconds()),
        "recovery_required": recovery,
        "status_inspection_required": recovery,
        "next_step": NEXT_STEP_INSPECT if recovery else None,
        "session_report": report, "stderr_bytes": outcome.stderr_bytes,
    }  # fmt: skip


def failure(result: str, code: str, *, launched: bool = False,
            names: Sequence[str] = ()) -> tuple[int, dict[str, object]]:  # fmt: skip
    """A ``REFUSED`` or ``SUPERVISOR_ERROR`` result: fixed codes, no exception text."""
    state = "UNCERTAIN" if launched else "NOT_STARTED"
    doc: dict[str, object] = {
        "supervisor": "history-acquisition", "result": result, "code": code,
        "launched": launched, "sessions_launched": 1 if launched else 0,
        "staging_state": state, "recovery_required": launched,
        "status_inspection_required": launched,
        "next_step": NEXT_STEP_INSPECT if launched else None,
    }  # fmt: skip
    if names:
        doc["variables"] = list(names)
    return (EXIT_REFUSED if result == "REFUSED" else EXIT_SUPERVISOR_ERROR), doc


Launch = Callable[[Sequence[str], Mapping[str, str]], ChildOutcome]


def _verify_plan() -> None:
    try:
        fingerprint = build_acquisition_plan().fingerprint
    except Exception:  # noqa: BLE001 - reported as a fixed code, never as text
        raise SupervisorError("PLAN_VERIFICATION_FAILED") from None
    if fingerprint != APPROVED_ACQUISITION_PLAN_FINGERPRINT:
        raise SupervisorRefusedError("PLAN_DRIFT")


def supervise(args: SessionArgs, environ: Mapping[str, str], *,
              launch: Launch = run_child) -> tuple[int, dict[str, object]]:  # fmt: skip
    """Every pre-launch check, then exactly one launch."""
    _verify_plan()
    env = child_environment(environ)
    return summarize(launch(args.argv(), env))


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message  # it may echo an argument value
        raise SupervisorRefusedError("INVALID_ARGUMENTS")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="python -m swing_trading_bot.history_acquisition.supervisor",
        description="Run ONE authorized acquisition session under a hard 300 s limit.",
    )
    p.add_argument("--plan-fingerprint", required=True)
    p.add_argument("--probe-evidence", required=True)
    p.add_argument("--authorization", required=True)
    p.add_argument("--max-sessions", type=int, required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new-store", action="store_true")
    mode.add_argument("--resume", action="store_true")
    return p


def _interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None,
         *, launch: Launch = run_child, stdout: IO[str] | None = None) -> int:  # fmt: skip
    """CLI entry point. Exit 0 complete, 1 incomplete/stopped, 2 refused, 3 supervisor
    timeout, 4 child failed, 5 supervisor error, 130 supervisor interrupted."""
    out = sys.stdout if stdout is None else stdout
    env = os.environ if environ is None else environ
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        ns = _parser().parse_args(argv)
        args = SessionArgs(ns.plan_fingerprint, ns.probe_evidence, ns.authorization,
                           ns.max_sessions, bool(ns.resume))  # fmt: skip
        code, result = supervise(args, env, launch=launch)
    except SupervisorRefusedError as exc:
        code, result = failure("REFUSED", exc.code, names=exc.names)
    except SupervisorError as exc:
        code, result = failure("SUPERVISOR_ERROR", exc.code, launched=exc.launched)
    except KeyboardInterrupt:
        code, result = EXIT_INTERRUPTED, failure("SUPERVISOR_ERROR", "INTERRUPTED")[1]
    except Exception:  # noqa: BLE001 - never a traceback with values in it
        code, result = failure("SUPERVISOR_ERROR", "INTERNAL_ERROR")
    finally:
        signal.signal(signal.SIGTERM, previous)
    out.write(json.dumps(result, indent=1) + "\n")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CHILD_ENV_KEEP",
    "PRE_SESSION_REFUSALS",
    "RECOVERY_REFUSALS",
    "REPORT_FIELDS",
    "RUNNER_MODULE",
    "SUPERVISOR_LIMIT",
    "TERMINATION_GRACE",
    "ChildOutcome",
    "ExitWatch",
    "Platform",
    "SessionArgs",
    "SupervisorError",
    "SupervisorRefusedError",
    "child_environment",
    "failure",
    "main",
    "native_platform",
    "run_child",
    "summarize",
    "supervise",
]
