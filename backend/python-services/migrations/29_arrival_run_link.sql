-- ============================================================================
-- Every arrival knows the run it became — and manual uploads are arrivals too.
--
-- WHY
-- ---
-- `file_arrival.bdx_upload_id` was reserved for "the processing seam" and never
-- written: a file that came in by email, SFTP or API was checked, stored and
-- then left where it was. The Files screen could only ever say "Waiting to be
-- run". And a file uploaded by hand on Process Bordereau was run but never
-- recorded as having arrived at all, so Files could not show it.
--
-- WHAT
-- ----
-- 1. The run an arrival became, and how that went:
--      run_state        NULL        accepted, not picked up yet (auto-run takes it)
--                       running     being run now
--                       done        the run finished — its result is on the export
--                       failed      the run stopped; run_error says why
--                       not_run     it cannot be run automatically (run_error says
--                                   why — e.g. the way in names no programme)
--                       pre_autorun arrived before auto-run existed; left alone
--      run_landing_id   landing_records.id of that run
--      run_export_id    output_exports.id — carries status + exception count
--      run_error, run_at
-- 2. What a manual upload has instead of a route:
--      channel, program_id, submitted_by_user_id
--
-- Every column is nullable. Existing accepted rows are stamped `pre_autorun`
-- so switching auto-run on does not suddenly run a backlog of old files.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS run_state             TEXT,
    ADD COLUMN IF NOT EXISTS run_landing_id        BIGINT,
    ADD COLUMN IF NOT EXISTS run_export_id         BIGINT,
    ADD COLUMN IF NOT EXISTS run_error             TEXT,
    ADD COLUMN IF NOT EXISTS run_at                TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS channel               TEXT,
    ADD COLUMN IF NOT EXISTS program_id            BIGINT,
    ADD COLUMN IF NOT EXISTS submitted_by_user_id  BIGINT;

ALTER TABLE file_arrival
    DROP CONSTRAINT IF EXISTS file_arrival_run_state_check;

ALTER TABLE file_arrival
    ADD CONSTRAINT file_arrival_run_state_check
    CHECK (run_state IS NULL OR run_state IN
           ('running', 'done', 'failed', 'not_run', 'pre_autorun'));

-- The backlog: everything already accepted stays exactly as it was.
UPDATE file_arrival
   SET run_state = 'pre_autorun'
 WHERE run_state IS NULL
   AND outcome = 'accepted';

-- What the auto-run worker asks every few seconds.
CREATE INDEX IF NOT EXISTS file_arrival_awaiting_run
    ON file_arrival (arrival_id)
 WHERE outcome = 'accepted' AND run_state IS NULL;

COMMIT;
