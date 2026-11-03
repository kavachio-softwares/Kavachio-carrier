-- ============================================================================
-- Feature 7 — Prior Period Files: the overlap guard.
--
-- SPLIT FROM 20_1 ON PURPOSE. This one can FAIL, and a failure is a finding
-- rather than an accident: it means two versions of the same contract both
-- claim the same date, so "the version in force on 12-Mar-2024" has two answers
-- and §7.1 cannot be satisfied for that period. Run 20_1, run
-- scripts/verify_contract_asof.py --check-overlaps, fix what it reports, then
-- run this. Installing it is the moment the invariant becomes enforced rather
-- than merely intended.
--
-- WHY A DATABASE CONSTRAINT AND NOT AN APPLICATION CHECK
-- -----------------------------------------------------
-- With this installed, contract_asof.resolve_as_of can return `.scalar()` over
-- a query with no ORDER BY and no LIMIT, because the database guarantees at
-- most one row matches. Without it the resolver would need a tie-break rule,
-- and any tie-break rule is a silent, arbitrary choice between two contract
-- versions — precisely the failure §7 exists to prevent. The constraint is what
-- lets the resolver have no opinion.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- Needed to mix scalar equality with a range overlap in one EXCLUDE.
CREATE EXTENSION IF NOT EXISTS btree_gist;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'contract_no_overlap'
    ) THEN
        ALTER TABLE contract ADD CONSTRAINT contract_no_overlap
        -- COALESCE on both nullable grouping columns: NULL = NULL is NULL, so a
        -- bare `WITH =` would let two NULL-schedule versions overlap freely —
        -- and NULL schedule_key is the legacy default, i.e. the common case.
        EXCLUDE USING gist (
            contract_program_id                     WITH =,
            COALESCE(schedule_key, '')              WITH =,
            COALESCE(contract_broker_party_id, 0)   WITH =,
            daterange(contract_effective_from,
                      contract_effective_to, '[)')  WITH &&
        )
        -- Rows that predate the backfill carry no dates at all. They are not in
        -- conflict with anything; they are simply not yet dated, and excluding
        -- them here keeps the constraint installable on a partially migrated
        -- table instead of demanding a full backfill first.
        WHERE (contract_effective_from IS NOT NULL);
    END IF;
END $$;

COMMIT;
