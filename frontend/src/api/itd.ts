import { api } from "./client";
import { currentMga } from "../auth";

// Feature 13 — ITD (inception-to-date) bordereaux. An ITD file repeats every
// transaction since the contract began, not just this period's. With ITD on,
// each row is compared with what has already been delivered: new rows and
// money adjustments go on, everything already delivered is left out. The file
// itself is always kept exactly as received. A row whose id ends in "-R"
// (INV110582-R) is a REVERSAL, linked to the transaction it reverses.
//
// A setup's ITD settings are not shown anywhere: the server fills them in from
// the setup's input sheets when the setup is created (itd.auto_config).

/** What one file did to the running total. */
export type ItdSummary = {
  load_type: "itd" | "regular";
  rows_in_file: number;
  rows_to_load: number;
  new: number;
  /** New rows reversing an earlier transaction (INV110582-R). Absent on a
   *  file compared before reversals were told apart. */
  reversal?: number;
  unchanged: number;
  changed: number;
  details_changed: number;
  dropped: number;
  duplicate: number;
  /** Of the duplicates: rows whose first copy is on another sheet of the file
   *  (a monthly tab and an ITD tab carrying the same transaction) — counted
   *  once, from that sheet. Not a mistake by the broker. */
  duplicate_other_sheet?: number;
  missing_key: number;
  /** New rows with no transaction number that are alike on every field of
   *  the business key (policy, type, date, period, amounts) to another row
   *  of the file. All are sent; a person confirms they are real and not one
   *  row sent twice. Absent on a file compared before this was checked. */
  identical_rows?: number;
  /** Rows whose transaction date is before the period they are reported in —
   *  late transactions, loaded like any other row. Absent on a file compared
   *  before the transaction date was read. */
  late_rows?: number;
  adjustment_totals: Record<string, number>;
  status?: "pending" | "applied" | "withdrawn";
  /** Set aside because this version of the submission was never delivered
   *  and a corrected version replaced it. */
  replaced_by?: number;
  /** On the corrected version: the versions it replaced. */
  replaced_versions?: number[];
  applied?: number;
  conflicts?: number;
  /** Set when the file was held because a newer ITD file is already loaded. */
  held?: "older_itd";
  last_itd?: string;
};

export type ItdResult = "new" | "reversal" | "unchanged" | "changed" | "details_changed"
  | "dropped" | "duplicate" | "missing_key";

/** The client's classes (Open Questions Register): every row is New,
 *  Previously processed, Changed or Cancelled or reversed. A duplicate in the
 *  file and a row with no transaction id fit none of them: Skipped. The
 *  result underneath says which kind (an amount or only details changed, a
 *  reversal or a transaction missing from this file, …). */
export type ItdClass = "new" | "previously_processed" | "changed"
  | "cancelled_or_reversed" | "held_back";

export const ITD_CLASSES: ItdClass[] = [
  "new", "previously_processed", "changed", "cancelled_or_reversed", "held_back",
];

export const ITD_CLASS_LABEL: Record<ItdClass, string> = {
  new: "New",
  previously_processed: "Previously processed",
  changed: "Changed",
  cancelled_or_reversed: "Cancelled or reversed",
  held_back: "Skipped",
};

export const ITD_CLASS_OF: Record<ItdResult, ItdClass> = {
  new: "new",
  unchanged: "previously_processed",
  changed: "changed", details_changed: "changed",
  reversal: "cancelled_or_reversed", dropped: "cancelled_or_reversed",
  duplicate: "held_back", missing_key: "held_back",
};

/** A file's rows counted by class — from the results, so a summary saved
 *  before the classes existed reads the same way. */
export function itdClassCounts(s: Partial<Record<ItdResult, number>>): Record<ItdClass, number> {
  const out = { new: 0, previously_processed: 0, changed: 0,
                cancelled_or_reversed: 0, held_back: 0 } as Record<ItdClass, number>;
  for (const [r, n] of Object.entries(s)) {
    const c = ITD_CLASS_OF[r as ItdResult];
    if (c && typeof n === "number") out[c] += n;
  }
  return out;
}

/** One file row, as the comparison saw it. */
export type ItdRow = {
  id: number;
  sheet: string | null;
  /** 0-based; shown +1. */
  row_no: number | null;
  /** The transaction id as it reads in the file. */
  source_ref: string | null;
  row_key: string | null;
  result: ItdResult;
  status: string;
  load_type: string;
  period: string | null;
  old_money: Record<string, number> | null;
  new_money: Record<string, number> | null;
  money_delta: Record<string, number> | null;
  changed_fields: string[] | null;
  adjust_no: number | null;
  note: string | null;
  /** The running-total entry this row belongs to — what a decision on a
   *  dropped row is recorded against. */
  position_id: number | null;
  stream: string | null;
  /** On a reversal: the id of the transaction it reverses (INV110582), and
   *  that transaction's key when it is in the file or delivered before. */
  reverses_ref: string | null;
  reverses_key: string | null;
  /** The client's class of this row. */
  class?: ItdClass;
  /** 'YYYY-MM' the row is reported in (its Reporting End Date). Equal to
   *  `period` (the month the file is as at) for this month's rows; earlier
   *  for the rows an ITD file repeats. On a dropped row, the month it was
   *  reported in before. Null on a file compared before this was recorded. */
  row_period?: string | null;
};

/** The rows in a file by the month they are reported in. */
export type ItdMonthCounts = { current: number; earlier: number; unknown: number };

export type ItdRowsResponse = {
  rows: ItdRow[];
  total: number;
  counts: Partial<Record<ItdResult, number>>;
  class_counts?: Record<ItdClass, number>;
  month_counts?: ItdMonthCounts;
  arrival_id: number;
  filename: string;
  load_type: "itd" | "regular" | null;
  load_type_reason: string | null;
  period_start: string | null;
  period_end: string | null;
  summary: ItdSummary | null;
};

export type ItdPosition = {
  id: number;
  stream: string;
  source_ref: string | null;
  status: string;
  money: Record<string, number> | null;
  adjust_count: number;
  first_arrival_id: number | null;
  last_arrival_id: number | null;
  last_period: string | null;
  last_seen_at: string | null;
};

export type ItdPositionsResponse = {
  rows: ItdPosition[];
  total: number;
  counts: Record<string, number>;
};

/** A dropped transaction: was it taken out on purpose, or is it still live? */
export type ItdDecision = "removed" | "keep";

export async function getArrivalItdRows(arrivalId: number, opts: {
  result?: ItdResult; cls?: ItdClass; limit?: number; offset?: number;
} = {}): Promise<ItdRowsResponse> {
  const { cls, ...rest } = opts;
  const { data } = await api.get(`/itd/arrivals/${arrivalId}/rows`,
    { params: { mga: currentMga(), ...rest, ...(cls ? { class: cls } : {}) } });
  return data;
}

/** Take a file's rows back out of the running total. Refused (409, with a
 *  sentence) when a later file was compared against it. */
export async function withdrawArrivalItd(arrivalId: number)
    : Promise<{ arrival_id: number; rows_undone: number }> {
  const { data } = await api.post(`/itd/arrivals/${arrivalId}/withdraw`, null,
    { params: { mga: currentMga() } });
  return data;
}

export async function listItdPositions(opts: {
  program_id?: number; broker_party_id?: number; status?: string;
  limit?: number; offset?: number;
} = {}): Promise<ItdPositionsResponse> {
  const { data } = await api.get("/itd/feeds/positions",
    { params: { mga: currentMga(), ...opts } });
  return data;
}

export async function decideItdPosition(positionId: number, decision: ItdDecision)
    : Promise<{ position_id: number; status: string }> {
  const { data } = await api.post(`/itd/positions/${positionId}/decision`, { decision },
    { params: { mga: currentMga() } });
  return data;
}

/** "89 in file · 50 already delivered · 19 new · 20 adjusted". Dropped,
 *  duplicate and no-id rows only when there are some — a zero there is noise. */
export function itdSummaryLine(s: ItdSummary): string {
  const n = (v: number) => v.toLocaleString();
  const c = itdClassCounts(s as unknown as Partial<Record<ItdResult, number>>);
  const parts = [
    `${n(s.rows_in_file)} in file`,
    `${n(c.new)} new`,
    `${n(c.previously_processed)} previously processed`,
    `${n(c.changed)} changed`,
    `${n(c.cancelled_or_reversed)} cancelled or reversed`,
  ];
  if (c.held_back > 0) {
    const onTwoTabs = s.duplicate_other_sheet ?? 0;
    parts.push(`${n(c.held_back)} skipped`
      + (onTwoTabs > 0 ? ` (${n(onTwoTabs)} on two tabs, counted once)` : ""));
  }
  const alike = s.identical_rows ?? 0;
  if (alike > 0) parts.push(`${n(alike)} identical row${alike === 1 ? "" : "s"} to confirm`);
  return parts.join(" · ");
}

/** " (25 late transactions reported this month)" on a monthly file's line —
 *  rows with an earlier transaction date, loaded as usual. "" when none. */
export function lateRowsNote(s: ItdSummary): string {
  const late = s.late_rows ?? 0;
  if (late <= 0) return "";
  return ` (${late.toLocaleString()} late transaction${late === 1 ? "" : "s"} reported this month)`;
}

/** What a row's class means in its case — shown under the class. */
export const ITD_RESULT_DETAIL: Record<ItdResult, string | null> = {
  new: null,
  unchanged: null,
  changed: "Amount changed",
  details_changed: "Details only — not sent",
  reversal: "Reversal",
  dropped: "Missing from this file",
  duplicate: "Duplicate",
  missing_key: "Can't identify",
};

/** The words for each result underneath the client's classes. */
export const ITD_RESULT_LABEL: Record<ItdResult, string> = {
  new: "New",
  reversal: "Reversal",
  unchanged: "Already delivered",
  changed: "Money changed",
  details_changed: "Details changed",
  dropped: "Dropped",
  duplicate: "Duplicate",
  missing_key: "Can't identify",
};

/** A refused ITD file's sentence — `detail` arrives as {code, message}. */
export function olderItdMessage(detail: unknown): string | null {
  if (!detail || typeof detail !== "object") return null;
  const d = detail as { code?: unknown; message?: unknown };
  if (d.code !== "older_itd") return null;
  return typeof d.message === "string" && d.message.trim()
    ? d.message
    : "This ITD file is older than one already loaded, so it was not processed.";
}
