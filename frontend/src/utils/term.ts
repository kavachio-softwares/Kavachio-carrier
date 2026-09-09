/**
 * The term of a contract — its two dates, and the duration that is a quicker
 * way of saying the second one.
 *
 * NOTHING HERE IS STORED. A contract has an inception and an expiry, and those
 * are the only two facts; picking "12 months" writes an expiry DATE and is then
 * forgotten. That is deliberate: a stored duration would be a second answer to
 * a question the dates already answer, and the day somebody edits one without
 * the other they disagree — with the checks, the wording and the schedule all
 * reading whichever one they happen to trust. A term of 54 days, or one that
 * ends when a scheme year does, is typed straight into the expiry and is no
 * less valid for matching none of the lengths on offer.
 *
 * The lengths on offer and the convention below are SERVED
 * (`GET /contract-types` → `term`), not listed here — see
 * contract_types.TERM_DURATION_MONTHS.
 *
 * THE CONVENTION — both days count. The wording says so in as many words
 * ("from {{inception}} to {{expiry}}, both days inclusive"), so twelve months
 * from 1 Jan 2027 expires on 31 Dec 2027, not 1 Jan 2028. Where adding the
 * months lands on a day the month does not have — 31 January plus one month —
 * the end of that month is the answer and nothing is taken off it, or a term
 * comes out a day short of the month it was meant to be.
 *
 * Dates are handled as "YYYY-MM-DD" strings and compared as UTC. Never as local
 * Date objects: `new Date("2027-01-01")` is midnight UTC, which west of
 * Greenwich is the 31st of December, and a term would silently start a day early
 * for anyone in that half of the world.
 */

/** One length the form offers. Served, so this list has one home. */
export type TermDuration = { months: number; label: string };

export type TermSpec = {
  durations: TermDuration[];
  /** Whether the last day is inside the term. See the convention above. */
  inclusive: boolean;
  note?: string;
};

/** The two spec fields a term is made of. Named once, here, so the screens that
 *  couple them do not each carry their own copy of the names. */
export const INCEPTION_FIELD = "inception_dt";
export const EXPIRY_FIELD = "expiry_dt";

const ISO = /^(\d{4})-(\d{2})-(\d{2})$/;

function parse(iso: string | null | undefined): [number, number, number] | null {
  const m = ISO.exec((iso ?? "").trim());
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  // A real date, not merely a well-shaped string: an <input type=date> cannot
  // produce 31 February, but a stored value or a pasted one can.
  if (mo < 1 || mo > 12 || d < 1 || d > daysInMonth(y, mo)) return null;
  return [y, mo, d];
}

function daysInMonth(year: number, month1: number): number {
  return new Date(Date.UTC(year, month1, 0)).getUTCDate();
}

function iso(year: number, month1: number, day: number): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${year}-${p(month1)}-${p(day)}`;
}

function utc(y: number, m: number, d: number): number {
  return Date.UTC(y, m - 1, d);
}

const DAY = 86_400_000;

/**
 * The expiry a term of `months` starting on `inception` works out to, or ""
 * when the inception is not a date yet.
 */
export function expiryFor(inception: string, months: number,
                          inclusive = true): string {
  const start = parse(inception);
  if (!start || !Number.isFinite(months) || months <= 0) return "";
  const [y, m, d] = start;

  const zeroBased = (m - 1) + months;
  const year = y + Math.floor(zeroBased / 12);
  const month = (zeroBased % 12) + 1;

  // The anniversary of the inception, with a day the month does not have
  // pulled back to its last — 31 January plus one month is 28 February.
  const last = daysInMonth(year, month);
  const clamped = d > last;
  const day = clamped ? last : d;

  // Exclusive: the anniversary itself. Inclusive: the day before it — except
  // where the clamp already landed on a month end, which IS the last day of
  // that month and would otherwise come out one short.
  if (!inclusive || clamped) return iso(year, month, day);
  const before = new Date(utc(year, month, day) - DAY);
  return iso(before.getUTCFullYear(), before.getUTCMonth() + 1,
             before.getUTCDate());
}

/**
 * Which of the offered lengths this pair of dates IS, or null for a term that
 * matches none of them.
 *
 * Answered by asking each length what expiry it would produce, rather than by
 * counting months between the two dates: the two would be separate pieces of
 * arithmetic, and the first time they disagreed the form would show "12 months"
 * beside a date twelve months does not produce.
 */
export function durationOf(inception: string, expiry: string,
                           durations: TermDuration[],
                           inclusive = true): number | null {
  if (!parse(inception) || !parse(expiry)) return null;
  for (const d of durations) {
    if (expiryFor(inception, d.months, inclusive) === expiry) return d.months;
  }
  return null;
}

/** How many days the term runs for, counting both ends, or null. Negative
 *  where the dates run backwards — which the server refuses to save, and which
 *  the form should therefore be able to say out loud. */
export function daysInTerm(inception: string, expiry: string): number | null {
  const a = parse(inception), b = parse(expiry);
  if (!a || !b) return null;
  return Math.round((utc(...b) - utc(...a)) / DAY) + 1;
}

/** "1 Jan 2027 → 31 Dec 2027 · 365 days" — the length of a term in the two
 *  ways people check it, for the hint under the picker. */
export function describeTerm(inception: string, expiry: string): string {
  const days = daysInTerm(inception, expiry);
  if (days === null) return "";
  const show = (s: string) => {
    const p = parse(s)!;
    return new Date(utc(...p)).toLocaleDateString(undefined,
      { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  };
  if (days <= 0) {
    return `${show(inception)} → ${show(expiry)} · expiry falls before inception`;
  }
  return `${show(inception)} → ${show(expiry)} · ${days} day`
       + `${days === 1 ? "" : "s"}, both days inclusive`;
}
