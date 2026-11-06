-- ============================================================================
-- 46 — MOPR 42.2: sending the Monthly Report.
--
-- WHY
-- ---
-- The Monthly Report (42.1) is on screen. 42.2 also delivers it: a PDF or
-- Excel file, emailed by itself on a working day after each month ends, or
-- sent by hand. A sent report must never change afterwards, so the numbers
-- are kept exactly as they went out.
--
-- WHAT
-- ----
--   monthly_report_setting   one row per carrier, written only when the
--                            carrier saves Send & Schedule (none = defaults,
--                            automatic sending OFF). Each change is also an
--                            activity_events row ("monthly_report_settings_updated").
--                            recipient_user_ids, sections and compare_* are
--                            unused (the report goes to the carrier's logins,
--                            in full); kept to match tables create_all already made.
--   monthly_report_send      one row per report sent — automatic or by hand —
--                            with who it went to, whether it arrived,
--                            and the numbers as sent. scheduled_key is unique so
--                            the automatic send for a month can only happen once.
--
-- Off RLS, db.init_db's create_all builds both tables; under RLS run this file.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS monthly_report_setting (
    id                   SERIAL PRIMARY KEY,
    tenant_id            INTEGER NOT NULL UNIQUE,
    auto_send            BOOLEAN NOT NULL DEFAULT FALSE,
    send_day             INTEGER NOT NULL DEFAULT 3,
    file_format          VARCHAR NOT NULL DEFAULT 'pdf',
    delivery             VARCHAR NOT NULL DEFAULT 'attach',
    recipient_user_ids   JSON,
    sections             JSON,
    compare_last_month   BOOLEAN NOT NULL DEFAULT TRUE,
    compare_last_year    BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by_user_id   INTEGER,
    updated_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_monthly_report_setting_tenant_id
    ON monthly_report_setting (tenant_id);

CREATE TABLE IF NOT EXISTS monthly_report_send (
    id                   SERIAL PRIMARY KEY,
    tenant_id            INTEGER NOT NULL,
    month                VARCHAR NOT NULL,
    kind                 VARCHAR NOT NULL,
    scheduled_key        VARCHAR UNIQUE,
    file_format          VARCHAR NOT NULL,
    delivery             VARCHAR NOT NULL,
    recipients           JSON,
    delivered            INTEGER NOT NULL DEFAULT 0,
    failed               INTEGER NOT NULL DEFAULT 0,
    pack                 JSON,
    options              JSON,
    sent_by_user_id      INTEGER,
    created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_monthly_report_send_tenant_id ON monthly_report_send (tenant_id);
CREATE INDEX IF NOT EXISTS ix_monthly_report_send_created_at ON monthly_report_send (created_at);

COMMIT;
