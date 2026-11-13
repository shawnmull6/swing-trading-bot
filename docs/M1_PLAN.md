# Milestone M1 — Core Infrastructure & Portfolio Accounting

Status: **Revision 3 — architecture approved; all decisions final. Steps 1–12 approved
(step 12 final safety checks resolved, §12.16); step 13 (broker reconciliation, §12.17)
conditionally approved; step 13.1 (reconciliation hardening, §12.18) conditionally
approved; step 13.2 (rebaseline accounting corrections: unverified dividends unresolved,
event-specific acknowledgements, corrected cash equation) implemented and awaiting review
(§12.19). Steps 13–13.2 committed. Steps 14 and 14.1 approved (A§12.20, A§12.21). Step 15
phase A accepted (A§12.22); step 15A.1 (harness budgets, sanitized outcomes, evidence
permissions, head-check retry fix; A§12.23) committed. Phase B: the first authorized
supervised read-only run (2026-11-12) stopped in setup after one request because the
account payload omitted `pattern_day_trader`; steps 15B.1–15B.3 (account payload
compatibility, exit code 10, exit-code consistency; A§12.24) implemented offline and
committed (5a6226b). Live run #2 stopped in setup after two requests because
`/account/configurations` omitted `pdt_check`/`dtbp_check`; step 15B.4 (configuration
payload compatibility; A§12.25) committed. Live run #3 completed: C1–C3, C6, C8 verified;
C4 failed (settlement calendar read from the wrong field); step 15B.5A (settlement
calendar mapping correction; A§12.26) implemented offline and awaiting review. C7
(empty activity description) and the evidence redaction fix remain open; live run #4
needs separate authorization. Position
adjustments are not implemented (operational limitation). No Alpaca order submission.**
Architecture: `docs/ARCHITECTURE.md` (section references below as A§n).

## 1. Goal

A restart-safe, fail-closed core that:
* loads paper-only config and a conservative cash-account operating policy;
* reads the dedicated Alpaca paper account (read-only, structurally unable to trade);
* maintains a persistent $5,000 virtual ledger with settlement, deployed-capital
  ceiling, average-cost P&L and equity baselines;
* runs the complete order lifecycle — including ambiguous submissions, partial fills
  and recovery — against `SimBroker` only;
* reconciles executions, orders, positions and explainable cash against broker
  baselines, and halts on anything it cannot explain.

M1 places **zero** orders at Alpaca.

## 2. Decision log

| ID | Decision | Status | Where |
|---|---|---|---|
| D1 | Dedicated Alpaca paper account; exact reconciliation | **Approved** | A§8 |
| D2 | Alpaca integration strictly read-only in M1; implemented as a read-port-only adapter | **Approved** | A§5 |
| D3 | Long-only US equities and ETFs, whole shares | **Approved** | A§11.1 |
| D4 | Limit orders for buys | **Approved** | A§6.4 |
| D5 | Average-cost accounting | **Approved** | A§6.1 |
| D6/D7 | Conservative cash-account operating policy; T+1 settlement via Alpaca settlement calendar; PDT not assumed identical across account types; account profiles configurable; no intentional same-day round trips; documented actual-vs-virtual differences | **Approved (revised)** | A§6.3 |
| D8 | INITIAL_CAPITAL = MAX_CAPITAL_ALLOCATION = 5000; equity compounds; deployed capital (positions at cost + working buy commitments) ≤ $5,000 fixed ceiling; risk % from current virtual equity; broker buying power never used | **Approved (revised)** | A§6.2 |
| D9 | Decimal stored as TEXT | **Approved** | A§12 |
| D10 | Polling in M1; websocket deferred | **Approved** | — |
| D11 | Persistent halts need manual resume + clean reconciliation | **Approved** | A§7 |
| D12 | Persisted broker baselines; reconcile executions, positions, open orders, explainable cash; no tolerance band; ambiguous orders never failed on a single lookup or a timer; recovery protocol | **Approved (revised)** | A§8, A§10.3 |
| D13 | Add `hypothesis` (dev only) | **Approved** | §5 |
| D14 | Max position 20% equity; 4 positions; $1,000 max order notional; 0.5% planned risk/trade; 2% daily loss; $10 min price; 120 s max sync age; 5% weekly drawdown halt; defined baselines/resets; gap modeling | **Approved (revised)**; finalized by U1–U3 | A§11 |
| D15 | Account stays dedicated; flat before `init-ledger` (enforced by command); operator handles credential cleanup and margin/shorting settings | **Approved** | A§8.1 |
| R1 | Startup gate (in-memory, auto-clearing) vs transient blocks vs persistent halts | **Adopted** | A§7 |
| R2 | Confirmed broker fills always booked; invariant violations then halt; no balance CHECK constraints | **Adopted** | A§6.5 |
| R3 | One working order per symbol in M1; documented evolution for linked stops / partial exits | **Adopted** | A§9 |
| R4 | `TradePlan` abstraction reserved for RTH-01 (`trade_plan_id`, `order_role` columns, NULL in M1); not implemented | **Adopted** | A§9 |
| R5 | Split `BrokerReadPort` / `BrokerExecutionPort`; OrderManager runs only against SimBroker; Alpaca adapter structurally execution-incapable | **Adopted** | A§5.2 |
| R6 | Alpaca SDK behavior verified from source; contract-test list | **Adopted** | A§3, §6.3 |

## 3. Final decisions U1–U9 and Revision 3 refinements

All previously open items are decided. No unresolved design decisions remain for M1.

| ID | Decision | Where |
|---|---|---|
| **U1** | 2% daily loss → auto-expiring lockout of new entries for the rest of the session; a second lockout within 5 consecutive trading sessions → persistent halt `DAILY_LOSS_REPEAT` | A§11.2 |
| **U2** | Calendar-week (Mon–Sun ET) baseline = prior week's last session close; 5% drawdown → persistent halt `WEEKLY_DRAWDOWN` | A§11.2 |
| **U3** | 15% adverse gap **scenario**; per-position scenario loss ≤ 3% of equity; portfolio scenario loss ≤ 10% of equity. Documented as a scenario, **not** a maximum possible loss | A§11.1 #10, A§11.3 |
| **U4** | Ambiguous orders become `CONFIRMED_ABSENT` only by operator confirmation, after complete evidence; no automatic path | A§10.3 |
| **U5** | Risk-reducing same-day exits allowed once exits exist; discretionary same-day round trips blocked | A§6.3 |
| **U6** | Margin/shorting settings reported in M1; `no_shorting=true` and `max_margin_multiplier=1` required before M2 paper execution | A§3 V8, §9 |
| **U7** | Timestamped marks from the market-data latest trade / session-close bar with explicit freshness rules; `Position.current_price` (no timestamp) is cross-check only | A§6.6 |
| **U8** | $5,000 cost-basis deployed-capital ceiling retained; market-value exposure tracked; concentration (20%) enforced on market value | A§6.1, A§6.2, A§11.1 #7 |
| **U9** | `TradePlan` is the RTH-01 aggregate | A§9 |
| **F1** | Account-activity pagination with page-atomic, crash-safe cursor advancement, tail confirmation, overlap re-scan for late activities | A§8.4 |
| **F2** | Settlement-date rules for weekends, exchange holidays and bank holidays; trading vs settlement calendars; fail closed | A§6.3 |
| **F3** | `PriceMarkSource` interface and `MarkFreshnessPolicy` | A§6.6 |
| **F4** | Two-step, audited rebaseline workflow that requires a halt, matching positions and explicit acknowledgement of the exact unexplained residual; never alters ledger cash/positions | A§8.5 |

## 4. Final implementation sequence

Each step lands with its tests; quality gates (§5.5) must pass at every step.

| # | Step | Deliverables |
|---|---|---|
| 1 | Scaffolding | Package layout (A§4), ruff / mypy strict / pytest config, markers (`alpaca_paper`), `hypothesis` dev dep, CI-less `make`-style uv scripts |
| 2 | Config, policy, secrets | `Settings` (paper-only), `config/risk.yaml`, `config/operating_policy.yaml` (`cash_conservative` profile), redacting JSON logger |
| 3 | Domain | Decimal money (rejects float), enums, models, order state machine (incl. `SUBMIT_AMBIGUOUS`, `CONFIRMED_ABSENT`, `ORPHAN`), exhaustive broker-status map |
| 4 | Persistence | SQLite factory, migration runner, `0001_init.sql` (A§12.2), tx helper, invariants module |
| 5 | Calendar & settlement | `market_calendar` cache, session/settlement date service, fail-closed on gaps |
| 6 | Virtual portfolio | cash journal, settlement, reservations, deployed capital & buying capacity, avg-cost fills, realized/unrealized P&L, dividends/fees booking |
| 7 | Marks, baselines & loss limits | `MarkFreshnessPolicy`, market-value exposure, session/calendar-week baselines, daily lockout + 5-session escalation, weekly halt, gap-stress scenario |
| 8 | Broker ports & SimBroker | `BrokerReadPort`, `BrokerExecutionPort`, error taxonomy, scriptable SimBroker (acks, rejects, partial fills, lookup latency, 504 after send, duplicate client id, activity lag, fee/dividend activities, position drift) |
| 9 | Alpaca read adapters | `AlpacaPaperReadAdapter`: account, config, clock, calendar, positions, orders, by-client-id, paginated raw activities via low-level `get` → Decimal (A§8.4); `AlpacaMarksSource` (A§6.6); paper URL assertion; SDK-retry-aware backoff, circuit breaker |
| 10 | Gating | startup gate, transient block evaluator, persistent halt store + `halt_events` |
| 11 | Risk gate | pre-trade checks A§11.1 #1–14 |
| 12 | OrderManager & recovery | tx A–D, ambiguous-order protocol (A§10.3), activity ingestion, orphan handling, post-commit invariants |
| 13 | Reconciler | baselines, execution/order/position/cash checks, consistent-snapshot loop, INCONCLUSIVE escalation, two-step rebaseline workflow and audited adjustments (A§8.5) |
| 14 | Runtime & CLI | lock, startup sequence, commands (§4.1) |
| 14.1 | Broker-outage breaker | §10.5 escalation: ≥ 3 failed sync cycles ≥ 60 s apart spanning ≥ 30 min → `BROKER_OUTAGE`; durable streak; reset on a complete sync; gate `BROKER_OUTAGE_ACTIVE` (A§12.21) |
| 15 | Hardening | **A (done, offline):** explicit credential loading, production read-only factory, `broker-check`, `verify-ledger`, contract replay suite + matrix, fault-injection suite, benchmarks, runbook, docs. **A.1 (done, offline):** shared 120-request/300 s budget, rate-limit evidence, sanitized outcomes C1–C13, evidence permissions, head-check retry fix (O2). **B (run 1, 2026-11-12):** stopped in setup after 1 GET (`/v2/account` omitted `pattern_day_trader`); no check ran. **B.1–B.3 (offline):** `pattern_day_trader` optional/informational (`None` = unknown), exit 10 `BROKER_PAYLOAD`, payload-halt exit-code consistency, docs (A§12.24). **B run 2 (2026-11-12):** stopped in setup after 2 GETs (`/account/configurations` omitted `pdt_check`/`dtbp_check`). **B.4 (offline):** both optional (`None` = not reported; JSON `null` in new baselines; historical baselines unchanged), fixture matches the observed 8-field configuration (A§12.25). **B run 3 (2026-11-12):** completed, 23 GETs; C1–C3, C6, C8 VERIFIED, C4 FAILED (settlement days read from trade `date`), C7 INCONCLUSIVE (empty activity `description`), C5/C9 INCONCLUSIVE (flat). **B.5A (offline):** settlement days from `settlement_date` (A§12.26). **Open:** B.5B activity description + payload classification, B.5C evidence redaction. **Next:** live run #4 — needs separate authorization |

### 4.1 CLI (M1)
```
init-ledger                      # requires flat dedicated account; seeds $5,000; baseline; halted
status [--broker]                # balances, capacity, deployed, P&L, baselines, gates/blocks/halts, last sync
sync                             # read-only ingest of activities/orders/positions/marks
reconcile                        # full reconciliation, prints diff
halt --reason TEXT
resume --reason TEXT             # refuses unless reconciliation is clean
resolve-order ID --confirm-absent  # only when A§10.3 evidence is complete
adjust cash|position ... --reason TEXT --evidence TEXT   # individually audited ledger adjustment (halted only)
rebaseline propose --reason TEXT                         # stores diff + exact unexplained residual
rebaseline approve ID --acknowledge-unexplained AMOUNT --reason TEXT
simulate SCENARIO                # OrderManager + SimBroker on a separate sim ledger
backup PATH
```
No command can create a real order in M1.

*As built (step 14, A§12.20):* `init-ledger` requires `--confirm-path`, `--confirm-account`
and `--confirm-environment`; added `migrate --backup PATH` and read-only `portfolio`,
`orders`, `halts`, `audit`; inspection commands take `--sim-run RUN_ID`; `adjust` is not
implemented; `simulate` accepts `happy-path`, `partial-cancel`, `ambiguous-adopt`,
`never-received`, `restart-recovery`, `unexplained-deposit` or `all`. Broker-backed
commands need the Step 15 credential loading (exit 7 until then).

## 5. Test plan

### 5.1 Unit
- **Money/config:** float rejected; live mode rejected; secrets never in repr/logs;
  `TRADING_ENABLED` defaults false; env drift after init reported, not applied.
- **State machine:** all legal transitions; every illegal one raises; every SDK
  `OrderStatus` value mapped (test enumerates the SDK enum).
- **Ledger:** init; reserve; full, partial, multi-partial fills; partial + cancel;
  sells with gain/loss/break-even; avg cost on remaining shares; fees; dividends;
  replayed activity is a no-op; positions rebuilt from fills equal materialized rows.
- **Settlement (A§6.3):** proceeds unavailable until 00:00 ET on the settlement date;
  fixture-calendar cases for weekend, exchange+bank holiday, bank holiday with exchange
  open (trade before and on that day), exchange-only holiday; trade date derived in
  America/New_York near midnight UTC; calendar not covering trade date + 10 days →
  `settles_on` NULL + block; cached calendar date changes flagged.
- **Marks (A§6.6):** fresh within 15 min in-session; outside session must be from the
  most recent completed session; future timestamp, non-positive price, missing symbol,
  `received_at` older than 120 s → stale; stale held-symbol mark → equity unknown → block.
- **Activity pagination (A§8.4):** multi-page ingestion; exact-100 boundary followed by
  empty page; crash after any page resumes from the committed cursor with no double
  booking; late activity older than cursor found by overlap re-scan; changed content
  for a stored id → halt; malformed row aborts without advancing the cursor.
- **Rebaseline (A§8.5):** refused while not halted, with working/ambiguous orders, or
  with position mismatch; propose records exact residual; approve refused on wrong
  acknowledged amount, stale proposal or changed snapshot hash; approve leaves halt
  set; history never deleted.
- **Capital:** buying capacity = min(available settled cash, headroom); cases where each
  side binds; working buy commitments count toward the $5,000 ceiling; profits raise
  equity (and risk-% budgets) but not the ceiling; broker buying power of $400k never
  changes any result; concentration uses market value (appreciated position above 20%
  blocks adds; cost-basis ceiling unaffected).
- **Risk gate:** one test per check (A§11.1), including planned-stop missing, 0.5% risk
  exceeded, $10 min price, $1,000 notional, 20% position, 4 positions, same-session sell.
- **Loss limits:** baseline from prior close; 2% intraday breach → lockout; reset at next
  session; second lockout within 5 sessions → persistent halt, but not when 6+ sessions
  apart; gap-open breach detected at first sync; calendar-week baseline across a
  holiday-shortened week; weekly 5% → persistent halt not cleared by baseline reset
  alone; missing baseline → block.
- **Gap scenario:** 15% scenario on a full 20% position = 3% (passes at the boundary);
  portfolio scenario > 10% rejects; working buy commitments included.
- **Gates:** startup gate clears only after clean startup; transient blocks clear with
  condition; persistent halt survives restart and requires resume + clean recon.
- **Static guards:** `buying_power` referenced only in the adapter's mapping module; no
  class implementing `BrokerExecutionPort` imports `alpaca`; `AlpacaPaperReadAdapter`
  has no `submit_order`/`cancel_order` attribute.
- **Property-based (Hypothesis):** random interleavings of reserve / fill / partial /
  cancel / sell / settle-day-advance / dividend preserve invariants A§12.3; deployed
  capital never exceeds $5,000 via the gate; available cash never negative via the gate.

### 5.2 Integration (SimBroker + SQLite files) — acceptance scenarios

| Area | Scenario | Expected |
|---|---|---|
| **Aggregate capital** | 4 working buys whose sum would exceed $5,000 | the order crossing the ceiling is rejected; earlier ones keep reservations |
| | equity grows to $6,000 via realized gains | position/risk limits scale to $6,000; deployed ceiling stays $5,000 |
| | equity falls to $4,000 | available cash binds; ceiling irrelevant |
| **Settlement** | sell Friday before a holiday Monday | proceeds available on the correct calendar settlement date, not before |
| | calendar cache missing next session | proceeds unsettled; new entries blocked |
| **Unresolved submission** | crash after tx A, before send | `PENDING_SUBMIT` → protocol; reservation held; no resend |
| | 504 after the broker accepted | `SUBMIT_AMBIGUOUS` → lookup finds it → adopted; fills booked; one order only |
| | lookup returns not-found for 8 min, then found (visibility lag) | not released early; adopted |
| | never received by broker | stays ambiguous until all evidence + operator `resolve-order`; reservation held until then |
| | `CONFIRMED_ABSENT` order later appears | adopted + persistent halt |
| | unresolved > 30 min | escalates to persistent halt |
| | duplicate client id response | treated as "exists"; no second order; no release |
| **Partial fills** | partial → partial → cancel | qty = Σ fills; remainder released only after broker-confirmed cancel |
| **Persistent halts** | recon mismatch → restart | still halted after restart; `resume` refused while mismatch persists; accepted after fix; `halt_events` trail complete |
| | operator halt | survives restart; startup gate clearing does not clear it |
| **Broker/ledger divergence** | extra broker position; missing position; qty drift; unknown open order; FILL for non-bot order; fill on unknown `stb-` id (orphan) | each → MISMATCH → persistent halt with diff |
| | $0.01 unexplained cash movement | MISMATCH (no tolerance) |
| | fee / dividend activity on bot position | explained, booked; OK |
| | interest on external cash | classified EXTERNAL; OK |
| | fill activity lags order `filled_qty` | INCONCLUSIVE → block → OK next cycle; 5× → halt |
| **Invariant breach** | broker fill exceeds reservation / creates negative available cash | fill **is** booked; then `LEDGER_INVARIANT` halt |
| **API failures** | 5xx burst → circuit open | transient block (`BROKER_OUTAGE_ACTIVE`); ≥ 3 failed cycles ≥ 60 s apart over ≥ 30 min → `BROKER_OUTAGE` halt; one failure never escalates |
| | 401 | immediate persistent halt, no retries |
| | unmapped status / float payload | persistent halt |
| **Crash safety** | exception injected mid tx C | rollback; replay converges |
| | two processes | second fails on lock |
| **Mode isolation** | open production ledger in `simulate` mode | refused |

### 5.3 Contract tests against Alpaca paper (opt-in, read-only)

*As built (A§12.22):* rows C1–C13 in `tests/contract/matrix.py` — each states what is
verified offline (synthetic replay), what needs a real read-only response, and what needs
paper orders. Credentials come only from the explicit loader (`STB_ENV_FILE` or the
environment); the run is skipped unless `RUN_ALPACA_PAPER_TESTS=1` and needs operator
authorization. The CT numbering below is historical.
Marker `alpaca_paper`, enabled only with `RUN_ALPACA_PAPER_TESTS=1`. A spy wraps the
underlying client and fails the test on any non-GET request.

M1 (read-only, runnable now):
1. Base URL resolves to paper; account `status`, `cash`, `multiplier`,
   `trading_blocked` present and string-typed where expected. *(Run 1, 2026-11-12: the
   real `/v2/account` omitted `pattern_day_trader` and `daytrade_count`; the field is now
   optional and informational — `null`/absent = unknown, never false (A§12.24).)*
2. Account configuration fields (`no_shorting`, `max_margin_multiplier`, `suspend_trade`,
   `fractional_trading`). *Run 2 (2026-11-12):* the response had 8 fields and omitted
   `pdt_check`/`dtbp_check`, now optional and audit-only (A§12.25); values not yet checked
   by C2.
3. Calendar: `TRADING` and `SETTLEMENT` date types; `settlement_date` populated; known
   bank-holiday dates; horizon end. *Run 3 (2026-11-12):* SETTLEMENT rows are trade dates
   selected by `settlement_date`; settlement days are now taken from `settlement_date`
   (A§12.26); Veterans Day is a trading day but not a settlement day.
4. `get_order_by_client_id` for a random unknown id → exact error class/status (404?).
5. `GET /v2/account/activities` raw shape: id format, ordering, `page_token`, numeric
   fields as strings; non-trade activity types present on this account.
6. Positions/orders on a flat account return empty lists.
7. Order/position numeric fields arrive as strings (V4).
8. Activities pagination: `direction=asc`, `page_size`, `page_token` semantics; ids
   time-ordered; behavior of `after=` date filter used by the overlap re-scan.
9. Market data: which feed this account can use (IEX/SIP), latest-trade and daily-bar
   timestamps present and in UTC, numeric fields as strings in raw JSON.

Deferred to the M2 pre-execution suite (needs order submission; not run in M1):
duplicate `client_order_id` response; lookup visibility latency after submit; fill
activity id uniqueness, granularity and lag vs order status; partial-fill reporting;
fee posting timing (`pending_reg_taf_fees` → activity); 504-on-submit behavior; behavior
of `qty_available` with working sell orders.

### 5.4 Fault injection
SimBroker scripts plus a `crash_at(point)` hook in OrderManager/recovery for each tx
boundary A–H; every boundary has at least one crash-and-restart test.

### 5.5 Quality gates
`ruff check`, `ruff format --check`, `mypy --strict src`, `pytest` (network-free by
default) with ≥ 90% line coverage on `domain/`, `ledger/`, `risk/`, `oms/` and branch
coverage on the state machine and recovery protocol.

## 6. Acceptance criteria

M1 is complete when all of the following are demonstrated:

1. **Initialization:** `init-ledger` refuses unless the Alpaca account is flat; creates
   the ledger with cash $5,000.00, ceiling $5,000.00, profile `cash_conservative`,
   persisted broker baseline, and persistent halt `INITIALIZED`. Re-running is refused.
2. **Observation:** `status --broker`, `sync`, `reconcile` run against the paper account
   read-only and report OK on the flat dedicated account; no Alpaca balance is ever
   shown or used as bot capital.
3. **Structural safety:** no Alpaca execution adapter exists; static and runtime tests
   prove the Alpaca integration cannot submit or cancel; live mode cannot start.
4. **Accounting:** all §5.1 ledger, settlement, capital and loss-limit tests pass;
   property tests find no invariant violation.
5. **Acceptance scenarios:** every §5.2 scenario passes — aggregate capital exposure,
   settlement calendars, unresolved order submissions, persistent halts, broker/ledger
   divergence, invariant breach booking, API failures, crash safety, mode isolation.
6. **Recovery:** in no scenario is an ambiguous order released on a single lookup or a
   timer, and no scenario produces a second broker order for one intent.
7. **Gating:** startup gate, transient blocks and persistent halts behave per A§7,
   including restart persistence and resume refusal.
8. **Contract tests:** §5.3 M1 items run once against paper and pass; findings recorded
   in `ARCHITECTURE.md` §3 (CT items resolved or re-scoped). *Status: offline replay passes;
   the live run (phase B) is outstanding — offline tests alone are not broker acceptance.*
9. **Secrets:** captured logs across the full suite contain no secret values.
10. **Quality gates** in §5.5 pass from a clean checkout without network access.
11. **Docs:** ARCHITECTURE.md and this plan reflect the implementation.

## 7. Risks and assumptions still to verify

| Item | Type | Mitigation / verification |
|---|---|---|
| SDK auto-retries `POST /orders` on 429/504 (V2) | Verified risk (M2) | Must be disabled on the write path before any execution adapter exists; M2 entry criterion |
| Activities endpoint not wrapped by SDK; float models (V3) | Verified | Raw `get` + Decimal parsing; contract test 5 |
| Unknown-client-id lookup error shape; post-submit visibility latency | Assumption | M1 CT 4; latency only testable in M2 |
| Paper `settlement_date` populated & correct on bank holidays | Assumption | M1 CT 3; fail closed if missing |
| Settlement eligibility of exchange holidays, Thanksgiving, Columbus/Veterans Day, 2027 dates | Assumption (fixture) | Only 2026-04-03, 2026-06-19, 2026-07-03 verified from DTCC/NSCC notices; all others confirmed by M1 CT 3 against Alpaca's SETTLEMENT calendar |
| Activity id ordering / `page_token` semantics; late-posted activities | Assumption | M1 CT 8; overlap re-scan + daily full re-scan regardless |
| Market-data feed entitlement and timestamp semantics (IEX may be sparse) | Assumption | M1 CT 9; stale marks block entries; feed recorded per mark |
| Alpaca calendar horizon ends 2029 | Future risk | Startup check warns when cache horizon < 1 year |
| Paper simulates regulatory fees / when they post | Assumption | M1 CT 1/5 partially; full in M2; fees pending → INCONCLUSIVE, not tolerance |
| Fill activity lag vs order/position updates | Assumption | consistent-snapshot loop + INCONCLUSIVE; measured in M2 |
| Paper account reset by Alpaca/operator | Operational | recon MISMATCH → halt → `rebaseline` |
| Corporate actions (splits) on held symbols | Operational (M2+) | halt; manual ADJUSTMENT workflow designed in M2 |
| Interest/other credits on the $100k | Assumption | classified EXTERNAL; unknown activity types → UNEXPLAINED → halt until classified |
| Paper account is 4× margin; broker won't stop leverage | Verified | virtual gate + broker cash backstop; operator settings (D15); U6 |
| Fee reported both on a fill and as a FEE activity (double count) | Adapter contract | Alpaca adapter sets fill fees to 0; regulatory fees only via FEE activities (§12.8); CT confirms fill activities carry no fees |
| Late activity executed before booked fills (average-cost order) | Detected | Booked then halt `OUT_OF_ORDER`; restatement procedure required before M2 |
| No verified official-close source | Limitation (M2 blocker) | Daily-bar observations (IEX/SIP) are never accepted as official; baselines need an OFFICIAL source before M2 execution (§12.11) |
| 500+ orders sharing one submitted_at | Detected | `/orders` cannot enumerate them; fails closed with IncompleteRetrievalError (§12.11) |
| Activity posting lag (assumed 15 min) | Assumption | Window closure + overlap re-scan; CT confirms actual lag |
| Alpaca activities cursor / window semantics | Assumption | Opaque-ID collector + overlapping windows + idempotent ingestion regardless; CT confirms |
| Paper fills are optimistic | Known | out of M1 scope; slippage tracking in M2 |
| PDT/settlement regulation changes | Regulatory | rules in configurable account profiles, not hard-coded |
| SQLite loss/corruption | Operational | WAL + FULL sync, backups, startup invariants |
| `alpaca.txt` plaintext credentials | Operational | operator cleanup (D15) |
| Account payload differs from the SDK model (`pattern_day_trader`, `daytrade_count` absent in run 1) | Observed (one sample) | PDT optional/informational; every other field strict; malformed payload → exit 10 + persistent `BROKER_PAYLOAD` halt (A§12.24) |
| SETTLEMENT calendar semantics | Observed (run 3); mapping corrected (A§12.26) | settlement days = `settlement_date` values; cross-checked against each session's own `settlement_date`; an eligible day on which nothing settles is not listed (can only delay a derived date) |
| `/account/configurations` shape | Observed (one sample, run 2) | 8 fields; `pdt_check`/`dtbp_check` absent → optional, audit-only; installed alpaca-py 0.45.0 marks them removed 2026-07-06 (SDK statement, not independently verified) (A§12.25) |
| `trade_suspended_by_user`, `closing_transactions_only`, `transfers_blocked` not mapped; `suspend_trade` not checked by reconciliation, `init-ledger` or exit readiness (it **is** recomputed on every gate evaluation and blocks entries) | Open question (**M2 blocker**) | verify semantics (`closing_transactions_only` is absent from the SDK) and enforce trading suspension / closing-only at every entry decision before any real order execution (A§12.25 ¶5) |

## 8. Out of scope for M1
Strategy/signals, scheduler, websocket stream, Alpaca execution adapter, RTH-01
TradePlan implementation, protective stops/exits, cancel-on-halt, corporate-action
adjustments, notifications, dashboards.

## 9. M2 entry criteria (preview)
SDK write-path retries disabled and tested; out-of-order fill restatement procedure and
execution-ordered ingestion in place (ARCHITECTURE §12.8); M2 pre-execution contract
suite designed; Alpaca execution adapter reviewed separately; U6 preflight enforced;
verified trading-suspension and closing-only enforcement (`trade_suspended_by_user`,
`suspend_trade`, `closing_transactions_only`; A§12.25 ¶5); contract rows C1–C9 reviewed from an authorized supervised run;
the M2 pre-execution verification requirements in §9.1 met;
explicit approval to submit paper orders.

### 9.1 M2 pre-execution verification requirements

Added 2026-11-12 after the read-only review of `alpacahq/alpaca-skills@0e7c35163ae4`
(`docs/EXTERNAL_REFERENCES.md`). Each item is verified from SDK source and/or the M2
pre-execution contract suite (authorized, supervised, paper only) and recorded in
ARCHITECTURE §3/§12 before any order is submitted to Alpaca. The external skills are
hypotheses, not evidence; where they disagree with this project's design, this project's
design stands until verification says otherwise and a reviewed change is approved.
Nothing here changes M1 behavior. These are verification requirements only: they do not
authorize implementing an execution adapter, enabling a broker write path, or submitting
any order, which still require the separate review and explicit approval listed in §9.

| # | Requirement | Verify | Pass condition |
|---|---|---|---|
| M2-V1 | **Trading restrictions and paper-account identity** | `/v2/account` `status` (including `PAPER_ONLY`), `trading_blocked`, `account_blocked`, `trade_suspended_by_user`; configuration `suspend_trade`, `no_shorting`, `max_margin_multiplier`; `closing_transactions_only` presence and meaning; resolved base URL is the paper URL literal (A§3 V1); the account id matches the one recorded in the ledger baseline | Every restriction field mapped and enforced at every entry decision (extends the §7 M2 blocker and U6); an unknown account status or a changed account id fails closed (block/halt); no path reads the base URL from configuration |
| M2-V2 | **Order field validation and asset eligibility** | Order payload rules for the order types M2 will use (`type`/`time_in_force` combinations, `qty` vs `notional` exclusivity, `extended_hours`, `client_order_id` ≤ 128 chars and uniqueness); asset record fields `tradable`, `status`, `fractionable`, `shortable`, `easy_to_borrow` via `GET /v2/assets/{symbol}` | Pre-trade gate rejects locally (reason code, no broker call) any order the broker would reject for field or eligibility reasons; asset eligibility is read fresh per session and a missing/unknown asset fails closed |
| M2-V3 | **HTTP 403 / 422 / 429 handling on order endpoints** | Actual status codes and bodies for: insufficient buying power or shares, unrecognized or invalid fields, non-tradable symbol, market closed, rate limiting (`Retry-After`, `X-RateLimit-*`). The external skill states 403 on `POST /v2/orders` means insufficient buying power/shares, not an auth failure; A§10.5 currently maps 401/403 to persistent halt `AUTH` | A reviewed, explicit mapping from observed responses to `REJECTED` vs halt vs ambiguous; any change to the 403 → `AUTH` rule is a separate reviewed decision (fail-closed until then); 429 on submit is never retried by the SDK or a generic loop (A§3 V2) |
| M2-V4 | **Ambiguous order submission outcomes** | SDK write-path retries disabled (V2); behavior of timeout, connection drop, 5xx and 504 on submit; `orders:by_client_order_id` response for a just-submitted and an unknown id (visibility latency, error shape); duplicate `client_order_id` response | Every non-definitive submit outcome enters `SUBMIT_AMBIGUOUS` and is resolved only by the A§10.3 protocol; a duplicate-id response is treated as "order exists"; no automated resend |
| M2-V5 | **Quantity and price precision** | Trading API limits for `qty` (decimal places; whole shares unless fractional is explicitly enabled), `notional`, `limit_price`/`stop_price` increments (sub-dollar vs ≥ $1 prices), and the precision of echoed fill fields | Values rounded once, at the send boundary, with an explicit Decimal rounding mode (round down for buy quantity and notional); sent values and broker-echoed values stored verbatim; a precision rejection is a local pre-trade rejection, never a broker surprise |
| M2-V6 | **Order-status polling limits** | Rate-limit budget from response headers on the paper account; order/activity visibility lag after submit | A documented polling schedule with a per-order cap and backoff, sharing one read budget with sync/reconcile; exceeding the cap escalates through the existing ambiguous/outage paths rather than polling indefinitely |
