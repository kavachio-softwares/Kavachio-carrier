/**
 * "Send a link to sign" — on a treaty's Sign & track tab.
 *
 * A reinsurer has no seat in Kavachio, so it cannot sign in the app the way a
 * broker does. Instead of the carrier typing the reinsurer's name in for them
 * (a RECORDED signature, which needs the signed copy as proof), the carrier
 * emails one person at the lead reinsurer a private link. They open it with
 * the one-time code sent to that address, read the treaty, and sign it
 * themselves — stored as signed on the link, with their email and the time,
 * and as the second signature it puts the treaty in force.
 *
 * Only once the carrier has signed: the carrier signs first, as everywhere.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Clock, Eye, Link2, RefreshCw, Send } from "lucide-react";
import { fieldErrors } from "../../api/contractRecord";
import {
  getReinsurerAccess, resendReinsurerLink, shareWithReinsurers,
  type ReinsurerAccess,
} from "../../api/treaty";
import { fmtStamp } from "../../utils/date";

export default function TreatySignLink({ contractId, onSigned }: {
  contractId: number;
  /** Called when the list shows a new signature, so the page can reload. */
  onSigned?: () => void;
}) {
  const [acc, setAcc] = useState<ReinsurerAccess | null>(null);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  useEffect(() => {
    getReinsurerAccess(contractId).then(setAcc).catch(() => setAcc(null));
  }, [contractId]);

  if (!acc?.signing) return null;
  const sg = acc.signing;
  const lead = acc.contacts.find(p => p.is_lead_reinsurer);
  const asked = acc.links.filter(l => l.sign_requested_at);
  const signed = asked.find(l => l.signed_at);

  // Nothing to offer and nothing to show.
  if (!asked.length && !sg.can_request) return null;

  async function send() {
    setBusy(true); setErr(""); setOk("");
    try {
      const r = await shareWithReinsurers(contractId, [Number(pick)], true);
      setAcc(r);
      const s = r.sent[0];
      setOk(s?.emailed
        ? `Sent to ${s.name} (${s.email}). They open it with the code in the email and sign there.`
        : `The link was made, but the email to ${s?.email} did not go. Use Resend to try again.`);
      setPick("");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  async function resend(id: number) {
    setBusy(true); setErr(""); setOk("");
    try {
      const r = await resendReinsurerLink(contractId, id);
      setAcc(r);
      setOk(r.emailed ? "Sent again — the earlier link no longer works."
                      : "The email did not go. Try again in a moment.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="note" style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
        <Link2 size={14} />
        <b>Send a link to sign</b>
      </div>
      <div className="sub" style={{ marginBottom: 10, color: "var(--p-ink-2, inherit)" }}>
        Email someone at {lead?.party_name ?? "the lead reinsurer"} a private link.
        They open it with a one-time code sent to the same address, read the
        treaty, and sign it themselves. It is stored as their signature — with
        their email and the time — not one you entered for them, and it puts
        the treaty in force.
      </div>

      {err && <div className="note warn" style={{ marginBottom: 10 }}>{err}</div>}
      {ok && <div className="note ok" style={{ marginBottom: 10 }}>{ok}</div>}

      {asked.map(l => (
        <div className="kv" key={l.id}>
          <span className="k">
            <b style={{ color: "var(--p-ink)" }}>{l.name}</b>
            <div className="sub">{l.email}</div>
          </span>
          <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            {l.signed_at ? (
              <span className="badge b-ok"><span className="d" /><Check size={11} />
                Signed {fmtStamp(l.signed_at)}</span>
            ) : l.first_opened_at ? (
              <span className="badge b-info"><span className="d" /><Eye size={11} />
                Opened {fmtStamp(l.last_viewed_at ?? l.first_opened_at)}</span>
            ) : (
              <span className="badge b-mut"><span className="d" /><Clock size={11} />
                Sent {l.sent_at ? fmtStamp(l.sent_at) : ""}</span>
            )}
            {!l.signed_at && !signed && l.status === "active" && (
              <button className="btn sm" type="button" disabled={busy}
                      onClick={() => resend(l.id)}>
                <RefreshCw size={12} /> Resend
              </button>
            )}
          </span>
        </div>
      ))}

      {sg.can_request && !signed && (
        lead && lead.contacts.some(c => c.email) ? (
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap",
                        marginTop: asked.length ? 10 : 0 }}>
            <select value={pick} onChange={e => setPick(e.target.value)}
                    style={{ flex: "1 1 260px", maxWidth: 420 }}>
              <option value="">Who signs for {lead.party_name}?</option>
              {lead.contacts.filter(c => c.email).map(c => (
                <option key={c.id} value={String(c.id)}>
                  {c.name}{c.title ? ` — ${c.title}` : ""} · {c.email}
                </option>
              ))}
            </select>
            <button className="btn pri" type="button" disabled={!pick || busy} onClick={send}>
              <Send size={14} /> {asked.length ? "Send to someone else" : "Send signing link"}
            </button>
          </div>
        ) : (
          <div className="sub">
            {lead?.party_name ?? "The lead reinsurer"} has no contact with an email
            address.{" "}
            {lead && (
              <Link className="linkish" to={`/parties/${lead.party_id}`}>Add one</Link>
            )}
            {" "}to send them a link.
          </div>
        )
      )}
      {!sg.can_request && !signed && sg.why_not && !asked.length && (
        <div className="sub">Not yet — {sg.why_not}.</div>
      )}
    </div>
  );
}
