"""Step 15B.10A: ``init-ledger --confirm-file`` keeps the account id out of argv.

The three exact-match confirmations come from a private JSON file; unsafe or malformed
files refuse before credentials are loaded or any broker request is made. Offline only:
the real read adapter over a fake HTTP account, temporary files and directories.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from swing_trading_bot.app import confirmation
from swing_trading_bot.ledger.repositories import TradingStateRepository
from tests.support.alpaca_account import ACCOUNT_ID
from tests.support.cli_env import CliEnv, cli_env


@pytest.fixture
def e(tmp_path: Path) -> CliEnv:
    return cli_env(tmp_path)


def fields(e: CliEnv, **override: Any) -> dict[str, Any]:
    return {"confirm_path": str(e.ledger), "confirm_account": ACCOUNT_ID,
            "confirm_environment": "paper", **override}  # fmt: skip


def write(path: Path, content: str | bytes, mode: int = 0o600) -> Path:
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(mode)
    return path


def conf(e: CliEnv, content: str | bytes | None = None, *, name: str = "confirm.json",
         mode: int = 0o600, **override: Any) -> Path:  # fmt: skip
    text = json.dumps(fields(e, **override)) if content is None else content
    return write(e.root / name, text, mode)


def run(e: CliEnv, *argv: str) -> tuple[int, dict[str, Any]]:
    assert ACCOUNT_ID not in " ".join(argv)  # the point of --confirm-file
    return e.run("init-ledger", *argv)


def requests(e: CliEnv) -> Counter[str]:
    return Counter(path for _svc, path, _params in e.account.transport.calls)


def assert_nothing_created(e: CliEnv) -> None:
    assert not e.ledger.exists()
    assert not [p for p in e.data.iterdir() if p.name != "ledger.sqlite3.lock"]  # no sidecars


def refused_before_broker(e: CliEnv, path: Path | str, problem: str) -> None:
    code, env = run(e, "--confirm-file", str(path))
    assert code == 3 and env["result"]["status"] == "REFUSED", env
    (got,) = env["result"]["problems"]
    assert problem in got, got
    assert not e.account.transport.calls  # no broker request at all
    assert ACCOUNT_ID not in e.last_text  # no value echoed
    assert_nothing_created(e)
    assert not (e.data / "ledger.sqlite3.lock").exists()  # refused before the lock too


# ============================================================ success
def test_valid_confirmation_file_initializes_halted(e: CliEnv) -> None:
    code, env = run(e, "--confirm-file", str(conf(e)))
    assert code == 0, env
    created = env["result"]["created"]
    assert created["halted"] is True and created["halt_reason"] == "INITIALIZED"
    with e.open() as db, db.read() as s:
        state = TradingStateRepository().get(s)
    assert state.halted and state.reason_code == "INITIALIZED"  # no automatic resume
    seen = requests(e)  # every broker observation is still made, and repeated
    assert seen["/account"] == 2 and seen["/positions"] == 2 and seen["/orders"] == 2
    assert seen["/account/activities"] == 1 and seen["/calendar"] == 2
    code, env = run(e, "--confirm-file", str(conf(e)))  # never overwrites
    assert code == 3 and "already exists" in env["result"]["problems"][0]


def test_preview_is_unchanged(e: CliEnv) -> None:
    code, env = run(e)
    assert code == 3 and env["result"]["status"] == "CONFIRMATION_REQUIRED"
    assert set(env["result"]["required_confirmation"]) == set(confirmation.FIELDS)
    assert_nothing_created(e)


def test_single_page_guard_still_applies(e: CliEnv) -> None:
    for _ in range(100):
        e.account.deposit("1.00")
    code, env = run(e, "--confirm-file", str(conf(e)))
    assert code == 3 and env["result"]["status"] == "REFUSED"
    assert any("needed 2 page requests" in p for p in env["result"]["problems"])
    assert_nothing_created(e)


# ============================================================ exact-match confirmation
@pytest.mark.parametrize(
    "override",
    [{"confirm_account": "00000000-0000-4000-8000-000000000000"},
     {"confirm_environment": "live"}, {"confirm_environment": "Paper"},
     {"confirm_path": "/elsewhere/ledger.sqlite3"}, {"confirm_account": ""}],
)  # fmt: skip
def test_wrong_value_is_not_confirmed(e: CliEnv, override: dict[str, str]) -> None:
    code, env = run(e, "--confirm-file", str(conf(e, json.dumps(fields(e, **override)))))
    assert code == 3 and env["result"]["status"] == "CONFIRMATION_REQUIRED"
    assert requests(e)["/account"] == 2  # observed, then refused before any write
    assert_nothing_created(e)


# ============================================================ unsafe files
def test_missing_file(e: CliEnv) -> None:
    refused_before_broker(e, e.root / "absent.json", "does not exist")


def test_relative_path(e: CliEnv) -> None:
    conf(e)
    refused_before_broker(e, "confirm.json", "absolute path")


def test_symlink(e: CliEnv) -> None:
    link = e.root / "link.json"
    link.symlink_to(conf(e))
    refused_before_broker(e, link, "symbolic link")


def test_directory(e: CliEnv) -> None:
    (e.root / "dir.json").mkdir()
    refused_before_broker(e, e.root / "dir.json", "regular file")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644, 0o666, 0o700, 0o610])
def test_group_or_world_access(e: CliEnv, mode: int) -> None:
    refused_before_broker(e, conf(e, mode=mode), "group/others")


def test_owner_only_read_is_accepted(e: CliEnv) -> None:
    code, _env = run(e, "--confirm-file", str(conf(e, mode=0o400)))
    assert code == 0


def test_wrong_owner(e: CliEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    path = conf(e)
    uid = os.stat(path).st_uid
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    refused_before_broker(e, path, "owned by the current user")


def test_too_large(e: CliEnv) -> None:
    big = json.dumps({**fields(e), "pad": "x" * confirmation.MAX_BYTES})
    refused_before_broker(e, conf(e, big), "too large")


def test_unreadable_file(e: CliEnv) -> None:
    refused_before_broker(e, conf(e, mode=0o000), "cannot be opened")


# ============================================================ malformed content
def _dup(e: CliEnv) -> str:
    return (f'{{"confirm_path":"{e.ledger}","confirm_account":"{ACCOUNT_ID}",'
            f'"confirm_environment":"paper","confirm_account":"{ACCOUNT_ID}"}}')  # fmt: skip


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (lambda e: json.dumps({"confirm_path": str(e.ledger), "confirm_account": ACCOUNT_ID}),
         "missing fields: confirm_environment"),
        (lambda e: json.dumps({**fields(e), ACCOUNT_ID: "x"}), "unknown fields: 1"),
        (_dup, "duplicate keys"),
        (lambda e: f'{{"confirm_account": "{ACCOUNT_ID}"', "not valid JSON"),
        (lambda e: "", "not valid JSON"),
        (lambda e: b"\xff\xfe" + ACCOUNT_ID.encode(), "not valid JSON"),
        (lambda e: json.dumps([ACCOUNT_ID]), "JSON object"),
        (lambda e: json.dumps(fields(e, confirm_account=[ACCOUNT_ID])),
         "must be strings: confirm_account"),
        (lambda e: json.dumps(fields(e, confirm_environment=None)),
         "must be strings: confirm_environment"),
        (lambda e: json.dumps(fields(e, confirm_path=1)), "must be strings: confirm_path"),
    ],
)  # fmt: skip
def test_malformed_content(e: CliEnv, content: Any, problem: str) -> None:
    refused_before_broker(e, conf(e, content(e)), problem)


# ============================================================ one source only
@pytest.mark.parametrize(
    "inline",
    [("--confirm-environment", "paper"), ("--confirm-path", "/x"),
     ("--confirm-account", "acct")],
)  # fmt: skip
def test_mixed_sources_refuse(e: CliEnv, inline: tuple[str, str]) -> None:
    code, env = run(e, "--confirm-file", str(conf(e)), *inline)
    assert code == 3 and "not both" in env["result"]["problems"][0]
    assert not e.account.transport.calls
    assert_nothing_created(e)


def test_inline_options_still_work(e: CliEnv) -> None:
    code, env = e.run("init-ledger", "--confirm-path", str(e.ledger), "--confirm-account",
                      ACCOUNT_ID, "--confirm-environment", "paper")  # fmt: skip
    assert code == 0 and env["result"]["created"]["halted"] is True


def test_confirmation_repr_hides_values() -> None:
    c = confirmation.Confirmation("/p", ACCOUNT_ID, "paper")
    assert ACCOUNT_ID not in repr(c) and ACCOUNT_ID not in str(c)
