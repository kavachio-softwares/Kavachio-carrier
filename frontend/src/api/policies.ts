import { api, downloadFile } from "./client";
import type { SendLogin, SendOutcome } from "../components/SendByEmail";

// Policy Search (Phase 2, 43.1) — the policies inside the processed
// bordereaux (backend policy_search.py). Carrier and broker seats only.

/** One policy: every row of it across the current bordereaux, as one line. */
export interface PolicyLine {
  policy: string;
  insured: string | null;
  state: string | null;
  /** Cover start / end — "YYYY-MM-DD", or the file's own text when it is not a date. */
  start: string | null;
  end: string | null;
  /** The latest row's transaction type. */
  transaction: string | null;
  /** Premium over all its rows; null when none or in more than one currency. */
  premium: number | null;
  currency: string | null;
  /** First and latest bordereau month, "YYYY-MM". */
  first_period: string | null;
  last_period: string | null;
  rows: number;
  files: number;
  insurer_id: number;
  insurer: string | null;
  program_id: number | null;
  programme: string | null;
  broker_party_id: number | null;
  broker: string | null;
}

export interface Choice { id: number; name: string }

export interface PolicySearchResult {
  items: PolicyLine[];
  total: number;
  page: number;
  page_size: number;
  seat: "carrier" | "broker";
  /** Processed files the policies were read from. */
  files: number;
  filters: { programmes: Choice[]; brokers: Choice[]; insurers: Choice[]; states: string[] };
  /** The number on a report these are the policies behind (38.1); null for a plain search. */
  drill: PolicyDrill | null;
}

/** Which number on a report was clicked (backend drill_down.py). */
export interface PolicyDrillParams {
  from: "financial" | "monthly";
  /** What the report called the number, shown back above the list. */
  label?: string;
  // Financial Summary
  basis?: string;
  period?: string;
  window?: "current" | "previous" | "last_year";
  metric?: string;
  currency?: string;
  open_problems?: "include" | "exclude";
  // Operational Report
  month?: string;
  measure?: string;
}

export interface PolicyDrill {
  source: "financial" | "monthly";
  label: string | null;
  period: string | null;
  /** "March 2027", "Q1 2027", "2027". */
  period_name: string;
  /** Files behind the number. */
  files: number;
  /** Rows behind the number (Financial Summary); null when not counted by row. */
  rows: number | null;
}

/** The URL query that opens the Policies page on the policies behind a number. */
export function drillLink(d: PolicyDrillParams, scope: { program?: number | null; broker?: number | null } = {}): string {
  const q = new URLSearchParams(clean({ ...d, program: scope.program, broker: scope.broker }) as Record<string, string>);
  return `/policies?${q.toString()}`;
}

/** Read the clicked number back out of the Policies page's own URL. */
export const DRILL_KEYS = ["from", "label", "basis", "period", "window", "metric", "currency",
  "open_problems", "month", "measure"] as const;
export function drillFrom(params: URLSearchParams): PolicyDrillParams | null {
  const from = params.get("from");
  if (from !== "financial" && from !== "monthly") return null;
  const out: Record<string, string> = {};
  for (const k of DRILL_KEYS) { const v = params.get(k); if (v) out[k] = v; }
  return out as unknown as PolicyDrillParams;
}

export type PolicyDateField = "start" | "end" | "month";

export interface PolicySearchParams {
  q?: string;
  program_id?: number | null;
  broker_party_id?: number | null;
  carrier_id?: number | null;
  state?: string;
  date_field?: PolicyDateField;
  date_from?: string;
  date_to?: string;
  page?: number;
  page_size?: number;
  drill?: PolicyDrillParams | null;
}

/** Drop the empty ones, so the request carries only the filters in use. */
function clean(p: object): Record<string, string | number> {
  return Object.fromEntries(Object.entries(p).filter(([, v]) => v !== null && v !== undefined && v !== ""));
}

/** The list's filters and the number clicked, as the server reads them —
 *  one set for the page, the Excel file and the email (policy_routes._filters). */
function listParams({ drill, ...p }: PolicySearchParams): Record<string, string | number> {
  return clean({ ...p, ...(drill ?? {}) });
}

export async function searchPolicies(p: PolicySearchParams): Promise<PolicySearchResult> {
  return (await api.get<PolicySearchResult>("/policies", { params: listParams(p) })).data;
}

// ── 43.2: the list taken out — every page of it, with the filters on screen ──

/** Every policy the filters select, as an Excel file. Recorded in the Audit Logs (43.3). */
export async function downloadPolicies(p: PolicySearchParams): Promise<void> {
  const q = new URLSearchParams(Object.entries(listParams(p)).map(([k, v]) => [k, String(v)]));
  await downloadFile(`/policies/file?${q.toString()}`, "Policies.xlsx");
}

/** Your own company's logins the list can be emailed to. */
export async function getPolicyRecipients(): Promise<{ logins: SendLogin[] }> {
  return (await api.get<{ logins: SendLogin[] }>("/policies/recipients")).data;
}

export interface PolicySendResult extends SendOutcome { policies: number }

/** Email every policy the filters select, as an Excel file. Recorded in the Audit Logs (43.3). */
export async function sendPolicies(p: PolicySearchParams,
  to: { user_ids: number[]; emails: string[] }): Promise<PolicySendResult> {
  return (await api.post<PolicySendResult>("/policies/send", to, { params: listParams(p) })).data;
}

/** One bordereau row of a policy, with every column of it. */
export interface PolicyRow {
  export_id: number;
  filename: string;
  period: string | null;
  version_no: number | null;
  contract_id: number | null;
  contract: string | null;
  processed_at: string | null;
  row: number;
  policy: string;
  insured: string | null;
  state: string | null;
  start: string | null;
  end: string | null;
  transaction: string | null;
  premium: number | null;
  currency: string | null;
  columns: { name: string; value: string | number | boolean | null }[];
}

export interface PolicyDetail {
  policy: string;
  insurer: string | null;
  programme: string | null;
  broker: string | null;
  seat: "carrier" | "broker";
  rows: PolicyRow[];
}

export async function getPolicyDetail(line: PolicyLine, carrierId?: number | null): Promise<PolicyDetail> {
  return (await api.get<PolicyDetail>("/policies/detail", {
    params: clean({
      policy: line.policy, insurer_id: line.insurer_id, program_id: line.program_id,
      broker_party_id: line.broker_party_id, carrier_id: carrierId,
    }),
  })).data;
}
