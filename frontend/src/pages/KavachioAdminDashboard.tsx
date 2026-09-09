// Kavachio platform-admin dashboard — cross-tenant health & activity.
// Data: GET /dashboard/platform?range=<preset> (aggregates).
// Both are kavachio_admin-only and sum across ALL tenants (no `mga`).
// Charts are hand-rolled inline SVG so we add no charting dependency.
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { InfoTip } from "../components/InfoTip";

// ---- motion helpers ----
// All entrance motion is CSS (see the `kd-*` block in proto.css); only the
// number count-up and the donut sweep need JS. Both no-op under
// prefers-reduced-motion so the page just renders at its final state.
const reducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Animated stagger delay for the i-th item, as an inline CSS var. */
const stagger = (i: number, step = 60, base = 0): React.CSSProperties =>
  ({ ["--kd-d" as any]: `${base + i * step}ms` });

/** Counts from 0 up to `value` on mount (ease-out), then holds. */
function useCountUp(value: number, ms = 900) {
  const [n, setN] = useState(() => (reducedMotion() ? value : 0));
  const raf = useRef<number>();
  useEffect(() => {
    if (reducedMotion()) { setN(value); return; }
    let start: number | null = null;
    const tick = (t: number) => {
      if (start == null) start = t;
      const p = Math.min(1, (t - start) / ms);
      setN(Math.round(value * (1 - Math.pow(1 - p, 3))));   // easeOutCubic
      if (p < 1) raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, [value, ms]);
  return n;
}

function CountUp({ value }: { value: number | null | undefined }) {
  const n = useCountUp(value ?? 0);
  return <>{value == null ? "—" : n.toLocaleString()}</>;
}

// ---- palette (aligned with proto.css tokens) ----
const C = {
  ink: "#0E1320", muted: "#566071", faint: "#8B93A2", line: "#E5E8EE",
  blue: "#3149C6", ok: "#0E9F6E", warn: "#C77A12", crit: "#D32F45",
  info: "#17A2B8", purple: "#7C5CFC", slate: "#5B6B85",
};

// Date-range presets shown in the filter (value → label). `range` is passed to
// the API, which windows every time-based metric.
const RANGES: { value: string; label: string; short: string }[] = [
  { value: "1d",  label: "Today",         short: "today" },
  { value: "7d",  label: "Last 7 Days",   short: "7d" },
  { value: "30d", label: "Last 30 Days",  short: "30d" },
  { value: "90d", label: "Last 90 Days",  short: "90d" },
  { value: "ytd", label: "This Year",     short: "YTD" },
  { value: "12m", label: "Last 12 Months", short: "12m" },
  { value: "all", label: "All Time",      short: "all" },
];

type Platform = {
  range: string; range_days: number;
  tenants: { total: number; active: number; invited: number; inactive: number };
  users: { total: number; pending_invites: number;
           by_role: { tenant_user: number; tenant_admin: number; kavachio_admin: number } };
  setups: { active: number; tenants: number };
  programs_active: number;
  runs: { window_count: number; prev_count: number; delta_pct: number | null; clean_rate: number | null };
  runs_series: { date: string; total: number; exceptions: number }[];
  exceptions_by_severity: { critical: number; warning: number; info: number };
  top_tenants: { name: string; code: string; runs: number }[];
  tenants_table: { code: string; name: string; users: number; setups: number;
                   runs: number; clean_pct: number | null; status: string }[];
  mapping_queue: { open: number; in_progress: number; resolved_window: number;
                   dismissed: number; avg_turnaround_hours: number | null };
};

const nf = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString());

// ---------- SVG area chart: full-width, live interactive hover ----------
function AreaChart({ series }: { series: Platform["runs_series"] }) {
  // preserveAspectRatio="none" stretches to full width; vector-effect keeps
  // stroke widths crisp despite the non-uniform scale.
  const W = 1000, H = 230, padL = 34, padB = 24, padT = 12, padR = 10;
  const pw = W - padL - padR, ph = H - padT - padB;
  const max = Math.max(1, ...series.map(d => d.total));
  const n = Math.max(1, series.length - 1);
  const x = (i: number) => padL + (pw * i) / n;
  const y = (v: number) => padT + ph - (ph * v) / max;
  const line = (key: "total" | "exceptions") =>
    series.map((d, i) => `${i ? "L" : "M"} ${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join(" ");
  const area = (key: "total" | "exceptions") =>
    `M ${padL},${padT + ph} ` +
    series.map((d, i) => `L ${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join(" ") +
    ` L ${padL + pw},${padT + ph} Z`;
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const everyN = Math.ceil(series.length / 8);

  // Live hover: map the cursor's x within the plot area to the nearest day and
  // drive a crosshair + floating tooltip from React state (instant, no OS delay).
  const [hi, setHi] = useState<number | null>(null);
  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const fx = (e.clientX - rect.left) / rect.width;         // 0..1 across container
    const frac = Math.min(1, Math.max(0, (fx - padL / W) / (pw / W)));
    setHi(Math.round(frac * n));
  };
  const hp = hi != null ? series[hi] : null;
  const leftPct = hi != null ? (x(hi) / W) * 100 : 0;
  // keep the tooltip inside the card near the edges
  const tipTransform = leftPct < 14 ? "translateX(0)"
    : leftPct > 86 ? "translateX(-100%)" : "translateX(-50%)";

  return (
    <div style={{ position: "relative" }} onMouseLeave={() => setHi(null)} onMouseMove={onMove}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none"
        style={{ display: "block", cursor: "crosshair" }}>
        <defs>
          <linearGradient id="adC" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor={C.blue} stopOpacity="0.28" />
            <stop offset="1" stopColor={C.blue} stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="adE" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor={C.warn} stopOpacity="0.35" />
            <stop offset="1" stopColor={C.warn} stopOpacity="0.03" />
          </linearGradient>
        </defs>
        {ticks.map((t, i) => {
          const yy = padT + ph - ph * t;
          return <g key={i}>
            <line x1={padL} y1={yy} x2={padL + pw} y2={yy} stroke={C.line} strokeWidth="1"
              vectorEffect="non-scaling-stroke" />
            <text x={padL - 6} y={yy + 3} fontSize="10" fill={C.faint} textAnchor="end"
              vectorEffect="non-scaling-stroke">{Math.round(max * t)}</text>
          </g>;
        })}
        <path className="kd-area" d={area("total")} fill="url(#adC)" />
        <path className="kd-line" pathLength={1} d={line("total")} fill="none" stroke={C.blue}
          strokeWidth="2" vectorEffect="non-scaling-stroke" />
        <path className="kd-area" d={area("exceptions")} fill="url(#adE)" />
        <path className="kd-line" pathLength={1} style={stagger(1, 120)} d={line("exceptions")}
          fill="none" stroke={C.warn} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
        {/* crosshair at the hovered day */}
        {hi != null && (
          <line x1={x(hi)} y1={padT} x2={x(hi)} y2={padT + ph} stroke={C.slate}
            strokeWidth="1" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" />
        )}
        {/* x labels */}
        {series.map((d, i) => (i % everyN === 0 || i === series.length - 1) ? (
          <text key={i} x={x(i)} y={H - 7} fontSize="10" fill={C.faint} textAnchor="middle"
            vectorEffect="non-scaling-stroke">{d.date.slice(5)}</text>) : null)}
      </svg>
      {/* dots on the two series at the hovered day (HTML so they stay round) */}
      {hp && [{ v: hp.total, c: C.blue }, { v: hp.exceptions, c: C.warn }].map((p, i) => (
        <span key={i} style={{
          position: "absolute", left: `${leftPct}%`, top: `${(y(p.v) / H) * 100}%`,
          width: 8, height: 8, borderRadius: 4, background: "#fff", border: `2px solid ${p.c}`,
          transform: "translate(-50%,-50%)", pointerEvents: "none",
        }} />
      ))}
      {/* floating tooltip */}
      {hp && (
        <div style={{
          position: "absolute", left: `${leftPct}%`, top: 4, transform: tipTransform,
          pointerEvents: "none", background: "#fff", border: `1px solid ${C.line}`,
          borderRadius: 8, boxShadow: "0 6px 20px rgba(14,19,32,.14)", padding: "8px 11px",
          fontSize: 11.5, whiteSpace: "nowrap", zIndex: 5,
        }}>
          <div style={{ fontWeight: 700, color: C.ink, marginBottom: 4 }}>{hp.date}</div>
          <Row c={C.slate} k="Total" v={hp.total} />
          <Row c={C.blue} k="Clean" v={hp.total - hp.exceptions} />
          <Row c={C.warn} k="Exceptions" v={hp.exceptions} />
        </div>
      )}
    </div>
  );
}

function Row({ c, k, v }: { c: string; k: string; v: number }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 7, lineHeight: 1.6 }}>
      <span style={{ width: 8, height: 8, borderRadius: 2, background: c }} />
      <span style={{ color: C.muted, flex: 1 }}>{k}</span>
      <span style={{ color: C.ink, fontWeight: 700 }}>{v.toLocaleString()}</span>
    </div>
  );
}

function Donut({ segments, total, unit }: {
  segments: { label: string; value: number; color: string }[]; total: number; unit: string;
}) {
  const R = 62, SW = 22, cx = 80, cy = 90, Cc = 2 * Math.PI * R;
  let off = 0;
  const sum = segments.reduce((a, s) => a + s.value, 0) || 1;
  // Arcs start collapsed and sweep out to their real length one frame after
  // mount (the CSS transition on .kd-arc does the work).
  const [swept, setSwept] = useState(() => reducedMotion());
  useEffect(() => {
    if (reducedMotion()) return;
    const t = requestAnimationFrame(() => setSwept(true));
    return () => cancelAnimationFrame(t);
  }, []);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
      <svg width="160" height="180" viewBox="0 0 160 180">
        <circle cx={cx} cy={cy} r={R} fill="none" stroke="#F0F2F7" strokeWidth={SW} />
        {segments.map((s, i) => {
          const len = (Cc * s.value) / sum;
          const el = (
            <circle key={i} className="kd-arc" cx={cx} cy={cy} r={R} fill="none" stroke={s.color}
              strokeWidth={SW}
              strokeDasharray={swept ? `${len} ${Cc - len}` : `0 ${Cc}`} strokeDashoffset={-off}
              transform={`rotate(-90 ${cx} ${cy})`}
              style={{ cursor: "pointer", transitionDelay: `${i * 140}ms` }}>
              <title>{`${s.label}: ${s.value.toLocaleString()} (${Math.round((s.value / sum) * 100)}%)`}</title>
            </circle>
          );
          off += len; return el;
        })}
        <text x={cx} y={cy - 2} fontSize="26" fontWeight="800" fill={C.ink} textAnchor="middle">
          <CountUp value={total} /></text>
        <text x={cx} y={cy + 18} fontSize="11" fill={C.muted} textAnchor="middle">{unit}</text>
      </svg>
      <div style={{ flex: 1 }}>
        {segments.map((s, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 0" }}
            title={`${s.label}: ${s.value.toLocaleString()} (${Math.round((s.value / sum) * 100)}%)`}>
            <span style={{ width: 12, height: 12, borderRadius: 3, background: s.color }} />
            <span style={{ fontSize: 13, color: C.ink, fontWeight: 600, flex: 1 }}>{s.label}</span>
            <span style={{ fontSize: 13, color: C.ink, fontWeight: 700 }}>{nf(s.value)}</span>
            <span style={{ fontSize: 11, color: C.faint, width: 34, textAlign: "right" }}>
              {Math.round((s.value / sum) * 100)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function statusBadge(s: string) {
  const map: Record<string, [string, string]> = {
    active: ["b-ok", "Active"], invited: ["b-warn", "Invited"], inactive: ["b-mut", "Inactive"],
  };
  const [cls, label] = map[s] ?? ["b-mut", s];
  return <span className={`badge ${cls}`}>{label}</span>;
}

// ---------- page ----------
export default function KavachioAdminDashboard() {
  const [range, setRange] = useState("30d");
  const [d, setD] = useState<Platform | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = (rng: string) => {
    setLoading(true);
    api.get<Platform>("/dashboard/platform", { params: { range: rng } })
      .then(a => { setD(a.data); setErr(null); })
      .catch(() => setErr("Could not load platform metrics."))
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(range); }, [range]);

  const rangeMeta = RANGES.find(r => r.value === range) ?? RANGES[2];

  // Tolerate the pre-merge backend shape: the monolith rename (window_count /
  // clean_rate / resolved_window) may not be deployed to the running service
  // yet, so fall back to the old field names. Keeps the live app from breaking
  // before the backend change is merged & rebuilt.
  const runsCount = d ? (d.runs.window_count ?? (d.runs as any).last_7d ?? 0) : 0;
  const cleanRate = d ? (d.runs.clean_rate ?? (d.runs as any).clean_rate_30d ?? null) : null;
  const resolvedCount = d ? (d.mapping_queue.resolved_window ?? (d.mapping_queue as any).resolved_30d ?? 0) : 0;

  const tiles = d ? [
    { k: "Brokers", v: d.tenants.total, foot: `${d.tenants.active} Active · ${d.tenants.invited} Invited`, col: C.blue,
      info: "Every broker organization on the platform, across all customers. "
        + "“Active” have signed in and are using Kavachio; “Invited” have been created but nobody has signed up yet." },
    { k: "Users", v: d.users.total, foot: `${d.users.pending_invites} Invited · Awaiting Sign-up`, col: C.purple,
      info: "All user accounts across every broker, including Kavachio staff. "
        + "“Invited · Awaiting Sign-up” have been sent an invite but haven’t set a password yet." },
    { k: "Programs", v: d.programs_active, foot: "Active BDX Cycles", col: C.ok,
      info: "Programs currently active across all brokers — each is a book of business under a carrier "
        + "that bordereaux are processed against." },
    { k: `Runs · ${rangeMeta.short}`, v: runsCount,
      foot: d.runs.delta_pct == null ? "vs prior period"
        : `${d.runs.delta_pct >= 0 ? "▲" : "▼"} ${Math.abs(d.runs.delta_pct)}% vs prior`,
      col: C.ink, up: (d.runs.delta_pct ?? 0) >= 0,
      info: `Bordereaux processed in the selected date range (${rangeMeta.label.toLowerCase()}). `
        + "The change compares this range with the immediately preceding one of the same length." },
    { k: "Remaining Bordereau Setup", v: d.mapping_queue.open,
      foot: `Awaiting Review · ${d.mapping_queue.in_progress} in Progress`, col: C.warn,
      info: "Data-mapping tasks still needing Kavachio staff to map a new file layout to the data model. "
        + "“Awaiting Review” haven’t been picked up; “In Progress” are being worked on." },
  ] : [];

  const card: React.CSSProperties = {
    background: "#fff", border: `1px solid ${C.line}`, borderRadius: 14, padding: 18,
  };
  // `info` adds an ⓘ next to the card title explaining what the card shows.
  const cardHead = (title: string, sub?: string, info?: string) => (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 12 }}>
      <div style={{ fontSize: 15, fontWeight: 700, color: C.ink, display: "flex", alignItems: "center", gap: 5 }}>
        {title}{info && <InfoTip text={info} />}
      </div>
      {sub && <div style={{ fontSize: 12, color: C.faint, fontWeight: 500 }}>{sub}</div>}
    </div>
  );

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Dashboard</h2>
            <p>Cross-Broker health &amp; activity · showing <b>{rangeMeta.label.toLowerCase()}</b>.</p>
          </div>
          <div className="actions" style={{ display: "flex", gap: 10, alignItems: "center" }}>
            {/* Date-range filter */}
            <select value={range} onChange={e => setRange(e.target.value)}
              style={{ border: `1px solid ${C.line}`, borderRadius: 9, padding: "8px 12px",
                       fontSize: 13, fontWeight: 600, color: C.ink, background: "#fff", cursor: "pointer" }}>
              {RANGES.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
            <button className="btn" onClick={() => load(range)} disabled={loading}>↻ Refresh</button>
          </div>
        </div>

        {err && <div className="card" style={{ padding: 16, color: C.crit }}>{err}</div>}

        {d && (
          // Keyed on `range` so switching the date filter replays the whole
          // entrance sequence instead of numbers silently swapping in place.
          <div key={range}>
            {/* KPI tiles */}
            <div className="tiles five" style={{ marginBottom: 18 }}>
              {tiles.map((t, i) => (
                <div className="tile kd-in" key={i}
                  style={{ position: "relative", overflow: "hidden", ...stagger(i) }}>
                  <span style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 3, background: t.col }} />
                  <div className="k" style={{ display: "flex", alignItems: "center", gap: 5 }}>
                    {t.k}{(t as any).info && <InfoTip text={(t as any).info} />}
                  </div>
                  <div className="v"><CountUp value={t.v} /></div>
                  <div className="foot" style={{ color: (t as any).up === true ? C.ok : C.muted }}>{t.foot}</div>
                </div>
              ))}
            </div>

            {/* Row: activity area + severity donut */}
            <div style={{ display: "grid", gridTemplateColumns: "1.7fr 1fr", gap: 18, marginBottom: 18 }}>
              <div className="kd-in" style={{ ...card, ...stagger(0, 0, 340) }}>
                {cardHead("Activity — Runs Per Day", `Clean vs exceptions · ${rangeMeta.label.toLowerCase()}`,
                  "Bordereaux processed per day across all brokers. Blue = the output passed every "
                  + "contract rule; amber = at least one exception was raised. “Clean rate” is the share "
                  + "of runs in this range that passed with no exceptions.")}
                <div style={{ display: "flex", gap: 16, marginBottom: 4 }}>
                  <Legend color={C.blue} label="Clean" />
                  <Legend color={C.warn} label="With Exceptions" />
                  {cleanRate != null &&
                    <span style={{ marginLeft: "auto", fontSize: 12, color: C.muted }}>
                      Clean rate <b style={{ color: C.ok }}>{cleanRate}%</b></span>}
                </div>
                <AreaChart series={d.runs_series} />
              </div>
              <div className="kd-in" style={{ ...card, ...stagger(1, 60, 340) }}>
                {cardHead("Exceptions by Severity", rangeMeta.label,
                  "Every validation exception raised in this date range, split by severity. "
                  + "Critical blocks submission and must be resolved; Warning should be reviewed; "
                  + "Info is advisory only.")}
                <Donut unit="exceptions"
                  total={d.exceptions_by_severity.critical + d.exceptions_by_severity.warning}
                  segments={[
                    { label: "Critical", value: d.exceptions_by_severity.critical, color: C.crit },
                    { label: "Warning", value: d.exceptions_by_severity.warning, color: C.warn },
                  ]} />
              </div>
            </div>

            {/* Row: top tenants + users by role + mapping queue */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 18, marginBottom: 18 }}>
              <div className="kd-in" style={{ ...card, ...stagger(2, 60, 340) }}>
                {cardHead("Top Brokers by Volume", `Runs · ${rangeMeta.short}`,
                  "The busiest broker organizations in this date range, ranked by how many bordereaux "
                  + "they processed. The bar is relative to the top broker.")}
                {d.top_tenants.length === 0 ? <div className="empty">No runs in this period.</div> :
                  d.top_tenants.map((t, i) => {
                    const max = Math.max(1, ...d.top_tenants.map(x => x.runs));
                    const pct = (t.runs / max) * 100;
                    const col = [C.blue, C.purple, C.info, C.ok, C.warn, C.slate][i % 6];
                    return (
                      <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, margin: "9px 0" }}
                        title={`${t.name} — ${t.runs} runs`}>
                        <span style={{ width: 88, fontSize: 12.5, color: C.ink, fontWeight: 600,
                          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.name}</span>
                        <div style={{ flex: 1, height: 20, background: "#F0F2F7", borderRadius: 6 }}>
                          <div className="kd-bar" style={{
                            ["--kd-w" as any]: `${pct}%`, height: "100%", background: col,
                            borderRadius: 6, ["--kd-d" as any]: `${520 + i * 70}ms`,
                          }} />
                        </div>
                        <span style={{ width: 26, fontSize: 12, fontWeight: 700, color: C.ink, textAlign: "right" }}>{t.runs}</span>
                      </div>
                    );
                  })}
              </div>
              <div className="kd-in" style={{ ...card, ...stagger(3, 60, 340) }}>
                {cardHead("Users by Role", undefined,
                  "All user accounts split by permission level. Operators process bordereaux; "
                  + "Broker Admins also manage their organization's setup and users; "
                  + "Kavachio Admins are platform staff with cross-broker access.")}
                <Donut unit="users" total={d.users.total} segments={[
                  { label: "Operators", value: d.users.by_role.tenant_user, color: C.blue },
                  { label: "Broker Admins", value: d.users.by_role.tenant_admin, color: C.purple },
                  { label: "Kavachio Admins", value: d.users.by_role.kavachio_admin, color: C.ink },
                ]} />
              </div>
              <div className="kd-in" style={{ ...card, ...stagger(4, 60, 340) }}>
                {cardHead("Data Mapping Queue", "Ops Health",
                  "Work for Kavachio staff: when a broker uploads a file layout we haven't seen, a task "
                  + "is raised to map its columns to the data model. Until it's done, that layout can't "
                  + "be processed automatically.")}
                {[
                  ["Awaiting Review", d.mapping_queue.open, C.warn],
                  ["In Progress", d.mapping_queue.in_progress, C.blue],
                  [`Resolved · ${rangeMeta.short}`, resolvedCount, C.ok],
                  // ["Dismissed", d.mapping_queue.dismissed, C.slate],
                ].map(([label, val, col], i) => (
                  <div key={i} style={{ display: "flex", alignItems: "center", gap: 12, margin: "8px 0" }}>
                    <span className="kd-chip" style={{ width: 30, height: 30, borderRadius: 15,
                      background: `${col}22`, color: col as string, fontWeight: 800, fontSize: 13,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      ...stagger(i, 80, 620) }}>{val as number}</span>
                    <span style={{ fontSize: 13, color: C.ink, fontWeight: 600 }}>{label as string}</span>
                  </div>
                ))}
                <div style={{ borderTop: `1px solid ${C.line}`, marginTop: 10, paddingTop: 10,
                  display: "flex", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 12.5, color: C.muted, display: "flex", alignItems: "center", gap: 4 }}>
                    Avg Turnaround
                    <InfoTip text={`Average time from a mapping task being opened to being marked done, for tasks resolved in the selected date range (${rangeMeta.short}).`} />
                  </span>
                  <span style={{ fontSize: 14, fontWeight: 800, color: C.ink }}>
                    {d.mapping_queue.avg_turnaround_hours == null ? "—" : `${d.mapping_queue.avg_turnaround_hours} hrs`}</span>
                </div>
                <div style={{ marginTop: 8 }}>
                  <Link className="linkish" to="/admin/mapping-tasks">Open the Queue →</Link>
                </div>
              </div>
            </div>

            {/* Tenants overview (full width) */}
            <div className="kd-in" style={{ ...card, ...stagger(5, 60, 340) }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
                <div style={{ fontSize: 15, fontWeight: 700, color: C.ink, display: "flex", alignItems: "center", gap: 5 }}>
                  Brokers Overview
                  <InfoTip text={"Per-broker summary for the selected range. Users = accounts in that "
                    + "organization; Setups = bordereau setups; Runs = bordereaux processed; "
                    + "Clean % = share of those runs that passed with no exceptions."} />
                  <span style={{ fontSize: 12, color: C.faint, fontWeight: 500 }}> · Top 8 by Volume</span></div>
                <Link className="linkish" to="/tenants">View All →</Link>
              </div>
              <div className="tbl-wrap">
                <table>
                  <thead><tr>
                    <th>Broker</th><th className="r">Users</th><th className="r">Setups</th>
                    <th className="r">Runs · {rangeMeta.short}</th><th className="r">Clean %</th><th>Status</th>
                  </tr></thead>
                  <tbody>
                    {d.tenants_table.slice(0, 8).map((t, i) => (
                      <tr key={i} className="kd-tr" style={stagger(i, 45, 720)}>
                        <td><b>{t.name}</b><span className="muted" style={{ marginLeft: 6 }}>{t.code}</span></td>
                        <td className="r">{t.users}</td>
                        <td className="r">{t.setups}</td>
                        <td className="r"><b>{t.runs}</b></td>
                        <td className="r">{t.clean_pct == null ? "—" : `${t.clean_pct}%`}</td>
                        <td>{statusBadge(t.status)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: C.muted }}>
      <span style={{ width: 11, height: 11, borderRadius: 3, background: color }} />{label}
    </span>
  );
}
