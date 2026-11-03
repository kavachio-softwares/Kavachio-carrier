/**
 * Change period, Phase B — correcting a contract's dates, and undoing the
 * latest correction.
 *
 * A correction is for dates entered wrongly; a change both sides agreed is an
 * endorsement. Everything shown here comes from the server's preview
 * (POST /contracts/{id}/period/preview), and the save sends back the preview's
 * token: if anything changed since the preview was shown, the server refuses
 * and the preview is shown again. The typed contract code for a large
 * correction is checked by the server too — the button only mirrors it.
 */
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CalendarRange, ShieldCheck, Undo2, Upload } from "lucide-react";
import {
  fieldErrors, periodCorrect, periodPreview, periodUndo, periodUndoPreview, uploadDocument,
  type ContractRecord, type PeriodPreview, type UndoPreview,
} from "../api/contractRecord";
import { fmtDate } from "../utils/date";

const STATE_LABEL: Record<string, string> = {
  active: "Active", expired: "Expired", superseded: "Superseded", terminated: "Terminated",
};

function detailOf(e: unknown): Record<string, unknown> | null {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return d && typeof d === "object" ? (d as Record<string, unknown>) : null;
}

function Line({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, padding: "6px 0",
                  borderBottom: "1px solid var(--p-border)", fontSize: 13 }}>
      <div style={{ width: 150, flex: "none", color: "var(--p-muted)" }}>{label}</div>
      <div style={{ color: "var(--p-ink)", minWidth: 0 }}>{children}</div>
    </div>
  );
}

const box: React.CSSProperties = { width: 640, maxHeight: "calc(100vh - 40px)",
                                   display: "flex", flexDirection: "column" };
const field: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 4,
                                     fontSize: 12.5, color: "var(--p-muted)" };

export function ChangePeriodDialog({ rec, onClose, onDone, onEvidenceAdded }: {
  rec: ContractRecord;
  onClose: () => void;
  onDone: (message: string) => void;
  /** An evidence file was attached: the page's Documents list is out of date. */
  onEvidenceAdded?: () => void;
}) {
  const [inception, setInception] = useState(rec.inception_dt ?? "");
  const [expiry, setExpiry] = useState(rec.expiry_dt ?? "");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [gapOk, setGapOk] = useState(false);
  const [evidenceId, setEvidenceId] = useState<number | null>(null);
  const [typed, setTyped] = useState("");
  const [pv, setPv] = useState<PeriodPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const seq = useRef(0);
  const allowed = rec.period_change?.allowed ?? { inception_dt: false, expiry_dt: false };

  async function refresh(i = inception, e = expiry) {
    const n = ++seq.current;
    setLoading(true);
    try {
      const p = await periodPreview(rec.id, i, e);
      if (n === seq.current) setPv(p);
    } catch (x) {
      if (n === seq.current) { setPv(null); setErr(fieldErrors(x).message); }
    } finally {
      if (n === seq.current) setLoading(false);
    }
  }

  useEffect(() => {
    setErr(""); setGapOk(false);
    if (!inception || !expiry) { setPv(null); return; }
    const t = setTimeout(() => refresh(inception, expiry), 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inception, expiry]);

  // A tick is for the gap it was given to: a refreshed preview showing a
  // different gap (or none) asks again.
  const gapKey = JSON.stringify((pv?.gaps ?? []).map(g => [g.code, g.with_id, g.days]));
  useEffect(() => { setGapOk(false); }, [gapKey]);

  const large = !!pv?.large.large;
  const unchanged = inception === rec.inception_dt && expiry === rec.expiry_dt;
  const blockedMsg = pv ? (Object.values(pv.errors)[0] ?? pv.conflicts[0]?.message) : undefined;
  const codeOk = !large || (!!pv?.code && typed.trim() === pv.code);
  const canSave = !!pv && pv.ready && !unchanged && !loading && !busy && !!reason.trim()
    && (!pv.gaps.length || gapOk) && (!large || (!!evidenceId && codeOk));

  async function attachEvidence(f: File) {
    setBusy("evidence"); setErr("");
    try {
      const r = await uploadDocument(rec.id, f, { kind: "evidence" });
      setEvidenceId(r.document.id);
      onEvidenceAdded?.();
      await refresh();
    } catch (x) {
      setErr(fieldErrors(x).message);
    } finally { setBusy(""); }
  }

  async function save() {
    if (!pv) return;
    setBusy("save"); setErr("");
    try {
      await periodCorrect(rec.id, {
        inception_dt: inception, expiry_dt: expiry, reason: reason.trim(),
        internal_note: note.trim() || null, confirm_gap: gapOk,
        evidence_document_id: large ? evidenceId : null,
        confirm_text: large ? typed : null, preview_token: pv.preview_token,
      });
      onDone("The dates were corrected.");
    } catch (x) {
      const d = detailOf(x);
      setErr(d?.stale_preview
        ? "Something changed since the preview was shown — check the impact again."
        : fieldErrors(x).message);
      if (d?.stale_preview || d?.needs_confirm) await refresh();
    } finally { setBusy(""); }
  }

  return (
    <div className="proto-modal-overlay">
      <div className="proto-modal" style={box} onClick={e => e.stopPropagation()}
           role="dialog" aria-label="Change period">
        <div className="m-h">
          <CalendarRange size={17} style={{ color: "var(--p-primary)" }} />
          <h3>Change period — correct the dates</h3>
          <button className="x" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="m-b" style={{ overflowY: "auto" }}>
          <p style={{ margin: "0 0 12px" }}>
            For dates that were entered wrongly. If both sides agreed a new term,
            that is an endorsement, not a correction.
          </p>
          {rec.lifecycle === "terminated" && rec.terminated_date && (
            <div className="note" style={{ marginBottom: 12 }}>
              Only the end date can be corrected. <b>The termination on{" "}
              {fmtDate(rec.terminated_date)} stays as recorded.</b>
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <label style={field}>
              Start date
              <input className="input" type="date" value={inception}
                     disabled={!allowed.inception_dt}
                     onChange={e => setInception(e.target.value)} />
              {!allowed.inception_dt && (
                <span style={{ fontSize: 11.5 }}>
                  Can't be corrected for a {rec.lifecycle} contract.
                </span>
              )}
            </label>
            <label style={field}>
              End date
              <input className="input" type="date" value={expiry}
                     disabled={!allowed.expiry_dt}
                     onChange={e => setExpiry(e.target.value)} />
            </label>
          </div>

          {unchanged && (
            <div className="hint" style={{ marginTop: 10 }}>
              Change a date to see what the correction would do.
            </div>
          )}
          {!unchanged && blockedMsg && (
            <div className="note crit" style={{ marginTop: 12 }}>{blockedMsg}</div>
          )}
          {!unchanged && !blockedMsg && (pv?.existing_conflicts ?? []).map((w, i) => (
            <div key={i} className="note" style={{ marginTop: 12 }}>
              Already on the record, and left as it is by this correction: {w.message}
            </div>
          ))}
          {!unchanged && pv && pv.ready && (
            <div style={{ marginTop: 14 }}>
              <div style={{ fontWeight: 600, color: "var(--p-ink)", marginBottom: 4 }}>
                What this correction does {loading && <span className="sub">· updating…</span>}
              </div>
              <Line label="Dates">
                {fmtDate(pv.current.inception_dt)} – {fmtDate(pv.current.expiry_dt)}
                {" → "}<b>{fmtDate(pv.proposed.inception_dt)} – {fmtDate(pv.proposed.expiry_dt)}</b>
              </Line>
              <Line label="Status afterwards">
                {STATE_LABEL[pv.lifecycle_after] ?? pv.lifecycle_after}
                {pv.lifecycle_after !== pv.lifecycle && (
                  <> (now {STATE_LABEL[pv.lifecycle] ?? pv.lifecycle})</>
                )}
              </Line>
              <Line label="Rules re-dated">
                {pv.rules.redated.length === 0
                  ? "None hold the old dates."
                  : `${pv.rules.redated.length} rule${pv.rules.redated.length === 1 ? "" : "s"} holding the old date${Object.keys(pv.moves).length === 1 ? "" : "s"}`}
                {pv.rules.other_dates.length > 0 && (
                  <div className="sub">
                    {pv.rules.other_dates.length} hold other dates and are left as written.
                  </div>
                )}
              </Line>
              <Line label="Reporting calendar">
                {!pv.calendar?.schedule ? "No calendar for this programme." : (
                  <>
                    {(pv.calendar.added ?? []).length > 0 && <div>Adds {pv.calendar.added!.join(", ")}</div>}
                    {(pv.calendar.removed ?? []).length > 0 && (
                      <div>Removes {pv.calendar.removed!.join(", ")} (no files)</div>
                    )}
                    {!(pv.calendar.added ?? []).length && !(pv.calendar.removed ?? []).length && "No change."}
                    {(pv.calendar.kept_with_files ?? []).length > 0 && (
                      <div className="sub">Periods with files are always kept.</div>
                    )}
                  </>
                )}
              </Line>
              <Line label="Files to review">
                {pv.review_items.length === 0 ? "None." : (
                  <>
                    {pv.review_items.length} already handled, now outside the term:
                    <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                      {pv.review_items.slice(0, 5).map(i => (
                        <li key={`${i.kind}-${i.id}`}>{i.label ?? `#${i.id}`} · {i.period}</li>
                      ))}
                    </ul>
                    <div className="sub">Nothing is changed — they are listed for review.</div>
                  </>
                )}
              </Line>
              <Line label="Broker is emailed">
                {pv.broker_recipients.length ? pv.broker_recipients.join(", ") : "Nobody (no broker contacts)."}
                <div className="sub">With the reason below — never the internal note.</div>
              </Line>
              <Line label="Prior-period dates">Unchanged</Line>
            </div>
          )}

          {!unchanged && pv && pv.ready && pv.gaps.map(g => (
            <div key={`${g.code}-${g.with_id}`} className="note warn" style={{ marginTop: 12 }}>
              <b>Gap between versions.</b> {g.message}
              <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8,
                              fontWeight: 500 }}>
                <input type="checkbox" checked={gapOk} onChange={e => setGapOk(e.target.checked)} />
                I confirm the gap is intended
              </label>
            </div>
          ))}

          {!unchanged && pv && pv.ready && large && (
            <div className="note warn" style={{ marginTop: 12 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <AlertTriangle size={15} style={{ color: "var(--p-warn)" }} />
                <b>Large correction.</b>
              </div>
              <div style={{ marginTop: 4 }}>{pv.large.reasons.join(" ")}</div>
              <div style={{ marginTop: 6 }}>
                A change this large is usually agreed between both sides — consider
                recording it as an endorsement instead. To go ahead as a correction:
              </div>
              <label style={{ ...field, marginTop: 10 }}>
                1. Supporting evidence (seen by your organisation only)
                <select className="input" value={evidenceId ?? ""}
                        onChange={e => setEvidenceId(e.target.value ? Number(e.target.value) : null)}>
                  <option value="">Choose an evidence file…</option>
                  {pv.evidence_documents.map(d => (
                    <option key={d.id} value={d.id}>{d.filename}</option>
                  ))}
                </select>
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <label className="btn sm" style={{ cursor: "pointer" }}>
                    <Upload size={12} /> {busy === "evidence" ? "Attaching…" : "Attach a new evidence file"}
                    <input type="file" hidden accept=".pdf,.png,.jpg,.jpeg,.docx"
                           onChange={e => { const f = e.target.files?.[0]; if (f) attachEvidence(f); e.target.value = ""; }} />
                  </label>
                </span>
              </label>
              <label style={{ ...field, marginTop: 10 }}>
                <span>2. Type the contract code <b style={{ fontFamily: "var(--p-mono, monospace)" }}>{pv.code}</b> to confirm</span>
                <input className="input" value={typed} onChange={e => setTyped(e.target.value)}
                       placeholder={pv.code ?? ""} autoComplete="off" spellCheck={false} />
                {typed && !codeOk && <span style={{ color: "var(--p-crit-ink)" }}>Doesn't match yet.</span>}
              </label>
            </div>
          )}

          <label style={{ ...field, marginTop: 14 }}>
            <span>Reason <b>— the broker will see this</b></span>
            <textarea className="input" rows={2} value={reason}
                      onChange={e => setReason(e.target.value)}
                      placeholder="e.g. The end date was taken from the draft; the signed schedule says 31 Mar 2027." />
          </label>
          <label style={{ ...field, marginTop: 10 }}>
            Internal note — your organisation and Kavachio only (optional)
            <textarea className="input" rows={2} value={note} onChange={e => setNote(e.target.value)} />
          </label>
          {err && <div className="note crit" style={{ marginTop: 12 }}>{err}</div>}
        </div>
        <div className="m-f">
          <button className="btn" type="button" onClick={onClose} disabled={busy === "save"}>Cancel</button>
          <button className="btn pri" type="button" disabled={!canSave} onClick={save}>
            <ShieldCheck size={13} /> {busy === "save" ? "Correcting…" : "Correct the dates"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function UndoPeriodDialog({ rec, onClose, onDone }: {
  rec: ContractRecord;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [u, setU] = useState<UndoPreview | null>(null);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [gapOk, setGapOk] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  async function refresh() {
    try { setU(await periodUndoPreview(rec.id)); }
    catch (x) { setErr(fieldErrors(x).message); }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { refresh(); }, []);
  // As in the correction: a tick belongs to the gap it was given to.
  const gapKey = JSON.stringify((u?.gaps ?? []).map(g => [g.code, g.with_id, g.days]));
  useEffect(() => { setGapOk(false); }, [gapKey]);

  const blocked = !!u && (!u.available || u.blockers.length > 0);
  const codeOk = !u?.large || typed.trim() === u.code;
  const canUndo = !!u && !blocked && !!reason.trim() && (!(u.gaps ?? []).length || gapOk)
    && codeOk && !busy;

  async function go() {
    if (!u?.preview_token) return;
    setBusy("undo"); setErr("");
    try {
      await periodUndo(rec.id, { reason: reason.trim(), internal_note: note.trim() || null,
                                 confirm_gap: gapOk, confirm_text: u.large ? typed : null,
                                 preview_token: u.preview_token });
      onDone("The latest date correction was undone.");
    } catch (x) {
      const d = detailOf(x);
      setErr(d?.stale_preview ? "Something changed — check it again." : fieldErrors(x).message);
      await refresh();
    } finally { setBusy(""); }
  }

  return (
    <div className="proto-modal-overlay">
      <div className="proto-modal" style={{ ...box, width: 560 }} onClick={e => e.stopPropagation()}
           role="dialog" aria-label="Undo date correction">
        <div className="m-h">
          <Undo2 size={17} style={{ color: "var(--p-primary)" }} />
          <h3>Undo the latest date correction</h3>
          <button className="x" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="m-b" style={{ overflowY: "auto" }}>
          {!u && !err && <div className="hint">Checking…</div>}
          {u && blocked && (
            <div className="note warn">
              <b>This correction can't be undone.</b>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {u.blockers.map(b => <li key={b.code}>{b.message}</li>)}
              </ul>
            </div>
          )}
          {u && !blocked && (
            <>
              <Line label="Dates go back">
                {fmtDate(u.from?.inception_dt)} – {fmtDate(u.from?.expiry_dt)}
                {" → "}<b>{fmtDate(u.restore?.inception_dt)} – {fmtDate(u.restore?.expiry_dt)}</b>
              </Line>
              <Line label="Rules restored">{u.rules ?? 0}, exactly as they were</Line>
              <Line label="History">
                The correction stays in the history; this undo is recorded as well.
              </Line>
              {(u.gaps ?? []).map(g => (
                <div key={`${g.code}-${g.with_id}`} className="note warn" style={{ marginTop: 12 }}>
                  <b>Gap between versions.</b> {g.message}
                  <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8,
                                  fontWeight: 500 }}>
                    <input type="checkbox" checked={gapOk} onChange={e => setGapOk(e.target.checked)} />
                    I confirm the gap is intended
                  </label>
                </div>
              ))}
              {u.large && (
                <label style={{ ...field, marginTop: 12 }}>
                  <span>This undoes a large correction. Type <b style={{ fontFamily: "var(--p-mono, monospace)" }}>{u.code}</b> to confirm</span>
                  <input className="input" value={typed} onChange={e => setTyped(e.target.value)}
                         autoComplete="off" spellCheck={false} />
                </label>
              )}
              <label style={{ ...field, marginTop: 12 }}>
                <span>Reason <b>— the broker will see this</b></span>
                <textarea className="input" rows={2} value={reason} onChange={e => setReason(e.target.value)} />
              </label>
              <label style={{ ...field, marginTop: 10 }}>
                Internal note — your organisation and Kavachio only (optional)
                <textarea className="input" rows={2} value={note} onChange={e => setNote(e.target.value)} />
              </label>
            </>
          )}
          {err && <div className="note crit" style={{ marginTop: 12 }}>{err}</div>}
        </div>
        <div className="m-f">
          <button className="btn" type="button" onClick={onClose}>{blocked ? "Close" : "Cancel"}</button>
          {!blocked && (
            <button className="btn pri" type="button" disabled={!canUndo} onClick={go}>
              <Undo2 size={13} /> {busy === "undo" ? "Undoing…" : "Undo the correction"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
