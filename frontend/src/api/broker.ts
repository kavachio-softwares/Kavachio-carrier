/**
 * The broker's side of the hierarchy.
 *
 * A broker creates no carriers and no programmes — the carrier hands them over
 * by putting the broker on a programme. So every call here is a READ of what
 * was given, and the carrier is never a parameter the broker chooses: it comes
 * back inside the data.
 */
import { api } from "./client";

export type BrokerCarrier = { id: number; name: string; programme_count: number };

export type BrokerProgramme = {
  id: number; name: string; status: string | null;
  carrier_id: number; carrier_name: string; assigned_at: string | null;
};

export type ApprovalStatus = "draft" | "pending_approval" | "approved" | "rejected";

export type BrokerContract = {
  id: number;
  filename: string | null;
  /** What it is called. An AUTHORED contract has no file, so a list keyed on
   *  filename shows it as "Contract 462". */
  name: string;
  contract_type: string | null;
  /** Where it is in its life. NOT the same question as `approval_status`,
   *  which only says whether this broker may set it up: a contract sitting in
   *  `in_review` is one the BROKER has to act on, and without this it renders
   *  as an ordinary approved row. */
  lifecycle: Lifecycle;
  /** Who it is waiting on — the question this list is actually scanned for. */
  whose_turn: "carrier" | "broker" | null;
  has_wording: boolean;
  programme: { id: number | null; name: string };
  carrier: { id: number | null; name: string };
  inception_dt: string | null;
  expiry_dt: string | null;
  approval_status: ApprovalStatus;
  /** Who put it there. A carrier upload is live on arrival; a broker upload waits. */
  source: "carrier" | "broker";
  submitted_at: string | null;
  created_at: string | null;
};

/** Mirrors the carrier-side type; `expired` is derived from the term. */
export type Lifecycle =
  | "draft" | "pending" | "in_review" | "changes_requested" | "agreed"
  | "signed" | "active" | "expired" | "terminated" | "superseded";

export type BrokerDashboard = {
  broker: { id: number; name: string };
  carriers: { id: number; name: string }[];
  counts: {
    waiting_on_carrier: number;
    /** The queue only this broker can move — terms to read, or a signature to
     *  give. Its absence is why a carrier could send terms over and the broker
     *  never be told. */
    waiting_on_me: number;
    /** In force, which since signing became mandatory is not the same as
     *  approved. */
    live_contracts: number;
    programmes: number; carriers: number;
  };
  waiting: {
    id: number; filename: string | null;
    programme: string; carrier: string; submitted_at: string | null;
  }[];
  waiting_on_me: {
    id: number; name: string; lifecycle: Lifecycle;
    programme: string; carrier: string;
    /** What the broker has to do, in words, not a state name. */
    what: string;
  }[];
};

export const getBrokerDashboard = () =>
  api.get<BrokerDashboard>("/broker/dashboard").then(r => r.data);

export const getBrokerCarriers = () =>
  api.get<BrokerCarrier[]>("/broker/carriers").then(r => r.data);

export const getBrokerProgrammes = (carrierId?: number) =>
  api.get<BrokerProgramme[]>("/broker/programmes", {
    params: carrierId ? { carrier_id: carrierId } : {},
  }).then(r => r.data);

export const getBrokerContracts = (opts: { carrierId?: number; programId?: number } = {}) =>
  api.get<BrokerContract[]>("/broker/contracts", {
    params: {
      carrier_id: opts.carrierId || undefined,
      program_id: opts.programId || undefined,
    },
  }).then(r => r.data);

// --- the broker's own team ---------------------------------------------------
// Scoped by broker, never by carrier: a broker producing for three carriers has
// one team, not three. Only OPERATOR can be invited from here — a second broker
// admin comes from the carrier, the same way the first one did.

export type BrokerUser = {
  id: number;
  email: string;
  full_name: string;
  role: "broker_admin" | "operator";
  status: string;
  last_login_at: string | null;
};

export type BrokerUsers = {
  broker: { id: number; name: string };
  items: BrokerUser[];
  total: number;
  /** Team-wide, so "can't remove the last admin" uses the true count. */
  total_admins: number;
};

export const getBrokerUsers = () =>
  api.get<BrokerUsers>("/broker/users").then(r => r.data);

export const inviteBrokerOperator = (full_name: string, email: string) =>
  api.post<BrokerUser>("/broker/users", { full_name, email }).then(r => r.data);

export const resendBrokerInvite = (userId: number) =>
  api.post(`/broker/users/${userId}/resend-invite`).then(r => r.data);

export const removeBrokerUser = (userId: number) =>
  api.delete(`/broker/users/${userId}`).then(r => r.data);

// --- the operator's day ------------------------------------------------------
// Same scope as the broker (an operator is a seat inside the broker), but the
// question is different: what is there to run, and what went wrong.

export type OperatorHome = {
  broker: { id: number; name: string };
  carriers: { id: number; name: string }[];
  counts: { programmes: number; setups: number; runs: number; exceptions: number };
  /** Which step is missing, so the screen can say whose job the next one is. */
  blocked_on: "no-programme" | "no-setup" | null;
};

export const getOperatorHome = () =>
  api.get<OperatorHome>("/broker/operator-home").then(r => r.data);
