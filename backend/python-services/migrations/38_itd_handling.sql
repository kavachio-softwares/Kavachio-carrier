-- ============================================================================
-- 38 — ITD (inception-to-date) bordereaux: load only what is new.
--
-- WHY
-- ---
-- An ITD file repeats every transaction since the programme began, every
-- month. Run like a monthly file it counts last month's rows again. With ITD
-- settings switched on for a setup, the run now decides from the rows whether
-- a file is ITD (itd.py), compares it with what the feed has already
-- delivered — its RUNNING TOTAL — and processes only the new rows and money
-- adjustments (itd_run.py, itd_store.py).
--
-- WHAT
-- ----
--   pipeline.itd_config            the setup's ITD settings (NULL = off)
--   file_arrival.load_type …       how a run read a file, its period span
--                                  and what it did with its rows
--   bordereau_position             the running total: one row per transaction
--                                  a feed has delivered (feed = tenant +
--                                  programme + broker + stream). Declared in
--                                  the data model long ago, never used until now.
--   bordereau_position_event       what happened to each row of each file —
--                                  pending until the run succeeds, then applied
--   premium_transaction            the broker's own reference, and on an ITD
--     .premium_transaction_source_ref / _itd_adjusts_row_key
--     / _itd_reverses_row_key      adjustment the transaction it corrects, on
--                                  a reversal (INV110582-R) the one it reverses
--
-- Every column is nullable and nothing is backfilled: a setup with no ITD
-- settings runs exactly as before. Off RLS, db.init_db adds the same columns
-- and canonical create_all builds the tables; under RLS this file owns them.
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

-- ── the setup ───────────────────────────────────────────────────────────────
ALTER TABLE pipeline ADD COLUMN IF NOT EXISTS itd_config JSONB;

-- ── the file ────────────────────────────────────────────────────────────────
ALTER TABLE file_arrival
    ADD COLUMN IF NOT EXISTS load_type        TEXT,      -- itd | regular
    ADD COLUMN IF NOT EXISTS load_type_reason TEXT,
    ADD COLUMN IF NOT EXISTS period_start     DATE,
    ADD COLUMN IF NOT EXISTS period_end       DATE,      -- what an ITD is "as at"
    ADD COLUMN IF NOT EXISTS itd_summary      JSONB;

-- ── the running total ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bordereau_position (
    position_id               BIGSERIAL PRIMARY KEY,
    tenant_id                 BIGINT,
    position_contract_id      BIGINT,
    position_row_key          TEXT,
    position_row_hash         TEXT,
    position_entity_id        BIGINT,
    position_status           TEXT,          -- active | dropped | removed | void
    position_last_seen_at     TIMESTAMP,
    created_at                TIMESTAMP,
    modified_at               TIMESTAMP,
    upload_id                 BIGINT
);
ALTER TABLE bordereau_position
    ADD COLUMN IF NOT EXISTS tenant_id                 BIGINT,
    ADD COLUMN IF NOT EXISTS position_program_id       BIGINT,
    ADD COLUMN IF NOT EXISTS position_broker_party_id  BIGINT,   -- 0 = no broker
    ADD COLUMN IF NOT EXISTS position_stream           TEXT,
    ADD COLUMN IF NOT EXISTS position_key_source       TEXT,     -- txn | composite
    ADD COLUMN IF NOT EXISTS position_source_ref       TEXT,
    ADD COLUMN IF NOT EXISTS position_money            JSONB,
    ADD COLUMN IF NOT EXISTS position_values           JSONB,
    ADD COLUMN IF NOT EXISTS position_adjust_count     INTEGER,
    ADD COLUMN IF NOT EXISTS position_first_arrival_id BIGINT,
    ADD COLUMN IF NOT EXISTS position_last_arrival_id  BIGINT,
    ADD COLUMN IF NOT EXISTS position_last_period      TEXT,
    ADD COLUMN IF NOT EXISTS position_reverses_key     TEXT;     -- on a reversal

-- One live row per transaction per feed. A withdrawn (void) row may repeat.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bordereau_position_feed_key
    ON bordereau_position (tenant_id, position_program_id, position_broker_party_id,
                           position_stream, position_row_key)
 WHERE COALESCE(position_status, 'active') <> 'void';

-- Loading a feed's running total before each comparison.
CREATE INDEX IF NOT EXISTS ix_bordereau_position_feed
    ON bordereau_position (tenant_id, position_program_id, position_broker_party_id,
                           position_stream, position_status);

-- ── what happened to each row of each file ──────────────────────────────────
CREATE TABLE IF NOT EXISTS bordereau_position_event (
    event_id              BIGSERIAL PRIMARY KEY,
    tenant_id             BIGINT,
    event_position_id     BIGINT,
    event_file_arrival_id BIGINT,
    event_landing_id      BIGINT,
    event_program_id      BIGINT,
    event_broker_party_id BIGINT,
    event_stream          TEXT,
    event_sheet           TEXT,
    event_row_no          INTEGER,
    event_row_key         TEXT,
    event_source_ref      TEXT,
    event_result          TEXT,   -- new | reversal | unchanged | changed
                                  -- | details_changed | dropped | duplicate
                                  -- | missing_key
    event_load_type       TEXT,   -- itd | regular
    event_status          TEXT,   -- pending | applied | conflict | check_only
                                  -- | void | dropped_decided
    event_period          TEXT,   -- YYYY-MM the file is as at
    event_old_hash        TEXT,
    event_new_hash        TEXT,
    event_old_money       JSONB,
    event_new_money       JSONB,
    event_money_delta     JSONB,
    event_changed_fields  JSONB,
    event_adjust_no       INTEGER,
    event_reverses_key    TEXT,   -- on a reversal: the transaction it reverses
    event_reverses_ref    TEXT,
    event_old_values      JSONB,
    event_new_values      JSONB,
    event_row_period      TEXT,   -- YYYY-MM the row is reported in
    event_note            TEXT,
    created_at            TIMESTAMP,
    modified_at           TIMESTAMP
);

-- A database that ran an earlier copy of this file.
ALTER TABLE bordereau_position_event
    ADD COLUMN IF NOT EXISTS event_reverses_key TEXT,
    ADD COLUMN IF NOT EXISTS event_reverses_ref TEXT,
    ADD COLUMN IF NOT EXISTS event_row_period   TEXT;

CREATE INDEX IF NOT EXISTS ix_bordereau_position_event_arrival
    ON bordereau_position_event (event_file_arrival_id, event_landing_id);
CREATE INDEX IF NOT EXISTS ix_bordereau_position_event_landing
    ON bordereau_position_event (event_landing_id, event_status);
CREATE INDEX IF NOT EXISTS ix_bordereau_position_event_position
    ON bordereau_position_event (event_position_id, event_id);
-- "The latest ITD this feed has loaded" — asked before every ITD file.
CREATE INDEX IF NOT EXISTS ix_bordereau_position_event_feed
    ON bordereau_position_event (tenant_id, event_program_id, event_broker_party_id,
                                 event_load_type, event_status);

-- ── the loaded transaction ──────────────────────────────────────────────────
ALTER TABLE premium_transaction
    ADD COLUMN IF NOT EXISTS premium_transaction_source_ref           TEXT,
    ADD COLUMN IF NOT EXISTS premium_transaction_itd_adjusts_row_key  TEXT,
    ADD COLUMN IF NOT EXISTS premium_transaction_itd_reverses_row_key TEXT;

-- The ITD load skips a transaction already loaded, by its row key.
CREATE INDEX IF NOT EXISTS ix_premium_transaction_row_key
    ON premium_transaction (premium_transaction_row_key)
 WHERE premium_transaction_row_key IS NOT NULL;

COMMIT;
