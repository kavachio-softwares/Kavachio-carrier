-- ============================================================================
-- An API Idempotency-Key belongs to the sender's way in, not to the carrier.
--
-- WHY
-- ---
-- 10_2 made the key unique per carrier: (tenant_id, idempotency_key). Every
-- broker of a carrier therefore shared one key space. Two brokers whose
-- overnight jobs both name their keys after the period ("bdx-2026-07")
-- collided: the second broker's genuine file was refused as "key already
-- used", or, with identical bytes, handed the first broker's receipt.
--
-- WHAT
-- ----
-- The rule becomes (tenant_id, route, idempotency_key). A route belongs to
-- one broker, and it is the same scope the receipt lookup
-- (GET /v1/bordereaux/{reference}) already uses. intake_api_routes looks keys
-- up the same way.
--
-- No rows change. The new index is looser than the old one (anything unique
-- per carrier is unique per carrier + route), so it cannot fail on existing
-- data. It is built BEFORE the old one is dropped, so there is no moment with
-- no guard at all. The only thing removed is the old per-carrier rule, which
-- is the bug.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- COALESCE: an e-mailed file that matched no route has route_id NULL, and a
-- plain NULL would make every such row distinct — the mail poller's
-- "email:<message-id>:<file>" keys would lose the guard they have today.
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_arrival_idempotency_route
    ON file_arrival (tenant_id, COALESCE(route_id, 0), idempotency_key)
    WHERE idempotency_key IS NOT NULL;

DROP INDEX IF EXISTS uq_file_arrival_idempotency;

COMMIT;

-- Check afterwards:
--   SELECT indexdef FROM pg_indexes
--    WHERE tablename = 'file_arrival' AND indexname LIKE 'uq_file_arrival_idem%';
