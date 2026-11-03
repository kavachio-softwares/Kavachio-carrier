-- ============================================================================
-- 44 — MOPR 41: the carrier's target for fixing problems.
--
-- WHY
-- ---
-- 41 measures how long the problems the checks find (check_error, migration
-- 43) take to be fixed, and how many are fixed within a target. The target is
-- the carrier's own: one number of days, 5 until they change it.
--
-- WHAT
-- ----
--   quality_setting   one row per carrier, written only when the carrier
--                     changes the target (none = the default, 5 days). Each
--                     change is also an activity_events row
--                     ("fix_target_updated") with the old and new value.
--
-- Off RLS, db.init_db's create_all builds the table; under RLS run this file.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS quality_setting (
    id                   SERIAL PRIMARY KEY,
    tenant_id            INTEGER NOT NULL UNIQUE,
    fix_target_days      INTEGER NOT NULL DEFAULT 5,
    updated_by_user_id   INTEGER,
    updated_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_quality_setting_tenant_id ON quality_setting (tenant_id);

COMMIT;
