/**
 * Invite Broker — how a carrier brings a broker on board.
 *
 * A broker admin is the first person at a broker. They join the BROKER and no
 * carrier at all, because the same broker produces for several carriers and
 * cannot be pinned to one.
 *
 * Carrier admins are NOT invited here. Kavachio provisions a carrier together
 * with its first admin (Platform → Carriers), so a carrier never creates
 * another carrier seat from inside its own app.
 *
 * Inviting a broker admin needs a broker to exist first. So this screen creates
 * it: you name the organisation, and it is created with this person as its
 * first admin in a single step.
 *
 * There is no "pick an existing broker" here on purpose. Every broker already
 * HAS its first admin — that is how it came to exist — so choosing one would
 * only ever mean adding a second, which is not what this screen is for. Adding
 * more staff to a broker is the broker admin's own job.
 *
 * Operators are deliberately absent: they belong to the broker, and the
 * broker's own admin adds them.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { currentMga } from "../auth";
import { InviteSentModal } from "../components/InviteSentModal";
import { createBroker } from "../api/hierarchy";

// The four kinds of organisation that can produce business. A broker is the
// usual one; the others occupy the same slot on the same terms.
const PARTY_TYPES: [string, string][] = [
  ["broker", "Broker"], ["mga", "MGA"], ["mgu", "MGU"], ["tpa", "TPA"],
];

export default function AddUser() {
  const mga = currentMga();
  const nav = useNavigate();

  const [full_name, setName] = useState("");
  const [email, setEmail] = useState("");
  // Broker admin is the only seat a carrier invites (see the docblock).
  const role = "broker_admin";

  // Broker side — the organisation this person will be the first admin of.
  const [brokerName, setBrokerName] = useState("");
  const [brokerType, setBrokerType] = useState("broker");

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<{ name: string; email: string; org: string } | null>(null);

  // Nothing to send until the form describes somebody. A broker admin also
  // needs the broker named, or there is no organisation for them to be admin of.
  const canSend = full_name.trim().length > 0 && email.trim().length > 0
    && brokerName.trim().length > 0 && !created;

  async function send() {
    if (!canSend) { setErr("Fill in the name and email first."); return; }
    setErr(null); setBusy(true);
    try {
      // One call: creates the broker organisation AND invites this person as
      // its first admin. A taken email or a name you already use is refused
      // before anything is created, so you never end up with half of it.
      const b = await createBroker({
        legal_name: brokerName.trim(),
        party_type: brokerType,
        admin_name: full_name.trim(),
        admin_email: email.trim(),
      });
      setCreated({ name: full_name.trim(), email: email.trim(), org: b.legal_name });
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not send invite.");
    } finally { setBusy(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Invite Broker</h2>
            <p>Name the broker and the person who will run it — both are created together.</p>
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

        {err && <div className="note warn" style={{ marginBottom: 18, maxWidth: 560 }}>{err}</div>}

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

          {/* Role & access */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Broker Organisation</h3>
            {/* The broker organisation itself. Created with this invitation —
                this person becomes its first admin. */}
            <div className="field">
              <label>Broker Organisation name</label>
              <input value={brokerName} placeholder="e.g. Marlowe Broking Ltd"
                onChange={e => setBrokerName(e.target.value)} />
              <div className="hint">The organisation they will run.</div>
            </div>

            <div className="hint" style={{ marginBottom: 12 }}>
              Operators are not here on purpose: they belong to the broker, and
              the broker&rsquo;s own admin adds them.
            </div>

            <div className="note" style={{ marginBottom: 0 }}>
              They sign in as a <b>broker</b>, add contracts and send you files.
              From then on they add their own staff, including operators, and you
              cannot change that list. They can only work on programmes you put
              them on — do that from <b>Programmes</b>.
            </div>
          </div>
        </div>
      </div>

      {created && (
        <InviteSentModal
          title="Broker invited"
          message={`${created.name} can set a password and sign in for ${created.org}.`}
          email={created.email}
          note="Put them on a programme from Programmes — until then they cannot produce."
          onDone={() => nav("/brokers")}
        />
      )}
    </div>
  );
}
