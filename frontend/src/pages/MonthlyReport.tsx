// MOPR 42 — the Monthly Report: one sidebar entry, three views of one month.
//   Overview              the pack (42.1): timeliness, volumes, data quality and
//                         fixes, beside last month and last year (42.2)
//   Data Quality          the detail behind the volumes and errors (MOPR 40)
//   Exception Resolution  the detail behind the fixes, and the work list (MOPR 41)
// The month, programme and broker are picked once, above the tabs, and kept in
// the address, so switching tab or reloading keeps them. Beside the tabs sit the
// report's own buttons (MonthlyReportActions): download, Send & Schedule, send now.
import { useCallback, useMemo, useState } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { BarChart3, LayoutDashboard, Timer } from "lucide-react";
import MonthlyOverview from "./MonthlyOverview";
import MonthlyReportActions, { type ActionNote } from "./MonthlyReportActions";
import DataQuality from "./DataQuality";
import ExceptionResolution from "./ExceptionResolution";
import { fmtMonth, lastMonths } from "../utils/reportFormat";

/** What every tab is showing — picked once, above the tabs. */
export interface ReportFilters {
  month: string;
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

const idOf = (v: string | null) => (v && Number(v) > 0 ? Number(v) : null);

export default function MonthlyReport() {
  const { tab = "" } = useParams();
  const [sp, setSp] = useSearchParams();
  const months = useMemo(() => lastMonths(), []);
  const asked = sp.get("month") ?? "";
  const month = months.includes(asked) ? asked : months[0];
  const programId = idOf(sp.get("programme"));
  const brokerId = idOf(sp.get("broker"));
  const filters = useMemo(() => ({ month, programId, brokerId }), [month, programId, brokerId]);
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

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head" style={{ flexWrap: "wrap", marginBottom: 14 }}>
          <div className="t" style={{ flex: "1 1 520px", minWidth: 0 }}>
            <h2>Monthly Report</h2>
            <p>Timeliness, volumes, data quality and fixes for one month — compared with last month and the same month last year.</p>
          </div>
          <div className="actions" style={{ alignItems: "flex-end" }}>
            <label className="fbar-field">
              <span className="sub">Month</span>
              <select className="fbar-select" value={month} onChange={e => pick("month", e.target.value)}>
                {months.map(m => <option key={m} value={m}>{fmtMonth(m)}</option>)}
              </select>
            </label>
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

        {/* The three views as one switch: a white bar that stands out on the
            grey page, the open tab in the brand teal. The report's buttons sit
            to its right, so they show on every tab. */}
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "12px 16px", marginBottom: 22 }}>
          <nav aria-label="Monthly Report views"
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
          <MonthlyReportActions filters={filters} onNote={setNote} />
        </div>
        {note && <div className={`note ${note.tone}`} style={{ marginBottom: 16 }}>{note.text}</div>}

        {tab === "" && <MonthlyOverview filters={filters} onLists={onLists} qs={qs} />}
        {tab === "data-quality" && <DataQuality filters={filters} onLists={onLists} qs={qs} />}
        {tab === "exception-resolution" && <ExceptionResolution filters={filters} onLists={onLists} qs={qs} />}
      </div>
    </div>
  );
}
