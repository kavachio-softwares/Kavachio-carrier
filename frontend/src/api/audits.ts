// Audit Management (backend audit_management.py, 53.1 / 53.2): audits the
// carrier records against its brokers and their contracts, and each finding
// followed to closure. A broker sees only the findings about itself.
import { api, downloadFile } from "./client";
import { currentMga } from "../auth";

export interface FindingStep {
  id: number; kind: string; title: string; note: string | null; from: string | null; to: string | null;
  file: string | null; by: string; side: string | null; at: string | null;
}
export interface Finding {
  id: number; audit_id: number; title: string; severity: string; severity_label: string;
  action: string | null; owner: string | null; due_date: string | null;
  status: "open" | "in_progress" | "closed"; status_label: string; overdue: boolean;
  resolution: string | null; closed_at: string | null; created_at: string | null; broker: string | null;
  history: FindingStep[];
  // on the broker's list
  audit?: string | null; audit_ref?: string | null; audit_type?: string | null; contract?: string | null; carrier?: string | null;
}
export interface Audit {
  id: number; ref: string; name: string; type: string; type_label: string;
  broker_id: number; broker: string | null; contract_id: number | null; contract: string | null;
  scope: string | null; assigned_to: string | null; start_date: string | null; due_date: string | null;
  status: "planned" | "in_progress" | "closed"; status_label: string; overdue: boolean;
  findings_total: number; findings_open: number; closing_note: string | null; closed_at: string | null;
  findings?: Finding[];
}
export interface AuditList {
  ready: boolean; message?: string; audits: Audit[];
  counts: { total: number; in_progress: number; open_findings: number; closed: number };
  types: { key: string; label: string }[]; severities: { key: string; label: string }[];
  form: { brokers: { id: number; name: string }[]; contracts: { id: number; broker_id: number; name: string }[] };
}
export interface AuditInput {
  name: string; type: string; broker_party_id: number | ""; contract_id: number | ""; scope: string;
  assigned_to: string; start_date: string; due_date: string;
}
export interface FindingInput { title: string; severity: string; action: string; owner: string; due_date: string }

const m = () => ({ params: { mga: currentMga() || undefined } });
const nul = <T extends object>(o: T) =>
  Object.fromEntries(Object.entries(o).map(([k, v]) => [k, v === "" ? null : v]));

export const getAudits = (f: { broker?: number | ""; status?: string; from?: string; to?: string }) =>
  api.get<AuditList>("/audits", { params: { mga: currentMga() || undefined, broker: f.broker || undefined,
    status: f.status || undefined, since: f.from || undefined, until: f.to || undefined } }).then(r => r.data);
export const getAudit = (id: number) => api.get<Audit>(`/audits/${id}`, m()).then(r => r.data);
export const addAudit = (b: AuditInput) => api.post<Audit>("/audits", nul(b), m()).then(r => r.data);
export const editAudit = (id: number, b: AuditInput) => api.put<Audit>(`/audits/${id}`, nul(b), m()).then(r => r.data);
export const closeAudit = (id: number, note: string) =>
  api.post<Audit>(`/audits/${id}/close`, { note }, m()).then(r => r.data);
export const addFinding = (id: number, b: FindingInput) =>
  api.post<Audit>(`/audits/${id}/findings`, nul(b), m()).then(r => r.data);
export const editFinding = (id: number, fid: number, b: FindingInput) =>
  api.put<Audit>(`/audits/${id}/findings/${fid}`, nul(b), m()).then(r => r.data);
export const moveFinding = (id: number, fid: number, status: string, note: string) =>
  api.post<Audit>(`/audits/${id}/findings/${fid}/status`, { status, note }, m()).then(r => r.data);

const form = (note: string, file: File | null) => {
  const f = new FormData();
  if (note.trim()) f.append("note", note.trim());
  if (file) f.append("file", file);
  return f;
};
export const carrierReply = (id: number, fid: number, note: string, file: File | null) =>
  api.post<Audit>(`/audits/${id}/findings/${fid}/reply`, form(note, file), m()).then(r => r.data);
export const carrierFile = (id: number, fid: number, eid: number, name: string) =>
  downloadFile(`/audits/${id}/findings/${fid}/files/${eid}?mga=${encodeURIComponent(currentMga() || "")}`, name);

// ── the broker ─────────────────────────────────────────────────────────────
export const getBrokerFindings = () =>
  api.get<{ ready: boolean; findings: Finding[]; open: number }>("/broker/audit-findings").then(r => r.data);
export const brokerReply = (fid: number, note: string, file: File | null) =>
  api.post<{ ready: boolean; findings: Finding[]; open: number }>(`/broker/audit-findings/${fid}/reply`, form(note, file))
    .then(r => r.data);
export const brokerFile = (fid: number, eid: number, name: string) =>
  downloadFile(`/broker/audit-findings/${fid}/files/${eid}`, name);

// ── the carrier's dashboard card ───────────────────────────────────────────
export interface WaitingFinding {
  id: number; audit_id: number; audit: string | null; audit_ref: string | null; title: string;
  severity: string; severity_label: string; broker: string | null; due_date: string | null; replied_at?: string | null;
}
export const getAuditsWaiting = () =>
  api.get<{ ready: boolean; replied: WaitingFinding[]; overdue: WaitingFinding[] }>("/audits/waiting", m())
    .then(r => r.data);
