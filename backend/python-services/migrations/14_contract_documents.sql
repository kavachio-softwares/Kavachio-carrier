-- ============================================================================
-- Contract management — the documents a contract is made of.
--
-- `contract_document` ALREADY EXISTS in the v4 canonical schema: contract_id,
-- type, version, filename, blob reference, file hash, is-executed-copy,
-- uploaded-by. It has been there, empty, reached by nothing in the app. It is
-- the right table for this, so this migration extends it rather than creating a
-- second one beside it.
--
-- Why the contract needs it at all. Until now a contract WAS its file: one blob
-- on the contract row. Three things in the contract flow cannot be said that
-- way.
--
--   * A contract can be raised from its TERMS before anyone has the executed
--     wording, so the file has to become optional — which it cannot be while it
--     is the thing the row is keyed on.
--   * A wording that defers rule content to another document ("per the
--     Purchasing Guidelines on file") needs that document held ALONGSIDE it,
--     attributed to the reference it answers, so "what is still missing" is a
--     query rather than a guess.
--   * An endorsement does not replace the wording. Both stay active and rule
--     generation reads the pair, with the endorsement's clauses overriding the
--     ones they amend. One blob column cannot hold two active documents.
--
-- The five columns below are what those three needs add:
--
--   satisfies_reference  which externally-named document this row answers, so
--                        the mandatory-reference gate can be computed.
--   effective_from       when an endorsement takes effect. Where two touch the
--                        same term, the later one wins.
--   is_active            what rule generation filters on. Superseding a
--                        document is a flag flip, never a delete — the rules it
--                        produced have to stay explainable afterwards.
--   extracted            cached parse, so re-generating rules does not re-read
--                        every attachment.
--   blob                 the DB-blob fallback for when blob storage is off,
--                        matching the (blob_ref, blob) pair every other
--                        file-bearing table here uses. The canonical table only
--                        ever had the Azure pointer.
--
-- Nothing is migrated INTO this table: contracts that already exist keep
-- resolving from contract.blob / contract.blob_ref exactly as before, and the
-- read path falls back to them when a contract has no `contract` document row.
-- That is deliberate — a backfill copying every existing blob would double the
-- storage of every contract on the platform to change nothing a user sees.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE contract_document
    ADD COLUMN IF NOT EXISTS contract_document_satisfies_reference TEXT,
    ADD COLUMN IF NOT EXISTS contract_document_effective_from      DATE,
    ADD COLUMN IF NOT EXISTS contract_document_is_active           BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS contract_document_extracted           JSONB,
    ADD COLUMN IF NOT EXISTS contract_document_blob                BYTEA;

CREATE INDEX IF NOT EXISTS ix_contract_document_contract
    ON contract_document (contract_document_contract_id);

-- Rule generation asks this question on every run: "the active documents for
-- this contract". Worth an index of its own because it is a partial one — the
-- inactive rows are history and are never in the answer.
CREATE INDEX IF NOT EXISTS ix_contract_document_active
    ON contract_document (contract_document_contract_id, contract_document_type)
    WHERE contract_document_is_active;

COMMIT;
