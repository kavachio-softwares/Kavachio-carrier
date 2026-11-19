// Small pieces the Change History and Rule History tabs share (tasks 48.1,
// 48.2): the date range, a labelled dropdown, the filter bar, the empty and
// loading states, and the before / after table. Kept apart from
// AuditHistoryTabs.tsx so each tab can change without touching the others.
import { useState, type ReactNode } from "react";
import { Search } from "lucide-react";
import type { FieldChange } from "../api/changeHistory";

export const L: React.CSSProperties = { textAlign: "left" };

const localDay = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const back = (months: number) => { const d = new Date(); d.setMonth(d.getMonth() - months); return localDay(d); };
const RANGES = [{ k: "1", label: "Last month" }, { k: "3", label: "Last 3 months" }, { k: "6", label: "Last 6 months" },
                { k: "12", label: "Last 12 months" }, { k: "all", label: "All time" }, { k: "custom", label: "Pick dates…" }];

/** The date range every tab offers, as local days. */
export function useRange(start = "3") {
  const [k, setK] = useState(start);
  const [from, setFrom] = useState(back(Number(start) || 3));
  const [to, setTo] = useState(localDay(new Date()));
  const dates = k === "custom" ? { from, to } : k === "all" ? {} : { from: back(Number(k)), to: localDay(new Date()) };
  const field = (label: string) => (
    <>
      <label className="fbar-field">
        <span className="sub">{label}</span>
        <select className="fbar-select" value={k} onChange={e => setK(e.target.value)}>
          {RANGES.map(r => <option key={r.k} value={r.k}>{r.label}</option>)}
        </select>
      </label>
      {k === "custom" && (
        <label className="fbar-field">
          <span className="sub">Between</span>
          <span className="fbar-daterange">
            <input type="date" className="fbar-date" value={from} max={to} onChange={e => setFrom(e.target.value)} />
            <span className="sub">–</span>
            <input type="date" className="fbar-date" value={to} min={from} onChange={e => setTo(e.target.value)} />
          </span>
        </label>)}
    </>
  );
  return { dates, field, reset: () => setK(start), changed: k !== start };
}

export function Pick({ label, value, onChange, options, all }: {
  label: string; value: string | number; onChange: (v: string) => void;
  options: { value: string | number; label: string }[]; all: string;
}) {
  return (
    <label className="fbar-field">
      <span className="sub">{label}</span>
      <select className="fbar-select" value={value} onChange={e => onChange(e.target.value)}>
        <option value="">{all}</option>
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

export function SearchBox({ value, onChange, placeholder }: {
  value: string; onChange: (v: string) => void; placeholder: string;
}) {
  return (
    <label className="fbar-field grow">
      <span className="sub">Search</span>
      <span className="search">
        <Search className="ic" />
        <input type="search" placeholder={placeholder} aria-label={placeholder}
          value={value} onChange={e => onChange(e.target.value)} />
      </span>
    </label>
  );
}

export const Bar = ({ children, onClear, clear }: { children: ReactNode; onClear: () => void; clear: boolean }) => (
  <div className="card" style={{ marginBottom: 14 }}>
    <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none", alignItems: "flex-end" }}>
      {children}
      {clear && <span className="linkish fbar-clear-link" onClick={onClear}>Clear filters</span>}
    </div>
  </div>
);

/** The empty, loading and failed states — null when there are rows to show. */
export const listState = (loading: boolean, n: number, empty: string, failed: boolean) =>
  failed ? <div className="empty">Could not load this. Try again in a moment.</div>
    : loading && n === 0 ? <div className="empty">Loading…</div>
    : n === 0 ? <div className="empty">{empty}</div> : null;

/** A value as shown: "—" for nothing. */
export const val = (v: string | null | undefined) =>
  v == null || v === "" ? <span className="muted">—</span> : <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{v}</span>;

/** Field | Before | After — every field one save changed. */
export function ChangesTable({ changes, more = 0, highlight }: {
  changes: FieldChange[]; more?: number; highlight?: string | null;
}) {
  if (!changes.length) return <div className="sub" style={{ marginBottom: 12 }}>No field values were recorded for this step.</div>;
  return (
    <div className="tbl-wrap" style={{ marginBottom: 12 }}>
      <table>
        <thead><tr><th style={L}>Field</th><th style={L}>Before</th><th style={L}>After</th></tr></thead>
        <tbody>
          {changes.map((c, i) => (
            <tr key={i} style={c.field === highlight ? { background: "var(--p-brand-soft, #e6f4f5)" } : undefined}>
              <td style={{ ...L, whiteSpace: "normal" }}><b>{c.field}</b></td>
              <td style={{ ...L, whiteSpace: "normal", maxWidth: 260 }}>{val(c.old)}</td>
              <td style={{ ...L, whiteSpace: "normal", maxWidth: 260 }}>{val(c.new)}</td>
            </tr>))}
        </tbody>
      </table>
      {more > 0 && <div className="sub" style={{ padding: "6px 10px" }}>…and {more} more changes in the same save.</div>}
    </div>
  );
}

/** The earlier version kept on file (a mapping, a rule's settings), read as
 *  "what it said" — one line per entry. */
export function KeptOnFile({ before }: { before: Record<string, unknown> | null | undefined }) {
  const [open, setOpen] = useState(false);
  if (!before || !Object.keys(before).length) return null;
  const lines = flatten(before);
  return (
    <div style={{ marginBottom: 12 }}>
      <button type="button" className="linkish" onClick={() => setOpen(o => !o)}>
        {open ? "Hide" : "Show"} the earlier version kept on file ({lines.length} {lines.length === 1 ? "entry" : "entries"})
      </button>
      {open && (
        <div className="tbl-wrap" style={{ marginTop: 6, maxHeight: 320, overflow: "auto" }}>
          <table><tbody>
            {lines.map(([k, v]) => (
              <tr key={k}><td style={{ ...L, whiteSpace: "normal" }} className="sub">{k}</td>
                <td style={{ ...L, whiteSpace: "normal" }}>{val(v)}</td></tr>))}
          </tbody></table>
        </div>)}
    </div>
  );
}

function flatten(v: unknown, prefix = "", out: [string, string][] = []): [string, string][] {
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) flatten(x, prefix ? `${prefix} › ${k}` : k, out);
  } else if (Array.isArray(v) && v.length && v.every(i => i && typeof i === "object")) {
    v.forEach((x, i) => flatten(x, `${prefix} › ${i + 1}`, out));
  } else {
    out.push([prefix || "Value", v == null ? "" : typeof v === "string" ? v : JSON.stringify(v)]);
  }
  return out.slice(0, 2000);
}
