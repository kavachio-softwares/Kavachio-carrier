-- Migration 40 — repair of migration 39 on a database where BOTH name pairs exist.
-- Moves the ITD history from bordereau_position / bordereau_position_event onto
-- bdx_sent_transaction / bdx_row_comparison, but only if the new pair is still
-- empty, carries nothing of its own, and has exactly the columns the old pair
-- will have once renamed. One transaction: any failed check undoes everything.
--   old + new present  -> repair (the shared DB today)
--   only new present   -> already repaired: only the missing indexes are added
--   only old present   -> stop: that is migration 39's job (run it, or restart the app)
--   neither present    -> nothing to do
--
-- Applied to the shared DB (192.168.2.11) on 16 Oct 2026 at 11:39 IST, after a
-- reviewed rehearsal and a live dry run (Migration_39_ITD_Table_Rename_Investigation.md).
-- Safe to run again anywhere: on an already-repaired database it only adds the
-- three ITD indexes if they are missing. Run with psql -v ON_ERROR_STOP=1.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = public;

DO $$
DECLARE
    n_pos bigint; n_ev bigint; m_pos text; m_ev text;
    n2 bigint; m2 text; s_pos bigint; s_ev bigint; c record;
    acl_pos text; acl_ev text; new_cols text; bad text;
BEGIN
    IF to_regclass('public.bordereau_position') IS NULL
       AND to_regclass('public.bordereau_position_event') IS NULL THEN
        RAISE NOTICE 'old ITD tables absent: nothing to move';
        RETURN;
    END IF;
    IF to_regclass('public.bdx_sent_transaction') IS NULL
       AND to_regclass('public.bdx_row_comparison') IS NULL THEN
        RAISE EXCEPTION 'only the old ITD tables exist: run migration 39 (or restart the app), not this repair';
    END IF;
    IF to_regclass('public.bordereau_position') IS NULL
       OR to_regclass('public.bordereau_position_event') IS NULL
       OR to_regclass('public.bdx_sent_transaction') IS NULL
       OR to_regclass('public.bdx_row_comparison') IS NULL THEN
        RAISE EXCEPTION 'unexpected state: not all four ITD tables exist; repair by hand';
    END IF;

    LOCK TABLE bordereau_position, bordereau_position_event,
               bdx_sent_transaction, bdx_row_comparison IN ACCESS EXCLUSIVE MODE;

    -- 1. The new pair is empty.
    SELECT count(*) INTO n2 FROM bdx_sent_transaction;
    IF n2 <> 0 THEN RAISE EXCEPTION 'bdx_sent_transaction holds % rows: stop', n2; END IF;
    SELECT count(*) INTO n2 FROM bdx_row_comparison;
    IF n2 <> 0 THEN RAISE EXCEPTION 'bdx_row_comparison holds % rows: stop', n2; END IF;

    -- 2. Nothing depends on the new pair or hangs off it that the DROP would lose.
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE confrelid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)) THEN
        RAISE EXCEPTION 'a foreign key points at the new ITD tables: stop';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
               WHERE d.refobjid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)
                 AND r.ev_class NOT IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)) THEN
        RAISE EXCEPTION 'a view or rule depends on the new ITD tables: stop';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_trigger
               WHERE tgrelid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)
                 AND NOT tgisinternal) THEN
        RAISE EXCEPTION 'a trigger sits on the new ITD tables: stop';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE oid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)
                                         AND (relrowsecurity OR relforcerowsecurity))
       OR EXISTS (SELECT 1 FROM pg_policy WHERE polrelid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)) THEN
        RAISE EXCEPTION 'row-level security is set on the new ITD tables: stop';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_publication_rel WHERE prrelid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)) THEN
        RAISE EXCEPTION 'the new ITD tables are in a publication: stop';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_statistic_ext WHERE stxrelid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)) THEN
        RAISE EXCEPTION 'extended statistics exist on the new ITD tables: stop';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_description WHERE classoid = 'pg_class'::regclass
               AND objoid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)) THEN
        RAISE EXCEPTION 'comments exist on the new ITD tables: stop';
    END IF;
    SELECT string_agg(conname, ', ') INTO bad FROM pg_constraint
     WHERE conrelid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)
       AND conname NOT IN ('bdx_sent_transaction_pkey', 'bdx_row_comparison_pkey');
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'unexpected constraints on the new ITD tables (%): stop', bad; END IF;
    SELECT string_agg(i.relname, ', ') INTO bad FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
     WHERE x.indrelid IN ('bdx_sent_transaction'::regclass, 'bdx_row_comparison'::regclass)
       AND i.relname NOT IN ('bdx_sent_transaction_pkey', 'uq_bdx_sent_transaction_feed_key', 'ix_bdx_sent_transaction_feed',
                             'bdx_row_comparison_pkey', 'ix_bdx_row_comparison_feed', 'ix_bdx_row_comparison_landing',
                             'ix_bdx_row_comparison_arrival', 'ix_bdx_row_comparison_sent');
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'unexpected indexes on the new ITD tables (%): stop', bad; END IF;

    -- 3. Same grants on tables and sequences as the old pair (nothing granted only on the new pair is lost).
    SELECT coalesce(relacl::text, '') INTO acl_pos FROM pg_class WHERE oid = 'bordereau_position'::regclass;
    SELECT coalesce(relacl::text, '') INTO acl_ev  FROM pg_class WHERE oid = 'bordereau_position_event'::regclass;
    IF (SELECT coalesce(relacl::text, '') FROM pg_class WHERE oid = 'bdx_sent_transaction'::regclass) <> acl_pos
       OR (SELECT coalesce(relacl::text, '') FROM pg_class WHERE oid = 'bdx_row_comparison'::regclass) <> acl_ev THEN
        RAISE EXCEPTION 'grants on the new ITD tables differ from the old ones: stop';
    END IF;
    IF (SELECT coalesce(relacl::text, '') FROM pg_class WHERE relname = 'bdx_sent_transaction_sent_id_seq' AND relkind = 'S')
         <> (SELECT coalesce(relacl::text, '') FROM pg_class WHERE relname = 'bordereau_position_position_id_seq' AND relkind = 'S')
       OR (SELECT coalesce(relacl::text, '') FROM pg_class WHERE relname = 'bdx_row_comparison_comparison_id_seq' AND relkind = 'S')
         <> (SELECT coalesce(relacl::text, '') FROM pg_class WHERE relname = 'bordereau_position_event_event_id_seq' AND relkind = 'S') THEN
        RAISE EXCEPTION 'grants on the new ITD sequences differ from the old ones: stop';
    END IF;

    -- 4. The new pair's columns (name, type, nullability) = the old pair's columns once renamed.
    SELECT string_agg(x, '; ') INTO bad FROM (
        (SELECT table_name || '.' || column_name || ' ' || data_type || ' ' || is_nullable AS x
           FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name IN ('bdx_sent_transaction', 'bdx_row_comparison')
         EXCEPT
         SELECT CASE table_name WHEN 'bordereau_position' THEN 'bdx_sent_transaction' ELSE 'bdx_row_comparison' END || '.' ||
                CASE WHEN table_name = 'bordereau_position' AND column_name LIKE 'position\_%' THEN 'sent_' || substr(column_name, 10)
                     WHEN table_name = 'bordereau_position_event' AND column_name = 'event_position_id' THEN 'comparison_sent_id'
                     WHEN table_name = 'bordereau_position_event' AND column_name LIKE 'event\_%' THEN 'comparison_' || substr(column_name, 7)
                     ELSE column_name END || ' ' || data_type || ' ' || is_nullable
           FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name IN ('bordereau_position', 'bordereau_position_event'))
        UNION ALL
        (SELECT CASE table_name WHEN 'bordereau_position' THEN 'bdx_sent_transaction' ELSE 'bdx_row_comparison' END || '.' ||
                CASE WHEN table_name = 'bordereau_position' AND column_name LIKE 'position\_%' THEN 'sent_' || substr(column_name, 10)
                     WHEN table_name = 'bordereau_position_event' AND column_name = 'event_position_id' THEN 'comparison_sent_id'
                     WHEN table_name = 'bordereau_position_event' AND column_name LIKE 'event\_%' THEN 'comparison_' || substr(column_name, 7)
                     ELSE column_name END || ' ' || data_type || ' ' || is_nullable
           FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name IN ('bordereau_position', 'bordereau_position_event')
         EXCEPT
         SELECT table_name || '.' || column_name || ' ' || data_type || ' ' || is_nullable
           FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name IN ('bdx_sent_transaction', 'bdx_row_comparison'))
    ) d;
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'column sets differ between the old and new ITD tables (%): stop', bad; END IF;
    SELECT string_agg(table_name || '.' || column_name || ' ' || data_type || ' ' || is_nullable, '; '
                      ORDER BY table_name, column_name) INTO new_cols
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name IN ('bdx_sent_transaction', 'bdx_row_comparison');

    -- 5. What must survive, measured before.
    SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY position_id), ''))
      INTO n_pos, m_pos FROM bordereau_position t;
    SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY event_id), ''))
      INTO n_ev, m_ev FROM bordereau_position_event t;
    SELECT last_value INTO s_pos FROM bordereau_position_position_id_seq;
    SELECT last_value INTO s_ev  FROM bordereau_position_event_event_id_seq;

    -- 6. Out with the empty pair (RESTRICT: refuse if anything depends on it).
    DROP TABLE bdx_row_comparison RESTRICT;
    DROP TABLE bdx_sent_transaction RESTRICT;

    -- 7. In with the real pair: tables, columns (same rules as migration 39).
    ALTER TABLE bordereau_position       RENAME TO bdx_sent_transaction;
    ALTER TABLE bordereau_position_event RENAME TO bdx_row_comparison;
    FOR c IN SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'bdx_sent_transaction'
               AND column_name LIKE 'position\_%' LOOP
        EXECUTE format('ALTER TABLE bdx_sent_transaction RENAME COLUMN %I TO %I',
                       c.column_name, 'sent_' || substr(c.column_name, 10));
    END LOOP;
    ALTER TABLE bdx_row_comparison RENAME COLUMN event_position_id TO comparison_sent_id;
    FOR c IN SELECT column_name FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'bdx_row_comparison'
               AND column_name LIKE 'event\_%' LOOP
        EXECUTE format('ALTER TABLE bdx_row_comparison RENAME COLUMN %I TO %I',
                       c.column_name, 'comparison_' || substr(c.column_name, 7));
    END LOOP;

    -- Indexes (a primary-key index rename renames its constraint too) and sequences.
    ALTER INDEX bordereau_position_pkey             RENAME TO bdx_sent_transaction_pkey;
    ALTER INDEX uq_bordereau_position_feed_key      RENAME TO uq_bdx_sent_transaction_feed_key;
    ALTER INDEX bordereau_position_event_pkey       RENAME TO bdx_row_comparison_pkey;
    ALTER INDEX ix_bordereau_position_event_feed    RENAME TO ix_bdx_row_comparison_feed;
    ALTER INDEX ix_bordereau_position_event_landing RENAME TO ix_bdx_row_comparison_landing;
    ALTER SEQUENCE bordereau_position_position_id_seq    RENAME TO bdx_sent_transaction_sent_id_seq;
    ALTER SEQUENCE bordereau_position_event_event_id_seq RENAME TO bdx_row_comparison_comparison_id_seq;

    -- 8. Postconditions: nothing lost, nothing changed, old names gone, same columns as the app's tables had.
    SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY sent_id), ''))
      INTO n2, m2 FROM bdx_sent_transaction t;
    IF n2 <> n_pos OR m2 <> m_pos THEN
        RAISE EXCEPTION 'sent history differs after rename (% vs % rows): undone', n2, n_pos;
    END IF;
    SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY comparison_id), ''))
      INTO n2, m2 FROM bdx_row_comparison t;
    IF n2 <> n_ev OR m2 <> m_ev THEN
        RAISE EXCEPTION 'comparison history differs after rename (% vs % rows): undone', n2, n_ev;
    END IF;
    SELECT last_value INTO n2 FROM bdx_sent_transaction_sent_id_seq;
    IF n2 <> s_pos THEN RAISE EXCEPTION 'sent id sequence moved: undone'; END IF;
    SELECT last_value INTO n2 FROM bdx_row_comparison_comparison_id_seq;
    IF n2 <> s_ev THEN RAISE EXCEPTION 'comparison id sequence moved: undone'; END IF;
    IF to_regclass('public.bordereau_position') IS NOT NULL
       OR to_regclass('public.bordereau_position_event') IS NOT NULL THEN
        RAISE EXCEPTION 'old ITD table names still present: undone';
    END IF;
    IF (SELECT string_agg(table_name || '.' || column_name || ' ' || data_type || ' ' || is_nullable, '; '
                          ORDER BY table_name, column_name)
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name IN ('bdx_sent_transaction', 'bdx_row_comparison')) <> new_cols THEN
        RAISE EXCEPTION 'columns after rename differ from the tables the app was using: undone';
    END IF;
    IF (SELECT coalesce(relacl::text, '') FROM pg_class WHERE oid = 'bdx_sent_transaction'::regclass) <> acl_pos
       OR (SELECT coalesce(relacl::text, '') FROM pg_class WHERE oid = 'bdx_row_comparison'::regclass) <> acl_ev THEN
        RAISE EXCEPTION 'grants changed: undone';
    END IF;
    RAISE NOTICE 'ITD history moved: % sent, % comparison rows', n_pos, n_ev;
END $$;

-- The indexes migration 38 meant to have (never built on the shared DB), only where the new pair exists.
DO $$
BEGIN
    IF to_regclass('public.bdx_sent_transaction') IS NOT NULL
       AND to_regclass('public.bordereau_position') IS NULL THEN
        CREATE INDEX IF NOT EXISTS ix_bdx_sent_transaction_feed
            ON bdx_sent_transaction (tenant_id, sent_program_id, sent_broker_party_id,
                                     sent_stream, sent_status);
    END IF;
    IF to_regclass('public.bdx_row_comparison') IS NOT NULL
       AND to_regclass('public.bordereau_position_event') IS NULL THEN
        CREATE INDEX IF NOT EXISTS ix_bdx_row_comparison_arrival
            ON bdx_row_comparison (comparison_file_arrival_id, comparison_landing_id);
        CREATE INDEX IF NOT EXISTS ix_bdx_row_comparison_sent
            ON bdx_row_comparison (comparison_sent_id, comparison_id);
    END IF;
END $$;

COMMIT;
