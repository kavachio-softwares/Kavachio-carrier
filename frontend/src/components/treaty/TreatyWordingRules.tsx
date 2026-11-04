/**
 * A treaty's Clauses tab, the treaty's half: its rules — the conditions the
 * ceded business must meet, from its terms and read from its signed wording —
 * and the button that reads the wording for its clauses and rules (done when
 * a signed treaty is uploaded; again here, or for one uploaded before that).
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Loader2, RefreshCw, ShieldCheck } from "lucide-react";
import { fieldErrors } from "../../api/contractRecord";
import { getSetup, type TreatyRule } from "../../api/ceded";
import { readTreatyClauses } from "../../api/treaty";

const SOURCE: Record<TreatyRule["source"], string> = {
  terms: "The treaty's terms", wording: "The signed wording", endorsement: "An endorsement",
};

export function TreatyWordingRules({ contractId, clauseCount, onRead }: {
  contractId: number;
  /** Clauses already on file — the button reads "Read again" once there are. */
  clauseCount: number;
  /** After a reading: reload the clauses. */
  onRead: () => void;
}) {
  const [rules, setRules] = useState<TreatyRule[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");

  const load = () => getSetup(contractId)
    .then(r => setRules(r.setup?.rules ?? r.defaults.rules ?? []))
    .catch(() => setRules([]));
  useEffect(() => { load(); }, [contractId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function read() {
    setBusy(true); setErr(""); setNote("");
    try {
      const r = await readTreatyClauses(contractId);
      setNote(`Read ${r.clauses} clause${r.clauses === 1 ? "" : "s"} and `
        + `${r.rules} rule${r.rules === 1 ? "" : "s"} from the signed wording.`);
      onRead();
      load();
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        <ShieldCheck size={16} className="ci" />
        <h3>Treaty rules</h3>
        <span className="sub">
          {rules == null ? "…" : `${rules.length} check${rules.length === 1 ? "" : "s"} on the ceded business`}
        </span>
        <span className="right">
          <button type="button" className="btn" disabled={busy} onClick={read}
            title="Read the signed wording for its clauses and the conditions the ceded business must meet">
            {busy ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
            {busy ? " Reading…" : clauseCount ? " Read again" : " Read clauses and rules"}
          </button>
        </span>
      </div>
      {note && <div className="note ok" style={{ margin: "0 18px 12px" }}>{note}</div>}
      {err && <div className="note warn" style={{ margin: "0 18px 12px" }}>{err}</div>}
      {rules != null && rules.length === 0 ? (
        <div className="empty">
          No rules yet — read the signed wording, or set out the treaty's structure.
        </div>
      ) : rules != null && (
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr><th>Rule</th><th>From</th><th>If a row breaks it</th></tr>
            </thead>
            <tbody>
              {rules.map(r => (
                <tr key={r.id}>
                  <td>
                    <b>{r.label}</b>
                    {r.clause && <div className="sub">“{r.clause}”</div>}
                  </td>
                  <td className="sub">{SOURCE[r.source] ?? r.source}</td>
                  <td>
                    <span className={`badge ${r.severity === "critical" ? "b-crit" : "b-warn"}`}>
                      <span className="d" />{r.severity === "critical" ? "Critical" : "Warning"}
                    </span>
                    {!r.enabled && <div className="sub">Switched off</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="sub" style={{ padding: "10px 18px" }}>
        Switched on or off, and Critical or Warning, on the treaty's{" "}
        <Link to={`/reinsurance/setups/${contractId}/edit?tab=rules`}>reinsurance bordereau setup</Link>.
      </div>
    </div>
  );
}
