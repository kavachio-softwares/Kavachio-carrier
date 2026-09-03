/**
 * One broker, seen from THIS carrier's side only.
 *
 * The same broker may produce far more business for someone else; none of that
 * is this carrier's to see. Everything on this page is scoped to the
 * relationship the signed-in carrier actually has.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Loader2, FileText, Layers, UserCog, Plus } from "lucide-react";
import { getBroker, type BrokerDetail as Detail } from "../api/hierarchy";
import { fmtStamp } from "../utils/date";
import Card from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { PageBody, PageHeader } from "../components/Layout";
import { OnboardingBadge } from "../components/OnboardingBadge";
import AddContractModal from "../components/AddContractModal";

const APPROVAL_LABEL: Record<string, { text: string; cls: string }> = {
  approved:         { text: "Live",        cls: "bg-success/10 text-success" },
  pending_approval: { text: "Waiting on you", cls: "bg-warn/10 text-warn" },
  rejected:         { text: "Sent back",   cls: "bg-danger/10 text-danger" },
};

export default function BrokerDetail() {
  const { brokerId } = useParams();
  const [b, setB] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  // What the last add produced, so the page can say where it went rather than
  // leaving the user to spot a new row.
  const [added, setAdded] = useState<{ id: number; programId: number } | null>(null);

  const load = useCallback(() => {
    if (!brokerId) return;
    getBroker(Number(brokerId))
      .then(setB)
      .catch(() => setErr("That broker is not on any of your programmes."));
  }, [brokerId]);
  useEffect(load, [load]);

  if (err) return (
    <>
      <PageHeader title="Broker" />
      <PageBody><Card><p className="text-sm text-ink-muted py-6 text-center">{err}</p></Card></PageBody>
    </>
  );
  if (!b) return (
    <>
      <PageHeader title="Broker" />
      <PageBody>
        <div className="flex items-center gap-2 text-sm text-ink-muted">
          <Loader2 size={15} className="animate-spin" /> Loading…
        </div>
      </PageBody>
    </>
  );

  // Only programmes the broker is still ON can take a new contract — a link
  // that was taken off keeps its contracts readable but produces nothing more.
  const live = b.programmes.filter(p => p.status === "active");

  return (
    <>
      <PageHeader
        title={b.legal_name}
        subtitle="What this broker holds with you. Anything they do for another carrier is not shown here — and your book is not shown to them."
        action={<Link to="/programs" className="text-sm text-navy hover:underline inline-flex items-center gap-1">
          <ArrowLeft size={14} /> Programmes
        </Link>}
      />
      <PageBody>
        <div className="grid gap-5 md:grid-cols-3">
          <Card title="Programmes" className="md:col-span-1">
            {b.programmes.length === 0 ? (
              <p className="text-sm text-ink-muted">
                Not on a programme yet, so they cannot produce anything.
              </p>
            ) : (
              <ul className="space-y-2.5">
                {b.programmes.map(p => (
                  <li key={p.id} className="flex items-start gap-2 text-sm">
                    <Layers size={14} className="mt-0.5 text-ink-muted shrink-0" />
                    <div>
                      <div className={p.status === "active" ? "font-medium" : "text-ink-soft line-through"}>
                        {p.name}
                      </div>
                      <div className="text-xs text-ink-muted">
                        {p.status === "active"
                          ? `on since ${fmtStamp(p.assigned_at)}`
                          : "taken off — their contracts stay readable"}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Their people" className="md:col-span-2">
            {/* The onboarding badge sits here rather than by the title because
                this is where it comes from — it is derived from the very list
                underneath it, so the two can never appear to disagree. */}
            <div className="mb-3 flex items-center gap-2 text-sm">
              <OnboardingBadge status={b.onboarding_status} />
              <span className="text-ink-muted">
                {b.onboarding_status === "active"
                  ? "— User access is enabled."
                  : b.onboarding_status === "invited"
                    ? "— Awaiting user activation."
                    : b.onboarding_status === "suspended"
                      ? "— User access is currently disabled."
                      : "Not onboarded — No user access has been provisioned."}
              </span>
            </div>
            {b.users.length === 0 ? (
              <p className="text-sm text-ink-muted">
                Nobody from this broker has a login yet.
              </p>
            ) : (
              <ul className="space-y-2.5">
                {b.users.map(u => (
                  <li key={u.id} className="flex items-center gap-3 text-sm">
                    <UserCog size={15} className="text-ink-muted shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="font-medium">{u.full_name}</div>
                      <div className="text-xs text-ink-muted truncate">{u.email}</div>
                    </div>
                    <span className="rounded bg-surface-2 px-2 py-0.5 text-xs">
                      {u.role === "broker_admin" ? "Broker Admin" : "Operator"}
                    </span>
                    <span className={`text-xs ${u.status === "active" ? "text-success" : "text-warn"}`}>
                      {u.status === "active" ? "Active" : "Invited"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        <Card title="Contracts" action={
          // A contract belongs to a (programme x broker) pair, so a broker on no
          // live programme has nothing for one to sit under. Saying that here,
          // on the disabled button, beats letting the click open a dialog whose
          // only content is the same refusal.
          live.length === 0 ? (
            <span className="text-xs text-ink-muted">Put them on a programme first</span>
          ) : (
            <Button onClick={() => setAdding(true)}>
              <Plus size={15} /> Add Contract
            </Button>
          )
        }>
          {added && (
            <div className="mb-3 rounded-md bg-emerald-50 px-3 py-2 text-[12.5px] text-emerald-800">
              Contract read and its clauses saved —{" "}
              <Link to={`/programs/${added.programId}/contracts/${added.id}`}
                className="underline font-medium">see what it produced</Link>. A
              Bordereau Setup for this broker can use it without reading it again.
            </div>
          )}
          {b.contracts.length === 0 ? (
            <p className="text-sm text-ink-muted">
              No contracts with this broker yet. Add one and it is read straight
              away — the same reading Bordereau Setup does, so a setup can use it
              without going over the document a second time.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-ink-muted border-b border-border">
                  <th className="pb-2 font-medium">Contract</th>
                  <th className="pb-2 font-medium">Term</th>
                  <th className="pb-2 font-medium">Where it stands</th>
                  <th className="pb-2 font-medium">Added</th>
                </tr>
              </thead>
              <tbody>
                {b.contracts.map(c => {
                  const badge = APPROVAL_LABEL[c.approval_status] ?? { text: c.approval_status, cls: "bg-surface-2" };
                  return (
                    <tr key={c.id} className="border-b border-border last:border-0">
                      <td className="py-3">
                        {/* Opens what the contract PRODUCED — its clauses and the
                            rules written from them. That page is the whole point
                            of adding one here, so the name is the way in. */}
                        {c.program_id != null ? (
                          <Link to={`/programs/${c.program_id}/contracts/${c.id}`}
                            className="inline-flex items-center gap-2 text-navy hover:underline">
                            <FileText size={14} className="text-ink-muted" />
                            {c.filename ?? `Contract ${c.id}`}
                          </Link>
                        ) : (
                          <span className="inline-flex items-center gap-2">
                            <FileText size={14} className="text-ink-muted" />
                            {c.filename ?? `Contract ${c.id}`}
                          </span>
                        )}
                      </td>
                      <td className="py-3 text-ink-muted">
                        {c.inception_dt && c.expiry_dt ? `${c.inception_dt} → ${c.expiry_dt}` : "—"}
                      </td>
                      <td className="py-3">
                        <span className={`rounded px-2 py-0.5 text-xs font-medium ${badge.cls}`}>
                          {badge.text}
                        </span>
                      </td>
                      <td className="py-3 text-ink-muted">{fmtStamp(c.created_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>

        <AddContractModal
          open={adding}
          onClose={() => setAdding(false)}
          broker={{ id: b.id, legal_name: b.legal_name }}
          programmes={b.programmes}
          onAdded={(contractId, programId) => {
            setAdded({ id: contractId, programId });
            // Re-read the broker so the new contract appears in the table with
            // the term and approval state the server actually recorded.
            load();
          }} />
      </PageBody>
    </>
  );
}
