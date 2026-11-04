import { api } from "./client";

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
}

/** Drop the empty ones, so the request carries only the filters in use. */
function clean(p: object): Record<string, string | number> {
  return Object.fromEntries(Object.entries(p).filter(([, v]) => v !== null && v !== undefined && v !== ""));
}

export async function searchPolicies(p: PolicySearchParams): Promise<PolicySearchResult> {
  return (await api.get<PolicySearchResult>("/policies", { params: clean(p) })).data;
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
