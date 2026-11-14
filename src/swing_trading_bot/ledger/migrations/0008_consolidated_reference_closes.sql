-- Migration 0008: consolidated SIP reference closes and close-conflict audit (Step 16.9).
--
-- Why this migration is necessary
--   * session_closes gains the source CONSOLIDATED_BAR: the close of a RAW consolidated
--     (SIP) daily bar admitted as a loss-limit reference close. It is never OFFICIAL.
--     The source CHECK cannot be altered in place, so the table is rebuilt.
--   * A new nullable column adjustment records the bar adjustment. It is required and
--     must be 'raw' for CONSOLIDATED_BAR (split-adjusted bars are restated retroactively
--     and do not match ledger share quantities) and must be NULL for every other source;
--     CONSOLIDATED_BAR also requires feed = 'sip'.
--   * session_close_conflicts (append-only) keeps every later observation that differs
--     from a recorded close. The recorded close is never changed; a conflict blocks new
--     entries while it affects a current baseline.
--   * The feed rules are written NULL-safe: a SQL CHECK that evaluates to NULL passes,
--     so 0004's "source <> 'DAILY_BAR' OR feed IN (...)" accepted a NULL feed. The
--     application never wrote such a row (SessionClose rejects it); the database now
--     rejects it too.
--   Existing rows (OFFICIAL, DAILY_BAR, SIM) are copied unchanged with adjustment NULL;
--   any row violating the new constraints aborts the migration (fail closed).

CREATE TABLE session_closes_v3 (
  symbol       TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
  session_date TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  close_price  TEXT NOT NULL CHECK (typeof(close_price) = 'text' AND close_price NOT GLOB '*[^0-9.]*' AND close_price GLOB '*[0-9]*' AND close_price NOT GLOB '*.*.*' AND close_price NOT GLOB '*.' AND close_price NOT GLOB '.*'),
  source       TEXT NOT NULL CHECK (source IN ('OFFICIAL', 'CONSOLIDATED_BAR', 'DAILY_BAR', 'SIM')),
  as_of        TEXT NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  received_at  TEXT NOT NULL CHECK (received_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  feed         TEXT,
  adjustment   TEXT CHECK (adjustment IS NULL OR adjustment = 'raw'),
  CHECK (source <> 'DAILY_BAR' OR (feed IS NOT NULL AND feed IN ('iex', 'sip', 'delayed_sip'))),
  CHECK (source <> 'SIM' OR feed IS NULL OR feed = 'sim'),
  CHECK (source <> 'CONSOLIDATED_BAR' OR (feed IS 'sip' AND adjustment IS 'raw')),
  CHECK (source = 'CONSOLIDATED_BAR' OR adjustment IS NULL),
  PRIMARY KEY (symbol, session_date, source)
) STRICT;

INSERT INTO session_closes_v3
  (symbol, session_date, close_price, source, as_of, received_at, feed, adjustment)
SELECT symbol, session_date, close_price, source, as_of, received_at, feed, NULL
FROM session_closes;

DROP TABLE session_closes;

ALTER TABLE session_closes_v3 RENAME TO session_closes;

CREATE TRIGGER session_closes_no_update BEFORE UPDATE ON session_closes
BEGIN SELECT RAISE(ABORT, 'session_closes is append-only'); END;

CREATE TRIGGER session_closes_no_delete BEFORE DELETE ON session_closes
BEGIN SELECT RAISE(ABORT, 'session_closes rows cannot be deleted'); END;

CREATE TABLE session_close_conflicts (
  id                   INTEGER PRIMARY KEY,
  symbol               TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
  session_date         TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  source               TEXT NOT NULL CHECK (source IN ('OFFICIAL', 'CONSOLIDATED_BAR', 'DAILY_BAR', 'SIM')),
  recorded_close       TEXT NOT NULL CHECK (typeof(recorded_close) = 'text' AND recorded_close NOT GLOB '*[^0-9.]*' AND recorded_close GLOB '*[0-9]*' AND recorded_close NOT GLOB '*.*.*' AND recorded_close NOT GLOB '*.' AND recorded_close NOT GLOB '.*'),
  observed_close       TEXT NOT NULL CHECK (typeof(observed_close) = 'text' AND observed_close NOT GLOB '*[^0-9.]*' AND observed_close GLOB '*[0-9]*' AND observed_close NOT GLOB '*.*.*' AND observed_close NOT GLOB '*.' AND observed_close NOT GLOB '.*'),
  observed_as_of       TEXT NOT NULL CHECK (observed_as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  observed_received_at TEXT NOT NULL CHECK (observed_received_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  detected_at          TEXT NOT NULL CHECK (detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  UNIQUE (symbol, session_date, source, observed_close)
) STRICT;

CREATE TRIGGER session_close_conflicts_no_update BEFORE UPDATE ON session_close_conflicts
BEGIN SELECT RAISE(ABORT, 'session_close_conflicts is append-only'); END;

CREATE TRIGGER session_close_conflicts_no_delete BEFORE DELETE ON session_close_conflicts
BEGIN SELECT RAISE(ABORT, 'session_close_conflicts rows cannot be deleted'); END;
