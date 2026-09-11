// Pure, presentation-agnostic helpers for reading a Direct-lane setup's saved
// routing + column mapping. Shared by the editable builder (DirectSetup) and
// the read-only setup viewer (BordereauSetupDetail) so both derive the same
// "what feeds what" picture from the same saved shape — no separate/hardcoded
// re-implementation per screen.

export type MappingRule = { kind: "copy" | "const" | "source_sheet"; source?: string; value?: string };
export type SheetRoute = { output_sheet: string; sources: { input_sheet: string }[]; filter: unknown };
export type SheetRouting = { version: number; mode: string; confidence: string; routes: SheetRoute[] };

// Derive a schedule identity from a file/sheet name: "Palms Sch H Current BDX"
// → "Schedule H". Mirrors the backend _sb_propose_schedule matcher so an
// uploaded contract auto-binds to the schedule sheet it belongs to.
// Separators (._-) are normalized to spaces first so real-world names like
// "… Schedule F_rss.pdf" still match (an underscore blocks the \b boundary).
// Shared: a contract added from a broker's page must be given the SAME schedule
// the setup builder would have given it, or the two would supersede each other.
export function scheduleOf(name: string | null | undefined): string | null {
  const m = (name || "").toLowerCase().replace(/[._-]+/g, " ")
    .match(/\bsch(?:edule)?\s*([a-z0-9])\b/);
  return m ? `Schedule ${m[1].toUpperCase()}` : null;
}

export const sheetFieldKey = (sheet: string, col: string) => `${sheet}||${col}`;

// column_mapping is keyed {output_sheet: {output_field: rule}}. Split it into:
//  - sel:   (outputSheet, inputColumn) -> outputField   — for kind:"copy" rules
//  - extra: (outputSheet, outputField) -> rule           — for const/source_sheet rules
export function seedFromColumnMapping(columnMapping: Record<string, Record<string, MappingRule>> | null | undefined) {
  const sel: Record<string, string> = {};
  const extra: Record<string, MappingRule> = {};
  for (const [sheet, cols] of Object.entries(columnMapping || {})) {
    for (const [outField, rule] of Object.entries(cols)) {
      if (rule.kind === "copy" && rule.source) sel[sheetFieldKey(sheet, rule.source)] = outField;
      else extra[sheetFieldKey(sheet, outField)] = rule;
    }
  }
  return { sel, extra };
}

// Which output sheet(s) a given input sheet feeds, per the saved routing.
export function outputsForInput(routing: SheetRouting | null | undefined, inputSheet: string): string[] {
  if (!routing) return [];
  return routing.routes
    .filter(r => r.sources.some(s => s.input_sheet === inputSheet))
    .map(r => r.output_sheet);
}

// Which of an input column's candidate output sheets it's actually mapped
// into, and to which output field — an input column belongs to at most one.
export function assignmentFor(
  sel: Record<string, string>, candidateOutputs: string[], inputCol: string,
): { sheet: string; field: string } | null {
  for (const sheet of candidateOutputs) {
    const field = sel[sheetFieldKey(sheet, inputCol)];
    if (field) return { sheet, field };
  }
  return null;
}

// ---- shared types (setup builder, its read-only viewer, and its editor) ----

export type Clause = { rule_id: number; severity?: string; text?: string; page?: number; match?: string; score?: number };
export type OutField = { sheet: string; field: string; clauses: Clause[] };

// A Pipeline binds a (carrier, program) scope's Input Template (a DirectFormat)
// + Output Template + contracts, and carries the run-time active status.
export type Pipeline = {
  id: number; name: string | null; status: "draft" | "active" | "superseded";
  carrier_party_id?: number | null; carrier_name?: string | null;
  program_id?: number | null; program_name?: string | null;
  input_format_id: number | null; input_format_name?: string | null;
  output_template_id: number | null; output_template_name?: string | null;
  ready: boolean; ready_reason: string;
  contracts: { contract_id: number; sheet_key: string | null; filename?: string | null }[];
  created_at?: string | null; modified_at?: string | null;
};

// Contracts uploaded for a program + their inline detail.
export type Contract = { id: number; filename: string | null; status: string; created_at: string | null };
export type ContractTerm = { id: number; category: string | null; value: unknown; source_text?: string | null; confidence?: number | null };
export type ContractRule = {
  validation_rule_id: number; rule_name: string; rule_description?: string | null;
  severity?: string | null; rule_status?: string | null;
  output_field?: string | null; output_fields?: string[]; rule_kind?: string | null;
  source_clause?: { text?: string | null; page_number?: number | null } | null;
  // cross_field_math (formula) rules only — the ±% tolerance band. `reject_pct`,
  // when set above `tolerance_pct`, splits flag (warning) from auto-reject.
  rule_template?: string | null;
  tolerance_pct?: number | null;
  reject_pct?: number | null;
  // Value-matching rules (value_in_set / value_not_in_set / conditional_*) only.
  // `enum_values` are the values the CONTRACT names; `variation_values` are the
  // surface spellings of those values the rule accepts — the list a tenant_admin
  // can extend from the setup screen.
  enum_field?: string | null;
  enum_values?: string[];
  variation_values?: string[];
  // Which of `variation_values` this admin may take back: the ones a person
  // added, never the ones the CONTRACT names (those sit in the same list, and
  // dropping one is not the admin's call). Computed by the SERVER from the same
  // rule the delete route enforces — never re-derive it in the browser, or the UI
  // will eventually offer a removal the API refuses with a 400.
  removable_variations?: string[];
  // Spellings the rule ALSO accepts that are not stored on the rule at all: the
  // compiler bakes in whatever the shared vocabulary knows for these values
  // (USA / US / U.S.A. for "United States of America"). Read-only — they come
  // from the global dictionary, not from this contract.
  vocabulary_values?: string[];
};

// One line's outcome. A REFUSAL IS A 200 — `accepted` is the outcome, not the
// HTTP status; `reason` and `clause_quote` explain it in the contract's own words.
export type VariationResult = {
  spelling: string;
  accepted: boolean;
  reason_code: string;
  reason: string | null;
  clause_quote: string | null;
  matched_value?: string | null;
  vocabulary_written: boolean;
};

// The whole submission from POST .../rules/{id}/variation-values — several
// variations are judged in one request (and one model call).
export type VariationDecision = {
  rule_id: number;
  accepted: boolean;            // true when at least one was applied
  accepted_count: number;
  refused_count: number;
  variation_values: string[];
  sql_changed: boolean;
  vocabulary_written: boolean;
  clause_page: number | null;
  results: VariationResult[];
};

// One staged removal's outcome. A REFUSAL IS A 200, exactly as on the add side.
export type VariationRemovalResult = {
  spelling: string;
  removed: boolean;
  reason_code: string;
  reason: string | null;
  // The contract value the shared dictionary still folds this spelling onto, if
  // any. Non-null means the compiled SQL keeps matching it regardless of the list.
  still_matched_by_dictionary?: string | null;
};

// POST .../rules/{id}/variation-values/remove — the undo, committed as a batch so
// several ✕ clicks are one recompile and one write.
export type VariationRemoval = {
  rule_id: number;
  removed: string[];
  removed_count: number;
  refused_count: number;
  variation_values: string[];
  sql_changed: boolean;
  // How many shared-dictionary entries this rule added were taken back too. Stays
  // 0 when provenance cannot be proven (migration 17 absent) or the entries came
  // from elsewhere — nothing global is ever deleted on a guess.
  vocabulary_removed: number;
  still_matched_by_dictionary: Record<string, string>;
  results: VariationRemovalResult[];
};
// ---- missing BDX columns (the contract-vs-bordereau gap NOTE) --------------
// One column the CONTRACT expects a bordereau to report that this setup's
// bordereau doesn't provide. Produced once per setup by the server-side check
// and stored, so every screen reads the same saved snapshot.
export type MissingColumn = {
  id: number;
  column_name: string;
  severity: "required" | "recommended" | string;
  reason: string | null;
  contract_reference: string | null;   // verbatim clause words, never paraphrased
  // Where the quote came from. Both are resolved server-side by matching the
  // quote back to its clause row — never taken from the model — so a page shown
  // beside a quote is one the contract really carries. null = untraceable quote,
  // which must render as the quote alone rather than an invented reference.
  source_page: number | null;
  clause_label: string | null;
  related_output_field: string | null; // an exact output template column, or null
  sheet_key: string | null;            // the sheet it's specific to, or null
  contract_id: number | null;
  analyzed_at: string | null;
};

/** One clause the extraction judged rule-worthy but could not bind to a column.
 *  `reason` is the extraction's own "why unmapped" note. */
export type UnmappedClause = {
  contract_id: number | null; clause_id: number | null;
  rule_name: string | null; clause_text: string | null;
  source_page: number | null; reason: string | null;
};

// GET /pipelines/{id}/missing-columns (and the POST .../analyze that fills it).
// `analyzed` false = never checked — which is NOT the same as "nothing missing"
// (that is analyzed true with an empty `items`).
export type MissingColumnsResp = {
  pipeline_id: number;
  analyzed: boolean;
  analyzed_at: string | null;
  /** Data points the contract asks for that are in NO rule at all. Only a model
   *  can read these out of the prose, so this half depends on `analyzed`. */
  items: MissingColumn[];
  counts: { total: number; required: number; recommended: number };
  /** Rule-bearing clauses with no output column yet — the review bucket, derived
   *  straight from the extraction. No model call, so these are present and
   *  current even when `analyzed` is false. Optional: older servers omit it. */
  unmapped_clauses?: UnmappedClause[];
  unmapped_count?: number;
  // analyze only: whether this call actually ran the check, and why not if it
  // didn't (no contract, no sample bordereau, check unavailable).
  ran?: boolean;
  skipped_reason?: string | null;
};

export type TemplateField = { name: string; sheet?: string | null };
export type FieldMapping = { contract_field: string; output_field: string; rule_names: (string | null)[] };
export type ClauseRouting = {
  clause_id: number | null; bucket: string; rule_name?: string | null;
  clause_text?: string | null; source_page?: number | null; reason?: string | null;
};
export type ContractDetailT = {
  contract: { id: number; filename: string | null; status: string; created_at: string | null };
  output_template: { name: string; version: number; fields?: TemplateField[] } | null;
  terms: ContractTerm[]; rules: ContractRule[]; field_mappings: FieldMapping[];
  clause_routing?: ClauseRouting[];
};

/** The reason the server gave, when it gave one a person can read.
 *
 *  Most errors carry `detail` as a sentence. An upload the server REFUSES —
 *  too short, the wrong format, password-protected — carries it as
 *  `{success: false, error: {code, message}}`, where `message` is written for
 *  the person uploading. Every screen used to handle that shape badly: one
 *  printed the JSON around the sentence, another replaced it with "could not be
 *  read, please try again", which sends someone to retry a file that can never
 *  pass. Null when there is no such sentence, so each caller keeps its own
 *  fallback. */
export function refusalMessage(e: unknown): string | null {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === "string") return d;
  const m = (d as { error?: { message?: unknown } } | null | undefined)?.error?.message;
  return typeof m === "string" && m.trim() ? m : null;
}

export function errText(e: unknown): string {
  const a = e as { response?: { data?: { detail?: unknown } }; message?: string };
  const d = a?.response?.data?.detail;
  return refusalMessage(e) ?? (d ? JSON.stringify(d) : null) ?? a?.message ?? "Something went wrong";
}

// ---- contract-upload recovery ------------------------------------------------
// A contract upload can run far longer than the connection carrying it survives.
// The pipeline is NOT cancelled when that connection dies — it finishes and
// saves the contract — but the id only ever travels in the reply that was lost.
// These helpers let the caller identify the contract its own upload produced.

/** One row of GET /programs/{id}/contracts, as far as recovery cares. */
export type ProgramContractRow = {
  id: number;
  filename?: string | null;
  output_template_id?: number | null;
  upload_token?: string | null;
};

/** A per-upload correlation id, sent with the upload and stored on the contract. */
export function newUploadToken(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  // Older browsers: randomness only has to make collisions between concurrent
  // uploads implausible, and getRandomValues is far more widely available than
  // randomUUID. Math.random is the last resort so this can never throw.
  if (c?.getRandomValues) {
    const b = new Uint8Array(16);
    c.getRandomValues(b);
    return Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

/**
 * Pick the contract that THIS upload created, from the programme's contracts.
 *
 * Two ways, in order of certainty:
 *
 *  1. `upload_token` — the id we sent with the upload, echoed back on the row.
 *     Exact: it cannot match another build, another tab, or a late contract
 *     left over from an earlier attempt at the same file.
 *
 *  2. Otherwise: a contract that did not exist when we started (`before`),
 *     carrying our filename and this setup's output template, AND carrying no
 *     token of its own. This is an inference, and it is kept because it is the
 *     only thing that works when the server predates the token — without it,
 *     deploying the two halves in either order would strand uploads.
 *
 *     Excluding rows that carry a DIFFERENT token is what keeps the inference
 *     honest: such a row provably belongs to another upload, so guessing it is
 *     ours would hand this setup someone else's contract. Only an untokened row
 *     is genuinely unattributed and therefore fair to infer from. (Every
 *     rolling-deploy permutation lands here correctly: whichever half of the
 *     change a replica is running, our own row is either tokened with our token
 *     or untokened, never tokened with another's.)
 *
 * Returns null when nothing matches, which means "not saved yet, keep waiting"
 * — never "there is no contract".
 */
export function pickRecoveredContract(
  rows: ProgramContractRow[],
  opts: { token: string | null; before: Set<number>; filename: string; templateId: number | null },
): number | null {
  const all = Array.isArray(rows) ? rows : [];
  if (opts.token) {
    const exact = all.filter(c => c.upload_token === opts.token);
    if (exact.length) return Math.max(...exact.map(c => c.id));
    // No row carries the token. That is either "not saved yet" or "this server
    // doesn't record tokens" — indistinguishable here, so fall through. Once
    // any row echoes a token the exact branch wins from then on.
  }
  const fresh = all.filter(c =>
    !opts.before.has(c.id)
    && !(c.upload_token && c.upload_token !== opts.token)   // another upload's
    && c.filename === opts.filename
    && (c.output_template_id == null || c.output_template_id === opts.templateId));
  return fresh.length ? Math.max(...fresh.map(c => c.id)) : null;
}
