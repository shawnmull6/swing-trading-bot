"""Ledger locations (ARCHITECTURE §5.3, §12.20).

* the account-backed observe ledger: ``<data>/ledger.sqlite3``;
* simulation ledgers: ``<data>/sim/<run-id>.sqlite3`` — never at the observe path, and
  the observe ledger is never under ``sim/``.

Mode isolation itself is enforced by the database identity (``Database.open`` refuses a
mode mismatch); these paths only keep the files apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

OBSERVE_LEDGER_NAME: Final = "ledger.sqlite3"
SIM_DIR_NAME: Final = "sim"
SQLITE_SIDECARS: Final = ("-wal", "-shm", "-journal")
_RUN_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ALLOWED_DATA_ENTRIES: Final = frozenset({SIM_DIR_NAME, OBSERVE_LEDGER_NAME + ".lock", ".gitkeep"})
"""What may already exist in the data directory before ``init-ledger``."""


class PathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DataPaths:
    root: Path

    @classmethod
    def at(cls, root: Path) -> DataPaths:
        return cls(root.expanduser().absolute())

    @property
    def observe_ledger(self) -> Path:
        return self.root / OBSERVE_LEDGER_NAME

    @property
    def sim_dir(self) -> Path:
        return self.root / SIM_DIR_NAME

    def sim_ledger(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise PathError(f"invalid simulation run id {run_id!r}")
        return self.sim_dir / f"{run_id}.sqlite3"

    def existing_artifacts(self, ledger: Path) -> list[Path]:
        """The ledger file and any SQLite sidecar files that already exist."""
        candidates = [ledger, *(ledger.with_name(ledger.name + s) for s in SQLITE_SIDECARS)]
        return [p for p in candidates if p.exists() or p.is_symlink()]

    def unexpected_entries(self) -> list[str]:
        """Entries in the data directory that ``init-ledger`` does not expect."""
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.name not in ALLOWED_DATA_ENTRIES)
