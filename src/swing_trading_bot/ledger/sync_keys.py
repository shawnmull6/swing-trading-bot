"""Well-known ``sync_state`` keys (no schema change: ``sync_state`` exists since 0001).

They are written by synchronization and initialization (steps 12-14) and read by the
startup/trading gates (step 10). A missing key always means "not established" and
blocks new entries.
"""

from __future__ import annotations

from typing import Final

BROKER_ACCOUNT_ID: Final = "broker.account_id"
"""Broker account id recorded at ledger initialization (identity check)."""

BROKER_ENVIRONMENT: Final = "broker.environment"
"""Broker environment recorded at initialization: ``paper`` (Alpaca) or ``sim``."""

LAST_SUCCESSFUL_SYNC: Final = "broker.last_successful_sync"
"""UTC ISO timestamp of the last complete read-only broker sync."""

ACTIVITIES_COMPLETE_THROUGH: Final = "activities.complete_through"
"""End (``until``) of the latest complete activity window ingested (ARCHITECTURE §12.11)."""

BASELINE_REFLECTED_ACTIVITIES: Final = "baseline.reflected_activities"
"""Canonical JSON ``{activity_id: raw_json}`` of every activity ``init-ledger`` observed in
its complete (single-page, tail-confirmed) window before recording the INIT baseline
cash (Step 15B.13A). Those activities are already inside ``baseline.cash``."""
