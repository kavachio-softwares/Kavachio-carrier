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
import { ArrowLeft, FileText, Plus, Layers } from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import {
  getHierarchy, getBrokers, addProgrammeBroker, removeProgrammeBroker,
  type HierarchyProgramme, type BrokerSummary,
} from "../api/hierarchy";
import { OnboardingBadge } from "../components/OnboardingBadge";

export default function ProgramBrokers() {
  const { programId } = useParams();
  const pid = Number(programId);
  const nav = useNavigate();

  const [prog, setProg] = useState<HierarchyProgramme | null>(null);
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
                {addable.length === 0 ? "Every broker you hold is already on this programme" : "Choose a broker…"}
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
            <Link to="/users/new" className="text-sm text-navy hover:underline">
              Invite a new broker →
            </Link>
          </div>
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

                  {/* Contracts belong to the PAIR, so they are listed under the
                      broker that produced them rather than on the programme. */}
                  {b.contracts.length === 0 ? (
                    <p className="text-sm text-ink-muted">
                      No contracts with this broker on this programme yet.
                    </p>
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
