// How often a bordereau is reported — the ONE list.
//
// This used to be five hardcoded lists in five screens with four different
// vocabularies between them: AddProgram offered "Monthly"/"Quarterly", Welcome
// offered "annually", DirectSetup and PartyDetail offered "semi-annual" and
// "annual", and the calendar editor offered "weekly". Two of those values —
// semi-annual and annual — were ones the calendar engine could not build from
// at all, so a programme set to Annual silently produced no deadlines and the
// screen said only "No deadlines yet".
//
// The tokens below are exactly what the backend stores and resolves
// (submission_calendar.FREQUENCIES). Anything a screen offers, the calendar can
// build. That is the whole point of there being one list.

export type Frequency =
  | "weekly" | "monthly" | "quarterly" | "half_yearly" | "yearly";

/** The value stored, and the label shown. Ordered shortest period first. */
export const FREQUENCIES: { value: Frequency; label: string }[] = [
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
  { value: "half_yearly", label: "Half-yearly" },
  { value: "yearly", label: "Yearly" },
];

// Weekly is a real frequency the engine supports, but no carrier reports a
// bordereau weekly — it exists for the odd delegated-authority feed. Programme
// forms therefore offer the four a carrier actually picks from, and the
// calendar's own editor offers all five so an existing weekly schedule can
// still be seen and changed.
export const PROGRAMME_FREQUENCIES = FREQUENCIES.filter(f => f.value !== "weekly");

/** Plain-English label for a stored value, including the legacy spellings that
 *  are still sitting in the database from before this list existed. */
export const FREQUENCY_LABEL: Record<string, string> = {
  ...Object.fromEntries(FREQUENCIES.map(f => [f.value, f.label])),
  // Legacy values the old per-screen lists wrote. The backend normalises these
  // when it resolves a schedule; this keeps them readable until they are next
  // saved, rather than showing a raw "semi-annual" beside a proper label.
  "semi-annual": "Half-yearly", "semi_annual": "Half-yearly",
  semiannual: "Half-yearly", "half-yearly": "Half-yearly",
  annual: "Yearly", annually: "Yearly",
};

/** How often it reports, said the way the calendar screen says it. */
export const FREQUENCY_CADENCE: Record<string, string> = {
  weekly: "Every week",
  monthly: "Every month",
  quarterly: "Every three months",
  half_yearly: "Every six months",
  yearly: "Once a year",
};

export function frequencyLabel(value?: string | null): string {
  if (!value) return "—";
  return FREQUENCY_LABEL[value] ?? FREQUENCY_LABEL[value.toLowerCase()] ?? value;
}
