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

/** The problems waiting on one file for one check, as one row to act on. */
export interface OpenGroup {
  check: string | null;
  /** The most serious of them. */
  severity: "critical" | "warning" | "info" | null;
  problems: number;
  past_target: number;
  oldest_days: number | null;
  file: string | null;
  period: string | null;
  export_id: number | null;
  program_id: number | null;
  broker_party_id: number | null;
  broker_name: string;
  programme_name: string;
  owner: string;
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
    /** The same list, one row per file and check, paged by the same page. */
    groups: OpenGroup[];
    groups_total: number;
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
  /** Narrows the open list only; the counts stay the whole picture. */
  severity?: string | null;
}): Promise<ExceptionResolutionReport> {
  const { data } = await api.get<ExceptionResolutionReport>("/reports/exception-resolution", {
    params: {
      mga: currentMga(), month: params.month,
      program_id: params.program_id ?? undefined,
      broker_party_id: params.broker_party_id ?? undefined,
      page: params.page ?? 1, page_size: params.page_size ?? 10,
      severity: params.severity || undefined,
    },
  });
  return data;
}

// MOPR 42 — the monthly pack (backend monthly_report.py).

/** One month, every measure counted the same way as in any other month. */
export interface MonthMeasures {
  /** Anything due, checked or fixed at all — false before the records began. */
  has_data: boolean;
  /** False while the month is still running: its numbers count to today. */
  complete: boolean;
  timeliness: {
    due: number;
    /** Of those, the ones whose due date has passed. */
    due_so_far: number;
    on_time: number;
    on_time_pct: number | null;
    late: number;
    overdue: number;
    never: number;
    avg_days_late: number | null;
    cases_open: number;
  };
  volumes: {
    files: number;
    rows: number;
    /** New / renewed / cancelled are policies, or rows when files carry no policy number. */
    counted_by: "policies" | "rows";
    new: number;
    renewed: number;
    cancelled: number;
  };
  quality: {
    errors: number;
    critical: number;
    rows_with_errors_pct: number | null;
    clean_first_time: number;
    clean_first_time_pct: number | null;
    top_check: string | null;
  };
  fixes: {
    fixed: number;
    avg_days: number | null;
    within_target: number;
    within_target_pct: number | null;
    /** On the month's last day — or today, for the month still running. */
    waiting: number;
    waiting_past_target: number;
  };
}

export interface MonthlyBrokerRow {
  broker_party_id: number | null;
  program_id: number | null;
  broker_name: string;
  programme_name: string;
  due_so_far: number;
  on_time: number;
  on_time_pct: number | null;
  files: number;
  clean_first_time: number;
  clean_first_time_pct: number | null;
  errors: number;
  avg_fix_days: number | null;
  waiting_past_target: number;
}

export interface MonthlyReport {
  month: string;
  last_month: string;
  last_year: string;
  target_days: number;
  as_of: string;
  months: Record<string, MonthMeasures>;
  /** The month and the five before it, newest first. */
  trend: string[];
  by_broker_programme: MonthlyBrokerRow[];
  programmes: { id: number; name: string }[];
  brokers: { id: number; name: string }[];
}

export async function getMonthlyReport(params: {
  month: string;
  program_id?: number | null;
  broker_party_id?: number | null;
}): Promise<MonthlyReport> {
  const { data } = await api.get<MonthlyReport>("/reports/monthly", {
    params: {
      mga: currentMga(), month: params.month,
      program_id: params.program_id ?? undefined,
      broker_party_id: params.broker_party_id ?? undefined,
    },
  });
  return data;
}

// MOPR 42.2 — sending the Monthly Report (backend report_delivery.py).

/** Someone who receives the report: one of the carrier's own logins. */
export interface ReportPerson {
  user_id: number;
  email: string;
  name: string | null;
}

export interface DeliverySettings {
  /** Off until the carrier turns it on — nothing is emailed before then. */
  auto_send: boolean;
  /** The Nth working day (Monday to Friday) after the month ends. */
  send_day: number;
  file_format: "pdf" | "excel" | "both";
  delivery: "attach" | "link";
  /** False until Send & Schedule is first saved: these are the defaults. */
  saved: boolean;
}

export interface SentReport {
  id: number;
  month: string;
  /** scheduled = by itself, now = Send now, test = an early test send (no longer offered). */
  kind: "scheduled" | "now" | "test";
  sent_at: string | null;
  file_format: "pdf" | "excel" | "both";
  delivery: "attach" | "link";
  people: number;
  delivered: number;
  failed: number;
  status: "sent" | "partly" | "failed";
  /** The numbers were kept, so it can be downloaded as it was sent. */
  has_file: boolean;
}

export interface DeliveryView {
  settings: DeliverySettings;
  /** The carrier's own logins — always everyone the report goes to. */
  recipients: ReportPerson[];
  /** Null while automatic sending is off. */
  next: { send_on: string; month: string } | null;
  sends: SentReport[];
  send_days: number[];
}

export async function getDelivery(): Promise<DeliveryView> {
  const { data } = await api.get<DeliveryView>("/reports/monthly/delivery",
    { params: { mga: currentMga() } });
  return data;
}

/** Carrier admin only. Each change is kept in the Audit Logs. */
export async function saveDelivery(body: Partial<DeliverySettings>): Promise<DeliveryView> {
  const { data } = await api.put<DeliveryView>("/reports/monthly/delivery", body,
    { params: { mga: currentMga() } });
  return data;
}

export interface SendResult {
  id: number;
  month: string;
  kind: SentReport["kind"];
  delivered: number;
  failed: number;
  status: SentReport["status"];
  recipients: { name: string | null; email: string; delivered: boolean }[];
}

/** Send a month's report now, to the carrier's own logins. */
export async function sendMonthlyReport(body: { month?: string }): Promise<SendResult> {
  const { data } = await api.post<SendResult>("/reports/monthly/send", body,
    { params: { mga: currentMga() } });
  return data;
}

async function saveBlob(url: string, params: Record<string, unknown>, filename: string) {
  const r = await api.get<Blob>(url, { params, responseType: "blob" });
  const href = URL.createObjectURL(r.data);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

/** The month as on screen — same filters — as a PDF or Excel file. */
export const downloadMonthlyReport = (p: {
  month: string; format: "pdf" | "excel"; program_id?: number | null; broker_party_id?: number | null;
}) => saveBlob("/reports/monthly/file", {
  mga: currentMga(), month: p.month, format: p.format,
  program_id: p.program_id ?? undefined, broker_party_id: p.broker_party_id ?? undefined,
}, `Monthly Report ${p.month}.${p.format === "pdf" ? "pdf" : "xlsx"}`);

/** A report as it was sent — the numbers kept with it. */
export const downloadSentReport = (s: SentReport, format: "pdf" | "excel") =>
  saveBlob(`/reports/monthly/sends/${s.id}/file`, { mga: currentMga(), format },
    `Monthly Report ${s.month}.${format === "pdf" ? "pdf" : "xlsx"}`);

/** Carrier admin only. Saved in the Audit Logs with the old and new value. */
export async function setFixTarget(days: number): Promise<{ target_days: number; was: number }> {
  const { data } = await api.put("/reports/exception-resolution/target", { days },
    { params: { mga: currentMga() } });
  return data;
}
