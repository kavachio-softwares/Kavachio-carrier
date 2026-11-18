// MOPR 42 — the Periodic Report: one sidebar entry, three views of one period.
//   Overview              the pack (42.1): timeliness, volumes, data quality and
//                         fixes, beside the period before and last year (42.2)
//   Data Quality          the detail behind the volumes and errors (MOPR 40)
//   Exception Resolution  the detail behind the fixes, and the work list (MOPR 41)
// The period, programme and broker are picked once, above the tabs, and kept in
// the address, so switching tab or reloading keeps them. The period is the same
// switch as the Executive Report's — Daily, Weekly, Monthly or Custom dates
// (backend monthly_report.window); Monthly is the report as it always was, and
// its address (?month=) still opens it. Beside the tabs sit the report's own
// buttons (MonthlyReportActions): download, Send & Schedule, send now.
import { useCallback, useMemo, useState } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { BarChart3, LayoutDashboard, Timer } from "lucide-react";
import MonthlyOverview from "./MonthlyOverview";
import MonthlyReportActions, { type ActionNote } from "./MonthlyReportActions";
import DataQuality from "./DataQuality";
import ExceptionResolution from "./ExceptionResolution";
import { InfoTip } from "../components/InfoTip";
import {
  addDays, daysAgo, fmtDay, fmtMonth, fmtSpan, lastMonths, lastWeeks, mondayOf, periodRanges, periodRunning,
  periodTitle, type ReportCadence,
} from "../utils/reportFormat";

/** What every tab is showing — picked once, above the tabs. `period` is
 *  "YYYY-MM" for a month, the day, the week's Monday, or "from..to". */
export interface ReportFilters {
  cadence: ReportCadence;
  period: string;
  programId: number | null;
  brokerId: number | null;
}

/** The carrier's programmes and brokers, for the two pickers. Each report
 *  carries them, so whichever tab loads hands them up. */
export interface FilterLists {
  programmes: { id: number; name: string }[];
  brokers: { id: number; name: string }[];
}

export interface TabProps {
  filters: ReportFilters;
  onLists: (lists: FilterLists) => void;
  /** The current query string, to keep the filters on links between tabs. */
  qs: string;
}

const TABS = [
  { key: "", label: "Overview", icon: LayoutDashboard },
  { key: "data-quality", label: "Data Quality", icon: BarChart3 },
  { key: "exception-resolution", label: "Exception Resolution", icon: Timer },
];

const CADENCES: { key: ReportCadence; label: string }[] = [
  { key: "day", label: "Daily" }, { key: "week", label: "Weekly" },
  { key: "month", label: "Monthly" }, { key: "custom", label: "Custom" },
];

/** What the report is, behind the ⓘ beside its title. The first sentence is
 *  the tip's bold lead; the "Pick …" one is set apart as the action (tipText). */
const ABOUT = "How your brokers' files did: on time, clean, and how fast errors were fixed. "
  + "Each period is compared with the one before, and a month or week with last year too.";

/** "Covers 9 – 15 Nov 2026, compared with 2 – 8 Nov 2026 and 10 – 16 Nov 2025."
 *  — beside the picker, as the Executive Report says it. */
function covers(cadence: ReportCadence, period: string): string {
  if (cadence === "day") {
    // As the Executive Report says it for today; a past day names both days.
    return period === daysAgo(0)
      ? `Today so far, ${fmtDay(period)}, compared with yesterday.`
      : `Covers ${fmtDay(period)}, compared with the day before, ${fmtDay(addDays(period, -1))}.`;
  }
  const { current, previous, lastYear } = periodRanges(cadence, period);
  const span = (r: { from: string; to: string }) => fmtSpan(r.from, r.to);
  const vs = lastYear ? `${span(previous)} and ${span(lastYear)}` : span(previous);
  return `Covers ${span(current)}, compared with ${vs}.`
    + (periodRunning(cadence, period) ? " Still running, so the numbers count to today." : "");
}

const idOf = (v: string | null) => (v && Number(v) > 0 ? Number(v) : null);
const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** The period in the address, or the one each kind opens on: this month,
 *  last week, today, the last 30 days. */
function periodFrom(cadence: ReportCadence, sp: URLSearchParams, months: string[], weeks: string[]): string {
  const today = daysAgo(0);
  if (cadence === "month") {
    const asked = sp.get("month") ?? sp.get("period") ?? "";
    return months.includes(asked) ? asked : months[0];
  }
  const asked = sp.get("period") ?? "";
  if (cadence === "day") return DAY.test(asked) && asked <= today ? asked : today;
  if (cadence === "week") return DAY.test(asked) && asked <= today ? mondayOf(asked) : weeks[1];
  const [a, b] = asked.split("..");
  return a && b && DAY.test(a) && DAY.test(b) ? `${a}..${b}` : `${daysAgo(30)}..${daysAgo(1)}`;
}

export default function MonthlyReport() {
  const { tab = "" } = useParams();
  const [sp, setSp] = useSearchParams();
  const months = useMemo(() => lastMonths(), []);
  const weeks = useMemo(() => lastWeeks(), []);
  const askedCadence = sp.get("cadence");
  const cadence: ReportCadence = CADENCES.some(c => c.key === askedCadence)
    ? askedCadence as ReportCadence : "month";
  const period = periodFrom(cadence, sp, months, weeks);
  const programId = idOf(sp.get("programme"));
  const brokerId = idOf(sp.get("broker"));
  const filters = useMemo(() => ({ cadence, period, programId, brokerId }),
                          [cadence, period, programId, brokerId]);
  const [lists, setLists] = useState<FilterLists>({ programmes: [], brokers: [] });
  const onLists = useCallback(
    (l: FilterLists) => setLists({ programmes: l.programmes, brokers: l.brokers }), []);
  const [note, setNote] = useState<ActionNote>(null);
  const qs = sp.toString() ? `?${sp.toString()}` : "";

  if (!TABS.some(t => t.key === tab)) return <Navigate to={`/monthly-report${qs}`} replace />;

  function pick(key: string, value: string) {
    const next = new URLSearchParams(sp);
    if (value) next.set(key, value); else next.delete(key);
    setSp(next, { replace: true });
  }

  /** Switch Daily / Weekly / Monthly / Custom. Monthly keeps the address it
   *  always had (?month=); the others carry ?cadence= and ?period=. */
  function pickCadence(c: ReportCadence) {
    const next = new URLSearchParams(sp);
    next.delete("cadence"); next.delete("period"); next.delete("month");
    if (c !== "month") {
      next.set("cadence", c);
      next.set("period", c === "day" ? daysAgo(0) : c === "week" ? weeks[1] : `${daysAgo(30)}..${daysAgo(1)}`);
    }
    setNote(null);
    setSp(next, { replace: true });
  }

  // Custom: the two dates, and whether they make a period at all.
  const [from = "", to = ""] = cadence === "custom" ? period.split("..") : [];
  const customOk = cadence !== "custom" || (!!from && !!to && from <= to && to <= daysAgo(0));
  // A week picked from a link may be older than the list: keep it choosable.
  const weekOptions = cadence === "week" && !weeks.includes(period) ? [period, ...weeks] : weeks;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head" style={{ marginBottom: 14 }}>
          <div className="t">
            <h2>Periodic Report
              <InfoTip text={ABOUT} />
            </h2>
          </div>
        </div>

        {/* The three views as one switch: a white bar that stands out on the
            grey page, the open tab in the brand teal. The report's buttons sit
            to its right, so they show on every tab. */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "12px 16px", marginBottom: 14 }}>
          <nav aria-label="Periodic Report views"
            className="flex min-w-0 flex-[1_1_520px] flex-wrap gap-1 rounded-[10px] border bg-white p-1"
            style={{ borderColor: "var(--p-border)", boxShadow: "var(--p-sh-sm)" }}>
            {TABS.map(({ key, label, icon: Icon }, i) => {
              const on = key === tab;
              // A thin line between two tabs, left out beside the open one.
              const line = i > 0 && !on && TABS[i - 1].key !== tab;
              return (
                <Link key={key} to={`/monthly-report${key ? `/${key}` : ""}${qs}`}
                  aria-current={on ? "page" : undefined}
                  className={`relative inline-flex h-9 min-w-[160px] flex-1 items-center justify-center gap-2 whitespace-nowrap rounded-lg px-6 text-[13.5px] no-underline transition-colors
                    ${line ? "before:absolute before:-left-[3px] before:top-1/4 before:h-1/2 before:w-px before:bg-[var(--p-border-2)]" : ""}
                    ${on ? "font-semibold" : "font-medium text-ink-muted hover:bg-surface-2 hover:text-ink"}`}
                  style={on ? { background: "var(--p-primary-soft)", color: "var(--p-primary)" } : undefined}>
                  <Icon size={16} strokeWidth={1.9} aria-hidden="true" />
                  {label}
                </Link>
              );
            })}
          </nav>
          {customOk && <MonthlyReportActions filters={filters} onNote={setNote} />}
        </div>

        {/* Below the tabs, the period in a white bar as on the Executive
            Report: which kind, which one, and what it covers and is compared
            with — and, on the right, the programme and broker it is narrowed to. */}
        <div className="card" style={{ overflow: "visible", marginBottom: 22 }}>
          <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none", alignItems: "flex-end" }}>
            <label className="fbar-field">
              <span className="sub">Report</span>
              <span className="seg">
                {CADENCES.map(c => (
                  <button key={c.key} type="button" className={cadence === c.key ? "on" : ""}
                    aria-pressed={cadence === c.key} onClick={() => pickCadence(c.key)}>{c.label}</button>
                ))}
              </span>
            </label>
            {cadence === "month" && (
              <label className="fbar-field">
                <span className="sub">Month</span>
                <select className="fbar-select" value={period} onChange={e => pick("month", e.target.value)}>
                  {months.map(m => <option key={m} value={m}>{fmtMonth(m)}</option>)}
                </select>
              </label>
            )}
            {cadence === "week" && (
              <label className="fbar-field">
                <span className="sub">Week</span>
                <select className="fbar-select" value={period} onChange={e => pick("period", e.target.value)}>
                  {weekOptions.map(w => (
                    <option key={w} value={w}>
                      {periodTitle("week", w)}{w === weeks[0] ? " (this week, so far)" : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {cadence === "day" && (
              <label className="fbar-field">
                <span className="sub">Day</span>
                <input type="date" className="fbar-date" value={period} max={daysAgo(0)}
                  onChange={e => { if (e.target.value) pick("period", e.target.value); }} />
              </label>
            )}
            {cadence === "custom" && (<>
              <label className="fbar-field">
                <span className="sub">From</span>
                <input type="date" className="fbar-date" value={from} max={to || daysAgo(0)}
                  onChange={e => pick("period", `${e.target.value}..${to}`)} />
              </label>
              <label className="fbar-field">
                <span className="sub">To</span>
                <input type="date" className="fbar-date" value={to} min={from || undefined} max={daysAgo(0)}
                  onChange={e => pick("period", `${from}..${e.target.value}`)} />
              </label>
            </>)}
            {customOk && (
              <span className="sub" style={{ flex: "1 1 260px", minWidth: 0, paddingBottom: 8 }}>
                {covers(cadence, period)}
              </span>
            )}
            <div style={{ display: "flex", gap: 14, alignItems: "flex-end", flexWrap: "wrap", marginLeft: "auto" }}>
              <label className="fbar-field">
                <span className="sub">Programme</span>
                <select className="fbar-select" value={programId ?? ""}
                  onChange={e => pick("programme", e.target.value)}>
                  <option value="">All programmes</option>
                  {lists.programmes.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </label>
              <label className="fbar-field">
                <span className="sub">Broker</span>
                <select className="fbar-select" value={brokerId ?? ""}
                  onChange={e => pick("broker", e.target.value)}>
                  <option value="">All brokers</option>
                  {lists.brokers.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
                </select>
              </label>
            </div>
          </div>
        </div>
        {note && <div className={`note ${note.tone}`} style={{ marginBottom: 16 }}>{note.text}</div>}

        {!customOk ? (
          <div className="card"><div className="empty">
            Pick a from date on or before the to date, and no later than today.
          </div></div>
        ) : (<>
          {tab === "" && <MonthlyOverview filters={filters} onLists={onLists} qs={qs} />}
          {tab === "data-quality" && <DataQuality filters={filters} onLists={onLists} qs={qs} />}
          {tab === "exception-resolution" && <ExceptionResolution filters={filters} onLists={onLists} qs={qs} />}
        </>)}
      </div>
    </div>
  );
}
