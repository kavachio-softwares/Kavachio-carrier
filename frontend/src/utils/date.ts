// Backend timestamps are naive UTC ("2026-07-15T10:32:07"). Older payloads carry no
// zone marker, newer ones end in "Z". new Date() reads an unmarked string as LOCAL,
// so every render silently shifts by the viewer's offset. Parse through here instead.
// Everything below formats in the viewer's own zone/locale (locale arg = undefined).

// A zone marker on the time part: trailing "Z"/"z", or an offset (+05:30, -0500, +05).
const HAS_ZONE = /(?:[Zz]|[+-]\d{2}(?::?\d{2})?)$/;
// An hour-only offset ("+05"). Valid ISO 8601, but new Date() rejects it.
const HOUR_ONLY_ZONE = /[+-]\d{2}$/;

// Parse a backend timestamp as UTC. null for missing/unparseable input.
export function parseUtc(iso?: string | null): Date | null {
  if (!iso) return null;
  let s = iso.trim().replace(" ", "T");   // tolerate "2026-07-15 10:32:07"
  if (!s) return null;
  const t = s.indexOf("T");
  // Date-only is already UTC per spec, and "2026-07-15Z" would be invalid — leave it.
  if (t >= 0) {
    const time = s.slice(t + 1);
    // Only stamp UTC on a zone-less time; a string carrying its own zone keeps it.
    if (!HAS_ZONE.test(time)) s += "Z";
    else if (HOUR_ONLY_ZONE.test(time)) s += ":00";   // "+05" -> "+05:00"
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

// Date + time, e.g. "15 Jul 2026, 10:32" (exact shape/order follows the viewer's locale).
export function fmtDateTime(iso?: string | null, fallback = "—"): string {
  const d = parseUtc(iso);
  if (!d) return fallback;
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" }) +
    ", " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

// Date only, e.g. "15 Jul 2026".
export function fmtDate(iso?: string | null, fallback = "—"): string {
  const d = parseUtc(iso);
  return d ? d.toLocaleDateString(undefined,
    { day: "2-digit", month: "short", year: "numeric" }) : fallback;
}

// Fixed-width stamp, e.g. "2026-07-15 10:32". Same shape as the raw ISO string
// these tables used to slice to 16 chars — zero-padded, sortable, locale-independent
// — but in the viewer's zone rather than UTC. Built from the local getters on
// purpose: toLocaleString can't be relied on for year-month-day order.
export function fmtStamp(iso?: string | null, fallback = "—"): string {
  const d = parseUtc(iso);
  if (!d) return fallback;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    ` ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Compact stamp for dense lists, e.g. "15 Jul, 10:32" — no year.
export function fmtShortDateTime(iso?: string | null, fallback = "—"): string {
  const d = parseUtc(iso);
  return d ? d.toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : fallback;
}

// Start/end of the LOCAL calendar day for a `<input type="date">` value
// ("YYYY-MM-DD"), for building an inclusive date-range filter. `new
// Date("YYYY-MM-DD")` parses as UTC midnight per spec, not local — the same
// zone trap this file exists to avoid — so this builds the Date from its
// numeric parts instead, which the Date constructor always reads as local.
export function localDayStart(dateOnly?: string | null): Date | null {
  if (!dateOnly) return null;
  const [y, m, d] = dateOnly.split("-").map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d, 0, 0, 0, 0);
}
export function localDayEnd(dateOnly?: string | null): Date | null {
  const start = localDayStart(dateOnly);
  if (!start) return null;
  return new Date(start.getFullYear(), start.getMonth(), start.getDate(), 23, 59, 59, 999);
}
