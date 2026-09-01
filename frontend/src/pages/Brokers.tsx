/**
 * Brokers — every broker this carrier works with.
 *
 * Sourced from program_broker, not from who created the party: a broker the
 * carrier did not create still belongs on this list the moment it is put on
 * one of the carrier's programmes. That is the whole point of a broker being a
 * `party` rather than a tenant — the same organisation produces for several
 * carriers, and each one sees only its own side of the relationship.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Users2, Loader2, AlertTriangle, Layers } from "lucide-react";
import { getBrokers, type BrokerSummary } from "../api/hierarchy";
import Card from "../components/ui/Card";
import { PageBody, PageHeader } from "../components/Layout";

export default function Brokers() {
  const [rows, setRows] = useState<BrokerSummary[] | null>(null);

  const load = useCallback(() => {
    getBrokers().then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(load, [load]);

  const pending = (rows ?? []).reduce((n, b) => n + b.pending_approvals, 0);

  return (
    <>
      <PageHeader
        title="Brokers"
        subtitle="The brokers producing business into your programmes. One broker can sit on several of your programmes — and can produce for other carriers too, which is none of your business and none of theirs."
      />
      <PageBody>
        {rows === null && (
          <div className="flex items-center gap-2 text-sm text-ink-muted">
            <Loader2 size={15} className="animate-spin" /> Loading…
          </div>
        )}

        {rows?.length === 0 && (
          <Card>
            <div className="py-10 text-center">
              <Users2 size={26} className="mx-auto text-ink-soft mb-3" />
              <p className="text-sm font-medium">No brokers yet.</p>
              <p className="text-sm text-ink-muted mt-1">
                Add a broker from a programme — putting them on it is what lets them produce.
              </p>
            </div>
          </Card>
        )}

        {pending > 0 && (
          <div className="rounded-md border border-warn/30 bg-warn/5 px-4 py-3 text-sm flex items-center gap-2">
            <AlertTriangle size={16} className="text-warn shrink-0" />
            <span className="flex-1">
              {pending} contract{pending === 1 ? "" : "s"} waiting on you.
            </span>
            <Link className="text-navy font-medium hover:underline" to="/approvals">Go to Approvals →</Link>
          </div>
        )}

        {rows && rows.length > 0 && (
          <Card>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-ink-muted border-b border-border">
                  <th className="pb-2 font-medium">Broker</th>
                  <th className="pb-2 font-medium">On your programmes</th>
                  <th className="pb-2 font-medium text-right">Contracts</th>
                  <th className="pb-2 font-medium text-right">Waiting</th>
                  <th className="pb-2 font-medium text-right">People</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(b => (
                  <tr key={b.id} className="border-b border-border last:border-0">
                    <td className="py-3">
                      <Link className="font-medium text-navy hover:underline" to={`/brokers/${b.id}`}>
                        {b.legal_name}
                      </Link>
                      {b.dba_name && <div className="text-ink-muted text-xs">{b.dba_name}</div>}
                    </td>
                    <td className="py-3">
                      {b.programmes.length === 0 ? (
                        // Not an error — it is exactly the state worth showing:
                        // a broker in the directory that cannot yet produce.
                        <span className="text-ink-muted">Not on a programme yet</span>
                      ) : (
                        <div className="flex flex-wrap gap-1.5">
                          {b.programmes.map(p => (
                            <span key={p.id}
                              className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs ${
                                p.status === "active"
                                  ? "bg-surface-2 text-ink"
                                  : "bg-surface-2 text-ink-soft line-through"}`}
                              title={p.status === "active" ? "" : "Taken off — their contracts stay readable"}>
                              <Layers size={11} /> {p.name}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="py-3 text-right tabular-nums">{b.contract_count}</td>
                    <td className="py-3 text-right tabular-nums">
                      {b.pending_approvals > 0
                        ? <span className="text-warn font-medium">{b.pending_approvals}</span>
                        : <span className="text-ink-soft">—</span>}
                    </td>
                    <td className="py-3 text-right tabular-nums">
                      {b.user_count || <span className="text-ink-soft">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </PageBody>
    </>
  );
}
