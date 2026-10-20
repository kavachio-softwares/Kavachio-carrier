-- ============================================================================
-- 41 — Reinsurance (ceded) bordereaux.
--
-- ceded_bordereau   one treaty × one reporting period × one version, built
--                   from the live inward bordereaux on the programmes the
--                   treaty protects (ceded_engine.py).
-- ceded_statement   each reinsurer's part of it: its share of every line, its
--                   totals, its workbook, and its answer (agreed / queried).
--
-- What each column means is on the ORM classes in db.py (CededBordereau,
-- CededStatement). Dev databases get these from create_all.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS ceded_bordereau (
    cb_id             SERIAL PRIMARY KEY,
    cb_tenant_id      INTEGER,
    cb_contract_id    INTEGER NOT NULL REFERENCES contract (contract_id),
    cb_period         VARCHAR NOT NULL,
    cb_version_no     INTEGER NOT NULL DEFAULT 1,
    cb_status         VARCHAR NOT NULL DEFAULT 'draft',
    cb_currency       VARCHAR,
    cb_totals         JSONB,
    cb_notes          JSONB,
    cb_skipped        JSONB,
    cb_lines          JSONB,
    cb_sources        JSONB,
    cb_missing        JSONB,
    cb_source_key     VARCHAR,
    cb_previous_id    INTEGER,
    cb_auto_prepared  BOOLEAN DEFAULT false,
    cb_built_at       TIMESTAMPTZ,
    cb_built_by       INTEGER,
    cb_sent_at        TIMESTAMPTZ,
    cb_sent_by        INTEGER,
    cb_created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ceded_bordereau_version
    ON ceded_bordereau (cb_contract_id, cb_period, cb_version_no);
CREATE INDEX IF NOT EXISTS ix_ceded_bordereau_cb_tenant_id
    ON ceded_bordereau (cb_tenant_id);

CREATE TABLE IF NOT EXISTS ceded_statement (
    cs_id                  SERIAL PRIMARY KEY,
    cs_cb_id               INTEGER NOT NULL REFERENCES ceded_bordereau (cb_id),
    cs_contract_id         INTEGER NOT NULL,
    cs_tenant_id           INTEGER,
    cs_reinsurer_party_id  INTEGER NOT NULL REFERENCES party (party_id),
    cs_shares              JSONB,
    cs_totals              JSONB,
    cs_previous_totals     JSONB,
    cs_filename            VARCHAR,
    cs_blob                BYTEA,
    cs_blob_ref            VARCHAR,
    cs_status              VARCHAR NOT NULL DEFAULT 'pending',
    cs_sent_at             TIMESTAMPTZ,
    cs_sent_to             JSONB,
    cs_answered_at         TIMESTAMPTZ,
    cs_answered_by         VARCHAR,
    cs_answer_note         TEXT,
    cs_answer_ip           VARCHAR
);
CREATE INDEX IF NOT EXISTS ix_ceded_statement_cb ON ceded_statement (cs_cb_id);
CREATE INDEX IF NOT EXISTS ix_ceded_statement_reinsurer
    ON ceded_statement (cs_reinsurer_party_id);

-- How each treaty's statements are made and sent (CededSetup in db.py). No
-- statement is prepared for a treaty until its setup is active.
CREATE TABLE IF NOT EXISTS ceded_setup (
    csu_id            SERIAL PRIMARY KEY,
    csu_tenant_id     INTEGER,
    csu_contract_id   INTEGER NOT NULL UNIQUE REFERENCES contract (contract_id),
    csu_status        VARCHAR NOT NULL DEFAULT 'draft',
    csu_frequency     VARCHAR NOT NULL DEFAULT 'monthly',
    csu_due_days      INTEGER NOT NULL DEFAULT 30,
    csu_program_ids   JSONB,
    csu_columns       JSONB,
    csu_layout        JSONB,
    csu_layout_blob   BYTEA,
    csu_file_format   VARCHAR NOT NULL DEFAULT 'xlsx',
    csu_recipients    JSONB,
    csu_cc            JSONB,
    csu_send_mode     VARCHAR NOT NULL DEFAULT 'manual',
    csu_note          TEXT,
    csu_submitted_by  INTEGER,
    csu_submitted_at  TIMESTAMPTZ,
    csu_approved_by   INTEGER,
    csu_approved_at   TIMESTAMPTZ,
    csu_updated_by    INTEGER,
    csu_created_at    TIMESTAMPTZ DEFAULT now(),
    csu_updated_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_ceded_setup_csu_tenant_id ON ceded_setup (csu_tenant_id);
-- A period's file of the carrier's own, when a setup reads from it.
CREATE TABLE IF NOT EXISTS ceded_input (
    cin_id            SERIAL PRIMARY KEY,
    cin_tenant_id     INTEGER,
    cin_contract_id   INTEGER NOT NULL REFERENCES contract (contract_id),
    cin_period        VARCHAR NOT NULL,
    cin_version_no    INTEGER NOT NULL DEFAULT 1,
    cin_filename      VARCHAR,
    cin_blob          BYTEA,
    cin_rows          INTEGER,
    cin_layout        JSONB,
    cin_uploaded_by   INTEGER,
    cin_uploaded_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ceded_input_cin_contract_id ON ceded_input (cin_contract_id);
CREATE INDEX IF NOT EXISTS ix_ceded_input_cin_tenant_id ON ceded_input (cin_tenant_id);

-- A database that ran an earlier copy of this file.
ALTER TABLE ceded_setup ADD COLUMN IF NOT EXISTS csu_input_layout JSONB;
ALTER TABLE ceded_input ADD COLUMN IF NOT EXISTS cin_layout JSONB;
ALTER TABLE ceded_setup ADD COLUMN IF NOT EXISTS csu_rules JSONB;
ALTER TABLE ceded_bordereau ADD COLUMN IF NOT EXISTS cb_checks JSONB;
ALTER TABLE ceded_setup ADD COLUMN IF NOT EXISTS csu_layout JSONB;
ALTER TABLE ceded_setup ADD COLUMN IF NOT EXISTS csu_layout_blob BYTEA;

COMMIT;
