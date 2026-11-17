"""Offline research data infrastructure (Step 16.15, M2-4; ARCHITECTURE §12.47).

* :mod:`.universe` — the fixed 20-symbol pilot research universe and its policy.
* :mod:`.dataset` — the versioned, checksummed on-disk research dataset: writer,
  integrity-checked loader and per-symbol quality assessment.
* :mod:`.acquisition` — the deterministic acquisition *plan* (requests that would be
  needed); it performs no request.

Nothing here makes a network request, reads credentials or environment variables, opens
the ledger, or reaches the OMS or a broker transport; it reads and writes only the local
paths it is given (``tests/unit/test_research_isolation.py``).
"""
