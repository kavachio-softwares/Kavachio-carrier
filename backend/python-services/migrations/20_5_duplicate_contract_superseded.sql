-- 20_5_duplicate_contract_superseded.sql
--
-- Programme 1 holds the same contract twice: c1 and c5 carry an IDENTICAL
-- content fingerprint (99c0c99b...) and an identical business window
-- (2025-10-01 -> 2026-10-01) on the same lineage (programme 1, no schedule,
-- broker 4). They were uploaded 100 minutes apart under different filenames.
--
-- Every transaction date in that window therefore resolves to two contracts.
-- resolve_as_of breaks the tie with "latest effective_from, then newest id",
-- which lands on c5 -- but by tiebreak rather than because the data says so.
--
-- c5 is the one that processes. c1 is labelled superseded to record that it is
-- the one not in use.
--
-- NOTE ON SEMANTICS. 'superseded' is a LABEL, not an exclusion: it is not in
-- contract_asof.NON_GOVERNING_STATUSES, so c1 still resolves. That is
-- deliberate and must not change -- contract_routes marks a RENEWED contract
-- superseded once its term runs out, and those are exactly the contracts a late
-- prior-period file needs (§7). Excluding superseded rows would break every
-- renewal in the system.
--
-- Consequence: the overlap remains, and the resolver keeps logging one warning
-- per affected row. Silencing that needs c1's window closed as well, which is a
-- separate decision and is NOT done here.

UPDATE contract
SET    status_ops = 'superseded'
WHERE  contract_id = 1
  AND  status_ops = 'active';
