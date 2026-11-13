# External references

External material consulted during design and review. Everything listed here is
**reference only**: it informs reviews and test design, and is never installed, loaded as
an agent skill, executed, or treated as a requirement of this project.

---

## 1. Alpaca Skills (`alpacahq/alpaca-skills`)

| Item | Value |
|---|---|
| Repository | <https://github.com/alpacahq/alpaca-skills> |
| Reviewed commit | `alpacahq/alpaca-skills@0e7c35163ae4` (committed 2026-11-06) |
| Reviewed on | 2026-11-12 (read-only file review; nothing installed or executed) |
| License | Apache-2.0 |
| Decision | **Do not install any skill.** Selected skills are pinned, read-only references. |

### 1.1 Status of this decision

* No Alpaca skill is installed in `.claude/skills/`, in a user-level skills directory, or
  through the Skills CLI (`npx skills add …`). The Alpaca CLI and the Alpaca MCP server are
  not installed for this project.
* The pin above is the only reviewed version. A later commit is unreviewed; citing it
  requires a new review recorded in this file.
* Claims taken from these skills are **hypotheses to verify** (contract tests, SDK source
  inspection), not facts. Verified behavior is recorded in ARCHITECTURE §3 and §12 as
  usual.

### 1.2 Reference areas

| Area | Skill (path at the pinned commit) | Used for | Notes |
|---|---|---|---|
| Trading API paper trading | `alpaca-trading-paper-trading` (`skills/trading-api/paper-trading/`) | M2 pre-execution contract-test design: account restriction fields, error semantics on `POST /v2/orders`, order-status polling cadence, asset-class order rules | Its agent-driven order workflow (preview → submit through SDK/REST, confirmation-off auto-submit, cron/cloud/webhook deployment guidance) is **not** applicable: orders exist only through the OMS. It sizes from broker buying power; we size from the virtual ledger. It names credentials `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`; this project uses `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` via explicit credential selection (RUNBOOK §2). |
| Reconciliation and idempotency | `alpaca-broker-reconciliation-idempotency` (`skills/broker-api/reconciliation-idempotency/`) | Cross-check of our intent-before-send, client-order-id lookup, at-most-once ingestion and windowed re-scan design | Broker API endpoints, not Trading API. Two rules **conflict** with ours and are rejected: "Alpaca is the source of truth and the DB is a cache that converges" (our ledger is the source of truth for what the bot may do, ARCHITECTURE §1) and "unknown statuses: log and skip, never crash" (we raise a persistent halt, A§3 V6). |
| Rate limits and resilience | `alpaca-broker-rate-limits-resilience` (`skills/broker-api/rate-limits-resilience/`) | Read-path resilience review: `X-RateLimit-*` headers, backoff, timeouts, pagination | Its generic retry loop retries 5xx/504 up to 10 times without distinguishing reads from writes. That is **never** applied to order submission: a submit timeout/5xx/504 is ambiguous and is resolved only by the recovery protocol (ARCHITECTURE §10.3, §10.5; A§3 V2). Header parsing and jitter are not adopted (deferred; would need a separate reviewed change). |
| Money and numeric precision | `alpaca-broker-money-precision` (`skills/broker-api/money-precision/`) | M2 order-building precision rules (`qty` up to 9 dp, `notional` up to 2 dp, explicit rounding direction) | Already exceeded for parsing and storage: Decimal everywhere, TEXT columns, float → halt (A§3 V4, A§6.1). Field limits are Broker API documentation and must be verified for the Trading API in M2. |
| Backtesting | `alpaca-trading-backtest` (`skills/trading-api/backtest/`) | Strategy-development guardrails: next-bar fill timing, no look-ahead, no mixing adjusted and raw bars, sample-std Sharpe, run artifacts with data fingerprints and documented assumptions | Not used as written: it requires the Alpaca CLI (`go install …@latest`, unpinned) and broad local permissions, forbids direct HTTP, defaults to the paid `sip` feed (we use `iex`), and generates a stand-alone `run.py` that would duplicate our fee, calendar and settlement logic. See §1.5. |

Skills reviewed and **not** used as references: `paper-trading-cli` and `paper-trading-mcp`
(same order workflow through an external CLI profile or MCP server, outside the OMS and
outside our credential handling), and the Broker-API-only skills (integration, account
onboarding, funding transfers, journals, SSE events, trading orders, market data). The
last two may be consulted for individual field facts, verified against the Trading API.

### 1.3 Authority boundary (binding)

External Alpaca Skills — and any other external agent guidance — **never override** this
project's:

1. **OMS.** Every broker order is created, submitted, recovered and terminated only by the
   `OrderManager` through a reviewed `BrokerExecutionPort`. No agent, script or tool places,
   replaces or cancels an order at Alpaca directly (SDK, REST, CLI, MCP or otherwise).
2. **Virtual ledger.** Sizing, affordability and risk use the virtual ledger only (A§1
   principle 2). Broker equity and buying power are never used for sizing.
3. **Risk gates.** Startup gate, transient blocks and every pre-trade check (A§7, A§11)
   apply to every order; no "confirmation mode", preview or override bypasses them.
4. **Persistent halts.** Halts are cleared only by `cli resume --reason` after a clean
   reconciliation (A§7). Unknown statuses, schema surprises and unexplained activity halt;
   they are never logged and skipped.
5. **Credential handling.** Credentials are selected explicitly (RUNBOOK §2), held as
   `SecretStr`, and never logged, printed or stored elsewhere (A§1 principle 7, A§13). No
   CLI profile, MCP configuration or alternative environment-variable name is a credential
   source for this project.
6. **Broker-write authorization.** M1 has no Alpaca write path (A§5.2). Enabling any
   broker write requires the M2 entry criteria (M1_PLAN §9), a separately reviewed
   execution adapter, and explicit operator approval. Write retries remain owned by the
   recovery protocol (A§10.3); SDK or generic retry loops are never used on writes.

Where an external reference disagrees with this project's documents, this project's
documents win; the disagreement is recorded here or as an M2 verification item.

### 1.4 Agent-tooling rule

Do not install Alpaca Skills (or the Alpaca CLI / MCP server) into this repository or the
development environment used for it. Agent skills activate from their descriptions, and
the paper-trading skills would steer an assistant to submit orders outside the OMS.

### 1.5 Future project-specific backtesting skill

A project-specific backtesting skill may be written later for strategy development. If it
is, it should reuse this project's existing modules — money and valuation
(`domain/money.py`, `domain/valuation.py`), fees (`domain/fees.py`, `config/fees.yaml`),
trading and settlement calendar (`domain/calendar.py`, `ledger/calendar_store.py`) and the
settlement operating policy (`config/operating_policy.py`) — so backtests share the bot's
accounting rules, and it may adopt
the guardrails and artifact conventions listed in §1.2 (Backtesting). It must be read-only
with respect to the broker, use the configured market-data feed, and be reviewed like any
other change.
