// How the Monthly Report's three tabs (MOPR 40, 41, 42) write months, numbers
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
/** Whole-number share, "—" when there is nothing to share out. */
export const share = (n: number, of: number) => (of > 0 ? `${Math.round((100 * n) / of)}%` : "—");

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
