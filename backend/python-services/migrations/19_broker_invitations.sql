-- ============================================================================
-- Broker invitations — a carrier asks a broker to produce on a programme, and
-- the broker answers.
--
-- WHY THIS EXISTS. A broker works with several carriers; that is the normal
-- shape of the market. But every way of adding one CREATED one, so the second
-- carrier to work with a broker made a second copy of that broker — a separate
-- organisation, a second invitation to the same person, and a login that could
-- only ever see one of the two carriers.
--
-- The fix was nearly a leak. Telling the carrier "that address already runs
-- Marlowe Broking, add them as existing" makes the flow work by DISCLOSING a
-- relationship that belongs to another carrier. A broker is onboarded and held
-- by the carrier organisation that brought them on; which carriers a broker
-- already works with is not something the next one gets to learn by typing an
-- address into a form.
--
-- SO THE CARRIER NEVER FINDS OUT. They invite, and that is all they see. What
-- happens next depends on facts they are not shown:
--
--   the person already has a login   the invitation appears on their own
--                                    screen and they ACCEPT it. No second
--                                    organisation, no second login.
--   the person is new                they are onboarded as today, and
--                                    completing onboarding accepts the
--                                    invitation for them — there is nothing
--                                    for a brand-new broker to weigh up, and
--                                    a second click that can only be yes is
--                                    just a second click.
--
-- Either way the carrier's screen says "invitation sent", and the programme
-- link appears when — and only when — the broker has agreed to it. That last
-- part is the point: a carrier can no longer put a broker on a programme by
-- unilateral act. The broker consents, which is what "invitation" has meant
-- all along and what the old direct-link path quietly skipped.
--
-- `broker_party_id` is NULL until it is known: for a new broker the party is
-- created alongside, but an invitation to an address nobody has claimed yet
-- has no party to point at.
-- ============================================================================

CREATE TABLE IF NOT EXISTS broker_invitation (
    broker_invitation_id          BIGSERIAL PRIMARY KEY,
    -- The carrier doing the inviting, and the programme they are inviting on to.
    broker_invitation_tenant_id   BIGINT NOT NULL,
    broker_invitation_program_id  BIGINT
        REFERENCES program (program_id) ON DELETE CASCADE,

    -- Who is invited. The address is the identity here: it is what the carrier
    -- knows, and it is what the invitation is matched to when somebody signs
    -- in — whether they existed at the time of sending or not.
    broker_invitation_email       TEXT NOT NULL,
    -- Known when the invitation created the broker, or filled in on accept.
    broker_invitation_party_id    BIGINT
        REFERENCES party (party_id) ON DELETE CASCADE,
    -- What the carrier typed. Only used when the broker turns out to be new.
    broker_invitation_org_name    TEXT,

    broker_invitation_status      TEXT NOT NULL DEFAULT 'pending'
        CHECK (broker_invitation_status IN ('pending','accepted','declined','revoked')),
    -- 'auto' where onboarding accepted it, 'broker' where a person clicked.
    broker_invitation_accepted_by TEXT
        CHECK (broker_invitation_accepted_by IN ('broker','auto')),

    broker_invitation_by_user_id  BIGINT,
    broker_invitation_created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    broker_invitation_answered_at TIMESTAMPTZ,
    broker_invitation_note        TEXT
);

CREATE INDEX IF NOT EXISTS ix_broker_invitation_email
    ON broker_invitation (lower(broker_invitation_email));
CREATE INDEX IF NOT EXISTS ix_broker_invitation_party
    ON broker_invitation (broker_invitation_party_id);

-- One live invitation per (carrier, programme, address). Inviting twice is a
-- slip, not a second invitation — and the carrier is told that plainly,
-- because it is a fact about their OWN organisation and discloses nothing.
CREATE UNIQUE INDEX IF NOT EXISTS ux_broker_invitation_pending
    ON broker_invitation (broker_invitation_tenant_id,
                          broker_invitation_program_id,
                          lower(broker_invitation_email))
    WHERE broker_invitation_status = 'pending';
