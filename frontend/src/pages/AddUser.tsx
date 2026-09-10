/**
 * Add someone — a CARRIER at this organisation, or a BROKER outside it.
 *
 * Two different acts behind one door, because both answer "who else works on
 * this" and both are the carrier admin's alone. What separates them is who the
 * person belongs to:
 *
 *   Carrier   a colleague HERE. They join this organisation and do the
 *             carrier's work — contracts, programmes, bordereaux — and they do
 *             not manage people. Only the carrier admin does that, and there
 *             is one of those per organisation (the owner pointer on `tenant`;
 *             see migration 18).
 *
 *   Broker    the first person at a BROKER, an outside company. They join the
 *             broker and no carrier at all, because the same broker produces
 *             for several carriers and cannot be pinned to one. The broker
 *             organisation does not exist yet, so this creates it in the same
 *             step — there is no "pick an existing broker" because every
 *             broker already HAS its first admin; choosing one could only mean
 *             adding a second, which is the broker admin's own job.
 *
 * ADDING A CARRIER HAPPENS HERE AND NOWHERE ELSE. It used to be possible from
 * the party directory, which added a carrier-shaped row to a COMPANY list —
 * a different thing wearing the same word, and a second front door to a list
 * of people that only Users & Roles is supposed to own.
 *
 * Operators are deliberately absent from both: they belong to the broker, and
 * the broker's own admin adds them.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { currentMga, getTenantBrand } from "../auth";
import { api } from "../api/client";
import { InviteSentModal } from "../components/InviteSentModal";
import { inviteBroker } from "../api/hierarchy";

// The four kinds of organisation that can produce business. A broker is the
// usual one; the others occupy the same slot on the same terms.
const PARTY_TYPES: [string, string][] = [
  ["broker", "Broker"], ["mga", "MGA"], ["mgu", "MGU"], ["tpa", "TPA"],
];

export default function AddUser() {
  const mga = currentMga();
  const brand = getTenantBrand();
  const nav = useNavigate();

  // Which of the two acts this is. Asked first, because it changes what the
  // rest of the form even means.
  const [kind, setKind] = useState<"carrier" | "broker">("carrier");

  const [full_name, setName] = useState("");
  const [email, setEmail] = useState("");

  // Broker side — the organisation this person will be the first admin of.
  const [brokerName, setBrokerName] = useState("");
  const [brokerType, setBrokerType] = useState("broker");


  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<{ name: string; email: string; org: string } | null>(null);

  // Nothing to send until the form describes somebody. A broker admin ALSO
  // needs the broker named, or there is no organisation for them to be admin
  // of; a carrier joins the one you are already in, so there is nothing extra
  // to say.
  const canSend = full_name.trim().length > 0 && email.trim().length > 0
    && (kind === "carrier" || brokerName.trim().length > 0) && !created;

  async function send() {
    if (!canSend) { setErr("Fill in the name and email first."); return; }
    setErr(null); setBusy(true);
    try {
      if (kind === "carrier") {
        // Joins THIS organisation. `carrier_admin` is the DB role every
        // carrier-side person holds — the column takes four values and the
        // distinction that matters (who may add and remove) is the owner
        // pointer, not a fifth role. So this adds a carrier, not a second
        // carrier admin.
        await api.post(`/users?mga=${encodeURIComponent(mga)}`, {
          full_name: full_name.trim(),
          email: email.trim(),
          role: "carrier_admin",
        });
        setCreated({ name: full_name.trim(), email: email.trim(),
                     org: brand?.legal_name || "your organisation" });
      } else {
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
                     org: r.message });
      }
    } catch (e: any) {
      // `detail` is a string for simple refusals and an object for the ones
      // carrying a remedy. Rendering the object would crash the page, so it is
      // unpacked here rather than trusted to be text.
      const d = e?.response?.data?.detail;
      setErr((typeof d === "string" ? d : d?.message) ?? "Could not send invite.");
    } finally { setBusy(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>{kind === "carrier" ? "Add a carrier" : "Invite a broker"}</h2>
            <p>
              {kind === "carrier"
                ? "A colleague at your organisation. They do the carrier's work "
                  + "— contracts, programmes and bordereaux."
                : "Name the broker and the person who will run it — both are "
                  + "created together."}
            </p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/users")}>← Users &amp; Roles</button>
            <button className="btn pri" onClick={send} disabled={busy || !canSend}
              title={canSend ? undefined : created ? "Invite already sent"
                : (kind === "broker" && !brokerName.trim()) ? "Name the broker first"
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

        {/* Asked first, because it changes what the rest of the form means:
            a carrier joins the organisation you are already in, a broker
            arrives with a company that has to be created around them. */}
        <div className="card pad" style={{ marginBottom: 18 }}>
          <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Who are you adding?</h3>
          <div className="hint" style={{ marginBottom: 12 }}>
            This is the only place either one is added.
          </div>
          <div className="segpick">
            <button type="button" className={kind === "carrier" ? "on" : ""}
                    onClick={() => setKind("carrier")} disabled={!!created}>
              A carrier — a colleague here
            </button>
            <button type="button" className={kind === "broker" ? "on" : ""}
                    onClick={() => setKind("broker")} disabled={!!created}>
              A broker — an outside company
            </button>
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            {kind === "carrier"
              ? "They join " + (brand?.legal_name || "your organisation")
                + " and work on its contracts, programmes and bordereaux. They "
                + "do not add or remove people — only you do."
              : "They join the BROKER, not you. The same broker produces for "
                + "several carriers, so it cannot belong to one."}
          </div>
        </div>

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
                reads best, not of where the value lives. Meaningless for a
                carrier, who joins an organisation that already exists. */}
            <div className="field" style={{ marginBottom: 0 }}
                 hidden={kind === "carrier"}>
              <label>Type</label>
              <select value={brokerType} onChange={e => setBrokerType(e.target.value)}>
                {PARTY_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <div className="hint">What kind of intermediary they are.</div>
            </div>
          </div>

          {kind === "carrier" ? (
            <div className="card pad">
              <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>What they can do</h3>
              <div className="kv">
                <span className="k">
                  <b style={{ color: "var(--p-ink)" }}>Contracts, programmes, bordereaux</b>
                  <div className="sub">
                    The carrier's work. They can raise contracts, run files and
                    see everything this organisation holds.
                  </div>
                </span>
              </div>
              <div className="kv">
                <span className="k">
                  <b style={{ color: "var(--p-ink)" }}>Not people</b>
                  <div className="sub">
                    They cannot add or remove anyone. One person per
                    organisation does that — the carrier admin — and that is
                    you. If you are leaving, hand the role over from Users
                    &amp; Roles first; it is what lets somebody remove you.
                  </div>
                </span>
              </div>
              <div className="note" style={{ marginBottom: 0 }}>
                They get an email inviting them to set a password. Until they
                accept, they show as <b>Invited</b> and cannot sign in.
              </div>
            </div>
          ) : (
            /* The broker organisation itself, created with this invitation. */
            <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Broker Organisation</h3>
            {/* The broker organisation itself. Created with this invitation —
                this person becomes its first admin. */}
            <div className="field">
              <label>Broker Organisation name</label>
              <input value={brokerName} placeholder="e.g. Marlowe Broking Ltd"
                onChange={e => setBrokerName(e.target.value)} />
              <div className="hint">
                Used if they are new to the platform. If they already have a
                login, they keep the organisation they have.
              </div>
            </div>


            <div className="hint" style={{ marginBottom: 12 }}>
              Operators are not here on purpose: they belong to the broker, and
              the broker&rsquo;s own admin adds them.
            </div>

            <div className="note" style={{ marginBottom: 0 }}>
              <b>They have to accept.</b> If they are new they will be onboarded
              first, and accepting happens as they finish. If they already work
              with another carrier they keep the login they have and simply
              accept — you will not be told which of the two it was.
              <br /><br />
              You are inviting them to work with <b>you</b>, not with one
              programme. Put them on programmes afterwards, from{" "}
              <b>Programmes</b> — as many as you like, whenever you like.
            </div>
            </div>
          )}
        </div>
      </div>

      {created && (
        <InviteSentModal
          title={kind === "carrier" ? "Carrier added" : "Broker invited"}
          message={kind === "carrier"
            ? `${created.name} can set a password and sign in for ${created.org}.`
            : created.org}
          email={created.email}
          note={kind === "carrier"
            ? "They can work on everything this organisation holds. Adding and "
              + "removing people stays with you."
            : "They appear on your Brokers list once they accept."}
          onDone={() => nav(kind === "carrier" ? "/users" : "/brokers")}
        />
      )}
    </div>
  );
}
