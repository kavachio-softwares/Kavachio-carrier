// Audit findings on the dashboards (audit_alerts.py, 53.2): the broker sees the
// findings still open about it, the carrier the findings the broker answered
// and those past their due date. Nothing is shown when nothing waits.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FileSearch } from "lucide-react";
import { getAuditsWaiting, getBrokerFindings, type Finding, type WaitingFinding } from "../api/audits";
import { InfoTip } from "./InfoTip";

const SHOWN = 5;
const day = (d: string | null | undefined) =>
  d ? new Date(d.length === 10 ? `${d}T00:00:00` : d).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "—";
const SEV: Record<string, string> = { critical: "b-crit", high: "b-warn", medium: "b-info", low: "b-mut" };

export function AuditFindingsCard({ side }: { side: "broker" | "carrier" }) {
  const [mine, setMine] = useState<Finding[] | null>(null);
  const [waiting, setWaiting] = useState<{ replied: WaitingFinding[]; overdue: WaitingFinding[] } | null>(null);

  useEffect(() => {
    if (side === "broker")
      getBrokerFindings().then(d => setMine(d.findings.filter(f => f.status !== "closed"))).catch(() => setMine([]));
    else
      getAuditsWaiting().then(setWaiting).catch(() => setWaiting({ replied: [], overdue: [] }));
  }, [side]);

  if (side === "broker") {
    const rows = mine ?? [];
    if (!rows.length) return null;
    return (
      <div className="card" style={{ marginBottom: 24 }}>
        <div className="card-h">
          <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <FileSearch size={18} /> Audit findings waiting for you ({rows.length})
            <InfoTip text={"Findings from your carrier's audits that are not closed yet.\nReply with what you did to fix each one."} />
          </h3>
          <Link className="linkish right" style={{ fontSize: 13 }} to="/broker/audit-findings">See all →</Link>
        </div>
        <div style={{ padding: "8px 20px 14px" }}>
          {rows.slice(0, SHOWN).map(f => (
            <div className="kv" key={f.id}>
              <span className="k">
                <b style={{ color: "var(--p-ink)" }}>{f.title}</b>
                <div className="sub">{f.carrier ?? "Carrier"} · {f.audit ?? "Audit"} · Due {day(f.due_date)}</div>
              </span>
              <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                {f.overdue && <span className="badge b-crit">Overdue</span>}
                <span className={`badge ${SEV[f.severity] ?? "b-mut"}`}>{f.severity_label}</span>
                <Link className="btn pri sm" to="/broker/audit-findings">Reply</Link>
              </span>
            </div>
          ))}
          {rows.length > SHOWN && <div className="sub" style={{ paddingTop: 8 }}>Showing {SHOWN} of {rows.length}.</div>}
        </div>
      </div>
    );
  }

  const replied = waiting?.replied ?? [];
  const overdue = waiting?.overdue ?? [];
  const count = new Set([...replied, ...overdue].map(f => f.id)).size;
  if (!count) return null;
  const row = (f: WaitingFinding, when: string) => (
    <div className="kv" key={`${when}-${f.id}`}>
      <span className="k">
        <b style={{ color: "var(--p-ink)" }}>{f.title}</b>
        <div className="sub">{f.broker ?? "Broker"} · {f.audit ?? "Audit"} · {when}</div>
      </span>
      <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span className={`badge ${SEV[f.severity] ?? "b-mut"}`}>{f.severity_label}</span>
        <Link className="btn sm" to={`/audit?tab=audits&audit=${f.audit_id}`}>Open</Link>
      </span>
    </div>
  );
  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <div className="card-h">
        <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <FileSearch size={18} /> Audit findings to look at ({count})
          <InfoTip text={"Findings where the broker has replied and you have not answered yet,\nand findings past their due date."} />
        </h3>
        <Link className="linkish right" style={{ fontSize: 13 }} to="/audit?tab=audits">See all →</Link>
      </div>
      <div style={{ padding: "8px 20px 14px" }}>
        {replied.length > 0 && <>
          <div className="sub" style={{ fontWeight: 600, padding: "4px 0" }}>Broker replied ({replied.length})</div>
          {replied.slice(0, SHOWN).map(f => row(f, `Replied ${day(f.replied_at)}`))}
        </>}
        {overdue.length > 0 && <>
          <div className="sub" style={{ fontWeight: 600, padding: "10px 0 4px" }}>Overdue ({overdue.length})</div>
          {overdue.slice(0, SHOWN).map(f => row(f, `Was due ${day(f.due_date)}`))}
        </>}
      </div>
    </div>
  );
}
