-- ---------------------------------------------------------------------------
-- Trigger functions, re-pointed at the v4 column names.
--
-- Postgres does NOT track column references inside a PL/pgSQL body: a function
-- reading OLD.broker_party_id keeps compiling after the column is renamed and
-- fails only when the trigger actually fires. That is why the column drop
-- succeeded and then broke login on the next UPDATE of app_user.
--
-- These are the same rules as before, with only the column names changed:
--   app_user : email/full_name/role/status/tenant_id/broker_party_id/
--              invited_by_user_id  ->  user_*
--   party    : is_active                                 -> party_is_active
--   contract : approval_status / submitted_by_user_id    -> contract_*
-- ---------------------------------------------------------------------------

-- Which onboarding state a broker party is in, from the people attached to it.
CREATE OR REPLACE FUNCTION public.broker_onboarding_state(
    p_party_id bigint, p_is_active boolean)
RETURNS text LANGUAGE plpgsql STABLE AS $function$
BEGIN
  IF p_is_active IS FALSE THEN
    RETURN 'suspended';
  END IF;

  IF EXISTS (SELECT 1 FROM app_user
              WHERE user_broker_party_id = p_party_id AND user_status = 'active') THEN
    RETURN 'active';
  END IF;

  IF EXISTS (SELECT 1 FROM app_user WHERE user_broker_party_id = p_party_id) THEN
    RETURN 'invited';
  END IF;

  RETURN 'not_invited';
END;
$function$;


-- A person joining/leaving a broker changes that broker's onboarding state.
CREATE OR REPLACE FUNCTION public.refresh_broker_onboarding()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
  old_id bigint := NULL;
  new_id bigint := NULL;
BEGIN
  IF TG_OP <> 'INSERT' THEN old_id := OLD.user_broker_party_id; END IF;
  IF TG_OP <> 'DELETE' THEN new_id := NEW.user_broker_party_id; END IF;

  UPDATE party
     SET onboarding_status = broker_onboarding_state(party_id, party_is_active)
   WHERE party_id IN (old_id, new_id)
     AND onboarding_status IS DISTINCT FROM
         broker_onboarding_state(party_id, party_is_active);

  RETURN NULL;
END;
$function$;


-- onboarding_status is derived, never written by the application.
CREATE OR REPLACE FUNCTION public.set_broker_onboarding()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
  IF NEW.party_type::text IN ('broker', 'mga', 'mgu', 'tpa') THEN
    NEW.onboarding_status := broker_onboarding_state(NEW.party_id, NEW.party_is_active);
  ELSE
    NEW.onboarding_status := NULL;
  END IF;
  RETURN NEW;
END;
$function$;


-- The ONE approval: who uploaded it decides whether it waits. A client can
-- never assert "approved" — this reads the submitter's own role.
CREATE OR REPLACE FUNCTION public.set_contract_approval()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE submitter app_user%ROWTYPE;
BEGIN
  IF NEW.contract_submitted_by_id IS NOT NULL THEN
    SELECT * INTO submitter FROM app_user WHERE user_id = NEW.contract_submitted_by_id;
    IF FOUND AND submitter.user_role IN ('broker_admin', 'operator') THEN
      NEW.contract_approval_status := 'pending_approval';
      NEW.submitted_at := COALESCE(NEW.submitted_at, now());
      RETURN NEW;
    END IF;
  END IF;
  NEW.contract_approval_status := COALESCE(NEW.contract_approval_status, 'approved');
  RETURN NEW;
END;
$function$;


-- Who may approve, reject or submit a contract.
CREATE OR REPLACE FUNCTION public.enforce_approval_authority()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
  actor        app_user%ROWTYPE;
  owner_tenant bigint;
BEGIN
  SELECT * INTO actor FROM app_user WHERE user_id = NEW.approval_acted_by_id;
  SELECT tenant_id INTO owner_tenant FROM contract
   WHERE contract_id = NEW.approval_contract_id;

  IF NEW.approval_action IN ('approved', 'rejected') THEN
    IF actor.user_role NOT IN ('carrier_admin', 'kavachio_admin') THEN
      RAISE EXCEPTION 'Only a carrier admin may approve or reject a contract (% tried)', actor.user_role;
    END IF;
    IF actor.user_role = 'carrier_admin'
       AND actor.user_tenant_id IS DISTINCT FROM owner_tenant THEN
      RAISE EXCEPTION 'A carrier may only approve contracts on its own programmes';
    END IF;
  END IF;

  IF NEW.approval_action = 'submitted'
     AND actor.user_role NOT IN ('broker_admin', 'operator', 'carrier_admin') THEN
    RAISE EXCEPTION 'Only a broker seat or a carrier admin may submit a contract (% tried)', actor.user_role;
  END IF;

  RETURN NEW;
END;
$function$;


-- Every login except the first Kavachio account records who let it in.
CREATE OR REPLACE FUNCTION public.enforce_invitation_chain()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE inviter app_user%ROWTYPE;
BEGIN
  IF NEW.user_invited_by_id IS NULL THEN
    IF NEW.user_role = 'kavachio_admin'
       AND NOT EXISTS (SELECT 1 FROM app_user WHERE user_role = 'kavachio_admin') THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Every login except the first Kavachio account must record who invited it (user %, role %)',
                    NEW.user_email, NEW.user_role;
  END IF;

  SELECT * INTO inviter FROM app_user WHERE user_id = NEW.user_invited_by_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'user_invited_by_id % does not exist', NEW.user_invited_by_id;
  END IF;

  -- Who may invite whom.
  --   Kavachio  → a carrier's first admin
  --   a carrier → its own colleagues, and a broker's first admin
  --   a broker  → its own operators, and nothing else
  IF NEW.user_role = 'carrier_admin'
     AND inviter.user_role NOT IN ('kavachio_admin', 'carrier_admin') THEN
    RAISE EXCEPTION 'A carrier admin is invited by Kavachio or by another carrier admin (% tried)', inviter.user_role;
  END IF;
  IF NEW.user_role = 'broker_admin'
     AND inviter.user_role NOT IN ('kavachio_admin', 'carrier_admin') THEN
    RAISE EXCEPTION 'Only Kavachio or a carrier admin can invite a broker admin (% tried)', inviter.user_role;
  END IF;
  IF NEW.user_role = 'operator'
     AND inviter.user_role NOT IN ('kavachio_admin', 'broker_admin') THEN
    RAISE EXCEPTION 'An operator is created by its own broker admin (% tried)', inviter.user_role;
  END IF;

  RETURN NEW;
END;
$function$;


-- A bordereau can only be processed against an approved contract.
CREATE OR REPLACE FUNCTION public.enforce_live_contract_for_upload()
RETURNS trigger LANGUAGE plpgsql AS $function$
DECLARE
  st text;
BEGIN
  SELECT contract_approval_status INTO st FROM contract
   WHERE contract_id = NEW.contract_id;
  IF st IS DISTINCT FROM 'approved' THEN
    RAISE EXCEPTION 'Contract % is % — a bordereau can only be processed against an approved contract',
                    NEW.contract_id, COALESCE(st, 'missing');
  END IF;
  RETURN NEW;
END;
$function$;
