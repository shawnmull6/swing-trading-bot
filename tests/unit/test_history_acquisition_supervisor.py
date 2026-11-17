"""The acquisition supervisor (Steps 16.17B, 16.17B.1): one child, a hard 300 s limit,
process-group termination that never signals a reaped group id, structured refusals and
errors, conservative recovery diagnostics, an environment allowlist, sanitized output and
no automatic restart, recovery or publication. Every child is SYNTHETIC
(``tests/support/acquisition_child.py``); nothing here reaches a network, a credential or
a ledger, and the supervisor is never run against live settings."""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.history_acquisition import runner as runner_mod
from swing_trading_bot.history_acquisition import supervisor as sup
from swing_trading_bot.history_acquisition.runner import (
    APPROVED_ACQUISITION_PLAN_FINGERPRINT,
    ResearchPaths,
    RunnerRefusedError,
    recover,
)
from swing_trading_bot.history_acquisition.supervisor import (
    PRE_SESSION_REFUSALS,
    RECOVERY_REFUSALS,
    SUPERVISOR_LIMIT,
    TERMINATION_GRACE,
    ChildOutcome,
    Platform,
    SessionArgs,
    SupervisorError,
    SupervisorRefusedError,
    child_environment,
    native_platform,
    run_child,
    summarize,
    supervise,
)
from swing_trading_bot.research.staging import LOCK_FILE, StagingStore
from tests.unit import test_history_acquisition_runner as rt

CHILD = Path(__file__).resolve().parents[1] / "support" / "acquisition_child.py"
SRC = Path(sup.__file__)
FP = APPROVED_ACQUISITION_PLAN_FINGERPRINT
EVIDENCE = "history-probe-20261117T025839Z"
SECRET = "PKSUPERVISORLEAK0000001"
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home", "LANG": "C.UTF-8",
       "STB_ENV_FILE": "/nonexistent/paper.env", "RUN_ALPACA_HISTORY_ACQUISITION": "1",
       "PYTHONPATH": "/elsewhere", "AWS_SECRET_ACCESS_KEY": "x", "SHELL": "/bin/zsh"}  # fmt: skip
CLI = ["--plan-fingerprint", FP, "--probe-evidence", EVIDENCE, "--authorization", "auth-1",
       "--max-sessions", "2", "--new-store"]  # fmt: skip
SHORT = timedelta(seconds=1)
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS process semantics")


def args(**over: Any) -> SessionArgs:
    base: dict[str, Any] = {
        "plan_fingerprint": FP,
        "probe_evidence": EVIDENCE,
        "authorization": "auth-1",
        "max_sessions": 2,
        "resume": False,
    }
    base.update(over)
    return SessionArgs(**base)


def child(mode: str, log: Path, *extra: str) -> list[str]:
    return [sys.executable, str(CHILD), mode, str(log), *extra]


def launches(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int, within: timedelta = timedelta(seconds=5)) -> bool:
    end = time.monotonic() + within.total_seconds()
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def run_main(argv: Sequence[str], env: Mapping[str, str], **kw: Any) -> tuple[int, Any, str]:
    buf = io.StringIO()
    code = sup.main(argv, env, stdout=buf, **kw)
    return code, json.loads(buf.getvalue()), buf.getvalue()


@dataclass
class Recorder:
    """A ``launch`` that records the real argv/env and runs a synthetic child instead."""

    argv: list[str]
    limit: timedelta = timedelta(seconds=20)
    grace: timedelta = TERMINATION_GRACE
    seen: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> ChildOutcome:
        self.seen.append((list(argv), dict(env)))
        merged = {**env, "PYTHONPATH": str(CHILD.parents[2])}
        return run_child(self.argv, merged, limit=self.limit, grace=self.grace)


# ============================================================ arguments and command
def test_the_child_gets_exactly_the_existing_session_arguments() -> None:
    assert args().argv("/py") == [
        "/py", "-I", "-m", "swing_trading_bot.history_acquisition", "session",
        "--plan-fingerprint", FP, "--probe-evidence", EVIDENCE, "--authorization", "auth-1",
        "--max-sessions", "2", "--new-store"]  # fmt: skip
    assert args(resume=True).argv("/py")[-1] == "--resume"


@pytest.mark.parametrize(
    ("over", "code"),
    [({"plan_fingerprint": "0" * 64}, "PLAN_FINGERPRINT_NOT_AUTHORIZED"),
     ({"probe_evidence": "../evidence"}, "INVALID_PROBE_EVIDENCE_NAME"),
     ({"authorization": "--max-sessions=9"}, "INVALID_AUTHORIZATION_LABEL"),
     ({"authorization": "a; rm -rf /"}, "INVALID_AUTHORIZATION_LABEL"),
     ({"authorization": "$(id)"}, "INVALID_AUTHORIZATION_LABEL"),
     ({"max_sessions": 0}, "INVALID_MAX_SESSIONS"),
     ({"max_sessions": 10}, "INVALID_MAX_SESSIONS"),
     ({"max_sessions": True}, "INVALID_MAX_SESSIONS"),
     ({"resume": "yes"}, "INVALID_MODE")],
)  # fmt: skip
def test_arguments_are_validated_before_anything_is_launched(
    over: dict[str, Any], code: str
) -> None:
    with pytest.raises(SupervisorRefusedError) as err:
        args(**over)
    assert err.value.code == code


@pytest.mark.parametrize(
    "argv",
    [[], ["--max-sessions", f"two-{SECRET}"],
     [*CLI[:-1]],  # neither --new-store nor --resume
     [*CLI, "--resume"],  # both
     [*CLI, "--limit", "999"], [*CLI, "--", "sh"], [*CLI[:-3], "--new-store"]],
)  # fmt: skip
def test_invalid_cli_arguments_are_a_structured_refusal(argv: list[str], tmp_path: Path) -> None:
    rec = Recorder(child("complete", tmp_path / "log"))
    code, doc, text = run_main(argv, ENV, launch=rec)
    assert (code, doc["result"], doc["code"]) == (2, "REFUSED", "INVALID_ARGUMENTS")
    assert doc["launched"] is False and doc["sessions_launched"] == 0 and rec.seen == []
    assert doc["recovery_required"] is False and SECRET not in text


def test_a_refused_label_never_reaches_a_child(tmp_path: Path) -> None:
    rec = Recorder(child("complete", tmp_path / "log"))
    argv = [*CLI[:5], "$(touch pwned)", *CLI[6:]]
    code, doc, _ = run_main(argv, ENV, launch=rec)
    assert (code, doc["code"]) == (2, "INVALID_AUTHORIZATION_LABEL") and rec.seen == []


def test_the_cli_takes_no_command_budget_or_path_argument() -> None:
    options = {a.dest for a in sup._parser()._actions}
    assert options == {"help", "plan_fingerprint", "probe_evidence", "authorization",
                       "max_sessions", "new_store", "resume"}  # fmt: skip


def test_the_limit_can_never_exceed_300_seconds(tmp_path: Path) -> None:
    assert timedelta(seconds=rt.plan().session_deadline_s) == SUPERVISOR_LIMIT
    for limit in (SUPERVISOR_LIMIT + timedelta(microseconds=1), timedelta(0)):
        with pytest.raises(SupervisorRefusedError, match="SUPERVISOR_LIMIT_NOT_ALLOWED"):
            run_child(child("complete", tmp_path / "log"), {}, limit=limit)
    with pytest.raises(SupervisorRefusedError, match="CLEANUP_INTERVAL_NOT_ALLOWED"):
        run_child(child("complete", tmp_path / "log"), {},
                  grace=TERMINATION_GRACE + timedelta(seconds=1))  # fmt: skip
    assert launches(tmp_path / "log") == []


def test_the_supervisor_launches_without_a_shell() -> None:
    tree = ast.parse(SRC.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    used = {f"{n.value.id}.{n.attr}" for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id in ("os", "subprocess")}  # fmt: skip
    assert {u for u in used if u.startswith("subprocess.")} <= {
        "subprocess.Popen", "subprocess.DEVNULL", "subprocess.TimeoutExpired",
        "subprocess.SubprocessError"}  # fmt: skip
    assert not used & {"os.system", "os.popen", "os.execv", "os.execvp", "os.spawnv",
                       "os.fork", "os.kill"}  # fmt: skip
    assert not {getattr(c.func, "id", "") for c in calls} & {"eval", "exec", "compile",
                                                            "__import__"}  # fmt: skip
    shell = [k for c in calls for k in c.keywords if k.arg == "shell"]
    assert shell and all(isinstance(k.value, ast.Constant) and k.value.value is False
                         for k in shell)  # fmt: skip


def test_only_the_supervisor_may_start_processes() -> None:
    for path in sorted(SRC.parent.glob("*.py")):
        if path.name != "supervisor.py":
            text = path.read_text()
            assert "subprocess" not in text and "os.fork" not in text, path.name


# ============================================================ environment and credentials
def test_the_child_environment_is_an_allowlist() -> None:
    assert child_environment(ENV) == {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home", "LANG": "C.UTF-8",
        "STB_ENV_FILE": "/nonexistent/paper.env",
        "RUN_ALPACA_HISTORY_ACQUISITION": "1"}  # fmt: skip


@pytest.mark.parametrize(
    "name", ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL", "APCA_API_KEY_ID",
             "TRADING_MODE", "TRADING_ENABLED", "INITIAL_CAPITAL", "MAX_CAPITAL_ALLOCATION"],
)  # fmt: skip
def test_credential_overrides_are_refused_by_name_only(name: str, tmp_path: Path) -> None:
    rec = Recorder(child("complete", tmp_path / "log"))
    code, doc, text = run_main(CLI, {**ENV, name: SECRET}, launch=rec)
    assert (code, doc["result"]) == (2, "REFUSED")
    assert doc["code"] == "CREDENTIAL_ENVIRONMENT_OVERRIDE" and doc["variables"] == [name]
    assert SECRET not in text and rec.seen == []


@pytest.mark.parametrize(
    ("change", "code"),
    [({"RUN_ALPACA_HISTORY_ACQUISITION": "0"}, "LIVE_SESSION_NOT_ENABLED"),
     ({"RUN_ALPACA_HISTORY_ACQUISITION": None}, "LIVE_SESSION_NOT_ENABLED"),
     ({"STB_ENV_FILE": None}, "CREDENTIAL_FILE_NOT_SELECTED"),
     ({"STB_ENV_FILE": " "}, "CREDENTIAL_FILE_NOT_SELECTED"),
     ({"STB_ENV_FILE": "relative/paper.env"}, "CREDENTIAL_FILE_NOT_SELECTED")],
)  # fmt: skip
def test_missing_authorization_configuration_is_refused(
    change: dict[str, str | None], code: str, tmp_path: Path
) -> None:
    env = {k: v for k, v in {**ENV, **change}.items() if v is not None}
    rec = Recorder(child("complete", tmp_path / "log"))
    got, doc, text = run_main(CLI, env, launch=rec)
    assert (got, doc["result"], doc["code"]) == (2, "REFUSED", code) and rec.seen == []
    assert doc["staging_state"] == "NOT_STARTED" and "paper.env" not in text


def test_the_running_child_sees_only_the_allowlist(tmp_path: Path) -> None:
    out = tmp_path / "env.json"
    rec = Recorder(child("env", tmp_path / "log", str(out)))
    code, result = supervise(args(), ENV, launch=rec)
    assert code == 0 and result["result"] == "COMPLETE"
    seen = set(json.loads(out.read_text()))
    assert not any(n.startswith(("ALPACA_", "APCA_", "TRADING_")) for n in seen)
    assert {"STB_ENV_FILE", "RUN_ALPACA_HISTORY_ACQUISITION"} <= seen
    assert "AWS_SECRET_ACCESS_KEY" not in seen and "SHELL" not in seen


def test_the_credential_file_is_never_opened(tmp_path: Path) -> None:
    cred = tmp_path / "paper.env"
    cred.write_text(f"ALPACA_API_KEY={SECRET}\n")
    cred.chmod(0o600)
    opened: list[str] = []

    def hook(event: str, a: tuple[Any, ...]) -> None:
        if event == "open" and a and str(a[0]) == str(cred):
            opened.append(event)

    sys.addaudithook(hook)
    rec = Recorder(child("complete", tmp_path / "log"))
    code, _, text = run_main(CLI, {**ENV, "STB_ENV_FILE": str(cred)}, launch=rec)
    assert code == 0 and opened == [] and SECRET not in text
    assert rec.seen[0][1]["STB_ENV_FILE"] == str(cred)


def test_child_output_is_reduced_to_fixed_fields(tmp_path: Path) -> None:
    rec = Recorder(child("leak", tmp_path / "log", SECRET))
    code, result = supervise(args(), ENV, launch=rec)
    assert code == 0 and SECRET not in json.dumps(result)
    assert isinstance(result["stderr_bytes"], int) and result["stderr_bytes"] > 0  # counted
    report = result["session_report"]
    assert isinstance(report, dict) and set(report) <= set(sup.REPORT_FIELDS)


# ============================================================ structured pre-launch failures
def test_a_plan_verification_exception_is_a_supervisor_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise RuntimeError(f"cannot build plan: {SECRET}")

    monkeypatch.setattr(sup, "build_acquisition_plan", broken)
    rec = Recorder(child("complete", tmp_path / "log"))
    code, doc, text = run_main(CLI, ENV, launch=rec)
    assert (code, doc["result"]) == (5, "SUPERVISOR_ERROR")
    assert doc["code"] == "PLAN_VERIFICATION_FAILED" and doc["launched"] is False
    assert rec.seen == [] and SECRET not in text
    assert "Traceback" not in text and "RuntimeError" not in text


def test_a_drifted_plan_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sup, "build_acquisition_plan",
                        lambda: type("P", (), {"fingerprint": "0"}))  # fmt: skip
    rec = Recorder(child("complete", tmp_path / "log"))
    code, doc, _ = run_main(CLI, ENV, launch=rec)
    assert (code, doc["result"], doc["code"]) == (2, "REFUSED", "PLAN_DRIFT") and rec.seen == []


@pytest.mark.parametrize(
    "error", [FileNotFoundError(2, f"no such file: {SECRET}"),
              PermissionError(13, SECRET), OSError(f"fork failed {SECRET}")],
)  # fmt: skip
def test_a_child_launch_failure_is_a_supervisor_error(error: OSError) -> None:
    calls: list[list[str]] = []

    def popen(argv: list[str], **_kw: Any) -> Any:
        calls.append(argv)
        raise error

    def launch(argv: Sequence[str], env: Mapping[str, str]) -> ChildOutcome:
        return run_child(argv, env, popen=popen)

    code, doc, text = run_main(CLI, ENV, launch=launch)
    assert (code, doc["result"], doc["code"]) == (5, "SUPERVISOR_ERROR", "CHILD_LAUNCH_FAILED")
    assert doc["launched"] is False and doc["recovery_required"] is False
    assert len(calls) == 1 and SECRET not in text  # one attempt, never retried


def test_a_real_missing_executable_is_a_launch_failure(tmp_path: Path) -> None:
    with pytest.raises(SupervisorError) as err:
        run_child([str(tmp_path / "missing-python")], {})
    assert err.value.code == "CHILD_LAUNCH_FAILED" and err.value.launched is False


def test_an_unexpected_internal_error_has_no_traceback() -> None:
    def launch(_argv: Sequence[str], _env: Mapping[str, str]) -> ChildOutcome:
        raise KeyError(SECRET)

    code, doc, text = run_main(CLI, ENV, launch=launch)
    assert (code, doc["result"], doc["code"]) == (5, "SUPERVISOR_ERROR", "INTERNAL_ERROR")
    assert SECRET not in text


def test_an_error_after_launch_is_reported_as_uncertain() -> None:
    _, doc = sup.failure("SUPERVISOR_ERROR", "CHILD_WATCH_FAILED", launched=True)
    assert doc["staging_state"] == "UNCERTAIN" and doc["recovery_required"] is True
    assert doc["next_step"] == sup.NEXT_STEP_INSPECT and doc["sessions_launched"] == 1


# ============================================================ results
def test_normal_completion(tmp_path: Path) -> None:
    log = tmp_path / "log"
    rec = Recorder(child("complete", log))
    code, result = supervise(args(), ENV, launch=rec)
    assert code == 0 and len(rec.seen) == 1 and len(launches(log)) == 1
    assert result["result"] == "COMPLETE" and result["staging_state"] == "CLOSED"
    assert result["child_exit_status"] == 0 and result["recovery_required"] is False
    assert result["termination_requested"] is False and result["forced_termination"] is False
    assert result["descendants_terminated"] is False
    assert result["process_group_survivors"] is False and result["sessions_launched"] == 1
    elapsed = result["elapsed_s"]
    assert isinstance(elapsed, int | float) and 0 <= elapsed < 20


def test_a_runner_refusal_before_the_session_is_not_a_recovery(tmp_path: Path) -> None:
    log = tmp_path / "log"
    rec = Recorder(child("refused", log, "NOTHING_TO_FETCH"))
    code, result = supervise(args(), ENV, launch=rec)
    assert code == 2 and result["result"] == "REFUSED" and len(launches(log)) == 1
    assert result["staging_state"] == "NOT_STARTED" and result["recovery_required"] is False


def test_an_incomplete_session_is_resumable_and_not_resumed(tmp_path: Path) -> None:
    log = tmp_path / "log"
    code, result = supervise(args(), ENV, launch=Recorder(child("incomplete", log)))
    assert code == 1 and result["result"] == "INCOMPLETE" and len(launches(log)) == 1
    report = result["session_report"]
    assert isinstance(report, dict) and report["resumable"] is True
    assert result["staging_state"] == "CLOSED" and result["recovery_required"] is False


def test_a_crashed_child_is_a_failure_not_a_result(tmp_path: Path) -> None:
    code, result = supervise(args(), ENV, launch=Recorder(child("crash", tmp_path / "log")))
    assert code == 4 and result["result"] == "CHILD_FAILED" and result["session_report"] is None
    assert result["staging_state"] == "UNCERTAIN" and result["recovery_required"] is True
    assert result["next_step"] == sup.NEXT_STEP_INSPECT


def _outcome(code: int | None, stdout: bytes = b"", **kw: Any) -> ChildOutcome:
    return ChildOutcome(code, kw.pop("requested", False), kw.pop("forced", False),
                        kw.pop("interrupted", False), SHORT, stdout, 0, **kw)  # fmt: skip


@pytest.mark.parametrize(
    ("outcome", "exit_code", "result", "state", "recovery"),
    [(_outcome(0, b'{"status": "COMPLETE"}'), 0, "COMPLETE", "CLOSED", False),
     (_outcome(1, b'{"status": "STOPPED"}'), 1, "STOPPED", "CLOSED", False),
     (_outcome(1, b'{"status": "INCOMPLETE"}'), 1, "INCOMPLETE", "CLOSED", False),
     (_outcome(2, b'{"refused": "STORE_EXISTS"}'), 2, "REFUSED", "NOT_STARTED", False),
     (_outcome(2, b'{"refused": "CREDENTIALS_UNAVAILABLE"}'), 2, "REFUSED", "NOT_STARTED",
      False),
     (_outcome(2, b'{"refused": "PREVIOUS_SESSION_INTERRUPTED"}'), 2, "REFUSED",
      "INTERRUPTED_EARLIER", True),
     # a staging error can be raised after the journal start: never ruled out
     (_outcome(2, b'{"refused": "TAMPERED_BATCH"}'), 2, "REFUSED", "UNCERTAIN", True),
     (_outcome(2, b'{"refused": "BUDGET_ABOVE_PLAN"}'), 2, "REFUSED", "UNCERTAIN", True),
     (_outcome(0, b"not json"), 4, "CHILD_FAILED", "UNCERTAIN", True),
     (_outcome(0, b'{"status": "STOPPED"}'), 4, "CHILD_FAILED", "UNCERTAIN", True),
     (_outcome(-9), 4, "CHILD_FAILED", "UNCERTAIN", True),
     (_outcome(None), 4, "CHILD_FAILED", "UNCERTAIN", True),
     (_outcome(-15, requested=True), 3, "SUPERVISOR_TIMEOUT", "UNCERTAIN", True),
     (_outcome(-9, requested=True, forced=True), 3, "SUPERVISOR_TIMEOUT", "UNCERTAIN", True),
     (_outcome(-15, requested=True, interrupted=True), 130, "SUPERVISOR_INTERRUPTED",
      "UNCERTAIN", True),
     # a valid report but a surviving group member: the store may still be touched
     (_outcome(0, b'{"status": "COMPLETE"}', survivors=True), 0, "COMPLETE", "UNCERTAIN",
      True)],
)  # fmt: skip
def test_recovery_is_ruled_out_only_when_provable(
    outcome: ChildOutcome, exit_code: int, result: str, state: str, recovery: bool
) -> None:
    code, got = summarize(outcome)
    assert (code, got["result"], got["staging_state"]) == (exit_code, result, state)
    assert got["recovery_required"] is recovery
    assert got["status_inspection_required"] is recovery
    assert got["next_step"] == (sup.NEXT_STEP_INSPECT if recovery else None)


RECOVER_PUBLISH_ONLY = {"DATASET_EXISTS", "INVALID_DATASET_ID", "NOTHING_TO_RECOVER",
                        "RECOVERY_LABEL_USED", "INVALID_RECOVERY_LABEL",
                        "PUBLISHED_DATASET_NOT_ACQUISITION_COMPLETE"}  # fmt: skip


def test_pre_session_refusals_are_exactly_the_runners_pre_journal_refusals() -> None:
    """Every runner refusal code is classified, and none can follow the journal start."""
    pattern = r'(?:RunnerRefusedError|LiveConnectionRefusedError)\("([A-Z_]+)"'
    codes: set[str] = set()
    for name in ("runner.py", "transport.py"):
        codes |= set(re.findall(pattern, (SRC.parent / name).read_text()))
    assert codes - RECOVER_PUBLISH_ONLY - RECOVERY_REFUSALS == PRE_SESSION_REFUSALS
    body = inspect.getsource(runner_mod.run_authorized_session)
    after_start = body[body.index("journal.start(") :]
    assert "RunnerRefusedError" not in after_start and "connect(" not in after_start
    assert "run_session(" in after_start


# ============================================================ process lifecycle
@dataclass
class FakeChild:
    """A child that never exits on its own; records every OS operation in order."""

    clock: list[timedelta]
    events: list[str]
    survive_term: bool = True
    pid: int = 4242
    exited: bool = False
    leftover: int = 0
    """Live descendants still in the group after the child exits."""

    def platform(self) -> Platform:
        fake = self

        class Watch:
            def wait(self, timeout: timedelta) -> bool:
                fake.events.append("wait")
                if not fake.exited:
                    fake.clock[0] += timeout
                return fake.exited

            def close(self) -> None:
                fake.events.append("close")

        def kill(pgid: int, sig: int) -> None:
            assert pgid == fake.pid
            fake.events.append(f"kill:{signal.Signals(sig).name}")
            if sig == signal.SIGKILL:
                fake.exited, fake.leftover = True, 0
            elif not fake.survive_term:
                fake.exited = True

        def live(_pgid: int) -> bool:
            fake.events.append("probe")
            return fake.leftover > 0

        def sleep(d: timedelta) -> None:
            fake.clock[0] += d

        return Platform(lambda _pid: Watch(), live, kill, sleep)

    def wait(self, timeout: Any = None) -> int:
        del timeout
        self.events.append("reap")
        return -9 if "kill:SIGKILL" in self.events else 0


def _fake_run(fake: FakeChild, platform: Platform | None = None) -> ChildOutcome:
    launched: list[list[str]] = []

    def popen(argv: list[str], **opts: Any) -> FakeChild:
        launched.append(argv)
        assert opts["start_new_session"] is True and opts["shell"] is False
        return fake

    out = run_child(["child"], {}, popen=popen, platform=platform or fake.platform(),
                    monotonic=lambda: fake.clock[0])  # fmt: skip
    assert len(launched) == 1
    return out


def _no_signal_after_reap(events: list[str]) -> None:
    assert events.count("reap") == 1
    after = events[events.index("reap") :]
    assert not [e for e in after if e.startswith(("kill", "probe"))], events


def test_a_child_exceeding_300_seconds_is_terminated_then_killed() -> None:
    fake = FakeChild([timedelta(seconds=1000)], [])
    out = _fake_run(fake)
    assert fake.events[:2] == ["wait", "kill:SIGTERM"]
    assert "kill:SIGKILL" in fake.events
    assert out.termination_requested and out.forced_termination and out.exit_code == -9
    assert out.elapsed >= SUPERVISOR_LIMIT + TERMINATION_GRACE
    _no_signal_after_reap(fake.events)
    code, result = summarize(out)
    assert (code, result["result"], result["staging_state"]) == (
        3, "SUPERVISOR_TIMEOUT", "UNCERTAIN")  # fmt: skip
    assert result["recovery_required"] is True and result["session_report"] is None


def test_descendants_are_signaled_before_the_reap_never_after() -> None:
    """Group-id reuse safety: every group signal and probe precedes the single reap."""
    fake = FakeChild([timedelta(0)], [], exited=True, leftover=1)
    out = _fake_run(fake)
    assert out.descendants_found and not out.survivors
    assert fake.events.index("kill:SIGKILL") < fake.events.index("reap")
    _no_signal_after_reap(fake.events)


def test_surviving_descendants_are_reported_not_abandoned_silently() -> None:
    fake = FakeChild([timedelta(0)], [], exited=True, leftover=1)
    plat = fake.platform()

    def stubborn(_pgid: int) -> bool:
        fake.events.append("probe")
        return True

    out = _fake_run(fake, Platform(plat.watch, stubborn, plat.kill_group, plat.sleep))
    assert out.survivors and out.descendants_found
    assert summarize(out)[1]["staging_state"] == "UNCERTAIN"
    _no_signal_after_reap(fake.events)


def test_a_supervisor_interrupt_stops_the_child() -> None:
    fake = FakeChild([timedelta(0)], [], survive_term=False)
    plat = fake.platform()
    first = [True]

    class Watch:
        def wait(self, timeout: timedelta) -> bool:
            del timeout
            if first[0]:
                first[0] = False
                raise KeyboardInterrupt
            return fake.exited

        def close(self) -> None:
            pass

    out = _fake_run(fake, Platform(lambda _p: Watch(), plat.live_members, plat.kill_group,
                                   plat.sleep))  # fmt: skip
    assert out.interrupted and out.termination_requested and fake.events[0] == "kill:SIGTERM"
    _no_signal_after_reap(fake.events)


def test_signaling_a_reaped_group_is_impossible() -> None:
    group = sup._Group(4242, FakeChild([timedelta(0)], []).platform(), lambda: timedelta(0))
    group.reaped = True
    with pytest.raises(SupervisorError, match="SIGNAL_AFTER_REAP"):
        group.signal(signal.SIGKILL)


@darwin_only
def test_the_exited_child_keeps_its_pid_until_it_is_reaped(tmp_path: Path) -> None:
    """The premise of the reuse safety: an exited, unreaped child (a zombie) still holds
    its pid, so its group id cannot be given to anyone else; a zombie-only group has no
    live member; after the reap the group is gone."""
    proc = subprocess.Popen(child("complete", tmp_path / "log"), start_new_session=True,  # noqa: S603
                            stdout=subprocess.DEVNULL)  # fmt: skip
    plat = native_platform()
    watch = plat.watch(proc.pid)
    try:
        assert watch.wait(timedelta(seconds=10))
        os.kill(proc.pid, 0)  # still reserved: no ProcessLookupError
        assert plat.live_members(proc.pid) is False
    finally:
        watch.close()
        assert proc.wait(timeout=5) == 0
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)


@dataclass
class Logged:
    """The native platform and a real ``Popen`` with every operation logged in order."""

    events: list[str] = field(default_factory=list)

    def platform(self) -> Platform:
        real = native_platform()

        def kill(pgid: int, sig: int) -> None:
            self.events.append(f"kill:{signal.Signals(sig).name}")
            real.kill_group(pgid, sig)

        def live(pgid: int) -> bool:
            self.events.append("probe")
            return real.live_members(pgid)

        return Platform(real.watch, live, kill, real.sleep)

    def popen(self, argv: list[str], **kw: Any) -> Any:
        proc = subprocess.Popen(argv, **kw)  # noqa: S603 - the synthetic child
        real_wait = proc.wait
        events = self.events

        def wait(timeout: Any = None) -> int:
            events.append("reap")
            return real_wait(timeout)

        proc.wait = wait  # type: ignore[method-assign]
        return proc


@darwin_only
def test_a_descendant_outliving_the_child_is_killed_before_the_reap(tmp_path: Path) -> None:
    log, pidfile = tmp_path / "log", tmp_path / "grandchild"
    logged = Logged()
    out = run_child(child("grandchild", log, str(pidfile)), {}, limit=timedelta(seconds=10),
                    grace=timedelta(seconds=2), popen=logged.popen,
                    platform=logged.platform())  # fmt: skip
    _, pid, pgid = launches(log)[0].split()
    assert pid == pgid  # the child leads its own group
    assert out.exit_code == 0 and not out.termination_requested
    assert out.descendants_found and not out.survivors
    assert wait_dead(int(pidfile.read_text()))  # the grandchild did not outlive it
    assert logged.events.index("kill:SIGTERM") < logged.events.index("reap")
    _no_signal_after_reap(logged.events)
    assert summarize(out)[1]["result"] == "COMPLETE"


@darwin_only
def test_a_normal_exit_signals_nobody(tmp_path: Path) -> None:
    logged = Logged()
    out = run_child(child("complete", tmp_path / "log"), {}, popen=logged.popen,
                    platform=logged.platform())  # fmt: skip
    assert out.exit_code == 0 and not out.descendants_found
    assert not [e for e in logged.events if e.startswith("kill")]
    _no_signal_after_reap(logged.events)


def test_graceful_termination(tmp_path: Path) -> None:
    log, ready = tmp_path / "log", tmp_path / "ready"
    out = run_child(child("sleep", log, str(ready)), {}, limit=timedelta(seconds=3),
                    grace=timedelta(seconds=5))  # fmt: skip
    assert ready.exists() and out.termination_requested and not out.forced_termination
    assert out.exit_code == -signal.SIGTERM
    assert timedelta(seconds=3) <= out.elapsed < timedelta(seconds=8)
    assert len(launches(log)) == 1


def test_forced_termination(tmp_path: Path) -> None:
    log, ready = tmp_path / "log", tmp_path / "ready"
    out = run_child(child("ignore_term", log, str(ready)), {}, limit=timedelta(seconds=3),
                    grace=timedelta(seconds=1))  # fmt: skip
    assert ready.exists() and out.termination_requested and out.forced_termination
    assert out.exit_code == -signal.SIGKILL and not out.survivors
    pid = int(launches(log)[0].split()[1])
    assert not alive(pid) and len(launches(log)) == 1


# ============================================================ one session, never more
def test_no_restart_no_recovery_and_no_publication(tmp_path: Path) -> None:
    log, ready = tmp_path / "log", tmp_path / "ready"
    rec = Recorder(child("sleep", log, str(ready)), limit=timedelta(seconds=2),
                   grace=timedelta(seconds=2))  # fmt: skip
    code, result = supervise(args(), ENV, launch=rec)
    assert code == 3 and result["result"] == "SUPERVISOR_TIMEOUT"
    time.sleep(1)
    assert len(rec.seen) == 1 and len(launches(log)) == 1
    tree = ast.parse(SRC.read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                for a in n.names}  # fmt: skip
    assert not imported & {"recover", "publish", "status", "run_authorized_session",
                           "run_session", "publish_dataset", "main"}  # fmt: skip
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For | ast.While)
             and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") in ("launch", "popen")
                     for c in ast.walk(n))]  # fmt: skip
    assert loops == []


# ============================================================ interruption safety
def _killed_runner(tmp_path: Path, mode: str) -> ResearchPaths:
    data = tmp_path / "data"
    data.mkdir()
    (data / "ledger.sqlite3").write_bytes(b"")  # a placeholder: names only, never opened
    marker = tmp_path / "marker"
    argv = child(mode, tmp_path / "log", str(data), str(marker))
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(CHILD.parents[2])}
    out = run_child(argv, env, limit=timedelta(seconds=15), grace=timedelta(seconds=2))
    assert marker.read_text() == "hanging", "the child never reached its hang point"
    _, result = summarize(out)
    assert result["result"] == "SUPERVISOR_TIMEOUT" and result["recovery_required"] is True
    assert result["staging_state"] == "UNCERTAIN"
    return ResearchPaths(data / "research")


@pytest.mark.parametrize("mode", ["runner_hang", "runner_link_hang"])
def test_a_killed_session_is_never_complete_and_needs_explicit_recovery(
    tmp_path: Path, mode: str
) -> None:
    paths = _killed_runner(tmp_path, mode)
    p = rt.plan()
    root = paths.staging(p)
    store = StagingStore.open(root, p)
    left = store.interruption()
    assert left[0] == LOCK_FILE
    if mode == "runner_link_hang":
        assert len(left) == 2  # the in-flight record stayed in tmp/, never linked
    committed = set(store.committed())  # every committed batch still verifies
    expected = {"C-CAL-TRADING"} if mode == "runner_link_hang" else {
        "C-CAL-TRADING", "C-CAL-SETTLEMENT"}  # fmt: skip
    assert {b.split(".")[0] for b in committed} == expected
    assert sorted(p.name for p in (root / "sessions").iterdir()) == ["01.start.json"]
    with pytest.raises(RunnerRefusedError, match="PREVIOUS_SESSION_INTERRUPTED"):
        rt.session(paths, rt.FakeHttp(), resume=True, label="auth-2")
    with pytest.raises(RunnerRefusedError):
        rt.do_publish(paths)
    done = recover(paths=paths, plan=p, fingerprint=p.fingerprint,
                   label="2026-11-17-supervisor-timeout", now=lambda: rt.NOW)  # fmt: skip
    assert LOCK_FILE in done["quarantined"]  # type: ignore[operator]
    assert done["sessions_closed"] == [1]
    second = rt.session(paths, rt.FakeHttp(), resume=True, label="auth-2")
    assert second["status"] == "COMPLETE"
    assert {b.split(".")[0] for b in second["already_committed"]} == expected
    assert (paths.root.parent / "ledger.sqlite3").read_bytes() == b""


# ============================================================ isolation
def test_the_supervisor_touches_no_ledger_or_data_path(tmp_path: Path) -> None:
    seen: list[str] = []
    data = str(runner_mod.REPO / "data")

    def hook(event: str, a: tuple[Any, ...]) -> None:
        if event.startswith("sqlite3"):
            seen.append(event)
        elif event in ("open", "os.listdir", "os.scandir") and a and \
                str(a[0]).startswith(data):  # fmt: skip
            seen.append(f"{event}:{a[0]}")

    sys.addaudithook(hook)
    supervise(args(), ENV, launch=Recorder(child("complete", tmp_path / "log")))
    run_main(CLI, {**ENV, "ALPACA_API_KEY": SECRET})
    assert seen == []
    source = SRC.read_text()
    assert "sqlite3" not in source and "ledger" not in source.lower()


def test_importing_the_supervisor_loads_no_ledger_oms_or_trading_module() -> None:
    code = (
        "import sys\n"
        "import swing_trading_bot.history_acquisition.supervisor\n"
        "bad = sorted(m for m in sys.modules if m.startswith(("
        "'swing_trading_bot.ledger', 'swing_trading_bot.oms', 'swing_trading_bot.app.runtime',"
        "'swing_trading_bot.app.commands', 'swing_trading_bot.app.cli',"
        "'swing_trading_bot.app.broker_factory', 'swing_trading_bot.app.simulation',"
        "'swing_trading_bot.broker.alpaca_read', 'swing_trading_bot.broker.alpaca_http',"
        "'swing_trading_bot.broker.sim', 'swing_trading_bot.risk.pretrade',"
        "'swing_trading_bot.history_acquisition.transport', 'requests', 'sqlite3')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and inline code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == ""
