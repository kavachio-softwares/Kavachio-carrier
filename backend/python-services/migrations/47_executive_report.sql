-- ============================================================================
-- 47 — Executive Reporting 38.2: the Executive Report, sent by hand or on a
-- schedule to the carrier's logins and to any other email address it names.
--
-- WHY
-- ---
-- Leadership reads the book without logging in. The report is a PDF or Excel
-- file emailed weekly or monthly, or sent by hand, and a sent report must never
-- change afterwards, so the numbers are kept exactly as they went out.
--
-- WHAT
-- ----
--   executive_report_setting   one row per carrier, written only when it saves
--                              the schedule (none = automatic sending OFF).
--                              Each change is also an activity_events row
--                              ("executive_report_settings_updated").
--   executive_report_send      one row per report sent, with each recipient and
--                              whether it arrived, and the numbers as sent.
--                              scheduled_key is unique so the automatic send for
--                              a week or month can only happen once.
--
-- Its own pair rather than monthly_report_setting / _send: that setting's
-- tenant_id is unique, so a carrier cannot hold a second report's schedule.
--
-- NUMBERING: 47 is the next number after the two 46_* files on
-- development-phase-2; the team has not yet agreed a scheme (CLAUDE.md).
--
-- Off RLS, db.init_db's create_all builds both tables; under RLS run this file.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS executive_report_setting (
    id                   SERIAL PRIMARY KEY,
    tenant_id            INTEGER NOT NULL UNIQUE,
    auto_send            BOOLEAN NOT NULL DEFAULT FALSE,
    cadence              VARCHAR NOT NULL DEFAULT 'month',
    week_day             INTEGER NOT NULL DEFAULT 0,
    send_day             INTEGER NOT NULL DEFAULT 3,
    file_format          VARCHAR NOT NULL DEFAULT 'pdf',
    user_ids             JSON,
    emails               JSON,
    updated_by_user_id   INTEGER,
    updated_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_executive_report_setting_tenant_id
    ON executive_report_setting (tenant_id);

CREATE TABLE IF NOT EXISTS executive_report_send (
    id                   SERIAL PRIMARY KEY,
    tenant_id            INTEGER NOT NULL,
    cadence              VARCHAR NOT NULL,
    period               VARCHAR NOT NULL,
    kind                 VARCHAR NOT NULL,
    scheduled_key        VARCHAR UNIQUE,
    file_format          VARCHAR NOT NULL,
    recipients           JSON,
    delivered            INTEGER NOT NULL DEFAULT 0,
    failed               INTEGER NOT NULL DEFAULT 0,
    pack                 JSON,
    options              JSON,
    sent_by_user_id      INTEGER,
    created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    hidden_at            TIMESTAMP WITHOUT TIME ZONE
);

-- "Clear all" on the Sent reports list sets hidden_at: the row is hidden from
-- that list, never deleted. For a table created before this column existed.
ALTER TABLE executive_report_send ADD COLUMN IF NOT EXISTS hidden_at TIMESTAMP WITHOUT TIME ZONE;

CREATE INDEX IF NOT EXISTS ix_executive_report_send_tenant_id ON executive_report_send (tenant_id);
CREATE INDEX IF NOT EXISTS ix_executive_report_send_created_at ON executive_report_send (created_at);

COMMIT;
