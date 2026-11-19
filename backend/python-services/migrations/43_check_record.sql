-- ============================================================================
-- 43 — MOPR 40: one shared record of every check run and every error found.
--
-- WHY
-- ---
-- Each half of the system kept its own record of its checks: the direct lane
-- on the processed file (output_exports.exceptions — overwritten by a
-- re-generate), the canonical lane in validation_run / validation_exception
-- (only when something failed), and the file-arrival checks only the reason a
-- file was refused. A check that ran and found nothing was recorded nowhere,
-- so "how many checks passed", "how many files passed with no human help" and
-- "how long did a problem stay open" could not be answered. Every one of them
-- now writes to the same three tables (check_record.py).
--
-- WHAT
-- ----
--   check_run      one row per run: lane, kind, status, counts, and the
--                  volumes it handled (rows, policies, transaction types)
--   check_result   every check in a run: passed, failed or not run
--   check_error    every error found, with when it was first found
--
-- Append-only. Nothing is backfilled: the record starts with the next run.
-- Off RLS, db.init_db's create_all builds the tables; under RLS run this file.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS check_run (
    id                SERIAL PRIMARY KEY,
    tenant_id         INTEGER,
    lane              VARCHAR NOT NULL,
    kind              VARCHAR NOT NULL,
    status            VARCHAR,
    status_reason     TEXT,
    export_id         INTEGER,
    landing_id        INTEGER,
    arrival_id        INTEGER,
    upload_id         INTEGER,
    pipeline_id       INTEGER,
    program_id        INTEGER,
    broker_party_id   INTEGER,
    contract_id       INTEGER,
    filename          VARCHAR,
    period            VARCHAR,
    submission_ref    VARCHAR,
    version_no        INTEGER,
    chain_key         VARCHAR,
    human_touch       JSON,
    checks_total      INTEGER DEFAULT 0,
    checks_passed     INTEGER DEFAULT 0,
    checks_failed     INTEGER DEFAULT 0,
    checks_not_run    INTEGER DEFAULT 0,
    errors_total      INTEGER DEFAULT 0,
    critical_count    INTEGER DEFAULT 0,
    warning_count     INTEGER DEFAULT 0,
    info_count        INTEGER DEFAULT 0,
    rows_in           INTEGER,
    rows_out          INTEGER,
    rows_checked      INTEGER,
    rows_with_errors  INTEGER,
    policies          INTEGER,
    transaction_types JSON,
    run_by            VARCHAR,
    run_by_user_id    INTEGER,
    created_at        TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_check_run_tenant_id       ON check_run (tenant_id);
CREATE INDEX IF NOT EXISTS ix_check_run_export_id       ON check_run (export_id);
CREATE INDEX IF NOT EXISTS ix_check_run_landing_id      ON check_run (landing_id);
CREATE INDEX IF NOT EXISTS ix_check_run_arrival_id      ON check_run (arrival_id);
CREATE INDEX IF NOT EXISTS ix_check_run_upload_id       ON check_run (upload_id);
CREATE INDEX IF NOT EXISTS ix_check_run_program_id      ON check_run (program_id);
CREATE INDEX IF NOT EXISTS ix_check_run_broker_party_id ON check_run (broker_party_id);
CREATE INDEX IF NOT EXISTS ix_check_run_chain_key       ON check_run (chain_key);
CREATE INDEX IF NOT EXISTS ix_check_run_created_at      ON check_run (created_at);

CREATE TABLE IF NOT EXISTS check_result (
    id           SERIAL PRIMARY KEY,
    run_id       INTEGER NOT NULL,
    tenant_id    INTEGER,
    check_key    VARCHAR NOT NULL,
    kind         VARCHAR NOT NULL,
    rule_id      INTEGER,
    name         VARCHAR,
    severity     VARCHAR,
    outcome      VARCHAR NOT NULL,
    error_count  INTEGER DEFAULT 0,
    message      TEXT,
    created_at   TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_check_result_run_id    ON check_result (run_id);
CREATE INDEX IF NOT EXISTS ix_check_result_tenant_id ON check_result (tenant_id);

CREATE TABLE IF NOT EXISTS check_error (
    id               SERIAL PRIMARY KEY,
    run_id           INTEGER NOT NULL,
    tenant_id        INTEGER,
    lane             VARCHAR,
    export_id        INTEGER,
    program_id       INTEGER,
    broker_party_id  INTEGER,
    chain_key        VARCHAR,
    check_key        VARCHAR,
    rule_id          INTEGER,
    code             VARCHAR,
    severity         VARCHAR,
    sheet            VARCHAR,
    "row"            INTEGER,
    field            VARCHAR,
    policy_number    VARCHAR,
    actual_value     TEXT,
    message          TEXT,
    fingerprint      VARCHAR,
    first_found_at   TIMESTAMP WITHOUT TIME ZONE,
    created_at       TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_check_error_run_id      ON check_error (run_id);
CREATE INDEX IF NOT EXISTS ix_check_error_tenant_id   ON check_error (tenant_id);
CREATE INDEX IF NOT EXISTS ix_check_error_export_id   ON check_error (export_id);
CREATE INDEX IF NOT EXISTS ix_check_error_fingerprint ON check_error (fingerprint);
CREATE INDEX IF NOT EXISTS ix_check_error_chain_key   ON check_error (chain_key);
CREATE INDEX IF NOT EXISTS ix_check_error_created_at  ON check_error (created_at);

COMMIT;

-- One month's files handled, and how many passed with no human help:
--   SELECT count(*) AS files,
--          count(*) FILTER (WHERE status = 'clean'
--                           AND coalesce(human_touch::text, '[]') = '[]') AS straight_through
--     FROM check_run
--    WHERE lane IN ('direct', 'canonical') AND kind = 'run'
--      AND created_at >= '2026-10-01' AND created_at < '2026-11-01';
