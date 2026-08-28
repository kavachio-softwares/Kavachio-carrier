import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, ArrowRight, CalendarDays, ChevronDown, ChevronRight,
  FileSpreadsheet, FileText, FileUp, Info, Pencil, ShieldCheck,
} from "lucide-react";
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
  assignmentFor, outputsForInput, seedFromColumnMapping, sheetFieldKey, errText,
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
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

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
          // Every sheet starts collapsed — same reasoning as the builder: a
          // setup can have many sheets, showing them all open at once makes
          // the page unusably long.
          setCollapsed(new Set(e.data.input_sheets));
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
  function toggle(sheet: string) {
    setCollapsed(prev => { const n = new Set(prev); n.has(sheet) ? n.delete(sheet) : n.add(sheet); return n; });
  }

  const setupName = pipeline
    ? [pipeline.carrier_name, pipeline.program_name].filter(Boolean).join(" — ")
      || pipeline.name
    : "";

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
            <Button onClick={() => nav(`/direct/setups/${id}/edit?back=${encodeURIComponent(`/direct/setups/${id}`)}`)}>
              <Pencil size={15} /> Edit
            </Button>
          </div>
        } />
      <PageBody>
        {activateMsg && <Banner kind="ok"><ShieldCheck size={15} /> {activateMsg}</Banner>}
        {activateErr && <Banner kind="error"><AlertTriangle size={15} /> {activateErr}</Banner>}
        <Banner kind="info">
          <Info size={15} /> This is a read-only view — changes can only be made from Bordereau Setup.
        </Banner>

        {err && <Banner kind="error"><AlertTriangle size={15} /> {err}</Banner>}

        {loading ? (
          <Card><div className="space-y-2">
            {Array.from({ length: 4 }, (_, i) => <Sk key={i} className="h-9 w-full" />)}
          </div></Card>
        ) : pipeline && (
          <>
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
                  <div className="text-[11px] uppercase text-ink-muted mb-1">Input Template</div>
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

              <MissingReferenceDocsNote refDocs={refDocs} className="mt-4"
                showSources={pipeline.contracts.length > 1} />

              {refDocs.provided.length > 0 && (
                <p className="mt-3 text-[12px] text-ink-muted">
                  Reference document{refDocs.provided.length > 1 ? "s" : ""} used:{" "}
                  <span className="text-ink">{refDocs.provided.join(", ")}</span>
                </p>
              )}

            </Card>

            {/* Submission Calendar — when this setup's bordereaux are due. Shown
                here for the same reason the edit page shows it: someone arriving
                from a deadline reminder should not have to scroll past hundreds
                of mapping rows to find the schedule. Read-only, like the rest of
                this page — the schedule is changed from Bordereau Setup, so a
                deadline only ever moves in one place. */}
            {pipeline.program_id != null && (
              <Card title={<span className="flex items-center gap-2">
                <CalendarDays size={16} className="text-navy" /> Submission Calendar</span>}>
                <ProgramCalendar
                  programId={pipeline.program_id}
                  programName={pipeline.program_name}
                  carrierName={pipeline.carrier_name}
                  collapsible readOnly defaultCollapsed
                  /* Schedule only — the period list is read on My Calendar and
                     Program Management, matching the edit page. */
                  showPeriods={false} />
              </Card>
            )}

            {/* What the contract asks for that this setup's bordereau doesn't
                provide. Sits between the overview and the contracts on purpose:
                it's read AFTER "what this setup is" and BEFORE the contract it
                was judged against. Self-loading from the stored check — reading
                it costs nothing, and a setup built before this check existed
                runs it once on its first visit. */}
            <MissingColumnsNote pipelineId={pipeline.id} refreshKey={noteKey} />

            {/* Contracts — same expand-to-detail (terms, rules, clause routing) the
                builder itself shows; a contract's own rules stay editable here even
                though the sheet/field mapping below is read-only. */}
            <Card title={<span className="flex items-center gap-2">
              <FileText size={16} className="text-navy" /> Contracts
              <span className="text-xs font-normal text-ink-muted">({pipeline.contracts.length})</span>
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
                                contractId={c.id} mga={mga} onChanged={reloadContractDetail} readOnly
                                canEditVariations={canEditVariations} />
                            : <div className="text-sm text-ink-muted">Could not load this contract.</div>}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>

            {!pipeline.input_format_id ? (
              <Banner kind="warn">
                <AlertTriangle size={15} /> This setup has no input template yet — open it in Bordereau
                Setup to finish mapping it.
              </Banner>
            ) : editor && editor.input_sheets.length === 0 ? (
              <Banner kind="warn"><AlertTriangle size={15} /> No input sheets found for this setup yet.</Banner>
            ) : editor && editor.input_sheets.map(inputSheet => {
              const outs = outputsForInput(editor.sheet_routing, inputSheet);
              const inputCols = editor.input_columns[inputSheet] ?? [];
              const isCollapsed = collapsed.has(inputSheet);
              return (
                <Card key={inputSheet}
                  title={<button type="button" onClick={() => toggle(inputSheet)}
                    className="flex items-center gap-2 text-left hover:text-navy transition">
                    {isCollapsed ? <ChevronRight size={16} className="shrink-0 text-ink-muted" />
                      : <ChevronDown size={16} className="shrink-0 text-ink-muted" />}
                    <FileSpreadsheet size={16} className="text-navy" /> {inputSheet}
                  </button>}>
                  {isCollapsed ? (
                    <p className="text-xs text-ink-muted">
                      {outs.length === 0
                        ? "Not mapped to any output sheet."
                        : <>Maps to <span className="font-medium text-ink">{outs.join(", ")}</span> ·{" "}
                            {inputCols.length} input column{inputCols.length === 1 ? "" : "s"}. Expand to view.</>}
                    </p>
                  ) : outs.length === 0 ? (
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
            })}
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
