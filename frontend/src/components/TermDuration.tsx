/**
 * Duration — a quicker way of saying the expiry date.
 *
 * Two dates is how a term is stored and how everything downstream reads it, but
 * it is not how anybody says one: contracts are agreed as "twelve months from
 * inception", and working out which day that lands on is arithmetic the person
 * filling the form should not be doing. So this sits BETWEEN the two date
 * inputs, and picking a length writes the expiry.
 *
 * It never takes the expiry away from them. A term of 54 days, or one that ends
 * when a scheme year does, is typed straight into the expiry field — and the
 * picker then says "Custom", because it reads the dates rather than remembering
 * what was last clicked. What it does remember is the length, and only for one
 * purpose: moving the inception moves the expiry with it, which is the whole
 * point of having said "twelve months" in the first place.
 *
 * The lengths on offer and the counting convention are SERVED (see
 * utils/term.ts and contract_types.TERM_DURATION_MONTHS). Nothing about a
 * duration is stored on the contract.
 */
import { useState } from "react";
import {
  daysInTerm, describeTerm, durationOf, expiryFor, type TermSpec,
} from "../utils/term";

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
  // The length last chosen. Deliberately NOT what the picker displays — that is
  // read back off the dates, so a hand-typed expiry cannot leave the control
  // claiming a length the dates do not have.
  const [chosen, setChosen] = useState<number | null>(null);

  const durations = spec?.durations ?? [];
  const inclusive = spec?.inclusive ?? true;

  return {
    ready: durations.length > 0,
    durations, inclusive, inception, expiry,
    /** Which offered length these two dates are, or null for a custom term. */
    value: durationOf(inception, expiry, durations, inclusive),
    days: daysInTerm(inception, expiry),

    pick(months: number | null) {
      setChosen(months);
      if (months === null) return;          // "Custom" leaves the dates alone
      const next = expiryFor(inception, months, inclusive);
      if (next) setExpiry(next);
    },
    /** Wrap the inception input with this: a term stated as a length has to
     *  follow its start date, or moving inception silently changes how long the
     *  contract runs for. */
    onInception(v: string) {
      setInception(v);
      if (chosen === null) return;
      const next = expiryFor(v, chosen, inclusive);
      if (next) setExpiry(next);
    },
    /** Wrap the expiry input with this. Typing a date by hand IS the way to say
     *  "not one of your lengths", so it forgets the one that was picked. */
    onExpiry(v: string) {
      setChosen(null);
      setExpiry(v);
    },
  };
}

/** The control itself — one `.field`, to sit in the same grid as the dates. */
export function TermDurationField({ term }: { term: Term }) {
  const { inception, expiry, value, days, durations } = term;
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
        onChange={e => term.pick(e.target.value ? Number(e.target.value) : null)}
      >
        <option value="">
          {noStart ? "Set the inception first"
           : value === null && days !== null
             ? `Custom — ${days} day${days === 1 ? "" : "s"}`
             : "Choose a length…"}
        </option>
        {durations.map(d => (
          <option key={d.months} value={d.months}>{d.label}</option>
        ))}
      </select>
      <div className="hint">
        {describeTerm(inception, expiry)
         || "Pick a length and the expiry works itself out — or type the expiry "
            + "yourself for a term of any length."}
      </div>
    </div>
  );
}
