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
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Layers, ArrowRight } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { getHierarchy, type HierarchyProgramme } from "../api/hierarchy";

export default function Programs() {
  const nav = useNavigate();
  const [rows, setRows] = useState<HierarchyProgramme[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getHierarchy().then(h => setRows(h.programmes))
      .catch(() => setErr("Could not load your programmes."));
  }, []);

  return (
    <>
      <PageHeader
        title="Programmes"
        subtitle="A programme is one type of business you write — commercial motor, say, reported every month. Everything starts here: you put brokers on a programme, and all your contracts sit underneath it."
        action={
          <button
            className="rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark"
            onClick={() => nav("/programs/new")}
          >
            ＋ Create Programme
          </button>
        }
      />
      <PageBody>
        {err && <Card><p className="text-sm text-danger">{err}</p></Card>}

        {rows && rows.length === 0 && (
          <Card>
            <p className="text-sm text-ink-muted">
              No programmes yet. Create one, and put at least one broker on it —
              a programme with no broker cannot hold a contract.
            </p>
          </Card>
        )}

        {rows && rows.length > 0 && (
          <Card>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-ink-muted">
                  <th className="pb-2 font-medium">Programme</th>
                  <th className="pb-2 font-medium">Segment</th>
                  <th className="pb-2 font-medium">Reporting</th>
                  <th className="pb-2 font-medium text-right">Brokers</th>
                  <th className="pb-2 font-medium text-right">Contracts</th>
                  <th className="pb-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {rows.map(p => (
                  <tr
                    key={p.id}
                    className="cursor-pointer border-b border-border last:border-0 hover:bg-surface-2"
                    onClick={() => nav(`/programs/${p.id}/brokers`)}
                  >
                    <td className="py-3">
                      {/* A link, not just a clickable row. The row handler alone
                          gave no affordance — nothing looked clickable, so the
                          programme read as a dead label — and it could not be
                          opened in a new tab or reached by keyboard. */}
                      <Link
                        to={`/programs/${p.id}/brokers`}
                        onClick={e => e.stopPropagation()}
                        className="flex items-center gap-2 font-medium text-navy hover:underline"
                      >
                        <Layers size={14} className="shrink-0" /> {p.name}
                      </Link>
                      {p.product_line && (
                        <div className="text-xs text-ink-muted">{p.product_line}</div>
                      )}
                    </td>
                    <td className="py-3 text-ink-muted">{p.business_segment || "—"}</td>
                    <td className="py-3 text-ink-muted">{p.bdx_frequency || "—"}</td>
                    {/* Zero brokers is the state worth calling out, not hiding:
                        nothing can be done with the programme until it has one. */}
                    <td className="py-3 text-right tabular-nums">
                      {p.broker_count === 0
                        ? <span className="font-medium text-warn">0</span>
                        : p.broker_count}
                    </td>
                    <td className="py-3 text-right tabular-nums">{p.contract_count}</td>
                    <td className="py-3 text-right">
                      <Link
                        to={`/programs/${p.id}/brokers`}
                        onClick={e => e.stopPropagation()}
                        className="inline-flex items-center gap-1 text-navy hover:underline"
                      >
                        Brokers <ArrowRight size={13} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}

        <p className="mt-4 text-sm text-ink-muted">
          A new programme starts empty. Add at least one broker to it, or there
          is nothing you can do with it.
        </p>
      </PageBody>
    </>
  );
}
