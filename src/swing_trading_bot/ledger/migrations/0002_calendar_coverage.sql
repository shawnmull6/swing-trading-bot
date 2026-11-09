-- Migration 0002: calendar coverage, settlement days and provenance (M1 step 5).
--
-- Why this migration is necessary
--   0001's market_calendar stores trading sessions only. That cannot distinguish an
--   exchange holiday (date inside a fetched range, no row) from data that was never
--   fetched, has no place for the separate settlement-day calendar (ARCHITECTURE §6.3:
--   bank holidays can be trading days, exchange holidays can be settlement days), and
--   records no provenance for staleness checks. This migration adds:
--     * calendar_fetches  - append-only log of every fetch (accepted or rejected) with
--                           its requested range: the ACCEPTED rows define coverage;
--     * settlement_days   - settlement-eligible dates from the SETTLEMENT calendar;
--     * market_calendar   - rebuilt with fetch provenance, open < close, and
--                           immutability triggers (a session never changes silently).
--   market_calendar is rebuilt rather than altered because the new provenance columns
--   are NOT NULL. Existing rows (none are written before step 5) would have no
--   provenance; copying them violates NOT NULL and aborts the migration (fail closed).

CREATE TABLE calendar_fetches (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('TRADING', 'SETTLEMENT')),
  range_start    TEXT NOT NULL CHECK (range_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  range_end      TEXT NOT NULL CHECK (range_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  fetched_at     TEXT NOT NULL CHECK (fetched_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  source         TEXT NOT NULL CHECK (length(trim(source)) > 0),
  row_count      INTEGER NOT NULL CHECK (row_count >= 0),
  payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
  status         TEXT NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED')),
  problems_json  TEXT NOT NULL CHECK (json_valid(problems_json) AND json_type(problems_json) = 'array'),
  CHECK (range_start <= range_end),
  CHECK ((status = 'ACCEPTED') = (problems_json = '[]'))
) STRICT;

CREATE INDEX ix_calendar_fetches_coverage
  ON calendar_fetches(kind, status, range_start, range_end);

CREATE TRIGGER calendar_fetches_no_update BEFORE UPDATE ON calendar_fetches
BEGIN SELECT RAISE(ABORT, 'calendar_fetches is append-only'); END;

CREATE TRIGGER calendar_fetches_no_delete BEFORE DELETE ON calendar_fetches
BEGIN SELECT RAISE(ABORT, 'calendar_fetches rows cannot be deleted'); END;

CREATE TABLE settlement_days (
  settlement_date         TEXT PRIMARY KEY CHECK (settlement_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  first_fetch_id          INTEGER NOT NULL REFERENCES calendar_fetches(id),
  last_confirmed_fetch_id INTEGER NOT NULL REFERENCES calendar_fetches(id)
) STRICT;

CREATE TRIGGER settlement_days_immutable BEFORE UPDATE OF settlement_date, first_fetch_id ON settlement_days
BEGIN SELECT RAISE(ABORT, 'settlement_days: column is immutable'); END;

CREATE TRIGGER settlement_days_no_delete BEFORE DELETE ON settlement_days
BEGIN SELECT RAISE(ABORT, 'settlement_days rows cannot be deleted'); END;

CREATE TABLE market_calendar_v2 (
  session_date            TEXT PRIMARY KEY CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  open_at                 TEXT NOT NULL CHECK (open_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  close_at                TEXT NOT NULL CHECK (close_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  settlement_date         TEXT CHECK (settlement_date IS NULL OR settlement_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  first_fetch_id          INTEGER NOT NULL REFERENCES calendar_fetches(id),
  last_confirmed_fetch_id INTEGER NOT NULL REFERENCES calendar_fetches(id),
  CHECK (open_at < close_at),
  CHECK (settlement_date IS NULL OR settlement_date > session_date)
) STRICT;

INSERT INTO market_calendar_v2
  (session_date, open_at, close_at, settlement_date, first_fetch_id, last_confirmed_fetch_id)
SELECT session_date, open_at, close_at, settlement_date, NULL, NULL FROM market_calendar;

DROP TABLE market_calendar;

ALTER TABLE market_calendar_v2 RENAME TO market_calendar;

CREATE TRIGGER market_calendar_immutable
BEFORE UPDATE OF session_date, open_at, close_at, first_fetch_id ON market_calendar
BEGIN SELECT RAISE(ABORT, 'market_calendar: column is immutable'); END;

CREATE TRIGGER market_calendar_settlement_set_once
BEFORE UPDATE OF settlement_date ON market_calendar
WHEN OLD.settlement_date IS NOT NULL AND NEW.settlement_date IS NOT OLD.settlement_date
BEGIN SELECT RAISE(ABORT, 'market_calendar: settlement_date cannot change once set'); END;

CREATE TRIGGER market_calendar_no_delete BEFORE DELETE ON market_calendar
BEGIN SELECT RAISE(ABORT, 'market_calendar rows cannot be deleted'); END;
