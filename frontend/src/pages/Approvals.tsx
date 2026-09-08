/**
 * Approvals — what the carrier's brokers have submitted and are waiting on.
 *
 * The gate. A broker can bring a contract, but it does not take effect on the
 * broker's say-so: the carrier owns the book, so the carrier decides what
 * enters it. Anything the CARRIER raises skips this queue entirely — there is
 * nobody left to approve it.
 *
 * Rejecting sends a submission back with a reason rather than deleting it. The
 * contract returns to draft so the broker can correct and re-submit, and the
 * rejection stays on its approval history — "sent back in June, and why" has to
 * survive the correction that followed.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Clock, XCircle } from "lucide-react";
import {
  approveContract, getApprovals, rejectContract, type PendingApproval,
} from "../api/hierarchy";
import { fmtDate } from "../utils/date";

/** How long it has been waiting, and how loudly to say so.
 *
 *  A queue that shows dates makes the reader do the arithmetic; a queue that
 *  shows "11 days" makes the oldest item obvious, which is the only thing the
 *  reader is really scanning for. */
function waiting(since: string | null): { label: string; cls: string } {
  if (!since) return { label: "—", cls: "b-mut" };
  const days = Math.floor((Date.now() - new Date(since).getTime()) / 86_400_000);
  if (days >= 7) return { label: `${days} days`, cls: "b-crit" };
  if (days >= 3) return { label: `${days} days`, cls: "b-warn" };
  if (days <= 0) return { label: "today", cls: "b-mut" };
  return { label: `${days} day${days === 1 ? "" : "s"}`, cls: "b-mut" };
}

const TYPE_LABEL: Record<string, string> = {
  insurer_broker: "Insurer ↔ Broker",
  insurer_reinsurer: "Insurer ↔ Reinsurer",
};

export default function Approvals() {
  const [rows, setRows] = useState<PendingApproval[] | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [done, setDone] = useState("");
  const [programme, setProgramme] = useState("");
  // Which row is being rejected, and the reason being typed for it. A rejection
  // without a reason is a wall, not a decision, so the server refuses one — the
  // form asks for it inline rather than letting the user find that out.
  const [rejecting, setRejecting] = useState<number | null>(null);
  const [reason, setReason] = useState("");

  const load = useCallback(() => {
    getApprovals()
      .then(setRows)
      .catch(e => setErr(e?.response?.data?.detail || "Could not load approvals."));
  }, []);
  useEffect(load, [load]);

  const programmes = useMemo(() => {
    const seen = new Map<number, string>();
    (rows ?? []).forEach(r => r.programme && seen.set(r.programme.id, r.programme.name));
    return [...seen.entries()];
  }, [rows]);

  const shown = (rows ?? []).filter(
    r => !programme || String(r.programme?.id) === programme);

  async function decide(row: PendingApproval, approve: boolean) {
    setBusy(row.contract_id);
    setErr("");
    try {
      if (approve) {
        await approveContract(row.contract_id);
        setDone(`${row.name} is approved and now live.`);
      } else {
        await rejectContract(row.contract_id, reason.trim());
        setDone(`${row.name} was sent back to ${row.broker?.legal_name ?? "the broker"}.`);
      }
      setRejecting(null);
      setReason("");
      load();
    } catch (e) {
      const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setErr(typeof d === "string" ? d : "That decision could not be recorded.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Approvals</h2>
            <p>What your brokers have submitted and are waiting on you for.</p>
          </div>
          {programmes.length > 1 && (
            <div className="actions">
              <select
                className="fbar-select" aria-label="Filter by programme"
                value={programme} onChange={e => setProgramme(e.target.value)}
              >
                <option value="">All programmes</option>
                {programmes.map(([id, name]) => (
                  <option key={id} value={String(id)}>{name}</option>
                ))}
              </select>
            </div>
          )}
        </div>

        <div className="note" style={{ marginBottom: 16 }}>
          <b>A broker's contract is not live until you say so.</b> Until then it
          cannot be set up and nothing can be produced against it. Anything you
          raise yourself skips this queue — you own the book, so there is nobody
          left to approve it. Rejecting sends a contract back with a reason so it
          can be corrected, rather than deleting it.
        </div>

        {err && (
          <div className="note warn" style={{ marginBottom: 16, maxWidth: 620 }}>
            {err}
          </div>
        )}
        {done && (
          <div className="note ok" style={{ marginBottom: 16, maxWidth: 620 }}>
            {done}
          </div>
        )}

        <div className="card">
          <div className="card-h">
            <h3>Contracts</h3>
            {shown.length > 0 && (
              <span className="badge b-warn"><span className="d" />{shown.length}</span>
            )}
            <span className="sub">gate — blocks bordereau setup</span>
          </div>

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Contract</th><th>Broker</th><th>Programme</th>
                  <th>Term</th><th>Waiting</th><th />
                </tr>
              </thead>
              <tbody>
                {shown.map(r => {
                  const w = waiting(r.submitted_at);
                  const isRejecting = rejecting === r.contract_id;
                  return (
                    <tr key={r.contract_id}>
                      <td>
                        <Link to={`/contracts/${r.contract_id}`}><b>{r.name}</b></Link>
                        <div className="sub">
                          {r.contract_type
                            ? TYPE_LABEL[r.contract_type] ?? r.contract_type
                            : "type not set"}
                          {r.umr && <> · {r.umr}</>}
                          {r.class_of_business && <> · {r.class_of_business}</>}
                        </div>
                        {r.submitted_by && (
                          <div className="sub">
                            submitted by {r.submitted_by.full_name}
                          </div>
                        )}
                      </td>
                      <td>{r.broker?.legal_name ?? "—"}</td>
                      <td>{r.programme?.name ?? "—"}</td>
                      <td className="mono">
                        {r.inception_dt && r.expiry_dt
                          ? `${fmtDate(r.inception_dt)} → ${fmtDate(r.expiry_dt)}`
                          : "—"}
                      </td>
                      <td>
                        <span className={`badge ${w.cls}`}>
                          <span className="d" /><Clock size={11} /> {w.label}
                        </span>
                      </td>
                      <td>
                        {isRejecting ? (
                          <div style={{ width: 260 }}>
                            <textarea
                              autoFocus rows={2} value={reason}
                              onChange={e => setReason(e.target.value)}
                              placeholder="Why is it going back? The broker sees this."
                              style={{
                                width: "100%", fontSize: 12, padding: "6px 8px",
                                border: "1px solid var(--p-border-2)",
                                borderRadius: "var(--p-r-sm)", fontFamily: "inherit",
                              }}
                            />
                            <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
                              <button
                                className="btn danger sm" type="button"
                                disabled={!reason.trim() || busy === r.contract_id}
                                onClick={() => decide(r, false)}
                              >
                                Send back
                              </button>
                              <button
                                className="btn sm" type="button"
                                onClick={() => { setRejecting(null); setReason(""); }}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="rowacts">
                            <Link to={`/contracts/${r.contract_id}`} className="linkish">
                              Review
                            </Link>
                            <button
                              className="btn pri sm" type="button"
                              disabled={busy === r.contract_id}
                              onClick={() => decide(r, true)}
                            >
                              <CheckCircle2 size={12} /> Approve
                            </button>
                            <button
                              className="btn sm" type="button"
                              disabled={busy === r.contract_id}
                              onClick={() => { setRejecting(r.contract_id); setReason(""); }}
                            >
                              <XCircle size={12} /> Reject
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {rows !== null && shown.length === 0 && (
              <div className="empty">Nothing is waiting on you.</div>
            )}
            {rows === null && !err && <div className="empty">Loading…</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
