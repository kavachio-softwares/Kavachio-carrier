// Integrity & Retention (backend record_integrity.py 52.1, retention.py 52.2):
// fingerprints that prove audit records were never changed, how long each
// kind of record is kept, legal holds, and what retention deleted.
import { api } from "./client";
import { currentMga } from "../auth";

const m = () => ({ params: { mga: currentMga() || undefined } });

export interface SealKind { table: string; type: string; info: string; sealed: number; gone: number; last_sealed: string | null }
export interface IntegritySummary {
  ready: boolean; message?: string; kinds?: SealKind[]; sealed?: number; settle_minutes?: number;
  last_check?: { at: string | null; by: string | null; result: string | null; checked: number | null; problems: number | null } | null;
}
export interface CheckKind {
  table: string; type: string; info: string; checked: number; unchanged: number; changed: number; missing: number;
  gone: number; not_sealed_yet: number; result: string;
}
export interface CheckResult {
  at: string; by: string | null; chain_ok: boolean; broken_at: number | null; kinds: CheckKind[];
  problems: { type: string; record: number; created: string | null; sealed: string | null; result: string }[];
  checked: number; problems_total: number; result: string;
}
export const getIntegrity = () => api.get<IntegritySummary>("/audit/integrity", m()).then(r => r.data);
export const checkIntegrity = () => api.post<CheckResult>("/audit/integrity/check", null, m()).then(r => r.data);

export interface RetentionKind {
  key: string; type: string; info: string; months: number | null; applies: string | null; oldest: string | null;
  next_count: number; held: number; cutoff: string | null;
}
export interface RetentionRule { id: number; key: string; type: string; country: string; months: number | null; note: string | null; updated_at: string | null }
export interface Hold {
  id: number; carrier: string | null; covers: string; broker_id: number | null; reason: string; placed_at: string | null;
  placed_by: string | null; on: boolean; lifted_at: string | null; lifted_by: string | null; lift_note: string | null;
}
export interface RetentionOverview {
  ready: boolean; message?: string; country?: string | null; kinds?: RetentionKind[]; whole_held?: boolean;
  rules?: RetentionRule[]; types?: { id: string; name: string }[]; holds?: Hold[];
  can_set_periods?: boolean; can_hold?: boolean; brokers?: { id: number; name: string }[];
}
export interface DeletionRow {
  id: number; at: string | null; carrier: string; type: string; months: number; country: string; cutoff: string | null;
  deleted: number; held: number; oldest: string | null; newest: string | null;
}
export const getRetention = () => api.get<RetentionOverview>("/audit/retention", m()).then(r => r.data);
export const getDeletions = (page: number, pageSize: number) =>
  api.get<{ items: DeletionRow[]; total: number; page: number; page_size: number }>("/audit/retention/deletions",
    { params: { ...m().params, page, page_size: pageSize } }).then(r => r.data);
export const saveRetentionRule = (b: { record_type: string; country: string; months: number | null; note: string }) =>
  api.put<RetentionOverview>("/audit/retention/rules", b).then(r => r.data);
export const removeRetentionRule = (id: number) => api.delete<RetentionOverview>(`/audit/retention/rules/${id}`).then(r => r.data);
export const placeHold = (b: { reason: string; broker_party_id: number | null }) =>
  api.post<{ holds: Hold[] }>("/audit/retention/holds", b, m()).then(r => r.data);
export const liftHold = (id: number, note: string) =>
  api.post<{ holds: Hold[] }>(`/audit/retention/holds/${id}/lift`, { note }, m()).then(r => r.data);
