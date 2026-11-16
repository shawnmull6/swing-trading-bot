-- Migration 0009: durable protective stops, trailing-stop evidence and protection gaps
-- (Step 16.12, M2-3; ARCHITECTURE §12.41).
--
-- orders.stop_price
--   The stop (trigger) price of STOP / STOP_LIMIT orders, as exact Decimal text. It is
--   added with ALTER TABLE ADD COLUMN, not a table rebuild: orders is referenced by
--   foreign keys and protected by a no-delete trigger, so DROP TABLE would abort. SQLite
--   (>= 3.37) evaluates a CHECK added this way against every existing row, so any legacy
--   row that violates it aborts the whole migration (fail closed). The CHECK also carries
--   the cross-column order rules, because a column CHECK is evaluated on every INSERT and
--   UPDATE of the row:
--     * STOP and STOP_LIMIT need a stop price; LIMIT and MARKET must not have one;
--     * LIMIT and STOP_LIMIT need a limit price; STOP and MARKET must not have one;
--     * trade_plan_id and order_role are both NULL or both set;
--     * a plan ENTRY buys, and every other plan role sells.
--   Existing rows: OMS orders are LIMIT with a limit price, orphans are MARKET without
--   one, and plan columns are NULL or consistent, so history satisfies the rules.
--
-- orders_immutable
--   Recreated to also freeze stop_price, trade_plan_id and order_role.
--
-- plan_stop_levels (append-only)
--   The persisted protective-stop level of a TradePlan and the evidence behind it
--   (policy P8): initial plan stop, breakeven after the partial bucket (P6), or a
--   trailing ratchet from an observed high. A trigger refuses a row whose stop or
--   high-water mark is lower than the plan's previous row: levels only ratchet up. The
--   trigger compares exactly, without floating point: each non-negative decimal text is
--   scaled to an integer number of millionths (prices carry at most four decimals). The
--   application compares exact Decimals before inserting; the trigger is a second guard.
--   Ownership: the plan must have an ENTRY order, the evidence must be a non-empty JSON
--   object, and an evidence client_order_id must be an order of the same plan.
--
-- protection_gaps
--   Protection gaps of a plan's position (policy P7). A row opens as a POTENTIAL gap
--   when a protective stop is asked to cancel (the old stop may still be working at the
--   broker), and becomes CONFIRMED (confirmed_at) once that stop is broker-confirmed
--   terminal with every fill booked while shares remain and no acknowledged stop covers
--   them. A stop that ends unfilled without a cancel request opens already confirmed.
--   It closes once a protective stop of the same plan is acknowledged by the broker and
--   covers the shares, or the position is flat; a declined cancel closes it unconfirmed.
--   At most one open gap per plan. confirmed_at and the closing columns are each set
--   once; nothing else ever changes. The prior and closing orders must be protective
--   stops of the same plan.

ALTER TABLE orders ADD COLUMN stop_price TEXT CHECK (
  (stop_price IS NULL OR (typeof(stop_price) = 'text' AND stop_price NOT GLOB '*[^0-9.]*' AND stop_price GLOB '*[0-9]*' AND stop_price NOT GLOB '*.*.*' AND stop_price NOT GLOB '*.' AND stop_price NOT GLOB '.*'))
  AND ((order_type IN ('STOP', 'STOP_LIMIT')) = (stop_price IS NOT NULL))
  AND ((order_type IN ('LIMIT', 'STOP_LIMIT')) = (limit_price IS NOT NULL))
  AND ((trade_plan_id IS NULL) = (order_role IS NULL))
  AND (order_role IS NULL OR ((order_role = 'ENTRY') = (side = 'BUY')))
);

DROP TRIGGER orders_immutable;

CREATE TRIGGER orders_immutable BEFORE UPDATE OF client_order_id, portfolio_id, intent_key, symbol, side, order_type, time_in_force, purpose, qty, limit_price, planned_stop, created_at, stop_price, trade_plan_id, order_role ON orders
BEGIN SELECT RAISE(ABORT, 'orders: column is immutable'); END;

CREATE INDEX ix_orders_plan ON orders(trade_plan_id) WHERE trade_plan_id IS NOT NULL;

CREATE TABLE plan_stop_levels (
  id            INTEGER PRIMARY KEY,
  trade_plan_id TEXT NOT NULL CHECK (trade_plan_id GLOB 'tp-[0-9a-f]*' AND length(trade_plan_id) = 35),
  seq           INTEGER NOT NULL CHECK (seq >= 1),
  stop_price    TEXT NOT NULL CHECK (typeof(stop_price) = 'text' AND stop_price NOT GLOB '*[^0-9.]*' AND stop_price GLOB '*[0-9]*' AND stop_price NOT GLOB '*.*.*' AND stop_price NOT GLOB '*.' AND stop_price NOT GLOB '.*'),
  high_water    TEXT CHECK (high_water IS NULL OR (typeof(high_water) = 'text' AND high_water NOT GLOB '*[^0-9.]*' AND high_water GLOB '*[0-9]*' AND high_water NOT GLOB '*.*.*' AND high_water NOT GLOB '*.' AND high_water NOT GLOB '.*')),
  source        TEXT NOT NULL CHECK (source IN ('PLAN_INTENT', 'BREAKEVEN', 'TRAILING')),
  evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_type(evidence_json) = 'object' AND evidence_json <> '{}'),
  recorded_at   TEXT NOT NULL CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  UNIQUE (trade_plan_id, seq),
  CHECK (source <> 'TRAILING' OR high_water IS NOT NULL)
) STRICT;

CREATE TRIGGER plan_stop_levels_ratchet BEFORE INSERT ON plan_stop_levels
WHEN EXISTS (
  SELECT 1 FROM plan_stop_levels p
  WHERE p.trade_plan_id = NEW.trade_plan_id
    AND p.seq = (SELECT max(seq) FROM plan_stop_levels WHERE trade_plan_id = NEW.trade_plan_id)
    AND (NEW.seq <> p.seq + 1
         OR (CAST(substr(NEW.stop_price, 1, instr(NEW.stop_price || '.', '.') - 1) AS INTEGER) * 1000000 + CAST(substr(substr(NEW.stop_price, instr(NEW.stop_price || '.', '.') + 1) || '000000', 1, 6) AS INTEGER)) < (CAST(substr(p.stop_price, 1, instr(p.stop_price || '.', '.') - 1) AS INTEGER) * 1000000 + CAST(substr(substr(p.stop_price, instr(p.stop_price || '.', '.') + 1) || '000000', 1, 6) AS INTEGER))
         OR (p.high_water IS NOT NULL AND (NEW.high_water IS NULL
             OR (CAST(substr(NEW.high_water, 1, instr(NEW.high_water || '.', '.') - 1) AS INTEGER) * 1000000 + CAST(substr(substr(NEW.high_water, instr(NEW.high_water || '.', '.') + 1) || '000000', 1, 6) AS INTEGER)) < (CAST(substr(p.high_water, 1, instr(p.high_water || '.', '.') - 1) AS INTEGER) * 1000000 + CAST(substr(substr(p.high_water, instr(p.high_water || '.', '.') + 1) || '000000', 1, 6) AS INTEGER)))))
) OR (NEW.seq <> 1 AND NOT EXISTS (
  SELECT 1 FROM plan_stop_levels WHERE trade_plan_id = NEW.trade_plan_id))
BEGIN SELECT RAISE(ABORT, 'plan_stop_levels: stop and high-water levels only ratchet up, in sequence'); END;

CREATE TRIGGER plan_stop_levels_owner BEFORE INSERT ON plan_stop_levels
WHEN NOT EXISTS (
  SELECT 1 FROM orders WHERE trade_plan_id = NEW.trade_plan_id AND order_role = 'ENTRY'
) OR (CASE WHEN json_valid(NEW.evidence_json) AND json_type(NEW.evidence_json) = 'object'
         THEN json_extract(NEW.evidence_json, '$.client_order_id') END IS NOT NULL
     AND NOT EXISTS (
  SELECT 1 FROM orders WHERE client_order_id = json_extract(NEW.evidence_json, '$.client_order_id')
    AND trade_plan_id = NEW.trade_plan_id
))
BEGIN SELECT RAISE(ABORT, 'plan_stop_levels: unknown plan, or evidence names an order of another plan'); END;

CREATE TRIGGER plan_stop_levels_no_update BEFORE UPDATE ON plan_stop_levels
BEGIN SELECT RAISE(ABORT, 'plan_stop_levels is append-only'); END;

CREATE TRIGGER plan_stop_levels_no_delete BEFORE DELETE ON plan_stop_levels
BEGIN SELECT RAISE(ABORT, 'plan_stop_levels rows cannot be deleted'); END;

CREATE TABLE protection_gaps (
  id                        INTEGER PRIMARY KEY,
  trade_plan_id             TEXT NOT NULL CHECK (trade_plan_id GLOB 'tp-[0-9a-f]*' AND length(trade_plan_id) = 35),
  opened_at                 TEXT NOT NULL CHECK (opened_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  reason                    TEXT NOT NULL CHECK (reason IN ('STOP_CANCEL_REQUESTED', 'STOP_ENDED_UNFILLED')),
  prior_client_order_id     TEXT NOT NULL REFERENCES orders(client_order_id),
  confirmed_at              TEXT CHECK (confirmed_at IS NULL OR confirmed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  closed_at                 TEXT CHECK (closed_at IS NULL OR closed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  close_reason              TEXT CHECK (close_reason IS NULL OR close_reason IN ('STOP_ACKNOWLEDGED', 'POSITION_FLAT')),
  closed_by_client_order_id TEXT REFERENCES orders(client_order_id),
  CHECK ((closed_at IS NULL) = (close_reason IS NULL)),
  CHECK ((close_reason IS 'STOP_ACKNOWLEDGED') = (closed_by_client_order_id IS NOT NULL)),
  CHECK (reason <> 'STOP_ENDED_UNFILLED' OR confirmed_at IS opened_at),
  CHECK (confirmed_at IS NULL OR confirmed_at >= opened_at),
  CHECK (closed_at IS NULL OR closed_at >= opened_at),
  CHECK (closed_at IS NULL OR confirmed_at IS NULL OR closed_at >= confirmed_at)
) STRICT;

CREATE UNIQUE INDEX ux_protection_gaps_open ON protection_gaps(trade_plan_id) WHERE closed_at IS NULL;

CREATE TRIGGER protection_gaps_owner BEFORE INSERT ON protection_gaps
WHEN NOT EXISTS (
  SELECT 1 FROM orders WHERE trade_plan_id = NEW.trade_plan_id AND order_role = 'ENTRY'
) OR NOT EXISTS (
  SELECT 1 FROM orders WHERE client_order_id = NEW.prior_client_order_id
    AND trade_plan_id = NEW.trade_plan_id AND order_role = 'PROTECTIVE_STOP'
) OR NEW.closed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'protection_gaps: must open for a known plan and one of its protective stops'); END;

CREATE TRIGGER protection_gaps_set_once BEFORE UPDATE ON protection_gaps
WHEN OLD.closed_at IS NOT NULL
  OR NEW.id IS NOT OLD.id OR NEW.trade_plan_id IS NOT OLD.trade_plan_id
  OR NEW.opened_at IS NOT OLD.opened_at OR NEW.reason IS NOT OLD.reason
  OR NEW.prior_client_order_id IS NOT OLD.prior_client_order_id
  OR (OLD.confirmed_at IS NOT NULL AND NEW.confirmed_at IS NOT OLD.confirmed_at)
BEGIN SELECT RAISE(ABORT, 'protection_gaps: confirmation and closure are each set once, on an open gap'); END;

CREATE TRIGGER protection_gaps_closer_owner BEFORE UPDATE OF closed_by_client_order_id ON protection_gaps
WHEN NEW.closed_by_client_order_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM orders WHERE client_order_id = NEW.closed_by_client_order_id
    AND trade_plan_id = NEW.trade_plan_id AND order_role = 'PROTECTIVE_STOP'
)
BEGIN SELECT RAISE(ABORT, 'protection_gaps: the closing order must be a protective stop of the same plan'); END;

CREATE TRIGGER protection_gaps_no_delete BEFORE DELETE ON protection_gaps
BEGIN SELECT RAISE(ABORT, 'protection_gaps rows cannot be deleted'); END;
