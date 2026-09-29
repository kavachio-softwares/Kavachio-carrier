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
import { fmtDate } from "../utils/date";
import { inAppSigningUrl } from "../api/esign";
import { AlertCircle, Building2, Clock, FileCheck2, PenLine } from "lucide-react";
import { RunTrend } from "../components/BrokerCharts";
import { InfoTip } from "../components/InfoTip";
import { ChartCard, LinkCard, StatCard } from "../components/StatCard";

/** The window the status chart and the recent-files card describe. */
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
            <h2>Dashboard
              <InfoTip text={`${d.broker.name} — your team, your carriers, and what needs you today.`} />
            </h2>
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

        <div style={grid(4)}>
          <StatCard title="Carriers" value={c.carriers} icon={Building2}
                    subtitle={`${c.programmes} ${c.programmes === 1 ? "programme" : "programmes"}`} />
          <StatCard title="Active Contracts" value={c.live_contracts} icon={FileCheck2}
                    subtitle="In force"
                    onClick={() => nav("/broker/contracts?status=active")} />
          <StatCard title="Pending Signatures" value={c.signatures_pending} icon={PenLine}
                    subtitle={`${c.signatures_completed} completed`}
                    info="Contracts whose terms are already agreed and are waiting for your signature."
                    tone={c.signatures_pending > 0 ? "alert" : undefined}
                    onClick={c.signatures_pending > 0 ? () => setWaitingOpen(true) : undefined} />
          <StatCard title="Exceptions to Review" value={c.agency_exceptions} icon={AlertCircle}
                    tone={c.agency_exceptions > 0 ? "alert" : undefined}
                    subtitle="Across your team" />
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
                info={<InfoTip text={`Your team's files over the last ${DAYS} days, by result: a clean file, or one with exceptions to review.`} />}>
                {!ins ? <div className="muted">Loading…</div> : <RunTrend data={ins.runs_by_day} audience="broker" />}
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

/** One group of contracts waiting on the broker, under its own heading.
 *
 *  The two are DIFFERENT JOBS and the drawer says so: agreeing terms is
 *  reading and negotiating, signing is executing something already settled.
 *  A single undivided list made them look like one queue, so somebody working
 *  down it met the two in whatever order the ids fell. */
function WaitingGroup({ heading, note, items, action }: {
  heading: string; note: string;
  items: Dash["waiting_on_me"]; action: "agree" | "sign";
}) {
  if (items.length === 0) return null;
  return (
    <>
      <div style={{ padding: "0 20px" }}>
        <div className="sub-h">{heading} · {items.length}</div>
        <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>{note}</div>
      </div>
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
                  {action === "sign" ? (
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
    </>
  );
}

/** The contracts behind the tile, split the same way the tile is.
 *
 *  Named for what it holds rather than for one of the two things in it: it was
 *  called "Pending Signatures", which is the heading of only its second half,
 *  so a contract waiting to be AGREED sat under a title that did not describe
 *  it. */
function WaitingDrawer({ open, items, onClose }: {
  open: boolean; items: Dash["waiting_on_me"]; onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  // The SAME cut the tile makes (broker_routes: terms_to_agree counts
  // in_review), so the two halves of the tile and the two sections here can
  // never disagree. A contract waiting on the broker is only ever in_review,
  // agreed or signed — see contract_types.WITH_BROKER — so these two groups
  // between them hold every row.
  const toAgree = items.filter(w => w.lifecycle === "in_review");
  const toSign = items.filter(w => w.lifecycle !== "in_review");
  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h">
          <div>
            <h4>Contracts Waiting on You</h4>
            <div className="ref">
              {[toAgree.length && `${toAgree.length} to agree`,
                toSign.length && `${toSign.length} to sign`]
                .filter(Boolean).join(" · ") || "nothing outstanding"}
            </div>
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b" style={{ padding: "0 0 18px" }}>
          <WaitingGroup heading="Pending Contract" items={toAgree} action="agree"
                        note="Terms to read and agree, or ask for changes." />
          {/* The rule between them, drawn only when both are there. */}
          {toAgree.length > 0 && toSign.length > 0 && (
            <div style={{ borderTop: "1px solid var(--p-border)", margin: "18px 0 0" }} />
          )}
          <WaitingGroup heading="Pending Signatures" items={toSign} action="sign"
                        note="Terms already settled — these need your signature." />
        </div>
      </aside>
    </>
  );
}
