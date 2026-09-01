/**
 * One broker, seen from THIS carrier's side only.
 *
 * The same broker may produce far more business for someone else; none of that
 * is this carrier's to see. Everything on this page is scoped to the
 * relationship the signed-in carrier actually has.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Loader2, FileText, Layers, UserCog } from "lucide-react";
import { getBroker, type BrokerDetail as Detail } from "../api/hierarchy";
import { fmtStamp } from "../utils/date";
import Card from "../components/ui/Card";
import { PageBody, PageHeader } from "../components/Layout";

const APPROVAL_LABEL: Record<string, { text: string; cls: string }> = {
  approved:         { text: "Live",        cls: "bg-success/10 text-success" },
  pending_approval: { text: "Waiting on you", cls: "bg-warn/10 text-warn" },
  rejected:         { text: "Sent back",   cls: "bg-danger/10 text-danger" },
};

export default function BrokerDetail() {
  const { brokerId } = useParams();
  const [b, setB] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);

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

  return (
    <>
      <PageHeader
        title={b.legal_name}
        subtitle="What this broker holds with you. Anything they do for another carrier is not shown here — and your book is not shown to them."
        action={<Link to="/brokers" className="text-sm text-navy hover:underline inline-flex items-center gap-1">
          <ArrowLeft size={14} /> All brokers
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

        <Card title="Contracts">
          {b.contracts.length === 0 ? (
            <p className="text-sm text-ink-muted">No contracts with this broker yet.</p>
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
                        <span className="inline-flex items-center gap-2">
                          <FileText size={14} className="text-ink-muted" />
                          {c.filename ?? `Contract ${c.id}`}
                        </span>
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
      </PageBody>
    </>
  );
}
