-- ============================================================================
-- The organisation's OWNER — one named person accountable for a carrier org.
--
-- WHY A COLUMN AND NOT A ROLE. Every carrier organisation already has any
-- number of carrier_admins, and they are deliberately equal: each can invite
-- colleagues, approve contracts and manage brokers. What none of them is, is
-- ACCOUNTABLE. "Who owns this organisation" had no answer, so it could not be
-- transferred, and there was nothing to stop the last accountable person being
-- removed by a colleague they had themselves invited.
--
-- Owner is therefore a pointer, not a fourth role: exactly one user per
-- organisation, always one of its own carrier_admins. Keeping it out of
-- `user_role` means an owner loses nothing when ownership moves — they stay a
-- carrier_admin and keep working — and it stays impossible for two rows to
-- disagree about who the owner is, because there is only one place to look.
--
-- WHAT IT MAKES POSSIBLE
--   * Kavachio admin creates the organisation and names its first owner.
--   * The owner invites more carrier_admins into the same organisation.
--   * The owner hands ownership to one of them, by email.
--   * The new owner may then deactivate the previous one — which is refused
--     while they still hold ownership, so an organisation is never left
--     without an owner by a single click.
--
-- ON DELETE SET NULL: an organisation whose owner row somehow disappears is
-- ownerless, which is a state the app can see and fix. A dangling id is not.
-- ============================================================================

ALTER TABLE tenant
    ADD COLUMN IF NOT EXISTS tenant_owner_user_id BIGINT
        REFERENCES app_user (user_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_tenant_owner_user
    ON tenant (tenant_owner_user_id);

-- Backfill: the organisation's FIRST carrier_admin is the person the Kavachio
-- admin invited when the organisation was created, which is exactly who the
-- owner was always meant to be. Only where that is unambiguous — an
-- organisation with no admin at all is left ownerless rather than guessed at,
-- and shows in the UI as needing one.
UPDATE tenant t
   SET tenant_owner_user_id = sub.user_id
  FROM (
        SELECT DISTINCT ON (u.user_tenant_id)
               u.user_tenant_id, u.user_id
          FROM app_user u
         WHERE u.user_tenant_id IS NOT NULL
           AND lower(u.user_role) IN ('carrier_admin', 'tenant_admin', 'admin')
           AND COALESCE(u.user_status, 'active') <> 'suspended'
         ORDER BY u.user_tenant_id, u.created_at ASC NULLS LAST, u.user_id ASC
       ) sub
 WHERE t.tenant_id = sub.user_tenant_id
   AND t.tenant_owner_user_id IS NULL;
