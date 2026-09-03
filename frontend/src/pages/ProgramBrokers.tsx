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
import { ArrowLeft, FileText, Plus, Layers, Upload } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
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
          <div className="mb-4 rounded border border-success/40 bg-success/10 px-3 py-2 text-sm text-success">
            {msg}
          </div>
        )}
        {err && (
          <div className="mb-4 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
            {err}
          </div>
        )}

        <Card title="Put a broker on this programme" className="mb-5">
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="min-w-[240px] flex-1 rounded border border-border px-2.5 py-1.5 text-sm"
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
            <button
              className="inline-flex items-center gap-1 rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark disabled:opacity-50"
              onClick={add} disabled={!adding || busy}
            >
              <Plus size={14} /> Add
            </button>
          </div>
          <p className="mt-2.5 text-xs text-ink-muted">
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
            <p className="text-sm text-ink-muted">
              No brokers on this programme yet, so it cannot hold a contract.
              Put at least one on it above.
            </p>
          </Card>
        ) : (
          <div className="space-y-4">
            {prog.brokers.map(b => {
              const meta = all.find(x => x.id === b.id);
              const off = b.link_status !== "active";
              const sx = setupFor(setups, b.id);
              return (
                <Card key={b.id}>
                  <div className="mb-3 flex flex-wrap items-center gap-2.5">
                    <Link to={`/brokers/${b.id}`}
                      className={`font-medium hover:underline ${off ? "text-ink-soft line-through" : "text-navy"}`}>
                      {b.legal_name}
                    </Link>
                    <OnboardingBadge status={meta?.onboarding_status} />
                    {off && (
                      <span className="rounded bg-surface-2 px-2 py-0.5 text-xs text-ink-muted"
                        title="Taken off this programme — their contracts stay readable">
                        Taken off
                      </span>
                    )}
                    <span className="flex-1" />
                    {!off && (
                      <button
                        className="text-sm text-ink-muted hover:text-danger hover:underline disabled:opacity-50"
                        onClick={() => remove(b.id, b.legal_name)} disabled={busy}
                        title="Take this broker off the programme"
                      >
                        Take off
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
                    <div className="flex flex-wrap items-center gap-3">
                      <p className="text-sm text-ink-muted">
                        No contracts with this broker on this programme yet.
                      </p>
                      {!off && (
                        <Link
                          to={`/direct/setup?program_id=${prog.id}&broker_party_id=${b.id}`}
                          className="inline-flex items-center gap-1 rounded bg-navy px-2.5 py-1 text-sm font-medium text-white hover:bg-navy-dark"
                        >
                          <Upload size={13} /> Upload contract
                        </Link>
                      )}
                    </div>
                  ) : (
                    <ul className="space-y-1.5">
                      {b.contracts.map(c => (
                        <li key={c.id}>
                          <button
                            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-surface-2"
                            onClick={() => nav(`/programs/${prog.id}/contracts/${c.id}`)}
                          >
                            <FileText size={14} className="shrink-0 text-ink-muted" />
                            <span className="min-w-0 flex-1 truncate">
                              {c.filename ?? `Contract #${c.id}`}
                            </span>
                            <span className={`rounded px-2 py-0.5 text-xs ${
                              c.approval_status === "approved" ? "bg-success/10 text-success"
                              : c.approval_status === "pending_approval" ? "bg-warn/10 text-warn"
                              : "bg-danger/10 text-danger"}`}>
                              {c.approval_status === "approved" ? "Live"
                                : c.approval_status === "pending_approval" ? "Waiting on you"
                                : "Rejected"}
                            </span>
                          </button>
                        </li>
                      ))}
                      {!off && (
                        <li className="pt-1">
                          {/* To the broker's own page, not straight into the
                              wizard: a second contract is usually a decision
                              about what they already hold, so it starts from
                              seeing that — across every programme, not just
                              this one. The upload lives there. */}
                          <Link
                            to={`/brokers/${b.id}`}
                            className="inline-flex items-center gap-1 px-2 text-sm text-ink-muted hover:text-navy hover:underline"
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

        <p className="mt-4 flex items-center gap-1.5 text-sm text-ink-muted">
          <Layers size={13} />
          A broker can be on several of your programmes. This screen shows only
          what they do on <b className="font-medium">{prog.name}</b>.
        </p>
      </PageBody>
    </>
  );
}
