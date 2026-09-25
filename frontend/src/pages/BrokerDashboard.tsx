/**
 * The broker's landing screen.
 *
 * A broker holds no book of its own — it produces into carriers' programmes.
 * So this screen answers two questions and no others: what have I been given,
 * and what is waiting on me — terms to read, or a signature to give. There is
 * no queue pointing the other way any more: the carrier's approval gate is
 * gone, so nothing a broker adds sits waiting for an answer.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  getBrokerDashboard, getBrokerInsights, getBrokerInvitations,
  acceptBrokerInvitation, declineBrokerInvitation,
  type BrokerDashboard as Dash, type BrokerInsights, type BrokerInvitation,
} from "../api/broker";
import { useBrokerCarrierId } from "../brokerCarrier";
import { canAccessPath } from "../access";
import { fmtDate } from "../utils/date";
import { inAppSigningUrl } from "../api/esign";
import { Activity, AlertCircle, Building2, Clock, FileCheck2, PenLine, Users } from "lucide-react";
import { RankedBars, RunTrend, UploaderBars } from "../components/BrokerCharts";
import { InfoTip } from "../components/InfoTip";
import { ChartCard, LinkCard, StatCard } from "../components/StatCard";

/** The window both charts and the weekly tile describe. */
const DAYS = 30;

export default function BrokerDashboard() {
  const nav = useNavigate();
  const [d, setD] = useState<Dash | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The carrier this broker is working on, chosen in the sidebar. Every count
  // below is scoped to it — "two waiting on you" has to mean two on THIS
  // carrier, or the number is answering a question nobody asked.
  const carrierId = useBrokerCarrierId();

  // Carriers asking to work with this broker. Above everything else on the
  // page, because until one is answered nothing else about that carrier
  // exists — no programmes, no contracts, no files.
  const [invites, setInvites] = useState<BrokerInvitation[] | null>(null);
  const [answering, setAnswering] = useState<number | null>(null);
  const [note, setNote] = useState("");

  const loadInvites = () =>
    getBrokerInvitations().then(setInvites).catch(() => setInvites([]));
  useEffect(() => { loadInvites(); }, []);

  async function answer(id: number, accept: boolean) {
    setAnswering(id); setNote("");
    try {
      const r = accept ? await acceptBrokerInvitation(id)
                       : await declineBrokerInvitation(id);
      setNote(r?.message ?? (accept ? "Accepted." : "Declined."));
      await loadInvites();
      // Accepting adds a carrier, so the counts and the switcher are stale.
      getBrokerDashboard(carrierId).then(setD).catch(() => {});
    } catch {
      setNote("That did not go through. Try again.");
    } finally { setAnswering(null); }
  }

  useEffect(() => {
    setD(null);
    getBrokerDashboard(carrierId).then(setD)
      .catch(() => setErr("Could not load your dashboard."));
  }, [carrierId]);

  const [ins, setIns] = useState<BrokerInsights | null>(null);
  const [waitingOpen, setWaitingOpen] = useState(false);
  useEffect(() => {
    getBrokerInsights(DAYS).then(setIns).catch(() => setIns(null));
  }, []);

  if (err) return (
    <div className="proto"><div className="view full">
      <div className="page-head"><div className="t"><h2>Dashboard</h2></div></div>
      <div className="note warn" style={{ maxWidth: 560 }}>{err}</div>
    </div></div>
  );
  if (!d) return (
    <div className="proto"><div className="view full">
      <div className="page-head"><div className="t"><h2>Dashboard</h2></div></div>
      <div className="muted">Loading…</div>
    </div></div>
  );

  const c = d.counts;
  const grid = (cols: number) => ({
    display: "grid", gridTemplateColumns: `repeat(${cols}, 1fr)`, gap: 20, marginBottom: 24,
  });

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Dashboard</h2>
            <p>{d.broker.name} — your team, your carriers, and what needs you today.</p>
          </div>
          {/* The admin runs bordereaux too, so the month's actual work is the
              primary action here and staffing the team is the secondary one —
              the same order the sidebar puts them in. Derived from
              canAccessPath, not hardcoded, so the button can never offer a
              screen ROUTE_ACCESS would bounce. */}
          <div className="actions">
            {canAccessPath("/broker/bordereau") && (
              <Link className="btn pri" to="/broker/bordereau">＋ Process Bordereau</Link>
            )}
            <Link className="btn" to="/broker/users">＋ Add a user</Link>
          </div>
        </div>

        {note && <div className="note ok" style={{ marginBottom: 16 }}>{note}</div>}

        {/* Until a carrier invitation is answered nothing else about that
            carrier exists, so it sits above everything. */}
        {!!invites?.length && (
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="card-h">
              <h3>
                {invites.length === 1
                  ? "A carrier wants to work with you"
                  : `${invites.length} carriers want to work with you`}
              </h3>
            </div>
            <div style={{ padding: "8px 20px 14px" }}>
              {invites.map(iv => (
                <div className="kv" key={iv.id}>
                  <span className="k">
                    <b style={{ color: "var(--p-ink)" }}>{iv.carrier}</b>
                    <div className="sub">
                      {iv.programme ? `Invited you on to ${iv.programme}` : "Invited you to work with them"}
                      {iv.invited_at && <> · {fmtDate(iv.invited_at)}</>}
                    </div>
                  </span>
                  <span style={{ display: "flex", gap: 8 }}>
                    <button className="btn sm" type="button" disabled={answering === iv.id}
                            onClick={() => answer(iv.id, false)}>Decline</button>
                    <button className="btn sm pri" type="button" disabled={answering === iv.id}
                            onClick={() => answer(iv.id, true)}>
                      {answering === iv.id ? "…" : "Accept"}
                    </button>
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div style={grid(3)}>
          <StatCard title="Carriers" value={c.carriers} icon={Building2}
                    subtitle={`${c.programmes} ${c.programmes === 1 ? "programme" : "programmes"}`} />
          <StatCard title="Active Contracts" value={c.live_contracts} icon={FileCheck2}
                    subtitle="In force" />
          <StatCard title="Pending Signatures" value={c.signatures_pending} icon={PenLine}
                    tone={c.waiting_on_me > 0 ? "alert" : undefined}
                    onClick={c.waiting_on_me > 0 ? () => setWaitingOpen(true) : undefined}
                    subtitle={`${c.signatures_completed} completed`
                      + (c.terms_to_agree ? ` · ${c.terms_to_agree} to agree` : "")} />
          <StatCard title="Exceptions to Review" value={c.agency_exceptions} icon={AlertCircle}
                    tone={c.agency_exceptions > 0 ? "alert" : undefined}
                    subtitle="Across your team" />
          <StatCard title="Files Run This Week" value={ins ? ins.totals.runs_this_week : "—"}
                    icon={Activity} subtitle="By your team and carriers" />
          <StatCard title="Team Members" value={c.users} icon={Users}
                    onClick={() => nav("/broker/users")}
                    subtitle={c.users_invited ? `${c.users_invited} not signed up yet` : "All signed up"} />
        </div>

        {c.programmes === 0 ? (
          <div className="card" style={{ padding: 24 }}>
            <p style={{ margin: 0, color: "var(--p-muted)" }}>
              {invites?.length
                ? "Accept an invitation above and that carrier can put you on their programmes."
                : "No programmes yet — a carrier has to put you on one before your team can send files."}
            </p>
          </div>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 24, marginBottom: 24 }}>
              <ChartCard title="Bordereau Status"
                info={<InfoTip text={`Your team's files over the last ${DAYS} days, by result: clean, flagged with exceptions, or not checked yet.`} />}>
                {!ins ? <div className="muted">Loading…</div> : <RunTrend data={ins.runs_by_day} />}
              </ChartCard>
              <ChartCard title="Team Activity"
                info={<InfoTip text={
                  `What each person on your team sent in the last ${DAYS} days, and how much of it `
                  + "is still waiting — you included, for the bordereaux you send yourself. "
                  + "Each bar is the exceptions on that person's files: amber is "
                  + "what is still open, green what has been put right, and a full grey bar means "
                  + "nothing was flagged at all. An exception is one cell, not a whole row, so the "
                  + "file's size is written beside the bar instead of being drawn. Bars are not "
                  + "compared with each other; the counts on the right are. Showing the busiest 5 "
                  + "— open the full list for everyone."} />}>
                {!ins ? <div className="muted">Loading…</div> : (
                  <UploaderBars cap={5}
                    total={ins.people_total}
                    onViewAll={() => nav("/broker/team-activity")}
                    personTo={r => `/broker/team-activity/${r.id}`}
                    empty="No one on your team yet."
                    rows={(ins.by_person ?? []).map(u => ({
                      id: u.id, name: u.name,
                      note: u.role === "broker_admin" ? "admin" : undefined,
                      files: u.files, uploads: u.uploads, resolved: u.resolved,
                    }))} />
                )}
              </ChartCard>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 24, marginBottom: 24 }}>
              <ChartCard title="Files by Carrier"
                info={<InfoTip text={`Files run for each carrier in the last ${DAYS} days. Showing the top 5 — open the full list for every carrier.`} />}>
                {!ins ? <div className="muted">Loading…</div> : (
                  <RankedBars unit="files" cap={5}
                    total={ins.carriers_total}
                    onViewAll={() => nav("/broker/files-by-carrier")}
                    empty="No files run in this period."
                    rows={ins.by_carrier.map(x => ({ id: x.id, name: x.name, value: x.runs }))} />
                )}
              </ChartCard>
              <LinkCard title="Recent File Submissions" dark icon={Clock}
                        value={ins ? ins.totals.runs_in_window : "—"}
                        label={`files run in ${DAYS} days`}
                        onClick={() => nav("/broker/runs")} />
            </div>
          </>
        )}
        <WaitingDrawer open={waitingOpen} items={d.waiting_on_me}
                       onClose={() => setWaitingOpen(false)} />
      </div>
    </div>
  );
}

/** The contracts behind the Pending Signatures tile, with the step to take. */
function WaitingDrawer({ open, items, onClose }: {
  open: boolean; items: Dash["waiting_on_me"]; onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h">
          <div>
            <h4>Pending Signatures</h4>
            <div className="ref">Contracts waiting on you</div>
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b" style={{ padding: 0 }}>
          <div className="tbl-wrap">
            <table>
              <tbody>
                {items.map(w => (
                  <tr key={w.id}>
                    <td>
                      <Link to={`/contracts/${w.id}`}><b>{w.name}</b></Link>
                      <div className="muted" style={{ fontSize: 12 }}>{w.carrier} · {w.programme}</div>
                    </td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      {/* The carrier has already signed by the time it waits
                          on the broker's signature, so go straight to signing. */}
                      {(w.lifecycle === "agreed" || w.lifecycle === "signed") ? (
                        <a className="btn sm pri" href={inAppSigningUrl(w.id)}
                           target="_blank" rel="noreferrer">Sign →</a>
                      ) : (
                        <Link className="btn sm pri" to={`/contracts/${w.id}`}>Agree terms →</Link>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </aside>
    </>
  );
}
