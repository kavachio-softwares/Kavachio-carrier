import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import {
  FileDown, Upload, Check, Loader2, FileSpreadsheet, Sparkles, X, AlertTriangle,
} from "lucide-react";
import { api, downloadFile } from "../api/client";
import {
  getUploadExceptions, saveFields,
  type CustomViolation, type StoredException, type StoredRun,
  type FieldsSaveResponse, type RuleExplanation,
} from "../api/validation";
import { ExceptionCards } from "../components/ExceptionCards";
import { recoParts } from "../components/ExceptionDecisionTable";
import { currentMga, getUser } from "../auth";
import { fmtStamp } from "../utils/date";
import Card from "../components/ui/Card";
import { Sk } from "../components/ui/Skeleton";
import { LoadingOverlay } from "../components/Busy";
import Button from "../components/ui/Button";
import Modal from "../components/ui/Modal";
import { Field, Select, TextInput } from "../components/ui/Field";
import { PageBody, PageHeader } from "../components/Layout";
import PipelineStepper from "../components/PipelineStepper";

const SEV_PILL: Record<string, string> = {
  critical: "pill pill-red",
  warning: "pill pill-amber",
  info: "pill pill-blue",
};

type Template = {
  id: number; mga: string; name: string; carrier?: string;
  version?: number; is_active?: boolean;
  carrier_party_id?: number; approved: boolean; structure: any;
  contract_id?: number;
};
type TemplateGroup = { name: string; active_id: number; versions: Template[] };
type UploadRow = { id: number; source_file: string; total_rows: number; has_source_blob?: boolean };
type ExportDownload = {
  id: number; filename: string; template_name?: string | null;
  policy_count: number; exception_count: number; status: string;
  generated_by?: string | null; created_at?: string | null;
  source_upload_id?: number | null;
};
type OutputException = {
  severity: string; code: string; sheet?: string; row?: number;
  column?: string; field?: string; message: string;
  rule_name?: string; rule_id?: number; contract_filename?: string;
  reason?: string; error_class?: string;
  contract_id?: number; contract_clause_text?: string | null;
  contract_clause_page?: number | null;
  policy_number?: string | null; actual_value?: string | null;
  /** Backend-derived plain-English explanation of the rule (rule_explainer.py). */
  explanation?: RuleExplanation | null;
  /** See StoredException.check_kind / root_cause — the row's own heading and
   *  hint depend on these, so they must survive the mapper below. */
  check_kind?: "numeric_format" | null;
  root_cause?: string | null;
};
type SheetGrid = { sheet: string; rows: any[][] };

// UI stages for the template-from-sample upload (pure-frontend animation
// so the user has visible feedback while the backend does parse + AI).
type Stage = "idle" | "uploading" | "parsing" | "ai_mapping" | "done";

export default function Outputs() {
  const mga = currentMga();
  const nav = useNavigate();
  const { pathname } = useLocation();
  const [groups, setGroups] = useState<TemplateGroup[]>([]);
  const [carriers, setCarriers] = useState<{ id: number; legal_name: string }[]>([]);
  const [contractsForCarrier, setContractsForCarrier] = useState<
    { id: number; filename: string; program_name?: string }[]
  >([]);
  const [loadingTemplates, setLoadingTemplates] = useState(true);
  const [uploads, setUploads] = useState<UploadRow[]>([]);
  const [templateId, setTemplateId] = useState<number | "">("");
  const [uploadId, setUploadId] = useState<number | "">("");
  const [policyIds, setPolicyIds] = useState("");
  const [filename, setFilename] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // When output validation finds exceptions, hold the result and open a
  // confirmation popup before generating.
  const [confirm, setConfirm] = useState<
    { total: number; critical: number; warning: number; violations: CustomViolation[] } | null
  >(null);
  // "Modify here": when true the exceptions popup switches to the inline editor.
  const [editMode, setEditMode] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [newName, setNewName] = useState("");
  // Output file format the new template will produce (xlsx | csv | xml | json).
  const [outputFormat, setOutputFormat] = useState("xlsx");
  const [isNewTemplate, setIsNewTemplate] = useState(false);
  const [selectedCarrierId, setSelectedCarrierId] = useState<number | "">("");
  const [selectedContractId, setSelectedContractId] = useState<number | "">("");
  // Template upload progress state
  const [stage, setStage] = useState<Stage>("idle");
  const [pickedFile, setPickedFile] = useState<File | null>(null);
  // The chosen output-template sample file, held until the user clicks process
  // (so they can change/remove it before uploading, like the contract file).
  const [templateFile, setTemplateFile] = useState<File | null>(null);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  // Contract upload — "new" uploads a file simultaneously; "existing" links to prior contract
  const [contractMode, setContractMode] = useState<"new" | "existing">("new");
  const [contractFile, setContractFile] = useState<File | null>(null);
  const contractRef = useRef<HTMLInputElement>(null);
  // /setup halt: contract defers rules to external documents. Holds the context
  // needed to re-submit when the user clicks "Continue Anyway".
  const [refsPrompt, setRefsPrompt] = useState<null | {
    programId: number;
    templateFile: File;
    refs: Array<{ document_name?: string; version_or_date?: string; source_texts?: string[]; pages?: number[] }>;
    resumeToken?: string | null;
  }>(null);
  const refDocRef = useRef<HTMLInputElement>(null);
  const mainRefDocRef = useRef<HTMLInputElement>(null);
  const [refFiles, setRefFiles] = useState<File[]>([]);
  // Reference documents accumulated across rounds — re-sent on every attempt so
  // a partial upload (1 of N referenced docs) doesn't lose the earlier ones.
  const [uploadedRefFiles, setUploadedRefFiles] = useState<File[]>([]);
  // Programs for the selected carrier (needed to pick program_id when uploading new contract)
  const [programsForCarrier, setProgramsForCarrier] = useState<{ id: number; name: string }[]>([]);
  const [carrierScopeLoading, setCarrierScopeLoading] = useState(false);
  const [selectedProgramId, setSelectedProgramId] = useState<number | "">("");
  // When the carrier has no program yet, the user names a new one to create inline.
  const [newProgramName, setNewProgramName] = useState<string>("");
  // Generated-output downloads, plus the data/exceptions viewers.
  const [downloads, setDownloads] = useState<ExportDownload[]>([]);
  const [loadingDownloads, setLoadingDownloads] = useState(true);
  const [dataView, setDataView] = useState<{ name: string; sheets: SheetGrid[] } | null>(null);
  const [dataLoading, setDataLoading] = useState(false);
  const [excView, setExcView] = useState<ExportDownload | null>(null);
  const [excList, setExcList] = useState<OutputException[] | null>(null);
  // Persisted validation exceptions for the selected upload (Option B —
  // survives reloads; read back from the DB via /api/validate/upload).
  const [storedRun, setStoredRun] = useState<StoredRun | null>(null);
  const [storedExc, setStoredExc] = useState<StoredException[]>([]);
  const [storedMapperSpec, setStoredMapperSpec] = useState<Record<string, Record<string, string | string[]>> | null>(null);
  const [showExcModal, setShowExcModal] = useState(false);

  // Tracks the currently-selected upload so a slow in-flight exceptions fetch
  // can't overwrite state after the user has moved to a different upload.
  const uploadSelRef = useRef<number | "">("");
  useEffect(() => { uploadSelRef.current = uploadId; }, [uploadId]);

  function loadExceptions(): Promise<void> {
    if (!uploadId) {
      setStoredRun(null); setStoredExc([]); setStoredMapperSpec(null);
      return Promise.resolve();
    }
    const forUpload = Number(uploadId);
    return getUploadExceptions(forUpload)
      .then(r => {
        if (uploadSelRef.current !== forUpload) return;  // selection moved on
        setStoredRun(r.validated ? r.run : null);
        setStoredExc(r.validated ? r.exceptions : []);
        setStoredMapperSpec(r.mapper_spec ?? null);
      })
      .catch(() => {
        if (uploadSelRef.current !== forUpload) return;
        setStoredRun(null); setStoredExc([]); setStoredMapperSpec(null);
      });
  }
  // Reload whenever the selected upload changes.
  useEffect(() => { loadExceptions(); }, [uploadId]);

  function load() {
    setLoadingTemplates(true);
    api.get<TemplateGroup[]>(`/export/templates`, { params: { mga } })
      .then(r => {
        setGroups(r.data);
        // Default the generator to an active version if none chosen yet.
        const flat = r.data.flatMap(g => g.versions);
        setTemplateId(prev => prev || (flat.find(t => t.is_active)?.id ?? ""));
      })
      .finally(() => setLoadingTemplates(false));
    api.get<UploadRow[]>(`/uploads`, { params: { mga, limit: 50 } })
      .then(r => {
        setUploads(r.data);
        // Restore the selection on reload so the validation-exceptions section
        // (keyed off the selected upload) survives a refresh / tab switch.
        if (r.data.length) {
          const saved = Number(localStorage.getItem(`outputs.uploadId.${mga}`) || 0);
          const valid = r.data.some(u => u.id === saved) ? saved : r.data[0].id;
          setUploadId(prev => prev || valid);
        }
      });
    // Load carriers for this tenant for the template creation dropdown
    api.get(`/parties`, { params: { mga, party_type: "carrier" } })
      .then(r => setCarriers(r.data.items || []))
      .catch(() => setCarriers([]));
    loadDownloads();
  }
  function loadDownloads() {
    setLoadingDownloads(true);
    api.get<ExportDownload[]>(`/export/downloads`, { params: { mga, limit: 50 } })
      .then(r => setDownloads(r.data))
      .finally(() => setLoadingDownloads(false));
  }
  useEffect(load, [mga]);

  // When a carrier is selected, fetch its programs + contracts to populate
  // the contract dropdown. The parties endpoint returns programs with their
  // contracts via /parties/{party_id}/programs.
  useEffect(() => {
    if (!selectedCarrierId) {
      setContractsForCarrier([]);
      setSelectedContractId("");
      setProgramsForCarrier([]);
      setSelectedProgramId("");
      setCarrierScopeLoading(false);
      return;
    }
    setCarrierScopeLoading(true);
    api.get(`/parties/${selectedCarrierId}/programs`).then(r => {
      const rows = r.data || [];
      const contracts: { id: number; filename: string; program_name?: string }[] = [];
      const programs: { id: number; name: string }[] = [];
      for (const prog of rows) {
        programs.push({ id: prog.id, name: prog.name });
        for (const c of (prog.contracts || [])) {
          contracts.push({ id: c.id, filename: c.filename, program_name: prog.name });
        }
      }
      setProgramsForCarrier(programs);
      setContractsForCarrier(contracts);
      setNewProgramName("");
      // Auto-select the first program so the user doesn't need to pick one manually
      // when there's only one (the common case).
      if (programs.length === 1) setSelectedProgramId(programs[0].id);
      else setSelectedProgramId("");
    }).catch(() => {
      setContractsForCarrier([]);
      setProgramsForCarrier([]);
    }).finally(() => setCarrierScopeLoading(false));
  }, [selectedCarrierId]);

  async function activate(id: number) {
    try {
      await api.post(`/export/template/${id}/activate`);
      load();
    } catch (e: any) {
      setMsg(e?.response?.data?.detail ?? "Could not activate version.");
    }
  }

  // Flat list of every version (for the generate dropdown / empty-state check)
  // and the distinct template names (to add a version to an existing template).
  const templates = groups.flatMap(g => g.versions);
  const templateNames = groups.map(g => g.name);

  // Fallback download name. The backend sets the correct extension on the
  // stored file (and downloadFile prefers the server's Content-Disposition), so
  // this only guards a name that has no recognised data extension at all.
  function ensureExt(name: string): string {
    if (!name) return "output.xlsx";
    return /\.(xlsx|xls|csv|xml|json|zip)$/i.test(name) ? name : name + ".xlsx";
  }

  function downloadExport(id: number, name: string) {
    // Authenticated blob download — a plain <a href> carries no Bearer token.
    downloadFile(`/export/downloads/${id}/file`, ensureExt(name))
      .catch(() => alert("We couldn't download that file — please try again."));
  }

  async function openExportData(d: ExportDownload) {
    setDataView({ name: d.filename, sheets: [] }); setDataLoading(true);
    try {
      const { data } = await api.get<{ filename: string; sheets: SheetGrid[] }>(
        `/export/downloads/${d.id}/data`);
      setDataView({ name: data.filename, sheets: data.sheets });
    } catch { setDataView({ name: d.filename, sheets: [] }); }
    finally { setDataLoading(false); }
  }

  async function onGenerate() {
    if (!templateId) return;
    setMsg(null); setConfirm(null); setEditMode(false);
    // Pre-generate dry run: /export/validate runs the SAME contract-rule
    // validation /export/generate runs (DuckDB engine — works for legacy AJV and
    // ir_v1 rules) but builds/persists nothing. If it finds exceptions we open the
    // confirmation popup; otherwise we go straight to generate. Best-effort: on
    // error we don't block — generate surfaces exceptions afterwards.
    if (uploadId || policyIds.trim()) {
      setBusy(true);
      try {
        const fd = new FormData();
        fd.append("template_id", String(templateId));
        if (uploadId) fd.append("upload_id", String(uploadId));
        else if (policyIds.trim()) fd.append("policy_ids", policyIds.trim());
        const { data } = await api.post<{
          exception_count: number; critical: number; warning: number;
          violations: CustomViolation[];
          unprocessable_rules?: { rule_id: number; rule_name: string; message: string }[];
        }>(`/export/validate`, fd);
        if (data.exception_count > 0) {
          setConfirm({
            total: data.exception_count,
            critical: data.critical,
            warning: data.warning,
            violations: data.violations ?? [],
          });
          setBusy(false);
          return; // wait for the user's yes/no in the popup
        }
      } catch (e: any) {
        console.warn("Pre-generate validation skipped:", e?.message ?? e);
      }
    }
    await doGenerate();
  }

  async function doGenerate() {
    setBusy(true); setConfirm(null);
    try {
      const fd = new FormData();
      fd.append("template_id", String(templateId));
      if (uploadId) fd.append("upload_id", String(uploadId));
      else if (policyIds.trim()) fd.append("policy_ids", policyIds.trim());
      if (filename) fd.append("filename", filename);
      const email = getUser()?.email; if (email) fd.append("actor", email);
      const { data } = await api.post<{
        id: number; filename: string; exception_count: number;
        unprocessable_rules?: { rule_id: number; rule_name: string; message: string }[];
      }>(`/export/generate`, fd);
      downloadExport(data.id, data.filename);
      const unproc = data.unprocessable_rules?.length ?? 0;
      const unprocNote = unproc > 0
        ? ` ${unproc} rule(s) could not be processed and were skipped.` : "";
      setMsg((data.exception_count > 0
        ? `Generated with ${data.exception_count} exception(s) — see Downloads below.`
        : "Generated. No exceptions found.") + unprocNote);
      loadDownloads();
      loadExceptions();
    } catch (e: any) {
      setMsg(e?.response?.data?.detail ?? e?.message ?? "Generate failed.");
    } finally { setBusy(false); }
  }

  async function uploadTemplate(file: File, referenceFiles: File[] = []) {
    if (!newName.trim()) { setUploadErr("Enter a template name first."); return; }
    if (!selectedCarrierId) { setUploadErr("Select a carrier."); return; }
    if (contractMode === "new" && !contractFile) {
      setUploadErr("Upload a contract file — output template and contract must go up together.");
      return;
    }
    const noProgram = programsForCarrier.length === 0;
    if (contractMode === "new" && !selectedProgramId && !(noProgram && newProgramName.trim())) {
      setUploadErr(noProgram
        ? "Name a program to create for this carrier."
        : "Select a program to attach the contract to.");
      return;
    }
    if (contractMode === "existing" && !selectedContractId) {
      setUploadErr("Select an existing contract.");
      return;
    }
    setUploadErr(null); setBusy(true);
    setPickedFile(file);
    setStage("uploading");

    const tParse = setTimeout(() => setStage("parsing"), 250);
    const tAi   = setTimeout(() => setStage("ai_mapping"), 1800);

    try {
      const fd = new FormData();

      if (contractMode === "new" && contractFile) {
        // Resolve the program: use the selected one, or create a new program
        // under this carrier when the carrier has none yet.
        let programId = selectedProgramId;
        if (!programId && newProgramName.trim()) {
          const { data: prog } = await api.post(
            `/programs`,
            { name: newProgramName.trim(), party_id: selectedCarrierId },
            { params: { mga } },
          );
          programId = prog.id;
        }

        // Simultaneous upload: output template + contract → /programs/{pid}/setup
        // The backend parses the template first, extracts field names, then runs
        // the LLM to map contract clauses → those field names.
        fd.append("contract_file", contractFile);
        fd.append("template_file", file);
        fd.append("template_name", newName);
        fd.append("output_format", outputFormat);
        // Optional reference documents — extracted and fed into the extraction
        // LLM so clauses that defer to them resolve against the real content.
        referenceFiles.forEach(f => fd.append("reference_files", f));
        const { data } = await api.post(`/programs/${programId}/setup`, fd);
        clearTimeout(tParse); clearTimeout(tAi);

        // Pipeline paused — contract references external document(s). Show the
        // popup and keep contractFile/template in state for the retry.
        if (data.status === "references_required") {
          setBusy(false);
          setStage("idle");
          setRefsPrompt({
            programId: Number(programId),
            templateFile: file,
            refs: data.external_references || [],
            resumeToken: data.resume_token ?? null,
          });
          return;
        }

        setStage("done");
        const templateId = data.template?.id;
        setTimeout(() => nav(templateId ? `/outputs/templates/${templateId}` : "/outputs"), 350);
      } else {
        // Link to an existing contract → /export/template/generate
        fd.append("mga", mga);
        fd.append("name", newName);
        fd.append("file", file);
        fd.append("output_format", outputFormat);
        if (selectedCarrierId) fd.append("carrier_party_id", String(selectedCarrierId));
        if (selectedContractId) fd.append("contract_id", String(selectedContractId));
        const { data } = await api.post(`/export/template/generate`, fd);
        clearTimeout(tParse); clearTimeout(tAi);
        setStage("done");
        setTimeout(() => nav(`/outputs/templates/${data.id}`), 350);
      }

      setContractFile(null);
      setTemplateFile(null);
      setUploadedRefFiles([]);
      if (contractRef.current) contractRef.current.value = "";
      if (fileRef.current) fileRef.current.value = "";
    } catch (e: any) {
      clearTimeout(tParse); clearTimeout(tAi);
      const detail = e?.response?.data?.detail;
      setUploadErr(typeof detail === "string"
        ? detail : detail?.message ?? e?.message ?? "Upload failed.");
      setStage("idle"); setPickedFile(null); setBusy(false);
      // Keep the chosen template file on error so the user can retry without re-picking.
    }
  }

  // "Upload Reference Document" from the popup: send the picked reference files
  // with the contract; the backend extracts their text and feeds it into the
  // extraction LLM so deferred clauses resolve against the real content.
  async function submitReferenceDocuments(files: File[]) {
    if (!refsPrompt || !contractFile || files.length === 0) return;
    const { programId, templateFile } = refsPrompt;
    // Accumulate with anything uploaded in earlier rounds so a partial upload
    // (some of N referenced docs) doesn't drop the previously provided ones.
    const allRefs = [...uploadedRefFiles, ...files];
    setUploadedRefFiles(allRefs);
    setRefsPrompt(null);
    setUploadErr(null); setBusy(true); setStage("uploading");
    const tParse = setTimeout(() => setStage("parsing"), 250);
    const tAi   = setTimeout(() => setStage("ai_mapping"), 1800);
    try {
      const fd = new FormData();
      fd.append("contract_file", contractFile);
      fd.append("template_file", templateFile);
      fd.append("template_name", newName);
      allRefs.forEach(f => fd.append("reference_files", f));
      const { data } = await api.post(`/programs/${programId}/setup`, fd);
      clearTimeout(tParse); clearTimeout(tAi);

      // Some references still missing — re-open the popup with only those that
      // remain (the backend excludes the docs we already provided).
      if (data.status === "references_required") {
        setBusy(false); setStage("idle");
        setRefsPrompt({
          programId, templateFile,
          refs: data.external_references || [],
          resumeToken: data.resume_token ?? null,
        });
        return;
      }

      setStage("done");
      const templateId = data.template?.id;
      setContractFile(null); setTemplateFile(null); setRefFiles([]); setUploadedRefFiles([]);
      if (contractRef.current) contractRef.current.value = "";
      if (fileRef.current) fileRef.current.value = "";
      setTimeout(() => nav(templateId ? `/outputs/templates/${templateId}` : "/outputs"), 350);
    } catch (e: any) {
      clearTimeout(tParse); clearTimeout(tAi);
      const detail = e?.response?.data?.detail;
      setUploadErr(typeof detail === "string"
        ? detail : detail?.message ?? e?.message ?? "Reference upload failed.");
      setStage("idle"); setBusy(false);
    }
  }

  // "Continue Anyway" from the external-reference popup: re-run /setup with
  // continue_anyway=true so the pipeline skips the halt and generates rules.
  async function continueWithoutReferences() {
    if (!refsPrompt || !contractFile) return;
    const { programId, templateFile, resumeToken } = refsPrompt;
    setRefsPrompt(null);
    setUploadErr(null); setBusy(true); setStage("uploading");
    const tParse = setTimeout(() => setStage("parsing"), 250);
    const tAi   = setTimeout(() => setStage("ai_mapping"), 1800);
    try {
      const fd = new FormData();
      fd.append("contract_file", contractFile);
      fd.append("template_file", templateFile);
      fd.append("template_name", newName);
      fd.append("continue_anyway", "true");
      // Resume from the cached extraction — skips the extraction LLM call.
      if (resumeToken) fd.append("resume_token", resumeToken);
      const { data } = await api.post(`/programs/${programId}/setup`, fd);
      clearTimeout(tParse); clearTimeout(tAi);
      setStage("done");
      const templateId = data.template?.id;
      setContractFile(null); setTemplateFile(null); setRefFiles([]); setUploadedRefFiles([]);
      if (contractRef.current) contractRef.current.value = "";
      if (fileRef.current) fileRef.current.value = "";
      setTimeout(() => nav(templateId ? `/outputs/templates/${templateId}` : "/outputs"), 350);
    } catch (e: any) {
      clearTimeout(tParse); clearTimeout(tAi);
      const detail = e?.response?.data?.detail;
      setUploadErr(typeof detail === "string"
        ? detail : detail?.message ?? e?.message ?? "Continue failed.");
      setStage("idle"); setBusy(false);
    }
  }

  // Which BDX Output tab is active. Driven by the sub-route so the sidebar's
  // step 3 (/outputs/new-template) and step 5 (/outputs/generate) deep-link here.
  const tab: "new-template" | "generate" =
    pathname.endsWith("/new-template") ? "new-template"
      : pathname.endsWith("/generate") ? "generate"
      : (!loadingTemplates && templates.length === 0) ? "new-template"
      : "generate";

  return (
    <>
      {busy && <LoadingOverlay label="Working on the output — this can take a few minutes…" />}
      <PageHeader title="Output Delivery"
        subtitle="Generate carrier-format BDX from canonical data" />
      <PipelineStepper current={tab === "new-template" ? "template" : "output"} />
      <PageBody>
        {/* Pipeline progress for the template-from-sample upload */}
        {stage !== "idle" && (
          <UploadProgress
            stage={stage}
            file={pickedFile}
            err={uploadErr}
            onCancel={() => {
              setStage("idle"); setPickedFile(null); setUploadErr(null);
              setBusy(false);
              if (fileRef.current) fileRef.current.value = "";
            }}
          />
        )}

        {/* Change 3 — two tabs: New template from sample · Generate output */}
        <div className="flex gap-1 border-b border-border">
          <Link to="/outputs/new-template"
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition ${
              tab === "new-template" ? "border-navy text-navy"
                : "border-transparent text-ink-muted hover:text-ink"}`}>
            New Template From Sample
          </Link>
          <Link to="/outputs/generate"
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition ${
              tab === "generate" ? "border-navy text-navy"
                : "border-transparent text-ink-muted hover:text-ink"}`}>
            Generate Output
          </Link>
        </div>

        {/* Loading skeleton for whichever tab is active */}
        {loadingTemplates && (
          <Card title={tab === "generate" ? "Generate Output" : "New Template From Sample"}>
            <div className="space-y-3 max-w-2xl">
              <Sk className="h-8" /><Sk className="h-8" /><Sk className="h-9 w-48" />
            </div>
          </Card>
        )}

        {/* ── New template from sample tab — first-time get-started ── */}
        {tab === "new-template" && !loadingTemplates && templates.length === 0 && (
          <Card title="Get Started: Configure Your First Output Template">
            <p className="text-sm text-ink-muted mb-4">
              You don't have any output templates yet. Upload an accepted
              sample BDX so the AI can reverse-engineer its structure. Once a
              template is approved, you'll be able to generate carrier-shaped
              BDX files from your canonical data.
            </p>
            <div className="grid grid-cols-2 gap-3 max-w-2xl">
              <Field label="Template name *">
                <TextInput value={newName} list="output-template-names"
                  onChange={e => setNewName(e.target.value)}
                  placeholder="e.g. Pinnacle Standard BDX" />
                <datalist id="output-template-names">
                  {templateNames.map(n => <option key={n} value={n} />)}
                </datalist>
              </Field>
              <Field label="Carrier">
                <Select value={selectedCarrierId}
                  onChange={e => setSelectedCarrierId(e.target.value ? Number(e.target.value) : "")}>
                  <option value="">Choose a Carrier…</option>
                  {carriers.map(c => (
                    <option key={c.id} value={c.id}>{c.legal_name}</option>
                  ))}
                </Select>
              </Field>
              <Field label="Output format">
                <Select value={outputFormat}
                  onChange={e => setOutputFormat(e.target.value)}>
                  <option value="xlsx">Excel (.xlsx)</option>
                  <option value="csv">CSV (.csv)</option>
                  <option value="xml">XML (.xml)</option>
                  <option value="json">JSON (.json)</option>
                </Select>
              </Field>
            </div>

            {/* Contract — must upload alongside the output template */}
            <div className="mt-3 max-w-2xl rounded-lg border border-border bg-surface-2/50 p-3 space-y-2">
              <div className="text-xs font-semibold text-ink-muted uppercase tracking-wide">
                Contract <span className="font-normal normal-case text-red-600">(required — uploaded together with template)</span>
              </div>
              <p className="text-[11px] text-ink-muted">
                The output template and contract are processed together. The AI maps contract
                clauses to the output template's column names so validations always reference
                columns your team recognises.
              </p>
              {selectedCarrierId && !carrierScopeLoading && programsForCarrier.length > 1 && (
                <Field label="Program">
                  <Select value={selectedProgramId}
                    onChange={e => setSelectedProgramId(e.target.value ? Number(e.target.value) : "")}
                    disabled={!selectedCarrierId}>
                    <option value="">Select Program…</option>
                    {programsForCarrier.map(p => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </Select>
                </Field>
              )}
              {selectedCarrierId && !carrierScopeLoading && programsForCarrier.length === 0 && (
                <Field label="Program (none yet — name a new one)">
                  <TextInput value={newProgramName}
                    onChange={e => setNewProgramName(e.target.value)}
                    placeholder="e.g. Aurenity GL Program" />
                </Field>
              )}
              {contractFile ? (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-emerald-700 font-medium truncate flex-1">
                    ✓ {contractFile.name}
                  </span>
                  <button
                    onClick={() => { setContractFile(null); if (contractRef.current) contractRef.current.value = ""; }}
                    className="text-[11px] text-ink-muted underline">Remove</button>
                </div>
              ) : (
                <Button variant="ghost" onClick={() => contractRef.current?.click()}
                  disabled={!selectedCarrierId}>
                  <FileSpreadsheet size={13} /> Choose Contract (.pdf/.docx)
                </Button>
              )}
              <input ref={contractRef} hidden type="file" accept=".pdf,.doc,.docx,.txt"
                onChange={e => setContractFile(e.target.files?.[0] ?? null)} />
            </div>

            {/* Output template sample — choose, review, then process */}
            <div className="mt-4 space-y-2 max-w-2xl">
              {templateFile ? (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-emerald-700 font-medium truncate flex-1">
                    ✓ {templateFile.name}
                  </span>
                  <button
                    onClick={() => { setTemplateFile(null); if (fileRef.current) fileRef.current.value = ""; }}
                    className="text-[11px] text-ink-muted underline">Remove</button>
                </div>
              ) : (
                <Button variant="ghost" onClick={() => fileRef.current?.click()}
                  disabled={!selectedCarrierId}>
                  <FileSpreadsheet size={13} /> Choose Output Template Sample (.xlsx/.csv/.xml)
                </Button>
              )}
              <input ref={fileRef} hidden type="file" accept=".xlsx,.xls,.csv,.xml,.json"
                onChange={e => setTemplateFile(e.target.files?.[0] ?? null)} />
            </div>

            {/* Reference documents (optional) — ATTACHED now (not processed yet),
                then sent together with the contract when the user clicks Upload.
                Their text is fed into the extraction LLM so clauses that defer to
                them (e.g. "Excluded Classes: per the Purchasing Guidelines on
                file") resolve against the real content. If none are attached and
                the contract defers to an external doc, the pipeline halts after
                extraction and the popup asks for it (see external-references modal). */}
            {uploadedRefFiles.length > 0 && (
              <div className="mt-3 max-w-2xl flex items-center gap-2 flex-wrap">
                <span className="text-xs text-emerald-700 font-medium">
                  ✓ {uploadedRefFiles.length} reference document(s): {uploadedRefFiles.map(f => f.name).join(", ")}
                </span>
                <button
                  onClick={() => { setUploadedRefFiles([]); if (mainRefDocRef.current) mainRefDocRef.current.value = ""; }}
                  className="text-[11px] text-ink-muted underline">Remove</button>
              </div>
            )}

            <div className="mt-3 flex items-center gap-3 flex-wrap">
              <Button onClick={() => templateFile && uploadTemplate(templateFile, uploadedRefFiles)}
                disabled={busy || !newName.trim() || !selectedCarrierId || !contractFile || !templateFile
                  || (programsForCarrier.length > 1 && !selectedProgramId)
                  || (programsForCarrier.length === 0 && !newProgramName.trim())}>
                <Upload size={14} /> Upload Output Template + Contract →
              </Button>
              <Button variant="secondary" onClick={() => mainRefDocRef.current?.click()}
                disabled={busy || !selectedCarrierId}>
                <Upload size={13} />
                {uploadedRefFiles.length > 0 ? "Add More Reference Documents" : "Upload Reference Document (Optional)"}
              </Button>
              <input ref={mainRefDocRef} hidden type="file" multiple
                onChange={e => {
                  const fs = Array.from(e.target.files || []);
                  if (fs.length) setUploadedRefFiles(prev => [...prev, ...fs]);
                  if (mainRefDocRef.current) mainRefDocRef.current.value = "";
                }} />
              <Sparkles size={12} className="text-accent" />
              <span className="text-xs text-ink-muted">
                AI maps contract clauses to output template columns.
              </span>
            </div>
          </Card>
        )}

        {/* ── Generate output tab — when at least one template exists ── */}
        {tab === "generate" && !loadingTemplates && templates.length > 0 && (
          <div className="max-w-2xl">
            <Card title="Generate Output">
              <div className="space-y-3">
                <Field label="Template">
                  <Select value={templateId}
                    onChange={e => setTemplateId(e.target.value ? Number(e.target.value) : "")}>
                    <option value="">Select…</option>
                    {groups.map(g => {
                      const t = g.versions.find(v => v.id === g.active_id);
                      if (!t) return null;
                      return (
                        <option key={t.id} value={t.id}>
                          {g.name} · v{t.version ?? 1}
                          {t.carrier ? ` · ${t.carrier}` : ""}
                        </option>
                      );
                    })}
                  </Select>
                  <p className="text-[11px] text-ink-soft mt-1">
                    Shows the active version of each template. Activate a different
                    version below to use it here.
                  </p>
                </Field>
                <Field label="Source — upload">
                  <Select value={uploadId}
                    onChange={e => {
                      const v = e.target.value ? Number(e.target.value) : "";
                      setUploadId(v);
                      if (v) localStorage.setItem(`outputs.uploadId.${mga}`, String(v));
                      else localStorage.removeItem(`outputs.uploadId.${mga}`);
                    }}>
                    <option value="">Use raw policy_ids instead…</option>
                    {uploads.map(u => (
                      <option key={u.id} value={u.id}>
                        #{u.id} · {u.source_file} · {u.total_rows} rows
                      </option>
                    ))}
                  </Select>
                </Field>
                {!uploadId && (
                  <Field label="Or policy IDs (comma-separated)">
                    <TextInput value={policyIds}
                      onChange={e => setPolicyIds(e.target.value)} />
                  </Field>
                )}
                <Field label="Output filename (optional)">
                  <TextInput value={filename}
                    onChange={e => setFilename(e.target.value)}
                    placeholder="e.g. Aurenity_BDX_2026-05.xlsx" />
                </Field>
                <Button onClick={onGenerate}
                  disabled={busy || !!confirm || !templateId || (!uploadId && !policyIds.trim())}>
                  <FileDown size={14} /> Validate & Generate
                </Button>

                {msg && !confirm && <p className="text-sm text-ink-muted">{msg}</p>}
                <p className="text-[11px] text-ink-soft">
                  {uploadId
                    ? "Output is validated before generation; if exceptions are found you'll be asked to confirm."
                    : "Validation runs only when generating from an upload."}
                </p>
              </div>
            </Card>
          </div>
        )}

        {/* ── New template from sample tab — when a template already exists ── */}
        {tab === "new-template" && !loadingTemplates && templates.length > 0 && (
          <div className="max-w-3xl">
            <Card title="New Template From Sample">
              <p className="text-sm text-ink-muted mb-3">
                Preferred: upload an accepted BDX. AI reverse-engineers the structure.
              </p>
              <Field label="Template">
                <Select value={isNewTemplate ? "__new__" : newName}
                  onChange={e => {
                    const v = e.target.value;
                    if (v === "__new__") { setIsNewTemplate(true); setNewName(""); }
                    else { setIsNewTemplate(false); setNewName(v); }
                  }}>
                  <option value="">Choose a template…</option>
                  {templateNames.map(n => <option key={n} value={n}>{n}</option>)}
                  <option value="__new__">➕ Add New Template…</option>
                </Select>
                <p className="text-[11px] text-ink-soft mt-1">
                  Pick an existing template to add a new <b>version</b>, or add a
                  new one. Activate any version below to use it for generation.
                </p>
              </Field>
              <div className="grid grid-cols-2 gap-3 mt-3">
                {isNewTemplate && (
                  <Field label="New template name *">
                    <TextInput value={newName} autoFocus
                      onChange={e => setNewName(e.target.value)}
                      placeholder="e.g. Pinnacle Standard" />
                  </Field>
                )}
                <Field label="Carrier">
                  <Select value={selectedCarrierId}
                    onChange={e => setSelectedCarrierId(e.target.value ? Number(e.target.value) : "")}>
                    <option value="">Choose a Carrier…</option>
                    {carriers.map(c => (
                      <option key={c.id} value={c.id}>{c.legal_name}</option>
                    ))}
                  </Select>
                </Field>
                {isNewTemplate && (
                  <Field label="Output format">
                    <Select value={outputFormat}
                      onChange={e => setOutputFormat(e.target.value)}>
                      <option value="xlsx">Excel (.xlsx)</option>
                      <option value="csv">CSV (.csv)</option>
                      <option value="xml">XML (.xml)</option>
                      <option value="json">JSON (.json)</option>
                    </Select>
                  </Field>
                )}
              </div>

              {/* Contract — required, upload new alongside template or link existing */}
              <div className="mt-3 rounded-lg border border-border bg-surface-2/50 p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-ink-muted uppercase tracking-wide">
                    Contract <span className="font-normal normal-case text-red-600">(required)</span>
                  </span>
                  <span className="flex gap-1.5">
                    <button
                      onClick={() => { setContractMode("new"); setSelectedContractId(""); }}
                      className={`px-2 py-0.5 rounded text-[11px] border transition
                        ${contractMode === "new"
                          ? "bg-navy text-white border-navy"
                          : "border-border text-ink-muted hover:bg-surface-2"}`}>
                      Upload New
                    </button>
                    <button
                      onClick={() => { setContractMode("existing"); setContractFile(null); }}
                      className={`px-2 py-0.5 rounded text-[11px] border transition
                        ${contractMode === "existing"
                          ? "bg-navy text-white border-navy"
                          : "border-border text-ink-muted hover:bg-surface-2"}`}>
                      Link Existing
                    </button>
                  </span>
                </div>

                {contractMode === "new" ? (
                  <div className="space-y-2">
                    <p className="text-[11px] text-ink-muted">
                      The output template and contract are uploaded together. The AI maps contract
                      clauses to the output template's column names — not the raw database schema.
                    </p>
                    {selectedCarrierId && !carrierScopeLoading && programsForCarrier.length > 1 && (
                      <Field label="Program">
                        <Select value={selectedProgramId}
                          onChange={e => setSelectedProgramId(e.target.value ? Number(e.target.value) : "")}
                          disabled={!selectedCarrierId}>
                          <option value="">Select Program…</option>
                          {programsForCarrier.map(p => (
                            <option key={p.id} value={p.id}>{p.name}</option>
                          ))}
                        </Select>
                      </Field>
                    )}
                    {selectedCarrierId && !carrierScopeLoading && programsForCarrier.length === 0 && (
                      <Field label="Program (none yet — name a new one)">
                        <TextInput value={newProgramName}
                          onChange={e => setNewProgramName(e.target.value)}
                          placeholder="e.g. Aurenity GL Program" />
                      </Field>
                    )}
                    {contractFile ? (
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-emerald-700 font-medium truncate flex-1">
                          ✓ {contractFile.name}
                        </span>
                        <button
                          onClick={() => { setContractFile(null); if (contractRef.current) contractRef.current.value = ""; }}
                          className="text-[11px] text-ink-muted underline">Remove</button>
                      </div>
                    ) : (
                      <Button variant="ghost" onClick={() => contractRef.current?.click()}
                        disabled={!selectedCarrierId}>
                        <FileSpreadsheet size={13} /> Choose Contract (.pdf/.docx)
                      </Button>
                    )}
                    <input ref={contractRef} hidden type="file" accept=".pdf,.doc,.docx,.txt"
                      onChange={e => setContractFile(e.target.files?.[0] ?? null)} />
                  </div>
                ) : (
                  <div className="space-y-1">
                    <p className="text-[11px] text-ink-muted">
                      Adding a new version of the output template for an existing contract.
                    </p>
                    <Select value={selectedContractId}
                      onChange={e => setSelectedContractId(e.target.value ? Number(e.target.value) : "")}
                      disabled={!selectedCarrierId}>
                      <option value="">Choose a Contract…</option>
                      {contractsForCarrier.map(c => (
                        <option key={c.id} value={c.id}>
                          {(c.program_name ? c.program_name + " · " : "") + c.filename}
                        </option>
                      ))}
                    </Select>
                  </div>
                )}
              </div>

              {/* Output template sample — choose, review, then process */}
              <div className="mt-3 space-y-2">
                {templateFile ? (
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-emerald-700 font-medium truncate flex-1">
                      ✓ {templateFile.name}
                    </span>
                    <button
                      onClick={() => { setTemplateFile(null); if (fileRef.current) fileRef.current.value = ""; }}
                      className="text-[11px] text-ink-muted underline">Remove</button>
                  </div>
                ) : (
                  <Button variant="ghost" onClick={() => fileRef.current?.click()}
                    disabled={!selectedCarrierId}>
                    <FileSpreadsheet size={13} /> Choose Output Template Sample (.xlsx/.csv/.xml)
                  </Button>
                )}
                <input ref={fileRef} hidden type="file" accept=".xlsx,.xls,.csv,.xml,.json"
                  onChange={e => setTemplateFile(e.target.files?.[0] ?? null)} />
              </div>

              {/* Reference documents (optional) — ATTACHED now (not processed),
                  then sent together with the contract when the user clicks Upload.
                  Their text is fed into the extraction LLM so clauses that defer
                  to them resolve against the real content. */}
              {contractMode === "new" && uploadedRefFiles.length > 0 && (
                <div className="mt-3 flex items-center gap-2 flex-wrap">
                  <span className="text-xs text-emerald-700 font-medium">
                    ✓ {uploadedRefFiles.length} reference document(s): {uploadedRefFiles.map(f => f.name).join(", ")}
                  </span>
                  <button
                    onClick={() => { setUploadedRefFiles([]); if (mainRefDocRef.current) mainRefDocRef.current.value = ""; }}
                    className="text-[11px] text-ink-muted underline">Remove</button>
                </div>
              )}

              <div className="mt-3 flex items-center gap-2 flex-wrap">
                <Button variant="secondary"
                  onClick={() => templateFile && uploadTemplate(templateFile, uploadedRefFiles)}
                  disabled={busy || !newName.trim() || !selectedCarrierId || !templateFile
                    || (contractMode === "new" && (!contractFile
                        || (!selectedProgramId && !(programsForCarrier.length === 0 && newProgramName.trim()))))
                    || (contractMode === "existing" && !selectedContractId)}>
                  <Upload size={14} />
                  {contractMode === "new"
                    ? "Upload Output Template + Contract →"
                    : "Upload Output Template →"}
                </Button>

                {contractMode === "new" && (
                  <Button variant="secondary" onClick={() => mainRefDocRef.current?.click()}
                    disabled={busy || !selectedCarrierId}>
                    <Upload size={13} />
                    {uploadedRefFiles.length > 0 ? "Add More Reference Documents" : "Upload Reference Document (Optional)"}
                  </Button>
                )}
                <input ref={mainRefDocRef} hidden type="file" multiple
                  onChange={e => {
                    const fs = Array.from(e.target.files || []);
                    if (fs.length) setUploadedRefFiles(prev => [...prev, ...fs]);
                    if (mainRefDocRef.current) mainRefDocRef.current.value = "";
                  }} />
              </div>
              {uploadErr && <div className="mt-2 text-sm text-danger">{uploadErr}</div>}
            </Card>
          </div>
        )}

        {/* Generate output — empty state until a template exists */}
        {tab === "generate" && !loadingTemplates && templates.length === 0 && (
          <Card title="Generate Output">
            <p className="text-sm text-ink-muted">
              No output template yet. Switch to <b>New Template From Sample</b> to
              create one, then come back here to validate &amp; generate.
            </p>
          </Card>
        )}

        {/* Validation exceptions — persisted from the latest run for the
            selected upload (survives reloads). Same layout as Downloads:
            File · Stage · Exceptions (click to view) · Generated. */}
        {tab === "generate" && storedExc.length > 0 && (() => {
          const count = storedRun?.violations_count ?? storedExc.length;
          const file = uploads.find(u => u.id === Number(uploadId))?.source_file
            ?? `upload #${uploadId}`;
          return (
            <Card title={`Validation Exceptions (${count})`}
              action={<span className="text-xs text-ink-muted">
                Exceptions from the latest validation of the selected upload.
              </span>}>
              <table>
                <thead><tr>
                  <th>File</th><th>Stage</th><th>Exceptions</th><th>Generated</th>
                </tr></thead>
                <tbody>
                  <tr>
                    <td className="font-medium">{file}</td>
                    <td className="text-ink-muted">{storedRun?.validation_stage ?? "—"}</td>
                    <td>
                      {count > 0 ? (
                        <button onClick={() => nav(`/uploads/${uploadId}/exceptions`)}
                          className="pill pill-amber hover:underline">
                          {count} exception{count === 1 ? "" : "s"}
                        </button>
                      ) : (
                        <span className="pill pill-green">Clean</span>
                      )}
                    </td>
                    <td className="text-ink-muted">
                      {fmtStamp(storedRun?.completed_at ?? storedRun?.started_at)}
                    </td>
                  </tr>
                </tbody>
              </table>
            </Card>
          );
        })()}

        {/* Downloads — generated output history (Generate tab) */}
        {tab === "generate" && (
        <Card title={`Downloads${downloads.length ? ` (${downloads.length})` : ""}`}
          action={<span className="text-xs text-ink-muted">
            Re-download a generated file, view its data, or review exceptions
            found when it was checked against the template.
          </span>}>
          <table>
            <thead><tr>
              <th>File</th><th>Template</th><th>Policies</th><th>Exceptions</th>
              <th>Generated</th><th></th>
            </tr></thead>
            <tbody>
              {downloads.map(d => (
                <tr key={d.id}>
                  <td className="font-medium">
                    {d.filename}
                    {d.generated_by && (
                      <div className="text-[11px] text-ink-soft">by {d.generated_by}</div>
                    )}
                  </td>
                  <td className="text-ink-muted">{d.template_name ?? "—"}</td>
                  <td>{d.policy_count}</td>
                  <td>
                    {d.exception_count > 0 ? (
                      <button onClick={() => nav(`/uploads/${d.source_upload_id ?? uploadId}/exceptions?download=${d.id}`)}
                        className="pill pill-amber hover:underline">
                        {d.exception_count} exception{d.exception_count === 1 ? "" : "s"}
                      </button>
                    ) : (
                      <span className="pill pill-green">clean</span>
                    )}
                  </td>
                  <td className="text-ink-muted">
                    {fmtStamp(d.created_at, "")}
                  </td>
                  <td className="whitespace-nowrap">
                    <button onClick={() => openExportData(d)}
                      className="text-xs underline text-accent mr-3">View Data</button>
                    <button onClick={() => downloadExport(d.id, d.filename)}
                      className="text-xs underline">Download</button>
                  </td>
                </tr>
              ))}
              {loadingDownloads
                ? [1, 2, 3].map(i => (
                    <tr key={i}>
                      {[1, 2, 3, 4, 5, 6].map(j => (
                        <td key={j}><Sk className="h-4" /></td>
                      ))}
                    </tr>
                  ))
                : downloads.length === 0 && (
                    <tr><td colSpan={6} className="text-center text-ink-muted py-6">
                      No downloads yet. Generate an output above.
                    </td></tr>
                  )}
            </tbody>
          </table>
        </Card>
        )}

        {/* Templates (library) — New template from sample tab */}
        {tab === "new-template" && !loadingTemplates && groups.length > 0 && (
          <Card
            title={`Templates (${groups.length})`}
            action={<span className="text-xs text-ink-muted">
              Each template groups its versions. Activate any version to make it
              the one used to generate output.
            </span>}>
            <div className="space-y-7">
              {groups.map(g => (
                <div key={g.name}>
                  <div className="flex items-center gap-2 mb-2">
                    <span className="font-semibold text-sm">{g.name}</span>
                    <span className="pill pill-grey">
                      {g.versions.length} version{g.versions.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  <table>
                    <thead><tr>
                      <th>Ver.</th><th>Carrier</th><th>Sheets</th>
                      <th>Status</th><th></th>
                    </tr></thead>
                    <tbody>
                      {g.versions.map(t => (
                        <TemplateRow key={t.id} t={t} onActivate={activate} />
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </div>
          </Card>
        )}
      </PageBody>

      {dataView && (
        <OutputDataModal name={dataView.name} sheets={dataView.sheets}
          loading={dataLoading} onClose={() => setDataView(null)} />
      )}
      {excView && (
        <ExceptionsModal download={excView} exceptions={excList}
          onClose={() => { setExcView(null); setExcList(null); }} />
      )}

      {/* Persisted validation-exceptions detail — wide popup, same size/style
          as the Downloads ExceptionsModal. */}
      {showExcModal && (
        <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6"
          onClick={() => setShowExcModal(false)}>
          <div className="bg-white rounded-xl shadow-xl w-full max-w-4xl max-h-[85vh] flex flex-col"
            onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-5 py-3 border-b border-border">
              <div>
                <div className="font-semibold text-sm flex items-center gap-2">
                  <AlertTriangle size={15} className="text-amber-500" />
                  Validation Exceptions
                  {uploads.find(u => u.id === Number(uploadId))?.source_file && (
                    <span className="text-ink-muted">
                      · {uploads.find(u => u.id === Number(uploadId))?.source_file}
                    </span>
                  )}
                </div>
                <div className="text-xs text-ink-muted">
                  {(storedRun?.violations_count ?? storedExc.length)} found
                  {storedRun?.validation_stage ? ` · ${storedRun.validation_stage}-stage` : ""}
                  {!!storedRun?.critical_count && ` · ${storedRun.critical_count} critical`}
                  {!!storedRun?.warning_count && ` · ${storedRun.warning_count} warning`}
                </div>
              </div>
              <button onClick={() => setShowExcModal(false)}
                className="text-ink-muted hover:text-ink"><X size={18} /></button>
            </div>
            <div className="overflow-auto p-4">
              <ExceptionCards
                exceptions={storedExc}
                label={`upload_${uploadId}`}
                mapperSpec={storedMapperSpec}
                uploadId={uploadId ? Number(uploadId) : undefined}
              />
            </div>
          </div>
        </div>
      )}

      {/* Output-stage validation exceptions — confirmation popup */}
      <Modal
        open={!!confirm}
        size="3xl"
        title="Validation Exceptions Found"
        onClose={() => { setConfirm(null); setEditMode(false); setMsg("Cancelled."); }}
        footer={
          <>
            <Button variant="ghost"
              onClick={() => { setConfirm(null); setEditMode(false); setMsg("Cancelled."); }}
              disabled={busy}>
              No, Cancel
            </Button>
            {editMode ? (
              <Button variant="secondary" onClick={() => setEditMode(false)} disabled={busy}>
                Back To Summary
              </Button>
            ) : (
              <Button variant="secondary"
                onClick={() => { setEditMode(true); loadExceptions(); }}
                disabled={busy}>
                Modify Data Here
              </Button>
            )}
            <Button variant="danger" onClick={doGenerate} disabled={busy}>
              Yes, Generate Anyway
            </Button>
          </>
        }>
        {confirm && !editMode && (
          <>
            <p className="text-sm text-ink-muted mb-3">
              Output validation found <b>{confirm.total}</b> exception(s)
              {confirm.critical > 0 && <> · <span className="text-danger font-medium">{confirm.critical} critical</span></>}
              {confirm.warning > 0 && <> · {confirm.warning} warning</>}.
              Do you want to proceed with output generation, or
              <b> Modify Data Here</b> to fix the flagged values?
            </p>
            {confirm.violations.length > 0 && (
              <div className="rounded-lg border border-border divide-y divide-border max-h-[55vh] overflow-y-auto">
                {confirm.violations.map((v, i) => (
                  <div key={i} className="px-3 py-2 flex items-start gap-2">
                    <span className={SEV_PILL[v.severity]}>{v.severity}</span>
                    <div className="min-w-0">
                      <div className="text-sm font-medium truncate">{v.ruleName}</div>
                      <div className="text-xs text-ink-muted">
                        {v.message || `${v.field ?? ""}${v.actualValue != null ? ` = ${v.actualValue}` : ""}`}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
        {confirm && editMode && (
          <ModifyHerePanel
            uploadId={uploadId ? Number(uploadId) : undefined}
            templateId={templateId ? Number(templateId) : undefined}
            contractId={templates.find(t => t.id === Number(templateId))?.contract_id ?? null}
            exceptions={storedExc}
            onSaved={loadExceptions}
          />
        )}
      </Modal>

      {/* External document references — halt popup (S-XX) */}
      <Modal
        open={!!refsPrompt}
        size="lg"
        title="Contract References External Documents"
        onClose={() => { setRefsPrompt(null); setRefFiles([]); }}
        footer={
          <>
            <Button variant="ghost"
              onClick={() => { setRefsPrompt(null); setRefFiles([]); }}
              disabled={busy}>
              Cancel
            </Button>
            <Button variant="secondary"
              onClick={() => refDocRef.current?.click()}
              disabled={busy}>
              <Upload size={14} /> Upload Reference Document
            </Button>
            <Button variant="primary" onClick={continueWithoutReferences} disabled={busy}>
              Continue Anyway
            </Button>
          </>
        }>
        {refsPrompt && (
          <>
            <p className="text-sm text-ink-muted mb-3">
              This contract defers some rules to external document(s) not included in
              the upload. Upload the referenced document(s) so those rules can be
              generated, or continue anyway to skip them for now.
            </p>
            <div className="rounded-lg border border-border divide-y divide-border max-h-[40vh] overflow-y-auto">
              {refsPrompt.refs.length === 0 && (
                <div className="px-3 py-2 text-sm text-ink-muted">
                  (no document name detected)
                </div>
              )}
              {refsPrompt.refs.map((r, i) => (
                <div key={i} className="px-3 py-2 flex items-start gap-2">
                  <AlertTriangle size={15} className="text-amber-500 mt-0.5 shrink-0" />
                  <div className="min-w-0">
                    <div className="text-sm font-medium">
                      {r.document_name || "(unnamed reference)"}
                      {r.version_or_date && (
                        <span className="text-ink-muted"> · {r.version_or_date}</span>
                      )}
                    </div>
                    {r.source_texts?.[0] && (
                      <div className="text-xs text-ink-muted">
                        {r.source_texts[0]}
                        {r.source_texts.length > 1 && (
                          <span className="text-ink-muted"> +{r.source_texts.length - 1} more</span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
            {refFiles.length > 0 && (
              <div className="mt-3 text-xs text-ink-muted">
                Selected: {refFiles.map(f => f.name).join(", ")}
                <span className="ml-1 text-amber-600">
                  (uploading &amp; re-running extraction with this document…)
                </span>
              </div>
            )}
            <input ref={refDocRef} type="file" multiple hidden
              onChange={e => {
                const fs = Array.from(e.target.files || []);
                setRefFiles(fs);
                if (fs.length) submitReferenceDocuments(fs);
              }} />
          </>
        )}
      </Modal>
    </>
  );
}

// "Modify here" inline editor: one row per OPEN exception. The user types
// corrected values and clicks a SINGLE "Save changes" — the backend re-validates
// them all together and writes one new SCD-2 version per affected record.
function ModifyHerePanel({ uploadId, templateId, contractId, exceptions, onSaved }: {
  uploadId?: number; templateId?: number; contractId?: number | null;
  exceptions: StoredException[]; onSaved?: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState(false);
  const [resp, setResp] = useState<FieldsSaveResponse | null>(null);
  const [sqlOpen, setSqlOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  // Dedupe by exception_id — a duplicated row would otherwise share a draft input.
  const open = Array.from(
    new Map(
      exceptions
        .filter(e => (e.status ?? "open") !== "resolved")
        .map(e => [e.exception_id, e])
    ).values()
  );
  const byExc = new Map((resp?.results ?? []).map(r => [r.exceptionId, r]));

  function label(e: StoredException): string {
    return e.policy_number || e.external_policy_number || e.certificate_number
      || (e.source_entity_id != null ? `policy #${e.source_entity_id}` : "—");
  }

  const pending = open.filter(e => (drafts[e.exception_id] ?? "").trim() !== "");

  async function saveAll() {
    if (uploadId == null || templateId == null || pending.length === 0) return;
    setSaving(true); setResp(null);
    try {
      const r = await saveFields({
        uploadId, templateId, contractId,
        edits: pending.map(e => ({
          fieldPath: e.field_path ?? "",
          newValue: drafts[e.exception_id],
          policyId: e.source_entity_id ?? undefined,
          exceptionId: e.exception_id,
          actualValue: e.actual_value ?? undefined,
        })),
        apply: true,
      });
      setResp(r);
      if (r.ok && r.applied) { setDrafts({}); onSaved?.(); }
    } catch (err: any) {
      setResp({ ok: false, results: [],
        reason: err?.response?.data?.detail ?? err?.message ?? "Save failed." });
    } finally { setSaving(false); }
  }

  async function copySql(sql: string) {
    try { await navigator.clipboard.writeText(sql); setCopied(true); setTimeout(() => setCopied(false), 1500); }
    catch { /* clipboard blocked */ }
  }

  if (!uploadId || !templateId)
    return <p className="text-sm text-ink-muted">Select an upload and an output template first.</p>;
  if (open.length === 0)
    return <p className="text-sm text-ink-muted">No open exceptions to modify.</p>;

  return (
    <div className="flex flex-col" style={{ maxHeight: "62vh" }}>
      <p className="text-xs text-ink-muted mb-2">
        Enter corrected values, then click <b>Save changes</b> once. All values are re-validated
        together; if clean, each record gets a <b>single</b> new SCD-2 version (the previous version
        is kept inactive with the same id — nothing is deleted) and its exceptions resolve. Several
        fields on the same policy become ONE new version. Pure aggregates of multiple rows can’t be
        edited here.
      </p>

      <div className="space-y-2 overflow-y-auto pr-1" style={{ flex: 1 }}>
        {open.map(e => {
          const rr = byExc.get(e.exception_id);
          return (
            <div key={e.exception_id} className="rounded-lg border border-border p-3 space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                {e.severity && <span className={SEV_PILL[e.severity]}>{e.severity}</span>}
                <span className="text-sm font-medium">{label(e)}</span>
                <span className="text-xs text-ink-muted">· {e.field_path ?? "(no field)"}</span>
                {e.rule_name && <span className="text-xs text-ink-muted truncate">· {e.rule_name}</span>}
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2 items-end">
                <div className="text-xs">
                  <div className="text-ink-muted mb-0.5">Current value</div>
                  <div className="px-2 py-1 rounded border border-border bg-surface-muted truncate">
                    {e.actual_value ?? "—"}
                  </div>
                  {(() => {
                    // The reviewer's wording for the constraint — the same one
                    // the exception screens show. A format rule reads as its
                    // EXAMPLE ("e.g. 1234"), never as the rule's raw pattern,
                    // and it is labelled as an example so nobody types it in as
                    // the answer. Falls back to the stored expected value.
                    const rp = recoParts(e);
                    if (rp)
                      return (
                        <div className="text-ink-muted mt-1">
                          {rp.illustrative ? "Example: " : "Expected: "}
                          {rp.sample ? `e.g. ${rp.value}` : rp.value}
                        </div>
                      );
                    return e.expected_value != null
                      ? <div className="text-ink-muted mt-1">Expected: {e.expected_value}</div>
                      : null;
                  })()}
                </div>
                <div className="text-xs">
                  <div className="text-ink-muted mb-0.5">New value</div>
                  <input
                    className="w-full px-2 py-1 rounded border border-border"
                    value={drafts[e.exception_id] ?? ""}
                    onChange={ev => setDrafts(p => ({ ...p, [e.exception_id]: ev.target.value }))}
                    placeholder="corrected value"
                  />
                </div>
              </div>
              {rr && !rr.ok && (
                <div className="text-xs rounded border border-danger/40 bg-danger/5 px-2 py-1.5 text-danger">
                  {rr.editable === false ? `Not editable — ${rr.reason}` : (rr.reason ?? "Could not save.")}
                </div>
              )}
              {rr && rr.ok && (
                <div className="text-xs text-green-700 flex items-center gap-1">
                  <Check size={13} /> Saved → {rr.table}.{rr.column}
                  {rr.ambiguous && (
                    <span className="text-amber-600"> (multiple matching rows — used the first)</span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* single save bar */}
      <div className="mt-3 border-t border-border pt-3 space-y-2">
        {resp && !resp.ok && (
          <div className="text-xs rounded border border-danger/40 bg-danger/5 px-2 py-1.5 text-danger">
            {resp.reason}
            {resp.introducedExceptions && resp.introducedExceptions.length > 0 && (
              <ul className="list-disc ml-4 mt-1">
                {resp.introducedExceptions.map((x, i) => (
                  <li key={i}>
                    rule {x.rule_id ?? "?"}{x.field_path ? ` · ${x.field_path}` : ""}
                    {x.actual_value != null ? ` → ${x.actual_value}` : ""}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {resp && resp.ok && resp.applied && (
          <div className="text-xs text-green-700 flex items-center gap-1">
            <Check size={13} /> Saved {resp.recordsVersioned} record(s) as new versions.
          </div>
        )}
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs text-ink-muted">
            {pending.length} value(s) entered.
            {resp?.sql && (
              <>
                {" · "}
                <button className="underline" onClick={() => setSqlOpen(o => !o)}>
                  {sqlOpen ? "Hide SQL" : "View SQL"}
                </button>
                {sqlOpen && (
                  <button className="underline ml-2" onClick={() => copySql(resp.sql ?? "")}>
                    {copied ? "Copied" : "Copy"}
                  </button>
                )}
              </>
            )}
          </div>
          <Button variant="primary" disabled={saving || pending.length === 0} onClick={saveAll}>
            {`Save ${pending.length || ""} change${pending.length === 1 ? "" : "s"}`}
          </Button>
        </div>
        {sqlOpen && resp?.sql && (
          <pre className="text-[11px] bg-ink/90 text-white rounded-lg p-3 overflow-x-auto whitespace-pre max-h-48">{resp.sql}</pre>
        )}
      </div>
    </div>
  );
}

// Read-only viewer of a generated output file's rendered rows (one tab/sheet).
function OutputDataModal({ name, sheets, loading, onClose }: {
  name: string; sheets: SheetGrid[]; loading: boolean; onClose: () => void;
}) {
  const [tab, setTab] = useState(0);
  const g = sheets[tab];
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6"
      onClick={onClose}>
      <div className="bg-white rounded-xl shadow-xl w-full max-w-6xl max-h-[85vh] flex flex-col"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b border-border">
          <div className="font-semibold text-sm">Output Data · {name}</div>
          <button onClick={onClose} className="text-ink-muted hover:text-ink"><X size={18} /></button>
        </div>
        {!loading && sheets.length > 1 && (
          <div className="px-5 py-2 border-b border-border flex gap-2 flex-wrap">
            {sheets.map((s, i) => (
              <button key={s.sheet} onClick={() => setTab(i)}
                className={`text-xs px-2.5 py-1 rounded-full ${i === tab ? "bg-navy text-white" : "bg-surface-2 text-ink-muted"}`}>
                {s.sheet} ({Math.max(0, s.rows.length - 1)})
              </button>
            ))}
          </div>
        )}
        <div className="overflow-auto p-4">
          {!g || g.rows.length === 0 ? (
            <div className="text-center text-ink-muted py-10">No rows in this file.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="text-xs">
                <thead><tr>
                  {(g.rows[0] ?? []).map((c, i) => <th key={i}>{String(c ?? "").trim() || `Column ${i + 1}`}</th>)}
                </tr></thead>
                <tbody>
                  {g.rows.slice(1, 500).map((row, ri) => (
                    <tr key={ri}>
                      {row.map((c, ci) => (
                        <td key={ci} className="whitespace-nowrap max-w-[280px] truncate"
                          title={String(c ?? "")}>{String(c ?? "")}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Map a generate-time OutputException to the StoredException shape that
// ExceptionCards groups and renders (clause text, page, contract download…).
function outputExcToStored(x: OutputException, i: number): StoredException {
  const sev = (x.severity || "").toLowerCase();
  const severity = sev === "critical" || sev === "error" || sev === "high"
    ? "critical"
    : sev === "info" || sev === "low" || sev === "notice"
    ? "info"
    : "warning";
  return {
    exception_id: i,
    rule_id: x.rule_id ?? null,
    source_entity: null,
    source_entity_id: null,
    severity: severity as any,
    field_path: x.column ?? x.field ?? null,
    expected_value: null,
    actual_value: x.actual_value ?? null,
    status: "open",
    created_at: null,
    policy_number: x.policy_number ?? null,
    external_policy_number: null,
    certificate_number: null,
    rule_name: x.rule_name ?? x.code ?? null,
    error_message: x.reason ?? x.message ?? null,
    // Hand-enumerated, like the copy in api/validation.ts: a field omitted here
    // is silently dropped for everything this mapper feeds, and `explanation?`
    // being optional means tsc will not catch the omission.
    explanation: x.explanation ?? null,
    contract_clause_text: x.contract_clause_text ?? null,
    contract_clause_page: x.contract_clause_page ?? null,
    rule_contract_id: x.contract_id ?? null,
    contract_filename: x.contract_filename ?? null,
    // Without these the modal merges a "not a number" row back into the rule's
    // own card and heads it with the rule's name — the display this fixed.
    check_kind: x.check_kind ?? null,
    root_cause: (x.root_cause as StoredException["root_cause"]) ?? null,
  };
}

// Exceptions screen for one generated download — grouped contract-rule cards.
// Only real data violations are shown; referral/manual-review clauses that have
// no data check are returned separately (unprocessable_rules) and excluded here.
function ExceptionsModal({ download, exceptions, onClose }: {
  download: ExportDownload; exceptions: OutputException[] | null;
  onClose: () => void;
}) {
  const violations = (exceptions ?? [])
    .filter(x => x.error_class !== "unprocessable_rule");
  const stored = violations.map(outputExcToStored);
  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-6"
      onClick={onClose}>
      <div className="bg-white rounded-xl shadow-xl w-full max-w-4xl max-h-[85vh] flex flex-col"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b border-border">
          <div>
            <div className="font-semibold text-sm flex items-center gap-2">
              <AlertTriangle size={15} className="text-amber-500" />
              Validation Exceptions · {download.filename}
            </div>
            <div className="text-xs text-ink-muted">
              {violations.length} found · output-stage · contract-rule validation
            </div>
          </div>
          <button onClick={onClose} className="text-ink-muted hover:text-ink"><X size={18} /></button>
        </div>
        <div className="overflow-auto p-4">
          {violations.length === 0 ? (
            <div className="text-center text-ink-muted py-10">
              No exceptions — this output passed all contract-rule checks.
            </div>
          ) : (
            <ExceptionCards
              exceptions={stored}
              label={`export_${download.id}`}
              uploadId={download.source_upload_id ?? undefined}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// One template-version row inside a template group.
function TemplateRow({ t, onActivate }: {
  t: Template; onActivate: (id: number) => void;
}) {
  return (
    <tr>
      <td>
        <div className="flex items-center gap-2">
          <Link to={`/outputs/templates/${t.id}`} className="font-medium">
            v{t.version ?? 1}
          </Link>
          {t.is_active && <span className="pill pill-green">Active</span>}
        </div>
        <div className="text-[11px] text-ink-muted">#{t.id}</div>
      </td>
      <td className="text-ink-muted">{t.carrier ?? "—"}</td>
      <td className="text-ink-muted">
        {(t.structure?.sheets ?? []).map((s: any) => s.sheet_name).join(", ")}
      </td>
      <td>
        <span className={`pill ${t.approved ? "pill-green" : "pill-amber"}`}>
          {t.approved ? "Approved" : "Draft"}
        </span>
      </td>
      <td className="whitespace-nowrap">
        {!t.is_active && (
          <button onClick={() => onActivate(t.id)}
            className="text-xs underline text-accent mr-3">
            Activate
          </button>
        )}
        <Link to={`/outputs/templates/${t.id}`} className="text-xs underline">
          Configure ›
        </Link>
      </td>
    </tr>
  );
}

function UploadProgress({ stage, file, err, onCancel }: {
  stage: Stage; file: File | null; err: string | null; onCancel: () => void;
}) {
  const steps: { key: Stage; label: string }[] = [
    { key: "uploading", label: "Upload File" },
    { key: "parsing", label: "Read Workbook & Detect Headers" },
    { key: "ai_mapping", label: "AI Map Columns To Canonical Fields" },
    { key: "done", label: "Open Template" },
  ];
  const order: Stage[] = ["uploading", "parsing", "ai_mapping", "done"];
  const idx = order.indexOf(stage);

  return (
    <Card title="Configuring Template"
      action={
        <Button variant="ghost" onClick={onCancel}
          disabled={stage === "done"}>Cancel</Button>
      }>
      {file && (
        <div className="flex items-center gap-2 text-sm text-ink-muted mb-4">
          <FileSpreadsheet size={14} /> {file.name}
        </div>
      )}
      <ol className="space-y-3">
        {steps.map((s, i) => {
          const done = stage === "done" || i < idx;
          const active = stage !== "done" && i === idx;
          return (
            <li key={s.key} className="flex items-center gap-3">
              <span className={`w-7 h-7 rounded-full flex items-center justify-center text-[11px] font-semibold
                ${done ? "bg-emerald-100 text-emerald-700"
                  : active ? "bg-navy text-white"
                    : "bg-surface-2 text-ink-soft"}`}>
                {done ? <Check size={14} />
                  : active ? <Loader2 size={14} className="animate-spin" />
                    : i + 1}
              </span>
              <span className={`text-sm ${active ? "font-medium text-ink" : "text-ink-muted"}`}>
                {s.label}
              </span>
            </li>
          );
        })}
      </ol>
      {err && <div className="mt-3 text-sm text-danger">{err}</div>}
    </Card>
  );
}
