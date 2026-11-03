/**
 * Create-a-Contract, step 4 — Signatures.
 *
 * ── WHERE A ROUND STARTS ───────────────────────────────────────────────────
 * On the contract, once both sides have agreed its terms — not from a screen
 * of its own and not from an email. `openInAppSigning(contractId)` opens one
 * (or resumes it) for whoever is logged in; the carrier signs first, and the
 * broker is emailed the document already carrying that signature.
 *
 *     const s = await openInAppSigning(contractId);
 *     // s.token / s.session   — the pair the signing page runs on
 *     // s.envelope_id         — track it on /contracts/signatures, reached
 *     //                          from Contracts and from the record itself
 *
 * ── WHO OWNS WHICH BOX ─────────────────────────────────────────────────────
 * Every field carries a `party_key`: `tenant:<tenant_id>` for the insurer,
 * `broker:<broker_party_id>` for the broker. The signing page uses it only to
 * grey out what is not yours — the server re-checks ownership on every write
 * and rejects a mismatch with 403, so nothing here is load-bearing for
 * security.
 */
import { api } from "./client";

export type Side = "insurer" | "broker";

export type FieldType = "signature" | "initial" | "name" | "title" | "date" | "text";

export type EsignField = {
  id: number;
  /** `tenant:<id>` or `broker:<id>` — who this box belongs to. */
  party_key: string;
  type: FieldType;
  page: number;                 // 1-based
  /** Fractions of the page (0..1, origin top-left), so a box lands in the same
   *  place whatever width the page is rendered at. */
  x: number; y: number; w: number; h: number;
  required: boolean;
  label: string | null;
  /** Whether the CALLER may fill it. The server decides this, not the page. */
  mine: boolean;
  value: string | null;
  filled: boolean;
  owner_name: string | null;
  owner_org: string | null;
  owner_side: Side | null;
};

export type EsignRecipient = {
  id: number;
  side: Side;
  party_key: string;
  tenant_id: number | null;
  broker_party_id: number | null;
  name: string;
  email: string;
  title: string | null;
  org: string | null;
  order: number;
  status: "pending" | "sent" | "viewed" | "signed" | "declined";
  sent_at: string | null;
  viewed_at: string | null;
  signed_at: string | null;
  decline_reason: string | null;
  signed_ip: string | null;
  /** Only returned to the carrier that owns the envelope — the "copy the link"
   *  affordance for a signer whose email bounced. */
  link?: string;
  token_expires?: string;
};

export type EsignEvent = {
  type: string; at: string; actor: string | null; ip: string | null;
  detail: Record<string, unknown> | null;
};

export type Envelope = {
  id: number;
  title: string;
  status: "draft" | "sent" | "in_progress" | "completed" | "declined" | "voided";
  contract_id: number | null;
  program_id: number | null;
  broker_party_id: number | null;
  page_count: number;
  pdf_version: number;
  created_at: string | null;
  sent_at: string | null;
  completed_at: string | null;
  /** Whose turn it is, or null when everybody has signed. */
  waiting_on: EsignRecipient | null;
  recipients: EsignRecipient[];
  fields: EsignField[];
  events?: EsignEvent[];
  /** Set by /send: whether the email actually went out. */
  emailed?: boolean;
  emailed_to?: string | null;
  email_error?: string;
};

/* Starting a round has no client function here on purpose.
 *
 * It used to: `sendContractForSignature()` was the seam a step-3 screen called
 * to build a round and email the insurer. That seam is gone. A round is opened
 * from the CONTRACT, by the person signing it, through
 * `openInAppSigning()` below — which starts one if none is running and refuses
 * while the terms are still being negotiated. Keeping a second way in would be
 * keeping a way to open a round on terms nobody had agreed, and a way to have
 * two open on one contract.
 */

/** One PAGE of rounds, newest first, plus how many there are in all. */
export type EnvelopePage = {
  envelopes: Envelope[];
  total: number;
  limit: number;
  offset: number;
};

/** Rounds this carrier has out, newest first.
 *
 *  Every filter is applied on the SERVER, and so is the paging. The response
 *  is one page: filtering it in the browser would search the page that
 *  happened to be loaded and report the rest of the archive as missing. */
export async function listEnvelopes(
  opts: { status?: string; contractId?: number; q?: string;
          limit?: number; offset?: number } = {},
): Promise<EnvelopePage> {
  const params: Record<string, string | number> = {};
  if (opts.status) params.status = opts.status;
  if (opts.contractId) params.contract_id = opts.contractId;
  if (opts.q?.trim()) params.q = opts.q.trim();
  if (opts.limit) params.limit = opts.limit;
  if (opts.offset) params.offset = opts.offset;
  const { data } = await api.get<EnvelopePage>("/esign/envelopes",
    { params: Object.keys(params).length ? params : undefined });
  return data;
}

export async function getEnvelope(id: number): Promise<Envelope> {
  const { data } = await api.get<Envelope>(`/esign/envelopes/${id}`);
  return data;
}

export async function remindEnvelope(id: number): Promise<{ ok: boolean; reminded: string }> {
  const { data } = await api.post(`/esign/envelopes/${id}/remind`);
  return data;
}

export async function voidEnvelope(id: number): Promise<Envelope> {
  const { data } = await api.post<Envelope>(`/esign/envelopes/${id}/void`);
  return data;
}

// ── the signing side ────────────────────────────────────────────────────────
// These take the emailed token instead of a Bearer header: the signer has no
// account, and is not asked to make one.

export type SigningView = {
  envelope: {
    id: number; title: string; status: Envelope["status"];
    page_count: number; pdf_version: number;
    pages: { width: number; height: number }[];
    programme: string; term: string;
  };
  me: {
    id: number; name: string; email: string; title: string | null;
    org: string | null; side: Side;
    /** Shown back to the signer, deliberately: it is the answer to "why are
     *  those boxes mine and these ones not?". */
    party_key: string;
    status: EsignRecipient["status"];
    /** Where in the queue this signer is. Served, because a round may run to
     *  three or four people and the page cannot work it out. */
    order: number;
    my_turn: boolean;
    signature_name: string;
  };
  others: { name: string; org: string | null; side: Side; status: string;
            party_key: string; signed_at: string | null; order: number }[];
  already_signed: { name: string; org: string | null; signed_at: string }[];
  fields: EsignField[];
};

/** What the server returns before the one-time code has been entered.
 *  Deliberately almost nothing: everything here is visible to whoever holds the
 *  URL, and the point of the code is that holding the URL is not enough. */
export type LockedView = {
  locked: true;
  /** `m****o@crcinsurisk.com` — enough to know which inbox to look in. */
  email_hint: string;
  attempts_left: number;
  lockout_seconds: number;
  can_resend: boolean;
};

export type OpenResult = LockedView | (SigningView & { locked: false });

export function isLocked(v: OpenResult): v is LockedView {
  return v.locked === true;
}

/** The unlock session, held in memory for the life of the page.
 *
 *  NOT localStorage. It is a bearer credential for a contract, and a shared or
 *  borrowed machine should not still be able to open it after the tab closes.
 *  A reload asks for the code again, which is the correct trade for something
 *  that unlocks a signature. */
let unlockSession = "";

export function setUnlockSession(s: string) { unlockSession = s; }
export function clearUnlockSession() { unlockSession = ""; }
export function hasUnlockSession(): boolean { return unlockSession.length > 0; }

/** JSON calls carry the session as a header. */
function unlockHeaders(): Record<string, string> {
  return unlockSession ? { "X-Esign-Session": unlockSession } : {};
}

export async function openForSigning(token: string): Promise<OpenResult> {
  const { data } = await api.get<OpenResult>(`/esign/sign/${token}`,
    { headers: unlockHeaders() });
  return data;
}

/* ── the in-app door ──────────────────────────────────────────────────────
 *
 * Signing no longer starts with an email. Once both sides have agreed the
 * terms, the carrier presses Sign on the contract and signs it in a new tab —
 * no message, no one-time code — because they are already logged in, and a
 * session this app minted says more about who somebody is than a link that
 * arrived in an inbox. The broker is emailed when the carrier has actually
 * signed, and can sign here instead if they would rather.
 *
 * What comes back is the same pair the emailed door produces: a link token and
 * an unlock session on it. Everything past this point is one code path.
 */

export type InAppSession = {
  token: string;
  session: string;
  envelope_id: number;
  status: string;
};

/** Where the signing page lives for a contract. The token is deliberately not
 *  in it: the page fetches its own, so the URL in the address bar is never
 *  something worth forwarding. */
export function inAppSigningUrl(contractId: number): string {
  return `/sign?contract=${contractId}`;
}

/** Open (or resume) this contract's signing round as whoever is logged in.
 *
 *  Starts the round when the carrier asks and none is running; never for the
 *  broker, who is told the carrier has not sent it yet. The session is stored
 *  the same way a typed code's is, so every later call carries it. */
export async function openInAppSigning(contractId: number): Promise<InAppSession> {
  const { data } = await api.post<InAppSession>(
    `/esign/contracts/${contractId}/signing-session`);
  setUnlockSession(data.session);
  return data;
}

/** Where this contract's signing round has got to — whether one is running,
 *  whether it is your move, and who it is waiting on when it is not.
 *
 *  Answered by the server so the button and the endpoint behind it cannot
 *  disagree about whether pressing it will work. */
export type ContractRound = {
  envelope_id: number | null;
  status: string | null;
  started: boolean;
  can_sign: boolean;
  my_turn: boolean;
  i_have_signed: boolean;
  waiting_on: "insurer" | "broker" | "carrier" | null;
  waiting_on_name: string | null;
  why: string | null;
};

export async function getContractRound(contractId: number): Promise<ContractRound> {
  const { data } = await api.get<ContractRound>(
    `/esign/contracts/${contractId}/round`);
  return data;
}

/** What is known about an address the carrier has just typed into the
 *  signatory list.
 *
 *  Answered by the SERVER and only about the two organisations already on this
 *  contract, so it says "we know them" or "we do not" and can never be used to
 *  go fishing for who else holds an account here. `known: false` is the answer
 *  that matters: this person is from outside, and the carrier has to say
 *  whether they are being let in to sign or only printed on the page. */
export type SignerLookup = {
  email: string;
  known: boolean;
  name: string | null;
  role: string | null;
  org: string | null;
  side: "carrier" | "counterparty" | null;
  note: string;
};

export async function lookupSigner(contractId: number, email: string):
  Promise<SignerLookup> {
  const { data } = await api.get<SignerLookup>(
    `/esign/contracts/${contractId}/signer-lookup`, { params: { email } });
  return data;
}

/** Enter the code from the email. On success the session is stored and every
 *  later call carries it automatically. */
export async function verifyCode(token: string, code: string):
  Promise<{ ok: boolean; expires_in: number }> {
  const { data } = await api.post<{ ok: boolean; session: string; expires_in: number }>(
    `/esign/sign/${token}/verify`, { code });
  setUnlockSession(data.session);
  return { ok: data.ok, expires_in: data.expires_in };
}

/** Email a fresh code. Does not re-issue the link — the URL they already have
 *  keeps working. */
export async function resendCode(token: string):
  Promise<{ ok: boolean; sent_to: string }> {
  const { data } = await api.post(`/esign/sign/${token}/resend-code`);
  return data;
}

/** Page images and the PDF are loaded by the browser, not by axios — an
 *  `<img src>` sets no headers — so those carry the session in the query
 *  string instead. Same value, same check on the server. */
function withSession(url: string): string {
  return unlockSession
    ? `${url}&session=${encodeURIComponent(unlockSession)}`
    : url;
}

export function signingPageUrl(token: string, page: number, version: number, scale = 2): string {
  const base = api.defaults.baseURL ?? "";
  return withSession(
    `${base}/esign/sign/${token}/pages/${page}?scale=${scale}&v=${version}`);
}

export function signingPdfUrl(token: string): string {
  return withSession(`${api.defaults.baseURL ?? ""}/esign/sign/${token}/pdf?dl=1`);
}

export async function submitSignature(token: string, body: {
  signature_name: string;
  /** A drawn signature as a data URL. Omitted, the typed name is used. */
  signature_image?: string | null;
  /** A drawn set of INITIALS, which is a different mark from the signature and
   *  goes only in the initials boxes. Omitted, those boxes carry the initials
   *  as text — never the signature image, which would put the two marks back
   *  together. */
  initials_image?: string | null;
  fields: { field_id: number; value: string }[];
  agreed: boolean;
}): Promise<{ ok: boolean; status: string; message: string }> {
  const { data } = await api.post(`/esign/sign/${token}`, body,
    { headers: unlockHeaders() });
  return data;
}

export async function declineSignature(token: string, reason: string):
  Promise<{ ok: boolean; status: string; message: string }> {
  const { data } = await api.post(`/esign/sign/${token}/decline`, { reason },
    { headers: unlockHeaders() });
  return data;
}

// ── small shared helpers ────────────────────────────────────────────────────

/** What a status means to a person, not to the database. */
export const ENVELOPE_STATUS: Record<Envelope["status"], { label: string; cls: string }> = {
  draft:       { label: "Not sent yet",   cls: "b-mut" },
  sent:        { label: "Out for signature", cls: "b-warn" },
  in_progress: { label: "Partly signed",  cls: "b-warn" },
  completed:   { label: "Fully signed",   cls: "b-ok" },
  declined:    { label: "Change asked for", cls: "b-crit" },
  voided:      { label: "Withdrawn",      cls: "b-mut" },
};

export const RECIPIENT_STATUS: Record<EsignRecipient["status"], { label: string; cls: string }> = {
  pending:  { label: "Waiting their turn", cls: "b-mut" },
  sent:     { label: "With them",          cls: "b-warn" },
  viewed:   { label: "Opened it",          cls: "b-info" },
  signed:   { label: "Signed",             cls: "b-ok" },
  declined: { label: "Declined",           cls: "b-crit" },
};

/** Which identifier a side is keyed by — the answer to the question the whole
 *  design turns on. Used in the UI to explain a locked box. */
/** What to call a side IN FRONT OF A SIGNER.
 *
 *  The signing page is public: it is opened from an emailed link by a broker
 *  who has no account here and no reason to see our primary keys. "tenant:10"
 *  tells them nothing, looks like a leaked internal detail, and quietly
 *  publishes how many carriers are on the platform. The role is what they
 *  actually need — it is what answers "are these boxes mine?" and what makes a
 *  wrongly-forwarded link obvious. */
export function sideLabel(side: Side | null | undefined): string {
  return side === "insurer" ? "the insurer"
       : side === "broker" ? "the broker"
       : "the other party";
}

/** The internal form, ids and all. Carrier-facing screens only — never the
 *  public signing page. */
export function keyExplains(partyKey: string): string {
  const [kind, id] = partyKey.split(":");
  return kind === "tenant"
    ? `the insurer, tenant_id ${id}`
    : `the broker, broker_party_id ${id}`;
}
