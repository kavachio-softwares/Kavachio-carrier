-- ============================================================================
-- Contract negotiation — what the broker actually asked to change.
--
-- `contract_approval` already records every decision on a contract: who acted,
-- when, and a free-text note. That is enough for a decision, which is a yes or
-- a no. It is not enough for a NEGOTIATION, which is a counter-proposal.
--
-- "The premium cap is too low" in a note leaves the carrier to work out which
-- field is meant, what number is wanted, and then to type it in — and leaves
-- nobody able to answer "what did they actually ask for in March?" without
-- reading prose. So a change request also carries the fields it is about and
-- the values being proposed:
--
--   [{"field": "premium_cap_amount", "current": "5000000",
--     "proposed": "7500000", "comment": "in line with last year's book"}]
--
-- The carrier can then see the request beside the current terms and apply it in
-- one move, and the thread stays answerable years later. The note survives
-- alongside it, because not every request is about a field — "we need to see
-- the schedule before agreeing" is a real thing to say and belongs in prose.
--
-- Nullable, with no backfill: the two existing rows are an approval and a
-- submission, neither of which proposed anything. Every read path treats an
-- absent value as "no fields named", which is what those rows mean.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE contract_approval
    ADD COLUMN IF NOT EXISTS approval_proposed_changes JSONB;

-- The negotiation thread for one contract, oldest first, is the only way this
-- table is ever read on the contract screen.
CREATE INDEX IF NOT EXISTS ix_contract_approval_contract_acted
    ON contract_approval (approval_contract_id, approval_acted_at);

COMMIT;
