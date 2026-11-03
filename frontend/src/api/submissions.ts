/**
 * The broker exception loop, carrier side: where a submission stands, whether
 * the broker was told, the programme's delivery rule and a channel's notify
 * list. The carrier never answers an exception here — only the broker can.
 */
import { api } from "./client";

export type NoticeSummary = {
  event: string;
  version: number | null;
  sent: number;
  failed: number;
  queued: number;
  skipped: number;
  recipients: string[];
  at: string | null;
};

export type Notice = {
  event: string;
  channel: "email" | "sftp_file";
  recipient: string | null;
  version: number | null;
  status: "sent" | "failed" | "skipped";
  error: string | null;
  at: string | null;
};

export type SubmissionDoc = {
  reference: string;
  file: string;
  version: number | null;
  this_file_version: number;
  status: string;
  status_text: string;
  message: string | null;
  period: string | null;
  progress: { total: number; fixed: number; remaining: number; blocking_remaining: number };
  progress_text: string | null;
  deadline: string | null;
  delivered_at: string | null;
  delivered_version: number | null;
  versions: { version: number; source: string; is_file: boolean; status: string; status_text: string;
              message: string | null; exceptions: number | null; open: number | null;
              created_at: string | null;
              /** The file behind a file version (carrier panel only). */
              arrival_id?: number | null }[];
  broker_notified: NoticeSummary | null;
  notifications: Notice[];
  can_deliver: boolean;
  can_notify: boolean;
};

export async function submissionForArrival(arrivalId: number): Promise<SubmissionDoc | null> {
  const { data } = await api.get<{ submission: SubmissionDoc | null }>(
    `/submissions/by-arrival/${arrivalId}`);
  return data.submission;
}

export async function deliverAsItIs(reference: string): Promise<void> {
  await api.post(`/submissions/${encodeURIComponent(reference)}/deliver`, {});
}

export async function notifyAgain(reference: string): Promise<void> {
  await api.post(`/submissions/${encodeURIComponent(reference)}/notify-again`);
}

export type DeliveryRule = {
  hold_severities: string[];
  rule_overrides: Record<string, "hold" | "pass">;
  correction_days: number;
  deadline_action: "deliver_flagged" | "keep_on_hold";
  is_default: boolean;
};

export async function getDeliveryRule(programId: number): Promise<DeliveryRule> {
  return (await api.get<DeliveryRule>(`/programs/${programId}/delivery-rule`)).data;
}

export async function putDeliveryRule(programId: number,
                                      body: Partial<DeliveryRule>): Promise<DeliveryRule> {
  return (await api.put<DeliveryRule>(`/programs/${programId}/delivery-rule`, body)).data;
}

export async function getRouteContacts(routeId: number): Promise<string[]> {
  return (await api.get<{ emails: string[] }>(`/intake/routes/${routeId}/contacts`)).data.emails;
}

export async function putRouteContacts(routeId: number, emails: string[]): Promise<string[]> {
  return (await api.put<{ emails: string[] }>(`/intake/routes/${routeId}/contacts`, { emails }))
    .data.emails;
}
