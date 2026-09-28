import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ComposedChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, Legend } from "recharts";
import { LayoutDashboard, Layers, Users, FileText, AlertCircle, Activity, Clock, ShieldCheck, UserCheck } from "lucide-react";
import { api } from "../api/client";
import { currentMga, getUser, isKavachioAdmin, userRole, ROLE_LABEL, type Role } from "../auth";
import { canAccessPath } from "../access";

import { getCalendar, type CalendarStatus } from "../api/calendar";
import { getBrokersPaged, getHierarchy } from "../api/hierarchy";
import { listContractsPaged } from "../api/contractRecord";
import { addsCarrierUsers, useCarrierSeat } from "../hooks/useCarrierSeat";
import { listArrivals, type Arrival } from "../api/intake";
import { InfoTip } from "../components/InfoTip";
import { StatCard } from "../components/StatCard";
import BrokerPerformance from "../components/BrokerPerformance";


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
  /** Contracts it is the CARRIER's move on — a colleague's draft, a change the
   *  broker asked for, and a signature due, together. A superset of
   *  pending_signatures, which counts only the last of the three. */
  contracts_waiting?: number;
  /** Of those, the ones only the carrier admin can move: an uploaded contract
   *  waiting to be accepted, or one whose terms the broker has agreed and
   *  which now needs the carrier's signature. */
  contracts_awaiting_admin?: number;
  /** Broker onboarding requests waiting on the carrier admin. Null for a
   *  carrier user, who is not the one being asked. */
  broker_requests_pending?: number | null;
  /** Still served, but no longer drawn on this screen — the carrier's
   *  "Avg Turnaround Time" box was hidden. Kept so the shape matches the
   *  endpoint. */
  avg_turnaround_min?: number | null;
};
// A "run" = a generated output export (carries the validation result).
type Run = {
  id: number; filename: string; template_name: string | null;
  policy_count: number; exception_count: number;
  status: string; created_at: string | null;
  source_upload_id: number | null;
};
const SUBTITLE: Record<Role, string> = {
  carrier_admin: "",
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
  // How many files have been run for this carrier ALTOGETHER. The card below
  // used to print `runs.length`, which is this snapshot's size and therefore
  // stopped at 5 however many files had actually come in — a carrier with
  // ninety submissions read "5 Completed". Counted by the same endpoint the
  // File Submissions screen pages through, so the two can never disagree.
  const [runsTotal, setRunsTotal] = useState<number | null>(null);
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
  // Bordereau setups a colleague has sent up and this carrier admin has not
  // decided on. The carrier admin's ONLY "waiting on me" — a setup sitting
  // here is a broker who cannot send a file yet, so it belongs on the first
  // screen they see rather than somewhere they have to think to look.
  const [pendingSetups, setPendingSetups] = useState<number | null>(null);
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

  // Only the carrier admin can decide on these, so only they are asked to.
  useEffect(() => {
    if (!addsCarrierUsers(seat)) { setPendingSetups(null); return; }
    api.get<{ total: number }>("/pipelines", {
      params: { mga, status: "pending_approval", page: 1, page_size: 1 },
    })
      .then(r => setPendingSetups(r.data?.total ?? 0))
      .catch(() => setPendingSetups(null));
  }, [mga, seat]);

  // Everything on this side's desk. `contracts_waiting` is the superset the
  // server derives through _whose_turn; pending_signatures is the part of it
  // that is a signature. Falls back to the signatures alone on an older server
  // that does not send the wider figure, so the tile never reads blank.
  const waitingTotal = stats?.contracts_waiting ?? stats?.pending_signatures ?? 0;
  // The part of it nobody else at this carrier can do: sign a contract the
  // broker has agreed, or accept an uploaded one. The server counts it, because
  // the server is what decides it (contract_routes._carrier_admin_turn).
  const adminOnly = Math.min(waitingTotal, stats?.contracts_awaiting_admin ?? 0);

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

  // The total, bought for one row: `page` makes /direct/runs answer with
  // {items, total}, and the total is counted over the whole tenant rather than
  // over the page, so page_size 1 still yields the real figure. Same trick the
  // book-count tiles above use.
  useEffect(() => {
    api.get<{ total: number }>(`/direct/runs`, { params: { mga, page: 1, page_size: 1 } })
      .then(r => setRunsTotal(r.data?.total ?? null))
      .catch(() => setRunsTotal(null));
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

          {/* Kavachio staff only. This used to be a ternary whose other half
              was "Avg Turnaround Time" for the carrier seats; that box is
              hidden now, so there is nothing to fall back to. The figure is
              still served (`avg_turnaround_min`) and the broker's own
              dashboard still draws its version of it — only the carrier's box
              is gone. */}
          {role === "kavachio_admin" && (
            <StatCard title="Mapping Tasks" value={fmt(stats?.mapping_tasks_open)} icon={Clock} onClick={() => nav("/admin/mapping-tasks")} />
          )}

          {/* ONE box, not two — but the number on it has to be the one that
              asks for something. It read `pending_signatures` before, which is
              0 for a contract waiting to be REVIEWED, so the thing the carrier
              admin had come to find showed as a zero they would not click.
              The headline is now everything on their desk and the subtitle
              splits it, which is also why the title is no longer only about
              signatures: a box called Pending Signatures reading 1 for a
              contract nobody can sign yet is a box that lies.

              Carrier ADMIN only. Signing and approving are both theirs alone,
              so for a carrier user this counts work they cannot do — they are
              told through the notification bell instead, which is addressed to
              them by name. */}
          {addsCarrierUsers(seat) && (
            <StatCard
              title="Contract Review"
              value={fmt(waitingTotal)}
              icon={FileText}
              tone={waitingTotal ? "alert" : undefined}
              // Split by WHOSE MOVE, not by lifecycle: what the admin opening
              // this wants to know is how much of it only they can clear. The
              // subtitle used to subtract pending_signatures from the total,
              // which counted a contract waiting to be ACCEPTED as one waiting
              // to be reviewed — the two are the admin's alike, and neither is
              // the author's.
              subtitle={waitingTotal
                ? `${adminOnly} needing you · `
                  + `${Math.max(0, waitingTotal - adminOnly)} other`
                : `${stats?.completed_signatures ?? 0} signed`}
              onClick={() => nav("/contracts?waiting=mine")}
              info={"Contracts it is your move on: terms the broker has agreed "
                    + "and which need your signature, an uploaded contract to "
                    + "accept, or one the broker has pushed back on. Only the "
                    + "carrier admin signs and accepts, and the carrier signs "
                    + "first."}
            />
          )}

          {/* The OTHER thing that stops on the carrier admin's desk, and the
              only one the contract tile beside it cannot count: a bordereau
              setup a colleague finished and sent up. It was already being
              fetched for this screen and then rendered nowhere, so the one
              place the admin looks first said nothing about it and the setup
              sat waiting until somebody happened to open Bordereau Setup.

              Its own tile rather than a line inside "Contract Review": that one
              counts CONTRACTS and opens the contracts list, and folding a
              second kind of thing into its number would make the figure and
              the screen it opens disagree.

              Carrier ADMIN only, like the fetch that feeds it — approving a
              setup is theirs alone, so for a carrier user this would count
              work they cannot do. They are told through the bell instead. */}
          {addsCarrierUsers(seat) && (
            <StatCard
              title="BDX Setup Review"
              value={fmt(pendingSetups ?? 0)}
              icon={ShieldCheck}
              tone={pendingSetups ? "alert" : undefined}
              subtitle={pendingSetups
                ? `${pendingSetups === 1 ? "setup is" : "setups are"} waiting `
                  + "on you"
                : `${stats?.active_setups ?? 0} live`}
              onClick={() => nav("/direct/setups?status=pending_approval")}
              info={"Bordereau setups your carrier users have finished and sent "
                    + "up. Nothing about one reaches the broker until you "
                    + "approve it — not the programme, not the contract and "
                    + "not the BDX template."}
            />
          )}

          {/* The THIRD thing that stops on the carrier admin's desk, and the
              earliest of the three: a broker a colleague wants to bring on.
              Nothing has been sent to that broker and nothing exists for them
              yet — this is the one gate where waiting costs nobody anything,
              which is exactly why it would otherwise go unnoticed.

              ALWAYS SHOWN, at nought as well. "Nothing is waiting on me" is
              the answer the admin comes to the dashboard for, and a tile that
              appears only when there is news is one nobody learns to trust.
              The count comes back as a number for them and null for a carrier
              user, so the seat test and the data agree.

              Not folded into BDX Setup Review beside it: that one counts
              setups and opens the setups list. These are two decisions about
              two different things, taken weeks apart. */}
          {addsCarrierUsers(seat) && (
            <StatCard
              title="Broker Onboarding Pending"
              value={fmt(stats?.broker_requests_pending ?? 0)}
              icon={UserCheck}
              tone={stats?.broker_requests_pending ? "alert" : undefined}
              subtitle={stats?.broker_requests_pending
                ? `${stats.broker_requests_pending === 1 ? "broker is" : "brokers are"} `
                  + "waiting on you"
                : "nothing waiting"}
              onClick={() => nav("/brokers/requests")}
              info={"Brokers your carrier users want to bring on board. Nothing "
                    + "has been sent to any of them — they hear from Kavachio "
                    + "only once you approve. Approving sends the invitation; "
                    + "what they may send you is still settled by the bordereau "
                    + "setup approval."}
            />
          )}

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
              <div style={{ width: "100%", height: 260, position: "relative" }}>
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
                <div style={{
                  position: "absolute", left: "50%", top: "45%", transform: "translate(-50%, -50%)",
                  textAlign: "center", pointerEvents: "none",
                }}>
                  <div style={{ fontSize: 30, fontWeight: 700, color: "#0f172a", lineHeight: 1 }}>
                    {fmt(stats.pending_exceptions)}
                  </div>
                  <div style={{ fontSize: 12.5, color: "#64748b", marginTop: 2 }}>Open</div>
                </div>
              </div>
            ) : (
              <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--p-faint)", fontSize: 15, fontWeight: 500 }}>
                No open exceptions! 🎉
              </div>
            )}
          </div>
        </div>

        {/* How each broker company is working and putting its issues right —
            the five that most recently sent this carrier a file. */}
        {carrierSeat && (
          <div style={{ marginBottom: 24 }}>
            <BrokerPerformance mga={mga} />
          </div>
        )}

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
                <span style={{ fontSize: 32, fontWeight: 600, color: "white" }}>
                  {runsTotal ?? runs.length}
                </span>
                {/* "Processed" rather than "Completed": a run in this count may
                    have come back with exceptions still open, and calling that
                    completed is the one reading a carrier must not take from
                    this card. The open work is the tile above. */}
                <span style={{ color: "rgba(255,255,255,0.7)", fontSize: 14 }}>
                  {(runsTotal ?? runs.length) === 1 ? "File Processed" : "Files Processed"}
                </span>
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
