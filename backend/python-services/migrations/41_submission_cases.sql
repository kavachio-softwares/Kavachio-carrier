-- ============================================================================
-- 41 — MOPR 39.2: the case on a missing bordereau.
--
-- WHY
-- ---
-- 39.1 says whether a file came (on time, late, overdue, never arrived). It
-- did not say what was being done about a missing one. A late file now opens
-- a CASE that moves Overdue → Delinquent (automatically, N days late) →
-- In Dispute (the broker says the mark is wrong) → Resolved (the file came,
-- or the carrier accepted the dispute, waived the period or gave more time
-- that was then met). See submission_case.py.
--
-- WHAT
-- ----
--   expected_submission.case_*        where the case stands now
--   expected_submission.dispute_*     the open (or last) dispute
--   submission_schedule.delinquent_after_days
--                                     days late before Delinquent (default 15)
--   submission_case_event             every move, append-only: who, when, why
--
-- Nothing is backfilled: the daily check (and opening a calendar) opens cases
-- for files that are late. Off RLS, db.init_db adds the columns and
-- create_all builds the table; under RLS run this file.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE expected_submission
    ADD COLUMN IF NOT EXISTS case_state         VARCHAR,
    ADD COLUMN IF NOT EXISTS case_since         DATE,
    ADD COLUMN IF NOT EXISTS case_until         DATE,
    ADD COLUMN IF NOT EXISTS case_resolution    VARCHAR,
    ADD COLUMN IF NOT EXISTS dispute_reason     VARCHAR,
    ADD COLUMN IF NOT EXISTS dispute_note       TEXT,
    ADD COLUMN IF NOT EXISTS dispute_raised_on  DATE,
    ADD COLUMN IF NOT EXISTS dispute_by_carrier BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS dispute_locked     BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS dispute_reminded   BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_expected_submission_case_state
    ON expected_submission (case_state);

ALTER TABLE submission_schedule
    ADD COLUMN IF NOT EXISTS delinquent_after_days INTEGER DEFAULT 15;

CREATE TABLE IF NOT EXISTS submission_case_event (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER,
    expected_id            INTEGER NOT NULL,
    program_id             INTEGER,
    broker_party_id        INTEGER,
    period                 VARCHAR,
    kind                   VARCHAR NOT NULL,
    from_state             VARCHAR,
    to_state               VARCHAR,
    effective_on           DATE,
    reason                 VARCHAR,
    note                   TEXT,
    details                JSON,
    actor                  VARCHAR,
    actor_user_id          INTEGER,
    actor_role             VARCHAR,
    actor_broker_party_id  INTEGER,
    created_at             TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_submission_case_event_tenant_id       ON submission_case_event (tenant_id);
CREATE INDEX IF NOT EXISTS ix_submission_case_event_expected_id     ON submission_case_event (expected_id);
CREATE INDEX IF NOT EXISTS ix_submission_case_event_program_id      ON submission_case_event (program_id);
CREATE INDEX IF NOT EXISTS ix_submission_case_event_broker_party_id ON submission_case_event (broker_party_id);
CREATE INDEX IF NOT EXISTS ix_submission_case_event_created_at      ON submission_case_event (created_at);

COMMIT;

-- A period's case, oldest move first:
--   SELECT created_at, kind, from_state, to_state, reason, note, actor
--     FROM submission_case_event WHERE expected_id = :id ORDER BY id;
