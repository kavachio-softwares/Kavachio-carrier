import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, ArrowRight, CalendarDays, CheckCircle2, ChevronDown,
  ChevronRight, FileSpreadsheet, FileText, Save, Trash2,
} from "lucide-react";
import { api } from "../api/client";
import { currentMga, isTenantAdmin } from "../auth";
import { fmtStamp } from "../utils/date";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Banner } from "../components/ui/Banner";
import { Select, TextInput } from "../components/ui/Field";
import { Sk } from "../components/ui/Skeleton";
import {
  ContractInline, ContractStatusChip, ScoreChip, SevBadge, ClauseMatchChip, Combo, TagPill,
} from "../components/DirectMappingWidgets";
import { ClauseText } from "../components/ClauseText";
import { MissingColumnsNote } from "../components/MissingColumnsNote";
import {
  MissingReferenceDocsNote, useRefDocs, PipelineRefDocs,
} from "../components/MissingReferenceDocsNote";
import ProgramCalendar from "../components/ProgramCalendar";
import {
  assignmentFor, outputsForInput, seedFromColumnMapping, sheetFieldKey, errText,
  MappingRule, SheetRouting, OutField, Contract, ContractDetailT,
} from "../utils/directSetup";

type PipelineDetail = {
  id: number; name: string | null; status: "draft" | "active" | "superseded";
  carrier_party_id: number | null; carrier_name: string | null;
  program_id: number | null; program_name: string | null;
  input_format_id: number | null; input_format_name: string | null;
  output_template_id: number | null; output_template_name: string | null;
  contracts: { contract_id: number; sheet_key: string | null; filename: string | null }[];
  // Absent on setups built before this was recorded — treat as [].
  reference_documents?: PipelineRefDocs[];
  created_at: string | null; modified_at: string | null;
};
type EditorResp = {
  format_id: number; input_sheets: string[]; input_columns: Record<string, string[]>;
  output_sheets: string[]; sheet_routing: SheetRouting;
  column_mapping: Record<string, Record<string, MappingRule>>;
  candidates: Record<string, Record<string, { source: string; confidence: number }[]>>;
  fields: OutField[];
};

const STATUS_LABEL: Record<PipelineDetail["status"], string> = {
  active: "Active", draft: "Draft", superseded: "Superseded",
};

// The editable surface for ONE existing setup — sheet↔contract binding, field
// mapping + contract clauses, and the contracts' own extracted terms/rules.
// Reached from Bordereau Setup's "See" action (after a build) and from the
// read-only setup view's "Edit in Bordereau Setup" action. Editing a setup
// only ever happens here — Bordereau Setup itself is upload-and-build only.
export default function BordereauSetupEdit() {
  const { id } = useParams<{ id: string }>();
  const mga = currentMga();
  const isAdmin = isTenantAdmin();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // Where the header's back button goes — the page that opened this editor
  // (Bordereau Setup, or a specific setup's detail view). Defaults to the All
  // Setups list when opened cold (direct link / refresh).
  const backTo = searchParams.get("back") || "/direct/setups";
  const backLabel = backTo === "/direct/setup" ? "Bordereau Setup"
    : backTo.startsWith("/direct/setups/") ? "Setup Details"
    : "Bordereau Setups";

  const [pipeline, setPipeline] = useState<PipelineDetail | null>(null);
  const [editor, setEditor] = useState<EditorResp | null>(null);
  const [contracts, setContracts] = useState<Contract[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  // editable mapping state, seeded from the loaded editor response
  const [sel, setSel] = useState<Record<string, string>>({});
  const [extra, setExtra] = useState<Record<string, MappingRule>>({});
  const [rowSheet, setRowSheet] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState<{ sheet: string; col: string; field: string; other: string } | null>(null);
  const [collapsedSheets, setCollapsedSheets] = useState<Set<string>>(new Set());

  // per-schedule contract bindings + the setup's fallback/default contract
  const [sheetContracts, setSheetContracts] = useState<Record<string, number | "">>({});
  const [contractId, setContractId] = useState<number | null>(null);
  const [savingSC, setSavingSC] = useState(false);
  const [scSaved, setScSaved] = useState(false);

  // contract expand-to-detail
  const [openContractId, setOpenContractId] = useState<number | null>(null);
  const [contractDetail, setContractDetail] = useState<ContractDetailT | null>(null);
  const [contractBusy, setContractBusy] = useState(false);
  // Bumped whenever a rule's mapping changes, to re-read the missing-columns note.
  const [noteKey, setNoteKey] = useState(0);

  async function load() {
    if (!id) return;
    setLoading(true); setNotFound(false); setErr(null);
    try {
      const { data: p } = await api.get<PipelineDetail>(`/pipelines/${id}`);
      setPipeline(p);
      setContractId(p.contracts.find(c => c.sheet_key == null)?.contract_id ?? null);
      if (p.input_format_id) {
        const [{ data: ed }, { data: fmt }] = await Promise.all([
          api.get<EditorResp>(`/direct/format/${p.input_format_id}/editor`),
          api.get(`/direct/format/${p.input_format_id}`),
        ]);
        setEditor(ed);
        setCollapsedSheets(new Set(ed.input_sheets));
        const { sel: seededSel, extra: seededExtra } = seedFromColumnMapping(ed.column_mapping);
        setSel(seededSel); setExtra(seededExtra); setRowSheet({});
        setSheetContracts((fmt?.sheet_contracts as Record<string, number>) || {});
      }
      if (p.program_id != null) {
        const { data: cs } = await api.get<Contract[]>(`/programs/${p.program_id}/contracts`);
        setContracts(Array.isArray(cs) ? cs : []);
      }
    } catch {
      setNotFound(true);
    } finally { setLoading(false); }
  }
  useEffect(() => { load(); }, [id]);

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
    // The rule just edited may belong to the field-mapping editor's clauses too.
    if (editor && pipeline?.input_format_id) {
      try {
        const of = await api.get<{ fields: OutField[] }>(`/direct/output-fields`, {
          params: { template_id: pipeline.output_template_id ?? undefined,
                    contract_id: contractId ?? undefined, format_id: pipeline.input_format_id },
        });
        setEditor(prev => prev && { ...prev, fields: of.data.fields });
      } catch { /* leave clauses as-is on failure */ }
    }
  }

  const clauseByField = useMemo(() => {
    const m: Record<string, OutField["clauses"]> = {};
    for (const f of editor?.fields ?? []) {
      const seen = new Set<string>();
      const unique: OutField["clauses"] = [];
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
  // attach to several output fields, so summing per-field clause counts overstates
  // it (e.g. a 17-rule contract showing as 36). Matches the count the build modal shows.
  const ruleCount = (() => {
    const ids = new Set<number>();
    for (const f of editor?.fields ?? []) for (const c of (f.clauses ?? [])) ids.add(c.rule_id);
    return ids.size;
  })();
  const fieldsWithClauses = (editor?.fields ?? []).filter(f => (f.clauses?.length ?? 0) > 0).length;

  // Reference documents the contract defers rules to — the same summary the
  // read-only setup view shows, so a document that was never attached is
  // visible from the screen where the setup is actually being fixed.
  const refDocs = useRefDocs(pipeline?.reference_documents);

  function outFieldsFor(outSheet: string): string[] {
    return (editor?.fields ?? []).filter(f => f.sheet === outSheet).map(f => f.field);
  }
  function scoreFor(sheet: string, col: string, field: string): number | null {
    const list = editor?.candidates?.[sheet]?.[field];
    const hit = list?.find(c => c.source === col);
    return hit ? hit.confidence : null;
  }
  const dupFields = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const [k, v] of Object.entries(sel)) {
      const sheet = k.split("||")[0];
      counts[`${sheet}::${v}`] = (counts[`${sheet}::${v}`] || 0) + 1;
    }
    return new Set(Object.entries(counts).filter(([, n]) => n > 1).map(([k]) => k));
  }, [sel]);
  function unmappedOutputs(outSheet: string): string[] {
    const used = new Set(Object.entries(sel).filter(([k]) => k.startsWith(`${outSheet}||`)).map(([, v]) => v));
    return outFieldsFor(outSheet).filter(f => !used.has(f) && !extra[sheetFieldKey(outSheet, f)]);
  }
  const unsourcedFields = useMemo(() => {
    const used = new Set<string>();
    for (const [k, v] of Object.entries(sel)) used.add(`${k.split("||")[0]}::${v}`);
    return (editor?.fields ?? []).filter(f => !used.has(`${f.sheet}::${f.field}`) && !extra[sheetFieldKey(f.sheet, f.field)]);
  }, [editor, sel, extra]);
  const unassignedOutputs = useMemo(() => {
    if (!editor) return [];
    const fed = new Set((editor.sheet_routing?.routes ?? [])
      .filter(r => r.sources.length > 0).map(r => r.output_sheet));
    return editor.output_sheets.filter(o => !fed.has(o));
  }, [editor]);

  function chooseOutput(sheet: string, col: string, field: string | null) {
    if (!field) { setSel(p => { const n = { ...p }; delete n[sheetFieldKey(sheet, col)]; return n; }); return; }
    const key = sheetFieldKey(sheet, col);
    const otherKey = Object.entries(sel).find(([k, v]) => k !== key && k.startsWith(`${sheet}||`) && v === field);
    if (otherKey) { setConflict({ sheet, col, field, other: otherKey[0].split("||")[1] }); return; }
    setSel(p => ({ ...p, [key]: field }));
  }
  function resolveConflict(mode: "move" | "both" | "cancel") {
    if (!conflict) return;
    const { sheet, col, field, other } = conflict;
    if (mode !== "cancel") {
      setSel(p => {
        const n = { ...p };
        if (mode === "move") delete n[sheetFieldKey(sheet, other)];
        n[sheetFieldKey(sheet, col)] = field;
        return n;
      });
    }
    setConflict(null);
  }
  function assignField(mappedOuts: string[], col: string, outputSheet: string, field: string | null) {
    setSel(p => {
      const n = { ...p };
      for (const s of mappedOuts) if (s !== outputSheet) delete n[sheetFieldKey(s, col)];
      return n;
    });
    chooseOutput(outputSheet, col, field);
  }
  function rowOutputSheet(inputSheet: string, mappedOuts: string[], col: string): string {
    const explicit = rowSheet[sheetFieldKey(inputSheet, col)];
    if (explicit && mappedOuts.includes(explicit)) return explicit;
    return assignmentFor(sel, mappedOuts, col)?.sheet ?? mappedOuts[0] ?? "";
  }
  function setRowOutputSheet(inputSheet: string, mappedOuts: string[], col: string, newSheet: string) {
    setRowSheet(p => ({ ...p, [sheetFieldKey(inputSheet, col)]: newSheet }));
    setSel(p => {
      const n = { ...p };
      for (const s of mappedOuts) if (s !== newSheet) delete n[sheetFieldKey(s, col)];
      return n;
    });
  }
  function setExtraRule(sheet: string, field: string, rule: MappingRule | null) {
    setExtra(p => {
      const n = { ...p };
      if (!rule) delete n[sheetFieldKey(sheet, field)]; else n[sheetFieldKey(sheet, field)] = rule;
      return n;
    });
  }
  function toggleSheetCollapse(s: string) {
    setCollapsedSheets(prev => { const n = new Set(prev); n.has(s) ? n.delete(s) : n.add(s); return n; });
  }

  async function saveSheetContracts() {
    if (!editor) return;
    setSavingSC(true);
    try {
      const map: Record<string, number> = {};
      for (const [sh, cid] of Object.entries(sheetContracts)) if (cid) map[sh] = Number(cid);
      await api.put(`/direct/format/${editor.format_id}`, { sheet_contracts: map });
      setScSaved(true);
    } finally { setSavingSC(false); }
  }

  function buildMapping(): Record<string, Record<string, MappingRule>> {
    const out: Record<string, Record<string, MappingRule>> = {};
    for (const [k, outField] of Object.entries(sel)) {
      const [sheet, col] = k.split("||");
      (out[sheet] ??= {})[outField] = { kind: "copy", source: col };
    }
    for (const [k, rule] of Object.entries(extra)) {
      const [sheet, field] = k.split("||");
      (out[sheet] ??= {})[field] = rule;
    }
    return out;
  }

  async function save(activate: boolean) {
    if (!pipeline || !editor) return;
    if (activate && unsourcedFields.length > 0) {
      const preview = unsourcedFields.slice(0, 8).map(f => `• ${f.field}`).join("\n");
      const more = unsourcedFields.length > 8 ? `\n…and ${unsourcedFields.length - 8} more` : "";
      const ok = window.confirm(
        `${unsourcedFields.length} output column(s) are still unsourced:\n${preview}${more}\n\n`
        + `Activate anyway? These fields will be blank in the output until they're sourced.`);
      if (!ok) return;
    }
    setBusy(true); setErr(null); setMsg(null);
    try {
      await api.put(`/direct/format/${editor.format_id}`, {
        sheet_routing: editor.sheet_routing,
        column_mapping: buildMapping(),
        carrier_party_id: pipeline.carrier_party_id, program_id: pipeline.program_id,
        ...(contractId != null ? { contract_id: contractId } : {}),
      });
      const contractsBody = [
        ...Object.entries(sheetContracts)
          .filter(([, cid]) => cid !== "" && cid != null)
          .map(([sheet, cid]) => ({ contract_id: Number(cid), sheet_key: sheet })),
        ...(contractId != null ? [{ contract_id: contractId, sheet_key: null }] : []),
      ];
      await api.put(`/pipelines/${pipeline.id}`, {
        input_format_id: editor.format_id, output_template_id: pipeline.output_template_id,
        contracts: contractsBody,
      });
      if (activate) await api.post(`/pipelines/${pipeline.id}/activate`);
      // Activating never navigates the user away — it stays on this page with a
      // success message and reloads, so nothing can bounce them to onboarding.
      setMsg(activate
        ? "Setup activated — Operator can now process bordereaux for this carrier + program. Any previous setup was superseded."
        : "Saved.");
      await load();
    } catch (e: unknown) { setErr(errText(e)); } finally { setBusy(false); }
  }

  async function deleteSetup() {
    if (!pipeline?.input_format_id) return;
    if (pipeline.status === "active") {
      setErr("This setup is active — activate a different setup for this carrier + program before deleting it.");
      return;
    }
    if (!window.confirm("Discard this setup? This cannot be undone.")) return;
    setBusy(true); setErr(null);
    try {
      await api.delete(`/direct/format/${pipeline.input_format_id}`);
      navigate("/direct/setups");
    } catch (e: unknown) { setErr(errText(e)); setBusy(false); }
  }

  const setupName = pipeline
    // pipeline.name is nullable, so keep a final fallback — the header title
    // is a required string and an unnamed setup must still render.
    ? [pipeline.carrier_name, pipeline.program_name].filter(Boolean).join(" — ") || pipeline.name || "Bordereau Setup"
    : "";
  const contractsById = new Map(contracts.map(c => [c.id, c]));
  const pinnedIds = new Set(Object.values(sheetContracts).filter(Boolean).map(Number));
  const defaultC = contractId && !pinnedIds.has(contractId)
    ? contracts.find(c => c.id === contractId) : undefined;
  const blankLabel = defaultC
    ? `— Default: ${defaultC.filename || `Contract #${defaultC.id}`} —` : "— No Contract —";
  const inputsForOutput = (osheet: string) =>
    (editor?.input_sheets ?? []).filter(i => outputsForInput(editor?.sheet_routing, i).includes(osheet));

  const contractRow = (c: Contract, sheetLabel?: string | null) => {
    const open = openContractId === c.id;
    const active = c.status === "active";
    return (
      <div key={c.id} className={`rounded-lg border ${active ? "border-emerald-300 bg-emerald-50/40" : "border-border bg-white"}`}>
        <button onClick={() => toggleContract(c.id)} className="w-full flex items-center gap-2 px-4 py-2.5 text-left">
          {open ? <ChevronDown size={14} className="shrink-0" /> : <ChevronRight size={14} className="shrink-0" />}
          <FileText size={14} className="text-ink-muted shrink-0" />
          <span className="font-medium text-sm truncate">{c.filename || `Contract #${c.id}`}</span>
          <ContractStatusChip status={c.status} />
          {sheetLabel !== undefined && (
            <span className="text-[11px] rounded-full px-2 py-0.5 bg-navy/10 text-navy font-medium shrink-0">
              {sheetLabel ? `→ ${sheetLabel}` : "Default"}
            </span>
          )}
          <span className="ml-auto text-[11px] text-ink-muted shrink-0">{fmtStamp(c.created_at)}</span>
        </button>
        {open && (
          <div className="border-t border-border px-4 py-3">
            {contractBusy ? null : contractDetail
              ? <ContractInline detail={contractDetail} programId={pipeline?.program_id ?? ""}
                  contractId={c.id} mga={mga} onChanged={reloadContractDetail}
                  canEditVariations={isAdmin} />
              : <div className="text-sm text-ink-muted">Could not load this contract.</div>}
          </div>
        )}
      </div>
    );
  };

  if (loading) {
    return (
      <>
        <PageHeader title="Bordereau Setup" subtitle="Loading…" />
        <PageBody><Card><div className="space-y-2">
          {Array.from({ length: 4 }, (_, i) => <Sk key={i} className="h-9 w-full" />)}
        </div></Card></PageBody>
      </>
    );
  }
  if (notFound || !pipeline) {
    return (
      <>
        <PageHeader title="Bordereau Setup"
          action={<Button variant="secondary" onClick={() => navigate(backTo)}>
            <ArrowLeft size={15} /> {backLabel}
          </Button>} />
        <PageBody>
          <Banner kind="error"><AlertTriangle size={15} /> This setup could not be found, or you don't have access to it.</Banner>
        </PageBody>
      </>
    );
  }

  return (
    <>
      <PageHeader title={setupName}
        subtitle={`${STATUS_LABEL[pipeline.status]} · Last Modified ${fmtStamp(pipeline.modified_at)}`}
        action={
          <Button variant="secondary" onClick={() => navigate(backTo)}>
            <ArrowLeft size={15} /> {backLabel}
          </Button>
        } />
      <PageBody>
        {err && <Banner kind="error"><AlertTriangle size={15} /> {err}</Banner>}
        {msg && <Banner kind="ok"><CheckCircle2 size={15} /> {msg}</Banner>}

        {/* 1) Bordereau Setup — identity + contracts list, with expand-to-detail */}
        <Card title={<span className="flex items-center gap-2">
          <FileText size={16} className="text-navy" /> Bordereau Setup</span>}>
          <div className="flex flex-wrap items-center gap-2 mb-4">
            <span className="font-medium text-sm">{pipeline.name || `Setup #${pipeline.id}`}</span>
            <span className={`text-[11px] rounded-full px-2 py-0.5 font-medium ${pipeline.status === "active"
              ? "bg-emerald-100 text-emerald-700" : "bg-surface-2 text-ink-muted"}`}>
              {STATUS_LABEL[pipeline.status]}
            </span>

          </div>

          {/* Documents the contract defers rules to that were never supplied —
              first, because it explains why some clauses produced no rule at
              all, which otherwise reads as a mapping gap below. */}
          <MissingReferenceDocsNote refDocs={refDocs} className="mb-4"
            showSources={pipeline.contracts.length > 1} />

          {/* What this setup's contract asks for that its bordereau doesn't
              provide — above the contracts on purpose: it is read BEFORE the
              contract it was judged against. Self-loading from the stored
              check, so opening this page costs no model call. */}
          <MissingColumnsNote pipelineId={pipeline.id} refreshKey={noteKey} className="mb-4" />

          <div className="text-sm font-semibold mb-2">
            Contracts <span className="text-xs font-normal text-ink-muted">({pipeline.contracts.length})</span>
          </div>
          <div className="space-y-2">
            {pipeline.contracts.length === 0 && (
              <div className="text-sm text-ink-muted">No contracts bound to this setup yet.</div>
            )}
            {pipeline.contracts.map(pc => {
              const c = contractsById.get(pc.contract_id);
              return c
                ? contractRow(c, pc.sheet_key)
                : (
                  <div key={pc.contract_id} className="rounded-lg border border-border bg-white px-4 py-2.5 flex items-center gap-2 text-sm text-ink-muted">
                    <FileText size={14} /> Contract #{pc.contract_id}
                    <span className="ml-auto text-[11px]">Not In This Program</span>
                  </div>
                );
            })}
          </div>
        </Card>

        {/* 2) Submission Calendar — when this setup's bordereaux are due.
            Placed here, directly under the setup identity, rather than at the
            bottom: Field Mapping below runs to hundreds of rows, and someone
            arriving from a deadline reminder would have to scroll past all of
            it. The carrier and program come from the pipeline, so unlike the
            standalone My Calendar page there is nothing to select — offering a
            picker here would let the section contradict the setup it is in. */}
        {pipeline.program_id != null && (
          <Card title={<span className="flex items-center gap-2">
            <CalendarDays size={16} className="text-navy" /> Submission Calendar</span>}>
            <ProgramCalendar
              programId={pipeline.program_id}
              programName={pipeline.program_name}
              carrierName={pipeline.carrier_name}
              collapsible
              /* Editor only. This page configures the schedule; reading the
                 periods belongs on My Calendar and Program Management. */
              showPeriods={false} />
          </Card>
        )}

        {/* 3) Sheet Mapping — which input feeds each output sheet, and which contract validates it */}
        {editor && editor.output_sheets.length > 0 && contracts.length > 0 && (
          <Card title={<span className="flex items-center gap-2">
            <FileSpreadsheet size={16} className="text-navy" /> Sheet Mapping</span>}
            action={
              <Button onClick={saveSheetContracts} disabled={savingSC}>
                {scSaved ? <><CheckCircle2 size={14} /> Saved</> : <><Save size={14} /> Save Schedule Contracts</>}
              </Button>
            }>
            <p className="text-xs text-ink-muted mb-3">
              Each output sheet: which input sheet(s) feed it and which contract validates it.
              {defaultC
                ? <> Sheets without a specific pick use the <b>default contract</b> automatically.</>
                : <> Sheets left on <b>No Contract</b> aren't contract-validated.</>}{" "}
              One contract can cover several sheets; pinning a contract to a sheet stops it
              applying anywhere else.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-sm border-collapse">
                <thead>
                  <tr className="text-xs text-ink-muted border-b border-border">
                    <th className="py-2 pr-3">Output Sheet</th>
                    <th className="py-2 pr-3">Input Sheet(s)</th>
                    <th className="py-2 pr-3">Contract</th>
                  </tr>
                </thead>
                <tbody>
                  {editor.output_sheets.map(sh => {
                    const feeders = inputsForOutput(sh);
                    return (
                      <tr key={sh} className="border-b border-border/60">
                        <td className="py-2 pr-3 font-medium align-top">{sh}</td>
                        <td className="py-2 pr-3 align-top">
                          <div className="flex flex-wrap gap-1.5">
                            {feeders.length > 0
                              ? feeders.map(i => <TagPill key={i}>{i}</TagPill>)
                              : <span className="text-xs text-amber-600">Not Mapped Yet</span>}
                          </div>
                        </td>
                        <td className="py-2 pr-3 align-top">
                          <select
                            value={sheetContracts[sh] ?? ""}
                            onChange={e => {
                              const v = e.target.value ? Number(e.target.value) : "";
                              setSheetContracts(m => ({ ...m, [sh]: v }));
                              setScSaved(false);
                            }}
                            className="text-xs px-2 py-1 rounded border border-border bg-white min-w-[16rem]">
                            <option value="">{blankLabel}</option>
                            {/* Only ACTIVE contracts are offered — a draft or
                                superseded version shouldn't be pinnable to a
                                sheet. The one exception: a sheet already pinned
                                to a non-active contract keeps that option
                                visible (labelled with its status) — hiding it
                                would make the select render blank while the
                                stale pin silently persists. */}
                            {contracts
                              .filter(c => c.status === "active" || sheetContracts[sh] === c.id)
                              .map(c => (
                                <option key={c.id} value={c.id}>
                                  {(c.filename || `Contract #${c.id}`)
                                    + (c.status !== "active" ? ` (${c.status})` : "")}
                                </option>
                              ))}
                          </select>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {editor && (ruleCount > 0 ? (
          <Banner kind="info">
            <CheckCircle2 size={15} /> Contract: {ruleCount} rule(s) across {fieldsWithClauses} output column(s).
          </Banner>
        ) : (
          <Banner kind="warn">
            <AlertTriangle size={15} /> No contract rules were detected for this template/contract. You can
            still activate; the output just won't be contract-validated until rules exist.
          </Banner>
        ))}

        {/* 3) Field Mapping & Validation Rules — one card per input sheet */}
        {editor && editor.input_sheets.length > 0 && (
          <div className="mt-3 flex flex-col justify-between gap-3 rounded-md border border-border bg-white p-3">
            <h1 className="font-semibold">Field Mapping &amp; Validation Rules</h1>
            {editor.input_sheets.map(inputSheet => {
              const outs = outputsForInput(editor.sheet_routing, inputSheet);
              const inputCols = editor.input_columns[inputSheet] ?? [];
              const multi = outs.length > 1;
              const sheetUnsourced = outs.reduce((n, o) => n + unmappedOutputs(o).length, 0);
              const collapsed = collapsedSheets.has(inputSheet);
              return (
                <Card key={inputSheet}
                  title={<button type="button" onClick={() => toggleSheetCollapse(inputSheet)}
                    className="flex items-center gap-2 text-left hover:text-navy transition">
                    {collapsed ? <ChevronRight size={16} className="shrink-0 text-ink-muted" />
                      : <ChevronDown size={16} className="shrink-0 text-ink-muted" />}
                    <FileSpreadsheet size={16} className="text-navy" /> {inputSheet}
                  </button>}
                  action={sheetUnsourced > 0
                    ? <span className="inline-flex items-center gap-1 text-[11px] font-semibold rounded-full px-2.5 py-1 bg-amber-50 text-amber-700">
                        <AlertTriangle size={12} /> {sheetUnsourced} Unsourced</span>
                    : outs.length > 0
                      ? <span className="inline-flex items-center gap-1 text-[11px] font-semibold rounded-full px-2.5 py-1 bg-emerald-50 text-emerald-700">
                          <CheckCircle2 size={12} /> All Sourced</span>
                      : undefined}>
                  {collapsed ? (
                    <p className="text-xs text-ink-muted">
                      {outs.length === 0
                        ? "Not mapped to any output sheet yet."
                        : <>Maps to <span className="font-medium text-ink">{outs.join(", ")}</span> ·{" "}
                            {inputCols.length} input column{inputCols.length === 1 ? "" : "s"}. Expand to edit.</>}
                    </p>
                  ) : outs.length === 0 ? (
                    <p className="text-sm text-ink-muted">
                      This input sheet isn't mapped to any output sheet.
                    </p>
                  ) : (
                    <>
                      {multi && (
                        <p className="text-xs text-ink-muted mb-2 flex items-center gap-1">
                          Each input Column routes to one output sheet — pick the sheet, then its column.
                        </p>
                      )}
                      <div className="overflow-x-auto">
                        <div className="min-w-[640px] rounded-lg border border-border overflow-hidden">
                          <div className="grid grid-cols-[1.1fr_26px_1.2fr_1.4fr] gap-3.5 px-4 py-2 bg-surface-2 text-[10px] font-bold uppercase tracking-wide text-ink-muted">
                            <div>Input Column</div><div></div><div>Output Column</div><div>Contract Clause &amp; Rule</div>
                          </div>
                          {inputCols.map(col => {
                            const chosen = rowOutputSheet(inputSheet, outs, col);
                            const cur = chosen ? sel[sheetFieldKey(chosen, col)] : undefined;
                            const dup = cur && dupFields.has(`${chosen}::${cur}`);
                            const clauses = cur && chosen ? clauseByField[sheetFieldKey(chosen, cur)] : undefined;
                            const score = cur ? scoreFor(chosen, col, cur) : null;
                            return (
                              <div key={col}
                                className="grid grid-cols-[1.1fr_26px_1.2fr_1.4fr] gap-3.5 items-start px-4 py-3 border-b border-border last:border-b-0">
                                <div className="pt-1.5 flex items-center gap-1.5 flex-wrap font-mono text-xs">
                                  <span className="text-ink">{col}</span>
                                </div>
                                <div className="pt-2 text-center text-ink-soft"><ArrowRight size={13} className="inline" /></div>
                                <div className="space-y-1">
                                  {multi && (
                                    <Select className="!py-1 !text-xs mb-1" value={chosen}
                                      onChange={e => setRowOutputSheet(inputSheet, outs, col, e.target.value)}>
                                      {outs.map(o => <option key={o} value={o}>{o}</option>)}
                                    </Select>
                                  )}
                                  <Combo value={cur} options={outFieldsFor(chosen)} placeholder="Search output column…"
                                    onSelect={f => assignField(outs, col, chosen, f)} />
                                  {cur && score != null && <div><ScoreChip score={score} /></div>}
                                  {dup && <span className="text-[11px] text-amber-600 flex items-center gap-1">
                                    <AlertTriangle size={11} /> Duplicate — Last Wins</span>}
                                  {conflict && conflict.sheet === chosen && conflict.col === col && (
                                    <div className="mt-1.5 rounded-md bg-amber-50 text-amber-800 p-2 text-xs">
                                      <div className="mb-1.5 flex items-start gap-1">
                                        <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                                        <span><b>{conflict.field}</b> is already mapped from <b>{conflict.other}</b>.</span>
                                      </div>
                                      <div className="flex flex-wrap gap-1.5">
                                        <Button variant="secondary" className="!py-1 !px-2 !text-xs"
                                          onClick={() => resolveConflict("move")}>Move Here</Button>
                                        <Button variant="ghost" className="!py-1 !px-2 !text-xs"
                                          onClick={() => resolveConflict("both")}>Keep Both</Button>
                                        <Button variant="ghost" className="!py-1 !px-2 !text-xs"
                                          onClick={() => resolveConflict("cancel")}>Cancel</Button>
                                      </div>
                                    </div>
                                  )}
                                </div>
                                <div className="flex flex-col gap-1.5">
                                  {clauses && clauses.length > 0
                                    ? clauses.map(cl => (
                                        <div key={cl.rule_id}
                                          className="rounded-md border border-navy/15 bg-navy/[0.04] px-2.5 py-2 text-[11.5px] leading-snug text-ink">
                                          <div className="flex items-center gap-1.5 mb-1">
                                            <SevBadge severity={cl.severity} />
                                            {cl.match && <ClauseMatchChip match={cl.match} score={cl.score} />}
                                          </div>
                                          <ClauseText text={cl.text} />
                                        </div>))
                                    : cur ? <span className="text-xs text-ink-soft pt-1.5">No contract rule for this field.</span>
                                          : <span className="text-xs text-ink-soft/70 pt-1.5">—</span>}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>

                      {outs.map(outSheet => unmappedOutputs(outSheet).length > 0 && (
                        <div key={outSheet} className="mt-4 rounded-lg border border-amber-200 bg-amber-50/60 overflow-hidden">
                          <div className="flex items-center gap-1.5 text-sm font-medium px-4 py-2.5 border-b border-amber-200 text-amber-800">
                            <AlertTriangle size={14} /> Output Columns Not Yet Sourced
                            {multi && <span className="font-mono text-xs">· {outSheet}</span>}
                          </div>
                          <div className="divide-y divide-amber-200/70">
                            {unmappedOutputs(outSheet).map(f => (
                              <div key={f} className="flex items-center gap-2 text-sm px-4 py-2.5">
                                <span className="w-56 truncate font-medium">{f}</span>
                                <Select className="!py-1 !w-44" onChange={e => {
                                  const v = e.target.value;
                                  if (v === "const") setExtraRule(outSheet, f, { kind: "const", value: "" });
                                  else if (v === "tab") setExtraRule(outSheet, f, { kind: "source_sheet" });
                                  else setExtraRule(outSheet, f, null);
                                }}>
                                  <option value="">— Leave Blank —</option>
                                  <option value="const">Constant…</option>
                                  <option value="tab">Source Tab Name</option>
                                </Select>
                                {extra[sheetFieldKey(outSheet, f)]?.kind === "const" && (
                                  <TextInput className="!py-1 !w-48" placeholder="value or @contract:KEY"
                                    value={String(extra[sheetFieldKey(outSheet, f)]?.value ?? "")}
                                    onChange={e => setExtraRule(outSheet, f, { kind: "const", value: e.target.value })} />
                                )}
                              </div>
                            ))}
                          </div>
                        </div>
                      ))}
                    </>
                  )}
                </Card>
              );
            })}
          </div>
        )}

        {editor && unassignedOutputs.length > 0 && (
          <Banner kind="warn">
            <AlertTriangle size={15} /> Output sheet(s) not mapped to any input yet:{" "}
            <span className="font-medium">{unassignedOutputs.join(", ")}</span>. Add them under an input
            sheet above, otherwise they'll be blank in the output.
          </Banner>
        )}


        <div className="sticky bottom-0 z-30 -mx-8 mt-2 px-8 py-3 bg-white/95 backdrop-blur
          border-t border-border shadow-[0_-1px_8px_rgba(17,24,39,0.06)]">
          <div className="flex flex-wrap items-center gap-2">
            {/* <span className={`inline-flex items-center gap-1 text-[11px] rounded-full px-2.5 py-1 mr-1 font-medium ${
              pipeline.status === "active" ? "bg-emerald-100 text-emerald-700" : "bg-surface-2 text-ink-muted"}`}>
              Setup #{pipeline.id} · {STATUS_LABEL[pipeline.status]}
            </span> */}
            <Button onClick={() => save(true)} disabled={busy}>
              <CheckCircle2 size={15} /> Activate Setup
            </Button>
            <Button variant="secondary" onClick={() => save(false)} disabled={busy}>Save Draft</Button>
            {isAdmin && (
              <Button variant="danger" onClick={deleteSetup}
                disabled={busy || pipeline.status === "active"}
                title={pipeline.status === "active" ? "This setup is active — activate a different setup before deleting it" : undefined}>
                <Trash2 size={15} /> Delete Draft
              </Button>
            )}
            {unsourcedFields.length > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-amber-600 ml-auto">
                <AlertTriangle size={13} /> {unsourcedFields.length} Field(s) Unsourced
              </span>
            )}
          </div>
        </div>
      </PageBody>
    </>
  );
}
