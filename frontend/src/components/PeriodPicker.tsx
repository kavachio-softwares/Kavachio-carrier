/**
 * The Reporting Period picker — Process Bordereau's own combobox, shared by
 * both screens that pick a period (BrokerBordereau.tsx, DirectRun.tsx).
 *
 * A native <select> cannot show a period, its due date and a "resubmit" tag
 * as three visually distinct things inside one option — every browser folds
 * an <option> down to plain text, which is what made the old picker read as
 * one run-on line ("2026-08 — due 2026-09-10 (already processed — resubmit
 * /correct)"). This draws its own list instead, the same button+panel
 * pattern BrokerCarrierSwitch already uses for the sidebar's carrier switch,
 * restyled for `.proto` (that one is sidebar-dark; this sits on a light
 * card).
 */
import { useEffect, useRef, useState } from "react";
import { ChevronDown, Check } from "lucide-react";

export type PeriodOption = {
  expected_id: number;
  period: string;
  due_date: string | null;
  processed: boolean;
};

/** "2026-08" → "August 2026". Everything else (2026-Q3, 2026-H2, 2026-W28,
 *  a bare 2026) is already a short, readable label on its own — read as
 *  typed rather than half-parsed into something worse. */
function friendlyPeriod(period: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(period);
  if (!m) return period;
  const [, y, mo] = m;
  const d = new Date(Number(y), Number(mo) - 1, 1);
  if (Number.isNaN(d.getTime())) return period;
  return d.toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

function friendlyDue(due: string | null): string {
  if (!due) return "no due date set";
  const d = new Date(`${due}T00:00:00`);
  if (Number.isNaN(d.getTime())) return due;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function PeriodPicker({
  value, onChange, periods, placeholder, emptyLabel,
}: {
  value: string;
  onChange: (period: string) => void;
  /** null = still loading. Empty array = nothing set up yet. */
  periods: PeriodOption[] | null;
  placeholder?: string;
  /** Shown when `periods` is an empty array — who "nothing set up" is about
   *  differs by caller (a contract, a broker, …). */
  emptyLabel?: string;
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

  const loading = periods === null;
  const empty = !loading && periods.length === 0;
  const current = periods?.find(p => p.period === value) ?? null;
  const disabled = loading || empty;

  function pick(period: string) {
    onChange(period);
    setOpen(false);
  }

  return (
    <div style={{ position: "relative" }} ref={box}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(o => !o)}
        style={{
          width: "100%", display: "flex", alignItems: "center", gap: 10,
          border: `1px solid ${open ? "var(--p-primary)" : "var(--p-border-2)"}`,
          borderRadius: "var(--p-r-sm)", background: "var(--p-surface)",
          padding: "9px 12px", cursor: disabled ? "not-allowed" : "pointer",
          boxShadow: open ? "0 0 0 3px var(--p-primary-soft)" : "none",
          transition: ".12s", textAlign: "left",
        }}
      >
        {current ? (
          <>
            <span style={{ fontSize: 13.5, fontWeight: 600, color: "var(--p-ink)" }}>
              {friendlyPeriod(current.period)}
            </span>
            <span style={{ fontSize: 12.5, color: "var(--p-faint)" }}>
              Due {friendlyDue(current.due_date)}
            </span>
            {current.processed && (
              <span className="badge b-warn" style={{ marginLeft: "auto", flex: "none" }}>
                Resubmit
              </span>
            )}
          </>
        ) : (
          <span style={{ fontSize: 13.5, color: "var(--p-faint)" }}>
            {loading ? "Loading…"
              : empty ? (emptyLabel ?? "No periods set up for this contract yet")
              : placeholder ?? "Select the period this bordereau is for…"}
          </span>
        )}
        <ChevronDown size={15} strokeWidth={2}
          style={{ marginLeft: current ? 0 : "auto", flex: "none", color: "var(--p-faint)" }} />
      </button>

      {open && periods && periods.length > 0 && (
        <div role="listbox" style={{
          position: "absolute", zIndex: 40, top: "calc(100% + 6px)", left: 0, right: 0,
          background: "var(--p-surface)", border: "1px solid var(--p-border-2)",
          borderRadius: "var(--p-r-sm)", boxShadow: "0 12px 28px -8px rgba(14,19,32,.18)",
          maxHeight: 280, overflowY: "auto", padding: 4,
        }}>
          {periods.map(p => {
            const selected = p.period === value;
            return (
              <button
                key={p.expected_id} type="button" role="option"
                aria-selected={selected}
                onClick={() => pick(p.period)}
                style={{
                  width: "100%", display: "flex", alignItems: "center", gap: 10,
                  border: "none", borderRadius: 6, cursor: "pointer",
                  background: selected ? "var(--p-primary-soft)" : "transparent",
                  padding: "8px 10px", textAlign: "left",
                }}
                onMouseEnter={e => { if (!selected) e.currentTarget.style.background = "var(--p-surface-2)"; }}
                onMouseLeave={e => { if (!selected) e.currentTarget.style.background = "transparent"; }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--p-ink)" }}>
                    {friendlyPeriod(p.period)}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--p-muted)", marginTop: 1 }}>
                    Due {friendlyDue(p.due_date)}
                  </div>
                </div>
                {p.processed && (
                  <span className="badge b-warn" style={{ flex: "none" }}>Resubmit</span>
                )}
                {selected && <Check size={14} style={{ color: "var(--p-primary)", flex: "none" }} />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
