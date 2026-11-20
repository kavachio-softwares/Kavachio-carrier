-- ============================================================================
-- 49 — Audit Management (tasks 53.1, 53.2): audits a carrier records against
-- its brokers and their contracts, the findings each audit raises, and every
-- step on a finding until it is closed.
--
-- WHAT
-- ----
--   audit_engagement       one audit: name, type, broker, contract (optional),
--                          what it covers, who it is assigned to, dates,
--                          status (planned | in_progress | closed).
--   audit_finding          one finding of an audit: what was found, how
--                          serious, what must be done, owner, due date,
--                          status (open | in_progress | closed) and how it
--                          was fixed. Carries the broker so a broker reads
--                          only the findings about itself.
--   audit_finding_event    every step on a finding, oldest first: created,
--                          reply, evidence (with the file), status changed,
--                          closed, reopened, edited — who and when.
--
-- WHO: the carrier creates audits and findings and is the only one who closes
-- a finding; a broker sees the findings about itself and may reply and add
-- evidence (audit_management.py).
--
-- NOT BUILT BY init_db: these tables are on their own model registry
-- (audit_management.AuditBase), so a person applies this file, deliberately,
-- after a backup and a test on a local copy (CLAUDE.md). Until it is applied
-- the Audits tab says Audits are not set up on that database.
--
-- NUMBERING: 49 is the next number after 48_programme_rules.sql on
-- development-phase-2; the team has not yet agreed a scheme (CLAUDE.md).
--
-- NAMING: audit_engagement, not "audit" — QA shares its database with
-- another app, and "audit" is a name another app could well use.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

CREATE TABLE IF NOT EXISTS audit_engagement (
    au_id                   SERIAL PRIMARY KEY,
    au_tenant_id            INTEGER      NOT NULL,
    au_name                 VARCHAR(200) NOT NULL,
    au_type                 VARCHAR(30)  NOT NULL DEFAULT 'other'
        CHECK (au_type IN ('annual', 'contract_compliance', 'claims', 'data_quality', 'other')),
    au_broker_party_id      INTEGER      NOT NULL REFERENCES party (party_id),
    au_contract_id          INTEGER      REFERENCES contract (contract_id),
    au_scope                TEXT,
    au_assigned_to          VARCHAR(200),
    au_start_date           DATE,
    au_due_date             DATE,
    au_status               VARCHAR(12)  NOT NULL DEFAULT 'planned'
        CHECK (au_status IN ('planned', 'in_progress', 'closed')),
    au_closing_note         TEXT,
    au_closed_at            TIMESTAMP WITHOUT TIME ZONE,
    au_closed_by_user_id    INTEGER,
    au_created_by_user_id   INTEGER,
    au_created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    au_updated_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_audit_engagement_tenant ON audit_engagement (au_tenant_id);
CREATE INDEX IF NOT EXISTS ix_audit_engagement_broker ON audit_engagement (au_broker_party_id);

CREATE TABLE IF NOT EXISTS audit_finding (
    af_id                   SERIAL PRIMARY KEY,
    af_audit_id             INTEGER      NOT NULL REFERENCES audit_engagement (au_id) ON DELETE CASCADE,
    af_tenant_id            INTEGER      NOT NULL,
    af_broker_party_id      INTEGER      NOT NULL REFERENCES party (party_id),
    af_title                TEXT         NOT NULL,
    af_severity             VARCHAR(10)  NOT NULL DEFAULT 'medium'
        CHECK (af_severity IN ('low', 'medium', 'high', 'critical')),
    af_action               TEXT,
    af_owner                VARCHAR(200),
    af_due_date             DATE,
    af_status               VARCHAR(12)  NOT NULL DEFAULT 'open'
        CHECK (af_status IN ('open', 'in_progress', 'closed')),
    af_resolution           TEXT,
    af_closed_at            TIMESTAMP WITHOUT TIME ZONE,
    af_closed_by_user_id    INTEGER,
    af_created_by_user_id   INTEGER,
    af_created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    af_updated_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_audit_finding_audit  ON audit_finding (af_audit_id);
CREATE INDEX IF NOT EXISTS ix_audit_finding_tenant ON audit_finding (af_tenant_id);
CREATE INDEX IF NOT EXISTS ix_audit_finding_broker ON audit_finding (af_broker_party_id);

CREATE TABLE IF NOT EXISTS audit_finding_event (
    afe_id                  SERIAL PRIMARY KEY,
    afe_finding_id          INTEGER      NOT NULL REFERENCES audit_finding (af_id) ON DELETE CASCADE,
    afe_tenant_id           INTEGER      NOT NULL,
    afe_kind                VARCHAR(12)  NOT NULL
        CHECK (afe_kind IN ('created', 'reply', 'evidence', 'status', 'closed', 'reopened', 'edited')),
    afe_note                TEXT,
    afe_from_status         VARCHAR(12),
    afe_to_status           VARCHAR(12),
    afe_file_name           VARCHAR(300),
    afe_file_ref            VARCHAR(500),
    afe_file_bytes          BYTEA,
    afe_by_user_id          INTEGER,
    afe_by_side             VARCHAR(10)  CHECK (afe_by_side IN ('carrier', 'broker')),
    afe_at                  TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_audit_finding_event_finding ON audit_finding_event (afe_finding_id);
