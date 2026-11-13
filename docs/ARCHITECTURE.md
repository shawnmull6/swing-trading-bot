# Swing Trading Bot — Architecture

Status: **Revision 3 — architecture approved; all decisions (D1–D15, U1–U9) final.
M1 steps 1–13.2 implemented; steps 14 and 14.1 approved (§12.20,
§12.21); step 15 phase A accepted (§12.22); step 15A.1 (live-run harness hardening and
reconciliation retry safety, §12.23) committed; the first supervised live read-only run
(phase B, 2026-11-12) stopped in setup on a missing account field; steps 15B.1–15B.3
(account payload compatibility and exit-code consistency, §12.24) committed; live run #2
stopped in setup on missing configuration fields; step 15B.4 (configuration payload
compatibility, §12.25) committed; live run #3 completed with C4 FAILED on a settlement
calendar mapping error; step 15B.5A (settlement calendar mapping correction, §12.26)
committed; step 15B.5B (activity description compatibility and wrapped payload errors,
§12.27) committed; step 15B.5B.1 (activity raw-JSON serialization, §12.28) committed;
step 15B.5C (live contract evidence sanitization, §12.29) implemented offline and
awaiting review. Live run #4 needs separate authorization.** Decision log: `docs/M1_PLAN.md` §2.

Scope: the core infrastructure delivered in Milestone M1 and the seams later milestones
(signals, strategy, TradePlan lifecycle "RTH-01", paper execution) plug into.

---

## 1. Guiding principles

1. **Paper only.** The only broker integration is Alpaca *paper*. No code path, flag or
   config value reaches a live endpoint. `TRADING_MODE` must equal `paper`.
2. **The virtual ledger is the bot's bank account.** Alpaca's $100,000 equity and
   $400,000 buying power are never used for sizing. Sizing, affordability and risk
   percentages use the virtual portfolio ledger, which starts at **$5,000.00**.
3. **The broker is the source of truth for what happened; the ledger is the source of
   truth for what the bot is allowed to do.** Nothing is booked as filled until the
   broker reports an execution — and every confirmed execution *is* booked, even if it
   breaks a ledger invariant (§5.5).
4. **Fail closed.** Uncertainty blocks new orders. Severe or unexplained conditions set
   a **persistent halt** that only an operator can clear (§7).
5. **Idempotent everywhere.** Every external event has a unique key and is applied at
   most once; replay after a crash converges to the same state.
6. **No network I/O inside a DB transaction**, and no state change outside one.
7. **Secrets never leave `.env`.** `SecretStr`, never logged, printed, repr'd or stored.
8. **External guidance is reference only.** External agent skills and vendor guidance
   (e.g. Alpaca Skills, pinned and reviewed in `docs/EXTERNAL_REFERENCES.md`) are never
   installed and never override the OMS, the virtual ledger, risk gates, persistent
   halts, credential handling or broker-write authorization.

---

## 2. Repository state (inspected 2026-11-09; historical — see §12 for the as-built system)

| Item | State |
|---|---|
| Tooling | Python 3.12, `uv`, `uv_build`, src layout |
| Runtime deps | `alpaca-py 0.45.0`, numpy, pandas, pydantic 2, python-dotenv, pyyaml |
| Dev deps | pytest, pytest-cov, mypy, ruff (+ `hypothesis` approved, D13) |
| Package | `src/swing_trading_bot/__init__.py` — hello-world `main()` |
| Scripts | `scripts/check_connection.py` — read-only account check (removed in step 15; replaced by `broker-check`) |
| Config | `.env` (gitignored): `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TRADING_MODE`, `TRADING_ENABLED`, `INITIAL_CAPITAL`, `MAX_CAPITAL_ALLOCATION` |
| Tests / config / data | empty `tests/unit`, `tests/integration`, `config/`, `data/` (gitignored) |
| Operator item | `alpaca.txt` at repo root (gitignored, not read) — operator cleaning up (D15) |

---

## 3. Verified Alpaca / alpaca-py behavior (source inspection of alpaca-py 0.45.0)

These facts come from reading the installed SDK, not from live calls. Items marked
**CT** still require a contract test against the paper API (M1_PLAN §5.3; matrix in §12.22).

| # | Finding | Design consequence |
|---|---|---|
| V1 | `TradingClient(paper=True)` hard-selects `https://paper-api.alpaca.markets`; base URL changes only via `url_override`; no env-var override in `common/` or `trading/`. | Adapter never passes `url_override`; asserts the resolved base URL equals the paper URL at construction. |
| V2 | **The SDK silently retries any request — including `POST /orders` — on HTTP 429 and 504**, 3 attempts, 3 s apart. `TradingClient.__init__` does not expose retry settings, and `RESTClient` ignores `retry_attempts=0` / `retry_exception_codes=[]` (falsy checks). | Our resilience layer must account for SDK retries on reads. Before any write is ever enabled (M2), the write path must disable SDK retries (subclass/override `_retry_codes`) so submission retries are owned by our recovery protocol. A 504 on submit must be treated as **ambiguous**, never as failure. |
| V3 | `TradingClient` 0.45 has **no account-activities method**. Activity models (`TradeActivity`, `NonTradeActivity`) type `price`, `qty`, `net_amount` as **float**. | Adapter calls `GET /v2/account/activities` through the client's low-level `get()` and parses the raw JSON strings directly into `Decimal`. SDK activity models are not used. |
| V4 | `Order.qty`, `filled_qty`, `filled_avg_price`, `limit_price` are typed `Union[str, float, None]`; `TradeAccount` and `Position` money fields are `str`. | Adapter parses from raw/str values into `Decimal`; a value arriving as `float` is a mapping error → halt. **CT**: confirm values arrive as strings. |
| V5 | `get_order_by_client_id` → `GET /v2/orders:by_client_order_id`. | Primary lookup for ambiguous orders. **CT**: exact error type/status for an unknown ID (expected 404) and visibility latency for a just-submitted order. |
| V6 | Order statuses in SDK: `new, partially_filled, filled, done_for_day, canceled, expired, replaced, pending_cancel, pending_replace, pending_review (deprecated), accepted, pending_new, accepted_for_bidding, stopped, rejected, suspended, calculated, held`. | Exhaustive explicit mapping in `domain/broker_status.py`; an unmapped status → persistent halt. A unit test enumerates the SDK enum so a new SDK value fails the build. |
| V10 | `Position` has `current_price` but **no timestamp**. Market-data `Trade` (latest trade) has `timestamp`, but types `price` as float. | `Position.current_price` is never a mark (U7); used only as a cross-check. Marks come from the latest-trade / latest-bar endpoints parsed raw into `Decimal` with their exchange timestamp (§6.6). **CT**: feed (IEX vs SIP) available to this account and timestamp semantics. |
| V7 | `get_calendar(GetCalendarRequest(date_type=SETTLEMENT|TRADING))`; `Calendar` has `settlement_date`; docstring says data covers 1970–2029. *Observed 2026-11-12 (§12.26):* `date_type=SETTLEMENT` returns **trade-date** rows (`date` = session, `settlement_date` = its settlement) selected by `settlement_date` in range; settlement days are the `settlement_date` values. | Settlement dates come from Alpaca's calendar, cached locally. **CT**: paper returns `settlement_date` populated; behavior on bank holidays when markets are open (e.g. Veterans Day). Missing calendar data → proceeds stay unsettled (fail closed). Calendar horizon (2029) noted as a future risk. |
| V8 | `AccountConfiguration` exposes `no_shorting`, `max_margin_multiplier` ("1"–"4"), `pdt_check`, `dtbp_check`, `suspend_trade`, `fractional_trading`. `TradeAccount` (the SDK model) exposes `cash`, `non_marginable_buying_power`, `multiplier`, `pattern_day_trader`, `daytrade_count`, `trading_blocked`, `account_blocked`, `accrued_fees`, `pending_reg_taf_fees`. *Observed 2026-11-12 (§12.24, §12.25):* the real paper `/v2/account` response omitted `pattern_day_trader` and `daytrade_count`; `/account/configurations` omitted `pdt_check` and `dtbp_check` and returned 8 fields. *Note:* the installed alpaca-py 0.45.0 itself marks `pattern_day_trader`, `daytrade_count`, `daytrading_buying_power`, `pdt_check` and `dtbp_check` deprecated ("removed from Alpaca responses on 2026-07-06", FINRA intraday-margin migration) — the original V-table inspection missed those markers. | Startup preflight reads (never writes) these and reports them (U6: report-only in M1; `no_shorting=true` and `max_margin_multiplier="1"` become **required** before M2 paper execution); `trading_blocked`/`account_blocked` → block. `pending_reg_taf_fees`/`accrued_fees` feed cash explainability (§8.3). **CT**: whether paper simulates regulatory fees at all. |
| V9 | `Position.qty_available` exists (shares not held by open sell orders). | Relevant to future protective stops/partial exits (§9). |

Assumptions that **cannot** be contract-tested in M1 because they require submitting an
order (deferred to the M2 pre-execution contract suite): duplicate `client_order_id`
rejection status/message; fill-activity id uniqueness and latency relative to order
status; partial-fill activity granularity; behavior of a 504 on submit.

---

## 4. Module layout

```
src/swing_trading_bot/
  config/      settings.py (paper-only), risk_limits.py, operating_policy.py
  domain/      money.py, enums.py, ids.py, models.py, state_machine.py,
               broker_status.py, calendar.py (step 5)
  broker/
    ports.py           # BrokerReadPort, BrokerExecutionPort, PriceMarkSource
    errors.py          # error taxonomy
    alpaca_read.py     # AlpacaPaperReadAdapter: implements BrokerReadPort ONLY
    alpaca_marks.py    # read-only market-data PriceMarkSource (latest trade / daily bar)
    sim.py             # SimBroker: implements Read + Execution ports, in memory
    resilience.py      # backoff for reads, circuit breaker, SDK-retry awareness
  ledger/      db.py, migrations/0001_init.sql, repository.py, portfolio.py, invariants.py
  risk/        pretrade.py, exposure.py, loss_limits.py, gates.py (startup gate, blocks, halts)
  oms/         order_manager.py, execution.py, lifecycle.py, sync.py, recovery.py (step 12);
               classify.py, reconciler.py (step 13); rebaseline.py (step 13.1)
  ledger/      … baselines.py, acknowledgements.py (steps 13-13.2)
  observability/ logging.py (JSON + secret redaction)
  app/         runtime.py, cli.py, commands.py, lock.py, … (step 14, §12.20)
```

*As built:* `broker/alpaca_marks.py` and `broker/resilience.py` do not exist — marks and
bounded read retries live in `broker/alpaca_read.py` / `broker/alpaca_http.py`; the broker-outage
circuit breaker is in `ledger/broker_outage.py` (§12.21). `risk/exposure.py` and `risk/loss_limits.py`
do not exist — exposure is in `ledger/portfolio.py`, loss limits in `ledger/loss_limits.py`.
The repository module is `ledger/repositories.py`.

Dependency direction: `app → oms → risk → ledger → domain`; `oms → broker → domain`.
`broker` never imports `ledger`. Future strategy / TradePlan code depends on `oms`
only.

---

## 5. Broker ports and execution modes

### 5.1 Two ports instead of one

*Sketch; the authoritative signatures are in `broker/ports.py` (§12.9–12.10).*

```python
class BrokerReadPort(Protocol):
    def get_account(self) -> AccountSnapshot: ...  # never used for sizing
    def get_account_config(self) -> AccountConfigSnapshot: ...
    def get_clock(self) -> MarketClock: ...
    def get_calendar(self, start: date, end: date) -> list[SessionDay]: ...
    def get_positions(self) -> list[BrokerPosition]: ...
    def get_open_orders(self) -> list[BrokerOrder]: ...
    def get_orders(self, after: datetime, status: Literal["all"]) -> list[BrokerOrder]: ...
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def get_activities(self, after_id: str | None, page_token: str | None) -> ActivityPage: ...


class BrokerExecutionPort(Protocol):
    def submit_order(self, req: SubmitOrderRequest) -> BrokerOrder: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
```

### 5.2 Simulation-only order manager vs execution-disabled Alpaca adapter

These are two different things and M1 delivers both:

| Component | What it is | In M1 |
|---|---|---|
| **`OrderManager`** | Broker-agnostic order lifecycle engine (intent → risk → reservation → submit → recovery → fills → terminal). Requires a `BrokerExecutionPort` and a `BrokerReadPort`. | Fully implemented, but only ever constructed with **`SimBroker`** as its execution port, against a **separate simulation ledger file**. Used by tests and by `cli simulate` scenarios. |
| **`AlpacaPaperReadAdapter`** | Read-only Alpaca integration. Implements `BrokerReadPort` **only**; it has no `submit_order`/`cancel_order` methods at all. | Used by `sync`, `reconcile`, `status`, preflight against the production ledger. |

"Execution disabled" is therefore **structural**, not a runtime flag: in M1 there is no
class that implements `BrokerExecutionPort` against Alpaca, so the production runtime
cannot build an `OrderManager` wired to Alpaca. A test asserts this (no
`BrokerExecutionPort` implementation imports `alpaca`). M2 adds an
`AlpacaPaperExecutionAdapter` as a separate, reviewed change.

### 5.3 Runtime modes

| Mode | Read port | Execution port | Ledger file | M1 |
|---|---|---|---|---|
| `observe` | Alpaca paper | none | `data/ledger.sqlite3` | ✅ |
| `simulate` | SimBroker | SimBroker | `data/sim/<run>.sqlite3` | ✅ (tests/CLI) |
| `paper_execute` | Alpaca paper | Alpaca paper | `data/ledger.sqlite3` | ❌ M2+ |

Each ledger file records its mode in `portfolios.mode`; opening a ledger with a
different mode is refused, so simulated fills can never mix with the production ledger.

---

## 6. Virtual portfolio accounting

### 6.1 Definitions (all `Decimal`; stored as TEXT; no REAL columns)

| Quantity | Definition |
|---|---|
| **cash** | Σ `cash_entries.amount` |
| **unsettled proceeds** | Σ SELL cash entries with `settles_on` > current settlement date |
| **settled cash** | cash − unsettled proceeds |
| **buy commitments** | Σ ACTIVE `cash_reservations.remaining` (working buy orders) |
| **available cash** | settled cash − buy commitments |
| **position qty / cost basis** | per symbol from fills; average-cost method (D5) |
| **deployed capital** | Σ open-position cost basis + buy commitments |
| **capital headroom** | `MAX_CAPITAL_ALLOCATION` (5,000) − deployed capital |
| **buying capacity** | min(available cash, capital headroom) — the only input to buy sizing |
| **position market value** | qty × fresh mark (§6.6) |
| **market exposure** | Σ position market value + buy commitments (tracked and reported; U8) |
| **market value** | Σ position market value (stale marks block new entries) |
| **equity** | cash + market value (profits compound here) |
| **realized P&L** | per sell fill: (price − avg cost) × qty − fees |
| **unrealized P&L** | Σ (mark − avg cost) × qty |

### 6.2 Capital rules (D8)

* `INITIAL_CAPITAL = 5000`, `MAX_CAPITAL_ALLOCATION = 5000`.
* **Equity compounds**: realized and unrealized profits stay in virtual equity and are
  the base for every risk percentage (position %, planned risk %, loss limits).
* **Deployed capital is capped at $5,000** (cost basis of open positions + working buy
  commitments). The ceiling is a constant from config; it never rises automatically.
  Changing it requires a config change and a recorded operator event.
* When equity < $5,000, available cash is the binding constraint; when equity > $5,000,
  the ceiling is. Either way `buying capacity` = the smaller of the two.
* Deployed capital uses **cost basis**, not market value, so appreciation of an open
  position does not consume headroom and depreciation does not free it (U8).
* **Market-value exposure is tracked alongside** (U8): position market value and total
  market exposure are computed from fresh marks on every evaluation and reported by
  `status`. **Concentration limits use market value**: a buy is rejected if the
  resulting position's market value (existing qty × mark + new qty × limit) would
  exceed 20% of virtual equity. A position that drifts above 20% through appreciation
  is flagged and cannot be added to; M1 does not force-sell.

### 6.3 Settlement & operating policy (D6/D7)

The virtual portfolio runs under a **conservative cash-account operating policy**,
independent of what the actual Alpaca paper account (a margin account) would permit:

| Aspect | Actual Alpaca paper account | Virtual operating policy (M1) |
|---|---|---|
| Account type | Margin, multiplier up to 4×, $100k equity | Cash account emulation, no leverage |
| Buying power | $400k (`buying_power`) | `buying capacity` (§6.1); broker buying power ignored |
| Settlement | Alpaca tracks internally; `cash` changes at trade time | Sell proceeds unavailable until settlement date (T+1) from the Alpaca **settlement** calendar |
| Good-faith / free-riding | Not applicable to a margin account | Impossible by construction: buys use settled cash only |
| PDT | Applies to margin accounts under $25k equity; the $100k paper account is not constrained | Not applied as such. Configurable `pdt_emulation` flag (off for cash profile) for a future small-margin profile |
| Same-day round trips | Permitted | `allow_same_day_round_trip: false` — discretionary sells of a position opened the same session are rejected; **risk-reducing exits** (protective stop, loss-limit exit) are allowed same-day once exits exist (`allow_same_day_risk_exit: true`; U5). No exits exist in M1. |
| Shorting | Allowed unless `no_shorting` configured | Rejected: sells limited to ledger sellable qty |

Policy is configured in `config/operating_policy.yaml` as an **account profile**
(`cash_conservative` default; `margin_small` reserved and not selectable in M1) so
account-type rules stay swappable and testable. The FINRA PDT framework has been under
revision; the policy deliberately does not hard-code PDT thresholds.

#### Settlement-date rules

Two calendars are involved and they are **not** the same:

* **Trading sessions** — days the exchange is open (Alpaca calendar, `date_type=TRADING`).
* **Settlement days** — days on which securities settle; exchange-open days can be
  non-settlement days (bank holidays such as Columbus Day and Veterans Day), and the
  two holiday sets need not coincide (Alpaca calendar, `date_type=SETTLEMENT`).

Rules:

1. **Trade date** = the trading session in which the execution occurred, determined by
   converting the broker execution timestamp to `America/New_York`. Executions outside a
   known session (should not happen: M1 policy has no extended hours) → halt.
2. **Settlement date** = the first settlement day **strictly after** the trade date
   (T+1 in settlement days, not calendar days). Examples:
   * Weekend: trade Fri → settles Mon (if Mon is a settlement day).
   * Exchange + bank holiday (e.g. a Monday holiday): trade Fri → settles Tue.
   * Bank holiday with exchange open (e.g. Columbus Day Monday): a trade on Fri settles
     Tue (Mon is not a settlement day); a trade *on* the Monday settles Tue.
   * Exchange holiday with banks open: no trades occur that day; it may still be a
     settlement day for the prior session's trades — whatever the settlement calendar
     says is authoritative.
3. Proceeds become **available at 00:00 America/New_York on the settlement date**.
4. Buys debit cash immediately at execution (conservative) and are not affected by
   settlement for availability purposes.
5. **Fail closed:** if the cached settlement calendar does not cover the trade date plus
   10 calendar days, `settles_on` is left NULL, proceeds stay unavailable, and new
   entries are blocked until the calendar is refreshed and the date resolved.
6. Calendar data is cached in `market_calendar` with fetch time; it is re-fetched daily
   and any change to an already-cached date is logged and raises a reconciliation
   attention flag (dates must not silently shift under existing entries).

All settlement logic is a pure function of (trade timestamp, calendar) and is unit-tested
against a fixture calendar covering each case above. Contract tests confirm Alpaca's
settlement calendar matches these expectations for known holidays.

### 6.4 Order → reservation → fill lifecycle

```
            tx A                     network             tx B
Intent ─> PENDING_SUBMIT ─submit─> broker ─ack──────> ACCEPTED
          + reservation             │ definitive 4xx ─> REJECTED (release)
                                    │ timeout/5xx/504 ─> SUBMIT_AMBIGUOUS (keep reservation)
ACCEPTED ─fill─> PARTIALLY_FILLED ─fill─> FILLED            (tx C per fill)
   └─────────── cancel / expire ──────────> CANCELED | EXPIRED (tx D: release remainder)
```

* Buys are limit-only (D4); reservation = qty × limit + fee estimate, so worst-case cost
  is bounded.
* Fills are applied one broker execution at a time (tx C), reducing the reservation by
  filled qty × limit price; the remainder is released only on a **broker-confirmed**
  terminal status.
* Order state machine: `PENDING_SUBMIT → ACCEPTED → PARTIALLY_FILLED → FILLED`, plus
  `SUBMIT_AMBIGUOUS`, `PENDING_CANCEL`, `CANCELED`, `EXPIRED`, `REJECTED`,
  `CONFIRMED_ABSENT`. Illegal transitions raise and set a persistent halt.

### 6.5 Confirmed fills are always booked

Ledger invariants are **not** enforced as SQLite `CHECK` constraints on balances or
quantities, so a broker-confirmed fill can always be written. Sequence: tx C commits the
fill → invariant checker runs → any violation (negative cash/available, negative or
over-ceiling position, fill exceeding order qty, fill for an unknown or terminal order,
reservation overrun) sets persistent halt `LEDGER_INVARIANT` with the details. Unknown
fills for `stb-` orders are booked against a placeholder order record marked `ORPHAN`;
fills for non-bot orders are recorded in `broker_activities` and halt `RECON_EXECUTION`.

### 6.6 Price marks (U7)

Marks drive market value, equity, concentration, gap stress and loss limits, so every
mark carries its own timestamp and freshness is checked on every use.

**Interface (M1):**

```python
@dataclass(frozen=True)
class PriceMark:
    symbol: str
    price: Decimal  # > 0, parsed from raw string
    as_of: datetime  # timestamp of the underlying trade/bar, from the source (UTC)
    received_at: datetime  # when we fetched it (UTC)
    source: MarkSource  # LATEST_TRADE | SESSION_CLOSE_BAR | SIM
    feed: str | None  # e.g. "iex" / "sip" — recorded, never assumed


class PriceMarkSource(Protocol):
    def get_marks(self, symbols: Sequence[str]) -> Mapping[str, PriceMark]: ...
```

* `observe` mode: implemented by the read-only Alpaca **market-data** adapter (latest
  trade, and latest daily bar for session-close marks), raw JSON → `Decimal`.
  `simulate` mode: SimBroker supplies marks.
* `Position.current_price` is **not** a mark source (no timestamp, V10). It is used only
  as a sanity cross-check; a >5% disagreement with a fresh mark is logged as attention.
* Missing symbols in the response are simply absent from the result (never defaulted).

**Freshness rules** (`MarkFreshnessPolicy`, values in `config/risk.yaml`):

| Situation | Mark is fresh when |
|---|---|
| During a regular session | `as_of` ≥ now − **15 min** and `as_of` within the current session |
| Outside a regular session | `as_of` falls within the **most recent completed session** (its close or last trade) |
| Always | `price > 0`; `as_of ≤ received_at + 5 s` (no future timestamps); `received_at` no older than `max_sync_age` (120 s) |

Consequences: any held symbol without a fresh mark → market value, equity and loss
limits are **unknown** → transient block on new entries (fail closed). Marks are stored
in `price_marks` (latest per symbol) with all fields; equity snapshots copy the marks
they used, so every baseline is reproducible.

---

## 7. Gating: startup gate, transient blocks, persistent halts

Three distinct mechanisms. All three must be clear for a new order to pass.

| Layer | Storage | Set by | Cleared by |
|---|---|---|---|
| **Startup gate** | in-memory, set on every boot | process start | automatically, once the startup sequence (§10.1) completes cleanly |
| **Transient blocks** | derived each evaluation from DB/broker state | stale sync (> 120 s), market closed, circuit breaker open, stale marks, any `SUBMIT_AMBIGUOUS` order, inconclusive reconciliation, missing calendar/baseline, broker `trading_blocked`, daily-loss lockout | automatically, when the underlying condition clears |
| **Persistent halts** | `trading_state` + `halt_events` | operator (`halt`), reconciliation mismatch, ledger invariant, auth failure, unmapped broker status, illegal transition, weekly drawdown, second daily-loss lockout within 5 sessions, rebaseline in progress, escalation (e.g. ambiguous order unresolved > 30 min, repeated inconclusive recon) | **only** `cli resume --reason`, which first runs a full reconciliation and refuses unless it is clean (D11) |

A persistent halt survives restarts; the startup gate never clears it. Halts and blocks
stop **new entries**. They do not cancel working orders (cancel-on-halt is an M2
decision) and, once exits exist, must not prevent risk-reducing exits.

---

## 8. Reconciliation (D12)

### 8.1 Broker baselines

`init-ledger` requires the account to be flat (no positions, no open orders) and
persists a **broker baseline**: timestamp, `cash`, `equity`, activity cursor, account
config snapshot. Baselines are immutable rows; a new one only ever supersedes an old one
through the rebaseline workflow (§8.5).

### 8.2 What is reconciled

| Check | Pass condition |
|---|---|
| **Executions** | Every broker FILL activity since baseline for an `stb-` order exists in `fills` with equal qty/price/side; every ledger fill has a broker activity; no FILL for a non-bot order. |
| **Orders** | Broker open orders == ledger non-terminal orders (by client id), with equal qty and filled qty; no unknown open order. |
| **Positions** | Broker qty == ledger qty per symbol, for every symbol on either side. |
| **Cash** | Broker cash == baseline cash + Σ net amounts of all ingested broker activities since baseline. Every activity is classified as **attributable** (fills, fees on bot fills, dividends on ledger positions → must have a matching ledger cash entry of equal amount) or **external** (e.g. interest on the $100k — explained, not booked to the virtual ledger). Any residual — even $0.01 — is unexplained → failure. No tolerance band. |

### 8.3 Consistent snapshots

Broker endpoints are not read atomically, and fill activities may lag order/position
updates. A reconciliation run therefore:

1. reads activities (to head), orders, positions, account, then activities again;
2. if new activities appeared, ingests them and repeats (max 3 tries);
3. if any order's broker `filled_qty` exceeds the ingested fill qty, or fees are pending
   (`pending_reg_taf_fees`, `accrued_fees`), the result is **INCONCLUSIVE** — a
   transient block, re-checked on the next cycle; 5 consecutive INCONCLUSIVE results
   escalate to a persistent halt;
4. otherwise the result is **OK** or **MISMATCH**. MISMATCH → persistent halt.

Every run is stored with a JSON diff in `reconciliation_runs`.

### 8.4 Account-activity pagination and cursor

*Superseded in part: ingestion uses overlapping time windows, `activities.complete_through`
and re-scan policies rather than a stored cursor (§12.10–12.13, §12.17, §12.19).*

Endpoint: `GET /v2/account/activities` (V3), called with `direction=asc`,
`page_size=100`, and `page_token` = id of the last activity already processed. Alpaca
activity ids are documented as time-ordered strings (`<timestamp>::<uuid>`); this
ordering and the `page_token` semantics are **CT** items.

**Ingestion loop (one sync cycle):**

1. Read the committed cursor `activities.cursor_id` from `sync_state`.
2. Request a page after the cursor. For each page:
   * parse every row strictly (unknown type, float where string expected, missing id →
     abort the cycle without advancing; persistent halt for schema surprises);
   * in **one transaction** (tx C): insert each activity into `broker_activities`
     (primary key = activity id, so replays are no-ops), apply its ledger effect, and
     set `activities.cursor_id` to the **last id of the page**;
   * commit, then request the next page with that id.
3. Stop when a page returns fewer than `page_size` rows, then perform one extra request
   to confirm the empty tail (guards against a page boundary exactly at 100).
4. Record `activities.last_complete_sync_at` only when the loop finished.

**Crash safety:** the cursor and the effects of every activity before it are committed
atomically, so a crash at any point either leaves the page fully applied or not at all;
restarting resumes from the last committed cursor. Re-applying an already-stored id is a
no-op by primary key.

**Late or out-of-order activities:** because activities can be posted with earlier
timestamps (e.g. fees, dividends, corrections), each cycle also performs an **overlap
re-scan** from the start of the previous trading session (by `after=` date) and
inserts any ids not yet stored; a newly discovered id older than the cursor is logged as
`LATE_ACTIVITY` and processed normally. Reconciliation additionally performs a full
re-scan since the active baseline once per session. Any activity that appears in a
re-scan with changed content for a stored id → persistent halt `RECON_EXECUTION`.

### 8.5 Rebaseline workflow (explicit, audited)

Purpose: re-anchor broker-side cash reconciliation after an event outside the bot's
control (e.g. an Alpaca paper-account reset). It must never be a way to make an
unexplained difference disappear.

Preconditions (all enforced):
* a persistent halt is active (rebaseline cannot run while trading is permitted);
* no non-terminal or ambiguous orders exist;
* **broker positions already equal ledger positions** — rebaseline never changes ledger
  positions or cash. If they differ, the operator must first record explicit, individually
  audited `ADJUSTMENT` entries (separate command, each with reason and evidence) until
  they match.

Two-step flow:

1. `rebaseline propose --reason TEXT` — runs a fresh reconciliation and stores a
   `rebaseline_requests` row with: the full diff, the current broker snapshot and its
   hash, the **unexplained cash residual** (exact amount), every unclassified activity,
   and status `PROPOSED`. Nothing else changes.
2. `rebaseline approve REQUEST_ID --acknowledge-unexplained AMOUNT --reason TEXT` —
   * refuses if the proposal is older than 15 minutes or the broker snapshot hash has
     changed (state moved underneath);
   * requires the operator to type the exact unexplained residual amount being
     acknowledged (a mismatch refuses);
   * in one transaction: marks the old baseline `superseded_at`, inserts the new
     baseline linked to the request, records the acknowledged residual permanently, and
     writes a `halt_events` entry. The halt **remains**; `resume` is still required and
     still demands a clean reconciliation against the new baseline.

Old baselines, proposals (including rejected/expired ones) and acknowledged residuals
are never deleted; `status` shows the cumulative acknowledged residual since inception.

---

## 9. Order uniqueness today and its evolution

**M1 rule:** at most one non-terminal order per symbol, and at most one order per
`intent_key`.

**Reserved for RTH-01 (not implemented in M1):** a `TradePlan` aggregate owning the
full life of a trade: entry, protective stop, targets / partial exits, and close. The
`orders` table carries nullable `trade_plan_id` and `order_role`
(`ENTRY | PROTECTIVE_STOP | TARGET | EXIT`) columns from migration 0001, always `NULL`
in M1, so no migration of order history is needed later. The rule will evolve to:

* all non-terminal orders for a symbol must belong to the same active TradePlan;
* at most one live order per role, except `TARGET` (several allowed for scale-outs);
* Σ qty of live sell-side orders ≤ position qty (share reservation, mirrors Alpaca's
  `qty_available`, V9);
* a protective stop is never canceled except as part of an atomic replace or position
  close; replace sequences are explicit and recoverable states.

Whether linked stops use Alpaca OCO/bracket orders or independent orders is an RTH-01
decision and needs its own contract tests.

---

## 10. Restart, duplicates, ambiguity, API failures

### 10.1 Startup sequence
1. Exclusive OS lock on `<ledger>.lock` (one per ledger, §12.20); a second instance exits.
2. Open DB, migrate, verify the ledger mode, run invariants (violation → persistent halt).
3. Startup gate set.
4. Preflight (read-only): account status, `trading_blocked`/`account_blocked`, account
   config (`no_shorting`, `max_margin_multiplier`) reported; calendar cache refreshed.
5. Sync: ingest activities from the high-water mark; refresh every non-terminal order.
6. Run the ambiguous-order recovery protocol (§10.3) for each unresolved order.
7. Compute daily/weekly baselines if a new session/week started (§11.2).
8. Reconciliation. Clean → startup gate clears. A persistent halt, if set, remains.

### 10.2 Duplicate prevention
* `client_order_id = "stb-" + UUIDv7`, persisted in tx A **before** any network call,
  never reused, never regenerated for the same intent.
* `intent_key` UNIQUE; one non-terminal order per symbol (§9).
* Single-instance lock.
* Writes are never retried by the SDK (V2 — enforced before M2) or by generic retry
  code; only the recovery protocol may resend, and only with the same client id.

### 10.3 Ambiguous-order recovery protocol
Applies to any order in `PENDING_SUBMIT` (crash before/around send) or
`SUBMIT_AMBIGUOUS` (timeout, connection drop, 5xx, 504).

1. The reservation stays ACTIVE and a transient block on new orders is in force.
2. Look up by client id (`get_order_by_client_id`). **Found → adopt**: store broker id,
   map status, ingest fills; continue normal lifecycle.
3. **Not found is not proof.** Record the attempt in `order_resolution_attempts` and keep
   checking with backoff. The order can only leave the ambiguous state as "absent" when
   **all** of the following hold:
   * ≥ 3 not-found lookups, spread over ≥ 10 minutes after the last possible send time;
   * a full `get_orders(status=all, after=created_at − 5 min)` scan does not contain it;
   * no activity references it;
   * positions and cash reconcile with the order assumed absent.
4. Even then, the transition to `CONFIRMED_ABSENT` and the reservation release
   **require operator confirmation** (`cli resolve-order <id> --confirm-absent`; U4).
   The command refuses unless the evidence above is complete, and the evidence is
   recorded with the decision. There is no automatic path to `CONFIRMED_ABSENT`.
5. If an order in `CONFIRMED_ABSENT` ever appears at the broker, it is adopted and a
   persistent halt `RECON_ORDER` is raised.
6. The intent is never resubmitted under a new client id while its old order is
   unresolved. The automated path never resends at all in M1/M2.
7. Unresolved for more than 30 minutes → escalate to persistent halt.

### 10.4 Partial fills
Applied execution by execution (unique activity id). The reservation shrinks per filled
share; the remainder is released only on a broker-confirmed terminal status. A partially
filled then canceled order leaves exactly the broker-reported qty.

### 10.5 Brokerage API failures
| Failure | Behavior |
|---|---|
| Read timeout / 429 / 5xx | bounded exponential backoff (no jitter), honouring `Retry-After`; the SDK's retry loop is never used (§12.10) |
| Repeated failures → circuit open | transient block `BROKER_OUTAGE_ACTIVE`; ≥ 3 failed cycles ≥ 60 s apart over ≥ 30 min → `BROKER_OUTAGE` (§12.21) |
| Submit timeout / drop / 5xx / 504 | `SUBMIT_AMBIGUOUS` → §10.3 |
| Definitive 4xx rejection | `REJECTED`, release reservation |
| Duplicate client id response | treated as "order exists" → lookup → adopt; never as rejection |
| 401 / 403 | persistent halt `AUTH`, no retries |
| Unmapped status / float where Decimal string expected / schema surprise | persistent halt |

---

## 11. Risk controls

### 11.1 Pre-trade gate (in order; first failure rejects with a reason code)
1. No persistent halt; startup gate clear; no transient block.
2. Mode allows execution (`simulate` only in M1) and `TRADING_ENABLED=true`.
3. Instrument: US equity or ETF, active, tradable, not on denylist, price ≥ **$10**.
4. Buy is a limit order, whole shares.
5. **Capital:** order reservation ≤ **buying capacity** (available settled cash and
   $5,000 deployed-capital headroom). Broker buying power is never consulted.
6. **Order notional** ≤ **$1,000**.
7. **Concentration (market value, U8):** existing qty × fresh mark + new qty × limit
   ≤ **20% of virtual equity**; no adds to a position already above 20%.
8. **Positions:** open positions + pending entries ≤ **4**.
9. **Planned risk:** intent must carry a planned stop; (entry − stop) × qty ≤
   **0.5% of virtual equity**. Intents without a stop are rejected.
10. **Gap stress (U3):** with a **15% adverse gap scenario**, the resulting position's
    scenario loss ≤ **3% of equity** and the portfolio's total scenario loss (all
    positions at market value + working buy commitments) ≤ **10% of equity**.
11. **Loss limits:** no active daily-loss lockout; no weekly-drawdown halt.
12. Sell: qty ≤ sellable qty; same-session round-trip policy (§6.3).
13. One non-terminal order per symbol; `intent_key` unused.
14. **Broker backstop:** order notional ≤ broker `cash`/`non_marginable_buying_power`.
    Can only make the bot more conservative.

### 11.2 Equity baselines and loss limits
* `equity_snapshots` records, per trading session, `SESSION_BASELINE` = equity at the
  **prior session's official close** (cash + Σ qty × prior close price), computed at the
  first sync of each session using the Alpaca trading calendar.
* **Daily loss** = SESSION_BASELINE − current equity (realized + unrealized, including
  overnight gaps). At ≥ **2%** of SESSION_BASELINE → **daily-loss lockout** (U1): no new
  entries for the rest of that session; it expires automatically when the next
  session's baseline is taken. Each lockout is recorded in `loss_limit_events`.
* **Lockout escalation (U1):** a second lockout within any window of **5 consecutive
  trading sessions** (the breaching session plus the 4 before it) → **persistent halt**
  `DAILY_LOSS_REPEAT`, manual resume.
* **Weekly drawdown (U2):** WEEK_BASELINE = equity at the close of the last trading
  session of the prior **calendar week** (Mon–Sun, America/New_York). Drawdown =
  WEEK_BASELINE − current equity. At ≥ **5%** of WEEK_BASELINE → **persistent halt**
  `WEEKLY_DRAWDOWN`, manual resume. The baseline resets only at the next week's first
  baseline, never on resume.
* Missing marks or baseline → transient block (fail closed).

### 11.3 Gaps and losses beyond stops
Stops (when they exist, RTH-01) cap *planned* loss, not *actual* loss: a stock can open
below its stop.

> **The 15% gap stress is a scenario, not a maximum possible loss.** Single-stock
> gaps larger than 15% (earnings, fraud, halts, bankruptcy) happen, and in the limit a
> long position can lose 100% of its value. The stress limits bound exposure to *one
> plausible adverse scenario*; they do not bound actual loss. Loss limits (§11.2) react
> to realized damage after the fact; neither mechanism prevents a gap loss.

The design accounts for this by:
* sizing to planned risk **and** bounding gap exposure (11.1 #10);
* measuring loss limits on mark-to-market equity, so a gap shows up immediately at the
  first sync of the session;
* recording on each closed trade the realized loss in R-multiples (realized ÷ planned
  risk), so losses beyond 1R are visible in reports (field reserved for RTH-01);
* treating a breach caused by a gap identically to any other breach.

---

## 12. Persistence (SQLite)

### 12.1 Settings
WAL, `foreign_keys=ON`, `synchronous=FULL`, `busy_timeout=5000`; single writer; all
writes `BEGIN IMMEDIATE`. `data/ledger.sqlite3`; simulation ledgers under `data/sim/`.
`cli backup` uses the online backup API.

### 12.2 Schema (migration `0001_init.sql`, sketch)

*Sketch only; the migrations 0001–0007 are authoritative (§12.5 onwards).*

```sql
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);

CREATE TABLE portfolios (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  mode TEXT NOT NULL CHECK (mode IN ('observe','simulate','paper_execute')),
  initial_capital TEXT NOT NULL, max_capital_allocation TEXT NOT NULL,
  account_profile TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE rebaseline_requests (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, reason TEXT NOT NULL,
  actor TEXT NOT NULL, diff_json TEXT NOT NULL, broker_snapshot_hash TEXT NOT NULL,
  unexplained_cash TEXT NOT NULL, unclassified_activities_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PROPOSED','APPROVED','REJECTED','EXPIRED')),
  acknowledged_amount TEXT, approved_at TEXT, approval_reason TEXT);

CREATE TABLE broker_baselines (
  id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
  taken_at TEXT NOT NULL, cash TEXT NOT NULL, equity TEXT NOT NULL,
  activity_cursor_id TEXT, account_config_json TEXT NOT NULL,
  reason TEXT NOT NULL,                      -- INIT | REBASELINE
  rebaseline_request_id INTEGER REFERENCES rebaseline_requests(id),
  superseded_at TEXT);

CREATE TABLE ledger_adjustments (            -- operator-recorded, individually audited
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('CASH','POSITION')),
  symbol TEXT, qty_delta TEXT, cash_delta TEXT, price TEXT,
  reason TEXT NOT NULL, evidence TEXT NOT NULL);

CREATE TABLE orders (
  client_order_id TEXT PRIMARY KEY, broker_order_id TEXT UNIQUE,
  portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
  intent_key TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  order_type TEXT NOT NULL, time_in_force TEXT NOT NULL,
  qty TEXT NOT NULL, limit_price TEXT, planned_stop TEXT,
  status TEXT NOT NULL, filled_qty TEXT NOT NULL DEFAULT '0', avg_fill_price TEXT,
  last_broker_status TEXT, reject_reason TEXT,
  trade_plan_id TEXT,                     -- reserved for RTH-01, NULL in M1
  order_role TEXT,                        -- reserved for RTH-01, NULL in M1
  created_at TEXT NOT NULL, send_attempted_at TEXT, updated_at TEXT NOT NULL,
  last_synced_at TEXT);

CREATE TABLE order_resolution_attempts (
  id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
  ts TEXT NOT NULL, method TEXT NOT NULL,   -- BY_CLIENT_ID | LIST_SCAN | ACTIVITY_SCAN | OPERATOR
  result TEXT NOT NULL, detail_json TEXT);

CREATE TABLE cash_reservations (
  client_order_id TEXT PRIMARY KEY REFERENCES orders(client_order_id),
  original TEXT NOT NULL, remaining TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','CONSUMED')),
  updated_at TEXT NOT NULL);

CREATE TABLE broker_activities (            -- every activity ingested since baseline
  activity_id TEXT PRIMARY KEY,             -- idempotency key
  activity_type TEXT NOT NULL, ts TEXT NOT NULL,
  symbol TEXT, qty TEXT, price TEXT, net_amount TEXT, side TEXT,
  broker_order_id TEXT, client_order_id TEXT,
  attribution TEXT NOT NULL CHECK (attribution IN ('ATTRIBUTABLE','EXTERNAL','UNEXPLAINED')),
  raw_json TEXT NOT NULL, ingested_at TEXT NOT NULL);

CREATE TABLE fills (
  id INTEGER PRIMARY KEY,
  activity_id TEXT NOT NULL UNIQUE REFERENCES broker_activities(activity_id),
  client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
  symbol TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL,
  fees TEXT NOT NULL DEFAULT '0', avg_cost_before TEXT, realized_pnl TEXT,
  filled_at TEXT NOT NULL, recorded_at TEXT NOT NULL);

CREATE TABLE cash_entries (                 -- append-only; no balance CHECKs (§6.5)
  id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
  ts TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('DEPOSIT','BUY','SELL','FEE','DIVIDEND','ADJUSTMENT')),
  amount TEXT NOT NULL, trade_date TEXT NOT NULL,
  settles_on TEXT,                          -- NULL = unresolved → unavailable (§6.3)
  activity_id TEXT REFERENCES broker_activities(activity_id),
  fill_id INTEGER REFERENCES fills(id),
  adjustment_id INTEGER REFERENCES ledger_adjustments(id), memo TEXT);

CREATE TABLE positions (                    -- materialized; rebuildable from fills
  portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), symbol TEXT NOT NULL,
  qty TEXT NOT NULL, cost_basis TEXT NOT NULL, realized_pnl TEXT NOT NULL DEFAULT '0',
  opened_at TEXT, opened_session TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (portfolio_id, symbol));

CREATE TABLE price_marks (symbol TEXT PRIMARY KEY, price TEXT NOT NULL,
  as_of TEXT NOT NULL, received_at TEXT NOT NULL, source TEXT NOT NULL, feed TEXT);

CREATE TABLE loss_limit_events (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, session_date TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('DAILY_LOCKOUT','DAILY_REPEAT_HALT','WEEKLY_HALT')),
  baseline_equity TEXT NOT NULL, equity TEXT NOT NULL, loss_pct TEXT NOT NULL);

CREATE TABLE market_calendar (session_date TEXT PRIMARY KEY, open_at TEXT NOT NULL,
  close_at TEXT NOT NULL, settlement_date TEXT, fetched_at TEXT NOT NULL);

CREATE TABLE equity_snapshots (
  id INTEGER PRIMARY KEY, portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
  kind TEXT NOT NULL CHECK (kind IN ('SESSION_BASELINE','WEEK_BASELINE','SESSION_CLOSE')),
  session_date TEXT NOT NULL, equity TEXT NOT NULL, cash TEXT NOT NULL,
  marks_json TEXT NOT NULL, taken_at TEXT NOT NULL,
  UNIQUE (portfolio_id, kind, session_date));

CREATE TABLE order_events (id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL,
  ts TEXT NOT NULL, from_status TEXT, to_status TEXT,
  source TEXT NOT NULL,                     -- LOCAL|BROKER|RECON|OPERATOR|RECOVERY
  detail_json TEXT);

CREATE TABLE trading_state (id INTEGER PRIMARY KEY CHECK (id = 1),
  halted INTEGER NOT NULL, reason_code TEXT, reason_detail TEXT,
  changed_at TEXT NOT NULL, changed_by TEXT NOT NULL);

CREATE TABLE halt_events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('HALT','RESUME','RESUME_REFUSED')),
  reason_code TEXT NOT NULL, detail TEXT, actor TEXT NOT NULL,
  reconciliation_run_id INTEGER REFERENCES reconciliation_runs(id));

CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE reconciliation_runs (id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('OK','MISMATCH','INCONCLUSIVE','ERROR')),
  diff_json TEXT);
```

Seed on `init-ledger`: portfolio `main` (mode, $5,000 initial, $5,000 ceiling,
`cash_conservative`), one `DEPOSIT` of 5000.00 settled immediately, broker baseline,
`trading_state.halted = 1` with reason `INITIALIZED` (first operator `resume` is a
deliberate act).

### 12.3 Invariants (startup, after every write tx in tests, `cli status`)
* cash = Σ cash_entries; available cash ≥ 0; deployed capital ≤ ceiling
* positions = replay of fills (qty and cost basis); qty ≥ 0
* order.filled_qty = Σ fills for the order ≤ order.qty
* terminal orders have non-ACTIVE reservations; ACTIVE reservations belong to
  non-terminal orders
* every attributable broker activity has exactly one ledger effect

### 12.4 Transaction boundaries (no network inside any)

| Tx | Contents |
|---|---|
| A create order | re-run capital/exposure checks on fresh balances; insert order `PENDING_SUBMIT`, reservation, event |
| A′ send marker | set `send_attempted_at` immediately before the network call |
| B submit outcome | `ACCEPTED` + broker id / `REJECTED` + release / `SUBMIT_AMBIGUOUS`; event |
| C ingest activity | insert `broker_activities` row (UNIQUE no-op on replay); if fill: fill, cash entry, position, reservation, order qty/status, event; then invariants (post-commit) |
| D terminal | broker-confirmed terminal status; release remainder; event |
| E reconciliation | run row; halt + `halt_events` on MISMATCH/escalation |
| F recovery attempt | `order_resolution_attempts` row; on adoption, broker id + status |
| G baselines | equity snapshot rows |
| H operator | halt / resume / resolve-order / adjustment, each with `halt_events` or audit row |
| I rebaseline approve | supersede old baseline, insert new baseline, record acknowledged residual, `halt_events` (§8.5) |

Activity ingestion (tx C) commits each page's effects **and** the cursor together (§8.4).

### 12.5 Step 4 implementation notes (persistence, as built)

These refine §12.1–12.4; `src/swing_trading_bot/ledger/migrations/0001_init.sql` is the
authoritative schema.

* **Files and opening.** `Database.create` makes a new file (mode `0600`) and refuses an
  existing one; `Database.open` refuses a missing file, so a path typo can never yield an
  empty ledger. Every connection sets `foreign_keys=ON`, `journal_mode=WAL`,
  `synchronous=FULL`, `busy_timeout=5000`, `trusted_schema=OFF`, `cell_size_check=ON`, and
  opening runs `quick_check` and `foreign_key_check`.
* **Identity and mode isolation.** A `database_identity` singleton (immutable) records the
  mode at creation, and `PRAGMA application_id` marks the file as this application's.
  Opening in a different mode, opening a foreign or plain SQLite file, and any use of
  `paper_execute` in M1 are refused. A trigger forces `portfolios.mode` to equal the
  database mode.
* **Initial halt at creation.** The `trading_state` row (halted, `INITIALIZED`) and its
  `halt_events` entry are written in the same transaction as migration 0001, i.e. at
  database creation rather than at `init-ledger`.
* **Migrations.** `schema_migrations(version, name, checksum, applied_at)` is created by the
  runner and is append-only. Each migration, its history row and `PRAGMA user_version` are
  applied in one `BEGIN IMMEDIATE` transaction, statement by statement (never
  `executescript`). Unknown/newer versions, history gaps, `user_version` disagreement and
  checksum changes are refused. Pending migrations are applied automatically on open, each
  atomically.
* **Transactions.** `Database.transaction()` (BEGIN IMMEDIATE, read-write `Tx`) and
  `Database.read()` (deferred, `query_only`, always rolled back `ReadTx`). Nesting is refused
  per process context, also across two `Database` objects. `assert_no_transaction()` is the
  guard broker code will call before any network I/O.
* **Schema hardening.** All tables are `STRICT`. Decimal columns carry CHECKs for canonical
  positional text, timestamps for fixed-width UTC (`…T…:…:….ffffff+00:00`), and dates for
  `YYYY-MM-DD`. Because STRICT TEXT columns coerce a bound number to text before CHECKs
  run, the codec (`ledger/codec.py`, Decimal-only) is the barrier against floats.
* **No defaults.** The `DEFAULT '0'` values sketched in §12.2 (`filled_qty`, `fees`,
  `realized_pnl`) were removed; writers must state every value.
* **Append-only / immutability triggers** on audit tables (`fills`, `order_events`,
  `halt_events`, `reconciliation_runs`, `ledger_adjustments`, `order_resolution_attempts`,
  `equity_snapshots`, `loss_limit_events`, `portfolios`, `database_identity`). No deletes
  on any ledger table. Column-level rules: order identity fields immutable,
  `broker_order_id` and `send_attempted_at` set once, reservations final once closed,
  `cash_entries.settles_on` resolvable once (NULL → date), baselines supersedable once with
  at most one active per portfolio, decided rebaseline requests final, broker activity
  content immutable (only `attribution` may change).
* **Additional constraints:** `orders.purpose` (U5); `UNIQUE(activity_id, kind)` on
  `cash_entries`; BUY/SELL cash entries require `fill_id`, ADJUSTMENT requires
  `adjustment_id`; baseline `reason = INIT` ⇔ no rebaseline request.
* **Deliberately absent:** balance/sign CHECKs (broker truth must always be recordable,
  §6.5) and a DB-level one-working-order-per-symbol index (it could block recording a
  late-appearing broker order). Both are enforced by the risk gate / invariant checker.
* **Resume guard in persistence.** `TradingStateRepository.resume` requires the id of an
  `OK` reconciliation run; otherwise it records `RESUME_REFUSED` and leaves the halt set.
* **Invariant framework.** `InvariantChecker` runs registered invariants after commit and
  never blocks writes. Step 4 registers data invariants (singleton rows, order filled qty
  = Σ fills ≤ qty, reservation/terminal consistency, position qty = fill replay,
  non-negative positions); accounting invariants are added in step 6.
* **Repositories** exist for portfolios, orders, order events, reservations, broker
  activities (idempotent: `INSERTED` / `DUPLICATE` / `CONFLICT`), fills (same), cash
  entries, positions, price marks, sync state, reconciliation runs and trading state /
  halt events. Repositories for baselines, rebaseline requests, adjustments, calendar,
  equity snapshots, loss-limit events and resolution attempts arrive with the steps that
  use them; their tables already exist.


### 12.6 Step 5 implementation notes (calendar & settlement, as built)

Code: `domain/calendar.py` (pure logic), `ledger/calendar_store.py` (persistence),
migration `0002_calendar_coverage.sql`.

* **Why migration 0002 was necessary.** 0001's `market_calendar` could not distinguish a
  holiday (inside fetched range, no row) from never-fetched data, had nowhere to store the
  separate settlement-day calendar, and lacked provenance for staleness. 0002 adds
  `calendar_fetches` (append-only log of every fetch with its requested range, row count,
  payload SHA-256, `ACCEPTED`/`REJECTED` and problems), `settlement_days`, and rebuilds
  `market_calendar` with `first_fetch_id` / `last_confirmed_fetch_id` (replacing
  `fetched_at`), `open_at < close_at`, `settlement_date > session_date`, and triggers
  (hours immutable, `settlement_date` set once, no deletes). The rebuild aborts if legacy
  rows without provenance exist (fail closed).
* **Coverage.** A date is known for a calendar kind only if an `ACCEPTED` fetch's requested
  range contains it. Every API raises `CalendarCoverageError` outside coverage.
* **Staleness.** Coverage of a date is stale when its newest covering fetch is older than
  `calendar_max_age_hours` (36) **and** the date was not already in the past (market tz)
  when fetched — history does not go stale, forecasts do. Stale data raises
  `StaleCalendarError`.
* **Settlement date** (`settlement_date(trade_date)`): requires a covered trading session;
  sources are the provider's per-session `settlement_date` and the first settlement day
  strictly after the trade date on the settlement calendar. If both exist they must agree
  (else `ContradictoryCalendarError`); if neither exists → `SettlementDateUnavailableError`.
  There is no weekday-arithmetic fallback.
* **Fetch acceptance.** `record_fetch` rejects (records, does not apply) a fetch that is
  internally inconsistent (weekend sessions/settlement days, duplicates, rows outside the
  range, open ≥ close, hours outside 09:30–16:00 ET or not on the session date,
  settlement date not after/too far after the trade date or on a weekend, empty long
  ranges), that changes accepted history in overlapping dates (session added/removed/
  re-timed, settlement date changed or withdrawn, settlement-day status changed), or that
  would create a cross-calendar contradiction. Filling a previously missing per-session
  settlement date is allowed. Identical refetches refresh `last_confirmed_fetch_id` and
  staleness.
* **APIs:** `is_trading_session`, `next_trading_session`, `previous_trading_session`
  (strict), `session` (incl. `is_early_close`, local open/close), `is_settlement_day`,
  `settlement_date`, `calendar_coverage(start, end)` → `CoverageReport`,
  `require_coverage`, `require_future_coverage(days)`, `horizon_status(warning_days)`
  (contiguous fresh horizon per calendar, days remaining, the provider's documented end
  2029-12-31, warning flag).
* **Policy additions** (`config/operating_policy.yaml` → `settlement`):
  `calendar_max_age_hours: 36`, `calendar_horizon_warning_days: 365`; M1 also enforces
  `settlement_cycle_days == 1` because only T+1 is implemented.
* **Offline fixture:** `tests/support/calendar_fixture.py`, a synthetic, hand-entered
  2025-12 .. 2027-12 calendar (NYSE holidays, early closes, Fed holidays including
  Columbus/Veterans Day, Saturday holidays not observed by banks). Not a source of truth.
* **Alpaca assumptions to confirm with contract tests (CT 3, step 15):** TRADING rows exist
  only for open sessions; `open`/`close` are ET wall-clock times that include early closes;
  per-session `settlement_date` is populated and reflects bank holidays; `date_type=
  SETTLEMENT` returns exactly the settlement-eligible dates; late calendar changes (e.g. a
  national day of mourning) appear as changed rows — the store rejects these as conflicts
  for operator attention rather than overwriting history. Mapping raw Alpaca JSON into
  `CalendarFetch` belongs to the step 9 adapter.
* **Fixture correction (step 6 review).** The synthetic test fixture originally treated
  Good Friday 2026 and 2026-07-03 as settlement days because banks were open. DTCC/NSCC
  Important Notices contradict that: A#9732 (2026-03-06) states "No CNS activity will
  settle on Friday, April 3, 2026" and A#9778 (2026-06-05) states the same for July 3;
  DTC B#24302-26 states DTC is closed on Juneteenth 2026. The fixture now encodes
  non-settlement weekdays explicitly with provenance (`VERIFIED` for those three, all
  others — including Thanksgiving 2026, Columbus/Veterans Day and all of 2027 — marked
  `ASSUMPTION` pending the step 15 contract tests). Lesson recorded: settlement
  eligibility must come from the clearing calendar, never from bank or exchange
  schedules. The calendar logic itself was unchanged.

### 12.7 Step 6 implementation notes (virtual portfolio accounting, as built)

Code: `domain/accounting.py` (pure average-cost math), `domain/valuation.py` (mark
freshness), `ledger/balances.py`, `ledger/portfolio.py` (`VirtualPortfolio`),
`ledger/accounting_invariants.py`. No schema change; repositories gained
`OrdersRepository.record_fill_progress`, `FillsRepository.list_in_application_order`,
`CashEntriesRepository.list_unresolved_settlements`.

* **Initialization.** `VirtualPortfolio.initialize` writes the portfolio row and one settled
  `DEPOSIT` equal to initial capital (used by `init-ledger` in step 14; tests use simulation
  ledgers only). M1 allows exactly one deposit (`initial_deposit` invariant).
* **Balances** (`compute_balances`, recomputed from the journal): cash = Σ entries;
  unsettled proceeds = SELL entries whose `settles_on` is NULL or after today (market tz);
  settled cash; buy commitments = Σ ACTIVE reservations; available = settled − commitments;
  deployed = Σ open-position cost basis + commitments; headroom = 5,000 − deployed;
  **buying capacity = max(0, min(available, headroom))**.
* **Reservations.** `reserve_buy` reserves `qty × limit + fee_estimate`, rounded up, only for a
  `PENDING_SUBMIT` limit buy, and refuses if it exceeds buying capacity (an accounting
  guard; the full risk gate is step 11). Each buy fill reduces the remaining reservation by
  `filled qty × limit` (floored at 0), so price improvement is already reflected in cash.
  `close_reservation` refuses unless the order is in a **broker-confirmed terminal status**
  (recorded by the caller in the same transaction): FILLED → `CONSUMED`, otherwise
  `RELEASED`; the remainder returns to available cash. This prevents premature release.
* **Fills** (`process_fill`, one transaction): activity (idempotent) → fill (with
  `avg_cost_before`, `realized_pnl`) → BUY/SELL cash entry (buy debit immediate; sell credit
  immediate but unsettled) → position → reservation → order `filled_qty`/`avg_fill_price`
  (status is untouched: the lifecycle is step 12) → order event. Settlement date comes from
  the stored calendar; if unavailable it is stored as NULL (proceeds stay unavailable,
  `UNRESOLVED_SETTLEMENT` blocks entries) and `resolve_settlements` fills it in later —
  never guessed.
* **Duplicates / anomalies.** Exact replay → `DUPLICATE`, no change. Same activity id,
  different content → `CONFLICT`, books untouched, persistent halt `RECON_EXECUTION`.
  Non-bot fill → activity recorded `UNEXPLAINED`, nothing booked, halt. Fill for an unknown
  `stb-` id → booked against an `ORPHAN` order, then halt.
* **Average cost.** Cost basis is stored, average derived. A partial sell removes
  `cost × sold / held` quantized to 0.0001 (half-even); a full close removes the entire
  remainder (no residue). Oversells are booked (negative quantity, zero cost) and halt via
  invariants. Fill-level fees go into buy cost basis / reduce sell realized P&L; standalone
  `FEE` activities and `DIVIDEND`s are cash entries available immediately and are not
  attributed to a position's realized P&L.
* **Post-commit invariants** (data + accounting): initial deposit, fill↔cash linkage (exact
  amount), average-cost replay of positions and per-fill realized P&L (in booking order),
  **cash conservation** `cash + Σ cost basis = deposits + Σ realized + dividends + fee
  entries + adjustments` (exact), cash ≥ 0, available cash ≥ 0, deployed ≤ ceiling,
  reservation bounds. Any violation → persistent `LEDGER_INVARIANT` halt (not re-written
  while the identical violation persists).
* **Valuation.** `snapshot` reports balances, positions (qty, cost basis, average cost,
  realized), mark assessments, market value, unrealized P&L, equity and market exposure
  (market value + commitments). If any non-flat position lacks a FRESH mark, market value,
  unrealized P&L, equity and market exposure are `None` (never estimated) and
  `STALE_OR_MISSING_MARKS` blocks new entries. Other entry blockers: unresolved settlement,
  negative available cash, ceiling exceeded, negative position, no buying capacity.
* **Mark freshness** (§6.6) as implemented: future timestamps rejected; receipt within
  `max_sync_age`; in session: as of this session and within 15 minutes; otherwise dated the
  last completed session (a `LATEST_TRADE` at/after that session's open — extended-hours
  prints are accepted — or a `SESSION_CLOSE_BAR` for that date); calendar unavailable →
  `UNVERIFIABLE`.

### 12.8 Step 7: accounting review and loss limits (as built)

#### Accounting review (before step 7)

1. **Cash-conservation identity and fee signs.**

   `cash + Σ position cost basis = deposits + Σ realized P&L + dividends + fee entries + adjustments`

   Sign conventions (enforced at input and by the `cash_entry_signs` invariant):

   | Item | Stored as | Effect |
   |---|---|---|
   | Buy fill fee `f` (magnitude ≥ 0) | inside BUY entry `−(q·p + f)` | adds `f` to cost basis |
   | Sell fill fee `f` (magnitude ≥ 0) | inside SELL entry `+(q·p − f)` | reduces realized P&L by `f` |
   | Standalone FEE activity | FEE entry `< 0` | reduces cash only |
   | DIVIDEND | DIVIDEND entry `> 0` | increases cash only |

   Each fee lands in exactly one place, so nothing is counted twice *provided the broker
   reports it once*. The ledger cannot detect a fee reported both on a fill and as a
   separate FEE activity, so this is an **adapter contract** for step 9: Alpaca fill
   activities carry no fees and regulatory fees (TAF/SEC) arrive only as FEE activities,
   so the Alpaca adapter sets `ConfirmedFill.fees = 0`. `ConfirmedFill` rejects negative
   fees and non-positive qty/price; `CashActivity` rejects FEE ≥ 0 and DIVIDEND ≤ 0 (a
   fee refund needs an explicit mapping). `PortfolioSnapshot.total_fees` = fill fees +
   |FEE entries|.
2. **Unknown settlement dates.** Balances are recomputed from the journal, so a NULL
   `settles_on` keeps sell proceeds unavailable across restarts and over any amount of
   time. `resolve_settlements` is idempotent (NULL-only update, set-once trigger, UNIQUE
   audit row) and now **auditable**: each resolution writes `settlement_resolutions`
   (entry, trade date, settlement date, time, method, accepted calendar fetch ids used;
   migration 0003, append-only).
3. **Booking order vs execution order.** Average cost (and so each sell's realized
   P&L and the remaining cost basis) depends on the order in which buys and sells are
   applied. The ledger applies fills in **booking order**, the order of record. Totals
   are order-independent in one sense (cash and quantity are sums), but the split
   between realized P&L and remaining cost basis is not. Step 6 did not detect a late
   activity whose execution time precedes already-booked fills for the same symbol. Now
   such a fill is **booked (broker truth) and then halts** (`OUT_OF_ORDER`,
   `RECON_EXECUTION`). Ordering is per portfolio and symbol.
   **Recovery requirements before M2** (M2 entry criterion):
   * activity ingestion must apply each page in ascending execution time and never
     book a later page before an earlier one (§8.4 already pages ascending);
   * an operator **restatement** procedure: re-derive average cost and per-fill realized
     P&L for the affected symbol in execution order, record the differences as audited
     `ADJUSTMENT` entries (fills stay append-only), re-run invariants, then resume;
   * the overlap re-scan (§8.4) must route late activities through this path rather than
     silently booking them.

#### Equity baselines and loss limits

Code: `domain/loss_limits.py` (pure math), `ledger/loss_limit_store.py`,
`ledger/loss_limits.py` (`LossLimitMonitor`), migration `0003_loss_limits.sql`.

* **Migration 0003** adds `settlement_resolutions` and `session_closes` (immutable official
  close per symbol and session), and rebuilds `equity_snapshots` and `loss_limit_events`.
  Those tables existed since 0001 but no earlier step wrote them; the rebuild aborts if
  rows exist. Snapshots record `applies_to` (session, or week Monday),
  `reference_session` and its close time, `basis` (`SESSION_CLOSE` / `INCEPTION`), equity,
  cash, market value and the closes used. They are UNIQUE per
  (portfolio, kind, applies_to). Events reference their baseline snapshot and record
  loss, loss %, threshold and evidence, with the same uniqueness rule, so repeated
  evaluation never duplicates an event.
* **Current session:** today's session until its close; after the close or on a
  non-trading day, the next session.
  **Daily baseline:** equity at the previous session's official close, i.e. the cash
  journal up to the close time plus the quantities held at that time × each symbol's
  recorded official close. If any held symbol has no recorded close, there is no
  baseline (`SESSION_BASELINE_MISSING`); nothing is estimated. If the portfolio was
  created after that close, the baseline is the inception capital (`INCEPTION`).
  **Weekly baseline:** equity at the close of the last session before the current
  week's Monday (handles holiday Mondays, Fridays and Thanksgiving weeks). Both are
  written once and never recomputed or reset, by a restart or by a resume.
* **Limits** compare against the recorded baseline (inclusive, exact Decimal,
  `baseline − equity ≥ pct × baseline`):
  * daily 2% → `DAILY_LOCKOUT` for the session (blocks new entries). **Expiry (step 7
    correction):** a lockout clears only when a *later* session has actually **opened**
    (`open_at ≤ now`) **and** has a recorded session baseline. A next-session baseline
    written after the close, overnight, over a weekend or on a holiday is necessary but
    not sufficient. With no usable calendar, an existing lockout is reported active (fail
    closed).
  * A second lockout within the 5-session window (this session + 4 before) →
    `DAILY_REPEAT_HALT` + persistent halt `DAILY_LOSS_REPEAT`. If the calendar cannot
    determine the window, this fails closed (halts).
  * weekly 5% → `WEEKLY_HALT` + persistent halt `WEEKLY_DRAWDOWN`. The breach keeps
    blocking new entries (`WEEKLY_LIMIT_ACTIVE`) for the rest of that week even after
    an operator resume; the resume itself is not undone.
* **Mark-to-market:** current equity comes from `VirtualPortfolio.snapshot`, which uses
  fresh marks only. Missing or stale marks → `EQUITY_UNKNOWN` (no breach can be
  evaluated, entries blocked). Missing or stale calendar → `CALENDAR_UNAVAILABLE`.
* **Overnight gaps:** since the daily baseline is the prior official close, the first
  valid price of a new session (`GapObservation`: quantity held at that close, prior
  close, price, P&L, %) counts toward both limits at that evaluation. Losses are
  measured when `evaluate` runs; the caller (sync loop) must evaluate during sessions.
* **Gap stress (U3):** a hypothetical 15% adverse move on every position's market value
  and on working buy commitments. Limits are 3% (per position) and 10% (portfolio) of
  the **recorded session-baseline equity**. An exceeded portfolio limit blocks new
  entries; an exceeded per-position limit is reported for the step 11 risk gate. It is
  a scenario, not a maximum possible loss (§11.3).
* **Reporting:** `LossLimitMonitor.report` is read-only (writes nothing). It returns
  baselines, equity, daily/weekly loss, loss %, budget and remaining budget, lockout
  status and session, weekly-breach status, lockouts in the 5-session window, gaps,
  gap-stress exposure and blockers. `evaluate` writes baselines, events and halts.
  Blockers apply to **new entries only**: risk-reducing exits stay conceptually
  permitted (`RISK_REDUCING_EXITS_PERMITTED`); exit execution is not part of M1.
* **Session closes** are recorded through `record_session_close` (immutable; an
  identical re-record is a no-op, a different price a `CONFLICT`). The step 9
  market-data adapter will feed them from daily bars.

#### Step 7 corrections (review of step 7)

* **Weekly resume (verified).** A `WEEKLY_HALT` sets one persistent halt. `resume` is
  refused (and audited as `RESUME_REFUSED`) without an `OK` reconciliation run. A
  successful resume clears the halt flag, but the week's breach keeps blocking new
  entries (`WEEKLY_LIMIT_ACTIVE`) until the next calendar week's baseline. Re-evaluation
  in the same week, even at a deeper drawdown, writes no new event and no new halt. A
  breach in a later week is a new event with a new halt.
* **First-week policy for a portfolio started midweek.** Any baseline whose reference
  close is earlier than the portfolio's creation uses `INCEPTION`: equity = cash =
  initial capital, no positions, no marks, `reference_session` NULL,
  `reference_at = created_at`. This covers creation before the open, intraday, or after
  the close. The first (partial) week's weekly baseline therefore stays at inception
  capital all week. Daily baselines switch to official closes from the first session
  that follows a session the portfolio existed through. From the next calendar week,
  the weekly baseline is the prior Friday's (last session's) close. No historical
  valuation is ever fabricated.

### 12.9 Step 8: broker ports and SimBroker (as built)

Code: `broker/ports.py`, `broker/errors.py`, `broker/models.py`, `broker/mapping.py`,
`broker/sim.py`.

* **Ports.** `BrokerReadPort` (account, account config, clock, calendar, positions, open
  orders, orders since, order by client id, paginated activities) and
  `BrokerExecutionPort` (`submit_order`, `cancel_order`) are separate
  `runtime_checkable` protocols; `PriceMarkSource` (`get_marks`) is a third. A static test
  asserts the only concrete class with `submit_order` + `cancel_order` is `SimBroker`.
* **Error taxonomy.** Each error states `order_state` (`NOT_CREATED`, `EXISTS`, `UNKNOWN`,
  `NOT_APPLICABLE`) and `retryable`:

  | Error | Meaning |
  |---|---|
  | `OrderRejectedError` | definitive rejection, no order |
  | `DuplicateClientOrderIdError` | the order exists: adopt, never treat as a rejection |
  | `AmbiguousSubmissionError` | any transport failure on submit, before or after receipt |
  | `AuthenticationError` | never retried |
  | `RateLimitedError` | not processed, safe to retry, `retry_after` as a timedelta |
  | `ServiceUnavailableError` | reads only |
  | `InvalidBrokerDataError` | the response could not be mapped exactly |
  | `NotCancelableError` | cancel refused; the order is terminal or not visible |
  | `OrderNotFoundError` | absence, which is not proof of non-existence |
* **Snapshots.** `AccountSnapshot` (no `buying_power` field — a static test forbids reading
  it anywhere; `non_marginable_buying_power` is the cash backstop), `AccountConfigSnapshot`
  (with `m2_protections_in_place` for U6), `MarketClock`, `BrokerActivity` (FILLs carry
  the broker `order_id`, as Alpaca does — no client id), `ActivityPage`
  (`last_id`, `possibly_more`), `SubmitOrderRequest`; orders and positions reuse the domain
  models. Exact Decimals, timezone-aware timestamps.
* **Raw mapping.** `broker/mapping.py` parses Alpaca-shaped JSON (numbers as strings)
  strictly: floats, NaN, missing fields, unknown enums, naive timestamps and malformed
  dates → `InvalidBrokerDataError`. SimBroker produces raw JSON and routes every
  response through this module; the step 9 read adapter will reuse it.
* **SimBroker.**
  * In memory only: no database, no network, no randomness. An injected `SimClock`
    supplies time.
  * Scheduled events (fills, cancels, expirations, post-accept rejections, fee and
    dividend activities) run in (time, sequence) order, **as of their scheduled time**,
    whenever any port method is next called. IDs come from a counter, and activity IDs
    use Alpaca's sortable `timestamp::uuid` shape.
  * `event_log` makes replays comparable exactly.
  * Scripted submit outcomes: `Accept` (optional visibility delay or initial status),
    `Reject`, `AcceptThenFail` (504 / read timeout / reset after the order was created),
    `FailBeforeReceipt`, `RateLimit` and `AuthFailure`. A duplicate client ID raises
    `DuplicateClientOrderIdError`.
  * Order-visibility and activity-reporting delays, either global or per event.
  * Cancels go through `pending_cancel`, with a configurable delay, and fills can race
    them.
  * No-shorting and buying-power rejections.
  * Positions with average entry price, cash, account config, clock and calendar
    derived from loaded calendar fetches, and marks.
  * Ascending ID pagination (page size 1..100) with an `after` time filter.
  * Injectable read faults and payload corruption.
* **Delayed activities.** A test shows an execution reported late sorting *before* the
  stored cursor (so a cursor-only sync misses it) and being found by the `after`
  overlap re-scan of §8.4.
* **Test isolation.** A test-wide guard refuses `Database.create/open` anywhere under the
  repository's `data/` directory; simulation ledgers live under `tmp_path`.
* **Fixed during step 8.** Events were first stamped with the time the simulator was
  next polled rather than their scheduled time; the deterministic-replay test caught it.

### 12.10 Step 9: broker-contract refinements and the read-only Alpaca adapter (as built)

#### Refinements to the step 8 contract

* **Retry semantics.** `BrokerError.retryable` was replaced by `read_retryable`: it means
  only that a *read* may be repeated under a bounded policy. No error class marks a
  write as safe to retry. After a failed submit, `submit_failure_state(err)` returns
  `NOT_CREATED` only for `OrderRejectedError` (the broker's explicit rejection) and
  `EXISTS` for `DuplicateClientOrderIdError`. Everything else is `UNKNOWN`: 429, 5xx,
  504, timeouts, resets, auth failures and malformed responses. `RateLimitedError` is
  now read-only in meaning (`order_state = NOT_APPLICABLE`), and SimBroker reports a 429
  on submit as `AmbiguousSubmissionError(status_code=429)`. Client order IDs stay stable
  across any recovery.
* **Opaque activity IDs.** Callers never compare, sort or parse activity IDs, and never
  assume contiguity. `page_token` is an implementation-defined opaque cursor.
  `get_activities` gained `until`, so `(after, until]` windows can overlap.
  `broker/activity_feed.py` provides `collect_activities` (follows the cursor, at most
  `max_pages` pages, detects non-advancing cursors and repeated IDs) and
  `merge_activities` (dedupes by ID; the same ID with different content →
  `InvalidBrokerDataError`). Ledger ingestion is idempotent by activity ID, so
  re-reading a window is always safe.

#### `AlpacaPaperReadAdapter`

Code: `broker/alpaca_http.py` (transport + retries), `broker/alpaca_read.py` (adapter),
`broker/mapping.py` (extended). It implements `BrokerReadPort` and `PriceMarkSource`
and defines no submit/replace/cancel method.

* **Why raw GET requests, not alpaca-py client methods** (alpaca-py 0.45.0 source):
  1. No account-activities method.
  2. `response.json()` decodes JSON numbers as floats, and market-data prices are JSON
     numbers.
  3. `RESTClient._request` silently retries 429/504 for **every** verb, with fixed 3 s
     sleeps, and ignores `retry_attempts=0`.
  4. No request timeout.
  5. Naive datetimes in request models are assumed to be UTC.

  The adapter therefore uses a transport with one method, `get_json` (HTTP GET only),
  limited to `https://paper-api.alpaca.markets/v2` and
  `https://data.alpaca.markets/v2`. It uses `allow_redirects=False` and timeouts of
  (5 s connect, 20 s read), and decodes with `json.loads(..., parse_float=Decimal)`.
  From alpaca-py it takes the base URLs (`BaseURL.TRADING_PAPER`, `BaseURL.DATA`) and
  the auth headers (`RESTClient._get_default_headers()` on a throw-away client that is
  never stored). No `TradingClient` or data client is ever constructed, so no SDK retry
  loop can run, and there is no write path to retry. A test pins the SDK's retry
  behaviour so an upgrade forces a review before any M2 write adapter exists.
* **Endpoints:**

  | Read | Endpoint |
  |---|---|
  | Account | `GET /account` |
  | Account configuration | `GET /account/configurations` |
  | Clock | `GET /clock` |
  | Calendar | `GET /calendar?start&end&date_type=TRADING\|SETTLEMENT` |
  | Positions | `GET /positions` |
  | Orders | `GET /orders?status=open\|all&direction=asc&limit=500&nested=false&after&until` |
  | Order by client ID | `GET /orders:by_client_order_id?client_order_id=` (404 → `None`) |
  | Activities | `GET /account/activities?direction=asc&page_size&page_token&after&until` |
  | Latest trades (marks) | data `GET /stocks/trades/latest?symbols&feed` |
  | Session closes | data `GET /stocks/bars?symbols&timeframe=1Day&start&end&feed&adjustment=raw&limit=1000&page_token` |
* **Parsing.** Trading values must be decimal strings; floats are rejected. Market data
  numbers arrive as `Decimal` via `parse_float=Decimal` (ints are accepted exactly;
  floats and bools are rejected). Timestamps need a UTC offset, and nanoseconds are
  truncated to microseconds by Python 3.12's `fromisoformat`. Order statuses outside
  the §3 V6 table raise `UnknownBrokerStatusError` (persistent halt
  `UNMAPPED_BROKER_STATUS`). Calendar times must be `HH:MM` America/New_York.
  Positions with a duplicate symbol, an order lookup returning another client ID,
  duplicate activity IDs within a page, a page larger than requested, or a page
  repeating its cursor all raise `InvalidBrokerDataError`.
* **Orders pagination:** pages of 500, ascending by submission time. Each next page
  starts 1 µs before the previous page's last `submitted_at` (so orders sharing a
  boundary timestamp aren't skipped), and results are deduplicated by order ID.
  No progress → error. At most 50 pages.
* **Activity pagination:** the opaque cursor is the last activity ID of the previous
  page, as Alpaca documents; this is unverified. `ActivityPage.possibly_more` (full
  page) triggers one more request.
* **Bounded read retries** (`DEFAULT_READ_POLICIES`). Only `RateLimitedError` and
  `ServiceUnavailableError` are retried; auth failures, request errors and invalid
  data are raised immediately.
  * Standard reads: 3 attempts, 0.5 / 1 / 2 / 4 s backoff (max 4 s), honouring
    `Retry-After` up to the cap.
  * Order lookup by client ID: 4 attempts, because recovery depends on it.
  * Marks: 2 attempts, max 2 s, because a stale mark is better than a slow one.
* **Price marks.** `get_marks` uses the latest trade per symbol, with its exchange
  timestamp `t`, received time and feed (default `iex`). Missing symbols are simply
  absent. Freshness is judged by `domain.valuation.assess_mark` (§6.6/§12.7), never by
  the adapter. `Position.current_price` has no timestamp and is exposed only through
  `get_position_price_crosscheck()`; it is never a mark. `get_session_closes` returns
  `BrokerSessionClose` values from daily bars (bar `t` must fall on the session date in
  America/New_York); converting them into ledger `SessionClose` rows is left to the
  caller (the broker package never imports the ledger).
* **Tests** run fully offline. They use a scripted `ReadOnlyHttp` fake and a fake
  `requests` session; sockets are blocked. Six optional read-only contract tests in
  `tests/contract/` run only with `RUN_ALPACA_PAPER_TESTS=1` and operator authorization,
  and load credentials with `load_settings(.env)` inside each test.

#### Alpaca behaviour still to verify (read-only contract tests, operator-authorized)

* Activities:
  * `page_token` semantics with `direction=asc`;
  * the `after`/`until` filters (exclusive or inclusive; timestamps or dates);
  * whether non-trade activities always carry `net_amount` and `date`;
  * how journal and adjustment types (JNLC, JNLS, CSD, …) appear;
  * late posting of fees and dividends relative to the cursor.
* `/calendar?date_type=SETTLEMENT`: exact row shape and that `date` is the settlement day.
  Whether TRADING rows' `settlement_date` is populated and accounts for clearing
  holidays (§12.6).
* `GET /orders:by_client_order_id` returns 404 (not 422) for unknown IDs, and how soon a
  new order becomes visible (M2).
* `/orders` `after` filters by `submitted_at`, and the maximum `limit` is 500.
* Market data:
  * which feed the account may use;
  * that latest-trade `t` is an exchange timestamp;
  * that daily-bar `t` is midnight America/New_York.
* **The IEX daily-bar close is not the consolidated official close.** A SIP
  subscription (or another official-close source) is needed before baselines rely on
  it for M2.
* Whether the trading API ever sends a monetary field as a JSON number. That would be
  rejected (fail closed), not converted.

### 12.11 Step 9 integration refinements (before step 10)

1. **Order-list pagination.**
   * **What the API offers:** alpaca-py's `get_orders` makes a single
     `GET /v2/orders` call. The endpoint has no page token and caps `limit` at 500.
     Paging is only possible by submission time (`direction=asc`, `after` exclusive),
     so timestamp overlap is the only strategy.
   * **What it can't guarantee:** complete retrieval when 500 or more orders share
     one `submitted_at`. The API returns an unspecified 500 of them, and nothing can
     reach the rest.
   * **What the adapter does:**
     * a full page that contains only one timestamp, a response not sorted by
       submission time, a missing `submitted_at`, a non-advancing cursor, or the
       page cap each raise `IncompleteRetrievalError`, with the orders read so far
       attached as `partial`;
     * a short page is the only proof of completeness, so an exactly full last page
       always triggers one more request;
     * an incomplete scan is never returned as a list.
2. **Activity coverage.** `broker/activity_feed.collect_window` returns an
   `ActivityWindow` whose `complete` flag requires both:
   * **tail confirmed:** pagination ended on an empty or short page;
   * **closed:** `until ≤ now − posting_lag`, because activities can be posted late.

   A cursor that simply advanced never establishes coverage, and a window that is
   fully paginated but not yet closed is not complete. Interrupted pagination or the
   page cap raise `IncompleteRetrievalError` with the partial activities, which can be
   ingested idempotently. The durable marker `activities.complete_through`
   (`ledger/activity_coverage.py`, stored in `sync_state`, no schema change) only moves
   forward, only for a complete window, and never across a gap. It is to be written
   in the same transaction that ingests the window. Future synchronization (steps
   12–13) reads overlapping windows (each `after` reaching back at least the posting
   lag) and re-scans the previous session daily; reconciliation re-scans since the
   baseline. The posting lag (assumed 15 minutes) is a contract-test item; activities
   posted later than that are still caught by the overlap re-scan.
3. **Official closes.**
   * Migration 0004 rebuilds `session_closes` with explicit provenance:
     * `OFFICIAL`: a verified official close (no source implemented in M1);
     * `DAILY_BAR`: a feed observation, which requires `feed ∈ {iex, sip, delayed_sip}`;
     * `SIM`.

     The primary key is now (symbol, session, source). Unclassifiable legacy
     `SESSION_CLOSE_BAR` rows abort the migration.
   * Baselines accept only `OFFICIAL` or `SIM` closes (`LossLimitConfig.baseline_close_sources`
     refuses `DAILY_BAR`). With only a daily-bar observation the baseline is missing,
     and the problem text names the observation (`DAILY_BAR/iex`); nothing is
     substituted.
   * The adapter returns `DailyBarClose` observations (labelled with their feed) via
     `get_daily_bar_closes`. `get_official_closes` raises
     `UnsupportedBrokerOperationError` without making a request.
   * **A verified official-close source is required before M2 execution.**

### 12.12 Step 10: startup readiness and trading gates (as built)

Code: `risk/gates.py` (`TradingGates`, `StartupGate`, `GateStatus`), `ledger/sync_keys.py`.
`AccountSnapshot` gained `account_id` and `environment` (`paper` or `sim`). No schema
change.

* **Layers.** All three are evaluated on every call, and every one must be clear for a
  new entry.
  * **STARTUP prerequisites:**
    * ledger mode equals the configured mode;
    * SQLite integrity and all data and accounting invariants pass;
    * the portfolio exists;
    * the broker account ID and environment were recorded at initialization (`sync_state`);
    * the connected account and environment match them;
    * the account configuration is supported (no `suspend_trade`, no fractional
      trading);
    * a clean reconciliation run within `reconciliation_max_age` exists.

    `StartupGate` is in memory and blocked at process start (`STARTUP_INCOMPLETE`).
    `run_startup` clears it only when every prerequisite passes, and clearing it
    removes `STARTUP_INCOMPLETE` and nothing else.
  * **PERSISTENT:** the stored halt (`PERSISTENT_HALT`, with its reason). Never cleared
    by gates; an operator resume backed by an OK reconciliation is the only way.
  * **TRANSIENT, re-derived each call:**
    * runtime mode (observe has no execution) and the `TRADING_ENABLED` switch;
    * broker read failures (unavailable / auth / invalid data), account restricted,
      market closed;
    * broker sync older than `max_sync_age`;
    * activity coverage older than `posting_lag + max_sync_age`;
    * reconciliation inconclusive, mismatched or stale;
    * calendar unavailable, or insufficient fresh future coverage;
    * unresolved orders (`PENDING_SUBMIT`, `SUBMIT_AMBIGUOUS`, `ORPHAN`) and unexplained
      broker activities;
    * every portfolio entry blocker (marks, settlement, capital) and loss-limit
      blocker (baselines, equity, daily lockout, weekly limit, gap stress).

    Unknown upstream codes become `UNMAPPED_BLOCKER`.
  * **ADVISORY, non-blocking:** `ACCOUNT_PROTECTIONS_NOT_SET` (U6: report-only in M1;
    blocking when `require_m2_account_protections`) and `CALENDAR_HORIZON_SHORT`.
* **Read-only, fail closed.** Broker reads (account, configuration, clock) happen first,
  guarded by `assert_no_transaction`. Then one read-only ledger snapshot is taken.
  Evaluation never writes. Missing, stale, contradictory or unavailable inputs block.
* **Exits vs new entries.** `GateStatus.exits` reports risk-reducing exits as permitted
  in principle unless a condition would make any broker action unsafe:
  * ledger mode or integrity failure, or a missing portfolio;
  * broker identity, environment, availability, auth or data failure.

  Persistent halts and loss limits never forbid exits. `execution_available` is False
  in M1.
* **Reporting.** Findings are de-duplicated and sorted by (layer, reason, detail).
  `GateStatus.reason_codes`, `to_dict()` (deterministic and JSON-friendly) and
  `render()` give the operator view.
* **Deviations from §7:**
  * Startup readiness here covers stored and broker-read prerequisites only. Running
    the sync, recovery and reconciliation that produce that state is steps 12–14.
  * Weekly-limit and daily-lockout blocks come from the loss monitor, so a resume can
    never bypass them.
  * Observe mode always blocks new entries (`RUNTIME_MODE_NO_EXECUTION`).

### 12.13 Step 10 refinements: activity re-scans and exit readiness (as built)

* **Posting lag is an assumption.** A window that is `complete` (confirmed tail and
  `until <= now - posting_lag`) is complete *operationally*. The 15-minute lag is
  unverified. If an activity is posted later than the lag with an earlier timestamp,
  it lands in a window already marked complete, and forward windows never see it.
  The durable marker `activities.complete_through` is kept; it is never treated as a
  permanent guarantee.
* **Re-scan policy** (`broker/activity_feed.py`):
  * `RescanPolicy(posting_lag, overlap, periodic_lookback, periodic_every,
    full_history)` is validated: overlap must be at least the lag, and the periodic
    lookback must reach further back than the overlap.
  * The default is a 15-minute lag, a 1-hour overlap, and a 7-day lookback run daily.
  * `rescans_due(...)` is pure planning. It returns:
    * OVERLAP, from `complete_through - overlap`;
    * PERIODIC, when one is due;
    * FULL_HISTORY, since the active baseline, while reconciling.

    Every window ends at `now - posting_lag`.
  * Re-scans never move the marker backwards. Activities they find that are older
    than the marker are ingested idempotently and flagged as late.
  * Tests show that an activity posted 40 minutes late is missed by forward windows
    and found only by a historical re-scan. They also show that an overlap shorter
    than the delay still misses it.
  * Step 13 reconciliation is not implemented here.
* **Exit readiness** (`risk/exits.py`) is read-only and separate from entry gating.
  `ExitReadinessAssessor.assess(symbol)` reads the broker (account, positions, open
  orders) outside any transaction, then reads one ledger snapshot.
  * *Entry-only restrictions* are reported but never make an exit unsafe: a
    persistent halt, a daily lockout, the weekly limit, `TRADING_DISABLED`, and the
    other loss blockers.
  * *Fundamental failures* make an exit unsafe:
    * the broker is unreachable, fails authentication, or returns invalid data;
    * the account identity is unverified or mismatched, the environment is wrong,
      or the account is restricted;
    * ledger integrity or invariant failures;
    * no ledger position, no broker position, or a quantity mismatch (missing
      evidence is never "safe");
    * unresolved local orders for the symbol (working, ambiguous or orphaned), or
      any open broker order for it.
  * `execution_available` is always False. Exit execution does not exist in M1.

### 12.14 Step 11: pre-trade risk checks (as built)

`risk/pretrade.py` holds the engine. `domain/fees.py`, `config/fees.py` and
`config/fees.yaml` hold the fee estimates. `ledger/state_fingerprint.py` holds the
ledger fingerprint. Nothing here writes, reserves, or submits.

* **Model.** `TradeProposal`:
  * intent key, symbol, side, order type, time in force;
  * entry (limit) and stop prices, instrument type, the extended-hours flag;
  * an optional `requested_qty` (None means "the maximum permitted").

  `PreTradeRiskEngine.evaluate(proposal, gate=, account=, now=)` returns a
  `RiskDecision`:
  * `approved`, `approved_qty` (whole shares), `reservation`, `planned_risk`;
  * sorted `rejections`, each a `RejectionCode` plus detail;
  * `sizing` (a `SizingPreview`), `evidence` (a `RiskEvidence`), `valid_until`;
  * a deterministic `decision_id`.

  `preview(...)` returns only the `SizingPreview`: the maximum quantity, every
  `ConstraintCap` (max_qty, limit, used), and the `binding` constraints.
* **Sizing.** Each constraint is a monotone cap on whole shares, using E = current
  virtual equity and B = the gap-stress denominator (baseline equity):

  | Constraint | Condition on q |
  |---|---|
  | RISK_BUDGET | q(entry−stop) + buy fee + est. exit fee at stop ≤ 0.5% E |
  | POSITION_CONCENTRATION | held market value + pending commitment for symbol + q·entry ≤ 20% E |
  | ORDER_COMMITMENT | reserve(q) = ⌈q·entry + buy fee⌉ ≤ $1,000 |
  | CAPITAL_CEILING | reserve(q) ≤ $5,000 − (open cost basis + active buy commitments) |
  | AVAILABLE_CASH | reserve(q) ≤ settled cash − active buy commitments |
  | GAP_STRESS_POSITION | 15% × (held MV + pending + q·entry) ≤ 3% B |
  | GAP_STRESS_PORTFOLIO | current stressed loss + 15% × q·entry ≤ 10% B |
  | BROKER_CASH_BACKSTOP | q·entry + buy fee ≤ max(0, min(broker cash, non-marginable buying power)) |

  * `max_whole_qty` starts from a floor estimate and moves only to verified values.
    A quantity is never rounded up.
  * A requested quantity above the maximum is rejected (`QUANTITY_EXCEEDS_LIMIT`),
    never silently reduced.
  * The broker's figure is only a backstop: it can lower the size, never raise it.
    Alpaca's $100k cash and $400k buying power are never capital.
* **Discrete checks** (all are collected, not first-failure):
  * Proposal shape:
    * long only (`NOT_LONG_ENTRY`); limit orders only; no extended hours;
    * equity or ETF only; symbol and intent-key shape;
    * a whole-share `requested_qty`; tick-valid prices;
    * `STOP_NOT_BELOW_ENTRY`; the $10 minimum on entry and on the fresh mark.
  * Evidence:
    * gate present, the same mode, ≤ 30 s old, not in the future, allowing new
      entries;
    * account snapshot ≤ 120 s old, the recorded account id, the expected
      environment, not restricted.
  * Ledger state:
    * integrity and accounting invariants; no persistent halt;
    * latest reconciliation `OK` and ≤ 10 min old;
    * no portfolio entry blockers;
    * loss-monitor blockers (lockout, weekly limit, missing baselines, unknown
      equity, calendar);
    * gap stress available.
  * Market data: a fresh mark for the symbol, and a valued existing holding.
  * Portfolio rules:
    * no working order for the symbol (`DUPLICATE_WORKING_ORDER`);
    * no reuse of an intent key;
    * at most 4 symbols held or with pending buy entries (adding to a held symbol
      uses no new slot);
    * `POSITION_AT_CONCENTRATION_LIMIT` when existing exposure is already ≥ 20%.
  * Zero size: `ONE_SHARE_EXCEEDS_RISK_BUDGET` if the risk cap is 0, otherwise
    `QUANTITY_ZERO`, naming the binding constraints.
* **Evidence.** A decision records:
  * the ledger fingerprint, the gate's evaluation time and codes;
  * the mark (price, as_of, source, feed), the account id and as_of;
  * equity, the session and week baseline ids, available cash, and capital headroom.
* **Fingerprint.** `ledger_fingerprint(session)` is a SHA-256 hash over the **full
  ordered contents of every ledger table** (each value tagged with its SQLite type).
  Only `schema_migrations` and the immutable `database_identity` are excluded, each
  with a stated reason. A test fails if a migration adds a table that is neither
  hashed nor excluded. Revised during step 11 verification: the first version hashed
  counts and maxima of append-only tables, which missed later updates to mutable
  columns such as `cash_entries.settles_on`, superseded baselines and rebaseline
  status. Any ledger write now invalidates an outstanding decision.
* **Not a reusable authorization.**
  * `valid_until` is the earliest of:
    * `now + decision_ttl` (30 s);
    * the gate's expiry;
    * the account evidence's expiry;
    * the mark's in-session age and receipt-age expiry.
  * `still_valid(now, current_fingerprint)` requires an approval, a time inside
    [evaluated_at, valid_until], and an unchanged fingerprint.
  * This is only a cheap pre-check.
* **Step 12 protocol** (to implement, not implemented here). Before submitting, the
  OrderManager:
  1. Re-reads the broker account and evaluates a fresh gate, outside any transaction.
  2. Opens `BEGIN IMMEDIATE`. That serializes it against every other writer.
  3. Inside that transaction, re-runs `evaluate` on the same snapshot, with the
     proposal pinned to the decision's approved quantity. It requires approval and a
     fingerprint identical to the decision's.
  4. In the same transaction, inserts the `PENDING_SUBMIT` order (unique intent key)
     and its cash reservation, then commits.
  5. Submits after the commit (no network inside the transaction).

  If any piece of evidence changed, the reservation is not made, and the proposal
  must be evaluated again from scratch. Reservations are what stop two concurrent
  approvals from both spending the same cash.
* **Fees** (`config/fees.yaml`) are estimates, not risk parameters:
  * buys: $0;
  * sells: FINRA TAF $0.000166/share, capped at $8.30, plus SEC $0.0000278 per
    dollar sold.

  Each component is rounded up to the cent. Actual fees come from broker activities.
* **Deviations / decisions:**
  * The risk budget includes estimated entry and exit fees, which is more
    conservative than (entry − stop) × qty alone.
  * Gap-stress caps use the loss monitor's denominator (baseline equity), consistent
    with §11.3. Concentration and risk use current equity.
  * Concentration counts pending buy commitments for the same symbol at their
    reserved amount.
  * A broker cash backstop was added: max(0, min(cash, non-marginable buying
    power)). It can only lower a size. Margin `buying_power` is not part of
    `AccountSnapshot` at all. The backstop never replaces virtual settled cash or the
    $5,000 ceiling, which are separate caps. Missing or stale account evidence means
    no sizing and a rejection.
  * `TimeInForce` has only DAY/GTC, both accepted. Extended hours is rejected
    outright.
  * Reconciliation freshness (10 min) mirrors the gate setting.

### 12.15 Step 12: order management and recovery (as built)

The package is `oms/`:
* `order_manager.py`: submission and cancellation;
* `lifecycle.py`: applies broker order state;
* `sync.py`: activities and order refresh;
* `recovery.py`: the §10.3 protocol and restart recovery.

It runs only against SimBroker. The guard (`check_execution_allowed`) requires a
simulate-mode ledger and an object implementing `BrokerExecutionPort` whose module
is not Alpaca's. The read-only Alpaca adapter has no `submit_order`, and a static
test proves only `broker/sim.py` implements `submit_order` and `cancel_order`.
No schema changes: every table used already existed in migration 0001. The new
`ResolutionAttemptsRepository` writes `order_resolution_attempts`.

* **Submission** (`OrderManager.place_entry(decision, now)`). No transaction is open
  during any broker call.
  1. Refuse a decision that is not approved (`NOT_APPROVED`) or is outside
     [evaluated_at, valid_until] (`DECISION_EXPIRED`).
  2. Read the broker account and evaluate the gates, outside any transaction
     (`ACCOUNT_UNAVAILABLE` on read failure).
  3. **tx A** (`BEGIN IMMEDIATE`):
     * the ledger fingerprint must equal the decision's (`LEDGER_CHANGED`);
     * `PreTradeRiskEngine.evaluate_in(tx, …)` re-runs every check on this snapshot,
       with the approved quantity pinned (`REVALIDATION_FAILED`);
     * then, atomically: insert the `PENDING_SUBMIT` order (new `stb-` UUIDv7 id,
       UNIQUE `intent_key`), call `reserve_buy`, and write an `INTENT_CREATED` event
       with both decision ids and the fingerprint.

     Two competing decisions can't both reserve: the first commit changes the
     fingerprint, so the second is refused and must be re-evaluated against the
     reduced cash.
  4. **tx A2**: set the `send_attempted_at` marker (set-once) and a `SEND_ATTEMPT`
     event. A NULL marker proves the order was never sent.
  5. `submit_order`, once. Never retried.
  6. **tx B**, by outcome:
     * snapshot returned → adopted through the lifecycle;
     * `OrderRejectedError` → `REJECTED`, reservation released;
     * `DuplicateClientOrderIdError` → `SUBMIT_AMBIGUOUS`, then an immediate lookup
       that adopts it (never treated as a rejection);
     * anything else (429, 5xx, 504, timeout, reset, malformed response, auth) →
       `SUBMIT_AMBIGUOUS`, reservation held; auth also halts `AUTH`.
* **Cancellation** (`request_cancel`):
  * Only for orders with a confirmed broker id (`ACCEPTED`, `PARTIALLY_FILLED`,
    `PENDING_CANCEL`). Ambiguous orders are refused locally.
  * `PENDING_CANCEL` and a `CANCEL_REQUESTED` event are committed first; then the
    broker call is made, and its result is audited (`CANCEL_SENT`,
    `CANCEL_NOT_CANCELABLE`, `CANCEL_SEND_FAILED`).
  * The reservation is never released by a request. If the broker never received the
    cancel, sync moves the order back to `ACCEPTED`.
* **Applying broker state** (`lifecycle.apply_snapshot`). Every change goes through
  `transition()` with an `order_events` row. A terminal target closes the reservation
  in the same transaction (tx D: `FILLED` consumes it; any other terminal status
  releases it). Fill gating: while booked fills are below the broker's `filled_qty`,
  a terminal status is held back and the order stays working. So the remainder is
  released only after a broker-confirmed terminal status *and* the final fill
  quantity. These all halt and leave the order untouched:

  | Condition | Halt |
  |---|---|
  | Identity mismatch (symbol, side, qty, type, limit, ids) | `RECON_ORDER` |
  | Booked fills above the broker's `filled_qty` | `RECON_ORDER` |
  | Unmapped status | `UNMAPPED_BROKER_STATUS` |
  | Unexpected status (`replaced`) | `UNEXPECTED_BROKER_STATUS` |
  | Inconsistent payload | `BROKER_PAYLOAD` |
  | Illegal transition | `ILLEGAL_TRANSITION` |
  | A `CONFIRMED_ABSENT` order appears | adopted, then `RECON_ORDER` |

  Read failures map to halts through `read_error_halt`: auth → `AUTH`; unknown status
  → `UNMAPPED_BROKER_STATUS`; malformed or float payload → `BROKER_PAYLOAD`. Anything
  else is transient.
* **Order-state transitions exercised:**
  * `PENDING_SUBMIT` → `ACCEPTED` | `REJECTED` | `SUBMIT_AMBIGUOUS`;
  * `SUBMIT_AMBIGUOUS` → any broker-derived status (adoption) | `CONFIRMED_ABSENT`
    (operator only);
  * `ACCEPTED` → `PARTIALLY_FILLED` | `PENDING_CANCEL` | `FILLED` | `CANCELED` |
    `EXPIRED` | `REJECTED`;
  * `PARTIALLY_FILLED` → `PENDING_CANCEL` | `FILLED` | `CANCELED`;
  * `PENDING_CANCEL` → `ACCEPTED` (the cancel never landed) | `FILLED` (the fill won
    the race) | `CANCELED`;
  * `CONFIRMED_ABSENT` → broker-derived (with a halt).
* **Synchronization** (`OrderSync.sync`):
  1. Activities from `complete_through − 1 h` to now. Each is booked in its own
     transaction through `VirtualPortfolio` (tx C), in execution-time order:
     * fills are always recorded first; invariants run after commit;
     * identical replay → no-op; conflicting replay → `RECON_EXECUTION`;
     * a fill executed earlier than already-booked fills → booked, then
       `RECON_EXECUTION`;
     * FEE/DIV → booked.
  2. Attribution: a fill for an unadopted ambiguous order adopts it first. An unknown
     `stb-` id becomes an `ORPHAN` record and halts. A non-bot or unidentifiable
     order is preserved as an UNEXPLAINED activity and halts. Other activity types
     are preserved as UNEXPLAINED for step 13; nothing is lost.
  3. The coverage marker then advances to `now − posting lag`, only if the window's
     tail was confirmed.
  4. Working orders are refreshed by client id.
  5. Post-commit invariants run.
* **Ambiguous-order protocol** (`AmbiguousOrderRecovery`), unchanged from §10.3 and
  never resending:
  * `lookup`: found → adopt. Not found, or an explicit 404 → a `NOT_FOUND` row, and a
    `PENDING_SUBMIT` order becomes `SUBMIT_AMBIGUOUS`. A read error → an `ERROR` row,
    which is not evidence.
  * `list_scan` and `activity_scan` record `order_resolution_attempts` rows.
  * `confirm_absent(operator)` refuses, and records the refusal, unless all of these
    hold:
    * ≥ 3 not-found lookups since the last possible send, the latest ≥ 10 min after
      it, and nothing ever found;
    * a clean list scan after that point;
    * a complete, clean activity scan whose closed end reaches ≥ 10 min past the
      send;
    * an `OK` reconciliation run newer than all of that evidence.

    On success it moves the order to `CONFIRMED_ABSENT`, releases the reservation,
    and stores the evidence.
  * `escalate`: an order unresolved > 30 min after its last possible send halts
    `AMBIGUOUS_ORDER_TIMEOUT`, once per order. A later adoption does not clear the
    halt.
  * `recover_after_restart` = forced lookups → sync → escalate (§10.1 steps 5–6).
* **Crash testing:**
  * `CRASH_POINTS`: `after_intent_commit`, `after_send_marker`, `after_submit`,
    `after_outcome_commit`, `after_cancel_marker`. Fill-transaction points come from
    `VirtualPortfolio`'s hook: `after_activity` … `before_commit`.
  * In-process tests raise a `BaseException` at each point.
  * Process-level tests spawn an interpreter that runs the real OMS against the
    ledger file and calls `os._exit` at the boundary. The parent's SimBroker is
    served over an OS pipe (no sockets), so broker state survives the crash; the
    parent reopens the ledger and recovers.
  * Every case ends with at most one broker order per intent, reservations open
    exactly while orders are working, and clean invariants.
* **Deviations from §10/§12.4:**
  1. Activity effects commit one activity per transaction (the existing
     `VirtualPortfolio` boundary). The coverage marker advances in a later
     transaction, only after all effects of the window have committed. A crash in
     between causes an idempotent re-read, never a gap.
  2. An order whose send marker is NULL (provably never sent) still goes through the
     full ambiguous protocol (no automatic abandonment, no send). The marker is
     recorded as evidence.
  3. Escalation and the absence window are measured from the last possible send (the
     marker, else creation). Because the activity scan must be closed past
     send + 10 min, operator confirmation is possible from about 25 minutes after the
     send.
  4. When an ambiguous order is adopted while the broker reports fills not yet
     booked, it is adopted as `ACCEPTED` (working) until the fills arrive.
  5. *(Superseded by §12.16: the execution boundary is now an explicit
     simulation-only capability.)*
  6. Only long limit entries are submitted; there is no exit/sell submission API in
     M1. FILL activities carry no fees (fees post as FEE activities).
  7. Lock, preflight, baselines and reconciliation in the startup sequence (§10.1
     steps 1–4, 7–8) remain step 14 and step 13 work.

### 12.16 Step 12 final safety checks (as built)

* **Durable send marker.** `oms/execution.py` holds the only code in `src/` that calls
  `submit_order` or `cancel_order`; an AST test proves it.
  * `SimulationExecution.submit` requires a `CommittedSendMarker`. Only
    `read_committed_send_marker` can produce one: it asserts no transaction is open,
    reads in a *new* read transaction (so it sees only committed data), and requires
    `send_attempted_at` with status `PENDING_SUBMIT`. The marker can't be constructed
    by hand or reused for another order.
  * Cancels similarly need a committed `PENDING_CANCEL` (`CommittedCancelMarker`).
  * Because of this ordering, a NULL marker after a crash *is* proof that this process
    never sent the order. The recovery protocol still requires the full §10.3
    evidence and an operator, and nothing is ever resubmitted.
  * New crash point `send_marker_uncommitted` (inside tx A2). Crashes just before and
    just after the marker commit are tested in-process and at process level.
* **Adoption with unbooked fills.** Migration 0005 adds `orders.broker_filled_qty`,
  the broker-reported filled quantity, stored on every snapshot; a regression halts
  `RECON_ORDER`. While it exceeds the booked quantity:
  * the order is never terminal;
  * the reservation is never released (it still covers the unbooked cost, because
    fills are at or below the limit);
  * gates report transient `UNBOOKED_FILLS` and the risk engine rejects every
    proposal with `UNBOOKED_FILLS`;
  * no cash, position or settlement effect is inferred.

  Tests cover partially and fully filled broker orders, normal and adopted, whose
  activities arrive 30–40 minutes late.
* **Execution boundary.** `SimulationExecution` accepts exactly a `SimBroker` (exact
  type, so subclasses are refused) or a class registered with
  `register_simulation_double`. A double must declare `SIMULATION_DOUBLE_PURPOSE`,
  implement both write methods, and not be Alpaca-named. The process-level crash
  harness's pipe proxy and one cancel-failure double are the only registrations, both
  in `tests/`. `OrderManager` accepts only this capability and still requires a
  simulate-mode ledger. The Alpaca adapter has no write methods and can't be
  registered.

### 12.17 Step 13: broker reconciliation (as built)

* **Modules:**
  * `oms/reconciler.py`: `Reconciler.run(now)`.
  * `oms/classify.py`: activity classification.
  * `ledger/baselines.py`: active baseline and INIT creation only.
  * Migration 0005: the append-only `reconciliation_findings` table.
* **Flow:**
  1. **Ingest.** Forward sync, then the `RescanPolicy` windows: OVERLAP, PERIODIC
     (the last complete one is recorded in `activities.last_periodic_rescan`) and
     FULL_HISTORY since the active baseline. Everything is ingested idempotently
     *before* judgement, so unexpected events are preserved before any halt.
  2. **Observe.** Account, account configuration, positions, open orders, and the
     complete order list since baseline − 5 min. Ambiguous or absent-declared orders
     that the list shows are adopted. Settlement dates are resolved from the calendar
     (never weekday arithmetic).
  3. **Head re-check.** If new activities appeared, repeat (up to
     `max_snapshot_retries` = 3); otherwise the run is INCONCLUSIVE
     (`ACTIVITY_HEAD_UNSTABLE`).
  4. **Judge** on one ledger snapshot.
  5. **Persist** in one transaction: the run (`OK`/`INCONCLUSIVE`/`MISMATCH`; CLEAN is
     stored as `OK`), evidence boundaries in `diff_json` (observation time, attempts,
     baseline, account, order-scan bounds, every window with its completeness, a
     broker snapshot hash), every finding, and any halt referencing the run.
* **Outcomes.** Any MISMATCH → MISMATCH. A failed read never hides an independently
  confirmed difference, and a check whose inputs weren't fully retrieved isn't
  evaluated. Otherwise any INCONCLUSIVE → INCONCLUSIVE. Otherwise CLEAN. Advisories
  never block.
* **Classifications:**

  | Severity | Codes |
  |---|---|
  | MISMATCH, account | `ACCOUNT_ID_MISMATCH`, `ENVIRONMENT_MISMATCH`, `CURRENCY_UNSUPPORTED` |
  | MISMATCH, orders | `DUPLICATE_BROKER_ORDER`, `MULTIPLE_WORKING_ORDERS`, `EXTERNAL_ORDER`, `UNEXPECTED_ORDER`, `MISSING_BROKER_ORDER`, `CONFLICTING_ORDER`, `ABSENT_ORDER_EXISTS`, `TERMINAL_LOCALLY_OPEN_AT_BROKER`, `ORPHAN_ORDER` |
  | MISMATCH, executions | `UNEXPLAINED_FILL`, `LEDGER_FILL_NOT_AT_BROKER`, `FILL_CONTENT_CONFLICT`, `BOOKED_EXCEEDS_BROKER_FILLS`, `OUT_OF_ORDER_BOOKING`, `OUT_OF_ORDER_FILL`, `ACTIVITY_CONFLICT` |
  | MISMATCH, cash | `UNEXPLAINED_CASH_ACTIVITY`, `ATTRIBUTABLE_NOT_BOOKED`, `CASH_UNEXPLAINED` (any non-zero residual of broker cash − (baseline cash + Σ net amounts of all activities since the baseline); no tolerance) |
  | MISMATCH, positions | `POSITION_QTY_MISMATCH`, `POSITION_COST_MISMATCH` (ledger average cost at the broker's 0.0001 precision), `UNSUPPORTED_POSITION` |
  | MISMATCH, ledger | `LEDGER_INVARIANT` |
  | INCONCLUSIVE | `READ_FAILED`, `INCOMPLETE_RETRIEVAL`, `SYNC_INCOMPLETE`, `ACTIVITY_WINDOW_INCOMPLETE`, `ACTIVITY_HEAD_UNSTABLE`, `BASELINE_MISSING`, `IDENTITY_UNRECORDED`, `ACCOUNT_RESTRICTED`, `FEES_PENDING`, `FILL_ACTIVITY_LAG` (position and cash differences for those symbols are then also INCONCLUSIVE), `CASH_RESIDUAL_PENDING`, `CASH_NOT_EVALUATED`, `STATUS_NOT_APPLIED`, `SETTLEMENT_DATE_UNKNOWN`, `AMBIGUOUS_ORDER_UNRESOLVED` |
  | ADVISORY | `ACCOUNT_PROTECTIONS_NOT_SET` (U6) |
* **Halts.**
  * A MISMATCH halts with the highest-priority category reason: account → new
    `RECON_ACCOUNT`; ledger → `LEDGER_INVARIANT`; executions and activities →
    `RECON_EXECUTION`; orders → `RECON_ORDER`; positions → `RECON_POSITION`; cash or
    settlement → `RECON_CASH`. The halt references the run.
  * The 5th consecutive INCONCLUSIVE run halts `RECON_INCONCLUSIVE_REPEAT`, once.
  * Halts persist across restarts.
* **Dependent operations:**
  * `latest_usable_run(now, max_age, after)` returns a run only if it is CLEAN, no
    older than `max_age` and newer than `after`.
  * `resume_after_clean_reconciliation` requires such a run taken after the halt,
    within 10 minutes. Refusals are recorded as `RESUME_REFUSED`.
  * Gates already require a fresh OK run.
  * The ambiguous-order protocol accepts a fresh run that is CLEAN, or INCONCLUSIVE
    *only* because of unresolved submissions with this order marked
    `absence_consistent`. That flag means everything else is complete and clean and
    the complete order list does not contain the order (§10.3, "reconcile with the
    order assumed absent"). It refuses runs older than 10 minutes.
* **Classification before booking** (`oms/classify.py`, used by sync):
  * FEE on a bot-execution day and DIV on a symbol the bot held → ATTRIBUTABLE
    (booked);
  * INT → EXTERNAL (explained, never booked);
  * everything else (a fee without an execution, a dividend on an unheld symbol,
    `CSD`, `CSW`, `JNLC`, unknown types) → UNEXPLAINED. Preserved, never booked;
    gates block and reconciliation reports a MISMATCH.

  Replays follow the stored attribution.
* **Deviations and decisions:**
  1. *(Superseded by §12.18: the rebaseline workflow is implemented. Audited ledger
     adjustments are still not implemented.)*
  2. Step 12 sync used to book every FEE/DIV; it now classifies first (above).
  3. SimBroker now applies a non-fill cash activity to its cash balance when the
     activity *posts*. Fills still move cash at execution, with the gap visible
     through `filled_qty`.
  4. CLEAN while an order is ambiguous is impossible by rule. The operator absence
     path therefore accepts the absence-consistent INCONCLUSIVE run described above,
     rather than requiring `OK`.
  5. A full-history re-scan runs on every reconciliation (`RescanPolicy.full_history`).
     A baseline younger than the posting lag yields an empty, closed history window.
     The head window is then only tail-confirmed, which is recorded in the evidence.
  6. An account restriction is INCONCLUSIVE (gates block it independently), not a
     MISMATCH. An unknown settlement date is INCONCLUSIVE, not a halt.
  7. The cost-basis check is skipped for symbols with capitalized fees, which are
     zero for Alpaca equities.

### 12.18 Step 13.1: reconciliation hardening (as built)

**1. Rebaseline workflow** (`oms/rebaseline.py`, migration 0006 `rebaseline_events`).
* **Propose** (`Rebaseliner.propose(actor, reason)`):
  1. Refused unless a persistent halt is active.
  2. Runs a fresh reconciliation, then refuses unless all of these hold:
     * no INCONCLUSIVE finding (complete evidence);
     * no position difference;
     * no working or unresolved ledger order and no open broker order;
     * every MISMATCH is acknowledgeable (`CASH_UNEXPLAINED`,
       `UNEXPLAINED_CASH_ACTIVITY`, `UNEXPLAINED_FILL`, `EXTERNAL_ORDER`);
     * there is something to acknowledge.
  3. Stores an immutable `PROPOSED` request containing:
     * the diff, the run id and the observation time;
     * the broker-state hash (account, positions, orders);
     * a fingerprint of the economic ledger tables;
     * every unexplained activity;
     * the **unexplained amount** = broker cash − baseline cash − Σ(attributable +
       external activity since the baseline), i.e. the residual plus all unexplained
       activity.
* **Confirm** (`confirm(request_id, acknowledged, actor, reason)`):
  1. Request state:
     * `APPROVED` with the same amount → `DUPLICATE` (idempotent; same baseline
       returned, event recorded);
     * a different amount on an `APPROVED` request → refused;
     * `EXPIRED` or `REJECTED` → refused.
  2. Older than 15 minutes → `EXPIRED`.
  3. A typed amount not exactly equal (as a Decimal) → refused. The request stays
     `PROPOSED`, so a typo can be corrected.
  4. No longer halted → `EXPIRED`.
  5. A second fresh reconciliation; if the broker-state hash, the economic ledger
     fingerprint, the unexplained amount, the active baseline or any precondition
     changed → `EXPIRED`.
  6. Otherwise, in one transaction:
     * the old baseline is superseded (kept);
     * a new `REBASELINE` baseline is inserted, linked to the request (cash = observed
       broker cash, taken at the observation time, equity = cash + broker position
       cost);
     * the request becomes `APPROVED` with the acknowledged amount;
     * the halt is re-recorded as `REBASELINE` (still halted);
     * an `APPROVED` event is written.
* **Never changed:** virtual cash, cost basis, positions, fills and cash entries.
  Resuming still needs a later CLEAN run and the operator resume (§12.17).
  `cumulative_acknowledged()` sums every approved acknowledgement.
* **Follow-on changes:**
  * Gates block on UNEXPLAINED activity only after the active baseline. Earlier
    activity was acknowledged by the approved rebaseline.
  * Reconciliation no longer reports external orders closed before the active
    baseline.
  * The broker snapshot hash now covers account, positions and orders only.
    Activities are bound through ledger ingestion; the closed history window grows
    over time, so including it made every preview look stale.
* **Conflicts with or additions to §8.5, identified before implementing:**
  1. §8.5 says approval "writes a `halt_events` entry", but `halt_events.action` only
     allows HALT/RESUME/RESUME_REFUSED. Approval is therefore recorded as a HALT with
     reason `REBASELINE` (consistent with "the halt remains"). Proposals, refusals,
     expiries and duplicates go to the new `rebaseline_events`.
  2. Additions §8.5 doesn't state: the economic-ledger binding, a second fresh
     reconciliation at confirmation, idempotent duplicate confirmation, and keeping a
     request PROPOSED after a mistyped amount.
  3. The "unexplained cash residual" is interpreted as residual + unexplained activity.
     A deposit recorded as an activity has a zero cash residual, but is still
     unexplained to the bot.
  4. §8.5's separate, audited ledger `ADJUSTMENT` command (for position differences)
     is **not implemented**. Rebaseline refuses whenever positions differ.

**2. Activity attribution** (`oms/classify.py`). Timing alone is never evidence.
* **FEE** is ATTRIBUTABLE only when all of these hold:
  * it references (`order_id`) a bot order with booked executions;
  * it is a charge;
  * that order's fills carry no fee of their own (no double booking);
  * no attributable fee for that order already has the same amount and description
    (a second representation is UNEXPLAINED).
* **DIV** is ATTRIBUTABLE only when all of these hold:
  * symbol, `qty` and `per_share_amount` are present, and amount = qty × per-share to
    the cent;
  * the ledger held exactly `qty` at payment;
  * the ledger held at least `qty` continuously for 30 days before payment.
* **INT** is EXTERNAL. Everything else, and any uncertainty, is UNEXPLAINED: always
  preserved, never booked, gates block, reconciliation reports a MISMATCH.
* **Unresolved assumptions and Alpaca fields to verify before M2** (contract-test
  items):
  * whether FEE activities carry `order_id`, plus a fee sub-type/`description` (REG,
    TAF) and quantity;
  * whether fees are ever reported on fills (only `fees = 0` is assumed today);
  * whether DIV activities carry `qty` and `per_share_amount`, and whether an
    ex-/record date is available (from the activity or a corporate-actions source).
    Without it, entitlement uses the 30-day continuous-holding assumption, and a
    dividend paid after a position closed is UNEXPLAINED.
  * the descriptions or sub-types for INT and other external cash movements.

**3. Ambiguous-order absence confirmation.**
* **Evidence required by `confirm_absent`** (operator only):
  * status `SUBMIT_AMBIGUOUS`;
  * nothing ever found;
  * ≥ 3 not-found lookups whose latest is ≥ send + 10 min, and **spanning ≥ 10 min**
    (failed lookups are not evidence; a 404 is evidence, not proof);
  * a clean, complete order-list scan after the window;
  * a complete, clean activity scan whose closed end is ≥ send + 10 min;
  * a reconciliation run no older than 10 minutes and newer than all of that evidence.
* **What that run must look like:** INCONCLUSIVE, with **exactly one** non-advisory
  finding, namely this order's `AMBIGUOUS_ORDER_UNRESOLVED`, marked
  `absence_consistent`.
* **What `absence_consistent` requires:** every other dimension clean, the order list
  complete and without the order, a complete history window whose closed end is
  ≥ send + 10 min (mature coverage), and no other unresolved submission.
* **Not accepted:** a CLEAN (`OK`) run, synthetic or not, any unrelated INCONCLUSIVE
  or MISMATCH finding, or another ambiguous order.
* **Unchanged:** late adoption still halts `RECON_ORDER`.

**4. Inconclusive escalation.** §8.3 says a run is "re-checked on the next cycle" and
"5 consecutive INCONCLUSIVE results escalate". The halt therefore counts distinct
**reconciliation cycles**, not raw runs:
* INCONCLUSIVE runs less than 60 s after the last counted run (`inconclusive_cycle_interval`)
  belong to the same cycle;
* any CLEAN or MISMATCH run ends the streak;
* the 5th cycle halts `RECON_INCONCLUSIVE_REPEAT` once;
* each run records `inconclusive_cycle` in its evidence.

Justification: an immediate retry repeats the same observation rather than showing
that the condition persists. Fail-closed is not weakened: every INCONCLUSIVE run
already blocks new entries through the transient `RECONCILIATION_INCONCLUSIVE` gate,
so a burst of retries can only delay a persistent halt during which no new risk can be
taken. Cycles at the normal cadence (≤ 120 s sync age) escalate exactly as before.

### 12.19 Step 13.2: rebaseline accounting corrections (as built)

Supersedes the following parts of §12.18:
* the dividend rule;
* the unexplained-amount formula;
* the "everything before the active baseline is acknowledged" behaviour, which is
  removed.

**1. Dividends.** The 30-day holding heuristic is gone. Every `DIV` activity is
preserved with attribution UNEXPLAINED ("entitlement unverified"):
* it is never booked;
* gates block (`UNEXPLAINED_ACTIVITY`);
* reconciliation reports MISMATCH `DIVIDEND_ENTITLEMENT_UNVERIFIED`;
* an operator may acknowledge it through a rebaseline as *not the bot's*, and it is
  still never booked.

Holdings on the payment date are never used. Required before M2 (contract or data
items):
* the DIV activity's `symbol`, `qty` and `per_share_amount`;
* a **verified ex-dividend (or record) date** for the symbol, from the activity or a
  corporate-actions source;
* the **eligible quantity** at that date (from the ledger's fill history, which would
  then be checked against it);
* the payment date, and how Alpaca represents reinvestment, foreign withholding and
  corrections or reversals.

The schema's attribution set (ATTRIBUTABLE/EXTERNAL/UNEXPLAINED) is unchanged:
"unresolved" is stored as UNEXPLAINED and distinguished by the finding code.

**2. Event-specific acknowledgement** (migration 0007, `rebaseline_acknowledgements`;
`ledger/acknowledgements.py`).
* **What an approved rebaseline records:** one row per acknowledged broker activity
  (by activity id) and per acknowledged external broker order (by client order id),
  with a verbatim copy of the payload: the raw activity JSON, or a canonical order
  record (ids, symbol, side, qty, filled qty, status). The amount and the
  `in_baseline` flag are recorded too.
* **When an event counts as acknowledged:** only if it is listed for an APPROVED
  request *and* its stored payload is byte-identical.
* **No time-range suppression:**
  * gates and reconciliation evaluate every UNEXPLAINED activity, whatever its
    timestamp, minus acknowledged ones;
  * the full-history re-scan always starts at the **first** (INIT) baseline;
  * an external order is skipped only when that exact closed order was acknowledged.
* **Newly discovered events** (e.g. fills posted after a rebaseline but timestamped
  before it) are flagged, and block, like any other.
* **Changed payloads:** a replay with different content is an `ACTIVITY_CONFLICT`
  MISMATCH (fail closed) for *every* activity type. Step 13.1 surfaced only fill
  conflicts; preserved and cash activities now also reach the findings. The stored
  row and its acknowledgement are never rewritten.
* **Binding:** the preview stores the exact list (ids, payloads, amounts, flags). At
  confirmation the freshly recomputed list must be identical; otherwise `EXPIRED`.

**3. The cash equation.** B is the active baseline (cash `B.cash`, observed at
`B.taken_at`). An activity is "since B" when it was **first ingested** after
`B.taken_at`. The rebaseline observation follows a complete activity read, so
anything ingested by then is already reflected in `B.cash`.

```
movements M = Σ net(activities ingested after B.taken_at)        (every attribution)
residual  R = broker_cash − (B.cash + M)    cash that moved with NO activity at all
counted   U = Σ net(UNEXPLAINED, unacknowledged activities ingested after B.taken_at)
net       N = R + U                         the amount the operator types
```

* An activity's amount is never in R, because it is inside M; so it is counted once,
  as its own item.
* Unexplained activities ingested *before* B (`in_baseline`) are listed for
  acknowledgement, but their cash is already in `B.cash`, so they are not in N.
* Reconciliation uses the same R (`CASH_UNEXPLAINED` when R ≠ 0, no tolerance).
* The preview shows R, each activity and order with its amount, and N. Confirmation
  requires the exact Decimal value of N; zero must be typed when N = 0.
* Examples:

  | Case | R | Items | N |
  |---|---|---|---|
  | $200 deposit with no activity | 200 | — | 200 |
  | $200 deposit reported as `CSD` | 0 | 200 | 200 (not 400) |
  | +100 and −100 deposits | 0 | +100, −100 | 0 (both still acknowledged) |
  | Silent −100 plus a +100 activity | −100 | +100 | 0 |

* Using the ingestion time rather than the activity timestamp means a late-posted,
  back-dated activity is counted when first seen. If its cash was in fact already
  part of an earlier acknowledged residual, R becomes non-zero and the system halts.
  It is never absorbed silently.

**4. Position safety (operational limitation).** Rebaseline still refuses whenever
broker and ledger positions differ. The audited `ADJUSTMENT` command of §8.5 is **not
implemented** in M1. A position difference therefore stays a persistent halt that only
a code-level remedy (a future audited adjustment command) can clear. Operators must
not attempt to fix it by rebaselining or by editing the ledger.
### 12.20 Step 14: runtime orchestration and the operator CLI (as built)

Package `app/`: `cli.py` (argparse, D14-1), `commands.py` (lock → open → operation →
close), `runtime.py` (wiring and sequences), `init_ledger.py`, `views.py` (read-only
inspection), `lock.py`, `paths.py`, `config.py`, `exit_codes.py`, `safety.py`,
`simulation.py`, `sim_calendar.py`. Ledger additions: `ledger/backup.py`,
`ledger/migration_guard.py`, `MigrationRequiredError`, `migrator.pending_migrations` /
`apply_pending_atomic`, `Database.open(auto_migrate=)`, and an observe-mode provenance
check in `CalendarStore.record_fetch`. **No schema change.** Console script:
`swing-trading-bot = swing_trading_bot.app.cli:entrypoint`.

**1. Broker access and credentials.** Commands receive broker access only through an
injected `Dependencies.broker_factory`. Step 14 ships **no** production factory and never
reads `.env`, settings or credentials: broker-backed commands exit 7 (`CONFIG`,
`BrokerNotConfiguredError`) until the separately authorized Step 15 credential loading.
All Step 14 tests use the real `AlpacaPaperReadAdapter` over an offline GET-only fake
transport (`tests/support/alpaca_account.py`).

**2. Command side effects** (contract in `app/safety.py`, verified by
`tests/integration/test_cli_safety.py`):

| Command | Broker | Ledger writes | OMS recovery | Halts | Confirmation | Lock |
|---|---|---|---|---|---|---|
| `status` | none | none | no | none | none | shared |
| `status --broker` | read-only | none | no | none | none | shared |
| `portfolio` | none | none | no | none | none | shared |
| `orders` | none | none | no | none | none | shared |
| `halts` | none | none | no | none | none | shared |
| `audit` | none | none | no | none | none | shared |
| `init-ledger` | read-only | creates a new ledger | no | creates INITIALIZED | `--confirm-path`, `--confirm-account`, `--confirm-environment` must match | exclusive |
| `migrate` | none | schema migration (after verified backup) | no | none | `--backup PATH` (new file) | exclusive |
| `backup` | none | writes a new backup file only | no | none | PATH must not exist | shared |
| `sync` | read-only | sync / audit records | yes | may create (never clears) | none | exclusive |
| `reconcile` | read-only | sync / reconciliation / audit records | yes | may create (never clears) | none | exclusive |
| `halt` | none | operator audit records | no | creates OPERATOR | `--reason` | exclusive |
| `resume` | read-only | sync / reconciliation / audit records | yes | clears only after a CLEAN reconciliation | `--reason` | exclusive |
| `resolve-order` | read-only | sync / reconciliation / audit records | yes | may create (never clears) | `--confirm-absent` | exclusive |
| `rebaseline propose` | read-only | reconciliation / audit records | no | may create (never clears) | `--reason`; ledger halted | exclusive |
| `rebaseline approve` | read-only | reconciliation / audit records | no | re-records REBASELINE (halt kept) | exact `--acknowledge-unexplained` amount, `--reason` | exclusive |
| `simulate` | SimBroker only (in memory) | new simulation ledger only | yes | inside the simulation ledger only | none | exclusive |

Inspection commands open the ledger without migrating, read one `query_only` snapshot,
never call the broker and never run recovery or invariant halting; a test proves the
full-ledger fingerprint and the broker call log are unchanged. Broker cash/equity is
never displayed.

**3. Runtime lifecycle** (§10.1). Every operational command: dependency/config checks →
exclusive lock → `Database.open(auto_migrate=False)` (mode, identity, integrity) →
post-commit invariant check (§10.1 step 2; a violation halts `LEDGER_INVARIANT`) →
command sequence → close → unlock. The **sync cycle** (`runtime.sync_cycle`, used by
`sync`, `reconcile`, `resume`):

1. preflight, outside any transaction: account, configuration, clock. The connected
   account/environment must equal the identity recorded by `init-ledger`; a mismatch
   halts `RECON_ACCOUNT` **before** anything is ingested. Auth / malformed payload →
   §10.5 halt (`AUTH`, `BROKER_PAYLOAD`, …); any other read failure stops the cycle with
   exit 8 and no halt;
2. calendar refresh when the newest accepted fetch is ≥ 24 h old or covers < 30 days
   ahead (fetch window: today − 7 … today + 60);
3. `recover_after_restart`: forced lookups → sync → escalation (never a send);
4. marks for held symbols;
5. `LossLimitMonitor.evaluate`;
6. `broker.last_successful_sync` written only when the sync status is OK.

`reconcile` adds `Reconciler.run` and `TradingGates.run_startup`. `resume` runs the
`reconcile` sequence, then `resume_after_clean_reconciliation` (refusals audited as
`RESUME_REFUSED`). `resolve-order`: preflight → one lookup → list scan → activity scan
→ reconciliation → evidence-gated `confirm_absent`. `rebaseline propose/approve`:
preflight → `Rebaseliner.propose/confirm` only (no separate sync cycle, so nothing else
moves the evidence the proposal is bound to).

**4. Safe initialization** (`init-ledger`). Target checks (no ledger or SQLite sidecar;
the data directory may contain only `sim/`, the ledger lock file and `.gitkeep`) →
exclusive lock → read-only evidence: account, configuration, clock, positions, open
orders, a tail-confirmed 7-day activity window, both calendars, then account, positions
and open orders again. Refused unless: environment `paper`; USD; not restricted; no
pending fees; flat (no positions, no open orders) on both reads; activity history read
to its end; cash unchanged between reads. Then the operator must confirm the exact
target path, account id and environment (`CONFIRMATION_REQUIRED`, exit 3, nothing
created, the required values are shown). The ledger is built under a temporary name —
calendars (must be ACCEPTED), $5,000 virtual portfolio, recorded identity, INIT broker
baseline (broker cash; equity = cash on a flat account) — in one transaction, then
hard-linked into place (no-clobber) and verified. The INITIALIZED halt from database
creation is kept. No other command creates a ledger (`Database.open` refuses a missing
file).

**5. Migration safety** (D14-5). `Database.open` reads the stored mode *before*
migrating and refuses a mismatch; an observe ledger with pending migrations is always
refused (`MigrationRequiredError`), as is any ledger opened with `auto_migrate=False`
(all CLI opens). Simulation ledgers keep the earlier auto-migration when opened directly.
`migrate --backup PATH` (exclusive lock): verify app id, mode, migration history (gaps,
newer schema, checksum/name, `user_version`) and integrity → consistent online backup to
a new 0600 file, verified from a fresh connection (`integrity_check`, app id, schema
version, content digest over schema and every row) → all pending migrations in **one**
transaction → reopen. A failure or a killed process leaves the original at its previous
version (tested in-process and with `os._exit` mid-migration); an invalid backup aborts
before any migration.

**6. Concurrency** (D14-2). `<ledger>.lock` + `fcntl.flock`, non-blocking; exclusive for
operations, shared for inspection and backup; taken before the database is opened or
migrated and held until the command finishes. The kernel releases it when the process
dies (tested with a spawned process killed while holding it). The JSON note in the lock
file (pid, host, command) is diagnostic only; ownership is never read from it. Deviation
from §10.1: one lock per ledger, not a single `data/bot.lock`, so simulations never block
the observe ledger.

**7. Exit codes** (`app/exit_codes.py`). 0 OK, 1 internal error or failed scenario,
2 usage, 3 refused, 4 persistent halt raised in this run, MISMATCH or account mismatch,
5 INCONCLUSIVE / incomplete sync, 6 lock held, 7 configuration (incl. no broker access),
8 broker unavailable, 9 ledger (missing, mode, integrity, migration, backup).
Precedence: 1 > 2 > 6 > 7 > 9 > 4 > 8 > 5 > 3 > 0. *(Step 15B.2–15B.3 added 10
`BROKER_PAYLOAD` between 4 and 8; see §12.24 ¶3.)* Inspection commands return 0 while
reporting a halt; `sync` returns 0 when it completed even if a pre-existing halt is still
set; INCONCLUSIVE and MISMATCH are never 0. `--json` gives a stable envelope (`command`,
`command_id`, `exit_code`, `exit_meaning`, `hypothetical`, `result`); logs are redacted
JSON on stderr carrying the `command_id`.

**8. Simulation isolation** (D14-3, D14-4). `simulate SCENARIO|all [--run-id]` creates a
new ledger under `<data>/sim/` and runs one scripted scenario in one process against an
in-memory SimBroker with a synthetic weekday calendar (source `sim:synthetic-weekdays`).
`CalendarStore` rejects every calendar source except `alpaca` for observe ledgers, so
synthetic calendars can never become observe evidence. Observe loss baselines accept
`OFFICIAL` closes only (D14-8). `app/simulation.py` is the only `app/` module that
constructs `SimulationExecution`/`OrderManager` (AST test) and is imported lazily by
`simulate` only. All simulation output and inspection of simulation ledgers is labelled
hypothetical. Scenario proposals are fixed and scripted (no signal logic);
`TRADING_ENABLED` is set only inside the simulation wiring. Scenarios: `happy-path`,
`partial-cancel`, `ambiguous-adopt`, `never-received` (operator absence confirmation at
+26 min), `restart-recovery` (crash after submit, restart, adoption),
`unexplained-deposit` (MISMATCH → refused resume → rebaseline → resume).

**9. Idempotency.** Verified on economic effects, not the whole-ledger fingerprint:
repeated sync/reconcile with unchanged broker evidence never duplicates fills, fee
bookings, cash entries or activities and never changes reservations, positions, order
state or order events, while reconciliation runs are appended and outcomes repeat.
Existing step 13 behaviour, now documented: every MISMATCH run re-records its halt (a
`halt_events` row referencing that run); the halt state itself is unchanged.

**10. Deviations and decisions.**
1. Per-ledger lock files (above).
2. `migrate` was added to the §4.1 command list; `portfolio`, `orders`, `halts` and
   `audit` were added as read-only inspection commands.
3. `adjust` is not implemented (D14-9, §12.19 ¶4).
4. `status --broker` evaluates gates read-only and never writes; it does not run
   preflight (which can halt).
5. A preflight identity mismatch halts `RECON_ACCOUNT` before ingestion (the reconciler
   reports the same as a MISMATCH, but only after a sync would have ingested the other
   account's activities).
6. *(Superseded by §12.21: the §10.5 broker-outage circuit breaker is implemented
   with the approved policy.)*

### 12.21 Step 14.1: broker-outage circuit breaker (as built)

Implements §10.5 "repeated failures → … escalate to persistent halt after a configurable
outage duration" with the approved policy. Supersedes §12.20 ¶10.6. Code:
`ledger/broker_outage.py` (state, policy, persistence, escalation),
`oms/lifecycle.read_error_outage_category` (classification, next to `read_error_halt`),
`app/runtime.RecordingBroker` and `sync_cycle` (integration), `risk/gates.py`
(`BROKER_OUTAGE_ACTIVE`), `config/risk.yaml` → `broker_outage` (policy values; not a
trading limit). **No migration.**

**1. Policy** (`OutagePolicy`; `risk.yaml` `broker_outage`). The persistent halt
`BROKER_OUTAGE` (existing reason) is raised only when **all** hold for the current
streak: ≥ 3 *counted* failed sync cycles; ≥ 30 min from the first failure to the latest
counted failure; counted cycles ≥ 60 s apart (a failure < 60 s after the last counted
cycle updates `last_failed_at` but is not counted). Boundaries are inclusive (exactly
30:00 and exactly 60 s qualify). Only counted failures advance escalation, so one
failure followed by any amount of silence never escalates.

**2. What counts.** A sync cycle (`sync`, `reconcile`, `resume`) is *failed* when any
broker read in it raised a qualifying error: `ServiceUnavailableError` (network →
`NETWORK`, timeout → `TIMEOUT`, 5xx → `SERVER_ERROR`, otherwise `UNAVAILABLE`),
`RateLimitedError` (`RATE_LIMITED`), or an `IncompleteRetrievalError` caused by one.
`ServiceUnavailableError` gained a `kind` tag set by the transport, so categories need no
message parsing. Never counted: anything `read_error_halt` maps to a dedicated halt
(auth, unmapped status, malformed payload), lookups that found nothing, other request
errors, identity mismatches, ledger, reconciliation and operator outcomes — those keep
their existing behaviour. All services in the runtime read through one
`RecordingBroker`, which remembers every escaped broker error during the cycle (after
the adapter's own bounded retries); the cycle is recorded **once**, with the first
qualifying category, however many requests failed.

**3. Durable state.** `sync_state['broker.outage']`, JSON with UTC timestamps:
`active`, and for the streak `first_failed_at`, `last_failed_at`, `last_counted_at`,
`counted_cycles`, `category` (latest), `categories` (all seen), `escalated_at`. A cycle
is identified by its start time from the injected clock; a report not later than
`last_failed_at` is a duplicate and changes nothing. Process restarts read the same
row; nothing depends on uptime or in-memory counters.

**4. Escalation and reset.**
* Escalation writes the halt (actor `outage_breaker`, detail lists cycles, span and
  categories, and any prior halt) and `escalated_at` in **one** transaction; later
  checks of the same streak never write another halt event.
* A cycle that is *complete* — preflight OK, sync OK, calendar refresh and marks without
  error, no qualifying failure — and later than the last failure resets the streak (the
  old streak is kept under `previous`). A reset never clears a halt.
* Any other cycle (partial, auth, identity, payload, …) leaves the streak unchanged
  (`UNCHANGED`).
* New entries are blocked at once: an active streak is a TRANSIENT gate finding
  `BROKER_OUTAGE_ACTIVE`, independent of the 120 s sync-age rule; live broker failures
  also block through `BROKER_UNAVAILABLE`.
* Recovery from `BROKER_OUTAGE` is the existing workflow only: fresh broker evidence, a
  CLEAN reconciliation and an explicit `resume`.

**5. Exit codes.** A cycle with a qualifying failure exits 8; one that escalates exits 4;
a cycle that ran but did not complete (e.g. calendar refresh failed) now exits 5 instead
of 0 (correction to §12.20 ¶7).

**6. Scope and deviations.**
1. The breaker observes sync cycles only. `resolve-order` and `rebaseline` run preflight
   without a sync cycle; their broker failures exit 8 but are not counted.
2. Policy values live in `risk.yaml` (as §10.5's "configurable" asks) under a section
   that is explicitly not a trading risk limit; the model defaults equal the approved
   values.
3. `BROKER_OUTAGE_ACTIVE` does not block risk-reducing exits by itself (live broker
   failures already do, via `EXIT_BLOCKING`); exits do not exist in M1.

### 12.22 Step 15 Phase A: read-only connectivity, contract suite and hardening (as built)

Offline only: no Alpaca request, no real credentials and no real ledger were touched.
**No schema change.**

**1. Credential loading** (`app/credentials.py`; D15-1, D15-2). Exactly one source:
process environment (`ALPACA_*`, `TRADING_*`, capital settings) **or** `STB_ENV_FILE`
(absolute path). Both present → refused; neither → refused; no `.env` discovery. A file
must be a regular file (not a symlink), owned by the current user, without group/other
permission bits, not tracked by Git (`git ls-files --error-unmatch`), and contain only the
known names. Values stay `SecretStr`; errors name variables, never values. Loading is lazy
— only a broker-backed command loads credentials — and the loaded secret values are
registered with every active log redactor (`observability.logging.register_secrets`) and
redacted from CLI output. Nothing credential-related is written to the ledger.

**2. Production broker factory** (`app/broker_factory.py`, `commands.production_dependencies`
is now the CLI default). It constructs only `AlpacaPaperReadAdapter` over
`AlpacaReadTransport`. Transport hardening (`broker/alpaca_http.py`):
* `ALLOWED_ORIGINS` — exactly `https://paper-api.alpaca.markets` and
  `https://data.alpaca.markets`; `verify_allowlist()` refuses to start if the SDK's base
  URLs ever differ, and the SDK's live host can never be listed;
* `session.trust_env = False` (no environment proxies, CA overrides or `.netrc`);
* redirects are not followed and a response from another host is refused;
* the transport's only public method is `get_json` (HTTP GET); no SDK `TradingClient`.

**What proves paper mode:** the transport can reach only the paper host, so any
authenticated response is from a paper account. The `PA` account-number prefix is reported
by contract checks but never used as proof. **Strict identity**
(`account_identity_problems`): canonical lowercase UUID account id, status `ACTIVE`,
currency USD, expected environment label. `init-ledger` refuses on any problem; the runtime
preflight treats one as an identity failure (persistent `RECON_ACCOUNT` before any
ingestion), in addition to the recorded-identity match. Structural tests (AST: no
`TradingClient`, no `alpaca.trading` import, no write-verb call in `broker/`; the factory
constructs exactly the two read classes; the transport exposes only `get_json`) and
runtime tests (every request is HTTPS GET to an allowlisted host; a redirect to the live
host is refused; `SimulationExecution` refuses the read adapter).

**3. New commands.** `broker-check` — ledger-free, no lock, GET-only: environment, masked
account id, status, restrictions, U6 protections, market open; never balances; exit 3 on
uncertain identity (replaces `scripts/check_connection.py`, removed; D15-4).
`verify-ledger PATH` — read-only verification of the live ledger or a backup (mode
detection, migration history, full `integrity_check`, accounting invariants, halt state,
masked identity); shared lock; never migrates (D15-8).

**4. Fault-injection findings fixed.**
* *An empty file at the ledger path was modified by a refused open*: `PRAGMA
  journal_mode = WAL` initialises an empty file. Every open, migration, mode detection,
  backup and verification now checks the 16-byte SQLite header first
  (`db.require_sqlite_header`; a bad header is `DatabaseCorruptError`, matching the
  existing contract) and never connects to a non-database file.
* *Unreadable outage state* (`sync_state['broker.outage']`) now fails closed with a
  `PersistenceError` (exit 9) instead of an unhandled decode error.

**5. Contract suite** (`tests/contract/`). `checks.py` holds nine read-only checks shared
by the offline replay (`test_contract_replay.py`, always runs, synthetic payloads labelled
as such) and the opt-in live module (`test_alpaca_paper_read.py`, skipped; not run).
The GET-only spy is now the budgeted session of §12.23. Live runs write raw
responses only to the Git-ignored `contract-evidence/<run>/raw/` and a sanitized
`summary.json` (booleans/enumerations only) that is checked against the account id, account
number, balances and secrets before it is written; nothing is committed without approval.
`matrix.py` separates what is verified offline, what needs a real read-only response, and
what needs paper orders (rows C1–C13; reproduced in M1_PLAN §5.3).

**6. Performance** (`tests/perf/`, opt-in `RUN_PERF=1`; results in `docs/PERFORMANCE.md`):
the full-ledger fingerprint meets 250 ms at one year of 10-minute reconciliations (160 ms)
but not at two (319 ms); the full-history re-scan exceeds half of an assumed 200/min rate
limit above ≈ 6,000 activities. No optimisation was implemented (D15-9).

**7. Deviations.** Identity validation requires a canonical UUID account id (Alpaca
documents account ids as UUIDs; to be confirmed by the live run, C1). `verify-ledger` may
create `<path>.lock` beside the verified file. `broker-check` takes no ledger lock.

### 12.23 Step 15A.1: live-run harness hardening and reconciliation retry safety (as built)

Offline only; no Alpaca request was made. **No schema change.** Supersedes the `spy.py`
description in §12.22 ¶5.

**O2 — head re-check failures no longer repeat the observation** (`oms/reconciler.py`).
The §8.3 head re-check now has three results (`HeadCheck`): `STABLE` (done), `CHANGED`
(new activity appeared → the existing bounded repeat, `max_snapshot_retries` = 3,
unchanged) and `READ_FAILED` (the head could not be read). On `READ_FAILED` the observation
stops: an INCONCLUSIVE `READ_FAILED` finding (subject "activity head re-check") is added
next to `ACTIVITY_HEAD_UNSTABLE`, the run is INCONCLUSIVE (never CLEAN), and the evidence
records `attempts` and `head_check`. Nothing else changed: ingestion stays idempotent,
completeness markers advance only from complete forward windows, late/back-dated/conflicting
activity detection is unchanged, the repeated-INCONCLUSIVE halt still applies, and the
outage breaker keeps its approved sync-cycle scope. Before this change a failed head read
repeated the whole observation — full-history re-scan included — up to three times.

**Supervised live run** (`tests/contract/live_run.py`, `supervisor.py`, `outcomes.py`;
`python -m tests.contract.live_run`, guarded by `RUN_ALPACA_PAPER_TESTS=1`; the pytest
module `test_alpaca_paper_read.py` is a thin wrapper):
* **R1 budgets.** One `RequestBudget` for the whole run — setup (the real CLI
  `broker-check` through the same session) and every contract check: at most 120 actual
  HTTP attempts (counted *before* sending, retries included) and 300 s on a monotonic clock.
  Each request's connect/read timeouts are cut to the remaining time; retry backoff uses a
  bounded sleep that refuses to sleep past the deadline. Exhaustion raises a
  `BaseException` (`BudgetExhausted`) that the code under test cannot swallow; once
  exhausted nothing more is sent; unrun checks are `NOT_RUN` → rows INCONCLUSIVE; a partial
  sanitized summary is still written.
* **R2 rate-limit evidence.** Only `X-RateLimit-Limit`, `-Remaining`, `-Reset` and
  `Retry-After` are kept from responses. Per host: `OBSERVED` only when every response
  carried all three X-RateLimit headers as integers, one consistent limit and
  `remaining ≤ limit`; otherwise `UNVERIFIED` (`HEADERS_MISSING`, `HEADERS_MALFORMED`,
  `LIMIT_INCONSISTENT`). No limit is ever assumed.
* **R3 sanitized failures.** Checks contain no `assert`; they raise `ContractViolation` or
  `InsufficientEvidence` with fixed codes. `run_check` maps every exception class to a fixed
  code and discards its message and traceback; pytest assertion rewriting is disabled for
  the live modules and the verdict uses `pytest.fail(..., pytrace=False)` with codes only.
* **R4 outcomes.** Each of C1–C13 is VERIFIED (check passed *and* the real response
  exercised the row), INCONCLUSIVE (broker error, insufficient evidence, not run, or not
  exercisable on a flat account — e.g. C5, C7 with ≤ 10 activities, C9 without positions or
  orders), FAILED (observed contradiction, including a payload that breaks the parsing
  contract or a non-404 unknown-id lookup) or DEFERRED (C10–C13). Execution status
  (`COMPLETED` / `STOPPED` + fixed stop code) is reported separately. The run stops on
  authentication failure, persisting rate limiting, invalid identity, a write attempt or a
  non-allowlisted destination, or budget/deadline exhaustion.
* **R5 evidence.** `contract-evidence/` must be Git-ignored (checked with
  `git check-ignore`); directories 0700, files 0600 created `O_EXCL | O_NOFOLLOW`; symlinked
  roots, existing run directories and path traversal are refused. The sanitized summary is
  checked against every secret and every identifier, account number, balance, position and
  order/activity id seen in the raw bodies before anything is written.
* **R6 redaction.** The redacting logger is installed before credentials are loaded; the
  loaded values are registered with it and redacted from logs and output. *Since Step
  15B.5C (§12.29) raw bodies are no longer passed through the log redactor: they are
  structurally sanitized at write time.*

### 12.24 Step 15B: first supervised read-only run, account payload compatibility and exit-code consistency (15B–15B.3)

Offline implementation; the only Alpaca contact was the one authorized run below. **No
schema change.** Nothing here touches risk limits, execution boundaries or the ledger
schema.

**1. First supervised run (15B, 2026-11-12).** One authorized run of
`python -m tests.contract.live_run`, credentials from an explicit `STB_ENV_FILE`. It
**stopped in setup** after **1** request (`GET /v2/account`, HTTP 200, host
`paper-api.alpaca.markets`, method GET only): the response omitted `pattern_day_trader`,
which the mapper then required, so `broker-check` failed closed (`InvalidBrokerDataError`,
then reported as exit 8 → stop code `BROKER_CHECK_EXIT_8`). No contract check ran:
C1–C9 `INCONCLUSIVE/NOT_RUN`, C10–C13 `DEFERRED`. The one response carried
`X-RateLimit-Limit: 200` (a single sample — not a verified limit). The market-data host
and `/v2/account/configurations` were never contacted, so **the configurations payload
(`pdt_check`, `dtbp_check`, `suspend_trade`, …) is still unverified**. Raw evidence stays in
the Git-ignored `contract-evidence/`; only field names and JSON types were inspected
afterwards (15B.1). Observed account fields that the SDK model lists but the response
lacked: `pattern_day_trader`, `daytrade_count`. No claim about Alpaca's intent is made
from the API itself; see §12.25 ¶2 for the installed SDK's deprecation note.

**2. `pattern_day_trader` is optional and informational (15B.2, Policy B).**
`AccountSnapshot.pattern_day_trader: bool | None`. Absent or `null` → `None` (**unknown,
never `false`**); `true`/`false` kept; any other present value → `InvalidBrokerDataError`
(`mapping.opt_boolean`, used for this field only). Every other account field is exactly
as strict as before (missing/`null`/wrong type still rejected). The field was and remains
used by nothing but `broker-check` output (JSON `null` when unknown): not by risk,
pre-trade, gates, identity, reconciliation, order submission or ledger persistence —
enforced by the structural test `tests/unit/test_pdt_isolation.py`. An unknown value
therefore grants no permission and bypasses no gate (tested through startup gates,
pre-trade, the order manager, init, reconciliation and sync). `pdt_check`/`dtbp_check`
stay **required**; relaxing them needs observed evidence and separate approval.
`SimConfig.pattern_day_trader` (default `False`) keeps simulation deterministic. The test
fake's `/v2/account` now has exactly the observed field names and JSON types, with
synthetic values only.

**3. Exit code 10 `BROKER_PAYLOAD` (15B.2–15B.3).** A broker response that breaks the
parsing contract is not an outage, so it no longer reports 8:
* CLI exception mapping (`broker-check`, any command failing on a read outside the
  runtime): `InvalidBrokerDataError` (incl. `UnknownBrokerStatusError`) → 10, checked
  before the generic `BrokerError` → 8. Auth, rate-limit, unavailable and not-found
  classifications are unchanged.
* `init-ledger`: `InitStatus.BROKER_PAYLOAD` → 10; evidence is gathered before anything
  is created, so no ledger or temp file is left behind.
* Runtime (`sync`, `reconcile`, `resume`, `resolve-order`, `rebaseline`): the malformed
  read still sets the persistent `BROKER_PAYLOAD` halt (§10.5) and never counts toward
  the outage breaker. **The operation that raised it** — starting from an un-halted ledger,
  with `BROKER_PAYLOAD` the only halt it raised (`CycleReport.halts_raised`) — exits 10.
  **An operation blocked by an already-recorded halt** (the payload halt from an earlier
  run, or any pre-existing halt the payload error lands on) exits 4. Before 15B.3 a
  repeated preflight payload error on an already-halted ledger exited 8 (`halt_once`
  wrote nothing, leaving a bare broker error). If another halt is raised in the same
  operation, 4 wins as before.
* Precedence is now 1 > 2 > 6 > 7 > 9 > 4 > 10 > 8 > 5 > 3 > 0; the relative order of every
  pre-existing code is unchanged. The halt is cleared only by the existing workflow
  (fixed payload, CLEAN reconciliation, explicit `resume`); a completed `sync` still
  exits 0 while a pre-existing halt remains set (§12.20 ¶7).
* The contract harness now stops with `BROKER_CHECK_EXIT_10` on a setup payload error.

**4. Open questions — account restriction fields (not implemented).** The observed
account payload also carries `trade_suspended_by_user` and `transfers_blocked`, which the
mapper ignores:
* `trade_suspended_by_user` may mirror the configurations flag `suspend_trade` (which a
  gate finding already covers, `BROKER_ACCOUNT_CONFIG_UNSUPPORTED`). That equivalence is
  **unverified**. *Corrected in 15B.4 (§12.25 ¶5):* `suspend_trade` is **not** checked
  only at startup. Its finding carries the STARTUP layer label but is recomputed from a
  fresh configuration read on **every** gate evaluation, and every entry path consumes a
  fresh evaluation, so a suspension set mid-session blocks the next entry. It is not
  checked by reconciliation, `init-ledger` or exit readiness, and
  `trade_suspended_by_user` is not read at all.
* `transfers_blocked`: Alpaca's field descriptions (not re-checked in this step) relate
  it to money transfers, not orders; whether it can signal a wider account hold is
  unknown, so it is not assumed harmless.
* Evidence needed: Alpaca's current `/v2/account` field definitions; whether
  `trade_suspended_by_user` always equals `configurations.suspend_trade`; whether either
  flag also changes `trading_blocked` or `status`; paper behaviour when `suspend_trade` is
  toggled (a configurations PATCH — a write, not authorized).
* **Blocker:** verified enforcement of trading suspension (both flags, at every entry
  decision) is required **before any real broker order execution is enabled** (M2).

**5. Still unverified / next steps.** C1–C9 (identity format, configuration values,
clock/calendar, flat-account lists, unknown-id 404, activity pagination, market data,
decimal strings); the configurations payload; rate limits beyond one sample. A second
supervised run is justified after review of 15B.2–15B.3 but **requires separate, explicit
authorization**. `init-ledger` against the real account stays blocked until C1–C9 are
reviewed.

### 12.25 Step 15B live run #2 and account configuration compatibility (15B.4)

Offline implementation after one authorized run. **No schema change, no migration;**
risk limits, execution boundaries and the HTTP transport are unchanged; no new
restriction gate.

**1. Live run #2 (2026-11-12, 21:35 ET, market closed).** One authorized run of
`python -m tests.contract.live_run`. It **stopped in setup** after **2** requests (both
`GET`, both HTTP 200, host `paper-api.alpaca.markets`): `/v2/account` parsed (same 37
field names and JSON types as run #1; `pattern_day_trader` absent → `None`), then
`/v2/account/configurations` lacked `pdt_check`, which the mapper required → `broker-check`
exit 10 → stop code `BROKER_CHECK_EXIT_10`. No contract check ran (C1–C9
`INCONCLUSIVE/NOT_RUN`, C10–C13 `DEFERRED`); the market-data host was not contacted.
Rate-limit headers: `X-RateLimit-Limit: 200` on both responses (two samples, not a
verified limit). Only field names and JSON types were read from the evidence.

**Observed configuration schema** (8 fields): `closing_transactions_only` bool,
`disable_overnight_trading` bool, `fractional_trading` bool, `max_margin_multiplier` str,
`no_shorting` bool, `ptp_no_exception_entry` bool, `suspend_trade` bool,
`trade_confirm_email` str. **Absent:** `pdt_check`, `dtbp_check` (and
`max_options_trading_level`, which the old test fake carried).

**2. Installed-SDK note (attributed, not independently verified).** The installed
alpaca-py **0.45.0** marks `AccountConfiguration.pdt_check`/`dtbp_check` and
`TradeAccount.pattern_day_trader`/`daytrade_count`/`daytrading_buying_power` as deprecated,
"removed from Alpaca responses on 2026-07-06 following the FINRA intraday-margin
migration". That matches both runs, but it is the SDK's statement; Alpaca's own API
documentation was not consulted (offline). `closing_transactions_only` does not appear in
the installed SDK at all.

**3. Policy B for `pdt_check` / `dtbp_check`.** `AccountConfigSnapshot.pdt_check` and
`.dtbp_check` are `str | None`, parsed with the existing `opt_text()`: absent or `null` →
`None` ("not reported" — never read as a restriction on or off); a present non-empty string
→ kept verbatim (values are not interpreted); empty string, boolean, number, array or
object → `InvalidBrokerDataError` (exit 10 / persistent `BROKER_PAYLOAD` halt, §12.24).
`no_shorting`, `max_margin_multiplier`, `suspend_trade` and `fractional_trading` are
unchanged and still required. Neither field was ever a decision input: the dependency
trace found them only in the model, the mapper, the SimBroker and the init-ledger baseline
dict; the structural test `tests/unit/test_config_field_isolation.py` keeps them out of
gates, risk, pre-trade, reconciliation, rebaseline, OMS, runtime, ledger and domain code.
`broker-check` never displayed them and still does not. `SimConfig.pdt_check` /
`dtbp_check` default to `"entry"` as before; `None` omits the key. The test fake's
configuration payload now has exactly the observed 8 names and JSON types (synthetic
values).

**4. Baselines and audit.** New broker baselines keep both keys — `"pdt_check": null`,
`"dtbp_check": null` when unreported — inside `account_config_json` (valid JSON, canonical
serialization). Historical baselines holding strings are untouched: rows are immutable
(trigger), rebaseline copies the stored dict verbatim (`rebaseline.py`), the loader does
not validate keys, `verify-ledger` treats the JSON as opaque, and the fingerprint of
existing rows cannot change. Tested: init stores `null`; historical-string ledgers load,
verify, sync and reconcile CLEAN while the broker omits the fields; the historical row's
digest and the `broker_baselines` fingerprint are stable; a rebaseline copies strings or
`null` verbatim.

**5. Restriction enforcement today (correction to §12.24 ¶4) and open questions.**
`TradingGates.evaluate()` reads account, configuration and clock fresh on every call; the
`suspend_trade`/`fractional_trading` finding (`BROKER_ACCOUNT_CONFIG_UNSUPPORTED`, STARTUP
layer label) is recomputed each time and blocks new entries whenever true. Pre-trade
requires a gate status at most 30 s old and an account snapshot at most 120 s old;
`OrderManager.place_entry` reads the account and evaluates the gates fresh before creating
an intent. `status`/`trading_blocked`/`account_blocked` additionally block via the
transient layer, pre-trade, exit readiness, reconciliation (INCONCLUSIVE) and
`init-ledger`. Not enforced anywhere: `trade_suspended_by_user` (SDK: "not allowed to place
orders"), `closing_transactions_only` (semantics **unconfirmed** — absent from the SDK;
not inferred from its name), `transfers_blocked` (SDK: money-transfer requests only).
`suspend_trade` is not checked by reconciliation, `init-ledger` or exit readiness. These
remain open; verified trading-suspension/closing-only enforcement at every entry decision
is an **M2 blocker** (not implemented in 15B.4).

**6. Next.** Live run #3 is justified after review of 15B.4 and **requires separate,
explicit authorization**. A mismatch inside a contract check is recorded as a FAILED row
and later checks still run; one in setup (`/clock` during `broker-check`) stops the run.
`init-ledger` against the real account stays blocked until C1–C9 are reviewed.

### 12.26 Step 15B live run #3 and the settlement calendar mapping correction (15B.5A)

Offline implementation after one authorized run. **No schema change, no migration;**
the TRADING mapping, the HTTP transport, risk limits, restrictions and execution
boundaries are unchanged.

**1. Live run #3 (2026-11-12, 22:06 ET, market closed).** Completed: 23 GETs of 120
(setup 3, contract 20), 4.0 s of 300 s, no retries, hosts `paper-api.alpaca.markets` and
`data.alpaca.markets` only. C1 identity, C2 restrictions, C3 clock, C6 unknown
client-order-id (real 404) and C8 IEX market data **VERIFIED**; **C4 FAILED**
(`CALENDAR_INCONSISTENT`); C5 and C9 INCONCLUSIVE (flat account); C7 INCONCLUSIVE
(`BROKER_INCOMPLETE_RETRIEVAL`: an activity's `description` is an empty string, which the
mapper rejects — not fixed in 15B.5A); C10–C13 DEFERRED. Rate-limit headers: data host
limit 200 (3 responses); trading host `LIMIT_INCONSISTENT` (20 responses). The stored copy
of one market-data body was made invalid JSON by the evidence redactor (not fixed in
15B.5A). Account settings observed: `fractional_trading` true, `no_shorting` false,
`max_margin_multiplier` ≠ 1 (U6 protections not in place).

**2. Observed SETTLEMENT response** (public market calendar; fields `date`, `open`,
`close`, `session_open`, `session_close`, `settlement_date`, all strings). With
`date_type=SETTLEMENT` and range 2026-11-02..2026-12-31, Alpaca returned 42 rows: one per
**trade date**, selected by `settlement_date` falling in the range. 0 rows had
`date == settlement_date`; 41 were identical to TRADING rows; the earliest `date`
(2026-10-30) preceded the range; every `settlement_date` was inside it; 2026-11-10 and
2026-11-11 (Veterans Day) both settled 2026-11-12. The TRADING and SETTLEMENT responses
agreed on every shared trade date.

**3. The defect.** The adapter used each row's `date` as a settlement day, producing
2026-10-30 (out of range — the C4 failure), 2026-11-11 (a bank holiday, not a settlement
day) and missing 2026-12-31. **It failed closed:** every real SETTLEMENT fetch was rejected
(the earliest trade date always precedes the range), so settlement coverage was missing,
`init-ledger` refused ("calendar rejected") and proceeds could settle only from a
session's own `settlement_date`. Even an accepted wrong fetch could not settle early: the
session value (Nov 10 → Nov 12) and the derived first settlement day (Nov 11) would
disagree → `ContradictoryCalendarError` → unresolved → unsettled.

**4. The correction.** `broker.mapping.settlement_days_from_raw`, used only by the
adapter's SETTLEMENT branch: every row needs a valid `date` and `settlement_date`
(missing, `null` or malformed → `InvalidBrokerDataError`, exit 10) with
`date < settlement_date <= date + MAX_SETTLEMENT_LAG_DAYS`; settlement days are the
sorted, de-duplicated `settlement_date` values; the trade `date` is validated but never
used as a settlement day. Unchanged and still enforced: `CalendarFetch` range and weekend
checks (an out-of-range or weekend `settlement_date` rejects the fetch), `CalendarStore`
conflict detection for repeated fetches, the cross-calendar audit (a session's
`settlement_date` must be a settlement day and equal the first settlement day after the
trade), staleness and coverage rules, `UNRESOLVED_SETTLEMENT`, and conservative cash
settlement (`settles_on` null or in the future → unsettled).

**5. Meaning and completeness limits.** A stored settlement day now means "a day on which
some trade settles" (domain docstring updated). A settlement-eligible date on which no
trading session settles (e.g. a bank day following an exchange-only holiday) is not
listed. Within covered dates this can only make a *derived* settlement date later, never
earlier: under T+1, any listed day strictly between a trade and its true settlement would
have to be the settlement of an earlier trade — which cannot fall after this trade's date
without being this trade's own settlement day. A derived date must also equal the
session's own `settlement_date` when present. Trades near the end of a SETTLEMENT range
settle beyond it (e.g. 2026-12-31 → 2027-01-04): with no settlement coverage for those
days the session's own value is used, exactly as before 15B.5A, and proceeds remain
unsettled until that date; with no session value either, the settlement is unresolved.
The remaining trust is in Alpaca's own `settlement_date` values (both calendars come from
the same provider and must agree).

**6. Tests.** Mapper matrix (valid, duplicates, ordering, missing/`null`/malformed `date`
and `settlement_date`, non-increasing dates, lag limit, range boundaries, weekend);
adapter, store and cash paths with real-shaped responses (Veterans Day, Thanksgiving,
Christmas, New Year; year-end settlement into January without January coverage — never
settled before 2027-01-04, and unresolved when the session value is also absent;
out-of-range and weekend rejection; conflicting repeat fetches; provider disagreement in
both directions; T+1 cash over Veterans Day); `init-ledger` accepting the real-shaped
calendar; the C4 contract check passing on it. The test fake's SETTLEMENT response now has
the observed shape (synthetic values).

**7. Next.** Live run #4 requires separate, explicit authorization. Expected: C4 VERIFIED;
C7 still inconclusive until the activity `description` fix (15B.5B); C5/C9 INCONCLUSIVE
on a flat account. `init-ledger` against the real account stays blocked until C4 and C7
are verified live and the flat-account INCONCLUSIVE rows are accepted on review.

### 12.27 Activity description compatibility and wrapped payload errors (15B.5B)

Offline implementation. **No schema change, no migration;** the settlement calendar
(§12.26), runtime, reconciliation, gates, OMS, risk limits, the evidence sanitizer and
broker-write behavior are unchanged.

**1. Observed C7 issue (live run #3).** The real account's one activity (a non-trade
activity) carried `"description": ""`. The mapper used `opt_text`, which rejects an
empty string, so the page raised `InvalidBrokerDataError`; `collect_window` wrapped it in
`IncompleteRetrievalError` (original kept as `__cause__`), and the C7 check reported
INCONCLUSIVE `BROKER_INCOMPLETE_RETRIEVAL`. The same wrapping made `init-ledger` and the
CLI report exit 8 ("broker unavailable") for what is a payload contract problem. The
runtime sync path already unwrapped the cause and raised the `BROKER_PAYLOAD` halt.

**2. Field-specific description rule.** `mapping._activity_description` (non-trade
activities only): absent or `null` → `None`; any string, **including `""`**, kept exactly;
boolean, number, array or object → `InvalidBrokerDataError`. It is evaluated before the
raw JSON is serialized, so a JSON-float description is a payload error rather than a
serialization `TypeError`. `text()`/`opt_text()` are unchanged, and every other field
(`id`, `activity_type`, `symbol`, `order_id`, …) is as strict as before. `""` and `None`
stay distinct end to end: the raw JSON is stored verbatim, and FEE twin detection
(`classify._fee`) compares the stored raw `description` with the mapped value — `""`
twins are detected exactly like named twins (booked once), while `""` vs absent are, like
any two different descriptions, distinct fees (unchanged rule).

**3. `payload_cause()`** (`broker/errors.py`): returns the `InvalidBrokerDataError`
(incl. `UnknownBrokerStatusError`) that is `exc` itself or the explicit `__cause__` of at
most `PAYLOAD_CAUSE_MAX_DEPTH` (4) nested `IncompleteRetrievalError` wrappers; nothing
else is traversed (no `__context__`, no other exception types). Timeouts, network
failures, 5xx, 429, auth errors and genuinely incomplete pagination (no cause, or a
non-payload cause) are never payload errors. Exceptions are not modified.

**4. Classification.**

| Where | Direct payload error | Wrapped payload error | Genuine incomplete / transient |
|---|---|---|---|
| CLI `exit_code_for` | 10 | **10** (was 8) | 8 |
| `init-ledger` | 10 `BROKER_PAYLOAD`, nothing created | **10** (was 8) | 8 `BROKER_UNAVAILABLE` |
| Runtime sync | persistent `BROKER_PAYLOAD` halt; 10 first, 4 when governed by a recorded halt (§12.24 ¶3) | unchanged (already unwrapped) | INCOMPLETE; outage breaker counts transient causes only |
| Outage breaker | not counted | not counted (unchanged) | counted per §12.21 |
| Reconciliation | read failure → INCONCLUSIVE | window incomplete → never CLEAN (unchanged) | INCONCLUSIVE |
| Gates | `BROKER_DATA_INVALID` (unchanged) | n/a (gates read account/config/clock) | `BROKER_UNAVAILABLE` |
| Contract outcomes | VIOLATION → FAILED `BROKER_PAYLOAD_CONTRACT_BROKEN` | **FAILED** (was INCONCLUSIVE `BROKER_INCOMPLETE_RETRIEVAL`) | INCONCLUSIVE `BROKER_INCOMPLETE_RETRIEVAL` |

Completeness is unchanged: a window that failed part-way is never complete and never
advances the coverage marker. As before (`OrderSync._ingest_all`), activities read on earlier good pages
are ingested idempotently ("nothing is skipped"); the malformed row itself is never
recorded. A malformed activity that recurs on every sync re-records the
`BROKER_PAYLOAD` halt event each cycle (existing mid-sync halt behavior); the halt is
never cleared automatically.

**5. Remaining C7 limitation.** C7's criterion is unchanged (`multi_page_exercised`:
more than 10 activities in the 30-day window). The real flat paper account had one
activity, so after this fix C7 is expected to be INCONCLUSIVE
`TOO_FEW_ACTIVITIES_TO_EXERCISE_PAGINATION` on live run #4 — tail confirmation,
page-size independence and window partition are then actually exercised, multi-page
pagination is not.

**6. Finding (resolved in Step 15B.5B.1, §12.28).** A JSON float anywhere in a
non-trade activity other than `description` (e.g. `net_amount`, `qty`, or an unmapped
field) was decoded as `Decimal` and made `raw_json()` raise `TypeError` before field
validation: exit 1 `INTERNAL`, no `BROKER_PAYLOAD` halt.

**7. Tests.** Description matrix (missing, `null`, `""`, non-empty, boolean, number,
JSON-float `Decimal`, array, object); other text fields still strict; global helpers
unchanged; structural check that only `description` uses the relaxed rule;
`payload_cause` (direct, nested up to the limit, bounded beyond it, transient causes,
`__context__` ignored, causes preserved); CLI exit 10 vs 8; collector with empty
descriptions, malformed first and later pages (partial never complete); runtime halt
(10 then 4), outage streak and coverage unchanged; reconciliation not CLEAN and gates
blocked by the persistent halt; `init-ledger` accepting `""` and refusing malformed
activities with exit 10 and no ledger (a timeout still exits 8); FEE twins with `""`;
C7 VERIFIED with empty descriptions, FAILED on a malformed later page, INCONCLUSIVE on a
genuine interruption and on too few activities.

### 12.28 Activity raw-JSON serialization (15B.5B.1)

Offline implementation. **No schema change, no migration;** field validation, the
description rule (§12.27), `payload_cause`, the settlement calendar (§12.26), runtime,
reconciliation, gates, OMS, risk limits, the evidence sanitizer and broker-write
behavior are unchanged.

**1. Root cause.** `decode_json` (`alpaca_http.py`) decodes with `parse_float=Decimal`, so
a JSON number with a fraction or exponent becomes `Decimal`, and the stdlib decoder also
accepts the non-JSON constants `NaN`/`Infinity`/`-Infinity` (as `float`). The only place
broker JSON is re-serialized is `mapping.raw_json()`, called by `activity_from_raw` for
both FILL and non-trade activities (`alpaca_read.get_activities`, `SimBroker`). It used
plain `json.dumps`, which raises `TypeError` on `Decimal` (before the money fields were
validated) and writes `NaN`/`Infinity` as invalid JSON. Offline, before the fix:

| Activity content | `sync` | `init-ledger` |
|---|---|---|
| numeric money field (`"net_amount": 10.5`) | exit 1, 1 — no halt | exit 1, nothing created |
| valid number in an unmapped field | exit 1, 1 — no halt | exit 1, nothing created |
| `NaN` in an unmapped field | exit 9 (the ledger's `json_valid(raw_json)` CHECK) | **exit 0 — ledger created** |

**2. Correction.** `raw_json()` is now a small canonical encoder (`mapping._encode`):

* Output is **byte-identical** to `json.dumps(raw, sort_keys=True, separators=(",", ":"))`
  for every payload that call accepted as valid JSON (property-tested), so stored
  `raw_json`, the `merge_activities` text comparison and idempotent re-ingestion are
  unaffected.
* A finite `Decimal` is written as its exact JSON number, `str(value)` (`1.50` stays
  `1.50`; `1e5` is written `1E+5` — numerically exact, canonical, not textually
  verbatim). Never via `float`, never as a string; no `default=str` coercion.
* NaN/±Infinity (`float` or `Decimal`), at any depth → `InvalidBrokerDataError`
  ("non-finite number").
* Values the decoder cannot produce (non-string keys, sets, bytes, arbitrary objects)
  still raise `TypeError`: a programming defect, deliberately **not** reclassified
  (exit 1). No broad exception handling was added.

Field validation is unchanged: money fields (`net_amount`, `qty`, `per_share_amount`,
FILL `qty`/`price`/`cum_qty`/`leaves_qty`) still require strings, so a numeric money
field is now an `InvalidBrokerDataError` from its own validator. A valid number in an
unmapped field is stored exactly (as integers already were). Valid string money keeps
exact `Decimal` processing; `""` vs `null` descriptions and FEE twin detection are
unchanged.

**3. Result.** Every such payload error takes the existing paths (§12.24, §12.27):

| Activity content | `sync` | `init-ledger` | C7 |
|---|---|---|---|
| numeric money field | exit 10, then 4; `BROKER_PAYLOAD` halt | exit 10 `BROKER_PAYLOAD`, nothing created | FAILED `BROKER_PAYLOAD_CONTRACT_BROKEN` |
| NaN/±Infinity anywhere | exit 10, then 4; halt | exit 10, nothing created | FAILED |
| valid number in an unmapped field | stored exactly; no halt | initializes | VERIFIED |

An existing persistent halt still governs (exit 4). Payload errors do not count toward
the outage breaker; a failed first or later page is never complete, never advances the
coverage marker, and the malformed row is never booked (earlier good pages are ingested
idempotently, unchanged); reconciliation is never CLEAN. Genuine interruptions stay
exit 8 / C7 INCONCLUSIVE, and too few activities stays C7 INCONCLUSIVE
`TOO_FEW_ACTIVITIES_TO_EXERCISE_PAGINATION`. Contract thresholds, pagination and tail
confirmation are unchanged.

**4. Remaining serialization notes.** (a) `ActivityRecord.content_key()` parses stored
`raw_json` with plain `json.loads`, so an *unmapped* non-integer number is compared as a
`float` there: two deliveries of one activity id differing only beyond float precision in
an unmapped field would be treated as the same content (mapped money fields are compared
as exact `Decimal`s; `merge_activities` compares the text exactly). Not changed (ledger
code). (b) Nesting deep enough to exhaust Python recursion (~1000 levels) raises
`RecursionError` (exit 1), as the decoder and the old `json.dumps` also would.
(c) Other broker payloads (account, orders, positions, clock, calendar) are mapped to
typed models and never re-serialized; the evidence writer's redaction is §12.29 (15B.5C).

**5. Tests.** `tests/unit/test_activity_raw_json.py` — byte identity with the previous
encoding (Hypothesis), tuples, exact round trip and canonical fixed point through
`decode_json` (Hypothesis), exact Decimal literals, non-finite values (top-level, in
lists, nested; float and Decimal; from the production decoder), programming defects
remaining `TypeError`/exit 1, unmapped numbers kept exactly, numeric money fields
rejected (non-trade and FILL), string money exact, description rule unchanged, direct and
wrapped exit 10. `tests/integration/test_activity_raw_json_paths.py` — collector
(valid numbers, malformed first and later pages, genuine timeout), runtime (no halt for
valid numbers, readable by SQLite `json_extract`; payload halt 10 then 4 with outage
streak, coverage and booked rows unchanged), existing halt → 4, reconcile exit 10 and not
CLEAN, `init-ledger` exit 10 with no ledger / initializes with valid numbers, FEE twins
with `""` and stored rows byte-identical to the old encoding, C7 VERIFIED / FAILED /
INCONCLUSIVE ×2.

### 12.29 Live contract evidence sanitization (15B.5C)

Offline implementation, confined to the contract harness (`tests/contract/supervisor.py`,
`tests/contract/live_run.py`). **No `src/` change:** the log redactor
(`observability/logging.py`), broker transport and retries, activity mapping, the
settlement calendar, ledger, reconciliation, OMS, gates and risk are unchanged. C1–C13
criteria and outcome classification are unchanged.

**1. Defect (live run #3).** `BudgetedSession.get` passed every raw body through
`SecretRedactor.redact`, whose `_KEY_VALUE` regex (built for log lines) masks the token
after any credential-looking key. On JSON that turned an unquoted value into bare text:
`"next_page_token": null` → `"next_page_token": ***REDACTED***` (likewise numbers,
booleans and the first token of arrays/objects) — persisted evidence was invalid JSON.
`forbidden_values()` then parsed those **already-redacted** bodies, `continue`d on any
`ValueError`, and inspected only top-level keys of each row: identifiers and balances
from a corrupted or nested body were silently never checked against `summary.json`. In
addition, raw files kept the account `id` and `account_number`, and a JSON-escaped secret
(`\u0050…`) would not have matched the text redactor.

**2. Pipeline (after).** HTTP response → `BudgetedSession` keeps the **original** text
in `Exchange.body` (in memory only; `repr=False`; never logged) → after the run,
`forbidden_values()` and `identity_values()` read the original bodies →
`EvidenceWriter.write` checks the summary, sanitizes and verifies **every** raw body, and
only then creates the directory and writes (0700/0600, `O_EXCL | O_NOFOLLOW`,
Git-ignored root). Any `SanitizationError` leaves nothing on disk.

**3. Policy** (`supervisor.py`):

* *Parsing:* `json.loads(parse_float=Decimal)`; `NaN`/`Infinity` rejected; nesting deeper
  than `MAX_EVIDENCE_DEPTH` (64) refused. A 2xx body is expected to be JSON; a non-2xx
  body that is not JSON is treated as text.
* *Identity values* (never persisted anywhere): the loaded credentials, every
  `account_number`/`account_id` value at any depth, and the top-level `id` of
  `/v2/account`.
* *JSON bodies:* keys matching `CREDENTIAL_KEY` (`api_key`, `secret`, `token`, `password`,
  `authorization`, `credential`, the APCA headers) or `ACCOUNT_ID_KEYS` hide their string
  and number values — recursively for nested objects/arrays — as `"***REDACTED***"`;
  `null` and booleans are kept (`"next_page_token":null` stays valid). Every other string
  (and every key) has identity values replaced exactly. Everything else — `null`,
  booleans, integers, exact `Decimal`s, arrays, objects — is preserved. Output is
  canonical (`canonical_json`: sorted keys, compact separators, `str(Decimal)`; byte-equal
  to `json.dumps(sort_keys=True, separators=(",", ":"))` for float-free JSON); no
  `default=str`, unsupported types fail.
* *Non-JSON bodies:* exact replacement of identity values only — no regex.
* *Verification:* after sanitization neither the body nor the persisted record may
  contain any identity value, raw or JSON-escaped.
* *Summary:* `forbidden_values()` = identity values + every value (string, or integer /
  `Decimal` as text, ≥ 4 characters) of a `SENSITIVE_KEYS` key at **any depth** of the
  original bodies; `assert_sanitized` refuses a summary containing one.
* *Unchanged:* the raw file layout `{"body": <text>, "status": <int>}`; balances,
  positions and activity/order ids remain in private raw evidence (they are only
  forbidden in the summary).

**4. Failure behavior.** `SanitizationError` carries a fixed code
(`EVIDENCE_JSON_MALFORMED`, `EVIDENCE_TOO_DEEP`, `EVIDENCE_UNSUPPORTED_TYPE`,
`EVIDENCE_NON_FINITE_NUMBER`, `EVIDENCE_KEY_COLLISION`, `EVIDENCE_FORBIDDEN_VALUE`,
`SUMMARY_UNSERIALIZABLE`, `SUMMARY_FORBIDDEN_VALUE`); its message is the code (no content,
value or index; causes suppressed). `run_supervised` then returns only
`{"execution": {"status": "STOPPED", "stop_code": "SUMMARY_SANITIZATION_FAILED"},
"evidence": {"written": false, "code": <code>}}` — no rows, so nothing can be VERIFIED;
the pytest verdict and `main()` fail. Before, only a summary leak was caught
(`AssertionError`); malformed bodies passed silently.

**5. Remaining limitations.** (a) Non-JSON text gets exact replacement only: an unknown
credential-like value in HTML/plain text (`token=…` that is not a loaded secret or
account identifier) is not masked; such bodies are rare (proxies, 5xx pages) and stay in
private, Git-ignored evidence. (b) A malformed 2xx body (e.g. truncated) now stops the
whole run's evidence as `SUMMARY_SANITIZATION_FAILED` instead of just failing its check —
deliberately fail-closed. (c) Summary matching ignores values shorter than 4 characters
(unchanged), and a short or common sensitive value can cause a false-positive
`SUMMARY_FORBIDDEN_VALUE` (fails closed). (d) Raw evidence still holds balances,
positions and activity/order ids by design: never share it. (e) Credential-like key
matching is by name; a credential under an innocuous key is masked only if it is a known
loaded secret.

**6. Tests.** `tests/contract/test_evidence_sanitization.py`: the original regex defect
(documented), `next_page_token: null`, every JSON type incl. exact decimals, determinism
and canonical form (Hypothesis), embedded secrets anywhere stay leak-free and valid
(Hypothesis), credential keys (strings/numbers hidden, `null`/booleans kept, nested),
account identifiers at any depth and inside other strings, secrets in values / keys /
JSON-escaped form, exact-only non-JSON replacement, structurally sanitized error bodies,
malformed expected JSON (incl. `NaN`, `Infinity`, unbalanced, very deep) → fixed code,
excessive nesting, nested and numeric sensitive values collected, unsupported types
never coerced, an identifier surviving as a number detected after sanitization, key
collision, nothing written on failure, original body in memory only; offline supervised
runs: valid leak-free raw evidence with unchanged rows, echoed error bodies scrubbed,
malformed evidence → `SUMMARY_SANITIZATION_FAILED` with nothing persisted and nothing
VERIFIED, a leaking summary → `SUMMARY_FORBIDDEN_VALUE`, permissions 0700/0600, the
evidence root Git-ignored and untracked.

---

## 13. Configuration & secrets
* `Settings`: credentials as `SecretStr`; `TRADING_MODE: Literal["paper"]`;
  `TRADING_ENABLED: bool = False`; `INITIAL_CAPITAL = 5000`;
  `MAX_CAPITAL_ALLOCATION = 5000`. After `init-ledger` the ledger's stored capital values
  are authoritative. *As built:* credentials come only from an explicitly selected source
  (environment **or** `STB_ENV_FILE`; §12.22); the CLI does not read `TRADING_ENABLED` or
  capital settings in observe mode (capital uses the D8 constant), and env-drift
  reporting is not implemented.
* `config/risk.yaml` (limits in §11), `config/operating_policy.yaml` (§6.3).
* Logging filter redacts loaded secret values and `*KEY*`/`*SECRET*` fields; tests
  assert no secret appears in captured logs.

---

## 14. Out of scope for M1
Strategy/signals, scheduler, websocket stream, Alpaca execution adapter, TradePlan /
RTH-01 implementation, protective stops and exits, cancel-on-halt, corporate actions
(splits, mergers — they surface as reconciliation mismatches and halt), notifications,
dashboards. Fees attributable to bot orders are booked in M1 (tx C). Cash dividends are
**not** booked: every DIV is preserved as UNEXPLAINED until entitlement can be verified
(§12.19).
