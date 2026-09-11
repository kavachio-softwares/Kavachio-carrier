/**
 * The broker's bordereau lane: submit a file against a CONTRACT, and read what
 * validation said about it.
 *
 * The contract is the scope, not the programme. A bordereau is checked against
 * a contract's rules, so the contract is what decides whether a file passes —
 * and a broker holding two contracts on one programme is answering to two
 * different sets of rules. Naming the programme alone would leave "against
 * which rules?" unanswered and would mix both contracts' history into one list.
 *
 * These are the CARRIER-CENTRIC paths, not the flat /direct/* ones, and that is
 * not a style choice — it is the only thing that makes the lane work for a
 * broker at all. Every /direct/* handler resolves the tenant from the token,
 * and a broker seat carries no tenant of its own, so those routes answer "no
 * tenant bound to this user" no matter what the UI sends. Under
 * /carriers/{c}/programs/{p}/brokers/{b}/contracts/{t}/... the server checks the
 * program_broker grant one segment at a time and derives the carrier from the
 * path it just authorized.
 *
 * The broker never sends a carrier_party_id. It is resolved server-side from
 * the carrier in the path — a broker has no business knowing a carrier's
 * internal party ids, and accepting one would be a way to aim a run somewhere
 * else.
 */
import { api } from "./client";
import type { RunResp, RunUrls } from "../components/RunResult";

/** Everything below is addressed at one contract, so they share this prefix. */
export type ContractPath = {
  carrierId: number; programId: number; brokerPartyId: number; contractId: number;
};

const base = (p: ContractPath) =>
  `/carriers/${p.carrierId}/programs/${p.programId}`
  + `/brokers/${p.brokerPartyId}/contracts/${p.contractId}`;

export type BrokerIdentity = { id: number; name: string; role: string };

export const getBrokerMe = () =>
  api.get<BrokerIdentity>("/broker/me").then(r => r.data);

/**
 * Whether there is anything to run against. `ready: false` always carries a
 * `reason` — a broker whose contract is not approved, or whose carrier has not
 * built the setup, should be told which, not shown an upload box that fails on
 * submit. Neither is something they can fix themselves.
 *
 * `held_by` says WHOSE setup was resolved: "broker" is one built for this
 * broker specifically, "programme" is the shared one every broker on the
 * programme runs against. Worth showing — it explains why the expected columns
 * may not be the ones agreed for this broker's own book.
 */
export type BordereauReadiness = {
  ready: boolean;
  reason: string | null;
  setup: {
    id: number | null;
    name: string | null;
    held_by: "broker" | "programme";
    output_template: { id: number; name: string } | null;
  } | null;
};

export const getBordereauReadiness = (p: ContractPath) =>
  api.get<BordereauReadiness>(`${base(p)}/bordereau`).then(r => r.data);

/** The run payload is IDENTICAL to the carrier's — same handler, same fields —
 *  so it is described once, next to the component that renders it. */
export type { RunResp as BrokerRunResult } from "../components/RunResult";

/**
 * `checkOnly` is the whole reason a broker gets this screen. It runs every
 * validation and returns the fix-list WITHOUT ingesting, recording a run, or
 * putting anything in front of the carrier — so a broker finds out what is
 * wrong with their file before the carrier does, rather than after.
 */
export const runBrokerBordereau = (
  p: ContractPath, file: File,
  opts: { checkOnly?: boolean; skipRows?: number } = {},
) => {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("filename", file.name);
  fd.append("check_only", String(!!opts.checkOnly));
  fd.append("skip_rows", String(opts.skipRows ?? 0));
  return api.post<RunResp>(`${base(p)}/runs`, fd).then(r => r.data);
};

export type BrokerRun = {
  landing_id: number;
  source_filename: string | null;
  row_count: number | null;
  created_at: string | null;
  export_id: number | null;
  filename: string | null;
  exception_count: number | null;
  status: string | null;
};

/** Submitted runs only — the self-check records nothing, by design. */
export const getBrokerRuns = (p: ContractPath, limit = 20) =>
  api.get<BrokerRun[]>(`${base(p)}/runs`, { params: { limit } }).then(r => r.data);

/**
 * Where the shared RunResult component reads this run's file and preview rows.
 *
 * The nested paths, not /export/downloads/{id}/*, whose only guard is "does
 * this export belong to the tenant" — which for a broker acting on this carrier
 * would pass for every other broker's runs too. Handing these to RunResult is
 * the ONLY difference between the broker's result view and the carrier's.
 */
export const brokerRunUrls = (p: ContractPath): RunUrls => ({
  file: id => `${base(p)}/runs/${id}/file`,
  data: (id, q) => `${base(p)}/runs/${id}/data?${q}`,
});

/** The blank bordereau for this contract — the layout its live setup reads,
 *  which is what a run finds the columns of an uploaded file by. */
export const bordereauTemplatePath = (p: ContractPath) =>
  `${base(p)}/bordereau-template`;
