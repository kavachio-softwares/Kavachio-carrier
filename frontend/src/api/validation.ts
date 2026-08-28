import { api } from "./client";

/**
 * Validation is orchestrated by the Python service (it owns the DB and calls
 * the stateless JS engine). The frontend talks only to Python.
 */

export type Severity = "critical" | "warning" | "info";
export type EngineKey = "global" | "custom" | "ajv";

export type CustomViolation = {
  ruleId: number;
  ruleName: string;
  severity: Severity;
  rowIndex: number | null;
  field: string | null;
  actualValue: unknown;
  expectedValue: unknown;
  operator: string | null;
  message: string;
  recordIdentifier: Record<string, unknown> | null;
  affectedRecords: Array<Record<string, unknown>> | null;
  violationDetail: Record<string, unknown> | null;
};

export type ValidationRunResult = {
  success: boolean;
  uploadId: number | string;
  contractId: number | string | null;
  tenantId: number | null;
  stage: string;
  recordCount: number;
  rules: { total: number; custom: number; ajv: number };
  engines: {
    global?: {
      summary: { total: number; passed: number; failed: number };
    };
    custom?: {
      rulesEvaluated: number;
      violations: CustomViolation[];
      criticalCount: number;
    };
    ajv?: {
      // When the AJV engine runs:
      rulesEvaluated?: number;
      violations?: CustomViolation[];
      criticalCount?: number;
      // When it's skipped/disabled:
      enabled?: boolean;
      skipped?: number;
      note?: string;
    };
  };
  exceptions: { total: number; critical: number; warning: number; info: number };
  /** True if exceptions exceeded the persist cap and were truncated. */
  truncated?: boolean;
  proceedToCanonical: boolean;
  // Persistence is performed by the Python orchestrator.
  persisted?: boolean;
  persistError?: string | null;
  runId?: number | null;
};

export type RunValidationInput = {
  uploadId: number | string;
  /** Optional — derived from the upload's policies when omitted. */
  contractId?: number | string;
  stage?: "input" | "output";
  engines?: EngineKey[];
  tenantId?: number;
  /** Output template to validate against — required for stage:"output" so the
   *  validator sees the resolved output columns the contract rules reference. */
  templateId?: number | string;
  correlationId?: string;
};

export async function runValidation(
  input: RunValidationInput
): Promise<ValidationRunResult> {
  const { data } = await api.post<ValidationRunResult>("/api/validate", input);
  return data;
}

/**
 * Plain-English explanation of the rule behind an exception, derived server-side
 * from the rule's IR (the template + params that were actually compiled and
 * run) — see backend rule_explainer.py. Every field is optional: a rule whose
 * IR yields nothing returns {}, and each render site keeps its old fallback.
 */
export type RuleExplanation = {
  /** What the rule requires, as one sentence. The headline. */
  requirement?: string;
  /** What is wrong with the flagged rows. */
  problem?: string;
  /** Row scope in words, when the rule only applies to some rows. */
  applies_to?: string;
  /** Extra surface spellings an enum rule also accepts. */
  accepts_also?: string;
  /** True when the rule is a referral TRIGGER, not a compliance breach — the
   *  reviewer refers the policy rather than correcting a value. */
  is_referral?: boolean;
  /** Chip text for a non-compliance rule kind, e.g. "Referral trigger". */
  kind_label?: string;
  /** Where the rule came from. */
  origin?: "contract" | "standard" | "derived";
  /** Short provenance chip, e.g. "Contract clause · policy.pdf · p.3". */
  origin_label?: string;
  /** One sentence on what that provenance means for the reviewer. */
  origin_note?: string;
  /** The clause / library wording, with internal markers stripped. */
  source_text?: string;
  /** What the reviewer should do about it. */
  how_to_fix?: string;
};

/** A persisted validation_exception row, enriched with policy identifiers and rule context. */
export type StoredException = {
  /** Plain-English explanation of the rule (backend-derived, additive). */
  explanation?: RuleExplanation | null;
  exception_id: number;
  rule_id: number | null;
  source_entity: string | null;
  source_entity_id: number | null;
  severity: Severity | null;
  field_path: string | null;
  expected_value: string | null;
  actual_value: string | null;
  status: string | null;
  /** Reviewer's saved reason/note (set by /api/validate/exceptions/decide). */
  resolution_note?: string | null;
  /** Backend-derived recommended value (expected_value → rule_spec → null). */
  recommendation?: string | null;
  /** Structured allowed values for an enum rule — each is one choice. Preferred
   *  over splitting the "one of: …" string (values may contain commas). */
  recommendation_options?: string[] | null;
  /** One value of the right SHAPE for a format rule (e.g. "1234" for a 4-digit
   *  code). Illustrative only: a format rule constrains what the value looks
   *  like, not what it says, so this is shown as an example and is never
   *  written back or pre-filled into Fix. */
  recommendation_example?: string | null;
  /** That shape in words, e.g. "4 digits" — the tooltip beside the example. */
  recommendation_format?: string | null;
  /** Rule-generation confidence (0..1): how sure the AI was when it created this
   *  rule from the contract. Shown under the recommendation. */
  confidence?: number | null;
  /** Backend classification of why the exception fired (additive). */
  root_cause?: "data_violation" | "mapping_gap" | "rule_incomplete" | "type_mismatch" | null;
  review_reason?: string | null;
  /** Machine-readable exception markers from the validation pass — e.g. a
   *  type-check carries code "type_check" / error_class "type_mismatch", whose
   *  recommendation is a FORMAT description rather than a writable value. */
  code?: string | null;
  error_class?: string | null;
  /** Which of a rule's checks flagged the row, when it is not the rule's own
   *  comparison. "numeric_format" = the cell could not be read as a number, so
   *  the comparison never ran — a different failure, with its own name,
   *  explanation and fix (see backend rule_explainer). Rows carrying it are
   *  listed under their own heading rather than the rule's. */
  check_kind?: "numeric_format" | null;
  created_at: string | null;
  // Policy identifiers
  policy_number: string | null;
  external_policy_number: string | null;
  certificate_number: string | null;
  // Rule enrichment
  rule_name: string | null;
  error_message: string | null;
  // Contract clause that triggered this rule
  contract_clause_text: string | null;
  contract_clause_page: number | null;
  rule_contract_id: number | null;
  // Contract file info for download
  contract_filename: string | null;
  /** 1-based output row of the offending policy (output-stage exceptions only).
   *  Used as a last-resort label when no policy identifier is available. */
  source_row?: number | null;
  /** Output sheet name (direct-lane output exceptions) — decision key. */
  source_sheet?: string | null;
};

export type StoredRun = {
  run_id: number;
  contract_id: number | null;
  validation_stage: string;
  rules_evaluated: number;
  violations_count: number;
  critical_count: number;
  warning_count: number;
  info_count: number;
  rows_validated: number | null;
  proceeded_to_canonical: boolean | null;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
};

export type UploadExceptionsResponse = {
  success: boolean;
  uploadId: number | string;
  validated: boolean;
  run: StoredRun | null;
  exceptions: StoredException[];
  source_file: string | null;
  mga: string | null;
  mapper_id: number | null;
  has_source_blob?: boolean;
  // spec_by_sheet from the mapper — used to reverse-map canonical field → source column+sheet
  mapper_spec: Record<string, Record<string, string | string[]>> | null;
  /** Output template the contract is bound to — enables Fix/Approve write-back. */
  output_template_id?: number | null;
};

/** Latest stored validation run + exceptions for an upload. */
export async function getUploadExceptions(
  uploadId: number | string
): Promise<UploadExceptionsResponse> {
  const { data } = await api.get<UploadExceptionsResponse>(
    `/api/validate/upload/${uploadId}`
  );
  return data;
}

// ── Output-stage (per-download) exceptions ───────────────────────────────────

/** A generate-time exception from /export/downloads/{id} (output stage). */
export type OutputException = {
  severity: string; code?: string; sheet?: string; row?: number;
  column?: string; field?: string; message?: string;
  rule_name?: string; rule_id?: number; contract_filename?: string;
  reason?: string; error_class?: string;
  contract_id?: number; contract_clause_text?: string | null;
  contract_clause_page?: number | null;
  policy_number?: string | null; actual_value?: string | null;
  /** Backend-derived recommended/expected value (rule_id → rule_spec). */
  recommendation?: string | null; expected_value?: string | null;
  /** Structured enum choices (comma-safe) — preferred over the joined string. */
  recommendation_options?: string[] | null;
  /** See StoredException.recommendation_example / recommendation_format. */
  recommendation_example?: string | null;
  recommendation_format?: string | null;
  /** Rule-generation confidence (0..1), when the output stage provides it. */
  confidence?: number | null;
  /** Present once a decision has been saved for this output exception. */
  exception_id?: number | null; status?: string | null;
  resolution_note?: string | null;
  /** Plain-English explanation of the rule (backend-derived, additive). */
  explanation?: RuleExplanation | null;
  /** See StoredException.check_kind / root_cause. */
  check_kind?: "numeric_format" | null;
  root_cause?: string | null;
};

/** Map an output-stage OutputException to the shared StoredException shape so it
 *  groups/renders identically to upload exceptions. */
export function outputExcToStored(x: OutputException, i: number): StoredException {
  const sev = (x.severity || "").toLowerCase();
  const severity: Severity =
    sev === "critical" || sev === "error" || sev === "high" ? "critical"
    : sev === "info" || sev === "low" || sev === "notice" ? "info"
    : "warning";
  return {
    // Real backing exception_id once a decision is saved; else a synthetic
    // negative id so it never collides with a real (positive) exception_id.
    exception_id: x.exception_id ?? -(i + 1),
    rule_id: x.rule_id ?? null,
    source_entity: null,
    source_entity_id: null,
    severity,
    field_path: x.column ?? x.field ?? null,
    expected_value: x.expected_value ?? x.recommendation ?? null,
    recommendation: x.recommendation ?? x.expected_value ?? null,
    recommendation_options: x.recommendation_options ?? null,
    recommendation_example: x.recommendation_example ?? null,
    recommendation_format: x.recommendation_format ?? null,
    confidence: x.confidence ?? null,
    actual_value: x.actual_value ?? null,
    status: x.status ?? "open",
    resolution_note: x.resolution_note ?? null,
    created_at: null,
    policy_number: x.policy_number ?? null,
    external_policy_number: null,
    certificate_number: null,
    rule_name: x.rule_name ?? x.code ?? null,
    error_message: x.reason ?? x.message ?? null,
    contract_clause_text: x.contract_clause_text ?? null,
    contract_clause_page: x.contract_clause_page ?? null,
    rule_contract_id: x.contract_id ?? null,
    contract_filename: x.contract_filename ?? null,
    source_row: x.row ?? null,
    source_sheet: x.sheet ?? null,
    // Hand-enumerated mapper: a field missing here is silently dropped for the
    // whole output lane (every ?download=<id> screen), so it must be listed.
    explanation: x.explanation ?? null,
    // Machine-readable exception class — writeValue keys off these to know a
    // type-check's recommendation is a FORMAT description, not a value.
    code: x.code ?? null,
    error_class: x.error_class ?? null,
    check_kind: x.check_kind ?? null,
    root_cause: (x.root_cause as StoredException["root_cause"]) ?? null,
  };
}

/** One output-stage decision — keyed by rule + policy + field (no real exception_id
 *  exists client-side; the backend materialises/updates the row). */
export type ExportDecision = {
  rule_id?: number | null;
  policy_number?: string | null;
  field?: string | null;
  kind: "approve" | "fix" | "dismiss" | "reject";
  value?: string | null;
  reason?: string | null;
  actual_value?: string | null;
  /** Direct-lane decision key (output sheet + 1-based row). */
  sheet?: string | null;
  row?: number | null;
};

export type ExportDecideResponse = {
  ok: boolean;
  updated: number;
  skipped: Array<{ policy_number?: string | null; field?: string | null; reason: string }>;
  writeback?: FieldsSaveResponse | null;
  /** "direct" when the export was served from the direct lane (landing_record). */
  lane?: string;
};

/** Re-generate a direct-lane export's output BDX with saved corrections applied.
 *  Returns the NEW export id. */
export async function rerenderExport(
  exportId: number | string,
  actor?: string
): Promise<{ export_id: number; exception_count?: number; status?: string }> {
  const { data } = await api.post(`/export/downloads/${exportId}/rerender`,
    { actor: actor ?? null });
  return data;
}

/**
 * Persist Approve/Fix/Dismiss/Reject for a download's OUTPUT-stage exceptions.
 * The backend materialises a validation_exception row per (rule, policy, field)
 * and writes Fix/Approve values back to canonical (SCD-2) in one call.
 */
export async function saveExportDecisions(
  exportId: number | string,
  decisions: ExportDecision[],
  userId?: number
): Promise<ExportDecideResponse> {
  const { data } = await api.post<ExportDecideResponse>(
    `/export/downloads/${exportId}/decide`,
    { decisions, user_id: userId ?? null, apply: true }
  );
  return data;
}

/** Fetch a generated download's output-stage exceptions, mapped to StoredException[]. */
export async function getDownloadExceptions(
  downloadId: number | string
): Promise<StoredException[]> {
  const { data } = await api.get<{ exceptions: OutputException[] }>(
    `/export/downloads/${downloadId}`
  );
  return (data.exceptions ?? []).map(outputExcToStored);
}

/** Batch "Save": re-validate ALL edits together and write one new version per
 *  record (several edits on the same row → a single new version). */
export type FieldsSaveInput = {
  uploadId: number | string;
  templateId: number | string;
  contractId?: number | string | null;
  edits: Array<{
    fieldPath: string;
    newValue: unknown;
    policyId?: number | null;
    exceptionId?: number | null;
    actualValue?: unknown;
  }>;
  apply?: boolean;
};

export type FieldsSaveResultRow = {
  index: number;
  fieldPath: string;
  exceptionId?: number | null;
  ok: boolean;
  editable?: boolean;
  reason?: string | null;
  table?: string;
  column?: string;
  targetId?: number;
  ambiguous?: boolean;
};

export type FieldsSaveResponse = {
  ok: boolean;
  applied?: boolean;
  recordsVersioned?: number;
  results: FieldsSaveResultRow[];
  introducedExceptions?: Array<{
    rule_id?: number | null;
    field_path?: string | null;
    severity?: string | null;
    actual_value?: unknown;
    expected_value?: unknown;
  }>;
  reason?: string | null;
  sql?: string | null;
};

export async function saveFields(input: FieldsSaveInput): Promise<FieldsSaveResponse> {
  const { data } = await api.post<FieldsSaveResponse>("/api/canonical/fields/save", input);
  return data;
}

// ── Persist exception decisions (Screen B "Save decisions") ───────────────────

/** One row's decision, matching ExceptionDecisionTable's Decision shape. */
export type ExceptionDecision = {
  exception_id: number;
  kind: "approve" | "fix" | "dismiss" | "reject";
  value?: string | null;   // corrected value (fix)
  reason?: string | null;  // reviewer reason / note
};

export type DecideResponse = {
  ok: boolean;
  updated: number;
  skipped: Array<{ exception_id: number; reason: string }>;
};

/**
 * Persist the reviewer's Approve/Fix/Dismiss/Reject decisions onto the
 * validation_exception rows. Records the decision + reason only — it does NOT
 * write the corrected value back into canonical data (use saveFields for that).
 */
export async function saveDecisions(
  decisions: ExceptionDecision[],
  userId?: number
): Promise<DecideResponse> {
  const { data } = await api.post<DecideResponse>(
    "/api/validate/exceptions/decide",
    { decisions, user_id: userId ?? null }
  );
  return data;
}

export function toTitleCase(str?: string | null): string {
  if (!str) return "";
  return str
    .toLocaleLowerCase()
    .split(' ')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}
