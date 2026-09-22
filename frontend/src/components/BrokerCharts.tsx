/**
 * The two chart shapes both broker dashboards use.
 *
 * WHY HORIZONTAL, RANKED AND CAPPED. A broker's team and its carriers are
 * unbounded lists — a broker with forty users is ordinary. A vertical column
 * per person collides with its neighbours' names past about seven of them, and
 * a pie needs one colour per slice, which stops being tellable apart at six.
 * Both break silently: the chart still draws, it just cannot be read.
 *
 * So ranked horizontal bars: a long name has a whole row to sit on, the order
 * carries the answer ("who is doing the most"), only the top few are drawn,
 * and everything else is one line away in a table that holds any number of
 * rows. Growing the data makes the list longer, never the chart messier.
 *
 * COLOUR. Bars for people or carriers are all ONE hue: the category is written
 * on the axis, so spending colour on it would encode the same fact twice and
 * leave nothing for the parts that do carry meaning. Colour is reserved for
 * run outcomes, where it means something — clean, flagged, not yet checked.
 * The two-series pairs used here were checked for colour-blind separation
 * rather than eyeballed.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

/** Brand teal, darkened until it holds its own as a fill — the sidebar teal
 *  is too grey at chart size to read as a colour at all. */
const HUE = "#0B8FA0";
const CLEAN = "#0E9F6E";
const FLAGGED = "#C77A12";
/** Not an outcome, so not a colour: a file nobody has checked yet. */
const NOT_CHECKED = "#C3C9D4";
const AXIS = "#8B93A2";
const GRID = "#E5E8EE";

const TIP = {
  contentStyle: {
    borderRadius: 8, border: "1px solid #E5E8EE", fontSize: 12,
    boxShadow: "0 10px 26px -10px rgba(14,19,32,.25)",
  },
  labelStyle: { color: "#566071", marginBottom: 4 },
  itemStyle: { color: "#0E1320", fontWeight: 600 },
};

const axisTick = { fontSize: 11, fill: AXIS };

/** "2026-09-22" → "22 Sep". Parsed by hand rather than through Date, which
 *  would shift the day backwards for anyone west of UTC. */
function shortDay(iso: string) {
  const [, m, d] = String(iso).split("-").map(Number);
  if (!m || !d) return String(iso);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${months[m - 1]}`;
}

/** Recharts hands the tooltip label through as a ReactNode. */
const dayLabel = (l: any) => shortDay(String(l ?? ""));

/** Enough ticks to orient by, never so many they overlap — the window can be
 *  7 days or 90 and the axis has to stay readable at both. */
const tickEvery = (n: number) => Math.max(0, Math.ceil(n / 8) - 1);

type Row = { id: number; name: string; value: number; note?: string };

/**
 * Ranked horizontal bars. The top `cap` rows are drawn, each with its value at
 * the end of its track, so a zero reads as a zero rather than a missing bar;
 * rows past the cap are one click away in a list that holds any number.
 */
export function RankedBars({ rows, cap = 8, unit, empty, total, onViewAll, linkTo }: {
  rows: Row[]; cap?: number; unit: string; empty: string;
  /** The full count when `rows` is only the top of a longer list the server
   *  holds; "View all" then calls `onViewAll` instead of opening `rows`. */
  total?: number | null;
  onViewAll?: () => void;
  /** When given, each row opens the detail behind its own number — e.g. a
   *  person's count is a total; this is how a reader reaches WHICH ones. */
  linkTo?: (row: Row) => string;
}) {
  const [all, setAll] = useState(false);
  const ranked = [...rows].sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
  if (!ranked.length) return <div className="empty">{empty}</div>;

  const shown = ranked.slice(0, cap);
  const count = total ?? ranked.length;
  const more = count - shown.length;
  const max = Math.max(1, ...shown.map(r => r.value));

  return (
    <div>
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {shown.map(r => {
          const row = (
            <div style={{ display: "grid", gridTemplateColumns: "140px 1fr 40px",
                          alignItems: "center", gap: 12 }}>
              <span style={{ fontSize: 13, color: "var(--p-ink)", overflow: "hidden",
                             textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {r.name}
                {r.note && <span style={{ color: "var(--p-faint)" }}> · {r.note}</span>}
              </span>
              <span style={{ height: 10, borderRadius: 5, background: "#F0F2F6", overflow: "hidden" }}>
                <span style={{ display: "block", height: "100%", borderRadius: 5, background: HUE,
                               width: `${(r.value / max) * 100}%` }} />
              </span>
              <span style={{ fontSize: 13, fontWeight: 600, textAlign: "right",
                             fontVariantNumeric: "tabular-nums",
                             color: r.value ? "var(--p-ink)" : "var(--p-faint)" }}>
                {r.value}
              </span>
            </div>
          );
          return linkTo ? (
            <Link key={r.id} to={linkTo(r)}
                  title={`${r.name}: ${r.value} ${unit} — see which ones`}
                  style={{ color: "inherit", textDecoration: "none" }}
                  className="rb-row">
              {row}
            </Link>
          ) : (
            <div key={r.id} title={`${r.name}: ${r.value} ${unit}`}>{row}</div>
          );
        })}
      </div>
      {more > 0 && (
        <div style={{ textAlign: "right", marginTop: 14 }}>
          <button type="button" className="linkish"
                  style={{ background: "none", border: 0, cursor: "pointer", fontSize: 13 }}
                  onClick={() => (onViewAll ? onViewAll() : setAll(v => !v))}>
            {all ? "Show less" : `View all ${count} →`}
          </button>
        </div>
      )}
      {all && <FullList rows={ranked} unit={unit} linkTo={linkTo} />}
    </div>
  );
}

/** Every row, however many there are — the chart shows the top of the list,
 *  this is the whole of it, and the readable form for anyone the chart's
 *  shapes do not serve. It scrolls rather than stretching the card. */
function FullList({ rows, unit, linkTo }: { rows: Row[]; unit: string; linkTo?: (row: Row) => string }) {
  return (
    <div className="tbl-wrap" style={{ marginTop: 10, maxHeight: 280, overflowY: "auto" }}>
      <table>
        <thead>
          <tr><th>#</th><th>Name</th><th style={{ textAlign: "right" }}>{unit}</th></tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id}>
              <td className="muted">{i + 1}</td>
              <td>
                {linkTo ? (
                  <Link to={linkTo(r)}>{r.name}</Link>
                ) : r.name}
                {r.note && <span className="muted" style={{ fontSize: 12 }}> · {r.note}</span>}
              </td>
              <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                           color: r.value ? undefined : "var(--p-faint)" }}>
                {r.value}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Runs a day, split by how each one came out. Stacked, because the three
 *  outcomes are parts of that day's total rather than competing series. */
export function RunTrend({ data, onDayClick }: {
  data: { date: string; clean: number; flagged: number; not_checked: number }[];
  /** When given, clicking anywhere in a day's column reports that day. */
  onDayClick?: (date: string) => void;
}) {
  const any = data.some(d => d.clean + d.flagged + d.not_checked > 0);
  if (!any) return <div className="empty">No files have been run in this period.</div>;
  return (
    <div style={{ width: "100%", height: 260 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}
          style={onDayClick ? { cursor: "pointer" } : undefined}
          onClick={onDayClick ? (st: any) => {
            // The whole column is the target, not just the painted bar, so a
            // day with one small run is as easy to hit as a busy one.
            const i = Number(st?.activeIndex);
            const day = Number.isInteger(i) && data[i] ? data[i].date : st?.activeLabel;
            if (day) onDayClick(String(day));
          } : undefined}>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="date" tickFormatter={shortDay} interval={tickEvery(data.length)}
                 axisLine={false} tickLine={false} tick={axisTick} />
          <YAxis allowDecimals={false} axisLine={false} tickLine={false} tick={axisTick} />
          <Tooltip {...TIP} cursor={{ fill: "#F7F8FB" }}
                   labelFormatter={dayLabel} />
          <Legend verticalAlign="bottom" height={30} iconType="circle"
                  wrapperStyle={{ fontSize: 12, color: AXIS }} />
          {/* 2px of surface between segments instead of a stroke, so the
              split reads without a border darkening the fill. */}
          <Bar dataKey="clean" name="Clean" stackId="r" fill={CLEAN} maxBarSize={26} />
          <Bar dataKey="flagged" name="Flagged" stackId="r" fill={FLAGGED} maxBarSize={26} />
          <Bar dataKey="not_checked" name="Not checked yet" stackId="r"
               fill={NOT_CHECKED} maxBarSize={26} radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Exceptions put right over time. Its own chart rather than a second axis on
 *  the runs chart: two scales on one plot invent a correlation that is not in
 *  the data. */
export function ResolvedTrend({ data }: {
  data: { date: string; resolved: number }[];
}) {
  if (!data.some(d => d.resolved > 0)) {
    return <div className="empty">Nothing has been put right in this period yet.</div>;
  }
  return (
    <div style={{ width: "100%", height: 260 }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 6, right: 12, left: -18, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID} />
          <XAxis dataKey="date" tickFormatter={shortDay} interval={tickEvery(data.length)}
                 axisLine={false} tickLine={false} tick={axisTick} />
          <YAxis allowDecimals={false} axisLine={false} tickLine={false} tick={axisTick} />
          <Tooltip {...TIP} labelFormatter={dayLabel}
                   formatter={(v: any) => [v, "Put right"]} />
          <Line type="monotone" dataKey="resolved" stroke={HUE} strokeWidth={2}
                dot={false} activeDot={{ r: 5 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
