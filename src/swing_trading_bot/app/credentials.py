"""Explicit, fail-closed credential loading (D15-1, D15-2; ARCHITECTURE §12.22).

Exactly one source must be selected:

* **process environment** — ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY`` (and the other
  :data:`~swing_trading_bot.config.settings.ENV_TO_FIELD` names) set in the environment;
* **an explicitly selected file** — ``STB_ENV_FILE=/absolute/path``.

There is no automatic ``.env`` discovery and no precedence rule: if ``STB_ENV_FILE`` is
set *and* any setting is also present in the environment, loading is refused. A selected
file must be an absolute path to a regular file (not a symlink), owned by the current
user, with no group/other permission bits, not tracked by Git, and containing only the
known setting names. Values are held as ``SecretStr`` by :class:`Settings`; no error
message, log line or CLI output contains a value (messages name variables only), and
nothing here writes to the ledger.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import dotenv_values

from swing_trading_bot.config.settings import (
    ENV_TO_FIELD,
    Settings,
    SettingsError,
    settings_from_mapping,
)

ENV_FILE_VARIABLE: Final = "STB_ENV_FILE"
GIT_TIMEOUT_S: Final = 5


class CredentialSourceError(SettingsError):
    """No usable, unambiguous credential source. Messages never contain values."""


@dataclass(frozen=True, slots=True)
class LoadedCredentials:
    settings: Settings
    source: str
    """``environment`` or ``file`` — never the file's contents."""

    def describe(self) -> dict[str, str]:
        return {"credential_source": self.source, **self.settings.describe()}


def _git_tracked(path: Path) -> bool:
    """True when ``path`` is tracked by a Git repository (fail closed on doubt)."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "ls-files", "--error-unmatch", "--", path.name],  # noqa: S607
            cwd=path.parent,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return False  # git not installed: no repository can track it
    except (OSError, subprocess.SubprocessError):
        raise CredentialSourceError(
            "could not determine whether the credential file is tracked by Git"
        ) from None
    return result.returncode == 0


def check_credential_file(path: Path) -> list[str]:
    """Problems with a selected credential file (empty when acceptable)."""
    if not path.is_absolute():
        return [f"{ENV_FILE_VARIABLE} must be an absolute path"]
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ["credential file does not exist"]
    except OSError:
        return ["credential file cannot be inspected"]
    if stat.S_ISLNK(info.st_mode):
        return ["credential file must not be a symbolic link"]
    if not stat.S_ISREG(info.st_mode):
        return ["credential file must be a regular file"]
    problems: list[str] = []
    if info.st_uid != os.getuid():
        problems.append("credential file must be owned by the current user")
    if info.st_mode & 0o077:
        problems.append("credential file must not be readable or writable by group/others "
                        "(chmod 600)")  # fmt: skip
    if not problems and _git_tracked(path):
        problems.append("credential file is tracked by Git")
    return problems


def _from_file(path: Path) -> Settings:
    problems = check_credential_file(path)
    if problems:
        raise CredentialSourceError("; ".join(problems))
    try:
        values = dotenv_values(path)
    except (OSError, UnicodeDecodeError):
        raise CredentialSourceError("credential file cannot be read") from None
    unknown = sorted(k for k in values if k not in ENV_TO_FIELD)
    if unknown:
        raise CredentialSourceError("credential file has unexpected variables: "
                                    + ", ".join(unknown))  # fmt: skip
    return settings_from_mapping(values)


def load_credentials(environ: Mapping[str, str]) -> LoadedCredentials:
    """Load settings from exactly one explicitly selected source, or refuse."""
    selected = environ.get(ENV_FILE_VARIABLE)
    present = sorted(k for k in ENV_TO_FIELD if environ.get(k, "").strip())
    if selected is not None and selected.strip():
        if present:
            raise CredentialSourceError(
                f"ambiguous credential source: {ENV_FILE_VARIABLE} is set and the environment "
                f"also defines {', '.join(present)}; select exactly one source"
            )
        return LoadedCredentials(_from_file(Path(selected)), "file")
    if not present:
        raise CredentialSourceError(
            f"no credential source selected: set {ENV_FILE_VARIABLE} to an explicit file "
            "or provide the settings as environment variables (no .env is read implicitly)"
        )
    return LoadedCredentials(settings_from_mapping({k: environ.get(k) for k in ENV_TO_FIELD}),
                             "environment")  # fmt: skip
