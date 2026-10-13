-- Migration 39 — clearer names for the two ITD tables of migration 38.
--
--   bordereau_position        → bdx_sent_transaction  (columns position_* → sent_*)
--     every transaction already sent to the carrier, once each
--   bordereau_position_event  → bdx_row_comparison    (columns event_* → comparison_*;
--                                                       event_position_id → comparison_sent_id)
--     every row of every file, and what the comparison decided for it
--
-- The rows stay where they are. Safe to run again, and on a database that
-- never had the old names. db.init_db does the same at start-up
-- (itd_store.rename_old_tables) when the app owns its schema.

DO $$
DECLARE
    c record;
BEGIN
    IF to_regclass('bordereau_position') IS NOT NULL
       AND to_regclass('bdx_sent_transaction') IS NULL THEN
        ALTER TABLE bordereau_position RENAME TO bdx_sent_transaction;
    END IF;
    IF to_regclass('bordereau_position_event') IS NOT NULL
       AND to_regclass('bdx_row_comparison') IS NULL THEN
        ALTER TABLE bordereau_position_event RENAME TO bdx_row_comparison;
    END IF;

    FOR c IN SELECT column_name FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'bdx_sent_transaction'
               AND column_name LIKE 'position\_%' LOOP
        EXECUTE format('ALTER TABLE bdx_sent_transaction RENAME COLUMN %I TO %I',
                       c.column_name, 'sent_' || substr(c.column_name, 10));
    END LOOP;

    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'bdx_row_comparison'
                 AND column_name = 'event_position_id') THEN
        ALTER TABLE bdx_row_comparison RENAME COLUMN event_position_id TO comparison_sent_id;
    END IF;
    FOR c IN SELECT column_name FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = 'bdx_row_comparison'
               AND column_name LIKE 'event\_%' LOOP
        EXECUTE format('ALTER TABLE bdx_row_comparison RENAME COLUMN %I TO %I',
                       c.column_name, 'comparison_' || substr(c.column_name, 7));
    END LOOP;

    -- Indexes and sequences keep their old names through a table rename.
    IF to_regclass('uq_bordereau_position_feed_key') IS NOT NULL THEN
        ALTER INDEX uq_bordereau_position_feed_key RENAME TO uq_bdx_sent_transaction_feed_key;
    END IF;
    IF to_regclass('ix_bordereau_position_feed') IS NOT NULL THEN
        ALTER INDEX ix_bordereau_position_feed RENAME TO ix_bdx_sent_transaction_feed;
    END IF;
    IF to_regclass('ix_bordereau_position_event_arrival') IS NOT NULL THEN
        ALTER INDEX ix_bordereau_position_event_arrival RENAME TO ix_bdx_row_comparison_arrival;
    END IF;
    IF to_regclass('ix_bordereau_position_event_landing') IS NOT NULL THEN
        ALTER INDEX ix_bordereau_position_event_landing RENAME TO ix_bdx_row_comparison_landing;
    END IF;
    IF to_regclass('ix_bordereau_position_event_position') IS NOT NULL THEN
        ALTER INDEX ix_bordereau_position_event_position RENAME TO ix_bdx_row_comparison_sent;
    END IF;
    IF to_regclass('ix_bordereau_position_event_feed') IS NOT NULL THEN
        ALTER INDEX ix_bordereau_position_event_feed RENAME TO ix_bdx_row_comparison_feed;
    END IF;
    IF to_regclass('bordereau_position_position_id_seq') IS NOT NULL THEN
        ALTER SEQUENCE bordereau_position_position_id_seq RENAME TO bdx_sent_transaction_sent_id_seq;
    END IF;
    IF to_regclass('bordereau_position_event_event_id_seq') IS NOT NULL THEN
        ALTER SEQUENCE bordereau_position_event_event_id_seq
            RENAME TO bdx_row_comparison_comparison_id_seq;
    END IF;
END $$;
