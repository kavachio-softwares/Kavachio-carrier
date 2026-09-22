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
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChevronRight, FileText, Layers, Search, UserPlus, Users2 } from "lucide-react";
import { OrgAvatar } from "../components/ui/OrgAvatar";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import {
  getBrokersPaged, resendBrokerInvitation, revokeBrokerInvitation,
  type BrokerSummary,
} from "../api/hierarchy";
import { fmtDate } from "../utils/date";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useServerList } from "../hooks/useServerList";
import { seesAllBrokers, useCarrierSeat } from "../hooks/useCarrierSeat";
import { getUser } from "../auth";

/** How many programme chips a row shows before it says "+N more". Enough for
 *  the common case, few enough that one busy broker cannot make its row tall. */
const MAX_CHIPS = 2;

/** Rows per page. The server cuts the page, so this is what gets fetched. */
const PAGE_SIZE = 10;

/** The onboarding state as a .proto pill — the same labels and hover text as
 *  OnboardingBadge, drawn in the badge styles every other list here uses. */
const ONBOARDING: Record<string, { label: string; cls: string; title: string }> = {
  not_invited: { label: "Not invited", cls: "b-mut",
    title: "This broker is on your list but nobody there has a login yet." },
  invited: { label: "Invited", cls: "b-warn",
    title: "Their admin was invited and hasn't used the link yet — worth chasing." },
  active: { label: "Active", cls: "b-ok",
    title: "Someone there has set a password and signed in." },
  suspended: { label: "Suspended", cls: "b-crit",
    title: "The company itself was switched off. Their history stays readable." },
};

export default function Brokers() {
  const nav = useNavigate();
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [note, setNote] = useState("");
  // Debounced because the search runs on the SERVER now — an undebounced box
  // would be one request per keystroke.
  const dq = useDebouncedValue(q, 300);
  // A carrier user's list is the broker companies THEY invited; the carrier
  // admin's is the whole company's (the server decides — `mine` below). An
  // invitation is chased or called off only by the carrier user who sent it,
  // or by the carrier admin, who oversees them all (the server refuses anyone
  // else).
  const seat = useCarrierSeat();
  const me = getUser();
  const mayChase = (byUserId?: number | null) =>
    seesAllBrokers(seat) || (!!me?.id && byUserId === me.id);

  const {
    items: shown, total, page, pageCount, loading, setPage, extra,
    reload: load,
  } = useServerList<BrokerSummary, { stranded?: number }>(
    (pg, size) => getBrokersPaged({
      q: dq.trim() || undefined, page: pg, page_size: size, mine: true,
    }).catch(e => {
      setErr(e?.response?.data?.detail || "Could not load brokers");
      throw e;
    }),
    dq.trim(), PAGE_SIZE,
  );
  const needle = dq.trim();

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

  // Said once above the table, the way Programmes says "has no broker yet",
  // rather than leaving the reader to scan a column for the amber cells.
  // Counted by the server across the WHOLE directory — a count that shrank as
  // you paged would be a different sentence.
  const stranded = extra?.stranded ?? 0;

  const invite = (
    /* One action, whether or not the broker already has a login. Which of the
       two it is depends on facts about somebody else's book, and a second
       button would let a carrier discover them by seeing which one worked. */
    <Link to="/users/new?for=broker" className="btn pri">
      <UserPlus size={15} /> Invite a party
    </Link>
  );

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Party
              <InfoTip text={"Open a party to see its contracts and people across "
                + "every programme. Put a party on a programme from that "
                + "programme's page."} />
            </h2>
            <p>
              {seat === "user"
                ? "The broker companies you invited, and how far each one reaches."
                : "The party organisations that produce into your programmes."}
            </p>
          </div>
          <div className="actions">{invite}</div>
        </div>

        {note && <div className="note ok" style={{ marginBottom: 16 }}>{note}</div>}
        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

        {/* Said once above the table, the way Contracts says a document is
            missing, rather than leaving the reader to scan for amber cells. */}
        {stranded > 0 && (
          <div className="note warn"
               style={{ marginBottom: 16, display: "flex", gap: 9, alignItems: "center" }}>
            <Layers size={15} style={{ flex: "0 0 auto" }} />
            <span>
              <b>
                {stranded === 1
                  ? "One party is not on a programme yet"
                  : `${stranded} parties are not on a programme yet`}
              </b>
              , so {stranded === 1 ? "it" : "they"} cannot produce anything.
            </span>
          </div>
        )}

        {loading && shown.length === 0 ? (
          <div className="card"><div className="empty">Loading…</div></div>
        ) : total === 0 && !needle ? (
          <div className="card">
            <div className="empty">
              <Users2 size={22} style={{ margin: "0 auto 10px", display: "block" }} />
              <b style={{ color: "var(--p-ink)" }}>No parties yet</b>
              <div style={{ maxWidth: 420, margin: "4px auto 14px" }}>
                A party is created by inviting its first admin — that person is
                what makes the organisation reachable.
              </div>
              {invite}
            </div>
          </div>
        ) : (
          <div className="card">
            {/* Search and the count sit on one line: the count is what tells you
                the search did something, so it belongs beside the box rather
                than left to be inferred from the table length. */}
            <div className="card-h" style={{ gap: 14, flexWrap: "wrap" }}>
              <div className="search">
                <Search className="ic" />
                <input placeholder="Search parties…" value={q}
                  onChange={e => setQ(e.target.value)} />
              </div>
              <div className="right">
                <span className="sub">
                  {needle
                    ? `${total} match${total === 1 ? "" : "es"}`
                    : `${total} part${total === 1 ? "y" : "ies"}`}
                </span>
              </div>
            </div>

            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Party</th>
                    <th>Status</th>
                    <th>Programmes</th>
                    <th>Contracts</th>
                    <th style={{ width: 44 }} />
                  </tr>
                </thead>
                <tbody>
                  {shown.map(b => {
                    const invited = b.relationship === "invited";
                    const extra = b.programmes.length - MAX_CHIPS;
                    const look = invited ? ONBOARDING.invited
                      : b.onboarding_status ? ONBOARDING[b.onboarding_status] : undefined;
                    return (
                      <tr
                        key={b.id}
                        className="click party-row"
                        onClick={() => nav(`/brokers/${b.id}`)}
                      >
                        <td>
                          {/* A link as well as a clickable row: the row alone gives
                              no affordance, and cannot be opened in a new tab or
                              reached by keyboard. */}
                          <div className="party-org">
                            <OrgAvatar name={b.legal_name} />
                            <div style={{ minWidth: 0 }}>
                              <Link
                                to={`/brokers/${b.id}`}
                                onClick={e => e.stopPropagation()}
                                className="party-nm"
                              >
                                {b.legal_name}
                              </Link>
                              {invited && b.invitation ? (
                                <div className="sub">
                                  Invited {b.invitation.email}
                                  {b.invitation.invited_at
                                    && <> on {fmtDate(b.invitation.invited_at)}</>}
                                  {mayChase(b.invitation.by_user_id) && <>
                                  {" · "}
                                  <span
                                    className="linkish" role="button" tabIndex={0}
                                    onClick={e => { e.stopPropagation(); resend(b.invitation!.id); }}
                                  >
                                    Send again
                                  </span>
                                  {" · "}
                                  <span
                                    className="linkish mut" role="button" tabIndex={0}
                                    onClick={e => { e.stopPropagation(); revoke(b.invitation!.id); }}
                                  >
                                    Withdraw
                                  </span>
                                  </>}
                                </div>
                              ) : b.dba_name ? (
                                <div className="sub party-dba">{b.dba_name}</div>
                              ) : null}
                            </div>
                          </div>
                        </td>

                        <td>
                          {/* The relationship with US, which is not the same as
                              how far the broker has got with their own account:
                              one who works with another carrier is fully set up
                              and still only INVITED here until they answer. A
                              party that isn't a producer carries no onboarding
                              state at all, and shows a dash. */}
                          {look ? (
                            <span className={`badge ${look.cls}`} title={look.title}>
                              <span className="d" />{look.label}
                            </span>
                          ) : <span className="faint">—</span>}
                        </td>

                        <td>
                          {/* No programme is the state that blocks everything
                              else, so it is named rather than counted. */}
                          {b.programmes.length === 0 ? (
                            <span className="badge b-warn"
                              title="Until they are on a programme they cannot produce anything.">
                              Needs a programme
                            </span>
                          ) : (
                            <div className="party-chips">
                              {b.programmes.slice(0, MAX_CHIPS).map(p => (
                                <Link
                                  key={p.id}
                                  to={`/programs/${p.id}/brokers`}
                                  onClick={e => e.stopPropagation()}
                                  className="party-chip"
                                >
                                  {p.name}
                                </Link>
                              ))}
                              {extra > 0 && (
                                <span
                                  className="sub"
                                  title={b.programmes.slice(MAX_CHIPS).map(p => p.name).join(", ")}
                                >
                                  +{extra} more
                                </span>
                              )}
                            </div>
                          )}
                        </td>

                        <td>
                          <span className={`party-count${b.contract_count === 0 ? " zero" : ""}`}>
                            <FileText size={14} />
                            {b.contract_count}
                          </span>
                        </td>

                        <td>
                          <ChevronRight size={16} className="party-chev" />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {shown.length === 0 && (
                <div className="empty">No party matches “{q}”.</div>
              )}
            </div>

            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={total} onPageChange={setPage} noun="parties" />
          </div>
        )}
      </div>
    </div>
  );
}
