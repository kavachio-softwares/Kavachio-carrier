/**
 * One programme, and the brokers on it.
 *
 * This is where the mesh is actually managed. A programme is fed by several
 * brokers, and each broker holds its own contracts on it — so contracts are
 * listed UNDER the broker that produced them, never in one flat pile. That
 * (programme × broker) pair is what makes "who produced this policy?"
 * answerable without guesswork.
 *
 * A TABLE, one row per broker, the same shape as the Brokers list. It was a
 * stack of tall cards, which reads well for one broker and falls apart at
 * fifty: every card a different height, the facts never lining up, and no way
 * to run an eye down a column to compare two brokers' contracts. A carrier with
 * thousands of brokers would have scrolled past all of them.
 *
 * What each broker HOLDS — their onboarding, their bordereau setup, their
 * contracts — opens out under their row, because it is detail about one broker
 * rather than a column that could line up with anyone else's. Several rows can
 * be open at once: comparing two brokers is the reason anyone opens two.
 *
 * Searching, filtering and paging are all CLIENT-SIDE. The programme's brokers
 * arrive in one payload with the programme itself, so none of it costs a
 * request — and the page shows ten rows at a time rather than everything.
 *
 * Taking a broker off a programme that already has contracts DEACTIVATES the
 * link rather than deleting it, so the contracts underneath keep their meaning.
 * The API says which of the two happened, and this screen repeats it back.
 */
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft, ChevronDown, ChevronRight, FileText, Plus, Layers, Search,
  Upload, Users2,
} from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { Button } from "../components/ui/Button";
import { OrgAvatar } from "../components/ui/OrgAvatar";
import {
  getHierarchy, getBrokers, addProgrammeBroker, removeProgrammeBroker,
  type HierarchyProgramme, type BrokerSummary,
} from "../api/hierarchy";
import { OnboardingBadge } from "../components/OnboardingBadge";
import { BrokerOnboarding } from "../components/BrokerOnboarding";
import { api } from "../api/client";
import AddContractModal from "../components/AddContractModal";
import { currentMga } from "../auth";

/** Rows per page. Ten keeps the table a screenful, so the pager is reached by
 *  looking down rather than by scrolling. */
const PAGE_SIZE = 10;

/**
 * One bordereau setup, as this screen needs it. `broker_party_id` NULL means a
 * PROGRAMME-WIDE setup: it was built before the broker level existed, or built
 * for the programme deliberately, and it covers every broker on it. That is why
 * a broker with no setup of its own is not necessarily blocked — the resolution
 * order here is the same one /direct/run uses.
 */
type Setup = {
  id: number;
  name: string | null;
  status: "draft" | "active" | "superseded";
  broker_party_id: number | null;
  broker_name: string | null;
  output_template_name: string | null;
  contracts: { contract_id: number; filename: string | null }[];
};

/** The states worth narrowing a long list down to — each one is a job someone
 *  opens this page to do, not an arbitrary property to filter on. */
type Filter = "all" | "no_setup" | "no_contract" | "off";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All brokers" },
  { value: "no_setup", label: "Needs a bordereau setup" },
  { value: "no_contract", label: "No contract yet" },
  { value: "off", label: "Taken off" },
];

/**
 * The setup a given broker on this programme would actually run against.
 *
 * Same resolution order as /direct/run: the broker's OWN active setup first,
 * then the programme-wide one. Getting this order wrong in the UI would be
 * worse than showing nothing — a card reading "no setup" next to a broker who
 * can run perfectly well would send the carrier off to build a duplicate.
 */
function setupFor(setups: Setup[] | null, brokerId: number):
  { setup: Setup; heldBy: "broker" | "programme" } | null {
  if (!setups) return null;
  const active = setups.filter(s => s.status === "active");
  const own = active.find(s => s.broker_party_id === brokerId);
  if (own) return { setup: own, heldBy: "broker" };
  const shared = active.find(s => s.broker_party_id == null);
  if (shared) return { setup: shared, heldBy: "programme" };
  return null;
}

/** One headline count for the strip at the top. Presentation only — every
 *  number handed to it is read off what the page already loaded. */
function StatTile({ icon, n, label }: {
  icon: React.ReactNode; n: number; label: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-white px-4 py-3 shadow-card">
      <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg
        bg-surface-2 text-ink-muted">
        {icon}
      </span>
      <div className="min-w-0">
        <div className="text-[19px] font-semibold leading-none">{n}</div>
        <div className="mt-1 text-xs text-ink-muted">{label}</div>
      </div>
    </div>
  );
}

export default function ProgramBrokers() {
  const { programId } = useParams();
  const pid = Number(programId);
  const nav = useNavigate();

  const [prog, setProg] = useState<HierarchyProgramme | null>(null);
  // Every bordereau setup on this programme, fetched ONCE for the whole screen
  // — one request per broker row would be N requests for one list.
  const [setups, setSetups] = useState<Setup[] | null>(null);
  const [all, setAll] = useState<BrokerSummary[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  // The upload dialog, opened over this page for one broker. The broker is kept
  // after closing so the dialog's title does not change while it fades.
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFor, setUploadFor] = useState<{ id: number; legal_name: string } | null>(null);
  // Narrowing and paging the list, all over data already in hand.
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [page, setPage] = useState(1);
  // Which rows are opened out, by broker id.
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const load = useCallback(() => {
    getHierarchy()
      .then(h => {
        const found = h.programmes.find(p => p.id === pid);
        if (!found) { setErr("That programme is not one of yours."); return; }
        setProg(found); setErr(null);
      })
      .catch(() => setErr("Could not load this programme."));
    getBrokers().then(setAll).catch(() => setAll([]));
    // The bordereau setups on this programme, so each broker row can say
    // whether that (programme × broker) pair can actually be run.
    //
    // /pipelines, not /direct/setup: the latter lists raw direct_format rows
    // with no status filter, so a setup that was built but never ACTIVATED
    // counts as done. Only an ACTIVE pipeline means runnable, which is the same
    // thing /direct/run resolves.
    api.get<Setup[]>("/pipelines", { params: { mga: currentMga(), program_id: pid } })
      .then(r => setSetups(Array.isArray(r.data) ? r.data : []))
      .catch(() => setSetups([]));
  }, [pid]);

  useEffect(load, [load]);

  // Only brokers not already on this programme can be added. A broker whose
  // link was deactivated IS offered again — adding it back reactivates it.
  const onIt = new Set((prog?.brokers ?? []).filter(b => b.link_status === "active").map(b => b.id));
  const addable = all.filter(b => !onIt.has(b.id));

  const brokers = prog?.brokers ?? [];
  const needle = q.trim().toLowerCase();
  const shown = useMemo(() => brokers.filter(b => {
    if (needle && !b.legal_name.toLowerCase().includes(needle)) return false;
    const off = b.link_status !== "active";
    if (filter === "off") return off;
    if (filter === "no_contract") return !off && b.contracts.length === 0;
    if (filter === "no_setup") {
      return !off && b.contracts.length > 0 && !setupFor(setups, b.id);
    }
    return true;
  }), [brokers, needle, filter, setups]);

  // A narrowed list is a different list: staying on page 4 of it would show an
  // empty table and no way to tell why.
  useEffect(() => { setPage(1); }, [needle, filter]);

  async function add() {
    if (!adding) return;
    setBusy(true); setMsg(null);
    try {
      const r = await addProgrammeBroker(pid, Number(adding));
      setMsg(r.reactivated
        ? "That broker was put back on this programme — their earlier contracts are live again."
        : "Broker added to this programme.");
      setAdding("");
      load();
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not add that broker.");
    } finally { setBusy(false); }
  }

  async function remove(brokerId: number, name: string) {
    setBusy(true); setMsg(null);
    try {
      const r = await removeProgrammeBroker(pid, brokerId);
      setMsg(r.deactivated
        ? `${name} was taken off, but their ${r.contract_count} contract${r.contract_count === 1 ? "" : "s"} stay readable.`
        : `${name} was removed from this programme.`);
      load();
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not remove that broker.");
    } finally { setBusy(false); }
  }

  function toggleRow(id: number) {
    setExpanded(s => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function openUpload(b: { id: number; legal_name: string }) {
    setUploadFor({ id: b.id, legal_name: b.legal_name });
    setUploadOpen(true);
  }

  if (err && !prog) return (
    <>
      <PageHeader title="Programme" />
      <PageBody><Card><p className="text-sm text-danger">{err}</p></Card></PageBody>
    </>
  );
  if (!prog) return (
    <>
      <PageHeader title="Programme" />
      <PageBody><p className="text-sm text-ink-muted">Loading…</p></PageBody>
    </>
  );

  // Paging maths, after the guards: `page` can outrun a list that has just been
  // narrowed, so what the table draws is the clamped page, not the stored one.
  const pageCount = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const current = Math.min(page, pageCount);
  const rows = shown.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE);
  const narrowed = needle !== "" || filter !== "all";

  return (
    <>
      <PageHeader
        title={prog.name}
        subtitle={[prog.business_segment, prog.product_line, prog.bdx_frequency]
          .filter(Boolean).join(" · ") || "The brokers on this programme, and what each of them holds."}
        action={
          <Link to="/programs" className="inline-flex items-center gap-1 text-sm text-navy hover:underline">
            <ArrowLeft size={14} /> All programmes
          </Link>
        }
      />
      <PageBody>
        {msg && (
          <div className="rounded-md border border-success/30 bg-success/10 px-3.5 py-2.5 text-sm text-success">
            {msg}
          </div>
        )}
        {err && (
          <div className="rounded-md border border-warn/30 bg-warn/10 px-3.5 py-2.5 text-sm text-warn">
            {err}
          </div>
        )}

        {/* What this programme adds up to, before the table that details it —
            three counts anyone opening this page is here to check. Read off
            data the page already holds; nothing extra is fetched. */}
        {brokers.length > 0 && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <StatTile
              icon={<Users2 size={15} />}
              n={brokers.filter(b => b.link_status === "active").length}
              label="brokers on this programme" />
            <StatTile
              icon={<FileText size={15} />}
              n={brokers.reduce((n, b) => n + b.contracts.length, 0)}
              label="contracts under those brokers" />
            <StatTile
              icon={<Layers size={15} />}
              n={brokers.filter(
                b => b.link_status === "active" && setupFor(setups, b.id)).length}
              label="ready to receive bordereaux" />
          </div>
        )}

        {/* Adding a broker is a one-line action, not the subject of the page —
            the brokers already on the programme are. It used to be a full card
            at the top, which gave the least-used control the most weight and
            pushed the actual content below the fold. */}
        <Card className="!p-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-[240px] flex-1">
              <label className="label" htmlFor="put-broker">
                Put a broker on this programme
              </label>
              <select
                id="put-broker"
                className="input"
                value={adding} onChange={e => setAdding(e.target.value)}
                disabled={addable.length === 0}
              >
                <option value="">
                  {all.length === 0
                    ? "You have no brokers yet"
                    : addable.length === 0
                      ? "Every broker you hold is already on this programme"
                      : "Choose a broker…"}
                </option>
                {addable.map(b => (
                  <option key={b.id} value={b.id}>
                    {b.legal_name}{b.programmes.length ? ` — on ${b.programmes.length} other` : ""}
                  </option>
                ))}
              </select>
            </div>
            <Button className="!px-4" onClick={add} disabled={!adding || busy}>
              <Plus size={14} /> Add
            </Button>
          </div>
          <p className="mt-2.5 text-xs leading-relaxed text-ink-muted">
            {all.length === 0
              ? <>You hold no brokers yet. A broker is created by inviting its
                  first admin, from <b className="font-medium">Brokers</b> —
                  then it can be put on this programme.</>
              : <>Only brokers you already hold are listed. To bring a new one on
                  board, invite it from{" "}
                  <b className="font-medium">Brokers</b>.</>}
          </p>
        </Card>

        {brokers.length === 0 ? (
          <Card>
            <div className="py-12 text-center">
              <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2">
                <Users2 size={20} className="text-ink-soft" />
              </div>
              <p className="text-sm font-medium">No brokers on this programme yet</p>
              <p className="mx-auto mt-1 max-w-md text-sm text-ink-muted">
                So it cannot hold a contract. Put at least one on it using the
                bar above.
              </p>
            </div>
          </Card>
        ) : (
          <Card className="!p-0 overflow-hidden">
            {/* Search, the narrowing filter, and the count — on one line above
                the table, because the count is what tells you the search did
                something and belongs beside the box rather than being inferred
                from the table's length. */}
            <div className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
              <div className="input flex max-w-xs flex-1 items-center gap-2 !py-1.5">
                <Search size={14} className="shrink-0 text-ink-soft" />
                <input
                  className="flex-1 bg-transparent text-sm outline-none"
                  placeholder="Search brokers…"
                  value={q}
                  onChange={e => setQ(e.target.value)}
                />
              </div>
              <select
                className="input !w-auto !py-1.5"
                aria-label="Narrow the list"
                value={filter}
                onChange={e => setFilter(e.target.value as Filter)}
              >
                {FILTERS.map(f => (
                  <option key={f.value} value={f.value}>{f.label}</option>
                ))}
              </select>
              {narrowed && (
                <button type="button" className="linkish text-xs"
                  onClick={() => { setQ(""); setFilter("all"); }}>
                  Clear
                </button>
              )}
              <span className="flex-1" />
              <span className="text-xs text-ink-muted">
                {narrowed
                  ? `${shown.length} of ${brokers.length}`
                  : `${brokers.length} broker${brokers.length === 1 ? "" : "s"}`}
              </span>
            </div>

            {shown.length === 0 ? (
              <div className="px-4 py-12 text-center">
                <p className="text-sm font-medium">No broker matches that</p>
                <p className="mx-auto mt-1 max-w-md text-sm text-ink-muted">
                  Every broker on this programme is still here — only what is
                  listed has been narrowed.
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table>
                  <thead>
                    <tr>
                      <th>Broker</th>
                      <th>Status</th>
                      <th>Contracts</th>
                      <th>Bordereau setup</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(b => {
                      const meta = all.find(x => x.id === b.id);
                      const off = b.link_status !== "active";
                      const sx = setupFor(setups, b.id);
                      const open = expanded.has(b.id);
                      return (
                        <Fragment key={b.id}>
                          <tr
                            className={`cursor-pointer ${off ? "opacity-75" : ""}`}
                            onClick={() => toggleRow(b.id)}
                          >
                            <td>
                              <div className="flex items-center gap-2.5">
                                <button
                                  type="button"
                                  aria-expanded={open}
                                  aria-label={open
                                    ? `Hide what ${b.legal_name} holds`
                                    : `Show what ${b.legal_name} holds`}
                                  className="rounded p-0.5 text-ink-soft transition hover:bg-surface-2 hover:text-ink"
                                  onClick={e => { e.stopPropagation(); toggleRow(b.id); }}
                                >
                                  {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                                </button>
                                <OrgAvatar name={b.legal_name} size="sm" muted={off} />
                                <div className="min-w-0">
                                  {/* A link as well as a clickable row: the row
                                      alone cannot be opened in a new tab or
                                      reached by keyboard. */}
                                  <Link
                                    to={`/brokers/${b.id}`}
                                    onClick={e => e.stopPropagation()}
                                    className={`block truncate font-medium ${
                                      off ? "text-ink-soft line-through" : "text-navy hover:underline"}`}
                                  >
                                    {b.legal_name}
                                  </Link>
                                  {off && (
                                    <div className="text-xs text-ink-muted">
                                      Taken off — their contracts stay readable
                                    </div>
                                  )}
                                </div>
                              </div>
                            </td>

                            <td><OnboardingBadge status={meta?.onboarding_status} /></td>

                            <td>
                              {b.contracts.length === 0
                                ? <span className="pill pill-amber whitespace-nowrap">None yet</span>
                                : <span className="font-medium">{b.contracts.length}</span>}
                            </td>

                            {/* Whether THIS pair can actually run. The
                                programme-level answer ("this programme has a
                                setup") cannot tell you that, which is why the
                                column is per broker. */}
                            <td>
                              {b.contracts.length === 0 ? (
                                <span className="text-ink-soft">—</span>
                              ) : setups === null ? (
                                <span className="text-xs text-ink-muted">Checking…</span>
                              ) : sx ? (
                                <span
                                  className={`pill whitespace-nowrap ${
                                    sx.heldBy === "broker" ? "pill-blue" : "pill-grey"}`}
                                  title={sx.heldBy === "broker"
                                    ? "Built for this broker alone"
                                    : "The programme's shared setup — every broker on it runs this"}
                                >
                                  {sx.heldBy === "broker" ? "Built for this broker" : "Shared by programme"}
                                </span>
                              ) : (
                                <span className="pill pill-amber whitespace-nowrap"
                                  title="They have a contract but cannot send you files until a setup is built.">
                                  None yet
                                </span>
                              )}
                            </td>

                            <td className="text-right">
                              {!off && (
                                <button
                                  className="rounded-md border border-transparent px-2.5 py-1.5 text-[12.5px]
                                    font-medium text-ink-soft transition hover:border-danger/30
                                    hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                                  onClick={e => { e.stopPropagation(); remove(b.id, b.legal_name); }}
                                  disabled={busy}
                                  title="Take this broker off the programme"
                                >
                                  Remove
                                </button>
                              )}
                            </td>
                          </tr>

                          {open && (
                            <tr className="bg-surface-2/40">
                              <td colSpan={5} className="text-left">
                                <div className="space-y-3 py-1">
                                  {/* Where this broker has got to ON THIS
                                      PROGRAMME. Renders nothing once they are
                                      ready — a broker with nothing outstanding
                                      needs no commentary. */}
                                  <BrokerOnboarding
                                    onboardingStatus={meta?.onboarding_status}
                                    onProgramme={!off}
                                    contractCount={b.contracts.length}
                                  />

                                  {/* THE BORDEREAU SETUP for this pair, in full:
                                      the column above says only which kind it
                                      is. Only once there is a contract — a setup
                                      is built from one, so offering it earlier
                                      would be a dead end. */}
                                  {b.contracts.length > 0 && (
                                    <div className={`rounded-lg border bg-white px-3.5 py-3 ${
                                      !sx && setups !== null ? "border-warn/40" : "border-border"}`}>
                                      <div className="mb-1.5 flex items-center gap-1.5 text-[10.5px] font-semibold
                                        uppercase tracking-wide text-ink-soft">
                                        <Layers size={12} className="shrink-0" /> Bordereau setup
                                      </div>
                                      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-xs">
                                        {sx ? (
                                          <>
                                            <Link
                                              to={`/direct/setups/${sx.setup.id}`}
                                              className="text-[13px] font-medium text-navy hover:underline"
                                            >
                                              {sx.setup.name ?? `Setup ${sx.setup.id}`}
                                            </Link>
                                            <span className="text-ink-muted">
                                              {sx.heldBy === "broker"
                                                ? "built for this broker"
                                                : "the programme's shared setup — every broker on it runs this"}
                                            </span>
                                            {sx.setup.output_template_name && (
                                              <span className="text-ink-soft">
                                                → {sx.setup.output_template_name}
                                              </span>
                                            )}
                                            <span className="flex-1" />
                                            {sx.heldBy === "programme" && !off && (
                                              <Link
                                                to={`/direct/setup?program_id=${prog.id}&broker_party_id=${b.id}`}
                                                className="font-medium text-navy hover:underline"
                                              >
                                                Build one just for them
                                              </Link>
                                            )}
                                          </>
                                        ) : setups === null ? (
                                          <span className="text-ink-muted">Checking bordereau setup…</span>
                                        ) : (
                                          <>
                                            <span className="text-warn">
                                              None yet — they have a contract but cannot send you
                                              files until one is built.
                                            </span>
                                            <span className="flex-1" />
                                            {!off && (
                                              <Link
                                                to={`/direct/setup?program_id=${prog.id}&broker_party_id=${b.id}`}
                                                className="inline-flex items-center gap-1 rounded-md bg-navy px-2.5 py-1
                                                  text-[12px] font-medium text-white transition hover:bg-navy-dark hover:no-underline"
                                              >
                                                Set up bordereau <ChevronRight size={12} />
                                              </Link>
                                            )}
                                          </>
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {/* Contracts belong to the PAIR, so they are
                                      listed under the broker that produced them
                                      rather than on the programme. */}
                                  {b.contracts.length === 0 ? (
                                    // No contract yet: this IS the next thing to
                                    // do for this broker, so it is the one
                                    // prominent action here.
                                    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg
                                      border border-dashed border-border bg-white px-3.5 py-3.5">
                                      <p className="flex items-center gap-2 text-sm text-ink-muted">
                                        <FileText size={15} className="shrink-0 text-ink-soft" />
                                        No contracts with this broker on this programme yet.
                                      </p>
                                      {/* TWO ways a contract starts, and they are
                                          different jobs. Uploading reads the terms
                                          out of a wording that already exists;
                                          raising states terms being agreed now and
                                          writes the wording from them. Raise is the
                                          primary one: it does not depend on somebody
                                          else having sent a document first. */}
                                      {!off && (
                                        <div className="flex shrink-0 items-center gap-2">
                                          <button
                                            type="button"
                                            onClick={() => openUpload(b)}
                                            className="inline-flex items-center gap-1.5 rounded-md border border-border
                                              px-3 py-1.5 text-[12.5px] font-medium text-ink transition hover:bg-surface-2"
                                          >
                                            <Upload size={13} /> Upload contract
                                          </button>
                                          <Link
                                            to={`/contracts/new?program_id=${prog.id}&broker_party_id=${b.id}`}
                                            className="inline-flex items-center gap-1.5 rounded-md bg-navy px-3 py-1.5
                                              text-[12.5px] font-medium text-white transition hover:bg-navy-dark hover:no-underline"
                                          >
                                            <Plus size={13} /> Raise a contract
                                          </Link>
                                        </div>
                                      )}
                                    </div>
                                  ) : (
                                    <div className="overflow-hidden rounded-lg border border-border bg-white">
                                      <div className="flex items-center justify-between border-b border-border px-3.5 py-2">
                                        <span className="text-[10.5px] font-semibold uppercase tracking-wide text-ink-soft">
                                          Contracts
                                        </span>
                                        <span className="text-[11px] text-ink-muted">
                                          {b.contracts.length}
                                        </span>
                                      </div>
                                      <ul className="divide-y divide-border">
                                        {b.contracts.map(c => (
                                          <li key={c.id}>
                                            <button
                                              className="group flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left
                                                text-sm transition hover:bg-surface-2"
                                              onClick={() => nav(`/programs/${prog.id}/contracts/${c.id}`)}
                                            >
                                              <FileText size={14} className="shrink-0 text-ink-soft" />
                                              <span className="min-w-0 flex-1 truncate group-hover:text-navy">
                                                {c.filename ?? `Contract #${c.id}`}
                                              </span>
                                              <ChevronRight size={14}
                                                className="shrink-0 text-ink-soft opacity-0 transition group-hover:opacity-100" />
                                            </button>
                                          </li>
                                        ))}
                                      </ul>
                                      {!off && (
                                        // The upload dialog, over this page — the
                                        // contracts they already hold are listed
                                        // just above, so there is no need to leave.
                                        <button
                                          type="button"
                                          onClick={() => openUpload(b)}
                                          className="flex w-full items-center gap-1.5 border-t border-dashed border-border
                                            bg-surface-2/40 px-3.5 py-2.5 text-left text-[12.5px] font-medium
                                            text-ink-muted transition hover:bg-surface-2 hover:text-navy"
                                        >
                                          <Plus size={13} /> Add another contract
                                        </button>
                                      )}
                                    </div>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            {/* The pager, only once there is more than one page of them. */}
            {pageCount > 1 && (
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border
                px-4 py-3 text-xs text-ink-muted">
                <span>
                  {(current - 1) * PAGE_SIZE + 1}–{Math.min(current * PAGE_SIZE, shown.length)}
                  {" of "}{shown.length} broker{shown.length === 1 ? "" : "s"}
                </span>
                <div className="flex items-center gap-2">
                  <Button variant="secondary" className="!px-2.5 !py-1 !text-xs"
                    disabled={current <= 1} onClick={() => setPage(current - 1)}>
                    ← Prev
                  </Button>
                  <span>Page {current} of {pageCount}</span>
                  <Button variant="secondary" className="!px-2.5 !py-1 !text-xs"
                    disabled={current >= pageCount} onClick={() => setPage(current + 1)}>
                    Next →
                  </Button>
                </div>
              </div>
            )}
          </Card>
        )}

        <p className="flex items-start gap-2 rounded-lg border border-border bg-white px-3.5 py-2.5
          text-xs leading-relaxed text-ink-muted">
          <Layers size={13} className="mt-0.5 shrink-0 text-ink-soft" />
          <span>
            A broker can be on several of your programmes. This screen shows only
            what they do on <b className="font-medium">{prog.name}</b>.
          </span>
        </p>

        {uploadFor && (
          <AddContractModal
            open={uploadOpen}
            onClose={() => setUploadOpen(false)}
            broker={uploadFor}
            programmes={[{ id: prog.id, name: prog.name, status: prog.status }]}
            // The new contract belongs under this broker's row — re-read it.
            onAdded={() => load()} />
        )}
      </PageBody>
    </>
  );
}
