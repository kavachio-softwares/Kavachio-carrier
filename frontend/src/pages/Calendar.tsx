// Group 3 — "Never miss a deadline": the standalone My Calendar page.
//
// The calendar itself now lives in components/ProgramCalendar, shared with the
// Bordereau Setup screen. What is left HERE is the one thing that page cannot
// have: a carrier -> program picker. Inside a setup the carrier and program are
// already fixed by the pipeline, so no choice is offered there.
//
// The picker is two steps because a carrier holds many programs, and program
// names are only meaningful under their carrier ("Program B" says nothing on
// its own). Narrowing by carrier first also matches how Program Management
// scopes its book, so the two screens are navigated the same way.
//
// This page is kept (and still reached from the bell, the deadline card, the
// dashboard link and the deadline email) so a program with no bordereau setup
// yet still has somewhere to get its schedule set.
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { CalendarDays } from "lucide-react";
import { listCarriers, listPrograms, type CarrierLite, type ProgramLite } from "../api/calendar";
import { AUTH_EVENT, getTenantBrand } from "../auth";
import ProgramCalendar from "../components/ProgramCalendar";
import NotificationBell from "../components/NotificationBell";

export default function Calendar() {
  const [programs, setPrograms] = useState<ProgramLite[]>([]);
  const [carriers, setCarriers] = useState<CarrierLite[]>([]);
  // ?carrier= / ?program= let other screens land here already narrowed — the
  // dashboard's "N bordereaux overdue" link is the main one. Read once, as the
  // initial value, so later picks are plain state and cannot fight the URL.
  const [sp, setSp] = useSearchParams();
  const [selCarrier, setSelCarrier] = useState<number | "">(
    () => (sp.get("carrier") ? Number(sp.get("carrier")) : ""));
  const [selProgram, setSelProgram] = useState<number | "">(
    () => (sp.get("program") ? Number(sp.get("program")) : ""));
  const [err, setErr] = useState<string | null>(null);
  // THIS TENANT'S OWN NAME — "Acceltree" — which is what the sidebar shows.
  //
  // It is the honest last resort for naming a carrier, because in this model the
  // tenant IS the carrier. Seeded from the cached brand (hydrated out of
  // localStorage, so it is normally there on the first paint) and refreshed on
  // AUTH_EVENT, which setTenantBrand fires once /tenants lands on a fresh login.
  const [ownName, setOwnName] = useState<string | null>(
    () => getTenantBrand()?.legal_name ?? null);
  useEffect(() => {
    const onAuth = () => setOwnName(getTenantBrand()?.legal_name ?? null);
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, []);

  useEffect(() => {
    (async () => {
      try { setPrograms(await listPrograms()); }
      catch (e: any) {
        setErr(e?.response?.data?.detail ?? e?.message ?? "Failed to load programs.");
      }
      // Names only. A failure here costs labels, not function, so it must not
      // surface an error banner over a calendar that still works.
      try { setCarriers(await listCarriers()); } catch { setCarriers([]); }
    })();
  }, []);

  // The program id is authoritative: take the carrier FROM it rather than trusting
  // both params to agree, so a stale or hand-edited ?carrier= can never leave the
  // program hidden behind the wrong carrier. Runs once, when programs land.
  useEffect(() => {
    if (programs.length === 0) return;
    const pid = sp.get("program") ? Number(sp.get("program")) : null;
    if (pid == null || Number.isNaN(pid)) return;
    const p = programs.find(x => x.id === pid);
    if (!p) { setSelProgram(""); return; }   // not this tenant's, or deleted
    setSelCarrier(carrierKey(p));
    setSelProgram(pid);
    // Re-runs when the carrier directory lands too: carrierKey depends on it,
    // and programs usually arrive first, so keying on the earlier answer alone
    // could leave the program hidden behind a group it no longer belongs to.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [programs, carriers]);

  const carrierName = useMemo(() => {
    const byId = new Map(carriers.map(c => [c.id, c.legal_name]));
    // Four steps, most specific first:
    //   1. the party's real name, when /parties returns it;
    //   2. the program's own free-text lead_carrier;
    //   3. THIS TENANT'S name — see below for why this is the common case;
    //   4. "Unassigned carrier", only when even that is unknown.
    //
    // Step 3 does the work in practice. Carrier parties are created by Kavachio
    // platform admins, not through the tenant app, so they carry
    // is_app_managed = false — and /parties returns app-managed parties only.
    // Step 1 therefore misses EVERY carrier, and a program that names its
    // carrier correctly still had no name to show. Falling back to the tenant is
    // not a guess: a carrier tenant's programs belong to that carrier.
    return (p: ProgramLite) =>
      (p.party_id != null ? byId.get(p.party_id) : null)
      || p.lead_carrier || ownName || "Unassigned carrier";
  }, [carriers, ownName]);

  // WHICH GROUP a program's carrier belongs to. Deliberately NOT `party_id`.
  //
  // A party we cannot NAME must not be its own group. Keying on the raw id put
  // programs with no party under -1 and programs with an unresolvable party
  // under that party's id — two groups whose labels both fall through to the
  // same fallback text, so the picker showed the same carrier name twice with
  // the programs split arbitrarily between them. Collapsing every unnameable
  // party into -1 gives one entry per name the user can actually read.
  const carrierKey = useMemo(() => {
    const byId = new Map(carriers.map(c => [c.id, c.legal_name]));
    return (p: ProgramLite) =>
      (p.party_id != null && byId.has(p.party_id)) ? p.party_id : -1;
  }, [carriers]);

  // Only carriers that actually hold a program — an empty carrier in this list
  // would be a dead end, since there would be nothing to pick at step two.
  const carrierOptions = useMemo(() => {
    const seen = new Map<number, string>();
    for (const p of programs) seen.set(carrierKey(p), carrierName(p));
    return [...seen].map(([id, name]) => ({ id, name }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [programs, carrierKey, carrierName]);

  const carrierPrograms = useMemo(
    () => selCarrier === "" ? []
      : programs.filter(p => carrierKey(p) === selCarrier),
    [programs, carrierKey, selCarrier]);

  // Changing carrier must clear the program — otherwise the calendar below keeps
  // rendering a program that is no longer in the visible list.
  const pickCarrier = (v: string) => {
    setSelCarrier(v === "" ? "" : Number(v));
    setSelProgram("");
    setSp(v === "" ? {} : { carrier: v }, { replace: true });
  };

  const pickProgram = (v: string) => {
    setSelProgram(v === "" ? "" : Number(v));
    setSp(v === "" ? { carrier: String(selCarrier) }
                   : { carrier: String(selCarrier), program: v }, { replace: true });
  };

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t"><h2>My Calendar</h2></div>
          {/* Reminder bell — overdue / due-soon submissions, right side of the header. */}
          <div className="actions">
            <NotificationBell placement="inline" />
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

        <div className="card" style={{ marginBottom: 18 }}>
          {/* Names what the card actually holds — the picker, the deadline
              settings AND the deadline list. It used to be titled "Submission
              schedule", which described only the middle third of it. */}
          <div className="card-h">
            <CalendarDays className="ci" />
            <h3>Bordereau deadlines</h3>
            <span className="sub">pick a program to see when its bordereaux are due</span>
          </div>
          <div style={{ padding: "16px 20px" }}>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <div className="field" style={{ minWidth: 260, flex: "0 1 320px" }}>
                <label>Carrier</label>
                <select value={selCarrier} onChange={e => pickCarrier(e.target.value)}>
                  <option value="">Select a carrier…</option>
                  {carrierOptions.map(c =>
                    <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </div>

              <div className="field" style={{ minWidth: 260, flex: "0 1 320px" }}>
                <label>Program</label>
                <select value={selProgram} disabled={selCarrier === ""}
                  onChange={e => pickProgram(e.target.value)}>
                  <option value="">
                    {selCarrier === "" ? "Select a carrier first…" : "Select a program…"}
                  </option>
                  {carrierPrograms.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
            </div>

            {selProgram === "" ? (
              <span className="muted" style={{ fontSize: 13 }}>
                {selCarrier === ""
                  ? "Choose a carrier, then one of its programs, to see and change its deadlines."
                  : carrierPrograms.length === 0
                    ? "This carrier has no programs yet."
                    : "Choose a program to see and change its deadlines."}
              </span>
            ) : (
              // Keyed on the program so switching resets the component's state
              // rather than briefly showing the previous program's periods.
              <ProgramCalendar
                key={selProgram}
                programId={Number(selProgram)}
                programName={carrierPrograms.find(p => p.id === selProgram)?.name} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
