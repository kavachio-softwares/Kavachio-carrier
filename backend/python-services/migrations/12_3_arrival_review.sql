-- ============================================================================
-- Feature 12.3 — quarantine bad files for review.
--
-- The listing screen has always been able to SHOW a held file. Nothing could
-- resolve one: `file_arrival` records what arrived and what the machine
-- decided, and had no column anywhere for what a PERSON decided. So the
-- release and discard buttons were built disabled, and a held file stayed held
-- for ever.
--
-- These five columns are that missing half. Every one is nullable, so existing
-- rows are untouched and mean exactly what they meant before: NULL resolution
-- is "nobody has looked at this yet".
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- ── 1. What a person decided ────────────────────────────────────────────────
-- released  — "this is fine, load it". The file goes on to processing.
-- discarded — "ignore this". It stays on the record, marked dealt with.
-- Deliberately NOT a deletion: the whole point of quarantine is that the
-- decision is auditable months later, when somebody asks why a month is short.
ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS resolution           TEXT,
    ADD COLUMN IF NOT EXISTS resolved_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resolved_by_user_id  BIGINT,
    ADD COLUMN IF NOT EXISTS resolution_note      TEXT;

ALTER TABLE file_arrival
    DROP CONSTRAINT IF EXISTS file_arrival_resolution_check;

ALTER TABLE file_arrival
    ADD CONSTRAINT file_arrival_resolution_check
    CHECK (resolution IS NULL OR resolution IN ('released', 'discarded'));


-- ── 2. Retention ────────────────────────────────────────────────────────────
-- A refused file is kept so somebody can look at it, not for ever. These are
-- broker files full of policyholder data; holding them indefinitely is a
-- liability, not thoroughness. When the bytes are purged this is stamped and
-- blob_ref cleared — the ROW survives, so "a file arrived on the 5th and was
-- refused because X" is still answerable long after the file itself is gone.
ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS bytes_purged_at TIMESTAMPTZ;


-- ── 3. An accepted file MAY carry the reason it was held ────────────────────
-- The original constraint reads "outcome <> 'accepted' OR turned_away_reason
-- IS NULL", and it was right: a file that sailed through every check has no
-- refusal reason to carry.
--
-- Releasing breaks that assumption, because a released file did NOT sail
-- through. It was held, and a person overruled the check. Keeping the reason is
-- the entire point — "released despite: there is no live contract yet" is the
-- record somebody will want in three months, and the drawer reads that same
-- text to show WHICH check had to be overruled. Blanking it to satisfy the
-- constraint would throw away the only evidence that a decision was made.
--
-- So the exception is narrow and explicit: an accepted row may carry a reason
-- only when it was released by hand. A file that genuinely passed still cannot.
ALTER TABLE file_arrival
    DROP CONSTRAINT IF EXISTS file_arrival_check;

ALTER TABLE file_arrival
    ADD CONSTRAINT file_arrival_check
--
-- COALESCE, not a bare `resolution = 'released'`. A CHECK passes when it
-- evaluates to NULL, and `NULL = 'released'` is NULL — so the bare comparison
-- would let ANY accepted row carry a reason as long as resolution was unset,
-- quietly removing the guarantee this constraint exists for.
    CHECK (outcome <> 'accepted'
           OR turned_away_reason IS NULL
           OR COALESCE(resolution, '') = 'released');


-- ── 4. The review queue's own index ─────────────────────────────────────────
-- The screen's first question is always "what is waiting on me?" — held rows
-- for this tenant, oldest first. Without this that is a full scan of every file
-- that ever arrived.
CREATE INDEX IF NOT EXISTS ix_file_arrival_review
    ON file_arrival (tenant_id, outcome, received_at)
    WHERE resolution IS NULL;

COMMIT;
