-- 43 — decisions on the rows a treaty rule caught in a reinsurance bordereau.
--
-- Like the broker's exception resolution: each row a treaty rule catches is
-- decided one by one — APPROVE (the value the rule recommends), FIX (a value
-- entered; both kept in cdd_new_value) or DISMISS (kept as it is); EXCLUDE
-- leaves the row out of the statement. Fix & Validate prepares the statement
-- again with them. Kept per treaty and period by the exception's key, so a
-- rebuilt statement keeps what was decided. Only a Critical rule's UNDECIDED
-- rows stop the statement being sent.
BEGIN;
CREATE TABLE IF NOT EXISTS ceded_decision (
    cdd_id          SERIAL PRIMARY KEY,
    cdd_tenant_id   INTEGER,
    cdd_contract_id INTEGER NOT NULL REFERENCES contract (contract_id),
    cdd_period      VARCHAR NOT NULL,
    cdd_key         VARCHAR NOT NULL,
    cdd_rule_id     VARCHAR NOT NULL,
    cdd_policy      VARCHAR,
    cdd_value       VARCHAR,
    cdd_kind        VARCHAR NOT NULL,
    cdd_new_value   VARCHAR,
    cdd_reason      TEXT,
    cdd_decided_by  INTEGER,
    cdd_decided_at  TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_ceded_decision_key UNIQUE (cdd_contract_id, cdd_period, cdd_key)
);
-- Re-runnable on a database that took this file before Approve / Fix / Dismiss.
ALTER TABLE ceded_decision ADD COLUMN IF NOT EXISTS cdd_new_value VARCHAR;
ALTER TABLE ceded_decision DROP CONSTRAINT IF EXISTS ceded_decision_cdd_kind_check;
ALTER TABLE ceded_decision ADD CONSTRAINT ceded_decision_cdd_kind_check
    CHECK (cdd_kind IN ('approve', 'fix', 'dismiss', 'accept', 'exclude'));
CREATE INDEX IF NOT EXISTS ix_ceded_decision_cdd_contract_id ON ceded_decision (cdd_contract_id);
CREATE INDEX IF NOT EXISTS ix_ceded_decision_cdd_tenant_id ON ceded_decision (cdd_tenant_id);
COMMIT;
