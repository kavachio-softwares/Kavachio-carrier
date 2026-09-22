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
import { Layers, ChevronRight, Plus } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { Sk } from "../components/ui/Skeleton";
import { getHierarchy, type HierarchyProgramme } from "../api/hierarchy";
import { Pagination } from "../components/Pagination";
import { ProgrammeStepper, flowUrl, nextStep } from "../components/ProgrammeStepper";
import { fmtDate } from "../utils/date";

/** Rows per page — the same ten the other lists show. Paged client-side: the
 *  programmes arrive in one payload with the hierarchy, so no request is saved
 *  by asking the server for a page. */
const PAGE_SIZE = 10;

export default function Programs() {
  const nav = useNavigate();
  const [rows, setRows] = useState<HierarchyProgramme[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    getHierarchy().then(h => setRows(h.programmes))
      .catch(() => setErr("Could not load your programmes."));
  }, []);

  const total = rows?.length ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // Clamped, so a list that shrinks never strands you on an empty page.
  const pageNow = Math.min(page, pageCount);
  const pageRows = (rows ?? []).slice((pageNow - 1) * PAGE_SIZE, pageNow * PAGE_SIZE);

  return (
    <>
      <PageHeader
        title="Programmes"
        subtitle="One type of business you write — commercial motor, say — with your brokers and contracts underneath it."
        action={
          <Button onClick={() => nav("/programs/new")}>
            <Plus size={15} /> Configure Program
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
                  <Plus size={15} /> Configure Program
                </Button>
              </div>
            </div>
          </Card>
        )}

        {rows && rows.length > 0 && (
          <Card>
            <div className="overflow-x-auto">
              <table>
                <thead>
                  <tr>
                    <th>Programme</th>
                    <th>How far it's got</th>
                    {/* Named for what the button is: the one thing that moves
                        this programme along the steps beside it. */}
                    <th className="text-right">Next step</th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map(p => {
                    // Clicking the row opens the Configure Program flow at the
                    // step this programme is up to; a finished one at its last.
                    const next = nextStep(p);
                    // Segment and product line are long free text; they live in
                    // the name's tooltip rather than a column that wraps the row.
                    const about = [p.business_segment, p.product_line].filter(Boolean).join(" · ");
                    return (
                    <tr
                      key={p.id}
                      className="group cursor-pointer"
                      title={next ? next.hint : "Everything is in place — files can be checked."}
                      onClick={() => nav(next ? next.to : flowUrl(p.id, "setup"))}
                    >
                      <td className="align-middle">
                        {/* A link, not just a clickable row — it can be opened in a
                            new tab and reached by keyboard. */}
                        <Link
                          to={`/programs/${p.id}/brokers`}
                          onClick={e => e.stopPropagation()}
                          title={about || undefined}
                          className="block font-semibold text-ink hover:text-navy hover:underline"
                        >
                          {p.name}
                        </Link>
                        <div className="mt-0.5 text-xs text-ink-muted">
                          {p.created_at ? `Created ${fmtDate(p.created_at)} · ` : ""}
                          {frequencyLabel(p.bdx_frequency).toLowerCase()}
                        </div>
                      </td>
                      <td className="align-middle">
                        {/* Each pill opens its own step for this programme. */}
                        <ProgrammeStepper programme={p} />
                      </td>
                      <td className="whitespace-nowrap text-right align-middle">
                        {next ? (
                          <Button className="!px-3 !py-1.5 !text-[12.5px] !font-semibold" title={next.hint}
                            onClick={e => { e.stopPropagation(); nav(next.to); }}>
                            {next.label}
                          </Button>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[12.5px] font-semibold text-success">
                            Ready
                            <ChevronRight size={16}
                              className="text-ink-soft transition group-hover:translate-x-0.5 group-hover:text-navy" />
                          </span>
                        )}
                      </td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* The shared pager is styled by the .proto tokens; proto-embed
                brings them in without .proto's page background. */}
            {pageCount > 1 && (
              <div className="proto proto-embed -mx-5">
                <Pagination page={pageNow} pageCount={pageCount} pageSize={PAGE_SIZE}
                  totalItems={total} onPageChange={setPage} noun="programmes" />
              </div>
            )}

            <p className="mt-3 border-t border-border pt-3 text-xs text-ink-muted">
              Each programme goes Programme → Brokers → Contract → Setup. Green is
              done, red is what's stopping files. Click a row to go straight to
              where that programme is up to, or click any step to open it.
            </p>
          </Card>
        )}

      </PageBody>
    </>
  );
}
