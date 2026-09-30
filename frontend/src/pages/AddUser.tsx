/**
 * Invite a BROKER — an outside company and its broker admin.
 *
 * This page used to add a carrier user as well. Carrier users were retired on
 * 29 Sep 2026: the platform has three seats (Kavachio admin, carrier admin,
 * broker admin), so the carrier admin adds nobody at their own organisation
 * and the one act left here is inviting a broker.
 *
 *   Broker    the BROKER ADMIN — the first person at a broker, an outside
 *             company — invited by the carrier admin. They join the broker
 *             and no carrier at all, because the same broker produces for
 *             several carriers and cannot be pinned to one. The broker
 *             organisation does not exist yet, so this creates it in the same
 *             step.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { InviteSentModal } from "../components/InviteSentModal";
import { InfoTip } from "../components/InfoTip";
import { inviteBroker } from "../api/hierarchy";
import { useCarrierSeat } from "../hooks/useCarrierSeat";

// The four kinds of organisation that can produce business. A broker is the
// usual one; the others occupy the same slot on the same terms.
const PARTY_TYPES: [string, string][] = [
  ["broker", "Broker"], ["mga", "MGA"], ["mgu", "MGU"], ["tpa", "TPA"],
];

export default function AddUser() {
  const nav = useNavigate();
  const seat = useCarrierSeat();

  const [full_name, setName] = useState("");
  const [email, setEmail] = useState("");

  // The organisation this person will be the first admin of.
  const [brokerName, setBrokerName] = useState("");
  const [brokerType, setBrokerType] = useState("broker");

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // `pending` is set when the invite became a REQUEST for the carrier admin
  // (only while the approval flow is switched on). Nothing was created and
  // nothing was sent, so the confirmation below must not say either.
  const [created, setCreated] = useState<{
    name: string; email: string; org: string;
    /** The organisation name as TYPED. `org` holds the server's sentence for
     *  the sent case, which is not a name and cannot be put in bold. */
    orgTyped?: string; pending?: boolean;
  } | null>(null);

  // Nothing to send until the form describes somebody, and the broker is
  // named — or there is no organisation for them to be admin of.
  const canSend = full_name.trim().length > 0 && email.trim().length > 0
    && brokerName.trim().length > 0 && !created;

  async function send() {
    if (!canSend) { setErr("Fill in the name and email first."); return; }
    setErr(null); setBusy(true);
    try {
      // One call: creates the broker organisation AND invites this person as
      // its first admin. A taken email or a name you already use is refused
      // before anything is created, so you never end up with half of it.
      const r = await inviteBroker({
        legal_name: brokerName.trim(),
        party_type: brokerType,
        admin_name: full_name.trim(),
        admin_email: email.trim(),
      });
      // The organisation name is what WE typed, not something the server
      // confirmed: if that address already belongs to a broker, no
      // organisation was created and their real name is not ours to show.
      setCreated({ name: full_name.trim(), email: email.trim(),
                   org: r.message, orgTyped: brokerName.trim(),
                   pending: r.pending ?? !r.invited });
    } catch (e: any) {
      // `detail` is a string for simple refusals and an object for the ones
      // carrying a remedy. Rendering the object would crash the page, so it is
      // unpacked here rather than trusted to be text.
      const d = e?.response?.data?.detail;
      setErr((typeof d === "string" ? d : d?.message) ?? "Could not send invite.");
    } finally { setBusy(false); }
  }

  if (seat === null) {
    // The organisation (and so which seat this is) has not loaded yet.
    return <div className="proto"><div className="view full"><div className="empty">Loading…</div></div></div>;
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Invite a broker
              <InfoTip text={"Name the broker company and the "
                + "person who runs it on Kavachio. Both are created together."} />
            </h2>
            <p>An outside broker company and the person who runs it.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/brokers")}>← Brokers</button>
            <button className="btn pri" onClick={send} disabled={busy || !canSend}
              title={canSend ? undefined : created ? "Invite already sent"
                : !brokerName.trim() ? "Name the broker first"
                : "Enter a name and email first"}>
              {busy ? "Sending…" : "Send invite"}
            </button>
          </div>
        </div>

        {err && (
          <div className="note warn" style={{ marginBottom: 18, maxWidth: 560 }}>
            {err}
          </div>
        )}

        <div className="grid g-2">
          {/* Person */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Person</h3>
            <div className="field">
              <label>Full name</label>
              <input value={full_name} autoFocus placeholder="e.g. Priya Nair"
                onChange={e => setName(e.target.value)} />
            </div>
            <div className="field">
              <label>Email</label>
              <input type="email" value={email} placeholder="name@company.com"
                onChange={e => setEmail(e.target.value)} />
              <div className="hint">The invite and password-setup link are sent here.</div>
            </div>
            {/* Sits with the person who is being invited, though it is SAVED on
                the organisation (party.party_type) — one invitation creates
                both, so which card it appears in is a question of where it
                reads best, not of where the value lives. */}
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Type</label>
              <select value={brokerType} onChange={e => setBrokerType(e.target.value)}>
                {PARTY_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <div className="hint">What kind of intermediary they are.</div>
            </div>
          </div>

          {/* The broker organisation itself, created with this invitation —
              this person becomes its first admin. */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Broker Organisation</h3>
            <div className="field">
              <label>Broker Organisation name</label>
              <input value={brokerName} placeholder="e.g. Marlowe Broking Ltd"
                onChange={e => setBrokerName(e.target.value)} />
            </div>
          </div>
        </div>
      </div>

      {created && (
        <InviteSentModal
          /* A green tick says "done", and it is read before the headline. On a
             request nothing has been done — it is waiting on somebody. */
          kind={created.pending ? "pending" : "sent"}
          title={created.pending ? "Waiting for approval" : "Broker invited"}
          message={created.pending
            ? <>Nothing is sent to <b>{created.orgTyped}</b> until your carrier
                admin approves.</>
            : created.org}
          /* No envelope row on a request: nothing has been sent. */
          email={created.pending ? undefined : created.email}
          note={created.pending
            ? "You will be told either way."
            : "They appear on your Brokers list once they accept."}
          doneLabel={created.pending ? "See my requests" : "Done"}
          onDone={() => nav(created.pending ? "/brokers/requests" : "/brokers")}
        />
      )}
    </div>
  );
}
