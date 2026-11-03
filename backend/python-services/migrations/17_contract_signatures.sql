-- ============================================================================
-- Contract management — the signatures, and the gate they hold.
--
-- WHY A TABLE AND NOT A JSON FIELD. Who a contract's signatories WILL be is
-- authoring metadata and lives with the wording. That somebody actually signed
-- is a different kind of fact: it is dated, attributed to a user, and it is
-- what a contract's being in force now rests on. It needs rows that can be
-- queried, audited and pointed at — not a key in a blob that an unrelated save
-- could overwrite.
--
-- WHAT IT CHANGES ABOUT GOING LIVE. A contract used to become active because
-- somebody pressed a button. Now it becomes active because both sides signed
-- it — the carrier and the counterparty, one row each. The button is still
-- there for the last step, but it refuses while a side is missing, and the
-- second signature normally puts the contract in force on its own.
--
-- KAVACHIO DOES NOT WITNESS A SIGNING. It records that one happened, which is
-- a smaller and much more honest claim, and `method` is what keeps the two
-- apart:
--
--   typed          the signatory was in Kavachio, was the right party, and
--                  typed their name against this contract. Attributable to a
--                  user id.
--   recorded       somebody signed on paper or in a provider elsewhere and the
--                  carrier recorded it here. Attributable to whoever recorded
--                  it — NOT to the signatory, who was never in this system.
--
-- The second is not a loophole, it is the only honest way to hold a contract
-- whose counterparty has no seat in Kavachio at all: a reinsurer never logs in,
-- so the alternative to recording their signature is a reinsurance contract
-- that can never go live.
--
-- A SIGNATURE IS ON A VERSION. Editing a draft's terms or its wording deletes
-- the signatures on it — see contract_routes.update_contract. A signature that
-- survived the thing it was on would be worse than no signature at all.
-- ============================================================================

CREATE TABLE IF NOT EXISTS contract_signature (
    contract_signature_id             BIGSERIAL PRIMARY KEY,
    contract_signature_tenant_id      BIGINT,
    contract_signature_contract_id    BIGINT NOT NULL
        REFERENCES contract (contract_id) ON DELETE CASCADE,

    -- Which organisation this signature is FOR, not who typed it. One side may
    -- have several signatories; it is signed when at least one has signed.
    contract_signature_side           TEXT NOT NULL
        CHECK (contract_signature_side IN ('carrier', 'counterparty')),

    contract_signature_signer_name    TEXT NOT NULL,
    contract_signature_signer_title   TEXT,
    contract_signature_signer_email   TEXT,

    -- See the header. 'typed' is attributable to the signatory; 'recorded' is
    -- attributable only to whoever recorded it.
    contract_signature_method         TEXT NOT NULL DEFAULT 'typed'
        CHECK (contract_signature_method IN ('typed', 'recorded')),

    -- The user who caused this row. For 'typed' that IS the signatory; for
    -- 'recorded' it is the carrier user who entered it.
    contract_signature_by_user_id     BIGINT,
    contract_signature_signed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The executed copy this signature was read off, where there is one.
    contract_signature_document_id    BIGINT
        REFERENCES contract_document (contract_document_id) ON DELETE SET NULL,

    contract_signature_note           TEXT,
    contract_signature_created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_contract_signature_contract
    ON contract_signature (contract_signature_contract_id);

-- One signature per person per side. Signing twice is a slip, not a second
-- signature, and it must not read as two signatories having signed.
CREATE UNIQUE INDEX IF NOT EXISTS ux_contract_signature_person
    ON contract_signature (contract_signature_contract_id,
                           contract_signature_side,
                           lower(contract_signature_signer_name));
