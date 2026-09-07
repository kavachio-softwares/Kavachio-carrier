// Group 3 — the submission calendar for ONE program: its schedule settings and
// the periods that schedule produces.
//
// Extracted from pages/Calendar.tsx so the same UI can live in two places:
//
//   * pages/Calendar.tsx        — the standalone page, which picks the program
//                                 with a dropdown and passes it in here;
//   * pages/BordereauSetupEdit  — inside a setup, where the carrier and program
//                                 are already fixed by the pipeline, so there is
//                                 nothing to pick and no dropdown is rendered.
//
// The program is therefore a PROP, never local state. That is the whole reason
// this component exists: the setup screen must not offer a choice that would
// contradict the setup it is part of.
//
// A carrier owns many programs, so `carrierName` is shown as context whenever
// the caller knows it — the schedule itself is keyed on the program alone
// (Program.party_id already fixes which carrier a program belongs to), so no
// carrier is ever passed to the API.
//
// EVERYTHING RENDERS INSIDE `.proto`. The badge / field / btn / note / fbar-*
// classes this markup uses are all defined as `.proto .x` in proto.css, so
// dropping them into the Tailwind-styled setup screen without this wrapper
// renders them as unstyled text. The wrapper is what keeps the calendar looking
// identical in both hosts.
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight } from "lucide-react";
import { InfoTip } from "./InfoTip";
import {
  getCalendar, getSchedule, putSchedule,
  type CalendarResponse, type CalendarStatus, type ScheduleState,
} from "../api/calendar";
import { Pagination } from "./Pagination";
import { FREQUENCIES, FREQUENCY_LABEL } from "../constants/frequency";

const PAGE_SIZE = 12;

// One colour per step of the deadline, escalating grey → blue → amber → red, so
// the table can be read by colour alone. Grey means "nothing for you to do":
// either it is not due yet, or it is already sent. Green is the good outcome.
//
// "Late" is gone. It only ever meant "overdue past the grace period", and with
// no grace period it and Overdue were the same state wearing two words that
// nobody could tell apart on screen.
const STATUS_META: Record<CalendarStatus, { label: string; cls: string }> = {
  scheduled:     { label: "Not due yet",       cls: "b-mut" },
  due_soon:      { label: "Due soon",          cls: "b-info" },
  due_today:     { label: "Due today",         cls: "b-warn" },
  overdue:       { label: "Overdue",           cls: "b-crit" },
  on_time:       { label: "Sent on time",      cls: "b-ok" },
  received_late: { label: "Sent late",         cls: "b-mut" },
};

// All five, from the one shared list — this editor is where an existing weekly
// schedule can still be seen and changed, so unlike the programme forms it does
// not hide it. Half-yearly and yearly used to be missing here entirely, which
// is why a programme created as "Annual" had no way to get a calendar.
const FREQ_OPTIONS: [string, string][] = [
  ["", "— use contract —"],
  ...FREQUENCIES.map(f => [f.value, f.label] as [string, string]),
];

// The same options, keyed for display — so the read-only view names a frequency
// the way the picker does without a second list to keep in step. Legacy stored
// spellings ("annual", "semi-annual") resolve through frequencyLabel().
const FREQ_LABEL: Record<string, string> = {
  ...Object.fromEntries(FREQ_OPTIONS),
  ...FREQUENCY_LABEL,
};

const REASON_TEXT: Record<string, string> = {
  not_set: "No deadlines yet — tell us how often this bordereau is due, and when it starts.",
  need_frequency: "Choose how often this bordereau is due to see its deadlines.",
  need_start_date: "Choose a start date to see its deadlines.",
  need_frequency_and_start: "Choose how often this bordereau is due, and when it starts.",
};

// Triage order: what needs chasing first, then what is settled. Drives both the
// status filter and the header pills, so the two always agree.
const STATUS_ORDER: CalendarStatus[] =
  ["overdue", "due_today", "due_soon", "scheduled", "on_time", "received_late"];
const SUMMARY: CalendarStatus[] =
  ["overdue", "due_today", "due_soon", "scheduled", "on_time"];

type Form = {
  frequency_override: string;
  anchor_date_override: string;
  due_day_of_month: number;
  due_offset_days: number;
  soon_window_days: number;
};

/** Whole days from `fromISO` to `toISO`. Both are parsed at local midnight, so
 *  the result is a calendar-day difference and never off by one from a clock. */
function daysBetween(fromISO: string, toISO: string): number {
  const a = new Date(`${fromISO}T00:00:00`).getTime();
  const b = new Date(`${toISO}T00:00:00`).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return 0;
  return Math.round((b - a) / 86_400_000);
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** "1st", "2nd", "3rd", "10th"… — a day of the month reads as an ordinal, and
 *  the 11th–13th are the exceptions that a naive last-digit rule gets wrong. */
function ordinal(n: number): string {
  const t = n % 100;
  if (t >= 11 && t <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
}

/** ISO date → "26 Aug 2026". The API speaks ISO because it sorts and compares
 *  correctly; a person reading a deadline should not have to. Falls back to the
 *  raw string rather than showing "Invalid Date" if the shape ever surprises us. */
function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric" });
}

/** One labelled fact in the schedule summary. `source` is the small line under
 *  the value — where it came from, or what it covers. */
function Fact({ label, value, source, tone }: {
  label: string; value?: string | null; source?: string; tone?: "crit";
}) {
  return (
    <div>
      <div style={{ color: "var(--p-muted)" }}>{label}</div>
      <div style={{ fontWeight: 700, color: tone === "crit" ? "var(--p-crit)" : "var(--p-ink)" }}>
        {value || "—"}
      </div>
      {source && <div style={{ color: "var(--p-faint)", fontSize: 11.5 }}>{source}</div>}
    </div>
  );
}

export default function ProgramCalendar({
  programId, programName, carrierName, collapsible = false, showPeriods = true, readOnly = false,
  defaultCollapsed = false,
}: {
  programId: number;
  programName?: string | null;
  /** Show the schedule as settled facts instead of a form. For the read-only
   *  setup view, which shows what a setup IS — the same schedule, the same
   *  periods, minus the controls that would change it. Changing it stays in
   *  Bordereau Setup, so there is one place where a deadline moves. */
  readOnly?: boolean;
  /** Start collapsed even when there IS a schedule to show. On a page that is
   *  read top-to-bottom as a summary, the calendar is one section among many and
   *  sits closed like the contracts and sheets around it; where the reader came
   *  specifically to set a schedule, it opens itself. Only meaningful with
   *  `collapsible`. */
  defaultCollapsed?: boolean;
  /** Shown only as context — one carrier owns many programs, and the schedule
   *  is keyed on the program. Omit when the caller doesn't know it. */
  carrierName?: string | null;
  /** Collapse to a one-line summary until the reader asks for the detail.
   *  Used where the calendar is one section among many (Bordereau Setup) and a
   *  program with no schedule would otherwise spend a screenful of space saying
   *  it has nothing. The standalone page leaves this off — there, the calendar
   *  IS the page. */
  collapsible?: boolean;
  /** Render the period list AND its status pills. Off in Bordereau Setup, where the
   *  job is CONFIGURING the schedule — the periods are read on the Calendar page
   *  and on Program Management, which is where chasing a late bordereau starts.
   *  The header's Late/Scheduled pills go with it — they count rows in a table
   *  that would not be on the page. */
  showPeriods?: boolean;
}) {
  const [cal, setCal] = useState<CalendarResponse | null>(null);
  const [sched, setSched] = useState<ScheduleState | null>(null);
  const [form, setForm] = useState<Form>({
    frequency_override: "", anchor_date_override: "",
    due_day_of_month: 10, due_offset_days: 10, soon_window_days: 5,
  });
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  // null = "not decided yet"; the fetch resolves it to whether this program
  // actually HAS a calendar. Once the reader clicks, their choice is a boolean
  // and a later reload must not override it.
  const [open, setOpen] = useState<boolean | null>(null);

  // Scoped to this program server-side rather than fetching every program and
  // filtering in the browser — both hosts only ever show one.
  async function loadCalendar() { setCal(await getCalendar(programId)); }

  useEffect(() => {
    let live = true;
    (async () => {
      setLoading(true); setErr(null); setMsg(null);
      try {
        // SEQUENTIAL, not Promise.all: the schedule GET is what auto-builds the
        // calendar from the contract (C-5), so fetching both at once races — the
        // calendar request can read the table before those periods are committed
        // and the page renders "Active" above an empty period list.
        const st = await getSchedule(programId);
        const c = await getCalendar(programId);
        if (!live) return;
        setCal(c);
        setSched(st);
        // Start expanded only when there is something to show. A program with
        // no schedule collapses to one line instead of a screenful of empty
        // fields — but the reader can still open it to create one. A caller that
        // asked for collapsed keeps it shut either way.
        setOpen(prev => prev ?? (!defaultCollapsed
                                 && (st.resolved || c.rows.length > 0)));
        setForm({
          frequency_override: st.frequency_override ?? "",
          anchor_date_override: st.anchor_date_override ?? "",
          // Falls back to the offset, not to a bare 10: a schedule saved before
          // day-of-month existed carries its deadline in due_offset_days, and
          // for monthly/quarterly the two are the same number (see the backfill
          // in db.init_db). Showing 10 there would silently propose a different
          // deadline than the one actually in force.
          due_day_of_month: st.due_day_of_month ?? st.due_offset_days ?? 10,
          due_offset_days: st.due_offset_days ?? 10,
          soon_window_days: st.soon_window_days ?? 5,
        });
      } catch (e: any) {
        if (live) setErr(e?.response?.data?.detail ?? e?.message ?? "Failed to load calendar.");
      } finally { if (live) setLoading(false); }
    })();
    return () => { live = false; };
  }, [programId]);

  const rows = cal?.rows ?? [];

  // Today as a local calendar date, NOT toISOString() — that converts to UTC
  // first and would report yesterday for anyone east of Greenwich in the evening.
  const todayISO = useMemo(() => {
    const d = new Date();
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }, []);

  // The frequency actually in force — the override if there is one, otherwise
  // the contract's. It decides which deadline control makes sense: a day of the
  // month for every calendar-aligned frequency, days-after-the-period for weekly.
  //
  // NORMALISED, not raw. A contract can carry a legacy spelling like
  // "semi-annual" or "annual"; the server resolves those to the stored tokens
  // and hands the result back as `effective_frequency`, so preferring it here
  // keeps this component from having to know the aliases. It falls back to the
  // raw values while an unsaved override is still being typed.
  const effectiveFreq = (
    form.frequency_override
    || sched?.effective_frequency
    || sched?.contract_frequency
    || "").toLowerCase();
  const isWeekly = effectiveFreq === "weekly";

  async function save() {
    setSaving(true); setErr(null); setMsg(null);
    try {
      // BOTH deadline fields go every time, whichever one is on screen. The
      // server picks by frequency (submission_calendar.due_date_for ignores
      // day-of-month for weekly), so the value the hidden control holds stays
      // intact and switching frequency later does not land on a blank.
      const res = await putSchedule(programId, {
        frequency_override: form.frequency_override || null,
        anchor_date_override: form.anchor_date_override || null,
        due_day_of_month: Number(form.due_day_of_month),
        due_offset_days: Number(form.due_offset_days),
        soon_window_days: Number(form.soon_window_days),
      });
      setSched(res.schedule);
      await loadCalendar();
      setMsg(res.calendar.resolved
        ? `Saved — ${res.calendar.count} deadlines set.`
        : "Saved, but we still need how often it is due and when it starts.");
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? e?.message ?? "Save failed.");
    } finally { setSaving(false); }
  }

  // Header filters: Due-date range + Status. Reset when the program changes.
  const [statusFilter, setStatusFilter] = useState<CalendarStatus | "">("");
  const [dueFrom, setDueFrom] = useState("");
  const [dueTo, setDueTo] = useState("");
  useEffect(() => { setStatusFilter(""); setDueFrom(""); setDueTo(""); }, [programId]);
  const filtersActive = statusFilter !== "" || dueFrom !== "" || dueTo !== "";
  function clearFilters() { setStatusFilter(""); setDueFrom(""); setDueTo(""); }

  // THE BACKLOG PROBLEM.
  //
  // The calendar is generated from the contract's inception date, so a programme
  // that was never filed produces one row per period all the way back to it —
  // years of them, every one reading "Overdue · —". Forty identical rows carry
  // no more information than one sentence does, and they push the periods a
  // broker can still act on off the first page.
  //
  // So the table opens on what is actionable — the current period, a little
  // context behind it, and everything ahead — and folds the older run into a
  // single line that STATES what it is holding and opens in one click. Nothing
  // is dropped: the status pills above still count every period, and the line
  // names the range and the count.
  const RECENT_BEFORE = 3;
  const [showBacklog, setShowBacklog] = useState(false);
  useEffect(() => { setShowBacklog(false); }, [programId]);

  const focusStart = useMemo(() => {
    if (rows.length === 0) return 0;
    // First period that has not finished yet — the current one, or the next.
    let idx = rows.findIndex(r => r.period_end && r.period_end >= todayISO);
    if (idx < 0) idx = rows.length;          // every period is already over
    return Math.max(0, idx - RECENT_BEFORE);
  }, [rows, todayISO]);

  // A filter is an explicit search, so it searches EVERYTHING. Folding the
  // backlog away inside a filtered result would make the filter quietly lie
  // about what matched.
  const collapsing = !showBacklog && !filtersActive && focusStart > 0;
  const windowRows = collapsing ? rows.slice(focusStart) : rows;
  const backlog = useMemo(() => {
    if (focusStart === 0) return null;
    const b = rows.slice(0, focusStart);
    return {
      count: b.length,
      from: b[0]?.period ?? null,
      to: b[b.length - 1]?.period ?? null,
      overdue: b.filter(r => !r.received_at).length,
      sent: b.filter(r => r.received_at).length,
    };
  }, [rows, focusStart]);

  const filteredRows = useMemo(
    () => windowRows.filter(r => {
      if (statusFilter && r.status !== statusFilter) return false;
      if (dueFrom && (!r.due_date || r.due_date < dueFrom)) return false;   // ISO dates: string compare works
      if (dueTo && (!r.due_date || r.due_date > dueTo)) return false;
      return true;
    }),
    [windowRows, statusFilter, dueFrom, dueTo],
  );

  // Summary / filter counts over the whole program, not the filtered view — the
  // summary answers "how am I doing", which a filter must not change.
  const counts = useMemo(() => {
    const c: Partial<Record<CalendarStatus, number>> = {};
    rows.forEach(r => { c[r.status] = (c[r.status] ?? 0) + 1; });
    return c;
  }, [rows]);
  const presentStatuses = STATUS_ORDER.filter(s => (counts[s] ?? 0) > 0);

  // Paginate the FILTERED rows (client-side); reset to page 1 when the program
  // or a filter changes, and clamp if the data shrinks below the current page.
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [programId, statusFilter, dueFrom, dueTo, showBacklog]);
  const totalItems = filteredRows.length;
  const pageCount = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [pageCount, page]);
  const pageRows = filteredRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // Save is allowed only once the schedule would actually resolve: a frequency
  // and a start date, each from either the override field or the contract.
  const freqOk = !!(form.frequency_override || sched?.contract_frequency);
  const anchorOk = !!(form.anchor_date_override || sched?.contract_anchor);
  const canSave = freqOk && anchorOk;

  // The start-date field is INHERITING when the reader has set no override and
  // the contract supplies one. Only then is the contract's date shown inside the
  // control — with no contract date there is nothing to state, and the browser's
  // dd/mm/yyyy mask is then the honest thing to show.
  const inheritingAnchor = !form.anchor_date_override && !!sched?.contract_anchor;

  // What is actually in force, and WHERE each value came from. The banner used to
  // state the frequency and start date without saying whether they were the
  // contract's or somebody's override — the one thing a reader checking a
  // deadline actually needs to know, since an override silently detaches the
  // schedule from the contract.
  const freqFromContract = !sched?.frequency_override;
  const anchorFromContract = !sched?.anchor_date_override;

  // Coverage: the calendar runs to its LAST generated period, which is not an
  // "expiry" — nothing expires it. Stating the last period is honest; calling it
  // an end date would imply the contract term, which this payload doesn't carry.
  const firstPeriod = rows[0]?.period ?? null;
  const lastPeriod = rows.length ? rows[rows.length - 1].period : null;

  // THE PERIOD WE ARE IN RIGHT NOW — the one question a broker opening this page
  // is actually asking ("is this month done or not?"), which a 30-row table
  // sorted by date does not answer without hunting.
  //
  // Matched on the period's own start/end window rather than by counting back
  // from today, so it stays correct for monthly, quarterly and weekly alike.
  // Note this is NOT the same row as "next due": the period covering today is
  // typically not due until well after it ends.
  const currentRow = useMemo(
    () => rows.find(r => r.period_start && r.period_end
      && r.period_start <= todayISO && todayISO <= r.period_end) ?? null,
    [rows, todayISO]);

  // What to call it. The banner says "This month" for a monthly programme and
  // "This quarter" for a quarterly one — calling a quarter "this month" would
  // be wrong in exactly the place the reader is trusting the label.
  const currentLabel = effectiveFreq === "quarterly" ? "This quarter"
    : effectiveFreq === "weekly" ? "This week"
    : effectiveFreq === "half_yearly" ? "This half-year"
    : effectiveFreq === "yearly" ? "This year" : "This month";

  // The next thing actually owed: earliest unsent period by due date. Rows
  // arrive newest-deadline-last, so a scan is enough — no sort needed. "Due
  // today" counts as upcoming: the deadline has not passed, so it is still the
  // next thing to send, not something already missed.
  const outstanding = rows.filter(r => !r.received_at && r.due_date);
  const nextDue = outstanding.find(
    r => r.status === "scheduled" || r.status === "due_soon" || r.status === "due_today") ?? null;
  const oldestLate = outstanding.find(r => r.status === "overdue") ?? null;

  // Which of the two contract-backed fields are currently overridden. Only these
  // two fall back to the contract (resolve_schedule: override -> contract ->
  // nothing); the two day-counts have no contract source and are always local.
  // What the contract ACTUALLY supplies. Not every contract sets both: C1 has an
  // inception date but no bdx_frequency, so claiming "both come from the
  // contract" would be false and would leave the reader wondering why Save is
  // dead. Each branch of the helper text below states only what is really there.
  const contractGives = [
    sched?.contract_frequency ? "How often it is due" : null,
    sched?.contract_anchor ? "the start date" : null,
  ].filter(Boolean) as string[];

  // What is still missing before deadlines can be worked out — drives the
  // disabled button's tooltip so it names the gap instead of listing both.
  const missing = [
    !freqOk ? "how often it is due" : null,
    !anchorOk ? "a start date" : null,
  ].filter(Boolean) as string[];

  const overridden = [
    form.frequency_override ? "how often it is due" : null,
    form.anchor_date_override ? "the start date" : null,
  ].filter(Boolean) as string[];

  // Of those overrides, the ones the contract COULD supply — i.e. where "clear it
  // to follow the contract again" is real advice. An override on a field the
  // contract leaves blank is the ONLY source there is: clearing it drops
  // freqOk/anchorOk to false, disables Save and builds no calendar, so offering
  // that route would walk the reader into a dead end.
  const revertable = [
    form.frequency_override && sched?.contract_frequency ? "how often it is due" : null,
    form.anchor_date_override && sched?.contract_anchor ? "the start date" : null,
  ].filter(Boolean) as string[];
  const stuck = overridden.filter(o => !revertable.includes(o));

  const heading = [carrierName, programName].filter(Boolean).join(" · ");
  // Until the fetch lands, `open` is null — treat that as whatever this instance
  // is going to settle on, so it doesn't flicker into the other state and back.
  const expanded = !collapsible || (open ?? !defaultCollapsed);

  // Counts of the period list — so they are governed by the same flag. Where the
  // caller wants the schedule editor only, an "Overdue: 10" pill would report on
  // a table that is not on the page. Labels come from STATUS_META so a pill and
  // the row it counts can never disagree about what a status is called.
  const summaryBadges = !showPeriods ? [] : SUMMARY.map(st => {
    const n = counts[st] ?? 0;
    if (!n) return null;
    return (
      <span key={st} className={`badge ${STATUS_META[st].cls}`}>
        <span className="d" />{STATUS_META[st].label}: {n}
      </span>
    );
  });

  return (
    // proto-embed drops `.proto`'s page-canvas background and 100vh floor; the
    // class itself is still needed for badge / field / btn / note styling.
    <div className="proto proto-embed">
      {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
      {msg && <div className="note ok" style={{ marginBottom: 14 }}>{msg}</div>}

      {/* Who this calendar is for, and — when collapsible — the toggle. Replaces
          the carrier/program dropdowns: on a setup screen both are already
          decided, and offering a choice here would let the section contradict
          the setup it belongs to. */}
      {(heading || rows.length > 0 || collapsible) && (
        <div
          onClick={collapsible ? () => setOpen(o => !(o ?? !defaultCollapsed)) : undefined}
          role={collapsible ? "button" : undefined}
          aria-expanded={collapsible ? expanded : undefined}
          style={{
            display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8,
            marginBottom: expanded ? 16 : 0,
            cursor: collapsible ? "pointer" : undefined,
            userSelect: collapsible ? "none" : undefined,
          }}
        >
          {collapsible && (expanded
            ? <ChevronDown size={14} style={{ flexShrink: 0 }} />
            : <ChevronRight size={14} style={{ flexShrink: 0 }} />)}
          {heading && (
            <span style={{ fontSize: 13, fontWeight: 600, marginRight: 4 }}>{heading}</span>
          )}
          {summaryBadges}
          {/* Collapsed and with nothing set up, the header has to carry the whole
              message on its own — otherwise the section reads as merely empty
              rather than as something waiting to be configured. */}
          {!loading && !expanded && rows.length === 0 && (
            <span className="muted" style={{ fontSize: 12.5 }}>
              No deadlines yet — click to set when this bordereau is due.
            </span>
          )}
        </div>
      )}

      {!expanded ? null : loading ? (
        <div className="note">Loading calendar…</div>
      ) : (
        <>
          {sched && !sched.resolved && (
            <div className="note warn" style={{ marginBottom: 14 }}>
              <AlertTriangle size={14} style={{ verticalAlign: "-2px", marginRight: 6 }} />
              {REASON_TEXT[sched.reason ?? "not_set"] ?? "Set the frequency and start date."}
            </div>
          )}
          {sched?.resolved && (
            <div className="note ok" style={{ marginBottom: 14 }}>
              <div style={{ fontWeight: 700, marginBottom: 8 }}>
                Deadlines are set for this program
              </div>
              <div style={{ display: "grid", gap: "6px 18px",
                gridTemplateColumns: "repeat(auto-fit,minmax(210px,1fr))", fontSize: 12.5 }}>
                <Fact label="How often it is due"
                  value={FREQ_LABEL[sched.effective_frequency ?? ""] ?? sched.effective_frequency}
                  source={freqFromContract ? "from the contract" : "set here, not the contract"} />
                <Fact label="Deadlines start from" value={fmtDate(sched.effective_anchor)}
                  source={anchorFromContract ? "from the contract" : "set here, not the contract"} />
                <Fact label="Bordereaux expected"
                  value={rows.length ? `${rows.length} files` : "none yet"}
                  source={firstPeriod && lastPeriod ? `covering ${firstPeriod} to ${lastPeriod}` : undefined} />
                <Fact label="Next one due"
                  value={nextDue ? fmtDate(nextDue.due_date) : "nothing upcoming"}
                  source={nextDue ? `for ${nextDue.period}` : undefined} />
                {oldestLate && (
                  <Fact label="Oldest still not sent" value={fmtDate(oldestLate.due_date)}
                    source={`was due for ${oldestLate.period}`} tone="crit" />
                )}
              </div>
            </div>
          )}

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))",
            gap: 14 }}>
            <div className="field">
              <label>How often is it due? <InfoTip text="Monthly means one bordereau for each month." /></label>
              <select value={form.frequency_override}
                onChange={e => setForm(f => ({ ...f, frequency_override: e.target.value }))}>
                {FREQ_OPTIONS.map(([v, l]) => (
                  // The blank option means "no override — inherit the contract".
                  // Naming the inherited value turns an empty-looking control into
                  // one that states what is actually in force.
                  <option key={v} value={v}>
                    {v === "" && sched?.contract_frequency
                      ? `— use contract (${FREQ_LABEL[sched.contract_frequency] ?? sched.contract_frequency}) —`
                      : l}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Starting from <InfoTip text="The first period is the one containing this date. Nothing is expected before it." /></label>
              {/* A native date input takes no placeholder — left empty it shows the
                  browser's dd/mm/yyyy mask, which reads as "nothing is set" when
                  in fact the contract's date is in force. So while the field
                  inherits, its own text is hidden and the inherited date is drawn
                  inside the control, worded like the Frequency option beside it so
                  the two inheriting fields read as a pair. With no contract date
                  there is nothing to stand in, and the browser's own mask is left
                  alone. The overlay is
                  pointer-events:none, so clicking the field still opens the picker. */}
              <div style={{ position: "relative" }}>
                <input type="date" value={form.anchor_date_override}
                  aria-describedby={inheritingAnchor ? "anchor-inherited" : undefined}
                  onChange={e => setForm(f => ({ ...f, anchor_date_override: e.target.value }))}
                  style={inheritingAnchor ? { color: "transparent" } : undefined} />
                {inheritingAnchor && (
                  <span id="anchor-inherited" style={{
                    position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)",
                    fontSize: 13.5, color: "var(--p-faint)",
                    pointerEvents: "none", whiteSpace: "nowrap",
                  }}>
                    — use contract ({fmtDate(sched!.contract_anchor)}) —
                  </span>
                )}
              </div>
            </div>
            {/* The deadline. For monthly and quarterly it is a DAY OF THE MONTH —
                the way a broker states it ("the bordereau is due on the 10th") —
                because those periods always end on a month's last day. Weekly
                periods end on arbitrary dates, several per month, so there is no
                day-of-month to name and they keep days-after-the-period. */}
            {isWeekly ? (
              <div className="field">
                <label>Days allowed after the week ends <InfoTip text="Weekly bordereaux are due this many days after each 7-day period ends." /></label>
                <input type="number" min={0} value={form.due_offset_days}
                  onChange={e => setForm(f => ({ ...f, due_offset_days: Number(e.target.value) }))} />
              </div>
            ) : (
              <div className="field">
                <label>Due on day of month <InfoTip text="The day of the following month the bordereau is due. Set it to 10 and the January bordereau is due on 10 February. Months too short for the day you pick use their last day instead." /></label>
                <input type="number" min={1} max={31} value={form.due_day_of_month}
                  onChange={e => setForm(f => ({
                    ...f,
                    // Clamped on the way in so the field can never hold a day
                    // that is not a day. The server clamps too — this is so the
                    // preview line below never reads "the 87th".
                    due_day_of_month: Math.max(1, Math.min(31, Number(e.target.value) || 1)),
                  }))} />
              </div>
            )}
            {/* There is no grace field. Removing it was the point: a deadline is
                either met or missed, and a second number quietly moving the date
                the user had just set is the opposite of clear. */}
            <div className="field">
              <label>Warn me this many days early <InfoTip text="You are reminded this far ahead, again on the due date, and again once it passes." /></label>
              <input type="number" min={0} value={form.soon_window_days}
                onChange={e => setForm(f => ({ ...f, soon_window_days: Number(e.target.value) }))} />
            </div>
          </div>

          {/* The whole rule in plain words, built from the numbers actually in the
              fields above, so the reader can check the setting against what will
              happen without saving to find out. */}
          <div style={{ fontSize: 12, color: "var(--p-muted)", margin: "10px 0 2px" }}>
            {!isWeekly && (
              <>
                Each bordereau is due on the <b>{ordinal(form.due_day_of_month)}</b> of
                the month after the period it covers.{" "}
              </>
            )}
            You get three reminders: <b>{form.soon_window_days} day
            {Number(form.soon_window_days) === 1 ? "" : "s"} before</b> the due date,
            again <b>on the day</b>, and once more <b>after it passes</b>. Each is
            sent once, not repeated. There is no grace period — one day past the due
            date counts as overdue.
          </div>
          <div style={{ fontSize: 12, color: "var(--p-muted)", margin: "2px 0 12px" }}>
            {/* Says which fields the contract actually drives, and — the part the
                old wording left out — that filling one STOPS it tracking the
                contract, since an override always wins over the contract value. */}
            {overridden.length === 0 ? (
              contractGives.length === 2 ? (
                <>
                  The first two come from the contract
                  {` (${FREQ_LABEL[sched!.contract_frequency!] ?? sched!.contract_frequency}, from ${fmtDate(sched!.contract_anchor)})`}
                  . Change one only if this program differs — it then stops following
                  the contract.
                </>
              ) : contractGives.length === 1 ? (
                <>
                  <b>{contractGives[0]}</b> comes from the contract
                  {sched?.contract_frequency
                    ? ` (${FREQ_LABEL[sched.contract_frequency] ?? sched.contract_frequency})`
                    : ` (${fmtDate(sched?.contract_anchor)})`}
                  . The contract does not say{" "}
                  <b>{sched?.contract_frequency ? "when it starts" : "how often it is due"}</b>,
                  so choose that here.
                </>
              ) : (
                <>
                  The contract does not say how often this bordereau is due or when it
                  starts, so choose both here.
                </>
              )
            ) : (
              <>
                Not following the contract for <b>{overridden.join(" and ")}</b>.{" "}
                {revertable.length > 0 && (
                  <>
                    Clear {revertable.length > 1 ? "those fields" : "that field"} to
                    follow the contract again.{" "}
                  </>
                )}
                {stuck.length > 0 && (
                  <>
                    The contract does not say <b>{stuck.join(" or ")}</b>, so{" "}
                    {stuck.length > 1 ? "those stay" : "that stays"} set here.
                  </>
                )}
              </>
            )}
          </div>
          <button className="btn pri" onClick={save} disabled={saving || !canSave}
            title={canSave ? "" : `Set ${missing.join(" and ")} first`}>
            {saving ? "Saving…" : "Save deadlines"}
          </button>

          {/* WHERE THE CURRENT PERIOD STANDS — answered before the table, because
              "is this month done?" is the question the page is opened with, and a
              long list sorted by date makes the reader hunt for the one row that
              answers it. Colour and wording come from the same STATUS_META the
              table uses, so the banner and the highlighted row always agree. */}
          {showPeriods && currentRow && (() => {
            const m = STATUS_META[currentRow.status]
              ?? { label: String(currentRow.status), cls: "b-mut" };
            const done = !!currentRow.received_at;
            return (
              <div className="card" style={{ marginTop: 18, padding: "12px 16px",
                display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10,
                borderLeft: `3px solid var(--p-${
                  currentRow.status === "overdue" ? "crit"
                    : currentRow.status === "due_today" ? "warn"
                    : done ? "ok" : "info"})` }}>
                <span style={{ fontWeight: 700, fontSize: 13 }}>
                  {currentLabel} · {currentRow.period}
                </span>
                <span className={`badge ${m.cls}`}><span className="d" />{m.label}</span>
                {/* Says something the badge does NOT already say. It used to read
                    "· not sent yet" beside a badge reading "Overdue" and a Sent
                    column reading "—" — the same fact three times. HOW LATE, or
                    how long there is left, is the part that was missing. */}
                <span className="muted" style={{ fontSize: 12.5 }}>
                  {(() => {
                    const due = fmtDate(currentRow.due_date);
                    if (done) {
                      const late = currentRow.due_date && currentRow.received_at
                        ? daysBetween(currentRow.due_date, currentRow.received_at) : 0;
                      return late > 0
                        ? `Sent ${fmtDate(currentRow.received_at)} · ${plural(late, "day")} late`
                        : `Sent ${fmtDate(currentRow.received_at)} · on time`;
                    }
                    if (!currentRow.due_date) return "No due date";
                    const d = daysBetween(todayISO, currentRow.due_date);
                    // No "today" here — the badge beside this already says it.
                    // The date is the part it doesn't carry.
                    if (d === 0) return `Due ${due}`;
                    return d > 0
                      ? `Due ${due} · ${plural(d, "day")} left`
                      : `Due ${due} · ${plural(-d, "day")} late`;
                  })()}
                </span>
              </div>
            );
          })()}

          {/* Calendar list — suppressed when the caller only wants the editor. */}
          {showPeriods && (rows.length === 0 ? (
            <div className="note" style={{ marginTop: 16 }}>
              No deadlines yet for this program — {readOnly
                ? "set them from Bordereau Setup."
                : "fill in the fields above and save."}
            </div>
          ) : (
            <div className="card" style={{ marginTop: 18 }}>
              {/* Header laid out on the SAME column grid as the table, so the Due
                  date filter sits over Due date, Status over Status, and Clear
                  over the right (Sent) column.

                  Each filter carries a VISIBLE caption. They used to be labelled
                  by aria-label alone, so a sighted reader saw two bare date boxes
                  with a dash between them and no way to tell what they filtered. */}
              <div className="card-h" style={{ display: "grid",
                gridTemplateColumns: "36% 26% 22% 16%", alignItems: "end", gap: 0 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 10, minWidth: 0 }}>
                  <h3 style={{ margin: 0 }}>{programName || "Deadlines"}</h3>
                  {/* Always says how many of the whole set is on screen when that
                      is not all of them — whether a filter or the folded backlog
                      is doing the narrowing. A bare "46 deadlines" over a table
                      showing six would be the screen contradicting itself. */}
                  <span className="sub">
                    {filtersActive || collapsing
                      ? `showing ${totalItems} of ${rows.length}`
                      : `${rows.length} deadlines`}
                  </span>
                </div>
                <div style={{ paddingLeft: 12 }}>
                  <div className="sub" style={{ marginBottom: 3 }}>Due between</div>
                  <div className="fbar-daterange">
                    <input type="date" className="fbar-date" aria-label="Due on or after"
                      value={dueFrom} onChange={e => setDueFrom(e.target.value)} />
                    <span className="sub">–</span>
                    <input type="date" className="fbar-date" aria-label="Due on or before"
                      value={dueTo} onChange={e => setDueTo(e.target.value)} />
                  </div>
                </div>
                <div style={{ paddingLeft: 12 }}>
                  <div className="sub" style={{ marginBottom: 3 }}>Status</div>
                  <select className="fbar-select" aria-label="Filter by status"
                    value={statusFilter}
                    onChange={e => setStatusFilter(e.target.value as CalendarStatus | "")}>
                    <option value="">Show all ({rows.length})</option>
                    {presentStatuses.map(st => (
                      <option key={st} value={st}>
                        {STATUS_META[st].label} ({counts[st] ?? 0})
                      </option>
                    ))}
                  </select>
                </div>
                <div style={{ textAlign: "right" }}>
                  {filtersActive && (
                    <span className="linkish" onClick={clearFilters}>Clear filters</span>
                  )}
                </div>
              </div>
              <div style={{ padding: "6px 8px" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13,
                  tableLayout: "fixed" }}>
                  <colgroup>
                    <col style={{ width: "36%" }} />
                    <col style={{ width: "26%" }} />
                    <col style={{ width: "22%" }} />
                    <col style={{ width: "16%" }} />
                  </colgroup>
                  <thead>
                    <tr style={{ color: "var(--p-muted)", textAlign: "left" }}>
                      <th style={{ padding: "8px 12px", fontWeight: 600 }}>Period</th>
                      <th style={{ padding: "8px 12px", fontWeight: 600 }}>Due date</th>
                      <th style={{ padding: "8px 12px", fontWeight: 600 }}>Status</th>
                      <th style={{ padding: "8px 12px", fontWeight: 600 }}>Sent</th>
                    </tr>
                  </thead>
                  <tbody>
                    {/* The folded backlog, as ONE row on the first page. It names
                        the range and the count instead of being a silent cap —
                        the reader can see exactly what is behind it before
                        deciding to open it. */}
                    {collapsing && backlog && page === 1 && (
                      <tr style={{ background: "var(--p-surface-2)" }}>
                        <td colSpan={4} style={{ padding: "10px 12px" }}>
                          <span className="muted" style={{ fontSize: 12.5 }}>
                            {plural(backlog.count, "earlier period")} ({backlog.from} – {backlog.to})
                            {backlog.overdue > 0 && <> · <b>{backlog.overdue} never sent</b></>}
                            {backlog.sent > 0 && <> · {backlog.sent} sent</>}
                          </span>{" "}
                          <span className="linkish" onClick={() => setShowBacklog(true)}>
                            Show all
                          </span>
                        </td>
                      </tr>
                    )}
                    {/* Once opened it stays open until the program changes, with
                        the way back stated in the same place it was opened. */}
                    {showBacklog && !filtersActive && focusStart > 0 && page === 1 && (
                      <tr style={{ background: "var(--p-surface-2)" }}>
                        <td colSpan={4} style={{ padding: "10px 12px" }}>
                          <span className="muted" style={{ fontSize: 12.5 }}>
                            Showing the full history.{" "}
                          </span>
                          <span className="linkish" onClick={() => setShowBacklog(false)}>
                            Hide older periods
                          </span>
                        </td>
                      </tr>
                    )}
                    {totalItems === 0 ? (
                      <tr>
                        <td colSpan={4} style={{ padding: "18px 12px", textAlign: "center",
                          color: "var(--p-muted)" }}>
                          Nothing matches these filters.
                        </td>
                      </tr>
                    ) : pageRows.map(r => {
                      // A status the UI doesn't know (an old 'late' row from a
                      // deployment that skipped the migration) still gets a
                      // readable cell rather than an empty badge.
                      const m = STATUS_META[r.status]
                        ?? { label: String(r.status), cls: "b-mut" };
                      // The period covering today, tinted and tagged so it is
                      // findable at a glance among a screenful of rows.
                      const isCurrent = currentRow?.id === r.id;
                      return (
                        <tr key={r.id} style={{
                          borderTop: "1px solid var(--p-border)",
                          background: isCurrent ? "var(--p-surface-3)" : undefined,
                        }}>
                          <td style={{ padding: "9px 12px", fontWeight: isCurrent ? 700 : 500,
                            // The accent sits on the first cell rather than the
                            // row: a border-left on <tr> is not painted with
                            // border-collapse, so it would simply not show.
                            boxShadow: isCurrent ? "inset 3px 0 0 var(--p-info)" : undefined }}>
                            {r.period}
                            {isCurrent && (
                              <span className="sub" style={{ marginLeft: 8, fontWeight: 600 }}>
                                {currentLabel}
                              </span>
                            )}
                          </td>
                          <td style={{ padding: "9px 12px" }}>{fmtDate(r.due_date)}</td>
                          <td style={{ padding: "9px 12px" }}>
                            <span className={`badge ${m.cls}`}><span className="d" />{m.label}</span>
                          </td>
                          <td style={{ padding: "9px 12px", color: "var(--p-muted)" }}>
                            {fmtDate(r.received_at)}
                            {/* A period can be filed more than once. Saying so
                                HERE matters: the date beside it is the first
                                submission, and without this the row would read
                                as if that were the only one. Silent on a period
                                filed once, which is almost all of them. */}
                            {r.version_count > 1 && (
                              <div className="sub" style={{ marginTop: 3 }}>
                                {r.version_label}
                                {r.latest_received_at
                                  && ` · latest ${fmtDate(r.latest_received_at)}`}
                              </div>
                            )}
                            {r.released_at && (
                              <div className="sub" style={{ marginTop: 3 }}>
                                Sent onward {fmtDate(r.released_at)}
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {pageCount > 1 && (
                <div style={{ padding: "10px 16px", borderTop: "1px solid var(--p-border)" }}>
                  <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
                    totalItems={totalItems} onPageChange={setPage} noun="deadlines" />
                </div>
              )}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
