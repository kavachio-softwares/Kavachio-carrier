import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";

const TYPES: [string, string][] = [
  ["carrier", "Carrier"],
];
const AM_BEST = ["—", "A++ (Superior)", "A+ (Superior)", "A (Excellent)", "A- (Excellent)", "B++ (Good)"];

export default function AddParty() {
  const mga = currentMga();
  const nav = useNavigate();
  const [f, setF] = useState({
    legal_name: "", dba_name: "", party_type: "carrier", naics_code: "",
    tax_id: "", am_best_rating: "", primary_jurisdiction: "",
    domicile_country: "United States", is_active: true,
  });
  const [c, setC] = useState({ full_name: "", title: "", email: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function set<K extends keyof typeof f>(k: K, v: (typeof f)[K]) { setF(p => ({ ...p, [k]: v })); }

  // next: where to go after a successful save.
  async function save(next: "directory" | "program") {
    if (!f.legal_name.trim()) { setErr("Legal name is required."); return; }
    setErr(null); setBusy(true);
    try {
      const { data } = await api.post("/parties", {
        ...f, am_best_rating: f.am_best_rating === "—" ? "" : f.am_best_rating, scope: "tenant",
      }, { params: { mga } });
      if (c.full_name.trim() || c.email.trim()) {
        await api.post(`/parties/${data.id}/contacts`, c);
      }
      nav(next === "program" ? `/programs/new?party=${data.id}` : "/parties");
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not create party.");
    } finally { setBusy(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Add Carrier</h2>
            <p>Create a carrier in your organization's directory.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/parties")}>← Directory</button>
            <button className="btn" onClick={() => save("directory")} disabled={busy}>
              Save
            </button>
            <button className="btn pri" onClick={() => save("program")} disabled={busy}>
              Save &amp; Add Program →
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 18, maxWidth: 560 }}>{err}</div>}

        <div className="grid g-2">
          {/* Identity */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Carrier Identity</h3>
            <div className="field">
              <label>Legal name</label>
              <input value={f.legal_name} autoFocus placeholder="e.g. Insurisk Specialty Insurance Co"
                onChange={e => set("legal_name", e.target.value)} />
            </div>
            <div className="row2">
              <div className="field">
                <label>Type</label>
                <select value={f.party_type} onChange={e => set("party_type", e.target.value)}>
                  {TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>NAIC company code</label>
                <input value={f.naics_code} placeholder="e.g. 24-7781"
                  onChange={e => set("naics_code", e.target.value)} />
              </div>
            </div>
          </div>

          {/* Details */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Details</h3>
            <div className="row2">
              {/* <div className="field">
                <label>AM Best rating</label>
                <select value={f.am_best_rating || "—"} onChange={e => set("am_best_rating", e.target.value)}>
                  {AM_BEST.map(a => <option key={a} value={a}>{a}</option>)}
                </select>
                <div className="hint">Mainly relevant for carriers.</div>
              </div> */}
              <div className="field">
                <label>State</label>
                <input value={f.primary_jurisdiction} placeholder="e.g. TX"
                  onChange={e => set("primary_jurisdiction", e.target.value)} />
              </div>
              <div className="field">
                <label>Country</label>
                <input value={f.domicile_country} onChange={e => set("domicile_country", e.target.value)} />
              </div>
            </div>

            <label className="toggle" onClick={() => set("is_active", !f.is_active)}>
              <span className={`sw2${f.is_active ? " on" : ""}`} /> Active on creation
            </label>
            <div className="note" style={{ marginTop: 16 }}>
              This carrier is <b>scoped to your organization</b>. Global records are managed by Kavachio.
            </div>
          </div>
        </div>

        {/* Primary contact */}
        <div className="card pad" style={{ marginTop: 18 }}>
          <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>
            Primary Contact <span style={{ fontWeight: 400, color: "var(--p-faint)", fontSize: 12 }}>— optional</span>
          </h3>
          <div className="row2">
            <div className="field">
              <label>Name</label>
              <input value={c.full_name} placeholder="Full name"
                onChange={e => setC({ ...c, full_name: e.target.value })} />
            </div>
            <div className="field">
              <label>Function</label>
              <input value={c.title} placeholder="e.g. Bordereau analyst"
                onChange={e => setC({ ...c, title: e.target.value })} />
            </div>
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Email</label>
            <input type="email" value={c.email} placeholder="name@company.com"
              onChange={e => setC({ ...c, email: e.target.value })} />
          </div>
        </div>

        {/* Next-step hint */}
        <div className="card pad" style={{ marginTop: 18, display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ flex: 1, fontSize: 13, color: "var(--p-muted)" }}>
            For a <b>carrier</b>, the next step is usually a <b>program</b> — then its contract and BDX
            setup.
          </div>
          <button className="btn" onClick={() => save("program")} disabled={busy}>Add a Program →</button>
        </div>
      </div>
    </div>
  );
}
