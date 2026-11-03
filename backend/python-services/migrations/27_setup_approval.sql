-- ============================================================================
-- Bordereau Setup approval: a carrier user's setup waits for the carrier admin.
--
-- WHY
-- ---
-- Everyone at a carrier holds the same `carrier_admin` role; the two seats are
-- told apart by the organisation's owner pointer (migration 18). Until now
-- both could put a setup live, and the moment one did, the broker could upload
-- against it. The carrier admin had no say and no sight of it.
--
-- The rule now: a setup a carrier USER builds goes to the carrier admin, who
-- sees the whole chain — programme, broker, contract, BDX template — and
-- approves it once. Nothing reaches the broker before that. A setup the
-- carrier ADMIN builds goes live as it always did; their own act IS the
-- approval, so there is nobody left to ask.
--
-- WHAT
-- ----
-- 1. `setup_approval` — every decision ever made on a setup. The pipeline row
--    holds the CURRENT state; this holds how it got there, so "why was this
--    rejected in June?" stays answerable after the fact. Deliberately its own
--    table rather than a widening of `contract_approval`, whose
--    `approval_contract_id` is NOT NULL: a setup is not a contract, and making
--    that column nullable would weaken the one table that is sure of itself.
--
-- 2. Two columns on `pipeline` naming who sent it up and when — what the
--    carrier admin's queue is ordered by, and who to tell when it is decided.
--
-- `pipeline.status` gains one value, `pending_approval`, between `draft` and
-- `active`. No DDL: status is already a free-text column. Nearly every reader
-- asks `status = 'active'`, so a pending setup is invisible to them by
-- construction — including setup_scope._live, which is the single gate behind
-- Process Bordereau, the broker's readiness check, the template lookup and the
-- template download.
--
-- NO BACKFILL. Every setup that is live today stays live, whoever built it.
-- The new rule governs new work; retro-fitting it would take running setups
-- off the air to ask a question nobody was asked at the time.
--
-- The app adds all of this itself at start-up (db.init_db -> _ensure_column /
-- create_all) unless KAVACHIO_RLS is on, in which case run this file.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS setup_approval (
    approval_id        BIGSERIAL PRIMARY KEY,
    tenant_id          INTEGER     NOT NULL,
    pipeline_id        INTEGER     NOT NULL,
    -- submitted | approved | rejected
    action             VARCHAR     NOT NULL,
    acted_by_user_id   INTEGER     NOT NULL,
    acted_at           TIMESTAMP,
    note               TEXT,
    created_at         TIMESTAMP
);

-- The queue reads "every decision on this setup, newest first"; the audit
-- trail reads one carrier's. Both are covered here.
CREATE INDEX IF NOT EXISTS ix_setup_approval_pipeline_id
    ON setup_approval (pipeline_id);
CREATE INDEX IF NOT EXISTS ix_setup_approval_tenant_id
    ON setup_approval (tenant_id);

-- Who sent this setup up for approval, and when. NULL on every setup built
-- before this existed, and on every setup a carrier admin built for itself —
-- which never goes up for approval at all.
ALTER TABLE pipeline ADD COLUMN IF NOT EXISTS submitted_by_user_id INTEGER;
ALTER TABLE pipeline ADD COLUMN IF NOT EXISTS submitted_at         TIMESTAMP;

-- The carrier admin's queue: "what is waiting on me at this carrier".
CREATE INDEX IF NOT EXISTS ix_pipeline_tenant_status
    ON pipeline (tenant_id, status);

COMMIT;

-- ============================================================================
-- Rolling back
-- ============================================================================
-- Any setup left at 'pending_approval' would be invisible to every reader and
-- unreachable by any screen, so send them back where they came from FIRST:
--
--   UPDATE pipeline SET status = 'draft' WHERE status = 'pending_approval';
--
-- and only then drop the table and the columns.
-- ============================================================================
