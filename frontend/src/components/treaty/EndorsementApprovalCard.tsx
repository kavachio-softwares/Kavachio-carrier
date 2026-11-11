/**
 * Where an endorsement stands with the reinsurers.
 *
 *   Draft → Waiting for approval → Approved → Signed by you → In force
 *   (or Changes requested, back to the carrier)
 *
 * Every reinsurer on the treaty approves it; the carrier signs it; then every
 * reinsurer signs it on its private link. Only then are its terms and rules
 * used — until then they show on the Calculation basis as waiting.
 */
import { useState } from "react";
import { CheckCircle2, Loader2, PenLine, Send } from "lucide-react";
import {
  sendEndorsementForApproval, signEndorsement, type EndorsementApproval,
  type EndorsementRecord,
} from "../../api/treaty";
import { InfoTip } from "../InfoTip";
import { showDone, showError } from "../ResultPopup";
import { fmtStamp } from "../../utils/date";

const STAGES = ["Draft", "Waiting for approval", "Approved", "Signed by you", "In force"];
const AT: Record<EndorsementApproval["status"], number> = {
  draft: 0, pending: 1, changes_requested: 1, approved: 2, carrier_signed: 3, in_force: 4,
};
const BADGE: Record<EndorsementApproval["status"], string> = {
  draft: "b-mut", pending: "b-warn", changes_requested: "b-crit", approved: "b-warn",
  carrier_signed: "b-warn", in_force: "b-ok",
};
const ANSWER: Record<string, [string, string]> = {
  pending: ["b-mut", "Not answered"],
  approved: ["b-ok", "Approved"],
  changes_requested: ["b-crit", "Asked for changes"],
};

export function EndorsementApprovalCard({ contractId, rec, onChange }: {
  contractId: number;
  rec: EndorsementRecord;
  onChange: (r: EndorsementRecord) => void;
}) {
  const a = rec.approval;
  const [busy, setBusy] = useState(false);
  const [signing, setSigning] = useState(false);
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  // Something to send: applied here (read and confirmed) or drafted here.
  const ready = !!rec.review || rec.document.drafted_here;
  const at = AT[a.status] ?? 0;
  const canSend = rec.can_edit && ["draft", "pending", "changes_requested"].includes(a.status);

  function sentNote(r: { sent: { emailed: boolean }[]; missing: string[] }, what: string) {
    const failed = r.sent.filter(x => !x.emailed).length;
    return `${what} ${r.sent.length} ${r.sent.length === 1 ? "person" : "people"} at the reinsurers.`
      + (failed ? ` ${failed} email${failed === 1 ? "" : "s"} could not be sent — try again.` : "")
      + (r.missing.length ? ` No contact with an email at ${r.missing.join(", ")}.` : "");
  }

  async function run(fn: () => Promise<EndorsementRecord & {
    sent: { emailed: boolean }[]; missing: string[] }>, what: string) {
    setBusy(true);
    try {
      const r = await fn();
      onChange(r);
      setSigning(false);
      showDone(sentNote(r, what));
    } catch (e) {
      showError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-h">
        <CheckCircle2 size={16} className="ci" />
        <h3>
          Reinsurers&rsquo; approval and signatures
          <InfoTip text={"Every reinsurer on the treaty approves the endorsement, then you sign "
            + "it, then every reinsurer signs it. Only then is it used on the reinsurance "
            + "bordereau. Until then its terms show on the Calculation basis as waiting."} />
        </h3>
        <span className={`badge ${BADGE[a.status]}`}><span className="d" />{a.label}</span>
        <div className="right" style={{ display: "flex", gap: 6 }}>
          {canSend && (
            <button className="btn pri sm" type="button" disabled={busy || !ready}
                    title={ready ? undefined : "Apply it to the treaty first"}
                    onClick={() => run(() => sendEndorsementForApproval(contractId, rec.document.id),
                                       "Sent for approval to")}>
              {busy ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
              {" "}{a.status === "draft" ? "Send for approval" : "Send again"}
            </button>
          )}
          {rec.can_edit && a.status === "approved" && !signing && (
            <button className="btn pri sm" type="button" onClick={() => setSigning(true)}>
              <PenLine size={13} /> Sign it
            </button>
          )}
        </div>
      </div>
      <div style={{ padding: "12px 20px", display: "grid", gap: 10 }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {STAGES.map((label, i) => (
            <span key={label} style={{ display: "contents" }}>
              {i > 0 && <span className="sub">→</span>}
              <span className={`badge ${i < at || (i === at && at === 4) ? "b-ok"
                : i === at ? (a.status === "changes_requested" ? "b-crit" : "b-warn") : "b-mut"}`}>
                <span className="d" />
                {i === 1 && a.status === "changes_requested" ? "Changes requested" : label}
              </span>
            </span>
          ))}
        </div>
        {a.status === "draft" && (
          <div className="sub">
            {ready ? "Not used yet. Send it to the reinsurers to approve."
                   : "Apply it to the treaty, then send it to the reinsurers to approve."}
          </div>
        )}
        {a.status === "changes_requested" && (
          <div className="sub">Change it, apply it again, then send it again.</div>
        )}
        {a.status === "approved" && !signing && (
          <div className="sub">Every reinsurer approved it. Sign it — each reinsurer is then
            sent its link to sign.</div>
        )}
        {signing && (
          <div className="note">
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
                <label>Your name</label>
                <input value={name} onChange={e => setName(e.target.value)} />
              </div>
              <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
                <label>Your title <span className="sub">— optional</span></label>
                <input value={title} onChange={e => setTitle(e.target.value)} />
              </div>
            </div>
            <div className="rowacts">
              <button className="btn pri" type="button" disabled={busy || !name.trim()}
                      onClick={() => run(() => signEndorsement(contractId, rec.document.id,
                                                               name.trim(), title.trim()),
                                         "Signed. Sent to sign to")}>
                {busy ? "Signing…" : "Sign for the carrier"}
              </button>
              <button className="btn" type="button" onClick={() => setSigning(false)}>Cancel</button>
            </div>
          </div>
        )}
        {a.carrier_signature && (
          <div className="sub">
            Signed for the carrier by <b>{a.carrier_signature.name}</b>
            {a.carrier_signature.title ? `, ${a.carrier_signature.title}` : ""} on{" "}
            {fmtStamp(a.carrier_signature.at)}.
          </div>
        )}
        {a.status !== "draft" && (
          <table className="panel-tbl" style={{ textAlign: "left" }}>
            <thead><tr><th>Reinsurer</th><th>Approval</th><th>Signature</th></tr></thead>
            <tbody>
              {a.parties.map(p => {
                const [cls, lbl] = ANSWER[p.status] ?? ANSWER.pending;
                return (
                  <tr key={p.party_id} style={{ verticalAlign: "top" }}>
                    <td>{p.party_name ?? "—"}{p.is_lead && <span className="sub"> · lead</span>}</td>
                    <td>
                      <span className={`badge ${cls}`}><span className="d" />{lbl}</span>
                      {p.signer_name && <div className="sub">{p.signer_name}{p.at ? ` · ${fmtStamp(p.at)}` : ""}</div>}
                      {p.note && <div className="sub" style={{ marginTop: 4 }}>&ldquo;{p.note}&rdquo;</div>}
                    </td>
                    <td>
                      {p.signed ? (<>
                        <span className="badge b-ok"><span className="d" />Signed</span>
                        <div className="sub">{p.signed.name} · {fmtStamp(p.signed.at)}</div>
                      </>) : a.status === "carrier_signed"
                        ? <span className="badge b-mut"><span className="d" />Not signed yet</span>
                        : <span className="sub">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {a.sent_at && (
          <div className="sub">
            Sent {fmtStamp(a.sent_at)}{a.sent_by ? ` by ${a.sent_by}` : ""} · {a.approved} of{" "}
            {a.total} approved{a.status === "carrier_signed" || a.status === "in_force"
              ? ` · ${a.signed} of ${a.total} signed` : ""}
          </div>
        )}
      </div>
    </div>
  );
}
