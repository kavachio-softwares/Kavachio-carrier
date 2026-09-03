/**
 * Invite User — the one place a carrier brings anybody on board.
 *
 * Two kinds of person, and they land in different places:
 *
 *   Carrier Admin — a colleague. Joins YOUR organisation.
 *   Broker Admin  — the first person at a broker. Joins the BROKER, and no
 *                   carrier at all, because the same broker produces for
 *                   several carriers and cannot be pinned to one.
 *
 * Because of that second point, inviting a broker admin needs a broker to exist
 * first. So this screen creates it: you name the organisation, and it is created
 * with this person as its first admin in a single step.
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
import { api } from "../api/client";
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
  const [role, setRole] = useState("carrier_admin");

  // Broker side — the organisation this person will be the first admin of.
  const [brokerName, setBrokerName] = useState("");
  const [brokerType, setBrokerType] = useState("broker");

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<{ name: string; email: string; org: string } | null>(null);

  // Nothing to send until the form describes somebody. A broker admin also
  // needs the broker named, or there is no organisation for them to be admin of.
  const canSend = full_name.trim().length > 0 && email.trim().length > 0 && !created
    && (role !== "broker_admin" || brokerName.trim().length > 0);

  async function send() {
    if (!canSend) { setErr("Fill in the name and email first."); return; }
    setErr(null); setBusy(true);
    try {
      let org = mga ?? "your organization";
      if (role === "broker_admin") {
        // One call: creates the broker organisation AND invites this person as
        // its first admin. A taken email or a name you already use is refused
        // before anything is created, so you never end up with half of it.
        const b = await createBroker({
          legal_name: brokerName.trim(),
          party_type: brokerType,
          admin_name: full_name.trim(),
          admin_email: email.trim(),
        });
        org = b.legal_name;
      } else {
        await api.post("/users", { full_name, email, role }, { params: { mga } });
      }
      setCreated({ name: full_name.trim(), email: email.trim(), org });
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not send invite.");
    } finally { setBusy(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Invite User</h2>
            <p>Add a colleague to your own team, or bring a broker on board.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/users")}>← Users</button>
            <button className="btn pri" onClick={send} disabled={busy || !canSend}
              title={canSend ? undefined : created ? "Invite already sent"
                : role === "broker_admin" && !brokerName.trim() ? "Name the broker first"
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
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Email</label>
              <input type="email" value={email} placeholder="name@company.com"
                onChange={e => setEmail(e.target.value)} />
              <div className="hint">The invite and password-setup link are sent here.</div>
            </div>
          </div>

          {/* Role & access */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Role &amp; access</h3>
            <div className="field">
              <label>Role</label>
              <select value={role} onChange={e => setRole(e.target.value)}>
                <option value="carrier_admin">Carrier Admin — someone on your own team</option>
                <option value="broker_admin">Broker Admin — someone at a broker who sends you files</option>
              </select>
              <div className="hint">
                Operators are not here on purpose: they belong to the broker, and
                the broker&rsquo;s own admin adds them.
              </div>
            </div>

            {/* The broker organisation itself. Created with this invitation —
                this person becomes its first admin. */}
            {role === "broker_admin" && (
              <div className="row2">
                <div className="field">
                  <label>Broker name</label>
                  <input value={brokerName} placeholder="e.g. Marlowe Broking Ltd"
                    onChange={e => setBrokerName(e.target.value)} />
                  <div className="hint">The organisation they will run.</div>
                </div>
                <div className="field">
                  <label>Type</label>
                  <select value={brokerType} onChange={e => setBrokerType(e.target.value)}>
                    {PARTY_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </div>
              </div>
            )}

            <div className="note" style={{ marginBottom: 0 }}>
              {role === "broker_admin" ? (
                <>They sign in as a <b>broker</b>, add contracts and send you files.
                  From then on they add their own staff, including operators, and you
                  cannot change that list. They can only work on programmes you put
                  them on — do that from <b>Programmes</b>.</>
              ) : (
                <>They join your organization and can do everything you can,
                  including approving contracts. Only invite people you trust with that.</>
              )}
            </div>
          </div>
        </div>
      </div>

      {created && (
        <InviteSentModal
          title={role === "broker_admin" ? "Broker invited" : "User invited"}
          message={role === "broker_admin"
            ? `${created.name} can set a password and sign in for ${created.org}.`
            : `${created.name} has been added to your organization.`}
          email={created.email}
          note={role === "broker_admin"
            ? "Put them on a programme from Programmes — until then they cannot produce."
            : "They'll set a password and sign in."}
          onDone={() => nav("/users")}
        />
      )}
    </div>
  );
}
