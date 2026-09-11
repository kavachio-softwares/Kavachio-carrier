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
 *   Generate BDX         the real run: ingested, recorded, and visible to the
 *                        carrier with whatever exceptions it carries.
 *
 * WHAT DIFFERS FROM THE CARRIER'S SCREEN is only the scope, and only because
 * the two genuinely differ:
 *
 *   - The carrier picks carrier → programme → broker → contract. A broker has
 *     exactly one thing to pick: which of ITS OWN contracts this file is for.
 *     Carrier, programme and broker all follow from it.
 *   - A broker seat carries no tenant, so every /direct/* and /export/* route
 *     refuses it. The run, the download and the preview all go through the
 *     carrier-centric chain instead, which authorizes the broker one path
 *     segment at a time. That is the whole of `brokerRunUrls`.
 *
 * The contract is the scope because a bordereau is checked against a CONTRACT's
 * rules — a broker holding two contracts on one programme is answering to two
 * different sets of rules. Only live contracts can be run; one the carrier has
 * not approved governs nothing, and is named with the reason rather than hidden.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { History, Download } from "lucide-react";
import { getBrokerContracts, type BrokerContract } from "../api/broker";
import { useBrokerCarrierId } from "../brokerCarrier";
import {
  getBrokerMe, getBordereauReadiness, runBrokerBordereau, getBrokerRuns,
  brokerRunUrls, bordereauTemplatePath,
  type ContractPath, type BordereauReadiness, type BrokerRun,
} from "../api/brokerBordereau";
import { Dropzone } from "../components/Dropzone";
import { RunResult, fetchPreview, type RunResp } from "../components/RunResult";
import { type Sheet } from "../components/OutputRows";
import { LoadingOverlay } from "../components/Busy";
import { downloadFile, downloadErrorText } from "../api/client";
import { fmtDate } from "../utils/date";

/** Server messages are for the log; this is what the broker can act on. */
function errorText(e: any): string {
  const d = e?.response?.data?.detail;
  if (typeof d === "string" && d.trim()) return d;
  if (e?.response?.status === 413) return "That file is too large to upload.";
  return "The run could not be completed. Try again, and tell your carrier if it keeps happening.";
}

const label = (c: BrokerContract) =>
  `${c.filename ?? `Contract ${c.id}`} — ${c.programme.name} (${c.carrier.name})`;

export default function BrokerBordereau() {
  const nav = useNavigate();
  const [brokerId, setBrokerId] = useState<number | null>(null);
  const [contracts, setContracts] = useState<BrokerContract[] | null>(null);
  const [contractId, setContractId] = useState<number | "">("");
  const [ready, setReady] = useState<BordereauReadiness | null>(null);
  const [readyLoading, setReadyLoading] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  // Which action is running, so the overlay says the right thing.
  const [mode, setMode] = useState<"check" | "run">("run");
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<RunResp | null>(null);
  const [preview, setPreview] = useState<Sheet | null>(null);
  const [runs, setRuns] = useState<BrokerRun[] | null>(null);
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
    setContractId("");
    getBrokerContracts({ carrierId: carrierId ?? undefined })
      .then(rows => {
        setContracts(rows);
        const live = rows.filter(c => c.lifecycle === "active");
        // One live contract is not a choice. Select it and let the broker get
        // on with the actual task.
        if (live.length === 1) setContractId(live[0].id);
      })
      .catch(() => setContracts([]));
  }, [carrierId]);

  // In force, which is what decides whether a file can be produced against it.
  // This used to read the carrier's approval instead — a weaker question, and
  // one that no longer exists now the gate has gone.
  const live = useMemo(
    () => (contracts ?? []).filter(c => c.lifecycle === "active"),
    [contracts]);
  const waiting = useMemo(
    () => (contracts ?? []).filter(c => c.lifecycle !== "active"),
    [contracts]);
  const contract = useMemo(
    () => live.find(c => c.id === contractId) ?? null, [live, contractId]);

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
  }, [contractId]);

  const loadRuns = useCallback(() => {
    if (!path) { setRuns(null); return; }
    getBrokerRuns(path).then(setRuns).catch(() => setRuns([]));
  }, [path]);

  useEffect(() => {
    if (!path) { setReady(null); return; }
    let stale = false;
    setReadyLoading(true);
    getBordereauReadiness(path)
      .then(r => { if (!stale) setReady(r); })
      .catch(() => { if (!stale) setReady(null); })
      .finally(() => { if (!stale) setReadyLoading(false); });
    loadRuns();
    return () => { stale = true; };
  }, [path, loadRuns]);

  async function submit(checkOnly: boolean) {
    if (!file || !path || !urls) return;
    setMode(checkOnly ? "check" : "run");
    setBusy(true); setErr(null); setResult(null); setPreview(null);
    try {
      const r = await runBrokerBordereau(path, file, { checkOnly });
      setResult(r);
      // Best-effort preview, exactly as the carrier's screen does it — a
      // missing preview must never make a good run look like a failure.
      fetchPreview(urls, r.export_id).then(setPreview);
      // A self-check records nothing, so only a real submission changes history.
      if (!checkOnly) loadRuns();
    } catch (e) {
      setErr(errorText(e));
    } finally { setBusy(false); }
  }

  function clearForm() {
    setFile(null); setResult(null); setPreview(null); setErr(null);
  }

  const canSubmit = !!file && !!ready?.ready && !busy;

  return (
    <div className="proto">
      {busy && <LoadingOverlay label={mode === "check"
        ? "Checking your bordereau — running every validation. Nothing is sent…"
        : "Processing bordereau — validating and generating output. This can take a few minutes…"} />}
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Process Bordereau</h2>
            <p>Pick the contract the bordereau is for, drop the file, check it,
              then send it.</p>
          </div>
        </div>

        {err && (
          <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>
        )}

        {contracts !== null && contracts.length === 0 && (
          <div className="note warn" style={{ marginBottom: 18, maxWidth: 640 }}>
            <b>You have no contracts yet.</b> A bordereau is checked against a
            contract's rules, so there is nothing to submit until a carrier puts
            you on a programme and a contract is in place. Ask your carrier
            contact — none of it is something you can do from this side.
          </div>
        )}

        {contracts !== null && contracts.length > 0 && live.length === 0 && (
          <div className="note warn" style={{ marginBottom: 18, maxWidth: 640 }}>
            <b>None of your contracts is live yet.</b> A contract governs
            nothing until it is in force, and it is the terms of a live contract
            that create the rules your file is checked against. See{" "}
            <Link to="/broker/contracts">My Contracts</Link> for where each one
            has got to.
          </div>
        )}

        <div className="card pad">
          <div className="field">
            <label>Contract</label>
            {live.length === 1 ? (
              // Nothing to choose between: this is what the bordereau is for.
              <input value={label(live[0])} readOnly disabled />
            ) : (
              <select
                value={contractId}
                disabled={live.length === 0}
                onChange={e => setContractId(e.target.value ? Number(e.target.value) : "")}
              >
                <option value="">
                  {live.length === 0 ? "No live contract yet" : "Select Contract…"}
                </option>
                {live.map(c => <option key={c.id} value={c.id}>{label(c)}</option>)}
              </select>
            )}
            <div className="hint">
              Each contract has its own rules, so the same spreadsheet can pass
              under one and fail under another. Carrier and programme come with
              it — there is nothing else to pick.
            </div>
          </div>

          {/* Named rather than hidden: a broker looking for a contract they know
              they sent should find out WHY it is not selectable. */}
          {waiting.length > 0 && (
            <div className="note" style={{ marginBottom: 16 }}>
              {waiting.length === 1
                ? <><b>{waiting[0].name ?? waiting[0].filename
                        ?? `Contract ${waiting[0].id}`}</b> is not listed — it is
                    not in force yet.</>
                : <><b>{waiting.length} of your contracts</b> are not listed —
                    they are not in force yet.</>}
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
                <button className="btn" style={{ marginLeft: "auto" }}
                  onClick={() => downloadFile(bordereauTemplatePath(path))
                    .catch(async e => setErr(await downloadErrorText(e,
                      "We couldn't download the bordereau template — please try again.")))}>
                  <Download size={14} /> Bordereau Template
                </button>
              )}
            </div>
          )}

          <Dropzone file={file} onPick={setFile} disabled={!ready?.ready} />

          <div style={{ marginTop: 18, display: "flex", gap: 10, alignItems: "center" }}>
            {/* The self-check is the reason a broker has this screen, so unlike
                the carrier's it is offered first and prominently. */}
            <button className="btn" onClick={() => submit(true)} disabled={!canSubmit}
              title="Run every validation without sending — see what to fix first">
              Check My Bordereau
            </button>
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
            // A broker has no Exception Triage screen to be sent to — that
            // route is carrier_admin-only, and deciding exceptions is the
            // carrier's call, not theirs. So the findings are listed HERE, on
            // a real submission as much as on a check: being told "14
            // exceptions" with nothing to act on is not a result.
            findings="always"
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
            footNote={<b>Ask your carrier to review the setup.</b>}
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
          </div>
        )}

        <div className="note" style={{ marginTop: 14 }}>
          Your carrier builds the setup that decides what a valid file looks
          like, and the contract decides the rules it is checked against. If
          either is wrong for your book, that is a conversation with them — see{" "}
          <Link to="/broker/contracts">My Contracts</Link>.
        </div>
      </div>
    </div>
  );
}
