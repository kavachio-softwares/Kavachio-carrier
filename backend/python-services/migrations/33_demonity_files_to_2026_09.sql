-- 33 — ONE-OFF DATA FIX (shared dev database, 1 Oct 2026)
--
-- Every file Mahi Corp pvt ltd has received on "demonity Programme" is for
-- reporting period 2026-09 (due 10 Oct), so all of them arrived on time.
--
-- Why: the first API files were sent without a period, so the calendar filed
-- them under the oldest period still open (2026-07, due 10 Aug), and one hand
-- upload went to 2026-08. From now on every channel has to state the period
-- (POST /v1/bordereaux refuses a file without one; email and SFTP files must
-- name it in the subject or file name). This moves the files already in.
--
-- For tenant 1377 / programme 1292 / broker 1600 only:
--   1. file_arrival.reporting_period = '2026-09' on every accepted file.
--   2. Each calendar version is pointed at its own file's output (an API
--      file's version was written before its run, and July's version 1 had
--      been given a later file's output).
--   3. A processed file that never reached the calendar gets its version
--      (the re-sent copy released by hand at 15:28 on 1 Oct).
--   4. Every version of 2026-07 and 2026-08 moves onto 2026-09, numbered 1..n
--      in the order the files came in; the first is the original.
--   5. 2026-09: received 30 Sep, due 10 Oct -> on time.
--      2026-07 and 2026-08 hold no file any more -> overdue, because nothing
--      was actually sent for them.
--   6. One submission: a file for the same broker, programme, contract and
--      period is the next version of the same submission, however it came in
--      — so the three hand uploads, the API files and the secure-link
--      corrections are versions 1..n of ONE submission, numbered as the
--      calendar numbers them. Its reference stays the one the broker already
--      has (the API files'), so their emails and secure links keep working.
--
-- Safe anywhere: on a database where programme 1292 is not this tenant's
-- "demonity Programme" it changes nothing. Atomic: one block, all or nothing.
-- Run it once, as one statement batch (pgAdmin: Execute script; psql: -f).

DO $$
DECLARE
    sep_id   integer;
    n_files  integer;
    n_linked integer;
    n_added  integer;
    n_moved  integer;
    ref      text;
    n_last   integer;
    n_joined integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM program p JOIN tenant t ON t.tenant_id = p.program_tenant_id
         WHERE p.program_id = 1292 AND p.program_name = 'demonity Programme'
           AND t.tenant_id = 1377 AND t.tenant_legal_name = 'Mahi Corp pvt ltd') THEN
        RAISE NOTICE 'Programme 1292 is not demonity Programme / Mahi Corp pvt ltd here: nothing changed.';
        RETURN;
    END IF;

    SELECT id INTO sep_id FROM expected_submission
     WHERE program_id = 1292 AND broker_party_id = 1600 AND period = '2026-09';
    IF sep_id IS NULL THEN
        RAISE EXCEPTION 'No 2026-09 calendar row for broker 1600 on programme 1292: nothing changed.';
    END IF;

    -- 1. the files
    UPDATE file_arrival a
       SET reporting_period = '2026-09'
     WHERE a.tenant_id = 1377 AND a.matched_broker_party_id = 1600
       AND a.outcome = 'accepted'
       AND COALESCE(a.program_id,
                    (SELECT r.program_id FROM intake_route r WHERE r.route_id = a.route_id)) = 1292
       AND a.reporting_period IS DISTINCT FROM '2026-09';
    GET DIAGNOSTICS n_files = ROW_COUNT;

    -- 2. each channel file's calendar version -> its own output.
    --    a) A version can only name the output of a file that had arrived when
    --       it was written: undo any link to a later file (July's version 1
    --       had been given the output of a file sent hours after it).
    UPDATE submission_version v
       SET received_export_id = NULL,
           modified_at = now() AT TIME ZONE 'UTC'
      FROM file_arrival a
     WHERE v.program_id = 1292 AND v.broker_party_id = 1600
       AND a.run_export_id = v.received_export_id
       AND a.received_at > (v.created_at AT TIME ZONE 'UTC') + interval '5 seconds';
    --    b) A version naming no output was written in the same transaction as
    --       its file (land_file), so its file is the one with that name that
    --       landed nearest that moment.
    UPDATE submission_version v
       SET received_export_id = m.run_export_id,
           modified_at = now() AT TIME ZONE 'UTC'
      FROM (SELECT DISTINCT ON (v2.id) v2.id, a.run_export_id
              FROM submission_version v2
              JOIN file_arrival a
                ON a.tenant_id = v2.tenant_id AND a.filename = v2.source_filename
             WHERE v2.program_id = 1292 AND v2.broker_party_id = 1600
               AND v2.received_export_id IS NULL
               AND a.route_id IS NOT NULL AND a.run_export_id IS NOT NULL
               AND abs(extract(epoch FROM (a.received_at - (v2.created_at AT TIME ZONE 'UTC')))) <= 30
             ORDER BY v2.id,
                      abs(extract(epoch FROM (a.received_at - (v2.created_at AT TIME ZONE 'UTC'))))) m
     WHERE v.id = m.id;
    GET DIAGNOSTICS n_linked = ROW_COUNT;

    -- 3. processed files with no calendar version (numbered properly in step 4)
    INSERT INTO submission_version
           (tenant_id, expected_id, program_id, broker_party_id, period, version_no,
            kind, received_at, received_export_id, source_filename, period_source,
            created_at, modified_at)
    SELECT a.tenant_id, sep_id, 1292, 1600, '2026-09', -100000 - a.arrival_id,
           'corrected', (a.received_at AT TIME ZONE 'UTC')::date, a.run_export_id,
           a.filename, 'explicit', a.received_at AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC'
      FROM file_arrival a
     WHERE a.tenant_id = 1377 AND a.matched_broker_party_id = 1600
       AND a.outcome = 'accepted' AND a.reporting_period = '2026-09'
       AND a.run_export_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM submission_version v
                        WHERE v.received_export_id = a.run_export_id);
    GET DIAGNOSTICS n_added = ROW_COUNT;

    -- 4. July + August + September versions -> September, 1..n by arrival.
    --    Two passes: (expected_id, version_no) is unique, so park them on
    --    negative numbers first.
    WITH ordered AS (
        SELECT v.id, row_number() OVER (ORDER BY v.created_at, v.id) AS n
          FROM submission_version v
         WHERE v.program_id = 1292 AND v.broker_party_id = 1600
           AND v.period IN ('2026-07', '2026-08', '2026-09'))
    UPDATE submission_version v SET version_no = -o.n
      FROM ordered o WHERE v.id = o.id;

    UPDATE submission_version v
       SET period_source = CASE WHEN v.period <> '2026-09' THEN 'explicit' ELSE v.period_source END,
           expected_id   = sep_id,
           period        = '2026-09',
           kind          = CASE WHEN v.version_no = -1 THEN 'original' ELSE 'corrected' END,
           version_no    = -v.version_no,
           modified_at   = now() AT TIME ZONE 'UTC'
     WHERE v.program_id = 1292 AND v.broker_party_id = 1600 AND v.version_no < 0;
    GET DIAGNOSTICS n_moved = ROW_COUNT;

    -- 5. the periods
    UPDATE expected_submission e
       SET version_count      = s.n,
           received_at        = s.first_on,
           received_export_id = s.first_export,
           latest_received_at = s.last_on,
           released_at        = s.last_release,
           released_count     = s.released,
           status             = CASE WHEN s.first_on <= e.due_date THEN 'on_time'
                                     ELSE 'received_late' END,
           modified_at        = now() AT TIME ZONE 'UTC'
      FROM (SELECT count(*)            AS n,
                   min(received_at)    AS first_on,
                   max(received_at)    AS last_on,
                   max(released_at)    AS last_release,
                   count(released_at)  AS released,
                   (array_agg(received_export_id ORDER BY version_no))[1] AS first_export
              FROM submission_version WHERE expected_id = sep_id) s
     WHERE e.id = sep_id;

    UPDATE expected_submission e
       SET version_count = 0, received_at = NULL, received_export_id = NULL,
           latest_received_at = NULL, released_at = NULL, released_count = 0,
           status = CASE WHEN e.due_date < (now() AT TIME ZONE 'UTC')::date
                         THEN 'overdue' ELSE e.status END,
           modified_at = now() AT TIME ZONE 'UTC'
     WHERE e.program_id = 1292 AND e.broker_party_id = 1600
       AND e.period IN ('2026-07', '2026-08');

    -- 6. one submission, versions 1..n in the order the files came in
    DROP TABLE IF EXISTS m33_versions;
    CREATE TEMP TABLE m33_versions ON COMMIT DROP AS
    WITH files AS (
        SELECT a.arrival_id AS id, 'file'::text AS kind, a.received_at AS at
          FROM file_arrival a
         WHERE a.tenant_id = 1377 AND a.matched_broker_party_id = 1600
           AND a.outcome = 'accepted' AND a.reporting_period = '2026-09'
           AND COALESCE(a.program_id,
                        (SELECT r.program_id FROM intake_route r
                          WHERE r.route_id = a.route_id)) = 1292),
    fixes AS (            -- secure-link corrections made before they were files
        SELECT e.id, 'export'::text AS kind, e.created_at AT TIME ZONE 'UTC' AS at
          FROM output_exports e
         WHERE e.version_status IS NOT NULL
           AND e.submission_ref IN (SELECT a.submission_ref FROM file_arrival a
                                     WHERE a.arrival_id IN (SELECT id FROM files)
                                       AND a.submission_ref IS NOT NULL))
    SELECT id, kind, row_number() OVER (ORDER BY at, kind DESC, id)::int AS n
      FROM (SELECT * FROM files UNION ALL SELECT * FROM fixes) m;

    SELECT max(n) INTO n_last FROM m33_versions;
    SELECT a.submission_ref INTO ref
      FROM file_arrival a JOIN m33_versions m ON m.kind = 'file' AND m.id = a.arrival_id
     WHERE a.submission_ref IS NOT NULL
     ORDER BY a.received_at LIMIT 1;
    IF ref IS NULL THEN
        SELECT a.public_ref INTO ref
          FROM file_arrival a JOIN m33_versions m ON m.kind = 'file' AND m.id = a.arrival_id
         ORDER BY a.received_at LIMIT 1;
    END IF;

    IF ref IS NOT NULL THEN
        UPDATE file_arrival a
           SET submission_ref = ref,
               version_no     = m.n,
               matched_by     = COALESCE(a.matched_by, 'period'),
               version_status = CASE
                   WHEN m.n = n_last THEN a.version_status
                   WHEN a.version_status IN ('delivered', 'delivered_flagged')
                        THEN a.version_status
                   ELSE 'superseded' END
          FROM m33_versions m
         WHERE m.kind = 'file' AND a.arrival_id = m.id;
        GET DIAGNOSTICS n_joined = ROW_COUNT;

        -- each file's checked output carries the submission and version
        UPDATE output_exports e
           SET submission_ref = ref, version_no = m.n
          FROM m33_versions m JOIN file_arrival a ON a.arrival_id = m.id
         WHERE m.kind = 'file' AND e.id = a.run_export_id;

        UPDATE output_exports e
           SET submission_ref = ref, version_no = m.n,
               version_status = CASE WHEN m.n = n_last THEN e.version_status
                                     ELSE 'superseded' END
          FROM m33_versions m
         WHERE m.kind = 'export' AND e.id = m.id;
    END IF;

    RAISE NOTICE 'Done: % file(s) set to 2026-09, % calendar version(s) re-linked, % added, % numbered on 2026-09; one submission % with % version(s).',
        n_files, n_linked, n_added, n_moved, ref, n_last;
END $$;

-- OPTIONAL. July and August now owe a file, so the daily sweep raises one
-- "overdue" notice for each. To skip those two notices, run this as well:
-- UPDATE expected_submission SET overdue_notified = true, overdue_emailed = true
--  WHERE program_id = 1292 AND broker_party_id = 1600 AND period IN ('2026-07', '2026-08');

-- Check:
SELECT arrival_id, filename, version_no, version_status, reporting_period
  FROM file_arrival
 WHERE tenant_id = 1377 AND matched_broker_party_id = 1600
 ORDER BY version_no NULLS LAST, arrival_id;

SELECT period, due_date, status, received_at, version_count, released_count
  FROM expected_submission
 WHERE program_id = 1292 AND broker_party_id = 1600
   AND period IN ('2026-07', '2026-08', '2026-09')
 ORDER BY period;
