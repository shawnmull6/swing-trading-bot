-- Migration 0005: broker fill progress on orders (step 12 check) and structured
-- reconciliation findings (step 13).
--
-- orders.broker_filled_qty
--   The filled quantity the broker last reported for the order. When it exceeds the
--   locally booked filled_qty (fills whose activities have not arrived yet), accounting
--   for that order is incomplete: gates and the risk engine block new risk, and the
--   reservation is never released, until the missing fills are booked. NULL means the
--   broker has not reported on the order yet. Existing rows get NULL (no defaults).
--
-- reconciliation_findings
--   One row per finding of a reconciliation run (append-only), so a run's outcome can
--   be explained and queried (e.g. by the ambiguous-order protocol). The run row in
--   reconciliation_runs keeps the result and the evidence boundaries (diff_json).

ALTER TABLE orders ADD COLUMN broker_filled_qty TEXT CHECK (broker_filled_qty IS NULL OR (typeof(broker_filled_qty) = 'text' AND broker_filled_qty NOT GLOB '*[^0-9.]*' AND broker_filled_qty GLOB '*[0-9]*' AND broker_filled_qty NOT GLOB '*.*.*' AND broker_filled_qty NOT GLOB '*.' AND broker_filled_qty NOT GLOB '.*'));

CREATE TABLE reconciliation_findings (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES reconciliation_runs(id),
  category    TEXT NOT NULL CHECK (category IN ('ACCOUNT', 'RETRIEVAL', 'ORDER', 'EXECUTION', 'ACTIVITY', 'POSITION', 'CASH', 'SETTLEMENT', 'LEDGER')),
  code        TEXT NOT NULL CHECK (length(trim(code)) > 0),
  severity    TEXT NOT NULL CHECK (severity IN ('MISMATCH', 'INCONCLUSIVE', 'ADVISORY')),
  subject     TEXT,
  detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json))
) STRICT;
CREATE INDEX ix_reconciliation_findings_run ON reconciliation_findings(run_id, id);

CREATE TRIGGER reconciliation_findings_no_update BEFORE UPDATE ON reconciliation_findings
BEGIN SELECT RAISE(ABORT, 'reconciliation_findings is append-only'); END;

CREATE TRIGGER reconciliation_findings_no_delete BEFORE DELETE ON reconciliation_findings
BEGIN SELECT RAISE(ABORT, 'reconciliation_findings rows cannot be deleted'); END;
