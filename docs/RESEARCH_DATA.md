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

## 3. Historical data plans (`research/acquisition.py`, revised 16.16B and 16.16E)

There are two separate, fingerprinted plans. Nothing is sent by building either.
**Logical requests** (one query each) are distinct from **HTTP requests** (pages ×
attempts).

### 3.0 Capability probe (Step 16.16B) — `build_capability_probe_plan()`

Plan `history-capability-probe-v1`, fingerprint
`1bfd4451e9df58e7a7b6de64c072bafc8298c3e509db05af15cd592386909931`. Runner:
`tests/contract/history_probe.py` (authorized operator only; RUNBOOK §15). It runs these
six requests and nothing else, then stops. It never continues into acquisition and writes
no dataset.

| # | Method, host, path | Query | Pages (expected / cap) |
|---|---|---|---|
| P1 | GET `paper-api.alpaca.markets` `/v2/calendar` | `start=2020-07-01&end=2026-10-31&date_type=TRADING` | 1 / 1 |
| P2 | GET `paper-api.alpaca.markets` `/v2/calendar` | `start=2020-07-01&end=2026-10-31&date_type=SETTLEMENT` | 1 / 1 |
| P3 | GET `data.alpaca.markets` `/v2/stocks/bars` | `symbols=AAPL&timeframe=1Day&start=2020-07-01T03:59:59Z&end=2020-12-31T05:00:01Z&feed=sip&adjustment=raw&limit=100&sort=asc` (+`page_token`) | 2 / 2 |
| P4 | GET `data.alpaca.markets` `/v2/stocks/bars` | `symbols=AAPL&…&start=2020-08-24T03:59:59Z&end=2020-09-04T04:00:01Z&adjustment=split&limit=10000` | 1 / 1 |
| P5 | GET `data.alpaca.markets` `/v2/stocks/bars` | `symbols=META&…&start=2022-06-06T03:59:59Z&end=2022-06-10T04:00:01Z&adjustment=raw&limit=10000` | 1 / 1 |
| P6 | GET `data.alpaca.markets` `/v1/corporate-actions` | `symbols=AAPL,SPY&types=forward_split,cash_dividend&start=2020-07-01&end=2021-06-30&limit=5&sort=asc` (+`page_token`) | 2 / 2 |

* **Budget.** 8 expected HTTP requests; a hard cap of **10 actual attempts** (pages and
  retries together; the 11th is refused before it is sent); a 120 s deadline.
* **Timeouts and retries.** Connect 5 s and read 20 s, cut to the time left. At most one
  retry per page, only after 429, 503 or a timeout. Backoff is 0.5 s, raised to a
  `Retry-After` value, capped at 4 s.
* **Stops.**
  * 401 or 403 (never retried).
  * 404 on P1–P5, any other 4xx, a 5xx other than 503, or a network error.
  * A redirect or a response from another host.
  * Budget or deadline exhausted.
  * A 404 on P6 makes `CA-SHAPE` `UNAVAILABLE`; no fallback endpoint is tried.
* **Expected counts come from the calendar.** P3's expected sessions and page layout are
  derived from P1, never hard-coded.
* **Holdout.** No probe reads a 2025–2026 price; only the calendars span that period.

**Executed once in Step 16.16D** (evidence `contract-evidence/history-probe-20261117T025839Z/`):
8 GETs, all HTTP 200, 1.9 s; every check `VERIFIED`. Limits of that evidence: SIP depth
and pagination were shown for AAPL 2020-H2 only; the split check is same-provider; the
corporate-action list is provider-reported; P6 showed the endpoint does not filter on
the ex-date (§3.3a).

### 3.0a Acquisition — `build_acquisition_plan()`

Fingerprint `6ca96c0601a404b19bd5adac44bc1e46141a9febd9c66db7b9438a7380ae5a3f`
(16.16E; was `c4d5d576…5b7e9`). It names the probe plan's id and fingerprint and the
proposed `ca-date-semantics-probe-v1` (§3.3a), runs only after the probe's checks pass,
and needs its own authorization (§7).

| Phase | Logical requests | Expected HTTP | Page cap (planning) |
|---|---|---|---|
| CORE | 42: TRADING + SETTLEMENT calendars 2020-07-01 → 2026-10-31; 20 symbols × {raw, split} daily SIP bars over the same window (`limit=10000`, ≈1,596 bars each) | 42 | 82 |
| CONDITIONAL | 9: corporate actions, one per ex-date year 2020–2026, requests padded −31/+92 days (only if `CA-SHAPE` passes; `PROVIDER_REPORTED` evidence only, §3.3a); FB raw/split before 2022-06-09 (only if `TICKER-IDENTITY` failed — it passed in 16.16D, so these are skipped) | 9 | 39 |
| **Total** | **51** | **51** | **121** |

* **Pagination.** Every request follows `next_page_token` to null; a repeated or
  non-string token, or a page beyond the request's planning cap, stops the session.
  Bar caps never exceed the adapter's 20.
* **Retries.** Only 429, 503 and timeouts, at most 3 attempts per page with the same
  token, and at most `pages + 2` attempts per batch (§3.0b). The session budget of
  **120** attempts / **300 s** is unchanged; a batch whose whole budget would not fit is
  left for a later session.
* **Request accounting** (`request_accounting(plan, gates)`, Step 16.16E.1). Every
  number is derived from the batch budgets the runner enforces; the planner's
  `worst_case_http_requests` is their sum. (The pages × 3 per-request bound of 16.16B,
  363 in total, was stale and has been removed.)

| Batch kind | Batches | Expected pages | Max pages | Retry allowance | Max attempts / batch | Worst case |
|---|---|---|---|---|---|---|
| Calendar (TRADING, SETTLEMENT) | 2 | 1 | 1 | 2 | 3 | 6 |
| Bars (20 symbols × raw/split) | 40 | 1 | 2 | 2 | 4 | 160 |
| Bars, FB fallback (only if `TICKER-IDENTITY` failed) | 2 | 1 | 2 | 2 | 4 | 8 |
| Corporate actions (one per ex-date year) | 7 | 1 | 5 | 2 | 7 | 49 |

| Total | All 51 batches | Eligible under 16.16D gates (49; FB skipped) |
|---|---|---|
| Expected requests | 51 | **49** |
| Page-cap requests (no retries) | 121 = 2·1 + 42·2 + 7·5 | **117** = 2·1 + 40·2 + 7·5 |
| Retry-inclusive worst case | 223 = 2·3 + 42·4 + 7·7 | **215** = 2·3 + 40·4 + 7·7 |
| Maximum attempts per session | 120 (300 s) | 120 (300 s) |
| Sessions, expected use | 1 | **1** |
| Sessions, every batch at its budget | 2 | **2** |

  Session counts simulate the runner's rule (a batch starts only if its whole budget
  still fits, `sessions_needed`); session time is not modelled, so a slow provider can
  need more sessions, each still within 120 attempts / 300 s. 16.16E's report called 51
  the expected count with FB skipped; 51 includes the two FB batches, 49 is correct.
* **Entitlement failures.** A data-host 403 (including the recent-SIP message, A§12.36),
  any other 403 or a 401 is never retried and stops the session.
* **Warm-up.** Bars before 2021-01-01 are history only and are never traded.
* **Storage.** Staging `data/research/staging/<plan fingerprint prefix>/`; the dataset
  `data/research/<dataset_id>/` (git-ignored), written once by `write_dataset`;
  responses mapped by `bars_from_alpaca_pages` (adapter validation).
* **Credentials.** The read-only paper key selected with `STB_ENV_FILE`; GET only; no
  ledger opened.

### 3.0b Resumable acquisition (`research/staging.py`, Step 16.16E)

| Item | Rule |
|---|---|
| Batch | one logical request; id `<request_id>.<12 hex>` over the request and its boundaries; explicit symbols, request and coverage dates |
| Per-batch budget | `min(pages + 2, pages × 3)`: calendar 3, bars 4, corporate actions 7 |
| Session | 120 attempts / 300 s (never raised); every attempt counted before it is sent; a batch starts only if its whole budget and 30 s fit |
| Eligibility | `requires_passed` gates must be `True`; FB batches need `TICKER-IDENTITY` `False`; a missing gate is neither (fail closed) |
| Staging | `store.json`, `blobs/<sha256>.json`, `batches/<id>.json`, `tmp/`, `quarantine/`; files written to `tmp/`, fsynced, then linked (never replaced), read-only |
| Integrity | record digest + page SHA-256s + page-token chain + plan fingerprint, re-verified on open, resume and assembly; `TAMPERED_BATCH`, `UNKNOWN_BATCH_RECORD`, `INCOMPATIBLE_PLAN` fail closed |
| Interruption | `session.lock` or in-flight files block sessions and publication until `recover(label)` quarantines them and re-verifies every batch |
| Publication | only when every eligible batch verifies and no ineligible one is staged; `write_dataset` (one rename, no overwrite), then `load_dataset`; no backtest, holdout view or tuning |

**Storage structure** after publication: §2's layout, with `manifest.acquisitions`
listing every batch (id, endpoint, parameters, request/receipt time, attempts) and
`manifest.acquisition_report` holding `acquisition_state: ACQUISITION_COMPLETE`, every
required batch with its page SHA-256s and attempts, the request accounting and attempts
actually used, the plan fingerprint, gate outcomes, date-filter semantics (with the
action types a verified role covers), padding, per-symbol corporate-action claims and
every import finding.
`quality_report(dataset)` lists all symbols with status and reasons; rejected ones are
never dropped.

### 3.1 Capability-validation checklist (answered by the probe)

Each check passes or stops (or disables) what depends on it:

| Check | Question | Probe | Passes when | On failure |
|---|---|---|---|---|
| CAL-DEPTH | every research-window session served? | P1 | rows parse; strictly increasing, no weekend; first 2020-07-01, last 2026-10-30; listed closures absent; 2020-11-27 closes 13:00 | stop |
| SETTLE-CYCLE | T+2 → T+1 change on 2024-05-28 visible? | P2 | trades before 2024-05-28 settle on the 2nd listed settlement day, from then on the 1st; 2024-05-24 and 2024-05-28 both settle 2024-05-29; agrees with P1's `settlement_date` | stop |
| SIP-DEPTH | SIP history for the oldest window? | P3 | one bar per P1 session in 2020-H2, `t` = market midnight | stop on 403 (no retry); new reviewed plan |
| PAGINATION | does `next_page_token` work historically? | P3 | `ceil(sessions/100)` pages of the expected sizes, then null; no repeated token, duplicate or out-of-order bar | stop |
| SPLIT-ADJ | do raw vs adjusted bars show AAPL's 2020-08-31 4:1 split? | P3 + P4 | `derive_split_events` finds exactly it (same-provider consistency only) | keep raw bars; split coverage needs independent/review evidence |
| TICKER-IDENTITY | does META return bars before the 2022-06-09 change? | P5 | a META bar for every P1 session 2022-06-06..10 | the FB conditional requests are needed |
| CA-SHAPE | what does `/v1/corporate-actions` return? | P6 | an object of typed lists plus `next_page_token`; shape, value types, limit semantics and pagination recorded before any importer is written | skip all corporate-action requests; coverage stays unattested and symbols are rejected |

The corporate-action endpoint is unverified: even a passing shape probe says nothing
about historical completeness. Its lists enter the dataset as `PROVIDER_REPORTED`.

### 3.1a Validation procedure (after an authorized fetch)

1. Review the probe evidence against the checklist (§3.1); stop where it says stop.
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

### 3.3a Corporate-action dates (Step 16.16E, `research/corporate_actions.py`)

Six dates are kept apart: the request window (padded), the provider's filter date, the
ex-date, the effective date (ex-date for splits and dividends), the payable date (the
dividend's pay date) and the research coverage window (selected by effective date).

* **What is known.** 16.16D's P6 returned SPY's 2020-06-19 ex-date dividend for
  `start=2020-07-01`; its record date was 06-22 and its payable and process dates 07-31.
  So the filter is neither the ex-date nor the record date; payable or process date
  remain. SPY's June-2021 dividend (paid end of July) was absent from the `end=2021-06-30`
  request, consistent with either.
* **Policy.** Requests are padded 31 days before and 92 days after each ex-date year;
  events are kept by ex-date; pad-zone events must be confirmed by the batch owning
  their date; duplicates are merged by provider id; conflicting or ambiguous duplicates
  fail closed for their symbol and list. Observed lags must stay 14 days inside the
  padding for every possible filter role.
* **Gate `CA-DATE-SEMANTICS`.** Without live verification no `PROVIDER_REPORTED`
  coverage claim is made (findings `DATE_SEMANTICS_UNVERIFIED`, per list), so
  corporate-action coverage then rests on independent or operator-review evidence or the
  symbol is rejected. Proposed check `ca-date-semantics-probe-v1`
  (`e586916efa62c41591bde68842b554707e19aeff4b54fc8b10f1174a7c1c4efa`, unchanged by
  16.16E.1). Reference events, with every date copied from the 16.16D evidence
  (`raw/006`, `raw/007`; nothing inferred):

| Event | Ex | Record | Payable | Process |
|---|---|---|---|---|
| AAPL 4:1 forward split | 2020-08-31 | 2020-08-24 | **2020-08-28** | **2020-08-31** |
| SPY cash dividend 1.3662419 | 2020-06-19 | 2020-06-22 | 2020-07-31 | 2020-07-31 |

  Expected responses (event returned = T), derived in code from those dates
  (`hypothesis_table()`):

| # | Single-day request (`/v1/corporate-actions`) | ex | record | payable | process | any date |
|---|---|---|---|---|---|---|
| Q1 | AAPL `forward_split` 2020-08-28 | F | F | **T** | **F** | T |
| Q2 | AAPL `forward_split` 2020-08-31 | T | F | **F** | **T** | T |
| Q3 | SPY `cash_dividend` 2020-07-31 | F | F | T | T | T |

  * **Payable vs process: distinguishable, for forward splits only.** Q1/Q2 use the only
    observed event whose payable and process dates differ. (T,F,T) ⇒ payable,
    (F,T,T) ⇒ process. Every hypothesis gives a different pattern.
  * **Q3 is a positive control.** It separates {payable, process} from {ex, record} and
    shows a single-day window returns anything (an all-F answer, the record-date
    pattern, is indistinguishable from a refused or exclusive window and is
    `INCONCLUSIVE`). It cannot separate payable from process for dividends: all nine
    dividends observed in 16.16D have payable = process.
  * **Scope.** A verified role is recorded with `scope = ("forward_splits",)`; for every
    other action type both payable and process stay candidates and both lags are
    checked. The `splits` list also covers unprobed reverse splits and the
    `other_events` list ten unprobed types, so **a successful probe alone unblocks no
    `PROVIDER_REPORTED` claim**. With RESEARCH_READY requiring attestations (§3.3b), the
    gate is off the critical path.
  * **Discovery (not authorized, not implemented as a runner).** A dividend-scope
    verification needs a cash dividend whose payable and process dates differ. None is in
    the evidence. Bounded options, each needing its own authorization: (a) after an
    authorized acquisition, scan the staged corporate-action pages offline with
    `discriminating_events()` (no request), then (b) one further single-day probe on any
    event found (≤ 2 requests, ≤ 4 attempts, 60 s, data host only).

  ≤ 5 attempts, 60 s. No runner exists; it needs its own implementation review and
  authorization.
* **Padding is not completeness.** Even a verified, unblocked provider claim is
  `PROVIDER_REPORTED` and is rejected by the default acceptance policy.

### 3.3b Acquisition complete vs research ready (Step 16.16E.1)

Two gates, assessed by `assess_readiness(dataset)` on every loaded dataset (states
`ACQUISITION_INCOMPLETE` → `ACQUISITION_COMPLETE` → `RESEARCH_READY`):

| Gate | Passes when | Blockers (examples) |
|---|---|---|
| `ACQUISITION_COMPLETE` | the manifest's `acquisition_report` says `ACQUISITION_COMPLETE`; every required batch is listed in `acquisitions` with page SHA-256s, and no other batch is; every file verified its checksum on load (else load fails) | `ACQUISITION_REPORT_MISSING`, `REQUIRED_BATCH_ABSENT`, `REQUIRED_BATCH_UNCHECKSUMMED`, `UNREQUIRED_BATCH` |
| `RESEARCH_READY` | acquisition complete; **every** universe symbol passed the quality checks; every symbol's splits, dividends and other-events coverage spans warm-up start → research end with `INDEPENDENT_SOURCE` or `OPERATOR_REVIEW` evidence | `SYMBOL_REJECTED <sym>`, `ATTESTATION_MISSING <sym> <list>`, `PROVIDER_ONLY_COVERAGE <sym>` |

* A complete dataset can stay not research-ready (e.g. one rejected symbol, or only
  provider-reported coverage even under a lenient `CoverageAcceptance`).
* `ResearchDataset.research_inputs()` is the only bridge to evaluation and raises
  `DatasetNotResearchReadyError` (carrying every blocker) unless RESEARCH_READY. It
  attaches a `ResearchAdmission` (digest, state, readiness fingerprint).
* `SealedResearchData` refuses inputs without an admission for the same digest in state
  `RESEARCH_READY` (`ResearchAdmissionError`), so `freeze_protocol`, `run_walk_forward`,
  `run_continuous` and `run_holdout` cannot run on anything else. The readiness
  fingerprint is part of the frozen protocol, so a holdout release is bound to it.
* `quality_report()` and `readiness()` need no admission: rejected symbols and every
  blocker stay inspectable.
* Excluding a rejected symbol to reach RESEARCH_READY means a new universe (new
  fingerprint and plan); there is no per-dataset exclusion switch.

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

## 7. Authorizations still needed

1. **The capability probe** (RUNBOOK §15): an explicit statement naming the probe-plan
   fingerprint (§3.0), the runner, the credential file selected with `STB_ENV_FILE`,
   the two hosts and three endpoints, the 10-attempt / 120 s limits, and the evidence
   location. No ledger access, broker write, trading enablement or dataset.
2. **CA-DATE-SEMANTICS check** (optional; not needed for RESEARCH_READY, §3.3b): an
   implemented and reviewed runner for `ca-date-semantics-probe-v1` (§3.3a), then a
   statement naming its fingerprint, ≤ 5 attempts / 60 s, the data host and the one
   endpoint. It can verify the filter role for forward splits only.
3. **Acquisition**, after the probe's evidence has been reviewed (done for 16.16D) and a
   live runner around `research/staging.py` has been implemented and reviewed. A
   separate statement naming:
   1. the acquisition-plan fingerprint (§3.0a);
   2. the credential file;
   3. read-only GETs, each session within 120 attempts / 300 s (49 expected, 117 at
      page caps, 215 worst case under the 16.16D gates; §3.0a), and the maximum number
      of sessions (2 when nothing stops);
   4. the probe evidence and gate outcomes that gate it (§3.1, §3.3a);
   5. the staging and destination directories under `data/research/`, with no ledger
      access;
   6. how split and dividend coverage will be attested (independent source or operator
      review; same-provider consistency and unverified provider claims do not count).

## 8. Operational deviation (Step 16.15)

During Step 16.15 the real ledger was opened read-only, outside scope, which left empty
WAL/SHM sidecars. The main file was unchanged. Full record, cleanup recommendation and
preventive controls: ARCHITECTURE §12.48 ¶1 and RUNBOOK §14.
