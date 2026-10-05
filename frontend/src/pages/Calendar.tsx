// Group 3 — "Never miss a deadline": the standalone My Calendar page.
//
// The calendar itself now lives in components/ProgramCalendar, shared with the
// Bordereau Setup screen. What is left HERE is the one thing that page cannot
// have: a program picker. Inside a setup the program is already fixed by the
// pipeline, so no choice is offered there.
//
// Program only. It used to ask for a carrier first, from when the page also
// served people looking after several carriers; a carrier signed in only ever
// sees its own programs, so that step was one option and one extra click.
//
// This page is kept (and still reached from the bell, the deadline card, the
// dashboard link and the deadline email) so a program with no bordereau setup
// yet still has somewhere to get its schedule set.
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { CalendarDays } from "lucide-react";
import { listPrograms, type ProgramLite } from "../api/calendar";
import { api } from "../api/client";
import ProgramCalendar from "../components/ProgramCalendar";
import NotificationBell from "../components/NotificationBell";

export default function Calendar() {
  const [programs, setPrograms] = useState<ProgramLite[]>([]);
  const [loaded, setLoaded] = useState(false);
  // ?program= lets other screens land here already narrowed — the dashboard's
  // "N bordereaux overdue" link is the main one. Read once, as the initial
  // value, so later picks are plain state and cannot fight the URL. An old
  // ?carrier= is simply ignored.
  const [sp, setSp] = useSearchParams();
  const [selProgram, setSelProgram] = useState<number | "">(
    () => (sp.get("program") ? Number(sp.get("program")) : ""));
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try { setPrograms(await listPrograms()); }
      catch (e: any) {
        setErr(e?.response?.data?.detail ?? e?.message ?? "Failed to load programs.");
      }
      setLoaded(true);
    })();
  }, []);

  // A linked program missing from the list is looked up on its own. The list
  // holds only programmes created in the app, but the Bordereau Calendar shows
  // every programme the carrier has, so its links can name one that is not in
  // it. One that is not this tenant's (or was deleted) is refused by the server
  // and dropped, rather than leaving a calendar up for something we cannot load.
  useEffect(() => {
    if (!loaded || selProgram === "") return;
    if (programs.some(p => p.id === selProgram)) return;
    let live = true;
    api.get<ProgramLite>(`/programs/${selProgram}`)
      .then(r => { if (live && r.data?.id) setPrograms(prev => [...prev, r.data]); })
      .catch(() => { if (live) setSelProgram(""); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, programs, selProgram]);

  const sorted = useMemo(
    () => [...programs].sort((a, b) => a.name.localeCompare(b.name)), [programs]);

  const pickProgram = (v: string) => {
    setSelProgram(v === "" ? "" : Number(v));
    setSp(v === "" ? {} : { program: v }, { replace: true });
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
                <label>Program</label>
                <select value={selProgram} onChange={e => pickProgram(e.target.value)}>
                  <option value="">Select a program…</option>
                  {sorted.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
            </div>

            {selProgram === "" ? (
              <span className="muted" style={{ fontSize: 13 }}>
                {loaded && programs.length === 0
                  ? "You have no programs yet."
                  : "Choose a program to see and change its deadlines."}
              </span>
            ) : (
              // Keyed on the program so switching resets the component's state
              // rather than briefly showing the previous program's periods.
              <ProgramCalendar
                key={selProgram}
                programId={Number(selProgram)}
                programName={programs.find(p => p.id === selProgram)?.name} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
