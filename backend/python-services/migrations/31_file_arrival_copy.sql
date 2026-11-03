-- ============================================================================
-- Keep a copy of every received file when there is no blob storage.
--
-- WHY
-- ---
-- With STORAGE_BACKEND=db, storage.store_or_keep hands the bytes back instead
-- of a blob ref, and file_arrival has no bytes column. So a file sent by API,
-- SFTP or email was checked and recorded, then auto-run found nothing to open:
-- "No copy of this file was kept, so it cannot be run."
--
-- WHAT
-- ----
-- A WAITING ROOM for API / SFTP / email files, not a second store. The file
-- is kept here exactly as received only until it is processed:
--   * a successful run deletes it -- the rows are in landing_record by then,
--     the same as a broker's own Process Bordereau run leaves;
--   * a failed run keeps it, so Run again has a file to run;
--   * a rejected or held file is deleted by the 90-day retention cleanup.
-- Manual uploads never use it. The arrival's blob_ref reads
-- 'db:file_arrival_file' while its copy is here. Its own table, not a
-- file_arrival column, so listing files never drags the bytes along.
--
-- Until this runs, the code skips the copy and behaves as before.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS file_arrival_file (
    arrival_id  BIGINT PRIMARY KEY
                REFERENCES file_arrival (arrival_id) ON DELETE CASCADE,
    tenant_id   BIGINT NOT NULL,
    file_bytes  BYTEA  NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
