/**
 * Uploading ONE contract and getting its id back — the whole awkward job in a
 * single place.
 *
 * It lives here rather than inside a screen because two screens now do it: the
 * Bordereau Setup builder, which uploads the contracts a setup is built from,
 * and the Add Contract flow on a broker's own page. They must behave
 * identically — the same extraction, the same pause when the contract defers to
 * a document nobody supplied, and above all the same recovery when the answer
 * to a long upload never arrives. Two copies of that would drift, and the half
 * that drifted would lose contracts.
 *
 * What makes it awkward is that extraction routinely outlives the request
 * carrying it (see `recoverContractId` below), so "no id in the reply" does not
 * mean "no contract".
 */
import { api } from "./client";
import {
  newUploadToken, pickRecoveredContract, type ProgramContractRow,
} from "../utils/directSetup";

/** An external document the contract defers rule content to, as the server
 *  names it. Only `document_name` is read here; the rest is for the screen that
 *  has to explain the pause. */
export type ExternalReference = {
  document_name?: string; version_or_date?: string; source_texts?: string[];
};

/** What the upload actually wrote, straight from the persister's own tally.
 *  Reported rather than inferred: "41 clauses, 0 rules" is the difference
 *  between a contract that was read and one that was read AND mapped, and a
 *  screen that guesses at it will eventually guess wrong. Null when the
 *  upload's reply was lost and the contract had to be recovered by id — the
 *  counts travelled in the reply that never arrived. */
export type ContractUploadCounts = {
  clauses: number; rules: number; review: number; control: number; terms: number;
};

export type ContractUploadResult =
  /** Extraction paused: the contract defers to document(s) that weren't given.
   *  The caller decides — attach them and retry, or continue anyway. */
  | { halt: { refs: ExternalReference[]; resumeToken: string | null } }
  /** Saved. `deferred` names any external document the extraction still went
   *  ahead without, so the caller can say so rather than imply completeness. */
  | { cid: number; deferred: string[]; counts: ContractUploadCounts | null };

export type ContractUploadOptions = {
  programId: number;
  /** Optional. WITH a template the clauses are mapped onto its field names and
   *  become compiled rules. WITHOUT one the contract is still read and its
   *  clauses still saved — extraction simply stops before rule generation,
   *  because a rule is written against an output template's columns and there
   *  are none to write against. That is what lets a contract be added on its
   *  own, before any bordereau work exists. */
  outputTemplateId?: number | null;
  file: File;
  /** Which broker holds it. Omitted by the setup builder, which uploads at
   *  (carrier, programme) scope; supplied wherever the broker is known. */
  brokerPartyId?: number | null;
  /** The schedule/sheet this contract covers, so one programme can hold several
   *  active contracts (one per schedule) instead of superseding each other. */
  scheduleKey?: string | null;
  /** Documents the contract defers to, sent with it so the deferred clauses
   *  resolve into real rules. */
  referenceFiles?: File[];
  /** Opt in to the pause above. Callers that cannot handle it leave it off and
   *  the contract simply proceeds without the deferred clauses. */
  enableReferenceHalt?: boolean;
  continueAnyway?: boolean;
  resumeToken?: string | null;
  /** Progress notes fit to show a user — currently only the "still reading"
   *  message from the recovery wait, which can last a long time. */
  onProgress?: (note: string) => void;
};

/** Every contract on a programme, as far as this module cares. */
async function contractIdsNow(programId: number): Promise<Set<number>> {
  try {
    const { data } = await api.get<Array<{ id: number }>>(
      `/programs/${programId}/contracts`, { silent: true });
    return new Set((Array.isArray(data) ? data : []).map(c => c.id));
  } catch { return new Set(); }
}

// How long to keep waiting for a contract whose upload reply was lost, and how
// often to look. A contract can take well over half an hour to extract: a long
// document falls back to per-section extraction, then one model call per clause
// and one per intent chunk — 75+ sequential calls is normal. Polling is one
// small request every 15s, so waiting too long costs far less than discarding a
// contract that was about to land.
const RECOVER_TIMEOUT_MS = 30 * 60_000;
const RECOVER_POLL_MS = 15_000;

/**
 * Find the contract THIS upload created, when the upload's answer was lost.
 *
 * The response is streamed with whitespace heartbeats so an idle-connection
 * timeout never fires, but an ingress that caps TOTAL request duration cuts the
 * connection regardless, and the browser is left holding a 200 whose body is
 * only heartbeat whitespace. Crucially the server does NOT stop: the pipeline
 * outlives the request and saves the contract minutes later. So wait for the
 * row the work produces instead of reporting a contract that doesn't exist.
 */
async function recoverContractId(
  opts: ContractUploadOptions, before: Set<number>, token: string,
): Promise<number | null> {
  const deadline = Date.now() + RECOVER_TIMEOUT_MS;
  for (;;) {
    try {
      const { data } = await api.get<ProgramContractRow[]>(
        `/programs/${opts.programId}/contracts`, { silent: true });
      const hit = pickRecoveredContract(data, {
        token, before, filename: opts.file.name,
        templateId: opts.outputTemplateId ?? null,
      });
      if (hit != null) return hit;
    } catch { /* keep waiting — a failed poll is not a failed upload */ }
    if (Date.now() >= deadline) return null;
    // Deliberately says nothing about the dropped connection. Nothing has gone
    // wrong from where the user sits — the contract is being read, and that is
    // the only fact they can act on.
    opts.onProgress?.(
      `Still reading “${opts.file.name}” on the server. A long contract takes `
      + `a while — checking again every ${RECOVER_POLL_MS / 1000}s…`);
    await new Promise(res => setTimeout(res, RECOVER_POLL_MS));
  }
}

/** Did the user attach a document that plausibly IS the one named? Compared
 *  loosely on purpose: the contract names a document in prose, the file is
 *  named by whoever saved it, and the two rarely match exactly. */
function coveredBy(files: File[], named: string): boolean {
  return files.some(rf => {
    const a = rf.name.toLowerCase(), b = named.toLowerCase();
    return a.includes(b.slice(0, 12)) || b.includes(a.replace(/\.[a-z]+$/, "").slice(0, 12));
  });
}

/**
 * Upload one contract and return its id — or the pause that needs an answer.
 *
 * Throws only when the contract genuinely never landed (or the server refused
 * it outright). Carrying on from a silent failure would bind whatever is being
 * built to NO contract, which hides every rule the contract produced behind a
 * green success screen.
 */
export async function uploadContract(
  opts: ContractUploadOptions,
): Promise<ContractUploadResult> {
  const refs = opts.referenceFiles ?? [];
  const fd = new FormData();
  if (opts.outputTemplateId != null) {
    fd.append("output_template_id", String(opts.outputTemplateId));
  }
  fd.append("file", opts.file);
  if (opts.brokerPartyId != null) fd.append("broker_party_id", String(opts.brokerPartyId));
  if (opts.scheduleKey) fd.append("schedule_key", opts.scheduleKey);
  refs.forEach(f => fd.append("reference_files", f));
  if (opts.enableReferenceHalt) fd.append("enable_reference_halt", "true");
  if (opts.continueAnyway) {
    fd.append("continue_anyway", "true");
    if (opts.resumeToken) fd.append("resume_token", opts.resumeToken);
  }
  // Correlation id for THIS upload, so the contract it creates can be
  // identified exactly rather than inferred, even when two uploads of the same
  // file run at once or an earlier attempt's contract lands late.
  const token = newUploadToken();
  fd.append("upload_token", token);

  const before = await contractIdsNow(opts.programId);
  let r: { data?: Record<string, unknown> } | null = null;
  try {
    r = await api.post(`/programs/${opts.programId}/contracts`, fd);
  } catch (e) {
    // A response the server actually sent (4xx/5xx, or the in-body pipeline
    // failure the interceptor re-throws) is a real error and must surface. A
    // transport-level failure carries no response — same lost-answer case as an
    // unparseable body, so fall through to recovery.
    if ((e as { response?: unknown })?.response) throw e;
  }
  const body = (r?.data ?? {}) as Record<string, unknown>;

  if (body.status === "references_required") {
    return {
      halt: {
        refs: (body.external_references ?? []) as ExternalReference[],
        resumeToken: (body.resume_token ?? null) as string | null,
      },
    };
  }

  // Extraction proceeded but still names external document(s) it didn't have
  // (the continue-anyway path, or a caller that never opted into the pause).
  const ext = ((body.extraction_output as { external_references?: unknown })
    ?.external_references ?? []) as ExternalReference[];
  const deferred = (ext.map(x => x?.document_name).filter(Boolean) as string[])
    .filter(n => !coveredBy(refs, n));

  const persisted = body.persisted as {
    contract_id?: number;
    counts?: Record<string, number>;
  } | undefined;
  let cid = (body.id ?? persisted?.contract_id ?? null) as number | null;
  if (cid == null) cid = await recoverContractId(opts, before, token);
  if (cid == null) {
    throw new Error(
      `“${opts.file.name}” was uploaded but no contract came back from the `
      + `server, so nothing was saved to take its rules from. Please try again.`);
  }
  const c = persisted?.counts;
  const counts: ContractUploadCounts | null = c ? {
    clauses: c.clauses_extracted ?? 0,
    rules:   c.validation_rule ?? 0,
    review:  c.review_queue ?? 0,
    control: c.control_register ?? 0,
    terms:   c.contract_terms ?? 0,
  } : null;
  return { cid, deferred, counts };
}

/** Rules a contract already on file produced for one Output Template. */
export type GeneratedRules = {
  ok: boolean;
  contract_id: number;
  output_template_id: number;
  /** Set when there was nothing to do, and the build should carry on.
   *  `already_generated` — this contract's rules for this template exist.
   *  `authored_contract` — terms were typed, not read off a document, so there
   *  are no clauses to write rules from; its checks come from its agreed
   *  limits instead. */
  skipped?: string;
  created: number;
  clauses?: number;
  rules?: number;
};

/**
 * Write a contract's rules for an Output Template, from the clauses it already
 * has — no re-upload, no re-read.
 *
 * A contract added on a broker's page before the programme had a template stops
 * after its clauses: rules name a template's COLUMNS, so there was nothing to
 * write them against, and every rule-bearing clause was parked awaiting one.
 * A setup built on that contract used to take its id and run, and the pipeline
 * came out with zero contract rules and no explanation. This finishes it.
 *
 * Slow (the same model calls an upload makes, minus the document read) and safe
 * to call again: a contract already done for this template reports `skipped`.
 */
export async function generateContractRules(opts: {
  programId: number; contractId: number; outputTemplateId: number;
}): Promise<GeneratedRules> {
  const { data } = await api.post<GeneratedRules>(
    `/programs/${opts.programId}/contracts/${opts.contractId}/generate-rules`,
    { output_template_id: opts.outputTemplateId });
  return data;
}
