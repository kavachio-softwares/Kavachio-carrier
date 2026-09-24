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
import { useEffect, useState } from "react";
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

type Row = {
  id: number; name: string; value: number; note?: string;
  /** The rest of this row's own whole — e.g. `value` is what is still open and
   *  `settled` what has been put right, so `value + settled` is everything
   *  that row raised. Present it and the bar stops being scaled against the
   *  biggest row and becomes this row's own 100%. See RankedRow. */
  settled?: number;
};

/**
 * Ranked horizontal bars. The top `cap` rows are drawn, each with its value at
 * the end of its track, so a zero reads as a zero rather than a missing bar;
 * rows past the cap are one click away in a list that holds any number.
 */
export function RankedBars({ rows, cap = 8, unit, empty, total, onViewAll, linkTo, title }: {
  rows: Row[]; cap?: number; unit: string; empty: string;
  /** The full count when `rows` is only the top of a longer list the server
   *  holds; "View all" then calls `onViewAll` instead of opening `rows`. */
  total?: number | null;
  onViewAll?: () => void;
  /** When given, each row opens the detail behind its own number — e.g. a
   *  person's count is a total; this is how a reader reaches WHICH ones. */
  linkTo?: (row: Row) => string;
  /** Heading for the "view all" dialog. Defaults to the unit, but a card that
   *  has a name should pass it — the dialog covers the card it came from, so
   *  without it the reader loses which chart they opened. */
  title?: string;
}) {
  const [all, setAll] = useState(false);
  const ranked = [...rows].sort((a, b) => b.value - a.value || a.name.localeCompare(b.name));
  if (!ranked.length) return <div className="empty">{empty}</div>;

  const shown = ranked.slice(0, cap);
  const count = total ?? ranked.length;
  const more = count - shown.length;
  const max = Math.max(1, ...shown.map(r => r.value));

  const share = ranked.some(r => r.settled != null);

  return (
    <div>
      {share && <ShareLegend />}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {shown.map(r => (
          <RankedRow key={r.id} r={r} max={max} unit={unit} linkTo={linkTo} />
        ))}
      </div>
      {more > 0 && (
        <div style={{ textAlign: "right", marginTop: 14 }}>
          <button type="button" className="linkish"
                  style={{ background: "none", border: 0, cursor: "pointer", fontSize: 13 }}
                  onClick={() => (onViewAll ? onViewAll() : setAll(true))}>
            View all {count} →
          </button>
        </div>
      )}
      {all && (
        <FullListModal rows={ranked} unit={unit} linkTo={linkTo}
          title={title ?? `All ${unit}`} onClose={() => setAll(false)} />
      )}
    </div>
  );
}

/** One bar. The same row in the card and in the dialog, so opening the full
 *  list does not change the shape of what is being read — only how much of it
 *  is on screen.
 *
 *  TWO BARS LIVE HERE, and `settled` decides which.
 *
 *  Without it the bar is scaled against the biggest row: the length says "how
 *  this row compares with the leader", which is what a ranked chart is for.
 *
 *  With it the bar is this row's OWN whole — still open, then put right — and
 *  length no longer compares rows at all. That is the honest shape for a
 *  rounded bar sitting in a track, which every reader has learned to read as a
 *  progress meter: at max-scaling a broker with 913 of the leader's 1,116 drew
 *  an 82%-full meter and was read as "82% of their file is broken". The count
 *  beside it carries the volume, which the length no longer can. */
function RankedRow({ r, max, unit, linkTo }: {
  r: Row; max: number; unit: string; linkTo?: (row: Row) => string;
}) {
  const share = r.settled != null;
  const whole = share ? r.value + (r.settled ?? 0) : 0;
  const openPct = share ? (whole ? (r.value / whole) * 100 : 0) : (r.value / max) * 100;
  const row = (
    <div style={{ display: "grid",
                  gridTemplateColumns: share ? "140px 1fr 96px" : "140px 1fr 40px",
                  alignItems: "center", gap: 12 }}>
      <span style={{ fontSize: 13, color: "var(--p-ink)", overflow: "hidden",
                     textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {r.name}
        {r.note && <span style={{ color: "var(--p-faint)" }}> · {r.note}</span>}
      </span>
      <span style={{ height: 10, borderRadius: 5, background: "#F0F2F6",
                     overflow: "hidden", display: "flex" }}>
        <span style={{ display: "block", height: "100%", borderRadius: 5,
                       background: share ? FLAGGED : HUE, width: `${openPct}%` }} />
        {share && (r.settled ?? 0) > 0 && (
          <span style={{ display: "block", height: "100%", borderRadius: 5,
                         background: CLEAN, width: `${100 - openPct}%` }} />
        )}
      </span>
      {share ? (
        <span style={{ fontSize: 12.5, textAlign: "right", fontVariantNumeric: "tabular-nums",
                       color: "var(--p-muted)", lineHeight: 1.25 }}>
          <b style={{ color: r.value ? "var(--p-ink)" : "var(--p-faint)" }}>{r.value}</b>
          {" "}of {whole}
          <span style={{ display: "block", fontSize: 11, color: "var(--p-faint)" }}>
            {whole ? Math.round(openPct) : 0}% open
          </span>
        </span>
      ) : (
        <span style={{ fontSize: 13, fontWeight: 600, textAlign: "right",
                       fontVariantNumeric: "tabular-nums",
                       color: r.value ? "var(--p-ink)" : "var(--p-faint)" }}>
          {r.value}
        </span>
      )}
    </div>
  );
  const tip = share
    ? `${r.name}: ${r.value} of ${whole} ${unit} still open — ${r.settled} put right`
    : `${r.name}: ${r.value} ${unit}`;
  return linkTo ? (
    <Link to={linkTo(r)} title={`${tip} — see which ones`}
          style={{ color: "inherit", textDecoration: "none" }} className="rb-row">
      {row}
    </Link>
  ) : (
    <div title={tip}>{row}</div>
  );
}

/** The two halves, named. Colour is doing real work in a share bar — it is the
 *  only thing separating the part that needs someone from the part that is
 *  done — so it has to be written down somewhere. */
function ShareLegend() {
  return (
    <div style={{ display: "flex", gap: 14, fontSize: 11.5, color: "var(--p-muted)",
                  marginBottom: 12 }}>
      {[["Still open", FLAGGED], ["Resolved", CLEAN]].map(([label, c]) => (
        <span key={label} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 9, height: 9, borderRadius: 3, background: c }} />{label}
        </span>
      ))}
    </div>
  );
}

/** Every row, however many there are, in a dialog.
 *
 *  It used to be a numbered table appended under the chart, which made the card
 *  grow a second, differently-shaped reading of the same data and pushed
 *  whatever sat beside it out of line. As a dialog the list keeps the chart's
 *  own bars — the long tail is still ranked against the leader, which is the
 *  comparison the card exists to make — and the card behind it does not move.
 *
 *  Bars are scaled to the top row of the WHOLE list, not of the page, so a row
 *  is the same length here as it is in the card. */
function FullListModal({ rows, unit, linkTo, title, onClose }: {
  rows: Row[]; unit: string; linkTo?: (row: Row) => string;
  title: string; onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const max = Math.max(1, ...rows.map(r => r.value));
  const sum = rows.reduce((a, r) => a + r.value, 0);

  return (
    <div className="proto-modal-overlay" onClick={onClose}>
      <div className="proto-modal" style={{ width: 680 }} onClick={e => e.stopPropagation()}
           role="dialog" aria-modal="true" aria-label={title}>
        <div className="m-h">
          <h3>{title}</h3>
          <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 14 }}>
            <span style={{ fontSize: 13, color: "var(--p-faint)" }}>
              {sum} {unit} · {rows.length} rows
            </span>
            <button type="button" className="x" onClick={onClose} aria-label="Close">×</button>
          </span>
        </div>
        <div className="m-b" style={{ maxHeight: "60vh", overflowY: "auto" }}>
          {rows.some(r => r.settled != null) && <ShareLegend />}
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {rows.map(r => (
              <RankedRow key={r.id} r={r} max={max} unit={unit} linkTo={linkTo} />
            ))}
          </div>
        </div>
      </div>
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
                   formatter={(v: any) => [v, "Resolved"]} />
          <Line type="monotone" dataKey="resolved" stroke={HUE} strokeWidth={2}
                dot={false} activeDot={{ r: 5 }} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ---- who sent what, and how much of it is still open --------------------- */

/** An exception nobody has decided yet — the flagged amber, so a run's colour
 *  means the same thing wherever it is drawn. */
const OPEN = FLAGGED;
/** An exception that has been fixed, approved, dismissed or rejected. */
const PUT_RIGHT = CLEAN;
/** No exception at all. Not an outcome anyone worked for, so no colour —
 *  the same neutral "nothing here" as a file nobody has checked. */
const NO_ISSUES = NOT_CHECKED;
const TRACK = "#F0F2F6";

export type UploaderRow = {
  id: number; name: string; note?: string;
  files: number;
  uploads: {
    rows: number; rows_flagged: number;
    exceptions: number; open: number; put_right: number;
  };
  /** Exceptions this person decided themselves, wherever the file came from. */
  resolved: number;
};

/**
 * One bar per person: the exceptions on the files THEY sent, amber for what is
 * still open and green for what has been put right.
 *
 * WHY EXCEPTIONS AND NOT ROWS OR CELLS. An exception sits on a single CELL. A
 * row of forty values with one bad date is one thing to fix, but counting it
 * as a ROW paints the whole row amber and reports "10 of 10 rows need review"
 * about a file that is 97% fine. Counting the other way — every exception
 * against every cell checked — is honest about the file and useless on a
 * dashboard: fourteen exceptions in five hundred cells is a sliver nobody can
 * see. So the bar is the WORK, which is the question this card answers, and
 * the size of what it came from is written beside it in files and rows.
 *
 * Each bar is that person's own 100%: it shows how far THEIR pile has been
 * cleared, never whose pile is biggest — the counts on the right carry that.
 * A person with nothing wrong still gets a bar, a full grey one that says so,
 * because an empty track reads as a broken chart rather than as good news.
 * Every number is written out too, so colour is never the only way to read a
 * row, and the key under the title says what each one means.
 */
export function UploaderBars({ rows, cap = 5, total, onViewAll, personTo, empty }: {
  rows: UploaderRow[]; cap?: number; empty: string;
  /** The whole team's size when `rows` is only the top of it. */
  total?: number | null;
  onViewAll?: () => void;
  /** Where a person's name goes — their own activity page. */
  personTo?: (row: UploaderRow) => string;
}) {
  if (!rows.length) return <div className="empty">{empty}</div>;
  const shown = rows.slice(0, cap);
  const count = total ?? rows.length;
  const more = count - shown.length;
  const anyFiles = shown.some(r => r.files > 0);

  return (
    <div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12,
                    color: "var(--p-muted)", marginBottom: 14 }}>
        <Key color={OPEN} label="Still open" />
        <Key color={PUT_RIGHT} label="Resolved" />
        <Key color={NO_ISSUES} label="No issues" />
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {shown.map(r => <UploaderRowView key={r.id} r={r} personTo={personTo} />)}
      </div>

      {!anyFiles && (
        <div style={{ fontSize: 12, color: "var(--p-faint)", marginTop: 12 }}>
          Nobody on your team has sent a file in this period. Files a carrier ran
          for you are not counted here — they belong to no one person.
        </div>
      )}

      {more > 0 && (
        <div style={{ textAlign: "right", marginTop: 14 }}>
          <button type="button" className="linkish"
                  style={{ background: "none", border: 0, cursor: "pointer", fontSize: 13 }}
                  onClick={onViewAll}>
            View all {count} →
          </button>
        </div>
      )}
    </div>
  );
}

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

function UploaderRowView({ r, personTo }: {
  r: UploaderRow; personTo?: (row: UploaderRow) => string;
}) {
  const u = r.uploads;
  const sent = r.files
    ? `${plural(r.files, "file")} sent · ${plural(u.rows, "row")}`
    : "No files sent in this period";
  const tip = [
    r.name,
    sent,
    u.exceptions
      ? `${plural(u.exceptions, "exception")} across ${plural(u.rows_flagged, "row")}`
        + ` — ${u.open} still open, ${u.put_right} put right`
      : r.files ? "Nothing was flagged on them" : "",
    `${plural(r.resolved, "exception")} put right by ${r.name} in this period`,
  ].filter(Boolean).join("\n");

  const seg = (n: number, color: string) => n > 0 && (
    <span style={{ flex: n, background: color, borderRadius: 6, minWidth: 3 }} />
  );

  return (
    <div title={tip}
         style={{ display: "grid", gridTemplateColumns: "minmax(110px, 150px) 1fr 122px",
                  alignItems: "center", gap: 14 }}>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: "var(--p-ink)",
                       overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {personTo ? (
            <Link to={personTo(r)} style={{ color: "inherit", textDecoration: "none" }}>{r.name}</Link>
          ) : r.name}
          {r.note && <span style={{ color: "var(--p-faint)", fontWeight: 400 }}> · {r.note}</span>}
        </span>
        <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
          {r.files ? `${plural(r.files, "file")} · ${plural(u.rows, "row")}` : "no files sent"}
        </span>
      </span>

      {r.files ? (
        <span style={{ height: 12, borderRadius: 6, background: TRACK, display: "block" }}>
          <span style={{ display: "flex", gap: 2, height: "100%", width: "100%" }}>
            {u.exceptions ? (
              <>{seg(u.open, OPEN)}{seg(u.put_right, PUT_RIGHT)}</>
            ) : (
              // Nothing was flagged. The track is filled rather than left
              // empty: "no issues" is an answer, and a blank bar is not one.
              <span style={{ flex: 1, background: NO_ISSUES, borderRadius: 6 }} />
            )}
          </span>
        </span>
      ) : (
        <span style={{ fontSize: 12, color: "var(--p-faint)" }}>Nothing sent in this period</span>
      )}

      <span style={{ fontSize: 12.5, textAlign: "right", fontVariantNumeric: "tabular-nums",
                     color: "var(--p-muted)" }}>
        {!r.files ? "—" : !u.exceptions ? (
          <>
            <b style={{ color: "var(--p-ink)" }}>No issues</b>
            <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
              nothing flagged
            </span>
          </>
        ) : u.open > 0 ? (
          <>
            <b style={{ color: "var(--p-ink)" }}>{u.open}</b> of {u.exceptions} open
            <span style={{ display: "block", fontSize: 11.5 }}>
              {/* Their FILES, not the newest one. Four files on two programmes
                  behind one number is the normal case, and dropping an admin
                  into whichever file happened to be last answered a question
                  nobody asked. */}
              {personTo ? (
                <Link className="linkish" to={personTo(r)}>
                  {r.files === 1 ? "Review this file →" : `Review ${r.files} files →`}
                </Link>
              ) : <span style={{ color: "var(--p-faint)" }}>{u.put_right} put right</span>}
            </span>
          </>
        ) : (
          <>
            <b style={{ color: "var(--p-ink)" }}>All clear</b>
            <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
              {plural(u.put_right, "exception")} put right
            </span>
          </>
        )}
      </span>
    </div>
  );
}

function Key({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span style={{ width: 10, height: 10, borderRadius: 3, background: color }} />{label}
    </span>
  );
}
