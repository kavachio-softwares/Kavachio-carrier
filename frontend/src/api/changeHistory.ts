// Audit Logs tabs for tasks 48.1, 48.2 and 49.1 (backend audit_change_routes.py):
// Change History, Rule History, reinsurers on their treaty links, and the
// contracts the "Contract / policy / file" filter offers. The server decides
// whose rows come back, exactly as for /audit/logs.
import { api, downloadFile } from "./client";
import { currentMga } from "../auth";

/** A local day ("YYYY-MM-DD") as the instant it starts / ends — as api/audit.ts does. */
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

export interface DateFilters { from?: string; to?: string; actor?: string; q?: string }
const dates = (f: DateFilters) => ({ from: dayStart(f.from), to: dayEnd(f.to), actor: f.actor, q: f.q?.trim() });

// ── Change History (48.1) ──────────────────────────────────────────────────
export interface ChangeFilters extends DateFilters { module?: string; entity?: string }
export interface ChangeRow {
  event_id: number; at: string | null; entity: string; target: string; module: string; kind: string;
  name: string; event: string; reason: string | null; by: string;
  field: string | null; old: string | null; new: string | null; line?: number; count: number;
}
export interface ChangePage {
  items: ChangeRow[]; total: number; page: number; page_size: number; modules: string[];
  counts: { changes: number; records: number; people: number; saves: number; with_reason: number };
}
export interface FieldChange { field: string; old: string | null; new: string | null }
export interface ChangeDetail {
  event_id: number; at: string | null; target: string; module: string; kind: string; name: string;
  event: string; reason: string | null; by: string; changes: FieldChange[]; more: number;
  before: Record<string, unknown> | null;
  history: { event_id: number; at: string | null; event: string; by: string; reason: string | null }[];
}
const cp = (f: ChangeFilters) => clean({ ...dates(f), module: f.module, entity: f.entity });

export const getChanges = (f: ChangeFilters, page: number, pageSize: number) =>
  api.get<ChangePage>("/audit/changes", { params: { ...cp(f), page, page_size: pageSize } }).then(r => r.data);
export const getChange = (id: number) => api.get<ChangeDetail>(`/audit/changes/${id}`).then(r => r.data);
export const downloadChanges = (f: ChangeFilters) =>
  downloadFile(`/audit/changes/download?${new URLSearchParams(cp(f) as Record<string, string>)}`);

// ── Rule History (48.2) ────────────────────────────────────────────────────
export interface RuleFilters extends DateFilters { programme?: number | ""; kind?: string; status?: string }
export interface RuleRow {
  event_id: number; at: string | null; key: string; name: string; kind: string; where: string;
  version: number; previous: number | null; latest_version: number; event: string; status: string;
  changes: number; reason: string | null; by: string;
}
export interface RulePage {
  items: RuleRow[]; total: number; page: number; page_size: number;
  counts: { rules: number; events: number; created: number; changed: number; off: number; mappings: number };
  choices: { kinds: string[]; statuses: string[] };
}
export interface RuleVersion {
  event_id: number; at: string | null; version: number; previous: number | null; event: string;
  changes: FieldChange[]; more: number; before: Record<string, unknown> | null; cause: string | null;
  reason: string | null; by: string;
}
export interface RuleVersions {
  key: string; name: string; kind: string; where: string | null; status: string; version: number;
  versions: RuleVersion[];
}
const rp = (f: RuleFilters) => clean({ ...dates(f), programme: f.programme, kind: f.kind, status: f.status });

export const getRuleHistory = (f: RuleFilters, page: number, pageSize: number) =>
  api.get<RulePage>("/audit/rules", { params: { ...rp(f), page, page_size: pageSize } }).then(r => r.data);
export const getRuleVersions = (key: string) =>
  api.get<RuleVersions>("/audit/rules/versions", { params: { key } }).then(r => r.data);
export const downloadRuleHistory = (f: RuleFilters) =>
  downloadFile(`/audit/rules/download?${new URLSearchParams(rp(f) as Record<string, string>)}`);

// ── Downloads & Views: reinsurers on their treaty links (49.1) ─────────────
export interface ReinsurerView {
  id: number; at: string | null; type: "viewed" | "downloaded"; status: "Viewed" | "Downloaded"; what: string;
  person: string; email: string | null; company: string | null; treaty: string; contract_id: number;
  document: string | null; ip: string | null;
}
export interface ReinsurerViewPage {
  items: ReinsurerView[]; total: number; page: number; page_size: number; hidden?: boolean;
  counts: { viewed: number; downloaded: number; people: number };
}
export const getReinsurerViews = (f: DateFilters, kind: string, page: number, pageSize: number) =>
  api.get<ReinsurerViewPage>("/audit/reinsurer-views", {
    params: clean({ ...dates(f), kind, mga: currentMga() || undefined, page, page_size: pageSize }),
  }).then(r => r.data);

// ── the "Contract / policy / file" filter (49.1) ───────────────────────────
export const getRecordChoices = () =>
  api.get<{ contracts: { id: number; name: string }[] }>("/audit/record-choices").then(r => r.data);
