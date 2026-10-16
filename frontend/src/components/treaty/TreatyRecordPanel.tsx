/**
 * The "Treaty structure" tab on a reinsurance contract's record.
 *
 * Read first: the headline, the tower, the facts, and each layer's panel —
 * who holds what, who leads. Editable while the terms still move (the server
 * says when, in `can_edit`); after that only the programmes it protects can
 * change, because those are a placement decision rather than a signed term.
 *
 * Saving rewrites the treaty clauses in a wording written in Kavachio, in the
 * same request — so `onSaved` asks the page to re-read the record.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Layers, PenLine, Star } from "lucide-react";
import { InfoTip } from "../InfoTip";
import { getCounterparties, fieldErrors } from "../../api/contractRecord";
import { getHierarchy } from "../../api/hierarchy";
import {
  getTreaty, getTreatyTypes, saveTreaty, saveTreatyProgrammes,
  type TreatyInput, type TreatyRecord, type TreatyTypeSpec,
} from "../../api/treaty";
import { EMPTY_TREATY, TreatyEditor, treatyForSave } from "./TreatyEditor";
import { PlaceBar, TreatyTower } from "./TreatyTower";

export function TreatyRecordPanel({ contractId, onSaved }: {
  contractId: number;
  onSaved?: () => void;
}) {
  const [rec, setRec] = useState<TreatyRecord | null>(null);
  const [types, setTypes] = useState<TreatyTypeSpec[] | null>(null);
  const [reinsurers, setReinsurers] = useState<{ id: number; name: string }[]>([]);
  const [allProgs, setAllProgs] = useState<{ id: number; name: string }[]>([]);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<TreatyInput>(EMPTY_TREATY);
  const [progs, setProgs] = useState<number[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [editingProgs, setEditingProgs] = useState(false);

  useEffect(() => {
    getTreaty(contractId).then(setRec).catch(e => setErr(fieldErrors(e).message));
    getTreatyTypes().then(setTypes).catch(() => setTypes([]));
    getCounterparties("reinsurer").then(l => setReinsurers(
      l.map(c => ({ id: c.id, name: c.name })))).catch(() => {});
    getHierarchy().then(h => setAllProgs(
      h.programmes.map(p => ({ id: p.id, name: p.name })))).catch(() => {});
  }, [contractId]);

  function startEdit() {
    if (!rec) return;
    setDraft(rec.structure
      ? { treaty_type: rec.structure.treaty_type, terms: rec.structure.terms,
          layers: rec.structure.layers }
      : EMPTY_TREATY);
    setProgs(rec.programmes.filter(p => !p.is_home).map(p => p.id));
    setErrors({}); setErr(""); setNote("");
    setEditing(true);
  }

  async function save() {
    setBusy(true); setErr(""); setErrors({});
    try {
      const r = await saveTreaty(contractId, { ...treatyForSave(draft),
                                               program_ids: progs });
      setRec(r);
      setEditing(false);
      setNote("Saved. The treaty clauses in the wording were rewritten to match.");
      onSaved?.();
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(fe.message);
      setErrors(fe.errors as Record<string, string>);
    } finally {
      setBusy(false);
    }
  }

  async function saveProgs() {
    setBusy(true); setErr("");
    try {
      const r = await saveTreatyProgrammes(contractId, progs);
      setRec(r);
      setEditingProgs(false);
      setNote("The programmes it protects were updated.");
      onSaved?.();
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  if (!rec) {
    return <div className="card pad">{err ? <div className="note warn">{err}</div>
      : <div className="sub">Loading the treaty…</div>}</div>;
  }

  const homeId = rec.programmes.find(p => p.is_home)?.id ?? null;

  if (editing) {
    return (
      <div className="card pad">
        <div className="card-h" style={{ padding: 0, marginBottom: 14, border: 0 }}>
          <PenLine size={16} className="ci" />
          <h3>Edit the treaty structure</h3>
        </div>
        {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}
        <TreatyEditor
          value={draft} onChange={v => { setDraft(v); setErrors({}); }}
          types={types} reinsurers={reinsurers} leadId={rec.lead_reinsurer_id}
          onReinsurerAdded={r => setReinsurers(list => [...list, r])}
          errors={errors} showErrors
          programmes={{ all: allProgs, selected: progs, homeId, onChange: setProgs }}
        />
        <div className="rowacts" style={{ marginTop: 16 }}>
          <button className="btn pri" type="button" disabled={busy || !draft.treaty_type}
                  onClick={save}>
            {busy ? "Saving…" : "Save the structure"}
          </button>
          <button className="btn" type="button" disabled={busy}
                  onClick={() => setEditing(false)}>Cancel</button>
          <span className="sub">Saving rewrites the treaty clauses in the wording.</span>
        </div>
      </div>
    );
  }

  const st = rec.structure;
  const summ = rec.summary;

  return (
    <div>
      {note && <div className="note ok" style={{ marginBottom: 12 }}>{note}</div>}
      {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-h">
          <Layers size={16} className="ci" />
          <h3>{summ?.headline ?? "Treaty structure"}</h3>
          {summ && (
            <span className={`badge ${summ.fully_placed ? "b-ok" : "b-warn"}`}>
              <span className="d" />
              {summ.fully_placed ? "Fully placed" : `${summ.placed_pct}% placed`}
            </span>
          )}
          <span className="right">
            {rec.can_edit && (
              <button className="btn sm" type="button" onClick={startEdit}>
                <PenLine size={12} /> {st ? "Edit" : "Set up the structure"}
              </button>
            )}
          </span>
        </div>
        <div style={{ padding: "16px 20px" }}>
          {!st ? (
            <div className="empty" style={{ padding: 20 }}>
              No structure yet. {rec.can_edit
                ? "Set out the treaty type, its layers and who holds them."
                : rec.edit_blocked_reason}
            </div>
          ) : (
            <div className="grid g-2" style={{ alignItems: "start" }}>
              <div>{summ && <TreatyTower summary={summ} />}</div>
              <div className="facts">
                <div className="kv"><span className="k">Type</span>
                  <span className="v">{summ?.type_label}</span></div>
                {(summ?.facts ?? []).map(f => (
                  <div className="kv" key={f.label}>
                    <span className="k">{f.label}</span><span className="v">{f.value}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {!!summ?.warnings?.length && (
            <div className="note warn" style={{ marginTop: 14 }}>
              {summ.warnings.map((w, i) => <div key={i}>{w}</div>)}
            </div>
          )}
          {!rec.can_edit && st && rec.edit_blocked_reason && (
            <div className="sub" style={{ marginTop: 12 }}>{rec.edit_blocked_reason}</div>
          )}
        </div>
      </div>

      {st && st.layers.map((lay, i) => (
        <div className="card" key={lay.id ?? i} style={{ marginBottom: 14 }}>
          <div className="card-h">
            <h3>{lay.name}</h3>
            <span className="sub">
              {(lay.facts ?? []).map(f => `${f.label} ${f.value}`).join(" · ")}
            </span>
            <span className="right"><PlaceBar pct={lay.placed_pct ?? 0} /></span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Reinsurer</th><th>Share of the layer</th><th>Slip reference</th></tr>
              </thead>
              <tbody>
                {lay.shares.length === 0 && (
                  <tr><td colSpan={3} className="sub">Nobody on this layer yet.</td></tr>
                )}
                {lay.shares.map(sh => (
                  <tr key={sh.id ?? sh.reinsurer_party_id}>
                    <td>
                      <b>{sh.reinsurer_name ?? "—"}</b>
                      {sh.is_lead && (
                        <span className="badge b-ok" style={{ marginLeft: 8 }}>
                          <Star size={11} fill="currentColor" /> Lead
                        </span>
                      )}
                    </td>
                    <td className="mono">{Number(sh.share_pct)}%</td>
                    <td className="mono">{sh.slip_reference || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      {/* 26.2 — the programmes it protects */}
      <div className="card">
        <div className="card-h">
          <h3>Programmes it protects</h3>
          <InfoTip text={"The programme it was raised on is always protected. "
            + "Others can be added while the treaty runs — the signed wording "
            + "does not list them, so this does not change what was signed."} />
          <span className="right">
            {rec.can_edit_programmes && !editingProgs && (
              <button className="btn sm" type="button" onClick={() => {
                setProgs(rec.programmes.filter(p => !p.is_home).map(p => p.id));
                setEditingProgs(true);
              }}>
                <PenLine size={12} /> Change
              </button>
            )}
          </span>
        </div>
        <div style={{ padding: "14px 20px" }}>
          {editingProgs ? (
            <>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {allProgs.map(p => {
                  const home = p.id === homeId;
                  const on = home || progs.includes(p.id);
                  return (
                    <label key={p.id} className={`badge ${on ? "b-ok" : ""}`}
                           style={{ border: "1px solid var(--p-border-2)",
                                    padding: "6px 11px", fontSize: 12.5,
                                    cursor: home ? "default" : "pointer",
                                    background: on ? undefined : "var(--p-surface)" }}>
                      <input type="checkbox" checked={on} disabled={home}
                             style={{ margin: 0 }}
                             onChange={e => setProgs(e.target.checked
                               ? [...progs, p.id] : progs.filter(x => x !== p.id))} />
                      {p.name}{home && <span className="sub"> · raised on</span>}
                    </label>
                  );
                })}
              </div>
              <div className="rowacts" style={{ marginTop: 12 }}>
                <button className="btn pri sm" type="button" disabled={busy}
                        onClick={saveProgs}>{busy ? "Saving…" : "Save"}</button>
                <button className="btn sm" type="button"
                        onClick={() => setEditingProgs(false)}>Cancel</button>
              </div>
            </>
          ) : (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {rec.programmes.map(p => (
                <Link key={p.id} to={`/programs/${p.id}/setup?stage=1`}
                      className="badge b-mut" style={{ textDecoration: "none" }}>
                  <span className="d" />{p.name}
                  {p.is_home && <span className="sub"> · raised on</span>}
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
