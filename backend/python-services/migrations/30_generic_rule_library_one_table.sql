-- ============================================================================
-- The rule library lives in ONE table: generic_rule_spec.
--
-- WHY
-- ---
-- The data-model migration (3 Sep) moved the rule library to the canonical
-- table `generic_rule_spec`, and the Rule Library screen (/rule-library) has
-- read and written that table ever since. Bordereau Setup did not follow: the
-- loader (generic_rule_library.load_generic_rules) kept reading the pre-v4
-- table `generic_rule_specification`. So:
--   * Kavachio admin's screen listed none of the platform rules that run;
--   * a rule a carrier admin added, edited or switched off on the screen never
--     reached a Bordereau Setup.
-- The loader now reads generic_rule_spec. This copies the rules that were only
-- in the old table across, so nothing that ran before stops running.
--
-- WHAT
-- ----
-- 1. Any row the screen created while the tables were split and that sits on
--    an id the old table also uses is moved above the old table's range.
--    Those rows never ran, and nothing points at their ids.
-- 2. Every old row is copied with its ID KEPT. The id is the rule's handle
--    downstream (a library rule is clause -id inside the pipeline, and the
--    AI answer cache is keyed on the rows), so keeping it keeps every
--    existing setup and cached answer pointing at the same rule.
-- 3. The id sequence is moved past both tables, so the next rule the screen
--    creates cannot land on a copied id.
--
-- The old table is left in place, unchanged. Nothing reads it any more.
--
-- Idempotent: safe to run more than once. A second run finds its own copies
-- (same id, same created_at) and does nothing, so a rule deleted on the
-- screen after the copy is not brought back.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    legacy_max  integer;
    shift       integer;
    copied      integer;
    top         integer;
BEGIN
    IF to_regclass('public.generic_rule_specification') IS NULL THEN
        RAISE NOTICE 'generic_rule_specification does not exist; nothing to copy.';
    ELSIF EXISTS (
        SELECT 1
          FROM generic_rule_spec s
          JOIN generic_rule_specification l
            ON l.id = s.generic_rule_id
           AND l.created_at IS NOT DISTINCT FROM s.created_at
    ) THEN
        RAISE NOTICE 'rule library already copied into generic_rule_spec; skipping.';
    ELSE
        SELECT COALESCE(MAX(id), 0) INTO legacy_max FROM generic_rule_specification;

        -- 1. Clear the old table's id range of screen-created rows.
        SELECT GREATEST(legacy_max, COALESCE(MAX(generic_rule_id), 0))
          INTO shift FROM generic_rule_spec;
        UPDATE generic_rule_spec s
           SET generic_rule_id = s.generic_rule_id + shift
         WHERE EXISTS (SELECT 1 FROM generic_rule_specification l
                        WHERE l.id = s.generic_rule_id);

        -- 2. Copy, ids kept. class_name is nullable in the old table and not in
        --    the new one; '' is an unknown class, which generates nothing —
        --    exactly what a NULL class did.
        INSERT INTO generic_rule_spec (
            generic_rule_id, generic_rule_name, generic_rule_severity,
            generic_rule_class_name, generic_rule_logic, is_generic,
            generic_rule_tenant_id, tenant_id, generic_rule_is_active,
            created_by, created_at, updated_at)
        SELECT id, rule_name, severity,
               COALESCE(class_name, ''), validation_logic, COALESCE(is_generic, TRUE),
               tenant_id, tenant_id, is_active,
               created_by, created_at, updated_at
          FROM generic_rule_specification
        ON CONFLICT (generic_rule_id) DO NOTHING;
        GET DIAGNOSTICS copied = ROW_COUNT;
        RAISE NOTICE 'copied % rule(s) into generic_rule_spec.', copied;
    END IF;

    -- 3. Next id goes past everything either table has used.
    SELECT GREATEST(
             COALESCE((SELECT MAX(generic_rule_id) FROM generic_rule_spec), 0),
             CASE WHEN to_regclass('public.generic_rule_specification') IS NULL THEN 0
                  ELSE (SELECT COALESCE(MAX(id), 0) FROM generic_rule_specification) END)
      INTO top;
    PERFORM setval(pg_get_serial_sequence('generic_rule_spec', 'generic_rule_id'),
                   GREATEST(top, 1), top > 0);
END $$;

COMMIT;
