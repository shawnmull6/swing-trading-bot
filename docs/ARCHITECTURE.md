# Swing Trading Bot — Architecture

Status: **Revision 3 — architecture approved; all decisions (D1–D15, U1–U9) final.
M1 implementation in progress (steps 1–3).** Decision log: `docs/M1_PLAN.md` §2.

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

---

## 2. Repository state (inspected 2026-11-09)

| Item | State |
|---|---|
| Tooling | Python 3.12, `uv`, `uv_build`, src layout |
| Runtime deps | `alpaca-py 0.45.0`, numpy, pandas, pydantic 2, python-dotenv, pyyaml |
| Dev deps | pytest, pytest-cov, mypy, ruff (+ `hypothesis` approved, D13) |
| Package | `src/swing_trading_bot/__init__.py` — hello-world `main()` |
| Scripts | `scripts/check_connection.py` — read-only account check |
| Config | `.env` (gitignored): `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TRADING_MODE`, `TRADING_ENABLED`, `INITIAL_CAPITAL`, `MAX_CAPITAL_ALLOCATION` |
| Tests / config / data | empty `tests/unit`, `tests/integration`, `config/`, `data/` (gitignored) |
| Operator item | `alpaca.txt` at repo root (gitignored, not read) — operator cleaning up (D15) |

---

## 3. Verified Alpaca / alpaca-py behavior (source inspection of alpaca-py 0.45.0)

These facts come from reading the installed SDK, not from live calls. Items marked
**CT** still require a contract test against the paper API (M1_PLAN §6.3).

| # | Finding | Design consequence |
|---|---|---|
| V1 | `TradingClient(paper=True)` hard-selects `https://paper-api.alpaca.markets`; base URL changes only via `url_override`; no env-var override in `common/` or `trading/`. | Adapter never passes `url_override`; asserts the resolved base URL equals the paper URL at construction. |
| V2 | **The SDK silently retries any request — including `POST /orders` — on HTTP 429 and 504**, 3 attempts, 3 s apart. `TradingClient.__init__` does not expose retry settings, and `RESTClient` ignores `retry_attempts=0` / `retry_exception_codes=[]` (falsy checks). | Our resilience layer must account for SDK retries on reads. Before any write is ever enabled (M2), the write path must disable SDK retries (subclass/override `_retry_codes`) so submission retries are owned by our recovery protocol. A 504 on submit must be treated as **ambiguous**, never as failure. |
| V3 | `TradingClient` 0.45 has **no account-activities method**. Activity models (`TradeActivity`, `NonTradeActivity`) type `price`, `qty`, `net_amount` as **float**. | Adapter calls `GET /v2/account/activities` through the client's low-level `get()` and parses the raw JSON strings directly into `Decimal`. SDK activity models are not used. |
| V4 | `Order.qty`, `filled_qty`, `filled_avg_price`, `limit_price` are typed `Union[str, float, None]`; `TradeAccount` and `Position` money fields are `str`. | Adapter parses from raw/str values into `Decimal`; a value arriving as `float` is a mapping error → halt. **CT**: confirm values arrive as strings. |
| V5 | `get_order_by_client_id` → `GET /v2/orders:by_client_order_id`. | Primary lookup for ambiguous orders. **CT**: exact error type/status for an unknown ID (expected 404) and visibility latency for a just-submitted order. |
| V6 | Order statuses in SDK: `new, partially_filled, filled, done_for_day, canceled, expired, replaced, pending_cancel, pending_replace, pending_review (deprecated), accepted, pending_new, accepted_for_bidding, stopped, rejected, suspended, calculated, held`. | Exhaustive explicit mapping in `domain/broker_status.py`; an unmapped status → persistent halt. A unit test enumerates the SDK enum so a new SDK value fails the build. |
| V10 | `Position` has `current_price` but **no timestamp**. Market-data `Trade` (latest trade) has `timestamp`, but types `price` as float. | `Position.current_price` is never a mark (U7); used only as a cross-check. Marks come from the latest-trade / latest-bar endpoints parsed raw into `Decimal` with their exchange timestamp (§6.6). **CT**: feed (IEX vs SIP) available to this account and timestamp semantics. |
| V7 | `get_calendar(GetCalendarRequest(date_type=SETTLEMENT|TRADING))`; `Calendar` has `settlement_date`; docstring says data covers 1970–2029. | Settlement dates come from Alpaca's calendar, cached locally. **CT**: paper returns `settlement_date` populated; behavior on bank holidays when markets are open (e.g. Veterans Day). Missing calendar data → proceeds stay unsettled (fail closed). Calendar horizon (2029) noted as a future risk. |
| V8 | `AccountConfiguration` exposes `no_shorting`, `max_margin_multiplier` ("1"–"4"), `pdt_check`, `dtbp_check`, `suspend_trade`, `fractional_trading`. `TradeAccount` exposes `cash`, `non_marginable_buying_power`, `multiplier`, `pattern_day_trader`, `daytrade_count`, `trading_blocked`, `account_blocked`, `accrued_fees`, `pending_reg_taf_fees`. | Startup preflight reads (never writes) these and reports them (U6: report-only in M1; `no_shorting=true` and `max_margin_multiplier="1"` become **required** before M2 paper execution); `trading_blocked`/`account_blocked` → block. `pending_reg_taf_fees`/`accrued_fees` feed cash explainability (§8.3). **CT**: whether paper simulates regulatory fees at all. |
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
  oms/         order_manager.py, recovery.py, sync.py, reconciler.py
  observability/ logging.py (JSON + secret redaction)
  app/         runtime.py (lock, startup sequence), cli.py
```

Dependency direction: `app → oms → risk → ledger → domain`; `oms → broker → domain`.
`broker` never imports `ledger`. Future strategy / TradePlan code depends on `oms`
only.

---

## 5. Broker ports and execution modes

### 5.1 Two ports instead of one

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
1. Exclusive file lock `data/bot.lock`; a second instance exits.
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
| Read timeout / 429 / 5xx | bounded backoff with jitter, aware of the SDK's own 3×3 s retries (V2) |
| Repeated failures → circuit open | transient block; escalate to persistent halt after a configurable outage duration |
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
---

## 13. Configuration & secrets
* `Settings`: credentials as `SecretStr`; `TRADING_MODE: Literal["paper"]`;
  `TRADING_ENABLED: bool = False`; `INITIAL_CAPITAL = 5000`;
  `MAX_CAPITAL_ALLOCATION = 5000`. Env values are read only by `init-ledger`; afterwards
  the ledger's stored values are authoritative and drift is reported.
* `config/risk.yaml` (limits in §11), `config/operating_policy.yaml` (§6.3).
* Logging filter redacts loaded secret values and `*KEY*`/`*SECRET*` fields; tests
  assert no secret appears in captured logs.

---

## 14. Out of scope for M1
Strategy/signals, scheduler, websocket stream, Alpaca execution adapter, TradePlan /
RTH-01 implementation, protective stops and exits, cancel-on-halt, corporate actions
(splits, mergers — they surface as reconciliation mismatches and halt), notifications,
dashboards. Fees and cash dividends on ledger positions *are* booked in M1 (tx C).
