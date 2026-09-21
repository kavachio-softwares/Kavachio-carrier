/**
 * Configure Program — the whole setup as one flow on one screen:
 *   1. the programme, 2. its brokers, 3. their contracts.
 *
 * Step 1 saves the programme on its own ("Configure Program", under the
 * Programme card). Step 2, the broker list, unlocks only once that exists —
 * a broker is put ON a programme, and until it is saved there is nothing to
 * put them on. The list is shown dimmed beforehand so it reads as the next
 * step rather than appearing from nowhere.
 *
 * Still one screen, not two. A programme with no broker cannot hold a
 * contract, so "create it now, add brokers some other day" tends to leave
 * something nobody can use. Doing both here, back to back, means the thing you
 * end up with actually works.
 *
 * "Add New Broker" invites one who is not on the list yet in a dialog, on this
 * screen — the flow never leaves. When the invite goes through, the list is
 * reloaded and whoever newly appears on it is ticked, so the next click is
 * Assign.
 *
 * Step 3 appears once anyone is assigned, because a contract is (programme ×
 * broker) and until then there is no pair to write one for. Each broker gets
 * the same two ways in as the programme's own broker screen: Upload (a dialog,
 * here) for a wording that already exists, and Raise (the full contract form)
 * for terms being agreed now. Raise leaves the screen — it is a long form of
 * its own and is the last thing in the flow.
 *
 * The carrier is not asked for. You are signed in as it.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { PROGRAMME_FREQUENCIES } from "../constants/frequency";
import { Link, useNavigate } from "react-router-dom";
import { Plus, Upload, UserPlus } from "lucide-react";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { PageBody, PageHeader } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { addProgrammeBroker, getBrokers, type BrokerSummary } from "../api/hierarchy";
import { OnboardingBadge } from "../components/OnboardingBadge";
import { InviteBrokerModal } from "../components/InviteBrokerModal";
import { InviteSentModal } from "../components/InviteSentModal";
import AddContractModal from "../components/AddContractModal";
import { Pagination } from "../components/Pagination";
import { getSegments, addSegment, type Segment } from "../api/segments";

// Segments are the CARRIER's own list now (api/segments), not a fixed five —
// a carrier that writes Cyber or Aviation can say so. Loaded on mount; the
// first load seeds the five this constant used to hold, so nothing a carrier
// already selected disappears.
// Sentinel for the dropdown's "add" option. Not a segment name, and no
// real name can collide with it.
const ADD_SEGMENT = "__add_segment__";
// Five, not the app-wide ten: this list shares the row with the Programme
// card, and ten tall broker rows ran far below it. Five keeps both cards
// roughly level so the page reads as one step beside another.
const BROKER_PAGE_SIZE = 5;
// One list for the whole app — see constants/frequency.ts for why.

// The step number on each card's title. The three cards ARE a sequence — each
// unlocks the next — so the numbers say something true about them.
function Step({ n, children }: { n: number; children: ReactNode }) {
  return (
    <span className="flex items-center gap-2">
      <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-navy text-[11px] font-semibold text-white">
        {n}
      </span>
      {children}
    </span>
  );
}

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
  const [err, setErr] = useState<string | null>(null);
  // Paged on screen, not by the server: the whole list is already needed here —
  // to spot who is new after an invite, and to list the assigned in step 3 —
  // so there is nothing to save by fetching it a page at a time. Ticks live in
  // `picked`, not on the page, so they survive paging.
  const [brokerPage, setBrokerPage] = useState(1);

  // Set once step 1 succeeds, and its presence IS the step: null while the
  // programme is still being filled in, an id once it is saved and the broker
  // list is open.
  const [programId, setProgramId] = useState<number | null>(null);
  const [configuring, setConfiguring] = useState(false);
  const [assigning, setAssigning] = useState(false);
  // Brokers an earlier attempt already put on the programme. The server
  // refuses a second add ("already on this programme", 409), so a retry after
  // a partial failure has to skip these rather than trip over them.
  const [assigned, setAssigned] = useState<Set<number>>(new Set());
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteNote, setInviteNote] = useState<string | null>(null);
  // The same "Invite sent" confirmation the Party invite page shows — the
  // dialog closing on its own was too quiet to be sure anything happened.
  const [sent, setSent] = useState<{ message: string; email: string } | null>(null);

  // Step 3. Who the upload dialog is open for, and how many contracts each
  // broker has had uploaded here — so a row can say it is done.
  const [uploadFor, setUploadFor] = useState<BrokerSummary | null>(null);
  const [uploaded, setUploaded] = useState<Record<number, number>>({});
  // Set when the open upload dialog has actually saved a contract. Its Done
  // button then carries on to Bordereau Setup instead of just closing.
  const [contractSaved, setContractSaved] = useState(false);
  // Bumped after a successful Assign; the effect scrolls step 3 into view once
  // it has rendered (it only exists after the first broker is assigned).
  const [jumpToContracts, setJumpToContracts] = useState(0);
  const contractsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (jumpToContracts) contractsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [jumpToContracts]);

  useEffect(() => {
    getBrokers().then(setBrokers).catch(() => setBrokers([]));
  }, []);

  function toggle(id: number) {
    if (assigned.has(id)) return;
    setPicked(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  // Step 1 — the programme alone.
  async function configure() {
    if (!name.trim()) { setErr("Give the programme a name."); return; }
    setConfiguring(true); setErr(null);
    try {
      const { data } = await api.post<{ id: number }>("/programs", {
        name: name.trim(),
        business_segment: segment,
        product_line: productLine.trim() || null,
        bdx_frequency: frequency,
        status,
      }, { params: { mga } });
      setProgramId(data.id);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not configure the programme.");
    } finally { setConfiguring(false); }
  }

  // Step 2 — put every ticked broker on it. One at a time, so a failure names
  // the broker it failed on. True when nothing is left outstanding.
  async function assignPicked(): Promise<boolean> {
    if (programId == null) return false;
    const todo = [...picked].filter(id => !assigned.has(id));
    if (todo.length === 0) return true;
    setAssigning(true); setErr(null);
    const ok: number[] = [];
    const failed: number[] = [];
    for (const id of todo) {
      try { await addProgrammeBroker(programId, id); ok.push(id); }
      catch { failed.push(id); }
    }
    setAssigned(prev => new Set([...prev, ...ok]));
    setAssigning(false);
    if (failed.length) {
      const names = failed.map(id => brokers?.find(b => b.id === id)?.legal_name ?? `#${id}`);
      setErr(`These brokers could not be added: ${names.join(", ")}. A broker who `
        + `already works with another carrier can only be put on a programme once `
        + `they accept your invitation; otherwise, try again.`);
      return false;
    }
    return true;
  }

  // Step 2's button. With nobody picked there is no contract to add — a
  // contract needs a broker — so the programme's own screen is next. Otherwise
  // assign, and carry on to step 3 on this same screen.
  async function finish() {
    if (pending === 0 && assigned.size === 0) {
      nav(`/programs/${programId}/brokers`);
      return;
    }
    if (await assignPicked()) setJumpToContracts(j => j + 1);
  }

  // After an invite: reload the list and tick whoever is new on it. The
  // invite does not return an id (it must not reveal whether that address
  // belonged to an existing broker), so "new" is read off the list itself —
  // which shows nothing the Party screen would not show anyway.
  async function onInvited(message: string, email: string) {
    setInviteNote(message);
    setSent({ message, email });
    const before = new Set((brokers ?? []).map(b => b.id));
    try {
      const fresh = await getBrokers();
      setBrokers(fresh);
      const added = fresh.filter(b => !before.has(b.id)).map(b => b.id);
      if (added.length) {
        setPicked(prev => new Set([...prev, ...added]));
        setBrokerPage(1);   // newest first, so whoever was just ticked is here
      }
    } catch { /* the list keeps what it had; the note still confirms the invite */ }
  }

  const locked = programId != null;
  const pending = [...picked].filter(id => !assigned.has(id)).length;
  const finishLabel = pending > 0
    ? `Assign ${pending} broker${pending === 1 ? "" : "s"}`
    : "Finish without brokers";
  const brokerCount = brokers?.length ?? 0;
  const brokerPageCount = Math.max(1, Math.ceil(brokerCount / BROKER_PAGE_SIZE));
  // Clamped so a list that shrinks under you never strands you on a blank page.
  const pageNow = Math.min(brokerPage, brokerPageCount);
  const brokersOnPage = (brokers ?? []).slice(
    (pageNow - 1) * BROKER_PAGE_SIZE, pageNow * BROKER_PAGE_SIZE);

  // Brokers now on the programme, in list order, for step 3.
  const assignedBrokers = (brokers ?? []).filter(b => assigned.has(b.id));

  return (
    <>
      <PageHeader
        title="Configure Program"
        subtitle="Set up a new type of business, choose which brokers send you business for it, and add their contracts."
        action={
          /* The save lives under each step now, where the step is — a single
             top-right button used to do both at once and is no longer needed. */
          <button className="rounded border border-border px-3 py-1.5 text-sm hover:bg-surface-2"
            onClick={() => nav("/programs")}>← Programmes</button>
        }
      />
      <PageBody>
        {err && (
          <div className="mb-4 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
            {err}
          </div>
        )}

        <div className="grid gap-5 md:grid-cols-2">
          <Card title={<Step n={1}>Programme</Step>}>
            {/* Locked once saved: this form only ever CREATES, so editing a
                field after step 1 would change nothing — and look as if it had. */}
            <fieldset disabled={locked} className="m-0 min-w-0 border-0 p-0">
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
            </fieldset>

            <div className="mt-5 flex items-center justify-end border-t border-border pt-4">
              {!locked ? (
                <button
                  className="rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark disabled:opacity-50"
                  onClick={configure} disabled={configuring || !name.trim()}
                  title={name.trim() ? undefined : "Give the programme a name first"}>
                  {configuring ? "Configuring…" : "Configure Program"}
                </button>
              ) : (
                <p className="text-sm font-medium text-ok">
                  Programme configured — now assign its brokers.
                </p>
              )}
            </div>
          </Card>

          <Card title={<Step n={2}>Assign brokers</Step>}
            action={
              <button type="button"
                className="inline-flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-xs font-medium hover:border-navy disabled:opacity-50"
                onClick={() => setInviteOpen(true)} disabled={!locked || assigning}
                title={locked ? "Invite a broker who is not on this list yet"
                  : "Configure the programme first"}>
                <UserPlus size={13} /> Add New Broker
              </button>
            }>
            {!locked && (
              <p className="mb-3 rounded border border-border bg-surface-2 px-3 py-2 text-xs text-ink-muted">
                Configure the programme first — its brokers can be assigned once it is saved.
              </p>
            )}

            <p className="mb-3 text-xs leading-relaxed text-ink-muted">
              Select brokers who will send business for this programme, or
              click <b className="font-medium text-ink">Add New Broker</b> if
              they are not listed.
            </p>

            {inviteNote && (
              /* Amber, not green: the invitation is out but nothing is settled —
                 they still have to accept, and nobody is on the programme until
                 Assign is pressed. Green read as "done". */
              <p className="mb-3 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
                {inviteNote} Anyone new is ticked below — Assign to put them on this programme.
              </p>
            )}

            {/* Shown but inert until step 1 is done, so the next step is
                visible rather than appearing from nowhere. */}
            <fieldset disabled={!locked || assigning}
              className={`m-0 min-w-0 border-0 p-0 ${locked ? "" : "opacity-50"}`}>
              {brokers === null && <p className="text-sm text-ink-muted">Loading…</p>}

              {brokers?.length === 0 && (
                <p className="text-sm text-ink-muted">
                  You have no brokers yet. Use Add New Broker to invite one, or finish
                  without — but the programme cannot hold a contract until it has one.
                </p>
              )}

              {brokers && brokers.length > 0 && (
                <div className="space-y-1.5">
                  {brokersOnPage.map(b => {
                    const done = assigned.has(b.id);
                    const on = picked.has(b.id) || done;
                    return (
                      <label key={b.id}
                        className={`flex items-center gap-2.5 rounded border px-2.5 py-2 text-sm ${
                          locked && !done ? "cursor-pointer" : ""} ${
                          on ? "border-navy bg-navy/5" : "border-border hover:bg-surface-2"}`}>
                        <input type="checkbox" checked={on} disabled={done}
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
                        {done
                          ? <span className="text-xs font-medium text-ok">Assigned</span>
                          : <OnboardingBadge status={b.onboarding_status} />}
                      </label>
                    );
                  })}
                </div>
              )}

              {/* The shared pager is styled by the .proto tokens; proto-embed
                  brings them in without .proto's page background. Only shown
                  once there is more than one page. */}
              {brokerPageCount > 1 && (
                <div className="proto proto-embed -mx-5 mt-3">
                  <Pagination page={pageNow} pageCount={brokerPageCount}
                    pageSize={BROKER_PAGE_SIZE} totalItems={brokerCount}
                    onPageChange={setBrokerPage} noun="brokers" />
                </div>
              )}
            </fieldset>

            {locked && (
              <div className="mt-4 flex items-center justify-between gap-3 border-t border-border pt-4">
                <p className="text-xs text-ink-muted">
                  {pending > 0
                    ? `${pending} broker${pending === 1 ? "" : "s"} will be put on this programme.`
                    : assigned.size > 0
                    ? `${assigned.size} broker${assigned.size === 1 ? "" : "s"} assigned — add their contracts below.`
                    : "No brokers picked — the programme will start empty."}
                </p>
                {/* Once everything ticked is on the programme, step 3 below is
                    where the flow continues, so there is nothing to press here. */}
                {(pending > 0 || assigned.size === 0) && (
                  <button
                    className="shrink-0 rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark disabled:opacity-50"
                    onClick={finish} disabled={assigning}>
                    {assigning ? "Assigning…" : finishLabel}
                  </button>
                )}
              </div>
            )}
          </Card>
        </div>

        {assigned.size > 0 && programId != null && (
          <div ref={contractsRef} className="mt-5 scroll-mt-4">
            <Card title={<Step n={3}>Add contracts</Step>}>
              <p className="mb-3 text-xs leading-relaxed text-ink-muted">
                A contract is what each broker's bordereaux are checked against.{" "}
                <b className="font-medium text-ink">Raise a contract</b> to set the terms now, or{" "}
                <b className="font-medium text-ink">Upload contract</b> for a wording that already exists.
              </p>

              <div className="space-y-1.5">
                {assignedBrokers.map(b => {
                  const n = uploaded[b.id] ?? 0;
                  return (
                    <div key={b.id}
                      className="flex flex-wrap items-center gap-3 rounded border border-border px-3 py-2.5">
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium">{b.legal_name}</span>
                        <span className={`block text-xs ${n ? "text-ok" : "text-ink-muted"}`}>
                          {n ? `${n} contract${n === 1 ? "" : "s"} uploaded`
                             : "No contract on this programme yet"}
                        </span>
                      </span>
                      <OnboardingBadge status={b.onboarding_status} />
                      <div className="flex shrink-0 items-center gap-2">
                        <button type="button" onClick={() => { setContractSaved(false); setUploadFor(b); }}
                          className="inline-flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-xs font-medium hover:bg-surface-2">
                          <Upload size={13} /> Upload contract
                        </button>
                        <Link to={`/contracts/new?program_id=${programId}&broker_party_id=${b.id}`}
                          className="inline-flex items-center gap-1.5 rounded bg-navy px-3 py-1.5 text-xs font-medium text-white hover:bg-navy-dark hover:no-underline">
                          <Plus size={13} /> Raise a contract
                        </Link>
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="mt-4 flex items-center justify-between gap-3 border-t border-border pt-4">
                <p className="text-xs text-ink-muted">
                  You can add more brokers and contracts later from the programme's own page.
                </p>
                <button type="button"
                  className="shrink-0 rounded border border-border px-3 py-1.5 text-sm hover:bg-surface-2"
                  onClick={() => nav(`/programs/${programId}/brokers`)}>
                  Go to programme →
                </button>
              </div>
            </Card>
          </div>
        )}

        {/* Step 4, reached from here. Once a contract is saved this broker has
            everything Bordereau Setup asks for — programme, broker, contract —
            so Done goes straight on to it, with the programme and broker
            filled in; Setup binds the contract from those two by itself.
            Closed without saving (Cancel, ×), the dialog just closes. */}
        {uploadFor && programId != null && (
          <AddContractModal
            open
            onClose={() => {
              if (contractSaved)
                nav(`/direct/setup?program_id=${programId}&broker_party_id=${uploadFor.id}`);
              else setUploadFor(null);
            }}
            broker={uploadFor}
            programmes={[{ id: programId, name: name.trim(), status }]}
            onAdded={() => {
              setUploaded(u => ({ ...u, [uploadFor.id]: (u[uploadFor.id] ?? 0) + 1 }));
              setContractSaved(true);
            }} />
        )}

        <InviteBrokerModal open={inviteOpen} onClose={() => setInviteOpen(false)}
          onInvited={onInvited} />

        {sent && (
          <InviteSentModal
            title="Broker invited"
            message={sent.message}
            email={sent.email}
            note="They are ticked in the broker list — press Assign to put them on this programme."
            doneLabel="Continue"
            onDone={() => setSent(null)}
          />
        )}
      </PageBody>
    </>
  );
}
