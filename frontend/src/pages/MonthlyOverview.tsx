// MOPR 42 — the Periodic Report's Overview tab: the pack (42.1) for one period,
// beside the period before and — a month or a week — the same one last year
// (42.2). Every number is counted the same way as on the detail tabs (backend
// monthly_report.py); a period before the records began says "Not available"
// rather than showing zeros. The split by broker and programme lives on the
// Exception Resolution tab; it is also in the PDF /
// Excel. A day, a week or dates picked are measured over their own dates
// (monthly_report.pack_for); a month reads exactly as it always did.
//
// 38.1: every number opens what is behind it — the policies (api/policies
// drillLink, counted from the same files) for volumes and errors, the
// Bordereau Calendar for timeliness, Exception Resolution for fixes.
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { FileText, LineChart } from "lucide-react";
import { getMonthlyReport, type MonthMeasures, type MonthlyReport } from "../api/reports";
import { drillLink } from "../api/policies";
import { Delta, type Better, type ChangeKind } from "../components/Delta";
import { InfoTip } from "../components/InfoTip";
import {
  fmtMonthName, fmtTook, inPeriod, lastMonths, num, pct, PERIOD_WORD, periodTitle, plural,
  type ReportCadence, upFirst, VS_WORDS,
} from "../utils/reportFormat";
import type { TabProps } from "./MonthlyReport";

/** One measure: the number compared, and what its cell says. */
interface Measure {
  label: string;
  tip?: string;
  /** The number behind the comparison; null for a measure that is words. */
  value: (m: MonthMeasures) => number | null;
  show: (m: MonthMeasures) => string;
  kind: ChangeKind;
  better: Better;
  /** What the number opens: the policies behind it (a drill_down measure),
   *  the Bordereau Calendar or Exception Resolution. None for words. */
  opens?: { policies: string } | "calendar" | "fixes";
}

/** Where a measure's number for one period leads, or null. */
type Opens = (measure: Measure, key: string) => string | null;

const cadenceOf = (r: MonthlyReport): ReportCadence => r.cadence ?? "month";

/** One of the pack's periods in words: "October 2026", "9 – 15 Nov 2026"… */
const titleOf = (r: MonthlyReport, key: string) => periodTitle(cadenceOf(r), key);

/** What the period before is called mid-sentence: "September", "last week". */
const beforeOf = (r: MonthlyReport) =>
  cadenceOf(r) === "month" ? fmtMonthName(r.last_month) : VS_WORDS[cadenceOf(r)];

function opener(r: MonthlyReport, programId: number | null, brokerId: number | null): Opens {
  const shown = new Set(lastMonths());
  const cadence = cadenceOf(r);
  const scope = new URLSearchParams();
  if (programId != null) scope.set("programme", String(programId));
  if (brokerId != null) scope.set("broker", String(brokerId));
  return (measure, key) => {
    const o = measure.opens;
    if (!o) return null;
    const span = r.spans?.[key];
    if (cadence !== "month" && !span) return null;
    if (typeof o === "object") {
      // A month by its key; anything else by its dates, both days in.
      const month = cadence === "month" ? key : `${span!.from}..${span!.to}`;
      return drillLink({ from: "monthly", label: measure.label, month, measure: o.policies },
                       { program: programId, broker: brokerId });
    }
    // The Bordereau Calendar is by the month files are due in.
    if (o === "calendar") return `/bordereau-calendar?month=${cadence === "month" ? key : span!.from.slice(0, 7)}`;
    const q = new URLSearchParams(scope);
    if (cadence === "month") {
      // The report's month picker holds the last twelve months only.
      if (!shown.has(key)) return null;
      q.set("month", key);
    } else {
      q.set("cadence", cadence);
      q.set("period", key);
    }
    return `/monthly-report/exception-resolution?${q.toString()}`;
  };
}

/** A number that opens what is behind it. */
function Opened({ to, children }: { to: string | null; children: ReactNode }) {
  if (!to) return <>{children}</>;
  return <Link to={to} className="linkish" style={{ color: "inherit" }}
               title="See what is behind this number">{children}</Link>;
}

const ofPct = (n: number, of: number, p: number | null) =>
  of > 0 ? `${num(n)} of ${num(of)} (${pct(p)})` : "—";

function measures(r: MonthlyReport) {
  // Files with no policy number column are counted by row instead.
  const unit = r.months[r.month]?.volumes.counted_by === "rows" ? " (rows)" : " policies";
  const cadence = cadenceOf(r);
  const what = PERIOD_WORD[cadence];
  const monthly = cadence === "month";
  // "at month end" for a month; "at the end of the week" and so on otherwise.
  const end = monthly ? "month end" : `the end of the ${what}`;
  const timeliness: Measure[] = [
    { label: "Files due", value: m => m.timeliness.due, show: m => num(m.timeliness.due),
      kind: "count", better: "none", opens: "calendar",
      tip: `Files whose due date falls in the ${what}. A programme with no broker owes nothing and is left out.` },
    { label: "Arrived on time", value: m => m.timeliness.on_time_pct,
      show: m => ofPct(m.timeliness.on_time, m.timeliness.due_so_far, m.timeliness.on_time_pct),
      kind: "points", better: "up", opens: "calendar",
      tip: "Of the files already due, how many came on or before their due date." },
    { label: "Arrived late", value: m => m.timeliness.late, show: m => num(m.timeliness.late),
      kind: "count", better: "down", opens: "calendar" },
    { label: "Overdue", value: m => m.timeliness.overdue, show: m => num(m.timeliness.overdue),
      kind: "count", better: "down", opens: "calendar", tip: "Past the due date, nothing yet, and still expected." },
    { label: "Never arrived", value: m => m.timeliness.never, show: m => num(m.timeliness.never),
      kind: "count", better: "down", opens: "calendar", tip: "The next period began and this one's file never came." },
    { label: "Average days late", value: m => m.timeliness.avg_days_late,
      show: m => fmtTook(m.timeliness.avg_days_late), kind: "time", better: "down", opens: "calendar",
      tip: "For the files that arrived late: how many days after their due date, on average." },
    { label: "Late-file cases still open", value: m => m.timeliness.cases_open,
      show: m => num(m.timeliness.cases_open), kind: "count", better: "down", opens: "calendar",
      tip: `Files due this ${what} whose late-file case is not settled yet — Overdue, Delinquent, In dispute or Extended. As it stands today.` },
  ];
  const volumes: Measure[] = [
    { label: "Files handled", value: m => m.volumes.files, show: m => num(m.volumes.files),
      kind: "share", better: "none", opens: { policies: "files" },
      tip: `Files checked in the ${what}, each counted once.` },
    { label: "Rows handled", value: m => m.volumes.rows, show: m => num(m.volumes.rows),
      kind: "share", better: "none", opens: { policies: "rows" } },
    { label: `New${unit}`, value: m => m.volumes.new, show: m => num(m.volumes.new),
      kind: "share", better: "none", opens: { policies: "new" } },
    { label: `Renewed${unit}`, value: m => m.volumes.renewed, show: m => num(m.volumes.renewed),
      kind: "share", better: "none", opens: { policies: "renewed" } },
    { label: `Cancelled${unit}`, value: m => m.volumes.cancelled, show: m => num(m.volumes.cancelled),
      kind: "share", better: "none", opens: { policies: "cancelled" } },
  ];
  const quality: Measure[] = [
    { label: "Errors found", value: m => m.quality.errors, show: m => num(m.quality.errors),
      kind: "share", better: "down", opens: { policies: "errors" },
      tip: "Every error the checks found, as each file first arrived." },
    { label: "Critical errors", value: m => m.quality.critical, show: m => num(m.quality.critical),
      kind: "share", better: "down", opens: { policies: "critical" } },
    { label: "Rows with errors", value: m => m.quality.rows_with_errors_pct,
      show: m => pct(m.quality.rows_with_errors_pct), kind: "points", better: "down",
      opens: { policies: "errors" } },
    { label: "Files clean first time", value: m => m.quality.clean_first_time_pct,
      show: m => ofPct(m.quality.clean_first_time, m.volumes.files, m.quality.clean_first_time_pct),
      kind: "points", better: "up", opens: { policies: "clean" },
      tip: "Files that passed every check when they arrived, so nobody had to fix, re-send or release them." },
    { label: "Most common problem", value: () => null, show: m => m.quality.top_check ?? "—",
      kind: "count", better: "none" },
  ];
  const fixes: Measure[] = [
    { label: "Problems fixed", value: m => m.fixes.fixed, show: m => num(m.fixes.fixed),
      kind: "share", better: "none", opens: "fixes", tip: `Problems fixed in the ${what}, whenever they were found.` },
    { label: "Average fix time", value: m => m.fixes.avg_days, show: m => fmtTook(m.fixes.avg_days),
      kind: "time", better: "down", opens: "fixes" },
    { label: `Fixed within target (${plural(r.target_days, "day")})`,
      value: m => m.fixes.within_target_pct,
      show: m => ofPct(m.fixes.within_target, m.fixes.fixed, m.fixes.within_target_pct),
      kind: "points", better: "up", opens: "fixes" },
    { label: `Waiting to be fixed at ${end}`, value: m => m.fixes.waiting,
      show: m => num(m.fixes.waiting), kind: "share", better: "down", opens: "fixes",
      tip: monthly
        ? "Problems found but not fixed by the month's last day. For the month still running, as of today."
        : `Problems found but not fixed by the end of the ${what}. For a ${what} still running, as of today.` },
    { label: `Waiting longer than target at ${end}`, value: m => m.fixes.waiting_past_target,
      show: m => num(m.fixes.waiting_past_target), kind: "count", better: "down", opens: "fixes",
      tip: monthly
        ? "Of those, the ones already older than the target on the month's last day."
        : `Of those, the ones already older than the target at the end of the ${what}.` },
  ];
  return { timeliness, volumes, quality, fixes };
}

/** "October 2026 at a glance" — the period in a few sentences. */
function glance(r: MonthlyReport, filtered: boolean): string {
  const m = r.months[r.month], p = r.months[r.last_month];
  const cadence = cadenceOf(r);
  const before = beforeOf(r);
  const when = inPeriod(cadence, titleOf(r, r.month));
  const sel = filtered ? " for this selection" : "";
  // Before the carrier's records began (backend monthly_report.records_began):
  // nothing to show, and the report says from when it counts.
  if (!m?.has_data) {
    const since = r.records_began ? periodTitle("day", r.records_began) : "when the records began";
    return `There are no records ${when}${sel}: this report counts from ${since}.`;
  }
  const t = m.timeliness, q = m.quality, f = m.fixes;
  const parts: string[] = [];
  if (t.due_so_far > 0) {
    const was = p?.has_data && p.timeliness.on_time_pct != null
      ? `, ${before}: ${pct(p.timeliness.on_time_pct)}` : "";
    parts.push(`${num(t.on_time)} of ${plural(t.due_so_far, "file")} arrived on time (${pct(t.on_time_pct)}${was}).`);
  } else if (cadence !== "month" && t.due === 0) {
    parts.push(`No files were due ${when}.`);
  }
  if (m.volumes.files > 0) {
    if (q.errors === 0) {
      parts.push("No errors were found.");
    } else {
      const found = `${plural(q.errors, "error")} ${q.errors === 1 ? "was" : "were"} found`;
      const then = p?.has_data ? p.quality.errors : 0;
      const ch = then > 0 ? Math.round((100 * (q.errors - then)) / then) : null;
      parts.push(ch == null ? `${found}.`
        : ch === 0 ? `${found}, the same as ${before}.`
        : `${found} — ${Math.abs(ch)}% ${ch < 0 ? "fewer" : "more"} than ${before}.`);
    }
  }
  if (f.fixed > 0) {
    const within = f.within_target_pct === 100 ? "all"
      : `${pct(f.within_target_pct)}`;
    parts.push(`Problems were fixed in ${fmtTook(f.avg_days)} on average, ${within} within the ${r.target_days}-day target.`);
  }
  if (t.never > 0) parts.push(`${plural(t.never, "file")} never arrived.`);
  // Records ran, but nothing happened: a real nothing, said plainly.
  if (!parts.length) parts.push(`Nothing was due, checked or fixed ${when}${sel}.`);
  if (!m.complete) parts.push(`The ${PERIOD_WORD[cadence]} is still running, so these numbers count to today.`);
  return parts.join(" ");
}

/** A period's cell: its number, or "Not available" before the records began. */
function Cell({ m, measure, strong, to }: {
  m: MonthMeasures | undefined; measure: Measure; strong?: boolean; to?: string | null;
}) {
  if (!m?.has_data) return <span className="muted">Not available</span>;
  return <span style={strong ? { fontWeight: 600 } : { color: "var(--p-muted)" }}>
    <Opened to={to ?? null}>{measure.show(m)}</Opened>
  </span>;
}

function Change({ now, then, measure }: { now?: MonthMeasures; then?: MonthMeasures; measure: Measure }) {
  if (!now?.has_data || !then?.has_data || measure.value(now) == null) return <span className="muted">—</span>;
  return <Delta now={measure.value(now)} then={measure.value(then)} kind={measure.kind} better={measure.better} />;
}

export default function MonthlyOverview({ filters, onLists, qs }: TabProps) {
  const { cadence, period, programId, brokerId } = filters;
  const [data, setData] = useState<MonthlyReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true); setErr(null);
    getMonthlyReport({ cadence, period, program_id: programId, broker_party_id: brokerId })
      .then(d => { if (live) { setData(d); onLists(d); } })
      .catch(e => { if (live) setErr(e?.response?.data?.detail ?? "Could not load the report."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [cadence, period, programId, brokerId, onLists]);

  // The numbers on screen belong to the period asked for, or to nothing yet.
  const r = data && (data.cadence ?? "month") === cadence && (data.period ?? data.month) === period ? data : null;
  const filtered = programId != null || brokerId != null;
  const m = r?.months[r.month], prev = r?.months[r.last_month];
  const ly = r && r.last_year ? r.months[r.last_year] : undefined;
  const ms = r ? measures(r) : null;
  const opens = r ? opener(r, programId, brokerId) : () => null;
  const what = PERIOD_WORD[cadence];
  // Nothing was due in a day, a week or the dates picked: say so, not "0 of 0".
  const noneDue = cadence !== "month" && !!m?.has_data && m.timeliness.due === 0;
  // The Bordereau Calendar opens on the month a period starts in.
  const calendarMonth = r ? (cadence === "month" ? r.month : (r.spans?.[r.month]?.from ?? r.month).slice(0, 7)) : "";

  // The four headline numbers, each beside the period before and last year.
  const cards = r && ms ? [
    { title: "Files on time", measure: ms.timeliness[1],
      big: (x: MonthMeasures) => pct(x.timeliness.on_time_pct),
      small: (x: MonthMeasures) => (cadence !== "month" && x.timeliness.due_so_far === 0
        ? "No files were due"
        : `${num(x.timeliness.on_time)} of ${plural(x.timeliness.due_so_far, "file")}`) },
    { title: "Files clean first time", measure: ms.quality[3],
      big: (x: MonthMeasures) => pct(x.quality.clean_first_time_pct),
      small: (x: MonthMeasures) => `${num(x.quality.clean_first_time)} of ${plural(x.volumes.files, "file")}` },
    { title: "Errors found", measure: ms.quality[0],
      big: (x: MonthMeasures) => num(x.quality.errors),
      small: (x: MonthMeasures) => `${num(x.quality.critical)} critical` },
    { title: "Average fix time", measure: ms.fixes[1],
      big: (x: MonthMeasures) => fmtTook(x.fixes.avg_days),
      small: () => `target ${plural(r.target_days, "day")}` },
  ] : [];

  const groups = r && ms ? [
    { title: "Timeliness", rows: ms.timeliness,
      to: `/bordereau-calendar?month=${calendarMonth}`, link: "Open Bordereau Calendar",
      note: noneDue ? `No files were due in this ${what}` : null },
    { title: "Volumes", rows: ms.volumes,
      to: `/monthly-report/data-quality${qs}`, link: "Open Data Quality", note: null },
    { title: "Data quality", rows: ms.quality,
      to: `/monthly-report/data-quality${qs}`, link: "Open Data Quality", note: null },
    { title: "Fixes", rows: ms.fixes,
      to: `/monthly-report/exception-resolution${qs}`, link: "Open Exception Resolution", note: null },
  ] : [];
  const columns = r?.last_year ? 6 : 4;

  return (
    <>
      {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

      {/* ---- the period in a few sentences ------------------------------ */}
      <div style={{ display: "flex", gap: 14, alignItems: "flex-start", marginBottom: 18,
                    background: "var(--p-primary-soft)", border: "1px solid #C7E3E7",
                    borderRadius: "var(--p-r)", padding: "16px 20px" }}>
        <span style={{ flex: "none", width: 36, height: 36, borderRadius: 18, background: "var(--p-surface)",
                       display: "inline-flex", alignItems: "center", justifyContent: "center",
                       color: "var(--p-primary)" }}>
          <FileText size={18} aria-hidden="true" />
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ fontSize: 12, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase",
                        color: "var(--p-info-ink)" }}>
            {periodTitle(cadence, period)} at a glance
          </div>
          <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.55, color: "var(--p-ink)" }}>
            {loading || !r ? "Loading…" : upFirst(glance(r, filtered))}
          </p>
        </div>
      </div>

      {/* ---- four headline cards, each compared ------------------------- */}
      <div className="tiles" style={{ marginBottom: 18 }}>
        {cards.map(c => (
          <div className="tile" key={c.title}>
            <div className="k">{c.title}</div>
            <div className="v">
              {m?.has_data ? <Opened to={opens(c.measure, r!.month)}>{c.big(m)}</Opened> : "—"}{" "}
              {m?.has_data && <small>{c.small(m)}</small>}
            </div>
            <div className="foot" style={{ display: "flex", flexDirection: "column", gap: 6,
                                           paddingTop: 10, marginTop: 12, borderTop: "1px solid var(--p-border)" }}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                <Change now={m} then={prev} measure={c.measure} />
                vs {beforeOf(r!)}{prev?.has_data ? ` (${c.big(prev)})` : ""}
              </span>
              {r!.last_year && (
                <span>
                  {titleOf(r!, r!.last_year)}:{" "}
                  <b style={{ color: "var(--p-ink)" }}>{ly?.has_data ? c.big(ly) : "Not available"}</b>
                </span>
              )}
            </div>
          </div>
        ))}
        {!r && [0, 1, 2, 3].map(i => (
          <div className="tile" key={i}><div className="k"> </div><div className="v">…</div></div>
        ))}
      </div>

      {/* ---- key numbers: the pack, in four groups ---------------------- */}
      {r && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h" style={{ flexWrap: "wrap" }}>
            <LineChart className="ci" />
            <h3>Key numbers</h3>
            <div className="right" style={{ flexWrap: "wrap" }}>
              <span className="sub" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span className="badge b-ok">Green</span> better
              </span>
              <span className="sub" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span className="badge b-crit">Red</span> worse
              </span>
              <span className="sub" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span className="badge b-mut">Grey</span> more or less work
              </span>
            </div>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="l">Measure</th>
                  <th className="r">{titleOf(r, r.month)}</th>
                  <th className="r">{titleOf(r, r.last_month)}</th>
                  <th className="r">vs {VS_WORDS[cadence]}</th>
                  {r.last_year && <>
                    <th className="r">{titleOf(r, r.last_year)}</th>
                    <th className="r">vs last year</th>
                  </>}
                </tr>
              </thead>
              <tbody>
                {groups.map(g => [
                  <tr key={g.title}>
                    <td colSpan={columns} style={{ background: "var(--p-surface-2)" }}>
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                                    gap: 8, flexWrap: "wrap" }}>
                        <span style={{ display: "inline-flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                          <b>{g.title}</b>
                          {g.note && <span className="sub">{g.note}</span>}
                        </span>
                        <Link to={g.to} style={{ fontSize: 13, fontWeight: 600 }}>{g.link} →</Link>
                      </div>
                    </td>
                  </tr>,
                  ...g.rows.map(row => (
                    <tr key={`${g.title}:${row.label}`}>
                      <td className="l">
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                          {row.label}{row.tip && <InfoTip text={row.tip} />}
                        </span>
                      </td>
                      <td className="r"><Cell m={m} measure={row} strong to={opens(row, r.month)} /></td>
                      <td className="r"><Cell m={prev} measure={row} to={opens(row, r.last_month)} /></td>
                      <td className="r"><Change now={m} then={prev} measure={row} /></td>
                      {r.last_year && <>
                        <td className="r"><Cell m={ly} measure={row} to={opens(row, r.last_year)} /></td>
                        <td className="r"><Change now={m} then={ly} measure={row} /></td>
                      </>}
                    </tr>
                  )),
                ])}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
