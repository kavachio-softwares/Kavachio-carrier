/**
 * One broker, seen from THIS carrier's side only.
 *
 * The same broker may produce far more business for someone else; none of that
 * is this carrier's to see. Everything on this page is scoped to the
 * relationship the signed-in carrier actually has.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft, Loader2, FileText, Layers, Search, UserCog, Plus, Upload,
} from "lucide-react";
import {
  addProgrammeBroker, getBroker, getHierarchy, listBrokerContracts,
  type BrokerContractRow, type BrokerDetail as Detail,
  type HierarchyProgramme,
} from "../api/hierarchy";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useServerList } from "../hooks/useServerList";
import { fmtDate, fmtStamp } from "../utils/date";
import Card from "../components/ui/Card";
import { Sk } from "../components/ui/Skeleton";
import { Button } from "../components/ui/Button";
import { Select, TextInput } from "../components/ui/Field";
import { PageBody, PageHeader } from "../components/Layout";
import { OnboardingBadge } from "../components/OnboardingBadge";
import AddContractModal from "../components/AddContractModal";

const PAGE_SIZE = 10;

export default function BrokerDetail() {
  const { brokerId } = useParams();
  const nav = useNavigate();

  // Putting them on a programme lives HERE, because onboarding no longer asks
  // for one: a broker is invited to work with the carrier, and which
  // programmes they produce on is a decision made afterwards and repeatedly.
  // Without this the answer to "they accepted, now what?" was to go and find
  // the programme and add them from its side.
  const [allProgrammes, setAllProgrammes] = useState<HierarchyProgramme[]>([]);
  const [assignTo, setAssignTo] = useState("");
  const [assigning, setAssigning] = useState(false);
  const [assignMsg, setAssignMsg] = useState("");

  useEffect(() => {
    getHierarchy().then(h => setAllProgrammes(h.programmes))
      .catch(() => setAllProgrammes([]));
  }, []);
  const bid = Number(brokerId);
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

  async function assign() {
    if (!assignTo || !brokerId) return;
    setAssigning(true); setAssignMsg("");
    try {
      const r = await addProgrammeBroker(Number(assignTo), Number(brokerId));
      const name = allProgrammes.find(p => String(p.id) === assignTo)?.name
                   ?? "that programme";
      // Re-assigning somebody taken off before reactivates the existing link
      // rather than adding a second — say which happened.
      setAssignMsg(r.reactivated
        ? `Back on ${name}. Their earlier contracts there are live again.`
        : `Added to ${name}. They can produce on it now.`);
      setAssignTo("");
      load();
    } catch (e: any) {
      const d = e?.response?.data?.detail;
      setAssignMsg((typeof d === "string" ? d : d?.message)
        ?? "Could not put them on that programme.");
    } finally { setAssigning(false); }
  }

  // Contract rows carry a programme id, not its name. Built once here rather
  // than scanned out of b.programmes per row — and it is the same list the
  // programme filter offers, so the two can never fall out of step.
  const programmeName = useMemo(
    () => new Map((b?.programmes ?? []).map(p => [p.id, p.name])),
    [b],
  );

  // The contract table is filtered and paged by the SERVER. A broker of any
  // size has hundreds and the table shows ten, so the alternative was shipping
  // the lot on every page load to hide most of it in the browser.
  const [q, setQ] = useState("");
  const [programme, setProgramme] = useState("");
  // Debounced because the search runs in SQL — an undebounced box is one
  // request per keystroke.
  const dq = useDebouncedValue(q, 300);
  const filterKey = [bid, programme, dq.trim()].join("|");
  const {
    page, setPage, items: contracts, total, pageCount, loading: loadingContracts,
    reload: reloadContracts,
  } = useServerList<BrokerContractRow>(
    (pg, size) => listBrokerContracts(bid, {
      q: dq.trim() || undefined,
      program_id: programme ? Number(programme) : undefined,
      limit: size, offset: (pg - 1) * size,
    }).then(r => ({ items: r.contracts, total: r.total })),
    filterKey, PAGE_SIZE,
  );
  const filtersActive = !!(q || programme);

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
                Not on a programme yet, so they cannot produce anything. Put
                them on one below.
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

            {/* Only programmes they are not already ON. Offering one they are
                already on would be offering a button whose only outcome is
                "already on this programme". */}
            {(() => {
              const on = new Set(b.programmes.filter(p => p.status === "active")
                                             .map(p => p.id));
              const available = allProgrammes.filter(p => !on.has(p.id));
              if (!available.length) {
                return (
                  <p className="mt-3 text-xs text-ink-muted">
                    {allProgrammes.length
                      ? "They are on every programme you have."
                      : "You have no programmes yet."}
                  </p>
                );
              }
              return (
                <div className="mt-4 border-t border-border pt-3">
                  <label className="mb-1.5 block text-xs font-medium text-ink-muted">
                    Put them on a programme
                  </label>
                  <div className="flex items-center gap-2">
                    <select
                      value={assignTo}
                      onChange={e => setAssignTo(e.target.value)}
                      className="min-w-0 flex-1 rounded-md border border-border bg-white
                        px-2.5 py-1.5 text-sm text-ink"
                    >
                      <option value="">Choose…</option>
                      {available.map(p => (
                        <option key={p.id} value={String(p.id)}>{p.name}</option>
                      ))}
                    </select>
                    <Button onClick={assign} disabled={!assignTo || assigning}>
                      {assigning ? "…" : "Add"}
                    </Button>
                  </div>
                  {assignMsg && (
                    <p className="mt-2 text-xs text-ink-muted">{assignMsg}</p>
                  )}
                </div>
              );
            })()}
          </Card>

          <Card title="Their team" className="md:col-span-2">
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
            /* TWO ways a contract starts, and they are different jobs.
               Uploading reads the terms out of a wording that already exists;
               raising states terms being agreed now and writes the wording
               from them. Only the upload was here, so a carrier with nothing
               to upload had no way to start one with this broker at all. */
            <div className="flex items-center gap-2">
              <Button variant="secondary" onClick={() => setAdding(true)}>
                <Upload size={15} /> Upload contract
              </Button>
              <Button onClick={() => nav(
                // The programme comes too when there is only one it could be.
                // With several, the raise flow asks — and re-applies this
                // broker once the programme narrows the list to people who are
                // actually on it.
                `/contracts/new?broker_party_id=${b.id}`
                + (live.length === 1 ? `&program_id=${live[0].id}` : ""))}>
                <Plus size={15} /> Raise a contract
              </Button>
            </div>
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
          {/* Shown even when the page is empty: with the filters hidden, a
              search that matched nothing looked identical to a broker with no
              contracts, and there was nothing on screen to clear. */}
          <div className="mb-4 flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-2/40 px-3 py-2.5">
            <div className="relative">
              <Search size={14}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-muted" />
              <TextInput className="!pl-8 !w-64" value={q} aria-label="Search contracts"
                placeholder="Name, UMR or filename"
                onChange={e => setQ(e.target.value)} />
            </div>
            <Select className="!w-52" value={programme} aria-label="Filter by programme"
              onChange={e => setProgramme(e.target.value)}>
              <option value="">All programmes</option>
              {b.programmes.map(p => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
            {filtersActive && (
              <button type="button" className="linkish text-xs"
                onClick={() => { setQ(""); setProgramme(""); }}>
                Clear filters
              </button>
            )}
            {/* The count sits with the filters rather than only by the pager:
                on a single page there is no pager, and "how many did that
                leave?" is the first thing a filter raises. */}
            <span className="ml-auto flex items-center gap-2 text-xs text-ink-muted">
              {loadingContracts && <Loader2 size={13} className="animate-spin" />}
              {total} {total === 1 ? "contract" : "contracts"}
              {filtersActive && total > 0 ? " match" : ""}
            </span>
          </div>
          {total === 0 && loadingContracts ? (
            // Nothing yet AND still asking. Without this the "no contracts"
            // line shows for as long as the first request takes, on a broker
            // that has plenty. Rows rather than a spinner, so the card keeps
            // the height it is about to have and nothing below it jumps.
            <div className="space-y-2 py-1">
              {[0, 1, 2, 3].map(i => <Sk key={i} className="h-10" />)}
            </div>
          ) : total === 0 ? (
            <div className="rounded-md border border-dashed border-border px-4 py-10 text-center">
              <FileText size={20} className="mx-auto mb-2 text-ink-muted opacity-60" />
              <p className="mx-auto max-w-md text-sm text-ink-muted">
                {filtersActive
                  ? "No contract matches that. Clear the filters to see them all."
                  : `No contracts with this broker yet. Add one and it is read
                     straight away — the same reading Bordereau Setup does, so a
                     setup can use it without going over the document a second
                     time.`}
              </p>
            </div>
          ) : (
            // Dimmed, not replaced, while the next page is on its way: swapping
            // the table for a spinner collapses the card and throws the pager
            // under the cursor that just clicked it.
            <div className={`overflow-x-auto transition-opacity ${loadingContracts ? "opacity-50" : ""}`}
              aria-busy={loadingContracts}>
              {/* No table classes of its own: the app-wide table rules in
                  index.css give every list the same header band, padding, hover
                  and centred columns. The local ones this table used to carry
                  were fighting them. */}
              <table>
                <thead>
                  <tr>
                    <th>Contract</th>
                    <th>Programme</th>
                    <th>Term</th>
                    <th>Added</th>
                  </tr>
                </thead>
                <tbody>
                  {contracts.map(c => {
                    // "Where it stands" was the carrier's approval decision on
                    // this contract. There is no decision to show now the gate is
                    // gone, and the ops status answers a different question, so
                    // the column went with it.
                    return (
                      <tr key={c.id}>
                        <td>
                          {/* Opens what the contract PRODUCED — its clauses and the
                              rules written from them. That page is the whole point
                              of adding one here, so the name is the way in. */}
                          {/* A contract WRITTEN here has no file and no clauses
                              read out of one — its home is its own record, where
                              its terms, its wording and its checks are. An
                              UPLOADED one opens what reading it produced, which
                              is the whole point of having added it here. */}
                          {c.is_app_managed ? (
                            <Link to={`/contracts/${c.id}`}
                              className="inline-flex items-center gap-2 text-navy hover:underline">
                              <FileText size={14} className="text-ink-muted" />
                              {c.name ?? `Contract ${c.id}`}
                            </Link>
                          ) : c.program_id != null ? (
                            <Link to={`/programs/${c.program_id}/contracts/${c.id}`}
                              className="inline-flex items-center gap-2 text-navy hover:underline">
                              <FileText size={14} className="text-ink-muted" />
                              {c.filename ?? c.name ?? `Contract ${c.id}`}
                            </Link>
                          ) : (
                            <span className="inline-flex items-center gap-2">
                              <FileText size={14} className="text-ink-muted" />
                              {c.filename ?? c.name ?? `Contract ${c.id}`}
                            </span>
                          )}
                        </td>
                        <td className="text-ink-muted">
                          {(c.program_id != null && programmeName.get(c.program_id)) || "—"}
                        </td>
                        <td className="text-ink-muted whitespace-nowrap">
                          {c.inception_dt && c.expiry_dt
                            ? `${fmtDate(c.inception_dt)} → ${fmtDate(c.expiry_dt)}`
                            : "—"}
                        </td>
                        <td className="text-ink-muted whitespace-nowrap">{fmtStamp(c.created_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {/* Only worth drawing when there is a second page to reach. */}
          {pageCount > 1 && (
            <div className="mt-4 flex items-center justify-between border-t border-border pt-3 text-xs text-ink-muted">
              <span>
                {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total} contracts
              </span>
              <div className="flex items-center gap-2">
                <Button variant="secondary" className="!px-2.5 !py-1 !text-xs"
                  disabled={page <= 1} onClick={() => setPage(page - 1)}>← Prev</Button>
                <span>Page {page} of {pageCount}</span>
                <Button variant="secondary" className="!px-2.5 !py-1 !text-xs"
                  disabled={page >= pageCount} onClick={() => setPage(page + 1)}>Next →</Button>
              </div>
            </div>
          )}
        </Card>

        <AddContractModal
          open={adding}
          onClose={() => setAdding(false)}
          broker={{ id: b.id, legal_name: b.legal_name }}
          programmes={b.programmes}
          onAdded={(contractId, programId) => {
            setAdded({ id: contractId, programId });
            // Only the table changed — the programmes and people above it did
            // not — so re-read the page of contracts, from the first page,
            // where a newly added one sorts. If a filter is on it may not be
            // on that page at all, which is what the banner's link is for.
            setPage(1);
            reloadContracts();
          }} />
      </PageBody>
    </>
  );
}
