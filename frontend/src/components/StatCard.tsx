import type { ReactNode } from "react";
import { ChevronRight, type LucideIcon } from "lucide-react";

/** The dashboard KPI tile — one look across the carrier and broker dashboards. */
export function StatCard({ title, value, icon: Icon, trend, subtitle, tone, onClick }: {
  title: string;
  value: string | number;
  icon: LucideIcon;
  trend?: string;
  subtitle?: string;
  tone?: "alert";
  onClick?: () => void;
}) {
  const alert = tone === "alert";
  return (
    <div
      style={{
        backgroundColor: "var(--p-surface)",
        border: alert ? "1px solid var(--p-crit)" : "1px solid var(--p-border-2)",
        borderRadius: 16, padding: "16px", display: "flex", flexDirection: "column", gap: 10,
        boxShadow: "0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05)",
        cursor: onClick ? "pointer" : "default",
        transition: "transform 0.2s, box-shadow 0.2s",
      }}
      onClick={onClick}
      onMouseOver={onClick ? (e) => { e.currentTarget.style.transform = "translateY(-2px)"; e.currentTarget.style.boxShadow = "0 10px 15px -3px rgb(0 0 0 / 0.1)"; } : undefined}
      onMouseOut={onClick ? (e) => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = "0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05)"; } : undefined}
    >
      <div style={{
        width: 36, height: 36, borderRadius: 10,
        background: alert ? "#fef2f2" : "#f0fdfa",
        display: "flex", alignItems: "center", justifyContent: "center",
        color: alert ? "#ef4444" : "#0d9488",
      }}>
        <Icon size={18} strokeWidth={2.5} />
      </div>
      <div>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
          <div style={{ fontSize: 26, fontWeight: 700, color: "var(--p-text)", lineHeight: 1 }}>
            {value}
          </div>
          {trend && (
            <div style={{
              fontSize: 13, fontWeight: 600,
              color: trend.startsWith("-") || alert ? "#ef4444" : "#10b981",
              background: trend.startsWith("-") || alert ? "#fef2f2" : "#ecfdf5",
              padding: "4px 8px", borderRadius: 6,
            }}>
              {trend}
            </div>
          )}
        </div>
        <div style={{ color: "var(--p-text)", fontSize: 15, fontWeight: 500 }}>{title}</div>
        {subtitle && <div style={{ fontSize: 13, color: "var(--p-muted)", marginTop: 2 }}>{subtitle}</div>}
      </div>
    </div>
  );
}

/** The chart card frame the carrier dashboard uses: title, one info icon, body. */
export function ChartCard({ title, info, children }: {
  title: string; info?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="card" style={{ padding: "24px 20px", display: "flex", flexDirection: "column" }}>
      <div className="card-h" style={{ marginBottom: 20 }}>
        <h3>{title}</h3>
        {info}
      </div>
      {children}
    </div>
  );
}

/** The big clickable card at the foot of a dashboard ("Recent File
 *  Submissions"). `dark` is the carrier dashboard's navy variant. */
export function LinkCard({ title, value, label, icon: Icon, onClick, dark }: {
  title: string; value: string | number; label: string;
  icon: LucideIcon; onClick: () => void; dark?: boolean;
}) {
  const rest = dark ? "none" : "var(--p-sh)";
  return (
    <div className="card" role="link" tabIndex={0}
      onClick={onClick}
      onKeyDown={e => { if (e.key === "Enter") onClick(); }}
      style={{
        padding: 24, display: "flex", justifyContent: "space-between", alignItems: "center",
        cursor: "pointer", transition: "transform 0.2s, box-shadow 0.2s",
        ...(dark ? { background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
                     color: "white", border: "none", boxShadow: rest } : {}),
      }}
      onMouseOver={e => { e.currentTarget.style.transform = "translateY(-2px)";
        e.currentTarget.style.boxShadow = dark ? "0 10px 15px -3px rgba(15,23,42,0.4)"
                                               : "0 10px 15px -3px rgb(0 0 0 / 0.1)"; }}
      onMouseOut={e => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = rest; }}>
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
          <div style={{ width: 40, height: 40, borderRadius: 8, display: "flex",
                        alignItems: "center", justifyContent: "center",
                        backgroundColor: dark ? "rgba(255,255,255,0.1)" : "var(--p-surface-2)" }}>
            <Icon size={20} />
          </div>
          <h3 style={{ margin: 0, fontSize: 18, color: dark ? "white" : undefined }}>{title}</h3>
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
          <span style={{ fontSize: 32, fontWeight: 600, color: dark ? "white" : "var(--p-text)" }}>{value}</span>
          <span style={{ fontSize: 14, color: dark ? "rgba(255,255,255,0.7)" : "var(--p-muted)" }}>{label}</span>
        </div>
      </div>
      <ChevronRight size={24} style={{ opacity: dark ? 0.5 : 0.3 }} />
    </div>
  );
}
