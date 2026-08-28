import { Fragment, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { SlidersHorizontal } from "lucide-react";
import { api } from "../api/client";
import { currentMga, isTenantAdmin } from "../auth";
import { toTitleCase } from "../api/validation";

type Party = {
  id: number; scope: string; party_type: string; legal_name: string;
  dba_name?: string; tax_id?: string; naics_code?: string;
  domicile_country?: string;
  primary_jurisdiction?: string; is_active: boolean;
  addresses: any[]; notes?: string;
};
type ProgramContract = { id: number; filename?: string; status?: string };
type Program = {
  id: number; name: string; party_id?: number | null;
  lead_carrier?: string; admin_party?: string; bdx_frequency?: string;
  business_segment?: string; product_line?: string;
  distribution_channel?: string; territory?: string; status?: string;
  contracts?: ProgramContract[];
};
// Which Bordereau Setup (Pipeline) governs a program — one row per program_id,
// preferring its active pipeline (what actually runs) over any older draft.
type ProgramPipeline = { id: number; program_id: number | null; status: "draft" | "active" | "superseded" };

const TYPES: [string, string][] = [
  ["carrier", "Carrier"],
];
const TYPE_LABEL: Record<string, string> = Object.fromEntries(TYPES);
const BDX_FREQUENCIES: [string, string][] = [
  ["monthly", "Monthly"], ["quarterly", "Quarterly"],
  ["semi-annual", "Semi-Annual"], ["annual", "Annual"],
];

// Programs carry only two operational states in the directory: Active and
// Inactive. Legacy/draft/empty values are treated as Active.
function isInactive(s?: string): boolean {
  return (s ?? "").toLowerCase() === "inactive";
}
function statusBadge(s?: string): string {
  return isInactive(s) ? "b-mut" : "b-ok";
}
function statusLabel(s?: string): string {
  return isInactive(s) ? "Inactive" : "Active";
}

export default function PartyDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const isAdmin = isTenantAdmin();
  const [p, setP] = useState<Party | null>(null);
  const [programs, setPrograms] = useState<Program[]>([]);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // Inline program editing: which program row is open, its editable draft copy,
  // and the save state. Saving PUTs to /programs/:id (the `program` table).
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Program | null>(null);
  const [savingProg, setSavingProg] = useState(false);
  const [progMsg, setProgMsg] = useState<string | null>(null);
  const [pipelineByProgram, setPipelineByProgram] = useState<Record<number, ProgramPipeline>>({});
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true); setErr(null); setP(null);
    api.get<Party>(`/parties/${id}`)
      .then(r => setP(r.data))
      .catch(e => setErr(e?.response?.data?.detail ?? "This party could not be found, or you don't have access to it."))
      .finally(() => setLoading(false));
    api.get<Program[]>(`/parties/${id}/programs`).then(r => setPrograms(r.data)).catch(() => {});
    // One call for every pipeline this party (as carrier) has, across all its
    // programs — cheaper than fetching per-program, and the active pipeline is
    // what /direct/run actually executes, so it's what "view setup" should open.
    api.get<ProgramPipeline[]>(`/pipelines`, { params: { mga: currentMga(), carrier_party_id: id } })
      .then(r => {
        const byProgram: Record<number, ProgramPipeline> = {};
        for (const pl of r.data) {
          if (pl.program_id == null) continue;
          if (!byProgram[pl.program_id] || pl.status === "active") byProgram[pl.program_id] = pl;
        }
        setPipelineByProgram(byProgram);
      })
      .catch(() => setPipelineByProgram({}));
  }, [id]);

  if (!p) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Party</h2></div></div>
        {loading ? null : (
          <div className="note warn" style={{ maxWidth: 560 }}>
            {err ?? "This party could not be found."}
          </div>
        )}
        <button className="btn" style={{ marginTop: 14 }} onClick={() => nav("/parties")}>← Directory</button>
      </div></div>
    );
  }
  const readOnly = p.scope === "global" || !isAdmin;

  function patch<K extends keyof Party>(k: K, v: Party[K]) {
    setP(prev => prev ? { ...prev, [k]: v } : prev);
  }

  async function save() {
    if (!p) return;
    setSaving(true); setMsg(null);
    try {
      const { data } = await api.put(`/parties/${id}`, {
        party_type: p.party_type, legal_name: p.legal_name, dba_name: p.dba_name,
        tax_id: p.tax_id, naics_code: p.naics_code,
        domicile_country: p.domicile_country, primary_jurisdiction: p.primary_jurisdiction,
        is_active: p.is_active, addresses: p.addresses, notes: p.notes, scope: p.scope,
      });
      setP(data); setMsg("Saved.");
    } catch (e: any) { setMsg(e?.message ?? "Save failed."); }
    finally { setSaving(false); }
  }
  // --- Program row editing -------------------------------------------------
  function startEditProgram(prog: Program) {
    setDraft({ ...prog });
    setEditingId(prog.id);
    setProgMsg(null);
  }
  function cancelEditProgram() {
    setEditingId(null); setDraft(null); setProgMsg(null);
  }
  function patchDraft<K extends keyof Program>(k: K, v: Program[K]) {
    setDraft(prev => prev ? { ...prev, [k]: v } : prev);
  }
  async function saveProgram() {
    if (!draft || !draft.name?.trim()) return;
    setSavingProg(true); setProgMsg(null);
    try {
      const { data } = await api.put<Program>(`/programs/${draft.id}`, {
        name: draft.name, party_id: draft.party_id ?? (p ? p.id : undefined),
        lead_carrier: draft.lead_carrier, admin_party: draft.admin_party,
        bdx_frequency: draft.bdx_frequency, business_segment: draft.business_segment,
        product_line: draft.product_line, distribution_channel: draft.distribution_channel,
        territory: draft.territory, status: draft.status,
      });
      // The PUT response omits `contracts`; merge so the row keeps its contract.
      setPrograms(prev => prev.map(pr => pr.id === draft.id ? { ...pr, ...data } : pr));
      setEditingId(null); setDraft(null); setProgMsg("Program saved");
    } catch (e: any) {
      setProgMsg(e?.response?.data?.detail ?? "Save failed.");
    } finally { setSavingProg(false); }
  }

  const isCarrier = p.party_type === "carrier";
  const domicile = p.primary_jurisdiction || p.domicile_country || "—";

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2 style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {p.legal_name}
              <span className={`badge ${isCarrier ? "b-info" : "b-mut"}`} style={{ verticalAlign: "middle" }}>
                <span className="d" />{TYPE_LABEL[p.party_type] ?? p.party_type}
              </span>
            </h2>
            <p className="mono">
              PTY-{String(p.id).padStart(4, "0")}
              {p.naics_code ? ` · NAIC ${p.naics_code}` : ""} 
            </p>
          </div>
          <div className="actions">
            {!isAdmin && (
              <span className="badge b-mut" style={{ alignSelf: "center" }}><span className="d" />View Only</span>
            )}
            <button className="btn" onClick={() => nav("/parties")}>← Directory</button>
            {/* Straight into this carrier's oversight dashboard, with the carrier
                already selected so the picker step is skipped. */}
            {isAdmin && (
              <button className="btn" onClick={() => nav(`/program-management?carrier=${p.id}`)}>
                Program Management →
              </button>
            )}
            {isAdmin && (
              <button className="btn pri" onClick={save} disabled={saving || readOnly}>
                Save Changes
              </button>
            )}
            {msg && <span className="muted" style={{ alignSelf: "center", fontSize: 13 }}>{msg}</span>}
          </div>
        </div>

        <div>
          {/* Identity */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Carrier Identity</h3>
            <div className="row2">
              <div className="field">
                <label>Legal name</label>
                <input value={p.legal_name} disabled={readOnly}
                  onChange={e => patch("legal_name", e.target.value)} />
              </div>
               <div className="field">
                <label>Type</label>
                <select value={p.party_type} disabled={readOnly}
                  onChange={e => patch("party_type", e.target.value)}>
                  {TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </div>
              {/* <div className="field">
                <label>DBA</label>
                <input value={p.dba_name ?? ""} disabled={readOnly}
                  onChange={e => patch("dba_name", e.target.value)} />
              </div> */}
            </div>
            <div className="row2">
              <div className="field" style={{ marginBottom: 0 }}>
                <label>State</label>
                <input value={p.primary_jurisdiction ?? ""} disabled={readOnly}
                  onChange={e => patch("primary_jurisdiction", e.target.value)} />
              </div>
            </div>
            {/* Same Activate/Deactivate the directory offers, as a form field —
                `save` already sends is_active, so it lands with Save changes. */}
            <label className="toggle"
              style={{ marginTop: 18, opacity: readOnly ? 0.6 : 1, cursor: readOnly ? "default" : "pointer" }}
              onClick={() => !readOnly && patch("is_active", !p.is_active)}>
              <span className={`sw2${p.is_active ? " on" : ""}`} />
              {p.is_active ? "Active" : "Inactive"}
            </label>
          </div>
        </div>

        {/* Programs & contracts */}
        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-h">
            <SlidersHorizontal className="ci" />
            <h3>Programs &amp; Contracts</h3>
            <span className="sub">
              Each program is validated against its current contract
              {isAdmin ? " · click a program to edit" : ""}
            </span>
            {isAdmin && (
              <div className="right" style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {progMsg && !editingId && (
                  <span className="muted" style={{ fontSize: 13 }}>{progMsg}</span>
                )}
                <button className="btn sm" onClick={() => nav(`/programs/new?party=${p.id}`)}>＋ Add Program</button>
              </div>
            )}
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Program</th><th>BDX frequency</th><th>Status</th><th>Actions</th></tr>
              </thead>
              <tbody>
                {programs.map((prog:any )=> {
                  const open = editingId === prog.id;
                  return (
                    <Fragment key={prog.id}>
                      <tr>
                        <td><b>{prog.name}</b></td>
                        <td>{toTitleCase(prog.bdx_frequency) || "—"}</td>
                        <td>
                          <span className={`badge ${statusBadge(prog.status)}`}>
                            <span className="d" />{statusLabel(prog.status)}
                          </span>
                        </td>
                        <td>
                          {isAdmin && pipelineByProgram[prog.id] && (
                            <>
                              <span className="linkish"
                                onClick={() => nav(`/direct/setups/${pipelineByProgram[prog.id].id}`)}>
                                View Bordereau Setup
                              </span>
                              {" · "}
                             
                            </>
                          )}
                          {isAdmin && (
                            <span className="linkish"
                              onClick={() => (open ? cancelEditProgram() : startEditProgram(prog))}>
                              {open ? "Close" : "Edit"}
                            </span>
                          )}
                        </td>
                      </tr>
                      {open && draft && (
                        <tr>
                          <td colSpan={4} style={{ background: "var(--p-surface-2)" }}>
                            <div className="grid g-3" style={{ gap: 14 }}>
                              <div className="field" style={{ marginBottom: 0 }}>
                                <label>Program name</label>
                                <input value={draft.name ?? ""} autoFocus
                                  onChange={e => patchDraft("name", e.target.value)} />
                              </div>
                              {/* <div className="field" style={{ marginBottom: 0 }}>
                                <label>Lead carrier</label>
                                <input value={draft.lead_carrier ?? ""} placeholder="Optional"
                                  onChange={e => patchDraft("lead_carrier", e.target.value)} />
                              </div>
                              <div className="field" style={{ marginBottom: 0 }}>
                                <label>Admin party</label>
                                <input value={draft.admin_party ?? ""} placeholder="Optional"
                                  onChange={e => patchDraft("admin_party", e.target.value)} />
                              </div> */}
                              <div className="field" style={{ marginBottom: 0 }}>
                                <label>BDX frequency</label>
                                <select value={draft.bdx_frequency ?? ""}
                                  onChange={e => patchDraft("bdx_frequency", e.target.value)}>
                                  <option value="">—</option>
                                  {BDX_FREQUENCIES.map(([v, l]) =>
                                    <option key={v} value={v}>{l}</option>)}
                                </select>
                              </div>
                              {/* <div className="field" style={{ marginBottom: 0 }}>
                                <label>Business segment</label>
                                <input value={draft.business_segment ?? ""} placeholder="Optional"
                                  onChange={e => patchDraft("business_segment", e.target.value)} />
                              </div>
                              <div className="field" style={{ marginBottom: 0 }}>
                                <label>Product line</label>
                                <input value={draft.product_line ?? ""} placeholder="Optional"
                                  onChange={e => patchDraft("product_line", e.target.value)} />
                              </div>
                              <div className="field" style={{ marginBottom: 0 }}>
                                <label>Distribution channel</label>
                                <input value={draft.distribution_channel ?? ""} placeholder="Optional"
                                  onChange={e => patchDraft("distribution_channel", e.target.value)} />
                              </div>
                              <div className="field" style={{ marginBottom: 0 }}>
                                <label>Territory</label>
                                <input value={draft.territory ?? ""} placeholder="Optional"
                                  onChange={e => patchDraft("territory", e.target.value)} />
                              </div> */}
                              <div className="field" style={{ marginBottom: 0 }}>
                                <label>Status</label>
                                <select value={draft.status === "inactive" ? "inactive" : "active"}
                                  onChange={e => patchDraft("status", e.target.value)}>
                                  <option value="active">Active</option>
                                  <option value="inactive">Inactive</option>
                                </select>
                              </div>
                            </div>
                            <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 10, marginTop: 14 }}>
                              {progMsg && (
                                <span className="muted" style={{ fontSize: 13 }}>{progMsg}</span>
                              )}
                              <button className="btn" onClick={cancelEditProgram} disabled={savingProg}>Cancel</button>
                              <button className="btn pri" onClick={saveProgram}
                                disabled={savingProg || !draft.name?.trim()}>
                                Save Program
                              </button>
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
            {programs.length === 0 && (
              <div className="empty">
                No programs yet.{isAdmin ? " Use “＋ Add Program” to create one." : ""}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
