-- Migration 0003: settlement-resolution audit, official session closes, and the
-- equity-baseline / loss-limit tables used by M1 step 7.
--
-- Why this migration is necessary
--   * settlement_resolutions: step 6 resolved unknown settlement dates in place with no
--     record of when or from which calendar data. The step 7 review requires the
--     resolution to be auditable; this append-only table records each one.
--   * session_closes: baselines use each held symbol's OFFICIAL close of the reference
--     session. price_marks keeps only the latest mark per symbol (overwritten intraday),
--     so closes need their own immutable, per-session store.
--   * equity_snapshots / loss_limit_events: 0001 created them before their design was
--     final. They lacked what step 7 needs: which session/week a baseline applies to vs.
--     which session's close defines it, the baseline a loss event was measured against,
--     the threshold used, and uniqueness that makes repeated evaluation idempotent. Both
--     are rebuilt; no step before 7 writes them, and copying any existing row (which
--     would lack the new NOT NULL columns) aborts the migration (fail closed).

CREATE TABLE settlement_resolutions (
  id             INTEGER PRIMARY KEY,
  cash_entry_id  INTEGER NOT NULL UNIQUE REFERENCES cash_entries(id),
  trade_date     TEXT NOT NULL CHECK (trade_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  settles_on     TEXT NOT NULL CHECK (settles_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  resolved_at    TEXT NOT NULL CHECK (resolved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  method         TEXT NOT NULL CHECK (method IN ('CALENDAR')),
  calendar_fetch_ids_json TEXT NOT NULL CHECK (json_valid(calendar_fetch_ids_json)),
  CHECK (settles_on > trade_date)
) STRICT;

CREATE TRIGGER settlement_resolutions_no_update BEFORE UPDATE ON settlement_resolutions
BEGIN SELECT RAISE(ABORT, 'settlement_resolutions is append-only'); END;

CREATE TRIGGER settlement_resolutions_no_delete BEFORE DELETE ON settlement_resolutions
BEGIN SELECT RAISE(ABORT, 'settlement_resolutions rows cannot be deleted'); END;

CREATE TABLE session_closes (
  symbol       TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
  session_date TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  close_price  TEXT NOT NULL CHECK (typeof(close_price) = 'text' AND close_price NOT GLOB '*[^0-9.]*' AND close_price GLOB '*[0-9]*' AND close_price NOT GLOB '*.*.*' AND close_price NOT GLOB '*.' AND close_price NOT GLOB '.*'),
  source       TEXT NOT NULL CHECK (source IN ('SESSION_CLOSE_BAR', 'SIM')),
  as_of        TEXT NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  received_at  TEXT NOT NULL CHECK (received_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  feed         TEXT,
  PRIMARY KEY (symbol, session_date)
) STRICT;

CREATE TRIGGER session_closes_no_update BEFORE UPDATE ON session_closes
BEGIN SELECT RAISE(ABORT, 'session_closes is append-only'); END;

CREATE TRIGGER session_closes_no_delete BEFORE DELETE ON session_closes
BEGIN SELECT RAISE(ABORT, 'session_closes rows cannot be deleted'); END;

CREATE TABLE equity_snapshots_v2 (
  id                 INTEGER PRIMARY KEY,
  portfolio_id       INTEGER NOT NULL REFERENCES portfolios(id),
  kind               TEXT NOT NULL CHECK (kind IN ('SESSION_BASELINE', 'WEEK_BASELINE')),
  applies_to         TEXT NOT NULL CHECK (applies_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  reference_session  TEXT CHECK (reference_session IS NULL OR reference_session GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  reference_at       TEXT NOT NULL CHECK (reference_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  basis              TEXT NOT NULL CHECK (basis IN ('SESSION_CLOSE', 'INCEPTION')),
  equity             TEXT NOT NULL CHECK (typeof(equity) = 'text' AND equity NOT GLOB '*[^0-9.-]*' AND equity GLOB '*[0-9]*' AND equity NOT GLOB '*.*.*' AND equity NOT GLOB '?*-*' AND equity NOT GLOB '*.' AND equity NOT GLOB '.*' AND equity NOT GLOB '-.*'),
  cash               TEXT NOT NULL CHECK (typeof(cash) = 'text' AND cash NOT GLOB '*[^0-9.-]*' AND cash GLOB '*[0-9]*' AND cash NOT GLOB '*.*.*' AND cash NOT GLOB '?*-*' AND cash NOT GLOB '*.' AND cash NOT GLOB '.*' AND cash NOT GLOB '-.*'),
  market_value       TEXT NOT NULL CHECK (typeof(market_value) = 'text' AND market_value NOT GLOB '*[^0-9.-]*' AND market_value GLOB '*[0-9]*' AND market_value NOT GLOB '*.*.*' AND market_value NOT GLOB '?*-*' AND market_value NOT GLOB '*.' AND market_value NOT GLOB '.*' AND market_value NOT GLOB '-.*'),
  marks_json         TEXT NOT NULL CHECK (json_valid(marks_json)),
  taken_at           TEXT NOT NULL CHECK (taken_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  CHECK ((basis = 'SESSION_CLOSE') = (reference_session IS NOT NULL)),
  UNIQUE (portfolio_id, kind, applies_to)
) STRICT;

INSERT INTO equity_snapshots_v2
  (id, portfolio_id, kind, applies_to, reference_session, reference_at, basis, equity, cash,
   market_value, marks_json, taken_at)
SELECT id, portfolio_id, kind, session_date, NULL, NULL, NULL, equity, cash, NULL, marks_json,
       taken_at
FROM equity_snapshots;

DROP TABLE equity_snapshots;

ALTER TABLE equity_snapshots_v2 RENAME TO equity_snapshots;

CREATE TRIGGER equity_snapshots_no_update BEFORE UPDATE ON equity_snapshots
BEGIN SELECT RAISE(ABORT, 'equity_snapshots is append-only'); END;

CREATE TRIGGER equity_snapshots_no_delete BEFORE DELETE ON equity_snapshots
BEGIN SELECT RAISE(ABORT, 'equity_snapshots rows cannot be deleted'); END;

CREATE TABLE loss_limit_events_v2 (
  id                   INTEGER PRIMARY KEY,
  portfolio_id         INTEGER NOT NULL REFERENCES portfolios(id),
  ts                   TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  kind                 TEXT NOT NULL CHECK (kind IN ('DAILY_LOCKOUT', 'DAILY_REPEAT_HALT', 'WEEKLY_HALT')),
  applies_to           TEXT NOT NULL CHECK (applies_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  baseline_snapshot_id INTEGER NOT NULL REFERENCES equity_snapshots(id),
  baseline_equity      TEXT NOT NULL CHECK (typeof(baseline_equity) = 'text' AND baseline_equity NOT GLOB '*[^0-9.-]*' AND baseline_equity GLOB '*[0-9]*' AND baseline_equity NOT GLOB '*.*.*' AND baseline_equity NOT GLOB '?*-*' AND baseline_equity NOT GLOB '*.' AND baseline_equity NOT GLOB '.*' AND baseline_equity NOT GLOB '-.*'),
  equity               TEXT NOT NULL CHECK (typeof(equity) = 'text' AND equity NOT GLOB '*[^0-9.-]*' AND equity GLOB '*[0-9]*' AND equity NOT GLOB '*.*.*' AND equity NOT GLOB '?*-*' AND equity NOT GLOB '*.' AND equity NOT GLOB '.*' AND equity NOT GLOB '-.*'),
  loss                 TEXT NOT NULL CHECK (typeof(loss) = 'text' AND loss NOT GLOB '*[^0-9.-]*' AND loss GLOB '*[0-9]*' AND loss NOT GLOB '*.*.*' AND loss NOT GLOB '?*-*' AND loss NOT GLOB '*.' AND loss NOT GLOB '.*' AND loss NOT GLOB '-.*'),
  loss_pct             TEXT NOT NULL CHECK (typeof(loss_pct) = 'text' AND loss_pct NOT GLOB '*[^0-9.-]*' AND loss_pct GLOB '*[0-9]*' AND loss_pct NOT GLOB '*.*.*' AND loss_pct NOT GLOB '?*-*' AND loss_pct NOT GLOB '*.' AND loss_pct NOT GLOB '.*' AND loss_pct NOT GLOB '-.*'),
  threshold_pct        TEXT NOT NULL CHECK (typeof(threshold_pct) = 'text' AND threshold_pct NOT GLOB '*[^0-9.]*' AND threshold_pct GLOB '*[0-9]*' AND threshold_pct NOT GLOB '*.*.*' AND threshold_pct NOT GLOB '*.' AND threshold_pct NOT GLOB '.*'),
  detail_json          TEXT NOT NULL CHECK (json_valid(detail_json)),
  UNIQUE (portfolio_id, kind, applies_to)
) STRICT;

INSERT INTO loss_limit_events_v2
  (id, portfolio_id, ts, kind, applies_to, baseline_snapshot_id, baseline_equity, equity, loss,
   loss_pct, threshold_pct, detail_json)
SELECT id, NULL, ts, kind, session_date, NULL, baseline_equity, equity, NULL, loss_pct, NULL, NULL
FROM loss_limit_events;

DROP TABLE loss_limit_events;

ALTER TABLE loss_limit_events_v2 RENAME TO loss_limit_events;

CREATE TRIGGER loss_limit_events_no_update BEFORE UPDATE ON loss_limit_events
BEGIN SELECT RAISE(ABORT, 'loss_limit_events is append-only'); END;

CREATE TRIGGER loss_limit_events_no_delete BEFORE DELETE ON loss_limit_events
BEGIN SELECT RAISE(ABORT, 'loss_limit_events rows cannot be deleted'); END;
