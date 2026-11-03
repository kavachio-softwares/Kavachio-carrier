import { api } from "./client";
import { currentMga } from "../auth";

// MOPR 40.2 / 40.3 — one month's data quality and volumes, read from the shared
// check record (backend check_record.summary).

/** New / Renewed / Cancelled / Other changes / Not classified. */
export interface TxnGroup {
  key: "new" | "renewed" | "cancelled" | "other" | "unclassified";
  label: string;
  policies: number;
  rows: number;
}

export interface FailingCheck {
  check: string;
  kind: "contract_rule" | "type_check" | "intake";
  /** The most serious it was anywhere in the month; null if not known. */
  severity: "critical" | "warning" | "info" | null;
  /** Files it failed on, and the errors it found across them. */
  files: number;
  errors: number;
}

export interface QualityBreakdown {
  id: number | null;
  name: string;
  files: number;
  rows: number;
  errors: number;
  straight_through: number;
  straight_through_pct: number | null;
}

/** One broker on one programme. */
export interface BrokerProgrammeRow {
  broker_party_id: number | null;
  program_id: number | null;
  broker_name: string;
  programme_name: string;
  files: number;
  rows: number;
  errors: number;
  rows_with_errors: number;
  rows_with_errors_pct: number | null;
  straight_through: number;
  straight_through_pct: number | null;
}

export interface DataQualityReport {
  month: string;
  from: string;
  to: string;
  volumes: {
    files: number;
    /** How many brokers and programmes those files came from. */
    brokers: number;
    programmes: number;
    corrected_files: number;
    rows: number;
    /** null when no file had a policy-number column to count by. */
    policies: number | null;
    policies_known: boolean;
    transaction_types: TxnGroup[];
  };
  quality: {
    check_runs: number;
    checks_run: number;
    checks_passed: number;
    checks_failed: number;
    checks_not_run: number;
    errors: number;
    critical: number;
    warning: number;
    info: number;
    files_clean: number;
    files_with_errors: number;
    files_not_validated: number;
    top_failing_checks: FailingCheck[];
    arrivals: number;
    arrivals_accepted: number;
    arrivals_held: number;
    arrivals_turned_away: number;
  };
  no_human_help: {
    files: number;
    files_pct: number | null;
    rows: number;
    rows_pct: number | null;
  };
  by_broker: QualityBreakdown[];
  by_programme: QualityBreakdown[];
  by_broker_programme: BrokerProgrammeRow[];
  programmes: { id: number; name: string }[];
  brokers: { id: number; name: string }[];
}

export async function getDataQuality(params: {
  month: string;
  program_id?: number | null;
  broker_party_id?: number | null;
}): Promise<DataQualityReport> {
  const { data } = await api.get<DataQualityReport>("/reports/data-quality", {
    params: {
      mga: currentMga(), month: params.month,
      program_id: params.program_id ?? undefined,
      broker_party_id: params.broker_party_id ?? undefined,
    },
  });
  return data;
}

// MOPR 41 — Exception Resolution Performance (backend fix_performance.py).

/** One problem still open: how old it is and who has to act. */
export interface OpenProblem {
  check: string | null;
  severity: "critical" | "warning" | "info" | null;
  message: string | null;
  policy_number: string | null;
  sheet: string | null;
  row: number | null;
  field: string | null;
  file: string | null;
  period: string | null;
  export_id: number | null;
  program_id: number | null;
  broker_party_id: number | null;
  broker_name: string;
  programme_name: string;
  /** The broker whose file it is, or "Carrier" for a file no broker sent. */
  owner: string;
  found_at: string | null;
  age_days: number | null;
  past_target: boolean;
}

export interface FixGroupRow {
  broker_party_id: number | null;
  program_id: number | null;
  broker_name: string;
  programme_name: string;
  owner: string;
  fixed: number;
  avg_days: number | null;
  within_target: number;
  within_target_pct: number | null;
  open: number;
  past_target: number;
  oldest_days: number | null;
}

export interface ExceptionResolutionReport {
  month: string;
  from: string;
  to: string;
  target_days: number;
  fixed: {
    problems: number;
    avg_days: number | null;
    median_days: number | null;
    within_target: number;
    within_target_pct: number | null;
    by_how: { key: string; label: string; problems: number }[];
  };
  found_in_period: number;
  open: {
    problems: number;
    past_target: number;
    oldest_days: number | null;
    by_age: { key: string; label: string; problems: number; past_target: boolean }[];
    page: number;
    page_size: number;
    total: number;
    items: OpenProblem[];
  };
  by_broker_programme: FixGroupRow[];
  programmes: { id: number; name: string }[];
  brokers: { id: number; name: string }[];
}

export async function getExceptionResolution(params: {
  month: string;
  program_id?: number | null;
  broker_party_id?: number | null;
  page?: number;
  page_size?: number;
}): Promise<ExceptionResolutionReport> {
  const { data } = await api.get<ExceptionResolutionReport>("/reports/exception-resolution", {
    params: {
      mga: currentMga(), month: params.month,
      program_id: params.program_id ?? undefined,
      broker_party_id: params.broker_party_id ?? undefined,
      page: params.page ?? 1, page_size: params.page_size ?? 10,
    },
  });
  return data;
}

/** Carrier admin only. Saved in the Audit Logs with the old and new value. */
export async function setFixTarget(days: number): Promise<{ target_days: number; was: number }> {
  const { data } = await api.put("/reports/exception-resolution/target", { days },
    { params: { mga: currentMga() } });
  return data;
}
