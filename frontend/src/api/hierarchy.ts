/**
 * The carrier hierarchy: Carrier → Programme → Broker → Contract.
 *
 * One module because every screen in the Phase 1 flow draws some slice of the
 * same shape, and the types below are that shape stated once.
 */
import { api } from "./client";
import { currentMga } from "../auth";

export type ApprovalStatus = "approved" | "pending_approval" | "rejected";

export type HierarchyContract = {
  id: number;
  filename: string | null;
  approval_status: ApprovalStatus;
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
  pending_approvals: number;
  user_count: number;
};

export type ProgrammeBroker = BrokerSummary & {
  link_id: number;
  status: string;
  assigned_at: string | null;
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
  contracts: {
    id: number; filename: string | null; program_id: number | null;
    status: string | null; approval_status: ApprovalStatus;
    inception_dt: string | null; expiry_dt: string | null; created_at: string | null;
  }[];
  users: {
    id: number; full_name: string; email: string;
    role: string; status: string; accepted_at: string | null;
  }[];
};

export type PendingApproval = {
  contract_id: number;
  filename: string | null;
  /** What is actually being decided on. A filename is not a contract — the
   *  queue has to say which contract, with whom and of what kind before anyone
   *  can decide without opening it. */
  name: string;
  contract_type: string | null;
  umr: string | null;
  class_of_business: string | null;
  programme: { id: number; name: string } | null;
  broker: { id: number; legal_name: string } | null;
  submitted_at: string | null;
  submitted_by: { id: number; full_name: string; email: string } | null;
  inception_dt: string | null;
  expiry_dt: string | null;
};

/** One act on a contract. Covers BOTH directions of travel: the broker→carrier
 *  approval gate, and the carrier→broker negotiation. Together they are the
 *  contract's thread — how it got to where it is. */
export type ApprovalEvent = {
  action: "submitted" | "approved" | "rejected" | "withdrawn"
        | "sent_for_review" | "changes_requested" | "terms_agreed";
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

export const getApprovals = () =>
  api.get<PendingApproval[]>("/approvals").then(r => r.data);

export const approveContract = (contractId: number, note?: string) =>
  api.post(`/contracts/${contractId}/approve`, { note: note ?? null }).then(r => r.data);

export const rejectContract = (contractId: number, note: string) =>
  api.post(`/contracts/${contractId}/reject`, { note }).then(r => r.data);

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
