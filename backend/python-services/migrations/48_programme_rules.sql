-- ============================================================================
-- 48 — Programme Rules: checks the carrier sets once for a whole programme,
-- for all or chosen broker contracts and/or reinsurance treaties on it, that
-- start checking for a broker or reinsurer once that party approves.
--
-- WHAT
-- ----
--   programme_rule            the rule (a data-model field, a condition, a
--                             value, a severity), the contracts it covers, who
--                             approves it, its status and version. Editing a
--                             sent rule makes it a draft of the next version.
--   programme_rule_approval   one row per approver (broker or reinsurer party)
--                             per version: pending | approved |
--                             changes_requested, with their note.
--
-- WHY NOT validation_rule: a row there with no contract is read by
-- validation_routes as the carrier's default for EVERY contract, before anyone
-- approved it. contract_approval and reinsurer_approval each belong to one
-- contract, and a programme rule is answered once per party for many.
--
-- NOT BUILT BY init_db: these tables are on their own model registry
-- (programme_rules.RuleBase), so a person applies this file, deliberately,
-- after a backup and a test on a local copy (CLAUDE.md). Until it is applied
-- the screens say Programme Rules are not set up on that database.
--
-- NUMBERING: 48 is the next number after 47_executive_report.sql on
-- development-phase-2; the team has not yet agreed a scheme (CLAUDE.md).
--
-- Idempotent: safe to run more than once.
-- ============================================================================

CREATE TABLE IF NOT EXISTS programme_rule (
    pr_id                   SERIAL PRIMARY KEY,
    pr_tenant_id            INTEGER      NOT NULL,
    pr_program_id           INTEGER      NOT NULL REFERENCES program (program_id),
    pr_name                 VARCHAR(200) NOT NULL,
    pr_description          TEXT,
    pr_field                VARCHAR(120) NOT NULL,
    pr_operator             VARCHAR(20)  NOT NULL
        CHECK (pr_operator IN ('lte', 'gte', 'eq', 'in', 'not_in', 'lte_aggregate', 'required')),
    pr_operand              JSON,
    pr_severity             VARCHAR(10)  NOT NULL DEFAULT 'warning'
        CHECK (pr_severity IN ('critical', 'warning', 'info')),
    pr_broker_scope         VARCHAR(10)  NOT NULL DEFAULT 'all'
        CHECK (pr_broker_scope IN ('none', 'all', 'selected')),
    pr_broker_contract_ids  JSON,
    pr_treaty_scope         VARCHAR(10)  NOT NULL DEFAULT 'none'
        CHECK (pr_treaty_scope IN ('none', 'all', 'selected')),
    pr_treaty_contract_ids  JSON,
    pr_approval_from        VARCHAR(10)  NOT NULL DEFAULT 'broker'
        CHECK (pr_approval_from IN ('broker', 'reinsurer', 'both')),
    pr_status               VARCHAR(12)  NOT NULL DEFAULT 'draft'
        CHECK (pr_status IN ('draft', 'sent', 'retired')),
    pr_version              INTEGER      NOT NULL DEFAULT 1,
    pr_created_by_user_id   INTEGER,
    pr_created_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    pr_updated_at           TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    pr_sent_at              TIMESTAMP WITHOUT TIME ZONE
);
CREATE INDEX IF NOT EXISTS ix_programme_rule_tenant  ON programme_rule (pr_tenant_id);
CREATE INDEX IF NOT EXISTS ix_programme_rule_program ON programme_rule (pr_program_id);

CREATE TABLE IF NOT EXISTS programme_rule_approval (
    pra_id                  SERIAL PRIMARY KEY,
    pra_rule_id             INTEGER      NOT NULL REFERENCES programme_rule (pr_id) ON DELETE CASCADE,
    pra_tenant_id           INTEGER      NOT NULL,
    pra_version             INTEGER      NOT NULL,
    pra_party_kind          VARCHAR(10)  NOT NULL CHECK (pra_party_kind IN ('broker', 'reinsurer')),
    pra_party_id            INTEGER      NOT NULL REFERENCES party (party_id),
    pra_status              VARCHAR(20)  NOT NULL DEFAULT 'pending'
        CHECK (pra_status IN ('pending', 'approved', 'changes_requested')),
    pra_note                TEXT,
    pra_acted_by_user_id    INTEGER,
    pra_acted_by_name       VARCHAR(200),
    pra_acted_at            TIMESTAMP WITHOUT TIME ZONE,
    pra_created_at          TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_programme_rule_approval
    ON programme_rule_approval (pra_rule_id, pra_version, pra_party_kind, pra_party_id);
CREATE INDEX IF NOT EXISTS ix_programme_rule_approval_rule   ON programme_rule_approval (pra_rule_id);
CREATE INDEX IF NOT EXISTS ix_programme_rule_approval_tenant ON programme_rule_approval (pra_tenant_id);
CREATE INDEX IF NOT EXISTS ix_programme_rule_approval_party  ON programme_rule_approval (pra_party_id);
