import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  CheckCircle2, AlertTriangle, ChevronDown, ChevronRight, Wand2,
  Search, RefreshCw, FileText, Quote, ShieldAlert, Layers,
} from "lucide-react";
import { api } from "../api/client";
import { currentMga } from "../auth";
import Card from "../components/ui/Card";
import Button from "../components/ui/Button";
import { Select, TextInput } from "../components/ui/Field";
import { PageBody, PageHeader } from "../components/Layout";
import OutputTemplateFields from "../components/OutputTemplateFields";
import TemplateSheetPreview from "../components/TemplateSheetPreview";
import { LoadingOverlay } from "../components/Busy";

type ExtraDef = {
  display_name: string; description?: string; data_type: string;
  shared?: boolean; _origin_tenant_id?: number | null;
};
type Candidate = { canonical: string; confidence: number; reason?: string };

type TplColumn = {
  column_index: number; column_name: string; samples: string[];
  canonical_field: string | null; confidence?: number | null;
  candidates?: Candidate[];
  transform: string | null; static_value: string | null;
  // The field-builder's half of the same column. Written by
  // `output_template_fields.complete_structure`, so every template has them on
  // read even if it predates them — which is what lets the sheet preview draw
  // the file exactly as it would be delivered.
  display_name?: string | null;
  required?: boolean | null;
  system_required?: boolean | null;
  active?: boolean | null;
  display_order?: number | null;
  data_type?: string | null;
  input_match?: { column?: string | null; confidence?: number | null } | null;
};
type TplSheet = {
  sheet_name: string; header_row: number; data_start_row: number;
  row_strategy: string; columns: TplColumn[];
  // Set by the AI header classifier (exporter.classify_sheet_roles). "reference"
  // and "summary" tabs are excluded from rule generation; the user can override
  // here before proceeding.
  sheet_role?: "data" | "reference" | "summary" | null;
  rule_generatable?: boolean | null;
  sheet_role_reason?: string | null;
};

// A sheet is a rule target unless it's explicitly classified reference.
function isReferenceSheet(sh: TplSheet): boolean {
  return sh.sheet_role === "reference";
}
function isSummarySheet(sh: TplSheet): boolean {
  return sh.sheet_role === "summary";
}
// Excluded from rule generation — reference, summary, or (legacy templates
// saved before sheet_role existed) explicitly marked non-generatable.
function isNonDataSheet(sh: TplSheet): boolean {
  return isReferenceSheet(sh) || isSummarySheet(sh) || sh.rule_generatable === false;
}
function sheetBucket(sh: TplSheet): "data" | "reference" | "summary" {
  if (isSummarySheet(sh)) return "summary";
  if (isReferenceSheet(sh) || sh.rule_generatable === false) return "reference";
  return "data";
}
type Template = {
  id: number; mga: string; name: string; carrier?: string;
  approved: boolean; output_format?: string; structure: { sheets: TplSheet[] };
};

// File formats an output template can produce. Excel preserves the sample
// workbook's styling; the others serialize the mapped rows.
const OUTPUT_FORMATS: { value: string; label: string }[] = [
  { value: "xlsx", label: "Excel (.xlsx)" },
  { value: "csv", label: "CSV (.csv)" },
  { value: "xml", label: "XML (.xml)" },
  { value: "json", label: "JSON (.json)" },
];

const STRATEGIES = ["policy", "claim", "coverage", "premium_transaction",
                    "insured_location", "building"];
const TRANSFORMS = ["", "upper", "lower", "date", "money", "int", "str"];

type ModelField = { key: string; table: string; column: string; type: string;
                    description: string; group: string };

type ContractRule = {
  rule_id: number; rule_engine: string; rule_name: string;
  rule_description: string; severity: string;
  source_clause: string | null; error_message: string | null;
  rule_spec: any; generation_confidence: number;
};
type ContractInfo = { id: number; filename: string; status: string };
type ContractMapping = {
  contract: ContractInfo | null;
  field_rules: Record<string, ContractRule[]>;
};

export default function OutputTemplate() {
  const { id } = useParams();
  const [t, setT] = useState<Template | null>(null);
  const [model, setModel] = useState<Record<string, any>>({});
  const [extras, setExtras] = useState<Record<string, ExtraDef>>({});
  const [contractMapping, setContractMapping] = useState<ContractMapping | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // Bumped when the grid above changes a column, so the field builder below
  // re-reads rather than showing the list as it was a moment ago.
  const [fieldsKey, setFieldsKey] = useState(0);

  // Re-read after the field builder saves, so the sheet preview above it shows
  // the rename / reorder / removal that was just made rather than the layout as
  // it was when the page opened.
  const reloadTemplate = useCallback(() => {
    api.get<Template>(`/export/template/${id}`).then(r => setT(r.data));
  }, [id]);

  useEffect(() => {
    api.get<Template>(`/export/template/${id}`).then(r => setT(r.data));
    api.get(`/data-model`).then(r => setModel(r.data));
    api.get(`/extra-fields`, { params: { mga: currentMga() } })
      .then(r => setExtras(r.data || {}))
      .catch(() => setExtras({}));
    api.get<ContractMapping>(`/export/template/${id}/contract-mapping`)
      .then(r => setContractMapping(r.data))
      .catch(() => setContractMapping({ contract: null, field_rules: {} }));
  }, [id]);

  // Canonical fields plus user-defined extras (all searchable, grouped).
  const allFields: ModelField[] = useMemo(() => {
    const base = Object.entries(model).map(([k, v]: any) => ({
      key: k, table: v.table, column: v.column, type: v.type,
      description: v.description ?? "", group: "Canonical fields",
    }));
    const xs = Object.entries(extras).map(([k, def]) => ({
      key: `_xf:${k}`,
      table: "extras", column: k, type: def.data_type,
      description: def.description ?? "",
      group: def.shared ? "Extra fields · shared" : "Extra fields",
    }));
    return [...base, ...xs];
  }, [model, extras]);

  function patchColumn(sheetIdx: number, colIdx: number, patch: Partial<TplColumn>) {
    setT(prev => {
      if (!prev) return prev;
      const s = prev.structure.sheets.map((sh, i) => {
        if (i !== sheetIdx) return sh;
        return { ...sh, columns: sh.columns.map((c, j) =>
          j === colIdx ? { ...c, ...patch } : c) };
      });
      return { ...prev, structure: { sheets: s } };
    });
  }
  function patchSheet(sheetIdx: number, patch: Partial<TplSheet>) {
    setT(prev => {
      if (!prev) return prev;
      const s = prev.structure.sheets.map((sh, i) =>
        i === sheetIdx ? { ...sh, ...patch } : sh);
      return { ...prev, structure: { sheets: s } };
    });
  }
  function setSheetRole(sheetIdx: number, role: "data" | "reference" | "summary") {
    patchSheet(sheetIdx, { sheet_role: role, rule_generatable: role === "data" });
  }
  async function save(approve: boolean) {
    if (!t) return;
    setBusy(true); setMsg(null);
    try {
      const { data } = await api.put(`/export/template/${id}`, {
        structure: t.structure,
        approved: approve || t.approved,
        output_format: t.output_format ?? "xlsx",
      });
      setT(data); setMsg(approve ? "Approved & activated." : "Saved.");
    } catch (e: any) { setMsg(e?.message ?? "Save failed."); }
    finally { setBusy(false); }
  }

  async function refreshCandidates() {
    if (!t) return;
    if (!confirm("Re-run AI mapping against the original sample? Transforms "
      + "and static values you've set will be preserved.")) return;
    setRefreshing(true); setMsg(null);
    try {
      const { data } = await api.post(`/export/template/${id}/refresh`);
      setT(data);
      setMsg("AI candidates refreshed.");
    } catch (e: any) {
      const d = e?.response?.data?.detail;
      setMsg(typeof d === "string" ? d : d?.message ?? "Refresh failed.");
    } finally { setRefreshing(false); }
  }

  // Detect whether any column has populated candidates. If none do, the
  // template predates the ranked-candidates feature — surface a Refresh
  // button so the user can backfill without re-uploading.
  const hasAnyCandidates = !!t?.structure?.sheets?.some(sh =>
    sh.columns.some(c => (c.candidates?.length ?? 0) > 0)
  );

  if (!t) return null;

  return (
    <>
      {refreshing && (
        <LoadingOverlay label="Re-running the AI mapping against the original sample — this can take a minute…" />
      )}
      <PageHeader
        title={`Template: ${t.name}`}
        subtitle={`${t.carrier ?? "—"} · review AI-proposed column mapping`}
        action={
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1.5 text-sm text-ink-muted">
              Output format
              <Select
                className="!py-1 !w-auto"
                value={t.output_format ?? "xlsx"}
                onChange={e => setT(prev => prev ? { ...prev, output_format: e.target.value } : prev)}
              >
                {OUTPUT_FORMATS.map(f => (
                  <option key={f.value} value={f.value}>{f.label}</option>
                ))}
              </Select>
            </label>
            <Link to="/outputs"><Button variant="ghost">Cancel</Button></Link>
            {!hasAnyCandidates && (
              <Button variant="secondary" onClick={refreshCandidates} disabled={busy || refreshing}>
                <RefreshCw size={14} /> Re-Run AI Mapping
              </Button>
            )}
            <Button onClick={() => save(false)} variant="secondary" disabled={busy}>
              Save Draft
            </Button>
            <Button onClick={() => save(true)} disabled={busy}>
              Save & Activate
            </Button>
          </div>
        }
      />
      <PageBody>
        <Card>
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-3">
              <span className={`pill ${t.approved ? "pill-green" : "pill-amber"}`}>
                {t.approved ? "Approved" : "Draft"}
              </span>
              <span className="text-sm text-ink-muted">
                Map every output column to a canonical or extra field. The
                sample workbook's fonts, fills, merges &amp; widths are
                preserved on generation.
              </span>
            </div>
            {msg && <span className="text-sm text-emerald-700">{msg}</span>}
          </div>
          {!hasAnyCandidates && (
            <div className="mt-3 text-xs text-amber-700 bg-amber-50 border border-amber-200
                            rounded-md px-3 py-2 flex items-start gap-2">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <div>
                <strong>This template predates the ranked-candidates feature.</strong>{" "}
                Per-column AI scores are empty — click <em>Re-Run AI Mapping</em>{" "}
                above to backfill the top-10 candidates for every column.
                Your transforms and static values will be preserved.
              </div>
            </div>
          )}
        </Card>

        {/* The template AS THE SPREADSHEET IT BECOMES. A layout is read across,
            not down: which headings sit together, how wide the file runs, which
            ones are mandatory. None of that is visible in a list of field rows,
            and it is the first thing anyone opening a template wants to know. */}
        <Card>
          <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
            <div className="flex items-center gap-2">
              <Layers size={15} className="text-accent shrink-0" />
              <h3 className="text-sm font-medium">The file this template produces</h3>
            </div>
            <span className="text-xs text-ink-muted">
              Active columns only, in delivery order, under the names the file
              will carry — edit them below.
            </span>
          </div>
          <TemplateSheetPreview sheets={t.structure?.sheets ?? []}
            templateId={Number(id)}
            onChanged={() => { reloadTemplate(); setFieldsKey(k => k + 1); }} />
        </Card>

        {/* The blueprint itself: which columns the delivered file carries, what
            feeds each one, and in what order. The mapping review below is the
            other half of the same template — this decides WHAT the fields are,
            that decides where their values come from. */}
        <OutputTemplateFields templateId={Number(id)} refreshKey={fieldsKey}
          onSaved={reloadTemplate} />

        {contractMapping?.contract && (
          <Card>
            <div className="flex items-center gap-2 text-sm">
              <FileText size={14} className="text-accent shrink-0" />
              <span className="font-medium">Active contract:</span>
              <span className="text-ink-muted">{contractMapping.contract.filename}</span>
              <span className="pill pill-green text-[11px]">{contractMapping.contract.status}</span>
              <span className="ml-auto text-xs text-ink-muted">
                {Object.values(contractMapping.field_rules).flat().length} rule
                {Object.values(contractMapping.field_rules).flat().length !== 1 ? "s" : ""} extracted
                across {Object.keys(contractMapping.field_rules).length} fields
              </span>
            </div>
          </Card>
        )}

        <Card>
          <div className="flex items-center gap-2">
            <Layers size={15} className="text-accent shrink-0" />
            <h3 className="text-sm font-semibold">Sheet Classification</h3>
            <span className="ml-auto text-xs text-ink-muted">
              {t.structure.sheets.filter(sh => !isNonDataSheet(sh)).length} data ·{" "}
              {t.structure.sheets.filter(isReferenceSheet).length} reference ·{" "}
              {t.structure.sheets.filter(isSummarySheet).length} summary
            </span>
          </div>
          <p className="mt-1 text-xs text-ink-muted">
            The AI classified each tab from its column headers.{" "}
            <strong>Data</strong> tabs are validated (rules are generated &amp; run on them);{" "}
            <strong>Reference</strong> tabs are lookup / mapping tables, and{" "}
            <strong>Summary</strong> tabs roll up totals from the data tabs — both are{" "}
            <strong>excluded from rule generation</strong>. Change any that are wrong before proceeding.
          </p>
          <div className="mt-3 space-y-1.5">
            {t.structure.sheets.map((sh, si) => {
              const bucket = sheetBucket(sh);
              return (
                <div key={sh.sheet_name}
                     className="flex items-center justify-between gap-3 rounded-md border
                                border-border px-3 py-2">
                  <div className="min-w-0">
                    <span className="font-mono text-[13px]">{sh.sheet_name}</span>
                    {sh.sheet_role_reason && (
                      <p className="text-xs text-ink-muted mt-0.5 line-clamp-2">
                        {sh.sheet_role_reason}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center rounded-md border border-border overflow-hidden shrink-0">
                    <button
                      onClick={() => setSheetRole(si, "data")}
                      className={`px-3 py-1 text-xs font-medium transition ${
                        bucket === "data" ? "bg-emerald-600 text-white" : "hover:bg-surface-2 text-ink-muted"}`}>
                      Data
                    </button>
                    <button
                      onClick={() => setSheetRole(si, "reference")}
                      className={`px-3 py-1 text-xs font-medium transition ${
                        bucket === "reference" ? "bg-slate-500 text-white" : "hover:bg-surface-2 text-ink-muted"}`}>
                      Reference
                    </button>
                    <button
                      onClick={() => setSheetRole(si, "summary")}
                      className={`px-3 py-1 text-xs font-medium transition ${
                        bucket === "summary" ? "bg-sky-600 text-white" : "hover:bg-surface-2 text-ink-muted"}`}>
                      Summary
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </Card>

        {t.structure.sheets.map((sh, si) => (
          <SheetBlock
            key={sh.sheet_name}
            sheet={sh}
            allFields={allFields}
            fieldRules={contractMapping?.field_rules ?? {}}
            onPatchSheet={(p) => patchSheet(si, p)}
            onPatchColumn={(ci, p) => patchColumn(si, ci, p)}
          />
        ))}
      </PageBody>
    </>
  );
}

function SheetBlock({ sheet: sh, allFields, fieldRules,
                     onPatchSheet, onPatchColumn }: {
  sheet: TplSheet;
  allFields: ModelField[];
  fieldRules: Record<string, ContractRule[]>;
  onPatchSheet: (p: Partial<TplSheet>) => void;
  onPatchColumn: (colIdx: number, p: Partial<TplColumn>) => void;
}) {
  const [open, setOpen] = useState(true);
  const mappedCount = sh.columns.filter(c => c.canonical_field).length;
  const unmappedCount = sh.columns.length - mappedCount;

  return (
    <section className="card overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between gap-3 px-5 py-3
                   hover:bg-surface-2 transition text-left">
        <span className="flex items-center gap-2 text-base font-semibold">
          {open
            ? <ChevronDown size={16} className="text-ink-muted" />
            : <ChevronRight size={16} className="text-ink-muted" />}
          Sheet:
          <span className="font-mono text-[13px] text-ink-muted">{sh.sheet_name}</span>
          {sheetBucket(sh) === "reference" && (
            <span className="pill bg-slate-100 text-slate-600 border-slate-200">
              <Layers size={11} /> Reference — Excluded From Rules
            </span>
          )}
          {sheetBucket(sh) === "summary" && (
            <span className="pill bg-sky-100 text-sky-700 border-sky-200">
              <Layers size={11} /> Summary — Excluded From Rules
            </span>
          )}
        </span>
        <span className="flex items-center gap-2">
          <span className="pill pill-green"><CheckCircle2 size={11} /> {mappedCount} Mapped</span>
          {unmappedCount > 0 && (
            <span className="pill pill-amber"><AlertTriangle size={11} /> {unmappedCount} Needs Review</span>
          )}
        </span>
      </button>
      {open && (
        <div className="px-5 pb-5 pt-2 border-t border-border space-y-2">
          {/* Sheet-level controls */}
          <div className="flex items-center gap-3 text-xs text-ink-muted pt-2">
            <span>Header row {sh.header_row + 1}</span>
            <span>·</span>
            <span>Row strategy:</span>
            <Select className="!py-1 !text-xs !w-auto" value={sh.row_strategy}
              onChange={e => onPatchSheet({ row_strategy: e.target.value })}>
              {STRATEGIES.map(s => <option key={s} value={s}>{s}</option>)}
            </Select>
          </div>

          {sh.columns.map((c, ci) => (
            <ColumnRow
              key={c.column_index}
              column={c}
              allFields={allFields}
              contractRules={fieldRules[c.column_name] ?? []}
              onPatch={(p) => onPatchColumn(ci, p)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

const SEV: Record<string, string> = {
  critical: "pill pill-red",
  warning:  "pill pill-amber",
  info:     "pill pill-blue",
};

function ColumnRow({ column: c, allFields, contractRules, onPatch }: {
  column: TplColumn;
  allFields: ModelField[];
  contractRules: ContractRule[];
  onPatch: (p: Partial<TplColumn>) => void;
}) {
  const [open, setOpen] = useState(!c.canonical_field);
  const [browse, setBrowse] = useState(false);
  const [q, setQ] = useState("");
  const isMapped = !!c.canonical_field;
  const candidates = c.candidates ?? [];
  const currentConf = isMapped
    ? (candidates.find(cd => cd.canonical === c.canonical_field)?.confidence
       ?? c.confidence
       ?? candidates[0]?.confidence
       ?? 0)
    : (candidates[0]?.confidence ?? 0);
  const hasRules = contractRules.length > 0;

  function apply(canonical: string | null) {
    onPatch({
      canonical_field: canonical,
      confidence: canonical
        ? (candidates.find(cd => cd.canonical === canonical)?.confidence ?? 1.0)
        : 0,
    });
  }

  const meta = c.canonical_field
    ? allFields.find(f => f.key === c.canonical_field)
    : null;

  return (
    <div className={`rounded-md border overflow-hidden
      ${!isMapped ? "border-amber-200" : hasRules ? "border-border" : "border-border"}
      bg-white`}>
      {/* ── Collapsed header row: 3 columns ── */}
      <button
        onClick={() => setOpen(!open)}
        className="w-full px-3.5 py-2.5 flex items-center gap-3 hover:bg-surface-2 text-left">
        <div className="w-5 flex-shrink-0">
          {isMapped
            ? <CheckCircle2 size={16} className="text-emerald-600" />
            : <AlertTriangle size={16} className="text-amber-600" />}
        </div>

        <div className="flex-1 min-w-0 grid grid-cols-3 gap-4 items-start">
          {/* Col 1 — Output Template field */}
          <div className="min-w-0">
            <div className="text-[11px] uppercase tracking-wide text-ink-soft">Output Template Column</div>
            <div className="font-medium truncate">{c.column_name}</div>
            {c.samples?.length > 0 && (
              <div className="text-[11px] text-ink-soft truncate">
                e.g. {c.samples.slice(0, 2).join(", ")}
              </div>
            )}
          </div>

          {/* Col 2 — Contract clause / rules */}
          <div className="min-w-0">
            <div className="text-[11px] uppercase tracking-wide text-ink-soft flex items-center gap-1">
              <FileText size={10} /> Contract Rules
            </div>
            {hasRules ? (
              <div className="space-y-0.5 mt-0.5">
                {contractRules.slice(0, 2).map(r => (
                  <div key={r.rule_id} className="flex items-center gap-1.5 min-w-0">
                    <span className={`${SEV[r.severity] ?? "pill pill-grey"} text-[10px] shrink-0`}>
                      {r.severity}
                    </span>
                    <span className="text-[12px] truncate text-ink">{r.rule_name}</span>
                  </div>
                ))}
                {contractRules.length > 2 && (
                  <div className="text-[11px] text-ink-muted">
                    +{contractRules.length - 2} more rule{contractRules.length - 2 !== 1 ? "s" : ""}
                  </div>
                )}
              </div>
            ) : (
              <div className="text-[12px] text-ink-soft mt-0.5">No contract rules</div>
            )}
          </div>

          {/* Col 3 — Data model field */}
          <div className="min-w-0">
            <div className="text-[11px] uppercase tracking-wide text-ink-soft">Canonical Model Field</div>
            {isMapped ? (
              <div className="font-mono text-sm truncate">{c.canonical_field}</div>
            ) : candidates[0] ? (
              <div className="font-mono text-sm text-ink-muted truncate">
                {candidates[0].canonical} <span className="text-ink-soft">(suggested)</span>
              </div>
            ) : (
              <div className="text-sm text-ink-soft">no suggestion</div>
            )}
            {meta && (
              <div className="text-[11px] text-ink-muted truncate">
                {meta.table}.{meta.column} · {meta.type}
              </div>
            )}
          </div>
        </div>

        <ConfidencePill value={currentConf} />
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>

      {open && (
        <div className="border-t border-border bg-surface-2/40 p-3.5 space-y-3">
          <div className="text-[11px] uppercase tracking-wide text-ink-muted font-medium">
            Top {candidates.length || 0} AI Candidates (Sorted by Similarity)
          </div>
          {candidates.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No AI candidates returned for this column. Use the search below.
            </p>
          ) : (
            <ul className="space-y-1">
              {candidates.map(cd => {
                const selected = cd.canonical === c.canonical_field;
                const fmeta = allFields.find(f => f.key === cd.canonical);
                return (
                  <li key={cd.canonical}>
                    <button
                      onClick={() => apply(selected ? null : cd.canonical)}
                      className={`w-full flex items-center gap-3 px-3 py-2 rounded-md
                        text-left transition border ${
                          selected
                            ? "border-navy bg-navy/5"
                            : "border-transparent bg-white hover:border-border"
                        }`}>
                      <div className={`w-4 h-4 rounded-full border-2 flex-shrink-0
                        ${selected ? "border-navy bg-navy" : "border-ink-soft"}`}>
                        {selected && <div className="w-full h-full rounded-full bg-white scale-[0.35]" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="font-mono text-[13px] truncate">{cd.canonical}</div>
                        <div className="text-[11px] text-ink-muted truncate">
                          {fmeta ? `${fmeta.table}.${fmeta.column} · ${fmeta.type}` : "—"}
                          {cd.reason && <> · {cd.reason}</>}
                        </div>
                      </div>
                      <ConfidenceBar value={cd.confidence} />
                      <ConfidencePill value={cd.confidence} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          {/* Contract rules for this field */}
          {hasRules && (
            <div className="pt-2 border-t border-border space-y-2">
              <div className="text-[11px] uppercase tracking-wide text-ink-muted font-medium flex items-center gap-1.5">
                <ShieldAlert size={12} /> Contract Conditions ({contractRules.length})
              </div>
              <div className="space-y-2">
                {contractRules.map(r => (
                  <div key={r.rule_id}
                    className="rounded-md border border-border bg-white p-3 space-y-1.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`${SEV[r.severity] ?? "pill pill-grey"} text-[11px]`}>
                        {r.severity}
                      </span>
                      <span className="font-medium text-sm">{r.rule_name}</span>
                      <span className="pill pill-grey text-[10px] ml-auto">{r.rule_engine.toUpperCase()}</span>
                    </div>

                    {r.rule_description && (
                      <p className="text-[12px] text-ink-muted leading-relaxed">
                        {r.rule_description}
                      </p>
                    )}

                    {r.source_clause && (
                      <div className="rounded bg-surface-2 px-2.5 py-1.5 flex items-start gap-1.5">
                        <Quote size={11} className="text-accent shrink-0 mt-0.5" />
                        <span className="text-[11px] text-ink-muted italic leading-relaxed">
                          "{r.source_clause}"
                        </span>
                      </div>
                    )}

                    {r.error_message && (
                      <div className="text-[11px] text-red-600">
                        ⚠ {r.error_message}
                      </div>
                    )}

                    {/* Rule spec summary (required / enum / pattern) */}
                    {r.rule_spec && typeof r.rule_spec === "object" && (
                      <div className="flex flex-wrap gap-2 pt-1">
                        {r.rule_spec.required && (
                          <span className="text-[10px] bg-red-50 text-red-700 border border-red-200 rounded px-1.5 py-0.5">
                            Required
                          </span>
                        )}
                        {Array.isArray(r.rule_spec.enum) && (
                          <span className="text-[10px] bg-blue-50 text-blue-700 border border-blue-200 rounded px-1.5 py-0.5">
                            Enum: {r.rule_spec.enum.slice(0, 4).join(", ")}
                            {r.rule_spec.enum.length > 4 && ` +${r.rule_spec.enum.length - 4}`}
                          </span>
                        )}
                        {r.rule_spec.minimum !== undefined && (
                          <span className="text-[10px] bg-purple-50 text-purple-700 border border-purple-200 rounded px-1.5 py-0.5">
                            Min: {r.rule_spec.minimum}
                          </span>
                        )}
                        {r.rule_spec.maximum !== undefined && (
                          <span className="text-[10px] bg-purple-50 text-purple-700 border border-purple-200 rounded px-1.5 py-0.5">
                            Max: {r.rule_spec.maximum}
                          </span>
                        )}
                        {r.rule_spec.pattern && (
                          <span className="text-[10px] bg-amber-50 text-amber-700 border border-amber-200 rounded px-1.5 py-0.5 font-mono">
                            Pattern: {r.rule_spec.pattern}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Search box for the FULL field catalogue (canonical + extras). */}
          <div className="pt-2 border-t border-border">
            <button onClick={() => setBrowse(!browse)}
              className="text-xs text-accent hover:underline inline-flex items-center gap-1">
              <Wand2 size={12} />
              {browse ? "Hide Search" : "Search The Full Data Model & Extras"}
            </button>
            {browse && (
              <DataModelPicker
                fields={allFields}
                currentKey={c.canonical_field ?? undefined}
                hideKeys={new Set(candidates.map(cd => cd.canonical))}
                q={q} setQ={setQ}
                onPick={(k) => { apply(k); setBrowse(false); setQ(""); }}
              />
            )}
          </div>

          {/* Transform / Static value / Remove */}
          <div className="grid grid-cols-3 gap-3 pt-2 border-t border-border">
            <div>
              <span className="label">Transform</span>
              <Select value={c.transform ?? ""}
                onChange={(e) => onPatch({ transform: e.target.value || null })}>
                {TRANSFORMS.map(t => <option key={t} value={t}>{t || "—"}</option>)}
              </Select>
            </div>
            <div className="col-span-2">
              <span className="label">Static value</span>
              <TextInput value={c.static_value ?? ""}
                onChange={(e) => onPatch({ static_value: e.target.value || null })}
                placeholder="Override with a fixed value (e.g. POL)" />
            </div>
          </div>

          {isMapped && (
            <div className="flex justify-end">
              <button onClick={() => apply(null)}
                className="text-xs text-ink-muted hover:text-danger">
                Remove Mapping
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DataModelPicker({ fields, currentKey, hideKeys, q, setQ, onPick }: {
  fields: ModelField[]; currentKey?: string;
  hideKeys: Set<string>;
  q: string; setQ: (s: string) => void;
  onPick: (key: string) => void;
}) {
  const ql = q.trim().toLowerCase();
  const list = ql
    ? fields.filter(f =>
        f.key.toLowerCase().includes(ql) ||
        f.column.toLowerCase().includes(ql) ||
        (f.description ?? "").toLowerCase().includes(ql))
    : fields.filter(f => !hideKeys.has(f.key));
  return (
    <div className="mt-2">
      <div className="relative">
        <Search size={13}
          className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-soft" />
        <input className="input !pl-7 !py-1.5 !text-[13px]"
          placeholder="Search Canonical Fields or Extras…"
          value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
      </div>
      <ul className="mt-2 max-h-56 overflow-auto divide-y divide-border rounded-md border border-border">
        {list.slice(0, 80).map(f => {
          const isCurrent = f.key === currentKey;
          return (
            <li key={f.key}>
              <button
                onClick={() => onPick(f.key)}
                className={`w-full text-left px-3 py-2 hover:bg-surface-2 flex items-start gap-2
                  ${isCurrent ? "bg-navy/5" : ""}`}>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[13px] truncate">{f.key}</span>
                    <span className="pill pill-grey text-[10px]">{f.group}</span>
                  </div>
                  <div className="text-[11px] text-ink-muted truncate">
                    {f.table}.{f.column} · {f.type}
                    {f.description && <> · {f.description.slice(0, 90)}</>}
                  </div>
                </div>
                {isCurrent && (
                  <CheckCircle2 size={14} className="text-navy mt-0.5 shrink-0" />
                )}
              </button>
            </li>
          );
        })}
        {list.length === 0 && (
          <li className="px-3 py-3 text-xs text-ink-muted">No fields match.</li>
        )}
      </ul>
      {list.length > 80 && (
        <p className="text-[11px] text-ink-soft mt-1">
          Showing first 80 — refine your search to narrow.
        </p>
      )}
    </div>
  );
}

function ConfidencePill({ value }: { value: number }) {
  const pct = Math.round((value ?? 0) * 100);
  const klass =
    value >= 0.75 ? "pill-green" :
    value >= 0.45 ? "pill-amber" :
    value > 0      ? "pill-red"   : "pill-grey";
  return <span className={`pill ${klass} font-mono`}>{pct || "—"}%</span>;
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round((value ?? 0) * 100);
  const color =
    value >= 0.75 ? "bg-emerald-500" :
    value >= 0.45 ? "bg-amber-500" :
                    "bg-red-400";
  return (
    <div className="w-20 h-1.5 rounded-full bg-gray-200 overflow-hidden flex-shrink-0">
      <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
    </div>
  );
}
