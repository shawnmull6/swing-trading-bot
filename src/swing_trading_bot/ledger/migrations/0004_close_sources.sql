-- Migration 0004: explicit provenance for session closes (step 9 refinement 3).
--
-- Why this migration is necessary
--   0003 labelled every stored close 'SESSION_CLOSE_BAR', which cannot distinguish an
--   official consolidated closing price from a single-venue (IEX) daily-bar
--   observation. Loss-limit baselines must use official closes only, so the source is
--   now explicit:
--     OFFICIAL   a verified official closing price (no such source is implemented in
--                M1; required before M2 execution),
--     DAILY_BAR  a market-data daily-bar close, which REQUIRES its feed (iex, sip,
--                delayed_sip) and is an observation, never an official close,
--     SIM        simulation input.
--   Existing 'SESSION_CLOSE_BAR' rows carry no provenance and cannot be classified;
--   copying them yields a NULL source and aborts the migration (fail closed). SIM rows
--   are copied unchanged.

CREATE TABLE session_closes_v2 (
  symbol       TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
  session_date TEXT NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  close_price  TEXT NOT NULL CHECK (typeof(close_price) = 'text' AND close_price NOT GLOB '*[^0-9.]*' AND close_price GLOB '*[0-9]*' AND close_price NOT GLOB '*.*.*' AND close_price NOT GLOB '*.' AND close_price NOT GLOB '.*'),
  source       TEXT NOT NULL CHECK (source IN ('OFFICIAL', 'DAILY_BAR', 'SIM')),
  as_of        TEXT NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  received_at  TEXT NOT NULL CHECK (received_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  feed         TEXT,
  CHECK (source <> 'DAILY_BAR' OR feed IN ('iex', 'sip', 'delayed_sip')),
  CHECK (source <> 'SIM' OR feed IS NULL OR feed = 'sim'),
  PRIMARY KEY (symbol, session_date, source)
) STRICT;

INSERT INTO session_closes_v2
  (symbol, session_date, close_price, source, as_of, received_at, feed)
SELECT symbol, session_date, close_price,
       CASE source WHEN 'SIM' THEN 'SIM' ELSE NULL END,
       as_of, received_at, feed
FROM session_closes;

DROP TABLE session_closes;

ALTER TABLE session_closes_v2 RENAME TO session_closes;

CREATE TRIGGER session_closes_no_update BEFORE UPDATE ON session_closes
BEGIN SELECT RAISE(ABORT, 'session_closes is append-only'); END;

CREATE TRIGGER session_closes_no_delete BEFORE DELETE ON session_closes
BEGIN SELECT RAISE(ABORT, 'session_closes rows cannot be deleted'); END;
