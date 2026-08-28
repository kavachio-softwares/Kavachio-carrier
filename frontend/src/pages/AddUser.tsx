import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";

export default function AddUser() {
  const mga = currentMga();
  const nav = useNavigate();
  const [full_name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("ops");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<{ name: string; email: string } | null>(null);

  // Nothing to send until both required fields are filled — keeps the button
  // inert on an untouched form instead of letting it through to a
  // "Name and email are required" error. Also stays disabled after the invite
  // has gone out, so the same person can't be invited twice on a double-click.
  const canSend = full_name.trim().length > 0 && email.trim().length > 0 && !created;

  async function send() {
    if (!full_name.trim() || !email.trim()) { setErr("Name and email are required."); return; }
    setErr(null); setBusy(true);
    try {
      await api.post("/users", { full_name, email, role }, { params: { mga } });
      setCreated({ name: full_name.trim(), email: email.trim() });
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
            <p>Send an invite to a teammate and set what they can do.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/users")}>← Users</button>
            <button className="btn pri" onClick={send} disabled={busy || !canSend}
              title={canSend ? undefined : created ? "Invite already sent" : "Enter a name and email first"}>
              Send invite
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
                <option value="ops">Operator — run files &amp; review exceptions</option>
                <option value="admin">Broker Admin — everything in the organization</option>
              </select>
              <div className="hint">Two roles only — labels match the Users table.</div>
            </div>
            <div className="note">
              <b>Broker Admin</b> manages setups, parties, users and org settings.
              {" "}<b>Operator</b> processes bordereaux and reviews exceptions.
            </div>
          </div>
        </div>
      </div>

      {created && (
        <div className="proto-modal-overlay">
          <div className="proto-modal tenant-success" onClick={e => e.stopPropagation()}>
            <div className="ts-icon">
              <svg viewBox="0 0 56 56" fill="none" aria-hidden="true">
                <circle cx="28" cy="28" r="28" fill="var(--p-ok-soft)" />
                <path d="M18 28.5l6.5 6.5L38 21" stroke="var(--p-ok)" strokeWidth="3"
                  strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </div>
            <h3 className="ts-title">User created</h3>
            <p className="ts-org">{created.name} has been added to your organization.</p>
            <div className="ts-row">
              <svg className="ts-row-ic" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <rect x="2.5" y="4.5" width="15" height="11" rx="2" stroke="currentColor" strokeWidth="1.4" />
                <path d="M3 5.5l7 5.5 7-5.5" stroke="currentColor" strokeWidth="1.4"
                  strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span>Invite sent to <b>{created.email}</b></span>
            </div>
            <p className="ts-sub">They'll set a password and sign in.</p>
            <button className="btn pri ts-done" onClick={() => nav("/users")}>Done</button>
          </div>
        </div>
      )}
    </div>
  );
}
