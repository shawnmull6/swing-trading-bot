# M1 performance and operational volume (Step 15 Phase A)

Measured offline on the development machine with `RUN_PERF=1 uv run pytest tests/perf`
(fake broker behind the real adapter; **no network latency**). Nothing was changed to
meet a target; no fingerprint scoping, audit exclusion, pruning or reconciliation shortcut
exists (D15-9: any such change needs separate approval).

## Workload model

| Quantity | Assumption | Basis |
|---|---|---|
| Reconciliation cadence | every 10 min, 24/7 → 144/day → **52,560/year** | gates and the risk engine require a CLEAN run ≤ 10 min old (§12.14) |
| Sync cadence | 1–2 per reconciliation | `reconcile` includes a sync cycle; `max_sync_age` 120 s only matters while trading |
| Broker activities | ≈ **5,000/year** (M2: ≤ 4 positions, ~2 fills + fees per trading day, interest); stressed to 20,000 | M1 observe on a flat account produces almost none |
| Reconciliation evidence | 812 B `diff_json` per CLEAN run (measured); findings rows only on non-CLEAN runs | real run on the offline fake |

## Results

| Metric | Measured | Target | Result |
|---|---|---|---|
| Full-ledger fingerprint, fresh ledger | 0.2 ms | — | — |
| … 10,000 reconciliation runs (~2.3 months) | 30 ms | — | — |
| … 52,560 runs (**1 year**, 54 MB) | **160 ms** | ≤ 250 ms | **pass** |
| … 105,120 runs (2 years, 108 MB) | **319 ms** | ≤ 250 ms | **fail after ~1.6 years** |
| Reconcile, 1,000 activities | 0.17 s, 32 requests | ≤ 5 s | pass |
| Reconcile, 5,000 activities (1 year) | 0.78 s, 81 requests | — | pass |
| Reconcile, 20,000 activities | 3.57 s, **266 requests** | ≤ 15 s (10k) | pass (time) |
| Broker requests per reconcile | 32 / 81 / 266 at 1k / 5k / 20k | ≤ 50 % of the documented per-minute limit (assumed 200/min → ≤ 100) | **fails above ≈ 6,000 activities** |
| First sync after ingesting 20,000 activities | 3.19 s | — | — |
| Steady-state sync | 14–62 ms | ≤ 2 s | pass |
| Database growth | ≈ 1,044 B per reconcile cycle → **≈ 55 MB/year** | measured | — |
| CLI cold start (`status`) | 0.34 s (`--help` 0.33 s) | ≤ 1.0 s | pass |

## Interpretation

* **Fingerprint.** Linear in the whole ledger (every row of every table, by design, so any
  write invalidates a risk decision). It is computed twice per order decision (evaluation
  and transaction A) — only in simulation in M1. The 250 ms target is exceeded after
  roughly 1.6 years of 10-minute reconciliations. It also loads each table fully into
  memory. Options (not implemented, need approval): incremental per-table digests,
  excluding pure audit tables, or archiving by verified backup.
* **Full-history re-scan.** Every reconciliation re-reads activities since the first
  (INIT) baseline (§12.19), so requests grow linearly with history: ≈ 1 request per 100
  activities per window, plus fixed reads. At the 5,000/year model a reconciliation costs
  ≈ 81 requests; it would exceed half the assumed rate limit after ≈ 15 months. With real
  network round trips (≈ 100–300 ms each) one reconciliation at 5,000 activities would take
  ≈ 8–25 s. The live rate limit and latency must be confirmed by the authorized contract
  run (C11). Options (need approval): a cap on the full-history window per run, a daily
  rather than per-run full re-scan, or a durable verified checkpoint.
* **Growth.** ≈ 55 MB/year is dominated by append-only reconciliation evidence. Append-only
  tables cannot be pruned in place; retention is by verified backup and a new ledger
  (runbook), which needs its own approved procedure before it is ever used.
