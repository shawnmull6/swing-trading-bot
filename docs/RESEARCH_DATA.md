# Research dataset and walk-forward validation (Step 16.15, M2-4)

Status: **offline infrastructure only.** No historical data has been fetched for this
step. Every result produced so far comes from SYNTHETIC fixtures and says nothing about
whether the strategy is profitable. Architecture detail: ARCHITECTURE §12.47.

---

## 1. Research universe (`research/universe.py`, `PILOT_UNIVERSE`, id `pilot-20-v1`)

| Group | Symbols | Asset class |
|---|---|---|
| Technology | NVDA, AMD, MSFT, AAPL | Equity |
| Communication | META, GOOGL | Equity |
| Consumer discretionary | AMZN, TSLA | Equity |
| Financials | JPM, GS | Equity |
| Industrials | CAT, BA | Equity |
| Healthcare | UNH, ABBV | Equity |
| Energy | XOM, CVX | Equity |
| Broad market | SPY, QQQ | Broad-market ETF |
| Financials / Energy | XLF, XLE | Sector ETF |

* **Membership policy — `FIXED_PRESENT_DAY_PILOT`.** Fixed for the whole window; never
  re-selected by performance; never silently substituted. A symbol whose data fails
  validation is reported and excluded, not replaced. A different list is a different
  universe id and fingerprint.
* **Survivorship bias.** `survivorship_bias_free = false`. All 20 are today's large,
  liquid survivors chosen with hindsight; no delisted, acquired or failed names can
  appear. Results are optimistic relative to a point-in-time universe and are a pilot,
  not an estimate of historical strategy performance. A survivorship-free study needs
  historical index membership plus delisted-symbol data (§3.4).
* **Correlation.** XLF overlaps JPM/GS and XLE overlaps XOM/CVX; signals are correlated.
* **Identity.** The research key is the current ticker. Ticker changes are recorded in the
  dataset's `identity.json` and every bar row's `source_symbol` is checked against it.
  Expectations to verify during acquisition (general knowledge, *not* attested data):
  META traded as FB before a 2022-06 ticker change; forward splits in NVDA (2021-07 4:1,
  2024-06 10:1), AMZN (2022-06 20:1), GOOGL (2022-07 20:1), TSLA (2022-08 3:1).
* **ETFs.** Same rules and filters as equities. All distributions (ordinary, capital-gain,
  return of capital) are booked as cash on the ex-date; tax character is ignored. An ETF
  with unattested distribution history is rejected like any equity.
* **Trading eligibility.** Not established offline. Asset status is an M2-V2 check.

## 2. Dataset schema (`research/dataset.py`, schema version 2)

```
<root>/manifest.json                    schema, provenance, universe, file checksums
<root>/calendar.json                    sessions (open/close local time, settlement date)
<root>/identity.json                    ticker spans, listing start, delisting date
<root>/bars/<SYMBOL>.raw.jsonl          raw SIP daily bars — the only decision/execution data
<root>/bars/<SYMBOL>.split.jsonl        optional provider split-adjusted bars — same-provider consistency check only
<root>/corporate_actions/<SYMBOL>.json  splits, cash distributions, other events + coverage
```

* Canonical JSON; prices and amounts are **decimal strings**; volumes are integers. A
  JSON float, NaN or Infinity anywhere fails the load.
* Bar row: `symbol, source_symbol, session, t, o, h, l, c, v, vw, n, feed, adjustment,
  received_at, batch_id`. Rows are parsed into the existing `DailyBar` (same checks as
  the live adapter: OHLC consistency, `t` on the session in America/New_York).
* `manifest.json`: `schema_version, dataset_id, created_at, description,
  market_timezone ("America/New_York"), feed ("sip"), research_window {start,end},
  warmup_start, universe {id, policy, symbols, survivorship_bias_free},
  acquisitions [{batch_id, kind, provider, endpoint, params, requested_at, received_at,
  request_count}], corporate_action_claims [{symbol, list, claim {start, end,
  evidence}}], files {path: {sha256, bytes}}, manifest_sha256`. The claims list is
  derived from the corporate-action files by `write_dataset` and re-derived on load.
* **Immutability.** `write_dataset` stages next to the target, fsyncs, publishes with one
  rename, never overwrites, and leaves files read-only. `load_dataset` refuses a changed,
  missing or unlisted file, a changed manifest or an unknown schema version. The
  manifest digest is the dataset identity and enters every protocol and result
  fingerprint.
* **Calendar.** Every session must carry its settlement date (the T+2 → T+1 change on
  2024-05-28 must come from the source, never from weekday arithmetic). The calendar must
  cover `warmup_start .. research_end`.

### 2.1 Corporate-action completeness and evidence (Step 16.15A)

Each list — splits, cash distributions, other events — has its own coverage claim
`{start, end, evidence: [{kind, source, reference}]}`. An empty list means "none
occurred" only inside a claim whose evidence the `CoverageAcceptance` policy accepts:

| Evidence kind | What it is | Establishes completeness? |
|---|---|---|
| `PROVIDER_REPORTED` | the provider's own fully paginated list | **no** by default; an explicit policy may accept it, and the symbol is then labelled `PROVIDER_ONLY_COVERAGE_ACCEPTED` |
| `SAME_PROVIDER_CONSISTENCY` | internal consistency of one provider's data (its raw vs its split-adjusted bars) | **never** — corroboration, not independent verification; a policy listing it is refused |
| `INDEPENDENT_SOURCE` | a different authoritative source (exchange, issuer, licensed vendor) | yes |
| `OPERATOR_REVIEW` | a recorded human review of named evidence | yes |

A same-provider claim must be re-checkable from the dataset: for splits the
split-adjusted bars must be stored and agree with the recorded splits; otherwise, and for
any non-split list, it is `EVIDENCE_UNVERIFIABLE` (fatal). The outcome of the internal
split check is reported per symbol as `split_consistency` (PASSED / FAILED /
UNAVAILABLE) and never upgrades a claim.

Fail-closed rules (the symbol is `REJECTED`; nothing is repaired or guessed): no record;
missing, gapped or unaccepted coverage; an event outside its coverage; splits that
disagree with the raw-vs-split derivation (`SPLIT_DERIVATION_MISMATCH`); a merger,
spin-off, stock dividend, rights distribution or delisting inside the bar span
(`UNSUPPORTED_CORPORATE_ACTION` — the backtest cannot model them); a name change absent
from the identity file; and every `prepare_series` fatal finding (A§12.46, e.g. a recorded
split the prices do not show, an unrecorded split-like rise). An unrecorded split-like
*drop* is kept and flagged (`PRICE_DISCONTINUITY_UNEXPLAINED`) so genuine crashes are not
excluded. Split-adjusted bars never reach the strategy; decisions use point-in-time
split adjustment of raw bars (A§12.45). Provider cash-in-lieu prices are stored for audit;
the backtest values fractions at the prior raw close.

### 2.2 Quality assessment

Per symbol: bar count, first/last session, expected sessions (calendar sessions in
`warmup_start .. research_end`, bounded by listing/delisting), missing sessions,
zero-volume sessions, findings, and the `prepare_series` findings. Missing sessions,
late starts, early ends (possible delisting), zero volume, an unavailable split
consistency check and unexplained drops are warnings (`ACCEPTED_WITH_WARNINGS`); windows that
span a gap produce no signal. Rejected symbols stay in the research inputs with no
corporate-action record, so every backtest audits them as `SERIES_REJECTED`.

## 3. Historical data acquisition plan (`research/acquisition.py`, revised 16.15A)

`build_acquisition_plan()` produces the exact plan (fingerprint `2c4d13f2377958cc…`).
Nothing is sent by this step. **Logical requests** (one query each) are distinct from
**HTTP requests** (pages × attempts).

| Phase | Logical requests | Expected HTTP | Page cap (planning) |
|---|---|---|---|
| PROBE | 7: TRADING calendar 2020-07; SETTLEMENT calendar 2024-05-20 → 06-07; SPY raw 2020-07-01 → 07-10; AAPL raw 2020-H2 with `limit=50`; NVDA raw + split 2021-07-14 → 07-23; one `/v1/corporate-actions` call (AAPL, 2020-07 → 2021-06) | 9 | 10 |
| CORE | 42: TRADING + SETTLEMENT calendars 2020-07-01 → 2026-10-31; 20 symbols × {raw, split} daily SIP bars over the same window (`limit=10000`, ≈1,596 bars each) | 42 | 82 |
| CONDITIONAL | 9: corporate actions, one per year 2020–2026 (only if `CA-SHAPE` passes; `PROVIDER_REPORTED` evidence only); FB raw/split before 2022-06-09 (only if META's early bars are missing) | 9 | 39 |
| **Total** | **58** | **60** | **131** |

* **Pagination.** Every request follows `next_page_token` to null; a repeated or
  non-string token, or a page beyond the request's planning cap, stops the session.
  Bar caps never exceed the adapter's 20.
* **Retries.** Only 429, 503 and timeouts, at most 3 attempts per page with the same
  token (worst case 131 × 3 = 393). The shared budget of **120** binds first:
  exhausting it stops the session with nothing published.
* **Entitlement failures.** A data-host 403 (including the recent-SIP message, A§12.36),
  any other 403 or a 401 is never retried and stops the session.
* **Warm-up.** Bars before 2021-01-01 are history only and are never traded.
* **Storage.** `data/research/<dataset_id>/` (git-ignored); written once by
  `write_dataset`; responses mapped by `bars_from_alpaca_pages` (adapter validation).
* **Credentials.** The read-only paper key selected with `STB_ENV_FILE`; GET only; no
  ledger opened.

### 3.1 Capability-validation checklist (for the authorized session)

Run the probes first. Each check passes or stops (or disables) what depends on it:

| Check | Question | Probe | Passes when | On failure |
|---|---|---|---|---|
| CAL-DEPTH | 2020 trading sessions served? | P-CAL-2020 | every row parses; first session 2020-07-01 | stop |
| SETTLE-CYCLE | T+2 → T+1 change on 2024-05-28 visible? | P-SETTLE-2024 | trades before 2024-05-28 settle two settlement days later, then one | stop |
| SIP-DEPTH | SIP history for the oldest window? | P-SIP-2020 | HTTP 200, a bar for each session, `t` = market midnight | stop on 403 (no retry); new reviewed plan |
| PAGINATION | does `next_page_token` work historically? | P-PAGES | 3 pages then null; no duplicate or out-of-order bar | stop |
| SPLIT-ADJ | do raw vs adjusted bars show NVDA's 2021-07-20 4:1 split? | P-SPLIT-RAW + P-SPLIT-ADJ | `derive_split_events` finds it (same-provider consistency only) | keep raw bars; split coverage needs independent/review evidence |
| CA-SHAPE | what does `/v1/corporate-actions` return? | P-CA | a documented shape with dates and exact amounts, recorded before any importer is written | skip all corporate-action requests; coverage stays unattested and symbols are rejected |

The corporate-action endpoint is unverified: even a passing shape probe says nothing
about historical completeness. Its lists enter the dataset as `PROVIDER_REPORTED`.

### 3.1a Validation procedure (after an authorized fetch)

1. Evaluate the checklist; stop where it says stop.
2. Fetch calendars, then bars per symbol; any `InvalidBrokerDataError`, 401/403 or
   pagination anomaly stops the run with nothing published.
3. Run the same-provider split consistency check (`derive_split_events`); record it as
   `SAME_PROVIDER_CONSISTENCY` evidence, never as independent verification.
4. Corroborate splits and dividends against an independent source (§3.3) or record an
   operator review with references; until then the symbol is rejected (or the study is
   explicitly run `PRICE_RETURN_ONLY` and labelled).
5. Confirm the expected events in §1, the META identity and the settlement-cycle change.
6. `write_dataset`, then `load_dataset`; review every `SymbolQuality` before research.

### 3.2 Does SIP history cover the window?

Unverified for 2020–2023 on this account. The Step 16.5 retest read 2024-06 SIP bars
successfully; the free plan's known restriction is on *recent* SIP data (A§12.36). The
first authorized request will show it; a 403 stops the run.

### 3.3 Corporate-action completeness

Alpaca's corporate-action endpoint has never been exercised by this repository; its
history depth and completeness are unknown. Alpaca's own raw vs split bars give an
internal consistency check for splits — the same provider on both sides, so it is not
independent verification. For splits **and** dividends, an authoritative second source
(exchange or issuer records, or a licensed vendor) or a documented operator review is
required before coverage is accepted. Mergers/spin-offs for the pilot universe are expected to be
none, but "expected" is not attested.

### 3.4 Delisted symbols and historical membership

Not represented in the pilot. A survivorship-free study would need point-in-time index
membership (e.g. S&P 500 constituents by date), bars for delisted symbols (Alpaca's
coverage of delisted tickers is unverified), and `identity.json` entries with
`delisted_on`. The dataset schema already carries `listing_start`/`delisted_on`, and a
`DELISTING` event inside a bar span rejects the symbol rather than inventing an exit.

## 4. Walk-forward design (`strategy/walk_forward.py`)

Periods (`DEFAULT_PERIODS`): development 2021-01-01 → 2023-12-31; validation
2024-01-01 → 2024-12-31; final holdout 2025-01-01 → 2026-10-31.

Default `FoldDesign`: EXPANDING training from the development start, first test window
12 months in, 6-month test windows stepping 6 months, 5-session embargo, minimum 20 test
sessions. ROLLING training (`rolling_train_months`) is supported. Dates below are
resolved on the synthetic test calendar; the acquired calendar will shift a few
holiday-adjacent dates.

| Fold | Train | Embargo | Test | Train / test sessions |
|---|---|---|---|---|
| WALK_FORWARD-00 | 2021-01-04 → 2021-12-24 | 2021-12-27 → 12-31 | 2022-01-03 → 2022-06-30 | 255 / 129 |
| WALK_FORWARD-01 | 2021-01-04 → 2022-06-23 | 2022-06-24 → 06-30 | 2022-07-01 → 2022-12-30 | 384 / 130 |
| WALK_FORWARD-02 | 2021-01-04 → 2022-12-23 | 2022-12-26 → 12-30 | 2023-01-02 → 2023-06-30 | 514 / 130 |
| WALK_FORWARD-03 | 2021-01-04 → 2023-06-23 | 2023-06-26 → 06-30 | 2023-07-03 → 2023-12-29 | 644 / 128 |
| VALIDATION-04 | 2021-01-04 → 2023-12-21 | 2023-12-22 → 12-29 | 2024-01-02 → 2024-12-31 | 772 / 259 |
| HOLDOUT (sealed) | 2021-01-04 → 2024-12-23 | 2024-12-24 → 12-31 | 2025-01-02 → 2026-10-30 | 1031 / 474 |

* **No future data.** Each fold reads `SealedResearchData.view(until)`: bars, sessions and
  corporate-action events after `until` are physically removed and coverage is clipped.
  Selection sees `view(train_end)`; the test sees `view(test_end)`.
* **Warm-up.** Test windows may use earlier bars as decision history but trade only from
  `test_start`; a fold with fewer than `params.required_sessions` (60) prior sessions is
  `INSUFFICIENT_WARMUP` and not evaluated.
* **Embargo / purge.** Training ends `embargo_sessions` before the test.
* **Fingerprints.** See the frozen protocol (§4.2).
* **Parameters.** `FixedDefaultsSelector` only: the 15 research defaults, unchanged. No
  tuning plan is approved.

### 4.0 Portfolio state at fold boundaries (Step 16.15A)

| State | `INDEPENDENT_FOLDS` (default; walk-forward, validation, holdout) | `CONTINUOUS` (`run_continuous`) |
|---|---|---|
| Start of a test window | flat; template initial equity; nothing pending, unsettled or receivable | carried from the previous session |
| Open positions at the end | marked at the last close; reported as open, never wins/losses | carried; listed in the boundary snapshot |
| Pending orders | none at the end (no decision on the last session) | carried (an order decided on a boundary works the next session) |
| Unsettled proceeds | in cash and equity; reported (`end_state.unsettled_proceeds`) | settle on schedule across the boundary |
| Dividend receivables | ex-date in window, pay date after: receivable, in equity | paid on schedule |
| Splits/dividends after the end | clipped from the view; cannot affect the window | applied when they occur |
| Fold returns | reported per fold; **never chained** | one equity curve |

`CONTINUOUS` is accepted only when one replay represents it exactly: identical selected
parameters in every fold, calendar-contiguous test windows, sufficient warm-up.
Otherwise it raises `UnsupportedEvaluationModeError`; it is never approximated by
stitching. The holdout is evaluated only as an independent fold.

### 4.1 Holdout isolation

1. `view(d)` for any `d >= 2025-01-01` raises `HoldoutAccessError` without a
   `HoldoutRelease` for this dataset digest.
2. `run_walk_forward` never requests a holdout view (tested with a recording wrapper).
3. Parameter selection only ever receives a training `DataView` ending before the fold's
   embargo; the view holds no reference back to the sealed data, and the fixed selector
   refuses a view that includes holdout data.
4. `run_holdout` requires a release naming the approver, reason, date, dataset digest,
   the frozen-protocol fingerprint (§4.2) and the walk-forward report fingerprint. It
   recomputes both: any change after the release voids it. The holdout fold's own
   parameter selection sees only data before its embargo. The release fingerprint is
   recorded in the report.

### 4.2 Frozen research protocol

`freeze_protocol(sealed, protocol)` fixes, and fingerprints:

* the dataset digest and every file's SHA-256;
* the universe (id, fingerprint, symbols), the feed and the dividend policy;
* the strategy id/version, engine version and protocol version;
* the parameter values and their fingerprint;
* the execution policy: exit template, fees, slippage, entry timing, final-price policy,
  risk limits and capital;
* the reported policies (both, always) and the portfolio mode;
* every fold, including the holdout, plus the periods and design;
* the metrics version, the sample minimum and sessions per year;
* the selection procedure (`fixed-defaults-v1`, training views only);
* the release conditions.

Per-fold dates in the backtest template are not part of it.

These are code-level guarantees within this process, not access control: anyone with the
files can read 2025–2026 prices. The operating rule is that no one runs holdout
evaluations, or inspects holdout-period charts for this strategy, before a recorded
release (RUNBOOK §14).

## 5. Metrics (`strategy/evaluation.py`)

Signals, rejection counts by reason, entry orders, filled / unfilled (expired, no data,
gap through stop, split-cancelled) / blocked (capacity, stale mark, sized zero, minimum
price, invalid intent), completed and open trades, win rate, average R, expectancy,
profit factor (gross profit / gross loss), total and annualized return (≥ 252 sessions,
valued), maximum drawdown, exposure and average gross exposure, holding duration,
fees, realized / unrealized / dividend P&L, the equity curve, and breakdowns by symbol,
sector and exit year with calendar-year returns. Undefined metrics are `None` with a
`ReportNote` (no signals, no trades, no completed trades, no losses, short period,
valuation incomplete, sample below 30 completed trades, hypothetical policy, ambiguous
intrabar ordering, rejected series). `cost_sensitivity` reruns identical data under
alternative slippage and fee assumptions.

Both exit policies are always reported separately: `PROTECTION_FIRST` (executable
through the current OMS: stop only, partial profits deferred) and
`HYPOTHETICAL_LINKED_EXITS` (not executable: needs verified linked orders). On the
synthetic fixtures `PROTECTION_FIRST` completes almost no trades because it has no
profit exit — a design gap (A§12.45 open decision 1), not a result.

## 6. Execution-readiness backlog (recorded, not implemented)

* `risk/pretrade.py` must reject an entry when the current executable price is at or
  below the planned stop (modelled in the backtest only, A§12.46).
* The integrated OMS has no verified linked exits (bucket stops + OCO).
* Broker-native stop and OCO semantics are unvalidated at Alpaca.
* Account protection settings (U6: `no_shorting`, `max_margin_multiplier=1`) still need
  correcting on the real account.
* Real-ledger migration 0009 requires an explicit, backed-up authorization (the real
  ledger is at schema 8).
* Deterministic real-market observation ids and evidence provenance need an adapter.
* Paper execution remains unauthorized.

No backtest number — synthetic or historical, either policy — changes any item above.

## 7. Authorization needed before acquisition

A separate, explicit authorization naming:

1. the plan fingerprint to execute (`2c4d13f2377958cc…`, §3);
2. the credential file to select via `STB_ENV_FILE`;
3. read-only GETs to the data and trading hosts within the existing 120-request / 300 s
   budget (60 expected HTTP requests);
4. that the six probes run first and the checklist (§3.1) gates everything else;
5. the destination `data/research/<dataset_id>/`, with no ledger access;
6. how split and dividend coverage will be attested (independent source or operator
   review — same-provider consistency does not count).

## 8. Operational deviation (Step 16.15)

During Step 16.15 the real ledger was opened read-only, outside scope, which left empty
WAL/SHM sidecars. The main file was unchanged. Full record, cleanup recommendation and
preventive controls: ARCHITECTURE §12.48 ¶1 and RUNBOOK §14.
