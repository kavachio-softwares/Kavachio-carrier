-- 42 — a reinsurer signs the treaty on its private link.
--
-- The carrier can ask one person at the lead reinsurer to SIGN, not only to
-- view and approve: the link they already hold (or a fresh one) then carries a
-- signature box. Their signature is stored in contract_signature with method
-- 'link' — signed by them, after the emailed code proved they hold the
-- mailbox — instead of one the carrier types in on their behalf.
BEGIN;
ALTER TABLE reinsurer_access_link ADD COLUMN IF NOT EXISTS ral_sign_requested_at TIMESTAMPTZ;
ALTER TABLE reinsurer_access_link ADD COLUMN IF NOT EXISTS ral_sign_requested_by INTEGER;
-- A signature can now be 'link' as well as 'typed' and 'recorded'.
ALTER TABLE contract_signature
    DROP CONSTRAINT IF EXISTS contract_signature_contract_signature_method_check;
ALTER TABLE contract_signature
    ADD CONSTRAINT contract_signature_contract_signature_method_check
    CHECK (contract_signature_method IN ('typed', 'recorded', 'link'));
COMMIT;
