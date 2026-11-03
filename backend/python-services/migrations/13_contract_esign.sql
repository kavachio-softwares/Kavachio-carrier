-- ============================================================================
-- Step 4 of Create-a-Contract — Signatures.
--
-- Four tables that together are one signing round ("envelope", DocuSign's word
-- for the same thing): the document, the people who must sign it, the boxes
-- each of them owns on the page, and everything that happened.
--
-- THE ONE IDEA WORTH READING BEFORE THE COLUMNS
-- --------------------------------------------
-- A box on the page belongs to exactly ONE signer, and it says so itself, in
-- `field_party_key`. That key is not a name and not an email — both change —
-- it is the identifier the platform already uses for the organisation:
--
--     the insurer   ->  'tenant:<tenant_id>'
--     the broker    ->  'broker:<broker_party_id>'
--
-- The same string is stamped on the recipient row (`recipient_party_key`). So
-- "is this the broker's signature box or the carrier's?" is answered by string
-- equality against the signer holding the link — never by trusting what the
-- browser posts. This mirrors chk_app_user_scope on app_user: a carrier seat
-- carries tenant_id, a broker seat carries broker_party_id, never both.
--
-- RUNS ALONGSIDE THE CONTRACT-MANAGEMENT MIGRATIONS (14, 15, 16)
-- --------------------------------------------------------------
-- This one only CREATES tables, all of them new and all prefixed
-- contract_esign_. 14/15/16 only ALTER existing ones (contract_document,
-- contract_approval, contract). Nothing here is touched by them and nothing
-- there is touched by this, so the four are independent and the order they run
-- in does not matter. All four are idempotent.
--
-- They do meet in the CODE, in two places worth knowing about:
--
--   * Migration 14 moves the wording off contract.blob and onto a
--     contract_document row, and 16 has step 2 composing it as a .docx.
--     esign_routes._contract_wording() reads the newest active wording document
--     and falls back to contract.blob; esign_pdf.ensure_pdf() converts a Word
--     wording to PDF on the way in. So the anchors this feature relies on have
--     to be in whatever step 2 puts in wording_sections' signature-page layout:
--         {{signature:tenant:<tenant_id>}}   {{name:...}} {{title:...}} {{date:...}}
--         {{signature:broker:<broker_party_id>}}                 (and the same four)
--
--   * Migration 15 adds approval_proposed_changes for structured change
--     requests. A signer declining here writes its reason to
--     contract_esign_recipient.recipient_decline_reason and emails it; it does
--     NOT yet write a contract_approval row. Joining those two threads is a
--     decision for whoever owns the negotiation screen — see the note in
--     esign_routes.decline().
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- ── 1. The envelope: one document, out for signature once ───────────────────
CREATE TABLE IF NOT EXISTS contract_esign_envelope (
    envelope_id             BIGSERIAL PRIMARY KEY,
    -- The carrier that owns the round. Every read is scoped by this.
    envelope_tenant_id      BIGINT      NOT NULL,
    -- What is being signed. NULL while the contract row does not exist yet —
    -- the wizard can send a generated draft before it is filed as a contract.
    envelope_contract_id    BIGINT,
    envelope_program_id     BIGINT,
    envelope_broker_party_id BIGINT,
    envelope_title          TEXT        NOT NULL,
    -- draft      nothing sent, still being set up
    -- sent       out with the first signer
    -- in_progress at least one has signed, at least one has not
    -- completed  everybody signed; the PDF carries every signature
    -- declined   somebody refused, with a reason. Not a failure — a negotiation
    -- voided     the carrier pulled it back
    envelope_status         TEXT        NOT NULL DEFAULT 'draft',
    -- The document as it was written, never overwritten. Keeping the original
    -- is what lets "what did they actually agree to?" stay answerable after
    -- signatures have been stamped on top of it.
    envelope_source_pdf     BYTEA,
    envelope_source_pdf_ref TEXT,
    -- The document as it stands now: the original plus every signature applied
    -- so far. This is what the NEXT signer opens, which is the whole point —
    -- the broker sees the insurer's signature already on the page.
    envelope_current_pdf    BYTEA,
    envelope_current_pdf_ref TEXT,
    envelope_page_count     INTEGER     NOT NULL DEFAULT 0,
    -- Bumped on every stamp, so a cached page image can never be served stale.
    envelope_pdf_version    INTEGER     NOT NULL DEFAULT 1,
    envelope_created_by_id  BIGINT,
    envelope_created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    envelope_sent_at        TIMESTAMPTZ,
    envelope_completed_at   TIMESTAMPTZ
);

ALTER TABLE contract_esign_envelope
    DROP CONSTRAINT IF EXISTS contract_esign_envelope_status_check;
ALTER TABLE contract_esign_envelope
    ADD CONSTRAINT contract_esign_envelope_status_check
    CHECK (envelope_status IN ('draft','sent','in_progress','completed','declined','voided'));

CREATE INDEX IF NOT EXISTS ix_esign_envelope_tenant
    ON contract_esign_envelope (envelope_tenant_id);
CREATE INDEX IF NOT EXISTS ix_esign_envelope_contract
    ON contract_esign_envelope (envelope_contract_id);


-- ── 2. Who signs, and in what order ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contract_esign_recipient (
    recipient_id            BIGSERIAL PRIMARY KEY,
    recipient_envelope_id   BIGINT      NOT NULL
        REFERENCES contract_esign_envelope (envelope_id) ON DELETE CASCADE,
    -- Which side of the contract this person signs for.
    recipient_side          TEXT        NOT NULL,     -- insurer | broker
    -- The identity the fields are matched against. See the note at the top.
    recipient_party_key     TEXT        NOT NULL,     -- tenant:<id> | broker:<id>
    -- Exactly one of these is set, matching recipient_side and mirroring the
    -- app_user scope rule. Both NULL, or both set, is a bug.
    recipient_tenant_id     BIGINT,
    recipient_broker_party_id BIGINT,
    recipient_user_id       BIGINT,                   -- when they have a login
    recipient_name          TEXT        NOT NULL,
    recipient_email         TEXT        NOT NULL,
    recipient_title         TEXT,                     -- job title on the block
    recipient_org           TEXT,                     -- organisation they bind
    -- 1 signs first. The next person is only emailed once the one before them
    -- has signed — that ordering IS the flow the carrier asked for.
    recipient_order         INTEGER     NOT NULL DEFAULT 1,
    -- pending  their turn has not come
    -- sent     the link is with them
    -- viewed   they opened it
    -- signed   done
    -- declined they refused, with a reason
    recipient_status        TEXT        NOT NULL DEFAULT 'pending',
    -- The emailed link. Long, random, single-recipient — it is the only thing
    -- standing between the internet and this document, so it is unique-indexed
    -- and it expires.
    recipient_token         TEXT UNIQUE,
    recipient_token_expires TIMESTAMPTZ,
    recipient_sent_at       TIMESTAMPTZ,
    recipient_viewed_at     TIMESTAMPTZ,
    recipient_signed_at     TIMESTAMPTZ,
    recipient_decline_reason TEXT,
    -- What they actually adopted as their signature: the typed name, and the
    -- drawn image when they drew one (data URL).
    recipient_signature_name  TEXT,
    recipient_signature_image TEXT,
    -- Kept beside the signature, not in a log: "who, when, from where" is part
    -- of what makes the signature worth anything later.
    recipient_signed_ip     TEXT,
    recipient_signed_agent  TEXT,
    recipient_created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE contract_esign_recipient
    DROP CONSTRAINT IF EXISTS contract_esign_recipient_side_check;
ALTER TABLE contract_esign_recipient
    ADD CONSTRAINT contract_esign_recipient_side_check
    CHECK (recipient_side IN ('insurer','broker'));

ALTER TABLE contract_esign_recipient
    DROP CONSTRAINT IF EXISTS contract_esign_recipient_status_check;
ALTER TABLE contract_esign_recipient
    ADD CONSTRAINT contract_esign_recipient_status_check
    CHECK (recipient_status IN ('pending','sent','viewed','signed','declined'));

-- One organisation signs once per envelope. Without this a duplicated signer
-- row would silently give one side two sets of boxes.
CREATE UNIQUE INDEX IF NOT EXISTS uq_esign_recipient_party
    ON contract_esign_recipient (recipient_envelope_id, recipient_party_key);
CREATE INDEX IF NOT EXISTS ix_esign_recipient_envelope
    ON contract_esign_recipient (recipient_envelope_id, recipient_order);


-- ── 3. The boxes on the page ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contract_esign_field (
    field_id                BIGSERIAL PRIMARY KEY,
    field_envelope_id       BIGINT      NOT NULL
        REFERENCES contract_esign_envelope (envelope_id) ON DELETE CASCADE,
    -- WHO OWNS THIS BOX. The whole access rule is this column compared with
    -- the recipient behind the link. Denormalised on purpose: it survives a
    -- recipient row being replaced, and it is what the anchor in the document
    -- literally spelled out.
    field_party_key         TEXT        NOT NULL,
    field_recipient_id      BIGINT
        REFERENCES contract_esign_recipient (recipient_id) ON DELETE SET NULL,
    -- signature | initial | name | title | date | text
    field_type              TEXT        NOT NULL,
    field_page              INTEGER     NOT NULL,     -- 1-based
    -- Position as a FRACTION of the page (0..1, origin top-left), not points.
    -- The signing screen renders the page at whatever width fits the browser;
    -- fractions land the box in the same place at every zoom and every DPI.
    field_x                 DOUBLE PRECISION NOT NULL,
    field_y                 DOUBLE PRECISION NOT NULL,
    field_w                 DOUBLE PRECISION NOT NULL,
    field_h                 DOUBLE PRECISION NOT NULL,
    field_required          BOOLEAN     NOT NULL DEFAULT TRUE,
    field_label             TEXT,
    -- The token in the document that put the box here, e.g.
    -- '{{signature:tenant:12}}'. Kept so a re-generated document can be
    -- re-tagged and the placement reproduced rather than re-drawn by hand.
    field_anchor            TEXT,
    field_value             TEXT,
    field_filled_at         TIMESTAMPTZ,
    field_created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE contract_esign_field
    DROP CONSTRAINT IF EXISTS contract_esign_field_type_check;
ALTER TABLE contract_esign_field
    ADD CONSTRAINT contract_esign_field_type_check
    CHECK (field_type IN ('signature','initial','name','title','date','text'));

CREATE INDEX IF NOT EXISTS ix_esign_field_envelope
    ON contract_esign_field (field_envelope_id, field_page);
CREATE INDEX IF NOT EXISTS ix_esign_field_party
    ON contract_esign_field (field_envelope_id, field_party_key);


-- ── 4. What happened ────────────────────────────────────────────────────────
-- The envelope row holds where it got to; this holds how. It is printed as the
-- certificate page on the completed PDF, so it has to be complete.
CREATE TABLE IF NOT EXISTS contract_esign_event (
    event_id                BIGSERIAL PRIMARY KEY,
    event_envelope_id       BIGINT      NOT NULL
        REFERENCES contract_esign_envelope (envelope_id) ON DELETE CASCADE,
    event_recipient_id      BIGINT,
    -- created | sent | delivered | viewed | signed | declined | completed
    -- | reminded | voided
    event_type              TEXT        NOT NULL,
    event_actor             TEXT,
    event_ip                TEXT,
    event_agent             TEXT,
    event_detail            JSONB,
    event_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_esign_event_envelope
    ON contract_esign_event (event_envelope_id, event_at);


-- ── 5. Reconcile a database the APP created first ───────────────────────────
-- init_db() still calls Base.metadata.create_all() when RLS is off, so on a
-- database where the app booted before this file was run, SQLAlchemy made these
-- tables and the CREATE TABLE IF NOT EXISTS statements above did nothing. The
-- columns match either way — db.py binds every one to its physical name — but
-- create_all() leaves out what only SQL can state: the DB-side DEFAULTs, and
-- two NOT NULLs.
--
-- Nothing is broken by that today, because the ORM fills all of these in Python
-- on every insert. It matters because the shape then DIFFERS between a machine
-- where the app booted first and one where the migration ran first, and a
-- schema that depends on boot order is a schema nobody can reason about. This
-- section makes both paths converge on the same table.
DO $$
BEGIN
    ALTER TABLE contract_esign_envelope
        ALTER COLUMN envelope_status      SET DEFAULT 'draft',
        ALTER COLUMN envelope_page_count  SET DEFAULT 0,
        ALTER COLUMN envelope_pdf_version SET DEFAULT 1,
        ALTER COLUMN envelope_created_at  SET DEFAULT now();
    ALTER TABLE contract_esign_recipient
        ALTER COLUMN recipient_order      SET DEFAULT 1,
        ALTER COLUMN recipient_status     SET DEFAULT 'pending',
        ALTER COLUMN recipient_created_at SET DEFAULT now();
    ALTER TABLE contract_esign_field
        ALTER COLUMN field_required       SET DEFAULT TRUE,
        ALTER COLUMN field_created_at     SET DEFAULT now();
    ALTER TABLE contract_esign_event
        ALTER COLUMN event_at             SET DEFAULT now();
END $$;

-- The timestamps are NOT NULL in the declarations above. Backfill first: a row
-- written before the default existed could have a NULL there, and SET NOT NULL
-- on a column holding one fails outright.
UPDATE contract_esign_envelope  SET envelope_created_at  = now() WHERE envelope_created_at  IS NULL;
UPDATE contract_esign_recipient SET recipient_created_at = now() WHERE recipient_created_at IS NULL;
UPDATE contract_esign_field     SET field_created_at     = now() WHERE field_created_at     IS NULL;
UPDATE contract_esign_event     SET event_at             = now() WHERE event_at             IS NULL;

ALTER TABLE contract_esign_envelope  ALTER COLUMN envelope_created_at  SET NOT NULL;
ALTER TABLE contract_esign_recipient ALTER COLUMN recipient_created_at SET NOT NULL;
ALTER TABLE contract_esign_field     ALTER COLUMN field_created_at     SET NOT NULL;
ALTER TABLE contract_esign_event     ALTER COLUMN event_at             SET NOT NULL;

-- Indexes create_all() made under ITS naming convention, before db.py named
-- them to match this file. Each is a duplicate of one created above — same
-- table, same leading column — so keeping them means every insert maintains two
-- identical B-trees for nothing. db.py no longer emits these names, so dropping
-- them is final rather than a thing that comes back on the next boot.
DROP INDEX IF EXISTS ix_contract_esign_envelope_envelope_tenant_id;
DROP INDEX IF EXISTS ix_contract_esign_envelope_envelope_contract_id;
DROP INDEX IF EXISTS ix_contract_esign_recipient_recipient_envelope_id;
DROP INDEX IF EXISTS ix_contract_esign_field_field_envelope_id;
DROP INDEX IF EXISTS ix_contract_esign_field_field_party_key;
DROP INDEX IF EXISTS ix_contract_esign_event_event_envelope_id;

-- The signing token must be unique whichever side built the table: this file
-- declares it as a column constraint, create_all() used to build a unique index
-- called ix_…_recipient_token. Add the constraint when it is absent, then drop
-- the index it replaces — in that order, so the column is never briefly
-- unguarded.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'contract_esign_recipient'::regclass
          AND contype = 'u'
          AND conname = 'contract_esign_recipient_recipient_token_key'
    ) THEN
        ALTER TABLE contract_esign_recipient
            ADD CONSTRAINT contract_esign_recipient_recipient_token_key
            UNIQUE (recipient_token);
    END IF;
END $$;

DROP INDEX IF EXISTS ix_contract_esign_recipient_recipient_token;

COMMIT;
