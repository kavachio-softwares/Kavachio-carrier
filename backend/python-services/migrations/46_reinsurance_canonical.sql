-- 46 — a treaty's reinsurance bordereau in the canonical data model
-- (ceded_canonical.py).
--
-- `cession` holds one row per reinsurance statement and reinsurer: share,
-- period, ceded premium, ceded loss, recoveries due. A statement also states
-- the ceding commission and the net premium due, which it had nowhere to go.
-- Until this is applied the app writes everything else and skips these two.
--
-- `reinsurance_arrangement.contract_id` links the arrangement to its treaty.
-- Databases restored from the shared schema already have it; one built from
-- the code's data model (canonical.py) does not.
--
-- Additive only: nothing is dropped, renamed or rewritten. Apply by hand,
-- after a backup, on a local copy first (CLAUDE.md, Database — precautions).
--
-- NOT in data_model.py on purpose: init_db adds every data-model column a
-- canonical table is missing (db.py, "ALTER every missing canonical column"),
-- so listing them there would add them to whatever database the app starts
-- against — without this migration, and without anyone deciding to. Add the
-- two cession fields to data_model.py once this has been applied everywhere.
BEGIN;
ALTER TABLE cession ADD COLUMN IF NOT EXISTS cession_ceding_commission_amount NUMERIC;
ALTER TABLE cession ADD COLUMN IF NOT EXISTS cession_net_premium_amount NUMERIC;
ALTER TABLE reinsurance_arrangement ADD COLUMN IF NOT EXISTS contract_id INTEGER;
-- Each row is found again by its key (treaty:<id>, ceded:<statement>:<reinsurer>).
CREATE INDEX IF NOT EXISTS ix_cession_row_key ON cession (row_key);
CREATE INDEX IF NOT EXISTS ix_reinsurance_arrangement_row_key
    ON reinsurance_arrangement (row_key);
COMMIT;
