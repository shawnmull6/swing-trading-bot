-- Migration 0007: event-specific rebaseline acknowledgements (step 13.2).
--
-- Step 13.1 excluded every unexplained activity and closed external order that
-- predated the active baseline. That suppressed a whole time range, so an event first
-- discovered after a rebaseline (posted late, earlier timestamp) could go unnoticed.
-- Acknowledgement is now per event: each approved rebaseline records exactly which
-- broker activities and broker orders it acknowledged, with a verbatim copy of the
-- payload it saw. An event is excluded from later unresolved-difference checks only
-- when it is listed here for an APPROVED request AND its stored payload is still
-- byte-identical; anything else (new events, changed payloads) is evaluated as usual.
-- Append-only.

CREATE TABLE rebaseline_acknowledgements (
  request_id   INTEGER NOT NULL REFERENCES rebaseline_requests(id),
  kind         TEXT NOT NULL CHECK (kind IN ('ACTIVITY', 'ORDER')),
  ref_id       TEXT NOT NULL CHECK (length(trim(ref_id)) > 0),
  payload      TEXT NOT NULL CHECK (length(payload) > 0),
  amount       TEXT CHECK (amount IS NULL OR (typeof(amount) = 'text' AND amount NOT GLOB '*[^0-9.-]*' AND amount GLOB '*[0-9]*' AND amount NOT GLOB '*.*.*' AND amount NOT GLOB '?*-*' AND amount NOT GLOB '*.' AND amount NOT GLOB '.*' AND amount NOT GLOB '-.*')),
  in_baseline  INTEGER NOT NULL CHECK (in_baseline IN (0, 1)),
  PRIMARY KEY (request_id, kind, ref_id)
) STRICT;
CREATE INDEX ix_rebaseline_ack_ref ON rebaseline_acknowledgements(kind, ref_id);

CREATE TRIGGER rebaseline_acknowledgements_no_update BEFORE UPDATE ON rebaseline_acknowledgements
BEGIN SELECT RAISE(ABORT, 'rebaseline_acknowledgements is append-only'); END;

CREATE TRIGGER rebaseline_acknowledgements_no_delete BEFORE DELETE ON rebaseline_acknowledgements
BEGIN SELECT RAISE(ABORT, 'rebaseline_acknowledgements rows cannot be deleted'); END;
