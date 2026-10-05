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

/** A programme a broker can send a bordereau for, with the contracts on it. */
export type BrokerProgramme = {
  id: number;
  name: string;
  carrier: string;
  contracts: BrokerContract[];
};

/** Live, or ended but still owed bordereaux for the months of its term. */
export const isRunnable = (c: BrokerContract) =>
  c.lifecycle === "active" || c.lifecycle === "expired";

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

/** One dropdown for both pickers: the closed box shows the pick in full. */
function ChoicePicker<T>({ items, value, onChange, keyOf, render, placeholder, empty }: {
  items: T[];
  value: number | "";
  onChange: (id: number) => void;
  keyOf: (t: T) => number;
  render: (t: T) => ReactNode;
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
  if (items.length === 1) return <div style={flat}>{render(items[0])}</div>;
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
        {current ? render(current) : (
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
      render={p => (
        <Row title={p.name}
          sub={`${p.carrier} · ${p.contracts.length} ${p.contracts.length === 1 ? "contract" : "contracts"}`} />
      )} />
  );
}

/** The term in a few words — and, once it has ended, that it has. */
function termOf(c: BrokerContract): string | null {
  if (c.lifecycle === "expired") return c.expiry_dt ? `Term ended ${fmtDate(c.expiry_dt)}` : "Term ended";
  return c.expiry_dt ? `In force until ${fmtDate(c.expiry_dt)}` : null;
}

export function ContractPicker({ value, onChange, contracts }: {
  value: number | "";
  onChange: (id: number) => void;
  contracts: BrokerContract[];
}) {
  return (
    <ChoicePicker items={contracts} value={value} onChange={onChange} keyOf={c => c.id}
      placeholder="Select Contract…" empty="No active contract yet"
      render={c => (
        <Row title={c.name || c.filename || `Contract ${c.id}`}
          sub={[c.carrier.name, termOf(c)].filter(Boolean).join(" · ")}
          badge={c.lifecycle === "expired"
            ? <span className="badge b-warn" style={{ flex: "none" }}><span className="d" />Ended</span>
            : null} />
      )} />
  );
}
