-- ============================================================================
-- 50 — Record integrity and retention (tasks 52.1, 52.2): prove audit records
-- were never changed, and delete them once their retention period has passed,
-- unless a legal hold keeps them.
--
-- WHAT
-- ----
--   audit_seal            one fingerprint (SHA-256) per audit record, each
--                         linked to the one before it (a chain), so a changed
--                         record, a removed record or a rewritten fingerprint
--                         all show. A record deleted by retention keeps its
--                         seal, marked gone, so the chain stays whole.
--   retention_rule        how many months each kind of record is kept, for
--                         one country or everywhere ('' = everywhere). No
--                         rows, or months left empty, means nothing is deleted.
--   legal_hold            a hold on a carrier's records, or on one broker's:
--                         nothing it covers is deleted while it is on.
--   retention_deletion    what each retention run deleted: when, which kind,
--                         how many, the oldest and newest, and the period used.
--
-- THE GUARD (trigger kavachio_protect_audit_record)
--   activity_events, auth_audit, exception_decision_log, audit_finding_event,
--   audit_seal and retention_deletion refuse UPDATE, DELETE and TRUNCATE.
--   The only way through is a transaction that has run
--       SET LOCAL kavachio.retention_delete = 'on'
--   which retention.py does for its own deletes. Nothing in the app updates or
--   deletes these rows today (checked 19 Nov 2026). NOTE for the team: a
--   cleanup script that deletes test carriers must now set that flag too.
--
-- WHO: Kavachio sets the retention periods; a carrier sees them and places or
-- lifts holds on its own records; Kavachio may do so for any carrier
-- (record_integrity.py, retention.py, integrity_routes.py).
--
-- NOT BUILT BY init_db: these tables are on their own model registry, so a
-- person applies this file, deliberately, after a backup and a test on a local
-- copy (CLAUDE.md). Until then the Integrity & Retention tab says it is not set
-- up on that database, and the daily run does nothing.
--
-- NUMBERING: 50 is the next number after 49_audit_management.sql on
-- development-phase-2; the team has not yet agreed a scheme (CLAUDE.md).
--
-- Idempotent: safe to run more than once.
-- ============================================================================

CREATE TABLE IF NOT EXISTS audit_seal (
    seal_id                 BIGSERIAL PRIMARY KEY,
    seal_table              VARCHAR(40)  NOT NULL
        CHECK (seal_table IN ('activity_events', 'auth_audit', 'exception_decision_log', 'audit_finding_event')),
    seal_record_id          BIGINT       NOT NULL,
    seal_tenant_id          INTEGER,
    seal_record_at          TIMESTAMP,
    seal_hash               CHAR(64)     NOT NULL,
    seal_chain              CHAR(64)     NOT NULL,
    seal_at                 TIMESTAMP    NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    seal_gone_at            TIMESTAMP,
    seal_gone_run_id        INTEGER,
    CONSTRAINT uq_audit_seal_record UNIQUE (seal_table, seal_record_id)
);
CREATE INDEX IF NOT EXISTS ix_audit_seal_tenant ON audit_seal (seal_tenant_id, seal_table);

CREATE TABLE IF NOT EXISTS retention_rule (
    rr_id                   SERIAL PRIMARY KEY,
    rr_record_type          VARCHAR(20)  NOT NULL
        CHECK (rr_record_type IN ('activity', 'sign_in', 'decisions', 'audits')),
    rr_country              VARCHAR(80)  NOT NULL DEFAULT '',
    rr_months               INTEGER
        CHECK (rr_months IS NULL OR rr_months BETWEEN 1 AND 1200),
    rr_note                 TEXT,
    rr_updated_by_user_id   INTEGER      REFERENCES app_user (user_id),
    rr_updated_at           TIMESTAMP    NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT uq_retention_rule UNIQUE (rr_record_type, rr_country)
);

CREATE TABLE IF NOT EXISTS legal_hold (
    lh_id                   SERIAL PRIMARY KEY,
    lh_tenant_id            INTEGER      NOT NULL REFERENCES tenant (tenant_id),
    lh_broker_party_id      INTEGER      REFERENCES party (party_id),
    lh_reason               TEXT         NOT NULL,
    lh_placed_by_user_id    INTEGER      REFERENCES app_user (user_id),
    lh_placed_at            TIMESTAMP    NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    lh_released_at          TIMESTAMP,
    lh_released_by_user_id  INTEGER      REFERENCES app_user (user_id),
    lh_release_note         TEXT
);
CREATE INDEX IF NOT EXISTS ix_legal_hold_tenant ON legal_hold (lh_tenant_id) WHERE lh_released_at IS NULL;

CREATE TABLE IF NOT EXISTS retention_deletion (
    rd_id                   SERIAL PRIMARY KEY,
    rd_run_at               TIMESTAMP    NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    rd_tenant_id            INTEGER,
    rd_record_type          VARCHAR(20)  NOT NULL,
    rd_country              VARCHAR(80),
    rd_months               INTEGER      NOT NULL,
    rd_cutoff               TIMESTAMP    NOT NULL,
    rd_count                INTEGER      NOT NULL DEFAULT 0,
    rd_held                 INTEGER      NOT NULL DEFAULT 0,
    rd_oldest               TIMESTAMP,
    rd_newest               TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_retention_deletion_tenant ON retention_deletion (rd_tenant_id, rd_run_at);

-- ── the guard ──────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION kavachio_protect_audit_record() RETURNS trigger AS $$
BEGIN
    IF current_setting('kavachio.retention_delete', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        IF TG_OP = 'UPDATE' THEN RETURN NEW; END IF;
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'Audit records cannot be changed or deleted (% on %).', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['activity_events', 'auth_audit', 'exception_decision_log', 'audit_finding_event',
                             'audit_seal', 'retention_deletion']
    LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format('DROP TRIGGER IF EXISTS trg_protect_audit_row ON %I', t);
            EXECUTE format('CREATE TRIGGER trg_protect_audit_row BEFORE UPDATE OR DELETE ON %I '
                           'FOR EACH ROW EXECUTE FUNCTION kavachio_protect_audit_record()', t);
            EXECUTE format('DROP TRIGGER IF EXISTS trg_protect_audit_truncate ON %I', t);
            EXECUTE format('CREATE TRIGGER trg_protect_audit_truncate BEFORE TRUNCATE ON %I '
                           'FOR EACH STATEMENT EXECUTE FUNCTION kavachio_protect_audit_record()', t);
        END IF;
    END LOOP;
END
$$;
