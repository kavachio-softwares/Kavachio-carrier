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
  getBrokerDashboard, getBrokerInsights, getBrokerInvitations, getBrokerRunHistory,
  acceptBrokerInvitation, declineBrokerInvitation,
  type BrokerDashboard as Dash, type BrokerInsights, type BrokerInvitation, type OperatorRun,
} from "../api/broker";
import { useBrokerCarrierId } from "../brokerCarrier";
import { fmtDate, fmtDateTime } from "../utils/date";
import { inAppSigningUrl } from "../api/esign";
import { AlertCircle, Building2, CalendarClock, FileCheck2, PenLine } from "lucide-react";
import { RunTrend } from "../components/BrokerCharts";
import { DayFilesDrawer, type DayRuns } from "../components/DayFilesDrawer";
import { api } from "../api/client";
import { InfoTip } from "../components/InfoTip";
import { ChartCard, StatCard } from "../components/StatCard";

/** The window the status chart describes. */
const DAYS = 30;

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Which bordereaux are overdue, in words that fit under a tile.
 *
 *  Periods are "YYYY-MM" for monthly programmes; anything else (a quarter) is
 *  shown as it is. Two programmes owing the same month are one month here —
 *  the count beside it already says how many files. Up to three months are
 *  named ("May, Jun 2026"); more than that becomes a range, because a list of
 *  seven months under a tile is a paragraph, not a hint. */
function overdueMonths(periods: string[]): string {
  const uniq = [...new Set(periods)];
  const parsed = uniq.map(p => {
    const m = /^(\d{4})-(\d{2})$/.exec(p);
    return m ? { y: m[1], m: MON[Number(m[2]) - 1] ?? m[2] } : { y: "", m: p };
  });
  if (parsed.length === 0) return "";
  const full = (x: typeof parsed[number]) => (x.y ? `${x.m} ${x.y}` : x.m);
  if (parsed.length > 3) {
    return `${full(parsed[0])} – ${full(parsed[parsed.length - 1])}`;
  }
  const oneYear = parsed.every(x => x.y && x.y === parsed[0].y);
  return oneYear
    ? `${parsed.map(x => x.m).join(", ")} ${parsed[0].y}`
    : parsed.map(full).join(", ");
}

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
  // The day whose files are open in the side drawer — a click on a bar of
  // Bordereau Status, same as the carrier dashboard.
  const [statusDay, setStatusDay] = useState<string | null>(null);
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

        <div style={grid(5)}>
          <StatCard title="Carriers" value={c.carriers} icon={Building2}
                    subtitle={`${c.programmes} ${c.programmes === 1 ? "programme" : "programmes"}`} />
          {/* A contract ending within 30 days is the one thing about the book
              a broker has to act on ahead of time — renew, or stop producing
              against it — so it replaces the plain "In force" when there is one. */}
          <StatCard title="Active Contracts" value={c.live_contracts} icon={FileCheck2}
                    subtitle={c.contracts_expiring > 0 ? (
                      <span style={{ color: "var(--p-warn)", fontWeight: 600 }}>
                        {c.contracts_expiring === 1
                          ? `1 expires on ${fmtDate(c.next_expiry)}`
                          : `${c.contracts_expiring} expire within 30 days`}
                      </span>
                    ) : "No upcoming expiries"}
                    onClick={() => nav("/broker/contracts?status=active")} />
          {/* Named for WHAT is signed. "Pending Signatures" left a new broker
              asking whose signature, on what — it is always a contract with a
              carrier, agreed and waiting for this broker to sign it. */}
          <StatCard title="Contracts to Sign" value={c.signatures_pending} icon={PenLine}
                    subtitle={
                      <>
                        {/* A contract still in REVIEW comes before any
                            signature, and is just as much waiting on this
                            broker — without this line a broker with terms to
                            read saw "0" and nothing to do. */}
                        {c.terms_to_agree > 0 && (
                          <div style={{ color: "var(--p-crit)", fontWeight: 600 }}>
                            {c.terms_to_agree} {c.terms_to_agree == 1 ? 'contract' : 'contracts' } waiting for your review
                          </div>
                        )}
                        <div>{c.signatures_completed} already signed</div>
                      </>
                    }
                    info="Contracts with your carriers that need you. Waiting for your review: read the terms and agree them or ask for changes. To sign: terms are agreed and your signature is needed before the contract becomes active. Click to open them."
                    tone={c.waiting_on_me > 0 ? "alert" : undefined}
                    onClick={c.waiting_on_me > 0 ? () => setWaitingOpen(true) : undefined} />
          <StatCard title="Exceptions to Review" value={c.agency_exceptions} icon={AlertCircle}
                    tone={c.agency_exceptions > 0 ? "alert" : undefined}
                    subtitle="Still open on your files"
                    onClick={() => nav("/broker/exceptions")} />
          {/* What is owed next. Opens the calendar on the month that matters:
              the oldest missed file's if there is one — the calendar itself
              opens on the current month, where a June miss is not on screen —
              otherwise the next one due. */}
          <StatCard title="Bordereaux Due" value={c.files_upcoming} icon={CalendarClock}
                    subtitle={
                      <>
                        {c.files_overdue > 0 && (
                          <div style={{ color: "var(--p-crit)", fontWeight: 600 }}
                               title={c.overdue_periods.join(", ")}>
                            {c.files_overdue} overdue · {overdueMonths(c.overdue_periods)}
                          </div>
                        )}
                        <div>
                          {c.files_upcoming > 0 || c.next_due
                            ? `in the next 30 days${c.next_due ? ` · next ${fmtDate(c.next_due)}` : ""}`
                            : "nothing scheduled"}
                        </div>
                      </>
                    }
                    info="Bordereaux you still have to process that are due in the next 30 days. Overdue ones are past their due date, named by the month they report on. Click to open your Bordereau Calendar."
                    tone={c.files_overdue > 0 ? "alert" : undefined}
                    onClick={() => {
                      const at = c.first_overdue ?? c.next_due;
                      nav(at ? `/broker/calendar?month=${at.slice(0, 7)}` : "/broker/calendar");
                    }} />
        </div>

        {c.programmes === 0 ? (
          <div className="card" style={{ padding: 24 }}>
            <p style={{ margin: 0, color: "var(--p-muted)" }}>
              {invites?.length
                ? "Accept an invitation above and that carrier can put you on their programmes."
                : "No programmes yet — a carrier has to put you on one before you can send files."}
            </p>
          </div>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 24, marginBottom: 24 }}>
              <ChartCard title="Bordereau Status"
                info={<InfoTip text={`Your files over the last ${DAYS} days, by result: a clean file, or one with exceptions to review. The blue line (right-hand scale) is how many exceptions were resolved each day. Click a day to see its files.`} />}>
                {!ins ? <div className="muted">Loading…</div> : <RunTrend data={ins.runs_by_day} audience="broker" showResolved onDayClick={setStatusDay} />}
              </ChartCard>
              <RecentFiles />
            </div>
          </>
        )}
        <DayFilesDrawer day={statusDay} from="broker"
          load={dd => api.get<DayRuns>("/broker/runs-on-day", { params: { day: dd } })
            .then(a => a.data)}
          onClose={() => setStatusDay(null)} />
        <WaitingDrawer open={waitingOpen} items={d.waiting_on_me}
                       onClose={() => setWaitingOpen(false)} />
      </div>
    </div>
  );
}

/** The last few bordereaux processed, each one opening its exceptions.
 *
 *  Replaces a dark link card that held one number ("8 files run in 30 days")
 *  and nothing to act on. The list answers what that number could not: which
 *  file, for which programme, and did it come out clean. */
function RecentFiles() {
  const nav = useNavigate();
  const [data, setData] = useState<{ items: OperatorRun[]; total: number } | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    getBrokerRunHistory(1, 5).then(setData).catch(() => setErr(true));
  }, []);
  const open = (r: OperatorRun) =>
    nav(`/uploads/${r.export_id}/exceptions?download=${r.export_id}&from=broker`);

  return (
    <ChartCard title="Recently Processed Files"
      info={<InfoTip text="The last five bordereaux processed for you, newest first. Click one to see its exceptions." />}>
      {err ? <div className="muted">Could not load your files.</div>
        : !data ? <div className="muted">Loading…</div>
        : data.items.length === 0 ? <div className="muted">No files processed yet.</div>
        : (
          <div style={{ display: "flex", flexDirection: "column", flex: 1 }}>
            {data.items.map(r => (
              <div key={r.export_id} role="link" tabIndex={0}
                onClick={() => open(r)}
                onKeyDown={e => { if (e.key === "Enter") open(r); }}
                style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 4px",
                         borderBottom: "1px solid var(--p-border)", cursor: "pointer" }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden",
                                textOverflow: "ellipsis" }} title={r.filename}>
                    {r.filename}
                  </div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {r.programme ?? "—"} · {fmtDateTime(r.created_at)}
                  </div>
                </div>
                {r.exception_count > 0
                  ? <span className="badge b-crit"><span className="d" />{r.exception_count} exceptions</span>
                  : <span className="badge b-ok"><span className="d" />Clean</span>}
              </div>
            ))}
            <div style={{ marginTop: "auto", paddingTop: 12, textAlign: "right" }}>
              <Link className="linkish" to="/broker/runs">
                View all {data.total} processed files →
              </Link>
            </div>
          </div>
        )}
    </ChartCard>
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
          <WaitingGroup heading="Contracts to Sign" items={toSign} action="sign"
                        note="Terms already settled — these need your signature." />
        </div>
      </aside>
    </>
  );
}
