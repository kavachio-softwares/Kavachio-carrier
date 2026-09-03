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
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Layers, FileText, UserPlus, Users2 } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { OnboardingBadge } from "../components/OnboardingBadge";
import { getBrokers, type BrokerSummary } from "../api/hierarchy";

export default function Brokers() {
  const [rows, setRows] = useState<BrokerSummary[] | null>(null);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    getBrokers().then(setRows).catch(e =>
      setErr(e?.response?.data?.detail || "Could not load brokers"));
  }, []);

  const shown = (rows ?? []).filter(b =>
    !q.trim() || b.legal_name.toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <>
      <PageHeader
        title="Brokers"
        subtitle="The broker organisations that produce into your programmes."
        action={
          <Link
            to="/users/new"
            className="inline-flex items-center gap-1 rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark"
          >
            <UserPlus size={14} /> Invite a broker
          </Link>
        }
      />
      <PageBody>
        {err && (
          <div className="mb-4 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
            {err}
          </div>
        )}

        {rows === null ? (
          <Card><p className="text-sm text-ink-muted">Loading…</p></Card>
        ) : rows.length === 0 ? (
          <Card>
            <p className="text-sm text-ink-muted">
              No brokers yet. A broker is created by inviting its first admin —
              that person is what makes the organisation reachable.
            </p>
            <Link to="/users/new" className="mt-2 inline-block text-sm text-navy hover:underline">
              Invite a broker →
            </Link>
          </Card>
        ) : (
          <>
            <div className="mb-4">
              <input
                className="w-full max-w-sm rounded border border-border px-2.5 py-1.5 text-sm"
                placeholder="Search brokers…"
                value={q}
                onChange={e => setQ(e.target.value)}
              />
            </div>

            <div className="grid gap-3">
              {shown.map(b => (
                <Card key={b.id}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Link
                          to={`/brokers/${b.id}`}
                          className="truncate font-medium text-navy hover:underline"
                        >
                          {b.legal_name}
                        </Link>
                        <OnboardingBadge status={b.onboarding_status} />
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-muted">
                        <span className="inline-flex items-center gap-1">
                          <Layers size={12} />
                          {b.programmes.length} programme{b.programmes.length === 1 ? "" : "s"}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <FileText size={12} />
                          {b.contract_count} contract{b.contract_count === 1 ? "" : "s"}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <Users2 size={12} />
                          {b.user_count} user{b.user_count === 1 ? "" : "s"}
                        </span>
                        {b.pending_approvals > 0 && (
                          // Still worth surfacing — a contract they submitted is
                          // not live until it is approved — but there is no
                          // Approvals screen to link to any more.
                          <span className="text-warn">
                            {b.pending_approvals} awaiting approval
                          </span>
                        )}
                      </div>
                      {b.programmes.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {b.programmes.map(p => (
                            <Link
                              key={p.id}
                              to={`/programs/${p.id}/brokers`}
                              className="rounded border border-border px-1.5 py-0.5 text-xs text-ink-muted hover:border-navy hover:text-navy"
                            >
                              {p.name}
                            </Link>
                          ))}
                        </div>
                      )}
                    </div>
                    <Link
                      to={`/brokers/${b.id}`}
                      className="shrink-0 text-sm text-navy hover:underline"
                    >
                      Open →
                    </Link>
                  </div>
                </Card>
              ))}
              {shown.length === 0 && (
                <Card><p className="text-sm text-ink-muted">No broker matches “{q}”.</p></Card>
              )}
            </div>
          </>
        )}
      </PageBody>
    </>
  );
}
