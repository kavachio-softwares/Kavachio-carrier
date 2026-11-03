-- ============================================================================
-- activity_events: record the acting SEAT, not only a display string.
--
-- WHY
-- ---
-- The Audit Logs screen shows each seat its own trail: a broker user sees
-- their own, a broker admin sees their whole organisation's, a carrier sees
-- every broker it works with, and Kavachio sees everything. None of that could
-- be answered from what the table held.
--
--   * `actor` is a DISPLAY string. For anything a broker does it is the broker
--     COMPANY ("broker:12" — see audit.actor_for), because the carrier deals
--     with the company and never with its people. That is right for the
--     carrier and wrong for the broker admin, who is accountable for their own
--     team and could not tell which of them acted.
--   * `tenant_id` is NULL on every row a broker seat writes: a broker token
--     carries a broker party, not a carrier, so there is no tenant to record.
--     The carrier therefore could not find those rows either.
--
-- WHAT
-- ----
-- Three nullable columns naming the seat behind the row. `actor` is unchanged,
-- so every existing reader (the S-12 activity feed, the notification bell, the
-- platform dashboard) sees exactly what it saw before.
--
-- No backfill. Rows written before this are still resolved at read time from
-- `actor` — an email finds the person, "broker:<id>" finds the company — they
-- simply never recorded which of a broker's people acted, and inventing one
-- now would be a guess in an audit trail.
--
-- The app adds these itself at start-up (db.init_db -> _ensure_column) unless
-- KAVACHIO_RLS is on, in which case run this file.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE activity_events ADD COLUMN IF NOT EXISTS actor_user_id         INTEGER;
ALTER TABLE activity_events ADD COLUMN IF NOT EXISTS actor_role            VARCHAR;
ALTER TABLE activity_events ADD COLUMN IF NOT EXISTS actor_broker_party_id INTEGER;

-- The two scoping lookups the screen makes on every page: "this person's rows"
-- and "this broker organisation's rows".
CREATE INDEX IF NOT EXISTS ix_activity_events_actor_user_id
    ON activity_events (actor_user_id);
CREATE INDEX IF NOT EXISTS ix_activity_events_actor_broker_party_id
    ON activity_events (actor_broker_party_id);

COMMIT;

-- What one broker organisation's people have been doing, newest first:
--   SELECT e.created_at, u.user_full_name, e.action, e.target
--     FROM activity_events e
--     LEFT JOIN app_user u ON u.user_id = e.actor_user_id
--    WHERE e.actor_broker_party_id = <party id>
--    ORDER BY e.created_at DESC;
