import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ComposedChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend } from "recharts";
import { LayoutDashboard, Layers, Users, FileText, AlertCircle, Activity, Clock } from "lucide-react";
import { api } from "../api/client";
import { currentMga, getUser, isKavachioAdmin, userRole, ROLE_LABEL, type Role } from "../auth";
import { canAccessPath } from "../access";

import { getCalendar, type CalendarStatus } from "../api/calendar";
import { getBrokersPaged, getHierarchy } from "../api/hierarchy";
import { listContractsPaged } from "../api/contractRecord";
import { useCarrierSeat } from "../hooks/useCarrierSeat";
import { listArrivals, type Arrival } from "../api/intake";
import { InfoTip } from "../components/InfoTip";
import { StatCard } from "../components/StatCard";


type Stats = {
  uploads_today: number; uploads_total: number; open_bdx_cycles: number;
  // This organisation's carrier users (not the carrier admin themselves),
  // invited ones included. Never the brokers' people.
  // Sent to the carrier admin only; null for a carrier user.
  users_total?: number | null; users_invited?: number | null;
  // Sent to a carrier user only: the broker companies THEY invited.
  my_brokers?: number | null; my_brokers_pending?: number | null;
  parties_in_directory: number; pending_exceptions: number;
  ai_cache_hit_rate: number | null;
  // --- extended fields for the prototype KPIs (optional until the API adds them) ---
  exception_runs?: number;                                       // # runs with open exceptions
  exceptions_by_severity?: { critical: number; warning: number; info: number };
  runs_this_week?: number;
  runs_by_day_status?: { clean: number; flagged: number; resolved: number }[];
  active_setups?: number;
  active_setup_carriers?: number;
  mapping_tasks_open?: number;                                   // kavachio_admin tile
  pending_signatures?: number;
  completed_signatures?: number;
  avg_turnaround_min?: number | null;                           // tenant/operator tile
};
// A "run" = a generated output export (carries the validation result).
type Run = {
  id: number; filename: string; template_name: string | null;
  policy_count: number; exception_count: number;
  status: string; created_at: string | null;
  source_upload_id: number | null;
};
const SUBTITLE: Record<Role, string> = {
  carrier_admin: "What needs you today, and your most recent bordereau runs.",
  kavachio_admin: "Platform activity and your most recent bordereau runs.",
  // Broker seats do not have a carrier Home yet — the API refuses a broker
  // token on every carrier route, so these are placeholders, not promises.
  broker_admin: "Your contracts and the files you have sent.",
  operator: "The files you have sent, and anything that needs fixing.",
};

export default function Home() {
  const mga = currentMga();
  const nav = useNavigate();
  const user = getUser();
  const role = userRole() ?? "operator";
  // Same role, two seats at a carrier: only the owner is the Carrier Admin.
  const seat = useCarrierSeat();
  const [stats, setStats] = useState<Stats | null>(null);
  // Whether this tenant still needs first-time setup (carrier + Bordereau).
  const [needsSetup, setNeedsSetup] = useState(false);
  // The dashboard shows a small snapshot; the full, filterable history lives
  // on the dedicated Run history page (/runs).
  const RUNS_PAGE = 5;
  const [runs, setRuns] = useState<Run[]>([]);
  // Group 3: deadline counts for the "Deadlines" tile (own submission calendar).
  const [calCounts, setCalCounts] = useState<Partial<Record<CalendarStatus, number>>>({});
  // "How big is my book" — the two directory sizes, each read from the SAME
  // endpoint its own screen reads. /dashboard/stats already carries a programme
  // count (`open_bdx_cycles`) but it counts only app-managed ACTIVE ones, so a
  // tile fed from it would disagree with the Programmes screen, which lists the
  // tenant's programmes unfiltered. The broker directory is a union (on a
  // programme + created here + invited + invitation still pending) that only
  // /brokers assembles, so re-deriving it anywhere else would drift from the
  // "N parties" the Party screen prints.
  const [progCount, setProgCount] = useState<number | null>(null);
  const [partyCount, setPartyCount] = useState<number | null>(null);
  const [contractCount, setContractCount] = useState<number | null>(null);
  // Files no longer has a sidebar entry — the dashboard is its way in. A short
  // snapshot of the latest arrivals; the counts, filters and decisions all
  // stay on /files, so they are not repeated here.
  const FILES_PAGE = 5;
  const [arrivals, setArrivals] = useState<Arrival[] | null>(null);

  useEffect(() => {
    api.get<Stats>(`/dashboard/stats`, { params: { mga } }).then(r => setStats(r.data));
  }, [mga]);

  useEffect(() => {
    getCalendar().then(c => setCalCounts(c.counts ?? {})).catch(() => setCalCounts({}));
  }, [mga]);

  // Both directories are carrier-scoped. A broker seat carries no tenant, so
  // these routes answer "no tenant bound to this user" for them — don't ask.
  const carrierSeat = role === "carrier_admin" || role === "kavachio_admin";
  // Process Bordereau follows the route rules (access.ts), so its button and
  // links show exactly to the seats that may open it.
  const canProcess = canAccessPath("/direct");

  useEffect(() => {
    if (!carrierSeat) return;
    getHierarchy()
      .then(h => setProgCount(h.programmes?.length ?? 0))
      .catch(() => setProgCount(null));
    // page_size 1 because only `total` is wanted — it (and `stranded`) are
    // counted over the whole directory server-side, not over the page, so the
    // smallest possible page still yields the real figure.
    // `mine`: the same list the Party screen shows — for a carrier user, the
    // broker companies they invited.
    getBrokersPaged({ page: 1, page_size: 1, mine: true })
      .then(r => setPartyCount(r.total))
      .catch(() => setPartyCount(null));
    // Same trick, same reason: sent with no filters so `total` is the carrier's
    // whole book — which is the unfiltered figure the Contracts screen prints.
    listContractsPaged({ page: 1, page_size: 1 })
      .then(r => setContractCount(r.total))
      .catch(() => setContractCount(null));
  }, [mga, carrierSeat]);

  // /files is carrier-only (ROUTE_ACCESS), so only a carrier seat that can open
  // it gets the card — anyone else would be shown links that bounce them back.
  const showFiles = canAccessPath("/files") && !isKavachioAdmin();
  useEffect(() => {
    if (!showFiles) return;
    listArrivals(FILES_PAGE)
      .then(r => setArrivals(r.rows))
      .catch(() => setArrivals([]));
  }, [mga, showFiles]);

  // Can the operator actually work yet? That hinges on there being an approved
  // Bordereau Setup to process against (`bordereau_ready`) — NOT on the full
  // onboarding wizard, which also demands admin-only cosmetics like tenant
  // currency. A tenant with carriers + an approved setup is workable even if the
  // admin hasn't filled every org field.
  useEffect(() => {
    api.get<{ bordereau_ready: boolean }>(`/onboarding/status`, { params: { mga } })
      .then(r => setNeedsSetup(!r.data?.bordereau_ready))
      .catch(() => setNeedsSetup(false));
  }, [mga]);

  // Latest runs only (newest first) — the dashboard is a snapshot, not the
  // archive. "View all" links to /runs for the full searchable history.
  useEffect(() => {
    api.get<Run[]>(`/export/downloads`, { params: { mga, limit: RUNS_PAGE } })
      .then(r => setRuns(r.data))
      .catch(() => setRuns([]));
  }, [mga]);

  const fmt = (v: number | null | undefined) => (v == null ? "—" : v);
  // A not-validated run is never clean: its checks did not run (it carries one
  // notice entry, so exception_count > 0 already sends it to triage).
  const runNotValidated = (r: Run) => r.status === "not_validated";
  const runHasExc = (r: Run) =>
    r.status === "has_exceptions" || runNotValidated(r) || r.exception_count > 0;
  // In download mode UploadExceptions loads by `download` id; the uploadId in the
  // path is only used for link-building, so 0 is a safe placeholder when absent.
  const goTriage = (r: Run) =>
    nav(`/uploads/${r.source_upload_id ?? 0}/exceptions?download=${r.id}&from=home`);
  // "Open triage" (KPI tile) — jump to the most recent run that has exceptions,
  // else the most recent run (renders the exceptions screen with a blank listing).
  const latestExcRun = runs.find(runHasExc);
  const openTriage = () =>
    latestExcRun ? goTriage(latestExcRun)
      : runs[0] ? goTriage(runs[0])
        : nav("/uploads/0/exceptions?from=home");

  const sev = stats?.exceptions_by_severity;

  const pieData = [
    { name: "Critical", value: sev?.critical || 0, color: "#ef4444" },
    { name: "Warning", value: sev?.warning || 0, color: "#f59e0b" },
    { name: "Info", value: sev?.info || 0, color: "#3b82f6" },
  ].filter(d => d.value > 0);

  if (pieData.length === 0 && stats?.pending_exceptions) {
    pieData.push({ name: "Uncategorized", value: stats.pending_exceptions, color: "#94a3b8" });
  }

  // Only a carrier admin can run the org/carrier/Bordereau setup. Until it is
  // done, anyone else gets a single notice instead of a dashboard with nothing
  // behind it.
  if (needsSetup && role !== "carrier_admin" && role !== "kavachio_admin") {
    return (
      <div className="proto">
        <div className="view full">
          <div className="page-head">
            <div className="t"><h2>Dashboard</h2></div>
          </div>
          <div className="card" style={{ padding: 20 }}>
            <p style={{ margin: 0, color: "var(--p-muted)" }}>
              This organization isn’t set up yet — the carrier and Bordereau Setup
              is still pending. Please ask your tenant admin to complete it.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Dashboard</h2>
            <p>{SUBTITLE[role]}</p>
          </div>
          {canProcess && (
            <div className="actions">
              <Link className="btn pri" to="/direct">＋ Process Bordereaux</Link>
            </div>
          )}
        </div>

        {/* KPI tiles — wired to /dashboard/stats (see API notes for the new fields).

            ONE grid for all of them, so every tile is the same width: four to a
            row, which lands the book counts on the first row beside Active
            Setups and the three operational tiles on the second. Two separate
            grids sized the numbers differently row to row, which read as two
            unrelated components rather than one panel. */}
        <div style={{ display: "grid", gridTemplateColumns: seat === "user" ? "repeat(3, 1fr)" : (role === "kavachio_admin" ? "repeat(4, 1fr)" : "repeat(5, 1fr)"), gap: 20, marginBottom: 24 }}>
          {seat !== "user" && (
            <StatCard
              title="Active Setups" value={fmt(stats?.active_setups ?? stats?.open_bdx_cycles)}
              icon={LayoutDashboard} trend="+5%" subtitle={stats?.active_setup_carriers != null ? `across ${stats.active_setup_carriers} carriers` : undefined}
            />
          )}

          {carrierSeat && (
            <>
              <StatCard title="Programmes" value={fmt(progCount)} icon={Layers} onClick={() => nav("/programs")} subtitle="Active" />

              {seat !== "user" && (
                <StatCard title="Parties" value={fmt(partyCount)} icon={Users} onClick={() => nav("/brokers")} subtitle="Entities" />
              )}

              <StatCard title="Contracts" value={fmt(contractCount)} icon={FileText} onClick={() => nav("/contracts")} subtitle="Executing" />

              {seat !== "user" && (
                stats?.my_brokers != null ? (
                  <StatCard title="Your Broker Companies" value={fmt(stats.my_brokers)} icon={Users} onClick={() => nav("/users")} subtitle={stats.my_brokers_pending ? `${stats.my_brokers_pending} not accepted yet` : undefined} />
                ) : (
                  <StatCard title="Carrier Users" value={fmt(stats?.users_total)} icon={Users} onClick={() => nav("/users")} trend="+3" subtitle={stats?.users_invited ? `${stats.users_invited} not signed up yet` : undefined} />
                )
              )}
            </>
          )}

          <StatCard
            title="Exceptions to Review" value={fmt(stats?.pending_exceptions)}
            icon={AlertCircle} tone="alert" subtitle="Alert"
          />

          <StatCard title="Runs This Week" value={fmt(stats?.runs_this_week)} icon={Activity} trend="+12%" />

          {role === "kavachio_admin" ? (
            <StatCard title="Mapping Tasks" value={fmt(stats?.mapping_tasks_open)} icon={Clock} onClick={() => nav("/admin/mapping-tasks")} />
          ) : (
            <StatCard title="Avg Turnaround Time" value={`${stats?.avg_turnaround_min == null ? "—" : stats.avg_turnaround_min}`} icon={Clock} trend="-8%" subtitle="Minutes per file" />
          )}

          <StatCard 
            title="Pending Signatures" 
            value={fmt(stats?.pending_signatures)} 
            icon={FileText} 
            tone={stats?.pending_signatures ? "alert" : undefined} 
            subtitle={`${stats?.completed_signatures ?? 0} Completed`} 
            onClick={() => nav("/contracts")} 
          />
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 24, marginBottom: 24 }}>
          {/* Processing Volume Chart */}
          <div className="card" style={{ padding: "24px 20px", display: "flex", flexDirection: "column" }}>
            <div className="card-h" style={{ marginBottom: 20 }}>
              <h3>Bordereau Status</h3>
              <InfoTip text="Clean vs Flagged runs and Resolved exceptions over the last 7 days." />
            </div>
            <div style={{ width: "100%", height: 260 }}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart
                  data={(stats?.runs_by_day_status ?? []).map((dayData, i) => {
                    const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
                    return { 
                        name: days[i] || `Day ${i + 1}`, 
                        clean: dayData.clean || 0,
                        flagged: dayData.flagged || 0,
                        resolved: dayData.resolved || 0
                    };
                  })}
                  margin={{ top: 10, right: 10, left: -20, bottom: 0 }}
                  barSize={32}
                >
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                  <XAxis dataKey="name" axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: "#64748b" }} dy={10} />
                  <YAxis yAxisId="left" allowDecimals={false} axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: "#64748b" }} />
                  <YAxis yAxisId="right" orientation="right" allowDecimals={false} axisLine={false} tickLine={false} tick={{ fontSize: 12, fill: "#64748b" }} />
                  <Tooltip
                    formatter={(value, name) => [
                      value as number,
                      name === 'clean' ? 'Clean Runs' : name === 'flagged' ? 'Flagged Runs' : 'Resolved Exceptions'
                    ]}
                    labelFormatter={(label) => `${label}`}
                    contentStyle={{ borderRadius: 8, border: "1px solid #e2e8f0", boxShadow: "0 10px 15px -3px rgb(0 0 0 / 0.1)" }}
                    itemStyle={{ color: "#0f172a", fontWeight: 600, textTransform: "capitalize" }}
                    labelStyle={{ color: "#64748b", marginBottom: 4 }}
                    cursor={{ fill: '#f1f5f9' }}
                  />
                  <Legend verticalAlign="bottom" height={36} iconType="circle" wrapperStyle={{ fontSize: 13, color: "#64748b", textTransform: "capitalize" }} />
                  <Bar yAxisId="left" dataKey="clean" name="Clean Runs" fill="#10b981" stackId="a" />
                  <Bar yAxisId="left" dataKey="flagged" name="Flagged Runs" fill="#f59e0b" stackId="a" radius={[4, 4, 0, 0]} />
                  <Line yAxisId="right" type="monotone" dataKey="resolved" name="Resolved Exceptions" stroke="#3b82f6" strokeWidth={3} dot={{ r: 4 }} activeDot={{ r: 6 }} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Exceptions Breakdown Pie Chart */}
          <div className="card" style={{ padding: "24px 20px", display: "flex", flexDirection: "column" }}>
            <div className="card-h" style={{ marginBottom: 20 }}>
              <h3>Exceptions Breakdown</h3>
              <InfoTip text="Distribution of open exceptions by severity." />
            </div>
            {stats?.pending_exceptions ? (
              <div style={{ width: "100%", height: 260 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={pieData}
                      cx="50%"
                      cy="45%"
                      innerRadius={60}
                      outerRadius={85}
                      paddingAngle={5}
                      dataKey="value"
                    >
                      {
                        pieData.map((entry, index) => (
                          <Cell key={`cell-${index}`} fill={entry.color} />
                        ))
                      }
                    </Pie>
                    <Tooltip 
                      formatter={(value) => [`${value} exceptions`, 'Count']}
                      contentStyle={{ borderRadius: 8, border: "1px solid #e2e8f0", boxShadow: "0 10px 15px -3px rgb(0 0 0 / 0.1)" }}
                      itemStyle={{ color: "#0f172a", fontWeight: 600 }}
                    />
                    <Legend verticalAlign="bottom" height={36} iconType="circle" wrapperStyle={{ fontSize: 13, color: "#64748b" }} />
                  </PieChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--p-faint)", fontSize: 15, fontWeight: 500 }}>
                No open exceptions! 🎉
              </div>
            )}
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginBottom: 18 }}>
          {/* Incoming files */}
          {showFiles && (
            <div className="card" style={{ padding: 24, display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer", transition: "transform 0.2s, box-shadow 0.2s" }} onClick={() => nav("/files")} onMouseOver={(e) => { e.currentTarget.style.transform = "translateY(-2px)"; e.currentTarget.style.boxShadow = "0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1)"; }} onMouseOut={(e) => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = "var(--p-shadow)"; }}>
              <div>
                <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
                  <div style={{ width: 40, height: 40, borderRadius: 8, backgroundColor: "var(--p-surface-2)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="12" y1="18" x2="12" y2="12"></line><line x1="9" y1="15" x2="15" y2="15"></line></svg>
                  </div>
                  <h3 style={{ margin: 0, fontSize: 18 }}>Incoming Files</h3>
                </div>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                  <span style={{ fontSize: 32, fontWeight: 600, color: "var(--p-text)" }}>{arrivals?.length ?? 0}</span>
                  <span style={{ color: "var(--p-muted)", fontSize: 14 }}>New Files</span>
                </div>
              </div>
              <div style={{ opacity: 0.3 }}>
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
              </div>
            </div>
          )}

          {/* Recent runs */}
          <div className="card" style={{ padding: 24, display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer", background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)", color: "white", border: "none", transition: "transform 0.2s, box-shadow 0.2s" }} onClick={() => nav("/runs?from=home")} onMouseOver={(e) => { e.currentTarget.style.transform = "translateY(-2px)"; e.currentTarget.style.boxShadow = "0 10px 15px -3px rgba(15,23,42,0.4), 0 4px 6px -4px rgba(15,23,42,0.4)"; }} onMouseOut={(e) => { e.currentTarget.style.transform = "none"; e.currentTarget.style.boxShadow = "none"; }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
                <div style={{ width: 40, height: 40, borderRadius: 8, backgroundColor: "rgba(255,255,255,0.1)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                </div>
                <h3 style={{ margin: 0, fontSize: 18, color: "white" }}>Recent File Submissions</h3>
              </div>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                <span style={{ fontSize: 32, fontWeight: 600, color: "white" }}>{runs.length}</span>
                <span style={{ color: "rgba(255,255,255,0.7)", fontSize: 14 }}>Completed</span>
              </div>
            </div>
            <div style={{ opacity: 0.5, color: "white" }}>
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
            </div>
          </div>
        </div>

       
      </div >
    </div >
  );
}
