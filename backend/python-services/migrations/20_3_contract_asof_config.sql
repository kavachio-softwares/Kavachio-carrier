-- ============================================================================
-- Feature 7 — Prior Period Files: per-setup configuration.
--
-- WHAT IS CONFIGURABLE AND WHY
-- ----------------------------
-- §7.1 says "resolve each transaction to the contract version in force on its
-- transaction date". Which column IS the transaction date is not a fact about
-- the software — it is a fact about the contract and the file type, and it
-- differs per programme:
--
--   premium / risk bordereau  → policy inception (the contract in force when
--                               the risk ATTACHED governs it for its full term)
--   claims bordereau          → date of loss (the cover in force when the loss
--                               occurred responds)
--   claims movement           → usually still date of loss, not movement date
--
-- Hard-coding one column name would silently produce plausible-looking wrong
-- money on every programme that uses a different one, so the choice is stored
-- per setup and per pipeline, with auto-detection only as a fallback.
--
-- asof_config carries the rest of the knobs as JSON so adding one later needs
-- no migration:
--   {"date_strategy": "min"|"max"|"mode",   -- which row's date governs the run
--    "on_unresolved": "pin"|"skip",         -- no version covers the date
--    "enabled": true|false}                 -- per-setup override of the global flag
--
-- Both columns are nullable; NULL everywhere means "fall back to the global
-- defaults in contract_asof_config.py", which in turn default to the
-- pre-Feature-7 behaviour. Applying this migration changes nothing on its own.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- The setup (Bordereau Setup screen) — where an operator configures a
-- programme's file shape, so also where the governing date belongs.
ALTER TABLE direct_format
    ADD COLUMN IF NOT EXISTS governing_date_field TEXT,
    ADD COLUMN IF NOT EXISTS asof_config          JSONB;

-- The pipeline overrides its setup when both are set: a pipeline is the thing
-- that actually runs, and a programme can carry more than one.
ALTER TABLE pipeline
    ADD COLUMN IF NOT EXISTS governing_date_field TEXT,
    ADD COLUMN IF NOT EXISTS asof_config          JSONB;

COMMIT;
