/**
 * The Contract picker on Process Bordereau — same treatment as
 * PeriodPicker, and for the same reason: a native <select> flattens
 * "Contract 462 — Insurisk Spec v1 (Demo-Insurisk Company org)" into one
 * run-on line, when the name, the programme and the carrier are three
 * different things a broker is actually scanning for.
 *
 * Still just ONE contract, still shown read-only (not a fake dropdown) when
 * there is nothing to choose between — see BrokerBordereau's own note on
 * why: a broker has exactly one thing to pick, and a control offering a
 * choice of one is furniture. This only changes how that one contract, or
 * the list of several, actually reads.
 */
import { useEffect, useRef, useState } from "react";
import { ChevronDown, Check } from "lucide-react";
import type { BrokerContract } from "../api/broker";

function ContractRow({ c }: { c: BrokerContract }) {
  return (
    <div style={{ minWidth: 0, flex: 1 }}>
      <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--p-ink)",
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {c.name || c.filename || `Contract ${c.id}`}
      </div>
      <div style={{ fontSize: 12, color: "var(--p-muted)", marginTop: 1,
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {c.programme.name} · {c.carrier.name}
      </div>
    </div>
  );
}

export function ContractPicker({
  value, onChange, contracts,
}: {
  value: number | "";
  onChange: (id: number) => void;
  contracts: BrokerContract[];
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

  const current = contracts.find(c => c.id === value) ?? null;

  // Nothing to choose between: shown flat, not as a control that opens onto
  // a single, unavoidable answer.
  if (contracts.length === 1) {
    return (
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        border: "1px solid var(--p-border-2)", borderRadius: "var(--p-r-sm)",
        background: "var(--p-surface-2)", padding: "9px 12px",
      }}>
        <ContractRow c={contracts[0]} />
      </div>
    );
  }

  if (contracts.length === 0) {
    return (
      <div style={{
        border: "1px solid var(--p-border-2)", borderRadius: "var(--p-r-sm)",
        background: "var(--p-surface-2)", padding: "9px 12px",
        fontSize: 13.5, color: "var(--p-faint)",
      }}>
        No active contract yet
      </div>
    );
  }

  function pick(id: number) {
    onChange(id);
    setOpen(false);
  }

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
        {current ? <ContractRow c={current} /> : (
          <span style={{ fontSize: 13.5, color: "var(--p-faint)" }}>Select Contract…</span>
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
          {contracts.map(c => {
            const selected = c.id === value;
            return (
              <button
                key={c.id} type="button" role="option"
                aria-selected={selected}
                onClick={() => pick(c.id)}
                style={{
                  width: "100%", display: "flex", alignItems: "center", gap: 10,
                  border: "none", borderRadius: 6, cursor: "pointer",
                  background: selected ? "var(--p-primary-soft)" : "transparent",
                  padding: "8px 10px", textAlign: "left",
                }}
                onMouseEnter={e => { if (!selected) e.currentTarget.style.background = "var(--p-surface-2)"; }}
                onMouseLeave={e => { if (!selected) e.currentTarget.style.background = "transparent"; }}
              >
                <ContractRow c={c} />
                {selected && <Check size={14} style={{ color: "var(--p-primary)", flex: "none" }} />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
