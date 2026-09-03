-- ============================================================================
-- Feature 10 — keep the row count of an arriving file.
--
-- `count_rows()` already computes this during the arrival checks: twice, in
-- fact — once to prove the file can be opened and once to prove it is not
-- empty — and then throws the number away. For a bordereau the row count is
-- what people actually want to see on Files Received; the file's size in
-- kilobytes tells them nothing.
--
-- NULL means "we could not open it", which is different from 0, "we opened it
-- and it is empty". Both already drive a decision in the checks, so the column
-- must be able to hold the difference.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS row_count INTEGER;

COMMIT;
