import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { History, AlertTriangle } from "lucide-react";
import { api, downloadFile } from "../api/client";
import { LoadingOverlay } from "../components/Busy";
import { Modal } from "../components/ui/Modal";
import { currentMga, isTenantAdmin } from "../auth";
import {
  InlineAllRows, HighlightGrid, firstDataSheet, HL_BG, HL_BD,
  HL_WARN_BG, HL_WARN_BD, type Sheet,
} from "../components/OutputRows";

type Party = { id: number; legal_name: string; is_active?: boolean };
type Program = { id: number; name: string; status?: string };
type Pipeline = { id: number; name: string | null; status: "draft" | "active" | "superseded"; has_supplement?: boolean };
type GoverningContract = {
  sheet: string; contract_id: number | null;
  contract_filename: string | null; fallback: boolean;
};
// One row/field validation finding (the DuckDB engine's exception shape).
type RunException = {
  severity?: string; sheet?: string; row?: number;
  column?: string; field?: string; rule_name?: string;
  policy_number?: string; actual_value?: string | number;
  expected_value?: string | number; reason?: string; message?: string;
};
type RunResp = {
  export_id: number; filename: string; row_count: number;
  exception_count: number; exceptions: RunException[];
  status: string; datamodel_mapped: boolean; admin_task_id: number | null;
  datamodel_queued?: boolean;
  format_drift: boolean;
  governing_contracts?: GoverningContract[];
  // True when produced by the pre-submission self-check (V-5) — not ingested,
  // not recorded as a run.
  check_only?: boolean;
};
export default function DirectRun() {
  const mga = currentMga();
  const nav = useNavigate();
  const admin = isTenantAdmin();

  const [carriers, setCarriers] = useState<Party[]>([]);
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
  // "See all rows": the full output (every sheet, all rows, with the same
  // highlighting), fetched on demand and expanded IN the Output Preview card —
  // the same layout as the Exception Triage BDX Review, but read-only. Cached
  // once loaded so re-expanding is instant; cleared whenever a new run/scope
  // replaces the output.
  const [showAll, setShowAll] = useState(false);
  const [allSheets, setAllSheets] = useState<Sheet[] | null>(null);
  const [allBusy, setAllBusy] = useState(false);
  // Whether this tenant still needs first-time setup (carrier + Bordereau).
  const [needsSetup, setNeedsSetup] = useState(false);

  const carrierName = carriers.find(c => c.id === carrierId)?.legal_name ?? "";
  const programName = programs.find(p => p.id === programId)?.name ?? "";
  // Carrier picked, program list finished loading, and it came back empty →
  // this carrier has no program yet. Surface it instead of a silent, empty
  // dropdown that leaves the user stuck with nothing to select.
  const noPrograms = carrierId !== "" && !programsLoading && programs.length === 0;

  // A generated output (and the uploaded file) belongs to the scope it was run
  // for — clear stale state when the carrier or program changes, so re-locking
  // the Dropzone on a cleared selection never leaves a stale file behind.
  useEffect(() => {
    setResult(null); setPreview(null); setErr(null);
    setShowAll(false); setAllSheets(null); setFile(null);
  }, [carrierId, programId]);

  useEffect(() => {
    api.get(`/parties`, { params: { mga, party_type: "carrier" } })
      .then(r => setCarriers(Array.isArray(r.data) ? r.data : (r.data?.items ?? [])))
      .catch(() => setCarriers([]));
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
    api.get<Program[]>(`/parties/${carrierId}/programs`)
      // Inactive programs are hidden here — they can't be run against.
      .then(r => setPrograms(Array.isArray(r.data) ? r.data.filter(p => p.status !== "inactive") : []))
      .catch(() => setPrograms([]))
      .finally(() => setProgramsLoading(false));
  }, [carrierId]);
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
    setMode(checkOnly ? "check" : "run");
    setBusy(true); setErr(null); setResult(null); setPreview(null);
    setShowAll(false); setAllSheets(null);
    try {
      const fd = new FormData();
      fd.append("mga", mga);
      fd.append("carrier_party_id", String(carrierId));
      fd.append("program_id", String(programId));
      fd.append("file", file);
      fd.append("actor", mga);
      if (checkOnly) fd.append("check_only", "true");
      const { data } = await api.post<RunResp>(`/direct/run`, fd);
      setResult(data);
      // Best-effort output preview for the result card. marks=1 so the flagged
      // cells match the downloaded file's highlighting; show the real data sheet
      // (not a spec/instruction sheet).
      api.get<{ sheets: Sheet[] }>(`/export/downloads/${data.export_id}/data?marks=1`)
        .then(r => setPreview(firstDataSheet(r.data.sheets ?? [])))
        .catch(() => setPreview(null));
    } catch (e: unknown) {
      const a = e as { response?: { data?: { detail?: string } }; message?: string };
      const msg = a?.response?.data?.detail ?? a?.message ?? "Run failed.";
      if (/multiple tables|more than one table/i.test(String(msg))) setMultiTableModal(String(msg));
      else setErr(msg);
    } finally { setBusy(false); }
  }

  // "See all rows": expand the full output (every sheet, all rows) in place, with
  // the same highlighting as the download. Fetched once (full=1 lifts the row cap,
  // marks=1 returns the flagged cells) and cached for instant re-expand.
  async function openAllRows() {
    if (!result) return;
    setShowAll(true);
    if (allSheets) return;
    setAllBusy(true);
    try {
      const { data } = await api.get<{ sheets: Sheet[] }>(
        `/export/downloads/${result.export_id}/data?full=1&marks=1`);
      setAllSheets(Array.isArray(data.sheets) ? data.sheets : []);
    } catch { setAllSheets([]); }
    finally { setAllBusy(false); }
  }

  // Download the self-check findings as a one-row-per-exception CSV — the
  // broker's downloadable fix-list. Self-contained (the exceptions already carry
  // every field), so it doesn't depend on the review-page's grouped exporter.
  function downloadFixList() {
    if (!result) return;
    const header = ["Severity", "Rule", "Policy", "Sheet", "Column",
                    "Actual value", "Expected", "Reason"];
    const cell = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const rows = result.exceptions.map(e => [
      e.severity, e.rule_name, e.policy_number, e.sheet,
      e.column ?? e.field, e.actual_value, e.expected_value,
      e.reason ?? e.message,
    ].map(cell).join(","));
    const csv = [header.map(cell).join(","), ...rows].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = `check_${result.filename.replace(/\.[^.]+$/, "")}.csv`;
    a.click(); URL.revokeObjectURL(url);
  }

  // All required fields for either action: a carrier + program with an active
  // setup to run against, and a file to run it on.
  const canSubmit = carrierId !== "" && programId !== "" && hasSetup === true && !!file;

  const isCheck = !!result?.check_only;
  const resultSpine = !result ? "" : result.status === "clean" ? "ok" : "warn";

  return (
    <div className="proto">
      {busy && <LoadingOverlay label={mode === "check"
        ? "Checking your bordereau — running every validation. Nothing is sent…"
        : "Processing bordereau — validating and generating output. This can take a few minutes…"} />}
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Process Bordereau</h2>
            <p>Pick the carrier and program, drop the file, generate the validated output.</p>
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
                  <select value={carrierId} onChange={e => setCarrierId(e.target.value ? Number(e.target.value) : "")}>
                    <option value="">Select Carrier…</option>
                    {/* `!== false`, not `=== true`: the API treats a null flag as
                        active and returns those rows, so `=== true` would hide them. */}
                    {carriers
                      .filter(c => c.is_active !== false)
                      .map(c => (
                        <option key={c.id} value={c.id}>
                          {c.legal_name}
                        </option>
                      ))}
                  </select>
                </div>
                <div className="field">
                  <label>Program</label>
                  <select value={programId} disabled={carrierId === "" || noPrograms}
                    onChange={e => setProgramId(e.target.value ? Number(e.target.value) : "")}>
                    <option value="">
                      {carrierId === "" ? "Select Program…"
                        : programsLoading ? "Loading programs…"
                        : noPrograms ? "No program for this carrier"
                        : "Select Program…"}
                    </option>
                    {programs.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </select>
                </div>
              </div>

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
              {hasSetup === true && (
                <div className="note" style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 8 }}>
                  Using setup <b>{setup?.name || `${carrierName} · ${programName}`}</b>
                  <span className="tag-pill">Active</span>
                </div>
              )}
              {hasSetup === false && programId !== "" && (
                <div className="note warn" style={{ marginBottom: 16 }}>
                  {admin ? (
                    <>No active setup for this carrier + program.{" "}
                      <span className="linkish" onClick={() => nav("/direct/setup")}>Configure It →</span></>
                  ) : (
                    <>No setup for this carrier. <b>Ask your admin to configure it.</b></>
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
            {result && (
              <>
                <div className={`card spine ${resultSpine} pad`}
                  style={{ margin: "18px 0", display: "flex", alignItems: "center", gap: 20, flexWrap: "wrap" }}>
                  <div style={{ flex: 1, minWidth: 220 }}>
                    <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 3 }}>
                      {isCheck && result.status === "clean"
                        ? <span style={{ color: "var(--p-ok)" }}>✓ Ready to send — no issues found</span>
                        : <>
                            {result.row_count.toLocaleString()} rows {isCheck ? "checked" : "validated"}
                            {result.status === "clean"
                              ? <span style={{ color: "var(--p-ok)" }}> · Clean</span>
                              : <span style={{ color: "var(--p-crit)" }}> · {result.exception_count.toLocaleString()} {isCheck ? "to fix" : "exceptions"}</span>}
                          </>}
                    </div>
                    <div style={{ color: "var(--p-muted)", fontSize: 13 }}>
                      {isCheck
                        ? "Self-check only — nothing was sent or saved. Fix any issues and check again, or Generate BDX to send."
                        : "Output generated. Exceptions don't block the file — review, or fix and re-run."}
                    </div>
                  </div>
                  {isCheck && result.exception_count > 0 && (
                    <button className="btn" onClick={downloadFixList}>Download Fix-List (CSV)</button>
                  )}
                  <button className="btn"
                    onClick={() => downloadFile(`/export/downloads/${result.export_id}/file`, result.filename)
                      .catch(() => setErr("We couldn't download that file — please try again."))}>
                    {isCheck ? "Download Checked File" : "Download BDX"}
                  </button>
                  {!isCheck && result.exception_count > 0 && (
                    <button className="btn pri"
                      onClick={() => nav(`/uploads/${result.export_id}/exceptions?download=${result.export_id}&from=direct`)}>
                      Review Exceptions
                    </button>
                  )}
                </div>

                {/* Self-check fix-list: row/field findings inline so the broker can
                    correct the file before sending. Read-only (no accept/reject —
                    a check is a look, not a submission). */}
                {isCheck && result.exceptions.length > 0 && (
                  <div className="card" style={{ marginBottom: 18 }}>
                    <div className="card-h">
                      <h3>What to Fix Before Sending</h3>
                      <span className="sub">{result.exception_count.toLocaleString()} finding{result.exception_count === 1 ? "" : "s"}</span>
                    </div>
                    <div className="tbl-wrap">
                      <table>
                        <thead>
                          <tr><th>Severity</th><th>Policy</th><th>Field</th><th>Value</th><th>Why</th></tr>
                        </thead>
                        <tbody>
                          {result.exceptions.slice(0, 200).map((e, i) => {
                            const sev = (e.severity || "").toLowerCase();
                            const tone = sev.includes("crit") || sev === "error" ? "var(--p-crit)"
                              : sev.includes("warn") ? "var(--p-warn, #b45309)" : "var(--p-muted)";
                            return (
                              <tr key={i}>
                                <td style={{ color: tone, fontWeight: 600, whiteSpace: "nowrap" }}>
                                  {sev.includes("crit") || sev === "error" ? "Critical" : sev.includes("warn") ? "Warning" : (e.severity || "Info")}
                                </td>
                                <td className="mono">{e.policy_number ?? (e.row != null ? `Row ${e.row}` : "—")}</td>
                                <td>{e.column ?? e.field ?? "—"}{e.sheet ? <span className="sub" style={{ marginLeft: 6 }}>{e.sheet}</span> : null}</td>
                                <td className="mono">{e.actual_value != null ? String(e.actual_value) : "—"}</td>
                                <td style={{ color: "var(--p-muted)" }}>{e.reason ?? e.message ?? "—"}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                    {result.exceptions.length > 200 && (
                      <div className="note" style={{ margin: 12 }}>
                        Showing the first 200 of {result.exception_count.toLocaleString()} — download the CSV for the full list.
                      </div>
                    )}
                  </div>
                )}

                {/* Governing contracts — which contract validated each output
                    sheet. Compact one-liner for a single contract; a table when
                    schedules are governed by different contracts. */}
                {result.governing_contracts && result.governing_contracts.length > 0 && (() => {
                  const gcs = result.governing_contracts!;
                  const withContract = gcs.filter(g => g.contract_id != null);
                  const distinct = new Set(withContract.map(g => g.contract_id));
                  const nameOf = (g: GoverningContract) =>
                    g.contract_filename || `Contract #${g.contract_id}`;
                  // One-liner only when EVERY sheet is governed by the same contract.
                  if (distinct.size === 1 && withContract.length === gcs.length) {
                    return (
                      <div className="note" style={{ marginBottom: 18 }}>
                        All output sheets validated against <strong>{nameOf(gcs[0])}</strong>.
                      </div>
                    );
                  }
                  return (
                    <div className="card" style={{ marginBottom: 18 }}>
                      <div className="card-h">
                        <h3>Governing Contracts</h3>
                        <span className="sub">
                          {distinct.size} contract{distinct.size === 1 ? "" : "s"} · {withContract.length}/{gcs.length} sheets covered
                        </span>
                      </div>
                      <div className="tbl-wrap">
                        <table>
                          <thead>
                            <tr><th>Output Sheet</th><th>Enforced By</th></tr>
                          </thead>
                          <tbody>
                            {gcs.map(g => (
                              <tr key={g.sheet}>
                                <td className="mono">{g.sheet}</td>
                                <td>
                                  {g.contract_id != null
                                    ? <>{nameOf(g)}{g.fallback && <span className="sub" style={{ marginLeft: 8 }}>(default)</span>}</>
                                    : <span className="sub">No contract</span>}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  );
                })()}

                {result.format_drift && (
                  <div className="note warn" style={{ marginBottom: 18 }}>
                    This file's columns differ from the setup's input template — the output may be incomplete.{" "}
                    {/* Bordereau Setup is admin-only (see access.ts), so an Operator
                        gets the ask-your-admin wording rather than a link that
                        bounces them straight back to the dashboard. */}
                    {admin
                      ? <span className="linkish" onClick={() => nav("/direct/setup")}>Review the Setup →</span>
                      : <b>Ask your admin to review the setup.</b>}
                  </div>
                )}
                {!isCheck && !result.datamodel_mapped && (
                  <div className="note" style={{ marginBottom: 18 }}>
                    New input format — a one-time admin task was raised to map it to the data model
                    {result.admin_task_id ? ` (task #${result.admin_task_id})` : ""}. Delivery is complete regardless.
                  </div>
                )}

                {/* Output Preview — the first rows by default, expanding in place
                    to the whole output (every sheet, all rows) rather than into a
                    modal. Read-only: this is the just-generated file, decisions
                    are made on the Exception Triage screen. */}
                {preview && preview.rows.length > 1 && (
                  showAll ? (
                    <InlineAllRows
                      title="Output Preview"
                      subtitle={`${result.filename} — cells that failed validation are highlighted, the same as in the downloaded file. Hover a cell to see why.`}
                      sheets={allSheets ?? []}
                      busy={allBusy}
                      actions={<button className="btn sm" onClick={() => setShowAll(false)}>Show Less</button>}
                    />
                  ) : (
                    <div className="card">
                      <div className="card-h">
                        <h3>Output Preview</h3>
                        <span className="sub" style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          {preview.sheet} · first {Math.min(5, preview.rows.length - 1)} of {preview.rows.length - 1} rows
                          <button className="btn sm" onClick={openAllRows}>See All Rows</button>
                        </span>
                      </div>
                      {(preview.marks?.length ?? 0) > 0 && (
                        <div className="note" style={{ margin: "10px 12px 10px", display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                          <span style={{ width: 13, height: 13, borderRadius: 3, background: HL_BG, border: `1px solid ${HL_BD}`, display: "inline-block", flex: "0 0 auto" }} />
                          <span style={{ width: 13, height: 13, borderRadius: 3, background: HL_WARN_BG, border: `1px solid ${HL_WARN_BD}`, display: "inline-block", flex: "0 0 auto", marginLeft: -4 }} />
                          Cells that failed validation are highlighted — critical in red, warnings in orange, the same as in the downloaded file. Hover a cell to see why.
                        </div>
                      )}
                      <div className="tbl-wrap">
                        <HighlightGrid sheet={preview} limit={5} sticky />
                      </div>
                    </div>
                  )
                )}
              </>
            )}

            {admin ? (
              <div className="note" style={{ marginTop: 14 }}>Missing a setup? <span className="linkish" onClick={() => nav("/direct/setup")}>Configure It →</span></div>
            ) : (
              <div className="note" style={{ marginTop: 14 }}>No setup for a carrier? <b>Ask your admin to configure it.</b></div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Dropzone({ file, onPick, disabled, lockedReason }: {
  file: File | null; onPick: (f: File | null) => void; disabled?: boolean; lockedReason?: string;
}) {
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  // Whenever the selection is cleared (Clear button, Remove link, or a
  // carrier/program change), also reset the native input's value. Otherwise the
  // input keeps the old file path and re-picking the SAME file fires no change
  // event — so the file never re-selects and the button stays disabled.
  useEffect(() => { if (!file && ref.current) ref.current.value = ""; }, [file]);
  return (
    <div
      onClick={() => !disabled && ref.current?.click()}
      onDragOver={e => { e.preventDefault(); if (!disabled) setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={e => {
        e.preventDefault(); setDrag(false);
        if (disabled) return;
        const f = e.dataTransfer.files?.[0]; if (f) onPick(f);
      }}
      className={`drop lg${file ? " filled" : ""}${disabled ? " disabled" : ""}`}
      style={{
        cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.6 : 1,
        ...(drag ? { borderColor: "var(--p-primary)", background: "var(--p-primary-soft)" } : {}),
      }}>
      <input ref={ref} type="file" accept=".xlsx,.xls,.csv,.xml,.json" style={{ display: "none" }} disabled={disabled}
        onClick={e => e.stopPropagation()}
        onChange={e => onPick(e.target.files?.[0] ?? null)} />
      <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M14 3v4a1 1 0 0 0 1 1h4" /><path d="M5 3h9l5 5v13H5z" /><path d="M9 14h6M9 17h4" />
      </svg>
      {file ? (
        <div>
          <b>{file.name}</b>
          <div style={{ fontSize: 12, marginTop: 4 }}>
            Drag a new file to replace ·{" "}
            <span className="linkish" onClick={e => { e.stopPropagation(); onPick(null); if (ref.current) ref.current.value = ""; }}>Remove</span>
          </div>
        </div>
      ) : (
        <div>
          <b>Click to Upload</b> or Drag &amp; Drop
          <div style={{ fontSize: 12, marginTop: 4 }}>.xlsx, .xls, .csv</div>
        </div>
      )}
    </div>
  );
}
