import { useMemo, useState, useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  CheckCircle2, AlertTriangle, FileSpreadsheet, ShieldCheck, FileUp, FileText,
  FileSpreadsheet as FileOut, FileWarning, UploadCloud, ShieldAlert, ArrowRight,
  Save, Trash2, Sparkles,
} from "lucide-react";
import { api } from "../api/client";
import { currentMga, isTenantAdmin } from "../auth";
import { PageBody, PageHeader } from "../components/Layout";
import Card from "../components/ui/Card";
import Button from "../components/ui/Button";
import { Modal } from "../components/ui/Modal";
import { Field, Select, TextInput } from "../components/ui/Field";
import { InfoTip } from "../components/ui/InfoTip";
import { LoadingOverlay } from "../components/Busy";
import { MissingColumnsList, UnmappedClausesList } from "../components/MissingColumnsNote";
import { errText, MissingColumnsResp, ProgramContractRow, newUploadToken,
         pickRecoveredContract } from "../utils/directSetup";
import CreateOutputTemplate from "../components/CreateOutputTemplate";
import { SHOW_BDX_TEMPLATE_BUILDER } from "../featureFlags";
import OutputTemplateState from "../components/OutputTemplateState";
import MappingReview, { type MappingReviewData } from "../components/MappingReview";
import {
  BoundContracts, BrokerSelect, useBrokerContractScope,
} from "../components/BrokerContractScope";
import {
  resolveOutputTemplate, type ResolveResult, type ScopedContract,
} from "../api/outputTemplate";

// ---- types -----------------------------------------------------------------
type Party = { id: number; legal_name: string; is_active?: boolean };
type Program = { id: number; name: string; status?: string };
const BDX_FREQUENCIES: [string, string][] = [
  ["monthly", "Monthly"], ["quarterly", "Quarterly"],
  ["semi-annual", "Semi-Annual"], ["annual", "Annual"],
];
type Rule = { kind: "copy" | "const" | "source_sheet"; source?: string; value?: string };
type RouteT = { output_sheet: string; sources: { input_sheet: string }[]; filter: null | { column: string; equals?: string } };
type Routing = { version: number; mode: string; confidence: string; routes: RouteT[] };
type UploadResp = {
  landing_id: number; format_id: number; known_format: boolean;
  /** How each output column got its source — see MappingReview. */
  mapping_review?: MappingReviewData | null;
  input_sheets: string[]; output_sheets: string[];
  input_columns: Record<string, string[]>;
  sheet_routing: Routing;
  column_mapping: Record<string, Record<string, Rule>>;
  candidates: Record<string, Record<string, { source: string; confidence: number }[]>>;
};
type Clause = { rule_id: number; severity?: string; text?: string; page?: number; match?: string; score?: number };
type OutField = { sheet: string; field: string; clauses: Clause[] };
// A Pipeline binds this scope's Input Template (a DirectFormat) + Output
// Template + contracts, and carries the run-time active status.
type Pipeline = {
  id: number; name: string | null; status: "draft" | "active" | "superseded";
  input_format_id: number | null; output_template_id: number | null;
  ready: boolean; ready_reason: string;
  contracts: { contract_id: number; sheet_key: string | null }[];
  /** Who the setup is for. NULL is a programme-wide setup — one made before the
   *  broker level existed, or deliberately built to cover everybody. */
  broker_party_id: number | null; broker_name: string | null;
};

// Program metadata captured on inline create — mirrors the Programs screen.
type ProgramForm = {
  name: string; lead_carrier: string; admin_party: string; bdx_frequency: string;
  business_segment: string; product_line: string; distribution_channel: string;
  territory: string; status: string;
};
const EMPTY_PROGRAM: ProgramForm = {
  name: "", lead_carrier: "", admin_party: "", bdx_frequency: "",
  business_segment: "", product_line: "", distribution_channel: "", territory: "", status: "draft",
};
const PROGRAM_META_KEYS: (keyof ProgramForm)[] = [
  "lead_carrier", "admin_party", "bdx_frequency", "business_segment",
  "product_line", "distribution_channel", "territory", "status",
];

// Derive a schedule identity from a file/sheet name: "Palms Sch H Current BDX"
// → "Schedule H". Mirrors the backend _sb_propose_schedule matcher so an
// uploaded contract auto-binds to the schedule sheet it belongs to.
// Separators (._-) are normalized to spaces first so real-world names like
// "… Schedule F_rss.pdf" still match (an underscore blocks the \b boundary).
function scheduleOf(name: string | null | undefined): string | null {
  const m = (name || "").toLowerCase().replace(/[._-]+/g, " ")
    .match(/\bsch(?:edule)?\s*([a-z0-9])\b/);
  return m ? `Schedule ${m[1].toUpperCase()}` : null;
}

// External document(s) a contract defers rules to (halt payload shape).
type RefsHaltRefs = Array<{
  document_name?: string; version_or_date?: string; source_texts?: string[];
}>;

// One sheet of a template's structure — only the fields the sheet-review modal
// needs are typed; the rest ride along untouched so the structure PUTs back whole.
type DsSheet = {
  sheet_name: string;
  sheet_role?: "data" | "reference" | "summary" | null;
  rule_generatable?: boolean | null;
  sheet_role_reason?: string | null;
  [k: string]: unknown;
};

const sk = (sheet: string, col: string) => `${sheet}||${col}`;

// The one thing worth telling someone during a long build: don't close the tab.
// It used to be appended to every step as "this usually takes 5–10 minutes",
// which was both wrong (a long contract runs far longer) and duplicated —
// a step that already explained its own wait ended up carrying two conflicting
// estimates side by side. How long a build takes depends entirely on the size
// of the contract, so the loader no longer claims a number; each step says what
// is happening and this says what to do.
const KEEP_OPEN = "Please keep this page open.";

function buildLabel(step: string): string {
  const s = step.trim() || "Processing…";
  return `${s} ${KEEP_OPEN}`;
}

export default function DirectSetup() {
  const mga = currentMga();
  const isAdmin = isTenantAdmin();
  const navigate = useNavigate();
  // The setup's edit page opens with a "back" target so its header button
  // returns the user to wherever they came from — here, Bordereau Setup —
  // instead of always dropping them on the All Setups list. (No onboarding
  // "return" is threaded through: activating a setup never navigates the user
  // away, so there is nothing to route back to.)
  const editHrefFor = (pipelineId: number) => {
    const p = new URLSearchParams();
    p.set("back", "/direct/setup");
    return `/direct/setups/${pipelineId}/edit?${p.toString()}`;
  };

  // scope
  // The carrier is no longer picked: a tenant IS the carrier. This holds the
  // tenant's own carrier party, resolved once from /my-carrier-party, purely so
  // the pipeline/fingerprint rows that still carry a carrier_party_id keep
  // getting the right value.
  const [carrierName, setCarrierName] = useState<string>("");
  const [programs, setPrograms] = useState<Program[]>([]);
  const [carrierId, setCarrierId] = useState<number | "">("");
  const [programId, setProgramId] = useState<number | "">("");
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  // The input template (DirectFormat) currently loaded in the editor.
  const [loadedSetupId, setLoadedSetupId] = useState<number | null>(null);
  const [creatingProgram, setCreatingProgram] = useState(false);
  const [programForm, setProgramForm] = useState<ProgramForm>(EMPTY_PROGRAM);
  const [createBusy, setCreateBusy] = useState(false);

  // contracts already uploaded for the selected program (+ inline detail)
  // Per-schedule contracts: {output_sheet: contract_id}. A contract can govern
  // several sheets (many→one); sheets left blank are validated by the default
  // contract if one applies, otherwise skipped. Read here only to build a
  // freshly-built setup's initial Pipeline row (see upsertPipeline below) —
  // editing them now happens on the setup's own edit page.
  const [sheetContracts, setSheetContracts] = useState<Record<string, number | "">>({});
  // Supplement: a simple extra-data file uploaded ONCE here on the Setup page
  // (like the input/output templates) and captured alongside the BDX on every
  // run — no policy-number join, no per-run upload. suppCurrentName is the file
  // already stored on the loaded setup, if any.
  const [suppFile, setSuppFile] = useState<File | null>(null);
  const [suppCurrentName, setSuppCurrentName] = useState<string | null>(null);
  const [savingSupp, setSavingSupp] = useState(false);

  // The broker and contract this setup is for. Optional at every step: a
  // programme with no brokers on it, and every setup built before the broker
  // level existed, simply leaves them blank.
  const scope = useBrokerContractScope(programId);
  // The output template already agreed for the scope on screen, if any —
  // together with HOW SPECIFIC the match was, so the user is told when they are
  // looking at the programme's template rather than this contract's.
  const [resolved, setResolved] = useState<ResolveResult | null>(null);
  const [resolving, setResolving] = useState(false);
  // Bumped after a template is created so the scope is re-resolved from the
  // server rather than patched up locally.
  const [resolveTick, setResolveTick] = useState(0);
  const [showCreateTemplate, setShowCreateTemplate] = useState(false);
  // The template this session just made. Held so the screen can hand the user
  // the one link that matters next — the full column list, with any required
  // column that has nothing to fill it named there rather than in the dialog
  // they have already closed.
  const [justCreated, setJustCreated] = useState<{ id: number; name: string } | null>(null);
  // "Create Output BDX Template" needs both sides of the job in front of it —
  // the bordereau to see what can be filled, the contract to see what must be
  // reported. When one is missing this names it instead of opening a dialog
  // that could only produce a worse answer.
  const [createGate, setCreateGate] = useState<string[] | null>(null);

  // the three uploads
  const [outFile, setOutFile] = useState<File | null>(null);
  // Contracts to upload on Build (REQUIRED, one or more). When more than one,
  // sheetContractMap assigns each output sheet to a contract (by index).
  const [contractFiles, setContractFiles] = useState<File[]>([]);
  // Contracts ALREADY uploaded for this (programme, broker) — carried in from
  // the scope rather than asked for again. A contract belongs to exactly one
  // pairing, so once the broker is picked the ones on file are decided; making
  // someone re-upload a document the carrier has already approved is asking
  // them to do work the system has already done, and it creates a second
  // contract row for the same paper.
  const [reusedContracts, setReusedContracts] = useState<ScopedContract[]>([]);
  const [sheetContractMap, setSheetContractMap] = useState<Record<string, number>>({});
  const [inputFile, setInputFile] = useState<File | null>(null);

  // Optional reference document(s) — external docs the contract defers to
  // ("Excluded Classes: per the Purchasing Guidelines on file"). Attached
  // upfront (Path A) AND/OR uploaded when the build pauses asking for them
  // (Path B). Accumulated across rounds so a partial upload doesn't drop the
  // earlier ones.
  const [refFiles, setRefFiles] = useState<File[]>([]);

  // Pull in whatever the scope already holds whenever the pairing changes. Keyed
  // on the ids, not the array, because the scope refetches and hands back a new
  // array each time — resetting on identity alone would undo a removal the
  // moment anything else re-rendered.
  const boundIds = scope.boundContracts.map(c => c.id).join(",");
  useEffect(() => {
    setReusedContracts(scope.boundContracts);
    // The sheet→contract map indexes into the staged list, so it cannot survive
    // that list being rebuilt from a different pairing.
    setSheetContractMap({});
  }, [boundIds]);   // eslint-disable-line react-hooks/exhaustive-deps

  // The contracts this build will use, in one list: the ones already on file
  // first, then anything newly picked. Everything downstream — the extraction
  // loop, the schedule auto-match, the sheet map, the counts — reads THIS, so a
  // reused contract and an uploaded one are the same thing to the build. The
  // only difference is that a reused one already has its id and skips
  // extraction entirely.
  type StagedContract =
    | { kind: "existing"; id: number; name: string }
    | { kind: "file"; file: File; name: string };
  const staged = useMemo<StagedContract[]>(() => [
    ...reusedContracts.map(c => ({
      kind: "existing" as const, id: c.id,
      name: c.filename || `Contract ${c.id}`,
    })),
    ...contractFiles.map(f => ({ kind: "file" as const, file: f, name: f.name })),
  ], [reusedContracts, contractFiles]);

  // Build paused waiting for reference docs: holds the already-created output
  // template id (so we re-run only the contract + mapping steps, not recreate
  // the template) + the referenced doc names to show the user.
  // Set when the user closes the halt dialog without deciding — the pause
  // survives as an inline card that can re-open it, so a stray Esc or
  // backdrop click can never leave a paused build with no way to resume.
  const [refsHaltDismissed, setRefsHaltDismissed] = useState(false);
  const [refsHalt, setRefsHalt] = useState<null | {
    templateId: number;
    refs: RefsHaltRefs;
    resumeToken: string | null;
    // Set when the halt came from the MULTI-contract build: which staged
    // contract halted + the contract ids uploaded before it, so the build can
    // resume from exactly that point (not restart from contract 1).
    multi?: { index: number; cids: Array<number | null>; contractName: string };
  }>(null);
  const refHaltInputRef = useRef<HTMLInputElement>(null);
  // Multi-contract flow is NON-halting, so a contract that defers rules to an
  // external document (e.g. "per the Facultative Purchasing Guidelines") won't
  // pause the build — but those deferred clauses produce NO rules. Surface the
  // gap loudly after Build so the user attaches the doc(s) and rebuilds.
  const [deferredRefs, setDeferredRefs] = useState<Array<{
    contract: string; refs: string[];
  }>>([]);

  // Build paused for the user to confirm the AI's data-vs-reference sheet split
  // on the output template BEFORE rules are generated. Reference (lookup/mapping)
  // tabs are excluded from rule generation; the user can override here. Holds the
  // created template id + its full structure (so overrides are PUT back verbatim).
  const [sheetReview, setSheetReview] = useState<null | {
    templateId: number;
    structure: { sheets: DsSheet[] };
  }>(null);

  // per-file sheet pickers: available sheet names + the user's selection.
  // Only the selected sheets are mapped; the output contains only its selection.
  const [inputSheetOpts, setInputSheetOpts] = useState<string[] | null>(null);
  // The multi-table refusal from /direct/peek-sheets, shown as a MODAL (an
  // in-app dialog the user must acknowledge) rather than the top error banner.
  const [multiTableModal, setMultiTableModal] = useState<string | null>(null);
  const [inputSheetSel, setInputSheetSel] = useState<Set<string>>(new Set());
  const [outputSheetOpts, setOutputSheetOpts] = useState<string[] | null>(null);
  const [outputSheetSel, setOutputSheetSel] = useState<Set<string>>(new Set());

  // build state
  const [building, setBuilding] = useState(false);
  const [step, setStep] = useState("");
  // "How long this takes" popup, shown once when a build kicks off.
  const [buildInfoOpen, setBuildInfoOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  // Shown once a build (fresh, or resumed through sheet-review / a reference-
  // doc pause) finishes successfully — a short "what got generated" summary,
  // set at the very end of buildContracts() so every path that reaches a
  // successful mapping shows it, without changing any of the build logic itself.
  const [buildSummary, setBuildSummary] = useState<null | {
    pipelineId: number | null;
    inputSheets: number; outputSheets: number; contracts: number;
    rules: number; fieldsWithRules: number; totalFields: number;
    sheetsBoundToContract: number; deferredCount: number;
    // How each output column got its source. Shown here because a required
    // column with no source is invisible until the file is opened.
    mappingReview: MappingReviewData | null;
    // Columns the contract expects that the uploaded bordereau doesn't carry —
    // checked once at the end of the build and stored, so this modal and the
    // setup's own page show the same finding. null = the check didn't run
    // (no contract, or unavailable); the build itself is unaffected either way.
    missing: MissingColumnsResp | null;
  }>(null);

  // results of the build
  const [templateId, setTemplateId] = useState<number | null>(null);
  const [contractId, setContractId] = useState<number | null>(null);
  const [up, setUp] = useState<UploadResp | null>(null);
  const [routing, setRouting] = useState<Routing | null>(null);
  const [outFields, setOutFields] = useState<OutField[]>([]);
  // sel/extra/rowSheet/collapsedSheets are populated by applyMapping() below
  // (shared by a fresh build and loadSetup) but no longer rendered or edited
  // here — that now happens on the setup's own edit page. Kept only so
  // applyMapping/loadSetup stay exactly as they were.
  const [sel, setSel] = useState<Record<string, string>>({});
  const [extra, setExtra] = useState<Record<string, Rule>>({});
  const [rowSheet, setRowSheet] = useState<Record<string, string>>({});
  const [collapsedSheets, setCollapsedSheets] = useState<Set<string>>(new Set());

  // ---- scope loaders -------------------------------------------------------
  useEffect(() => {
    // Who this carrier is, not which carrier to use. Resolved once so the
    // stored carrier_party_id stays correct without ever asking the user.
    api.get<{ id: number; legal_name: string }>(`/my-carrier-party`, { params: { mga } })
      .then(r => { setCarrierId(r.data.id); setCarrierName(r.data.legal_name ?? ""); })
      .catch(() => { setCarrierId(""); setCarrierName(""); });
  }, [mga]);
  useEffect(() => {
    // The carrier owns its programmes directly, so this is the whole scope
    // picker now: pick a programme of your own book.
    resetEditor();
    setPrograms([]); setProgramId("");
    api.get(`/programs`, { params: { mga } })
      // Inactive programs are hidden here — you can't build a setup on them.
      .then(r => {
        const list: Program[] = Array.isArray(r.data) ? r.data : (r.data?.items ?? []);
        setPrograms(list.filter(p => p.status !== "inactive"));
      })
      .catch(() => setPrograms([]));
  }, [mga]);

  // Arriving from Process Bordereau with a selection already made. The screen
  // that sent the user here knows the carrier, the programme and the broker —
  // asking for them again would be asking a question that has been answered.
  // Applied once the programme list is in (a programme cannot be selected
  // before it is listed), and only while nothing has been picked by hand.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    if (!programs.length) return;
    const wanted = Number(params.get("program_id") || 0);
    if (!wanted || !programs.some(p => p.id === wanted)) return;
    setProgramId(prev => (prev === "" ? wanted : prev));
    // Consumed: a reload, or a later change of programme, must not snap the
    // selection back to where the link pointed.
    const next = new URLSearchParams(params);
    next.delete("program_id");
    next.delete("carrier_party_id");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [programs]);

  // The broker too, once the programme's broker list has arrived. Separate
  // because the list only loads after the programme is chosen, and a broker
  // cannot be selected before it is there.
  useEffect(() => {
    const wanted = Number(params.get("broker_party_id") || 0);
    if (!wanted || !scope.brokers.some(b => b.id === wanted)) return;
    if (scope.brokerPartyId === "") scope.setBrokerPartyId(wanted);
    const next = new URLSearchParams(params);
    next.delete("broker_party_id");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope.brokers]);
  // Reset the mapping editor AND everything staged for a build (used when the
  // scope changes or a setup is deleted). The staged files belong to the scope
  // they were picked under — carrying a BDX/contract/template upload over to a
  // different carrier would build that carrier's setup from the wrong files.
  /** Drop the editor state belonging to a scope that is no longer selected.
   *
   *  `keepUploads` is for a change WITHIN a programme — picking the broker. The
   *  bordereau and the templates are the same documents whichever broker they
   *  are filed against, and someone who staged three files and then narrowed
   *  the scope has not asked for that work to be thrown away. What always goes
   *  is everything the OLD scope decided: the loaded setup, the mapping, a
   *  paused build, and the sheet→contract bindings — those index into a staged
   *  list that the broker pick rebuilds. */
  function resetEditor(opts?: { keepUploads?: boolean }) {
    setUp(null); setRouting(null); setOutFields([]);
    setSel({}); setExtra({}); setRowSheet({});
    setContractId(null); setTemplateId(null); setLoadedSetupId(null);
    setDeferredRefs([]);
    // Bindings and a paused build belong to the scope that produced them,
    // never to the next one — cleared whichever kind of change this is.
    setSheetContractMap({}); setRefsHalt(null);
    setSuppCurrentName(null);
    setBuildSummary(null); setStep("");
    // The banners as well. "Loaded setup for editing." is a statement about the
    // setup that WAS loaded, so leaving it up after the scope changes tells
    // someone a setup was found for a programme that has none — and an error
    // from the previous programme reads as a fault in the new one.
    setMsg(null); setErr(null);
    if (opts?.keepUploads) return;
    // Staged uploads + everything derived from them.
    setOutFile(null); setInputFile(null); setContractFiles([]);
    setRefFiles([]); setSuppFile(null);
    // Sheet pickers / review derived from the staged workbooks.
    setSheetReview(null);
    setInputSheetOpts(null); setInputSheetSel(new Set());
    setOutputSheetOpts(null); setOutputSheetSel(new Set());
    setCollapsedSheets(new Set());
  }
  // Load the saved setups for the current scope. When `autoLoad` is set (on a
  // fresh carrier + program pick) the saved setup is loaded into the editor
  // automatically — no separate "load" step. Prefers the active setup, else the
  // only draft. Callers after a build/save pass no options (list refresh only).
  function refreshExisting(opts?: { autoLoad?: boolean }) {
    if (programId === "" || carrierId === "") { setPipelines([]); return; }
    // Scoped to the broker as well, not just carrier + programme. A setup is
    // built against a contract and a contract belongs to one broker, so a list
    // that stops at the programme offers setups built on somebody else's terms
    // — and "load the active one" would then load the wrong setup entirely.
    // The server keeps programme-wide setups (broker_party_id NULL) in the
    // answer, so asking for one broker never hides the setup covering everyone.
    api.get(`/pipelines`, { params: {
      mga, carrier_party_id: carrierId, program_id: programId,
      broker_party_id: scope.brokerPartyId === "" ? undefined : scope.brokerPartyId,
    } })
      .then(r => {
        const all: Pipeline[] = Array.isArray(r.data) ? r.data : [];
        // Sending no broker means the server applies no broker filter, so this
        // comes back holding every broker's setups. With nobody picked, a setup
        // built for ONE broker is not in scope — it was built on that broker's
        // contract, and listing it (worse, auto-loading it) puts someone in an
        // editor for terms they did not ask for. Programme-wide setups stay:
        // they belong to no broker, which is exactly the current scope, and it
        // is how a programme with no brokers on it keeps working at all.
        const list = scope.brokerPartyId === ""
          ? all.filter(p => p.broker_party_id == null)
          : all;
        setPipelines(list);
        if (opts?.autoLoad) {
          // Auto-load the active pipeline's input template (else the only one) —
          // just to know its format id, for the supplement-management section
          // and the sheet-contracts fetch below (mapping review/edit itself
          // now happens on the setup's own edit page).
          const target = list.find(p => p.status === "active") ?? (list.length === 1 ? list[0] : null);
          const fid = target?.input_format_id ?? null;
          if (fid != null && fid !== loadedSetupId) loadSetup(fid);
        }
      })
      .catch(() => setPipelines([]));
  }
  // On a scope change, drop the editor state from the previous scope, then load
  // this scope's saved setup automatically. The BROKER is part of that scope:
  // it decides which contract is in play, so it has to reload the same way a
  // programme change does — but it must not take the staged uploads with it,
  // which is what `keepUploads` is for. Tracked with a ref rather than two
  // effects because the hook clears the broker as part of a programme change,
  // so both would fire for what is really one event.
  const prevProgramId = useRef<number | "">("");
  useEffect(() => {
    const programChanged = prevProgramId.current !== programId;
    prevProgramId.current = programId;
    resetEditor({ keepUploads: !programChanged });
    refreshExisting({ autoLoad: true });
  }, [programId, scope.brokerPartyId]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Which output template the four levels currently point at. Re-run on every
  // scope change so the answer on screen always describes what is selected, and
  // again whenever a template is created — the server's answer, never a guess
  // assembled here, because it also reports which SETUP would run.
  useEffect(() => {
    if (programId === "") { setResolved(null); return; }
    let stale = false;
    setResolving(true);
    resolveOutputTemplate(mga, {
      program_id: Number(programId),
      carrier_party_id: carrierId === "" ? null : Number(carrierId),
      broker_party_id: scope.brokerPartyId === "" ? null : Number(scope.brokerPartyId),
      contract_id: scope.contractId === "" ? null : Number(scope.contractId),
    })
      .then(r => { if (!stale) setResolved(r); })
      .catch(() => { if (!stale) setResolved(null); })
      .finally(() => { if (!stale) setResolving(false); });
    return () => { stale = true; };
  }, [mga, programId, carrierId, scope.brokerPartyId, scope.contractId, resolveTick]);

  // Load saved per-schedule contract bindings whenever the active format changes.
  useEffect(() => {
    const fid = up?.format_id;
    if (!fid) { setSheetContracts({}); return; }
    api.get(`/direct/format/${fid}`)
      .then(r => {
        setSheetContracts((r.data?.sheet_contracts as Record<string, number>) || {});
        const supp = r.data?.supplement || null;
        setSuppCurrentName(supp && supp.enabled ? (supp.filename || "supplement file") : null);
        setSuppFile(null);
      })
      .catch(() => setSheetContracts({}));
  }, [up?.format_id]);

  // Upload the supplement file to a format (parsed + stored server-side). Called
  // during Build and by the inline "Save supplement" action on a loaded setup.
  async function uploadSupplement(fid: number, f: File) {
    const fd = new FormData();
    fd.append("file", f);
    await api.post(`/direct/format/${fid}/supplement`, fd);
  }
  async function saveSupplementNow() {
    if (!up?.format_id || !suppFile) return;
    setSavingSupp(true);
    try {
      await uploadSupplement(up.format_id, suppFile);
      setSuppCurrentName(suppFile.name); setSuppFile(null);
    } finally { setSavingSupp(false); }
  }
  async function removeSupplement() {
    const fid = up?.format_id;
    setSuppFile(null); setSuppCurrentName(null);
    if (!fid) return;
    const fd = new FormData(); fd.append("clear", "true");
    await api.post(`/direct/format/${fid}/supplement`, fd);
  }

  // Setup/template name is derived from the scope — no separate field needed.
  const programName = programs.find(p => p.id === programId)?.name ?? "";
  const setupName = [carrierName, programName].filter(Boolean).join(" — ") || "Direct setup";
  // The uploads are all scoped to a (carrier, program) pair — nothing picked
  // yet means there's nowhere for a file to attach to, so every drop zone
  // stays disabled until both are selected.
  // Nothing can be uploaded until the scope is real. A programme is not a
  // production relationship on its own — a bordereau arrives FROM a broker,
  // under a contract the carrier approved — so until a broker has been put on
  // the programme there is nobody for this setup to be for, and every upload
  // box stays shut with the reason said out loud rather than accepting files
  // into a scope that cannot be finished.
  //
  // `hasBrokers === null` means the answer has not arrived yet; the boxes stay
  // shut for that moment too rather than flickering open and closed.
  //
  // ONE CARVE-OUT: a programme that already has a saved setup keeps working.
  // Those were built before the broker level existed, and locking their owner
  // out of editing them would be a worse fault than the one this prevents.
  const noBrokerYet = programId !== "" && scope.hasBrokers === false
    && pipelines.length === 0;
  const scopeIncomplete =
    programId === "" || (scope.hasBrokers !== true && pipelines.length === 0);

  // ---- inline create of carrier / program ---------------------------------
  async function createProgram() {
    if (!programForm.name.trim() || carrierId === "") return;
    setCreateBusy(true); setErr(null);
    try {
      const payload: Record<string, unknown> = { name: programForm.name.trim(), party_id: carrierId };
      for (const k of PROGRAM_META_KEYS) {
        const v = programForm[k]?.trim();
        if (v) payload[k] = v;
      }
      const { data } = await api.post(`/programs`, payload, { params: { mga } });
      setPrograms(prev => [...prev, { id: data.id, name: data.name }]);
      setProgramId(data.id);
      setCreatingProgram(false); setProgramForm(EMPTY_PROGRAM);
    } catch (e: unknown) { setErr(errText(e)); } finally { setCreateBusy(false); }
  }
  function setProgramField<K extends keyof ProgramForm>(k: K, v: ProgramForm[K]) {
    setProgramForm(prev => ({ ...prev, [k]: v }));
  }

  // Populate the editor from an upload/editor response (shared by build + load).
  function applyMapping(upObj: UploadResp, fields: OutField[], cid: number | null, tid: number | null) {
    const seed: Record<string, string> = {};
    const seedExtra: Record<string, Rule> = {};
    for (const [sheet, cols] of Object.entries(upObj.column_mapping || {})) {
      for (const [outField, rule] of Object.entries(cols)) {
        if (rule.kind === "copy" && rule.source) seed[sk(sheet, rule.source)] = outField;
        else seedExtra[sk(sheet, outField)] = rule;
      }
    }
    setUp(upObj); setRouting(upObj.sheet_routing); setOutFields(fields);
    setSel(dedupeSeedByInputCol(seed, upObj)); setExtra(seedExtra);
    setRowSheet({}); setContractId(cid); setTemplateId(tid);
    // Every sheet card starts collapsed — a freshly built/loaded setup can have
    // many sheets, and showing them all open at once makes the page unusably
    // long. The user expands only the ones they need to review.
    setCollapsedSheets(new Set(upObj.input_sheets));
  }
  // A proposal may map the SAME input column into several output sheets (split
  // mode reuses the one input). Each column belongs to one output sheet, so keep
  // the highest-confidence target and drop the rest.
  function dedupeSeedByInputCol(seed: Record<string, string>, upObj: UploadResp): Record<string, string> {
    const outToIn: Record<string, string> = {};
    for (const r of upObj.sheet_routing?.routes ?? []) outToIn[r.output_sheet] = r.sources[0]?.input_sheet ?? "";
    const groups: Record<string, { key: string; sheet: string; field: string; col: string }[]> = {};
    for (const [key, field] of Object.entries(seed)) {
      const [sheet, col] = key.split("||");
      const inSheet = outToIn[sheet];
      if (!inSheet) continue;
      (groups[`${inSheet}||${col}`] ??= []).push({ key, sheet, field, col });
    }
    const result = { ...seed };
    for (const items of Object.values(groups)) {
      if (items.length <= 1) continue;
      let best = items[0], bestScore = -1;
      for (const it of items) {
        const sc = upObj.candidates?.[it.sheet]?.[it.field]?.find(c => c.source === it.col)?.confidence ?? 0;
        if (sc > bestScore) { bestScore = sc; best = it; }
      }
      for (const it of items) if (it !== best) delete result[it.key];
    }
    return result;
  }

  // Load an EXISTING setup into the editor (review / edit / re-activate).
  async function loadSetup(id: number) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const { data } = await api.get(`/direct/format/${id}/editor`);
      const upObj: UploadResp = {
        landing_id: data.landing_id ?? 0, format_id: data.format_id, known_format: true,
        input_sheets: data.input_sheets || [], output_sheets: data.output_sheets || [],
        input_columns: data.input_columns || {},
        sheet_routing: data.sheet_routing || { version: 1, mode: "pair", confidence: "high", routes: [] },
        column_mapping: data.column_mapping || {}, candidates: data.candidates || {},
      };
      applyMapping(upObj, data.fields || [], data.contract_id ?? null, data.template_id ?? null);
      setLoadedSetupId(id);
      setMsg(`Loaded setup for editing.`);
    } catch (e: unknown) { setErr(errText(e)); } finally { setBusy(false); }
  }

  // Activate an existing pipeline directly (supersedes the others for this scope).
  async function activateExisting(pipelineId: number) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      await api.post(`/pipelines/${pipelineId}/activate`);
      setMsg("Setup activated — Operator can now process bordereaux for this carrier + program.");
      refreshExisting();
    } catch (e: unknown) { setErr(errText(e)); } finally { setBusy(false); }
  }

  // ---- sheet pickers (input / output) --------------------------------------
  // When a workbook is picked, list its sheets so the user can choose which to
  // include. Default: all selected. Clearing the file resets the picker.
  async function pickFileWithSheets(kind: "input" | "output", f: File | null) {
    const setFile = kind === "input" ? setInputFile : setOutFile;
    const setOpts = kind === "input" ? setInputSheetOpts : setOutputSheetOpts;
    const setSel = kind === "input" ? setInputSheetSel : setOutputSheetSel;
    setFile(f);
    setOpts(null); setSel(new Set());
    if (!f) return;
    setErr(null);
    try {
      const fd = new FormData();
      fd.append("file", f); fd.append("kind", kind);
      const { data } = await api.post<{ sheets: string[] }>(`/direct/peek-sheets`, fd);
      const names = Array.isArray(data.sheets) ? data.sheets : [];
      setOpts(names); setSel(new Set(names));   // default: include all
    } catch (e: unknown) {
      // peek REFUSES unusable workbooks (e.g. several tables stacked in one
      // sheet) with an actionable 400 — clear the pick so the setup cannot
      // proceed with it, and show the server's own message verbatim. The
      // multi-table refusal opens a MODAL (it needs an acknowledgement and
      // instructions); any other read failure keeps the plain banner.
      setFile(null); setOpts(null);
      const msg = errText(e);
      if (/multiple tables|more than one table/i.test(msg)) setMultiTableModal(msg);
      else setErr(msg);
    }
  }
  function toggleSheet(kind: "input" | "output", name: string) {
    const setSel = kind === "input" ? setInputSheetSel : setOutputSheetSel;
    setSel(prev => { const n = new Set(prev); n.has(name) ? n.delete(name) : n.add(name); return n; });
  }

  // What the template builder still needs. An input format already saved on
  // the loaded setup counts as the input side; a contract already approved for
  // the scope counts as the contract side — neither has to be re-uploaded.
  function missingForCreate(): string[] {
    const missing: string[] = [];
    if (!inputFile && !up?.format_id) missing.push("the input template");
    if (staged.length === 0) missing.push("the contract");
    return missing;
  }

  function openCreateTemplate() {
    const missing = missingForCreate();
    if (missing.length) { setCreateGate(missing); return; }
    setErr(null);
    setShowCreateTemplate(true);
  }

  // ---- build setup from the uploads ----------------------------------------
  async function buildSetup() {
    // The output template no longer has to be an uploaded file: one created
    // from a reporting standard or from the contract counts too, and when the
    // scope already resolves to one there is nothing to upload at all.
    const existingTemplateId = resolved?.template?.id ?? 0;
    if (carrierId === "" || programId === "" || !inputFile || staged.length === 0) {
      setErr("Pick a program, an input file and at least one contract."); return;
    }
    // No output template, and none uploaded? Offer the two ways to make one
    // rather than refusing — this is the fork in the road, not an error.
    if (!outFile && !existingTemplateId) {
      openCreateTemplate();
      return;
    }
    if (outputSheetOpts && outputSheetSel.size === 0) {
      setErr("Select at least one output sheet to include."); return;
    }
    if (inputSheetOpts && inputSheetSel.size === 0) {
      setErr("Select at least one input sheet to map."); return;
    }
    setBuilding(true); setErr(null); setMsg(null); setUp(null); setRefsHalt(null); setSheetReview(null);
    // setBuildInfoOpen(true);  // build-info popup disabled — see the commented modal below
    try {
      if (!outFile) {
        // The scope already has a template — one created from a reporting
        // standard or from the contract. Nothing to create, so go straight to
        // contracts + mapping against it.
        await buildContracts(existingTemplateId);
        return;
      }
      // 1) Output template. Creating it runs the AI header classifier that tags
      // each tab data vs reference (lookup). We PAUSE here and let the user review
      // that split before any rules are generated — the modal drives step 2 + 3.
      setStep("Creating output template…");
      const fOut = new FormData();
      fOut.append("mga", mga); fOut.append("name", setupName); fOut.append("file", outFile);
      fOut.append("carrier_party_id", String(carrierId));
      if (outputSheetOpts) for (const sh of outputSheetSel) fOut.append("sheets", sh);
      const tplRes = await api.post(`/export/template/generate`, fOut);
      const tid = tplRes.data.id as number;
      setTemplateId(tid);
      // Only pause for review when the classifier actually flagged a reference
      // tab — otherwise there is nothing to confirm, so continue the build straight
      // through to contract + mapping.
      const sheets: DsSheet[] = tplRes.data.structure?.sheets ?? [];
      if (sheets.some(isNonDataSheet)) {
        setSheetReview({ templateId: tid, structure: tplRes.data.structure });
        setStep("");
      } else {
        await buildContracts(tid);
      }
    } catch (e: unknown) { setErr(`${step || "Build"} failed — ${errText(e)}`); }
    finally { setBuilding(false); setStep(""); }
  }

  // Flip one sheet's role in the review modal (local only until the user proceeds).
  function setReviewRole(idx: number, role: "data" | "reference" | "summary") {
    setSheetReview(prev => prev && {
      ...prev,
      structure: {
        sheets: prev.structure.sheets.map((sh, i) =>
          i === idx ? { ...sh, sheet_role: role, rule_generatable: role === "data" } : sh),
      },
    });
  }

  // "Looks right — generate rules": persist any role overrides, then run the
  // contract + mapping steps (2 + 3) against the reviewed template.
  async function proceedAfterSheetReview() {
    if (!sheetReview) return;
    const { templateId: tid, structure } = sheetReview;
    setSheetReview(null);
    setBuilding(true); setErr(null); setMsg(null);
    try {
      await api.put(`/export/template/${tid}`, { structure });
      await buildContracts(tid);
    } catch (e: unknown) { setErr(`${step || "Build"} failed — ${errText(e)}`); }
    finally { setBuilding(false); setStep(""); }
  }

  // Upload every contract, create the format, then (when there's more than one
  // contract) bind each sheet to its mapped contract.
  // Non-halting: reference docs attached upfront are sent with every contract.
  async function buildContracts(tid: number, resume?: {
    fromIndex: number; cids: Array<number | null>;
    resumeToken?: string | null; continueAnyway?: boolean;
    referenceFiles?: File[];
  }) {
    const missing: Array<{ contract: string; refs: string[] }> = [];
    const allRefs = resume?.referenceFiles ?? refFiles;
    type UpResult = { cid: number | null } | {
      halt: { refs: RefsHaltRefs; resumeToken: string | null } };

    // The contract this program already has, by id. Snapshotted BEFORE each
    // upload so the contract an upload CREATES can be identified afterwards by
    // id alone — no clock comparison, so a browser/server clock skew can't
    // pick the wrong row.
    const contractIdsNow = async (): Promise<Set<number>> => {
      try {
        const { data } = await api.get<Array<{ id: number }>>(
          `/programs/${programId}/contracts`, { silent: true });
        return new Set((Array.isArray(data) ? data : []).map(c => c.id));
      } catch { return new Set(); }
    };

    // Recover the contract id when the upload's ANSWER was lost.
    //
    // A contract can take well over half an hour to extract: a long document
    // falls back to per-section extraction, then Call 2 runs once per clause and
    // Call 3 once per intent chunk — 75+ sequential model calls is normal. The
    // response is streamed with whitespace heartbeats so an idle-connection
    // timeout never fires, but an ingress that caps TOTAL request duration cuts
    // the connection regardless, and the browser is then left holding a 200
    // whose body is only heartbeat whitespace — which axios cannot parse and
    // hands back as a raw string.
    //
    // Crucially the server does NOT stop: the pipeline task outlives the request
    // and persists the contract and its rules minutes later. (Observed: the
    // browser gave up at 04:50:31 while the server was still running Call-3
    // retries at 04:52:33.) So "no id in the response" means the answer was
    // lost, not that there is no contract — wait for the row the work produces
    // instead of treating the build as contract-less. The window has to cover
    // the REMAINDER of a run that may already be ~30 min in; polling is one
    // small request every 15s, so waiting too long costs far less than
    // discarding a contract that was about to land.
    const RECOVER_TIMEOUT_MS = 30 * 60_000;
    const RECOVER_POLL_MS = 15_000;
    const recoverContractId = async (
      file: File, before: Set<number>, token: string,
    ): Promise<number | null> => {
      const deadline = Date.now() + RECOVER_TIMEOUT_MS;
      for (;;) {
        try {
          const { data } = await api.get<ProgramContractRow[]>(
            `/programs/${programId}/contracts`, { silent: true });
          const hit = pickRecoveredContract(data, {
            token, before, filename: file.name, templateId: tid });
          if (hit != null) return hit;
        } catch { /* keep waiting — a failed poll is not a failed upload */ }
        if (Date.now() >= deadline) return null;
        // Deliberately says nothing about the dropped connection. Nothing has
        // gone wrong from where the user sits — the contract is being read, and
        // that is the only fact they can act on. Naming the transport here read
        // as an error for something that is working exactly as intended.
        setStep(`Still reading “${file.name}” on the server. A long contract `
              + `takes a while — checking again every ${RECOVER_POLL_MS / 1000}s…`);
        await new Promise(res => setTimeout(res, RECOVER_POLL_MS));
      }
    };

    const uploadOne = async (
      file: File, scheduleKey: string | null,
      opts?: { continueAnyway?: boolean; resumeToken?: string | null },
    ): Promise<UpResult> => {
      const fd = new FormData();
      fd.append("output_template_id", String(tid));
      fd.append("file", file);
      if (scheduleKey) fd.append("schedule_key", scheduleKey);
      allRefs.forEach(f => fd.append("reference_files", f));
      // HALT when the contract defers rules to an external document that wasn't
      // provided — the user chooses: upload it, or continue anyway. Skipped by
      // the backend when reference docs were already attached above, or when
      // this is the continue-anyway resume below.
      fd.append("enable_reference_halt", "true");
      if (opts?.continueAnyway) {
        fd.append("continue_anyway", "true");
        if (opts.resumeToken) fd.append("resume_token", opts.resumeToken);
      }
      // Correlation id for THIS upload, so the contract it creates can be
      // identified exactly rather than inferred, even when two builds of the
      // same setup run at once or an earlier attempt's contract lands late.
      const token = newUploadToken();
      fd.append("upload_token", token);
      const before = await contractIdsNow();
      let r: { data?: Record<string, unknown> } | null = null;
      try {
        r = await api.post(`/programs/${programId}/contracts`, fd);
      } catch (e) {
        // A response the server actually sent (4xx/5xx, or the in-body pipeline
        // failure the interceptor re-throws) is a real error and must surface.
        // A transport-level failure carries no response — same lost-answer case
        // as an unparseable body, so fall through to recovery.
        if ((e as { response?: unknown })?.response) throw e;
      }
      const body = (r?.data ?? {}) as Record<string, unknown>;
      if (body.status === "references_required") {
        return { halt: { refs: (body.external_references ?? []) as RefsHaltRefs,
                         resumeToken: (body.resume_token ?? null) as string | null } };
      }
      // Extraction proceeded but still names external document(s) it didn't
      // have (continue-anyway path) — collect for the post-build warning.
      const ext = ((body.extraction_output as { external_references?: unknown })
        ?.external_references ?? []) as Array<{ document_name?: string }>;
      const covered = (n: string) => allRefs.some(rf => {
        const a = rf.name.toLowerCase(), b = n.toLowerCase();
        return a.includes(b.slice(0, 12)) || b.includes(a.replace(/\.[a-z]+$/, "").slice(0, 12));
      });
      const names = (ext.map(x => x?.document_name).filter(Boolean) as string[])
        .filter(n => !covered(n));
      if (names.length) missing.push({ contract: file.name, refs: names });
      const persisted = body.persisted as { contract_id?: number } | undefined;
      let cid = (body.id ?? persisted?.contract_id ?? null) as number | null;
      if (cid == null) cid = await recoverContractId(file, before, token);
      // Still nothing: the contract genuinely never landed. Fail the build —
      // carrying on would bind the setup to NO contract, which renders every
      // rule the contract produced invisible behind a green success screen.
      if (cid == null) {
        throw new Error(
          `“${file.name}” was uploaded but no contract came back from the `
          + `server, so this setup has no contract to take its rules from. `
          + `Please run the build again.`);
      }
      return { cid };
    };

    // Upload each contract; tag it with the schedule its filename names (so
    // supersede stays per-schedule) and keep the cid per staged index. On a
    // resume, skip the contracts already extracted and re-run the halted one
    // (with the new reference docs, or continue-anyway).
    const start = resume?.fromIndex ?? 0;
    const cids: (number | null)[] = resume ? [...resume.cids] : [];
    for (let i = start; i < staged.length; i++) {
      const entry = staged[i];
      // Already extracted, approved and sitting against this (programme,
      // broker). Re-uploading it would spend minutes re-reading the same paper
      // and leave a duplicate contract row behind, so take the id and move on.
      if (entry.kind === "existing") {
        setStep(`Using the contract already on file (${entry.name})…`);
        cids.push(entry.id);
        continue;
      }
      setStep(`Extracting contract ${i + 1}/${staged.length}…`);
      const res = await uploadOne(
        entry.file, scheduleOf(entry.name),
        i === start && resume?.continueAnyway
          ? { continueAnyway: true, resumeToken: resume?.resumeToken }
          : undefined,
      );
      if ("halt" in res) {
        // Pause the whole build here; the halt card's actions resume it from
        // this exact contract (upload the docs, or continue anyway).
        setRefsHalt({
          templateId: tid, refs: res.halt.refs, resumeToken: res.halt.resumeToken,
          multi: { index: i, cids: [...cids], contractName: entry.name },
        });
        setRefsHaltDismissed(false);
        setMsg(null);
        return;
      }
      cids.push(res.cid);
    }
    setDeferredRefs(missing);
    const primaryCid = cids.find(c => c != null) ?? null;
    setContractId(primaryCid);

    // Bind each output sheet to a contract. Priority per sheet:
    //   1) the user's explicit pick in the map-to-sheets UI (when >1 contract)
    //   2) name auto-match — a contract whose filename schedule matches the
    //      sheet's ("Palms Sch H" contract → "…Schedule H…" sheet).
    // Auto-match runs for ANY contract count, so a lone Schedule-H contract binds
    // ONLY to the H sheet and never leaks onto the others. A contract whose name
    // has no schedule stays unbound → it's the default/fallback for the format.
    const schedOfContract = staged.map(e => scheduleOf(e.name));
    const autoIdx = (sh: string): number => {
      const sched = scheduleOf(sh);
      return sched ? schedOfContract.findIndex(s => s === sched) : -1;
    };
    const map: Record<string, number> = {};
    for (const sh of outputSheetSel) {
      const idx = (staged.length > 1 && sh in sheetContractMap)
        ? sheetContractMap[sh] : autoIdx(sh);
      if (idx >= 0 && cids[idx] != null) map[sh] = cids[idx] as number;
    }

    // Input → landing + proposed mapping + format
    setStep("Mapping input → output…");
    const fIn = new FormData();
    fIn.append("mga", mga); fIn.append("file", inputFile as File);
    fIn.append("output_template_id", String(tid));
    if (primaryCid != null) fIn.append("contract_id", String(primaryCid));
    fIn.append("carrier_party_id", String(carrierId));
    fIn.append("program_id", String(programId));
    fIn.append("name", setupName);
    if (inputSheetOpts) for (const sh of inputSheetSel) fIn.append("selected_sheets", sh);
    const u = await api.post<UploadResp>(`/direct/upload`, fIn);

    // Persist the sheet→contract bindings AND repoint the format's fallback
    // contract at the one just processed — the editor reads the format's
    // contract_id to attach clauses, so without this a rebuild would keep
    // showing the PREVIOUS contract's rules.
    await api.put(`/direct/format/${u.data.format_id}`,
      primaryCid != null ? { sheet_contracts: map, contract_id: primaryCid } : { sheet_contracts: map });
    // Supplement: uploaded once here (setup-time), parsed + stored with the format.
    if (suppFile) {
      setStep("Storing supplementary data…");
      await uploadSupplement(u.data.format_id, suppFile);
      setSuppCurrentName(suppFile.name); setSuppFile(null);
    }

    const of = await api.get<{ fields: OutField[] }>(`/direct/output-fields`,
      { params: { template_id: tid, contract_id: primaryCid ?? undefined,
                  format_id: u.data.format_id } });
    applyMapping(u.data, of.data.fields, primaryCid, tid);
    // Create (or reuse) this scope's Pipeline row right away, in draft status —
    // so it shows up in the list below immediately, and the completion modal
    // has somewhere to send "Review Mapping" to. Reviewing/editing the mapping
    // itself now happens on that setup's own page, not here. Pass the freshly-
    // built values explicitly (setState above hasn't reached the closure yet).
    const pipelineContracts = [
      ...Object.entries(map).map(([sheet, cid]) => ({ contract_id: cid, sheet_key: sheet })),
      ...(primaryCid != null ? [{ contract_id: primaryCid, sheet_key: null }] : []),
    ];
    const pid = await upsertPipeline({ formatId: u.data.format_id, templateId: tid, contracts: pipelineContracts });
    refreshExisting();
    const nMapped = Object.keys(map).length;
    setMsg(nMapped > 0
      ? `Mapping proposed — ${nMapped} sheet(s) bound to their contract.`
      : "Mapping proposed.");
    // Count DISTINCT contract rules (by rule_id), not clause attachments: one
    // rule can attach to several output fields, so summing per-field clause
    // counts overstates it (e.g. 17 rules showing as 36).
    const ruleIds = new Set<number>();
    for (const f of of.data.fields) for (const c of (f.clauses ?? [])) ruleIds.add(c.rule_id);
    // Which columns the contract expects that this bordereau doesn't provide.
    // One quick server-side check over the ALREADY-extracted contract, run once
    // here and stored — so the setup's page later reads the same answer for
    // free. force: the build may have replaced the contract or the sample file,
    // which makes any earlier answer stale.
    // Deliberately best-effort: a failure here must not cost the user a build
    // that has just taken minutes, so the summary simply shows no note.
    let missingCols: MissingColumnsResp | null = null;
    if (pid != null) {
      setStep("Checking the bordereau against the contract…");
      try {
        const { data } = await api.post<MissingColumnsResp>(
          `/pipelines/${pid}/missing-columns/analyze`, null, { params: { force: true } });
        missingCols = data;
      } catch { missingCols = null; }
    }
    setBuildSummary({
      pipelineId: pid,
      inputSheets: u.data.input_sheets.length, outputSheets: u.data.output_sheets.length,
      contracts: staged.length,
      rules: ruleIds.size,
      fieldsWithRules: of.data.fields.filter(f => (f.clauses?.length ?? 0) > 0).length,
      totalFields: of.data.fields.length,
      sheetsBoundToContract: nMapped, deferredCount: missing.length,
      missing: missingCols,
      mappingReview: u.data.mapping_review ?? null,
    });
  }

  // One sentence naming what paused, used by both the dialog and the card that
  // stands in for it — a multi-contract build has to say WHICH contract stopped.
  const refsHaltTitle = refsHalt?.multi
    ? `Contract ${refsHalt.multi.index + 1} of ${staged.length} `
      + `(${refsHalt.multi.contractName}) refers to a document that wasn't provided`
    : "This contract refers to a document that wasn't provided";

  // "Upload reference document" from the halt prompt: re-run with the newly-
  // picked docs (accumulated with any provided earlier). The build resumes
  // from the HALTED contract (earlier ones aren't re-extracted).
  async function uploadReferenceForBuild(files: File[]) {
    if (!refsHalt || files.length === 0) return;
    const all = [...refFiles, ...files];
    setRefFiles(all);
    const { templateId: tid, multi } = refsHalt;
    setRefsHalt(null);
    setBuilding(true); setErr(null); setMsg(null);
    try {
      if (multi) {
        await buildContracts(tid, {
          fromIndex: multi.index, cids: multi.cids, referenceFiles: all,
        });
      }
    } catch (e: unknown) { setErr(`${step || "Build"} failed — ${errText(e)}`); }
    finally { setBuilding(false); setStep(""); }
  }

  // "Continue anyway" from the halt prompt: proceed with the contract text alone
  // (deferred clauses won't become rules). Resumes from the cached extraction.
  async function continueWithoutReferences() {
    if (!refsHalt) return;
    const { templateId: tid, resumeToken: token, multi } = refsHalt;
    setRefsHalt(null);
    setBuilding(true); setErr(null); setMsg(null);
    try {
      if (multi) {
        await buildContracts(tid, {
          fromIndex: multi.index, cids: multi.cids,
          continueAnyway: true, resumeToken: token,
        });
      }
    } catch (e: unknown) { setErr(`${step || "Build"} failed — ${errText(e)}`); }
    finally { setBuilding(false); setStep(""); }
  }

  // Assemble the pipeline from what the build produced (this input template +
  // output template + the per-sheet and fallback contracts). Reuses the
  // pipeline already bound to this input template if one exists, in draft
  // status (activating, and any further editing of the mapping, happens on
  // that setup's own edit page — this just guarantees the Pipeline row
  // exists so the setup shows up there right after a build).
  // `over` lets a just-finished build pass the freshly-produced values directly.
  // buildContracts sets up/templateId/contractId via setState right before calling
  // this, and setState doesn't update the closure synchronously — so without the
  // override the pipeline would be (re)bound to the PREVIOUS build's contract, not
  // the one just processed. The Save Draft button calls it with no args and reads
  // the (by-then-committed) state, which is correct there.
  async function upsertPipeline(over?: {
    formatId: number; templateId: number;
    contracts: { contract_id: number; sheet_key: string | null }[];
  }): Promise<number | null> {
    const fid = over?.formatId ?? up?.format_id ?? null;
    const tid = over?.templateId ?? templateId;
    if (fid == null || tid == null || carrierId === "" || programId === "") return null;
    const contracts = over?.contracts ?? [
      ...Object.entries(sheetContracts)
        .filter(([, cid]) => cid !== "" && cid != null)
        .map(([sheet, cid]) => ({ contract_id: Number(cid), sheet_key: sheet })),
      ...(contractId != null ? [{ contract_id: contractId, sheet_key: null }] : []),
    ];
    const existing = pipelines.find(p => p.input_format_id === fid);
    if (existing) {
      await api.put(`/pipelines/${existing.id}`, {
        input_format_id: fid, output_template_id: tid, contracts,
      });
      return existing.id;
    }
    const { data } = await api.post<{ id: number }>(`/pipelines`, {
      name: setupName, carrier_party_id: carrierId, program_id: programId,
      // Null when no broker was picked — which is what every setup built
      // before this looked like, so nothing existing changes.
      broker_party_id: scope.brokerPartyId === "" ? null : Number(scope.brokerPartyId),
      input_format_id: fid, output_template_id: tid, contracts,
    }, { params: { mga } });
    return data.id;
  }

  // The setup currently built/loaded in this scope — matched by its input
  // template (DirectFormat). Drives the action bar below (Save / Activate /
  // Delete), so those buttons always act on the setup on screen, never a guess.
  const currentPipeline = up?.format_id != null
    ? pipelines.find(p => p.input_format_id === up.format_id) ?? null
    : null;

  // Re-persist the built/loaded setup as a draft (mapping is already stored by
  // the build; this just guarantees the Pipeline row is up to date and gives
  // the user an explicit "it's saved" action). Idempotent.
  async function saveDraft() {
    setBusy(true); setErr(null); setMsg(null);
    try {
      await upsertPipeline();
      refreshExisting();
      setMsg("Saved as draft.");
    } catch (e: unknown) { setErr(errText(e)); } finally { setBusy(false); }
  }

  // Discard the built/loaded setup. Deleting its input template cascades to the
  // Pipeline row. An active setup can't be deleted — activate another first.
  async function deleteBuiltSetup() {
    const fid = up?.format_id;
    if (fid == null) return;
    if (currentPipeline?.status === "active") {
      setErr("This setup is active — activate a different setup for this carrier + program before deleting it.");
      return;
    }
    if (!window.confirm("Discard this setup? This cannot be undone.")) return;
    setBusy(true); setErr(null); setMsg(null);
    try {
      await api.delete(`/direct/format/${fid}`);
      setBuildSummary(null);
      resetEditor();
      refreshExisting();
      setMsg("Setup deleted.");
    } catch (e: unknown) { setErr(errText(e)); } finally { setBusy(false); }
  }

  return (
    <>
      {building && (
        <LoadingOverlay label={buildLabel(step)} />
      )}
      {/* Build-info popup disabled for now — flip `false` back to `buildInfoOpen`
          (and re-enable setBuildInfoOpen(true) in buildSetup) to restore it.
          Stacked above the loading overlay (z-1000) so it's readable over the blur. */}
      {false && (
        <div className="relative z-[1100]">
          <Modal open={buildInfoOpen} onClose={() => setBuildInfoOpen(false)}
            title="Building Your Setup" size="md"
            footer={<Button onClick={() => setBuildInfoOpen(false)}>Got It</Button>}>
            <div className="text-sm text-ink-muted space-y-2">
              <p>Generating the template, extracting the contract clauses and proposing the
                 mapping takes <b className="text-ink">as long as the contract needs</b> —
                 minutes for a short schedule, considerably longer for a full treaty.</p>
              <p>Please keep this page open — the progress is shown on the loader,
                 and the proposed mapping will appear below when it finishes.</p>
            </div>
          </Modal>
        </div>
      )}
      {/* Widened only when there is a missing-column list to read — column names
          plus their reason are unreadable at the summary-only width, and the
          summary-only case keeps exactly the width it had. */}
      <Modal open={!!buildSummary} onClose={() => setBuildSummary(null)}
        title="Mapping Generated"
        size={buildSummary?.missing?.items.length ? "2xl" : "md"}
        footer={
          <div className="flex items-center gap-2">
            {/* Review the mapping on the setup's own page … */}
            <Button variant="secondary" onClick={() => {
              const pid = buildSummary?.pipelineId;
              setBuildSummary(null);
              if (pid != null) navigate(editHrefFor(pid));
            }}>
              Review Mapping
            </Button>
            {/* … or activate the setup right here, without leaving this screen.
                A fresh build is a draft; activating supersedes any previous active
                setup for this carrier + program. (Also available in the bar below
                and the setups list.) */}
            {(() => {
              const bsPid = buildSummary?.pipelineId ?? null;
              const bsPipe = bsPid != null ? pipelines.find(p => p.id === bsPid) : null;
              if (bsPipe?.status === "active") {
                return <span className="inline-flex items-center gap-1 text-sm text-emerald-700 font-medium px-2">
                  <CheckCircle2 size={15} /> Active
                </span>;
              }
              return <Button disabled={busy || bsPid == null} onClick={() => {
                setBuildSummary(null);
                if (bsPid != null) activateExisting(bsPid);
              }}>
                <CheckCircle2 size={15} /> Activate Setup
              </Button>;
            })()}
          </div>
        }>
        {buildSummary && (
          <div className="text-center">
            <div className="mx-auto mb-3 grid h-14 w-14 place-items-center rounded-full bg-emerald-50 text-emerald-600">
              <CheckCircle2 size={28} />
            </div>
            <p className="text-sm text-ink-muted mb-4">
              <span className="font-semibold text-ink">{setupName}</span> is mapped and ready to review.
            </p>
            <div className="grid grid-cols-2 gap-2.5 text-left">
              <div className="rounded-lg border border-border p-3">
                <div className="text-[20px] font-semibold text-ink">{buildSummary.outputSheets}</div>
                <div className="text-[11px] text-ink-muted">
                  Output Sheet{buildSummary.outputSheets === 1 ? "" : "s"} From{" "}
                  {buildSummary.inputSheets} Input Sheet{buildSummary.inputSheets === 1 ? "" : "s"}
                </div>
              </div>
              <div className="rounded-lg border border-border p-3">
                <div className="text-[20px] font-semibold text-ink">{buildSummary.rules}</div>
                <div className="text-[11px] text-ink-muted">
                  Contract Rule{buildSummary.rules === 1 ? "" : "s"} Across{" "}
                  {buildSummary.fieldsWithRules} Of {buildSummary.totalFields} Fields
                </div>
              </div>
              <div className="rounded-lg border border-border p-3">
                <div className="text-[20px] font-semibold text-ink">{buildSummary.contracts}</div>
                <div className="text-[11px] text-ink-muted">
                  Contract{buildSummary.contracts === 1 ? "" : "s"} Processed
                </div>
              </div>
              <div className="rounded-lg border border-border p-3">
                <div className="text-[20px] font-semibold text-ink">{buildSummary.sheetsBoundToContract}</div>
                <div className="text-[11px] text-ink-muted">
                  Sheet{buildSummary.sheetsBoundToContract === 1 ? "" : "s"} Bound To A Contract
                </div>
              </div>
            </div>
            {/* The mapping ladder's verdict. A required output column with no
                confident source will be EMPTY in the delivered file, so it is
                said here rather than found later. */}
            {buildSummary.mappingReview && (
              <div className="mt-4 text-left">
                <MappingReview review={buildSummary.mappingReview} compact />
              </div>
            )}

            {buildSummary.deferredCount > 0 && (
              <div className="mt-3 flex items-start gap-2 rounded-lg bg-amber-50 text-amber-800 text-xs px-3 py-2.5 text-left">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                <span>
                  {buildSummary.deferredCount} contract{buildSummary.deferredCount === 1 ? "" : "s"}{" "}
                  refer{buildSummary.deferredCount === 1 ? "s" : ""} to a missing reference document —
                  see the notice below the mapping.
                </span>
              </div>
            )}

            {/* NOTE: what the contract asks for that this bordereau doesn't
                provide. Every column is listed (the list scrolls inside its own
                box, so a long one can't push the actions out of reach), and the
                same note is kept on the setup's own page. */}
            {buildSummary.missing && buildSummary.missing.items.length > 0 && (
              <div className="mt-4 text-left">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <FileWarning size={15} className="shrink-0 text-amber-600" />
                  <span className="text-sm font-semibold text-ink">
                    Note · {buildSummary.missing.counts.total} Column
                    {buildSummary.missing.counts.total === 1 ? "" : "s"} May Be Missing From Your Bordereau
                  </span>
                  {buildSummary.missing.counts.required > 0 && (
                    <span className="pill pill-red">{buildSummary.missing.counts.required} Required</span>
                  )}
                  {buildSummary.missing.counts.recommended > 0 && (
                    <span className="pill pill-amber">
                      {buildSummary.missing.counts.recommended} Recommended</span>
                  )}
                </div>
                <MissingColumnsList items={buildSummary.missing.items} maxHeight="15rem" />
                <p className="mt-2 text-[11px] text-ink-soft">
                  This note stays on the setup — you can review it any time from Configured
                  Bordereau Setups.
                </p>
              </div>
            )}
            {/* The DERIVED half: clauses the extraction wanted a rule for but
                could not bind to a column. No model call behind these, so they
                show even when the check above couldn't run. Resolved from the
                setup's own page, which is where the column picker lives. */}
            {(buildSummary.missing?.unmapped_clauses?.length ?? 0) > 0 && (
              <div className="mt-4 text-left">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <FileWarning size={15} className="shrink-0 text-sky-600" />
                  <span className="text-sm font-semibold text-ink">
                    {buildSummary.missing!.unmapped_clauses!.length} Clause
                    {buildSummary.missing!.unmapped_clauses!.length === 1 ? "" : "s"} Awaiting a Column
                  </span>
                </div>
                <UnmappedClausesList items={buildSummary.missing!.unmapped_clauses!}
                  maxHeight="15rem" />
                <p className="mt-2 text-[11px] text-ink-soft">
                  Pick a column for each on the setup's page to generate its rule.
                </p>
              </div>
            )}
            {buildSummary.missing?.analyzed && buildSummary.missing.items.length === 0
              && (buildSummary.missing.unmapped_clauses?.length ?? 0) === 0 && (
              <div className="mt-3 flex items-start gap-2 rounded-lg bg-emerald-50 text-emerald-700 text-xs px-3 py-2.5 text-left">
                <CheckCircle2 size={14} className="mt-0.5 shrink-0" />
                <span>
                  Every contract clause is mapped to a column, and your bordereau provides
                  everything the contract asks for.
                </span>
              </div>
            )}
          </div>
        )}
      </Modal>
      <PageHeader title="Bordereau Setup"
        subtitle="Do this once for each carrier and program: upload the output template, the contract and a sample bordereau. Kavachio learns how your data maps to the output — after that, your team just uploads each new bordereau and gets a validated file back." />
      <PageBody>
        {err && <Banner kind="error"><AlertTriangle size={15} /> {err}</Banner>}

        <Modal open={multiTableModal != null} size="xl"
          title={<span className="flex items-center gap-2">
            <AlertTriangle size={17} className="text-amber-500" /> Multiple Tables Found
          </span>}
          onClose={() => setMultiTableModal(null)}
          footer={<Button variant="danger" onClick={() => setMultiTableModal(null)}>Got It</Button>}>
          <p className="text-sm text-ink">{multiTableModal}</p>
        </Modal>

        {/* Both sides, before the columns can be decided. */}
        <Modal open={createGate != null} size="lg"
          title={<span className="flex items-center gap-2">
            <AlertTriangle size={17} className="text-amber-500" />
            Upload {(createGate ?? []).join(" and ")} first
          </span>}
          onClose={() => setCreateGate(null)}
          footer={<Button onClick={() => setCreateGate(null)}>Got It</Button>}>
          <div className="space-y-2 text-sm text-ink">
            <p>
              The output BDX template is worked out from two things, and{" "}
              {(createGate ?? []).length === 1
                ? `${createGate?.[0]} is missing.`
                : "neither is here yet."}
            </p>
            <ul className="list-disc pl-5 space-y-1 text-ink-muted">
              <li>
                <b>The input template</b> — a sample of the bordereau you
                receive. A reporting standard publishes hundreds of columns per
                territory and most of them will not apply to this binder; your
                own file is what says which ones can actually be filled.
              </li>
              <li>
                <b>The contract</b> — what this binder is obliged to report.
                Its terms are read to work out the columns the standard does not
                cover, and to keep the ones it does.
              </li>
            </ul>
            <p className="text-ink-muted">
              Add {(createGate ?? []).join(" and ")} above, then try again.
            </p>
          </div>
        </Modal>
        {msg && <Banner kind="ok"><CheckCircle2 size={15} /> {msg}</Banner>}

        {justCreated && (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3.5
            flex items-start gap-3">
            <CheckCircle2 size={17} className="text-emerald-600 mt-0.5 shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-emerald-900">
                Output template “{justCreated.name}” created
              </div>
              <p className="text-[12.5px] text-emerald-800 mt-0.5 leading-relaxed">
                Open it to see the file it produces as a spreadsheet, check the
                columns and their sources, and deal with anything the standard
                requires that your bordereau does not carry. Your uploads here
                stay put while you do.
              </p>
              <div className="flex gap-2 mt-2">
                <Button variant="secondary"
                  onClick={() => navigate(`/outputs/templates/${justCreated.id}`)}>
                  Review the columns <ArrowRight size={14} />
                </Button>
                <Button variant="ghost" onClick={() => setJustCreated(null)}>
                  Later — build the setup
                </Button>
              </div>
            </div>
          </div>
        )}

        <Card title="1 · Scope & Uploads">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-2xl">
            {/* The carrier is who you are, not a choice. Shown so the scope
                is unambiguous, but there is nothing here to pick. */}
            <Field label="Carrier">
              <div className="input flex items-center bg-surface-2 text-ink-muted">
                {carrierName || mga}
              </div>
            </Field>
            <Field label="Program">
              <Select value={creatingProgram ? "__new__" : programId} onChange={e => {
                if (e.target.value === "__new__") {
                  // Clear the loaded setup + contracts for the previously-selected
                  // program. setProgramId("") cascades through the scope effects
                  // (resetEditor + refreshExisting/refreshContracts run on it).
                  setCreatingProgram(true);
                  setProgramId("");
                  return;
                }
                setCreatingProgram(false);
                setProgramId(e.target.value ? Number(e.target.value) : "");
              }}>
                <option value="">Select Program…</option>
                {programs.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                <option value="__new__">➕ Add New Program…</option>
              </Select>
            </Field>

            {/* The broker. The contract follows from it rather than being asked
                for again — see BrokerContractScope for why. */}
            <BrokerSelect scope={scope} disabled={scopeIncomplete} />
          </div>
          <div className="max-w-2xl mt-3">
            <BoundContracts scope={scope} programPicked={programId !== ""} />
          </div>

          {/* Why everything below is shut. Said once, where the answer is —
              not repeated on each of the five upload boxes. */}
          {noBrokerYet && (
            <div className="max-w-2xl mt-3 rounded-md border border-amber-300
              bg-amber-50 px-3 py-2.5 text-[12px] text-amber-800 leading-relaxed">
              <div className="font-medium flex items-center gap-1.5">
                <AlertTriangle size={14} /> No broker on this programme yet
              </div>
              <p className="mt-1">
                A bordereau arrives from a broker, under a contract you have
                approved — so there is nobody for this setup to be for until one
                is on the programme. Put a broker on it from the{" "}
                <button type="button" className="underline font-medium"
                  onClick={() => navigate("/users/new")}>Users &amp; Roles</button>{" "}
                screen and the uploads below open up. A programme that already
                has a saved setup is not held shut this way — those predate the
                broker level and stay editable.
              </p>
            </div>
          )}

          {creatingProgram && (
            <div className="mt-4 max-w-3xl rounded-lg border border-border bg-surface-2 p-4 space-y-3">
              <div className="text-sm font-medium">Add New Program</div>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                <Field label="Program Name *">
                  <TextInput autoFocus value={programForm.name} placeholder="e.g. Property Binder 2025"
                    onChange={e => setProgramField("name", e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") createProgram(); }} />
                </Field>
                {/* <Field label="Lead carrier">
                  <TextInput value={programForm.lead_carrier} placeholder="Optional"
                    onChange={e => setProgramField("lead_carrier", e.target.value)} />
                </Field>
                <Field label="Admin party">
                  <TextInput value={programForm.admin_party} placeholder="Optional"
                    onChange={e => setProgramField("admin_party", e.target.value)} />
                </Field> */}
                <Field label="BDX Frequency">
                  <Select value={programForm.bdx_frequency}
                    onChange={e => setProgramField("bdx_frequency", e.target.value)}>
                    <option value="">—</option>
                    {BDX_FREQUENCIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </Select>
                </Field>
                {/* <Field label="Business segment">
                  <TextInput value={programForm.business_segment} placeholder="Optional"
                    onChange={e => setProgramField("business_segment", e.target.value)} />
                </Field>
                <Field label="Product line">
                  <TextInput value={programForm.product_line} placeholder="Optional"
                    onChange={e => setProgramField("product_line", e.target.value)} />
                </Field>
                <Field label="Distribution channel">
                  <TextInput value={programForm.distribution_channel} placeholder="Optional"
                    onChange={e => setProgramField("distribution_channel", e.target.value)} />
                </Field>
                <Field label="Territory">
                  <TextInput value={programForm.territory} placeholder="Optional"
                    onChange={e => setProgramField("territory", e.target.value)} />
                </Field> */}
                <Field label="Status">
                  <Select value={programForm.status}
                    onChange={e => setProgramField("status", e.target.value)}>
                    <option value="active">Active</option>
                    <option value="inactive">Inactive</option>
                  </Select>
                </Field>
              </div>
              <div className="flex gap-2">
                <Button onClick={createProgram} disabled={createBusy || !programForm.name.trim()}>
                  Add Program
                </Button>
                <Button variant="ghost"
                  onClick={() => { setCreatingProgram(false); setProgramForm(EMPTY_PROGRAM); }}>Cancel</Button>
              </div>
            </div>
          )}
          {carrierId !== "" && programId !== "" && (
            <p className="text-xs text-ink-muted mt-2">
              Setup Name: <span className="font-medium text-ink">{setupName}</span>
            </p>
          )}

          {/* Data uploads — the templates and the sample they're mapped from.
              Three across on a wide screen (full width, no cell left hanging),
              two on a tablet, stacked on a phone. Three only from xl: the sidebar
              eats ~200px, so 3-up below that leaves the hints too cramped to read. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4 mt-4 items-start">
            <div className="space-y-2">
              <FilePick label="Input Template" icon={<FileUp size={15} />} file={inputFile} tone="sky" required
                onPick={f => pickFileWithSheets("input", f)} hint="A representative input sample"
                disabled={scopeIncomplete} />
              <SheetPicker kind="input" options={inputSheetOpts}
                selected={inputSheetSel} onToggle={n => toggleSheet("input", n)}
                hint="Only the checked sheets are mapped to the output." />
            </div>
            <div className="space-y-2">
              <FilePick label="Output Template" icon={<FileOut size={15} />} file={outFile} tone="sky"
                required={!resolved?.template}
                onPick={f => pickFileWithSheets("output", f)}
                hint={SHOW_BDX_TEMPLATE_BUILDER
                  ? "Upload the layout you have been asked for, or build one here"
                  : "Upload the layout you have been asked for"}
                // No altAction when the builder is off, which puts the card back
                // to opening the file dialog wherever you click it.
                altAction={SHOW_BDX_TEMPLATE_BUILDER ? {
                  label: "Create BDX Template",
                  onClick: openCreateTemplate,
                  hint: "Built from a reporting standard or the contract, and "
                      + "checked against your bordereau",
                } : undefined}
                disabled={scopeIncomplete} />
              <SheetPicker kind="output" options={outputSheetOpts}
                selected={outputSheetSel} onToggle={n => toggleSheet("output", n)}
                hint="The generated output will contain only the checked sheets." />
              {/* The "not configured" case is NOT reported here any more: the
                  box above now carries both ways to fix it, and saying it twice
                  read as a fault rather than as a choice.

                  The whole box is off while the builder is: it names the
                  template a scope already has, says which way it was built and
                  links into the field editor, all of which belong to the flow
                  being kept off screen. Note that it also carries two real
                  warnings — a template inherited from a broader scope, and a
                  setup built against a different template — so those go quiet
                  too until the flag comes back on. */}
              {SHOW_BDX_TEMPLATE_BUILDER && (
                <OutputTemplateState
                  resolving={resolving} resolved={resolved}
                  disabled={scopeIncomplete}
                  uploading={!!outFile}
                  hideMissing
                  onCreate={openCreateTemplate}
                  onOpen={id => navigate(`/outputs/templates/${id}`)} />
              )}
            </div>

            {/* Supplementary data — optional, uploaded ONCE here like the templates.
                Stored with the setup and captured alongside the BDX on every run;
                never asked for again at run time. */}
            <div className="space-y-2">
              <FilePick label="Supplementary Data (Optional)" icon={<FileUp size={15} />} tone="sky"
                file={suppFile} onPick={setSuppFile}
                hint="Extra data captured alongside the BDX on every run — uploaded once here"
                disabled={scopeIncomplete} />
              {suppCurrentName && !suppFile && (
                <div className="text-xs text-ink-muted flex items-center gap-2">
                  Stored: <b className="truncate min-w-0 flex-1">{suppCurrentName}</b>
                  <button className="text-red-400 hover:text-red-600 shrink-0" onClick={removeSupplement}>Remove</button>
                </div>
              )}
              {up?.format_id && suppFile && (
                <Button onClick={saveSupplementNow} disabled={savingSupp}>
                  <Save size={14} /> Save Supplement
                </Button>
              )}
            </div>
          </div>

          {/* Contract documents — its own group: the contract, plus anything the
              contract defers out to. Two across, full width; stacked on a phone. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-4 items-start">
            {/* Contracts — REQUIRED. One contract applies to every sheet; with more
                than one, map each schedule sheet to its contract below. */}
            <div className="space-y-2">
              <ContractPick
                files={contractFiles}
                /* Already approved for this programme and broker — shown here
                   so the required field is satisfied without asking for the
                   same document twice. Removable, because replacing a contract
                   with a newer one is a real thing to want. */
                existing={reusedContracts.map(c => ({
                  id: c.id, name: c.filename || `Contract ${c.id}`,
                  from: c.broker_name,
                }))}
                onRemoveExisting={id =>
                  setReusedContracts(cs => cs.filter(c => c.id !== id))}
                loadingExisting={scope.contractsLoading}
                /* Contracts DO exist for this programme, they just belong to a
                   broker nobody has picked. Without this the field reads as
                   "nothing on file" and the next thing someone does is upload a
                   second copy of a contract already approved. */
                awaitingBroker={scope.awaitingBroker.length > 0
                  ? { contracts: scope.awaitingBroker.length,
                      brokers: scope.awaitingBrokerCount }
                  : null}
                onAdd={fs => setContractFiles(cs => {
                  const seen = new Set(cs.map(c => `${c.name}|${c.size}`));
                  return [...cs, ...fs.filter(f => !seen.has(`${f.name}|${f.size}`))];
                })}
                onRemoveAt={i => setContractFiles(cs => cs.filter((_, j) => j !== i))}
                disabled={scopeIncomplete}
              />
            </div>

            {/* Reference document(s) — optional (Path A). External docs the contract
                defers to ("per the Purchasing Guidelines on file"); their text is fed
                into extraction so deferred clauses (e.g. Authorized / Excluded Classes
                of Business) resolve into real rules. If omitted and the contract
                defers, the build pauses below and asks for them (Path B). */}
            <div className="space-y-2">
              <ReferencePick
                files={refFiles}
                onAdd={fs => setRefFiles(prev => [...prev, ...fs])}
                onRemoveAt={i => setRefFiles(prev => prev.filter((_, j) => j !== i))}
                disabled={scopeIncomplete}
              />
            </div>
          </div>

          {!up?.format_id && !building && staged.length > 1 && outputSheetOpts && outputSheetSel.size > 0 && (
            <div className="mt-4">
              <div className="text-sm font-medium mb-1">Map Contracts to Schedule Sheets</div>
              <p className="text-xs text-ink-muted mb-2">
                Pre-filled by name (e.g. a “Sch H” contract → the “Schedule H” sheet).
                One contract can cover several sheets — just pick it on each. Leave a
                sheet on <b>No Contract</b> to skip contract validation for it.
              </p>
              <div className="grid grid-cols-1  gap-2 max-w-3xl">
                {[...outputSheetSel].map(sh => {
                  const auto = staged.findIndex(
                    e => scheduleOf(e.name) && scheduleOf(e.name) === scheduleOf(sh));
                  return (
                    <div key={sh} className="flex items-center gap-2 text-sm">
                      <span className="w-48 truncate">{sh}</span>
                      <select className="text-xs px-2 py-1 rounded border border-border bg-white flex-1"
                        value={sheetContractMap[sh] ?? auto}
                        onChange={e => setSheetContractMap(m => ({ ...m, [sh]: Number(e.target.value) }))}>
                        <option value={-1}>— No Contract —</option>
                        {staged.map((e, i) => (
                          <option key={i} value={i}>
                            {e.name}{e.kind === "existing" ? " (on file)" : ""}
                          </option>))}
                      </select>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="mt-4 flex items-center gap-3">
            {/* Deliberately NOT gated on having an output template: without one
                the click OFFERS the two ways to create it (see buildSetup), which
                is the whole point. Gating it here would leave a user staring at a
                dead button with no way to find out what is missing. */}
            <Button onClick={buildSetup}
              disabled={building || !inputFile || staged.length === 0}
              title={!building && (!inputFile || staged.length === 0)
                ? "Upload the input template and at least one contract to continue"
                : (!building && !outFile && !resolved?.template
                    ? "No output template yet — you'll be offered the two ways to create one"
                    : undefined)}
              className="!px-5 !py-2.5 !text-[13.5px] !rounded-lg
                shadow-md hover:shadow-lg hover:brightness-110 transition disabled:hover:brightness-100
                disabled:shadow-none">
              {"Set Up Bordereau Pipeline"}
            </Button>
          </div>

          {pipelines.length > 0 && (
            <div className="mt-3 rounded-md border border-border p-3">
              <div className="flex items-center gap-1.5 text-sm font-medium mb-2">
                {/* The heading names the scope it is actually showing. Saying
                    "Carrier + Program" while the list is narrowed to a broker
                    is how someone concludes a setup has gone missing. */}
                <ShieldCheck size={15} />{" "}
                {scope.brokerName
                  ? <>Saved Setups for This Programme + {scope.brokerName}</>
                  : <>Programme-wide Saved Setups</>}
                <span className="text-[11px] rounded-full px-2 py-0.5 bg-surface-2 text-ink-muted">{pipelines.length}</span>
                <InfoTip text={scope.brokerName
                  ? `Setups built for ${scope.brokerName} on this programme, plus any programme-wide setup that also covers them. A setup is built against a contract and a contract belongs to one broker, so another broker's setups are not listed here. The active one loads automatically. Activating a setup replaces whichever was previously active for the same scope.`
                  : "Setups on this programme that are not tied to a broker — they cover everyone on it. Pick a broker above to see the setups built on their contract. The active setup for the current scope loads automatically, and activating one replaces whichever was previously active for that same scope."} />
              </div>
              <ul className="space-y-1.5">
                {pipelines.map(p => (
                  <li key={p.id} className="flex items-center gap-2 text-sm">
                    <span className="font-medium">{p.name}</span>
                    <span className={`text-[11px] rounded-full px-2 py-0.5 ${p.status === "active"
                      ? "bg-emerald-100 text-emerald-700" : "bg-surface-2 text-ink-muted"}`}>
                      {p.status === "active" ? "Active" : p.status === "superseded" ? "Superseded" : "Draft"}</span>
                    {/* Which of the two kinds this is. Without it a programme-wide
                        setup sitting beside a broker's own looks identical, and
                        activating the wrong one is silent. */}
                    <span className="text-[11px] text-ink-soft">
                      {p.broker_name ?? "programme-wide"}
                    </span>
                    {/* <span className="text-xs text-ink-muted">#{p.id}</span> */}
                    <div className="ml-auto flex gap-1.5">
                      <Button variant="ghost" className="!py-1"
                        onClick={() => navigate(editHrefFor(p.id))}>Open / Edit</Button>
                      {p.status !== "active" && (
                        <Button variant="ghost" className="!py-1" disabled={busy || !p.ready}
                          title={p.ready ? undefined : p.ready_reason}
                          onClick={() => activateExisting(p.id)}>Activate</Button>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Deferred external references (multi-contract, non-halting flow):
              contract(s) name documents that weren't attached — rules from those
              documents were NOT generated. Fewer clauses/rules than expected is
              THIS, not a mapping failure. */}
          {deferredRefs.length > 0 && (
            <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4">
              <div className="flex items-center gap-2 text-sm font-semibold text-amber-800">
                <ShieldAlert size={16} /> Some rules were NOT generated — contracts refer to missing document(s)
              </div>
              <ul className="mt-2 space-y-1 text-xs text-amber-800/90">
                {deferredRefs.map((d, i) => (
                  <li key={i}>
                    <b>{d.contract}</b> refers to: {d.refs.join(", ")}
                  </li>
                ))}
              </ul>
              <p className="text-xs text-amber-800/90 mt-2">
                Attach these under <b>Reference document(s)</b> above and click
                <b> Set Up Bordereau Pipeline</b> again — their clauses then become real rules.
              </p>
            </div>
          )}

          {/* Reference-document halt (Path B): after extraction, the contract was
              found to defer rules to an external document that wasn't provided.
              The decision is asked as a MODAL — the build is paused on it, and an
              inline card competes with the page it is blocking. Dismissing the
              modal doesn't strand the build: the same card renders here instead,
              and re-opens the dialog. */}
          {refsHalt && refsHaltDismissed && (
            <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4">
              <div className="flex items-center gap-2 text-sm font-semibold text-amber-800">
                <ShieldAlert size={16} /> {refsHaltTitle} — build paused
              </div>
              <p className="text-xs text-amber-800/90 mt-1">
                Upload the document(s) so those rules can be generated, or continue without them.
              </p>
              <div className="mt-3">
                <Button variant="secondary" disabled={building}
                  onClick={() => setRefsHaltDismissed(false)}>
                  Choose
                </Button>
              </div>
            </div>
          )}
        </Card>

        {/* The reference-document decision. Both actions resume the paused build
            from the contract that halted — earlier contracts are not re-extracted. */}
        <Modal
          open={!!refsHalt && !refsHaltDismissed}
          size="xl"
          title={<span className="flex items-center gap-2">
            <ShieldAlert size={17} className="text-amber-500" /> Reference Document Required
          </span>}
          onClose={() => setRefsHaltDismissed(true)}
          footer={
            <div className="flex items-center gap-2">
              <Button variant="ghost" disabled={building}
                onClick={continueWithoutReferences}>
                Continue Without It
              </Button>
              <Button disabled={building}
                onClick={() => refHaltInputRef.current?.click()}>
                <FileUp size={14} /> Upload Reference Document
              </Button>
              <input ref={refHaltInputRef} hidden type="file" multiple
                accept=".pdf,.docx,.doc,.txt,.xlsx,.xls,.csv"
                onChange={e => {
                  const fs = Array.from(e.target.files || []);
                  if (refHaltInputRef.current) refHaltInputRef.current.value = "";
                  if (fs.length) uploadReferenceForBuild(fs);
                }} />
            </div>
          }>
          <div className="text-sm text-ink">
            <p className="font-medium">{refsHaltTitle}.</p>
            <p className="mt-1 text-[13px] text-ink-muted leading-snug">
              Some rules — authorized and excluded classes of business, for example — are
              defined in the document{(refsHalt?.refs.length ?? 0) > 1 ? "s" : ""} below rather
              than in the contract itself. Upload {(refsHalt?.refs.length ?? 0) > 1 ? "them" : "it"} so
              those rules can be generated, or continue without
              {(refsHalt?.refs.length ?? 0) > 1 ? " them" : " it"} — the contract's own rules are
              built either way.
              {refsHalt?.multi && <> The remaining contract(s) continue after your choice.</>}
            </p>
            <ul className="mt-3 space-y-2">
              {refsHalt?.refs.length === 0 && (
                <li className="text-[13px] text-ink-muted">(no document name detected)</li>
              )}
              {(refsHalt?.refs ?? []).map((r, i) => (
                <li key={i} className="rounded-lg border border-amber-200 bg-amber-50/60 px-3 py-2.5">
                  <div className="flex items-start gap-2">
                    <AlertTriangle size={14} className="mt-0.5 shrink-0 text-amber-600" />
                    <div className="min-w-0">
                      <div className="font-medium break-words">
                        {r.document_name || "(unnamed reference)"}
                        {r.version_or_date && (
                          <span className="font-normal text-ink-muted"> · {r.version_or_date}</span>
                        )}
                      </div>
                      {r.source_texts?.[0] && (
                        <p className="mt-0.5 text-[12px] italic leading-snug text-ink-muted">
                          “{r.source_texts[0]}”
                        </p>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
            {refFiles.length > 0 && (
              <p className="mt-3 text-[12px] text-ink-muted">
                Already provided: <span className="text-ink">{refFiles.map(f => f.name).join(", ")}</span>
              </p>
            )}
          </div>
        </Modal>

        {/* Sheet-role review — the build pauses here after the output template is
            created so the user can confirm/override the AI's data-vs-reference
            classification before any rules are generated. */}
        <Modal
          open={!!sheetReview}
          size="2xl"
          title={<span className="flex items-center gap-2">
            <FileSpreadsheet size={16} className="text-navy" /> Review Sheet Classification
          </span>}
          onClose={() => setSheetReview(null)}
          footer={
            <div className="flex items-center justify-between gap-3 w-full">
              <span className="text-xs text-ink-muted">
                {sheetReview?.structure.sheets.filter(sh => !isNonDataSheet(sh)).length ?? 0} Data ·{" "}
                {sheetReview?.structure.sheets.filter(isRefSheet).length ?? 0} Reference ·{" "}
                {sheetReview?.structure.sheets.filter(isSummarySheet).length ?? 0} Summary
              </span>
              <div className="flex items-center gap-2">
                <Button variant="ghost" onClick={() => setSheetReview(null)}>Cancel</Button>
                <Button onClick={proceedAfterSheetReview} disabled={building}>
                  Looks Right — Generate Rules <ArrowRight size={14} />
                </Button>
              </div>
            </div>
          }>
          <p className="text-sm text-ink-muted">
            The AI classified each tab from its column headers.{" "}
            <strong>Data</strong> tabs are validated (rules are generated &amp; run on them).{" "}
            <strong>Reference</strong> tabs are lookup / mapping tables, and{" "}
            <strong>Summary</strong> tabs roll up totals from the data tabs — both are{" "}
            <strong>excluded from rule generation</strong>. Fix any that are wrong, then proceed.
          </p>
          <div className="mt-3 space-y-1.5">
            {sheetReview?.structure.sheets.map((sh, i) => {
              const bucket = sheetBucket(sh);
              return (
                <div key={sh.sheet_name}
                     className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
                  <div className="min-w-0">
                    <span className="font-mono text-[13px]">{sh.sheet_name}</span>
                    {sh.sheet_role_reason && (
                      <p className="text-xs text-ink-muted mt-0.5 line-clamp-2">{sh.sheet_role_reason}</p>
                    )}
                  </div>
                  <div className="flex items-center rounded-md border border-border overflow-hidden shrink-0">
                    <button onClick={() => setReviewRole(i, "data")}
                      className={`px-3 py-1 text-xs font-medium transition ${
                        bucket === "data" ? "bg-emerald-600 text-white" : "hover:bg-surface-2 text-ink-muted"}`}>
                      Data
                    </button>
                    <button onClick={() => setReviewRole(i, "reference")}
                      className={`px-3 py-1 text-xs font-medium transition ${
                        bucket === "reference" ? "bg-slate-500 text-white" : "hover:bg-surface-2 text-ink-muted"}`}>
                      Reference
                    </button>
                    <button onClick={() => setReviewRole(i, "summary")}
                      className={`px-3 py-1 text-xs font-medium transition ${
                        bucket === "summary" ? "bg-sky-600 text-white" : "hover:bg-surface-2 text-ink-muted"}`}>
                      Summary
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </Modal>

        {/* Not mounted at all while the builder is off, so nothing of it can
            be reached by a stray state change during a demo. */}
        {SHOW_BDX_TEMPLATE_BUILDER && <CreateOutputTemplate
          open={showCreateTemplate}
          onClose={() => setShowCreateTemplate(false)}
          mga={mga}
          programId={programId === "" ? 0 : Number(programId)}
          carrierPartyId={carrierId === "" ? null : Number(carrierId)}
          brokerPartyId={scope.brokerPartyId === "" ? null : Number(scope.brokerPartyId)}
          contractId={scope.contractId === "" ? null : Number(scope.contractId)}
          scopeNames={{
            carrier: carrierName || mga,
            programme: programs.find(p => p.id === programId)?.name ?? null,
            broker: scope.brokerName,
            contract: scope.contractName,
          }}
          // Both sides of the job. The bordereau says which of a territory's
          // published columns can actually be filled; the contracts say what
          // has to be reported — including one staged here and not uploaded
          // yet, which is the ordinary case on a first setup.
          inputFile={inputFile}
          inputSheets={[...inputSheetSel]}
          contractFiles={contractFiles}
          boundContracts={scope.boundContracts}
          onCreated={t => {
            setShowCreateTemplate(false);
            setTemplateId(t.id);
            // Ask the server again rather than assemble the answer here: it is
            // the only thing that knows which setup would run against it.
            setResolveTick(n => n + 1);
            setMsg(null);
            // The uploads staged here are NOT thrown away by reviewing the
            // template — the card below links out and the user comes back to
            // the same screen with the same files attached.
            setJustCreated({ id: t.id, name: t.name });
          }} />}

        {/* Save / Activate / Delete for the setup just built (or loaded) in this
            scope. Editing the mapping itself still happens on the setup's own
            page (the "Open / Edit" button) — this bar only persists, activates
            or discards it, so the user never has to leave to save or delete.
            `fixed` (not `sticky`) so it never shifts as content above it
            resizes (loading states resolving, panels expanding/collapsing,
            etc.) — `left` tracks the sidebar's current width via the
            --sidebar-w var Layout.tsx keeps in sync with collapsed/expanded. */}
        {currentPipeline && (
          <>
            <div className="fixed bottom-0 right-0 z-30 px-8 py-3 bg-white/95 backdrop-blur
              border-t border-border shadow-[0_-1px_8px_rgba(17,24,39,0.06)]"
              style={{ left: "var(--sidebar-w, 256px)" }}>
              <div className="flex flex-wrap items-center gap-2">
                {currentPipeline.status !== "active" && (
                  <Button onClick={() => activateExisting(currentPipeline.id)}
                    disabled={busy || !currentPipeline.ready}
                    title={currentPipeline.ready ? undefined : currentPipeline.ready_reason}>
                    <CheckCircle2 size={15} /> Activate Setup
                  </Button>
                )}
                <Button variant="secondary" onClick={saveDraft} disabled={busy}>
                  <Save size={15} /> Save Draft
                </Button>
                <Button variant="secondary" onClick={() => navigate(editHrefFor(currentPipeline.id))} disabled={busy}>
                  <ArrowRight size={15} /> Open / Edit
                </Button>
                {isAdmin && (
                  <Button variant="danger" onClick={deleteBuiltSetup}
                    disabled={busy || currentPipeline.status === "active"}
                    title={currentPipeline.status === "active"
                      ? "This setup is active — activate a different setup before deleting it" : undefined}>
                    <Trash2 size={15} /> Delete Draft
                  </Button>
                )}
              </div>
            </div>
            {/* Reserves the space the now out-of-flow fixed bar used to occupy,
                so it doesn't cover the last bit of page content. */}
            <div style={{ height: 68 }} aria-hidden="true" />
          </>
        )}
      </PageBody>
    </>
  );
}

// A sheet is a rule target unless explicitly classified reference (see
// exporter.classify_sheet_roles).
function isRefSheet(sh: DsSheet): boolean {
  return sh.sheet_role === "reference";
}
function isSummarySheet(sh: DsSheet): boolean {
  return sh.sheet_role === "summary";
}
// Excluded from rule generation — reference, summary, or (legacy templates
// saved before sheet_role existed) explicitly marked non-generatable.
function isNonDataSheet(sh: DsSheet): boolean {
  return isRefSheet(sh) || isSummarySheet(sh) || sh.rule_generatable === false;
}
// Which review-modal bucket a sheet falls into. Legacy rows with only
// rule_generatable===false and no sheet_role land in "reference", matching
// what isRefSheet used to treat as reference before sheet_role existed.
function sheetBucket(sh: DsSheet): "data" | "reference" | "summary" {
  if (isSummarySheet(sh)) return "summary";
  if (isRefSheet(sh) || sh.rule_generatable === false) return "reference";
  return "data";
}

// ---- small components ------------------------------------------------------

// Resting tint per drop zone, so the uploads read apart at a glance. Each entry is
// a whole literal class string on purpose: Tailwind scans the source for complete
// names, so a built-up `border-${tone}-200` would be purged and render colourless.
// Only the resting/hover state is toned — dragging (navy) and filled (emerald)
// stay common across every zone, so those signals mean one thing everywhere.
const DROP_TONES = {
  sky: {
    idle: "border-sky-200 bg-sky-50/60 hover:border-sky-400 hover:bg-sky-50",
    icon: "text-sky-600", cta: "text-sky-700",
  },
  indigo: {
    idle: "border-indigo-200 bg-indigo-50/60 hover:border-indigo-400 hover:bg-indigo-50",
    icon: "text-indigo-600", cta: "text-indigo-700",
  },
  amber: {
    idle: "border-amber-200 bg-amber-50/60 hover:border-amber-400 hover:bg-amber-50",
    icon: "text-amber-600", cta: "text-amber-700",
  },
  rose: {
    idle: "border-rose-200 bg-rose-50/60 hover:border-rose-400 hover:bg-rose-50",
    icon: "text-rose-600", cta: "text-rose-700",
  },
  teal: {
    idle: "border-teal-200 bg-teal-50/60 hover:border-teal-400 hover:bg-teal-50",
    icon: "text-teal-600", cta: "text-teal-700",
  },
} as const;
type DropTone = keyof typeof DROP_TONES;

function FilePick({ label, icon, file, onPick, accept, hint, tone, required, disabled,
                   altAction }: {
  label: string; icon: React.ReactNode; file: File | null;
  onPick: (f: File | null) => void; accept?: string; hint?: string; tone: DropTone; required?: boolean;
  disabled?: boolean;
  /** A second way to satisfy this box, offered INSIDE it. The output template
   *  can be uploaded or created, and those are equal choices — putting the
   *  second one in a separate panel underneath made it read as an error
   *  message rather than as the other half of the same decision. */
  altAction?: { label: string; onClick: () => void; hint?: string };
}) {
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  const t = DROP_TONES[tone];
  // The native input keeps its own value, so when the file is cleared from the
  // outside (e.g. the scope reset on a carrier change) it has to be cleared too
  // — otherwise re-picking the SAME file fires no change event and silently
  // attaches nothing.
  useEffect(() => { if (!file && ref.current) ref.current.value = ""; }, [file]);
  // With two actions offered, clicking the box itself is ambiguous — the
  // buttons say which is which, so the whole-card click only stands when there
  // is one thing it could mean.
  const cardOpens = !altAction || !!file;
  return (
    <div
      onClick={() => { if (!disabled && cardOpens) ref.current?.click(); }}
      onDragOver={e => { e.preventDefault(); if (!disabled) setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={e => {
        e.preventDefault(); setDrag(false);
        if (disabled) return;
        const f = e.dataTransfer.files?.[0]; if (f) onPick(f);
      }}
      className={`rounded-lg border-2 border-dashed p-4 text-center transition select-none
        ${disabled ? "cursor-not-allowed opacity-50 border-border bg-surface-2"
          : `${cardOpens ? "cursor-pointer" : ""} ${drag ? "border-navy bg-navy/5" : file ? "border-emerald-300 bg-emerald-50/40" : t.idle}`}`}>
      <input ref={ref} type="file" accept={accept ?? ".xlsx,.xls,.csv,.xml,.json"} className="hidden" disabled={disabled}
        onClick={e => e.stopPropagation()}
        onChange={e => onPick(e.target.files?.[0] ?? null)} />
      <div className="flex items-center justify-center gap-1.5 text-sm font-medium mb-1">
        <span className={file ? "text-emerald-600" : t.icon}>{icon}</span> {label}
        {required && <span className="text-red-500">*</span>}
      </div>
      {file ? (
        <div className="flex items-center justify-center gap-1.5 text-[11px] text-emerald-700">
          <CheckCircle2 size={12} className="shrink-0" />
          <span className="truncate min-w-0">{file.name}</span>
          <button className="text-ink-muted hover:text-danger ml-0.5 shrink-0"
            onClick={e => { e.stopPropagation(); onPick(null); if (ref.current) ref.current.value = ""; }}>✕</button>
        </div>
      ) : disabled ? (
        <div className="text-[11px] text-ink-muted">Select a carrier and program first</div>
      ) : altAction ? (
        <div className="text-[11px] text-ink-muted">
          <div className="flex flex-wrap items-center justify-center gap-2">
            <button type="button"
              onClick={e => { e.stopPropagation(); ref.current?.click(); }}
              className={`inline-flex items-center gap-1 rounded-md border border-border
                bg-white px-2.5 py-1 font-medium transition hover:border-brand
                hover:bg-surface-2 ${t.cta}`}>
              <UploadCloud size={12} /> Upload BDX Layout
            </button>
            <span className="text-ink-soft">or</span>
            <button type="button"
              onClick={e => { e.stopPropagation(); altAction.onClick(); }}
              className="inline-flex items-center gap-1 rounded-md border border-border
                bg-white px-2.5 py-1 font-medium text-ink transition
                hover:border-brand hover:bg-surface-2">
              <Sparkles size={12} /> {altAction.label}
            </button>
          </div>
          {hint ? <div className="mt-1.5 opacity-80">{hint}</div> : null}
          {altAction.hint
            ? <div className="mt-0.5 text-ink-soft">{altAction.hint}</div> : null}
        </div>
      ) : (
        <div className="text-[11px] text-ink-muted">
          <span className={`inline-flex items-center gap-1 font-medium ${t.cta}`}>
            <UploadCloud size={12} /> Click to Upload</span> or Drag &amp; Drop
          {hint ? <div className="mt-0.5 opacity-80">{hint}</div> : null}
        </div>
      )}
    </div>
  );
}

// Optional MULTI-file reference-document picker (dashed box, matches FilePick).
// External documents the contract defers to are attached here and sent with the
// contract so deferred clauses resolve into real rules.
function ReferencePick({ files, onAdd, onRemoveAt, disabled }: {
  files: File[]; onAdd: (fs: File[]) => void; onRemoveAt: (i: number) => void; disabled?: boolean;
}) {
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  return (
    <div
      onClick={() => { if (!disabled) ref.current?.click(); }}
      onDragOver={e => { e.preventDefault(); if (!disabled) setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={e => {
        e.preventDefault(); setDrag(false);
        if (disabled) return;
        const fs = Array.from(e.dataTransfer.files || []); if (fs.length) onAdd(fs);
      }}
      className={`rounded-lg border-2 border-dashed p-4 text-center transition select-none
        ${disabled ? "cursor-not-allowed opacity-50 border-border bg-surface-2"
          : `cursor-pointer ${drag ? "border-navy bg-navy/5" : files.length ? "border-emerald-300 bg-emerald-50/40" : DROP_TONES.rose.idle}`}`}>
      <input ref={ref} type="file" multiple accept=".pdf,.docx,.doc,.txt,.xlsx,.xls,.csv" className="hidden" disabled={disabled}
        onClick={e => e.stopPropagation()}
        onChange={e => { const fs = Array.from(e.target.files || []); if (ref.current) ref.current.value = ""; if (fs.length) onAdd(fs); }} />
      <div className="flex items-center justify-center gap-1.5 text-sm font-medium mb-1">
        <span className={files.length ? "text-emerald-600" : DROP_TONES.rose.icon}><FileText size={15} /></span>
        Reference Document(s) <span className="font-normal"> (Optional)</span>
      </div>
      {files.length > 0 ? (
        <div className="space-y-1">
          {files.map((f, i) => (
            <div key={i} className="flex items-center justify-center gap-1.5 text-[11px] text-emerald-700">
              <CheckCircle2 size={12} className="shrink-0" />
              <span className="truncate min-w-0">{f.name}</span>
              <button className="text-ink-muted hover:text-danger ml-0.5 shrink-0"
                onClick={e => { e.stopPropagation(); onRemoveAt(i); }}>✕</button>
            </div>
          ))}
          <div className={`text-[11px] font-medium inline-flex items-center gap-1 pt-0.5 ${DROP_TONES.rose.cta}`}>
            <UploadCloud size={12} /> Add More
          </div>
        </div>
      ) : disabled ? (
        <div className="text-[11px] text-ink-muted">Select a carrier and program first</div>
      ) : (
        <div className="text-[11px] text-ink-muted">
          <span className={`inline-flex items-center gap-1 font-medium ${DROP_TONES.rose.cta}`}>
            <UploadCloud size={12} /> Click to Upload</span> or Drag &amp; Drop
          <div className="mt-0.5 opacity-80">Guidelines the contract defers to (e.g. Purchasing Guidelines)</div>
        </div>
      )}
    </div>
  );
}

// REQUIRED multi-file contract picker (dashed box, matches FilePick/ReferencePick).
// Picking rules are unchanged from the button this replaced: several at once, added
// over rounds, de-duplicated by name+size so re-picking one doesn't double it up.
function ContractPick({ files, existing, onRemoveExisting, loadingExisting,
                       awaitingBroker, onAdd, onRemoveAt, disabled }: {
  files: File[];
  /** Contracts already approved for the chosen programme + broker. They satisfy
   *  the requirement exactly as an upload does — the build takes their id and
   *  skips extraction entirely. */
  existing?: { id: number; name: string; from: string | null }[];
  onRemoveExisting?: (id: number) => void;
  loadingExisting?: boolean;
  /** Approved contracts on the programme that the current pick does not bind,
   *  because they belong to a broker nobody has chosen. */
  awaitingBroker?: { contracts: number; brokers: number } | null;
  onAdd: (fs: File[]) => void; onRemoveAt: (i: number) => void; disabled?: boolean;
}) {
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  const onFile = existing ?? [];
  const have = onFile.length + files.length;
  return (
    <div
      onClick={() => { if (!disabled) ref.current?.click(); }}
      onDragOver={e => { e.preventDefault(); if (!disabled) setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={e => {
        e.preventDefault(); setDrag(false);
        if (disabled) return;
        const fs = Array.from(e.dataTransfer.files || []); if (fs.length) onAdd(fs);
      }}
      className={`rounded-lg border-2 border-dashed p-4 text-center transition select-none
        ${disabled ? "cursor-not-allowed opacity-50 border-border bg-surface-2"
          : `cursor-pointer ${drag ? "border-navy bg-navy/5" : have ? "border-emerald-300 bg-emerald-50/40" : DROP_TONES.rose.idle}`}`}>
      <input ref={ref} type="file" multiple accept=".pdf,.docx" className="hidden" disabled={disabled}
        onClick={e => e.stopPropagation()}
        onChange={e => { const fs = Array.from(e.target.files || []); if (ref.current) ref.current.value = ""; if (fs.length) onAdd(fs); }} />
      <div className="flex items-center justify-center gap-1.5 text-sm font-medium mb-1">
        <span className={have ? "text-emerald-600" : DROP_TONES.rose.icon}><FileText size={15} /></span>
        Contracts <span className="text-red-500">*</span>
      </div>
      {have > 0 ? (
        <div className="space-y-1">
          {/* Already on file. Listed before the new picks because that is the
              order the build stages them in, so "Contract 2 of 3" during a
              build names the same document the user can see here. */}
          {onFile.map(c => (
            <div key={`e${c.id}`}
              className="flex items-center justify-center gap-1.5 text-[11px] text-emerald-700">
              <CheckCircle2 size={12} className="shrink-0" />
              <span className="truncate min-w-0">{c.name}</span>
              <span className="text-[10px] text-ink-soft shrink-0">
                on file{c.from ? ` · ${c.from}` : " · carrier held"}
              </span>
              {onRemoveExisting && (
                <button className="text-ink-muted hover:text-danger ml-0.5 shrink-0"
                  title="Leave this one out — upload a replacement instead"
                  onClick={e => { e.stopPropagation(); onRemoveExisting(c.id); }}>✕</button>
              )}
            </div>
          ))}
          {files.map((f, i) => (
            <div key={i} className="flex items-center justify-center gap-1.5 text-[11px] text-emerald-700">
              <CheckCircle2 size={12} className="shrink-0" />
              <span className="truncate min-w-0">{f.name}</span>
              <button className="text-ink-muted hover:text-danger ml-0.5 shrink-0"
                onClick={e => { e.stopPropagation(); onRemoveAt(i); }}>✕</button>
            </div>
          ))}
          <div className={`text-[11px] font-medium inline-flex items-center gap-1 pt-0.5 ${DROP_TONES.rose.cta}`}>
            <UploadCloud size={12} /> Add More
          </div>
        </div>
      ) : loadingExisting ? (
        <div className="text-[11px] text-ink-muted">Looking for a contract already on file…</div>
      ) : awaitingBroker ? (
        <div className="text-[11px] text-amber-700 px-2">
          {awaitingBroker.contracts} contract
          {awaitingBroker.contracts === 1 ? "" : "s"} already on file for this
          programme, held by {awaitingBroker.brokers} broker
          {awaitingBroker.brokers === 1 ? "" : "s"}.
          <div className="mt-0.5 text-ink-muted">
            <b>Pick the broker above</b> to use theirs — or upload one here.
          </div>
        </div>
      ) : disabled ? (
        <div className="text-[11px] text-ink-muted">Select a carrier and program first</div>
      ) : (
        <div className="text-[11px] text-ink-muted">
          <span className={`inline-flex items-center gap-1 font-medium ${DROP_TONES.rose.cta}`}>
            <UploadCloud size={12} /> Click to Upload</span> or Drag &amp; Drop
          <div className="mt-0.5 opacity-80">One applies to every sheet; add several to map each schedule sheet</div>
        </div>
      )}
    </div>
  );
}


function Banner({ kind, children, className = "" }: {
  kind: "error" | "ok" | "warn" | "info"; children: React.ReactNode; className?: string;
}) {
  const styles = {
    error: "bg-danger/10 text-danger", ok: "bg-emerald-50 text-emerald-700",
    warn: "bg-amber-50 text-amber-700", info: "bg-surface-2 text-ink-muted",
  }[kind];
  return <div className={`flex flex-wrap items-center gap-2 rounded-md px-4 py-2.5 text-sm ${styles} ${className}`}>{children}</div>;
}

// Checkbox list of the sheets found in a picked workbook. `options === null`
// means nothing picked yet; `[]` means the file had no readable sheets.
function SheetPicker({ kind, options, selected, onToggle, hint }: {
  kind: "input" | "output"; options: string[] | null;
  selected: Set<string>; onToggle: (name: string) => void; hint: string;
}) {
  if (options === null) return null;
  if (options.length === 0) {
    return <div className="rounded-md bg-surface-2 px-3 py-2 text-xs text-ink-muted">No readable sheets found.</div>;
  }
  return (
    <div className="rounded-md border border-border bg-surface px-3 py-2">
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-medium">
          {kind === "input" ? "Input Sheets to Map" : "Output Sheets to Include"}
        </span>
        <span className="text-[11px] text-ink-muted">{selected.size}/{options.length}</span>
      </div>
      <div className="space-y-1 max-h-40 overflow-auto">
        {options.map(name => (
          <label key={name} className="flex items-center gap-2 text-xs cursor-pointer">
            <input type="checkbox" checked={selected.has(name)} onChange={() => onToggle(name)} />
            <span className="truncate">{name}</span>
          </label>
        ))}
      </div>
      <p className="mt-1.5 text-[11px] text-ink-muted">{hint}</p>
    </div>
  );
}

