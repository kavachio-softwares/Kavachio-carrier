-- 37 — Uploaded contracts get a real contract type.
--
-- An upload whose screen named no type stored the document heading the model
-- read off the PDF ("Program Schedule G", "Reinsurance Contract", ...) as
-- contract.contract_type. contract_types.py knows only insurer_broker and
-- insurer_reinsurer, so every later validate() refused the row: Renew and
-- Edit failed with a 400 and the record showed "… is not a contract type".
-- Fixed in code (db_persister._coded_type) on 5 Oct 2026; this repairs the
-- rows already written.
--
-- All of them carry a broker, so they are insurer ↔ broker contracts. The
-- heading is not lost: it stays in extracted.document_type (copied there
-- first for any row that lacks it).
--
-- Left alone on purpose: `binding_authority` — the placeholder contracts the
-- BDX ingester creates for a programme with none (no broker, term to 2999).
-- That flow is unchanged.
--
-- Safe to re-run: the second run finds nothing to change.

UPDATE contract
   SET extracted = jsonb_set(COALESCE(extracted, '{}'::jsonb),
                             '{document_type}', to_jsonb(contract_type))
 WHERE contract_type NOT IN ('insurer_broker', 'insurer_reinsurer', 'binding_authority')
   AND (extracted->>'document_type') IS NULL;

UPDATE contract
   SET contract_type = 'insurer_broker'
 WHERE contract_type NOT IN ('insurer_broker', 'insurer_reinsurer', 'binding_authority')
   AND contract_broker_party_id IS NOT NULL;
