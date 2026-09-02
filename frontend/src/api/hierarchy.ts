/**
 * The carrier hierarchy: Carrier → Programme → Broker → Contract.
 *
 * One module because every screen in the Phase 1 flow draws some slice of the
 * same shape, and the types below are that shape stated once.
 */
import { api } from "./client";

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
  programme: { id: number; name: string } | null;
  broker: { id: number; legal_name: string } | null;
  submitted_at: string | null;
  submitted_by: { id: number; full_name: string; email: string } | null;
  inception_dt: string | null;
  expiry_dt: string | null;
};

export type ApprovalEvent = {
  action: "submitted" | "approved" | "rejected" | "withdrawn";
  note: string | null;
  acted_at: string | null;
  acted_by: { id: number; full_name: string; email: string } | null;
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
