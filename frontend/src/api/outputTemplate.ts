/**
 * The Output BDX Template — the blueprint for the file that goes out.
 *
 * A template is agreed at CARRIER -> PROGRAMME -> BROKER -> CONTRACT, the same
 * chain a contract already sits at. The last two are optional everywhere: a
 * setup made before the broker level existed carries carrier + programme only
 * and keeps working, which is why every scope field here is nullable rather
 * than required.
 *
 * There are three ways to get one and they all end in the same editable
 * template: upload a sample workbook (the original `/export/template/generate`,
 * untouched), adopt a bundled reporting standard, or build it from the
 * contract's own terms.
 */
import { api } from "./client";

export type TemplateScope = {
  carrier_party_id: number | null;
  program_id: number | null;
  broker_party_id: number | null;
  contract_id: number | null;
};

export type ScopeNames = {
  carrier: string | null; programme: string | null;
  broker: string | null; contract: string | null;
};

export type OutputTemplate = {
  id: number;
  name: string;
  version: number;
  is_active: boolean;
  approved: boolean;
  output_format: string;
  /** uploaded | standard | contract — where its field list came from. */
  source_kind: "uploaded" | "standard" | "contract";
  standard_meta: {
    standard_id?: string; standard?: string;
    version?: string | null; jurisdiction?: string | null;
  } | null;
  scope: TemplateScope;
  scope_names: ScopeNames;
  has_sample: boolean;
  created_at: string | null;
};

/**
 * How specific the match was. Shown to the user rather than hidden: being told
 * "this is the programme's template, not this contract's" while there is still
 * time to make one is the whole point.
 */
export type MatchLevel = "contract" | "broker" | "programme" | null;

/**
 * Which Bordereau Setup would actually run for a scope.
 *
 * A setup's input mapping is learned against ONE output template — its sheet
 * and column names ARE the mapping's keys — so a setup cannot be pointed at a
 * different template: it would write the right headings with nothing under
 * them. `matches` is what lets a screen say so before a file is uploaded.
 */
export type ScopeSetup = {
  pipeline_id: number;
  name: string | null;
  broker_party_id: number | null;
  output_template_id: number | null;
  output_template_name: string | null;
  matches: boolean;
};

export type ResolveResult = {
  found: boolean;
  match_level: MatchLevel;
  template: OutputTemplate | null;
  scope: TemplateScope;
  scope_names: ScopeNames;
  /** null when this scope has no active setup at all. */
  setup: ScopeSetup | null;
};

export const resolveOutputTemplate = (mga: string, scope: {
  program_id: number; carrier_party_id?: number | null;
  broker_party_id?: number | null; contract_id?: number | null;
}) =>
  api.get<ResolveResult>("/output-template/resolve", {
    params: {
      mga,
      program_id: scope.program_id,
      carrier_party_id: scope.carrier_party_id ?? undefined,
      broker_party_id: scope.broker_party_id ?? undefined,
      contract_id: scope.contract_id ?? undefined,
    },
  }).then(r => r.data);

// --- bundled reporting standards --------------------------------------------

export type Standard = {
  id: string; label: string; version: string | null;
  jurisdictions: string[]; default_jurisdiction: string | null;
  field_count: number;
};

export type StandardsResult = {
  available: boolean;
  output_formats: string[];
  standards: Standard[];
};

export const getStandards = () =>
  api.get<StandardsResult>("/output-template/standards").then(r => r.data);

export type StandardField = {
  ref: string; field: string; required: boolean;
  requirement: string | null; territory: string | null;
  comments: string | null; display_order: number;
};

export const getStandardFields = (standardId: string, jurisdiction: string) =>
  api.get<{ fields: StandardField[]; mandatory_count: number }>(
    `/output-template/standards/${encodeURIComponent(standardId)}/fields`,
    { params: { jurisdiction } }).then(r => r.data);

// --- creating ---------------------------------------------------------------

export type CreateScope = {
  mga: string;
  program_id: number;
  carrier_party_id?: number | null;
  broker_party_id?: number | null;
  contract_id?: number | null;
  output_format?: string;
  name?: string | null;
};

export const createFromStandard = (
  scope: CreateScope, standard_id: string, jurisdiction: string,
  fields?: ProposedField[] | null,
) =>
  api.post<OutputTemplate & { structure: unknown }>(
    "/output-template/from-standard",
    { ...scope, standard_id, jurisdiction, fields: fields ?? null },
  ).then(r => r.data);

// --- both sides of the job --------------------------------------------------
// A territory's published column list is long and a good deal of it does not
// apply to a given binder, so the field list is proposed from what the standard
// and the contract DEMAND together with what the incoming bordereau can
// actually FILL. Nothing is created by this call — it returns a list to agree
// with or change.

export type ProposedField = {
  field: string;
  required: boolean;
  data_type: string;
  /** standard | contract | contract_rule | contract_ai */
  origin: string;
  category?: string | null;
  requirement?: string | null;
  reason?: string | null;
  contract_reference?: string | null;
  source_field?: string | null;
  /** A published column the contract asks for in its own different words —
   *  folded onto the standard's own row rather than added beside it. */
  also_in_contract?: boolean;
  contract_required?: boolean;

  /** Confidently matched to a column of the uploaded bordereau. */
  in_input: boolean;
  /** Not confident enough to fill unattended, but worth keeping the column. */
  likely_in_input: boolean;
  input_column: string | null;
  best_candidate: { source: string; confidence: number } | null;
  mapping_method: string | null;
  mapping_status: string | null;
  confidence: number;
  mapping_reason: string;
  candidates: { source: string; confidence: number; method: string | null;
                compatible: boolean }[];

  /** Ticked by default, with the reason spelled out beside it. */
  recommended: boolean;
  recommend_reason: string;
};

export type SourceAnalysis = {
  fields: ProposedField[];
  counts: {
    total: number; recommended: number; dropped: number;
    from_standard: number; from_contract: number;
    matched_input: number; likely_input: number;
    unfilled_required: number; checked_input: boolean;
  };
  input: { provided: boolean; columns: string[];
           sheets: string[]; sheets_read: string[] };
  standard: { id: string; label: string; version: string | null;
              jurisdiction: string | null;
              /** "full" — the standard supplied the layout. "essential" — it
               *  only supplied the columns every bordereau carries. */
              scope: "full" | "essential";
              field_count: number } | null;
  contract: { source: string; clause_count: number; rule_count: number;
              model_used: boolean; field_count: number };
  /** The bar a match has to clear to be filled without a person looking. */
  threshold: number;
};

export const analyzeSources = (opts: {
  mga: string;
  program_id: number;
  broker_party_id?: number | null;
  contract_id?: number | null;
  standard_id?: string | null;
  jurisdiction?: string | null;
  include_standard_library?: boolean;
  /** "full" — the standard's whole published list, because it IS the layout.
   *  "essential" — only what it marks mandatory, because the contract is the
   *  layout and the standard is filling in what contracts never name. */
  standard_scope?: "full" | "essential";
  read_contract?: boolean;
  inputFile?: File | null;
  inputSheets?: string[];
  contractFile?: File | null;
}) => {
  const fd = new FormData();
  fd.append("program_id", String(opts.program_id));
  fd.append("mga", opts.mga);
  if (opts.broker_party_id) fd.append("broker_party_id", String(opts.broker_party_id));
  if (opts.contract_id) fd.append("contract_id", String(opts.contract_id));
  if (opts.standard_id) fd.append("standard_id", opts.standard_id);
  if (opts.jurisdiction) fd.append("jurisdiction", opts.jurisdiction);
  fd.append("include_standard_library",
            String(opts.include_standard_library !== false));
  fd.append("standard_scope", opts.standard_scope ?? "full");
  fd.append("read_contract", String(opts.read_contract !== false));
  if (opts.inputFile) fd.append("input_file", opts.inputFile);
  for (const s of opts.inputSheets ?? []) fd.append("input_sheets", s);
  if (opts.contractFile) fd.append("contract_file", opts.contractFile);
  return api.post<SourceAnalysis>("/output-template/analyze-sources", fd)
    .then(r => r.data);
};

export type ContractFieldProposal = {
  field: string;
  source_field: string | null;
  data_type: string;
  required: boolean;
  category?: string | null;
  /** contract_rule = the contract's own rules already name it; contract_ai = read from the wording. */
  origin: string;
  reason?: string | null;
  contract_reference?: string | null;
};

export type ContractAnalysis = {
  fields: ContractFieldProposal[];
  /** False when the model was unavailable — the rule-derived fields still came back. */
  model_used: boolean;
  clause_count: number;
  rule_count: number;
  standard_field_count: number;
};

export const analyzeContract = (
  mga: string, contract_id: number,
  standard_id?: string | null, jurisdiction?: string | null,
) =>
  api.post<ContractAnalysis>("/output-template/analyze-contract", {
    mga, contract_id, standard_id, jurisdiction,
  }).then(r => r.data);

export const createFromContract = (
  // The contract may be one staged in the setup form and not uploaded yet, in
  // which case there is no id to send — `contract_name` names the scope and the
  // reviewed `fields` ARE the answer.
  scope: CreateScope & { contract_id?: number | null; contract_name?: string | null },
  opts: {
    standard_id?: string | null; jurisdiction?: string | null;
    include_standard_library?: boolean;
    standard_scope?: "full" | "essential";
    fields?: (ContractFieldProposal | ProposedField)[] | null;
    /** True when `fields` is the complete reviewed list, library included. */
    fields_reviewed?: boolean;
  },
) =>
  api.post<OutputTemplate & { structure: unknown }>(
    "/output-template/from-contract", { ...scope, ...opts }).then(r => r.data);

// --- the field editor -------------------------------------------------------

export type TemplateField = {
  sheet: string;
  column_index: number;
  /** Minted once from the original heading. Renaming never changes it. */
  field_key: string;
  display_name: string;
  column_name: string;
  source_field: string | null;
  source_type: string | null;
  data_type: string | null;
  required: boolean;
  conditional: boolean;
  /** Demanded by the reporting standard — cannot be removed or made optional. */
  system_required: boolean;
  default_value: string | null;
  transformation_rule: string | null;
  display_order: number;
  active: boolean;
  category: string | null;
  standard_ref: string | null;
  standard_note: string | null;
  /** Which column of the bordereau this field was expected to be filled from.
   *  Null on every template built before that check existed. */
  input_match: { column: string | null; method: string | null;
                 confidence: number | null } | null;
};

export type Finding = {
  severity: "critical" | "warning";
  code: string; message: string;
  sheet: string | null; field: string | null;
};

export type ValidationReport = {
  valid: boolean; error_count: number; warning_count: number;
  findings: Finding[];
};

export type FieldsDoc = {
  template: OutputTemplate;
  sheets: string[];
  fields: TemplateField[];
  validation: ValidationReport;
  source_types: string[];
  data_types: string[];
  standard: OutputTemplate["standard_meta"];
  /** What the both-sides check found when this template was built. Null on a
   *  template made before it existed — which is not the same as "all clear". */
  source_check: {
    checked_input: boolean;
    unresolved_required: string[];
    version?: string;
  } | null;
  /** A file has already been generated from this version, so saving forks a new one. */
  locked_by_history: boolean;
};

export const getTemplateFields = (id: number) =>
  api.get<FieldsDoc>(`/output-template/${id}/fields`).then(r => r.data);

export type SaveResult = {
  template: OutputTemplate; validation: ValidationReport;
  fields: TemplateField[];
  /** True when the save created a new version because this one had been used. */
  versioned: boolean;
};

export const saveTemplateFields = (
  id: number, fields: TemplateField[], activate = false,
) =>
  api.put<SaveResult>(`/output-template/${id}/fields`, { fields, activate })
    .then(r => r.data);

export const addTemplateField = (id: number, field: {
  sheet: string; display_name: string;
  source_field?: string | null; source_type?: string | null;
  data_type?: string | null; required?: boolean;
  default_value?: string | null; transformation_rule?: string | null;
  /** Where in the delivery order it lands. Omitted, it appends — which is what
   *  the "Add Field" button has always done. The sheet grid uses it to put a
   *  column immediately left or right of the one being pointed at. */
  position?: number;
}) =>
  api.post<{ template: OutputTemplate; fields: TemplateField[] }>(
    `/output-template/${id}/fields`, field).then(r => r.data);

export const validateTemplate = (id: number) =>
  api.post<ValidationReport>(`/output-template/${id}/validate`).then(r => r.data);

// --- the sample the recipient supplied --------------------------------------

export type SampleInfo = {
  configured: boolean;
  sample: { id: number; filename: string | null; sheets: string[];
            column_count: number; uploaded_at?: string | null } | null;
};

export const getSample = (id: number) =>
  api.get<SampleInfo>(`/output-template/${id}/sample`).then(r => r.data);

export const uploadSample = (id: number, file: File) => {
  const fd = new FormData();
  fd.append("file", file);
  return api.post<SampleInfo>(`/output-template/${id}/sample`, fd).then(r => r.data);
};

export const clearSample = (id: number) =>
  api.delete<SampleInfo>(`/output-template/${id}/sample`).then(r => r.data);

export type SampleComparison = {
  status: "MATCHED" | "REQUIRES_REVIEW";
  checked_sheets: number;
  issue_count: number;
  issues: { status: string; sheet: string | null; field: string | null;
            message: string }[];
};

export const compareWithSample = (id: number) =>
  api.post<{ configured: boolean; comparison: SampleComparison | null }>(
    `/output-template/${id}/compare-sample`).then(r => r.data);

// --- scope pickers ----------------------------------------------------------
// Both reuse endpoints that already existed; neither is new.

export type ProgrammeBroker = {
  id: number; legal_name: string; status: string;
  contract_count: number;
  /** Contracts the carrier has actually approved — the ones that can be worked
   *  with. `contract_count` includes those still waiting. */
  approved_contract_count?: number;
  pending_approvals: number;
};

export const getProgrammeBrokers = (programId: number) =>
  api.get<ProgrammeBroker[]>(`/programs/${programId}/brokers`).then(r => r.data);

export type ScopedContract = {
  id: number; filename: string | null; status: string;
  broker_party_id: number | null; broker_name: string | null;
  approval_status: string;
  inception_dt: string | null; expiry_dt: string | null;
  output_template_id: number | null;
};

export const getScopedContracts = (
  programId: number, brokerPartyId?: number | null, approvedOnly = true,
) =>
  api.get<ScopedContract[]>(`/programs/${programId}/contracts`, {
    params: {
      broker_party_id: brokerPartyId ?? undefined,
      approved_only: approvedOnly || undefined,
    },
  }).then(r => r.data);
