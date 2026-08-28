import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { isKavachioAdmin } from "../auth";
import { InfoTip } from "../components/InfoTip";
import { LoadingOverlay } from "../components/Busy";

// Carriers aren't provisioned as their own platform tenant —
// they're onboarded as Party directory entries (Parties/AddParty) under an
// MGA/MGU/broker/TPA's own tenant instead. Kept in sync with the same
// allow-list the backend enforces on tenant creation (app_routes.py).
const TYPES: [string, string][] = [
  ["mga", "MGA"], ["mgu", "MGU"], ["broker", "Broker"], ["tpa", "TPA"],
];
const CURRENCIES: [string, string][] = [
  ["USD", "US Dollar"], ["GBP", "Pound"], ["EUR", "Euro"],
  ["CAD", "Canadian Dollar"], ["AUD", "Australian Dollar"],
];
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function AddTenant() {
  const nav = useNavigate();
  const isAdmin = isKavachioAdmin();
  const [f, setF] = useState({
    name: "", tenant_type: "mga", currency: "USD",
    admin_name: "", admin_email: "",
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<{ org: string; email: string } | null>(null);

  function set<K extends keyof typeof f>(k: K, v: (typeof f)[K]) { setF(p => ({ ...p, [k]: v })); }

  const canCreate = f.name.trim() !== "" && f.admin_name.trim() !== "" && EMAIL_RE.test(f.admin_email.trim());

  async function create() {
    if (!canCreate) return;
    setErr(null); setBusy(true);
    try {
      await api.post("/tenants", {
        name: f.name, tenant_type: f.tenant_type, currency: f.currency,
        is_active: true, admin_name: f.admin_name, admin_email: f.admin_email,
      });
      setCreated({ org: f.name.trim(), email: f.admin_email.trim() });
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not create broker.");
    } finally { setBusy(false); }
  }

  if (!isAdmin) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Add Broker</h2></div></div>
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
            <h2>Add Broker</h2>
            <p>Create a new organization on Kavachio and send its admin an invite to get started.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/tenants")}>← Brokers</button>
            <button className="btn pri" onClick={create} disabled={busy || !canCreate}>
              Create Broker
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 18, maxWidth: 560 }}>{err}</div>}

        <div className="grid g-2">
          {/* Organization */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>
              Organization
              <InfoTip text="This organization starts with nothing configured — its admin will add carriers, programs, and users after signing in." />
            </h3>
            <div className="field">
              <label>Organization name <span style={{ color: "var(--p-crit)" }}>*</span></label>
              <input value={f.name} autoFocus placeholder="e.g. Northwind Underwriting"
                onChange={e => set("name", e.target.value)} />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Type</label>
              <select value={f.tenant_type} onChange={e => set("tenant_type", e.target.value)}>
                {TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            <div className="field" style={{ marginBottom: 0, marginTop: 16 }}>
              <label>Base currency</label>
              <select value={f.currency} onChange={e => set("currency", e.target.value)}>
                {CURRENCIES.map(([v, l]) => <option key={v} value={v}>{v} — {l}</option>)}
              </select>
            </div>
          </div>

          {/* Admin */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>
              Admin
              <InfoTip text="An email invite is sent so they can set a password. From there, they can add their own teammates." />
            </h3>
            <div className="field">
              <label>Admin full name <span style={{ color: "var(--p-crit)" }}>*</span></label>
              <input value={f.admin_name} placeholder="Full name"
                onChange={e => set("admin_name", e.target.value)} />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Admin email <span style={{ color: "var(--p-crit)" }}>*</span></label>
              <input type="email" value={f.admin_email} placeholder="admin@org.com"
                onChange={e => set("admin_email", e.target.value)} />
            </div>
          </div>
        </div>
      </div>

      {busy && <LoadingOverlay label="Creating the broker and sending the invite…" />}

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
            <h3 className="ts-title">Broker created</h3>
            <p className="ts-org">{created.org} is now live on the platform.</p>
            <div className="ts-row">
              <svg className="ts-row-ic" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <rect x="2.5" y="4.5" width="15" height="11" rx="2" stroke="currentColor" strokeWidth="1.4" />
                <path d="M3 5.5l7 5.5 7-5.5" stroke="currentColor" strokeWidth="1.4"
                  strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span>Invite sent to <b>{created.email}</b></span>
            </div>
            <p className="ts-sub">They'll set a password and sign in as admin.</p>
            <button className="btn pri ts-done" onClick={() => nav("/tenants")}>Done</button>
          </div>
        </div>
      )}
    </div>
  );
}
