# Operator runbook (M1)

The bot is **paper-only and read-only toward Alpaca** in M1. No command can submit,
replace or cancel an Alpaca order; the only execution path is the in-memory SimBroker used
by `simulate`. Every command prints a JSON envelope with `--json`; logs are redacted JSON
on stderr. Exit codes: 0 OK · 1 internal / failed scenario · 2 usage · 3 refused ·
4 halt raised, MISMATCH or account mismatch · 5 INCONCLUSIVE / incomplete · 6 lock held ·
7 configuration / credentials · 8 broker unavailable · 9 ledger problem · 10 broker
payload broke the parsing contract (`InvalidBrokerDataError`). Precedence when several
apply: 1 > 2 > 6 > 7 > 9 > 4 > 10 > 8 > 5 > 3 > 0.

Exit 10 vs 4 for a malformed broker payload (Step 15B.3): the operation that **raises** the
persistent `BROKER_PAYLOAD` halt from an un-halted ledger, with no other halt, exits 10.
Every later operation blocked by that recorded halt exits 4, as does a payload error on a
ledger that was already halted for another reason. A malformed payload never exits 8, is
never retried as an outage and never counts toward `BROKER_OUTAGE` (§7, §13).

All commands accept `--data-dir` (default `data`), `--config-dir` (default `config`),
`--json` and `--actor` (audit identity, default `operator:<os user>`).

## 1. Installation and configuration

```bash
uv sync
```
```bash
uv run swing-trading-bot --help
```

Configuration lives in `config/risk.yaml` (risk limits — do not change without approval;
the `broker_outage` section is the outage breaker policy, not a trading limit),
`config/operating_policy.yaml` and `config/fees.yaml`.

## 2. Credential-source selection

Credentials are read **only** when a broker-backed command runs, from exactly one source:

* **environment variables** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TRADING_MODE=paper`
  (optionally `TRADING_ENABLED`, `INITIAL_CAPITAL`, `MAX_CAPITAL_ALLOCATION`), **or**
* **an explicit file**: `STB_ENV_FILE=/absolute/path/to/file`. The file must be a regular
  file (not a symlink), owned by you, mode `600`, not tracked by Git, and contain only the
  names above.

No `.env` is ever discovered implicitly. Setting both sources is refused (exit 7). Error
messages name variables, never values; secrets are redacted from logs and CLI output and
are never written to the ledger.

## 3. Read-only broker verification

```bash
STB_ENV_FILE=/abs/path/paper.env uv run swing-trading-bot broker-check
```

Ledger-free, GET-only. Shows the environment (`paper`), a masked account id, status,
restrictions and U6 protections — never balances. Exit 3 if the account identity is
uncertain (id not a canonical UUID, not ACTIVE, not USD); exit 10 if the account or
configuration payload is malformed or a required field is missing or `null`. Only the
HTTPS paper-trading and market-data hosts can be contacted; redirects and proxies are
refused.

`pattern_day_trader` is **informational only**: shown as reported, or `null` when the
broker omits it (unknown — never treated as `false`). The configuration fields
`pdt_check`/`dtbp_check` are optional too (not shown; recorded in a new ledger's broker
baseline as JSON `null` when unreported; A§12.25); every other configuration field is
still required. No gate, risk check, identity check,
reconciliation or order decision uses it. The account's `trade_suspended_by_user` and
`transfers_blocked` fields are **not** read or shown yet (open question, A§12.24).

### Supervised read-only contract run (operator-authorized only)

```bash
RUN_ALPACA_PAPER_TESTS=1 STB_ENV_FILE=/abs/path/paper.env uv run python -m tests.contract.live_run
```

One process and one budget (120 requests, 300 s) for the broker-check and every contract
check; it stops on authentication failure, persisting rate limiting, uncertain identity or
budget exhaustion. It prints only the execution status and the C1–C13 outcomes. Raw
responses stay in the Git-ignored `contract-evidence/<run>/raw/` (0600); `summary.json` is
sanitized. Never commit either without review. The raw files contain the account id,
account number and balances (only credentials are redacted): never paste or share them.

Status (2026-11-12): the first authorized run stopped safely in setup after one
`GET /v2/account` because the response omitted `pattern_day_trader` (then a required
field; stop code `BROKER_CHECK_EXIT_8`, which would now be `_10`). No contract check ran;
C1–C9 remain unverified and `/account/configurations` was never reached. The mapper was
made compatible in Step 15B.2 (A§12.24). Run #2 (2026-11-12) parsed the account, then
stopped in setup after 2 requests (`BROKER_CHECK_EXIT_10`) because
`/account/configurations` omitted `pdt_check`/`dtbp_check`; Step 15B.4 made those
optional (A§12.25). Run #3 (2026-11-12) completed: C1, C2, C3, C6, C8 verified; C4
failed because the settlement calendar was read from the wrong field (corrected in Step
15B.5A, A§12.26); C7 inconclusive (an activity with an empty `description`; open);
C5/C9 inconclusive on a flat account. **Run #4 needs separate, explicit operator
authorization**; do not re-run on the strength of an earlier approval. Run it well away
from the market open and close.

Settlement calendar: Alpaca's SETTLEMENT calendar lists trade dates with their
`settlement_date`; the bot stores the `settlement_date` values as settlement days. Sale
proceeds settle only on a date both calendars agree on (or the session's own date when
no settlement data covers it); otherwise they stay unsettled and new entries are blocked
(`UNRESOLVED_SETTLEMENT`). A rejected calendar fetch is recorded in `audit`.

## 4. Ledger initialization (account-backed observe ledger)

Prerequisites: the dedicated paper account is **flat** (no positions, no open orders), not
restricted, no pending fees; `data/` contains nothing but `sim/` or the lock file.

1. Preview — creates nothing and prints the exact values to confirm:
   ```bash
   uv run swing-trading-bot init-ledger
   ```
2. Confirm all three values exactly:
   ```bash
   uv run swing-trading-bot init-ledger --confirm-path <path> --confirm-account <id> --confirm-environment paper
   ```

The new ledger starts with $5,000 virtual capital, a separate broker baseline, and the
persistent halt `INITIALIZED`. Re-running is refused. Then:

```bash
uv run swing-trading-bot reconcile
```
```bash
uv run swing-trading-bot resume --reason "initial resume after clean reconciliation"
```

## 5. Synchronization and reconciliation

* `sync` — preflight (identity), calendar refresh, recovery lookups, activity/order sync,
  marks, loss limits, outage breaker. Read-only toward the broker.
* `reconcile` — the sync sequence plus full reconciliation and startup readiness.
  OK → 0; INCONCLUSIVE → 5 (re-run on the next cycle; 5 cycles escalate); MISMATCH → 4
  and a persistent halt with findings.
* Inspection (no broker, no writes): `status`, `portfolio`, `orders`, `halts`, `audit`;
  `status --broker` additionally evaluates gates read-only. Add `--sim-run RUN_ID` to
  inspect a simulation ledger.

Run `reconcile` at least every 10 minutes while gates must be fresh (M1 has no scheduler).

## 6. Operator halt and resume

```bash
uv run swing-trading-bot halt --reason "maintenance"
```
```bash
uv run swing-trading-bot resume --reason "maintenance done"
```

`resume` runs a full reconciliation first and clears the halt **only** if it is CLEAN and
newer than the halt; a refusal is recorded (`RESUME_REFUSED`). Loss-limit lockouts and the
weekly drawdown limit keep blocking new entries independently of a resume.

## 7. BROKER_OUTAGE recovery

A failed sync cycle (network, timeout, 5xx, 429) blocks new entries at once
(`BROKER_OUTAGE_ACTIVE`). The persistent halt `BROKER_OUTAGE` is raised only after ≥ 3
failed cycles, ≥ 60 s apart, spanning ≥ 30 min. To recover:

1. confirm connectivity: `broker-check` (exit 0);
2. `sync` until it completes (the streak resets; the halt stays);
3. `resume --reason "..."` (runs a CLEAN reconciliation first).

Auth failures (`AUTH`), malformed data (`BROKER_PAYLOAD`) and account mismatches
(`RECON_ACCOUNT`) are separate halts: fix the cause (credentials, broker incident, wrong
account) before resuming. Never edit the ledger.

## 8. Ambiguous-order investigation

Applies only to simulation in M1 (no Alpaca orders exist). An order in `PENDING_SUBMIT` or
`SUBMIT_AMBIGUOUS` is never resent and never released by a timer. After ≥ 25 minutes and
≥ 3 not-found lookups:

```bash
uv run swing-trading-bot resolve-order <client_order_id> --confirm-absent
```

It runs a lookup, an order-list scan, an activity scan and a reconciliation, then confirms
absence only if all §10.3 evidence is complete; otherwise exit 3 with the missing items.
Unresolved for > 30 min → persistent `AMBIGUOUS_ORDER_TIMEOUT` halt.

## 9. Rebaseline (after an external cash event such as a deposit or paper reset)

Requires a persistent halt, no working orders, and matching positions (a position
difference cannot be fixed in M1 — `adjust` does not exist; escalate instead).

```bash
uv run swing-trading-bot rebaseline propose --reason "owner deposit"
```
```bash
uv run swing-trading-bot rebaseline approve <request_id> --acknowledge-unexplained <exact amount> --reason "statement checked"
```

The amount must equal the proposal exactly (Decimal equality); a wrong value is refused and
can be corrected. Proposals expire after 15 minutes or when broker/ledger state changes.
The halt remains (`REBASELINE`); `resume` is still required. Virtual cash never changes.

## 10. Backup, verification and manual restore (D15-8)

```bash
uv run swing-trading-bot backup /backups/ledger-YYYYMMDD.sqlite3
```
```bash
uv run swing-trading-bot verify-ledger /backups/ledger-YYYYMMDD.sqlite3
```

Backups are consistent online copies, mode `600`, verified (integrity check, application
id, schema version, content digest) before success is reported. `verify-ledger` is
read-only (never migrates; may create `<path>.lock`).

Manual restore (only when the live ledger is unusable):

1. stop every process using the ledger (`status` must not report lock contention);
2. `verify-ledger <backup>` → exit 0;
3. move the current ledger aside (never delete it), including any `-wal`/`-shm` files;
4. copy the backup to `data/ledger.sqlite3` and `chmod 600` it;
5. `verify-ledger data/ledger.sqlite3` → exit 0;
6. `reconcile` — the ledger must catch up with everything the broker reported since the
   backup; any MISMATCH halts and is handled as usual;
7. `resume --reason "restored from <backup>"` only after a CLEAN reconciliation.

## 11. Explicit migrations

Account-backed ledgers are never migrated implicitly; every command refuses with exit 9
(`MigrationRequiredError`) until:

```bash
uv run swing-trading-bot migrate --backup /backups/pre-migration.sqlite3
```

It verifies history and integrity, writes and verifies the backup, then applies all pending
migrations in one transaction (all or nothing). The backup path must not exist.

## 12. Lock contention

Each ledger has `<ledger>.lock`, held with an OS lock for the whole command (exclusive for
operations, shared for inspection/backup/verify). Exit 6 means another process is using the
ledger. Wait for it; do **not** delete the lock file — a crashed process's lock is released
by the OS automatically, and the JSON note in the file is diagnostic only.

## 13. Common errors and safe recovery

| Exit / error | Meaning | Safe action |
|---|---|---|
| 7 `CredentialSourceError` | no, ambiguous or unsafe credential source | select exactly one source (§2); fix file permissions/ownership |
| 7 `BrokerNotConfiguredError` | no broker factory in this wiring | use the installed CLI entry point |
| 8 | broker unavailable | retry later; see §7 if it persists |
| 10 `InvalidBrokerDataError` | the broker's response broke the parsing contract (missing/`null` required field, wrong type, float money, unmapped status); runtime commands also set a persistent `BROKER_PAYLOAD` halt | do **not** retry or resume: it is not an outage. Inspect the error (field names only), get the mapper reviewed and fixed, then a CLEAN `reconcile` and an explicit `resume` |
| 4 `BROKER_PAYLOAD` halt | an operation was blocked by an already-recorded payload halt | as for exit 10; the halt is never cleared automatically |
| 9 `DatabaseNotFoundError` | no ledger | `init-ledger` (only for a new, flat account) |
| 9 `MigrationRequiredError` | schema older than the code | `migrate --backup` (§11) |
| 9 `ModeMismatchError` | simulation ledger opened as observe (or vice versa) | use `--sim-run` for simulation ledgers |
| 9 `DatabaseCorruptError` | file damaged or not a ledger | do not touch it; restore (§10) |
| 4 `RECON_*` halt | reconciliation mismatch | inspect `audit` / `reconcile --json` findings; rebaseline only for external cash events |
| 4 `RECON_ACCOUNT` | connected account differs from the ledger or identity uncertain | check credentials; never rebaseline to another account |
| 6 | lock held | wait (§12) |
