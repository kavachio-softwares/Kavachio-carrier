/**
 * Create Programme — and pick its brokers in the same step.
 *
 * Deliberately one screen, not two. A programme with no broker cannot hold a
 * contract, so creating one and "adding brokers later" just leaves you with
 * something nobody can use. Ticking them here means the thing you end up with
 * actually works.
 *
 * The carrier is not asked for. You are signed in as it.
 */
import { useEffect, useState } from "react";
import { PROGRAMME_FREQUENCIES } from "../constants/frequency";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { addProgrammeBroker, getBrokers, type BrokerSummary } from "../api/hierarchy";
import { OnboardingBadge } from "../components/OnboardingBadge";
import { getSegments, addSegment, type Segment } from "../api/segments";

// Segments are the CARRIER's own list now (api/segments), not a fixed five —
// a carrier that writes Cyber or Aviation can say so. Loaded on mount; the
// first load seeds the five this constant used to hold, so nothing a carrier
// already selected disappears.
// Sentinel for the dropdown's "add" option. Not a segment name, and no
// real name can collide with it.
const ADD_SEGMENT = "__add_segment__";
// One list for the whole app — see constants/frequency.ts for why.

export default function AddProgram() {
  const mga = currentMga();
  const nav = useNavigate();

  const [name, setName] = useState("");
  const [segments, setSegments] = useState<Segment[]>([]);
  const [segment, setSegment] = useState("");
  const [newSegment, setNewSegment] = useState("");
  const [addingSegment, setAddingSegment] = useState(false);
  const [creatingSegment, setCreatingSegment] = useState(false);
  const [segErr, setSegErr] = useState("");
  const [productLine, setProductLine] = useState("");
  const [frequency, setFrequency] = useState<string>(PROGRAMME_FREQUENCIES[0].value);
  const [status, setStatus] = useState("active");

  // The carrier's segments. Selecting the first keeps the form immediately
  // valid, exactly as the hard-coded list did.
  useEffect(() => {
    getSegments()
      .then(rows => { setSegments(rows); setSegment(p => p || rows[0]?.name || ""); })
      .catch(() => setSegErr("Could not load your business segments."));
  }, []);

  async function createSegment() {
    const name = newSegment.trim();
    if (!name) return;
    setAddingSegment(true); setSegErr("");
    try {
      const created = await addSegment(name);
      setSegments(rows => [...rows, created]);
      setSegment(created.name);          // pick what you just made
      setNewSegment("");
      setCreatingSegment(false);         // back to the dropdown, now including it
    } catch (e: any) {
      setSegErr(e?.response?.data?.detail ?? "Could not add that segment.");
    } finally { setAddingSegment(false); }
  }

  const [brokers, setBrokers] = useState<BrokerSummary[] | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getBrokers().then(setBrokers).catch(() => setBrokers([]));
  }, []);

  function toggle(id: number) {
    setPicked(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  async function create() {
    if (!name.trim()) { setErr("Give the programme a name."); return; }
    setBusy(true); setErr(null);
    try {
      const { data } = await api.post<{ id: number }>("/programs", {
        name: name.trim(),
        business_segment: segment,
        product_line: productLine.trim() || null,
        bdx_frequency: frequency,
        status,
      }, { params: { mga } });

      // Creating the programme is only half of it. The brokers are what make it
      // usable, so they are linked here rather than left as a second job — one
      // at a time, so a single failure names the broker it failed on.
      const failed: string[] = [];
      for (const id of picked) {
        try {
          await addProgrammeBroker(data.id, id);
        } catch {
          failed.push(brokers?.find(b => b.id === id)?.legal_name ?? `#${id}`);
        }
      }
      if (failed.length) {
        setErr(`Programme created, but these brokers could not be added: ${failed.join(", ")}. Add them from the programme's Brokers screen.`);
        setBusy(false);
        return;
      }
      nav(`/programs/${data.id}/brokers`);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not create the programme.");
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Create Programme"
        subtitle="Set up a new type of business, and choose which brokers will send you business for it."
        action={
          <div className="flex items-center gap-2">
            <button className="rounded border border-border px-3 py-1.5 text-sm hover:bg-surface-2"
              onClick={() => nav("/programs")}>← Programmes</button>
            <button
              className="rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark disabled:opacity-50"
              onClick={create} disabled={busy || !name.trim()}
              title={name.trim() ? undefined : "Give the programme a name first"}>
              {busy ? "Creating…" : "Create programme"}
            </button>
          </div>
        }
      />
      <PageBody>
        {err && (
          <div className="mb-4 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
            {err}
          </div>
        )}

        <div className="grid gap-5 md:grid-cols-2">
          <Card title="Programme">
            <div className="space-y-4">
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-muted">Programme name</label>
                <input className="w-full rounded border border-border px-2.5 py-1.5 text-sm"
                  autoFocus value={name} placeholder="e.g. Spectrum Transportation"
                  onChange={e => setName(e.target.value)} />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs font-medium text-ink-muted">Business segment</label>
                  {/* Adding a segment is an option IN the dropdown, not a second
                      control beside it: naming a new segment and picking an
                      existing one are the same decision, so they share one
                      control. Choosing "+ Add a segment…" swaps this select for
                      the text field in place, and picking/cancelling swaps back. */}
                  {!creatingSegment ? (
                    <select className="w-full rounded border border-border px-2.5 py-1.5 text-sm"
                      value={segment}
                      onChange={e => {
                        if (e.target.value === ADD_SEGMENT) { setCreatingSegment(true); setSegErr(""); }
                        else setSegment(e.target.value);
                      }}>
                      {segments.length === 0 && <option value="">Loading…</option>}
                      {segments.map(s => <option key={s.id} value={s.name}>{s.name}</option>)}
                      <option value={ADD_SEGMENT}>+ Add a segment…</option>
                    </select>
                  ) : (
                    <div className="flex gap-1.5">
                      <input
                        className="min-w-0 flex-1 rounded border border-border px-2.5 py-1.5 text-sm"
                        autoFocus placeholder="e.g. Cyber"
                        value={newSegment}
                        onChange={e => setNewSegment(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === "Enter") { e.preventDefault(); createSegment(); }
                          if (e.key === "Escape") { setCreatingSegment(false); setNewSegment(""); setSegErr(""); }
                        }}
                      />
                      <button type="button"
                        className="shrink-0 rounded bg-navy px-2.5 py-1.5 text-sm font-medium text-white hover:bg-navy-dark disabled:opacity-50"
                        onClick={createSegment}
                        disabled={!newSegment.trim() || addingSegment}>
                        {addingSegment ? "Adding…" : "Add"}
                      </button>
                      <button type="button"
                        className="shrink-0 rounded border border-border px-2 py-1.5 text-sm hover:border-navy"
                        onClick={() => { setCreatingSegment(false); setNewSegment(""); setSegErr(""); }}>
                        Cancel
                      </button>
                    </div>
                  )}
                  {segErr && <div className="mt-1 text-xs text-warn">{segErr}</div>}
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-ink-muted">Product line</label>
                  <input className="w-full rounded border border-border px-2.5 py-1.5 text-sm"
                    value={productLine} placeholder="e.g. Commercial Auto"
                    onChange={e => setProductLine(e.target.value)} />
                </div>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs font-medium text-ink-muted">BDX frequency</label>
                  <select className="w-full rounded border border-border px-2.5 py-1.5 text-sm"
                    value={frequency} onChange={e => setFrequency(e.target.value)}>
                    {PROGRAMME_FREQUENCIES.map(f =>
                      <option key={f.value} value={f.value}>{f.label}</option>)}
                  </select>
                  <p className="mt-1 text-xs text-ink-muted">How often you expect a file.</p>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-ink-muted">Status</label>
                  <select className="w-full rounded border border-border px-2.5 py-1.5 text-sm"
                    value={status} onChange={e => setStatus(e.target.value)}>
                    <option value="active">Active</option>
                    <option value="draft">Draft</option>
                  </select>
                </div>
              </div>
            </div>
          </Card>

          <Card title="Assign brokers">
            <p className="mb-3 text-xs leading-relaxed text-ink-muted">
              Tick every broker that will send you business on this programme — a
              programme is normally fed by several. Only brokers you already hold
              appear here; create a new one on the{" "}
              <a href="/users/new" className="text-navy hover:underline">Users &amp; Roles</a>{" "}
              first and it will show up in this list.
            </p>

            {brokers === null && <p className="text-sm text-ink-muted">Loading…</p>}

            {brokers?.length === 0 && (
              <p className="text-sm text-ink-muted">
                You have no brokers yet. You can create the programme now and add
                them later, but it cannot hold a contract until it has one.
              </p>
            )}

            {brokers && brokers.length > 0 && (
              <>
                <div className="space-y-1.5">
                  {brokers.map(b => (
                    <label key={b.id}
                      className={`flex cursor-pointer items-center gap-2.5 rounded border px-2.5 py-2 text-sm ${
                        picked.has(b.id) ? "border-navy bg-navy/5" : "border-border hover:bg-surface-2"}`}>
                      <input type="checkbox" checked={picked.has(b.id)}
                        onChange={() => toggle(b.id)} />
                      <span className="min-w-0 flex-1">
                        <span className="block font-medium">{b.legal_name}</span>
                        <span className="block text-xs text-ink-muted">
                          {b.programmes.length === 0
                            ? "On no programme yet"
                            : `Already on ${b.programmes.map(p => p.name).join(", ")}`}
                          {" · "}{b.contract_count} contract{b.contract_count === 1 ? "" : "s"}
                        </span>
                      </span>
                      <OnboardingBadge status={b.onboarding_status} />
                    </label>
                  ))}
                </div>
                <p className="mt-3 text-xs text-ink-muted">
                  {picked.size === 0
                    ? "No brokers picked — the programme will start empty."
                    : `${picked.size} broker${picked.size === 1 ? "" : "s"} will be put on this programme.`}
                </p>
              </>
            )}
          </Card>
        </div>
      </PageBody>
    </>
  );
}
