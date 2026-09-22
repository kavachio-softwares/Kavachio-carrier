-- ============================================================================
-- Check migration 23 (API Idempotency-Key per way in) on this database.
--
--   psql "<database url>" -f scripts/check_migration_23.sql
--
-- Part 1 only reads. Part 2 tries the rule for real INSIDE A TRANSACTION THAT
-- IS ALWAYS ROLLED BACK — nothing it inserts survives, whatever happens (an
-- error stops the script and the transaction is thrown away with it). The
-- only trace is a few arrival_id sequence numbers skipped.
--
-- Expected output after migration 23: one index, uq_file_arrival_idempotency_route,
-- the old uq_file_arrival_idempotency gone, then three PASS notices.
-- ============================================================================

\set ON_ERROR_STOP on

\echo '-- Part 1: which rule is in force'
SELECT indexname, indexdef
  FROM pg_indexes
 WHERE tablename = 'file_arrival' AND indexname LIKE 'uq_file_arrival_idem%';

SELECT count(*) AS arrivals, count(idempotency_key) AS with_a_key FROM file_arrival;

\echo '-- Part 2: the rule itself (rolled back)'
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    t  bigint;
    r1 bigint;
    r2 bigint;
    k  text := 'migration23-check-' || md5(clock_timestamp()::text);
BEGIN
    -- Any carrier with two ways in will do; nothing here is kept.
    SELECT tenant_id INTO t FROM intake_route
     GROUP BY tenant_id HAVING count(*) > 1 ORDER BY tenant_id LIMIT 1;
    IF t IS NULL THEN
        RAISE EXCEPTION 'no carrier has two ways in to test with';
    END IF;
    SELECT min(route_id), max(route_id) INTO r1, r2 FROM intake_route WHERE tenant_id = t;

    -- 1. The same key from two different ways in (two brokers): allowed.
    INSERT INTO file_arrival (tenant_id, route_id, filename, received_at, outcome,
                              idempotency_key, created_at)
    VALUES (t, r1, 'check-1.csv', now(), 'accepted', k, now()),
           (t, r2, 'check-2.csv', now(), 'accepted', k, now());
    RAISE NOTICE 'PASS 1: two ways in may use the same Idempotency-Key';

    -- 2. The same key twice on ONE way in: still refused.
    BEGIN
        INSERT INTO file_arrival (tenant_id, route_id, filename, received_at, outcome,
                                  idempotency_key, created_at)
        VALUES (t, r1, 'check-3.csv', now(), 'accepted', k, now());
        RAISE EXCEPTION 'FAIL 2: a key was accepted twice on one way in';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'PASS 2: a key used twice on one way in is refused';
    END;

    -- 3. A file with no way in (e-mail that matched no route): the same key
    --    twice is still refused — the COALESCE in the index.
    INSERT INTO file_arrival (tenant_id, route_id, filename, received_at, outcome,
                              idempotency_key, created_at)
    VALUES (t, NULL, 'check-4.csv', now(), 'accepted', k || '-mail', now());
    BEGIN
        INSERT INTO file_arrival (tenant_id, route_id, filename, received_at, outcome,
                                  idempotency_key, created_at)
        VALUES (t, NULL, 'check-5.csv', now(), 'accepted', k || '-mail', now());
        RAISE EXCEPTION 'FAIL 3: a mail key was accepted twice';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'PASS 3: a mail key used twice is refused';
    END;
END $$;

ROLLBACK;

\echo '-- Nothing kept: arrivals should equal the count above'
SELECT count(*) AS arrivals FROM file_arrival;
