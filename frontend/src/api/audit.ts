// Audit Logs — the trail every seat has in its own sidebar.
//
// The server decides WHOSE rows come back (audit_feed.scope_for): a broker user
// sees their own, a broker admin their whole organisation's, a carrier every
// broker it works with, and Kavachio everything. Nothing here filters by role —
// asking for more than your seat may read simply returns less.
import { api, downloadFile } from "./client";

export type AuditRow = {
  id: string;                  // "activity:1204" — unique across the three stores
  at: string | null;           // explicit-UTC ISO
  category: "activity" | "auth" | "access" | "decision";
  actor: string;               // a person, or a broker company when masked
  actor_role: string;          // "Broker Admin", "Carrier User", "Automation", …
  actor_role_key: string;
  actor_org: string | null;
  action: string;              // the stored event name
  action_label: string;        // …in words
  /** Exactly what was done: the field, the policy, the old and new value, the
   *  row counts. Empty when the event recorded nothing beyond itself. */
  detail: string;
  target: string;              // the file or record it was done to
  status: string;
  tone: "ok" | "warn" | "info" | "bad" | "muted";
  ip: string | null;
  carrier: string | null;
};

export type AuditPage = {
  items: AuditRow[];
  total: number;
  page: number;
  page_size: number;
  seat: string;
};

export type AuditOptions = {
  actors: { key: string; label: string; role: string }[];
  action_groups: { key: string; label: string; actions: string[] }[];
  seat: string;
  categories: string[];
};

export type AuditQuery = {
  /** Local calendar day, "YYYY-MM-DD". Converted to a UTC instant below. */
  from?: string;
  to?: string;
  /** "user:12" | "broker:5" | "system" — "" for everyone in reach. */
  actor?: string;
  /** An Action Type group key, or one event name. "" / "all" for everything. */
  action?: string;
  q?: string;
};

/** Start of a local calendar day as an instant the server can compare.
 *
 *  The audit columns hold UTC, the person reading them is in their own zone,
 *  and only the browser knows which. Sending the instant (not the date) is what
 *  makes "1 Oct – 31 Oct" mean the reader's October rather than a UTC one. */
function dayStart(day?: string): string | undefined {
  if (!day) return undefined;
  const [y, m, d] = day.split("-").map(Number);
  if (!y || !m || !d) return undefined;
  return new Date(y, m - 1, d, 0, 0, 0, 0).toISOString();
}

function dayEnd(day?: string): string | undefined {
  if (!day) return undefined;
  const [y, m, d] = day.split("-").map(Number);
  if (!y || !m || !d) return undefined;
  return new Date(y, m - 1, d, 23, 59, 59, 999).toISOString();
}

function params(q: AuditQuery): Record<string, string> {
  const out: Record<string, string> = {};
  const from = dayStart(q.from);
  const to = dayEnd(q.to);
  if (from) out.from = from;
  if (to) out.to = to;
  if (q.actor) out.actor = q.actor;
  if (q.action && q.action !== "all") out.action = q.action;
  if (q.q?.trim()) out.q = q.q.trim();
  return out;
}

export async function getAuditLogs(
  q: AuditQuery, page: number, pageSize: number,
): Promise<AuditPage> {
  const { data } = await api.get<AuditPage>("/audit/logs", {
    params: { ...params(q), page, page_size: pageSize },
  });
  return data;
}

export async function getAuditOptions(): Promise<AuditOptions> {
  const { data } = await api.get<AuditOptions>("/audit/options");
  return data;
}

/** The rows the current filters select, as a file. Same scope and same wording
 *  as the table — a download is never a second door onto rows the screen
 *  would not show. */
export async function downloadAuditLogs(q: AuditQuery, format: "csv" | "xlsx") {
  const search = new URLSearchParams({ ...params(q), format }).toString();
  await downloadFile(`/audit/export?${search}`);
}
