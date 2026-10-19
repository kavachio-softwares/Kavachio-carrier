-- ============================================================================
-- 42 — MOPR 39.2: remove repeated automatic moves from the case history.
--
-- WHY
-- ---
-- The daily check (submission_calendar_service.sweep_overdue) also runs when a
-- calendar is opened, and the Home dashboard opens two at once. Before the
-- check took a lock, sweeps that overlapped each read "no case yet" and each
-- wrote the same automatic moves, so one file's history read "Became overdue,
-- Became delinquent" three times. The code now lets one sweep run at a time;
-- this removes the copies already written.
--
-- WHAT
-- ----
-- Keeps the FIRST of each repeat — same file, same move, same day — and only
-- among automatic rows (actor = 'system'). Nothing a person did (a dispute or
-- a decision) is touched. The case itself (expected_submission.case_*) was
-- right all along and is not changed.
--
-- Off RLS, db.init_db runs the same DELETE on boot; under RLS run this file.
-- Idempotent: a second run deletes nothing.
-- ============================================================================

-- How many repeats there are, before deleting:
--   SELECT expected_id, kind, effective_on, count(*)
--     FROM submission_case_event
--    WHERE actor = 'system'
--    GROUP BY 1, 2, 3
--   HAVING count(*) > 1;

BEGIN;

DELETE FROM submission_case_event
 WHERE actor = 'system'
   AND id NOT IN (SELECT MIN(id)
                    FROM submission_case_event
                   WHERE actor = 'system'
                   GROUP BY expected_id, kind, effective_on);

COMMIT;
