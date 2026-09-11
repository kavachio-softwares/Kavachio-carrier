import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { History, AlertTriangle, Download } from "lucide-react";
import { api, downloadFile, downloadErrorText } from "../api/client";
import { LoadingOverlay } from "../components/Busy";
import { Dropzone } from "../components/Dropzone";
import { Modal } from "../components/ui/Modal";
import { currentMga, isTenantAdmin } from "../auth";
import { type Sheet } from "../components/OutputRows";
import { RunResult, fetchPreview, type RunResp } from "../components/RunResult";
import { useBrokerContractScope } from "../components/BrokerContractScope";
import { resolveOutputTemplate, type ResolveResult } from "../api/outputTemplate";
import { contractLabel } from "../utils/contractLabel";

type Party = { id: number; legal_name: string; is_active?: boolean };
type Program = { id: number; name: string; status?: string; party_id?: number | null };
type Pipeline = { id: number; name: string | null; status: "draft" | "active" | "superseded"; has_supplement?: boolean };
// The run payload and its parts live with the component that renders them, so
// this screen and the broker's cannot describe the same response differently.
export default function DirectRun() {
  const mga = currentMga();
  const nav = useNavigate();
  const admin = isTenantAdmin();

  const [carriers, setCarriers] = useState<Party[]>([]);
  // A tenant IS a carrier, and its own carrier party is not "app managed", so
  // it never appears in /parties — which left this dropdown empty and the whole
  // screen unusable. Resolve it the way Bordereau Setup does and show it as a
  // fact rather than a choice. The dropdown below stays for any tenant that
  // genuinely does have several carrier parties to pick between.
  const [ownCarrier, setOwnCarrier] = useState<Party | null>(null);
  const [programs, setPrograms] = useState<Program[]>([]);
  const [programsLoading, setProgramsLoading] = useState(false);
  const [carrierId, setCarrierId] = useState<number | "">("");
  const [programId, setProgramId] = useState<number | "">("");
  const [setup, setSetup] = useState<Pipeline | null>(null);   // active pipeline, if any
  const [hasSetup, setHasSetup] = useState<boolean | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  // Which action is running, so only that button shows a spinner. "check" is the
  // pre-submission self-check; "run" is the real (committing) Generate BDX.
  const [mode, setMode] = useState<"run" | "check">("run");
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<RunResp | null>(null);
  // The multi-table refusal from /direct/run — shown as a modal, not the banner.
  const [multiTableModal, setMultiTableModal] = useState<string | null>(null);
  const [preview, setPreview] = useState<Sheet | null>(null);
  // Whether this tenant still needs first-time setup (carrier + Bordereau).
  const [needsSetup, setNeedsSetup] = useState(false);

  // The rest of the chain. Both optional: a programme with no brokers on it
  // runs exactly as it always did.
  const scope = useBrokerContractScope(programId);
  // Which output template these four levels resolve to. Checked BEFORE the run
  // so "no template for this contract" is answered here, with a way out, rather
  // than as a failure after the file has been uploaded.
  const [tpl, setTpl] = useState<ResolveResult | null>(null);
  const [tplLoading, setTplLoading] = useState(false);

  /** The Bordereau Setup screen, opened on the selection made here. */
  function setupHref(): string {
    const p = new URLSearchParams();
    if (carrierId !== "") p.set("carrier_party_id", String(carrierId));
    if (programId !== "") p.set("program_id", String(programId));
    if (scope.brokerPartyId !== "") p.set("broker_party_id", String(scope.brokerPartyId));
    const q = p.toString();
    return q ? `/direct/setup?${q}` : "/direct/setup";
  }

  /** The blank bordereau the active setup reads — what to fill in and upload
   *  here. See the note above the drop target for why it is not the output. */
  function downloadBordereauTemplate() {
    if (!runSetup) return;
    downloadFile(`/pipelines/${runSetup.id}/bordereau-template`)
      .catch(async e => setErr(await downloadErrorText(e,
        "We couldn't download the bordereau template — please try again.")));
  }

  function downloadOutputTemplate(templateId: number) {
    downloadFile(`/output-template/${templateId}/download`)
      .catch(async e => setErr(await downloadErrorText(e,
        "We couldn't download the output template — please try again.")));
  }

  const carrierName = carriers.find(c => c.id === carrierId)?.legal_name
    ?? (ownCarrier?.id === carrierId ? ownCarrier.legal_name : "") ?? "";
  const programName = programs.find(p => p.id === programId)?.name ?? "";
  // Carrier picked, program list finished loading, and it came back empty →
  // this carrier has no program yet. Surface it instead of a silent, empty
  // dropdown that leaves the user stuck with nothing to select.
  const noPrograms = carrierId !== "" && !programsLoading && programs.length === 0;

  // A generated output belongs to the scope it was run for, so any change to
  // that scope makes it stale.
  useEffect(() => {
    setResult(null); setPreview(null); setErr(null);
  }, [carrierId, programId, scope.brokerPartyId, scope.contractId]);

  // The FILE is dropped only when WHO the run is for changes — clearing it then
  // is what stops re-locking the Dropzone on a cleared selection from leaving a
  // stale file behind. Naming a different contract is not that: the file is the
  // bordereau, and the contract is whose terms it is checked against. Since
  // picking the contract is now something you do deliberately, wiping an
  // already-dropped file for it would be a surprise with no reason behind it.
  useEffect(() => {
    setFile(null);
  }, [carrierId, programId, scope.brokerPartyId]);

  // Resolve the output template for whatever is selected right now.
  useEffect(() => {
    if (programId === "" || carrierId === "") { setTpl(null); return; }
    let stale = false;
    setTplLoading(true);
    resolveOutputTemplate(mga, {
      program_id: Number(programId),
      carrier_party_id: Number(carrierId),
      broker_party_id: scope.brokerPartyId === "" ? null : Number(scope.brokerPartyId),
      contract_id: scope.contractId === "" ? null : Number(scope.contractId),
    })
      .then(r => { if (!stale) setTpl(r); })
      .catch(() => { if (!stale) setTpl(null); })
      .finally(() => { if (!stale) setTplLoading(false); });
    return () => { stale = true; };
  }, [mga, carrierId, programId, scope.brokerPartyId, scope.contractId]);

  useEffect(() => {
    api.get(`/parties`, { params: { mga, party_type: "carrier" } })
      .then(r => setCarriers(Array.isArray(r.data) ? r.data : (r.data?.items ?? [])))
      .catch(() => setCarriers([]));
  }, [mga]);
  useEffect(() => {
    api.get<{ id: number; legal_name: string }>(`/my-carrier-party`, { params: { mga } })
      .then(r => {
        setOwnCarrier({ id: r.data.id, legal_name: r.data.legal_name });
        // Selecting it here is what makes the programme list load — the rest of
        // the screen already keys off carrierId and needs no other change.
        setCarrierId(prev => (prev === "" ? r.data.id : prev));
      })
      .catch(() => setOwnCarrier(null));
  }, [mga]);
  // Operators can process as soon as there's an approved Bordereau Setup to run
  // against (`bordereau_ready`) — not gated on the whole onboarding wizard (which
  // also wants admin-only org fields like tenant currency).
  useEffect(() => {
    api.get<{ bordereau_ready: boolean }>(`/onboarding/status`, { params: { mga } })
      .then(r => setNeedsSetup(!r.data?.bordereau_ready))
      .catch(() => setNeedsSetup(false));
  }, [mga]);
  useEffect(() => {
    setPrograms([]); setProgramId(""); setHasSetup(null); setSetup(null);
    if (carrierId === "") { setProgramsLoading(false); return; }
    setProgramsLoading(true);
    // The tenant's own programmes. This used to read /parties/{id}/programs,
    // which only finds programmes explicitly linked to a carrier PARTY — and a
    // programme belongs to the tenant, so that column is normally empty and the
    // dropdown came back empty with it. A programme that IS linked to a party is
    // still narrowed to the selected carrier below, so both shapes work.
    api.get<Program[]>(`/programs`, { params: { mga } })
      // Inactive programs are hidden here — they can't be run against.
      .then(r => setPrograms(Array.isArray(r.data)
        ? r.data.filter(p => p.status !== "inactive"
            && (p.party_id == null || p.party_id === carrierId))
        : []))
      .catch(() => setPrograms([]))
      .finally(() => setProgramsLoading(false));
  }, [carrierId, mga]);
  useEffect(() => {
    setHasSetup(null); setSetup(null);
    if (programId === "" || carrierId === "") return;
    api.get<Pipeline[]>(`/pipelines`, { params: { mga, carrier_party_id: carrierId, program_id: programId } })
      .then(r => {
        const active = (r.data ?? []).find(p => p.status === "active") ?? null;
        setSetup(active);
        setHasSetup(!!active);
      })
      .catch(() => { setHasSetup(false); setSetup(null); });
  }, [programId]);

  function clearForm() {
    setFile(null); setResult(null); setPreview(null); setErr(null);
  }

  // Shared submit for both actions. checkOnly=true is the pre-submission
  // self-check: the backend runs every validation but does NOT ingest or record
  // a run; checkOnly=false is the real, committing Generate BDX.
  async function submit(checkOnly: boolean) {
    if (!file || carrierId === "" || programId === "") {
      setErr("Pick carrier, program and an input file."); return;
    }
    // The server refuses this too. Said here as well, because a disabled button
    // with no sentence beside it is a screen that will not say what is wrong.
    if (scope.needsContractChoice) {
      setErr("Pick which contract this bordereau was written under — its terms "
             + "are what every row is checked against."); return;
    }
    setMode(checkOnly ? "check" : "run");
    setBusy(true); setErr(null); setResult(null); setPreview(null);
    try {
      const fd = new FormData();
      fd.append("mga", mga);
      fd.append("carrier_party_id", String(carrierId));
      fd.append("program_id", String(programId));
      fd.append("file", file);
      fd.append("actor", mga);
      // Sent only when picked. Without them the run resolves its template the
      // way it always has — from the programme's active setup.
      if (scope.brokerPartyId !== "") fd.append("broker_party_id", String(scope.brokerPartyId));
      if (scope.contractId !== "") fd.append("contract_id", String(scope.contractId));
      if (checkOnly) fd.append("check_only", "true");
      const { data } = await api.post<RunResp>(`/direct/run`, fd);
      setResult(data);
      // Best-effort output preview for the result card. marks=1 so the flagged
      // cells match the downloaded file's highlighting; show the real data sheet
      // (not a spec/instruction sheet).
      fetchPreview({ file: id => `/export/downloads/${id}/file`,
                     data: (id, q) => `/export/downloads/${id}/data?${q}` },
                   data.export_id).then(setPreview);
    } catch (e: unknown) {
      const a = e as { response?: { data?: { detail?: string } }; message?: string };
      const msg = a?.response?.data?.detail ?? a?.message ?? "Run failed.";
      if (/multiple tables|more than one table/i.test(String(msg))) setMultiTableModal(String(msg));
      else setErr(msg);
    } finally { setBusy(false); }
  }



  // All required fields for either action: a carrier + program with an active
  // setup to run against, and a file to run it on.
  // A scope was named but has no output template? Then there is nothing to
  // generate INTO, and the fix is to create one — not to upload and fail.
  const templateMissing = tpl !== null && !tpl.found;
  // Or there IS one, but the setup that runs here was built against a different
  // template. Running anyway would deliver a file with the right headings and
  // no data, so it is blocked here as well as on the server.
  const templateMismatch = !!tpl?.found && !!tpl.setup && !tpl.setup.matches;
  // Several contracts and none picked is not a runnable state: the run would
  // fall back on whatever the setup was built against, which is the silent
  // wrong answer this picker exists to stop.
  const canSubmit = carrierId !== "" && programId !== "" && hasSetup === true
    && !!file && !templateMissing && !templateMismatch
    && !scope.needsContractChoice;
  // THE SETUP FOR THIS SELECTION — the one the server says a run would use, not
  // the programme's newest. A broker with two contracts on two BDX templates
  // has two live setups, so the name shown and the layout downloaded must
  // follow the contract picked. Until that answer arrives, the programme's.
  const runSetup: { id: number; name: string | null } | null = tpl?.setup
    ? { id: tpl.setup.pipeline_id, name: tpl.setup.name }
    : setup ? { id: setup.id, name: setup.name } : null;


  return (
    <div className="proto">
      {busy && <LoadingOverlay label={mode === "check"
        ? "Checking your bordereau — running every validation. Nothing is sent…"
        : "Processing bordereau — validating and generating output. This can take a few minutes…"} />}
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Process Bordereau</h2>
            <p>Pick who the bordereau is for, drop the file, generate the
              validated output.</p>
          </div>
        </div>

        {/* Tenant not configured yet: keep the page usable-looking but flag it.
            Setup is a tenant-admin job, so operators are pointed at their admin. */}
        {needsSetup && !admin && (
          <div className="note warn" style={{ marginBottom: 18, display: "flex", alignItems: "center", gap: 8 }}>
            This organization isn’t set up yet — the carrier and Bordereau Setup is
            still pending. Please ask your tenant admin to complete it before
            processing bordereau.
          </div>
        )}

        {err && (
          <div className="note warn" style={{ marginBottom: 18, display: "flex", alignItems: "center", gap: 8 }}>
            {err}
          </div>
        )}

        <Modal open={multiTableModal != null} size="xl"
          title={<span className="flex items-center gap-2">
            <AlertTriangle size={17} className="text-amber-500" /> Multiple Tables Found
          </span>}
          onClose={() => setMultiTableModal(null)}
          footer={<button className="btn pri" onClick={() => setMultiTableModal(null)}>
            Got It
          </button>}>
          <p className="text-sm">{multiTableModal}</p>
        </Modal>



        <div>
          {/* ---------------- setup + upload + previous runs ---------------- */}
          <div>
            <div className="card pad">
              <div className="row2">
                <div className="field">
                  <label>Carrier</label>
                  {ownCarrier && carriers.length === 0 ? (
                    // Nothing to choose between: this tenant's own carrier is
                    // who the bordereau is for.
                    <input value={ownCarrier.legal_name} readOnly disabled />
                  ) : (
                    <select value={carrierId}
                      onChange={e => setCarrierId(e.target.value ? Number(e.target.value) : "")}>
                      <option value="">Select Carrier…</option>
                      {/* `!== false`, not `=== true`: the API treats a null flag as
                          active and returns those rows, so `=== true` would hide them. */}
                      {ownCarrier && !carriers.some(c => c.id === ownCarrier.id) && (
                        <option value={ownCarrier.id}>{ownCarrier.legal_name}</option>
                      )}
                      {carriers
                        .filter(c => c.is_active !== false)
                        .map(c => (
                          <option key={c.id} value={c.id}>
                            {c.legal_name}
                          </option>
                        ))}
                    </select>
                  )}
                </div>
                <div className="field">
                  <label>Program</label>
                  <select value={programId} disabled={carrierId === "" || noPrograms}
                    onChange={e => setProgramId(e.target.value ? Number(e.target.value) : "")}>
                    <option value="" disabled>
                      {carrierId === "" ? "Select Program…"
                        : programsLoading ? "Loading programs…"
                        : noPrograms ? "No program for this carrier"
                        : "Select Program…"}
                    </option>
                    {programs.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
              </div>

              {/* Broker, then contract. Each narrows the next, and both narrow
                  which output template the run writes into. Left blank, the run
                  behaves exactly as it did before they existed. */}
              {/* The broker. The contract comes with them — a contract belongs
                  to one (programme, broker) pair, so there is nothing left to
                  ask once the broker is known. */}
              <div className="row2">
                <div className="field">
                  <label>Broker</label>
                  <select value={scope.brokerPartyId}
                    disabled={programId === "" || scope.brokers.length === 0}
                    onChange={e => scope.setBrokerPartyId(
                      e.target.value ? Number(e.target.value) : "")}>
                    <option value="" disabled>
                      {programId === "" ? "Select a program first"
                        : scope.brokers.length === 0 ? "No brokers on this program"
                        : "All brokers"}
                    </option>
                    {scope.brokers.map(b => (
                      <option key={b.id} value={b.id}>{b.legal_name}</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>Contract</label>
                  {/* A CHOICE ONLY WHEN THERE IS ONE.
                      With one contract on file there is nothing to ask — it is
                      the only answer and it is simply reported, exactly as this
                      field has always done.
                      With SEVERAL there is a real question, and it used to be
                      answered silently: the run measured the file against
                      whichever contract the setup was built on, whatever the
                      bordereau was actually written under. A broker's two live
                      contracts are two different sets of terms. */}
                  {scope.contracts.length > 1 ? (
                    <select value={scope.contractId}
                      onChange={e => scope.setContractId(
                        e.target.value ? Number(e.target.value) : "")}>
                      <option value="" disabled>
                        Select Contract
                      </option>
                      {scope.contracts.map(c => (
                        <option key={c.id} value={c.id}>
                          {contractLabel(c)}
                          {c.broker_name ? "" : " — carrier held"}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input readOnly disabled value={
                      programId === "" ? "Select a program first"
                        : scope.contractsLoading ? "Finding the live contracts…"
                        : scope.contracts.length === 0
                          ? "No approved contract for this selection"
                          : contractLabel(scope.contracts[0])} />
                  )}
                </div>
              </div>

              {/* What the pick decides, said once and only where it is a
                  question. The terms are what the file is checked against, so
                  naming the wrong contract is not a labelling mistake — it is a
                  bordereau measured against somebody else's binder. */}
              {scope.contracts.length > 1 && (
                <div className={`note${scope.contractId === "" ? " warn" : ""}`}
                     style={{ marginBottom: 16 }}>
                  {scope.contractId === "" ? (
                    <>
                      <b>This broker has {scope.contracts.length} live
                      contracts.</b> Pick the one this bordereau was written
                      under — its terms are what every row is checked against.
                    </>
                  ) : (
                    <>
                      Checked against the terms of{" "}
                      <b>{scope.contractName}</b>. Its clauses are the rules
                      this run applies.
                    </>
                  )}
                </div>
              )}

              {/* Which output template this run would write into — answered
                  before the file is uploaded, not after. */}
              {programId !== "" && (
                tplLoading ? (
                  <div className="note" style={{ marginBottom: 16 }}>
                    Checking which output template applies…
                  </div>
                ) : tpl && !tpl.found ? (
                  <div className="note warn" style={{ marginBottom: 16 }}>
                    <b>Output BDX Template not configured.</b> Nothing has been
                    agreed for{" "}
                    {[tpl.scope_names.carrier, tpl.scope_names.programme,
                      tpl.scope_names.broker, tpl.scope_names.contract]
                      .filter(Boolean).join(" · ")}
                    , so there is nothing to generate into.{" "}
                    {/* To the SETUP screen, not a dialog on this one. A template
                        is only half of what a run needs — the other half is the
                        setup that maps your bordereau into it — and building the
                        template here left the user back on a screen that still
                        could not run. The setup screen does both, in order, with
                        the scope already filled in. */}
                    <span className="linkish" onClick={() => nav(setupHref())}>
                      Set Up the Output BDX Template →
                    </span>
                  </div>
                ) : templateMismatch ? (
                  <div className="note warn" style={{ marginBottom: 16 }}>
                    <b>This setup writes into a different template.</b> The
                    output template agreed for this selection is{" "}
                    <b>{tpl!.template!.name}</b>, but the setup that runs here —{" "}
                    <b>{tpl!.setup!.name}</b> — was built against{" "}
                    <b>{tpl!.setup!.output_template_name}</b>. A setup learns its
                    mapping from one output template, so running it against
                    another would produce a file with the right column headings
                    and no data in it. Build a Bordereau Setup for this
                    selection first —{" "}
                    <span className="linkish" onClick={() => nav(setupHref())}>
                      Bordereau Setup →
                    </span>
                  </div>
                ) : tpl?.template ? (
                  <div className="note" style={{ marginBottom: 16, display: "flex",
                    alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    Output template <b>{tpl.template.name}</b>
                    <span className="tag-pill">v{tpl.template.version}</span>
                    <span className="tag-pill">{tpl.template.output_format.toUpperCase()}</span>
                    {tpl.template.standard_meta?.jurisdiction && (
                      <span className="tag-pill">
                        {tpl.template.standard_meta.standard}{" "}
                        {tpl.template.standard_meta.jurisdiction}
                      </span>
                    )}
                    {scope.contractId !== "" && tpl.match_level !== "contract" && (
                      <span style={{ fontSize: 11.5, color: "var(--p-faint)" }}>
                        — this is the {tpl.match_level}'s template, not one made
                        for the selected contract.
                      </span>
                    )}
                  </div>
                ) : null
              )}

              {/* <div style={{ fontSize: 11.5, color: "var(--p-faint)", margin: "-6px 0 14px" }}>
                Only carriers &amp; programs with an <b>active setup</b> generate output — there's no
                "create carrier" on this screen.
              </div> */}

              {/* Carrier picked but it has no program yet — a program is required
                  before there can be a setup to run against, so guide the user to
                  add one (linked to this carrier) instead of leaving them stuck. */}
              {noPrograms && (
                <div className="note warn" style={{ marginBottom: 16 }}>
                  {admin ? (
                    <>There is no program for this carrier yet.{" "}
                      <span className="linkish" onClick={() => nav(`/programs/new?party=${carrierId}`)}>Create a Program →</span></>
                  ) : (
                    <>There is no program for this carrier. <b>Ask your admin to add one.</b></>
                  )}
                </div>
              )}

              {/* active-setup confirmation / no-setup guidance */}
              {/* Not while the selection has no template of its own: naming a
                  setup built for another contract there reads as "this is what
                  will run", and it will not — the run is refused. */}
              {hasSetup === true && !templateMissing && (
                <div className="note" style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
                  Using setup <b>{runSetup?.name || `${carrierName} · ${programName}`}</b>
                  <span className="tag-pill">Active</span>
                </div>
              )}
              {hasSetup === false && programId !== "" && (
                <div className="note warn" style={{ marginBottom: 16 }}>
                  {admin ? (
                    <>No active setup for this carrier + program.{" "}
                      <span className="linkish" onClick={() => nav(setupHref())}>Configure It →</span></>
                  ) : (
                    <>No setup for this carrier. <b>Ask your admin to configure it.</b></>
                  )}
                </div>
              )}

              {/* WHAT TO FILL IN — offered before the drop target, not after a
                  run that could not read the file. The bordereau template is
                  the layout this setup READS (its Input Template), because a
                  run finds each column by the name it learned from that
                  layout. The output template is what Generate BDX WRITES; it
                  sits beside it for reference, and says so, because filled in
                  and uploaded its columns are only found where they share a
                  name with the input layout. */}
              {hasSetup === true && runSetup && !templateMissing && !templateMismatch && (
                <div className="note" style={{ marginBottom: 16, display: "flex",
                  alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  <span style={{ flex: 1, minWidth: 260 }}>
                    <b>Need a blank bordereau?</b> Download the layout this setup
                    reads, fill it in, and upload it below.
                    {tpl?.template && !templateMismatch && (
                      <> The output template is the file Generate BDX gives
                      back — for reference, not for filling in.</>
                    )}
                  </span>
                  <button className="btn" onClick={downloadBordereauTemplate}>
                    <Download size={14} /> Bordereau Template
                  </button>
                  {tpl?.template && !templateMismatch && (
                    <button className="btn ghost"
                      onClick={() => downloadOutputTemplate(tpl.template!.id)}>
                      <Download size={14} /> Output Template
                    </button>
                  )}
                </div>
              )}

              <Dropzone file={file} onPick={setFile}
                disabled={carrierId === "" || programId === "" || hasSetup !== true}
                />

              {setup?.has_supplement && (
                <div style={{ marginTop: 10, fontSize: 12, color: "#64748b" }}>
                  Supplementary data from the setup is captured automatically — no upload needed.
                </div>
              )}

              <div style={{ marginTop: 18, display: "flex", gap: 10, alignItems: "center" }}>
                {/* Pre-submission self-check (V-5): validate WITHOUT committing, so
                    the broker can fix issues before the real send. */}
                {/* <button className="btn" onClick={() => submit(true)}
                  disabled={busy || !canSubmit}
                  title="Run every validation without sending — see what to fix first">
                  Check My Bordereau
                </button> */}
                <button className="btn pri" onClick={() => submit(false)} disabled={busy || !canSubmit}>
                  Generate BDX
                </button>
                <button className="btn" onClick={clearForm} disabled={busy}>Clear</button>
                {/* One entry point to history: carries the selected carrier as a
                    filter when set, otherwise opens the full list. */}
                <button className="btn ghost" style={{ marginLeft: "auto" }}
                  onClick={() => nav(carrierId !== "" ? `/runs?carrier=${carrierId}&from=direct` : "/runs?from=direct")}>
                  <History size={15} /> {carrierId !== "" ? "View This Carrier's Process Bordereaux" : "View Past Process Bordereaux"}
                </button>
              </div>
            </div>

            {/* ---------------- result ---------------- */}
            {/* Rendered by the SHARED component, so the broker's Process
                Bordereau shows exactly this and cannot drift from it. Only the
                URLs differ: a carrier reads /export/downloads/*, a broker reads
                the same export through the carrier-centric chain. */}
            {result && (
              <RunResult
                result={result}
                preview={preview}
                urls={{
                  file: id => `/export/downloads/${id}/file`,
                  data: (id, q) => `/export/downloads/${id}/data?${q}`,
                }}
                onError={setErr}
                actions={!result.check_only && result.exception_count > 0 ? (
                  <button className="btn pri"
                    onClick={() => nav(`/uploads/${result.export_id}/exceptions?download=${result.export_id}&from=direct`)}>
                    Review Exceptions
                  </button>
                ) : null}
                footNote={
                  // Bordereau Setup is admin-only (see access.ts), so an
                  // Operator gets the ask-your-admin wording rather than a link
                  // that bounces them back to the dashboard.
                  admin
                    ? <span className="linkish" onClick={() => nav(setupHref())}>Review the Setup →</span>
                    : <b>Ask your admin to review the setup.</b>
                }
              />
            )}

            {admin ? (
              <div className="note" style={{ marginTop: 14 }}>Missing a setup? <span className="linkish" onClick={() => nav(setupHref())}>Configure It →</span></div>
            ) : (
              <div className="note" style={{ marginTop: 14 }}>No setup for a carrier? <b>Ask your admin to configure it.</b></div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
