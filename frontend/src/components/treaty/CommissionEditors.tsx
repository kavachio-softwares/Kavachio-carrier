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

export const DEFAULT_SCALE: SlidingScale = {
  provisional_pct: 30, method: "linear",
  points: [{ loss_ratio: 50, rate: 35 }, { loss_ratio: 70, rate: 25 }],
};
export const DEFAULT_PROFIT: ProfitCommission = { rate_pct: 20, expense_pct: 5, deficit_years: 0 };

const n = (v: string) => (v === "" ? 0 : Number(v));

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

export function SlidingScaleEditor({ value, onChange, disabled = false }: {
  value: SlidingScale; onChange: (v: SlidingScale) => void; disabled?: boolean;
}) {
  return (
    <div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end", marginBottom: 8 }}>
        <div className="field" style={{ marginBottom: 0, width: 200 }}>
          <label>Provisional commission (%)</label>
          <input inputMode="decimal" value={value.provisional_pct} disabled={disabled}
                 onChange={e => onChange({ ...value, provisional_pct: n(e.target.value) })} />
        </div>
        <div className="field" style={{ marginBottom: 0, width: 220 }}>
          <label>Between points</label>
          <select value={value.method} disabled={disabled}
                  onChange={e => onChange({ ...value, method: e.target.value as SlidingScale["method"] })}>
            <option value="linear">Slides in a straight line</option>
            <option value="step">Steps at each point</option>
          </select>
        </div>
      </div>
      <div className="sub" style={{ marginBottom: 4 }}>The scale — the commission at each loss ratio:</div>
      {value.points.map((p, i) => (
        <div key={i} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4, flexWrap: "wrap" }}>
          <span className="sub" style={{ width: 70 }}>Loss ratio</span>
          <input style={{ width: 90 }} inputMode="decimal" value={p.loss_ratio} disabled={disabled}
                 onChange={e => onChange({ ...value, points: value.points.map((q, j) =>
                   j === i ? { ...q, loss_ratio: n(e.target.value) } : q) })} />
          <span className="sub">% → commission</span>
          <input style={{ width: 90 }} inputMode="decimal" value={p.rate} disabled={disabled}
                 onChange={e => onChange({ ...value, points: value.points.map((q, j) =>
                   j === i ? { ...q, rate: n(e.target.value) } : q) })} />
          <span className="sub">%</span>
          {value.points.length > 2 && !disabled && (
            <button className="btn sm ghost" type="button" aria-label="Remove this point"
                    onClick={() => onChange({ ...value, points: value.points.filter((_, j) => j !== i) })}>
              <Trash2 size={12} /></button>)}
        </div>
      ))}
      {!disabled && (
        <button className="btn sm" type="button"
                onClick={() => onChange({ ...value, points: [...value.points, { loss_ratio: 0, rate: 0 }] })}>
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
      <div className="field" style={{ marginBottom: 0, width: 200 }}>
        <label>Profit commission (% of profit)</label>
        <input inputMode="decimal" value={value.rate_pct} disabled={disabled}
               onChange={e => onChange({ ...value, rate_pct: n(e.target.value) })} />
      </div>
      <div className="field" style={{ marginBottom: 0, width: 220 }}>
        <label>Reinsurers&rsquo; expenses (% of premium)</label>
        <input inputMode="decimal" value={value.expense_pct} disabled={disabled}
               onChange={e => onChange({ ...value, expense_pct: n(e.target.value) })} />
      </div>
      <div className="field" style={{ marginBottom: 0, width: 220 }}>
        <label>Deficit carried forward (years)</label>
        <input inputMode="numeric" value={value.deficit_years} disabled={disabled}
               onChange={e => onChange({ ...value, deficit_years: n(e.target.value) })} />
      </div>
    </div>
  );
}

/** An optional commission term on a form: "Add" until it is agreed, then its
 *  editor and "Remove". */
export function OptionalCommission({ kind, label, hint, value, onChange, disabled, error }: {
  kind: "sliding" | "profit"; label: string; hint?: string | null;
  value: SlidingScale | ProfitCommission | null | undefined;
  onChange: (v: SlidingScale | ProfitCommission | null) => void;
  disabled?: boolean; error?: string | null;
}) {
  const on = !!value && Object.keys(value).length > 0;
  return (
    <div className="card pad" style={{ boxShadow: "none", marginBottom: 12,
         borderColor: error ? "var(--p-crit)" : undefined }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: on ? 10 : 0 }}>
        <b style={{ fontSize: 13 }}>{label}</b>
        <span className="muted" style={{ fontWeight: 500, fontSize: 12.5 }}>— optional</span>
        {!on && hint && <span className="sub" style={{ flex: 1 }}>{hint}</span>}
        {!disabled && (
          <button className="btn sm" type="button" style={{ marginLeft: "auto" }}
                  onClick={() => onChange(on ? null : kind === "sliding" ? DEFAULT_SCALE : DEFAULT_PROFIT)}>
            {on ? <><Trash2 size={12} /> Remove</> : <><Plus size={12} /> Add</>}
          </button>
        )}
      </div>
      {on && (kind === "sliding"
        ? <SlidingScaleEditor value={value as SlidingScale} onChange={onChange} disabled={disabled} />
        : <ProfitCommissionEditor value={value as ProfitCommission} onChange={onChange} disabled={disabled} />)}
      {error && <div className="hint" style={{ color: "var(--p-crit-ink)", marginTop: 6 }}>{error}</div>}
    </div>
  );
}
