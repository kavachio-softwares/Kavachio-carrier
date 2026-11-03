-- ============================================================================
-- Broker onboarding approval: a carrier user's broker waits for the carrier
-- admin BEFORE the broker is told anything at all.
--
-- WHY
-- ---
-- Putting a broker on a programme is the act that lets them produce, and until
-- now a carrier USER could do it outright. Two things happened the moment they
-- clicked: the broker organisation and its admin login were created, and an
-- email went out inviting that person in. The carrier admin found out
-- afterwards, if at all, and there was nothing left to decide — you cannot
-- un-send an invitation.
--
-- The rule now: a broker a carrier USER wants to bring on goes to the carrier
-- admin first. Nothing is created and nothing is sent until they approve. If
-- they reject it, the broker never learns they were considered, and the reason
-- goes back to the colleague who asked.
--
-- A broker the carrier ADMIN adds goes on directly, as it always did — their
-- own act IS the approval, so there is nobody left to ask.
--
-- THIS IS AN ADDITIONAL GATE, NOT A REPLACEMENT. The Bordereau Setup approval
-- of migration 27 is untouched and still does exactly what it did: a carrier
-- user's programme->broker link is still created `pending_approval`, and it is
-- still released when the setup built on top of it is approved
-- (direct_routes._activate_links_for). The two gates answer two different
-- questions, in order:
--
--     28 (this one)  WHO do we work with?        -> the invitation is sent
--     27             WHAT may they send us?      -> the programme goes live
--
-- So approving a broker here does NOT put them on the programme in the
-- broker's sight. It sends the invitation and opens the relationship; the
-- programme, its contract and its BDX template stay invisible to them until
-- the setup is approved, as before.
--
-- WHAT
-- ----
-- One table, `broker_onboarding_request`. It holds an INTENTION, not a thing:
-- no party, no app_user, no carrier_broker row and no program_broker row
-- exists while it waits. A rejected request therefore leaves nothing behind,
-- which is the only way to be sure a rejection cannot accidentally mail
-- somebody.
--
-- Two shapes in the one table, told apart by `broker_party_id`:
--
--   set    a broker already in this carrier's directory. Only a programme link
--          is wanted; no email is ever sent for one of these.
--   NULL   a broker to invite. The typed email, organisation name, type and
--          admin name are all that is kept. Whether that address already
--          belongs to a broker is decided at APPROVAL, by the same code that
--          decides it today — so this row cannot leak the answer either, and
--          the "a carrier never learns whose broker this is" rule behind
--          broker_invitation (migration 19) survives intact.
--
-- ONE ROW PER DECISION, and no separate decision log beside it. Re-asking
-- after a rejection writes a NEW row, so "why was this turned down in June?"
-- stays answerable. (Setups needed the second table because a pipeline already
-- existed before it was submitted; a request like this one IS the submission.)
--
-- NO BACKFILL, and nothing to backfill: every broker already on a programme
-- stays there, whoever put them there. The new rule governs new work only.
--
-- The app adds this itself at start-up (db.init_db -> create_all) unless
-- KAVACHIO_RLS is on, in which case run this file.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS broker_onboarding_request (
    request_id            BIGSERIAL PRIMARY KEY,
    tenant_id             INTEGER     NOT NULL,
    -- NULL where no programme was named: bringing a broker into the DIRECTORY
    -- sends the same email to the same person, so it needs the same approval,
    -- and it has no programme to record.
    program_id            INTEGER,
    -- Set for an existing broker out of the directory; NULL for one to invite.
    broker_party_id       INTEGER,
    -- What the carrier user typed. Unused where broker_party_id is set.
    email                 VARCHAR,
    org_name              VARCHAR,
    party_type            VARCHAR,
    admin_name            VARCHAR,
    -- pending | approved | rejected | withdrawn
    status                VARCHAR     NOT NULL DEFAULT 'pending',
    requested_by_user_id  INTEGER     NOT NULL,
    requested_at          TIMESTAMP,
    decided_by_user_id    INTEGER,
    decided_at            TIMESTAMP,
    -- Why it was turned down. Enforced by the route, not the column.
    reason                TEXT,
    created_at            TIMESTAMP,
    modified_at           TIMESTAMP
);

-- program_id MUST be nullable, and this is here for the databases that met the
-- table before that was true. CREATE TABLE IF NOT EXISTS above does nothing to
-- a table that already exists, so one created with program_id NOT NULL would
-- keep that shape for ever and reject every directory invite. Idempotent, and a
-- no-op where the column is already nullable. db.init_db does the same.
ALTER TABLE broker_onboarding_request ALTER COLUMN program_id DROP NOT NULL;

-- The carrier admin's queue: "what is waiting on me at this carrier".
CREATE INDEX IF NOT EXISTS ix_broker_onboarding_request_tenant_status
    ON broker_onboarding_request (tenant_id, status);
-- A programme's own pending requests, read by its broker screen so a request
-- already in flight is not raised twice.
CREATE INDEX IF NOT EXISTS ix_broker_onboarding_request_program_id
    ON broker_onboarding_request (program_id);
-- "Has anyone already asked about this broker?" — the duplicate guard.
CREATE INDEX IF NOT EXISTS ix_broker_onboarding_request_party_id
    ON broker_onboarding_request (broker_party_id);

COMMIT;

-- ============================================================================
-- Rolling back
-- ============================================================================
-- Nothing else points at this table and nothing outside it changed, so it can
-- simply be dropped. Any request still at 'pending' is an ASK THAT WAS NEVER
-- ANSWERED, not work in progress: nothing was created for it, so dropping the
-- table loses the question and breaks nothing. Tell the carrier users who
-- raised them, because from their side the request will silently vanish:
--
--   SELECT r.request_id, r.program_id, r.email, r.broker_party_id,
--          u.user_email AS asked_by
--     FROM broker_onboarding_request r
--     LEFT JOIN app_user u ON u.user_id = r.requested_by_user_id
--    WHERE r.status = 'pending';
--
--   DROP TABLE IF EXISTS broker_onboarding_request;
-- ============================================================================
