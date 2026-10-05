/**
 * The secure correction link — one submission, no login.
 *
 * Bare axios on purpose, not `api`: the shared client refreshes sessions,
 * redirects a 401 to /login and pops a global error dialog. None of that
 * means anything to a broker who opened a link from an email; this page
 * shows its own errors.
 */
import axios from "axios";
import {
  outputExcToStored,
  type ExportDecideResponse, type ExportDecision, type OutputException, type StoredException,
} from "./validation";

const baseURL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const http = axios.create({ baseURL });

export type FixGate = {
  reference: string;
  file: string;
  programme: string | null;
  carrier: string | null;
  broker: string | null;
  period: string | null;
  status: string;
  status_text: string;
  email: string;
  locked: boolean;
};

export type FixException = {
  key: string;
  severity: string;
  holds_file: boolean;
  status: string;          // open | approved | fixed | dismissed | rejected
  answer_note: string | null;
  rule: string;
  message: string;
  sheet: string | null;
  row: number | null;
  column: string | null;
  located_in_your_file: boolean;
  policy_number: string | null;
  current_value: unknown;
  /** Set when current_value is a total ("File total"), not one cell's value. */
  value_label?: string | null;
  limit?: number | null;
  over_by?: number | null;
  expected_value: unknown;
  what_to_fix: string;
};

export type FixVersion = {
  version: number;
  source: string;
  status: string;
  status_text: string;
  message: string | null;
  exceptions: number | null;
  open: number | null;
  created_at: string | null;
};

export type FixSubmission = {
  reference: string;
  file: string;
  version: number | null;
  status: string;
  status_text: string;
  message: string | null;
  programme: string | null;
  carrier: string | null;
  period: string | null;
  progress: { total: number; fixed: number; remaining: number; blocking_remaining: number };
  progress_text: string | null;
  deadline: string | null;
  delivered_at: string | null;
  versions: FixVersion[];
  exceptions?: FixException[];
  can_submit: boolean;
  drafts: number;
};

export type FixAnswer = { key: string; action: "correct" | "keep"; value?: string; reason?: string };

export function errorText(e: unknown): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === "string") return d;
  return "Something went wrong. Please try again.";
}

export function errorStatus(e: unknown): number | undefined {
  return (e as { response?: { status?: number } })?.response?.status;
}

const sess = (session: string) => ({ headers: { "X-Fix-Session": session } });

export async function openLink(token: string): Promise<FixGate> {
  return (await http.get<FixGate>(`/fix-link/${encodeURIComponent(token)}`)).data;
}

export async function sendCode(token: string): Promise<{ sent_to: string; valid_minutes: number }> {
  return (await http.post(`/fix-link/${encodeURIComponent(token)}/code`)).data;
}

export async function verifyCode(token: string, code: string): Promise<{ session: string }> {
  return (await http.post(`/fix-link/${encodeURIComponent(token)}/verify`, { code })).data;
}

export async function loadSubmission(token: string, session: string): Promise<FixSubmission> {
  return (await http.get<FixSubmission>(`/fix-link/${encodeURIComponent(token)}/submission`,
    sess(session))).data;
}

export async function saveAnswers(token: string, session: string, answers: FixAnswer[]):
    Promise<{ saved: number; skipped: { key: string; reason: string }[] }> {
  return (await http.post(`/fix-link/${encodeURIComponent(token)}/answers`, { answers },
    sess(session))).data;
}

export async function submitVersion(token: string, session: string): Promise<FixSubmission> {
  return (await http.post<FixSubmission>(`/fix-link/${encodeURIComponent(token)}/submit`, {},
    { ...sess(session), timeout: 300000 })).data;
}

// ── the portal's BDX review, through the link ───────────────────────────────
// The page renders the SAME components as the portal's exception screen
// (BdxInlineReview, the rule cards). These reach the portal's own handlers for
// this submission's current file, with the link session instead of a login.

export type FixExport = { id: number; filename: string | null; exceptions: StoredException[] };

export async function loadExport(token: string, session: string): Promise<FixExport> {
  const { data } = await http.get<{ id: number; filename?: string | null; exceptions?: OutputException[] }>(
    `/fix-link/${encodeURIComponent(token)}/export`, sess(session));
  return { id: data.id, filename: data.filename ?? null,
           exceptions: (data.exceptions ?? []).map(outputExcToStored) };
}

export async function decideViaLink(token: string, session: string,
                                    decisions: ExportDecision[]): Promise<ExportDecideResponse> {
  return (await http.post<ExportDecideResponse>(`/fix-link/${encodeURIComponent(token)}/decide`,
    { decisions, apply: true }, sess(session))).data;
}

export const streamPathFor = (token: string) => `/fix-link/${encodeURIComponent(token)}/data/stream`;

export type FixValidation = {
  version: number;
  corrected: number;
  corrected_ok: number;
  still_failing: FixException[];
  still_failing_count: number;
  open_after: number;
  blocking_after: number;
};

/** Every rule over the file with the saved decisions applied — a dry run of
 *  the next version, kept nowhere. Submit is refused until this has run. */
export async function validateLink(token: string, session: string): Promise<FixValidation> {
  return (await http.post<FixValidation>(`/fix-link/${encodeURIComponent(token)}/validate`, {},
    { ...sess(session), timeout: 300000 })).data;
}
