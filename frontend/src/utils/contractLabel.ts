/**
 * What to call a contract on screen.
 *
 * There are two ways a contract gets into Kavachio and they leave different
 * things behind. One written here is typed — it has a NAME ("DEMO 2") and no
 * file at all, so no filename. One uploaded arrives as a document and has a
 * FILENAME, and often a name taken from it. Either can be missing; a fair
 * number of older rows have neither.
 *
 * Every screen was answering that on its own, and they did not agree: the
 * Contracts list read `name`, the bordereau setup pickers read `filename`, and
 * a typed contract therefore appeared as "DEMO 2" on one screen and
 * "Contract 3115" on the next. Same row, two names, and nothing to tell the
 * user they were looking at the same contract.
 *
 * The order below is what a person would answer if asked what the contract is
 * called: the name it was given, else the file it came in as, else its id —
 * which is not a label anyone chose, but it is the only thing left and a blank
 * cell would be worse.
 */
export type ContractLike = {
  id: number;
  name?: string | null;
  filename?: string | null;
};

/** The name to show. Never empty — falls back to the id. */
export function contractLabel(c: ContractLike): string {
  return (c.name || "").trim()
    || (c.filename || "").trim()
    || `Contract ${c.id}`;
}

/** The name to show, when the contract may not be there at all. */
export function contractLabelOr(c: ContractLike | null | undefined,
                                fallback = "—"): string {
  return c ? contractLabel(c) : fallback;
}
