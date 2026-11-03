/**
 * What the Documents panel says when nothing has been attached to it.
 *
 * The panel lists files attached THROUGH it — references, endorsements, the
 * signed copy. An uploaded contract's own PDF is not one of them: it is stored
 * on the contract row itself (`filename`), and its clauses were read from it on
 * upload. Saying "without a wording this contract has no clauses" there was
 * wrong — the clauses are on the Clauses tab — so three cases, not two.
 */
export type DocumentsEmptyInput = {
  /** The wording was written in Kavachio. */
  authored: boolean;
  /** The file the contract itself was uploaded as, if any. */
  filename?: string | null;
};

export type DocumentsEmpty = { title: string; bullets: string[] };

export function documentsEmptyMessage(c: DocumentsEmptyInput): DocumentsEmpty {
  if (c.authored) {
    return {
      title: "No extra files needed",
      bullets: ["The contract wording is written above."],
    };
  }
  if ((c.filename || "").trim()) {
    return {
      title: "No extra files yet",
      bullets: [
        "Add documents which the contract refers to.",
        "Add changes agreed later, or the signed contract.",
      ],
    };
  }
  return {
    title: "No files yet",
    bullets: ["Without a wording, this contract has no clauses."],
  };
}
