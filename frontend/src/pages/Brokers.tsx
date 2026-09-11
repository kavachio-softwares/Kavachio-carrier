/**
 * Brokers — every broker organisation this carrier holds.
 *
 * The carrier's counterparties, in one place. A carrier does not manage other
 * CARRIERS: Kavachio creates those (Platform → Carriers), and this tenant IS
 * one. What a carrier manages is the brokers that produce into its programmes,
 * which is what this screen lists.
 *
 * There is no "add broker" button here on purpose. A broker comes into
 * existence by inviting its FIRST ADMIN — one step, in Users & Roles — because
 * a broker with nobody in it can never be reached (see AddUser). So the action
 * links there rather than offering a second way in that would leave brokers
 * with no way to log in.
 *
 * A TABLE, the same shape as Programmes beside it in the sidebar. The list
 * used to be a stack of cards, each a different height depending on how many
 * programmes it named, so the eye could not run down a column to compare two
 * brokers' contracts or users. One row per broker, one column per fact, and
 * the facts line up. It carries no table classes of its own: the app-wide
 * rules in index.css give it the same header band, padding and hover as every
 * other list.
 *
 * The columns keep the old reading order — are they on a programme, do they
 * hold a contract, has anyone signed in — and the state that blocks everything
 * (no programme) is still said in words, not left as a 0 to be spotted.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChevronRight, FileText, Layers, Search, UserPlus, Users2 } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { OrgAvatar } from "../components/ui/OrgAvatar";
import { Sk } from "../components/ui/Skeleton";
import { OnboardingBadge } from "../components/OnboardingBadge";
import {
  getBrokers, resendBrokerInvitation, revokeBrokerInvitation,
  type BrokerSummary,
} from "../api/hierarchy";
import { fmtDate } from "../utils/date";

/** How many programme chips a row shows before it says "+N more". Enough for
 *  the common case, few enough that one busy broker cannot make its row tall. */
const MAX_CHIPS = 2;

export default function Brokers() {
  const nav = useNavigate();
  const [rows, setRows] = useState<BrokerSummary[] | null>(null);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");

  const [note, setNote] = useState("");
  const load = () => getBrokers().then(setRows).catch(e =>
    setErr(e?.response?.data?.detail || "Could not load brokers"));
  useEffect(() => { load(); }, []);

  // An unanswered invitation was a dead end: nothing showed it, and inviting
  // again was refused. These are the two things a carrier can actually do
  // about one.
  async function resend(id: number) {
    setNote("");
    try { setNote((await resendBrokerInvitation(id)).message ?? "Sent again."); }
    catch { setNote("Could not send that again."); }
  }
  async function revoke(id: number) {
    setNote("");
    try {
      setNote((await revokeBrokerInvitation(id)).message ?? "Withdrawn.");
      load();
    } catch { setNote("Could not withdraw that invitation."); }
  }

  const needle = q.trim().toLowerCase();
  const shown = (rows ?? []).filter(b =>
    !needle
    || b.legal_name.toLowerCase().includes(needle)
    || (b.dba_name ?? "").toLowerCase().includes(needle));

  // Said once above the table, the way Programmes says "has no broker yet",
  // rather than leaving the reader to scan a column for the amber cells.
  const stranded = (rows ?? []).filter(b => b.programmes.length === 0).length;

  return (
    <>
      <PageHeader
        title="Brokers"
        subtitle="The broker organisations that produce into your programmes."
        action={
          /* One action, whether or not the broker already has a login. Which
             of the two it is depends on facts about somebody else's book, and
             a second button would let a carrier discover them by seeing which
             one worked. */
          <Link
            to="/users/new"
            className="inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md
              bg-navy px-3.5 py-2 text-sm font-medium text-white transition hover:bg-navy-dark
              hover:no-underline"
          >
            <UserPlus size={15} /> Invite a broker
          </Link>
        }
      />
      <PageBody>
        {note && (
          <div className="rounded-md border border-ok/40 bg-ok/10 px-3 py-2 text-sm">
            {note}
          </div>
        )}
        {err && (
          <div className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
            {err}
          </div>
        )}

        {rows === null ? (
          <Card>
            <div className="space-y-2">
              {Array.from({ length: 3 }, (_, i) => <Sk key={i} className="h-12 w-full" />)}
            </div>
          </Card>
        ) : rows.length === 0 ? (
          <Card>
            <div className="py-12 text-center">
              <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2">
                <Users2 size={20} className="text-ink-soft" />
              </div>
              <p className="text-sm font-medium">No brokers yet</p>
              <p className="mx-auto mt-1 max-w-md text-sm text-ink-muted">
                A broker is created by inviting its first admin — that person is
                what makes the organisation reachable.
              </p>
              <Link
                to="/users/new"
                className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-navy px-3.5 py-2
                  text-sm font-medium text-white transition hover:bg-navy-dark hover:no-underline"
              >
                <UserPlus size={15} /> Invite a broker
              </Link>
            </div>
          </Card>
        ) : (
          <Card>
            {/* Search and the count sit on one line: the count is what tells you
                the search did something, so it belongs beside the box rather
                than left to be inferred from the table length. */}
            <div className="mb-3 flex flex-wrap items-center gap-3">
              <div className="input flex max-w-sm flex-1 items-center gap-2">
                <Search size={14} className="shrink-0 text-ink-soft" />
                <input
                  className="flex-1 bg-transparent text-sm outline-none"
                  placeholder="Search brokers…"
                  value={q}
                  onChange={e => setQ(e.target.value)}
                />
              </div>
              <span className="text-xs text-ink-muted">
                {needle
                  ? `${shown.length} of ${rows.length}`
                  : `${rows.length} broker${rows.length === 1 ? "" : "s"}`}
              </span>
            </div>

            {stranded > 0 && (
              <div className="mb-3 flex items-center gap-2 rounded-md bg-warn/10 px-3 py-2 text-[12.5px] text-warn">
                <Layers size={14} className="shrink-0" />
                {stranded === 1
                  ? "One broker is not on a programme yet, so it cannot produce anything."
                  : `${stranded} brokers are not on a programme yet, so they cannot produce anything.`}
              </div>
            )}

            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Broker</th>
                    <th>Status</th>
                    <th>Programmes</th>
                    <th>Contracts</th>
                    <th>Users</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {shown.map(b => {
                    const invited = b.relationship === "invited";
                    const extra = b.programmes.length - MAX_CHIPS;
                    return (
                      <tr
                        key={b.id}
                        className="group cursor-pointer"
                        onClick={() => nav(`/brokers/${b.id}`)}
                      >
                        <td>
                          {/* A link as well as a clickable row: the row alone gives
                              no affordance, and cannot be opened in a new tab or
                              reached by keyboard. */}
                          <div className="flex items-center gap-2.5">
                            <OrgAvatar name={b.legal_name} />
                            <div className="min-w-0">
                              <Link
                                to={`/brokers/${b.id}`}
                                onClick={e => e.stopPropagation()}
                                className="block truncate font-medium text-navy hover:underline"
                              >
                                {b.legal_name}
                              </Link>
                              {invited && b.invitation ? (
                                <div className="text-xs text-ink-muted">
                                  Invited {b.invitation.email}
                                  {b.invitation.invited_at
                                    && <> on {fmtDate(b.invitation.invited_at)}</>}
                                  {" · "}
                                  <button
                                    type="button"
                                    className="linkish text-xs"
                                    onClick={e => { e.stopPropagation(); resend(b.invitation!.id); }}
                                  >
                                    Send again
                                  </button>
                                  {" · "}
                                  <button
                                    type="button"
                                    className="linkish mut text-xs"
                                    onClick={e => { e.stopPropagation(); revoke(b.invitation!.id); }}
                                  >
                                    Withdraw
                                  </button>
                                </div>
                              ) : b.dba_name ? (
                                <div className="truncate text-xs text-ink-muted">{b.dba_name}</div>
                              ) : null}
                            </div>
                          </div>
                        </td>

                        <td>
                          {/* The relationship with US, which is not the same as
                              how far the broker has got with their own account:
                              one who works with another carrier is fully set up
                              and still only INVITED here until they answer. */}
                          {invited
                            ? <span className="pill pill-amber">Invited</span>
                            : <OnboardingBadge status={b.onboarding_status} />}
                        </td>

                        <td>
                          {/* No programme is the state that blocks everything
                              else, so it is named rather than counted. */}
                          {b.programmes.length === 0 ? (
                            <span className="pill pill-amber whitespace-nowrap"
                              title="Until they are on a programme they cannot produce anything.">
                              Needs a programme
                            </span>
                          ) : (
                            <div className="flex flex-wrap items-center gap-1.5">
                              {b.programmes.slice(0, MAX_CHIPS).map(p => (
                                <Link
                                  key={p.id}
                                  to={`/programs/${p.id}/brokers`}
                                  onClick={e => e.stopPropagation()}
                                  className="whitespace-nowrap rounded-md border border-border px-2 py-0.5
                                    text-[11.5px] text-ink-muted transition hover:border-navy
                                    hover:text-navy hover:no-underline"
                                >
                                  {p.name}
                                </Link>
                              ))}
                              {extra > 0 && (
                                <span
                                  className="text-[11.5px] text-ink-soft"
                                  title={b.programmes.slice(MAX_CHIPS).map(p => p.name).join(", ")}
                                >
                                  +{extra} more
                                </span>
                              )}
                            </div>
                          )}
                        </td>

                        <td>
                          <span className={`inline-flex items-center gap-1.5 ${
                            b.contract_count === 0 ? "text-ink-soft" : "text-ink"}`}>
                            <FileText size={13} className="text-ink-soft" />
                            <span className="font-medium tabular-nums">{b.contract_count}</span>
                          </span>
                        </td>

                        <td>
                          <span className={`inline-flex items-center gap-1.5 ${
                            b.user_count === 0 ? "text-ink-soft" : "text-ink"}`}>
                            <Users2 size={13} className="text-ink-soft" />
                            <span className="font-medium tabular-nums">{b.user_count}</span>
                          </span>
                        </td>

                        <td className="w-10">
                          <ChevronRight
                            size={16}
                            className="text-ink-soft transition group-hover:translate-x-0.5 group-hover:text-navy"
                          />
                        </td>
                      </tr>
                    );
                  })}

                  {shown.length === 0 && (
                    <tr>
                      <td colSpan={6} className="py-8 text-center text-sm text-ink-muted">
                        No broker matches “{q}”.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <p className="mt-3 border-t border-border pt-3 text-xs text-ink-muted">
              Open a broker to see its contracts and people across every
              programme. Put a broker on a programme from that programme's page.
            </p>
          </Card>
        )}
      </PageBody>
    </>
  );
}
