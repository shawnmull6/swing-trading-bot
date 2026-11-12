-- Migration 0006: audit trail of the rebaseline workflow (ARCHITECTURE §8.5, §12.18).
--
-- rebaseline_requests (0001) holds each immutable preview and its final decision.
-- rebaseline_events records every operator action against the workflow, including
-- refusals that never produced a request (e.g. a proposal while trading is not halted)
-- and repeated confirmations. halt_events cannot hold these: its action is limited to
-- HALT / RESUME / RESUME_REFUSED. Append-only.

CREATE TABLE rebaseline_events (
  id          INTEGER PRIMARY KEY,
  request_id  INTEGER REFERENCES rebaseline_requests(id),
  ts          TEXT NOT NULL CHECK (ts GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
  action      TEXT NOT NULL CHECK (action IN ('PROPOSED', 'PROPOSE_REFUSED', 'CONFIRM_REFUSED', 'EXPIRED', 'APPROVED', 'DUPLICATE_CONFIRM')),
  actor       TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json))
) STRICT;
CREATE INDEX ix_rebaseline_events_request ON rebaseline_events(request_id, id);

CREATE TRIGGER rebaseline_events_no_update BEFORE UPDATE ON rebaseline_events
BEGIN SELECT RAISE(ABORT, 'rebaseline_events is append-only'); END;

CREATE TRIGGER rebaseline_events_no_delete BEFORE DELETE ON rebaseline_events
BEGIN SELECT RAISE(ABORT, 'rebaseline_events rows cannot be deleted'); END;
