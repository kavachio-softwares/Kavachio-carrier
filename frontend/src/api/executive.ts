import { api } from "./client";
import { currentMga } from "../auth";

// Executive Reporting 38.2 — the Executive Report (backend executive_report.py,
// executive_delivery.py, executive_routes.py): the whole book for one day, week,
// month, to read, download, or email to anyone the carrier names.

/** custom = any dates picked, asked for as date_from / date_to (the report's
 *  period then reads "YYYY-MM-DD..YYYY-MM-DD"); never scheduled. */
export type Cadence = "day" | "week" | "month" | "custom";
export type ScheduleCadence = "day" | "week" | "month";
export type FileFormat = "pdf" | "excel" | "both";
export type TrendUnit = "hour" | "day" | "week" | "month";

export interface Fig {
  current: number | null; previous: number | null; change: number | null;
  /** The same month (a day: the same day) a year earlier. */
  last_year?: number | null; change_last_year?: number | null;
}

export interface Escalation {
  type: string; type_label: string; status: string; title: string; detail: string | null;
  owner: string | null; started_on: string | null; due_on: string | null;
  age_days: number | null; overdue: boolean; link: string | null;
  /** Daily only: the day it became an escalation, and whether it is still open now. */
  became_on?: string; still_open?: boolean;
}

/** The dates a report covers — the Executive Report's and Programme reports'. */
export interface ReportWindow {
  cadence: Cadence;
  period: string;
  title: string;
  from: string;
  to: string;
  previous: { period: string; from: string; to: string };
  complete: boolean;
  as_of: string;
  as_at: string;
}

export interface ExecutiveReport {
  cadence: Cadence;
  /** "YYYY-MM", or the week's Monday "YYYY-MM-DD". */
  period: string;
  /** "October 2026" / "Week of 2 Nov 2026". */
  title: string;
  from: string;
  to: string;
  /** The week or month it is compared with. */
  previous: { period: string; from: string; to: string };
  complete: boolean;
  as_of: string;
  /** The day Programmes, Needs attention and Reinsurance are worked out for. */
  as_at: string;
  /** When the server built it (UTC). */
  updated_at: string;
  /** The headline numbers as the email and files write them. */
  headline: { label: string; value: string }[];
  money: {
    /** null on a daily report, whose premium is from the files processed that day. */
    month: string | null; basis?: "processed"; currency: string | null; files: number; other_currencies: number;
    gross_premium: Fig | null; commission: Fig | null;
    /** Missing on a report sent before it was added. */
    earned_premium?: Fig | null;
    top_programmes: { id: number; name: string; gross_premium: number }[];
    top_brokers: { id: number; name: string; gross_premium: number }[];
  };
  operations: {
    files: number; rows: number; errors: number; critical: number;
    clean_first_time: number; clean_first_time_pct: number | null;
    previous: { files: number; errors: number; clean_first_time_pct: number | null };
  };
  timeliness: { month: string | null; due: number; due_so_far: number; on_time: number;
                on_time_pct: number | null; previous_on_time_pct: number | null };
  /** Daily: `open` is problems that started that day, `fixed` those fixed that day,
   *  and the escalations are the ones that arose that day. */
  problems: { open: number; overdue: number; fixed?: number; escalations: number; escalation_list: Escalation[] };
  /** Daily: `changes` is set and new / went_live / into_runoff count that day's changes. */
  portfolio: { total: number; active: number; runoff: number; not_live: number;
               changes?: boolean; new?: number; went_live?: number; into_runoff?: number;
               /** Programmes by stage (Active, Under Review, Runoff, In Setup, Onboarding). */
               by_stage?: { stage: string; programmes: number }[] };
  /** 37.2 — errors over the 14 days / 12 weeks / 12 months up to the period's end. */
  history?: TrendPoint[];
  /** 37 — the programmes with the most errors, split by severity (5 shown of `total`). */
  errors_by_programme?: { total: number; rows: { id: number; name: string; errors: number;
                                                 critical: number; warning: number; info: number }[] };
  /** Errors inside the period: by hour for a day, by day up to 31 days, by week
   *  up to 26 weeks, else by month. `to` is not included. */
  trend: TrendPoint[];
  /** The statements due in the period, and whether each was sent on time. */
  reinsurance: { due: number; sent_on_time: number; sent_late: number;
                 not_sent: number; overdue: number;
                 statements: { contract_id: number; treaty: string; period: string;
                               due_on: string | null; overdue: boolean;
                               status: "Sent on time" | "Sent late" | "Not sent";
                               sent_on: string | null }[] };
  /** Files as they first arrived, and the checks that failed most. */
  quality: {
    clean: number; with_errors: number; not_checked: number;
    /** `key` opens the policies that check named (missing on reports sent before it was kept). */
    top_errors: { check: string; key?: string | null; files: number; errors: number; severity: string | null }[];
  };
  /** One row per broker and per programme, most late or missing first (10 shown of `total`). */
  scorecard: { broker: { total: number; rows: ScoreRow[] }; programme: { total: number; rows: ScoreRow[] } };
  /** Files due in the period that came late or had not come by its end (latest due first). */
  late: { late: number; missing: number; never: number; total: number;
          list: { program_id: number; broker_party_id: number; programme: string; broker: string;
                  month: string; due_on: string; arrived_on: string | null;
                  status: "Arrived late" | "Not yet" | "Never arrived"; days_late: number }[] };
  /** Contract utilization: bound contracts that sent files (and those that sent none),
   *  and premium written over each term against its premium cap, fullest first. */
  contracts: {
    bound: number; with_files: number; without_files: number;
    idle: ContractRef[];
    capped: number; no_cap: number; over_cap: number; near_cap: number;
    caps: (ContractRef & { currency: string | null; cap: number; written: number;
                           used_pct: number | null; flag: "Over cap" | "Near cap" | null;
                           other_currency_rows: number })[];
  };
}

export interface TrendPoint {
  unit: TrendUnit; label: string; from: string; to: string;
  files: number; errors: number; clean_first_time_pct: number | null;
}

export interface ScoreRow {
  id: number; name: string; premium: number | null; files: number; errors: number;
  critical?: number; warning?: number; info?: number;
  clean_first_time_pct: number | null; prev_clean_first_time_pct: number | null;
  /** Files due in the period so far; on time out of those. */
  due: number; on_time_pct: number | null; prev_on_time_pct: number | null;
  late_or_missing: number;
}
export interface ContractRef { contract_id: number; contract: string; programme: string; broker: string }

export interface Login { user_id: number; email: string; name: string | null }

export interface ExecutiveSettings {
  auto_send: boolean;
  cadence: ScheduleCadence;
  /** 0 = Monday … 4 = Friday. */
  week_day: number;
  /** Nth working day of the month. */
  send_day: number;
  file_format: FileFormat;
  /** The logins ticked; null = every login. */
  user_ids: number[] | null;
  emails: string[];
  saved: boolean;
}

export interface SentExecutive {
  id: number; cadence: Cadence; period: string; title: string; kind: "scheduled" | "now";
  sent_at: string | null; sent_by: string | null; file_format: FileFormat;
  recipients: { name: string | null; email: string; delivered: boolean }[];
  delivered: number; failed: number; status: "sent" | "partly" | "failed"; has_file: boolean;
}

export interface ExecutiveDelivery {
  settings: ExecutiveSettings;
  logins: Login[];
  next: { send_on: string; period: string; cadence: Cadence; title: string } | null;
  sends: SentExecutive[];
  send_days: number[];
  week_days: number[];
}

export interface ExecutiveSendResult {
  id: number; cadence: Cadence; period: string; title: string;
  delivered: number; failed: number; status: "sent" | "partly" | "failed";
  recipients: { name: string | null; email: string; delivered: boolean }[];
}

/** What the server is asked for: custom dates as date_from / date_to, any
 *  other report by its period. */
function when(cadence: Cadence, period?: string | null): Record<string, string | undefined> {
  if (cadence !== "custom") return { cadence, period: period || undefined };
  const [date_from, date_to] = (period ?? "").split("..");
  return { cadence, date_from, date_to };
}

export async function getExecutiveReport(p: { cadence: Cadence; period?: string | null }): Promise<ExecutiveReport> {
  return (await api.get<ExecutiveReport>("/reports/executive", {
    params: { mga: currentMga(), ...when(p.cadence, p.period) } })).data;
}

/** The last six months of premium, for the trend lines in the money tiles
 *  (none for a daily report). Asked for after the report itself. */
export interface MoneyTrend {
  currency: string | null;
  months: { month: string; gross: number | null; commission: number | null; earned: number | null }[];
}

export async function getMoneyTrend(p: { cadence: Cadence; period?: string | null }): Promise<MoneyTrend> {
  return (await api.get<MoneyTrend>("/reports/executive/money-trend", {
    params: { mga: currentMga(), ...when(p.cadence, p.period) } })).data;
}

export async function getExecutiveDelivery(): Promise<ExecutiveDelivery> {
  return (await api.get<ExecutiveDelivery>("/reports/executive/delivery",
    { params: { mga: currentMga() } })).data;
}

export async function saveExecutiveDelivery(body: Partial<ExecutiveSettings>): Promise<ExecutiveDelivery> {
  return (await api.put<ExecutiveDelivery>("/reports/executive/delivery", body,
    { params: { mga: currentMga() } })).data;
}

/** Hide every sent report from the Sent reports list. Nothing is deleted: the
 *  records and files stay, and a scheduled report is never sent twice. */
export async function clearExecutiveSends(): Promise<ExecutiveDelivery> {
  return (await api.post<ExecutiveDelivery>("/reports/executive/sends/clear", null,
    { params: { mga: currentMga() } })).data;
}

export async function sendExecutiveReport(body: {
  cadence: Cadence; period: string; user_ids: number[]; emails: string[]; file_format: FileFormat;
}): Promise<ExecutiveSendResult> {
  const { period, ...rest } = body;
  return (await api.post<ExecutiveSendResult>("/reports/executive/send",
    { ...rest, ...when(body.cadence, period) }, { params: { mga: currentMga() } })).data;
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

const ext = (f: "pdf" | "excel") => (f === "pdf" ? "pdf" : "xlsx");

export const downloadExecutiveReport = (p: { cadence: Cadence; period: string; format: "pdf" | "excel" }) =>
  saveBlob("/reports/executive/file", { mga: currentMga(), format: p.format, ...when(p.cadence, p.period) },
    `Executive Report ${p.period.replace("..", " to ")}.${ext(p.format)}`);

export const downloadSentExecutive = (s: SentExecutive, format: "pdf" | "excel") =>
  saveBlob(`/reports/executive/sends/${s.id}/file`, { mga: currentMga(), format },
    `Executive Report ${s.period.replace("..", " to ")}.${ext(format)}`);

// ── Programme Management Report (backend programme_report.py) ─────────────
// A sub-screen of the Executive Report: every programme and broker for the
// same period, each opening its detail under its row.

export interface ManagementRow {
  program_id: number; broker_party_id: number | null;
  programme: string; broker: string; stage: string | null;
  line_of_business: string | null; reporting_period: string | null;
  /** Files due by the period's end; received; missing (not come, past due); late. */
  expected: number; received: number; missing: number; late: number;
  files: number; errors: number; critical: number; warning: number; info: number;
  premium: number | null;
  /** Attention: a file missing or late, or critical errors. */
  status: "Attention" | "OK";
}

export interface ProgrammeManagement extends ReportWindow {
  /** The bordereau month premium is read for; null on a daily report. */
  money_month: string | null; currency: string | null;
  totals: { programmes: number; writing: number; expected: number; received: number;
            missing: number; errors: number; premium: number | null };
  rows: ManagementRow[];
}

export interface OwedFile {
  month: string; due_on: string; arrived_on: string | null; state: string;
  status: string; due: boolean; days_late: number;
}

export interface ProgrammeBroker extends ReportWindow {
  programme: { id: number; name: string };
  broker: { id: number; name: string };
  money: Pick<ExecutiveReport["money"], "month" | "basis" | "currency" | "gross_premium" | "commission" | "earned_premium">;
  operations: ExecutiveReport["operations"];
  timeliness: ExecutiveReport["timeliness"];
  submissions: { at: string | null; filename: string | null; month: string | null;
                 status: "clean" | "has_exceptions" | "not_validated" | string;
                 errors: number; critical: number; rows: number }[];
  files_total: number;
  owed: { expected: number; received: number; list: OwedFile[] };
  validation: { rows: number; rows_with_errors: number; errors: number; critical: number;
                warning: number; info: number; top_errors: ExecutiveReport["quality"]["top_errors"] };
  /** The broker's current contract (Overview, Premium cap). */
  contract: ContractDetail | null;
  /** Every contract this broker holds on the programme, the current one first. */
  contracts: ContractDetail[];
  /** The treaties protecting the programme — they cover every broker's business on it. */
  treaties: (ContractDetail & { reinsurers: string[] })[];
}

export interface ContractDetail {
  id: number; name: string; type: string | null; status: string | null;
  starts: string | null; ends: string | null; umr: string | null;
  class_of_business: string | null; year_of_account: string | null; current: boolean;
  cap: null | { currency: string | null; cap: number; written: number;
                used_pct: number | null; flag: string | null };
}

export async function getProgrammeManagement(p: { cadence: Cadence; period?: string | null }): Promise<ProgrammeManagement> {
  return (await api.get<ProgrammeManagement>("/reports/executive/programmes", {
    params: { mga: currentMga(), ...when(p.cadence, p.period) } })).data;
}

export async function getProgrammeBroker(programme: number, broker: number,
                                         p: { cadence: Cadence; period?: string | null }): Promise<ProgrammeBroker> {
  return (await api.get<ProgrammeBroker>(`/reports/executive/programmes/${programme}/brokers/${broker}`, {
    params: { mga: currentMga(), ...when(p.cadence, p.period) } })).data;
}

export const downloadProgrammeManagement = (p: { cadence: Cadence; period: string; format: "pdf" | "excel" }) =>
  saveBlob("/reports/executive/programmes/file", { mga: currentMga(), format: p.format, ...when(p.cadence, p.period) },
    `Programme Management Report ${p.period.replace("..", " to ")}.${ext(p.format)}`);
