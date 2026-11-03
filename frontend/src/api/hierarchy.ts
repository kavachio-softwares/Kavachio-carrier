/**
 * The carrier hierarchy: Carrier → Programme → Broker → Contract.
 *
 * One module because every screen in the Phase 1 flow draws some slice of the
 * same shape, and the types below are that shape stated once.
 */
import { api } from "./client";
import { currentMga } from "../auth";

export type HierarchyContract = {
  id: number;
  filename: string | null;
  /** Short code a broker can write in an email subject or file name instead
   *  of the name, e.g. CTR-7824YC. */
  code?: string;
  status: string | null;
  /** The business state (draft … active). `status` is what extraction did. */
  lifecycle?: string | null;
  /** Finished enough to build a bordereau setup on — the SAME rule the setup
   *  activation gate enforces (direct_routes._pipeline_ready), not merely
   *  "lifecycle is active". A contract the carrier has signed but the broker
   *  has not yet countersigned answers true here while its lifecycle still
   *  reads "agreed" — the broker's signature is not waited for. Absent on an
   *  older server; treat missing as unknown, not as settled. */
  settled?: boolean;
  /** Is it the CARRIER ADMIN's move on this one right now — the same answer
   *  the contract record's own banner, the dashboard's "Waiting on You" tile
   *  and the notification bell all give (contract_routes._carrier_admin_turn).
   *  Lets a screen offer a "Review contract" action only where there truly is
   *  something to review, rather than a plain "Open" that reads the same
   *  whether the ball is in the admin's court or the broker's. */
  awaiting_carrier_admin?: boolean;
};

export type HierarchyBroker = {
  id: number;
  legal_name: string;
  /** active | pending_approval | inactive — a broker taken off a programme that
   *  still has contracts stays here as inactive rather than disappearing, and
   *  one a carrier USER put on waits at pending_approval until the carrier
   *  admin approves the setup built on it. Test it with onProgramme(), never
   *  against "active": from the CARRIER's side a waiting link is a broker they
   *  are working with, and reading it as "taken off" stops the chain one step
   *  after they started it. */
  link_status: string;
  /** The best bordereau setup this broker has on the programme. */
  setup_status?: "active" | "pending_approval" | "draft" | null;
  contracts: HierarchyContract[];
};

/** A broker a carrier user has ASKED to put on this programme, still waiting
 *  on their carrier admin.
 *
 *  NOT a broker on the programme, and never counted as one: no contract can
 *  hang off it and no setup can be built on it. It exists so the screens stop
 *  saying "add a broker" to somebody who just did — which is what sent people
 *  round the loop of adding the same broker again and being refused. */
export type AwaitingBroker = {
  request_id: number;
  broker_party_id: number | null;
  legal_name: string | null;
};

/** Is this broker ON the programme, as the CARRIER sees it?
 *
 *  The twin of carrier_scope.link_is_live on the server, and it has to agree
 *  with it: a link waiting for the carrier admin is real — the carrier can
 *  raise the contract and build the setup on it — it simply has not been
 *  released to the broker yet. Only "inactive" means taken off. A missing
 *  value is a legacy row, which has always meant on. */
export const onProgramme = (linkStatus?: string | null): boolean =>
  !linkStatus || linkStatus === "active" || linkStatus === "pending_approval";

/** Has the carrier FINISHED with this broker's setup?
 *
 *  Not the same question as whether the broker can send a file — only a live
 *  setup lets them do that. This is "is there anything left for the person
 *  building it to do", and for one waiting on the carrier admin there is not.
 *  Pointing them back at the builder would ask them to make a second copy of
 *  the setup they just sent up. */
export const setupFinished = (setupStatus?: string | null): boolean =>
  setupStatus === "active" || setupStatus === "pending_approval";

export type HierarchyProgramme = {
  id: number;
  name: string;
  /** Short code a broker can write instead of the name, e.g. PRG-5QSH84. */
  code?: string;
  status: string | null;
  business_segment: string | null;
  product_line: string | null;
  bdx_frequency: string | null;
  created_at?: string | null;
  broker_count: number;
  contract_count: number;
  brokers: HierarchyBroker[];
  /** Asked for, not yet approved. Deliberately NOT in broker_count — see
   *  AwaitingBroker. Absent on an older server. */
  brokers_awaiting?: AwaitingBroker[];
};

export type Hierarchy = { tenant_id: number; programmes: HierarchyProgramme[] };

export type BrokerSummary = {
  id: number;
  legal_name: string;
  dba_name?: string | null;
  party_type: string;
  is_active: boolean;
  /** Derived by the DB from their people: not_invited | invited | active | suspended. */
  onboarding_status: string | null;
  created_at: string | null;
  programmes: { id: number; name: string; status: string }[];
  contract_count: number;
  user_count: number;
  /** THE RELATIONSHIP WITH THIS CARRIER, not a property of the broker. The
   *  same broker is `active` to one carrier and `invited` to another who is
   *  still waiting for an answer. */
  relationship?: "active" | "invited";
  /** by_user_id: the carrier user who sent it — the only one who may resend
   *  or withdraw it. */
  invitation?: { id: number; email: string; invited_at: string | null;
                 by_user_id?: number | null } | null;
};

export const resendBrokerInvitation = (id: number) =>
  api.post<{ message?: string }>(`/broker-invitations/${id}/resend`, {})
     .then(r => r.data);

export const revokeBrokerInvitation = (id: number) =>
  api.delete<{ message?: string }>(`/broker-invitations/${id}`).then(r => r.data);

export type ProgrammeBroker = BrokerSummary & {
  link_id: number;
  status: string;
  assigned_at: string | null;
};

/** One row of a broker's contract table. No longer part of BrokerDetail: the
 *  table asks for a page of these at a time (listBrokerContracts). */
export type BrokerContractRow = {
  id: number;
  /** What the carrier called it. A contract WRITTEN here has no file, so this
   *  is the only name it has — and it is the one every other screen shows. */
  name: string | null;
  filename: string | null; program_id: number | null;
  /** Written here rather than uploaded, which decides where its name leads:
   *  its own record, not the page that reads clauses out of a document. */
  is_app_managed: boolean;
  status: string | null;
  inception_dt: string | null; expiry_dt: string | null; created_at: string | null;
};

export type BrokerDetail = {
  id: number;
  legal_name: string;
  dba_name?: string | null;
  party_type: string;
  is_active: boolean;
  /** Derived by the DB from their people: not_invited | invited | active | suspended. */
  onboarding_status: string | null;
  created_at: string | null;
  programmes: { id: number; name: string; status: string; assigned_at: string | null }[];
  users: {
    id: number; full_name: string; email: string;
    role: string; status: string; accepted_at: string | null;
  }[];
};

/** One act on a contract — the carrier→broker negotiation thread: how it got
 *  to where it is. `approved` / `rejected` are kept in the union because rows
 *  written before the carrier's approval gate was removed are still on the
 *  record, and a thread that cannot name what happened is worse than one
 *  carrying a word nothing writes any more. */
export type ApprovalEvent = {
  action: "submitted" | "approved" | "rejected" | "withdrawn"
        | "sent_for_review" | "changes_requested" | "terms_agreed"
        // The carrier settling the terms alone, without a review. Same
        // destination as terms_agreed and a different fact, which is the whole
        // reason it is written down under its own name.
        | "review_skipped";
  note: string | null;
  acted_at: string | null;
  /** A broker user's row is shown to the carrier as the broker company:
   *  id and email are then null. */
  acted_by: { id: number | null; full_name: string; email: string | null } | null;
  /** The terms named by a change request. Empty for every other action. */
  proposed_changes: Array<{
    field: string; current?: string | null;
    proposed?: string | null; comment?: string | null;
  }>;
};

export const getHierarchy = () =>
  api.get<Hierarchy>("/hierarchy").then(r => r.data);

export const getBrokers = () =>
  api.get<BrokerSummary[]>("/brokers").then(r => r.data);

/** One page of the broker directory, searched and counted by the server.
 *
 *  Left alongside the unpaged getBrokers above, which still has callers that
 *  need every broker at once to fill a picker (Add Programme, and the
 *  programme's own broker screen).
 *
 *  `stranded` counts brokers on no programme across the WHOLE directory, not
 *  this page — the screen states it above the table as a fact about the book. */
export const getBrokersPaged = (params: {
  q?: string; page: number; page_size: number;
  /** The Party screen's view: a carrier user gets only the broker companies
   *  they invited. No effect for the carrier admin, whose reach is the whole
   *  company. The programme pickers (getBrokers) leave it off. */
  mine?: boolean;
}) =>
  api.get<{ items: BrokerSummary[]; total: number; stranded: number }>(
    "/brokers", { params }).then(r => r.data);

export const getBroker = (brokerId: number) =>
  api.get<BrokerDetail>(`/brokers/${brokerId}`).then(r => r.data);

/** One page of a broker's contracts, filtered and counted by the server. */
export const listBrokerContracts = (brokerId: number, params: {
  q?: string; program_id?: number; limit: number; offset: number;
}) =>
  api.get<{ contracts: BrokerContractRow[]; total: number }>(
    `/brokers/${brokerId}/contracts`, { params }).then(r => r.data);

export const getProgrammeBrokers = (programId: number) =>
  api.get<ProgrammeBroker[]>(`/programs/${programId}/brokers`).then(r => r.data);

/** Put a broker on a programme.
 *
 *  WHAT THIS DOES DEPENDS ON THE SEAT, and the server is what knows. A carrier
 *  ADMIN's call links the broker as it always did. A carrier USER's raises a
 *  BROKER ONBOARDING REQUEST for their carrier admin instead — `pending` is
 *  true and `link_id` is null. Nothing is linked and nothing is sent to the
 *  broker until that is approved.
 *
 *  Callers must read `pending` and say so, rather than reporting "added". */
export const addProgrammeBroker = (programId: number, brokerPartyId: number) =>
  api.post(`/programs/${programId}/brokers`, { broker_party_id: brokerPartyId })
     .then(r => r.data as {
       ok: boolean; reactivated: boolean; link_id: number | null;
       /** True when this became a request for the carrier admin. */
       pending?: boolean; request_id?: number; message?: string;
       /** Set only for a broker taken off this programme before: they are
        *  emailed that they are back (a first-time add emails nobody). */
       added_back?: boolean; emailed?: number; email_failed?: string[];
       no_contact?: boolean;
     });

/** Removing a pair that already carries contracts DEACTIVATES it — the
 *  response says which happened so the UI can tell the truth about it.
 *  The broker is emailed as part of the removal: `emailed` is how many
 *  contacts the notice reached, `email_failed` the addresses it did not, and
 *  `no_contact` that nobody on record could be emailed at all. */
export const removeProgrammeBroker = (programId: number, brokerPartyId: number) =>
  api.delete(`/programs/${programId}/brokers/${brokerPartyId}`)
     .then(r => r.data as {
       ok: boolean; deactivated: boolean; contract_count: number; message?: string;
       already?: boolean; emailed?: number; email_failed?: string[]; no_contact?: boolean;
     });

export const getApprovalHistory = (contractId: number) =>
  api.get<ApprovalEvent[]>(`/contracts/${contractId}/approvals`).then(r => r.data);

/** Bring a broker on board: the organisation, its first admin and (optionally)
 *  the programme it produces into, in one call. Doing them separately is what
 *  used to leave a carrier with a broker nobody could sign in as. */
/** Invite a broker onto a programme.
 *
 *  FOR A CARRIER USER THIS ONLY ASKS. The server raises a broker onboarding
 *  request for their carrier admin and sends the broker nothing at all;
 *  `invited` comes back false and `pending` true. For a carrier admin it
 *  onboards and mails, exactly as before.
 *
 *  The response is the SAME whether or not that address already has a login —
 *  which of the two it is depends on facts about another carrier's book, and
 *  the inviting carrier does not get to learn them. An existing broker sees
 *  the invitation on their own screen; a new one is onboarded, and completing
 *  onboarding accepts it for them. Either way the link appears only once the
 *  broker has agreed. */
export const inviteBroker = (body: {
  legal_name: string;
  party_type?: string;
  admin_name?: string;
  admin_email: string;
  /** The programme this broker is being brought on FOR, when the invite was
   *  raised from one.
   *
   *  Carried all the way through: it is stamped on the request, so the carrier
   *  admin's queue shows which programme they are approving for rather than
   *  "No programme yet", and on approval the programme link is written with
   *  the rest of the onboarding. Without it the invite and the programme were
   *  two separate asks, and the second one could only be made after the first
   *  was approved.
   *
   *  Left off by the Party screen, which genuinely has no programme — a broker
   *  can be brought into the directory before anyone decides where they
   *  produce. */
  program_id?: number;
}) => api.post<{
  ok: boolean;
  /** FALSE when a carrier user raised a request instead — nothing has been
   *  sent to the broker, and `message` says so. Never render "invitation
   *  sent" without checking this. */
  invited: boolean;
  email: string; message: string;
  pending?: boolean; request_id?: number;
}>("/brokers", body).then(r => r.data);


// ── creating the things a contract needs to exist ──────────────────────────
// A contract sits on a programme and is held with a counterparty. When either
// is missing, the Raise Contract flow creates it in place rather than sending
// the user away to another screen and losing what they had typed.
//
// These call the SAME endpoints the dedicated screens do — nothing new on the
// server, and no second way for a programme or a broker to come into
// existence. Note both `/programs` and `/parties` still take the legacy `mga`
// query param; the tenant is resolved from the token regardless.

/** Create a programme. Only the name is required — everything else about a
 *  programme can be filled in later on its own screen. */
export const createProgramme = (body: {
  name: string;
  business_segment?: string | null;
  product_line?: string | null;
  bdx_frequency?: string | null;
  status?: string | null;
}) => api.post<{ id: number; name: string }>("/programs", body,
                                             { params: { mga: currentMga() } })
        .then(r => r.data);

/** Create a reinsurer in the carrier's directory.
 *
 *  Deliberately not `createBroker`: that endpoint refuses anything outside
 *  PRODUCER_PARTY_TYPES, because it also invites an admin and puts the party on
 *  a programme — neither of which applies to a reinsurer, which has no seat in
 *  Kavachio and produces nothing into a programme. */
export const createReinsurer = (legalName: string) =>
  api.post<{ id: number; legal_name: string }>(
    "/parties",
    { party_type: "reinsurer", legal_name: legalName, scope: "tenant" },
    { params: { mga: currentMga() } },
  ).then(r => r.data);
