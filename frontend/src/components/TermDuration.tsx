/**
 * Duration — a quicker way of saying the expiry date.
 *
 * Two dates is how a term is stored and how everything downstream reads it, but
 * it is not how anybody says one: contracts are agreed as "twelve months from
 * inception", and working out which day that lands on is arithmetic the person
 * filling the form should not be doing. So this sits BETWEEN the two date
 * inputs, and picking a length writes the expiry.
 *
 * It never takes the expiry away from them — but it does make them ASK for it.
 * The expiry input is held shut while a length is in force, because two ways of
 * saying the same fact, both editable, is how a contract ends up reading "12
 * months" beside a date twelve months does not produce. A term of 54 days, or
 * one that ends when a scheme year does, is still typed straight into the
 * expiry: picking "Custom — I'll set the expiry myself" opens the field and
 * hands the term back to the person filling the form.
 *
 * What the picker displays is READ OFF THE DATES, not remembered — so a term
 * loaded from a draft, or one typed under Custom, cannot leave the control
 * claiming a length the dates do not have. What it does remember is the length,
 * and only for one purpose: moving the inception moves the expiry with it,
 * which is the whole point of having said "twelve months" in the first place.
 *
 * The lengths on offer and the counting convention are SERVED (see
 * utils/term.ts and contract_types.TERM_DURATION_MONTHS). Nothing about a
 * duration is stored on the contract.
 */
import { useState } from "react";
import {
  describeLength, describeTerm, durationOf, expiryFor, type TermSpec,
} from "../utils/term";

/** The picker's answer for "none of your lengths — I'll type the date". A
 *  string, so it can never be confused with a number of months. */
export const CUSTOM = "custom";

export type Term = ReturnType<typeof useTermDuration>;

/** The coupling between the two dates, owned in one place so that every screen
 *  holding a term behaves the same way. */
export function useTermDuration({ spec, inception, expiry, setInception, setExpiry }: {
  spec: TermSpec | null;
  inception: string;
  expiry: string;
  setInception: (v: string) => void;
  setExpiry: (v: string) => void;
}) {
  // The length last chosen, or CUSTOM where the person said they would set the
  // expiry themselves. Deliberately NOT what the picker displays — that is read
  // back off the dates, so a hand-typed expiry cannot leave the control
  // claiming a length the dates do not have.
  const [chosen, setChosen] = useState<number | typeof CUSTOM | null>(null);

  const durations = spec?.durations ?? [];
  const inclusive = spec?.inclusive ?? true;

  /** Which offered length these two dates are, or null for a term that is
   *  none of them. */
  const matched = durationOf(inception, expiry, durations, inclusive);

  // A term that matches no offered length IS a custom one, whether it was
  // asked for or arrived on a draft — so an existing 54-day contract opens with
  // its expiry editable rather than locked to a length it does not have.
  const custom = chosen === CUSTOM
    || (chosen === null && matched === null && Boolean(inception && expiry));

  // Custom is a MODE, not a length: it says who owns the expiry, and only
  // choosing a length hands it back. So it holds even when the dates happen to
  // work out to one — otherwise clicking Custom on a term that is already a
  // round year would snap the picker straight back and look like nothing
  // happened. The length is still named, in the option itself.
  const value: number | typeof CUSTOM | null = custom ? CUSTOM : matched;

  return {
    ready: durations.length > 0,
    durations, inclusive, inception, expiry,
    /** What the picker shows: an offered length, CUSTOM, or nothing yet. */
    value,
    /** Whether the expiry is the person's to type. Asking for Custom OPENS the
     *  field and nothing closes it again but choosing a length: a date that
     *  turns out to be a round year gets named as one, but the field must not
     *  lock itself mid-edit because a half-typed date briefly matched. */
    custom,
    /** The term said in the unit it was agreed in — "1 year", "426 days". */
    length: describeLength(inception, expiry, inclusive),

    pick(months: number | typeof CUSTOM | null) {
      setChosen(months);
      // Custom opens the expiry and leaves the dates exactly as they are: the
      // length just abandoned is the obvious starting point for editing.
      if (months === null || months === CUSTOM) return;
      const next = expiryFor(inception, months, inclusive);
      if (next) setExpiry(next);
    },
    /** Wrap the inception input with this: a term stated as a length has to
     *  follow its start date, or moving inception silently changes how long the
     *  contract runs for. */
    onInception(v: string) {
      setInception(v);
      // A stated length follows its start date — that is the whole point of
      // having said "twelve months". A custom term does not: its expiry is the
      // person's, and moving it under them is exactly what they opted out of.
      if (typeof value !== "number") return;
      const next = expiryFor(v, value, inclusive);
      if (next) setExpiry(next);
    },
    /** Wrap the expiry input with this. Only reachable under Custom, and it
     *  keeps it that way: a date typed by hand must not be re-read as a length
     *  and then quietly locked again on the next keystroke. */
    onExpiry(v: string) {
      setChosen(CUSTOM);
      setExpiry(v);
    },
  };
}

/** The control itself — one `.field`, to sit in the same grid as the dates. */
export function TermDurationField({ term }: { term: Term }) {
  const { inception, expiry, value, length, durations } = term;
  const noStart = !inception;

  return (
    <div className="field" style={{ marginBottom: 0 }}>
      <label>
        Duration
        <span className="muted" style={{ fontWeight: 500 }}> — sets the expiry</span>
      </label>
      <select
        value={value ?? ""}
        disabled={noStart}
        aria-label="How long the term runs for"
        onChange={e => term.pick(
          e.target.value === CUSTOM ? CUSTOM
          : e.target.value ? Number(e.target.value) : null)}
      >
        <option value="">
          {noStart ? "Set the inception first" : "Choose a length…"}
        </option>
        {durations.map(d => (
          <option key={d.months} value={d.months}>{d.label}</option>
        ))}
        {/* Last, and worded as an instruction rather than a length, because it
            is the one choice that hands the work back to the person: it opens
            the expiry field instead of filling it in. */}
        <option value={CUSTOM}>
          Custom — I&rsquo;ll set the expiry myself
          {value === CUSTOM && length ? ` (${length})` : ""}
        </option>
      </select>
      <div className="hint">
        {describeTerm(inception, expiry, term.inclusive)
         || (value === CUSTOM
             ? "Type the expiry date beside this."
             : "Pick a length and the expiry works itself out — or choose "
               + "Custom to type the date yourself.")}
      </div>
    </div>
  );
}
