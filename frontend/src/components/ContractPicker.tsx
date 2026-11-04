/**
 * The Programme and Contract pickers on Process Bordereau — same treatment as
 * PeriodPicker, and for the same reason: a native <select> flattens
 * "Contract 462 — Insurisk Spec v1 (Demo-Insurisk Company org)" into one
 * run-on line, when the name, the programme and the carrier are different
 * things a broker is actually scanning for.
 *
 * Shown read-only (not a fake dropdown) when there is nothing to choose
 * between: a control offering a choice of one is furniture. Both pickers are
 * the one `ChoicePicker`, so they look and behave alike.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Check } from "lucide-react";
import type { BrokerContract } from "../api/broker";
import { fmtDate } from "../utils/date";
import { InfoTip } from "./InfoTip";

/** A programme a broker can send a bordereau for, with the contracts on it. */
export type BrokerProgramme = {
  id: number;
  name: string;
  /** Short code for email subjects and file names, e.g. PRG-5QSH84. */
  code?: string | null;
  carrier: string;
  contracts: BrokerContract[];
};

/** Live, or ended but still owed bordereaux for the months of its term — and
 *  a renewed contract's PREVIOUS TERM, for the months of that term a broker
 *  never sent (the server lists only those months for it). */
export const isRunnable = (c: BrokerContract) =>
  c.lifecycle === "active" || c.lifecycle === "expired" || !!c.previous_term;

/** The contract to pre-select among a programme's: the only one, or the one
 *  in force when the rest are previous terms — the renewal stays the default
 *  for its own months; a previous term is picked on purpose. */
export function defaultContractId(list: BrokerContract[]): number | "" {
  if (list.length === 1) return list[0].id;
  const current = list.filter(c => !c.previous_term);
  return current.length === 1 ? current[0].id : "";
}

function Row({ title, sub, badge }: { title: string; sub?: ReactNode; badge?: ReactNode }) {
  return (
    <div style={{ minWidth: 0, flex: 1, display: "flex", alignItems: "center", gap: 10 }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--p-ink)",
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {title}
        </div>
        {sub && (
          <div style={{ fontSize: 12, color: "var(--p-muted)", marginTop: 1,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {sub}
          </div>
        )}
      </div>
      {badge}
    </div>
  );
}

/** The pick as the closed box shows it: one line, the name first. The detail
 *  lines stay on the open list, where they are what tells options apart. */
function Picked({ title, aside, badge }: { title: string; aside?: string | null; badge?: ReactNode }) {
  return (
    <div style={{ minWidth: 0, flex: 1, display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--p-ink)",
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</span>
      {aside && (
        <span style={{ fontSize: 12, color: "var(--p-muted)", whiteSpace: "nowrap",
          overflow: "hidden", textOverflow: "ellipsis" }}>· {aside}</span>
      )}
      {badge && <span style={{ marginLeft: "auto", flex: "none" }}>{badge}</span>}
    </div>
  );
}

/** One dropdown for both pickers: the closed box shows the pick in full. */
function ChoicePicker<T>({ items, value, onChange, keyOf, render, renderPicked, placeholder, empty }: {
  items: T[];
  value: number | "";
  onChange: (id: number) => void;
  keyOf: (t: T) => number;
  render: (t: T) => ReactNode;
  /** The closed box; defaults to `render`. */
  renderPicked?: (t: T) => ReactNode;
  placeholder: string;
  empty: string;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const flat = {
    display: "flex", alignItems: "center", gap: 10,
    border: "1px solid var(--p-border-2)", borderRadius: "var(--p-r-sm)",
    background: "var(--p-surface-2)", padding: "9px 12px",
  } as const;

  // Nothing to choose between: shown flat, not as a control that opens onto
  // a single, unavoidable answer.
  const picked = renderPicked ?? render;
  if (items.length === 1) return <div style={flat}>{picked(items[0])}</div>;
  if (items.length === 0) {
    return <div style={{ ...flat, fontSize: 13.5, color: "var(--p-faint)" }}>{empty}</div>;
  }

  const current = items.find(t => keyOf(t) === value) ?? null;
  return (
    <div style={{ position: "relative" }} ref={box}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        style={{
          width: "100%", display: "flex", alignItems: "center", gap: 10,
          border: `1px solid ${open ? "var(--p-primary)" : "var(--p-border-2)"}`,
          borderRadius: "var(--p-r-sm)", background: "var(--p-surface)",
          padding: "9px 12px", cursor: "pointer",
          boxShadow: open ? "0 0 0 3px var(--p-primary-soft)" : "none",
          transition: ".12s", textAlign: "left",
        }}
      >
        {current ? picked(current) : (
          <span style={{ flex: 1, fontSize: 13.5, color: "var(--p-faint)" }}>{placeholder}</span>
        )}
        <ChevronDown size={15} strokeWidth={2}
          style={{ flex: "none", color: "var(--p-faint)" }} />
      </button>

      {open && (
        <div role="listbox" style={{
          position: "absolute", zIndex: 40, top: "calc(100% + 6px)", left: 0, right: 0,
          background: "var(--p-surface)", border: "1px solid var(--p-border-2)",
          borderRadius: "var(--p-r-sm)", boxShadow: "0 12px 28px -8px rgba(14,19,32,.18)",
          maxHeight: 320, overflowY: "auto", padding: 4,
        }}>
          {items.map(t => {
            const id = keyOf(t);
            const selected = id === value;
            return (
              <button
                key={id} type="button" role="option"
                aria-selected={selected}
                onClick={() => { onChange(id); setOpen(false); }}
                style={{
                  width: "100%", display: "flex", alignItems: "center", gap: 10,
                  border: "none", borderRadius: 6, cursor: "pointer",
                  background: selected ? "var(--p-primary-soft)" : "transparent",
                  padding: "8px 10px", textAlign: "left",
                }}
                onMouseEnter={e => { if (!selected) e.currentTarget.style.background = "var(--p-surface-2)"; }}
                onMouseLeave={e => { if (!selected) e.currentTarget.style.background = "transparent"; }}
              >
                {render(t)}
                {selected && <Check size={14} style={{ color: "var(--p-primary)", flex: "none" }} />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function ProgrammePicker({ value, onChange, programmes }: {
  value: number | "";
  onChange: (id: number) => void;
  programmes: BrokerProgramme[];
}) {
  return (
    <ChoicePicker items={programmes} value={value} onChange={onChange} keyOf={p => p.id}
      placeholder="Select Programme…" empty="No programme yet"
      renderPicked={p => <Picked title={p.name} aside={p.carrier} />}
      render={p => (
        <Row title={p.name}
          sub={[p.code, p.carrier,
            `${p.contracts.length} ${p.contracts.length === 1 ? "contract" : "contracts"}`]
            .filter(Boolean).join(" · ")} />
      )} />
  );
}

/** The term in a few words — and, once it has ended, that it has. */
function termOf(c: BrokerContract): string | null {
  if (c.previous_term) {
    const span = c.inception_dt && c.expiry_dt
      ? `${fmtDate(c.inception_dt)} – ${fmtDate(c.expiry_dt)}` : null;
    return [span, "for months of this term not yet sent"].filter(Boolean).join(" · ");
  }
  if (c.lifecycle === "expired") return c.expiry_dt ? `Term ended ${fmtDate(c.expiry_dt)}` : "Term ended";
  return c.expiry_dt ? `In force until ${fmtDate(c.expiry_dt)}` : null;
}

/** Previous term / Ended. On the closed box the term's detail sits behind the
 *  badge's ⓘ instead of a line of its own. */
function contractBadge(c: BrokerContract, withTip: boolean): ReactNode {
  const tip = withTip ? termNote(c) : null;
  const info = tip && (
    // A click on the ⓘ must not also open the dropdown it sits in.
    <span onClick={e => e.stopPropagation()} style={{ display: "inline-flex" }}>
      <InfoTip text={tip} />
    </span>
  );
  if (c.previous_term) {
    return <span className="badge b-info" style={{ flex: "none" }}
      title={withTip ? undefined : (c.renewed_by?.name ? `Renewed as ${c.renewed_by.name}` : "Renewed")}>
      <span className="d" />Previous term{info}</span>;
  }
  if (c.lifecycle === "expired") {
    return <span className="badge b-warn" style={{ flex: "none" }}><span className="d" />Ended{info}</span>;
  }
  return null;
}

/** What the badge means, in a sentence. */
function termNote(c: BrokerContract): string | null {
  if (c.previous_term) {
    const by = c.renewed_by?.name ? ` as ${c.renewed_by.name}` : "";
    const span = c.inception_dt && c.expiry_dt
      ? ` (${fmtDate(c.inception_dt)} – ${fmtDate(c.expiry_dt)})` : "";
    return `Renewed${by}. Use this previous term${span} only for its months not yet sent.`;
  }
  if (c.lifecycle === "expired") {
    return `Term ended${c.expiry_dt ? ` on ${fmtDate(c.expiry_dt)}` : ""}. `
      + "You can still send bordereaux for the months up to then.";
  }
  return null;
}

export function ContractPicker({ value, onChange, contracts }: {
  value: number | "";
  onChange: (id: number) => void;
  contracts: BrokerContract[];
}) {
  return (
    <ChoicePicker value={value} onChange={onChange} keyOf={c => c.id}
      // The contract in force first; a previous term after it.
      items={[...contracts].sort((a, b) => Number(!!a.previous_term) - Number(!!b.previous_term))}
      placeholder="Select Contract…" empty="No active contract yet"
      renderPicked={c => <Picked title={c.name || c.filename || `Contract ${c.id}`}
        badge={contractBadge(c, true)} />}
      render={c => (
        <Row title={c.name || c.filename || `Contract ${c.id}`}
          sub={[c.code, c.carrier.name, termOf(c)].filter(Boolean).join(" · ")}
          badge={contractBadge(c, false)} />
      )} />
  );
}
