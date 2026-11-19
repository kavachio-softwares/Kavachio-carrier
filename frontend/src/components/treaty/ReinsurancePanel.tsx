/**
 * Reinsurance on the Home dashboard — three cards in the dashboard's own
 * style (the same shape as Overdue Bordereaux):
 *
 *   Treaties                 how many are in place, and in what state
 *   Reinsurance Bordereaux   this month's inward bordereaux on the programmes
 *                            a treaty protects — what a ceded bordereau is
 *                            built from. The ceded file itself is not produced
 *                            in Kavachio yet, so "Sent to reinsurers" is a dash,
 *                            never a made-up number.
 *   Expiring Treaties        the ones running out in the next 90 days
 */
import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, CalendarClock, FileSpreadsheet, ShieldCheck } from "lucide-react";
import { InfoTip } from "../InfoTip";
import { getReinsuranceDashboard, type ReinsuranceDashboard } from "../../api/treaty";
import { fmtDate } from "../../utils/date";

const SHADOW = "0 4px 6px -1px rgb(0 0 0 / 0.05), 0 2px 4px -2px rgb(0 0 0 / 0.05)";

function DashCard({ icon, tint, title, info, to, alert, children }: {
  icon: ReactNode; tint: [string, string]; title: string; info: string;
  to: string; alert?: boolean; children: ReactNode;
}) {
  const nav = useNavigate();
  return (
    <div role="link" tabIndex={0}
      onClick={() => nav(to)}
      onKeyDown={e => { if (e.key === "Enter") nav(to); }}
      style={{
        backgroundColor: "var(--p-surface)",
        border: `1px solid ${alert ? "var(--p-warn)" : "var(--p-border-2)"}`,
        borderRadius: 16, padding: 16, display: "flex", flexDirection: "column",
        gap: 10, boxShadow: SHADOW, cursor: "pointer",
        transition: "transform 0.2s, box-shadow 0.2s",
      }}
      onMouseOver={e => { e.currentTarget.style.transform = "translateY(-2px)";
                          e.currentTarget.style.boxShadow = "0 10px 15px -3px rgb(0 0 0 / 0.1)"; }}
      onMouseOut={e => { e.currentTarget.style.transform = "none";
                         e.currentTarget.style.boxShadow = SHADOW; }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ width: 36, height: 36, borderRadius: 10, flex: "none",
                      background: tint[0], color: tint[1],
                      display: "flex", alignItems: "center", justifyContent: "center" }}>
          {icon}
        </div>
        <div style={{ fontSize: 15, fontWeight: 600, display: "flex",
                      alignItems: "center", gap: 5, minWidth: 0 }}>
          {title}<InfoTip text={info} />
        </div>
      </div>
      {children}
    </div>
  );
}

function Big({ n, caption }: { n: number | null | undefined; caption: string }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
      <span style={{ fontSize: 28, fontWeight: 700, lineHeight: 1 }}>{n ?? "—"}</span>
      <span style={{ fontSize: 13, color: "var(--p-muted)" }}>{caption}</span>
    </div>
  );
}

type Row = { label: string; v: number | null | undefined; c: string; tip?: string };

/** The split bar and the rows under it — the Overdue Bordereaux layout. */
function Rows({ rows }: { rows: Row[] }) {
  const total = rows.reduce((a, r) => a + (r.v ?? 0), 0);
  return (
    <>
      <div style={{ display: "flex", gap: 2, height: 6, borderRadius: 3,
                    overflow: "hidden", background: "var(--p-surface-3)" }}>
        {total > 0 && rows.filter(r => (r.v ?? 0) > 0).map(r => (
          <span key={r.label} title={`${r.label}: ${r.v}`}
                style={{ width: `${((r.v ?? 0) / total) * 100}%`, background: r.c }} />
        ))}
      </div>
      <div>
        {rows.map((r, i) => (
          <div key={r.label} title={r.tip} style={{
            display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
            borderTop: i === 0 ? undefined : "1px solid var(--p-border)" }}>
            <span style={{ width: 9, height: 9, borderRadius: 3, background: r.c, flex: "none" }} />
            <span style={{ fontSize: 13, color: "var(--p-muted)" }}>{r.label}</span>
            <span style={{ marginLeft: "auto", fontSize: 16, fontWeight: 700 }}>
              {r.v == null ? "—" : r.v.toLocaleString()}
            </span>
          </div>
        ))}
      </div>
    </>
  );
}

export function ReinsurancePanel() {
  const [d, setD] = useState<ReinsuranceDashboard | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    getReinsuranceDashboard().then(setD).catch(() => setFailed(true));
  }, []);
  if (failed) return null;
  const c = d?.counts;
  const b = d?.bordereaux;
  const exp = d?.expiring ?? [];
  const month = new Date().toLocaleDateString(undefined, { month: "long", year: "numeric" });

  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                    gap: 12, marginBottom: 12 }}>
        <div style={{ display: "flex", alignItems: "center" }}>
          <h3 style={{ margin: 0, fontSize: 16 }}>Reinsurance</h3>
          <InfoTip text="Your treaties, the bordereaux on the programmes they protect, and the treaties running out soon." />
        </div>
        <Link to="/contracts?type=insurer_reinsurer" className="linkish"
              style={{ fontSize: 13, display: "inline-flex", alignItems: "center", gap: 4 }}>
          All treaties <ArrowRight size={13} />
        </Link>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
                    gap: 24 }}>
        <DashCard icon={<ShieldCheck size={18} strokeWidth={2.5} />}
                  tint={["#f0fdfa", "#0d9488"]} title="Treaties"
                  info="Treaties in place — not terminated, renewed or expired."
                  to="/contracts?type=insurer_reinsurer">
          <Big n={d?.live} caption={`protecting ${d?.programmes.protected ?? 0} of ${d?.programmes.total ?? 0} programmes`} />
          <Rows rows={[
            { label: "In force", v: c?.active, c: "var(--p-ok)" },
            { label: "Being settled", v: c?.settling, c: "var(--p-faint)" },
            { label: "Waiting for signature", v: c?.awaiting_signature, c: "var(--p-warn)" },
          ]} />
        </DashCard>

        <DashCard icon={<FileSpreadsheet size={18} strokeWidth={2.5} />}
                  tint={["#eff6ff", "#2563eb"]} title="Reinsurance Bordereaux"
                  info={"What you cede to your reinsurers, per treaty and period, worked "
                    + "out from the bordereaux your brokers sent. Prepared automatically; "
                    + "sent when you press Send."}
                  to="/reinsurance/bordereaux"
                  alert={(d?.ceded.overdue ?? 0) > 0 || (d?.ceded.queried ?? 0) > 0}>
          <Big n={d?.ceded.sent_this_month} caption={`sent in ${month}`} />
          <Rows rows={[
            { label: "Ready to send", v: d?.ceded.ready_to_send, c: "var(--p-info)" },
            { label: "Waiting for the reinsurer", v: d?.ceded.sent, c: "var(--p-faint)" },
            { label: "Queried", v: d?.ceded.queried, c: "var(--p-warn)" },
            { label: "Agreed", v: d?.ceded.agreed, c: "var(--p-ok)" },
          ]} />
          {!!d?.ceded.overdue && (
            <div style={{ fontSize: 12.5, color: "var(--p-crit-ink)", fontWeight: 600 }}>
              {d.ceded.overdue} overdue — past {"30"} days after the period ended
            </div>
          )}
          {!!d?.ceded.not_set_up && (
            <Link to="/direct/setups?tab=reinsurance" onClick={e => e.stopPropagation()}
                  style={{ fontSize: 12.5, color: "var(--p-warn-ink)", fontWeight: 600 }}>
              {d.ceded.not_set_up} treat{d.ceded.not_set_up === 1 ? "y needs" : "ies need"} a
              setup before {d.ceded.not_set_up === 1 ? "its" : "their"} statements can be prepared →
            </Link>
          )}
          <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>
            From {b?.total ?? 0} inward bordereau{(b?.total ?? 0) === 1 ? "" : "x"} this
            month on protected programmes.
          </div>
          <Link to="/reinsurance/bordereaux" onClick={e => e.stopPropagation()}
                className="btn pri sm" style={{ alignSelf: "flex-start" }}>
            Process reinsurance bordereaux <ArrowRight size={12} />
          </Link>
        </DashCard>

        <DashCard icon={<CalendarClock size={18} strokeWidth={2.5} />}
                  tint={exp.length ? ["#fef2f2", "#ef4444"] : ["#f0fdfa", "#0d9488"]}
                  title="Expiring Treaties"
                  info="Treaties in force that run out in the next 90 days, soonest first."
                  to="/contracts?type=insurer_reinsurer" alert={exp.length > 0}>
          <Big n={d ? exp.length : null} caption="in the next 90 days" />
          <div>
            {d && exp.length === 0 && (
              <div style={{ fontSize: 13, color: "var(--p-muted)", padding: "5px 0" }}>
                Nothing runs out soon.
              </div>
            )}
            {exp.slice(0, 3).map((e, i) => (
              <Link key={e.contract_id} onClick={ev => ev.stopPropagation()}
                    to={`/contracts/${e.renewal_id ?? e.contract_id}`}
                    style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 0",
                             borderTop: i === 0 ? undefined : "1px solid var(--p-border)",
                             color: "inherit", textDecoration: "none" }}>
                <span style={{ width: 9, height: 9, borderRadius: 3, flex: "none",
                               background: e.renewal_id ? "var(--p-ok)"
                                 : e.days_left <= 30 ? "var(--p-crit)" : "var(--p-warn)" }} />
                <span style={{ fontSize: 13, minWidth: 0, overflow: "hidden",
                               textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {e.name}
                </span>
                <span style={{ marginLeft: "auto", fontSize: 12.5, color: "var(--p-muted)",
                               whiteSpace: "nowrap" }}
                      title={fmtDate(e.expiry_dt)}>
                  {e.renewal_id ? "renewal raised" : `${e.days_left} days`}
                </span>
              </Link>
            ))}
            {exp.length > 3 && (
              <div style={{ fontSize: 12.5, color: "var(--p-muted)", paddingTop: 4 }}>
                +{exp.length - 3} more
              </div>
            )}
          </div>
        </DashCard>
      </div>
    </div>
  );
}
