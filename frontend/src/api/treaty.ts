/**
 * Reinsurance treaties — Phase 26.
 *
 *   26.1  a treaty's structure: its type, layers, and each reinsurer's share
 *   26.2  the programmes it protects
 *
 * …and the reinsurer's private, read-only link. A reinsurer has no seat in
 * Kavachio, so the carrier sends one named person a link that opens only after
 * the one-time code emailed to the same address is typed.
 *
 * The form is built from `getTreatyTypes()` — which fields each type asks for,
 * and which are required — so this file restates none of it. The server is
 * treaty_types.py; its summary (headline, tower, split) is computed there too,
 * so every screen draws the same picture of the same treaty.
 */
import { api } from "./client";

// ── the spec ─────────────────────────────────────────────────────────────────
export type TreatyFieldKind = "money" | "pct" | "int" | "text" | "select"
  /** 27.2 — a sliding-scale or profit commission, as its own small structure. */
  | "sliding" | "profit";

export type TreatyField = {
  name: string;
  label: string;
  kind: TreatyFieldKind;
  hint?: string | null;
  example?: string | null;
  required: boolean;
  options?: { value: string; label: string }[];
  /** Shown, and required, only when another treaty-level field has a value —
   *  stop loss is measured as a loss ratio OR an amount, never both. */
  when?: { field: string; equals: string };
};

export type TreatyTypeSpec = {
  key: TreatyTypeKey;
  label: string;
  short: string;
  blurb: string;
  proportional: boolean;
  /** Only XOL stacks layers. Every other type has exactly one. */
  multi_layer: boolean;
  default_layer: string;
  fields: TreatyField[];
  layer_fields: TreatyField[];
};

export type TreatyTypeKey =
  "quota_share" | "surplus" | "xol" | "stop_loss" | "facultative";

// ── the structure ────────────────────────────────────────────────────────────
export type TreatyShare = {
  id?: number;
  reinsurer_party_id: number | null;
  reinsurer_name?: string | null;
  share_pct: number | string | null;
  is_lead?: boolean;
  slip_reference?: string | null;
};

export type TreatyLayer = {
  id?: number;
  name: string;
  terms: Record<string, unknown>;
  shares: TreatyShare[];
  /** Served on read only. */
  facts?: Fact[];
  placed_pct?: number;
};

export type TreatyInput = {
  treaty_type: TreatyTypeKey | null;
  terms: Record<string, unknown>;
  layers: TreatyLayer[];
  /** Absent leaves the programmes as they are. */
  program_ids?: number[];
};

export type TreatyStructure = TreatyInput & { updated_at?: string | null };

export type Fact = { label: string; value: string };

/** One band of a tower: the carrier's retention at the bottom, then each
 *  layer stacked by amount (or by loss-ratio points for a stop loss). */
export type TowerBand = {
  label: string;
  kind: "retention" | "layer";
  from: number;
  to: number;
  /** Index into the structure's layers. Absent on the retention band. */
  layer?: number;
  placed_pct: number | null;
};

export type TreatySummary = {
  treaty_type: TreatyTypeKey;
  type_label: string;
  type_short: string;
  proportional: boolean;
  currency: string | null;
  headline: string;
  facts: Fact[];
  /** Non-proportional types (and surplus): bands stacked by amount. */
  tower: { unit: "money" | "pct"; currency: string | null;
           bands: TowerBand[] } | null;
  /** Proportional types: ceded against retained. */
  split: { ceded_pct: number; retained_pct: number } | null;
  layers_placed: { name: string; placed_pct: number }[];
  /** The least-placed layer — a treaty is only as placed as its weakest layer. */
  placed_pct: number;
  fully_placed: boolean;
  warnings?: string[];
};

export type TreatyProgramme = { id: number; name: string; is_home: boolean };

/** What the contract record and list carry about a treaty. */
export type TreatyRecordSummary = {
  has_structure: boolean;
  treaty_type: TreatyTypeKey | null;
  type_label: string | null;
  headline: string | null;
  placed_pct: number | null;
  fully_placed: boolean;
  programmes: TreatyProgramme[];
  /** How many reinsurers on the panel have approved the treaty as it is now. */
  approvals: { approved: number; total: number; changes_requested: number;
               all_approved: boolean } | null;
} & Partial<TreatyStatuses> & { placement?: PlacementSummary | null };

// ── a master treaty: placement and each reinsurer's signature ───────────────
export type WrittenStatus = "unplaced" | "partially_placed" | "fully_placed" | "over_placed";
export type SignedStatus = "unsigned" | "partially_signed" | "fully_signed";

export type PlacementSummary = {
  written_status: WrittenStatus; written_label: string;
  signed_status: SignedStatus; signed_label: string;
  parties: number; parties_signed: number;
};

export type PartySignature = {
  id: number | null; signer_name: string; signer_title: string | null;
  signer_email: string | null; method: "link" | "esign" | "recorded"; method_label: string;
  signed_at: string | null;
  evidence: { document_id: number; filename: string | null; kind: string; has_file: boolean } | null;
  /** Its line has changed since it signed — the signature is for the old one. */
  stale: boolean;
};

export type PlacementLine = {
  party_id: number | null; name: string | null; share_pct: number; is_lead: boolean;
  slip_reference: string | null; signature: PartySignature | null;
  status: "signed" | "awaiting" | "line_changed";
};

export type PlacementLayer = {
  index: number; name: string; written_pct: number; signed_pct: number; retained_pct: number;
  written_status: WrittenStatus; written_label: string;
  signed_status: SignedStatus; signed_label: string;
  lines: PlacementLine[];
};

export type Placement = PlacementSummary & {
  has_structure: boolean; lead_party_id: number | null;
  wording: SignedWording | null;
  layers: PlacementLayer[];
  /** Each reinsurer's own documents (signed line slips), by party id. */
  evidence: Record<string, { document_id: number; filename: string | null; kind: string }[]>;
  /** Why each reinsurer cannot sign now — null when it can. */
  can_record: Record<string, string | null>;
};

export const getPlacement = (contractId: number) =>
  api.get<Placement>(`/contracts/${contractId}/placement`).then(r => r.data);

export const recordPartySignature = (contractId: number, partyId: number, body: {
  signer_name: string; signer_title?: string | null; signer_email?: string | null;
  document_id: number; note?: string | null;
}) => api.post<Placement>(`/contracts/${contractId}/placement/${partyId}/signature`, body)
  .then(r => r.data);

export const withdrawPartySignature = (contractId: number, partyId: number) =>
  api.delete<Placement>(`/contracts/${contractId}/placement/${partyId}/signature`)
    .then(r => r.data);

// ── 26.3: configured from its signed wording ────────────────────────────────
export type SignedWording = {
  source: "signed_copy" | "esigned"; filename: string | null;
  document_id?: number; envelope_id?: number; version?: string | null;
  uploaded_at?: string | null; uploaded_by?: string | null;
  validation: { status: string; label: string; pages: number };
};

/** Three statuses, never merged: an expired treaty stays expired however
 *  complete its documentation and configuration are. */
export type TreatyStatuses = {
  lifecycle: string;
  documentation: { status: "missing" | "received"; label: string };
  configuration: { status: "not_started" | "in_progress" | "pending_approval" | "approved";
                   label: string; terms_confirmed: boolean; setup: string | null };
  wording: SignedWording | null;
};

/** Where a term was read from in the signed wording. Keyed by path:
 *  "inception_dt", "terms.cession_pct", "layers.0.terms.limit", "layers.0.shares.1". */
export type TermSources = Record<string, { page: number; quote: string | null }>;

export type WordingReading = {
  name: string | null; inception_dt: string | null; expiry_dt: string | null;
  year_of_account: string | null; notice_period_days: number | null;
  executed_date: string | null; effective_date?: string | null;
  treaty: TreatyInput | null; unmatched_reinsurers: string[];
  sources: TermSources; pages: number; read_at: string;
  document: { filename: string | null; version?: string | null };
};

export type WordingReview = {
  confirmed_at: string; confirmed_by: string | null; effective_date: string | null;
  document: { filename: string | null; version?: string | null };
  sources: TermSources; changed: boolean;
};

export type WordingRecord = TreatyStatuses & {
  treaty: string; contract_id: number;
  structure: TreatyStructure | null;
  record: { name: string | null; inception_dt: string | null; expiry_dt: string | null;
            year_of_account: string | null; notice_period_days: number | null;
            executed_date: string | null };
  reading: WordingReading | null;
  review: WordingReview | null;
  editable_dates: boolean;
  notes?: string[];
};

export const getWording = (contractId: number) =>
  api.get<WordingRecord>(`/contracts/${contractId}/wording`).then(r => r.data);

export const readWording = (contractId: number) =>
  api.post<WordingRecord>(`/contracts/${contractId}/wording/read`).then(r => r.data);

/** Read the signed wording for its clauses and its rules — done when a signed
 *  treaty is uploaded; again here, or for one uploaded before that. */
export const readTreatyClauses = (contractId: number) =>
  api.post<{ clauses: number; rules: number }>(`/contracts/${contractId}/wording/clauses`)
    .then(r => r.data);

export const confirmWording = (contractId: number, body: {
  treaty: TreatyInput; sources: TermSources; effective_date?: string | null;
  name?: string | null; inception_dt?: string | null; expiry_dt?: string | null;
  year_of_account?: string | null; notice_period_days?: number | null;
}) => api.post<WordingRecord>(`/contracts/${contractId}/wording/confirm`, body).then(r => r.data);

/** One page of the signed wording, as an image URL (fetched with the session). */
export async function wordingPageUrl(contractId: number, page: number): Promise<string> {
  const r = await api.get<Blob>(`/contracts/${contractId}/wording/pages/${page}`,
                                { responseType: "blob" });
  return URL.createObjectURL(r.data);
}

/** One reinsurer's answer to the treaty. `stale` = it answered an earlier
 *  version; the treaty has changed since, so it has to answer again. */
export type ApprovalStatus = "approved" | "changes_requested" | "pending" | "stale";

export type ReinsurerApproval = {
  party_id: number;
  party_name: string | null;
  is_lead: boolean;
  status: ApprovalStatus;
  last_status: "approved" | "changes_requested" | null;
  signer_name: string | null;
  signer_title: string | null;
  note: string | null;
  at: string | null;
};

export type TreatyRecord = {
  contract_id: number;
  structure: TreatyStructure | null;
  summary: TreatySummary | null;
  programmes: TreatyProgramme[];
  /** The contract's counterparty — the lead reinsurer. */
  lead_reinsurer_id: number | null;
  can_edit: boolean;
  can_edit_programmes: boolean;
  edit_blocked_reason: string | null;
};

export type WordingSectionOut = {
  key: string; title: string; body: string; origin?: string; locked?: boolean;
};

export const getTreatyTypes = () =>
  api.get<{ types: TreatyTypeSpec[] }>("/treaty-types").then(r => r.data.types);

/** Summary, tower and clauses for a structure still being typed. Lenient: a
 *  half-filled form previews what it has. `errors` is what a save would say. */
export const previewTreaty = (body: TreatyInput) =>
  api.post<{ summary: TreatySummary | null; sections: WordingSectionOut[];
             errors: Record<string, string> }>(
    "/treaty/preview", body, { silent: true }).then(r => r.data);

export const getTreaty = (contractId: number) =>
  api.get<TreatyRecord>(`/contracts/${contractId}/treaty`).then(r => r.data);

export const saveTreaty = (contractId: number, body: TreatyInput) =>
  api.put<TreatyRecord>(`/contracts/${contractId}/treaty`, body).then(r => r.data);

export const saveTreatyProgrammes = (contractId: number, programIds: number[]) =>
  api.put<TreatyRecord>(`/contracts/${contractId}/treaty/programmes`,
    { program_ids: programIds }).then(r => r.data);

// ── programmes (26.2) ────────────────────────────────────────────────────────
export type ProgrammeTreaty = {
  contract_id: number;
  name: string;
  lifecycle: string;
  lead_reinsurer: string | null;
  inception_dt: string | null;
  expiry_dt: string | null;
  /** The programme the treaty was raised on — it cannot be taken off it. */
  is_home: boolean;
  summary: TreatySummary | null;
};

export type ProgrammeTreaties = {
  programme: { id: number; name: string };
  treaties: ProgrammeTreaty[];
  live_count: number;
  attachable: { contract_id: number; name: string; type_label: string | null;
                lifecycle: string }[];
};

export const getProgrammeTreaties = (programId: number) =>
  api.get<ProgrammeTreaties>(`/programs/${programId}/treaties`).then(r => r.data);

export const attachTreaty = (programId: number, contractId: number) =>
  api.post<ProgrammeTreaties>(`/programs/${programId}/treaties`,
    { contract_id: contractId }).then(r => r.data);

export const detachTreaty = (programId: number, contractId: number) =>
  api.delete<ProgrammeTreaties>(
    `/programs/${programId}/treaties/${contractId}`).then(r => r.data);

/** Live treaties per programme id, for the Programmes list. */
/** Per programme id: its live treaties, and how many of them have their
 *  reinsurance bordereau setup in use. */
export const getTreatyCounts = () =>
  api.get<{ counts: Record<string, number>; in_use?: Record<string, number> }>(
    "/treaty-counts", { silent: true })
    .then(r => ({ counts: r.data.counts, inUse: r.data.in_use ?? {} }));

// ── the reinsurer's private link — carrier side ──────────────────────────────
export type AccessLinkStatus = "active" | "revoked" | "expired" | "closed";

export type AccessLink = {
  id: number;
  party_id: number;
  party_name: string | null;
  contact_id: number | null;
  name: string;
  email: string;
  status: AccessLinkStatus;
  status_title: string | null;
  /** Too many wrong codes — locked for a few minutes. */
  locked: boolean;
  sent_at: string | null;
  first_opened_at: string | null;
  last_viewed_at: string | null;
  expires_at: string | null;
  /** The last day it works, as a date — show this rather than expires_at. */
  expires_on: string | null;
  revoked_at: string | null;
  /** Asked to sign the treaty on this link (the lead reinsurer only). */
  sign_requested_at?: string | null;
  /** When they signed it there. */
  signed_at?: string | null;
};

export type AccessEventType =
  "sent" | "code_sent" | "code_failed" | "locked" | "unlocked" | "viewed"
  | "downloaded" | "revoked" | "refused" | "approved" | "changes_requested"
  | "sign_requested" | "signed";

export type AccessEvent = {
  id: number;
  type: AccessEventType;
  at: string;
  link_id: number;
  who: string | null;
  party_name: string | null;
  actor: string | null;
  ip: string | null;
  detail: Record<string, unknown> | null;
};

export type ShareableParty = {
  party_id: number;
  party_name: string | null;
  is_lead_reinsurer: boolean;
  contacts: { id: number; name: string | null; title: string | null;
              email: string | null }[];
};

export type ReinsurerAccess = {
  approvals: { parties: ReinsurerApproval[]; approved: number; total: number;
               changes_requested: number; all_approved: boolean };
  /** Whether the lead reinsurer can be asked to sign on its link now. */
  signing?: { lead_party_id: number | null; can_request: boolean;
              why_not: string | null; carrier_signed: boolean;
              counterparty_signed: boolean };
  can_share: boolean;
  why_not: string | null;
  links: AccessLink[];
  events: AccessEvent[];
  contacts: ShareableParty[];
};

export const getReinsurerAccess = (contractId: number) =>
  api.get<ReinsurerAccess>(`/contracts/${contractId}/reinsurer-access`)
    .then(r => r.data);

/** `sign` asks them to SIGN the treaty on the link — each reinsurer signs for
 *  its own lines, once the carrier has signed. */
export const shareWithReinsurers = (contractId: number, contactIds: number[],
                                    sign = false) =>
  api.post<ReinsurerAccess & {
    sent: { link_id: number; name: string; email: string; emailed: boolean }[];
  }>(`/contracts/${contractId}/reinsurer-access`, { contact_ids: contactIds, sign })
    .then(r => r.data);

export const resendReinsurerLink = (contractId: number, linkId: number) =>
  api.post<ReinsurerAccess & { emailed: boolean }>(
    `/contracts/${contractId}/reinsurer-access/${linkId}/resend`).then(r => r.data);

export const revokeReinsurerLink = (contractId: number, linkId: number) =>
  api.post<ReinsurerAccess>(
    `/contracts/${contractId}/reinsurer-access/${linkId}/revoke`).then(r => r.data);

// ── the reinsurer's private link — public side ───────────────────────────────
/** The unlock session lives in sessionStorage, keyed by link: the code is
 *  asked for again in every new browser session, and a reload inside one does
 *  not ask twice. Wrapped — storage can be unavailable (private windows). */
const SESSION_KEY = (token: string) => `treaty-view:${token.slice(0, 16)}`;

export function getViewSession(token: string): string {
  try { return sessionStorage.getItem(SESSION_KEY(token)) ?? ""; }
  catch { return ""; }
}

function setViewSession(token: string, session: string) {
  try { sessionStorage.setItem(SESSION_KEY(token), session); } catch { /* none */ }
}

export function clearViewSession(token: string) {
  try { sessionStorage.removeItem(SESSION_KEY(token)); } catch { /* none */ }
}

export type TreatyViewClosed = {
  closed: true;
  status: "revoked" | "expired" | "closed";
  title: string;
  message: string;
};

export type TreatyViewLocked = {
  closed: false;
  locked: true;
  carrier: string;
  email_hint: string;
  attempts_left: number;
  lockout_seconds: number;
  can_resend: boolean;
};

export type TreatyViewLayer = {
  index: number;
  name: string;
  facts: Fact[];
  placed_pct: number;
  your_share: { share_pct: number; is_lead: boolean;
                slip_reference: string | null } | null;
};

export type TreatyViewOpen = {
  closed: false;
  locked: false;
  viewer: { name: string; email: string; organisation: string | null };
  carrier: string;
  treaty: { name: string; inception_dt: string | null; expiry_dt: string | null;
            year_of_account: string | null; lifecycle: string;
            programmes: string[] };
  summary: TreatySummary | null;
  layers: TreatyViewLayer[];
  documents: { id: number; filename: string | null; is_executed_copy: boolean;
               uploaded_at: string | null }[];
  wording_pdf: boolean;
  /** The reinsurance bordereaux this reinsurer has been sent for the treaty. */
  statements?: import("./ceded").LinkStatement[];
  /** Set when they were asked to sign it on this link. */
  signature?: {
    requested_at: string | null;
    signed: { name: string; title: string | null; at: string | null } | null;
    can_sign: boolean;
    why_not: string | null;
    active: boolean;
  } | null;
  /** This reinsurer's own answer to the treaty. */
  approval: Partial<ReinsurerApproval> & {
    status: ApprovalStatus;
    can_answer: boolean;
    /** Only while the terms can still change — never on a signed treaty. */
    can_request_changes?: boolean;
    /** Why it cannot answer, when it cannot. */
    why_not?: string | null;
    /** True while the treaty is a draft: approving is what agrees it. */
    settles_terms: boolean;
  };
  expires_at: string | null;
  expires_on: string | null;
};

export const approveTreaty = (token: string, signerName: string,
                              signerTitle?: string) =>
  api.post<{ ok: boolean; terms_agreed: boolean; view: TreatyViewOpen }>(
    `/treaty-view/${encodeURIComponent(token)}/approve`,
    { signer_name: signerName, signer_title: signerTitle || null },
    { headers: { "X-Treaty-Session": getViewSession(token) } }).then(r => r.data);

export const signTreatyOnLink = (token: string, signerName: string,
                                 signerTitle: string, consent: boolean) =>
  api.post<{ ok: boolean; in_force: boolean; blocked_by: string | null;
             view: TreatyViewOpen }>(
    `/treaty-view/${encodeURIComponent(token)}/sign`,
    { signer_name: signerName, signer_title: signerTitle || null, consent },
    { headers: { "X-Treaty-Session": getViewSession(token) } }).then(r => r.data);

export const requestTreatyChanges = (token: string, note: string) =>
  api.post<{ ok: boolean; view: TreatyViewOpen }>(
    `/treaty-view/${encodeURIComponent(token)}/request-changes`, { note },
    { headers: { "X-Treaty-Session": getViewSession(token) } }).then(r => r.data);

export type TreatyView = TreatyViewClosed | TreatyViewLocked | TreatyViewOpen;

export const openTreatyView = (token: string) =>
  api.get<TreatyView>(`/treaty-view/${encodeURIComponent(token)}`, {
    headers: { "X-Treaty-Session": getViewSession(token) },
  }).then(r => r.data);

export async function verifyTreatyCode(token: string, code: string) {
  const { data } = await api.post<{ ok: boolean; session: string }>(
    `/treaty-view/${encodeURIComponent(token)}/verify`, { code });
  setViewSession(token, data.session);
  return data;
}

export const resendTreatyCode = (token: string) =>
  api.post<{ ok: boolean; sent_to: string }>(
    `/treaty-view/${encodeURIComponent(token)}/resend-code`).then(r => r.data);

/** Fetched as a blob rather than linked to, so the session travels as a
 *  header and never sits in a URL someone could copy. */
async function saveBlob(url: string, token: string, filename: string) {
  const r = await api.get<Blob>(url, {
    responseType: "blob",
    headers: { "X-Treaty-Session": getViewSession(token) },
  });
  const href = URL.createObjectURL(r.data);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

export const downloadTreatyDocument = (token: string, docId: number,
                                       filename: string) =>
  saveBlob(`/treaty-view/${encodeURIComponent(token)}/documents/${docId}`,
           token, filename);

export const downloadTreatyWording = (token: string, name: string) =>
  saveBlob(`/treaty-view/${encodeURIComponent(token)}/wording.pdf`,
           token, `${name} - wording.pdf`);

// ── a reinsurer's own page ───────────────────────────────────────────────────
export type ReinsurerTreaty = {
  contract_id: number;
  name: string;
  lifecycle: string;
  is_lead_reinsurer: boolean;
  type_label: string | null;
  headline: string | null;
  inception_dt: string | null;
  expiry_dt: string | null;
  lines: { layer: string; share_pct: number; is_lead: boolean }[];
};

export const getReinsurerTreaties = (partyId: number) =>
  api.get<{ treaties: ReinsurerTreaty[] }>(`/reinsurers/${partyId}/treaties`)
    .then(r => r.data.treaties);

// ── reinsurers as parties ────────────────────────────────────────────────────
/** A reinsurer is a PARTY the carrier keeps — no seat, no invitation, no login.
 *  Created with POST /parties (type reinsurer); its people are party contacts,
 *  and a contact's email is where a treaty link is sent. */
export type ReinsurerContact = {
  id: number; full_name: string; title?: string | null;
  email?: string | null; phone?: string | null;
};

export async function createReinsurer(mga: string, body: {
  legal_name: string; am_best_rating?: string | null;
  domicile_country?: string | null; notes?: string | null;
  contact?: { full_name: string; email: string; title?: string | null } | null;
}): Promise<{ id: number; legal_name: string }> {
  const { contact, ...party } = body;
  const { data } = await api.post<{ id: number; legal_name: string }>(
    "/parties", { ...party, party_type: "reinsurer", scope: "tenant" },
    { params: { mga } });
  if (contact && contact.full_name.trim()) {
    await api.post(`/parties/${data.id}/contacts`, contact);
  }
  return data;
}

export const listPartyContacts = (partyId: number) =>
  api.get<ReinsurerContact[]>(`/parties/${partyId}/contacts`).then(r => r.data);

export const addPartyContact = (partyId: number, body: {
  full_name: string; email?: string | null; title?: string | null;
  phone?: string | null;
}) => api.post<ReinsurerContact>(`/parties/${partyId}/contacts`, body)
  .then(r => r.data);

export const removePartyContact = (partyId: number, contactId: number) =>
  api.delete(`/parties/${partyId}/contacts/${contactId}`);

// ── the Home dashboard ───────────────────────────────────────────────────────
export type ReinsuranceDashboard = {
  live: number;
  counts: { active: number; settling: number; awaiting_signature: number;
            under_placed: number; expiring_soon: number; no_structure: number };
  links: { active: number; opened: number; never_opened: number;
           viewed_this_week: number };
  programmes: { total: number; protected: number };
  /** This month's inward bordereaux on treaty-protected programmes — what a
   *  reinsurance bordereau is built from. Latest version of each, by result. */
  bordereaux: { since: string; total: number; clean: number;
                with_exceptions: number; not_validated: number;
                programmes: number };
  /** The reinsurance (ceded) statements, by state. */
  ceded: { period?: string | null; total?: number;
           not_set_up: number; ready_to_send: number; sent: number; sent_this_month: number;
           queried: number; agreed: number; overdue: number };
  /** Treaties running out within 90 days, soonest first. */
  expiring: { contract_id: number; name: string; lead_reinsurer: string | null;
              type_label: string | null; expiry_dt: string; days_left: number;
              renewal_id: number | null }[];
  actions: { contract_id: number; name: string; lifecycle: string;
             lead_reinsurer: string | null; type_label: string | null;
             headline: string | null; reason: string;
             tab: "summary" | "treaty" | "access"; tone: "crit" | "warn" }[];
  actions_total: number;
  recent_activity: { type: "viewed" | "downloaded" | "locked" | "sent";
                     at: string; contract_id: number; treaty: string | null;
                     who: string | null; party: string | null }[];
};

/** `period` ('2027-03', '2027') narrows the ceded counts to that month or
 *  year — the dashboard's Monthly Activity pick. Omitted: every period. */
export const getReinsuranceDashboard = (period?: string) =>
  api.get<ReinsuranceDashboard>("/reinsurance/dashboard",
    { silent: true, params: period ? { period } : undefined })
    .then(r => r.data);

// ── a treaty signed outside Kavachio ─────────────────────────────────────────
/** Upload a treaty that was ALREADY SIGNED: recorded with its structure, the
 *  signed copy attached as the executed wording, and accepted (in force) — or
 *  sent up to the carrier admin, for a carrier user. Not read for rules. */
export type TreatyReading = {
  name: string | null;
  inception_dt: string | null;
  expiry_dt: string | null;
  year_of_account: string | null;
  notice_period_days: number | null;
  executed_date: string | null;
  /** Null when the document did not say what kind of treaty it is. Shares the
   *  reader could not match to one of your reinsurers have no party id, and
   *  carry the name as written. */
  treaty: TreatyInput | null;
  unmatched_reinsurers: string[];
};

/** Read a signed treaty — what it says, to fill the upload form. Saves nothing. */
export async function readSignedTreaty(file: File) {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<TreatyReading>("/treaties/read", form);
  return data;
}

export async function uploadSignedTreaty(body: {
  file: File; program_id: number; name: string; inception_dt: string;
  expiry_dt: string; year_of_account: string; notice_period_days: number;
  executed_date?: string | null; treaty: TreatyInput; program_ids: number[];
  reading?: TreatyReading | null;
}) {
  const form = new FormData();
  form.append("file", body.file);
  form.append("program_id", String(body.program_id));
  form.append("name", body.name);
  form.append("inception_dt", body.inception_dt);
  form.append("expiry_dt", body.expiry_dt);
  form.append("year_of_account", body.year_of_account);
  form.append("notice_period_days", String(body.notice_period_days));
  if (body.executed_date) form.append("executed_date", body.executed_date);
  form.append("treaty", JSON.stringify(body.treaty));
  form.append("program_ids", JSON.stringify(body.program_ids));
  if (body.reading) form.append("reading", JSON.stringify(body.reading));
  const { data } = await api.post<{ id: number; lifecycle: string }>(
    "/treaties/upload", form);
  return data;
}

// ── 27.1 / 27.2: the calculation basis, with the dates it applies from ──────
export type BasisKind = "pct" | "money" | "int" | "profit" | "sliding";
export type SlidingScale = {
  provisional_pct: number; method: "linear" | "step";
  points: { loss_ratio: number; rate: number }[];
  min_pct?: number; max_pct?: number;
};
export type ProfitCommission = { rate_pct: number; expense_pct: number; deficit_years: number };

export type BasisVersion = {
  /** null for the structure's own value, which applies from inception. */
  id: number | null; effective_from: string | null;
  value: number | SlidingScale | ProfitCommission;
  source: "structure" | "manual" | "wording" | "endorsement";
  note: string | null; entered_by: string | null; entered_at?: string | null;
};
export type BasisTerm = {
  term: string; label: string; kind: BasisKind; level: "treaty" | "layer";
  layer: number | null; layer_name: string | null; versions: BasisVersion[];
};
export type Basis = {
  treaty_type: TreatyTypeKey | null; inception: string | null; expiry: string | null;
  terms: BasisTerm[]; can_edit: boolean;
};

export const getBasis = (contractId: number) =>
  api.get<Basis>(`/contracts/${contractId}/basis`).then(r => r.data);

export const addBasis = (contractId: number, body: {
  term: string; layer?: number | null; value: unknown; effective_from: string;
  note?: string | null; source?: string;
}) => api.post<Basis>(`/contracts/${contractId}/basis`, body).then(r => r.data);

export const removeBasis = (contractId: number, entryId: number) =>
  api.delete<Basis>(`/contracts/${contractId}/basis/${entryId}`).then(r => r.data);

export type SlidingResult = { loss_ratio: number; rate_pct: number; commission: number;
                              provisional: number; adjustment: number };
export type ProfitResult = { premium: number; ceding_commission: number; losses: number;
                             expenses: number; deficit_brought_forward: number; profit: number;
                             commission: number; deficit_carried_forward: number };

export const evaluateBasis = (contractId: number, body: {
  term: "sliding_scale" | "profit_commission"; value: unknown; premium: number;
  losses: number; ceding_commission?: number | null; deficit_brought_forward?: number;
}) => api.post<SlidingResult & ProfitResult>(`/contracts/${contractId}/basis/evaluate`, body)
  .then(r => r.data);

// ── an endorsement uploaded on a treaty: read, reviewed, applied ────────────
export type EndorsementChangeRow = {
  term: string; layer: number | null; label?: string; kind?: BasisKind;
  value: unknown; page?: number | null; quote?: string | null;
};
export type EndorsementClauseRow = { title: string; text: string; page?: number | null };

export type EndorsementReading = {
  endorsement_number: string | null; effective_date: string | null;
  changes: EndorsementChangeRow[];
  /** Read from it, but not a term this treaty has, or not a sound value. */
  other_changes: { text: string; page: number | null }[];
  clauses: EndorsementClauseRow[]; pages: number; read_at: string;
};

export type EndorsementRecord = {
  document: { id: number; filename: string | null; version: string | null;
              effective_from: string | null; uploaded_at: string | null; has_file: boolean;
              validation: { status: string; label: string; pages: number };
              drafted_here: boolean };
  reading: EndorsementReading | null;
  review: { confirmed_at: string; confirmed_by: string | null; effective_date: string | null;
            endorsement_number: string | null; changes: EndorsementChangeRow[];
            clauses: EndorsementClauseRow[] } | null;
  applied: { id: number; term: string; layer: number | null; value: unknown; effective_from: string }[];
  /** Rules read from its clauses when it was applied — checked from its date. */
  rules: { id: string; label: string; severity: "critical" | "warning"; clause: string | null;
           effective_from: string | null }[];
  rules_error: string | null;
  terms: { term: string; layer: number | null; label: string; layer_name: string | null;
           kind: BasisKind; value_before: unknown }[];
  treaty: string; contract_id: number; can_edit: boolean;
};

export const getEndorsement = (contractId: number, docId: number) =>
  api.get<EndorsementRecord>(`/contracts/${contractId}/endorsements/${docId}`).then(r => r.data);

export const readEndorsement = (contractId: number, docId: number) =>
  api.post<EndorsementRecord>(`/contracts/${contractId}/endorsements/${docId}/read`).then(r => r.data);

export const confirmEndorsement = (contractId: number, docId: number, body: {
  effective_date: string | null; endorsement_number: string | null;
  changes: EndorsementChangeRow[]; clauses: EndorsementClauseRow[];
}) => api.post<EndorsementRecord>(`/contracts/${contractId}/endorsements/${docId}/confirm`, body)
  .then(r => r.data);

/** One page of a treaty's document, as an image URL (fetched with the session). */
export async function documentPageUrl(contractId: number, docId: number, page: number): Promise<string> {
  const r = await api.get<Blob>(`/contracts/${contractId}/documents/${docId}/pages/${page}`,
                                { responseType: "blob" });
  return URL.createObjectURL(r.data);
}
