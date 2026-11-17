"""Synthetic children for the acquisition supervisor tests (Step 16.17B). Spawned as
``python acquisition_child.py <mode> <launch-log> [args]``; no network, no credential, no
ledger. Every mode first appends one line to ``<launch-log>`` so a test can count
launches."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HANG_S = 600


def _print(doc: object) -> None:
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()


def _ready(path: str) -> None:
    Path(path).write_text("ready")


def _runner_session(data: Path, marker: Path, *, hang_in_link: bool) -> None:
    """A real ``run_authorized_session`` over the mocked HTTP of the runner tests that
    hangs (until killed) during the bars fetch, or inside the write-once link of the
    second calendar batch's record (leaving an in-flight ``tmp/`` file)."""
    sys.path.insert(0, str(REPO))
    from swing_trading_bot.history_acquisition.runner import ResearchPaths  # noqa: PLC0415
    from tests.unit import test_history_acquisition_runner as t  # noqa: PLC0415

    def hang() -> None:
        marker.write_text("hanging")
        time.sleep(HANG_S)

    def bars(url: str, _p: dict[str, str]) -> None:
        if url.endswith("/v2/stocks/bars"):
            hang()

    if hang_in_link:
        real = os.link

        def link(src: Any, dst: Any, **kw: Any) -> None:
            if Path(dst).parent.name == "batches" and "C-CAL-SETTLEMENT" in Path(dst).name:
                hang()
            real(src, dst, **kw)

        os.link = link  # this child only: staging's write-once link
    paths = ResearchPaths(data / "research")
    http = t.FakeHttp(override=None if hang_in_link else bars)
    _print(t.session(paths, http))


def main() -> None:
    mode, log = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    with Path(log).open("a") as fh:
        fh.write(f"{mode} {os.getpid()} {os.getpgrp()}\n")
    if mode == "complete":
        _print({"status": "COMPLETE", "stop_code": None, "attempts_used": 51,
                "committed": ["x"], "resumable": False})  # fmt: skip
    elif mode == "incomplete":
        _print({"status": "INCOMPLETE", "stop_code": "SESSION_BUDGET_RESERVED",
                "stop_reason": "SESSION_TIME_RESERVE_INSUFFICIENT",
                "stop_detail": "BATCH_TIME_RESERVE", "resumable": True,
                "attempts_used": 40})  # fmt: skip
        sys.exit(1)
    elif mode == "refused":
        _print({"refused": rest[0]})
        sys.exit(2)
    elif mode == "crash":
        raise RuntimeError("synthetic crash")
    elif mode == "env":
        Path(rest[0]).write_text(json.dumps(sorted(os.environ)))
        _print({"status": "COMPLETE"})
    elif mode == "leak":
        secret = rest[0]
        _print({"status": "COMPLETE", "stop_detail": None, "body": secret,
                "next_page_token": secret, "authorization": secret})  # fmt: skip
        sys.stderr.write(f"Traceback: key={secret}\n")
    elif mode == "sleep":
        _ready(rest[0])
        time.sleep(HANG_S)
    elif mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _ready(rest[0])
        time.sleep(HANG_S)
    elif mode == "grandchild":
        # A grandchild in the same process group that would outlive the child.
        g = subprocess.Popen(  # noqa: S603 - fixed interpreter
            [sys.executable, "-c", f"import time; time.sleep({HANG_S})"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(rest[0]).write_text(str(g.pid))
        _print({"status": "COMPLETE"})
    elif mode in ("runner_hang", "runner_link_hang"):
        _runner_session(Path(rest[0]), Path(rest[1]), hang_in_link=mode == "runner_link_hang")
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
