/**
 * What each wording token is called on screen.
 *
 * A section body holds `{{commission_pct}}` so the sentence follows its term;
 * the editor shows it as a chip named for the term. The agreed terms are named
 * by the served vocabulary (their `question`); the handful of tokens that are
 * not terms — who the parties are, the dates — are named here, once, for every
 * screen that edits wording (contract creation and mid-term changes).
 */
import type { AgreedLimitSpec } from "../api/contractRecord";

const NON_TERM_TOKENS: Record<string, string> = {
  contract_name: "Contract name", inception: "Start date",
  expiry: "End date", class_of_business: "Class of business",
  carrier_name: "Carrier", counterparty_name: "Broker",
  programme_name: "Programme", notice_period_days: "Notice to cancel",
};

export function wordingTokenLabels(limitSpec: AgreedLimitSpec[]): Record<string, string> {
  const out: Record<string, string> = { ...NON_TERM_TOKENS };
  limitSpec.forEach(l => { out[l.name] = l.question; });
  return out;
}
