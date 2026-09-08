/**
 * My Contracts — every contract this broker holds, across every programme a
 * carrier has put them on.
 *
 * Two kinds sit in one list. A contract the CARRIER added works straight away.
 * A contract the BROKER added waits until the carrier approves it, and only a
 * live one can be set up. That approval is the only permission a broker ever
 * needs — after it, the setup and everything downstream is theirs to do.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getBrokerContracts, getBrokerCarriers,
  type BrokerContract, type BrokerCarrier, type Lifecycle,
} from "../api/broker";
import { inAppSigningUrl } from "../api/esign";
import { fmtDate } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";

/** Where the contract is in its life, said from the BROKER's side.
 *
 *  This is a different question from `approval_status`, which only answers
 *  whether this broker may set the contract up. A contract in `in_review` is
 *  one the carrier has sent over for the broker to read — the negotiation is
 *  waiting on THEM — and describing it as "approved" hides exactly the thing
 *  they need to see. */
const STATE: Record<Lifecycle, { label: string; cls: string; note: string }> = {
  draft: { label: "Not live yet", cls: "b-mut",
           note: "no signatures on it yet — it goes live when both sides sign" },
  pending: { label: "Pending", cls: "b-warn", note: "waiting on the carrier" },
  in_review: { label: "For your review", cls: "b-warn",
               note: "read the terms — agree them or ask for changes" },
  changes_requested: { label: "Changes asked for", cls: "b-warn",
                       note: "you pushed back — the carrier is revising" },
  agreed: { label: "Terms agreed", cls: "b-ok", note: "yours to sign" },
  signed: { label: "Signed", cls: "b-ok", note: "waiting on the carrier to sign" },
  active: { label: "Live", cls: "b-ok", note: "in force — you can produce against it" },
  expired: { label: "Expired", cls: "b-mut", note: "its term has run out" },
  terminated: { label: "Terminated", cls: "b-crit", note: "ended early" },
  superseded: { label: "Superseded", cls: "b-mut", note: "replaced by a renewal" },
};

/** What the row means to the broker, which is not the raw column value. */
function approval(c: BrokerContract): { cls: string; label: string; note: string } {
  if (c.approval_status === "approved")
    return c.source === "carrier"
      ? { cls: "b-ok", label: "Live", note: "the carrier added it — no approval needed" }
      : { cls: "b-ok", label: "Approved", note: "the carrier approved it" };
  if (c.approval_status === "pending_approval")
    return { cls: "b-warn", label: "Pending carrier", note: "you cannot set it up yet" };
  if (c.approval_status === "rejected")
    return { cls: "b-crit", label: "Rejected", note: "sent back with a reason" };
  return { cls: "b-mut", label: "Draft", note: "not sent yet" };
}

export default function BrokerContracts() {
  const [rows, setRows] = useState<BrokerContract[] | null>(null);
  const [carriers, setCarriers] = useState<BrokerCarrier[]>([]);
  const [carrier, setCarrier] = useState("");
  const [status, setStatus] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { getBrokerCarriers().then(setCarriers).catch(() => setCarriers([])); }, []);

  const load = useCallback(() => {
    getBrokerContracts({ carrierId: carrier ? Number(carrier) : undefined })
      .then(setRows)
      .catch(() => setErr("Could not load your contracts."));
  }, [carrier]);
  useEffect(load, [load]);

  const shown = (rows ?? []).filter(r =>
    !status
    || (status === "mine" ? r.whose_turn === "broker" : r.approval_status === status));
  const filtersActive = carrier !== "" || status !== "";

  // The only queue a broker cannot move by waiting. Surfaced above the table
  // because it is the reason to open this page at all — a negotiation that
  // does not announce itself is one nobody answers.
  const mine = (rows ?? []).filter(r => r.whose_turn === "broker");

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>My Contracts</h2>
            <p>
              Every contract you hold, across all the programmes you have been
              put on. You do not add the programme — the carrier does.
            </p>
          </div>
          <div className="actions">
            <Link to="/broker/contracts/new" className="btn pri">
              ＋ Upload Contract
            </Link>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14, maxWidth: 560 }}>{err}</div>}

        {mine.length > 0 && (
          <div className="note warn" style={{ marginBottom: 16 }}>
            <b>
              {mine.length} contract{mine.length === 1 ? " is" : "s are"} waiting
              on you.
            </b>{" "}
            {mine.map((m, i) => (
              <span key={m.id}>
                {i > 0 && ", "}
                <Link to={`/contracts/${m.id}`} className="linkish">{m.name}</Link>
                {" "}({STATE[m.lifecycle].label.toLowerCase()})
              </span>
            ))}
            {mine.some(m => m.lifecycle === "agreed" || m.lifecycle === "signed")
              ? " The carrier has signed the ones marked terms agreed — they "
                + "are waiting on your signature, and the contract goes in "
                + "force the moment you give it."
              : " Open one to read the terms and either agree them or ask for "
                + "changes — nothing moves until you do."}
          </div>
        )}

        <div className="note" style={{ marginBottom: 16 }}>
          <b>Two columns, two questions.</b> <b>State</b> is where the contract
          is in its life and whose move it is — terms sent for you to read,
          changes you asked for, yours to sign. <b>Approval</b> is only about
          contracts <i>you</i> added, which wait for the carrier to accept them.
          A contract can be approved and still not live: one goes in force when
          both sides have signed it.
        </div>

        <div className="card">
          <ListFilterBar
            selects={[
              // Only the carriers that have actually put this broker on a
              // programme — a broker never picks a carrier freely.
              {
                key: "carrier", ariaLabel: "Filter by carrier", value: carrier, onChange: setCarrier,
                options: [{ value: "", label: "All carriers" },
                  ...carriers.map(c => ({ value: String(c.id), label: c.name }))],
              },
              {
                key: "status", ariaLabel: "Filter by state", value: status, onChange: setStatus,
                options: [
                  { value: "", label: "All statuses" },
                  { value: "mine", label: "Waiting on me" },
                  { value: "approved", label: "Live / Approved" },
                  { value: "pending_approval", label: "Pending carrier" },
                  { value: "rejected", label: "Rejected" },
                ],
              },
            ]}
            onClear={() => { setCarrier(""); setStatus(""); }}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Contract</th><th>Carrier</th><th>Programme</th>
                  <th>Term</th><th>State</th><th>Approval</th><th>Bordereau</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(c => {
                  const a = approval(c);
                  const st = STATE[c.lifecycle] ?? STATE.draft;
                  // A live contract is one you can actually produce against,
                  // so the column offers the thing to DO rather than a status
                  // word — "Not set up yet" named a screen the broker has no
                  // access to and could do nothing about.
                  const canSetUp = c.approval_status === "approved"
                    && c.lifecycle === "active";
                  return (
                    <tr key={c.id}>
                      <td>
                        {/* Opens the contract RECORD: its terms, the documents
                            it is made of, and — after a rejection — the form to
                            correct it and re-submit. */}
                        <Link to={`/contracts/${c.id}`}><b>{c.name}</b></Link>
                        <div className="sub">
                          {c.source === "carrier"
                            ? "the carrier added it" : "you added it"}
                          {c.filename && <> · {c.filename}</>}
                        </div>
                      </td>
                      <td>{c.carrier.name}</td>
                      <td>{c.programme.name}</td>
                      <td className="muted">
                        {c.inception_dt && c.expiry_dt
                          ? `${c.inception_dt} → ${c.expiry_dt}` : "—"}
                      </td>
                      <td>
                        {/* The state, not the approval. These answer different
                            questions and the broker is usually here for this
                            one: whose move is it. */}
                        <span className={`badge ${st.cls}`}>
                          <span className="d" />{st.label}
                        </span>
                        <div className="sub">{st.note}</div>
                      </td>
                      <td>
                        <span className={`badge ${a.cls}`}><span className="d" />{a.label}</span>
                        <div className="sub">{a.note}</div>
                      </td>
                      <td className={canSetUp ? "" : "muted"}>
                        {canSetUp
                          ? <Link to="/broker/bordereau">Process bordereau →</Link>
                          // Waiting on this broker's signature. Offered here
                          // rather than only on the contract's own page,
                          // because this is the screen they are on — and by
                          // the time a contract reaches this state the carrier
                          // has signed and the only thing left is them.
                          : (c.whose_turn === "broker"
                             && (c.lifecycle === "agreed" || c.lifecycle === "signed"))
                            ? <a className="linkish" href={inAppSigningUrl(c.id)}
                                 target="_blank" rel="noreferrer">Sign it →</a>
                          : c.approval_status !== "approved"
                            ? "Locked"
                            // Approved but not in force. Since a contract goes
                            // live only when both sides have signed it, saying
                            // "locked" would hide a thing the broker can act on.
                            : "Not live yet"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {rows !== null && shown.length === 0 && (
              <div className="empty">
                {filtersActive
                  ? "No contracts match the filters."
                  : "No contracts yet. A carrier has to put you on a programme first."}
              </div>
            )}
            {rows === null && !err && <div className="empty">Loading…</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
