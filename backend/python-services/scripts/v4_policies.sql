-- ---------------------------------------------------------------------------
-- Row-level security policies, re-pointed at the v4 tenancy columns.
--
-- The database carries 58 `tenant_isolation` policies keyed on `tenant_id`.
-- Two of those tables — party and program — have their old `tenant_id` dropped
-- by the v4 migration, because the model names the column `party_tenant_id` /
-- `program_tenant_id` instead. Postgres therefore refuses to drop the column
-- while the policy reads it, which is what stopped the first run.
--
-- Re-pointing them is NOT cosmetic. The ORM already writes the v4 columns, so
-- a policy still reading the old `tenant_id` would test a column nothing fills:
-- every new row would have NULL there and the `OR tenant_id IS NULL` arm would
-- make it visible to EVERY tenant. Leaving the old column in place to keep the
-- policy compiling is the one option that silently removes the isolation.
--
-- The other 56 policies are untouched: their tables either keep a plain
-- `tenant_id` (the operational tables) or were never part of this migration.
-- ---------------------------------------------------------------------------

-- party ----------------------------------------------------------------------
DROP POLICY IF EXISTS tenant_isolation ON party;
CREATE POLICY tenant_isolation ON party
    FOR ALL
    USING (party_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint
           OR party_tenant_id IS NULL)
    WITH CHECK (party_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint
                OR party_tenant_id IS NULL);

-- A broker is visible to a carrier it has been put on a programme with. Reads
-- program_broker and program, both of which also moved to v4 column names.
DROP POLICY IF EXISTS broker_visible_via_programme ON party;
CREATE POLICY broker_visible_via_programme ON party
    FOR SELECT
    USING (
        party_type = 'broker'::party_type_e
        AND EXISTS (
            SELECT 1
              FROM program_broker pb
              JOIN program pr ON pr.program_id = pb.program_broker_program_id
             WHERE pb.program_broker_party_id = party.party_id
               AND pr.program_tenant_id
                   = NULLIF(current_setting('app.tenant_id', true), '')::bigint
        )
    );

-- program --------------------------------------------------------------------
DROP POLICY IF EXISTS tenant_isolation ON program;
CREATE POLICY tenant_isolation ON program
    FOR ALL
    USING (program_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint
           OR program_tenant_id IS NULL)
    WITH CHECK (program_tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::bigint
                OR program_tenant_id IS NULL);
