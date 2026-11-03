-- ============================================================================
-- Step 4 — a one-time code on top of the signing link.
--
-- WHAT THIS ACTUALLY DEFENDS AGAINST
-- ----------------------------------
-- Until now the emailed link WAS the credential: anyone holding the URL could
-- open the contract and sign it. A URL leaks in ways a mailbox does not — it
-- ends up in browser history, in a chat message, in a screen share, in a proxy
-- log, on a shoulder-surfed address bar, and above all in a forwarded email
-- where the sender only meant to ask a colleague's opinion.
--
-- The code closes that gap: holding the URL is no longer enough. It does NOT
-- defend against somebody who has taken over the mailbox itself — the code is
-- in the same message as the link — and it is not sold as if it did. Sending
-- the code out of band (SMS, or read out on a call) is what defends against
-- that, and `recipient_otp_hash` is agnostic about how the code got there, so
-- that is a delivery change and not a schema one.
--
-- WHAT IS AND IS NOT STORED
-- -------------------------
-- The code is stored as a bcrypt HASH, never in plain text, exactly like a
-- password (auth_utils.hash_password). A signing code sitting readable in a
-- table is a signing code readable by anyone with a database backup.
--
-- Six digits is a small space, so the hash alone is not the protection —
-- `recipient_otp_attempts` and `recipient_otp_locked_until` are. Five wrong
-- guesses locks the link for fifteen minutes, which turns a one-in-a-million
-- guess into an attack that takes years and is loud in the audit trail long
-- before it succeeds.
--
-- Idempotent: safe to run more than once.
-- ============================================================================

BEGIN;

ALTER TABLE contract_esign_recipient
    -- bcrypt hash of the six-digit code. NULL means no code was ever issued —
    -- which is what every row created before this migration means, and those
    -- links keep working (see the note in esign_routes._require_unlocked).
    ADD COLUMN IF NOT EXISTS recipient_otp_hash        TEXT,
    -- Codes die with the link they came in. A live link always has a usable
    -- code, and an expired one cannot be unlocked by a code kept from an older
    -- email.
    ADD COLUMN IF NOT EXISTS recipient_otp_expires     TIMESTAMPTZ,
    -- Consecutive failures. Reset to 0 the moment a correct code is entered.
    ADD COLUMN IF NOT EXISTS recipient_otp_attempts    INTEGER DEFAULT 0,
    -- Set when the attempts run out. Until this passes, no code is accepted —
    -- not even the right one.
    ADD COLUMN IF NOT EXISTS recipient_otp_locked_until TIMESTAMPTZ,
    -- When the signer last got past the code. Part of the audit trail: "they
    -- proved they had the email at 14:03, and signed at 14:06" is a stronger
    -- record than the signature on its own.
    ADD COLUMN IF NOT EXISTS recipient_otp_verified_at TIMESTAMPTZ,
    -- How many times a fresh code has been emailed for this link, so a resend
    -- button cannot be used to mail somebody hundreds of messages.
    ADD COLUMN IF NOT EXISTS recipient_otp_sent_count  INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS recipient_otp_last_sent_at TIMESTAMPTZ;

-- Rows that predate this migration have NULL attempts, and NULL + 1 is NULL —
-- a counter that never increments is a lockout that never triggers.
UPDATE contract_esign_recipient
   SET recipient_otp_attempts = 0
 WHERE recipient_otp_attempts IS NULL;

UPDATE contract_esign_recipient
   SET recipient_otp_sent_count = 0
 WHERE recipient_otp_sent_count IS NULL;

ALTER TABLE contract_esign_recipient
    ALTER COLUMN recipient_otp_attempts   SET DEFAULT 0,
    ALTER COLUMN recipient_otp_attempts   SET NOT NULL,
    ALTER COLUMN recipient_otp_sent_count SET DEFAULT 0,
    ALTER COLUMN recipient_otp_sent_count SET NOT NULL;

COMMIT;
