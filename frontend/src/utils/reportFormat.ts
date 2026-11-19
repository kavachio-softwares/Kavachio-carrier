// How the Operational Report's three tabs (MOPR 40, 41, 42) write months, numbers
// and times — one place, so the Overview and the detail tabs never disagree.

/** "2026-10" → "October 2026". */
export function fmtMonth(key: string): string {
  const [y, m] = key.split("-").map(Number);
  if (!y || !m) return key;
  return new Date(y, m - 1, 1).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

/** "2026-10" → "October". */
export function fmtMonthName(key: string): string {
  const [y, m] = key.split("-").map(Number);
  if (!y || !m) return key;
  return new Date(y, m - 1, 1).toLocaleDateString("en-GB", { month: "long" });
}

/** This month and the eleven before it, newest first. */
export function lastMonths(n = 12): string[] {
  const now = new Date();
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  });
}

/** "2026-10" moved by n months: shiftMonth("2026-10", -1) → "2026-09". */
export function shiftMonth(key: string, n: number): string {
  const [y, m] = key.split("-").map(Number);
  const d = new Date(y, m - 1 + n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

export const num = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("en-GB"));
export const pct = (n: number | null | undefined) => (n == null ? "—" : `${Math.round(n)}%`);
export const plural = (n: number, w: string, ws = `${w}s`) => `${num(n)} ${n === 1 ? w : ws}`;

/** A fix time: hours under a day, else days to one decimal. */
export function fmtTook(d: number | null | undefined): string {
  if (d == null) return "—";
  if (d < 1) return `${Math.max(1, Math.round(d * 24))} h`;
  const r = Math.round(d * 10) / 10;
  return `${Number.isInteger(r) ? r : r.toFixed(1)} ${r === 1 ? "day" : "days"}`;
}

/** 1 → "1st", 3 → "3rd", 10 → "10th". */
export function ordinal(n: number): string {
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${suffix}`;
}

/** "2026-11-04" → "Wednesday 4 November 2026". Read as a calendar day, so no
 *  time zone can move it. */
export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Date(y, m - 1, d).toLocaleDateString("en-GB",
    { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

/** How long a problem has been waiting, in whole days. */
export function fmtAge(d: number | null | undefined): string {
  if (d == null) return "—";
  return d < 1 ? "Under 1 day" : plural(Math.floor(d), "day");
}

// --- a day, a week, a month or dates picked (the Operational Report's switch) ------
// The same periods as the Executive Report (backend monthly_report.window): a
// week is Monday to Sunday, a custom range is "from..to" with both days in.

export type ReportCadence = "day" | "week" | "month" | "custom";

/** A Date as "YYYY-MM-DD", by its own calendar day. */
export function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Today, or n days before it, as "YYYY-MM-DD". */
export function daysAgo(n = 0): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return isoDay(d);
}

const dayOf = (iso: string) => {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d);
};

/** The Monday of the week a day falls in. */
export function mondayOf(iso: string): string {
  const d = dayOf(iso);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return isoDay(d);
}

/** This week's Monday and the weeks before it, newest first. */
export function lastWeeks(n = 13): string[] {
  const monday = dayOf(mondayOf(daysAgo(0)));
  return Array.from({ length: n }, (_, i) =>
    isoDay(new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() - 7 * i)));
}

// Fixed short names, so "Sep" reads the same here as in the PDF (browsers
// differ: some write "Sept").
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const short = (d: Date) => `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;

/** A day n days after (or before) another, as "YYYY-MM-DD". */
export function addDays(iso: string, n: number): string {
  const d = dayOf(iso);
  return isoDay(new Date(d.getFullYear(), d.getMonth(), d.getDate() + n));
}

/** "9 – 15 Nov 2026", "28 Sep – 4 Oct 2026", "20 Dec 2025 – 5 Jan 2026"; one day "3 Oct 2026". */
export function fmtSpan(from: string, to: string): string {
  const x = dayOf(from), y = dayOf(to);
  if (from === to) return short(y);
  if (x.getFullYear() !== y.getFullYear()) return `${short(x)} – ${short(y)}`;
  return `${x.getDate()}${x.getMonth() === y.getMonth() ? "" : ` ${MON[x.getMonth()]}`} – ${short(y)}`;
}

/** "October 2026", "9 – 15 Nov 2026", "17 Nov 2026", "1 – 18 Nov 2026" — a
 *  week by its dates, as the Executive Report's week picker writes it. */
export function periodTitle(cadence: ReportCadence, period: string): string {
  if (cadence === "month") return fmtMonth(period);
  if (cadence === "day") return short(dayOf(period));
  if (cadence === "week") return fmtSpan(period, addDays(period, 6));
  const [a, b] = period.split("..");
  return a && b ? fmtSpan(a, b) : period;
}

/** A period's title as it reads mid-sentence. */
export const titleInSentence = (cadence: ReportCadence, title: string) =>
  cadence === "week" ? title.charAt(0).toLowerCase() + title.slice(1) : title;

/** "in October 2026", "on 17 Nov 2026", "in the week of 9 – 15 Nov 2026", "in 1 – 18 Nov 2026". */
export function inPeriod(cadence: ReportCadence, title: string): string {
  if (cadence === "day") return `on ${title}`;
  if (cadence === "week") return `in the week of ${title}`;
  return `in ${title}`;
}

/** The first and last day of a period, the period it is compared with, and —
 *  a month or a week — the same one last year. The backend's rules
 *  (monthly_report.window, as the Executive Report's): a day beside the day
 *  before, a week beside the week before and the Monday 52 weeks back, dates
 *  picked beside as many days before. */
export function periodRanges(cadence: ReportCadence, period: string): {
  current: { from: string; to: string };
  previous: { from: string; to: string };
  lastYear: { from: string; to: string } | null;
} {
  const monthRange = (key: string) => {
    const [y, m] = key.split("-").map(Number);
    return { from: `${key}-01`, to: isoDay(new Date(y, m, 0)) };
  };
  if (cadence === "month") {
    return { current: monthRange(period), previous: monthRange(shiftMonth(period, -1)),
             lastYear: monthRange(shiftMonth(period, -12)) };
  }
  if (cadence === "week") {
    const week = (monday: string) => ({ from: monday, to: addDays(monday, 6) });
    return { current: week(period), previous: week(addDays(period, -7)), lastYear: week(addDays(period, -364)) };
  }
  if (cadence === "day") {
    const before = addDays(period, -1);
    return { current: { from: period, to: period }, previous: { from: before, to: before }, lastYear: null };
  }
  const [a, b] = period.split("..");
  const days = Math.round((dayOf(b).getTime() - dayOf(a).getTime()) / 86400000) + 1;
  return { current: { from: a, to: b }, previous: { from: addDays(a, -days), to: addDays(a, -1) }, lastYear: null };
}

/** The first letter up: "In October 2026". */
export const upFirst = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** What a period is compared with, in words. */
export const VS_WORDS: Record<ReportCadence, string> = {
  month: "last month", week: "last week", day: "the day before", custom: "the period before",
};

/** What one period is called: "month", "week", "day", "period". */
export const PERIOD_WORD: Record<ReportCadence, string> = {
  month: "month", week: "week", day: "day", custom: "period",
};

/** Whether a period runs to today or later, so its numbers still move. */
export function periodRunning(cadence: ReportCadence, period: string): boolean {
  const today = daysAgo(0);
  if (cadence === "month") return period >= today.slice(0, 7);
  if (cadence === "day") return period >= today;
  if (cadence === "week") {
    const m = dayOf(period);
    return isoDay(new Date(m.getFullYear(), m.getMonth(), m.getDate() + 6)) >= today;
  }
  return (period.split("..")[1] ?? "") >= today;
}

// --- the automatic send (Send & Schedule) -------------------------------------

export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];

/** An hour of the day: 9 → "09:00". */
export const fmtHour = (h: number) => `${String(h).padStart(2, "0")}:00`;

/** "every day", "every Wednesday", "on the 3rd working day of each month". */
export function scheduleWords(s: { cadence: "day" | "week" | "month"; week_day: number; send_day: number }): string {
  if (s.cadence === "day") return "every day";
  if (s.cadence === "week") return `every ${WEEKDAYS[s.week_day] ?? "Monday"}`;
  return `on the ${ordinal(s.send_day)} working day of each month`;
}
