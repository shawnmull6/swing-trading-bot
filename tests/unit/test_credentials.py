"""Step 15A: explicit credential loading. Synthetic values and temporary files only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from swing_trading_bot.app.credentials import (
    ENV_FILE_VARIABLE,
    load_credentials,
)
from swing_trading_bot.config.settings import SettingsError

KEY = "PKSYNTHETICKEY0000000001"
SECRET = "synthetic/Secret/Value/NotReal/0000000000001"
GOOD = f"ALPACA_API_KEY={KEY}\nALPACA_SECRET_KEY={SECRET}\nTRADING_MODE=paper\n"
ENV = {"ALPACA_API_KEY": KEY, "ALPACA_SECRET_KEY": SECRET, "TRADING_MODE": "paper"}


def env_file(tmp_path: Path, text: str = GOOD, mode: int = 0o600) -> Path:
    path = tmp_path / "creds.env"
    path.write_text(text)
    path.chmod(mode)
    return path


def refused(environ: dict[str, str], match: str) -> str:
    with pytest.raises(SettingsError, match=match) as info:
        load_credentials(environ)
    message = str(info.value)
    assert KEY not in message and SECRET not in message
    return message


def test_environment_source() -> None:
    loaded = load_credentials(ENV)
    assert loaded.source == "environment"
    assert loaded.settings.secret_values() == (KEY, SECRET)
    assert KEY not in repr(loaded) and SECRET not in repr(loaded)
    assert loaded.describe()["alpaca_api_key"] == "set"


def test_explicit_file_source(tmp_path: Path) -> None:
    loaded = load_credentials({ENV_FILE_VARIABLE: str(env_file(tmp_path))})
    assert loaded.source == "file" and loaded.settings.trading_mode == "paper"


def test_no_source_is_refused_and_no_dotenv_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(GOOD)
    monkeypatch.chdir(tmp_path)
    refused({}, "no credential source selected")
    refused({ENV_FILE_VARIABLE: "   "}, "no credential source selected")


def test_both_sources_are_ambiguous(tmp_path: Path) -> None:
    message = refused(
        {ENV_FILE_VARIABLE: str(env_file(tmp_path)), "ALPACA_API_KEY": KEY}, "ambiguous"
    )
    assert "ALPACA_API_KEY" in message  # names only
    refused({ENV_FILE_VARIABLE: str(env_file(tmp_path)), "TRADING_MODE": "paper"}, "ambiguous")


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644, 0o700 | 0o020])
def test_group_or_other_permissions_refused(tmp_path: Path, mode: int) -> None:
    refused({ENV_FILE_VARIABLE: str(env_file(tmp_path, mode=mode))}, "chmod 600")


def test_relative_missing_symlink_and_directory_refused(tmp_path: Path) -> None:
    refused({ENV_FILE_VARIABLE: "creds.env"}, "absolute path")
    refused({ENV_FILE_VARIABLE: str(tmp_path / "nope.env")}, "does not exist")
    target = env_file(tmp_path)
    link = tmp_path / "link.env"
    link.symlink_to(target)
    refused({ENV_FILE_VARIABLE: str(link)}, "symbolic link")
    refused({ENV_FILE_VARIABLE: str(tmp_path)}, "regular file")


def test_foreign_owner_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = env_file(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(path).st_uid + 1)
    refused({ENV_FILE_VARIABLE: str(path)}, "owned by the current user")


def test_git_tracked_file_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = env_file(repo)
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)  # noqa: S603
    loaded = load_credentials({ENV_FILE_VARIABLE: str(path)})  # untracked: fine
    assert loaded.source == "file"
    subprocess.run([*git, "add", path.name], cwd=repo, check=True)  # noqa: S603
    refused({ENV_FILE_VARIABLE: str(path)}, "tracked by Git")


def test_unexpected_variables_refused_by_name_only(tmp_path: Path) -> None:
    message = refused(
        {ENV_FILE_VARIABLE: str(env_file(tmp_path, GOOD + "APCA_API_BASE_URL=https://x\n"))},
        "unexpected variables",
    )
    assert "APCA_API_BASE_URL" in message and "https://x" not in message


@pytest.mark.parametrize(
    "text",
    [
        GOOD.replace("paper", "live"),
        f"ALPACA_API_KEY={KEY}\nTRADING_MODE=paper\n",
        GOOD + "TRADING_ENABLED=yes\n",
    ],
)
def test_invalid_contents_never_echo_values(tmp_path: Path, text: str) -> None:
    refused({ENV_FILE_VARIABLE: str(env_file(tmp_path, text))}, "invalid settings")
