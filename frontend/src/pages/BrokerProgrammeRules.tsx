// Programme Rules — the broker's side (backend programme_rule_routes.py): the
// rules a carrier set for a programme this broker is on, sent to it to
// approve. A rule checks this broker's files only once it approves. Only the
// rules sent to this broker's own organisation are listed.
import { useEffect, useState } from "react";
import { CheckCircle2, MessageSquare } from "lucide-react";
import {
  approveBrokerRule, getBrokerRules, requestBrokerRuleChanges, type BrokerRule,
} from "../api/programmeRules";
import { InfoTip } from "../components/InfoTip";
import { showDone, showError } from "../components/ResultPopup";
import { fmtDay } from "../utils/reportFormat";

const SEVERITY: Record<string, string> = { critical: "b-crit", warning: "b-warn", info: "b-info" };

export default function BrokerProgrammeRules() {
  const [rules, setRules] = useState<BrokerRule[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [asking, setAsking] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getBrokerRules().then(d => setRules(d.rules))
      .catch(e => setFailed(e?.response?.data?.detail ?? "Could not load the programme rules."));
  }, []);

  async function answer(r: BrokerRule, approve: boolean) {
    setBusy(true);
    try {
      const d = approve ? await approveBrokerRule(r.id) : await requestBrokerRuleChanges(r.id, note.trim());
      setRules(d.rules);
      setAsking(null); setNote("");
      showDone(approve ? `Approved. ${r.carrier ?? "The carrier"} has been told, and this rule now checks your files for ${r.programme ?? "the programme"}.`
        : `Sent to ${r.carrier ?? "the carrier"}. You will be asked again once the rule is changed.`);
    } catch (e) { showError(e, "That did not go through"); }
    finally { setBusy(false); }
  }

  const waiting = (rules ?? []).filter(r => r.status !== "approved");
  const done = (rules ?? []).filter(r => r.status === "approved");

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Programme Rules
              <InfoTip text={"Rules a carrier set for a whole programme you are on.\nA rule checks your files only once you approve it."} />
            </h2>
          </div>
        </div>
        {failed ? <div className="card"><div className="empty">{failed}</div></div>
          : !rules ? <div className="card"><div className="empty">Loading…</div></div>
          : rules.length === 0 ? <div className="card"><div className="empty">No programme rules have been sent to you.</div></div>
          : (<>
            <div className="card" style={{ marginBottom: 18 }}>
              <div className="card-h"><h3>Waiting for you</h3>
                <span className={`badge ${waiting.length ? "b-warn" : "b-mut"}`} style={{ padding: "2px 8px" }}>{waiting.length}</span></div>
              {waiting.length === 0 ? <div className="empty">Nothing is waiting for you.</div> : (
                <div style={{ padding: "4px 16px 16px", display: "grid", gap: 12 }}>
                  {waiting.map(r => (
                    <div key={r.id} style={{ borderTop: "1px solid var(--p-border)", paddingTop: 12 }}>
                      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                        <b>{r.name}</b>
                        <span className={`badge ${SEVERITY[r.severity] ?? "b-mut"}`}>{r.severity[0].toUpperCase() + r.severity.slice(1)}</span>
                        {r.status === "changes_requested" && <span className="badge b-warn">You asked for changes</span>}
                      </div>
                      <div style={{ margin: "4px 0", fontSize: 14 }}>{r.sentence}</div>
                      <div className="sub">
                        {r.carrier ?? "Carrier"} · {r.programme ?? "Programme"} · {r.contracts.join(", ") || "your contracts"}
                        {r.sent_at && <> · sent {fmtDay(r.sent_at.slice(0, 10))}</>}
                      </div>
                      {r.description && <div className="sub" style={{ marginTop: 4 }}>{r.description}</div>}
                      {r.status === "changes_requested" && r.note && (
                        <div className="sub" style={{ marginTop: 4 }}>You asked: <i>“{r.note}”</i></div>)}
                      {asking === r.id ? (
                        <div style={{ marginTop: 10, display: "grid", gap: 8, maxWidth: 560 }}>
                          <textarea rows={3} value={note} onChange={e => setNote(e.target.value)}
                            placeholder="What should change? e.g. Make it 6,000,000 for fleet policies."
                            style={{ width: "100%", padding: 8, border: "1px solid var(--p-border)", borderRadius: 6 }} />
                          <div style={{ display: "flex", gap: 8 }}>
                            <button className="btn pri sm" disabled={busy || !note.trim()} onClick={() => answer(r, false)}>
                              {busy ? "Sending…" : "Send"}</button>
                            <button className="btn sm" onClick={() => { setAsking(null); setNote(""); }}>Cancel</button>
                          </div>
                        </div>
                      ) : (
                        <div style={{ marginTop: 10, display: "flex", gap: 8 }}>
                          <button className="btn pri sm" disabled={busy} onClick={() => answer(r, true)}>
                            <CheckCircle2 size={14} /> Approve</button>
                          {r.status !== "changes_requested" && (
                            <button className="btn sm" disabled={busy} onClick={() => setAsking(r.id)}>
                              <MessageSquare size={14} /> Request changes</button>)}
                        </div>
                      )}
                    </div>))}
                </div>)}
            </div>

            <div className="card">
              <div className="card-h"><h3>Approved</h3>
                <span className="badge b-mut" style={{ padding: "2px 8px" }}>{done.length}</span></div>
              {done.length === 0 ? <div className="empty">You have not approved any rule yet.</div> : (
                <div className="tbl-wrap"><table>
                  <thead><tr><th className="l">Rule</th><th className="l">Programme</th><th className="l">Carrier</th><th>Approved</th></tr></thead>
                  <tbody>{done.map(r => (
                    <tr key={r.id}>
                      <td className="l"><b>{r.name}</b><div className="sub">{r.sentence}</div></td>
                      <td className="l">{r.programme ?? "—"}</td>
                      <td className="l">{r.carrier ?? "—"}</td>
                      <td>{r.at ? fmtDay(r.at.slice(0, 10)) : "—"}</td>
                    </tr>))}</tbody>
                </table></div>)}
            </div>
          </>)}
      </div>
    </div>
  );
}
