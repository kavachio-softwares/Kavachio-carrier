// Programme Rules waiting for approval, on the dashboards (programme_rule_routes):
// the broker sees the rules waiting for its own answer, the carrier the rules it
// sent that not everyone has approved yet. Nothing is shown when nothing waits.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ListChecks } from "lucide-react";
import { getBrokerRules, getWaitingRules, type BrokerRule, type WaitingRule } from "../api/programmeRules";
import { InfoTip } from "./InfoTip";

const SHOWN = 5;

export function RulesWaitingCard({ side }: { side: "broker" | "carrier" }) {
  const [broker, setBroker] = useState<BrokerRule[] | null>(null);
  const [carrier, setCarrier] = useState<WaitingRule[] | null>(null);

  useEffect(() => {
    if (side === "broker")
      getBrokerRules().then(d => setBroker(d.rules.filter(r => r.status !== "approved"))).catch(() => setBroker([]));
    else
      getWaitingRules().then(d => setCarrier(d.rules)).catch(() => setCarrier([]));
  }, [side]);

  const count = side === "broker" ? broker?.length ?? 0 : carrier?.length ?? 0;
  if (!count) return null;
  const all = side === "broker" ? "/broker/programme-rules" : null;

  return (
    <div className="card" style={{ marginBottom: 24 }}>
      <div className="card-h">
        <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <ListChecks size={18} />
          {side === "broker" ? "Programme rules waiting for your approval" : "Programme rules waiting for approval"} ({count})
          <InfoTip text={side === "broker"
            ? "Rules your carrier set for a whole programme.\nA rule checks your files only once you approve it."
            : "Rules you sent that not everyone has approved yet.\nA rule checks a broker's or reinsurer's files only once they approve it."} />
        </h3>
        {all && count > 0 && <Link className="linkish right" style={{ fontSize: 13 }} to={all}>Review all →</Link>}
      </div>
      <div style={{ padding: "8px 20px 14px" }}>
        {side === "broker" ? broker!.slice(0, SHOWN).map(r => (
          <div className="kv" key={r.id}>
            <span className="k">
              <b style={{ color: "var(--p-ink)" }}>{r.name}</b>
              <div className="sub">{r.sentence}</div>
              <div className="sub">{r.carrier ?? "Carrier"} · {r.programme ?? "Programme"}</div>
            </span>
            <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {r.status === "changes_requested" && <span className="badge b-warn">You asked for changes</span>}
              <Link className="btn pri sm" to="/broker/programme-rules">Review</Link>
            </span>
          </div>
        )) : carrier!.slice(0, SHOWN).map(r => (
          <div className="kv" key={r.id}>
            <span className="k">
              <b style={{ color: "var(--p-ink)" }}>{r.name}</b>
              <div className="sub">{r.sentence}</div>
              <div className="sub">{r.programme}</div>
            </span>
            <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span className={`badge ${r.changes_requested ? "b-warn" : "b-info"}`}>{r.status_line}</span>
              <Link className="btn sm" to={`/programs/${r.program_id}/setup?tab=rules`}>Open</Link>
            </span>
          </div>
        ))}
        {count > SHOWN && (
          <div className="sub" style={{ paddingTop: 8 }}>
            Showing {SHOWN} of {count}.{side === "broker" && <> <Link className="linkish" to="/broker/programme-rules">See all →</Link></>}
          </div>)}
      </div>
    </div>
  );
}
