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
  status: string | null;
};

export type HierarchyBroker = {
  id: number;
  legal_name: string;
  /** active | inactive — a broker taken off a programme that still has
   *  contracts stays here as inactive rather than disappearing. */
  link_status: string;
  contracts: HierarchyContract[];
};

export type HierarchyProgramme = {
  id: number;
  name: string;
  status: string | null;
  business_segment: string | null;
  product_line: string | null;
  bdx_frequency: string | null;
  broker_count: number;
  contract_count: number;
  brokers: HierarchyBroker[];
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
};

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
  acted_by: { id: number; full_name: string; email: string } | null;
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

export const addProgrammeBroker = (programId: number, brokerPartyId: number) =>
  api.post(`/programs/${programId}/brokers`, { broker_party_id: brokerPartyId })
     .then(r => r.data as { ok: boolean; reactivated: boolean; link_id: number });

/** Removing a pair that already carries contracts DEACTIVATES it — the
 *  response says which happened so the UI can tell the truth about it. */
export const removeProgrammeBroker = (programId: number, brokerPartyId: number) =>
  api.delete(`/programs/${programId}/brokers/${brokerPartyId}`)
     .then(r => r.data as { ok: boolean; deactivated: boolean; contract_count: number; message?: string });

export const getApprovalHistory = (contractId: number) =>
  api.get<ApprovalEvent[]>(`/contracts/${contractId}/approvals`).then(r => r.data);

/** Bring a broker on board: the organisation, its first admin and (optionally)
 *  the programme it produces into, in one call. Doing them separately is what
 *  used to leave a carrier with a broker nobody could sign in as. */
export const createBroker = (body: {
  legal_name: string;
  party_type?: string;
  admin_name?: string;
  admin_email?: string;
  program_id?: number;
}) => api.post<BrokerSummary & {
  admin_invited: boolean; admin_email: string | null; program_id: number | null;
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
