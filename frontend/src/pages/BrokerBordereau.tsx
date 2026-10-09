/**
 * Process Bordereau — the broker's side of the same screen the carrier uses.
 *
 * Deliberately the SAME experience: the same drop target, the same result view
 * (summary spine, fix-list, governing contracts, highlighted output preview
 * with See All Rows, downloads), because it is literally the same components —
 * Dropzone and RunResult. A broker looking at a run and a carrier looking at
 * the same run see the same thing, and a fix to either reaches both.
 *
 * Two buttons, and the difference between them is the point of the screen:
 *
 *   Check My Bordereau   every validation runs, nothing is submitted, nothing
 *                        is recorded, and the carrier is never shown it. A
 *                        broker finds out what is wrong BEFORE the carrier does.
 *   Generate BDX         the real run: ingested, recorded against the
 *                        reporting period picked below, AND sent — emailed to
 *                        the carrier (cc your own admin) the moment it
 *                        processes, whatever exceptions it carries. There is
 *                        no separate Send step or screen any more; this IS
 *                        the send (see RunResult's "Sent to the carrier"
 *                        note, and submission_calendar_service.send_bordereau
 *                        on the server).
 *
 * WHAT DIFFERS FROM THE CARRIER'S SCREEN is only the scope, and only because
 * the two genuinely differ:
 *
 *   - The carrier picks carrier → programme → broker → contract. A broker
 *     picks only programme → contract, each shown flat when there is just
 *     one; carrier and broker follow from the contract.
 *   - A broker seat carries no tenant, so every /direct/* and /export/* route
 *     refuses it. The run, the download and the preview all go through the
 *     carrier-centric chain instead, which authorizes the broker one path
 *     segment at a time. That is the whole of `brokerRunUrls`.
 *
 * The contract is the scope because a bordereau is checked against a CONTRACT's
 * rules — a broker holding two contracts on one programme is answering to two
 * different sets of rules. Live contracts can be run, and so can one whose term
 * has ENDED, for the months inside its term (a late bordereau is still owed);
 * one not yet in force governs nothing, and is named with the reason.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { History, Download, AlertTriangle } from "lucide-react";
import { Modal } from "../components/ui/Modal";
import { getBrokerContracts, type BrokerContract } from "../api/broker";
import { useBrokerCarrierId } from "../brokerCarrier";
import {
  getBrokerMe, getBordereauReadiness, runBrokerBordereau, getBrokerRunsPaged,
  brokerRunUrls, bordereauTemplatePath, getContractPeriods,
  type ContractPath, type BordereauReadiness, type BrokerRun, type BordereauPeriod,
} from "../api/brokerBordereau";
import { Dropzone } from "../components/Dropzone";
import { PeriodPicker } from "../components/PeriodPicker";
import { ContractPicker, ProgrammePicker, isRunnable, type BrokerProgramme } from "../components/ContractPicker";
import { RunResult, ColumnToggles, fetchPreview, type RunResp, type LayoutChanges } from "../components/RunResult";
import { type Sheet } from "../components/OutputRows";
import { LoadingOverlay } from "../components/Busy";
import { downloadFile, downloadErrorText } from "../api/client";
import { fmtDate } from "../utils/date";
import { Pagination } from "../components/Pagination";
import { InfoTip } from "../components/InfoTip";

/** Runs per page of the history. Server-side: the endpoint used to stop at the
 *  twentieth most recent file with no way to reach the twenty-first. */
const RUNS_PAGE_SIZE = 10;

/** Server messages are for the log; this is what the broker can act on. */
function errorText(e: any): string {
  const d = e?.response?.data?.detail;
  if (typeof d === "string" && d.trim()) return d;
  if (e?.response?.status === 413) return "That file is too large to upload.";
  return "The run could not be completed. Try again, and tell your carrier if it keeps happening.";
}

export default function BrokerBordereau() {
  const nav = useNavigate();
  const [brokerId, setBrokerId] = useState<number | null>(null);
  const [contracts, setContracts] = useState<BrokerContract[] | null>(null);
  const [programmeId, setProgrammeId] = useState<number | "">("");
  const [contractId, setContractId] = useState<number | "">("");
  const [ready, setReady] = useState<BordereauReadiness | null>(null);
  const [readyLoading, setReadyLoading] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  // Which action is running, so the overlay says the right thing.
  const [mode, setMode] = useState<"check" | "run">("run");
  const [err, setErr] = useState<string | null>(null);
  // "This exact file was sent before" — asked on the spot, not refused.
  const [duplicateMsg, setDuplicateMsg] = useState<string | null>(null);
  // "Fewer than 90% of the setup's columns are in this file" — asked in a
  // popup before the run: Cancel, or Process Bordereau Anyway
  // (direct_routes._layout_gate).
  const [layoutBlock, setLayoutBlock] = useState<(LayoutChanges & { message: string; min_match_pct?: number }) | null>(null);
  // The broker chose Continue Anyway for this file, so a duplicate question
  // that follows must not ask about the columns again.
  const [layoutOk, setLayoutOk] = useState(false);
  const [result, setResult] = useState<RunResp | null>(null);
  const [preview, setPreview] = useState<Sheet | null>(null);
  const [periods, setPeriods] = useState<BordereauPeriod[] | null>(null);
  const [period, setPeriod] = useState<string>("");
  const [runs, setRuns] = useState<BrokerRun[] | null>(null);
  const [runsPage, setRunsPage] = useState(1);
  const [runsTotal, setRunsTotal] = useState(0);
  const [showHistory, setShowHistory] = useState(false);

  // THE SCOPE THAT MATTERS MOST. A bordereau is checked against one contract's
  // rules, and two carriers' contracts sitting in one dropdown are separated
  // only by a name in brackets — which is how a file gets run against the
  // wrong carrier and comes back with exceptions that mean nothing. Narrowing
  // the list to the carrier being worked on removes the mistake rather than
  // labelling it.
  const carrierId = useBrokerCarrierId();

  useEffect(() => {
    getBrokerMe().then(m => setBrokerId(m.id)).catch(() => setBrokerId(null));
  }, []);

  useEffect(() => {
    // Switching carrier drops the current pick: keeping it would leave the
    // screen addressed at a contract no longer in the list it is showing.
    setContractId(""); setProgrammeId("");
    getBrokerContracts({ carrierId: carrierId ?? undefined })
      .then(rows => {
        setContracts(rows);
        const live = rows.filter(isRunnable);
        // One programme, or one contract, is not a choice. Select it and let
        // the broker get on with the actual task.
        const progs = new Set(live.map(c => c.programme.id).filter(id => id != null));
        if (progs.size === 1) setProgrammeId([...progs][0] as number);
        if (live.length === 1) setContractId(live[0].id);
      })
      .catch(() => setContracts([]));
  }, [carrierId]);

  // In force, which is what decides whether a file can be produced against it.
  // This used to read the carrier's approval instead — a weaker question, and
  // one that no longer exists now the gate has gone.
  const live = useMemo(
    () => (contracts ?? []).filter(isRunnable),
    [contracts]);
  const waiting = useMemo(
    () => (contracts ?? []).filter(c => !isRunnable(c)),
    [contracts]);
  // The broker's programmes, from the contracts it can run — a programme is
  // what the broker thinks in ("the Demonity file"), the contract follows.
  const programmes: BrokerProgramme[] = useMemo(() => {
    const m = new Map<number, BrokerProgramme>();
    for (const c of live) {
      if (c.programme.id == null) continue;
      const p = m.get(c.programme.id) ?? { id: c.programme.id, name: c.programme.name,
                                           code: c.programme.code,
                                           carrier: c.carrier.name, contracts: [] };
      p.contracts.push(c);
      m.set(c.programme.id, p);
    }
    return [...m.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [live]);
  const onProgramme = useMemo(
    () => programmes.find(p => p.id === programmeId)?.contracts ?? [],
    [programmes, programmeId]);
  const contract = useMemo(
    () => live.find(c => c.id === contractId) ?? null, [live, contractId]);

  function pickProgramme(id: number) {
    setProgrammeId(id);
    const on = programmes.find(p => p.id === id)?.contracts ?? [];
    setContractId(on.length === 1 ? on[0].id : "");
  }

  /** The full chain this run is addressed at. Null until everything resolves. */
  const path: ContractPath | null = useMemo(() => {
    if (!contract || brokerId == null) return null;
    if (contract.carrier.id == null || contract.programme.id == null) return null;
    return {
      carrierId: contract.carrier.id,
      programId: contract.programme.id,
      brokerPartyId: brokerId,
      contractId: contract.id,
    };
  }, [contract, brokerId]);

  const urls = useMemo(() => (path ? brokerRunUrls(path) : null), [path]);

  // A result belongs to the contract it was run for — clear it when that
  // changes, so a stale exception list can never be read as this one's.
  useEffect(() => {
    setResult(null); setPreview(null); setErr(null); setFile(null);
    setLayoutBlock(null); setLayoutOk(false);
    setPeriod("");
    // A different contract is a different history, so it is read from its start.
    setRunsPage(1);
  }, [contractId]);

  // The real reporting periods this contract's calendar expects — never a
  // free-text month/year, so what gets sent always matches an actual row (see
  // GET .../periods). Re-fetched whenever the contract changes.
  useEffect(() => {
    if (!path) { setPeriods(null); return; }
    getContractPeriods(path)
      .then(rows => {
        // An ended contract's calendar already stops at its term (the server
        // builds no month that starts after the expiry).
        setPeriods(rows);
        // The most recent NOT-YET-PROCESSED period is almost always what
        // somebody dropping a file today means — pre-select it, but only if
        // there is exactly one live contract's worth of ambiguity to resolve
        // FOR them, not silently override a choice they are mid-way through.
        const open = rows.find(r => !r.processed);
        if (open) setPeriod(p => p || open.period);
      })
      .catch(() => setPeriods([]));
  }, [path]);

  const loadRuns = useCallback(() => {
    if (!path) { setRuns(null); setRunsTotal(0); return; }
    getBrokerRunsPaged(path, runsPage, RUNS_PAGE_SIZE)
      .then(r => { setRuns(r.items); setRunsTotal(r.total); })
      .catch(() => { setRuns([]); setRunsTotal(0); });
  }, [path, runsPage]);

  useEffect(() => {
    if (!path) { setReady(null); return; }
    let stale = false;
    setReadyLoading(true);
    getBordereauReadiness(path)
      .then(r => { if (!stale) setReady(r); })
      .catch(() => { if (!stale) setReady(null); })
      .finally(() => { if (!stale) setReadyLoading(false); });
    return () => { stale = true; };
  }, [path]);

  // The history fetches on its own, so turning its page does not also re-ask
  // whether this contract is ready to be run.
  useEffect(() => { loadRuns(); }, [loadRuns]);

  async function submit(checkOnly: boolean, confirmDuplicate = false, confirmLayout = layoutOk) {
    if (!file || !path || !urls) return;
    setMode(checkOnly ? "check" : "run");
    setBusy(true); setErr(null); setResult(null); setPreview(null); setLayoutBlock(null);
    try {
      const r = await runBrokerBordereau(path, file,
        { checkOnly, confirmDuplicate, confirmLayout, period });
      setResult(r);
      // Best-effort preview, exactly as the carrier's screen does it — a
      // missing preview must never make a good run look like a failure.
      fetchPreview(urls, r.export_id).then(setPreview);
      // A self-check records nothing, so only a real submission changes history.
      // Back to the first page with it: a submitted run is the newest row, and
      // it belongs at the top of the history rather than wherever the reader
      // happened to have paged to.
      if (!checkOnly) { setRunsPage(1); loadRuns(); }
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      if (detail && typeof detail === "object"
          && (detail as { code?: string }).code === "duplicate_file") {
        setDuplicateMsg((detail as { message?: string }).message
          ?? "This exact file has already been sent.");
      } else if (detail && typeof detail === "object"
          && (detail as { code?: string }).code === "layout_mismatch") {
        setLayoutBlock(detail as LayoutChanges & { message: string; min_match_pct?: number });
      } else {
        setErr(errorText(e));
      }
    } finally { setBusy(false); }
  }

  function clearForm() {
    pickFile(null); setResult(null); setPreview(null); setErr(null);
  }

  // A new file is a new answer to "run it anyway?".
  function pickFile(f: File | null) {
    setFile(f); setLayoutBlock(null); setLayoutOk(false);
  }

  function downloadTemplate() {
    if (!path) return;
    downloadFile(bordereauTemplatePath(path))
      .catch(async e => setErr(await downloadErrorText(e,
        "We couldn't download the bordereau template — please try again.")));
  }

  // Required on a real submission — see contract_bordereau_run. Not required
  // to Check (there is no submit(true) button today, but the rule stays
  // correct if it is ever restored).
  const canSubmit = !!file && !!ready?.ready && !!period && !busy;

  return (
    <div className="proto">
      <Modal open={duplicateMsg != null}
        title={<span className="flex items-center gap-2">
          <AlertTriangle size={17} className="text-amber-500" /> Same File Already Sent
        </span>}
        onClose={() => setDuplicateMsg(null)}
        footer={<>
          <button className="btn" onClick={() => setDuplicateMsg(null)}>Don’t send it</button>
          <button className="btn pri" onClick={() => { setDuplicateMsg(null); submit(false, true, layoutOk); }}>
            Send it anyway</button>
        </>}>
        <p className="text-sm">{duplicateMsg}</p>
      </Modal>
      <Modal open={layoutBlock != null}
        title={<span className="flex items-center gap-2">
          <AlertTriangle size={17} className="text-amber-500" /> Your columns don’t match the carrier’s template
        </span>}
        onClose={() => setLayoutBlock(null)}
        footer={<>
          <button className="btn" onClick={() => setLayoutBlock(null)}>Cancel</button>
          <button className="btn pri" disabled={!canSubmit}
            onClick={() => { setLayoutBlock(null); setLayoutOk(true); submit(false, false, true); }}>
            Process Bordereau Anyway</button>
        </>}>
        <div>
          {layoutBlock && (
            <ul className="text-sm" style={{ margin: 0, paddingLeft: 20, listStyle: "disc",
                                             display: "grid", gap: 4 }}>
              <li><b>{layoutBlock.missing_columns.length} of {layoutBlock.expected_count} columns</b> are
                missing or renamed ({Math.round(layoutBlock.changed_pct)}%).</li>
              <li>At least <b>{layoutBlock.min_match_pct ?? 90}%</b> must match.</li>
              <li>Missing columns will be <b>blank</b> in the output.</li>
            </ul>
          )}
          <ColumnToggles changes={layoutBlock} />
        </div>
      </Modal>
      {busy && <LoadingOverlay label={mode === "check"
        ? "Checking your bordereau — running every validation. Nothing is sent…"
        : "Processing bordereau — validating and generating output. This can take a few minutes…"} />}
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Process Bordereau
              <InfoTip text={"Pick the contract and reporting period, drop the "
                + "file, and Generate BDX — it's sent to the carrier the "
                + "moment it's processed, no second step needed."} />
            </h2>
          </div>
        </div>

        {err && (
          <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>
        )}

        {contracts !== null && contracts.length === 0 && (
          <div className="note warn" style={{ marginBottom: 18 }}>
            <b>No contracts yet.</b> You can process a bordereau once your
            carrier adds you to a programme and your contract is active. Please
            contact your carrier to get started.
          </div>
        )}

        {contracts !== null && contracts.length > 0 && live.length === 0 && (
          <div className="note warn" style={{ marginBottom: 18, maxWidth: 640 }}>
            <b>No active contracts yet.</b> You can process a bordereau once one
            of your contracts is active. Track their progress in{" "}
            <Link to="/broker/contracts">My Contracts</Link>.
          </div>
        )}

        <div className="card pad">
          <div className="field">
            <label>
              Programme
              <InfoTip text={"Which of your programmes this bordereau is for. "
                + "The carrier comes with it."} />
            </label>
            <ProgrammePicker value={programmeId} onChange={pickProgramme}
              programmes={programmes} />
          </div>

          {programmeId !== "" && (
            <div className="field">
              <label>
                Contract
                <InfoTip text={"Each contract has its own rules, so the same "
                  + "spreadsheet can pass under one and fail under another."} />
              </label>
              <ContractPicker value={contractId}
                onChange={id => setContractId(id)} contracts={onProgramme} />
            </div>
          )}

          {/* An ended contract is still owed the bordereaux of its term — say
              so, rather than leaving the broker to wonder why it is offered. */}
          {contract?.lifecycle === "expired" && (
            <div className="note" style={{ marginBottom: 16 }}>
              This contract’s term ended{contract.expiry_dt ? <> on <b>{fmtDate(contract.expiry_dt)}</b></> : null}.
              You can still send bordereaux for the months up to then.
            </div>
          )}

          {/* Which month (or quarter, on a quarterly programme) this file is
              FOR — the reporting period. A REAL row from this contract's own
              calendar, never a free-text month/year, so it can only ever match
              a period the carrier is actually expecting — see GET .../periods.
              This is what both Generate BDX's automatic send and the
              carrier's own Bordereau Calendar then read back. */}
          {contract && (
            <div className="field">
              <label>
                Reporting Period
                <InfoTip text={"Which month this bordereau reports on — not "
                  + "today's date. A July file sent in September is still "
                  + "July's."} />
              </label>
              <PeriodPicker value={period} onChange={setPeriod} periods={periods} />
            </div>
          )}

          {/* Named rather than hidden: a broker looking for a contract they know
              they sent should find out WHY it is not selectable. */}
          {waiting.length > 0 && (
            <div className="note" style={{ marginBottom: 16 }}>
              {waiting.length === 1
                ? <><b>{waiting[0].name ?? waiting[0].filename
                        ?? `Contract ${waiting[0].id}`}</b> is not listed — it is
                    not active yet.</>
                : <><b>{waiting.length} of your contracts</b> are not listed —
                    they are not active yet.</>}
            </div>
          )}

          {/* Readiness is answered BEFORE the drop target is usable, not after a
              failed submit: a broker whose carrier has not made the setup live
              can do nothing here, and should be told which of the two it is. */}
          {contract && readyLoading && (
            <div className="note" style={{ marginBottom: 16 }}>
              Checking the setup for this contract…
            </div>
          )}
          {contract && !readyLoading && ready && !ready.ready && (
            <div className="note warn" style={{ marginBottom: 16 }}>
              <b>Not ready yet.</b> {ready.reason}
            </div>
          )}
          {contract && !readyLoading && ready?.ready && (
            <div className="note" style={{ marginBottom: 16, display: "flex",
              alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              Using setup <b>{ready.setup?.name ?? "the programme's"}</b>
              <span className="tag-pill">Active</span>
              {ready.setup?.output_template && (
                <>→ <b>{ready.setup.output_template.name}</b></>
              )}
              {ready.setup?.held_by === "programme" && (
                <span style={{ fontSize: 11.5, color: "var(--p-faint)" }}>
                  — the programme's shared setup, not one built for you.
                </span>
              )}
              {/* The blank layout this setup reads — the file to fill in before
                  dropping it below. Only for a real setup: the legacy fallback
                  has no id and no sample of its own to rebuild. */}
              {ready.setup?.id != null && path && (
                <button className="btn" style={{ marginLeft: "auto" }} onClick={downloadTemplate}>
                  <Download size={14} /> Bordereau Input Template
                </button>
              )}
            </div>
          )}

          <Dropzone file={file} onPick={pickFile} disabled={!ready?.ready} />

          <div style={{ marginTop: 18, display: "flex", gap: 10, alignItems: "center" }}>
            {/* Hidden for now, the same as on the carrier's screen — Generate
                BDX is the only action offered. Everything behind it still
                works (submit(true) → check_only, which validates without
                submitting), so restoring it is uncommenting these lines. */}
            {/* <button className="btn" onClick={() => submit(true)} disabled={!canSubmit}
              title="Run every validation without sending — see what to fix first">
              Check My Bordereau
            </button> */}
            <button className="btn pri" onClick={() => submit(false)} disabled={!canSubmit}>
              Generate BDX
            </button>
            <button className="btn" onClick={clearForm} disabled={busy}>Clear</button>
            <button className="btn ghost" style={{ marginLeft: "auto" }}
              disabled={!contract}
              onClick={() => setShowHistory(v => !v)}>
              <History size={15} /> {showHistory ? "Hide" : "View"} What I've Sent
            </button>
          </div>
        </div>

        {/* The SAME result view the carrier sees — same component, different
            URLs, because a broker reads its runs through the chain. */}
        {result && urls && (
          <RunResult
            result={result}
            preview={preview}
            urls={urls}
            onError={setErr}
            // TEMPORARILY HIDDEN on a real submission (was findings="always").
            // Restore that value to bring the "What Failed Validation" list
            // back. The reason it was there: a broker has no Exception Triage
            // screen to be sent to — that route is carrier_admin-only, and
            // deciding exceptions is the carrier's call, not theirs — so being
            // told "268 exceptions" with nothing to act on is not a result.
            findings="check-only"
            // The SAME Exception Triage screen the carrier uses. The export id
            // in the path is what scopes it, and the server only hands back an
            // export stamped with this broker — so this opens their own run and
            // nobody else's. `from=broker` keeps the sidebar highlight on
            // Process Bordereau when they get there.
            actions={!result.check_only && result.exception_count > 0 ? (
              <button className="btn pri"
                onClick={() => nav(`/uploads/${result.export_id}/exceptions`
                                   + `?download=${result.export_id}&from=broker`)}>
                Review Exceptions
              </button>
            ) : null}
            // Unmapped columns and mapping tasks are the carrier's to fix.
            carrierNotes={false}
          />
        )}

        {showHistory && contract && (
          <div className="card" style={{ marginTop: 18 }}>
            <div className="card-h">
              <h3>What you have sent</h3>
              <span className="sub">{contract.filename ?? `Contract ${contract.id}`}</span>
            </div>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr><th>File</th><th>Sent</th><th>Rows</th><th>Exceptions</th><th /></tr>
                </thead>
                <tbody>
                  {(runs ?? []).map(r => (
                    <tr key={r.landing_id}>
                      <td><b>{r.source_filename ?? `Run ${r.landing_id}`}</b></td>
                      <td className="muted">{r.created_at ? fmtDate(r.created_at) : "—"}</td>
                      <td>{r.row_count ?? 0}</td>
                      <td>
                        {/* The count IS the way in. It used to be a dead badge:
                            the one number on the row a person actually wants to
                            act on, and nothing to click. Opens the same
                            Exception Triage screen a fresh run does. */}
                        {r.exception_count
                          ? (r.export_id != null
                              ? <button
                                  className="badge b-warn"
                                  style={{ border: 0, cursor: "pointer", font: "inherit" }}
                                  title="See what failed on this file"
                                  onClick={() => nav(`/uploads/${r.export_id}/exceptions`
                                                     + `?download=${r.export_id}&from=broker`)}>
                                  <span className="d" />{r.exception_count}
                                </button>
                              : <span className="badge b-warn"><span className="d" />{r.exception_count}</span>)
                          : <span className="badge b-ok"><span className="d" />Clean</span>}
                      </td>
                      <td>
                        {/* downloadFile, not <a href>: these are API paths, and
                            a bare href would resolve against the FRONTEND
                            origin and 404. It also carries the auth header. */}
                        {r.export_id != null && urls && (
                          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                            {/* Spelled out as well as on the count, because a
                                clean run has no count to click and a reviewer
                                scanning the last column should still find it. */}
                            {!!r.exception_count && (
                              <button className="btn sm"
                                onClick={() => nav(`/uploads/${r.export_id}/exceptions`
                                                   + `?download=${r.export_id}&from=broker`)}>
                                Review
                              </button>
                            )}
                            <button className="btn sm"
                              onClick={() => downloadFile(urls.file(r.export_id!),
                                                          r.filename ?? undefined)
                                .catch(() => setErr("We couldn't download that file — please try again."))}>
                              Download
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {runs !== null && runs.length === 0 && (
                <div className="empty">
                  Nothing sent against this contract yet. Files you only CHECK
                  never appear here — that is what makes checking safe.
                </div>
              )}
            </div>
            <Pagination
              page={runsPage}
              pageCount={Math.max(1, Math.ceil(runsTotal / RUNS_PAGE_SIZE))}
              pageSize={RUNS_PAGE_SIZE} totalItems={runsTotal}
              onPageChange={setRunsPage} noun="files" />
          </div>
        )}

        {/* <div className="note" style={{ marginTop: 14 }}>
          Your carrier builds the setup that decides what a valid file looks
          like, and the contract decides the rules it is checked against. If
          either is wrong for your book, that is a conversation with them — see{" "}
          <Link to="/broker/contracts">My Contracts</Link>.
        </div> */}
      </div>
    </div>
  );
}
