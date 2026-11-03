-- ============================================================================
-- Feature 7 — Prior Period Files.
--
-- §7.1 "Resolve each transaction to the contract version in force on its
--       transaction date"
-- §7.2 "Route late and corrected files to that version rather than the current
--       one"
--
-- WHY NEW COLUMNS AND NOT valid_from/valid_until
-- ----------------------------------------------
-- `contract` already carries Type-2 SCD columns, but they record SYSTEM time —
-- db_persister closes a superseded version with `valid_until = now()`, i.e.
-- "the date we stopped believing this". §7 needs BUSINESS time: "the date this
-- stopped applying". The two differ whenever an endorsement is backdated, which
-- in delegated authority is normal rather than exceptional. Overloading
-- valid_until would silently give the wrong answer for every backdated
-- endorsement, so business time gets its own pair of columns and the SCD
-- columns keep meaning exactly what they meant before.
--
-- The result is a bitemporal contract table:
--   business time  (contract_effective_from/to) → which rules applied
--   system time    (valid_from/valid_until)     → what we knew, and when
-- An auditor asks both.
--
-- NO LINEAGE KEY COLUMN
-- ---------------------
-- An earlier draft stored a hashed "lineage key" grouping the versions of one
-- logical contract. It is deliberately absent: the grouping is already fully
-- expressed by (contract_program_id, schedule_key, contract_broker_party_id),
-- and a stored copy can drift out of step the moment one of those three is
-- edited. Resolution and the overlap constraint both match on the three columns
-- directly, so there is nothing to keep in sync.
--
-- Every column is nullable and nothing reads them until CONTRACT_ASOF_ENABLED
-- is switched on, so applying this migration changes no behaviour.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- ── 1. Business-effective term of THIS contract version ─────────────────────
-- contract_effective_to is EXCLUSIVE: a version runs
--     contract_effective_from <= txn_date < contract_effective_to
-- Half-open, so consecutive versions can share a boundary date without both
-- matching it. NULL means open-ended — the version currently in force.
--
-- These are NOT contract_inception_date/contract_expiry_date. Inception is the
-- term of the whole agreement; these are the slice of that term for which THIS
-- version's wording applied. For an unamended contract the two coincide, which
-- is exactly what the backfill below relies on.
ALTER TABLE contract
    ADD COLUMN IF NOT EXISTS contract_effective_from DATE,
    ADD COLUMN IF NOT EXISTS contract_effective_to   DATE,
    ADD COLUMN IF NOT EXISTS contract_version_label  TEXT;

-- ── 2. schedule_key safety net ──────────────────────────────────────────────
-- Part of the resolution key below. It is created by 11_contract_schedule_key
-- and mapped on the ORM, but a deployment that skipped that migration would
-- fail at step 4 rather than here, with a much less obvious error.
ALTER TABLE contract
    ADD COLUMN IF NOT EXISTS schedule_key TEXT;

-- ── 3. The as-of lookup index ───────────────────────────────────────────────
-- Mirrors the resolver's WHERE clause exactly (contract_asof.resolve_as_of):
-- equality on the three grouping columns, then the date range.
CREATE INDEX IF NOT EXISTS ix_contract_asof
    ON contract (contract_program_id, schedule_key, contract_broker_party_id,
                 contract_effective_from, contract_effective_to);

-- ── 4. Backfill ─────────────────────────────────────────────────────────────
-- Every existing contract becomes a single version covering its own term. With
-- one contract live this is the whole timeline and it is exactly right; where
-- several versions already exist it makes each cover its own inception→expiry,
-- which is right for renewals (they do not overlap) and needs a human pass for
-- mid-term endorsements (which do — 20_2 will refuse to install until they are
-- resolved, which is the point of running it second).
--
-- COALESCE on the upper bound: a contract with no expiry is open-ended, and
-- NULL is how the resolver spells that.
UPDATE contract
SET    contract_effective_from = contract_inception_date,
       contract_effective_to   = contract_expiry_date,
       contract_version_label  = COALESCE(contract_version_label, 'Original')
WHERE  contract_effective_from IS NULL
  AND  contract_inception_date IS NOT NULL;

COMMIT;
