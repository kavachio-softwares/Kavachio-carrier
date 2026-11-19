-- 44 — a master treaty signed party by party.
--
-- A treaty is placed with several reinsurers, each writing its own line under
-- one shared wording. Each signs for ITSELF: on its private link, in the
-- signing round (the lead), or on paper — then the carrier records it against
-- document evidence (its signed line slip, or the signed wording). One row per
-- reinsurer: what it signed (its lines, the wording version), how, and the
-- evidence. Placement statuses (unplaced / partially / fully / over-placed;
-- unsigned / partially / fully signed) are worked out from these and the
-- structure, not stored.
BEGIN;
CREATE TABLE IF NOT EXISTS treaty_party_signature (
    tps_id            SERIAL PRIMARY KEY,
    tps_tenant_id     INTEGER,
    tps_contract_id   INTEGER NOT NULL REFERENCES contract (contract_id),
    tps_party_id      INTEGER NOT NULL REFERENCES party (party_id),
    tps_signer_name   VARCHAR NOT NULL,
    tps_signer_title  VARCHAR,
    tps_signer_email  VARCHAR,
    tps_method        VARCHAR NOT NULL CHECK (tps_method IN ('link', 'esign', 'recorded')),
    -- Evidence: the document it rests on (a signed line slip, the signed wording).
    tps_document_id   INTEGER REFERENCES contract_document (contract_document_id),
    -- The shared wording it signed (the version), and its lines as signed.
    tps_wording_document_id INTEGER,
    tps_lines         JSON,
    tps_link_id       INTEGER,
    tps_ip            VARCHAR,
    tps_note          TEXT,
    tps_recorded_by   INTEGER,
    tps_signed_at     TIMESTAMPTZ DEFAULT now(),
    tps_withdrawn_at  TIMESTAMPTZ,
    tps_withdrawn_by  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_tps_contract ON treaty_party_signature (tps_contract_id);
CREATE INDEX IF NOT EXISTS ix_tps_tenant ON treaty_party_signature (tps_tenant_id);
-- One live signature per reinsurer per treaty.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tps_live ON treaty_party_signature (tps_contract_id, tps_party_id)
    WHERE tps_withdrawn_at IS NULL;
-- A document can belong to ONE reinsurer on the treaty (its signed line slip).
ALTER TABLE contract_document ADD COLUMN IF NOT EXISTS contract_document_party_id INTEGER;
COMMIT;
