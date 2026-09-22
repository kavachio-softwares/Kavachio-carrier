// Kavachio platform-admin dashboard — every carrier and broker in one place.
// Data: GET /dashboard/platform?range=&carrier=&broker= (kavachio_admin only).
//
// Two halves, in this order on purpose:
//   1. Who is on Kavachio — the four seats. Point-in-time and never narrowed,
//      so it sits ABOVE the filter row: a filter only ever scopes the cards
//      below it.
//   2. Bordereau work — runs, open exceptions, overdue files, signatures, the
//      carriers table and the mapping queue, all scoped by period / carrier /
//      broker from the one filter row.
// Open exceptions are counted the way Exception Triage counts them (decisions
// matched to each current file), so every number here equals the screen it
// leads to. Layout follows figma design/kavachio_admin_dashboard_v2.jpg.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle, Briefcase, Building2, CalendarX, CheckCircle2, PenLine, User, Users, Zap,
} from "lucide-react";
import { api } from "../api/client";
import { InfoTip } from "../components/InfoTip";
import { RankedBars, RunTrend } from "../components/BrokerCharts";
import { ChartCard, StatCard } from "../components/StatCard";

// Date-range presets (value → label). `range` is passed to the API, which
// windows every time-based metric.
const RANGES: { value: string; label: string }[] = [
  { value: "1d", label: "Today" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "90d", label: "90 days" },
  { value: "ytd", label: "This year" },
  { value: "all", label: "All time" },
];

// Severity colours — validated as a set with the dataviz palette checker
// (red / amber / blue all pass, including colour-blind separation).
const SEV = { critical: "#D32F45", warning: "#C77A12", info: "#3F6FD1" };
const HUE = "#0B8FA0";            // the chart teal used for single-hue bars

type Seat = { total: number; signed_up: number; not_signed_up: number };
type Named = { id: number; name: string };

type Platform = {
  range: string; range_days: number;
  filters: { carrier: number | null; broker: number | null };
  filter_options: { carriers: Named[]; brokers: Named[] };
  seats: {
    carrier_admins: Seat; carrier_users: Seat; broker_admins: Seat; broker_users: Seat;
    carrier_companies: number; broker_companies: number
  };
  runs: {
    window_count: number; prev_count: number; delta_pct: number | null;
    clean_rate: number | null; clean_count: number
  };
  runs_series: { date: string; total: number; exceptions: number; not_validated?: number }[];
  open_exceptions: {
    total: number; critical: number; warning: number; info: number; waiting_over_7d: number;
    put_right: { fixed: number; approved: number; dismissed: number; rejected: number; total: number };
    by_carrier: (Named & { open: number })[]; by_broker: (Named & { open: number })[];
    carriers_clear: number; brokers_clear: number;
  };
  overdue: { total: number; brokers: number; carriers: number };
  signatures: { awaiting: number; over_7d: number };
  tenants_table: {
    tenant_id: number; code: string; name: string; users: number; brokers: number;
    programs: number; runs: number; clean_pct: number | null;
    open_exceptions: number; overdue: number; status: string
  }[];
  mapping_queue: {
    open: number; in_progress: number; resolved_window: number;
    oldest_open_days: number | null
  };
};

const nf = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString());
/** The viewer's zone, so a bar's "7 Sep" is the 7 Sep they see everywhere else. */
const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
                    catch { return "UTC"; } })();
/** "2026-09-07" → "Mon 7 Sep 2026", read as a calendar day (no zone shift). */
function longDay(iso: string) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
const pct = (a: number, b: number) => (b ? Math.round((a / b) * 100) : 0);
const plural = (n: number, one: string, many: string) => `${nf(n)} ${n === 1 ? one : many}`;

function statusBadge(s: string) {
  const map: Record<string, [string, string]> = {
    active: ["b-ok", "Active"], invited: ["b-warn", "Invited"], inactive: ["b-mut", "Inactive"],
  };
  const [cls, label] = map[s] ?? ["b-mut", s];
  return <span className={`badge ${cls}`}>{label}</span>;
}

/** Small uppercase heading over a group of tiles. */
function Section({ title, right }: { title: string; right?: React.ReactNode }) {
  return (
    <div style={{
      display: "flex", justifyContent: "space-between", alignItems: "baseline",
      margin: "0 0 10px"
    }}>
      <div style={{
        fontSize: 12, fontWeight: 700, letterSpacing: ".08em",
        textTransform: "uppercase", color: "var(--p-faint)"
      }}>{title}</div>
      {right}
    </div>
  );
}

/** How many of a seat have accepted their invitation — a bar plus the words. */
function SignedUp({ s }: { s: Seat }) {
  return (
    <div>
      <div style={{ height: 6, borderRadius: 3, background: "#EEF1F5", overflow: "hidden" }}>
        <div style={{
          height: "100%", borderRadius: 3, background: HUE,
          width: `${pct(s.signed_up, s.total)}%`
        }} />
      </div>
      <div style={{
        display: "flex", justifyContent: "space-between", fontSize: 12,
        color: "var(--p-muted)", marginTop: 6
      }}>
        <span>{nf(s.signed_up)} signed up</span>
        {s.not_signed_up > 0
          ? <span><b style={{ color: "#A55F08" }}>{nf(s.not_signed_up)}</b> not signed up yet</span>
          : <span>all signed up</span>}
      </div>
    </div>
  );
}

/** A small number-over-label block (put-right split, mapping queue). */
function Chip({ v, k }: { v: number | string; k: string }) {
  return (
    <div style={{
      flex: 1, background: "var(--p-surface-2, #F6F8FB)", borderRadius: 10,
      padding: "9px 11px", minWidth: 0
    }}>
      <div style={{ fontSize: 18, fontWeight: 700, color: "var(--p-text)" }}>{v}</div>
      <div style={{ fontSize: 12, color: "var(--p-muted)" }}>{k}</div>
    </div>
  );
}

type DayRun = {
  export_id: number; source_upload_id: number | null; run_at: string | null;
  result: "clean" | "flagged" | "not_checked"; exceptions: number; current: boolean;
  file: string; programme: string | null;
  carrier: { id: number; name: string; code: string | null };
  broker: { id: number; name: string } | null;
};
type DayRuns = {
  day: string; items: DayRun[];
  totals: { runs: number; clean: number; flagged: number; not_checked: number };
  carriers: { id: number; name: string; code: string | null; runs: number }[];
};

/** The files behind one bar of Runs per Day — every carrier's, in the
 *  dashboard's current carrier/broker scope, counted exactly as the bar is. */
function DayPanel({ day, carrier, broker, onClose }: {
  day: string | null; carrier: number | ""; broker: number | ""; onClose: () => void;
}) {
  const [data, setData] = useState<DayRuns | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!day) return;
    setData(null); setErr(null);
    api.get<DayRuns>("/dashboard/platform/runs", {
      params: { day, tz: TZ, carrier: carrier || undefined, broker: broker || undefined },
    }).then(a => setData(a.data)).catch(() => setErr("Could not load the files for this day."));
  }, [day, carrier, broker]);
  useEffect(() => {
    if (!day) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [day, onClose]);

  const open = !!day;
  const t = data?.totals;
  const result = (x: DayRun) => {
    if (!x.current) return <span className="badge b-mut">Replaced by a re-run</span>;
    if (x.result === "clean") return <span className="badge b-ok"><span className="d" />Clean</span>;
    if (x.result === "not_checked") return <span className="badge b-mut">Not checked</span>;
    return <span className="badge b-warn"><span className="d" />{nf(x.exceptions)} exception{x.exceptions === 1 ? "" : "s"}</span>;
  };
  // The app's tables centre their cells; a list of files reads down the left edge.
  const L: React.CSSProperties = { textAlign: "left" };
  const triage = (x: DayRun) =>
    `/uploads/${x.source_upload_id ?? x.export_id}/exceptions?download=${x.export_id}&from=admin`;

  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h">
          <div>
            <h4>Files processed on {day ? longDay(day) : ""}</h4>
            <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>
              {t ? <>{plural(t.runs, "run", "runs")} · <b style={{ color: "var(--p-ok)" }}>{nf(t.clean)} clean</b>
                {" · "}<b style={{ color: "var(--p-warn)" }}>{nf(t.flagged)} flagged</b>
                {t.not_checked > 0 && <> · {nf(t.not_checked)} not checked</>}</> : "\u00a0"}
            </div>
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b" style={{ padding: 0 }}>
          {err && <div className="note warn" style={{ margin: 16 }}>{err}</div>}
          {!data && !err && <div className="muted" style={{ padding: 20 }}>Loading…</div>}
          {data && data.items.length === 0 && <div className="empty" style={{ padding: 20 }}>No files were processed on this day.</div>}
          {data && data.items.length > 0 && (
            <div className="tbl-wrap">
              <table>
                <thead><tr>{["Time", "Carrier ← Broker · File", "Result"].map(h =>
                  <th key={h} style={L}>{h}</th>)}</tr></thead>
                <tbody>
                  {data.items.map(x => (
                    <tr key={x.export_id} style={x.current ? undefined : { opacity: 0.6 }}>
                      <td className="muted" style={{ ...L, whiteSpace: "nowrap", fontSize: 12.5 }}>
                        {x.run_at ? new Date(x.run_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}</td>
                      <td style={L}>
                        <div><b>{x.carrier.name}</b>{x.broker && <span className="muted"> ← {x.broker.name}</span>}</div>
                        <div className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>{x.file}</div>
                        {x.programme && <div className="muted" style={{ fontSize: 12 }}>{x.programme}</div>}
                      </td>
                      <td style={{ ...L, whiteSpace: "nowrap" }}>
                        {result(x)}
                        {/* A replaced run is history: its problems are worked on the
                            re-run that replaced it, which is listed as its own row. */}
                        {x.result === "flagged" && x.current && (
                          <div style={{ marginTop: 6 }}>
                            <Link className="linkish" style={{ fontSize: 12.5 }} to={triage(x)}>See exceptions →</Link>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        {data && data.carriers.some(c => c.code) && (
          <div className="drawer-f">
            {data.carriers.filter(c => c.code).map(c => (
              <Link key={c.id} className="btn sm" to={`/tenants/${c.code}?tab=runs&from=${data.day}&to=${data.day}`}>
                Open {c.name}'s files for this day →
              </Link>
            ))}
          </div>
        )}
      </aside>
    </>
  );
}

// ---------- page ----------
export default function KavachioAdminDashboard() {
  const [range, setRange] = useState("30d");
  const [carrier, setCarrier] = useState<number | "">("");
  const [broker, setBroker] = useState<number | "">("");
  const [d, setD] = useState<Platform | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [day, setDay] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api.get<Platform>("/dashboard/platform", {
      params: { range, carrier: carrier || undefined, broker: broker || undefined, tz: TZ },
    })
      .then(a => {
        setD(a.data); setErr(null);
        // The broker list follows the chosen carrier. A broker picked earlier
        // that this carrier does not work with would only zero every card, so
        // it drops back to "All brokers" (which reloads once more).
        if (broker && !a.data.filter_options.brokers.some(b => b.id === broker)) setBroker("");
      })
      .catch(() => setErr("Could not load platform metrics."))
      .finally(() => setLoading(false));
  };
  useEffect(load, [range, carrier, broker]);

  const periodWords = range === "all" ? "all time"
    : range === "ytd" ? "this year"
      : range === "1d" ? "today" : `the last ${d?.range_days ?? ""} days`;

  const grid = (min: number): React.CSSProperties => ({
    display: "grid", gridTemplateColumns: `repeat(auto-fit, minmax(${min}px, 1fr))`,
    gap: 18, marginBottom: 24,
  });

  const selectStyle: React.CSSProperties = {
    border: "1px solid var(--p-border-2)", borderRadius: 9, padding: "7px 10px", fontSize: 13,
    fontWeight: 500, color: "var(--p-text)", background: "var(--p-surface)", maxWidth: 220,
  };

  const head = (
    <div className="page-head">
      <div className="t">
        <h2>Dashboard</h2>
        {/* <p>Every carrier and broker on Kavachio, in one place.</p> */}
      </div>
      <div className="actions">
        <button className="btn" onClick={load} disabled={loading}>↻ Refresh</button>
      </div>
    </div>
  );

  if (!d) {
    return (
      <div className="proto"><div className="view full">
        {head}
        {err ? <div className="note warn" style={{ maxWidth: 560 }}>{err}</div>
          : <div className="muted">Loading…</div>}
      </div></div>
    );
  }

  const st = d.seats;
  const ox = d.open_exceptions;
  const brokersWithOpen = ox.by_broker.length;
  const codeOf: Record<number, string> = Object.fromEntries(
    d.tenants_table.map(t => [t.tenant_id, t.code]));

  // Runs per day, in the three outcomes the broker dashboards already use.
  const trend = d.runs_series.map(p => {
    const nc = p.not_validated ?? 0;
    return {
      date: p.date, clean: Math.max(0, p.total - p.exceptions - nc),
      flagged: p.exceptions, not_checked: nc
    };
  });

  const sevRows = [
    { k: "Critical", sub: "Stops the file going out.", v: ox.critical, c: SEV.critical },
    { k: "Warning", sub: "Should be checked.", v: ox.warning, c: SEV.warning },
    { k: "Info", sub: "For awareness only.", v: ox.info, c: SEV.info },
  ];
  const pr = ox.put_right;

  return (
    <div className="proto">
      <div className="view full">
        {head}
        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

        {/* ===== Who is on Kavachio (never filtered) ===== */}
        <Section title=""
          right={<Link className="linkish" to="/admin/users">Open Users &amp; Roles →</Link>} />
        <div style={grid(220)}>
          <StatCard title="Carrier Admins" value={nf(st.carrier_admins.total)} icon={Building2}
            info={`Across ${plural(st.carrier_companies, "carrier company", "carrier companies")} — one admin per carrier.`}
            footer={<SignedUp s={st.carrier_admins} />} />
          <StatCard title="Carrier Users" value={nf(st.carrier_users.total)} icon={Users}
            info="Added by their carrier admins."
            footer={<SignedUp s={st.carrier_users} />} />
          <StatCard title="Broker Admins" value={nf(st.broker_admins.total)} icon={Briefcase}
            info={`Across ${plural(st.broker_companies, "broker company", "broker companies")} — one admin per broker.`}
            footer={<SignedUp s={st.broker_admins} />} />
          <StatCard title="Broker Users" value={nf(st.broker_users.total)} icon={User}
            info="Added by their broker admins."
            footer={<SignedUp s={st.broker_users} />} />
        </div>

        {/* ===== Bordereau work — one filter row scopes everything below ===== */}
        <Section title={`Bordereau work · ${periodWords}`} />
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginBottom: 16 }}>
          <div className="seg">
            {RANGES.map(r => (
              <button key={r.value} type="button" className={r.value === range ? "on" : ""}
                onClick={() => setRange(r.value)}>{r.label}</button>
            ))}
          </div>
          <select value={carrier} onChange={e => setCarrier(e.target.value ? Number(e.target.value) : "")}
            aria-label="Carrier" style={selectStyle}>
            <option value="">All carriers</option>
            {d.filter_options.carriers.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <select value={broker} onChange={e => setBroker(e.target.value ? Number(e.target.value) : "")}
            aria-label="Broker" style={selectStyle}>
            <option value="">All brokers</option>
            {d.filter_options.brokers.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
          </select>
          {(carrier || broker) && (
            <button type="button" className="linkish"
              style={{ background: "none", border: 0, cursor: "pointer", fontSize: 13 }}
              onClick={() => { setCarrier(""); setBroker(""); }}>Clear</button>
          )}
          {loading && <span className="muted" style={{ fontSize: 12.5 }}>Updating…</span>}
        </div>

        <div style={grid(185)}>
          <StatCard title="Files Processed" value={nf(d.runs.window_count)} icon={Zap}
            trend={d.runs.delta_pct == null ? undefined
              : `${d.runs.delta_pct >= 0 ? "+" : ""}${d.runs.delta_pct}%`}
            subtitle={d.runs.delta_pct == null ? "no runs the period before"
              : `vs ${nf(d.runs.prev_count)} the period before`} />
          <StatCard title="Clean Runs" icon={CheckCircle2}
            value={d.runs.clean_rate == null ? "—" : `${d.runs.clean_rate}%`}
            subtitle={`${nf(d.runs.clean_count)} of ${nf(d.runs.window_count)} runs`} />
          <StatCard title="Open Exceptions" value={nf(ox.total)} icon={AlertTriangle}
            info="Still waiting on files run in this period. Pick All time for the whole backlog."
            tone={ox.critical > 0 ? "alert" : undefined}
            subtitle={ox.total ? `across ${plural(brokersWithOpen, "broker", "brokers")}` : "nothing waiting"} />
          <StatCard title="Overdue Bordereaux" value={nf(d.overdue.total)} icon={CalendarX}
            info="Bordereaux that fell due in this period and still have not arrived. Pick All time for every missed deadline."
            subtitle={d.overdue.total ? `from ${plural(d.overdue.brokers, "broker", "brokers")}` : "nothing overdue"} />
          <StatCard title="Awaiting Signature" value={nf(d.signatures.awaiting)} icon={PenLine}
            info="Contracts sent for signing in this period and not yet signed by everyone."
            subtitle={d.signatures.over_7d
              ? `${nf(d.signatures.over_7d)} waiting over 7 days` : "contracts out for signing"} />
        </div>

        {/* ===== Runs per day + open exceptions by severity ===== */}
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0,2fr) minmax(0,1fr)", gap: 18, marginBottom: 18 }}>
          <ChartCard title="Runs Overview (Daily)"
            info={<InfoTip text={"Bordereaux processed per day. Clean = passed every check; "
              + "flagged = at least one exception was raised; not checked = the checks could not run. "
              + "Click a day to see its files."} />}>
            <RunTrend data={trend} onDayClick={setDay} />
          </ChartCard>

          <ChartCard title="Open Exceptions by Severity"
            info={<InfoTip text={"Exceptions still waiting on files run in this period (each file's latest "
              + "run), counted the way the Exception Triage screen counts them. \"Put right\" = decisions made "
              + "in this period. Pick All time for the whole backlog."} />}>
            {ox.total === 0 ? <div className="empty">Nothing is waiting to be reviewed.</div> : (
              <>
                <div style={{ display: "flex", gap: 2, height: 14, marginBottom: 10 }}>
                  {sevRows.filter(r => r.v > 0).map((r, i, a) => (
                    <span key={r.k} title={`${r.k}: ${nf(r.v)}`} style={{
                      width: `${(r.v / ox.total) * 100}%`, background: r.c,
                      borderRadius: `${i === 0 ? 4 : 0}px ${i === a.length - 1 ? 4 : 0}px ${i === a.length - 1 ? 4 : 0}px ${i === 0 ? 4 : 0}px`,
                    }} />
                  ))}
                </div>
                {sevRows.map(r => (
                  <div key={r.k} style={{ display: "flex", alignItems: "center", gap: 9, padding: "6px 0" }}>
                    <span style={{ width: 11, height: 11, borderRadius: 3, background: r.c, flex: "none" }} />
                    <div style={{ fontSize: 13, fontWeight: 600, display: "flex", alignItems: "center", gap: 5 }}>
                      {r.k}<InfoTip text={r.sub} />
                    </div>
                    <span style={{ marginLeft: "auto", fontSize: 13, fontWeight: 700 }}>{nf(r.v)}</span>
                    <span style={{ width: 36, textAlign: "right", fontSize: 12, color: "var(--p-faint)" }}>
                      {pct(r.v, ox.total)}%</span>
                  </div>
                ))}
              </>
            )}
            <div style={{ borderTop: "1px solid var(--p-border)", margin: "12px 0 10px" }} />
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "baseline",
              fontSize: 13, color: "var(--p-muted)"
            }}>
              <span>Put right in {periodWords}</span>
              <b style={{ fontSize: 16, color: "var(--p-text)" }}>{nf(pr.total)}</b>
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <Chip v={nf(pr.fixed)} k="Fixed" />
              <Chip v={nf(pr.approved)} k="Approved as is" />
              <Chip v={nf(pr.dismissed)} k="Dismissed" />
              {pr.rejected > 0 && <Chip v={nf(pr.rejected)} k="Rejected" />}
            </div>
            {ox.waiting_over_7d > 0 && (
              <div style={{ fontSize: 12.5, color: "var(--p-muted)", marginTop: 12 }}>
                <b style={{ color: "var(--p-text)" }}>{nf(ox.waiting_over_7d)}</b> have waited longer than 7 days
              </div>
            )}
          </ChartCard>
        </div>

        {/* ===== Where the open exceptions are + the Kavachio team's own queue ===== */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 18, marginBottom: 18 }}>
          <ChartCard title="Exceptions by Carrier"
            info={<InfoTip text="Open exceptions on files run in this period, per carrier, most first. A carrier opens its Recent File Submissions." />}>
            <RankedBars cap={6} unit="open"
              rows={ox.by_carrier.map(r => ({ id: r.id, name: r.name, value: r.open }))}
              empty="Nothing is open at any carrier."
              linkTo={r => codeOf[r.id] ? `/tenants/${codeOf[r.id]}?tab=runs` : "/tenants"} />
            {ox.carriers_clear > 0 && (
              <div className="muted" style={{ fontSize: 12.5, marginTop: 12 }}>
                {plural(ox.carriers_clear, "other carrier has", "other carriers have")} files with nothing open.
              </div>
            )}
          </ChartCard>
          <ChartCard title="Exceptions by Broker"
            info={<InfoTip text="Open exceptions on files run in this period, per broker, most first — across every carrier they send to." />}>
            <RankedBars cap={6} unit="open"
              rows={ox.by_broker.map(r => ({ id: r.id, name: r.name, value: r.open }))}
              empty="Nothing is open for any broker." />
            {ox.brokers_clear > 0 && (
              <div className="muted" style={{ fontSize: 12.5, marginTop: 12 }}>
                {plural(ox.brokers_clear, "other broker has", "other brokers have")} files with nothing open.
              </div>
            )}
          </ChartCard>
          <ChartCard title="Data Mapping Queue"
            info={<InfoTip text={"Work for the Kavachio team: a file layout nobody has seen before waits here "
              + "until its columns are mapped. Until then that layout cannot be processed automatically. "
              + "Waiting / in progress = raised in this period; finished = finished in it."} />}>
            <div style={{ display: "flex", gap: 8 }}>
              <Chip v={nf(d.mapping_queue.open)} k="Waiting" />
              <Chip v={nf(d.mapping_queue.in_progress)} k="In progress" />
              <Chip v={nf(d.mapping_queue.resolved_window)} k="Finished" />
            </div>
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "center",
              marginTop: "auto", paddingTop: 14, fontSize: 12.5, color: "var(--p-muted)"
            }}>
              <span>{d.mapping_queue.oldest_open_days == null ? "Nothing waiting"
                : `Oldest waiting: ${plural(d.mapping_queue.oldest_open_days, "day", "days")}`}</span>
              <Link className="linkish" to="/admin/mapping-tasks">Open the queue →</Link>
            </div>
          </ChartCard>
        </div>

        <DayPanel day={day} carrier={carrier} broker={broker} onClose={() => setDay(null)} />
      </div>
    </div>
  );
}
