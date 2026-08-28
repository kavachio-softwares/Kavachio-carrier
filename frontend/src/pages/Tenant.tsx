import { useEffect, useState } from "react";
import { api, getDeduped } from "../api/client";
import { currentMga, isTenantAdmin, setTenantBrand } from "../auth";
import { fileToLogoDataUrl, initials } from "../branding";
import CountryOptions from "../components/CountryOptions";

type Tenant = {
  id: number; mga: string; legal_name?: string; tenant_type?: string;
  address?: any; currency?: string; logo?: string | null;
};

// Stable signature of just the fields this page edits (and save() sends), so the
// Save button can tell "actually changed" from "loaded and untouched". Keys are
// written out in a fixed order — comparing whole objects would flag a re-ordered
// or server-extended address as a change when nothing was edited.
function editableSig(x: Tenant | null): string {
  if (!x) return "";
  const a = (x.address ?? {}) as Record<string, any>;
  return JSON.stringify({
    legal_name: x.legal_name ?? "",
    tenant_type: x.tenant_type ?? "",
    currency: x.currency ?? "",
    logo: x.logo ?? null,
    line1: a.line1 ?? "", city: a.city ?? "", state: a.state ?? "",
    zip: a.zip ?? "", country: a.country ?? "",
  });
}

const CURRENCIES: [string, string][] = [
  ["USD", "US Dollar"], ["GBP", "Pound"], ["EUR", "Euro"], ["CAD", "Canadian Dollar"],
  ["AUD", "Australian Dollar"], ["INR", "Indian Rupee"], ["JPY", "Yen"],
  ["CHF", "Swiss Franc"], ["SGD", "Singapore Dollar"], ["AED", "UAE Dirham"],
];
const ORG_TYPES: [string, string][] = [
  ["carrier", "Carrier"], ["mga", "MGA"], ["mgu", "MGU"],
  ["broker", "Broker"], ["tpa", "TPA"], ["reinsurer", "Reinsurer"],
];

export default function TenantPage() {
  const mga = currentMga();
  const isAdmin = isTenantAdmin();
  const [t, setT] = useState<Tenant | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // Signature of the last persisted state — Save stays disabled until the form
  // differs from it, and goes back to disabled once a save lands.
  const [savedSig, setSavedSig] = useState("");

  useEffect(() => {
    getDeduped<Tenant>(`/tenants/${mga}`).then(r => {
      setT(r.data); setSavedSig(editableSig(r.data));
    });
  }, [mga]);

  const dirty = editableSig(t) !== savedSig;

  function patch<K extends keyof Tenant>(k: K, v: Tenant[K]) {
    setT(prev => prev ? { ...prev, [k]: v } : prev);
  }
  function patchAddr(k: string, v: string) {
    setT(prev => prev ? { ...prev, address: { ...(prev.address ?? {}), [k]: v } } : prev);
  }

  async function onLogoFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!f) return;
    setMsg(null);
    try { patch("logo", await fileToLogoDataUrl(f)); }
    catch (er: any) { setMsg(er?.message ?? "Could not read that image."); }
  }

  async function save() {
    if (!t) return;
    setMsg(null); setSaving(true);
    try {
      const { data } = await api.put(`/tenants/${mga}`, {
        legal_name: t.legal_name, tenant_type: t.tenant_type,
        address: t.address, currency: t.currency, logo: t.logo ?? null,
      });
      setT(data);
      setSavedSig(editableSig(data));   // back to "no unsaved changes"
      // Refresh the sidebar co-brand right away.
      setTenantBrand({ mga, legal_name: data.legal_name, logo: data.logo ?? null });
      setMsg("Saved.");
    } catch (e: any) {
      setMsg(e?.message ?? "Save failed.");
    } finally { setSaving(false); }
  }

  if (!t) return null;
  const addr = t.address ?? {};

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Organization</h2>
            <p>Your organization's identity, currency and address.</p>
          </div>
          {isAdmin && (
            <div className="actions">
              <button className="btn pri" onClick={save} disabled={saving || !dirty}
                title={dirty ? undefined : "No changes to save"}>
                Save Changes
              </button>
              {msg && <span className="muted" style={{ alignSelf: "center", fontSize: 13 }}>{msg}</span>}
            </div>
          )}
        </div>

        <div className="grid g-2">
          {/* Identity */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Organization Identity</h3>
            <div className="field">
              <label>Organization Logo</label>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                {/* Preview on a dark swatch that mirrors the sidebar, so the
                    logo is shown exactly as it will appear there (no white box). */}
                {t.logo
                  ? <img src={t.logo} alt="Organization logo"
                      style={{ width: 48, height: 48, borderRadius: 9, objectFit: "contain",
                               background: "#131a29", border: "1px solid rgba(255,255,255,.08)" }} />
                  : <div style={{ width: 48, height: 48, borderRadius: 9, display: "grid",
                               placeItems: "center",
                               background: "linear-gradient(135deg,#077282,#03A2A6)",
                               color: "#fff", fontSize: 14, fontWeight: 700 }}>
                      {initials(t.legal_name)}
                    </div>}
                {isAdmin && (
                  <>
                    <label className="btn" style={{ cursor: "pointer" }}>
                      {t.logo ? "Replace" : "Upload Logo"}
                      <input type="file" accept="image/*" style={{ display: "none" }} onChange={onLogoFile} />
                    </label>
                    {t.logo && (
                      <button type="button" className="btn ghost"
                        onClick={() => patch("logo", null)}>Remove</button>
                    )}
                  </>
                )}
              </div>
              <span className="muted" style={{ fontSize: 11 }}>
                Shown in the sidebar on a dark background — a transparent or light
                logo looks best. PNG, SVG or JPG.
              </span>
            </div>
            <div className="field">
              <label>Organization Name</label>
              <input value={t.legal_name ?? ""} disabled={!isAdmin}
                onChange={e => patch("legal_name", e.target.value)} />
            </div>
            <div className="row2">
              <div className="field">
                <label>Account Code</label>
                <input className="ro" value={t.mga} readOnly />
              </div>
              <div className="field">
                <label>Base Currency</label>
                <select value={t.currency ?? ""} disabled={!isAdmin}
                  onChange={e => patch("currency", e.target.value)}>
                  <option value="">—</option>
                  {CURRENCIES.map(([c, n]) => <option key={c} value={c}>{c} — {n}</option>)}
                </select>
              </div>
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Organization Type</label>
              <select value={t.tenant_type ?? ""} disabled={!isAdmin}
                onChange={e => patch("tenant_type", e.target.value)}>
                <option value="">Select…</option>
                {ORG_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
          </div>

          {/* Address */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Address</h3>
            <div className="field">
              <label>Street</label>
              <input value={addr.line1 ?? ""} disabled={!isAdmin}
                onChange={e => patchAddr("line1", e.target.value)} />
            </div>
            <div className="row2">
              <div className="field">
                <label>City</label>
                <input value={addr.city ?? ""} disabled={!isAdmin}
                  onChange={e => patchAddr("city", e.target.value)} />
              </div>
              <div className="field">
                <label>State</label>
                <input value={addr.state ?? ""} disabled={!isAdmin}
                  onChange={e => patchAddr("state", e.target.value)} />
              </div>
            </div>
            <div className="row2">
              <div className="field" style={{ marginBottom: 0 }}>
                <label>ZIP</label>
                <input value={addr.zip ?? ""} disabled={!isAdmin}
                  onChange={e => patchAddr("zip", e.target.value)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Country</label>
                <select value={addr.country ?? ""} disabled={!isAdmin}
                  onChange={e => patchAddr("country", e.target.value)}>
                  <option value="">Select Country…</option>
                  <CountryOptions />
                </select>
              </div>
            </div>
          </div>
        </div>

        {!isAdmin && (
          <div className="note" style={{ marginTop: 18, maxWidth: 560 }}>
            Organization settings are read-only for your role. Ask a Tenant Admin to make changes.
          </div>
        )}
      </div>
    </div>
  );
}
