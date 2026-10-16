-- ============================================================================
-- 40 — Reinsurance treaty structure (Phase 26) and the reinsurer's private,
--      read-only link.
--
-- 26.1  A treaty's type (quota share, surplus, XOL, stop loss, facultative),
--       its layers, and each reinsurer's share of each layer.
-- 26.2  The programmes a treaty protects. A treaty is raised on one programme
--       (contract.contract_program_id) and may protect more.
--
-- The reinsurer has no seat in Kavachio. reinsurer_access_link is how one
-- person at a reinsurer reads their own line: an emailed link that opens only
-- after the one-time code sent to the same address is typed — the lock the
-- signing link already uses (17_contract_esign_otp.sql), on its own table.
--
-- What each column means is documented on the ORM classes in db.py
-- (Treaty, TreatyLayer, TreatyShare, TreatyProgramme, ReinsurerAccessLink,
-- ReinsurerAccessEvent). Dev databases get these tables from create_all.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS reinsurance_treaty (
    treaty_contract_id  INTEGER PRIMARY KEY REFERENCES contract (contract_id),
    treaty_tenant_id    INTEGER,
    treaty_type         VARCHAR NOT NULL,
    treaty_terms        JSONB,
    treaty_updated_by   INTEGER,
    treaty_created_at   TIMESTAMPTZ DEFAULT now(),
    treaty_updated_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_reinsurance_treaty_tenant
    ON reinsurance_treaty (treaty_tenant_id);

CREATE TABLE IF NOT EXISTS reinsurance_treaty_layer (
    layer_id            SERIAL PRIMARY KEY,
    layer_contract_id   INTEGER NOT NULL REFERENCES contract (contract_id),
    layer_tenant_id     INTEGER,
    layer_position      INTEGER NOT NULL DEFAULT 0,
    layer_name          VARCHAR NOT NULL,
    layer_terms         JSONB
);
CREATE INDEX IF NOT EXISTS ix_treaty_layer_contract
    ON reinsurance_treaty_layer (layer_contract_id, layer_position);

CREATE TABLE IF NOT EXISTS reinsurance_treaty_share (
    share_id                  SERIAL PRIMARY KEY,
    share_layer_id            INTEGER NOT NULL
                              REFERENCES reinsurance_treaty_layer (layer_id),
    share_contract_id         INTEGER NOT NULL REFERENCES contract (contract_id),
    share_tenant_id           INTEGER,
    share_reinsurer_party_id  INTEGER NOT NULL REFERENCES party (party_id),
    share_pct                 NUMERIC NOT NULL
                              CHECK (share_pct > 0 AND share_pct <= 100),
    share_is_lead             BOOLEAN NOT NULL DEFAULT false,
    share_slip_reference      VARCHAR
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_treaty_share_layer_party
    ON reinsurance_treaty_share (share_layer_id, share_reinsurer_party_id);
CREATE INDEX IF NOT EXISTS ix_reinsurance_treaty_share_layer
    ON reinsurance_treaty_share (share_layer_id);
CREATE INDEX IF NOT EXISTS ix_reinsurance_treaty_share_contract
    ON reinsurance_treaty_share (share_contract_id);
CREATE INDEX IF NOT EXISTS ix_reinsurance_treaty_share_reinsurer
    ON reinsurance_treaty_share (share_reinsurer_party_id);

CREATE TABLE IF NOT EXISTS reinsurance_treaty_programme (
    tp_id           SERIAL PRIMARY KEY,
    tp_contract_id  INTEGER NOT NULL REFERENCES contract (contract_id),
    tp_program_id   INTEGER NOT NULL REFERENCES program (program_id),
    tp_tenant_id    INTEGER,
    tp_created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_treaty_programme
    ON reinsurance_treaty_programme (tp_contract_id, tp_program_id);
CREATE INDEX IF NOT EXISTS ix_reinsurance_treaty_programme_program
    ON reinsurance_treaty_programme (tp_program_id);

-- Every existing treaty protects at least the programme it was raised on.
INSERT INTO reinsurance_treaty_programme (tp_contract_id, tp_program_id, tp_tenant_id)
SELECT c.contract_id, c.contract_program_id, c.tenant_id
  FROM contract c
 WHERE c.contract_type = 'insurer_reinsurer'
   AND c.contract_program_id IS NOT NULL
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS reinsurer_access_link (
    ral_id                SERIAL PRIMARY KEY,
    ral_contract_id       INTEGER NOT NULL REFERENCES contract (contract_id),
    ral_tenant_id         INTEGER,
    ral_party_id          INTEGER NOT NULL REFERENCES party (party_id),
    ral_contact_id        INTEGER,
    ral_name              VARCHAR NOT NULL,
    ral_email             VARCHAR NOT NULL,
    -- SHA-256 of the link token. The token itself is never stored.
    ral_token_hash        VARCHAR NOT NULL UNIQUE,
    ral_expires_at        TIMESTAMPTZ NOT NULL,
    ral_revoked_at        TIMESTAMPTZ,
    ral_revoked_by        INTEGER,
    ral_sent_at           TIMESTAMPTZ,
    ral_sent_by           INTEGER,
    ral_first_opened_at   TIMESTAMPTZ,
    ral_last_viewed_at    TIMESTAMPTZ,
    -- bcrypt hash of the six-digit code, and its lockout bookkeeping.
    ral_otp_hash          TEXT,
    ral_otp_expires       TIMESTAMPTZ,
    ral_otp_attempts      INTEGER NOT NULL DEFAULT 0,
    ral_otp_locked_until  TIMESTAMPTZ,
    ral_otp_verified_at   TIMESTAMPTZ,
    ral_otp_sent_count    INTEGER NOT NULL DEFAULT 0,
    ral_otp_last_sent_at  TIMESTAMPTZ,
    ral_created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_reinsurer_access_contract
    ON reinsurer_access_link (ral_contract_id);
CREATE INDEX IF NOT EXISTS ix_reinsurer_access_link_ral_tenant_id
    ON reinsurer_access_link (ral_tenant_id);

CREATE TABLE IF NOT EXISTS reinsurer_access_event (
    rae_id           SERIAL PRIMARY KEY,
    rae_link_id      INTEGER NOT NULL REFERENCES reinsurer_access_link (ral_id),
    rae_contract_id  INTEGER NOT NULL,
    rae_tenant_id    INTEGER,
    rae_type         VARCHAR NOT NULL,
    rae_actor        VARCHAR,
    rae_ip           VARCHAR,
    rae_agent        VARCHAR,
    rae_detail       JSONB,
    rae_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_reinsurer_access_event_contract
    ON reinsurer_access_event (rae_contract_id, rae_at);

-- A reinsurer's approval of the treaty (or its request for a change), given on
-- its private link. rap_fingerprint ties it to the structure it was given on.
CREATE TABLE IF NOT EXISTS reinsurer_approval (
    rap_id            SERIAL PRIMARY KEY,
    rap_contract_id   INTEGER NOT NULL REFERENCES contract (contract_id),
    rap_tenant_id     INTEGER,
    rap_party_id      INTEGER NOT NULL REFERENCES party (party_id),
    rap_link_id       INTEGER REFERENCES reinsurer_access_link (ral_id),
    rap_status        VARCHAR NOT NULL,     -- approved | changes_requested
    rap_signer_name   VARCHAR,
    rap_signer_title  VARCHAR,
    rap_signer_email  VARCHAR,
    rap_note          TEXT,
    rap_fingerprint   VARCHAR NOT NULL,
    rap_ip            VARCHAR,
    rap_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_reinsurer_approval_contract
    ON reinsurer_approval (rap_contract_id, rap_at);

COMMIT;
