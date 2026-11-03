/**
 * The term arithmetic, over the cases that actually go wrong.
 *
 * The frontend has no test runner and this is not worth adding one for, so it
 * runs on node's own type stripping and needs nothing installed:
 *
 *     node --experimental-strip-types scripts/term.scenarios.ts
 *
 * Exits non-zero on the first disagreement, so it can go in front of a commit.
 * What it is really watching: month ends (31 January plus one month), leap
 * years, and the both-days-inclusive convention the wording states — a term
 * that comes out a day short prints one date on a signed contract and expires
 * on another.
 */
import { expiryFor, durationOf, daysInTerm, describeTerm }
  from "../src/utils/term.ts";

const DURATIONS = [1,2,3,4,5,6,7,8,9,10,11,12,18,24,36,48,60]
  .map(m => ({ months: m, label: String(m) }));

let bad = 0;
function eq(label: string, got: unknown, want: unknown) {
  const ok = String(got) === String(want);
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FAIL"}  ${label}  got=${got} want=${want}`);
}

console.log("── inclusive expiry ──");
eq("1 Jan 2027 + 1 month",      expiryFor("2027-01-01", 1),  "2027-01-31");
eq("1 Jan 2027 + 12 months",    expiryFor("2027-01-01", 12), "2027-12-31");
eq("1 Jan 2027 + 60 months",    expiryFor("2027-01-01", 60), "2031-12-31");
eq("15 Mar 2027 + 60 months",   expiryFor("2027-03-15", 60), "2032-03-14");
eq("1 Jul 2026 + 6 months",     expiryFor("2026-07-01", 6),  "2026-12-31");
eq("15 Aug 2026 + 3 months",    expiryFor("2026-08-15", 3),  "2026-11-14");

console.log("── month ends and leap years ──");
eq("31 Jan 2027 + 1 month",     expiryFor("2027-01-31", 1),  "2027-02-28");
eq("31 Jan 2028 + 1 month",     expiryFor("2028-01-31", 1),  "2028-02-29");
eq("30 Nov 2027 + 3 months",    expiryFor("2027-11-30", 3),  "2028-02-29");
eq("29 Feb 2028 + 12 months",   expiryFor("2028-02-29", 12), "2029-02-28");
eq("1 Mar 2027 + 12 months",    expiryFor("2027-03-01", 12), "2028-02-29");
eq("31 Mar 2027 + 1 month",     expiryFor("2027-03-31", 1),  "2027-04-30");
eq("31 Dec 2027 + 1 month",     expiryFor("2027-12-31", 1),  "2028-01-30");

console.log("── exclusive, for completeness ──");
eq("1 Jan 2027 + 12 (exclusive)", expiryFor("2027-01-01", 12, false), "2028-01-01");

console.log("── nothing out of a non-date ──");
eq("empty inception", expiryFor("", 12), "");
eq("half-typed year", expiryFor("202", 12), "");
eq("31 February",     expiryFor("2027-02-31", 1), "");
eq("zero months",     expiryFor("2027-01-01", 0), "");
eq("negative months", expiryFor("2027-01-01", -6), "");

console.log("── reading a duration back off the dates ──");
eq("12 months round-trips", durationOf("2027-01-01", "2027-12-31", DURATIONS), 12);
eq("1 month round-trips",   durationOf("2027-01-31", "2027-02-28", DURATIONS), 1);
eq("5 years round-trips",   durationOf("2027-03-15", "2032-03-14", DURATIONS), 60);
eq("54 days is custom",     durationOf("2027-01-01", "2027-02-23", DURATIONS), null);
eq("a day out is custom",   durationOf("2027-01-01", "2028-01-01", DURATIONS), null);
eq("no expiry yet",         durationOf("2027-01-01", "", DURATIONS), null);
for (const d of DURATIONS) {
  const exp = expiryFor("2027-06-10", d.months);
  eq(`every offered length round-trips (${d.months})`,
     durationOf("2027-06-10", exp, DURATIONS), d.months);
}

console.log("── the day count people check against ──");
eq("54 days",  daysInTerm("2027-01-01", "2027-02-23"), 54);
eq("365 days", daysInTerm("2027-01-01", "2027-12-31"), 365);
eq("366 in a leap year", daysInTerm("2028-01-01", "2028-12-31"), 366);
eq("one day",  daysInTerm("2026-09-08", "2026-09-08"), 1);
eq("backwards", daysInTerm("2027-12-31", "2027-01-01"), -363);
console.log(describeTerm("2027-01-01", "2027-12-31"));
console.log(describeTerm("2027-01-01", "2027-02-23"));
console.log(describeTerm("2027-12-31", "2027-01-01"));

console.log(bad === 0 ? "\nALL SCENARIOS PASS" : `\n${bad} FAILED`);
process.exit(bad === 0 ? 0 : 1);
