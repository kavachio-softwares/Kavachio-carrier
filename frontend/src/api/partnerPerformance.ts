import { api } from "./client";
import { currentMga } from "../auth";

// Task 36 — Partner Performance (backend partner_performance.py): how often each
// broker sends a clean file on time, one row per broker + programme + contract,
// with the best and worst brokers. Carrier only.

export type PerfCadence = "month" | "quarter" | "year";

/** The counts and shares every row, broker score and total carries. */
export interface PerfFigures {
  /** Expected files due in the period (the on-time denominator). */
  due: number;
  /** Files due that have come in (on time or late). */
  received: number;
  on_time: number;
  late_or_missing: number;
  /** Came in, but after the due date. */
  late: number;
  /** Due, and not come in yet. */
  missing: number;
  /** Files processed in the period (the clean-first-time denominator). */
  files: number;
  clean_first_time: number;
  /** Files due whose clean-and-on-time result is known. */
  scored: number;
  /** Came on time but no check is recorded — left out of the share. */
  not_checked: number;
  clean_on_time: number;
  /** Exceptions on the files as they first came. */
  problems_found: number;
  /** Exceptions still open on the files as they stand now. */
  problems_open: number;
  /** Found minus still open: put right while cleaning. */
  problems_fixed: number;
  /** Files whose current version still has open exceptions. */
  files_with_problems: number;
  on_time_pct: number | null;
  clean_first_time_pct: number | null;
  clean_on_time_pct: number | null;
  fixed_pct: number | null;
}

interface PerfScore extends PerfFigures {
  /** How the files came in: "Portal", "Email", … */
  file_types: string[];
  prev_on_time_pct: number | null;
  prev_clean_first_time_pct: number | null;
  prev_clean_on_time_pct: number | null;
  /** Files received in the period before (null = none were due then). */
  prev_received: number | null;
  /** Clean and on time against the period before, in percentage points;
   *  null when there is nothing to compare, or no file came in either period. */
  change_pts: number | null;
}

/** A broker in the Best / Worst lists. */
export interface PerfBroker extends PerfScore {
  key: number;
  name: string;
  broker_id: number;
  /** 1.. ; null = no file with a known result. */
  rank: number | null;
}

/** One row of the table: a broker on one programme and contract. */
export interface PerfRow extends PerfScore {
  key: string;
  /** The broker's rank. */
  rank: number | null;
  broker_id: number;
  broker: string;
  programme_id: number | null;
  programme: string;
  contract_id: number | null;
  contract: string;
}

export interface PerfOption { value: number | string; label: string }

export interface PartnerPerformance {
  cadence: PerfCadence;
  period: string;
  from: string;
  to: string;
  previous: { period: string; from: string; to: string };
  complete: boolean;
  as_at: string;
  filters: { programme: number | null; file_type: string | null; broker: number | null };
  options: { brokers: PerfOption[]; programmes: PerfOption[]; file_types: PerfOption[] };
  min_files: number;
  summary: PerfFigures & {
    previous: { on_time_pct: number | null; clean_first_time_pct: number | null; clean_on_time_pct: number | null };
    change_pts: { on_time_pct: number | null; clean_first_time_pct: number | null; clean_on_time_pct: number | null };
  };
  best: PerfBroker[];
  worst: PerfBroker[];
  /** With one broker picked: where it stands among all the brokers (best and
   *  worst are then empty — they would only name it). */
  picked: { broker_id: number; name: string; rank: number | null; of: number;
            clean_on_time_pct: number | null } | null;
  ranked_brokers: number;
  rows: PerfRow[];
}

type Query = {
  cadence: PerfCadence; period: string; programId?: number | null; fileType?: string | null;
  brokerId?: number | null;
};

const params = (p: Query) => ({
  mga: currentMga(), cadence: p.cadence, period: p.period,
  program_id: p.programId ?? undefined, file_type: p.fileType || undefined,
  broker_id: p.brokerId ?? undefined,
});

export async function getPartnerPerformance(p: Query): Promise<PartnerPerformance> {
  return (await api.get<PartnerPerformance>("/reports/partner-performance", { params: params(p) })).data;
}

/** The table, with the same filters, as an Excel file. */
export async function downloadPartnerPerformance(p: Query): Promise<void> {
  const r = await api.get<Blob>("/reports/partner-performance/file", { params: params(p), responseType: "blob" });
  const href = URL.createObjectURL(r.data);
  const a = document.createElement("a");
  a.href = href;
  a.download = `Partner Performance ${p.period}.xlsx`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
