/**
 * What to call the other side of a contract on screen.
 *
 * A broker contract (insurer ↔ broker) is with a broker. A treaty
 * (insurer ↔ reinsurer) is with its reinsurers — the lead reinsurer signs it.
 * Screens shared by both say "the broker" only where the contract really is
 * with one; chosen once here so every sentence on a page agrees.
 */
export type CounterpartyWord = "broker" | "reinsurer";

/** "reinsurer" for a treaty, "broker" for everything else. */
export function counterpartyWord(contractType: string | null | undefined): CounterpartyWord {
  return contractType === "insurer_reinsurer" ? "reinsurer" : "broker";
}

/** The same word with a capital, for labels and the start of a sentence. */
export function counterpartyLabel(contractType: string | null | undefined): "Broker" | "Reinsurer" {
  return counterpartyWord(contractType) === "reinsurer" ? "Reinsurer" : "Broker";
}
