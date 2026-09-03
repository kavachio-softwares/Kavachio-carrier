-- ============================================================================
-- Feature 10.2 — API intake endpoint.
--
-- Three changes. The first two are limitations the existing intake code already
-- documents in its own comments; they fix SFTP (10.1) as much as they enable
-- the API door, because both channels run through the same land_file().
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- ── 1. A route can name its programme ───────────────────────────────────────
-- intake_service.land_file says it outright: "intake_route has no program_id
-- column, so a route identifies the BROKER but not the programme... for a
-- broker on two programmes this cannot yet say which one a file belongs to."
--
-- NULL keeps today's behaviour exactly (broker-wide), so every existing SFTP
-- route is unaffected. Set it, and that route is pinned to one programme — the
-- recommended shape for an API key: one key per (programme, broker) pair, so
-- the sender has nothing to supply and nothing to get wrong.
ALTER TABLE intake_route
    ADD COLUMN IF NOT EXISTS program_id BIGINT REFERENCES program(program_id);

CREATE INDEX IF NOT EXISTS ix_intake_route_program
    ON intake_route (program_id);


-- ── 2. "Held" becomes a real outcome ────────────────────────────────────────
-- Today outcome is accepted|turned_away only, so the three checks that mean
-- "hold this for a person" (duplicate, empty file, no live contract) are
-- recorded as refusals. The code compensates by prefixing the reason with
-- "Held —", which the screen cannot filter on and a partner cannot act on.
--
-- A held file is NOT a refusal: it landed, it is stored, and somebody decides.
ALTER TABLE file_arrival
    DROP CONSTRAINT IF EXISTS file_arrival_outcome_check;

ALTER TABLE file_arrival
    ADD CONSTRAINT file_arrival_outcome_check
    CHECK (outcome IN ('accepted', 'held', 'turned_away'));

-- Backfill the rows that were already holds wearing a refusal's label.
UPDATE file_arrival
   SET outcome = 'held'
 WHERE outcome = 'turned_away'
   AND turned_away_reason LIKE 'Held%';


-- ── 3. API keys ─────────────────────────────────────────────────────────────
-- SFTP identifies a broker by which folder the file landed in. An API caller
-- has no folder, so the key does that job: it is bound to one intake_route, and
-- the route already carries the broker (and now the programme).
--
-- The key itself is never stored. `key_hash` is HMAC-SHA256(pepper, key) with
-- the pepper held outside the database, so a dump alone yields no working keys.
-- `key_prefix` is the indexed lookup handle, parsed out of the request header.
CREATE TABLE IF NOT EXISTS intake_credential (
    credential_id   BIGSERIAL PRIMARY KEY,
    route_id        BIGINT NOT NULL REFERENCES intake_route(route_id) ON DELETE CASCADE,
    tenant_id       BIGINT NOT NULL REFERENCES tenant(tenant_id),
    key_prefix      TEXT NOT NULL UNIQUE,
    key_hash        TEXT NOT NULL,
    last4           TEXT,
    label           TEXT,
    ip_allowlist    JSONB,
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,           -- revoke, never delete: arrivals point here
    last_used_at    TIMESTAMPTZ,
    created_by_user_id BIGINT REFERENCES app_user(user_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_intake_credential_route
    ON intake_credential (route_id) WHERE revoked_at IS NULL;


-- ── 4. Idempotency, so a retrying cron job cannot double-load a month ───────
-- A partner's overnight job that times out will send the same file again. With
-- a key we can tell "this is the same submission" from "this is a second file",
-- and hand back the original receipt instead of loading it twice.
ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

-- Partial unique index: the DB arbitrates the race rather than application
-- check-then-insert, which loses it when two identical POSTs arrive together.
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_arrival_idempotency
    ON file_arrival (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- The reference a partner quotes back at us. Never expose arrival_id: a
-- sequential integer lets anyone count and enumerate other people's traffic.
ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS public_ref TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_arrival_public_ref
    ON file_arrival (public_ref) WHERE public_ref IS NOT NULL;

-- Where the bytes were kept, so a turned-away file can still be looked at.
ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS blob_ref TEXT;

COMMIT;


-- ============================================================================
-- 5. One API route per (broker, programme), not one per carrier.
--
-- `UNIQUE (tenant_id, channel, address)` works for SFTP and email, where the
-- address IS the identity — one folder, one mailbox, one broker. It breaks for
-- API, where every route shares the one endpoint "POST /v1/bordereaux": the
-- constraint then allows a carrier exactly ONE api route in total, which makes
-- one-key-per-(programme, broker) impossible.
--
-- Split it. Non-API keeps address uniqueness. API is unique on what actually
-- identifies it — the broker and the programme the route is for.
-- ============================================================================

BEGIN;

ALTER TABLE intake_route
    DROP CONSTRAINT IF EXISTS intake_route_tenant_id_channel_address_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_route_address
    ON intake_route (tenant_id, channel, address)
    WHERE channel <> 'api';

-- COALESCE so a broker-wide API route (program_id NULL) is still unique per
-- broker; NULLs would otherwise never collide and you could create it twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_intake_route_api_scope
    ON intake_route (tenant_id, broker_party_id, COALESCE(program_id, 0))
    WHERE channel = 'api';

COMMIT;
