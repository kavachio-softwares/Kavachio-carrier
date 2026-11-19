/**
 * The two commission terms a treaty may agree, as editors (27.2) — used on the
 * treaty's structure step, on its Calculation basis tab and on an endorsement.
 *
 * Their figures are each treaty's own: the wording varies from company to
 * company, so nothing here is assumed — the carrier enters what its treaty
 * says, and the server checks it (treaty_basis.clean_value).
 */
import { Plus, Trash2 } from "lucide-react";
import type { ProfitCommission, SlidingScale } from "../../api/treaty";
import { InfoTip } from "../InfoTip";

// What "Add" starts with: empty boxes, with an example in each placeholder.
// Nothing is assumed — the carrier types what its treaty says, and the server
// says what is still missing (treaty_basis.clean_value).
export const DEFAULT_SCALE: SlidingScale = {
  provisional_pct: null, method: "linear",
  points: [{ loss_ratio: null, rate: null }, { loss_ratio: null, rate: null }],
};
export const DEFAULT_PROFIT: ProfitCommission = { rate_pct: null, expense_pct: null, deficit_years: null };

/** A typed number, or null while the box is empty. */
const n = (v: string) => (v.trim() === "" ? null : Number(v));
const shown = (v: number | null | undefined) => (v == null ? "" : String(v));

/** A sliding scale in one line. */
export function scaleText(s: SlidingScale | null | undefined): string {
  if (!s?.points?.length) return "—";
  const pts = s.points.map(p => `${p.rate}% at ${p.loss_ratio}% LR`).join(" → ");
  return `Provisional ${s.provisional_pct}% · ${pts} (${s.method === "step" ? "stepped" : "linear"})`;
}

/** A profit commission in one line. */
export function profitText(p: ProfitCommission | null | undefined): string {
  if (!p) return "—";
  return `${p.rate_pct}% of profit · ${p.expense_pct}% expenses`
    + (p.deficit_years ? ` · deficit carried ${p.deficit_years} yr${p.deficit_years === 1 ? "" : "s"}` : "");
}

/** A field that shares a row: grows to fill it, wraps when it is narrow. */
const FIELD = { marginBottom: 0, flex: "1 1 170px", minWidth: 0, maxWidth: 240 } as const;

export function SlidingScaleEditor({ value, onChange, disabled = false }: {
  value: SlidingScale; onChange: (v: SlidingScale) => void; disabled?: boolean;
}) {
  return (
    <div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 8 }}>
        <div className="field" style={FIELD}>
          <label>Provisional commission (%)</label>
          <input inputMode="decimal" value={shown(value.provisional_pct)} disabled={disabled}
                 placeholder="e.g. 27.5"
                 onChange={e => onChange({ ...value, provisional_pct: n(e.target.value) })} />
        </div>
        <div className="field" style={FIELD}>
          <label>Between points</label>
          <select value={value.method} disabled={disabled}
                  onChange={e => onChange({ ...value, method: e.target.value as SlidingScale["method"] })}>
            <option value="linear">Slides in a straight line</option>
            <option value="step">Steps at each point</option>
          </select>
        </div>
      </div>
      {/* The scale as a small table: one row per point. */}
      <table className="panel-tbl" style={{ textAlign: "left", marginBottom: 6, maxWidth: 420 }}>
        <thead><tr><th>Loss ratio (%)</th><th>Commission (%)</th><th style={{ width: 36 }} /></tr></thead>
        <tbody>
          {value.points.map((p, i) => (
            <tr key={i}>
              <td><input style={{ width: "100%" }} inputMode="decimal" value={shown(p.loss_ratio)}
                         placeholder={i === 0 ? "e.g. 55" : "e.g. 65"}
                         disabled={disabled} aria-label="Loss ratio (%)"
                         onChange={e => onChange({ ...value, points: value.points.map((q, j) =>
                           j === i ? { ...q, loss_ratio: n(e.target.value) } : q) })} /></td>
              <td><input style={{ width: "100%" }} inputMode="decimal" value={shown(p.rate)}
                         placeholder={i === 0 ? "e.g. 35" : "e.g. 25"}
                         disabled={disabled} aria-label="Commission (%)"
                         onChange={e => onChange({ ...value, points: value.points.map((q, j) =>
                           j === i ? { ...q, rate: n(e.target.value) } : q) })} /></td>
              <td>
                {value.points.length > 2 && !disabled && (
                  <button className="btn sm ghost" type="button" aria-label="Remove this point"
                          onClick={() => onChange({ ...value, points: value.points.filter((_, j) => j !== i) })}>
                    <Trash2 size={12} /></button>)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!disabled && (
        <button className="btn sm" type="button"
                onClick={() => onChange({ ...value, points: [...value.points, { loss_ratio: null, rate: null }] })}>
          <Plus size={12} /> Add a point</button>
      )}
    </div>
  );
}

export function ProfitCommissionEditor({ value, onChange, disabled = false }: {
  value: ProfitCommission; onChange: (v: ProfitCommission) => void; disabled?: boolean;
}) {
  return (
    <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
      <div className="field" style={FIELD}>
        <label>Profit commission (% of profit)</label>
        <input inputMode="decimal" value={shown(value.rate_pct)} disabled={disabled}
               placeholder="e.g. 20"
               onChange={e => onChange({ ...value, rate_pct: n(e.target.value) })} />
      </div>
      <div className="field" style={FIELD}>
        <label>Reinsurers&rsquo; expenses (% of premium)</label>
        <input inputMode="decimal" value={shown(value.expense_pct)} disabled={disabled}
               placeholder="e.g. 5"
               onChange={e => onChange({ ...value, expense_pct: n(e.target.value) })} />
      </div>
      <div className="field" style={FIELD}>
        <label>Deficit carried forward (years)</label>
        <input inputMode="numeric" value={shown(value.deficit_years)} disabled={disabled}
               placeholder="e.g. 3"
               onChange={e => onChange({ ...value, deficit_years: n(e.target.value) })} />
      </div>
    </div>
  );
}

/** An optional commission term on a form: "Add" until it is agreed, then its
 *  editor and "Remove". */
// What each one is, in a few words — the ⓘ beside it explains it in full.
const SHORT: Record<"sliding" | "profit", string> = {
  sliding: "Commission that goes up or down with claims.",
  profit: "Extra commission when the treaty makes a profit.",
};

export function OptionalCommission({ kind, label, hint, value, onChange, disabled, error, bare }: {
  kind: "sliding" | "profit"; label: string; hint?: string | null;
  value: SlidingScale | ProfitCommission | null | undefined;
  onChange: (v: SlidingScale | ProfitCommission | null) => void;
  disabled?: boolean; error?: string | null;
  /** Inside a row that already names the term: no box, no second title. */
  bare?: boolean;
}) {
  const on = !!value && Object.keys(value).length > 0;
  const toggle = !disabled && (
    <button className="btn sm" type="button"
            onClick={() => onChange(on ? null : kind === "sliding" ? DEFAULT_SCALE : DEFAULT_PROFIT)}>
      {on ? <><Trash2 size={12} /> Remove</> : <><Plus size={12} /> Add</>}
    </button>
  );
  const editor = on && (kind === "sliding"
    ? <SlidingScaleEditor value={value as SlidingScale} onChange={onChange} disabled={disabled} />
    : <ProfitCommissionEditor value={value as ProfitCommission} onChange={onChange} disabled={disabled} />);
  if (bare) return (
    <div>
      {editor}
      {!on && <span className="sub" style={{ marginRight: 8 }}>Not agreed</span>}
      <div style={{ marginTop: on ? 8 : 0, display: "inline-block" }}>{toggle}</div>
      {error && <div className="hint" style={{ color: "var(--p-crit-ink)", marginTop: 6 }}>{error}</div>}
    </div>
  );
  return (
    <div className="card pad" style={{ boxShadow: "none", marginBottom: 12,
         borderColor: error ? "var(--p-crit)" : undefined }}>
      {/* One short line; the full explanation and example sit behind the ⓘ. */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: on ? 10 : 0 }}>
        <b style={{ fontSize: 13 }}>{label}</b>
        <span className="muted" style={{ fontWeight: 500, fontSize: 12.5 }}>— optional</span>
        {hint && <InfoTip text={hint} />}
        {!on && <span className="sub">{SHORT[kind]}</span>}
        {toggle && <span style={{ marginLeft: "auto" }}>{toggle}</span>}
      </div>
      {on && (kind === "sliding"
        ? <SlidingScaleEditor value={value as SlidingScale} onChange={onChange} disabled={disabled} />
        : <ProfitCommissionEditor value={value as ProfitCommission} onChange={onChange} disabled={disabled} />)}
      {error && <div className="hint" style={{ color: "var(--p-crit-ink)", marginTop: 6 }}>{error}</div>}
    </div>
  );
}
