/**
 * Reinsurance (ceded) bordereaux — what the carrier passes to its reinsurers,
 * per treaty and reporting period, built from the inward bordereaux it has
 * already processed (backend ceded_engine.py / ceded_routes.py).
 *
 * Prepared automatically when a period's inward bordereaux are in; sent only
 * when the carrier admin presses Send; answered by each reinsurer (agreed or
 * queried) on its private treaty link.
 */
import { api } from "./client";
import { getViewSession } from "./treaty";

export type CededStatus = "draft" | "sent" | "queried" | "agreed" | "superseded";

export type CededTotals = {
  ceded_premium?: number | null;
  ceding_commission?: number | null;
  net_premium?: number | null;
  claims_paid?: number | null;
  claims_outstanding?: number | null;
  recovery?: number | null;
  lines?: number;
};

export type CededSummary = {
  id: number;
  contract_id: number;
  treaty: string;
  type_label: string | null;
  period: string;
  version_no: number;
  status: CededStatus;
  currency: string | null;
  totals: CededTotals;
  reinsurers: number;
  agreed: number;
  queried: number;
  auto_prepared: boolean;
  is_correction: boolean;
  missing_programmes: number;
  skipped: number;
  due_on: string | null;
  overdue: boolean;
  built_at: string | null;
  sent_at: string | null;
};

export type CededStatement = {
  id: number;
  reinsurer_party_id: number;
  reinsurer: string | null;
  shares: Record<string, number>;
  totals: CededTotals;
  previous_totals: CededTotals | null;
  filename: string | null;
  status: "pending" | "sent" | "agreed" | "queried";
  sent_at: string | null;
  sent_to: string[];
  answered_at: string | null;
  answered_by: string | null;
  answer_note: string | null;
};

export type CededLine = Record<string, string | number | null>;

export type CededSource = {
  export_id: number; program_id: number; programme: string | null;
  broker: string | null; filename: string | null; period: string | null;
  version_no: number | null; rows: number; has_premium: boolean;
  has_sum_insured: boolean; has_claims: boolean; notes: string[];
};

export type CededDetail = CededSummary & {
  summary_headline: string | null;
  notes: string[];
  lines: CededLine[];
  lines_total: number;
  skipped: (CededLine & { reason: string })[];
  sources: CededSource[];
  missing: { id: number; name: string }[];
  statements: CededStatement[];
  versions: { id: number; version_no: number; status: CededStatus; sent_at: string | null }[];
  /** The inward files changed since this was prepared. */
  stale: boolean;
  can_send: boolean;
  columns: { key: string; label: string; kind: "text" | "money" | "pct" }[];
  sent?: { emails: number; of: number; no_contact: (string | null)[] };
  /** What Send does, before it is pressed: each reinsurer, who it is
   *  emailed to, and the BDX file attached. */
  send_plan?: { statement_id: number; reinsurer: string | null; reinsurer_party_id: number;
                to: { name: string | null; email: string }[];
                attachment: string | null; attachment_bytes: number }[];
  /** Copied on every email (the setup's cc). */
  cc?: string[];
  /** The treaty's rules the rows broke. */
  checks?: TreatyCheck[];
  /** Critical rules that stop Send. */
  send_blocked_by?: string[];
};

export type CededList = {
  items: CededSummary[];
  counts: Record<CededStatus | "overdue", number>;
  prepared_now: number;
  /** Treaties with an active setup — the ones a statement can be prepared for. */
  treaties: { contract_id: number; name: string;
              /** "own": statements are read from a file the carrier uploads
               *  each period. */
              input_source?: "brokers" | "own"; frequency?: Frequency;
              /** The own input template's file name, when it reads one. */
              input_file?: string | null;
              /** The output layout: Kavachio's, an uploaded one, or Lloyd's. */
              output?: LayoutSource; output_file?: string | null }[];
  /** Live treaties whose setup is missing or not active yet. */
  needs_setup: { contract_id: number; name: string; setup_status: string | null }[];
};

export const listCeded = (contractId?: number) =>
  api.get<CededList>("/reinsurance/bordereaux",
    { params: { contract_id: contractId } }).then(r => r.data);

export const cededPeriods = (contractId: number) =>
  api.get<{ periods: string[] }>(`/reinsurance/bordereaux/periods/${contractId}`)
    .then(r => r.data.periods);

export const prepareCeded = (contractId: number, period: string) =>
  api.post<CededDetail>("/reinsurance/bordereaux",
    { contract_id: contractId, period }).then(r => r.data);

export const getCeded = (id: number) =>
  api.get<CededDetail>(`/reinsurance/bordereaux/${id}`).then(r => r.data);

export const sendCeded = (id: number, note?: string) =>
  api.post<CededDetail>(`/reinsurance/bordereaux/${id}/send`, { note: note || null })
    .then(r => r.data);

async function saveBlob(url: string, filename: string, headers?: Record<string, string>) {
  const r = await api.get<Blob>(url, { responseType: "blob", headers });
  const href = URL.createObjectURL(r.data);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

export const downloadStatement = (cbId: number, s: CededStatement) =>
  saveBlob(`/reinsurance/bordereaux/${cbId}/statements/${s.id}/file`,
           s.filename ?? "statement.xlsx");

// ── on the reinsurer's private link ──────────────────────────────────────────
export type LinkStatement = {
  id: number; period: string; version_no: number; superseded: boolean;
  currency: string | null; shares: Record<string, number>; totals: CededTotals;
  previous_totals: CededTotals | null;
  status: "sent" | "agreed" | "queried";
  sent_at: string | null; answered_by: string | null; answered_at: string | null;
  answer_note: string | null; filename: string | null;
};

const linkHeaders = (token: string) => ({ "X-Treaty-Session": getViewSession(token) });

export const downloadLinkStatement = (token: string, st: LinkStatement) =>
  saveBlob(`/treaty-view/${encodeURIComponent(token)}/statements/${st.id}/file`,
           st.filename ?? "statement.xlsx", linkHeaders(token));

export const answerLinkStatement = (token: string, id: number,
                                    action: "agree" | "query",
                                    name: string, note?: string) =>
  api.post<{ ok: boolean; statements: LinkStatement[] }>(
    `/treaty-view/${encodeURIComponent(token)}/statements/${id}/${action}`,
    { name, note: note || null }, { headers: linkHeaders(token) }).then(r => r.data);

// ── the setup ────────────────────────────────────────────────────────────────
/** How one treaty's statements are made and sent. No statement is prepared
 *  for a treaty until its setup is active. */
export type SetupStatus = "draft" | "pending_approval" | "active";
export type Frequency = "monthly" | "quarterly" | "annually";

/** A column of the reinsurer's file. `key` is what fills it: a figure worked
 *  out for the treaty ("ceded_premium"), "share_pct", "field:<data-model key>"
 *  passed through from the inward bordereau, or "" — left blank. */
export type SetupColumn = {
  key: string;
  heading: string;
  /** Its position in the carrier's own file (an uploaded layout). */
  column_index?: number | null;
  /** Unticked columns of the Lloyd's sheet are left out of the file. */
  keep?: boolean;
  /** The Lloyd's standard makes it mandatory. */
  required?: boolean;
  ref?: string | null;
  /** A reinsurance column added after the Lloyd's ones. */
  added?: boolean;
};

export type LayoutSource = "kavachio" | "uploaded" | "standard";

export type InputLayout =
  | { source: "brokers" }
  | { source: "own"; filename?: string | null; sheet_name?: string | null;
      header_row?: number; data_start_row?: number;
      columns: { name: string; column_index: number; field: string | null }[] };

export type SetupLayout = {
  source: LayoutSource;
  filename?: string | null;
  sheet_name?: string | null;
  header_row?: number | null;
  data_start_row?: number | null;
  standard?: { standard_id: string; label: string; version: string | null;
               jurisdiction: string } | null;
};

/** One of the treaty's rules (backend treaty_rules). */
export type TreatyRule = {
  id: string; label: string;
  kind: "within_term" | "currency" | "required" | "max" | "excluded" | "allowed" | "duplicate";
  subject: string; params: Record<string, unknown>;
  severity: "critical" | "warning"; enabled: boolean;
  /** "terms": from the treaty's structure; "wording": read from its wording. */
  source: "terms" | "wording"; clause: string | null;
};

/** A rule the rows broke — or could not be tested ("untested"). */
export type TreatyCheck = {
  id: string; label: string; severity: "critical" | "warning" | "untested";
  source?: string | null; clause?: string | null; rows: number;
  examples: { policy: string | null; value: string | null; why: string }[];
  why?: string;
};

export type SetupValues = {
  frequency: Frequency;
  due_days: number;
  /** null = every programme the treaty protects. */
  program_ids: number[] | null;
  columns: SetupColumn[];
  layout?: SetupLayout;
  /** Where the statement is read from: the brokers' bordereaux, or a file of
   *  the carrier's own (uploaded each period). */
  input_layout?: InputLayout;
  /** The treaty's rules as set on the Treaty Rules step. */
  rules?: TreatyRule[];
  /** The carrier's file (base64), sent once with the save after an upload. */
  layout_file?: string | null;
  file_format: "xlsx" | "csv";
  /** party id → contact ids. A reinsurer left out gets every contact with an email. */
  recipients: Record<string, number[]>;
  cc: string[];
  send_mode: "manual" | "auto";
};

export type InputTemplate = {
  template_id: number; name: string; version: number | null; active: boolean;
  program_id: number | null; programme: string | null;
  /** Live bordereaux already received on it. */
  files: number;
  /** The carrier's own file, not a programme's template. */
  own?: boolean;
  columns: { name: string; field: string | null; sheet: string | null }[];
};

export type SetupRecord = {
  contract_id: number;
  treaty: string;
  type_label: string | null;
  has_structure: boolean;
  setup: (SetupValues & { status: SetupStatus; note: string | null;
                          has_layout_file?: boolean;
                          submitted_at: string | null; approved_at: string | null;
                          updated_at: string | null }) | null;
  defaults: SetupValues;
  options: {
    programmes: { id: number; name: string; is_home: boolean }[];
    reinsurers: { party_id: number; name: string | null;
                  contacts: { id: number; name: string | null; email: string | null }[] }[];
    columns: { key: string; label: string; kind: string }[];
    /** Fields the programmes' bordereaux carry, to pass through. */
    fields: { key: string; label: string }[];
    share: { key: string; label: string };
    /** The same on every line of a statement — period, treaty, reinsurer… */
    context: { key: string; label: string }[];
    /** The INPUT templates — what each feeding programme's bordereaux are
     *  written to, column by column. */
    inputs: InputTemplate[];
    /** How each worked-out figure is made, and the input fields it reads. */
    derived: Record<string, { how: string; fields: string[] }>;
    /** Every field a column of the carrier's own file can be tied to. */
    all_fields: { key: string; label: string }[];
    /** Which fields each treaty rule reads, by its subject. */
    rule_fields?: Record<string, string[]>;
    frequencies: Frequency[];
  };
  carrier_admin: boolean;
};

export type SetupListItem = {
  contract_id: number; treaty: string; type_label: string | null;
  lifecycle: string; has_structure: boolean; programmes: number;
  status: SetupStatus | null; frequency: Frequency | null;
  send_mode: "manual" | "auto" | null; updated_at: string | null;
};

export type SetupPreview = {
  periods: string[];
  period: string | null;
  lines: Record<string, string | number | null>[];
  lines_total?: number;
  totals: CededTotals;
  reinsurers: { name: string | null; shares: Record<string, number>; totals: CededTotals }[];
  sources: CededSource[];
  missing: { id: number; name: string }[];
  notes: string[];
  skipped: number;
  currency?: string | null;
  checks?: TreatyCheck[];
  /** The file as the reinsurer will see it, column by column. */
  table?: { headings: string[]; rows: (string | number | null)[][] };
};

/** What an uploaded file, or the Lloyd's risk sheet, would give — nothing
 *  is saved until the setup is. */
export type LayoutProposal = {
  layout: SetupLayout;
  columns: SetupColumn[];
  layout_file: string;
  file_format: "xlsx" | "csv";
  /** Ticked columns Kavachio has no data for — left blank. */
  unfilled: string[];
};

export const uploadLayout = (contractId: number, file: File) => {
  const fd = new FormData();
  fd.append("file", file);
  return api.post<LayoutProposal>(`/reinsurance/setups/${contractId}/layout/upload`, fd)
    .then(r => r.data);
};

export const lloydsLayout = (contractId: number, jurisdiction: string, standardId?: string) =>
  api.post<LayoutProposal>(`/reinsurance/setups/${contractId}/layout/standard`,
    { jurisdiction, standard_id: standardId ?? null }).then(r => r.data);

export const listSetups = () =>
  api.get<{ items: SetupListItem[] }>("/reinsurance/setups").then(r => r.data.items);

export const getSetup = (contractId: number) =>
  api.get<SetupRecord>(`/reinsurance/setups/${contractId}`).then(r => r.data);

export const saveSetup = (contractId: number, v: SetupValues) =>
  api.put<SetupRecord>(`/reinsurance/setups/${contractId}`, v).then(r => r.data);

export const activateSetup = (contractId: number) =>
  api.post<SetupRecord>(`/reinsurance/setups/${contractId}/activate`).then(r => r.data);

export const sendBackSetup = (contractId: number, note: string) =>
  api.post<SetupRecord>(`/reinsurance/setups/${contractId}/send-back`, { note })
    .then(r => r.data);

export const previewSetup = (contractId: number, v: SetupValues, period?: string | null) =>
  // The file itself is not needed to preview — only what fills each column.
  api.post<SetupPreview>(`/reinsurance/setups/${contractId}/preview`,
    { ...v, layout_file: null, period: period || null }, { silent: true }).then(r => r.data);

/** A sample of the carrier's own input file → its columns, each tied to a
 *  field. Nothing is saved until the setup is. */
export const uploadInputSample = (contractId: number, file: File) => {
  const fd = new FormData();
  fd.append("file", file);
  return api.post<InputLayout>(`/reinsurance/setups/${contractId}/input/upload`, fd)
    .then(r => r.data);
};

/** This period's own file — stored, and the statement prepared from it. */
export const uploadPeriodInput = (contractId: number, period: string, file: File) => {
  const fd = new FormData();
  fd.append("contract_id", String(contractId));
  fd.append("period", period);
  fd.append("file", file);
  return api.post<CededDetail>("/reinsurance/bordereaux/input", fd).then(r => r.data);
};

/** Check: the statement for a period worked out and shown — from `file` when
 *  the setup reads the carrier's own — with nothing saved. */
export const checkCeded = (contractId: number, period: string, file?: File | null) => {
  const fd = new FormData();
  fd.append("contract_id", String(contractId));
  fd.append("period", period);
  if (file) fd.append("file", file);
  return api.post<SetupPreview>("/reinsurance/bordereaux/check", fd).then(r => r.data);
};

/** Read the treaty's wording for its conditions — the rules returned with
 *  them added. Saved with the setup. */
export const extractTreatyRules = (contractId: number) =>
  api.post<{ rules: TreatyRule[]; read_from: string; found: number }>(
    `/reinsurance/setups/${contractId}/rules/extract`).then(r => r.data);

/** Delete a draft setup (an active one is refused). */
export const deleteSetup = (contractId: number) =>
  api.delete<{ ok: boolean }>(`/reinsurance/setups/${contractId}`).then(r => r.data);
