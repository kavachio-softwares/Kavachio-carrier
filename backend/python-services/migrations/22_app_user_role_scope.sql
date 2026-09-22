-- ============================================================================
-- Put the two app_user checks back: which roles exist, and which organisation
-- each role belongs to.
--
-- WHY
-- ---
-- Section 19 of the carrier-flow migration created both:
--   chk_app_user_role   the role is one of the four seats;
--   chk_app_user_scope  a seat carries exactly the column its side needs —
--                       Kavachio neither, a carrier a tenant, a broker seat a
--                       broker party. Never both.
-- The shared database lost them (found 17 Sep 2026, most likely in the
-- entity-prefixed column rename), while the code kept saying "the database
-- refuses this". It did not: PUT /users stored any role it was given,
-- kavachio_admin included, and nothing underneath stopped it.
--
-- WHAT
-- ----
-- Both checks, written against whichever column names this database has
-- (user_role / user_tenant_id / user_broker_party_id after the rename;
-- role / tenant_id / broker_party_id before it).
--
-- NOT VALID: existing rows are not checked, only rows inserted or updated from
-- now on. At the time of writing exactly one row breaks the scope rule — user
-- 5, a carrier admin with no organisation — and this migration does not change
-- data. Find such rows with the query at the bottom; once they are fixed,
-- `ALTER TABLE app_user VALIDATE CONSTRAINT chk_app_user_scope;` makes the
-- check cover them too.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    c_role   text;
    c_tenant text;
    c_broker text;
BEGIN
    SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
                             WHERE table_name = 'app_user' AND column_name = 'user_role')
                THEN 'user_role' ELSE 'role' END INTO c_role;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
                             WHERE table_name = 'app_user' AND column_name = 'user_tenant_id')
                THEN 'user_tenant_id' ELSE 'tenant_id' END INTO c_tenant;
    SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns
                             WHERE table_name = 'app_user' AND column_name = 'user_broker_party_id')
                THEN 'user_broker_party_id' ELSE 'broker_party_id' END INTO c_broker;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'chk_app_user_role'
                     AND conrelid = 'app_user'::regclass) THEN
        EXECUTE format(
            'ALTER TABLE app_user ADD CONSTRAINT chk_app_user_role CHECK '
            '(%I IN (''kavachio_admin'', ''carrier_admin'', ''broker_admin'', ''operator'')) '
            'NOT VALID', c_role);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'chk_app_user_scope'
                     AND conrelid = 'app_user'::regclass) THEN
        EXECUTE format(
            'ALTER TABLE app_user ADD CONSTRAINT chk_app_user_scope CHECK ('
            '   (%1$I = ''kavachio_admin'' AND %2$I IS NULL     AND %3$I IS NULL)'
            ' OR (%1$I = ''carrier_admin''  AND %2$I IS NOT NULL AND %3$I IS NULL)'
            ' OR (%1$I IN (''broker_admin'', ''operator'')'
            '                             AND %2$I IS NULL     AND %3$I IS NOT NULL)'
            ') NOT VALID', c_role, c_tenant, c_broker);
    END IF;
END $$;

COMMIT;

-- Rows the scope check would refuse (post-rename names):
--   SELECT user_id, user_email, user_role, user_tenant_id, user_broker_party_id
--     FROM app_user
--    WHERE NOT (   (user_role = 'kavachio_admin' AND user_tenant_id IS NULL AND user_broker_party_id IS NULL)
--               OR (user_role = 'carrier_admin'  AND user_tenant_id IS NOT NULL AND user_broker_party_id IS NULL)
--               OR (user_role IN ('broker_admin','operator') AND user_tenant_id IS NULL AND user_broker_party_id IS NOT NULL));
