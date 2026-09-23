import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, ArrowRight, CalendarDays, ChevronDown, ChevronRight,
  FileSpreadsheet, FileText, FileUp, Info, Pencil, ShieldCheck,
} from "lucide-react";
import { SetupTabs, SheetChips, useSetupTab, type SetupTab } from "../components/SetupTabs";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { fmtStamp } from "../utils/date";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Banner } from "../components/ui/Banner";
import { Modal } from "../components/ui/Modal";
import { Sk } from "../components/ui/Skeleton";
import { ContractInline, ContractStatusChip } from "../components/DirectMappingWidgets";
import { MissingColumnsNote } from "../components/MissingColumnsNote";
import {
  MissingReferenceDocsNote, useRefDocs, PipelineRefDocs,
} from "../components/MissingReferenceDocsNote";
import ProgramCalendar from "../components/ProgramCalendar";
import {
  assignmentFor, outputsForInput, seedFromColumnMapping, sheetFieldKey, errText, feedIndex,
  MappingRule, SheetRouting, Contract, ContractDetailT,
} from "../utils/directSetup";

type PipelineContract = { contract_id: number; sheet_key: string | null; filename: string | null };
type PipelineDetail = {
  id: number; name: string | null;
  carrier_party_id: number | null; carrier_name: string | null;
  program_id: number | null; program_name: string | null;
  input_format_id: number | null; input_format_name: string | null;
  output_template_id: number | null; output_template_name: string | null;
  status: "draft" | "active" | "superseded";
  has_supplement: boolean;
  contracts: PipelineContract[];
  // Absent on setups built before this was recorded — treat as [].
  reference_documents?: PipelineRefDocs[];
  created_at: string | null; modified_at: string | null;
};
type Clause = { rule_id: number; severity?: string; text?: string; page?: number; match?: string; score?: number };
type OutField = { sheet: string; field: string; clauses: Clause[] };
type EditorResp = {
  format_id: number; input_sheets: string[]; input_columns: Record<string, string[]>;
  output_sheets: string[]; sheet_routing: SheetRouting;
  column_mapping: Record<string, Record<string, MappingRule>>;
  fields: OutField[];
};

const STATUS_LABEL: Record<PipelineDetail["status"], string> = {
  active: "Active", draft: "Draft", superseded: "Superseded",
};
const STATUS_TONE: Record<PipelineDetail["status"], string> = {
  active: "bg-emerald-100 text-emerald-700",
  draft: "bg-amber-50 text-amber-700",
  superseded: "bg-surface-2 text-ink-muted",
};

// Read-only mirror of the Bordereau Setup mapping editor. Renders the SAME
// saved routing/column-mapping/clause data the builder does, but with no
// interactive controls — editing only ever happens from Bordereau Setup
// itself (the "Edit in Bordereau Setup" action below sends you there with
// this exact carrier + program + setup pre-selected).
export default function BordereauSetupDetail() {
  const { id } = useParams<{ id: string }>();
  const mga = currentMga();
  // Nothing on this page is editable, spellings included — the banner above says
  // so, and an "Add Variation" button sitting inside a read-only view is the one
  // control that contradicts it. Adding a spelling is done from Edit, where the
  // same widget enables the button for an admin. (The server enforces the role
  // either way; this is only about what the page offers.)
  const canEditVariations = false;
  const nav = useNavigate();
  const [pipeline, setPipeline] = useState<PipelineDetail | null>(null);
  const [editor, setEditor] = useState<EditorResp | null>(null);
  const [contracts, setContracts] = useState<Contract[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  // Field mapping shows one input sheet at a time; this is the one picked.
  const [activeSheet, setActiveSheet] = useState("");

  // contract expand-to-detail (rules/terms/clause routing) — a contract's own
  // rules stay editable here even though the sheet/field mapping is read-only,
  // the same as the standalone Contract page.
  const [openContractId, setOpenContractId] = useState<number | null>(null);
  const [contractDetail, setContractDetail] = useState<ContractDetailT | null>(null);
  const [contractBusy, setContractBusy] = useState(false);
  // Bumped whenever a rule's mapping changes, to re-read the missing-columns note.
  const [noteKey, setNoteKey] = useState(0);
  // Activation straight from this read-only view: a setup that only needs to be
  // switched on shouldn't have to be opened in the editor and re-saved.
  const [confirmActivate, setConfirmActivate] = useState(false);
  const [activating, setActivating] = useState(false);
  const [activateErr, setActivateErr] = useState<string | null>(null);
  const [activateMsg, setActivateMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setLoading(true); setErr(null); setPipeline(null); setEditor(null); setContracts([]);
    api.get<PipelineDetail>(`/pipelines/${id}`)
      .then(async r => {
        setPipeline(r.data);
        if (r.data.input_format_id) {
          const e = await api.get<EditorResp>(`/direct/format/${r.data.input_format_id}/editor`);
          setEditor(e.data);
          // One sheet at a time (chips pick it) — a setup can have many
          // sheets, and all of them open at once made the page unusably long.
          setActiveSheet(e.data.input_sheets[0] ?? "");
        }
        if (r.data.program_id != null) {
          const cs = await api.get<Contract[]>(`/programs/${r.data.program_id}/contracts`);
          setContracts(Array.isArray(cs.data) ? cs.data : []);
        }
      })
      .catch(() => setErr("This setup could not be found, or you don't have access to it."))
      .finally(() => setLoading(false));
  }, [id]);

  async function toggleContract(cid: number) {
    if (openContractId === cid) { setOpenContractId(null); setContractDetail(null); return; }
    setOpenContractId(cid); setContractDetail(null); setContractBusy(true);
    try {
      const { data } = await api.get<ContractDetailT>(`/programs/${pipeline?.program_id}/contracts/${cid}`);
      setContractDetail(data);
    } catch { setContractDetail(null); } finally { setContractBusy(false); }
  }
  async function reloadContractDetail() {
    if (openContractId == null || !pipeline?.program_id) return;
    try {
      const { data } = await api.get<ContractDetailT>(`/programs/${pipeline.program_id}/contracts/${openContractId}`);
      setContractDetail(data);
    } catch { /* keep current view on failure */ }
    // A rule that just gained an output field is no longer a gap — re-read the
    // missing-columns note so it drops that entry without a page reload.
    setNoteKey(k => k + 1);
  }

  const { sel, extra } = useMemo(() => seedFromColumnMapping(editor?.column_mapping), [editor]);
  // BDX output column → the broker column(s) feeding it, for Contracts & rules.
  const feedFor = useMemo(() => feedIndex(sel, extra), [sel, extra]);

  // Output columns with no source — same calculation the editor warns about
  // before activating, so activating from here can't skip that check.
  const unsourcedFields = useMemo(() => {
    const used = new Set<string>();
    for (const [k, v] of Object.entries(sel)) used.add(`${k.split("||")[0]}::${v}`);
    return (editor?.fields ?? []).filter(
      f => !used.has(`${f.sheet}::${f.field}`) && !extra[sheetFieldKey(f.sheet, f.field)]);
  }, [editor, sel, extra]);

  async function activateSetup() {
    if (!pipeline) return;
    setActivating(true); setActivateErr(null); setActivateMsg(null);
    try {
      await api.post(`/pipelines/${pipeline.id}/activate`);
      // Re-read rather than patching status locally: activating supersedes the
      // previously active setup, so other fields (and modified_at) change too.
      const { data } = await api.get<PipelineDetail>(`/pipelines/${pipeline.id}`);
      setPipeline(data);
      setConfirmActivate(false);
      setActivateMsg("Setup activated — your team can now process bordereaux for this "
                     + "carrier and program. Any previously active setup was superseded.");
    } catch (e: unknown) {
      setActivateErr(errText(e));
    } finally { setActivating(false); }
  }

  // Same de-dup-by-signature as the builder — a clause can otherwise repeat
  // when more than one matched source text produced the identical rule.
  const clauseByField = useMemo(() => {
    const m: Record<string, Clause[]> = {};
    for (const f of editor?.fields ?? []) {
      const seen = new Set<string>();
      const unique: Clause[] = [];
      for (const cl of f.clauses ?? []) {
        const sig = `${cl.match}::${(cl.text ?? "").trim()}::${cl.severity ?? ""}`;
        if (seen.has(sig)) continue;
        seen.add(sig);
        unique.push(cl);
      }
      m[sheetFieldKey(f.sheet, f.field)] = unique;
    }
    return m;
  }, [editor]);

  // DISTINCT contract rules (by rule_id), not clause attachments — one rule can
  // attach to several fields, so summing per-field clause counts overstates it.
  const ruleCount = (() => {
    const ids = new Set<number>();
    for (const f of editor?.fields ?? []) for (const c of (f.clauses ?? [])) ids.add(c.rule_id);
    return ids.size;
  })();
  const fieldsWithClauses = (editor?.fields ?? []).filter(f => (f.clauses?.length ?? 0) > 0).length;

  const refDocs = useRefDocs(pipeline?.reference_documents);

  function outFieldsFor(sheet: string): string[] {
    return (editor?.fields ?? []).filter(f => f.sheet === sheet).map(f => f.field);
  }
  // Output columns still unfilled across the output sheets one input sheet
  // feeds — the amber count on its chip.
  function unsourcedFor(inputSheet: string): number {
    if (!editor) return 0;
    return outputsForInput(editor.sheet_routing, inputSheet).reduce((n, outSheet) => {
      const used = new Set(
        Object.entries(sel).filter(([k]) => k.startsWith(`${outSheet}||`)).map(([, v]) => v));
      return n + outFieldsFor(outSheet).filter(f => !used.has(f)).length;
    }, 0);
  }

  const setupName = pipeline
    ? [pipeline.carrier_name, pipeline.program_name].filter(Boolean).join(" — ")
      || pipeline.name
    : "";

  // ── the tabs ── one per section of what used to be one long page.
  const tabs: SetupTab[] = [
    { key: "overview", label: "Overview" },
    { key: "mapping", label: "Field mapping",
      ...(unsourcedFields.length ? { count: unsourcedFields.length, warn: true } : {}) },
    { key: "contracts", label: "Contracts & rules", count: ruleCount },
    { key: "attention", label: "Needs attention",
      ...(refDocs.missing.length ? { count: refDocs.missing.length, warn: true } : {}) },
    ...(pipeline?.program_id != null
      ? [{ key: "calendar" as const, label: "Submission calendar" }] : []),
  ];
  const [tab, setTab] = useSetupTab(tabs);
  // A setup with a single contract opens it on Contracts & rules — there is
  // nothing to choose between, so the rules are the first thing shown.
  useEffect(() => {
    if (tab === "contracts" && pipeline?.contracts.length === 1 && openContractId == null
        && contracts.some(c => c.id === pipeline.contracts[0].contract_id))
      toggleContract(pipeline.contracts[0].contract_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, pipeline, contracts]);
  // Edit has no "Needs attention" tab — its fixes are made on Field mapping.
  const editTab = tab === "attention" ? "mapping" : tab;

  return (
    <>
      <PageHeader title={setupName || "Bordereau Setup"}
        subtitle={pipeline
          ? `${STATUS_LABEL[pipeline.status]} · Last Modified ${fmtStamp(pipeline.modified_at)}`
          : "Read-only view of a saved setup."}
        action={
          <div className="flex items-center gap-2">
            <Button variant="secondary" onClick={() => nav("/direct/setups")}>
              <ArrowLeft size={15} /> Configured Bordereau Setups
            </Button>
            {/* Only a setup that ISN'T already live can be switched on — an
                active one has nothing to activate. */}
            {pipeline && pipeline.status !== "active" && (
              <Button variant="secondary" disabled={activating}
                onClick={() => { setActivateErr(null); setConfirmActivate(true); }}>
                <ShieldCheck size={15} /> Activate Setup
              </Button>
            )}
            <Button onClick={() => nav(`/direct/setups/${id}/edit?back=${encodeURIComponent(`/direct/setups/${id}`)}&tab=${editTab}`)}>
              <Pencil size={15} /> Edit
            </Button>
          </div>
        } />
      <PageBody>
        {activateMsg && <Banner kind="ok"><ShieldCheck size={15} /> {activateMsg}</Banner>}
        {activateErr && <Banner kind="error"><AlertTriangle size={15} /> {activateErr}</Banner>}
        {err && <Banner kind="error"><AlertTriangle size={15} /> {err}</Banner>}

        {loading ? (
          <Card><div className="space-y-2">
            {Array.from({ length: 4 }, (_, i) => <Sk key={i} className="h-9 w-full" />)}
          </div></Card>
        ) : pipeline && (
          <>
            <SetupTabs tabs={tabs} current={tab} onChange={setTab} />

            {tab === "overview" && (
              <>
                <p className="mb-3 flex items-center gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-ink-muted">
                  <Info size={14} className="shrink-0" /> Read-only. Press Edit to change anything.
                </p>
                <Card title={<span className="flex items-center gap-2">
                  <ShieldCheck size={16} className="text-navy" /> Setup Overview</span>}>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Carrier</div>
                      <div className="font-medium">{pipeline.carrier_name ?? "—"}</div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Program</div>
                      <div className="font-medium">{pipeline.program_name ?? "—"}</div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Status</div>
                      <span className={`inline-flex text-[11px] rounded-full px-2 py-0.5 font-medium ${STATUS_TONE[pipeline.status]}`}>
                        {STATUS_LABEL[pipeline.status]}
                      </span>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Created</div>
                      <div className="font-medium">{fmtStamp(pipeline.created_at)}</div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1 b">Input Template</div>
                      <div className="font-medium flex items-center gap-1.5">
                        <FileUp size={13} className="text-ink-soft shrink-0" />
                        <span className="truncate">{pipeline.input_format_name ?? "— None —"}</span>
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Output Template</div>
                      <div className="font-medium flex items-center gap-1.5">
                        <FileSpreadsheet size={13} className="text-ink-soft shrink-0" />
                        <span className="truncate">{pipeline.output_template_name ?? "— None —"}</span>
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Contract Rules</div>
                      <div className="font-medium">{ruleCount} Rule(s) Across {fieldsWithClauses} Field(s)</div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Supplementary Data</div>
                      <div className="font-medium">{pipeline.has_supplement ? "Attached" : "None"}</div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase text-ink-muted mb-1">Reference Documents</div>
                      <div className="font-medium">
                        {refDocs.missing.length > 0
                          ? <span className="text-amber-700">{refDocs.missing.length} Not Provided</span>
                          : refDocs.provided.length > 0
                            ? `${refDocs.provided.length} Attached`
                            : "None"}
                      </div>
                    </div>
                  </div>
                  {refDocs.provided.length > 0 && (
                    <p className="mt-3 text-[12px] text-ink-muted">
                      Reference document{refDocs.provided.length > 1 ? "s" : ""} used:{" "}
                      <span className="text-ink">{refDocs.provided.join(", ")}</span>
                    </p>
                  )}
                </Card>

                {/* Where to go next — each tile opens the tab it counts. */}
                <div className="mt-4 grid gap-3 sm:grid-cols-3">
                  <JumpTile n={fieldsWithClauses} label="fields with a contract rule"
                    onClick={() => setTab("mapping")} />
                  <JumpTile n={ruleCount} label={`rules from ${pipeline.contracts.length} contract${pipeline.contracts.length === 1 ? "" : "s"}`}
                    onClick={() => setTab("contracts")} />
                  <JumpTile n={unsourcedFields.length + refDocs.missing.length}
                    label="things to look at" warn={unsourcedFields.length + refDocs.missing.length > 0}
                    onClick={() => setTab(refDocs.missing.length ? "attention" : "mapping")} />
                </div>
              </>
            )}

            {tab === "mapping" && (
              !pipeline.input_format_id ? (
                <Banner kind="warn">
                  <AlertTriangle size={15} /> This setup has no input template yet — open it in Bordereau
                  Setup to finish mapping it.
                </Banner>
              ) : editor && editor.input_sheets.length === 0 ? (
                <Banner kind="warn"><AlertTriangle size={15} /> No input sheets found for this setup yet.</Banner>
              ) : editor && (() => {
                const inputSheet = editor.input_sheets.includes(activeSheet)
                  ? activeSheet : editor.input_sheets[0];
                const outs = outputsForInput(editor.sheet_routing, inputSheet);
                const inputCols = editor.input_columns[inputSheet] ?? [];
                return (
                  <Card title={<span className="flex items-center gap-2">
                    <FileSpreadsheet size={16} className="text-navy" /> {inputSheet}</span>}
                    action={<span className="text-xs text-ink-muted">
                      {outs.length ? <>Maps to <span className="font-medium text-ink">{outs.join(", ")}</span></> : null}
                    </span>}>
                    <SheetChips sheets={editor.input_sheets} current={inputSheet}
                      onPick={setActiveSheet} unsourced={unsourcedFor} />
                    {outs.length === 0 ? (
                      <p className="text-sm text-ink-muted">This input sheet isn't mapped to any output sheet.</p>
                    ) : (
                      <div className="overflow-x-auto">
                        <div className="min-w-[640px] rounded-lg border border-border overflow-hidden">
                          <div className="grid grid-cols-[1.1fr_26px_1.2fr_1.4fr] gap-3.5 px-4 py-2 bg-surface-2 text-[10px] font-bold uppercase tracking-wide text-ink-muted">
                            <div>Input Column</div><div /><div>Output Column</div><div>Contract Clause &amp; Rule</div>
                          </div>
                          {inputCols.map(col => {
                            const assignment = assignmentFor(sel, outs, col);
                            const clauses = assignment
                              ? clauseByField[sheetFieldKey(assignment.sheet, assignment.field)] : undefined;
                            return (
                              <div key={col}
                                className="grid grid-cols-[1.1fr_26px_1.2fr_1.4fr] gap-3.5 items-start px-4 py-3 border-b border-border last:border-b-0">
                                <div className="pt-1.5 font-mono text-xs text-ink">{col}</div>
                                <div className="pt-1 text-center text-ink-soft"><ArrowRight size={13} className="inline" /></div>
                                <div className="pt-1 text-sm">
                                  {assignment ? (
                                    <>
                                      {assignment.field}
                                      {outs.length > 1 && (
                                        <div className="text-[11px] text-ink-muted mt-0.5">On {assignment.sheet}</div>
                                      )}
                                    </>
                                  ) : <span className="text-ink-soft">— Not Mapped —</span>}
                                </div>
                                <div className="flex flex-col gap-1.5">
                                  {clauses && clauses.length > 0
                                    ? clauses.map(cl => (
                                        <div key={cl.rule_id}
                                          className="rounded-md border border-navy/15 bg-navy/[0.04] px-2.5 py-2 text-[11.5px] leading-snug text-ink">
                                          {cl.text}
                                        </div>))
                                    : assignment
                                      ? <span className="text-xs text-ink-soft pt-1.5">No contract rule for this field.</span>
                                      : <span className="text-xs text-ink-soft/70 pt-1.5">—</span>}
                                </div>
                              </div>
                            );
                          })}
                        </div>

                        {outs.map(outSheet => {
                          const used = new Set(
                            Object.entries(sel).filter(([k]) => k.startsWith(`${outSheet}||`)).map(([, v]) => v));
                          const unmapped = outFieldsFor(outSheet).filter(f => !used.has(f));
                          if (unmapped.length === 0) return null;
                          return (
                            <div key={outSheet} className="mt-4 rounded-lg border border-amber-200 bg-amber-50/60 overflow-hidden">
                              <div className="flex items-center gap-1.5 text-sm font-medium px-4 py-2.5 border-b border-amber-200 text-amber-800">
                                <AlertTriangle size={14} /> Not Sourced From This Sheet
                                {outs.length > 1 && <span className="font-mono text-xs">· {outSheet}</span>}
                              </div>
                              <div className="divide-y divide-amber-200/70">
                                {unmapped.map(f => {
                                  const rule = extra[sheetFieldKey(outSheet, f)];
                                  return (
                                    <div key={f} className="flex items-center gap-2 text-sm px-4 py-2.5">
                                      <span className="w-56 truncate font-medium">{f}</span>
                                      <span className="text-ink-muted text-xs">
                                        {rule?.kind === "const" ? `Constant: ${rule.value || "(Blank)"}`
                                          : rule?.kind === "source_sheet" ? "Source Tab Name"
                                          : "Left Blank"}
                                      </span>
                                    </div>
                                  );
                                })}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </Card>
                );
              })()
            )}

            {tab === "contracts" && (
              /* Same expand-to-detail (terms, rules, clause routing) the builder
                 itself shows; a contract's own rules stay editable here even
                 though the sheet/field mapping is read-only. */
              <Card title={<span className="flex items-center gap-2">
                <FileText size={16} className="text-navy" /> Contracts
                <span className="text-xs font-normal text-ink-muted">({pipeline.contracts.length})</span>
                <span className="text-xs font-normal text-ink-muted">· {ruleCount} rule(s) across {fieldsWithClauses} field(s)</span>
              </span>}>
              <div className="space-y-2">
                {pipeline.contracts.length === 0 && (
                  <div className="text-sm text-ink-muted">No contracts attached.</div>
                )}
                {pipeline.contracts.map(pc => {
                  const c = contracts.find(x => x.id === pc.contract_id);
                  if (!c) {
                    return (
                      <div key={pc.contract_id} className="rounded-lg border border-border bg-white px-4 py-2.5 flex items-center gap-2 text-sm text-ink-muted">
                        <FileText size={14} /> Contract #{pc.contract_id}
                        <span className="ml-auto text-[11px]">Not In This Program</span>
                      </div>
                    );
                  }
                  const open = openContractId === c.id;
                  return (
                    <div key={c.id} className={`rounded-lg border ${c.status === "active"
                      ? "border-emerald-300 bg-emerald-50/40" : "border-border bg-white"}`}>
                      <button onClick={() => toggleContract(c.id)} className="w-full flex items-center gap-2 px-4 py-2.5 text-left">
                        {open ? <ChevronDown size={14} className="shrink-0" /> : <ChevronRight size={14} className="shrink-0" />}
                        <FileText size={14} className="text-ink-muted shrink-0" />
                        <span className="font-medium text-sm truncate">{c.filename || `Contract #${c.id}`}</span>
                        <ContractStatusChip status={c.status} />
                        <span className="text-[11px] rounded-full px-2 py-0.5 bg-navy/10 text-navy font-medium shrink-0">
                          {pc.sheet_key ? `→ ${pc.sheet_key}` : "Default"}
                        </span>
                        <span className="ml-auto text-[11px] text-ink-muted shrink-0">{fmtStamp(c.created_at)}</span>
                      </button>
                      {open && (
                        <div className="border-t border-border px-4 py-3">
                          {contractBusy ? null : contractDetail
                            ? <ContractInline detail={contractDetail} programId={pipeline.program_id ?? ""}
                                contractId={c.id} mga={mga} onChanged={reloadContractDetail} readOnly feedFor={feedFor}
                                canEditVariations={canEditVariations} />
                            : <div className="text-sm text-ink-muted">Could not load this contract.</div>}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              </Card>
            )}

            {tab === "attention" && (
              <>
                {/* What the contract asks for that this setup cannot check yet:
                    documents it defers to that were never supplied, and clauses
                    with no column to check. Each is fixed from Edit. */}
                <MissingReferenceDocsNote refDocs={refDocs} className="mb-4"
                  showSources={pipeline.contracts.length > 1} />
                <MissingColumnsNote pipelineId={pipeline.id} refreshKey={noteKey} />
                <div className="mt-4 flex justify-end">
                  <Button variant="secondary"
                    onClick={() => nav(`/direct/setups/${id}/edit?back=${encodeURIComponent(`/direct/setups/${id}`)}&tab=mapping`)}>
                    <Pencil size={14} /> Fix these in Edit
                  </Button>
                </div>
              </>
            )}

            {tab === "calendar" && pipeline.program_id != null && (
              /* When this setup's bordereaux are due. Read-only, like the rest of
                 this page — the schedule is changed from Edit, so a deadline only
                 ever moves in one place. */
              <Card title={<span className="flex items-center gap-2">
                <CalendarDays size={16} className="text-navy" /> Submission Calendar</span>}>
                <ProgramCalendar
                  programId={pipeline.program_id}
                  programName={pipeline.program_name}
                  carrierName={pipeline.carrier_name}
                  readOnly
                  /* Schedule only — the period list is read on My Calendar and
                     Program Management, matching the edit page. */
                  showPeriods={false} />
              </Card>
            )}
          </>
        )}
      </PageBody>

      {/* Activation is not a silent toggle: it makes this setup the one every
          bordereau run uses and supersedes whichever setup was active before,
          so it's confirmed explicitly — and warns about unsourced columns the
          same way the editor does. */}
      <Modal open={confirmActivate} title="Activate this setup?" size="md"
        onClose={() => { if (!activating) setConfirmActivate(false); }}
        footer={
          <div className="flex items-center justify-end gap-2">
            <Button variant="secondary" disabled={activating}
              onClick={() => setConfirmActivate(false)}>Cancel</Button>
            <Button disabled={activating} onClick={activateSetup}>
              {activating ? "Activating…" : "Activate setup"}
            </Button>
          </div>
        }>
        <div className="text-sm text-ink-muted space-y-3">
          <p>
            <strong className="text-ink">{setupName || "This setup"}</strong> will become the
            setup used for every bordereau processed for this carrier and program.
            Any setup that is currently active will be superseded.
          </p>
          {unsourcedFields.length > 0 && (
            <Banner kind="warn">
              <AlertTriangle size={15} />
              <span>
                {unsourcedFields.length} output column{unsourcedFields.length === 1 ? "" : "s"} still
                {unsourcedFields.length === 1 ? " has" : " have"} no source
                ({unsourcedFields.slice(0, 5).map(f => f.field).join(", ")}
                {unsourcedFields.length > 5 ? `, and ${unsourcedFields.length - 5} more` : ""}).
                {" "}They will be blank in the output until they are mapped in Edit.
              </span>
            </Banner>
          )}
        </div>
      </Modal>
    </>
  );
}

/** A count on the Overview that opens the tab it counts. */
function JumpTile({ n, label, warn, onClick }: {
  n: number; label: ReactNode; warn?: boolean; onClick: () => void;
}) {
  return (
    <button type="button" onClick={onClick}
      className="grid gap-0.5 rounded-lg border border-border bg-white px-4 py-3 text-left transition-colors hover:border-navy
                 focus-visible:outline focus-visible:outline-2 focus-visible:outline-navy">
      <span className={`text-xl font-bold tabular-nums ${warn ? "text-amber-600" : "text-ink"}`}>{n}</span>
      <span className="text-xs text-ink-muted">{label} →</span>
    </button>
  );
}
