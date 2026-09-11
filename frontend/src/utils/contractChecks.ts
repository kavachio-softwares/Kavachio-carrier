import type { ContractChecks } from "../api/contractRecord";

/**
 * The words for what a contract checks — one place, used by the contracts list
 * and the contract page, so the two screens never say different things.
 *
 * `rules` and `checkable` count different things (rules that run, and agreed
 * terms), so they are never shown as a fraction. How many terms are checked is
 * its own number, `terms_checked`; a rule from a Bordereau Setup carries no
 * term key and is never credited to a term.
 */
export type ChecksWords = {
  /** No rules and no terms: a contract waiting for a Bordereau Setup. */
  none: boolean;
  /** "102 rules · 9 terms" */
  badge: string;
  /** "0 of 9 terms checked · rules from Bordereau Setup" */
  detail: string;
  /** "19 from the wording · 83 standard checks" */
  sources: string;
  /** Every term checked, or a contract without terms that has rules. */
  ok: boolean;
};

const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

export function describeChecks(k: ContractChecks): ChecksWords {
  // TEMPORARILY OFF (11 Sep 2026, user's call): the helper text under the
  // badge — "0 of 9 terms checked · rules from Bordereau Setup",
  // "4 of 9 terms checked", "19 from the wording · 29 standard checks" — is
  // commented out below rather than removed. The numbers behind it
  // (checks.terms_checked / from_terms / from_clauses / standard) still come
  // from the server; only the sentences built from them are switched off.
  // const sources = [
  //   k.from_terms > 0 && `${k.from_terms} from the terms`,
  //   k.from_clauses > 0 && `${k.from_clauses} from the wording`,
  //   k.standard > 0 && count(k.standard, "standard check"),
  // ].filter(Boolean).join(" · ");
  const sources = "";

  if (k.rules === 0 && k.checkable === 0) {
    // return { none: true, badge: "None", sources: "", ok: false,
    //   detail: "Rules are added when this contract is used in a Bordereau Setup" };
    return { none: true, badge: "None", sources: "", ok: false, detail: "" };
  }
  // An uploaded wording: no agreed terms to count, so say where its rules came from.
  if (k.checkable === 0) {
    // return { none: false, badge: count(k.rules, "rule"), detail: sources,
    //   sources, ok: true };
    return { none: false, badge: count(k.rules, "rule"), detail: "",
      sources: "", ok: true };
  }

  const all = k.terms_checked === k.checkable;
  // const terms = all && k.checkable > 1
  //   ? `all ${k.checkable} terms checked`
  //   : `${k.terms_checked} of ${count(k.checkable, "term")} checked`;
  // const fromSetup = k.rules > 0 && k.from_terms === 0;
  return {
    none: false,
    badge: `${count(k.rules, "rule")} · ${count(k.checkable, "term")}`,
    // detail: fromSetup ? `${terms} · rules from Bordereau Setup` : terms,
    detail: "",
    sources: "",
    ok: all,
  };
}
