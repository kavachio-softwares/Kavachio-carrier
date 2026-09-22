-- ============================================================================
-- Record which broker SENT each policy.
--
-- WHY
-- ---
-- A policy was tied to a contract by programme and effective date alone. On a
-- programme several brokers share, a contract the carrier holds for the whole
-- programme (no broker on it) collected every broker's policies, and each
-- broker could list the others' — insured, premium, claims. The contract's own
-- broker (policy_contract_broker_party_id) cannot tell them apart, because that
-- contract has none.
--
-- WHAT
-- ----
-- One nullable column: the broker whose bordereau run loaded the policy. The
-- loader fills it from the run (output_exports.broker_party_id), and the
-- carrier-scoped policy endpoints show a broker only the policies it sent.
-- Existing rows stay NULL ("not recorded"): the carrier still sees them, no
-- broker does. No rows change.
--
-- The app adds this column itself at start-up (db.init_db adds every
-- canonical column a table lacks) unless KAVACHIO_RLS is on, in which case
-- run this file.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE policy
    ADD COLUMN IF NOT EXISTS policy_submitting_broker_party_id INTEGER;

COMMIT;
