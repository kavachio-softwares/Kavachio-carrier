/**
 * One programme, and the brokers on it.
 *
 * This is where the mesh is actually managed. A programme is fed by several
 * brokers, and each broker holds its own contracts on it — so contracts are
 * listed UNDER the broker that produced them, never in one flat pile. That
 * (programme × broker) pair is what makes "who produced this policy?"
 * answerable without guesswork.
 *
 * Taking a broker off a programme that already has contracts DEACTIVATES the
 * link rather than deleting it, so the contracts underneath keep their meaning.
 * The API says which of the two happened, and this screen repeats it back.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, FileText, Plus, Layers, Upload, Users2 } from "lucide-react";
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
import { currentMga } from "../auth";

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

export default function ProgramBrokers() {
  const { programId } = useParams();
  const pid = Number(programId);
  const nav = useNavigate();

  const [prog, setProg] = useState<HierarchyProgramme | null>(null);
  // Every bordereau setup on this programme, fetched ONCE for the whole screen
  // — one request per broker card would be N requests for one list.
  const [setups, setSetups] = useState<Setup[] | null>(null);
  const [all, setAll] = useState<BrokerSummary[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    getHierarchy()
      .then(h => {
        const found = h.programmes.find(p => p.id === pid);
        if (!found) { setErr("That programme is not one of yours."); return; }
        setProg(found); setErr(null);
      })
      .catch(() => setErr("Could not load this programme."));
    getBrokers().then(setAll).catch(() => setAll([]));
    // The bordereau setups on this programme, so each broker card can say
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

        {/* Adding a broker is a one-line action, not the subject of the page —
            the brokers already on the programme are. It used to be a full card
            at the top, which gave the least-used control the most weight and
            pushed the actual content below the fold. */}
        <Card className="!p-3.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 pr-1 text-[12.5px] font-medium text-ink-muted">
              <Plus size={14} /> Put a broker on this programme
            </span>
            <select
              className="input !w-auto min-w-[240px] flex-1 !py-1.5"
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
            <Button className="!py-1.5" onClick={add} disabled={!adding || busy}>
              Add
            </Button>
          </div>
          <p className="mt-2 text-xs text-ink-muted">
            {all.length === 0
              ? <>You hold no brokers yet. A broker is created by inviting its
                  first admin, from <b className="font-medium">Brokers</b> —
                  then it can be put on this programme.</>
              : <>Only brokers you already hold are listed. To bring a new one on
                  board, invite it from{" "}
                  <b className="font-medium">Brokers</b>.</>}
          </p>
        </Card>

        {prog.brokers.length === 0 ? (
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
          <div className="space-y-4">
            {prog.brokers.map(b => {
              const meta = all.find(x => x.id === b.id);
              const off = b.link_status !== "active";
              const sx = setupFor(setups, b.id);
              return (
                <Card key={b.id} className={off ? "opacity-75" : undefined}>
                  {/* The broker IS the heading of its own card, so it is given
                      the weight of one — mark, name, state — with the
                      destructive action kept small and to the side rather than
                      sitting at the same size as the name it would remove. */}
                  <div className="mb-3 flex flex-wrap items-center gap-3 border-b border-border pb-3">
                    <OrgAvatar name={b.legal_name} muted={off} />
                    <div className="min-w-0">
                      <Link to={`/brokers/${b.id}`}
                        className={`text-[15px] font-semibold hover:underline ${
                          off ? "text-ink-soft line-through" : "text-ink hover:text-navy"}`}>
                        {b.legal_name}
                      </Link>
                      <div className="mt-0.5 text-xs text-ink-muted">
                        {b.contracts.length === 0
                          ? "No contract on this programme"
                          : `${b.contracts.length} contract${b.contracts.length === 1 ? "" : "s"} on this programme`}
                      </div>
                    </div>
                    <OnboardingBadge status={meta?.onboarding_status} />
                    {off && (
                      <span className="pill pill-grey"
                        title="Taken off this programme — their contracts stay readable">
                        Taken off
                      </span>
                    )}
                    <span className="flex-1" />
                    {!off && (
                      <button
                        className="rounded-md px-2 py-1 text-[12.5px] text-ink-soft transition
                          hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                        onClick={() => remove(b.id, b.legal_name)} disabled={busy}
                        title="Take this broker off the programme"
                      >
                        Remove
                      </button>
                    )}
                  </div>

                  {/* Where this broker has got to ON THIS PROGRAMME. Hidden once
                      they are ready — a card with nothing outstanding should
                      not carry a status strip. */}
                  <BrokerOnboarding
                    onboardingStatus={meta?.onboarding_status}
                    onProgramme={!off}
                    contractCount={b.contracts.length}
                  />

                  {/* THE BORDEREAU SETUP for this (programme × broker) pair.
                      Shown on the broker card because that is the pair it
                      belongs to — the programme-level answer ("this programme
                      has a setup") cannot tell you whether THIS broker can run,
                      which is the thing anyone actually needs to know.

                      Only once there is a contract: a setup is built from one,
                      so offering it earlier would be a dead end. */}
                  {b.contracts.length > 0 && (
                    <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded border border-border bg-surface-2/50 px-2.5 py-2 text-xs">
                      <Layers size={12} className="shrink-0 text-ink-muted" />
                      {sx ? (
                        <>
                          <Link
                            to={`/direct/setups/${sx.setup.id}`}
                            className="font-medium text-navy hover:underline"
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
                              className="text-navy hover:underline"
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
                            No bordereau setup — they have a contract but cannot
                            send you files yet.
                          </span>
                          <span className="flex-1" />
                          {!off && (
                            <Link
                              to={`/direct/setup?program_id=${prog.id}&broker_party_id=${b.id}`}
                              className="font-medium text-navy hover:underline"
                            >
                              Set up bordereau →
                            </Link>
                          )}
                        </>
                      )}
                    </div>
                  )}

                  {/* Contracts belong to the PAIR, so they are listed under the
                      broker that produced them rather than on the programme. */}
                  {b.contracts.length === 0 ? (
                    // No contract yet: this IS the next thing to do for this
                    // broker, so it is the one prominent action on the card.
                    <div className="flex flex-wrap items-center justify-between gap-3 rounded-md
                      border border-dashed border-border bg-surface-2/40 px-3 py-3">
                      <p className="text-sm text-ink-muted">
                        No contracts with this broker on this programme yet.
                      </p>
                      {/* TWO ways a contract starts, and they are different
                          jobs. Uploading reads the terms out of a wording that
                          already exists; raising states terms being agreed now
                          and writes the wording from them. Only the first was
                          offered here, so a carrier with nothing to upload had
                          no way forward from the page that told them a contract
                          was the next thing needed. Raise is the primary one:
                          it is the flow that does not depend on somebody else
                          having sent a document first. */}
                      {!off && (
                        <div className="flex shrink-0 items-center gap-2">
                          {/* To the contract upload screen, NOT Bordereau
                              Setup. This card's empty state says the broker
                              has no contract; the thing that fixes it is
                              putting the contract in. Bordereau Setup happens
                              afterwards and has its own links further down —
                              sending someone into a setup wizard to add a
                              contract makes them finish a different job to
                              start this one. */}
                          <Link
                            to={`/contracts/upload?program_id=${prog.id}&broker_party_id=${b.id}`}
                            className="inline-flex items-center gap-1.5 rounded-md border border-border
                              px-3 py-1.5 text-[12.5px] font-medium text-ink transition hover:bg-surface-2"
                          >
                            <Upload size={13} /> Upload contract
                          </Link>
                          <Link
                            to={`/contracts/new?program_id=${prog.id}&broker_party_id=${b.id}`}
                            className="inline-flex items-center gap-1.5 rounded-md bg-navy px-3 py-1.5
                              text-[12.5px] font-medium text-white transition hover:bg-navy-dark"
                          >
                            <Plus size={13} /> Raise a contract
                          </Link>
                        </div>
                      )}
                    </div>
                  ) : (
                    <ul className="divide-y divide-border overflow-hidden rounded-md border border-border">
                      {b.contracts.map(c => (
                        <li key={c.id}>
                          <button
                            className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left text-sm transition hover:bg-surface-2"
                            onClick={() => nav(`/programs/${prog.id}/contracts/${c.id}`)}
                          >
                            <FileText size={14} className="shrink-0 text-ink-soft" />
                            <span className="min-w-0 flex-1 truncate">
                              {c.filename ?? `Contract #${c.id}`}
                            </span>
                          </button>
                        </li>
                      ))}
                      {!off && (
                        <li className="bg-surface-2/40">
                          {/* To the broker's own page, not straight into the
                              wizard: a second contract is usually a decision
                              about what they already hold, so it starts from
                              seeing that — across every programme, not just
                              this one. The upload lives there. */}
                          <Link
                            to={`/brokers/${b.id}`}
                            className="flex items-center gap-1.5 px-3 py-2 text-[12.5px] font-medium
                              text-ink-muted transition hover:text-navy"
                          >
                            <Plus size={13} /> Add another contract
                          </Link>
                        </li>
                      )}
                    </ul>
                  )}
                </Card>
              );
            })}
          </div>
        )}

        <p className="flex items-start gap-1.5 text-xs text-ink-muted">
          <Layers size={13} className="mt-0.5 shrink-0" />
          <span>
            A broker can be on several of your programmes. This screen shows only
            what they do on <b className="font-medium">{prog.name}</b>.
          </span>
        </p>
      </PageBody>
    </>
  );
}
