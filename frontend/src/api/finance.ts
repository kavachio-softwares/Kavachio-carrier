import { api } from "./client";

// Financial Summary & Trends (Phase 2, task 35) — written premium, commission,
// brokerage, fees, taxes, net and earned premium from the processed
// bordereaux (backend financial_summary.py). Carrier and broker seats only.

export type Basis = "month" | "quarter" | "year";
export type Window = "current" | "previous" | "last_year";
/** How sure a figure is: from the file, worked out, unclear, or absent. */
export type FigureStatus = "reported" | "calculated" | "needs_review" | "not_reported";

export type MetricKey =
  | "gross_premium" | "total_gwp" | "commission_amount" | "commission_pct"
  | "brokerage_amount" | "brokerage_pct" | "fees" | "taxes" | "net_premium" | "earned_premium";

export interface WindowValue {
  /** Sum of reported + calculated values; null when no row has one. */
  value: number | null;
  /** Rows by label — a total is only as complete as these say. */
  counts: Record<FigureStatus, number>;
}

export interface MetricTotals {
  /** "percent" totals are total amount ÷ total gross premium; they change in points. */
  unit: "money" | "percent";
  current: WindowValue;
  previous: WindowValue;
  last_year: WindowValue;
  /** % change for money, percentage points for a rate; null = nothing to compare with. */
  change: { previous: number | null; last_year: number | null };
}

export type Totals = Record<MetricKey, MetricTotals>;

export interface PartTotals { id: number | null; name: string; totals: Totals }

export interface CurrencyGroup {
  /** A currency code, or "none" (not stated) / "review" (unclear). */
  currency: string;
  label: string;
  rows: number;
  totals: Totals;
  by_programme: PartTotals[];
  /** Empty for a broker seat. */
  by_broker: PartTotals[];
}

export interface FileRef {
  export_id: number;
  filename: string | null;
  period: string | null;
  programme: string | null;
  same_as?: number | null;
  problem?: string | null;
}

export interface Choice { id: number; name: string }

export interface FinancialSummary {
  seat: "carrier" | "broker";
  basis: Basis;
  /** The window shown; null when no file has a period yet. */
  period: string | null;
  windows: Partial<Record<Window, { period: string; files: number; rows: number }>>;
  /** Every period of this basis that has files, newest first. */
  periods: string[];
  filters: { programmes: Choice[]; brokers: Choice[]; insurers: Choice[] };
  groups: CurrencyGroup[];
  net_written_premium: { status: "not_available"; formula: string; reason: string };
  transactions: { sent: string | null; class: string | null; negative: boolean; rows: number }[];
  coverage: {
    files: number;
    no_period: FileRef[];
    longer_period: FileRef[];
    copies: FileRef[];
    unreadable: FileRef[];
    open_problems: { files: number; excluded: boolean };
    headings_not_in_file: number;
    percent_unverified_templates: number;
  };
}

export interface SummaryParams {
  basis?: Basis;
  period?: string | null;
  program_id?: number | null;
  broker_party_id?: number | null;
  carrier_id?: number | null;
  open_problems?: "include" | "exclude";
}

/** Drop the empty ones, so the request carries only the filters in use. */
function clean(p: object): Record<string, string | number> {
  return Object.fromEntries(Object.entries(p).filter(([, v]) => v !== null && v !== undefined && v !== ""));
}

/** The data-model fields (money figures) whose columns may be added together —
 *  served by the backend, not restated here. */
export async function getAddTogetherFields(): Promise<string[]> {
  return (await api.get<{ fields: string[] }>("/reports/financial-summary/add-together-fields")).data.fields;
}

export async function getFinancialSummary(p: SummaryParams): Promise<FinancialSummary> {
  return (await api.get<FinancialSummary>("/reports/financial-summary", { params: clean(p) })).data;
}

export interface Figure {
  value: number | null;
  /** The amount as the bordereau sent it (differs on a cancellation). */
  source: number | null;
  status: FigureStatus;
  /** The working, or why it needs review. */
  how: string | null;
}

export interface FinancialRow {
  export_id: number;
  filename: string | null;
  period: string | null;
  programme: string | null;
  broker: string | null;
  insurer: string | null;
  sheet: string;
  row: number;
  policy: string;
  transaction: { sent: string | null; class: string | null; negative: boolean; review?: string };
  cover_start: { value: string | null; status: FigureStatus };
  cover_end: { value: string | null; status: FigureStatus };
  currency: { value: string | null; status: FigureStatus };
  figures: Record<MetricKey, Figure>;
}

export interface FinancialRows {
  items: FinancialRow[];
  total: number;
  page: number;
  page_size: number;
  window: Window;
  period: string | null;
}

export async function getFinancialRows(p: SummaryParams & {
  window?: Window; currency?: string | null; status?: FigureStatus | null;
  page?: number; page_size?: number;
}): Promise<FinancialRows> {
  return (await api.get<FinancialRows>("/reports/financial-summary/rows", { params: clean(p) })).data;
}
