-- 34 — Which CONTRACT a received file is for.
--
-- Process Bordereau asks a person three things: programme, contract and
-- reporting period. Files that arrive by API, email or SFTP now answer the
-- same three (intake_service.identify), and the contract is kept on the file:
--
--   * the run checks the file against THAT contract's terms, and
--   * a file for the same programme + contract + period — whichever way it
--     came in — is the next version of the same submission.
--
-- Additive and safe to re-run. Backfill: a file that has already been run
-- takes the contract its run was checked against.

ALTER TABLE file_arrival ADD COLUMN IF NOT EXISTS contract_id BIGINT;

UPDATE file_arrival a
   SET contract_id = e.contract_id
  FROM output_exports e
 WHERE e.id = a.run_export_id
   AND a.contract_id IS NULL
   AND e.contract_id IS NOT NULL;

-- "The earlier file for this broker, programme, contract and period" — the
-- question every arriving file asks to find its submission.
CREATE INDEX IF NOT EXISTS ix_file_arrival_period_contract
    ON file_arrival (tenant_id, matched_broker_party_id, reporting_period, contract_id)
 WHERE submission_ref IS NOT NULL;
