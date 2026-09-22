/**
 * The operator's day.
 *
 * An operator is a seat the BROKER adds to do the day-to-day work — the chain
 * is Kavachio, then carrier, then broker, then operator. Their scope is the
 * broker's: same carriers, same programmes. What differs is the question. A
 * broker admin asks "what is holding me up"; an operator asks "what do I have
 * to run, and what went wrong".
 *
 * The counts are real. When there is nothing to run yet the screen says which
 * step is missing and whose job it is, rather than showing an empty table that
 * looks like a fault.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  getBrokerInsights, getOperatorHome,
  type BrokerInsights, type OperatorHome as Home, type OperatorRun,
} from "../api/broker";
import { Activity, AlertCircle, Clock, FileSpreadsheet, Timer, Upload } from "lucide-react";
import { ResolvedTrend, RunTrend } from "../components/BrokerCharts";
import { InfoTip } from "../components/InfoTip";
import { ChartCard, LinkCard, StatCard } from "../components/StatCard";

/** The window both charts describe. */
const DAYS = 30;

/** Seconds as the unit a person would say it in. */
function duration(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 60) return `${sec.toFixed(1)} sec`;
  if (sec < 3600) return `${(sec / 60).toFixed(1)} min`;
  return `${(sec / 3600).toFixed(1)} hrs`;
}

/** The exception screen a run opens on — the same one Process Bordereau uses. */
const reviewPath = (r: OperatorRun) =>
  `/uploads/${r.export_id}/exceptions?download=${r.export_id}&from=broker`;

export default function OperatorHome() {
  const nav = useNavigate();
  const [d, setD] = useState<Home | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getOperatorHome().then(setD).catch(() => setErr("Could not load your dashboard."));
  }, []);

  const [ins, setIns] = useState<BrokerInsights | null>(null);
  useEffect(() => {
    getBrokerInsights(DAYS).then(setIns).catch(() => setIns(null));
  }, []);

  const head = (
    <div className="page-head">
      <div className="t">
        <h2>Dashboard</h2>
        <p>
          {d ? `${d.broker.name} — your files, and what needs fixing.` : "Your files, and what needs fixing."}
        </p>
      </div>
      <div className="actions">
        <Link className="btn pri" to="/broker/bordereau">＋ Process Bordereau</Link>
      </div>
    </div>
  );

  if (err) return (
    <div className="proto"><div className="view full">{head}
      <div className="note warn" style={{ maxWidth: 560 }}>{err}</div>
    </div></div>
  );
  if (!d) return (
    <div className="proto"><div className="view full">{head}
      <div className="muted">Loading…</div>
    </div></div>
  );

  const c = d.counts;
  const carrierNames = d.carriers.map(x => x.name).join(", ") || "—";
  // The newest run that still has something to look at — where the
  // exceptions tile takes you.
  const firstToReview = d.recent_runs.find(r => r.exception_count > 0);

  return (
    <div className="proto">
      <div className="view full">
        {head}

        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 20, marginBottom: 24 }}>
          <StatCard title="Exceptions to Review" value={c.exceptions} icon={AlertCircle}
                    tone={c.exceptions > 0 ? "alert" : undefined}
                    subtitle={c.exceptions > 0
                      ? `In ${c.exception_runs} ${c.exception_runs === 1 ? "run" : "runs"}`
                      : "Nothing to fix"} />
          <StatCard title="My Files Uploaded" value={d.my_uploads_this_week} icon={Upload}
                    subtitle="This week" />
          <StatCard title="Team Runs This Week" value={ins ? ins.totals.runs_this_week : "—"}
                    icon={Activity} subtitle={`${c.runs} in total`} />
          <StatCard title="Avg Turnaround Time" value={duration(d.avg_turnaround_sec)}
                    icon={Timer} subtitle="Per file, last 30 days" />
        </div>

        {d.blocked_on && d.recent_runs.length === 0 && (
          <div className="card" style={{ padding: 24, marginBottom: 24 }}>
            <p style={{ margin: 0, color: "var(--p-muted)" }}>
              {d.blocked_on === "no-programme"
                ? "Nothing to run yet — your broker has not been put on a carrier's programme."
                : `Nothing to run yet — ${carrierNames} has not built a bordereau setup for your programme.`}
            </p>
          </div>
        )}

        {d.recent_runs.length > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 24, marginBottom: 24 }}>
            <ChartCard title="Bordereau Status"
              info={<InfoTip text={`Your team's files over the last ${DAYS} days, by result: clean, flagged with exceptions, or not checked yet.`} />}>
              {!ins ? <div className="muted">Loading…</div> : <RunTrend data={ins.runs_by_day} />}
            </ChartCard>
            <ChartCard title="Exceptions Resolved"
              info={<InfoTip text={`Exceptions your team put right each day over the last ${DAYS} days.`} />}>
              {!ins ? <div className="muted">Loading…</div> : <ResolvedTrend data={ins.runs_by_day} />}
            </ChartCard>
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginBottom: 18 }}>
          <LinkCard title="Setups Ready" value={c.setups} label="to run files against"
                    icon={FileSpreadsheet} onClick={() => nav("/broker/bordereau")} />
          <LinkCard title="Recent File Submissions" value={c.runs} label="files run" dark
                    icon={Clock} onClick={() => nav("/broker/runs")} />
        </div>
      </div>
    </div>
  );
}
