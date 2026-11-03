-- 42 — Mid-term changes (Kavachio_Mid_Term_Change_Plan.md §11.1).
--
-- No new table. A mid-term change is a row of the existing, empty
-- contract_amendment table; its checks use validation_rule's existing
-- effective_from / effective_to / source_amendment_id; its document is a
-- contract_document with the existing amendment_id. This adds only what those
-- tables lack:
--
--   1. One change in progress per contract, enforced by the database as well
--      as by the app (which also checks, under a row lock).
--   2. Indexes for the two new lookups: a contract's changes, and the checks
--      a change opened.
--   3. A signature now belongs to a DOCUMENT as well as a contract. The same
--      person signs the contract and, later, its mid-term change; the old
--      unique index (contract, side, name) refused the second signature. The
--      new one is (contract, document, side, name) — a contract's own
--      signatures (document_id NULL, or its wording) behave exactly as before.
--   4. An e-signing round can be for a mid-term change's document rather than
--      the contract wording: envelope_document_id names it (NULL = the
--      contract itself, as every existing round).
--
-- Apply BEFORE deploying the mid-term change code.
-- Safe to re-run: every statement checks before it acts.

-- 1 + 2 ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_contract_amendment_contract
    ON contract_amendment (contract_amendment_contract_id);

CREATE UNIQUE INDEX IF NOT EXISTS ux_contract_amendment_one_open
    ON contract_amendment (contract_amendment_contract_id)
 WHERE status IN ('with_broker', 'changes_requested', 'agreed',
                  'carrier_signed', 'signed', 'pending');

CREATE INDEX IF NOT EXISTS ix_validation_rule_source_amendment
    ON validation_rule (source_amendment_id)
 WHERE source_amendment_id IS NOT NULL;

-- 3 -------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS ux_contract_signature_person_document
    ON contract_signature (contract_signature_contract_id,
                           COALESCE(contract_signature_document_id, 0),
                           contract_signature_side,
                           lower(contract_signature_signer_name));
DROP INDEX IF EXISTS ux_contract_signature_person;

-- 4 -------------------------------------------------------------------------
ALTER TABLE contract_esign_envelope
    ADD COLUMN IF NOT EXISTS envelope_document_id integer NULL
        REFERENCES contract_document (contract_document_id) ON DELETE SET NULL;
