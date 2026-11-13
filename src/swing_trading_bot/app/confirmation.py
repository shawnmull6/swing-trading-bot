"""Private confirmation input for ``init-ledger`` (Step 15B.10A).

``--confirm-file PATH`` supplies the three exact-match confirmations from a private
local JSON file, so the full broker account id never appears in process arguments or
shell history. The file holds exactly::

    {"confirm_path": "...", "confirm_account": "...", "confirm_environment": "..."}

— the same keys ``init-ledger`` prints under ``required_confirmation``. The values are
passed unchanged to the existing exact-match check (:func:`init_ledger._confirmations`);
nothing here compares them.

The file must be an absolute path to a regular file (not a symlink), owned by the
current user, mode 0600 or stricter (no execute, group or other bits), at most
:data:`MAX_BYTES`, and a JSON object with exactly the three string fields (no duplicate
keys). Anything that cannot be verified refuses. Keep the file outside the repository
(RUNBOOK §4). Error messages name the rule or the field, never a value. This is not
a credential source: it holds no secret the broker accepts, and nothing reads it except
``init-ledger``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

FIELDS: Final = ("confirm_path", "confirm_account", "confirm_environment")
MAX_BYTES: Final = 4096


class ConfirmationFileError(ValueError):
    """The confirmation file is unsafe or malformed. Messages never contain values."""


@dataclass(frozen=True, slots=True)
class Confirmation:
    path: str
    account: str
    environment: str

    def __repr__(self) -> str:  # never print the values
        return "Confirmation(<redacted>)"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ConfirmationFileError("confirmation file has duplicate keys")
    return dict(pairs)


def _read_private(path: Path) -> bytes:
    if not path.is_absolute():
        raise ConfirmationFileError("--confirm-file must be an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ConfirmationFileError("confirmation file does not exist") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ConfirmationFileError("confirmation file must not be a symbolic link") from None
        raise ConfirmationFileError("confirmation file cannot be opened") from None
    try:
        info = os.fstat(fd)  # checks apply to the file actually opened
        if not stat.S_ISREG(info.st_mode):
            raise ConfirmationFileError("confirmation file must be a regular file")
        if info.st_uid != os.getuid():
            raise ConfirmationFileError("confirmation file must be owned by the current user")
        if info.st_mode & 0o177:  # 0600 or stricter: no execute, no group/other bits
            raise ConfirmationFileError(
                "confirmation file must be mode 0600 or stricter (no execute, no "
                "group/others access)"
            )
        if info.st_size > MAX_BYTES:
            raise ConfirmationFileError("confirmation file is too large")
        data = os.read(fd, MAX_BYTES + 1)
    except OSError:
        raise ConfirmationFileError("confirmation file cannot be read") from None
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ConfirmationFileError("confirmation file is too large")
    return data


def load_confirmation(path: Path) -> Confirmation:
    """The three confirmations from a private file, or :class:`ConfirmationFileError`."""
    data = _read_private(path)
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except ConfirmationFileError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ConfirmationFileError("confirmation file is not valid JSON") from None
    if not isinstance(doc, dict):
        raise ConfirmationFileError("confirmation file must contain a JSON object")
    missing = [f for f in FIELDS if f not in doc]
    unknown = sorted(k for k in doc if k not in FIELDS)
    if missing or unknown:
        parts = ([f"missing fields: {', '.join(missing)}"] if missing else []) + (
            [f"unknown fields: {len(unknown)}"] if unknown else []
        )
        raise ConfirmationFileError("confirmation file " + "; ".join(parts))
    wrong = [f for f in FIELDS if not isinstance(doc[f], str)]
    if wrong:
        raise ConfirmationFileError(f"confirmation fields must be strings: {', '.join(wrong)}")
    return Confirmation(doc["confirm_path"], doc["confirm_account"], doc["confirm_environment"])
