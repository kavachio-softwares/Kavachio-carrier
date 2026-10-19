-- ============================================================================
-- 40 — MOPR 39.1: tell "Overdue" apart from "Never arrived".
--
-- WHY
-- ---
-- A bordereau past its due date was Overdue for ever, so "Never arrived" on
-- the calendar and the dashboards was only another name for it. The two are
-- different answers: Overdue is still being waited for; Never arrived is the
-- period closed with nothing in it. The period closes when the NEXT period's
-- file falls due as well (submission_calendar.closes_on_for): a monthly July
-- file due 10 Aug is Overdue from 11 Aug and Never arrived from 11 Sep.
--
-- WHAT
-- ----
--   expected_submission.closes_on   the next period's due date — the last day
--                                   we still wait for this period's file
--
-- Nothing is backfilled here. The date depends on each programme's schedule
-- (frequency and due day), so the app fills it: heal_calendars rebuilds any
-- calendar with a NULL closes_on the next time the calendar is opened. Until
-- then a NULL row reads exactly as before (Overdue, never closing). Off RLS,
-- db.init_db adds the same column. Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE expected_submission ADD COLUMN IF NOT EXISTS closes_on DATE;

COMMIT;
