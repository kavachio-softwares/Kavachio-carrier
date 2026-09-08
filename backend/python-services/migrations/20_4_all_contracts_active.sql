-- ============================================================================
-- Feature 7 follow-on — currency stops being stored.
--
-- WHY
-- ---
-- `status_ops` carried two unrelated ideas in one word:
--     approval  — was this contract accepted for use?   (a decision)
--     currency  — is this the newest version?           (a date comparison)
--
-- Currency is redundant: it is exactly resolve_as_of(today). Storing it created
-- a cache with no matching invalidation scope — the write cleared
-- is_current_version per PROGRAMME while contracts are actually scoped per
-- (programme, schedule, broker) — and it drifted: programme 1 ended up with
-- four rows simultaneously claiming to be current, and contract 94 reported
-- itself `active` seven months after its term expired.
--
-- It was also misleading. A `superseded` contract still governs every file
-- dated inside its own window — that is the whole of §7 — so the word never
-- meant "unusable". It meant "not the newest", which is a date comparison
-- dressed up as a status.
--
-- AFTER THIS MIGRATION
-- --------------------
--   status_ops  = approval only: drafted / active / rejected / failed
--   "which version is current?"  -> contract_effective_from/to vs CURRENT_DATE
--   "which version governs THIS FILE?" -> contract_asof.resolve_as_of(txn date)
--
-- Every approved version is `active`, meaning approved and usable. Nothing is
-- lost: the version chain lives in the date windows, and system time
-- (valid_from/valid_until) still records what was believed when.
--
-- Run AFTER the code change — the readers must already be resolving currency by
-- date, or a screen asking for "the active contract" will get several.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- 1. Every superseded contract becomes active. It was always usable; the flag
--    only ever said a newer one existed, which the date windows already say.
UPDATE contract
SET    status_ops = 'active'
WHERE  status_ops = 'superseded';

-- 2. Clear the drifted currency cache. Nothing reads it for contracts any more
--    (the column is left in place for now — dropping it is a separate,
--    irreversible step, and it is still load-bearing on the SCD-2 DATA tables
--    policy / coverage / premium_transaction / claim, which are untouched here).
UPDATE contract
SET    is_current_version = TRUE
WHERE  is_current_version IS FALSE;

COMMIT;
