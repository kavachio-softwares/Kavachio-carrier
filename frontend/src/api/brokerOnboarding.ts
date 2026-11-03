// ---------------------------------------------------------------------------
// Broker onboarding requests — the carrier admin's say on WHO the carrier
// works with, taken before the broker is told anything.
//
// A carrier USER adding a broker to a programme, or inviting one, no longer
// does it: the server writes a request and asks their carrier admin. Nothing
// is created and no email is sent until that is answered, so a rejected
// request leaves nothing behind and the broker never learns they were
// considered.
//
// Raising one is NOT here, deliberately. It happens through the same two calls
// it always did — addProgrammeBroker and inviteBroker in ./hierarchy — because
// the intent is the same act; the server decides whether it becomes a link or
// a request, and says which in `pending`. There is no second button and no
// second screen to keep in step with the first.
//
// This module is only the answering side: the queue, and the two decisions.
// ---------------------------------------------------------------------------

import { api } from "./client";

export type BrokerRequestStatus = "pending" | "approved" | "rejected" | "withdrawn";

export type BrokerOnboardingRequest = {
  id: number;
  status: BrokerRequestStatus;
  /** Which shape this is. `existing` wants only a programme link and never
   *  sends mail; `invite` would bring a new organisation on board. */
  kind: "invite" | "existing";
  program_id: number | null;
  /** Null where no programme was named — a broker asked into the DIRECTORY
   *  rather than onto a programme. The screen says "No programme yet". */
  programme: string | null;
  broker_party_id: number | null;
  /** For an existing broker, their real name. For an invitation, what the
   *  carrier user typed — all anyone here knows about them yet. */
  broker_name: string | null;
  party_type: string | null;
  onboarding_status: string | null;
  /** Who would be mailed. Null for an existing broker: nothing is ever sent. */
  admin_email: string | null;
  admin_name: string | null;
  requested_by: { id: number; full_name: string | null; email: string | null } | null;
  requested_at: string | null;
  decided_by: { id: number; full_name: string | null; email: string | null } | null;
  decided_at: string | null;
  /** Why it was turned down. The carrier user who asked reads this. */
  reason: string | null;
};

/** The queue. A carrier admin gets every request at the carrier; a carrier
 *  user gets their own, which is how the rejection reason reaches them.
 *
 *  `status` takes the four real states and one derived value, `answered` —
 *  everything that is not waiting. The screen shows waiting and answered as
 *  two tables that page separately, so each asks for its own half rather than
 *  cutting one out of a page of the other.
 *
 *  `pending` is always the whole queue's waiting count, whatever was filtered
 *  or paged; `total` is how many matched the filter. */
export const listBrokerRequests = (params?: {
  status?: BrokerRequestStatus | "answered";
  page?: number; page_size?: number;
}) =>
  api.get<{ items: BrokerOnboardingRequest[]; pending: number; total: number }>(
    "/broker-onboarding-requests", { params }).then(r => r.data);

/** Approve it: the broker is onboarded and the invitation goes out now.
 *
 *  This does NOT put the programme live for the broker. It settles who the
 *  carrier works with; what they may send is still the Bordereau Setup
 *  approval's question, and that gate is unchanged. */
export const approveBrokerRequest = (id: number) =>
  api.post<BrokerOnboardingRequest>(
    `/broker-onboarding-requests/${id}/approve`).then(r => r.data);

/** Turn it down. The reason is required and goes back to whoever asked. */
export const rejectBrokerRequest = (id: number, reason: string) =>
  api.post<BrokerOnboardingRequest>(
    `/broker-onboarding-requests/${id}/reject`, { reason }).then(r => r.data);

/** Take the question back. The requester's own — an admin rejects with a
 *  reason instead. */
export const withdrawBrokerRequest = (id: number) =>
  api.delete<{ ok: boolean; message: string }>(
    `/broker-onboarding-requests/${id}`).then(r => r.data);
