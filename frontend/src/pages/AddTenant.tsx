import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { isKavachioAdmin } from "../auth";
import { InfoTip } from "../components/InfoTip";
import { LoadingOverlay } from "../components/Busy";
import { InviteSentModal } from "../components/InviteSentModal";

// Kavachio provisions carriers, and only carriers. A broker is not a tenant:
// it is a party a carrier adds on its own programmes, so it can never be
// created from this screen. Kept in step with NEW_TENANT_TYPES on the backend,
// which refuses anything else.
const ORG_TYPE = "carrier";
const CURRENCIES: [string, string][] = [
  ["USD", "US Dollar"], ["GBP", "Pound"], ["EUR", "Euro"],
  ["CAD", "Canadian Dollar"], ["AUD", "Australian Dollar"],
];
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function AddTenant() {
  const nav = useNavigate();
  const isAdmin = isKavachioAdmin();
  const [f, setF] = useState({
    name: "", currency: "USD", admin_name: "", admin_email: "",
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<{ org: string; email: string } | null>(null);

  function set<K extends keyof typeof f>(k: K, v: (typeof f)[K]) { setF(p => ({ ...p, [k]: v })); }

  // Naming the gap beats a button that is dead for reasons the user cannot see.
  const missing = [
    !f.name.trim() && "Legal name",
    !f.admin_name.trim() && "Admin full name",
    !EMAIL_RE.test(f.admin_email.trim()) && "a valid admin email",
  ].filter(Boolean) as string[];
  const canCreate = missing.length === 0;

  async function create() {
    if (!canCreate) return;
    setErr(null); setBusy(true);
    try {
      await api.post("/tenants", {
        name: f.name, tenant_type: ORG_TYPE, currency: f.currency,
        is_active: true, admin_name: f.admin_name, admin_email: f.admin_email,
      });
      setCreated({ org: f.name.trim(), email: f.admin_email.trim() });
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not create the carrier.");
    } finally { setBusy(false); }
  }

  if (!isAdmin) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Create Carrier</h2></div></div>
        <div className="note warn" style={{ maxWidth: 560 }}>
          This screen is restricted to Kavachio platform admins.
        </div>
      </div></div>
    );
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Create Carrier</h2>
            <p>
              Add the insurance company and email an invitation to their first
              admin. From there, they set up their own programmes, brokers and
              contracts.
            </p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/tenants")}>← Carriers</button>
            <button className="btn pri" onClick={create} disabled={busy || !canCreate}>
              Create &amp; send invite
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 18, maxWidth: 560 }}>{err}</div>}

        <div className="grid g-2">
          {/* The carrier itself */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>
              Carrier organisation
              <InfoTip text="A brand new carrier is empty until its own admin signs in. They add the programmes and the brokers, not us." />
            </h3>
            <div className="field">
              <label>Legal name <span style={{ color: "var(--p-crit)" }}>*</span></label>
              <input value={f.name} autoFocus
                placeholder="e.g. Northgate Mutual Insurance Co"
                onChange={e => set("name", e.target.value)} />
            </div>
            <div className="field">
              <label>Base currency</label>
              <select value={f.currency} onChange={e => set("currency", e.target.value)}>
                {CURRENCIES.map(([v, l]) => <option key={v} value={v}>{v} — {l}</option>)}
              </select>
              <div className="hint">The currency this carrier reports in.</div>
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Organisation type</label>
              <input className="ro" value="Carrier" readOnly />
              <div className="hint">
                Fixed. Brokers are not set up here — a carrier adds its own
                brokers on its own programmes.
              </div>
            </div>
          </div>

          {/* Its first admin */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>First admin</h3>
            <p style={{ margin: "0 0 14px", fontSize: 12, color: "var(--p-muted)" }}>
              They get an email inviting them to set a password. After that they
              set the company up and add their own colleagues.
            </p>
            <div className="field">
              <label>Full name <span style={{ color: "var(--p-crit)" }}>*</span></label>
              <input value={f.admin_name} placeholder="Full name"
                onChange={e => set("admin_name", e.target.value)} />
            </div>
            <div className="field">
              <label>Email <span style={{ color: "var(--p-crit)" }}>*</span></label>
              <input type="email" value={f.admin_email} placeholder="admin@carrier.com"
                onChange={e => set("admin_email", e.target.value)} />
            </div>
            <div className="note" style={{ marginTop: 4 }}>
              When they accept, they arrive at an empty account and we guide them
              through adding their first programme.
            </div>
          </div>
        </div>

        {missing.length > 0 && (
          <div style={{ marginTop: 16, fontSize: 13, color: "var(--p-muted)" }}>
            Still needed: {missing.join(", ")}
          </div>
        )}
      </div>

      {busy && <LoadingOverlay label="Creating the carrier and sending the invite…" />}

      {created && (
        <InviteSentModal
          title="Carrier created"
          message={`${created.org} is now on the platform.`}
          email={created.email}
          note="They'll set a password, then add their first programme."
          onDone={() => nav("/tenants")}
        />
      )}
    </div>
  );
}
