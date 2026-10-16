/**
 * Write a treaty's structure — Phase 26.1 and 26.2 in one form.
 *
 *   1  the treaty type (five cards)
 *   2  the numbers that type is defined by
 *   3  its layers — one for every type but XOL — and on each layer, the panel:
 *      which reinsurers hold what share, and who leads
 *   4  the programmes it protects (optional — the create flow and the record
 *      pass them; nothing else does)
 *
 * Built entirely from the server's spec (GET /treaty-types): this file knows
 * no field names. A field the spec marks `when` appears only while the field
 * it depends on has the right value — stop loss is a loss ratio OR an amount.
 *
 * The tower and the headline on the right are the SERVER's reading of what is
 * typed (POST /treaty/preview), so they are the same picture the record and the
 * reinsurer will see, not this screen's guess at it.
 *
 * Reinsurers are parties the carrier keeps; none of them has a seat in
 * Kavachio. Each share is the carrier's record of a signed line.
 */
import { useEffect, useMemo, useState } from "react";
import { Plus, Star, Trash2 } from "lucide-react";
import { InfoTip } from "../InfoTip";
import {
  previewTreaty,
  type TreatyField, type TreatyInput, type TreatyLayer, type TreatyShare,
  type TreatySummary, type TreatyTypeKey, type TreatyTypeSpec,
} from "../../api/treaty";
import { PlaceBar, TreatyTower } from "./TreatyTower";
import { AddReinsurerModal } from "./AddReinsurerModal";

export type Reinsurer = { id: number; name: string };

export const EMPTY_TREATY: TreatyInput = { treaty_type: null, terms: {}, layers: [] };

/** Terms every type shares — carried across when the type changes, so
 *  switching from quota share to surplus does not wipe the currency. */
const COMMON = ["currency", "basis", "reinsurance_broker", "placement_reference"];

const num = (v: unknown) => {
  const n = Number(String(v ?? "").replace(/,/g, ""));
  return Number.isFinite(n) ? n : 0;
};

export function placedOf(layer: TreatyLayer): number {
  return Math.round(layer.shares.reduce((a, s) => a + num(s.share_pct), 0) * 10000) / 10000;
}

export function TreatyEditor({
  value, onChange, types, reinsurers, leadId, errors, showErrors, disabled,
  programmes, onReinsurerAdded,
}: {
  value: TreatyInput;
  onChange: (v: TreatyInput) => void;
  types: TreatyTypeSpec[] | null;
  reinsurers: Reinsurer[];
  /** The contract's counterparty — the lead reinsurer. Put on an empty panel
   *  as lead, since that is who the treaty is with. */
  leadId?: number | null;
  /** The server's objections from a save, keyed by dotted path. */
  errors?: Record<string, string>;
  /** Show what a save would refuse, before anyone presses save. */
  showErrors?: boolean;
  disabled?: boolean;
  /** Offered as "Add a reinsurer" beside each panel: the new one is added to
   *  the caller's list and put on the layer it was asked from. */
  onReinsurerAdded?: (r: Reinsurer) => void;
  programmes?: {
    all: { id: number; name: string }[];
    selected: number[];
    homeId: number | null;
    onChange: (ids: number[]) => void;
    disabled?: boolean;
  } | null;
}) {
  const spec = useMemo(
    () => (types ?? []).find(t => t.key === value.treaty_type) ?? null,
    [types, value.treaty_type]);

  // The server's reading of the structure as typed.
  const [summary, setSummary] = useState<TreatySummary | null>(null);
  const [liveErrors, setLiveErrors] = useState<Record<string, string>>({});
  // Which layer asked for a new reinsurer, while the dialog is open.
  const [addingFor, setAddingFor] = useState<number | null>(null);
  useEffect(() => {
    if (!value.treaty_type) { setSummary(null); setLiveErrors({}); return; }
    const t = setTimeout(() => {
      previewTreaty(value)
        .then(r => { setSummary(r.summary); setLiveErrors(r.errors); })
        .catch(() => {});
    }, 300);
    return () => clearTimeout(t);
  }, [value]);

  const err = (path: string) =>
    errors?.[path] ?? (showErrors ? liveErrors[path] : undefined);

  // An empty panel starts with the lead reinsurer on it — the treaty is WITH
  // them, so they are on it. Only ever fills an empty first layer, so it
  // cannot fight anything the carrier typed.
  useEffect(() => {
    if (!leadId || !spec || disabled) return;
    const first = value.layers[0];
    if (value.layers.length && first.shares.length) return;
    const layers = value.layers.length ? value.layers
      : [{ name: spec.default_layer, terms: {}, shares: [] }];
    onChange({ ...value, layers: [
      { ...layers[0], shares: [{ reinsurer_party_id: leadId, share_pct: "",
                                  is_lead: true }] },
      ...layers.slice(1)] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [leadId, spec?.key]);

  function pickType(key: TreatyTypeKey) {
    if (disabled || key === value.treaty_type) return;
    const next = (types ?? []).find(t => t.key === key);
    if (!next) return;
    const kept = Object.fromEntries(
      Object.entries(value.terms).filter(([k]) => COMMON.includes(k)));
    // The panel usually survives a change of type — the same markets — so the
    // first layer's shares come across and its type-specific numbers do not.
    const panel = value.layers[0]?.shares ?? [];
    onChange({
      ...value, treaty_type: key, terms: kept,
      layers: [{ name: next.default_layer, terms: {}, shares: panel }],
    });
  }

  const setTerm = (name: string, v: unknown) =>
    onChange({ ...value, terms: { ...value.terms, [name]: v } });

  const setLayer = (i: number, patch: Partial<TreatyLayer>) =>
    onChange({ ...value, layers: value.layers.map(
      (l, j) => j === i ? { ...l, ...patch } : l) });

  const setShare = (i: number, j: number, patch: Partial<TreatyShare>) => {
    const lay = value.layers[i];
    let shares = lay.shares.map((s, k) => k === j ? { ...s, ...patch } : s);
    // One lead per layer: making somebody lead stands the old one down.
    if (patch.is_lead) shares = shares.map((s, k) => ({ ...s, is_lead: k === j }));
    setLayer(i, { shares });
  };

  function addLayer() {
    if (!spec) return;
    const last = value.layers[value.layers.length - 1];
    const top = last ? num(last.terms.retention) + num(last.terms.limit) : 0;
    onChange({ ...value, layers: [...value.layers, {
      name: `Layer ${value.layers.length + 1}`,
      // A new layer usually attaches where the one below it stops.
      terms: top ? { retention: top } : {},
      shares: [],
    }] });
  }

  const layers = value.layers.length || !spec ? value.layers
    : [{ name: spec.default_layer, terms: {}, shares: [] }];

  function field(f: TreatyField, val: unknown, set: (v: unknown) => void,
                 path: string) {
    const bad = err(path);
    const label = (
      <label>
        {f.label}
        {f.required
          ? <span style={{ color: "var(--p-crit-ink)" }}>*</span>
          : <span className="muted" style={{ fontWeight: 500 }}> — optional</span>}
        {f.hint && <InfoTip text={f.hint} />}
      </label>
    );
    const style = bad ? { borderColor: "var(--p-crit)" } : undefined;
    return (
      <div className="field" key={path} style={{ marginBottom: 0 }}>
        {label}
        {f.kind === "select" ? (
          <select value={String(val ?? "")} disabled={disabled} style={style}
                  onChange={e => set(e.target.value || null)}>
            <option value="">Choose…</option>
            {(f.options ?? []).map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        ) : (
          <div style={{ position: "relative" }}>
            <input
              type={f.kind === "text" ? "text" : "number"}
              step={f.kind === "int" ? 1 : "any"} min={f.kind === "text" ? undefined : 0}
              inputMode={f.kind === "text" ? undefined : "decimal"}
              placeholder={f.example ?? undefined}
              value={String(val ?? "")} disabled={disabled} style={{
                ...style,
                paddingRight: f.kind === "pct" ? 30
                  : f.kind === "money" && !!value.terms.currency ? 52 : undefined }}
              onChange={e => set(e.target.value)}
            />
            {(f.kind === "pct" || (f.kind === "money" && !!value.terms.currency)) && (
              <span className="sub" style={{ position: "absolute", right: 11,
                    top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
                {f.kind === "pct" ? "%" : String(value.terms.currency).toUpperCase()}
              </span>
            )}
          </div>
        )}
        {bad && <div className="hint" style={{ color: "var(--p-crit-ink)" }}>{bad}</div>}
      </div>
    );
  }

  const shows = (f: TreatyField) =>
    !f.when || value.terms[f.when.field] === f.when.equals;

  const nameOf = (id: number | null) =>
    reinsurers.find(r => r.id === id)?.name ?? "—";

  return (
    <div>
      {/* 1 · the type */}
      <div className="ttype" role="radiogroup" aria-label="Treaty type"
           style={{ marginBottom: 6 }}>
        {(types ?? []).map(t => (
          <button key={t.key} type="button" role="radio"
                  aria-checked={value.treaty_type === t.key}
                  className={value.treaty_type === t.key ? "on" : ""}
                  disabled={disabled && value.treaty_type !== t.key}
                  onClick={() => pickType(t.key)}>
            <b>{t.label}</b><span>{t.blurb}</span>
          </button>
        ))}
      </div>
      {err("treaty_type") && (
        <div className="hint" style={{ color: "var(--p-crit-ink)", fontSize: 12 }}>
          Pick a treaty type.
        </div>
      )}

      {spec && (
        <div className="grid tedit" style={{ marginTop: 18 }}>
          <div>
            {/* 2 · the treaty's own numbers */}
            <div className="fh" style={{ marginTop: 0 }}>
              {spec.label} terms
              <InfoTip text="What this kind of treaty is defined by. Required ones are starred." />
            </div>
            <div className="grid g-3" style={{ gap: 14, marginBottom: 18 }}>
              {spec.fields.filter(f => f.required).map(f =>
                field(f, value.terms[f.name], v => setTerm(f.name, v),
                      `terms.${f.name}`))}
              {spec.fields.filter(f => !f.required).map(f =>
                field(f, value.terms[f.name], v => setTerm(f.name, v),
                      `terms.${f.name}`))}
            </div>

            {/* 3 · layers and their panels */}
            <div className="fh">
              {spec.multi_layer ? "Layers and who holds them" : "Who holds it"}
              <InfoTip text={"Each reinsurer's share is a percentage of the layer. "
                + "A layer is placed when its shares reach 100%. Reinsurers are "
                + "not on Kavachio, so these are your record of their signed lines."} />
            </div>
            {err("layers") && <div className="note warn" style={{ marginBottom: 10 }}>{err("layers")}</div>}
            {layers.map((lay, i) => {
              const placed = placedOf(lay);
              const taken = new Set(lay.shares.map(s => s.reinsurer_party_id));
              const free = reinsurers.filter(r => !taken.has(r.id));
              return (
                <div className="tlayer" key={i}>
                  <div className="tlayer-h">
                    {spec.multi_layer ? (
                      <input aria-label="Layer name" value={lay.name}
                             disabled={disabled}
                             onChange={e => setLayer(i, { name: e.target.value })} />
                    ) : <b style={{ fontSize: 13.5 }}>{lay.name}</b>}
                    <span style={{ marginLeft: "auto" }}><PlaceBar pct={placed} /></span>
                    {spec.multi_layer && layers.length > 1 && !disabled && (
                      <button type="button" className="btn sm ghost"
                              aria-label={`Remove ${lay.name}`}
                              onClick={() => onChange({ ...value,
                                layers: value.layers.filter((_, j) => j !== i) })}>
                        <Trash2 size={13} />
                      </button>
                    )}
                  </div>

                  {spec.layer_fields.some(shows) && (
                    <div className="grid g-3" style={{ gap: 12, marginBottom: 14 }}>
                      {spec.layer_fields.filter(shows).map(f =>
                        field(f, lay.terms[f.name],
                              v => setLayer(i, { terms: { ...lay.terms, [f.name]: v } }),
                              `layers.${i}.${f.name}`))}
                    </div>
                  )}

                  <div className="tbl-wrap">
                    <table className="panel-tbl">
                      <thead>
                        <tr>
                          <th>Reinsurer</th>
                          <th style={{ width: 96 }}>Share</th>
                          <th style={{ width: 54 }}>Lead</th>
                          <th style={{ width: "26%" }}>Slip reference</th>
                          <th style={{ width: 42 }} />
                        </tr>
                      </thead>
                      <tbody>
                        {lay.shares.length === 0 && (
                          <tr><td colSpan={5} className="sub">
                            Nobody on this layer yet.
                          </td></tr>
                        )}
                        {lay.shares.map((sh, j) => {
                          const pErr = err(`layers.${i}.shares.${j}.reinsurer_party_id`);
                          const sErr = err(`layers.${i}.shares.${j}.share_pct`);
                          return (
                            <tr key={j}>
                              <td>
                                <select value={sh.reinsurer_party_id ?? ""}
                                        className={pErr ? "bad" : ""}
                                        disabled={disabled}
                                        aria-label="Reinsurer"
                                        onChange={e => setShare(i, j, {
                                          reinsurer_party_id: e.target.value
                                            ? Number(e.target.value) : null })}>
                                  <option value="">Choose a reinsurer…</option>
                                  {sh.reinsurer_party_id != null
                                    && !free.some(r => r.id === sh.reinsurer_party_id) && (
                                    <option value={sh.reinsurer_party_id}>
                                      {sh.reinsurer_name ?? nameOf(sh.reinsurer_party_id)}
                                    </option>
                                  )}
                                  {free.map(r => (
                                    <option key={r.id} value={r.id}>{r.name}</option>
                                  ))}
                                </select>
                                {pErr && <div className="sub" style={{ color: "var(--p-crit-ink)" }}>{pErr}</div>}
                              </td>
                              <td>
                                <div style={{ position: "relative" }}>
                                  <input type="number" min={0} max={100} step="any"
                                         aria-label="Share percent"
                                         className={sErr ? "bad" : ""}
                                         value={String(sh.share_pct ?? "")}
                                         disabled={disabled}
                                         style={{ paddingRight: 24 }}
                                         onChange={e => setShare(i, j, { share_pct: e.target.value })} />
                                  <span className="sub" style={{ position: "absolute", right: 8,
                                        top: "50%", transform: "translateY(-50%)" }}>%</span>
                                </div>
                                {sErr && <div className="sub" style={{ color: "var(--p-crit-ink)" }}>{sErr}</div>}
                              </td>
                              <td>
                                <button type="button"
                                        className={`btn sm ${sh.is_lead ? "" : "ghost"}`}
                                        aria-pressed={!!sh.is_lead}
                                        title={sh.is_lead ? "Leads this layer" : "Make lead"}
                                        disabled={disabled}
                                        onClick={() => setShare(i, j, { is_lead: !sh.is_lead })}>
                                  <Star size={13} fill={sh.is_lead ? "currentColor" : "none"} />
                                </button>
                              </td>
                              <td>
                                <input value={sh.slip_reference ?? ""} disabled={disabled}
                                       aria-label="Slip reference"
                                       placeholder="Optional"
                                       onChange={e => setShare(i, j, { slip_reference: e.target.value })} />
                              </td>
                              <td>
                                {!disabled && (
                                  <button type="button" className="btn sm ghost"
                                          aria-label="Remove this reinsurer"
                                          onClick={() => setLayer(i, {
                                            shares: lay.shares.filter((_, k) => k !== j) })}>
                                    <Trash2 size={13} />
                                  </button>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {err(`layers.${i}.shares`) && (
                    <div className="sub" style={{ color: "var(--p-crit-ink)", marginTop: 6 }}>
                      {err(`layers.${i}.shares`)}
                    </div>
                  )}
                  {!disabled && (
                    <div className="rowacts">
                      <button type="button" className="btn sm" disabled={!free.length}
                              onClick={() => setLayer(i, { shares: [...lay.shares,
                                { reinsurer_party_id: null, share_pct: "",
                                  is_lead: lay.shares.length === 0 }] })}>
                        <Plus size={13} /> Add a reinsurer
                      </button>
                      {i > 0 && lay.shares.length === 0 && layers[i - 1].shares.length > 0 && (
                        <button type="button" className="btn sm ghost"
                                onClick={() => setLayer(i, { shares: layers[i - 1].shares
                                  .map(({ id: _id, ...s }) => ({ ...s })) })}>
                          Same panel as {layers[i - 1].name}
                        </button>
                      )}
                      {placed < 99.995 && lay.shares.length > 0 && (
                        <span className="sub">{+(100 - placed).toFixed(4)}% still unplaced</span>
                      )}
                      {onReinsurerAdded ? (
                        <button type="button" className="btn sm ghost"
                                onClick={() => setAddingFor(i)}>
                          Not listed? Add a reinsurer
                        </button>
                      ) : !reinsurers.length && (
                        <span className="sub">
                          No reinsurers yet — add one under Carriers &amp; Reinsurers.
                        </span>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            {spec.multi_layer && !disabled && (
              <button type="button" className="btn sm" onClick={addLayer}>
                <Plus size={13} /> Add a layer
              </button>
            )}

            {/* 4 · programmes (26.2) */}
            {programmes && (
              <>
                <div className="fh" style={{ marginTop: 22 }}>
                  Programmes it protects
                  <InfoTip text={"The programme it is raised on is always covered. "
                    + "A quota share across your book usually protects several. "
                    + "This can change while the treaty runs."} />
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {programmes.all.map(p => {
                    const home = p.id === programmes.homeId;
                    const on = home || programmes.selected.includes(p.id);
                    return (
                      <label key={p.id} className={`badge ${on ? "b-ok" : ""}`}
                             style={{ cursor: home || programmes.disabled ? "default" : "pointer",
                                      border: "1px solid var(--p-border-2)",
                                      padding: "6px 11px", fontSize: 12.5,
                                      background: on ? undefined : "var(--p-surface)" }}>
                        <input type="checkbox" checked={on}
                               disabled={home || programmes.disabled}
                               style={{ margin: 0 }}
                               onChange={e => programmes.onChange(e.target.checked
                                 ? [...programmes.selected, p.id]
                                 : programmes.selected.filter(x => x !== p.id))} />
                        {p.name}{home && <span className="sub"> · raised on</span>}
                      </label>
                    );
                  })}
                  {!programmes.all.length && <span className="sub">No programmes yet.</span>}
                </div>
              </>
            )}
          </div>

          {/* The server's reading of it, live. */}
          <div className="card pad" style={{ position: "sticky", top: 12 }}>
            <div className="sub" style={{ marginBottom: 4 }}>How it reads</div>
            <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 12 }}>
              {summary?.headline ?? spec.label}
            </div>
            {summary ? <TreatyTower summary={summary} /> : (
              <div className="sub">Fill in the terms to see it drawn.</div>
            )}
            {!!summary?.warnings?.length && (
              <div className="note warn" style={{ marginTop: 12 }}>
                {summary.warnings.map((w, k) => <div key={k}>{w}</div>)}
              </div>
            )}
            {!!summary?.facts.length && (
              <div style={{ marginTop: 12 }}>
                {summary.facts.slice(0, 6).map(f => (
                  <div className="kv" key={f.label}>
                    <span className="k">{f.label}</span><span className="v">{f.value}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      {onReinsurerAdded && (
        <AddReinsurerModal
          open={addingFor !== null}
          onClose={() => setAddingFor(null)}
          onCreated={r => {
            onReinsurerAdded(r);
            const i = addingFor ?? 0;
            const lay = value.layers[i];
            if (lay) {
              setLayer(i, { shares: [...lay.shares.filter(x => x.reinsurer_party_id != null),
                { reinsurer_party_id: r.id, reinsurer_name: r.name, share_pct: "",
                  is_lead: lay.shares.length === 0 }] });
            }
          }} />
      )}
    </div>
  );
}

/** What a save would refuse, as one sentence for a banner. */
export function firstTreatyError(errors: Record<string, string>): string | null {
  const k = Object.keys(errors)[0];
  return k ? errors[k] : null;
}

/** The structure, shaped for the API: blanks left out and the read-only
 *  extras taken off. A share row nobody filled in is SENT, not dropped — the
 *  server's "pick a reinsurer" then lands on the row it is about, which it
 *  could not if the rows below it had moved up one. */
export function treatyForSave(v: TreatyInput): TreatyInput {
  return {
    treaty_type: v.treaty_type,
    terms: Object.fromEntries(Object.entries(v.terms)
      .filter(([, x]) => x !== "" && x !== null && x !== undefined)),
    layers: v.layers.map(l => ({
      name: l.name, terms: Object.fromEntries(Object.entries(l.terms)
        .filter(([, x]) => x !== "" && x !== null && x !== undefined)),
      shares: l.shares
        .map(s => ({ reinsurer_party_id: s.reinsurer_party_id,
                     share_pct: s.share_pct, is_lead: !!s.is_lead,
                     slip_reference: s.slip_reference || null })),
    })),
  };
}
