-- Migration 0001: complete M1 schema (ARCHITECTURE.md Revision 3, §12.2).
--
-- Conventions
--   * Every table is STRICT.
--   * Money, prices and quantities are canonical decimal TEXT ('-123.4500'); never REAL.
--     CHECK constraints reject exponents, NaN/Infinity, blanks and non-text values.
--   * Timestamps are UTC ISO-8601 with microseconds ('2026-11-09T15:30:00.000000+00:00');
--     dates are 'YYYY-MM-DD'.
--   * No DEFAULT on any safety-critical column: writers must state every value.
--   * No CHECK constraints on balances or quantity signs: confirmed broker events must
--     always be recordable; accounting invariants are checked after commit (§6.5).
--   * Audit tables are append-only, enforced by triggers.
--   * schema_migrations is created by the migration runner, not here.

CREATE TABLE database_identity (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  application TEXT NOT NULL CHECK (application = 'swing-trading-bot'),
  mode        TEXT NOT NULL CHECK (mode IN ('observe', 'simulate', 'paper_execute')),
  created_at  TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;

CREATE TRIGGER database_identity_no_update BEFORE UPDATE ON database_identity
BEGIN SELECT RAISE(ABORT, 'database_identity is immutable'); END;

CREATE TRIGGER database_identity_no_delete BEFORE DELETE ON database_identity
BEGIN SELECT RAISE(ABORT, 'database_identity rows cannot be deleted'); END;

CREATE TABLE portfolios (
  id                     INTEGER PRIMARY KEY,
  name                   TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
  mode                   TEXT NOT NULL CHECK (mode IN ('observe', 'simulate', 'paper_execute')),
  initial_capital        TEXT NOT NULL CHECK (typeof(initial_capital) = 'text' AND initial_capital NOT GLOB '*[^0-9.-]*' AND initial_capital GLOB '*[0-9]*' AND initial_capital NOT GLOB '*.*.*' AND initial_capital NOT GLOB '?*-*' AND initial_capital NOT GLOB '*.' AND initial_capital NOT GLOB '.*' AND initial_capital NOT GLOB '-.*'),
  max_capital_allocation TEXT NOT NULL CHECK (typeof(max_capital_allocation) = 'text' AND max_capital_allocation NOT GLOB '*[^0-9.-]*' AND max_capital_allocation GLOB '*[0-9]*' AND max_capital_allocation NOT GLOB '*.*.*' AND max_capital_allocation NOT GLOB '?*-*' AND max_capital_allocation NOT GLOB '*.' AND max_capital_allocation NOT GLOB '.*' AND max_capital_allocation NOT GLOB '-.*'),
  account_profile        TEXT NOT NULL CHECK (length(trim(account_profile)) > 0),
  created_at             TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;

CREATE TRIGGER portfolios_mode_matches_database BEFORE INSERT ON portfolios
WHEN NEW.mode IS NOT (SELECT mode FROM database_identity WHERE id = 1)
BEGIN SELECT RAISE(ABORT, 'portfolio mode does not match database mode'); END;

CREATE TRIGGER portfolios_no_update BEFORE UPDATE ON portfolios
BEGIN SELECT RAISE(ABORT, 'portfolios is immutable'); END;

CREATE TRIGGER portfolios_no_delete BEFORE DELETE ON portfolios
BEGIN SELECT RAISE(ABORT, 'portfolios rows cannot be deleted'); END;

CREATE TABLE rebaseline_requests (
  id                           INTEGER PRIMARY KEY,
  created_at                   TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  reason                       TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  actor                        TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  diff_json                    TEXT NOT NULL CHECK (json_valid(diff_json)),
  broker_snapshot_hash         TEXT NOT NULL CHECK (length(trim(broker_snapshot_hash)) > 0),
  unexplained_cash             TEXT NOT NULL CHECK (typeof(unexplained_cash) = 'text' AND unexplained_cash NOT GLOB '*[^0-9.-]*' AND unexplained_cash GLOB '*[0-9]*' AND unexplained_cash NOT GLOB '*.*.*' AND unexplained_cash NOT GLOB '?*-*' AND unexplained_cash NOT GLOB '*.' AND unexplained_cash NOT GLOB '.*' AND unexplained_cash NOT GLOB '-.*'),
  unclassified_activities_json TEXT NOT NULL CHECK (json_valid(unclassified_activities_json)),
  status                       TEXT NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'EXPIRED')),
  acknowledged_amount          TEXT CHECK (acknowledged_amount IS NULL OR (typeof(acknowledged_amount) = 'text' AND acknowledged_amount NOT GLOB '*[^0-9.-]*' AND acknowledged_amount GLOB '*[0-9]*' AND acknowledged_amount NOT GLOB '*.*.*' AND acknowledged_amount NOT GLOB '?*-*' AND acknowledged_amount NOT GLOB '*.' AND acknowledged_amount NOT GLOB '.*' AND acknowledged_amount NOT GLOB '-.*')),
  approved_at                  TEXT CHECK (approved_at IS NULL OR approved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  approval_reason              TEXT
) STRICT;

CREATE TRIGGER rebaseline_requests_immutable BEFORE UPDATE OF id, created_at, reason, actor, diff_json, broker_snapshot_hash, unexplained_cash, unclassified_activities_json ON rebaseline_requests
BEGIN SELECT RAISE(ABORT, 'rebaseline_requests: column is immutable'); END;

CREATE TRIGGER rebaseline_requests_final BEFORE UPDATE ON rebaseline_requests
WHEN OLD.status <> 'PROPOSED'
BEGIN SELECT RAISE(ABORT, 'rebaseline_requests: decided requests are final'); END;

CREATE TRIGGER rebaseline_requests_no_delete BEFORE DELETE ON rebaseline_requests
BEGIN SELECT RAISE(ABORT, 'rebaseline_requests rows cannot be deleted'); END;

CREATE TABLE broker_baselines (
  id                    INTEGER PRIMARY KEY,
  portfolio_id          INTEGER NOT NULL REFERENCES portfolios(id),
  taken_at              TEXT NOT NULL CHECK (taken_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  cash                  TEXT NOT NULL CHECK (typeof(cash) = 'text' AND cash NOT GLOB '*[^0-9.-]*' AND cash GLOB '*[0-9]*' AND cash NOT GLOB '*.*.*' AND cash NOT GLOB '?*-*' AND cash NOT GLOB '*.' AND cash NOT GLOB '.*' AND cash NOT GLOB '-.*'),
  equity                TEXT NOT NULL CHECK (typeof(equity) = 'text' AND equity NOT GLOB '*[^0-9.-]*' AND equity GLOB '*[0-9]*' AND equity NOT GLOB '*.*.*' AND equity NOT GLOB '?*-*' AND equity NOT GLOB '*.' AND equity NOT GLOB '.*' AND equity NOT GLOB '-.*'),
  activity_cursor_id    TEXT,
  account_config_json   TEXT NOT NULL CHECK (json_valid(account_config_json)),
  reason                TEXT NOT NULL CHECK (reason IN ('INIT', 'REBASELINE')),
  rebaseline_request_id INTEGER REFERENCES rebaseline_requests(id),
  superseded_at         TEXT CHECK (superseded_at IS NULL OR superseded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  CHECK ((reason = 'INIT') = (rebaseline_request_id IS NULL))
) STRICT;
CREATE UNIQUE INDEX ux_broker_baselines_active ON broker_baselines(portfolio_id)
  WHERE superseded_at IS NULL;

CREATE TRIGGER broker_baselines_immutable BEFORE UPDATE OF id, portfolio_id, taken_at, cash, equity, activity_cursor_id, account_config_json, reason, rebaseline_request_id ON broker_baselines
BEGIN SELECT RAISE(ABORT, 'broker_baselines: column is immutable'); END;

CREATE TRIGGER broker_baselines_supersede_once BEFORE UPDATE OF superseded_at ON broker_baselines
WHEN OLD.superseded_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'broker_baselines: already superseded'); END;

CREATE TRIGGER broker_baselines_no_delete BEFORE DELETE ON broker_baselines
BEGIN SELECT RAISE(ABORT, 'broker_baselines rows cannot be deleted'); END;

CREATE TABLE ledger_adjustments (
  id         INTEGER PRIMARY KEY,
  ts         TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  actor      TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  kind       TEXT NOT NULL CHECK (kind IN ('CASH', 'POSITION')),
  symbol     TEXT,
  qty_delta  TEXT CHECK (qty_delta IS NULL OR (typeof(qty_delta) = 'text' AND qty_delta NOT GLOB '*[^0-9.-]*' AND qty_delta GLOB '*[0-9]*' AND qty_delta NOT GLOB '*.*.*' AND qty_delta NOT GLOB '?*-*' AND qty_delta NOT GLOB '*.' AND qty_delta NOT GLOB '.*' AND qty_delta NOT GLOB '-.*')),
  cash_delta TEXT CHECK (cash_delta IS NULL OR (typeof(cash_delta) = 'text' AND cash_delta NOT GLOB '*[^0-9.-]*' AND cash_delta GLOB '*[0-9]*' AND cash_delta NOT GLOB '*.*.*' AND cash_delta NOT GLOB '?*-*' AND cash_delta NOT GLOB '*.' AND cash_delta NOT GLOB '.*' AND cash_delta NOT GLOB '-.*')),
  price      TEXT CHECK (price IS NULL OR (typeof(price) = 'text' AND price NOT GLOB '*[^0-9.-]*' AND price GLOB '*[0-9]*' AND price NOT GLOB '*.*.*' AND price NOT GLOB '?*-*' AND price NOT GLOB '*.' AND price NOT GLOB '.*' AND price NOT GLOB '-.*')),
  reason     TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  evidence   TEXT NOT NULL CHECK (length(trim(evidence)) > 0),
  CHECK (kind <> 'POSITION' OR (symbol IS NOT NULL AND qty_delta IS NOT NULL)),
  CHECK (kind <> 'CASH' OR cash_delta IS NOT NULL)
) STRICT;

CREATE TRIGGER ledger_adjustments_no_update BEFORE UPDATE ON ledger_adjustments
BEGIN SELECT RAISE(ABORT, 'ledger_adjustments is append-only'); END;

CREATE TRIGGER ledger_adjustments_no_delete BEFORE DELETE ON ledger_adjustments
BEGIN SELECT RAISE(ABORT, 'ledger_adjustments rows cannot be deleted'); END;

CREATE TABLE orders (
  client_order_id    TEXT PRIMARY KEY CHECK (length(trim(client_order_id)) > 0),
  broker_order_id    TEXT UNIQUE,
  portfolio_id       INTEGER NOT NULL REFERENCES portfolios(id),
  intent_key         TEXT NOT NULL UNIQUE CHECK (length(trim(intent_key)) > 0),
  symbol             TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
  side               TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
  order_type         TEXT NOT NULL CHECK (order_type IN ('LIMIT', 'MARKET', 'STOP', 'STOP_LIMIT')),
  time_in_force      TEXT NOT NULL CHECK (time_in_force IN ('DAY', 'GTC')),
  purpose            TEXT NOT NULL CHECK (purpose IN ('ENTRY', 'DISCRETIONARY_EXIT', 'RISK_EXIT')),
  qty                TEXT NOT NULL CHECK (typeof(qty) = 'text' AND qty NOT GLOB '*[^0-9.-]*' AND qty GLOB '*[0-9]*' AND qty NOT GLOB '*.*.*' AND qty NOT GLOB '?*-*' AND qty NOT GLOB '*.' AND qty NOT GLOB '.*' AND qty NOT GLOB '-.*'),
  limit_price        TEXT CHECK (limit_price IS NULL OR (typeof(limit_price) = 'text' AND limit_price NOT GLOB '*[^0-9.-]*' AND limit_price GLOB '*[0-9]*' AND limit_price NOT GLOB '*.*.*' AND limit_price NOT GLOB '?*-*' AND limit_price NOT GLOB '*.' AND limit_price NOT GLOB '.*' AND limit_price NOT GLOB '-.*')),
  planned_stop       TEXT CHECK (planned_stop IS NULL OR (typeof(planned_stop) = 'text' AND planned_stop NOT GLOB '*[^0-9.-]*' AND planned_stop GLOB '*[0-9]*' AND planned_stop NOT GLOB '*.*.*' AND planned_stop NOT GLOB '?*-*' AND planned_stop NOT GLOB '*.' AND planned_stop NOT GLOB '.*' AND planned_stop NOT GLOB '-.*')),
  status             TEXT NOT NULL CHECK (status IN ('PENDING_SUBMIT', 'SUBMIT_AMBIGUOUS', 'ACCEPTED', 'PARTIALLY_FILLED', 'PENDING_CANCEL', 'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'CONFIRMED_ABSENT', 'ORPHAN')),
  filled_qty         TEXT NOT NULL CHECK (typeof(filled_qty) = 'text' AND filled_qty NOT GLOB '*[^0-9.-]*' AND filled_qty GLOB '*[0-9]*' AND filled_qty NOT GLOB '*.*.*' AND filled_qty NOT GLOB '?*-*' AND filled_qty NOT GLOB '*.' AND filled_qty NOT GLOB '.*' AND filled_qty NOT GLOB '-.*'),
  avg_fill_price     TEXT CHECK (avg_fill_price IS NULL OR (typeof(avg_fill_price) = 'text' AND avg_fill_price NOT GLOB '*[^0-9.-]*' AND avg_fill_price GLOB '*[0-9]*' AND avg_fill_price NOT GLOB '*.*.*' AND avg_fill_price NOT GLOB '?*-*' AND avg_fill_price NOT GLOB '*.' AND avg_fill_price NOT GLOB '.*' AND avg_fill_price NOT GLOB '-.*')),
  last_broker_status TEXT,
  reject_reason      TEXT,
  trade_plan_id      TEXT,
  order_role         TEXT CHECK (order_role IS NULL OR order_role IN ('ENTRY', 'PROTECTIVE_STOP', 'TARGET', 'EXIT')),
  created_at         TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  send_attempted_at  TEXT CHECK (send_attempted_at IS NULL OR send_attempted_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  updated_at         TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  last_synced_at     TEXT CHECK (last_synced_at IS NULL OR last_synced_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;

CREATE INDEX ix_orders_working ON orders(portfolio_id, symbol)
  WHERE status NOT IN ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'CONFIRMED_ABSENT', 'ORPHAN');

CREATE TRIGGER orders_immutable BEFORE UPDATE OF client_order_id, portfolio_id, intent_key, symbol, side, order_type, time_in_force, purpose, qty, limit_price, planned_stop, created_at ON orders
BEGIN SELECT RAISE(ABORT, 'orders: column is immutable'); END;

CREATE TRIGGER orders_broker_id_set_once BEFORE UPDATE OF broker_order_id ON orders
WHEN OLD.broker_order_id IS NOT NULL AND NEW.broker_order_id IS NOT OLD.broker_order_id
BEGIN SELECT RAISE(ABORT, 'orders: broker_order_id cannot change once set'); END;
CREATE TRIGGER orders_send_attempt_set_once BEFORE UPDATE OF send_attempted_at ON orders
WHEN OLD.send_attempted_at IS NOT NULL AND NEW.send_attempted_at IS NOT OLD.send_attempted_at
BEGIN SELECT RAISE(ABORT, 'orders: send_attempted_at cannot change once set'); END;

CREATE TRIGGER orders_no_delete BEFORE DELETE ON orders
BEGIN SELECT RAISE(ABORT, 'orders rows cannot be deleted'); END;

CREATE TABLE order_resolution_attempts (
  id              INTEGER PRIMARY KEY,
  client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
  ts              TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  method          TEXT NOT NULL CHECK (method IN ('BY_CLIENT_ID', 'LIST_SCAN', 'ACTIVITY_SCAN', 'OPERATOR')),
  result          TEXT NOT NULL CHECK (length(trim(result)) > 0),
  detail_json     TEXT CHECK (detail_json IS NULL OR json_valid(detail_json))
) STRICT;
CREATE INDEX ix_resolution_attempts_order ON order_resolution_attempts(client_order_id, ts);

CREATE TRIGGER order_resolution_attempts_no_update BEFORE UPDATE ON order_resolution_attempts
BEGIN SELECT RAISE(ABORT, 'order_resolution_attempts is append-only'); END;

CREATE TRIGGER order_resolution_attempts_no_delete BEFORE DELETE ON order_resolution_attempts
BEGIN SELECT RAISE(ABORT, 'order_resolution_attempts rows cannot be deleted'); END;

CREATE TABLE cash_reservations (
  client_order_id TEXT PRIMARY KEY REFERENCES orders(client_order_id),
  original        TEXT NOT NULL CHECK (typeof(original) = 'text' AND original NOT GLOB '*[^0-9.-]*' AND original GLOB '*[0-9]*' AND original NOT GLOB '*.*.*' AND original NOT GLOB '?*-*' AND original NOT GLOB '*.' AND original NOT GLOB '.*' AND original NOT GLOB '-.*'),
  remaining       TEXT NOT NULL CHECK (typeof(remaining) = 'text' AND remaining NOT GLOB '*[^0-9.-]*' AND remaining GLOB '*[0-9]*' AND remaining NOT GLOB '*.*.*' AND remaining NOT GLOB '?*-*' AND remaining NOT GLOB '*.' AND remaining NOT GLOB '.*' AND remaining NOT GLOB '-.*'),
  status          TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED', 'CONSUMED')),
  updated_at      TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;

CREATE TRIGGER cash_reservations_immutable BEFORE UPDATE OF client_order_id, original ON cash_reservations
BEGIN SELECT RAISE(ABORT, 'cash_reservations: column is immutable'); END;

CREATE TRIGGER cash_reservations_final BEFORE UPDATE ON cash_reservations
WHEN OLD.status <> 'ACTIVE'
BEGIN SELECT RAISE(ABORT, 'cash_reservations: released/consumed reservations are final'); END;

CREATE TRIGGER cash_reservations_no_delete BEFORE DELETE ON cash_reservations
BEGIN SELECT RAISE(ABORT, 'cash_reservations rows cannot be deleted'); END;

CREATE TABLE broker_activities (
  activity_id     TEXT PRIMARY KEY CHECK (length(trim(activity_id)) > 0),
  activity_type   TEXT NOT NULL CHECK (length(trim(activity_type)) > 0),
  ts              TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  symbol          TEXT,
  qty             TEXT CHECK (qty IS NULL OR (typeof(qty) = 'text' AND qty NOT GLOB '*[^0-9.-]*' AND qty GLOB '*[0-9]*' AND qty NOT GLOB '*.*.*' AND qty NOT GLOB '?*-*' AND qty NOT GLOB '*.' AND qty NOT GLOB '.*' AND qty NOT GLOB '-.*')),
  price           TEXT CHECK (price IS NULL OR (typeof(price) = 'text' AND price NOT GLOB '*[^0-9.-]*' AND price GLOB '*[0-9]*' AND price NOT GLOB '*.*.*' AND price NOT GLOB '?*-*' AND price NOT GLOB '*.' AND price NOT GLOB '.*' AND price NOT GLOB '-.*')),
  net_amount      TEXT CHECK (net_amount IS NULL OR (typeof(net_amount) = 'text' AND net_amount NOT GLOB '*[^0-9.-]*' AND net_amount GLOB '*[0-9]*' AND net_amount NOT GLOB '*.*.*' AND net_amount NOT GLOB '?*-*' AND net_amount NOT GLOB '*.' AND net_amount NOT GLOB '.*' AND net_amount NOT GLOB '-.*')),
  side            TEXT CHECK (side IS NULL OR side IN ('BUY', 'SELL')),
  broker_order_id TEXT,
  client_order_id TEXT,
  attribution     TEXT NOT NULL CHECK (attribution IN ('ATTRIBUTABLE', 'EXTERNAL', 'UNEXPLAINED')),
  raw_json        TEXT NOT NULL CHECK (json_valid(raw_json)),
  ingested_at     TEXT NOT NULL CHECK (ingested_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;
CREATE INDEX ix_broker_activities_order ON broker_activities(broker_order_id);

CREATE TRIGGER broker_activities_immutable BEFORE UPDATE OF activity_id, activity_type, ts, symbol, qty, price, net_amount, side, broker_order_id, client_order_id, raw_json, ingested_at ON broker_activities
BEGIN SELECT RAISE(ABORT, 'broker_activities: column is immutable'); END;

CREATE TRIGGER broker_activities_no_delete BEFORE DELETE ON broker_activities
BEGIN SELECT RAISE(ABORT, 'broker_activities rows cannot be deleted'); END;

CREATE TABLE fills (
  id              INTEGER PRIMARY KEY,
  activity_id     TEXT NOT NULL UNIQUE REFERENCES broker_activities(activity_id),
  client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
  symbol          TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
  side            TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
  qty             TEXT NOT NULL CHECK (typeof(qty) = 'text' AND qty NOT GLOB '*[^0-9.-]*' AND qty GLOB '*[0-9]*' AND qty NOT GLOB '*.*.*' AND qty NOT GLOB '?*-*' AND qty NOT GLOB '*.' AND qty NOT GLOB '.*' AND qty NOT GLOB '-.*'),
  price           TEXT NOT NULL CHECK (typeof(price) = 'text' AND price NOT GLOB '*[^0-9.-]*' AND price GLOB '*[0-9]*' AND price NOT GLOB '*.*.*' AND price NOT GLOB '?*-*' AND price NOT GLOB '*.' AND price NOT GLOB '.*' AND price NOT GLOB '-.*'),
  fees            TEXT NOT NULL CHECK (typeof(fees) = 'text' AND fees NOT GLOB '*[^0-9.-]*' AND fees GLOB '*[0-9]*' AND fees NOT GLOB '*.*.*' AND fees NOT GLOB '?*-*' AND fees NOT GLOB '*.' AND fees NOT GLOB '.*' AND fees NOT GLOB '-.*'),
  avg_cost_before TEXT CHECK (avg_cost_before IS NULL OR (typeof(avg_cost_before) = 'text' AND avg_cost_before NOT GLOB '*[^0-9.-]*' AND avg_cost_before GLOB '*[0-9]*' AND avg_cost_before NOT GLOB '*.*.*' AND avg_cost_before NOT GLOB '?*-*' AND avg_cost_before NOT GLOB '*.' AND avg_cost_before NOT GLOB '.*' AND avg_cost_before NOT GLOB '-.*')),
  realized_pnl    TEXT CHECK (realized_pnl IS NULL OR (typeof(realized_pnl) = 'text' AND realized_pnl NOT GLOB '*[^0-9.-]*' AND realized_pnl GLOB '*[0-9]*' AND realized_pnl NOT GLOB '*.*.*' AND realized_pnl NOT GLOB '?*-*' AND realized_pnl NOT GLOB '*.' AND realized_pnl NOT GLOB '.*' AND realized_pnl NOT GLOB '-.*')),
  filled_at       TEXT NOT NULL CHECK (filled_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  recorded_at     TEXT NOT NULL CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;
CREATE INDEX ix_fills_order ON fills(client_order_id);
CREATE INDEX ix_fills_symbol ON fills(symbol, filled_at);

CREATE TRIGGER fills_no_update BEFORE UPDATE ON fills
BEGIN SELECT RAISE(ABORT, 'fills is append-only'); END;

CREATE TRIGGER fills_no_delete BEFORE DELETE ON fills
BEGIN SELECT RAISE(ABORT, 'fills rows cannot be deleted'); END;

CREATE TABLE cash_entries (
  id            INTEGER PRIMARY KEY,
  portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
  ts            TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  kind          TEXT NOT NULL CHECK (kind IN ('DEPOSIT', 'BUY', 'SELL', 'FEE', 'DIVIDEND', 'ADJUSTMENT')),
  amount        TEXT NOT NULL CHECK (typeof(amount) = 'text' AND amount NOT GLOB '*[^0-9.-]*' AND amount GLOB '*[0-9]*' AND amount NOT GLOB '*.*.*' AND amount NOT GLOB '?*-*' AND amount NOT GLOB '*.' AND amount NOT GLOB '.*' AND amount NOT GLOB '-.*'),
  trade_date    TEXT NOT NULL CHECK (trade_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  settles_on    TEXT CHECK (settles_on IS NULL OR settles_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  activity_id   TEXT REFERENCES broker_activities(activity_id),
  fill_id       INTEGER REFERENCES fills(id),
  adjustment_id INTEGER REFERENCES ledger_adjustments(id),
  memo          TEXT,
  CHECK (kind <> 'ADJUSTMENT' OR adjustment_id IS NOT NULL),
  CHECK (kind NOT IN ('BUY','SELL') OR fill_id IS NOT NULL)
) STRICT;
CREATE UNIQUE INDEX ux_cash_entries_activity_kind ON cash_entries(activity_id, kind)
  WHERE activity_id IS NOT NULL;
CREATE INDEX ix_cash_entries_settlement ON cash_entries(portfolio_id, settles_on);

CREATE TRIGGER cash_entries_immutable BEFORE UPDATE OF id, portfolio_id, ts, kind, amount, trade_date, activity_id, fill_id, adjustment_id, memo ON cash_entries
BEGIN SELECT RAISE(ABORT, 'cash_entries: column is immutable'); END;

CREATE TRIGGER cash_entries_settle_once BEFORE UPDATE OF settles_on ON cash_entries
WHEN OLD.settles_on IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'cash_entries: settles_on can only be resolved once'); END;

CREATE TRIGGER cash_entries_no_delete BEFORE DELETE ON cash_entries
BEGIN SELECT RAISE(ABORT, 'cash_entries rows cannot be deleted'); END;

CREATE TABLE positions (
  portfolio_id   INTEGER NOT NULL REFERENCES portfolios(id),
  symbol         TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
  qty            TEXT NOT NULL CHECK (typeof(qty) = 'text' AND qty NOT GLOB '*[^0-9.-]*' AND qty GLOB '*[0-9]*' AND qty NOT GLOB '*.*.*' AND qty NOT GLOB '?*-*' AND qty NOT GLOB '*.' AND qty NOT GLOB '.*' AND qty NOT GLOB '-.*'),
  cost_basis     TEXT NOT NULL CHECK (typeof(cost_basis) = 'text' AND cost_basis NOT GLOB '*[^0-9.-]*' AND cost_basis GLOB '*[0-9]*' AND cost_basis NOT GLOB '*.*.*' AND cost_basis NOT GLOB '?*-*' AND cost_basis NOT GLOB '*.' AND cost_basis NOT GLOB '.*' AND cost_basis NOT GLOB '-.*'),
  realized_pnl   TEXT NOT NULL CHECK (typeof(realized_pnl) = 'text' AND realized_pnl NOT GLOB '*[^0-9.-]*' AND realized_pnl GLOB '*[0-9]*' AND realized_pnl NOT GLOB '*.*.*' AND realized_pnl NOT GLOB '?*-*' AND realized_pnl NOT GLOB '*.' AND realized_pnl NOT GLOB '.*' AND realized_pnl NOT GLOB '-.*'),
  opened_at      TEXT CHECK (opened_at IS NULL OR opened_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  opened_session TEXT CHECK (opened_session IS NULL OR opened_session GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  updated_at     TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  PRIMARY KEY (portfolio_id, symbol)
) STRICT;

CREATE TRIGGER positions_immutable BEFORE UPDATE OF portfolio_id, symbol ON positions
BEGIN SELECT RAISE(ABORT, 'positions: column is immutable'); END;

CREATE TRIGGER positions_no_delete BEFORE DELETE ON positions
BEGIN SELECT RAISE(ABORT, 'positions rows cannot be deleted'); END;

CREATE TABLE price_marks (
  symbol      TEXT PRIMARY KEY CHECK (length(trim(symbol)) > 0),
  price       TEXT NOT NULL CHECK (typeof(price) = 'text' AND price NOT GLOB '*[^0-9.-]*' AND price GLOB '*[0-9]*' AND price NOT GLOB '*.*.*' AND price NOT GLOB '?*-*' AND price NOT GLOB '*.' AND price NOT GLOB '.*' AND price NOT GLOB '-.*'),
  as_of       TEXT NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  received_at TEXT NOT NULL CHECK (received_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  source      TEXT NOT NULL CHECK (source IN ('LATEST_TRADE', 'SESSION_CLOSE_BAR', 'SIM')),
  feed        TEXT
) STRICT;

CREATE TABLE market_calendar (
  session_date    TEXT PRIMARY KEY CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  open_at         TEXT NOT NULL CHECK (open_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  close_at        TEXT NOT NULL CHECK (close_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  settlement_date TEXT CHECK (settlement_date IS NULL OR settlement_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  fetched_at      TEXT NOT NULL CHECK (fetched_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;

CREATE TABLE equity_snapshots (
  id           INTEGER PRIMARY KEY,
  portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
  kind         TEXT NOT NULL CHECK (kind IN ('SESSION_BASELINE', 'WEEK_BASELINE', 'SESSION_CLOSE')),
  session_date TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  equity       TEXT NOT NULL CHECK (typeof(equity) = 'text' AND equity NOT GLOB '*[^0-9.-]*' AND equity GLOB '*[0-9]*' AND equity NOT GLOB '*.*.*' AND equity NOT GLOB '?*-*' AND equity NOT GLOB '*.' AND equity NOT GLOB '.*' AND equity NOT GLOB '-.*'),
  cash         TEXT NOT NULL CHECK (typeof(cash) = 'text' AND cash NOT GLOB '*[^0-9.-]*' AND cash GLOB '*[0-9]*' AND cash NOT GLOB '*.*.*' AND cash NOT GLOB '?*-*' AND cash NOT GLOB '*.' AND cash NOT GLOB '.*' AND cash NOT GLOB '-.*'),
  marks_json   TEXT NOT NULL CHECK (json_valid(marks_json)),
  taken_at     TEXT NOT NULL CHECK (taken_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  UNIQUE (portfolio_id, kind, session_date)
) STRICT;

CREATE TRIGGER equity_snapshots_no_update BEFORE UPDATE ON equity_snapshots
BEGIN SELECT RAISE(ABORT, 'equity_snapshots is append-only'); END;

CREATE TRIGGER equity_snapshots_no_delete BEFORE DELETE ON equity_snapshots
BEGIN SELECT RAISE(ABORT, 'equity_snapshots rows cannot be deleted'); END;

CREATE TABLE loss_limit_events (
  id              INTEGER PRIMARY KEY,
  ts              TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  session_date    TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  kind            TEXT NOT NULL CHECK (kind IN ('DAILY_LOCKOUT', 'DAILY_REPEAT_HALT', 'WEEKLY_HALT')),
  baseline_equity TEXT NOT NULL CHECK (typeof(baseline_equity) = 'text' AND baseline_equity NOT GLOB '*[^0-9.-]*' AND baseline_equity GLOB '*[0-9]*' AND baseline_equity NOT GLOB '*.*.*' AND baseline_equity NOT GLOB '?*-*' AND baseline_equity NOT GLOB '*.' AND baseline_equity NOT GLOB '.*' AND baseline_equity NOT GLOB '-.*'),
  equity          TEXT NOT NULL CHECK (typeof(equity) = 'text' AND equity NOT GLOB '*[^0-9.-]*' AND equity GLOB '*[0-9]*' AND equity NOT GLOB '*.*.*' AND equity NOT GLOB '?*-*' AND equity NOT GLOB '*.' AND equity NOT GLOB '.*' AND equity NOT GLOB '-.*'),
  loss_pct        TEXT NOT NULL CHECK (typeof(loss_pct) = 'text' AND loss_pct NOT GLOB '*[^0-9.-]*' AND loss_pct GLOB '*[0-9]*' AND loss_pct NOT GLOB '*.*.*' AND loss_pct NOT GLOB '?*-*' AND loss_pct NOT GLOB '*.' AND loss_pct NOT GLOB '.*' AND loss_pct NOT GLOB '-.*')
) STRICT;

CREATE TRIGGER loss_limit_events_no_update BEFORE UPDATE ON loss_limit_events
BEGIN SELECT RAISE(ABORT, 'loss_limit_events is append-only'); END;

CREATE TRIGGER loss_limit_events_no_delete BEFORE DELETE ON loss_limit_events
BEGIN SELECT RAISE(ABORT, 'loss_limit_events rows cannot be deleted'); END;

CREATE TABLE order_events (
  id              INTEGER PRIMARY KEY,
  client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
  ts              TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  from_status     TEXT CHECK (from_status IS NULL OR from_status IN ('PENDING_SUBMIT', 'SUBMIT_AMBIGUOUS', 'ACCEPTED', 'PARTIALLY_FILLED', 'PENDING_CANCEL', 'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'CONFIRMED_ABSENT', 'ORPHAN')),
  to_status       TEXT CHECK (to_status IS NULL OR to_status IN ('PENDING_SUBMIT', 'SUBMIT_AMBIGUOUS', 'ACCEPTED', 'PARTIALLY_FILLED', 'PENDING_CANCEL', 'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'CONFIRMED_ABSENT', 'ORPHAN')),
  source          TEXT NOT NULL CHECK (source IN ('LOCAL', 'BROKER', 'RECON', 'OPERATOR', 'RECOVERY')),
  detail_json     TEXT CHECK (detail_json IS NULL OR json_valid(detail_json))
) STRICT;
CREATE INDEX ix_order_events_order ON order_events(client_order_id, id);

CREATE TRIGGER order_events_no_update BEFORE UPDATE ON order_events
BEGIN SELECT RAISE(ABORT, 'order_events is append-only'); END;

CREATE TRIGGER order_events_no_delete BEFORE DELETE ON order_events
BEGIN SELECT RAISE(ABORT, 'order_events rows cannot be deleted'); END;

CREATE TABLE trading_state (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  halted        INTEGER NOT NULL CHECK (halted IN (0, 1)),
  reason_code   TEXT,
  reason_detail TEXT,
  changed_at    TEXT NOT NULL CHECK (changed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  changed_by    TEXT NOT NULL CHECK (length(trim(changed_by)) > 0),
  CHECK (halted = 0 OR reason_code IS NOT NULL)
) STRICT;

CREATE TRIGGER trading_state_no_delete BEFORE DELETE ON trading_state
BEGIN SELECT RAISE(ABORT, 'trading_state rows cannot be deleted'); END;

CREATE TABLE reconciliation_runs (
  id        INTEGER PRIMARY KEY,
  ts        TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  result    TEXT NOT NULL CHECK (result IN ('OK', 'MISMATCH', 'INCONCLUSIVE', 'ERROR')),
  diff_json TEXT CHECK (diff_json IS NULL OR json_valid(diff_json))
) STRICT;

CREATE TRIGGER reconciliation_runs_no_update BEFORE UPDATE ON reconciliation_runs
BEGIN SELECT RAISE(ABORT, 'reconciliation_runs is append-only'); END;

CREATE TRIGGER reconciliation_runs_no_delete BEFORE DELETE ON reconciliation_runs
BEGIN SELECT RAISE(ABORT, 'reconciliation_runs rows cannot be deleted'); END;

CREATE TABLE halt_events (
  id                     INTEGER PRIMARY KEY,
  ts                     TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  action                 TEXT NOT NULL CHECK (action IN ('HALT', 'RESUME', 'RESUME_REFUSED')),
  reason_code            TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
  detail                 TEXT,
  actor                  TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reconciliation_run_id  INTEGER REFERENCES reconciliation_runs(id)
) STRICT;

CREATE TRIGGER halt_events_no_update BEFORE UPDATE ON halt_events
BEGIN SELECT RAISE(ABORT, 'halt_events is append-only'); END;

CREATE TRIGGER halt_events_no_delete BEFORE DELETE ON halt_events
BEGIN SELECT RAISE(ABORT, 'halt_events rows cannot be deleted'); END;

CREATE TABLE sync_state (
  key        TEXT PRIMARY KEY CHECK (length(trim(key)) > 0),
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL CHECK (updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')
) STRICT;

CREATE TRIGGER sync_state_no_delete BEFORE DELETE ON sync_state
BEGIN SELECT RAISE(ABORT, 'sync_state rows cannot be deleted'); END;
