-- ============================================================================
-- carrier_broker — which carriers a broker works with.
--
-- WHY. `party.party_tenant_id` says who ONBOARDED a broker: one carrier, one
-- column. The question every screen actually asks is "does this carrier work
-- with this broker", and that is many-to-many — a broker produces for several
-- carriers, which is the normal shape of the market.
--
-- Using the ownership column for it meant a broker shared with a second
-- carrier was invisible to that carrier in every list that filtered on it, and
-- the same bug had to be found and fixed separately in the broker directory,
-- Users & Roles, and the contract counterparty picker. The relationship was
-- then derived from `broker_invitation.status = 'accepted'` instead — better,
-- but still a rule every query had to know, and an invitation is an EVENT
-- whereas working together is a FACT that outlives it.
--
-- So the fact gets its own row. One place to read, one place to write, and
-- somewhere to hang what the relationship itself needs: when it started, and
-- whether it is still live.
--
-- WHAT IT IS NOT. Not a permission to produce — that is program_broker, and it
-- is per programme. This says the two organisations work together at all; the
-- programme link says on what. A carrier can work with a broker who is on none
-- of their programmes yet, which is exactly the state right after an
-- invitation is accepted.
-- ============================================================================

CREATE TABLE IF NOT EXISTS carrier_broker (
    carrier_broker_id          BIGSERIAL PRIMARY KEY,
    carrier_broker_tenant_id   BIGINT NOT NULL,
    carrier_broker_party_id    BIGINT NOT NULL
        REFERENCES party (party_id) ON DELETE CASCADE,

    -- 'active' | 'ended'. Ended rather than deleted: contracts and bordereaux
    -- underneath a relationship stay readable, and "we used to work with them"
    -- is a different answer from "we never did".
    carrier_broker_status      TEXT NOT NULL DEFAULT 'active'
        CHECK (carrier_broker_status IN ('active', 'ended')),

    -- How it began, for the record: 'invitation' (they accepted), 'onboarded'
    -- (this carrier created them), or 'backfill' for rows this migration
    -- inferred from the state that existed before the table did.
    carrier_broker_origin      TEXT,
    carrier_broker_since       TIMESTAMPTZ NOT NULL DEFAULT now(),
    carrier_broker_ended_at    TIMESTAMPTZ,
    carrier_broker_by_user_id  BIGINT,

    -- One row per pair. Working together twice is not a thing.
    CONSTRAINT ux_carrier_broker UNIQUE (carrier_broker_tenant_id,
                                         carrier_broker_party_id)
);

CREATE INDEX IF NOT EXISTS ix_carrier_broker_tenant
    ON carrier_broker (carrier_broker_tenant_id);
CREATE INDEX IF NOT EXISTS ix_carrier_broker_party
    ON carrier_broker (carrier_broker_party_id);

-- ── backfill, from the three things that used to imply the relationship ──
-- Order matters only for `origin`: the earliest explanation wins, and
-- ON CONFLICT keeps the first row written for a pair.

-- 1. The carrier that onboarded them.
INSERT INTO carrier_broker (carrier_broker_tenant_id, carrier_broker_party_id,
                            carrier_broker_origin, carrier_broker_since)
SELECT p.party_tenant_id, p.party_id, 'onboarded',
       COALESCE(p.created_at, now())
  FROM party p
 WHERE p.party_tenant_id IS NOT NULL
   AND lower(p.party_type::text) IN ('broker', 'mga', 'mgu', 'tpa')
ON CONFLICT ON CONSTRAINT ux_carrier_broker DO NOTHING;

-- 2. Anyone who accepted an invitation.
INSERT INTO carrier_broker (carrier_broker_tenant_id, carrier_broker_party_id,
                            carrier_broker_origin, carrier_broker_since)
SELECT i.broker_invitation_tenant_id, i.broker_invitation_party_id, 'invitation',
       COALESCE(i.broker_invitation_answered_at, i.broker_invitation_created_at, now())
  FROM broker_invitation i
 WHERE i.broker_invitation_status = 'accepted'
   AND i.broker_invitation_party_id IS NOT NULL
ON CONFLICT ON CONSTRAINT ux_carrier_broker DO NOTHING;

-- 3. Anyone already on one of the carrier's programmes. Predates invitations
--    entirely, and is the strongest evidence there is: they have been
--    producing.
INSERT INTO carrier_broker (carrier_broker_tenant_id, carrier_broker_party_id,
                            carrier_broker_origin, carrier_broker_since)
SELECT DISTINCT pb.tenant_id, pb.program_broker_party_id, 'backfill',
       COALESCE(pb.created_at, now())
  FROM program_broker pb
 WHERE pb.tenant_id IS NOT NULL
   AND pb.program_broker_party_id IS NOT NULL
ON CONFLICT ON CONSTRAINT ux_carrier_broker DO NOTHING;
