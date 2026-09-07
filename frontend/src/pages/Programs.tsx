/**
 * Programmes — where the carrier's book starts.
 *
 * This screen used to ask you to pick a PARTY first and then manage "their"
 * programmes, which is the old MGA-era shape: it made the programme belong to
 * a broker. It doesn't. A programme is one type of business the CARRIER writes,
 * and brokers are put onto it afterwards — so the carrier is not a step here,
 * because you are already signed in as it.
 *
 * Click a programme to manage its brokers. Contracts hang off the
 * (programme × broker) pair, never off the programme alone.
 *
 * The table is ordered by what a carrier actually scans for: the programme's
 * name, then how it reports, then whether it can do anything yet. That last one
 * is the reason the broker count is not just a number — a programme with no
 * broker is inert, and saying so on the row is the difference between a list
 * you read and a list you act on.
 */
import { useEffect, useState } from "react";
import { frequencyLabel } from "../constants/frequency";
import { Link, useNavigate } from "react-router-dom";
import { Layers, ChevronRight, FileText, Users2, Plus } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Sk } from "../components/ui/Skeleton";
import { getHierarchy, type HierarchyProgramme } from "../api/hierarchy";

export default function Programs() {
  const nav = useNavigate();
  const [rows, setRows] = useState<HierarchyProgramme[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getHierarchy().then(h => setRows(h.programmes))
      .catch(() => setErr("Could not load your programmes."));
  }, []);

  const needBrokers = (rows ?? []).filter(p => p.broker_count === 0).length;

  return (
    <>
      <PageHeader
        title="Programmes"
        subtitle="One type of business you write — commercial motor, say, reported every month. Put brokers on a programme; your contracts sit underneath it."
        action={
          <Button onClick={() => nav("/programs/new")}>
            <Plus size={15} /> Create Programme
          </Button>
        }
      />
      <PageBody>
        {err && <Card><p className="text-sm text-danger">{err}</p></Card>}

        {rows === null && (
          <Card>
            <div className="space-y-2">
              {Array.from({ length: 3 }, (_, i) => <Sk key={i} className="h-12 w-full" />)}
            </div>
          </Card>
        )}

        {rows && rows.length === 0 && (
          <Card>
            <div className="py-12 text-center">
              <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2">
                <Layers size={20} className="text-ink-soft" />
              </div>
              <p className="text-sm font-medium">No programmes yet</p>
              <p className="mx-auto mt-1 max-w-md text-sm text-ink-muted">
                Create one, then put at least one broker on it — a programme with
                no broker cannot hold a contract.
              </p>
              <div className="mt-4">
                <Button onClick={() => nav("/programs/new")}>
                  <Plus size={15} /> Create Programme
                </Button>
              </div>
            </div>
          </Card>
        )}

        {rows && rows.length > 0 && (
          <Card>
            {/* The one thing worth saying about the whole list, said once above
                it rather than repeated as a footnote nobody connects to a row. */}
            {needBrokers > 0 && (
              <div className="mb-3 flex items-center gap-2 rounded-md bg-warn/10 px-3 py-2 text-[12.5px] text-warn">
                <Users2 size={14} className="shrink-0" />
                {needBrokers === 1
                  ? "One programme has no broker on it yet, so it cannot hold a contract."
                  : `${needBrokers} programmes have no broker on them yet, so they cannot hold a contract.`}
              </div>
            )}

            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Programme</th>
                    <th>Segment</th>
                    <th>Reporting</th>
                    <th>Brokers</th>
                    <th>Contracts</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map(p => (
                    <tr
                      key={p.id}
                      className="group cursor-pointer"
                      onClick={() => nav(`/programs/${p.id}/brokers`)}
                    >
                      <td>
                        {/* A link, not just a clickable row. The row handler alone
                            gave no affordance — nothing looked clickable, so the
                            programme read as a dead label — and it could not be
                            opened in a new tab or reached by keyboard. */}
                        <div className="flex items-center gap-2.5">
                          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-navy/10">
                            <Layers size={15} className="text-navy" />
                          </span>
                          <div className="min-w-0">
                            <Link
                              to={`/programs/${p.id}/brokers`}
                              onClick={e => e.stopPropagation()}
                              className="block truncate font-medium text-navy hover:underline"
                            >
                              {p.name}
                            </Link>
                            {p.product_line && (
                              <div className="truncate text-xs text-ink-muted">{p.product_line}</div>
                            )}
                          </div>
                        </div>
                      </td>
                      <td>
                        {p.business_segment
                          ? <span className="pill pill-grey">{p.business_segment}</span>
                          : <span className="text-ink-soft">—</span>}
                      </td>
                      <td className="text-ink-muted">{frequencyLabel(p.bdx_frequency)}</td>
                      {/* Zero brokers is the state worth calling out, not hiding:
                          nothing can be done with the programme until it has one,
                          so the cell says what to DO rather than showing a 0 the
                          reader has to interpret. */}
                      <td>
                        {p.broker_count === 0 ? (
                          <span className="pill pill-amber whitespace-nowrap"
                            title="A programme with no broker cannot hold a contract.">
                            Needs a broker
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1.5 text-ink">
                            <Users2 size={13} className="text-ink-soft" />
                            <span className="font-medium tabular-nums">{p.broker_count}</span>
                          </span>
                        )}
                      </td>
                      <td>
                        <span className={`inline-flex items-center gap-1.5 ${
                          p.contract_count === 0 ? "text-ink-soft" : "text-ink"}`}>
                          <FileText size={13} className="text-ink-soft" />
                          <span className="font-medium tabular-nums">{p.contract_count}</span>
                        </span>
                      </td>
                      <td className="w-10">
                        <ChevronRight
                          size={16}
                          className="text-ink-soft transition group-hover:translate-x-0.5 group-hover:text-navy"
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <p className="mt-3 border-t border-border pt-3 text-xs text-ink-muted">
              Open a programme to manage the brokers on it. Contracts belong to a
              programme <b className="font-medium">and</b> a broker together, so
              they are listed under the broker that produced them.
            </p>
          </Card>
        )}
      </PageBody>
    </>
  );
}
