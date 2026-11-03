import { useNavigate } from "react-router-dom";
import { CalendarX } from "lucide-react";
import { InfoTip } from "./InfoTip";

/** The "Overdue Bordereaux" box: how the files that fell due actually turned
 *  up — on time, late, or not at all — in one tile, with a bar showing the
 *  split. The same three counts, and the same words, as the headline tiles on
 *  the Bordereau Calendar, which is where the box opens. */
export function ArrivalsCard({ onTime, late, never, subtitle, info, to = "/bordereau-calendar" }: {
  onTime: number | null | undefined;
  late: number | null | undefined;
  never: number | null | undefined;
  subtitle?: string;
  info?: string;
  to?: string;
}) {
  const nav = useNavigate();
  const rows = [
    { label: "Arrived on time", v: onTime, c: "var(--p-ok)" },
    { label: "Arrived late", v: late, c: "var(--p-warn)" },
    { label: "Never arrived", v: never, c: "var(--p-crit)" },
  ];
  const total = rows.reduce((a, r) => a + (r.v ?? 0), 0);
  const alert = (never ?? 0) > 0;
  const shadow = "0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05)";

  return (
    <div
      role="link" tabIndex={0}
      onClick={() => nav(to)}
      onKeyDown={e => { if (e.key === "Enter") nav(to); }}
      style={{
        backgroundColor: "var(--p-surface)",
        border: alert ? "1px solid var(--p-crit)" : "1px solid var(--p-border-2)",
        borderRadius: 16, padding: 16, display: "flex", flexDirection: "column", gap: 10,
        boxShadow: shadow, cursor: "pointer", transition: "transform 0.2s, box-shadow 0.2s",
      }}
      onMouseOver={e => { e.currentTarget.style.transform = "translateY(-2px)"; e.currentTarget.style.boxShadow = "0 10px 15px -3px rgb(0 0 0 / 0.1)"; }}
      onMouseOut={e => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = shadow; }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 36, height: 36, borderRadius: 10, flex: "none",
          background: alert ? "#fef2f2" : "#f0fdfa",
          display: "flex", alignItems: "center", justifyContent: "center",
          color: alert ? "#ef4444" : "#0d9488",
        }}>
          <CalendarX size={18} strokeWidth={2.5} />
        </div>
        <div style={{ color: "var(--p-text)", fontSize: 15, fontWeight: 600,
                      display: "flex", alignItems: "center", gap: 5, minWidth: 0 }}>
          Overdue Bordereaux{info && <InfoTip text={info} />}
        </div>
      </div>

      {/* The split at a glance; grey until anything has fallen due. */}
      <div style={{ display: "flex", gap: 2, height: 6, borderRadius: 3, overflow: "hidden",
                    background: "var(--p-surface-3)" }}>
        {total > 0 && rows.filter(r => (r.v ?? 0) > 0).map(r => (
          <span key={r.label} title={`${r.label}: ${r.v}`}
                style={{ width: `${((r.v ?? 0) / total) * 100}%`, background: r.c }} />
        ))}
      </div>

      <div>
        {rows.map((r, i) => (
          <div key={r.label} style={{
            display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
            borderTop: i === 0 ? undefined : "1px solid var(--p-border)",
          }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: r.c, flex: "none" }} />
            <span style={{ fontSize: 13, color: "var(--p-muted)" }}>{r.label}</span>
            <span style={{ marginLeft: "auto", fontSize: 16, fontWeight: 700, color: "var(--p-text)" }}>
              {r.v == null ? "—" : r.v.toLocaleString()}
            </span>
          </div>
        ))}
      </div>

      {subtitle && <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>{subtitle}</div>}
    </div>
  );
}
