-- ============================================================================
-- Template-aware contract rules.
--
-- WHY
-- ---
-- A rule is a contract clause bound to one output template's columns: the
-- same clause against a different template is a different rule, because it
-- names different columns. Until now a rule carried only its contract, so a
-- contract could hold ONE rule set — binding it to a second template was
-- refused (409), and a setup ran every rule its contract had.
--
-- WHAT
-- ----
-- validation_rule.output_template_id — the template a rule was written for.
--   NULL on every rule written before this: such a rule belongs to its
--   contract's original template (contract.output_template_id), which is what
--   it always meant. Read as COALESCE(rule, contract) — see rule_scope.py.
--
-- pipeline.rule_scope — which of its contracts' rule sets a setup runs, and
--   which single rules it has switched off, for THIS setup only:
--     {"template_ids": [202, 203], "excluded_rule_ids": [5]}
--   NULL on every existing setup: it runs its own template's rules, as before.
--
-- Nothing is backfilled, updated or deleted. Applying this migration changes
-- no behaviour on its own.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE validation_rule
    ADD COLUMN IF NOT EXISTS output_template_id INTEGER;

ALTER TABLE pipeline
    ADD COLUMN IF NOT EXISTS rule_scope JSONB;

COMMIT;
