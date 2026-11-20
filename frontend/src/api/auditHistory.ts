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
                    record: q.record, verb: q.verb || undefined, programme: q.programme || undefined,
                    status: q.status || undefined,
                    tz: q.q?.trim() ? -new Date().getTimezoneOffset() : undefined }),
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

// ── Partner Monitoring (54.1) ──────────────────────────────────────────────
export interface PartnerRow {
  broker_id: number; broker: string; type: string; type_label: string; programmes: number; files_sent: number; late: number; missing: number;
  open_findings: number; critical_findings: number; risk: "High" | "Medium" | "Low"; reasons: string[];
}
export interface PartnerPage {
  items: PartnerRow[]; total: number; page: number; page_size: number;
  counts: { brokers: number; high: number; open_findings: number; late: number };
  rule: string; months: number; audits_ready: boolean; types: { id: string; name: string }[];
}
export interface PartnerProfile extends PartnerRow {
  programme_list: Choice[]; rule: string;
  findings: { id: number; audit_id: number; title: string; severity: string; status: string; due_date: string | null; overdue: boolean }[];
}
export const getPartners = (f: HistoryFilters & { risk?: string; type?: string }, page: number, pageSize: number) =>
  api.get<PartnerPage>("/audit/history/partners", { params: { ...hp(f, page, pageSize), ...clean({ risk: f.risk, type: f.type }) } })
    .then(r => r.data);
export const getPartnerProfile = (brokerId: number) =>
  api.get<PartnerProfile>(`/audit/history/partners/${brokerId}`, { params: clean(mga()) }).then(r => r.data);

// ── Setups made active again (51.1) ────────────────────────────────────────
export interface RestoreRow {
  at: string | null; setup: string | null; setup_id: number | null; programme: string; broker: string;
  replaced: string; reason: string | null; by: string;
}
export const getRestores = (page: number, pageSize: number) =>
  api.get<{ items: RestoreRow[]; total: number; page: number; page_size: number }>("/audit/history/restores",
    { params: clean({ ...mga(), page, page_size: pageSize }) }).then(r => r.data);
export interface RestorePreview { setup: string; ready: boolean; not_ready_reason: string | null; replaces: Choice[] }
export const getRestorePreview = (pipelineId: number) =>
  api.get<RestorePreview>(`/pipelines/${pipelineId}/restore`).then(r => r.data);
export const restoreSetup = (pipelineId: number, reason: string) =>
  api.post(`/pipelines/${pipelineId}/restore`, { reason }).then(r => r.data);

// ── Audit package (50.1) ───────────────────────────────────────────────────
export interface PackageAsk {
  policy?: string; month?: string; from?: string; to?: string; programme?: number | ""; broker?: number | "";
  /** The Date filter as the screen names it ("Last 30 days"), for the file's own words. */
  dates?: string;
  format: "excel" | "pdf";
}
export const downloadAuditPackage = (a: PackageAsk) =>
  downloadFile(`/audit/history/package?${new URLSearchParams(Object.entries(
    clean({ ...mga(), ...a })).map(([k, v]) => [k, String(v)])).toString()}`);

// ── Treaty History (backend treaty_history.py) ─────────────────────────────
export interface TreatyStep {
  at: string | null; action: string; title: string; kind: string; before: string | null; after: string | null;
  note: string | null; by: string; side: "insurer" | "reinsurer";
}
export interface TreatyEvent extends TreatyStep { treaty_id: number; treaty: string; reinsurers: string; programme: string }
export interface TreatyPage {
  items: TreatyEvent[]; total: number; page: number; page_size: number;
  counts: { treaties: number; active: number; terms_changed: number; waiting: number };
  choices: { treaties: Choice[]; reinsurers: Choice[]; programmes: Choice[]; statuses: string[] };
}
export interface TreatyTimeline {
  id: number; name: string; type: string | null; status: string; starts: string | null; ends: string | null;
  programmes: string; renews: Choice | null; renewed_by: Choice | null;
  reinsurers: { id: number; name: string; share: string | null; lead: boolean; standing: string }[];
  steps: TreatyStep[];
}
export type TreatyFilters = { reinsurer?: number | ""; programme?: number | ""; treaty?: number | ""; status?: string; from?: string; to?: string };
export const getTreatyHistory = (f: TreatyFilters, page: number, pageSize: number) =>
  api.get<TreatyPage>("/audit/history/treaties", {
    params: clean({ ...mga(), reinsurer: f.reinsurer, programme: f.programme, treaty: f.treaty, status: f.status,
                    from: f.from, to: f.to, page, page_size: pageSize }),
  }).then(r => r.data);
export const getTreatyTimeline = (id: number) =>
  api.get<TreatyTimeline>(`/audit/history/treaties/${id}`, { params: clean(mga()) }).then(r => r.data);
