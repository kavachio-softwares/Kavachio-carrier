-- ============================================================================
-- Contract authoring — a contract WRITTEN in Kavachio, not just uploaded to it.
--
-- The create flow now runs Terms → Wording → Read it through → Signatures: the
-- carrier states the deal once and Kavachio writes the wording from it, quoting
-- each value verbatim in a generated section, adding the signature page, and
-- attaching the composed .docx as the contract's wording document — which then
-- flows through the SAME clause extraction and rule generation as an uploaded
-- contract. Two columns carry the authored state:
--
--   commercial_terms   what the two sides agreed to pay each other (commission,
--                      shares, fees, settlement) as one dict. JSON, not eleven
--                      columns, because the vocabulary grows deal by deal — see
--                      contract_types.COMMERCIAL_TERMS for the current one.
--   wording_sections   the sections as the carrier left them (generated, edited
--                      or hand-written) plus the signature-page layout. The
--                      .docx is built FROM this, so the document can always be
--                      regenerated and the text is never trapped in a binary.
--
-- Nullable, no backfill: an uploaded contract never had authored sections, and
-- NULL is exactly that fact.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE contract
    ADD COLUMN IF NOT EXISTS commercial_terms JSONB,
    ADD COLUMN IF NOT EXISTS wording_sections JSONB;

COMMIT;
