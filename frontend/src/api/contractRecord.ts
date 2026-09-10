/**
 * The contract as a RECORD — raise it, fill it in, get it approved, keep it.
 *
 * Separate from `contracts.ts`, which does one thing: push a PDF through
 * extraction and get an id back. That file is about a FILE. This one is about
 * the contract — its terms, its counterparty, the documents it is made of, and
 * where it is in its life.
 *
 * The field rules are NOT restated here. `getContractTypes()` fetches them from
 * the server, which validates against the same list, so a field cannot be
 * mandatory in one place and optional in the other. That is why the create form
 * is built from a response rather than from a hardcoded array.
 */
import { api } from "./client";
import type { TermSpec } from "../utils/term";

/** How the server renders one input. `kind` says what to draw, not how to store. */
export type ContractFieldKind =
  "text" | "date" | "int" | "decimal" | "currency" | "party";

export type ContractField = {
  name: string;
  label: string;
  kind: ContractFieldKind;
  hint: string;
  required: boolean;
  /** Reference text for the INPUT ITSELF — the placeholder. Null where the
   *  control already says what shape the answer takes (a date, a picker). */
  example?: string | null;
};

export type ContractTypeSpec = {
  key: string;
  label: string;
  blurb: string;
  /** The kind of organisation the other side of this contract must be. */
  counterparty_party_type: "broker" | "reinsurer";
  counterparty_label: string;
  /** Brokers must already be on the programme; reinsurers need no such link. */
  counterparty_must_be_on_programme: boolean;
  fields: ContractField[];
};

/**
 * Where a contract is in its life. Distinct from approval — see the record.
 *
 * Two directions of travel meet here, which is why there are two "waiting"
 * states rather than one:
 *
 *   BROKER-ORIGINATED   draft → pending → (carrier approves) → active
 *   CARRIER-ORIGINATED  draft → in_review ⇄ changes_requested
 *                             → agreed → signed → active
 *
 * Nobody "approves" in the second one: the carrier already owns the book, so
 * what is being sought is the broker's AGREEMENT, which neither side can settle
 * alone.
 */
export type Lifecycle =
  | "draft" | "pending"
  | "in_review" | "changes_requested" | "agreed" | "signed"
  | "active" | "expired" | "terminated" | "superseded";

/** One term the broker wants changed, and what they want it changed to. */
export type ProposedChange = {
  field: string;
  current?: string | null;
  proposed?: string | null;
  comment?: string | null;
};

/** The change request the carrier has not answered yet. Null unless the
 *  contract is sitting with the carrier because of one. */
export type OpenChangeRequest = {
  note: string | null;
  proposed_changes: ProposedChange[];
  acted_at: string | null;
};

/**
 * What may be done to this contract right now, decided by the SERVER.
 *
 * Read rather than re-derived on purpose: if a screen worked these out itself
 * it would eventually offer a button the API refuses, and the user would have
 * no way to tell which of the two was wrong.
 */
export type ContractActions = {
  edit: boolean;
  /** Carrier: put the terms out to the broker, or re-send after revising. */
  send_for_review: boolean;
  /** Carrier: settle the terms without sending them out, and go straight to
   *  signing. Draft only. Offered beside send_for_review, never instead of
   *  it — the review is the default road and this is the one for a contract
   *  with nothing to negotiate or nobody to ask. */
  skip_review: boolean;
  /** Broker: push back on the terms. */
  request_changes: boolean;
  /** Broker: agree them. Not the signature, and not going live. */
  accept_terms: boolean;
  /** Broker: sign and return it to the carrier. Their last act — after this
   *  the contract is the carrier's to place and put in force. */
  submit_signed: boolean;
  /** Sign for your own side. A broker signs for the counterparty, a carrier
   *  for itself; neither can sign for the other by asking. */
  sign: boolean;
  /** Carrier only: enter a signature the other side made on paper or through a
   *  provider. Stored as `recorded`, never as one made here — and the only way
   *  a reinsurance contract is ever signed on both sides. */
  record_signature: boolean;
  /** Only the tidy-up path for a contract both sides signed while something
   *  else was in the way. The second signature normally does this on its own,
   *  and this never stands in for one. */
  activate: boolean;
  terminate: boolean;
  renew: boolean;
  upload_documents: boolean;
  generate_rules: boolean;
};

export type ContractDocumentKind = "contract" | "reference" | "endorsement";

export type ContractDocument = {
  id: number;
  kind: ContractDocumentKind;
  filename: string | null;
  /** Which externally-named document this one answers, when it answers one. */
  satisfies_reference: string | null;
  effective_from: string | null;
  /** The signed copy rather than a draft. Reported from outside — Kavachio
   *  does not witness the signing, it records that it happened. */
  is_executed_copy: boolean;
  is_active: boolean;
  created_at: string | null;
  /** False when the bytes are no longer stored — offering "download" would lie. */
  has_file: boolean;
};

export type ContractRecord = {
  id: number;
  name: string;
  /** Null for contracts raised before contracts had a coded type. */
  contract_type: string | null;
  contract_type_label: string | null;
  filename: string | null;
  programme: { id: number; name: string } | null;
  counterparty: { id: number; name: string; party_type: string } | null;
  output_template: { id: number; name: string; version: number } | null;
  schedule_key: string | null;

  umr: string | null;
  class_of_business: string | null;
  year_of_account: string | null;
  earnings_pattern: string | null;
  inception_dt: string | null;
  expiry_dt: string | null;
  executed_date: string | null;
  notice_period_days: number | null;
  premium_cap_amount: number | null;
  premium_cap_currency: string | null;

  /** What it IS now — `expired` is derived from the term, not stored. */
  lifecycle: Lifecycle;
  /** What the column holds, which differs from the above once a term lapses. */
  lifecycle_stored: string | null;
  lifecycle_effective_date: string | null;
  status_ops: string | null;
  terminated_date: string | null;
  termination_reason: string | null;
  renews_contract_id: number | null;
  submitted_at: string | null;
  approved_at: string | null;
  created_at: string | null;

  documents: ContractDocument[] | null;
  has_wording: boolean;
  endorsement_count: number;
  /** Documents the wording defers to that nobody has supplied. While this is
   *  non-empty the contract cannot be submitted, activated or re-read. */
  /** The authored contract, where there is one. Null on an upload — that is
   *  the fact, not an omission. */
  agreed_limits: AgreedLimits | null;
  /** Each section BOTH ways: `body` carries the tokens the editor turns into
   *  chips, `rendered` is the same sentence with today's values in it. Read
   *  `rendered`; edit `body`. Resolving a token is the server's job — it is
   *  the only place that knows how a percentage, a money amount or a date is
   *  written, and a second resolver here is what made a clause naming the
   *  parties read "carrier_name". */
  wording_sections: WordingSection[] | null;
  /** Terms this contract is CHECKED on that its wording does not state —
   *  nearly always a clause edited with the figure typed over the chip, after
   *  which the document keeps saying the old number. */
  wording_unquoted?: { key: string; question: string; value: string | null }[];
  /** Who signs, named while the contract was being written. Naming them sends
   *  them nothing: the address is kept for the signing round, which is the one
   *  thing in this flow that emails anybody, and it is started separately from
   *  the contract's own signature page. */
  signers: Signer[] | null;
  /** What this contract's signature page asks each side for. Always present
   *  and always normalised — the server fills in the four historic lines for a
   *  contract raised before the block was configurable, so no screen has to
   *  know that older rows hold nothing. */
  signature_layout: SignatureLayout;
  /** Who actually signed. A contract goes in force when both sides have. */
  signatures: ContractSignature[];
  /** The sides still outstanding — [] means it can go in force. */
  unsigned_sides: Array<"carrier" | "counterparty">;
  missing_references: string[];
  actions: ContractActions;
  /** Who the contract is waiting on. Both sides read the same field, so
   *  neither has to infer it from a state name written for the other one. */
  whose_turn: "carrier" | "broker" | null;
  /** What the broker asked to change, while it is still unanswered. */
  open_change_request: OpenChangeRequest | null;
  /** What is actually CHECKED on a file, which is not what the contract says.
   *  A term becomes a check only once the contract is bound to an output
   *  template — a check compares a bordereau column, and until a template is
   *  chosen there are no columns. So `rules` can be 0 while `checkable` is 10,
   *  and that gap is the thing this field exists to make visible. */
  checks: ContractChecks;
  /** Only present on a termination response. */
  warning?: string | null;
};

/** That somebody signed — as opposed to `Signer`, who was merely named to.
 *
 *  `method` is the honest bit. `typed` means the signatory was in Kavachio and
 *  typed their name; `recorded` means they signed on paper or elsewhere and the
 *  carrier entered the fact, so it is attributable to the person who recorded
 *  it and not to the signatory. Never show the two as the same thing. */
export type ContractSignature = {
  id: number;
  side: "carrier" | "counterparty";
  signer_name: string;
  signer_title: string | null;
  signer_email: string | null;
  method: "typed" | "recorded";
  by_user_id: number | null;
  signed_at: string | null;
  document_id: number | null;
  note: string | null;
};

export type SignatureInput = {
  signer_name: string;
  signer_title?: string | null;
  signer_email?: string | null;
  /** Only meaningful when recording the other side's. */
  side?: "carrier" | "counterparty";
  /** The carrier entering a signature made outside Kavachio. */
  recorded?: boolean;
  document_id?: number | null;
  note?: string | null;
};

/** Sign, or record a signature made elsewhere. The second one normally puts
 *  the contract in force — read `lifecycle` on what comes back rather than
 *  assuming it did. */
export const signContract = (id: number, body: SignatureInput) =>
  api.post<ContractRecord & { signature_note?: string }>(
    `/contracts/${id}/sign`, body).then(r => r.data);

/** Withdraw one given in error. Refused once the contract is in force. */
export const unsignContract = (id: number, signatureId: number) =>
  api.delete<ContractRecord>(`/contracts/${id}/sign/${signatureId}`)
     .then(r => r.data);

export type Counterparty = { id: number; name: string; party_type: string };

export type ContractInput = {
  program_id: number;
  contract_type: string;
  name?: string | null;
  counterparty_party_id?: number | null;
  schedule_key?: string | null;
  output_template_id?: number | null;
  inception_dt?: string | null;
  expiry_dt?: string | null;
  umr?: string | null;
  class_of_business?: string | null;
  year_of_account?: string | null;
  earnings_pattern?: string | null;
  executed_date?: string | null;
  notice_period_days?: number | null;
  premium_cap_amount?: number | null;
  premium_cap_currency?: string | null;
  /** The contract this one renews, when it is a renewal. It becomes a NEW
   *  contract pointing back — last year's is left exactly as it is. */
  renews_contract_id?: number | null;
  /** The authored contract: what was agreed, and the wording written from it. */
  agreed_limits?: AgreedLimits;
  wording_sections?: WordingSection[];
  signature_layout?: SignatureLayout | null;
  signers?: Signer[];
  /** Start a negotiation instead of putting it straight in force. Default
   *  false, so the carrier's "what I raise is live on arrival" behaviour is
   *  unchanged — this is a choice for when there is something to agree. */
  send_for_review?: boolean;
  /** What the contract should BE when created:
   *    "draft"   written down, not live, nothing checked, still editable
   *    "review"  out to the broker to agree
   *  Supersedes `send_for_review`, which only offered two of the three. */
  //    "draft"   written down, nothing more
  //    "review"  sent to the broker to agree or push back
  // There is no "live". A contract goes in force because both sides signed it.
  create_as?: "draft" | "review";
};

/** The server's per-field complaint, so the form can mark the actual inputs
 *  rather than printing one sentence above all of them. */
export type FieldErrors = Record<string, string>;

export function fieldErrors(e: unknown): { message: string; errors: FieldErrors } {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (d && typeof d === "object" && "errors" in (d as object)) {
    const det = d as { message?: string; errors?: FieldErrors };
    return { message: det.message ?? "That could not be saved.", errors: det.errors ?? {} };
  }
  return {
    message: typeof d === "string" ? d : "That could not be saved.",
    errors: {},
  };
}

/** One row of step 1's table: a question in plain words, the answer, and what
 *  happens when a file breaks it. `checkable` false means the term is real and
 *  worth recording but has nothing in a spreadsheet to measure it against, so
 *  it gets no severity control and produces no check. */
export type AgreedLimitSpec = {
  name: string;
  question: string;
  sub: string;
  kind: "percent" | "money" | "int" | "choice" | "text";
  unit: string | null;
  choices: string[] | null;
  group: "underwriting" | "commercial";
  checkable: boolean;
  /** A key from `severities` — see SeveritySpec. Deliberately not a union of
   *  literals: the list of severities is served, and spelling it out here made
   *  the client the second place that decided what one could be. */
  default_severity: string | null;
  /** Offered before "show every term". Served rather than decided here: the
   *  form used to hold its own list, so a limit added to the vocabulary existed
   *  everywhere except the screen somebody would have typed it on. */
  common: boolean;
};

/** What the carrier agreed, as the flow holds it. */
export type AgreedLimits = Record<string, {
  value: string | number;
  /** A key from `severities` — see SeveritySpec. */
  severity?: string | null;
}>;

/** A section of the wording. `body` holds TOKENS ({{commission_max_pct}});
 *  `rendered` is the same text with the live values in. The editor keeps the
 *  body — that is what makes a chip follow the term it came from. */
export type WordingSection = {
  key: string;
  title: string;
  body: string;
  origin: string;
  locked?: boolean;
  rendered?: string;
  tokens?: string[];
};

/** One person who signs. `side` is which organisation they sign for. */
export type Signer = {
  name: string;
  email: string;
  role?: string;
  side: "carrier" | "counterparty";
  /** Whether this person is given a way to sign IN Kavachio — their own boxes
   *  on the page and a link to fill them. Somebody from outside is often named
   *  on a contract without ever being let into the system, and false means
   *  exactly that: their lines are printed, and they sign the paper copy.
   *  Missing means yes, which is what every contract written before the
   *  question existed meant. */
  access?: boolean;
};

export type WordingPreview = {
  sections: WordingSection[];
  tokens: Record<string, string>;
  checks: Array<{ from: string; expression: string; title: string;
                  severity: "critical" | "warning"; detail: string }>;
  warnings: Array<{ title: string; detail: string }>;
  uncheckable: Array<{ key: string; title: string }>;
  pages: number;
};

export type WordingInput = {
  contract_type?: string;
  values: Record<string, unknown>;
  agreed_limits: AgreedLimits;
  sections?: WordingSection[];
  carrier_name?: string | null;
  counterparty_name?: string | null;
  programme_name?: string | null;
  signature_layout?: SignatureLayout | null;
};

/** The three headings the limits table is grouped under, served so neither the
 *  create screen nor the contract record restates them. */
export type LimitGroup = { key: string; label: string; sub: string };

/** One line a signature block can carry. `fixed` marks the one that cannot be
 *  turned off — a block with nowhere to sign is not a signature block. */
export type SignatureFieldSpec = {
  key: string; label: string; hint: string;
  default_on: boolean; fixed: boolean;
};

/** How much of this contract is measured on every row.
 *
 *  `checkable` counts the agreed limits that CAN become a check; a limit whose
 *  check is None is wording only by design and is not counted, so a fully bound
 *  contract reads n of n rather than n of some larger number it can never
 *  reach. */
export type ContractChecks = {
  rules: number;
  checkable: number;
  output_template_id: number | null;
  bindable: boolean;
  /** The template the checks were written against, and the sheets it reports
   *  on. Shown because nobody was asked: binding resolves the template itself,
   *  so a carrier can be measured on a layout for a country they do not write
   *  in and have no way to notice. Empty on the contracts LIST, which resolves
   *  nothing per row. */
  output_template: string | null;
  sheets: string[];
};

/** What a signature block may contain and how the two blocks sit on the page.
 *
 *  Served, never restated here. The form offers exactly what the wording
 *  builder can draw and the server will accept, because all three read one
 *  list — esign_pdf.SIGNATURE_BLOCK_FIELDS. Add a line there and it appears
 *  here with no frontend change at all. */
export type SignatureBlockSpec = {
  fields: SignatureFieldSpec[];
  arrangements: Array<{ key: string; label: string; hint: string }>;
  sides: string[];
  default: SignatureLayout;
  /** The size of a hand-placed block as a fraction of the page. Served so the
   *  box somebody drags is the size of the block that gets drawn. */
  placed_block: { width: number; height: number };
};

/** What the carrier chose: which lines each side signs, and how the blocks
 *  sit. Stored on the contract and read by everything that draws the page. */
export type SignatureLayout = {
  arrangement: string;
  fields: Record<string, string[]>;
  /** Where each side's block was dragged to, when it was placed by hand: a
   *  page number and the top-left corner as a fraction of that page. Kept
   *  whatever the arrangement is, so trying the automatic ones and coming back
   *  does not throw a placement away. */
  blocks: Record<string, { page: number; x: number; y: number }>;
};

/** How hard a check bites, and the words for it — served, never typed here.
 *
 *  `key` is what is stored; `label` is the name a person picks ("Critical");
 *  `action` says what actually happens to a row that breaks it ("Stops the
 *  row"). Renaming a severity is a change to SEVERITY_VOCAB on the server and
 *  nothing else — the form, the summary badge and the sample contract all read
 *  the same two words. */
export type SeveritySpec = {
  key: string; label: string; action: string; hint: string;
};

export const getContractTypes = () =>
  api.get<{
    types: ContractTypeSpec[]; default: string; lifecycle: Lifecycle[];
    agreed_limits: AgreedLimitSpec[]; limit_groups: LimitGroup[];
    /** The term lengths a form may offer, and how a term is counted. Nothing
     *  about a duration is stored — see utils/term.ts. */
    term: TermSpec;
    signature_block: SignatureBlockSpec;
    severities: SeveritySpec[];
  }>("/contract-types").then(r => r.data);

/** Steps 2 and 3, computed without writing anything. Called as the terms
 *  change: same terms, same document — no model is involved. */
export const previewWording = (body: WordingInput) =>
  api.post<WordingPreview>("/contract-wording/preview", body).then(r => r.data);

/** Download the draft before anything exists, so "nothing has been sent yet"
 *  stays true while somebody takes it to a colleague. */
export async function downloadDraft(body: WordingInput): Promise<void> {
  const r = await api.post("/contract-wording/draft", body,
                           { responseType: "blob" });
  const url = URL.createObjectURL(r.data as Blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${(body.values.name as string) || "contract"} (draft).pdf`;
  a.click();
  URL.revokeObjectURL(url);
}

/**
 * The WHOLE contract as a PDF — schedule, wording, signature page.
 *
 * A contract is not a document in this system. It is its terms and the wording
 * written from them, both held as data and read on screen, where they are
 * live: change a limit and the schedule, the sentence quoting it and the check
 * behind it all move together. This is for the one thing a screen cannot do,
 * which is be sent, printed or signed. The file that comes back is a different
 * artefact from what is on screen — fixed, paginated, and correct only as at
 * the moment it was asked for.
 *
 * Composed on the server each time and stored nowhere, so it never becomes a
 * second version of the contract that stops following its terms.
 */
export async function downloadContractPdf(
  id: number, name?: string | null,
): Promise<void> {
  const r = await api.get(`/contracts/${id}/contract.pdf`,
                          { responseType: "blob" });
  const url = URL.createObjectURL(r.data as Blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${(name || "contract").trim()}.pdf`;
  a.click();
  URL.revokeObjectURL(url);
}

/** Who a contract of this type may be written with. Narrowed to the programme
 *  for brokers, because a broker not on it cannot hold a contract on it. */
export const getCounterparties = (partyType: string, programId?: number) =>
  api.get<Counterparty[]>("/counterparties", {
    params: { party_type: partyType, program_id: programId },
  }).then(r => r.data);

export type ContractFilters = {
  program_id?: number;
  counterparty_id?: number;
  contract_type?: string;
  lifecycle?: string;
  q?: string;
};

export const listContracts = (filters: ContractFilters = {}) =>
  api.get<ContractRecord[]>("/contracts", { params: filters }).then(r => r.data);

export const getContract = (id: number) =>
  api.get<ContractRecord>(`/contracts/${id}`).then(r => r.data);

export const createContract = (body: ContractInput) =>
  api.post<ContractRecord>("/contracts", body).then(r => r.data);

/** Correct a contract that is still a draft, still pending, or back with the
 *  carrier after a change request. Everything is fair game in those states —
 *  the identity fields, the agreed limits and the wording — because a contract
 *  nobody has agreed to yet has nothing to protect. Absent keys are left
 *  alone, so saving one part never wipes another. */
export const updateContract = (
  id: number,
  // Every part of a draft: its identity, what was agreed, the words, and who
  // signs. Absent means unchanged, so saving one part never blanks another.
  body: Partial<ContractInput> & {
    agreed_limits?: AgreedLimits;
    wording_sections?: WordingSection[];
    signers?: Signer[];
    signature_layout?: SignatureLayout | null;
  },
  // `wording_retied` names the terms a saved clause was tied back to: a figure
  // typed where a chip used to be is bound to the term it quotes, or the
  // sentence stops moving when the term does. Never silent — it changed the
  // text of a contract.
) => api.patch<ContractRecord & { wording_retied?: string[] }>(
  `/contracts/${id}`, body).then(r => r.data);

export const activateContract = (id: number) =>
  api.post<ContractRecord>(`/contracts/${id}/activate`).then(r => r.data);

// ── negotiation ────────────────────────────────────────────────────────────
// The carrier proposes terms, the broker answers, and the two loop until they
// agree. Nobody approves anything here — see Lifecycle above for why that is a
// different thing from the approve/reject gate.

/** Carrier: put the terms out to the broker. Also how they are re-sent after
 *  revising in answer to a change request — the same act, so the same call. */
export const sendForReview = (id: number, note?: string) =>
  api.post<ContractRecord>(`/contracts/${id}/send-for-review`,
                           { note: note ?? null }).then(r => r.data);

/** Carrier: agree the terms alone and go straight to signing.
 *
 *  Lands in the same state the broker's agreement does, and is recorded
 *  differently on purpose — the history has to be able to say which of the two
 *  happened, because one of them means the other side never got a say. */
export const skipReview = (id: number, note?: string) =>
  api.post<ContractRecord>(`/contracts/${id}/skip-review`,
                           { note: note ?? null }).then(r => r.data);

/** Broker: push back.
 *
 *  Something has to be said — the carrier can only answer what it can read —
 *  but either form counts: prose, or a named term carrying the value wanted.
 *  A named term is the more useful of the two, since it lets the carrier see
 *  the request beside the current terms and apply it in one move. The server
 *  enforces the same rule; saying nothing at all is what it refuses. */
export const requestChanges = (
  id: number, note: string, changes: ProposedChange[] = [],
) => api.post<ContractRecord>(`/contracts/${id}/request-changes`,
                              { note, changes }).then(r => r.data);

/** Broker: agree the terms. NOT the signature and NOT going live — both of
 *  those are separate acts, done separately, because they are. */
export const acceptTerms = (id: number, note?: string) =>
  api.post<ContractRecord>(`/contracts/${id}/accept-terms`,
                           { note: note ?? null }).then(r => r.data);

/**
 * Broker: sign and return the contract to the carrier. Step 5.
 *
 * Records two facts — the execution date, and WHICH attachment is the signed
 * copy rather than a draft. What follows is the carrier's PLACEMENT, which
 * Kavachio does not do yet; until it does, the carrier goes from here straight
 * to putting the contract in force.
 */
export const submitSigned = (id: number, body: {
  document_id?: number | null;
  executed_date?: string | null;
  note?: string | null;
} = {}) => api.post<ContractRecord>(`/contracts/${id}/submit-signed`, body)
             .then(r => r.data);

export const terminateContract = (
  id: number, reason: string, terminatedDate?: string | null,
) =>
  api.post<ContractRecord>(`/contracts/${id}/terminate`, {
    reason, terminated_date: terminatedDate ?? null,
  }).then(r => r.data);

/** Renew into a SUCCESSOR. A new contract that points back at this one — never
 *  new dates on the old row, whose term has to keep meaning what it meant while
 *  bordereaux were checked against it. */
export const renewContract = (id: number, body: {
  inception_dt: string; expiry_dt: string; name?: string;
  premium_cap_amount?: number | null; notice_period_days?: number | null;
  year_of_account?: string | null; schedule_key?: string | null;
}) => api.post<ContractRecord>(`/contracts/${id}/renew`, body).then(r => r.data);

export type DocumentsPayload = {
  documents: ContractDocument[];
  missing_references: string[];
  /** What the wording ASKED for, supplied or not — so the screen can show the
   *  request beside the answer. */
  external_references: Array<{ document_name?: string; version_or_date?: string }>;
};

export const getDocuments = (id: number) =>
  api.get<DocumentsPayload>(`/contracts/${id}/documents`).then(r => r.data);

export const uploadDocument = (
  id: number,
  file: File,
  opts: {
    kind: ContractDocumentKind;
    satisfiesReference?: string | null;
    effectiveFrom?: string | null;
    /** Mark this as the SIGNED copy rather than a draft. */
    isExecutedCopy?: boolean;
  },
) => {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("kind", opts.kind);
  if (opts.satisfiesReference) fd.append("satisfies_reference", opts.satisfiesReference);
  if (opts.effectiveFrom) fd.append("effective_from", opts.effectiveFrom);
  if (opts.isExecutedCopy) fd.append("is_executed_copy", "true");
  return api.post<{
    document: ContractDocument;
    missing_references: string[];
    /** The rules on file were generated without this document. */
    rules_stale: boolean;
  }>(`/contracts/${id}/documents`, fd).then(r => r.data);
};

/** Retire a document — never delete it. The rules in force were generated from
 *  these, and a deleted one leaves rules nobody can explain. */
export const deactivateDocument = (id: number, documentId: number) =>
  api.delete<{ document: ContractDocument; missing_references: string[];
               rules_stale: boolean }>(
    `/contracts/${id}/documents/${documentId}`).then(r => r.data);

/**
 * Fetch one attached document.
 *
 * Through axios, NOT as a plain link. The download route is authenticated like
 * every other, and a bare `<a href>` sends no Authorization header — which is
 * why the old `documentDownloadUrl` helper produced a link that always came
 * back "missing bearer token". Going through the client means the interceptor
 * attaches the token (and refreshes it) exactly as it does everywhere else.
 */
async function fetchDocument(id: number, documentId: number): Promise<Blob> {
  const r = await api.get(`/contracts/${id}/documents/${documentId}/download`,
                          { responseType: "blob" });
  return r.data as Blob;
}

/** Save it to disk. */
export async function downloadDocument(
  id: number, documentId: number, filename?: string | null,
): Promise<void> {
  const blob = await fetchDocument(id, documentId);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "document";
  a.click();
  URL.revokeObjectURL(url);
}

/** An object URL for showing it in the page. The caller must revoke it when
 *  the viewer closes, or every open leaks the whole file. */
export async function openDocument(
  id: number, documentId: number,
): Promise<{ url: string; type: string }> {
  const blob = await fetchDocument(id, documentId);
  return { url: URL.createObjectURL(blob), type: blob.type };
}

/**
 * Re-read the contract from its ACTIVE documents and rebuild its rules.
 *
 * Long: a full contract is 75+ sequential model calls, so the server streams
 * whitespace to hold the connection open and the real payload arrives as the
 * final chunk. Failures come back as HTTP 200 with {success:false} in the body
 * — the client interceptor turns that into a thrown error, so a plain await is
 * enough here.
 */
/**
 * Turn this contract's agreed terms into the checks that run on every row.
 *
 * The same step the broker page runs when a contract is UPLOADED there, for a
 * contract written here instead: uploading has to read a document for its
 * clauses first, whereas a written contract already carries its terms, so the
 * rules follow by translation. The output template is resolved by the server
 * from the contract's own scope — the screen never picks one, because every
 * run resolves it the same way and two answers to that question is how a
 * bordereau gets checked against a template nobody reports into.
 */
/** How many pages the composed contract has, and the shape of each one.
 *
 *  Only the placement screen needs this: a hand-placed signature block is a
 *  page number and a point on it, so the screen has to be looking at the same
 *  pages the PDF has. */
export const getContractPages = (id: number) =>
  api.get<{ pages: number; sizes: Array<{ width: number; height: number }> }>(
    `/contracts/${id}/pages`).then(r => r.data);

/** One page of the composed contract as an object URL the caller must revoke.
 *
 *  Fetched rather than pointed at with an <img src>, because every call to this
 *  API carries a bearer token and an <img> cannot set a header. */
export const getContractPageImage = async (
  id: number, page: number, scale = 1.5,
): Promise<string> => {
  const r = await api.get<Blob>(`/contracts/${id}/pages/${page}`, {
    params: { scale }, responseType: "blob", silent: true,
  });
  return URL.createObjectURL(r.data);
};

export const bindChecks = (id: number) =>
  api.post<ContractRecord & {
    mapping: {
      output_template: { id: number; name: string };
      rules_written: number;
      /** Terms this template has no column for. Not a failure — the things
       *  this contract says that this bordereau cannot measure. */
      unmapped: Array<{ key: string; question: string; value: string;
                        reason: string; looked_for: string[] }>;
      match_level: "contract" | "broker" | "programme" | null;
    };
  }>(`/contracts/${id}/bind-checks`).then(r => r.data);

export const generateRules = (id: number, outputTemplateId?: number | null) => {
  const fd = new FormData();
  if (outputTemplateId != null) fd.append("output_template_id", String(outputTemplateId));
  return api.post<{
    success: boolean;
    contract: ContractRecord;
    counts: Record<string, number> | null;
    endorsements_applied: string[];
    references_applied: string[];
  }>(`/contracts/${id}/generate-rules`, fd).then(r => r.data);
};


// ── endorsements: amending a contract that is already running ──────────────
// A mid-term change keeps the contract, its id and every bordereau already
// checked against it. What moves is some of its terms, from a stated date —
// so this produces a document saying what changed, attached alongside the
// wording rather than replacing it.

/** One term that moved. `check_before`/`check_after` are the point: the
 *  carrier is changing what passes on a contract with files already running
 *  through it. */
export type EndorsementChange = {
  key: string;
  question: string;
  kind: "amended" | "added" | "removed";
  from: string;
  to: string;
  severity_from: string | null;
  severity_to: string | null;
  check_before: string | null;
  check_after: string | null;
};

export type EndorsementPreview = {
  number: number;
  changes: EndorsementChange[];
  sections: WordingSection[];
  current_limits: AgreedLimits;
  contract: { id: number; name: string; inception_dt: string | null;
              expiry_dt: string | null };
};

export type EndorsementInput = {
  agreed_limits: AgreedLimits;
  effective_from?: string | null;
  note?: string | null;
  sections?: WordingSection[];
};

/** What the change would say and which checks would move. Writes nothing. */
export const previewEndorsement = (id: number, body: EndorsementInput) =>
  api.post<EndorsementPreview>(`/contracts/${id}/endorsement/preview`, body)
     .then(r => r.data);

/** Endorse it: compose the document, attach it, and move the contract's live
 *  terms to the endorsed values. The rules are NOT regenerated — that discards
 *  the rules in force, so it stays an explicit action on the contract's page. */
export const createEndorsement = (id: number, body: EndorsementInput) =>
  api.post<ContractRecord & {
    endorsement: { number: number; document_id: number;
                   changes: EndorsementChange[]; effective_from: string | null };
    rules_stale: boolean;
  }>(`/contracts/${id}/endorsement`, body).then(r => r.data);
