import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";

const LOB = ["Commercial Auto", "Property", "General Liability", "Personal Lines", "Workers' Comp", "Other"];
const STATUS = ["Active", "Inactive"];

export default function AddProgram() {
  const mga = currentMga();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const partyId = params.get("party");
  const [carrier, setCarrier] = useState("");
  const [f, setF] = useState({ name: "", product_line: "Commercial Auto", status: "Active" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (partyId) {
      api.get(`/parties/${partyId}`).then(r => setCarrier(r.data.legal_name ?? "")).catch(() => {});
    }
  }, [partyId]);

  async function save() {
    if (!f.name.trim()) { setErr("Program name is required."); return; }
    setErr(null); setBusy(true);
    try {
      await api.post("/programs", {
        name: f.name, product_line: f.product_line, status: f.status,
        party_id: partyId ? Number(partyId) : undefined,
      }, { params: { mga } });
      // The program's contract is uploaded later, in Setup.
      nav("/direct/setup");
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not create program.");
    } finally { setBusy(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Add Program</h2>
            <p>A program is a book of business under a carrier — what a Setup and contract are scoped to.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav(partyId ? `/parties/${partyId}` : "/parties")}>← Carrier</button>
            <button className="btn pri" onClick={save} disabled={busy}>
              Save & Continue to Setup →
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 18, maxWidth: 560 }}>{err}</div>}

        {/* Program. Fields auto-fit the card: four across on a wide screen, folding
            to fewer columns as it narrows (`.row2` is a fixed 1fr 1fr and can't). */}
        <div className="card pad">
          <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Program</h3>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 16 }}>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Carrier</label>
              <input className="ro" value={carrier || "—"} readOnly />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Program name</label>
              <input value={f.name} autoFocus placeholder="e.g. Spectrum Transportation"
                onChange={e => setF({ ...f, name: e.target.value })} />
            </div>
            {/* <div className="field" style={{ marginBottom: 0 }}>
              <label>Line of business</label>
              <select value={f.product_line} onChange={e => setF({ ...f, product_line: e.target.value })}>
                {LOB.map(l => <option key={l} value={l}>{l}</option>)}
              </select>
            </div> */}
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Status</label>
              <select value={f.status} onChange={e => setF({ ...f, status: e.target.value })}>
                {STATUS.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
          </div>
        </div>

        <div className="note" style={{ marginTop: 18 }}>
          Saving takes you straight to the <b>Bordereau Setup</b>, where you map the fields, upload
          the program's contract and confirm its rules.
        </div>
      </div>
    </div>
  );
}
