import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, downloadFile } from "../api/client";
import { currentMga, getUser, userRole, ROLE_LABEL, type Role } from "../auth";
import { fmtStamp } from "../utils/date";
import { InfoTip } from "../components/InfoTip";
import { getCalendar, type CalendarStatus } from "../api/calendar";

type Stats = {
  uploads_today: number; uploads_total: number; open_bdx_cycles: number;
  parties_in_directory: number; pending_exceptions: number;
  ai_cache_hit_rate: number | null;
  // --- extended fields for the prototype KPIs (optional until the API adds them) ---
  exception_runs?: number;                                       // # runs with open exceptions
  exceptions_by_severity?: { critical: number; warning: number; info: number };
  runs_this_week?: number;
  runs_by_day?: number[];                                        // last 7 days, for the sparkline
  active_setups?: number;
  active_setup_carriers?: number;
  mapping_tasks_open?: number;                                   // kavachio_admin tile
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
  tenant_user:    "What needs you today, and your most recent bordereau runs.",
  tenant_admin:   "What needs you today, and your most recent bordereau runs.",
  kavachio_admin: "Platform activity and your most recent bordereau runs.",
};

export default function Home() {
  const mga = currentMga();
  const nav = useNavigate();
  const user = getUser();
  const role = userRole() ?? "tenant_user";
  const [stats, setStats] = useState<Stats | null>(null);
  // Whether this tenant still needs first-time setup (carrier + Bordereau).
  const [needsSetup, setNeedsSetup] = useState(false);
  // The dashboard shows a small snapshot; the full, filterable history lives
  // on the dedicated Run history page (/runs).
  const RUNS_PAGE = 5;
  const [runs, setRuns] = useState<Run[]>([]);
  // Group 3: deadline counts for the "Deadlines" tile (own submission calendar).
  const [calCounts, setCalCounts] = useState<Partial<Record<CalendarStatus, number>>>({});

  useEffect(() => {
    api.get<Stats>(`/dashboard/stats`, { params: { mga } }).then(r => setStats(r.data));
  }, [mga]);

  useEffect(() => {
    getCalendar().then(c => setCalCounts(c.counts ?? {})).catch(() => setCalCounts({}));
  }, [mga]);

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
  const runHasExc = (r: Run) => r.status === "has_exceptions" || r.exception_count > 0;
  // Exception triage for a generated run — matches the Outputs page route.
  // In download mode UploadExceptions loads by `download` id; the uploadId in the
  // path is only used for link-building, so 0 is a safe placeholder when absent.
  const goTriage = (r: Run) =>
    nav(`/uploads/${r.source_upload_id ?? 0}/exceptions?download=${r.id}&from=home`);
  // "Open triage" (KPI tile) — jump to the most recent run that has exceptions,
  // else the most recent run (renders the exceptions screen with a blank listing).
  const latestExcRun = runs.find(runHasExc);
  const openTriage = () =>
    latestExcRun ? goTriage(latestExcRun)
    : runs[0]    ? goTriage(runs[0])
    : nav("/uploads/0/exceptions?from=home");

  const sev = stats?.exceptions_by_severity;
  const spark = stats?.runs_by_day;
  const sparkMax = spark && spark.length ? Math.max(...spark, 1) : 1;

  // Operators can't run the org/carrier/Bordereau setup — that's a tenant-admin
  // job. Until it's done, an operator gets a single notice instead of a dashboard
  // with nothing behind it.
  if (needsSetup && role === "tenant_user") {
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
          <div className="actions">
            <Link className="btn pri" to="/direct">＋ Process Bordereaux</Link>
          </div>
        </div>

        {/* KPI tiles — wired to /dashboard/stats (see API notes for the new fields) */}
        <div className="tiles" style={{ marginBottom: 18 }}>
          {/* Exceptions to review */}
          <div className="tile alert">
            <div className="k" style={{ display: "flex", alignItems: "center", gap: 4 }}>
              Exceptions to Review
              <InfoTip text="Cells that failed a validation rule and are still waiting on a decision, across every bordereau you've processed. “Open Triage” takes you to your most recent run that has exceptions, where you can approve, fix or dismiss each one." />
            </div>
            <div className="v" style={{ color: "var(--p-crit)" }}>
              {fmt(stats?.pending_exceptions)}
              {stats?.exception_runs != null && <small> · {stats.exception_runs} runs</small>}
            </div>
            {sev && (
              <div className="sevbar">
                <i className="c" style={{ flex: sev.critical || 0 }} />
                <i className="w" style={{ flex: sev.warning || 0 }} />
                <i className="i" style={{ flex: sev.info || 0 }} />
              </div>
            )}
            <div className="foot"><span className="linkish" onClick={openTriage}>Open Latest Exception Triage →</span></div>
          </div>

          {/* Runs this week */}
          <div className="tile">
            <div className="k">Runs This Week</div>
            <div className="v">{fmt(stats?.runs_this_week)}</div>
            {spark && spark.length > 0 && (
              <div className="spark">
                {spark.map((n, i) => (
                  <i key={i}
                    className={i === spark.length - 1 ? "hi" : undefined}
                    style={{ height: `${Math.max(10, Math.round((n / sparkMax) * 100))}%` }} />
                ))}
              </div>
            )}
          </div>

          {/* Active setups — falls back to open_bdx_cycles (active programs) until
              the API exposes a dedicated active_setups count. */}
          <div className="tile">
            <div className="k">Active Setups</div>
            <div className="v">{fmt(stats?.active_setups ?? stats?.open_bdx_cycles)}</div>
            {stats?.active_setup_carriers != null && (
              <div className="foot">across {stats.active_setup_carriers} carriers</div>
            )}
          </div>

          {/* Role-specific 4th tile */}
          {role === "kavachio_admin" ? (
            <div className="tile">
              <div className="k">Mapping Tasks</div>
              <div className="v">{fmt(stats?.mapping_tasks_open)}</div>
              <div className="foot"><Link className="linkish" to="/admin/mapping-tasks">Data-Model Queue →</Link></div>
            </div>
          ) : (
            <div className="tile">
              <div className="k" style={{ display: "flex", alignItems: "center", gap: 4 }}>
                Avg Turnaround
                <InfoTip text="Average time from a file being uploaded to its validation finishing, over uploads validated in the last 30 days." />
              </div>
              <div className="v">
                {stats?.avg_turnaround_min == null ? "—" : stats.avg_turnaround_min}
                <small> min</small>
              </div>
              <div className="foot">file → validated output</div>
            </div>
          )}

          {/* Group 3: Deadlines — surfaces only when something needs attention, so
              the dashboard stays clean until a bordereau is coming due or missed.

              Leads with the WORST state rather than a single blended number. It
              used to headline `due_soon + overdue` under the word "due soon",
              which called already-missed deadlines upcoming, and then reported
              "late" separately — three states, two of them past due, presented as
              if one of them were still ahead of you. */}
          {/* HIDDEN FOR NOW, at request — commented out rather than deleted so it
              can come straight back. Everything it needs is still live: the
              counts are still fetched above, My Calendar still shows the same
              deadlines, and the reminder bell still counts them. */}
          {/* {role !== "kavachio_admin" && (() => {
            const overdue = calCounts.overdue ?? 0;
            const upcoming = (calCounts.due_today ?? 0) + (calCounts.due_soon ?? 0);
            if (overdue + upcoming === 0) return null;
            return (
              <div className={`tile${overdue > 0 ? " alert" : ""}`}>
                <div className="k">Deadlines</div>
                <div className="v" style={{ color: overdue > 0 ? "var(--p-crit)" : undefined }}>
                  {overdue > 0 ? overdue : upcoming}
                  <small>{overdue > 0 ? " overdue" : " due soon"}</small>
                </div>
                <div className="foot">
                  {overdue > 0 && upcoming > 0 && <span>{upcoming} due soon · </span>}
                  <Link className="linkish" to="/calendar">My Calendar →</Link>
                </div>
              </div>
            );
          })()} */}
        </div>


        {/* Recent runs — latest 5 generated outputs. Full history: /runs */}
        <div className="card">
          <div className="card-h">
            <h3>Recent Runs</h3>
            <span className="sub">View your most recently processed bordereaux.</span>
            <div className="right" style={{ display: "flex", gap: 14 }}>
              <Link className="linkish" to="/direct">Process Bordereau →</Link>
              <Link className="linkish" to="/runs?from=home">View All →</Link>
            </div>
          </div>
          {runs.length === 0 ? (
            <div className="empty">No runs yet — process a Bordereaux and it will appear here.</div>
          ) : (
            <div className="tbl-wrap">
              <table>
                <thead><tr><th className="l">Output File</th><th>Program</th><th>Policies</th><th>Result</th><th>Generated</th><th></th></tr></thead>
                <tbody>
                  {runs.map(r => {
                    const hasExc = runHasExc(r);
                    return (
                      <tr key={r.id}>
                        <td className="l"><b>{r.filename}</b></td>
                        <td className="muted">{r.template_name ?? "—"}</td>
                        <td className="muted">{r.policy_count.toLocaleString()}</td>
                        <td>
                          <span className={`badge ${hasExc ? "b-crit" : "b-ok"}`}>
                            <span className="muted" />
                            {hasExc ? `${r.exception_count.toLocaleString()} exceptions` : "Clean"}
                          </span>
                        </td>
                        <td className="muted">{fmtStamp(r.created_at, "")}</td>
                        <td className="muted">
                          <span className="linkish"
                            onClick={() => hasExc
                              ? goTriage(r)
                              : downloadFile(`/export/downloads/${r.id}/file`, r.filename)
                                  .catch(() => alert("We couldn't download that file — please try again."))}>
                            {hasExc ? "Review →" : "Download →"}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {runs.length >= RUNS_PAGE && (
            <div style={{ padding: "10px 20px", borderTop: "1px solid var(--p-border)" }}>
              <Link className="linkish" to="/runs?from=home" style={{ fontSize: 12.5 }}>
                View All Process Bordereaux →
              </Link>
            </div>
          )}
        </div>

        {user && (
          <div className="muted" style={{ fontSize: 11.5, marginTop: 18 }}>
            Signed in as {user.full_name} · {ROLE_LABEL[role]}
          </div>
        )}
      </div>
    </div>
  );
}
