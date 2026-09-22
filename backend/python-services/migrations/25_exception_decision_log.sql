-- ============================================================================
-- exception_decision_log: who decided each exception, kept for good.
--
-- WHY
-- ---
-- landing_correction keeps only the LATEST decision per cell (it is the
-- override the renderer applies), and its decided_by was whatever user id the
-- browser sent. validation_exception keeps only the latest resolution, same
-- source. So "which broker user fixed this row, and who decided it before
-- them" had no reliable answer.
--
-- WHAT
-- ----
-- One new table, append-only: a row per decision as it is saved, with the
-- decider taken from the login (db.ExceptionDecisionLog, decision_log.py).
-- Nothing existing changes; no rows are touched.
--
-- The app creates this table itself at start-up (db.init_db -> create_all)
-- unless KAVACHIO_RLS is on, in which case run this file.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS exception_decision_log (
    id                          SERIAL PRIMARY KEY,
    tenant_id                   INTEGER,
    export_id                   INTEGER,
    landing_id                  INTEGER,
    exception_id                INTEGER,
    program_id                  INTEGER,
    broker_party_id             INTEGER,
    lane                        VARCHAR NOT NULL,
    decided_by_user_id          INTEGER,
    decided_by_role             VARCHAR,
    decided_by_broker_party_id  INTEGER,
    kind                        VARCHAR NOT NULL,
    rule_id                     INTEGER,
    policy_number               VARCHAR,
    sheet                       VARCHAR,
    "row"                       INTEGER,
    field                       VARCHAR,
    old_value                   TEXT,
    new_value                   TEXT,
    reason                      TEXT,
    decided_at                  TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_exception_decision_log_tenant_id          ON exception_decision_log (tenant_id);
CREATE INDEX IF NOT EXISTS ix_exception_decision_log_export_id          ON exception_decision_log (export_id);
CREATE INDEX IF NOT EXISTS ix_exception_decision_log_landing_id         ON exception_decision_log (landing_id);
CREATE INDEX IF NOT EXISTS ix_exception_decision_log_broker_party_id    ON exception_decision_log (broker_party_id);
CREATE INDEX IF NOT EXISTS ix_exception_decision_log_decided_by_user_id ON exception_decision_log (decided_by_user_id);
CREATE INDEX IF NOT EXISTS ix_exception_decision_log_decided_at         ON exception_decision_log (decided_at);

COMMIT;

-- Who fixed what on one run, newest first:
--   SELECT l.decided_at, u.user_full_name, l.decided_by_role, l.kind,
--          l.sheet, l.row, l.field, l.old_value, l.new_value, l.reason
--     FROM exception_decision_log l
--     LEFT JOIN app_user u ON u.user_id = l.decided_by_user_id
--    WHERE l.export_id = <export id>
--    ORDER BY l.decided_at DESC;
