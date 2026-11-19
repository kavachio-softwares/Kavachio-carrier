// The Audit Logs tabs beyond the trail (backend audit_history.py): its tiles,
// Downloads & Views, Partner Submission History, Contract History and File
// Versions. The server decides whose rows come back, as for /audit/logs.
import { api, downloadFile } from "./client";
import { currentMga } from "../auth";
import type { AuditPage, AuditQuery } from "./audit";

/** Start / end of a local day as an instant — as api/audit.ts does. */
const dayStart = (d?: string) => {
  if (!d) return undefined;
  const [y, m, n] = d.split("-").map(Number);
  return new Date(y, m - 1, n, 0, 0, 0, 0).toISOString();
};
const dayEnd = (d?: string) => {
  if (!d) return undefined;
  const [y, m, n] = d.split("-").map(Number);
  return new Date(y, m - 1, n, 23, 59, 59, 999).toISOString();
};
const clean = (o: Record<string, unknown>) =>
  Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== ""));
const mga = () => ({ mga: currentMga() || undefined });

export interface TrailSummary { activities: number; people: number; viewed: number; downloaded: number; rule_changes: number }

export const getTrailSummary = (q: AuditQuery) =>
  api.get<TrailSummary>("/audit/history/summary", {
    params: clean({ from: dayStart(q.from), to: dayEnd(q.to), actor: q.actor,
                    action: q.action && q.action !== "all" ? q.action : undefined, q: q.q,
                    record: q.record }),
  }).then(r => r.data);

export type AccessPage = AuditPage & { counts: { viewed: number; downloaded: number; people: number } };

export const getAccess = (q: AuditQuery, kind: string, page: number, pageSize: number) =>
  api.get<AccessPage>("/audit/history/access", {
    params: clean({ from: dayStart(q.from), to: dayEnd(q.to), actor: q.actor, q: q.q, kind, page,
                    page_size: pageSize, record: q.record }),
  }).then(r => r.data);

export interface Choice { id: number; name: string }
export const getHistoryChoices = () =>
  api.get<{ programmes: Choice[]; brokers: Choice[] }>("/audit/history/choices", { params: clean(mga()) })
    .then(r => r.data);

/** The filters every history tab shares. Dates are local days, "YYYY-MM-DD". */
export interface HistoryFilters {
  broker?: number | ""; programme?: number | ""; period?: string; from?: string; to?: string;
  status?: string; contract?: number | "";
}
const hp = (f: HistoryFilters, page: number, pageSize: number) =>
  clean({ ...mga(), broker: f.broker, programme: f.programme, period: f.period, status: f.status,
          contract: f.contract, from: f.from, to: f.to, page, page_size: pageSize });

// ── Partner Submission History ─────────────────────────────────────────────
export interface SubmissionRow {
  id: number; programme: string; broker: string; period: string; due_on: string; received_on: string | null;
  status: string; tone: string; days_late: number; reminders: number;
}
export interface SubmissionPage {
  items: SubmissionRow[]; total: number; page: number; page_size: number;
  counts: { expected: number; received: number; late: number; missing: number };
}
export interface TimelineStep { at: string | null; kind: string; title: string; detail: string | null; by: string }
export interface SubmissionTimeline {
  id: number; programme: string | null; broker: string | null; period: string; due_on: string; status: string;
  steps: TimelineStep[];
}
export const getSubmissions = (f: HistoryFilters, page: number, pageSize: number) =>
  api.get<SubmissionPage>("/audit/history/submissions", { params: hp(f, page, pageSize) }).then(r => r.data);
export const getSubmissionTimeline = (id: number) =>
  api.get<SubmissionTimeline>(`/audit/history/submissions/${id}`).then(r => r.data);

// ── Contract History ───────────────────────────────────────────────────────
export interface ContractStep {
  at: string | null; action: string; title: string; kind: string;
  before: string | null; after: string | null; note: string | null; by: string;
  /** "Draft changed": its recorded before / after on Change History (task 49.3). */
  change_id?: number | null;
}
export interface ContractEvent extends ContractStep {
  contract_id: number; contract: string; programme: string; broker: string;
}
export interface ContractPage {
  items: ContractEvent[]; total: number; page: number; page_size: number;
  counts: { contracts: number; active: number; changed_mid_term: number; renewed: number };
  choices: { contracts: Choice[]; statuses: string[] };
}
export interface ContractTimeline {
  id: number; name: string; programme: string | null; broker: string | null; status: string;
  starts: string | null; ends: string | null; renews: Choice | null; renewed_by: Choice | null;
  steps: ContractStep[];
}
export const getContractHistory = (f: HistoryFilters, page: number, pageSize: number) =>
  api.get<ContractPage>("/audit/history/contracts", { params: hp(f, page, pageSize) }).then(r => r.data);
export const getContractTimeline = (id: number) =>
  api.get<ContractTimeline>(`/audit/history/contracts/${id}`).then(r => r.data);

// ── File Versions ──────────────────────────────────────────────────────────
export interface VersionRow {
  ref: string; version: number; versions: number; filename: string; programme: string; broker: string;
  period: string | null; received_at: string | null; how: string; state: string | null;
  current: boolean; export_id: number | null;
}
export interface VersionPage {
  items: VersionRow[]; total: number; page: number; page_size: number;
  counts: { files: number; versions: number; resent: number };
}
export const getVersions = (f: HistoryFilters, page: number, pageSize: number) =>
  api.get<VersionPage>("/audit/history/versions", { params: hp(f, page, pageSize) }).then(r => r.data);
export const downloadVersion = (exportId: number) => downloadFile(`/export/downloads/${exportId}/file`);
