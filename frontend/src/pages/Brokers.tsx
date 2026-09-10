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
 * From here: a broker's own page (what it holds, across every programme), or
 * the programme screen where it is put on one.
 *
 * The row is built around what makes a broker USABLE, in the order it becomes
 * true: are they on a programme, do they hold a contract, has anyone signed in.
 * A broker missing the first of those is the one you came here to find, so the
 * row says so in words rather than leaving a 0 to be spotted.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Layers, FileText, UserPlus, Users2, Search, ArrowRight } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { Metric } from "../components/ui/Metric";
import { OrgAvatar } from "../components/ui/OrgAvatar";
import { Sk } from "../components/ui/Skeleton";
import { OnboardingBadge } from "../components/OnboardingBadge";
import {
  getBrokers, resendBrokerInvitation, revokeBrokerInvitation,
  type BrokerSummary,
} from "../api/hierarchy";
import { fmtDate } from "../utils/date";

export default function Brokers() {
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

  const shown = (rows ?? []).filter(b =>
    !q.trim() || b.legal_name.toLowerCase().includes(q.trim().toLowerCase()));

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
              bg-navy px-3.5 py-2 text-sm font-medium text-white transition hover:bg-navy-dark"
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
          <div className="grid gap-3">
            {Array.from({ length: 3 }, (_, i) => <Sk key={i} className="h-24 w-full" />)}
          </div>
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
                  text-sm font-medium text-white transition hover:bg-navy-dark"
              >
                <UserPlus size={15} /> Invite a broker
              </Link>
            </div>
          </Card>
        ) : (
          <>
            {/* Search and the count sit on one line: the count is what tells you
                the search did something, so it belongs beside the box rather
                than left to be inferred from the list length. */}
            <div className="flex flex-wrap items-center gap-3">
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
                {q.trim()
                  ? `${shown.length} of ${rows.length}`
                  : `${rows.length} broker${rows.length === 1 ? "" : "s"}`}
              </span>
            </div>

            <div className="grid gap-3">
              {shown.map(b => {
                const stranded = b.programmes.length === 0;
                return (
                  <Card key={b.id} className="transition hover:border-navy/30 hover:shadow-md">
                    <div className="flex flex-wrap items-start gap-3.5">
                      <OrgAvatar name={b.legal_name} />

                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Link
                            to={`/brokers/${b.id}`}
                            className="truncate text-[15px] font-semibold text-ink hover:text-navy hover:underline"
                          >
                            {b.legal_name}
                          </Link>
                          {/* The relationship with US, which is not the same
                              as how far the broker has got with their own
                              account: one who works with another carrier is
                              fully set up and still only INVITED here until
                              they answer. */}
                          {b.relationship === "invited" ? (
                            <span className="pill pill-amber">Invited</span>
                          ) : (
                            <OnboardingBadge status={b.onboarding_status} />
                          )}
                        </div>

                        {b.relationship === "invited" && b.invitation && (
                          <div className="mt-1.5 text-[12.5px] text-ink-muted">
                            Invited {b.invitation.email}
                            {b.invitation.invited_at
                              && <> on {fmtDate(b.invitation.invited_at)}</>}
                            {" — waiting for them to accept. "}
                            <span
                              className="cursor-pointer font-medium text-navy hover:underline"
                              role="button" tabIndex={0}
                              onClick={() => resend(b.invitation!.id)}
                            >
                              Send again
                            </span>
                            {" · "}
                            <span
                              className="cursor-pointer font-medium text-ink-muted hover:underline"
                              role="button" tabIndex={0}
                              onClick={() => revoke(b.invitation!.id)}
                            >
                              Withdraw
                            </span>
                          </div>
                        )}

                        <div className="mt-2 flex flex-wrap items-center gap-1.5">
                          {/* No programme is the state that blocks everything
                              else, so it is named rather than counted. */}
                          {stranded ? (
                            <Metric
                              icon={<Layers size={12} />}
                              value="No"
                              label="programme yet"
                              tone="attention"
                              title="Until they are on a programme they cannot produce anything."
                            />
                          ) : (
                            <Metric
                              icon={<Layers size={12} />}
                              value={b.programmes.length}
                              label={b.programmes.length === 1 ? "programme" : "programmes"}
                            />
                          )}
                          <Metric
                            icon={<FileText size={12} />}
                            value={b.contract_count}
                            label={b.contract_count === 1 ? "contract" : "contracts"}
                          />
                          <Metric
                            icon={<Users2 size={12} />}
                            value={b.user_count}
                            label={b.user_count === 1 ? "user" : "users"}
                          />
                        </div>

                        {b.programmes.length > 0 && (
                          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                            <span className="text-[10.5px] uppercase tracking-wide text-ink-soft">
                              On
                            </span>
                            {b.programmes.map(p => (
                              <Link
                                key={p.id}
                                to={`/programs/${p.id}/brokers`}
                                className="rounded-md border border-border px-2 py-0.5 text-[11.5px]
                                  text-ink-muted transition hover:border-navy hover:text-navy"
                              >
                                {p.name}
                              </Link>
                            ))}
                          </div>
                        )}
                      </div>

                      <Link
                        to={`/brokers/${b.id}`}
                        className="inline-flex shrink-0 items-center gap-1 self-center rounded-md border
                          border-border px-2.5 py-1.5 text-[12.5px] font-medium text-ink-muted
                          transition hover:border-navy hover:text-navy"
                      >
                        Open <ArrowRight size={13} />
                      </Link>
                    </div>
                  </Card>
                );
              })}

              {shown.length === 0 && (
                <Card>
                  <p className="py-8 text-center text-sm text-ink-muted">
                    No broker matches “{q}”.
                  </p>
                </Card>
              )}
            </div>
          </>
        )}
      </PageBody>
    </>
  );
}
