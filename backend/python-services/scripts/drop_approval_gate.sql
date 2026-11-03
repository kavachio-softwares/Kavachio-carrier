-- Drop the carrier's approval gate.
--
-- A broker could once bring a contract, and it could not be used until the
-- carrier approved it. That upload and the gate that policed it were removed
-- together: with nobody bringing a contract there is nothing to approve, and
-- with no gate there is nothing to hold an upload back. Every contract is now
-- the carrier's, and exists the moment it is raised.
--
-- WHAT THIS DROPS
--   the two triggers and their functions, which are the only things that still
--   write the gate. Nothing in the application reads it any more.
--
-- WHAT THIS DELIBERATELY DOES NOT DROP
--   contract_approval_status, contract_approved_by_id, contract_approved_at.
--   The schema is shared, so dropping a column is not this application's
--   decision to take alone. They are simply unmapped now (see db.py) — dead
--   weight, but dead weight nobody can revive by accident.
--
--   The contract_approval TABLE stays and is still written: it carries the
--   NEGOTIATION thread (sent_for_review, changes_requested, review_skipped,
--   terms_agreed), which the contract record reads. Only the gate's own rows
--   are removed below.

BEGIN;

DROP TRIGGER IF EXISTS trg_set_contract_approval   ON contract;
DROP TRIGGER IF EXISTS trg_enforce_approval_authority ON contract_approval;

DROP FUNCTION IF EXISTS public.set_contract_approval();
DROP FUNCTION IF EXISTS public.enforce_approval_authority();

-- The gate's own history. 'submitted' goes with them: it recorded a broker
-- handing a contract over for a decision, which is the act that no longer
-- exists. Every other action is the negotiation and is left alone.
DELETE FROM contract_approval
 WHERE approval_action IN ('approved', 'rejected', 'submitted');

COMMIT;
