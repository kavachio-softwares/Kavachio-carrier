-- Migration 32 — the broker exception loop.
--
-- No new tables. A broker's files for one bordereau are one SUBMISSION
-- (file_arrival rows sharing submission_ref = the first file's public_ref);
-- a correction made on the secure link is an output_exports row carrying the
-- next version number. Messages, deliveries and the link's codes/answers are
-- activity_events rows. See submission_service.py.
--
-- Every column is nullable and nothing reads it outside the loop, so existing
-- flows are unchanged. Idempotent. The app's init_db adds the same columns
-- (db.py, _ensure_column) when it is not running under RLS.

BEGIN;

-- file_arrival: which submission a file is, its version, and (on the first
-- file only) the submission's correction deadline and delivery.
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS reporting_period VARCHAR;
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS submission_ref   VARCHAR;
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS version_no       INTEGER;
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS matched_by       VARCHAR;
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS version_status   VARCHAR;
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS version_note     TEXT;
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS deadline_at      TIMESTAMP WITH TIME ZONE;
ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS delivered_at     TIMESTAMP WITH TIME ZONE;
CREATE INDEX IF NOT EXISTS ix_file_arrival_submission_ref ON file_arrival (submission_ref);

-- output_exports: the submission and version a checked file belongs to; a
-- secure-link correction keeps its own status here (NULL on a file's export).
ALTER TABLE output_exports ADD COLUMN IF NOT EXISTS submission_ref VARCHAR;
ALTER TABLE output_exports ADD COLUMN IF NOT EXISTS version_no     INTEGER;
ALTER TABLE output_exports ADD COLUMN IF NOT EXISTS version_status VARCHAR;
ALTER TABLE output_exports ADD COLUMN IF NOT EXISTS version_note   TEXT;
CREATE INDEX IF NOT EXISTS ix_output_exports_submission_ref ON output_exports (submission_ref);

-- program: what holds a broker's file back, and what happens at the deadline.
-- NULL = the defaults (critical holds; 5 days; deliver marked "unresolved").
ALTER TABLE program ADD COLUMN IF NOT EXISTS delivery_rule JSONB;

-- intake_route: extra addresses told about every file on a channel.
ALTER TABLE intake_route ADD COLUMN IF NOT EXISTS notify_emails JSONB;

COMMIT;
