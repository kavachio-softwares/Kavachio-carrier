-- 45 — a treaty's calculation basis, held with the dates it applies from
-- (27.1, 27.2).
--
-- The structure holds each term's agreed value, from inception. Each row here
-- is a CHANGE to one term from a date: cession %, a surplus's lines, a layer's
-- attachment or limit, the ceding commission — and the profit and
-- sliding-scale commission terms, whose figures are each treaty's own (JSON).
-- A statement is worked out with the terms in force on its period's first day.
-- Removed, never deleted, so a figure worked out with one stays traceable.
BEGIN;
CREATE TABLE IF NOT EXISTS treaty_term (
    tt_id             SERIAL PRIMARY KEY,
    tt_tenant_id      INTEGER,
    tt_contract_id    INTEGER NOT NULL REFERENCES contract (contract_id),
    tt_term           VARCHAR NOT NULL,
    -- None: the treaty's own term; else the layer's position.
    tt_layer_index    INTEGER,
    tt_value          JSON NOT NULL,
    tt_effective_from DATE NOT NULL,
    tt_note           TEXT,
    tt_source         VARCHAR NOT NULL DEFAULT 'manual',
    -- The endorsement (contract_document) a change was made by, when it was.
    tt_document_id    INTEGER,
    tt_created_by     INTEGER,
    tt_created_by_name VARCHAR,
    tt_created_at     TIMESTAMPTZ DEFAULT now(),
    tt_removed_at     TIMESTAMPTZ,
    tt_removed_by     INTEGER
);
ALTER TABLE treaty_term ADD COLUMN IF NOT EXISTS tt_document_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_treaty_term_contract ON treaty_term (tt_contract_id);
CREATE INDEX IF NOT EXISTS ix_treaty_term_tenant ON treaty_term (tt_tenant_id);
COMMIT;
