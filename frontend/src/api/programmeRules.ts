import { api } from "./client";
import { currentMga } from "../auth";
import { getViewSession } from "./treaty";

// Programme Rules (backend programme_rules.py, programme_rule_routes.py):
// checks the carrier sets for a whole programme, for all or chosen broker
// contracts and/or treaties, that start checking for a broker or reinsurer
// once that party approves.

export type RuleOperator = "lte" | "gte" | "eq" | "in" | "not_in" | "lte_aggregate" | "required";
export type RuleScope = "none" | "all" | "selected";
export type RuleApprover = "broker" | "reinsurer" | "both";
export type RuleSeverity = "critical" | "warning" | "info";
export type AnswerStatus = "pending" | "approved" | "changes_requested";

/** One question a rule can ask — the same ones a contract's agreed limits ask. */
export interface RuleCheck {
  key: string; label: string; group: string;
  /** What the answer is: an amount, a percent, months, a list, text, or none (must be filled in). */
  value: "amount" | "percent" | "months" | "list" | "text" | null;
  /** Whether a reinsurance treaty can check it too. */
  treaty: boolean;
}

export interface RuleApproval {
  kind: "broker" | "reinsurer"; party_id: number; party: string;
  status: AnswerStatus; note: string | null; by: string | null; at: string | null;
}

export interface ProgrammeRule {
  id: number; program_id: number; name: string; description: string | null;
  check: string; operator: RuleOperator; operand: number | string | string[] | null;
  severity: RuleSeverity; sentence: string;
  broker_scope: RuleScope; broker_contract_ids: number[];
  treaty_scope: RuleScope; treaty_contract_ids: number[];
  approval_from: RuleApprover;
  status: "draft" | "sent" | "retired"; version: number;
  /** "Draft" · "Waiting for approval (2 of 3)" · "Changes requested by …" · "Active" · "Retired". */
  status_line: string;
  approvals: RuleApproval[];
  sent_at: string | null; updated_at: string | null;
}

export interface ContractChoice { id: number; name: string; party: string }

export interface ProgrammeRulesPage {
  /** false while migration 48 is not applied on this database; `message` says so. */
  ready?: boolean; message?: string;
  programme: { id: number; name: string };
  rules: ProgrammeRule[];
  checks: RuleCheck[];
  contracts: { broker: ContractChoice[]; treaty: ContractChoice[] };
  sent?: { brokers: number; reinsurers: number; emails: number };
}

export interface RuleInput {
  /** Optional: a rule with no name is called what it checks. */
  name: string; description?: string | null;
  check: string; operand: number | string | string[] | null;
  severity: RuleSeverity;
  broker_scope: RuleScope; broker_contract_ids: number[];
  treaty_scope: RuleScope; treaty_contract_ids: number[];
  approval_from: RuleApprover;
}

const p = () => ({ params: { mga: currentMga() } });

export const getProgrammeRules = (programId: number) =>
  api.get<ProgrammeRulesPage>(`/programs/${programId}/rules`, p()).then(r => r.data);
export const addProgrammeRule = (programId: number, body: RuleInput) =>
  api.post<ProgrammeRulesPage>(`/programs/${programId}/rules`, body, p()).then(r => r.data);
export const editProgrammeRule = (programId: number, id: number, body: RuleInput) =>
  api.put<ProgrammeRulesPage>(`/programs/${programId}/rules/${id}`, body, p()).then(r => r.data);
export const sendProgrammeRule = (programId: number, id: number) =>
  api.post<ProgrammeRulesPage>(`/programs/${programId}/rules/${id}/send`, null, p()).then(r => r.data);
export const retireProgrammeRule = (programId: number, id: number) =>
  api.post<ProgrammeRulesPage>(`/programs/${programId}/rules/${id}/retire`, null, p()).then(r => r.data);
export const deleteProgrammeRule = (programId: number, id: number) =>
  api.delete<ProgrammeRulesPage>(`/programs/${programId}/rules/${id}`, p()).then(r => r.data);

/** The dashboard: sent rules not yet approved by everyone they went to. */
export interface WaitingRule {
  id: number; program_id: number; programme: string; name: string; sentence: string;
  status_line: string; changes_requested: boolean; sent_at: string | null;
}
export const getWaitingRules = () =>
  api.get<{ rules: WaitingRule[]; waiting: number; changes_requested: number }>("/programme-rules/waiting", p())
    .then(r => r.data);

// ── the broker ──────────────────────────────────────────────────────────────
export interface BrokerRule {
  id: number; name: string; description: string | null; sentence: string;
  severity: RuleSeverity; version: number; programme: string | null; carrier: string | null;
  contracts: string[]; sent_at: string | null;
  status: AnswerStatus; note: string | null; at: string | null;
}

export const getBrokerRules = () =>
  api.get<{ rules: BrokerRule[]; waiting: number }>("/broker/programme-rules").then(r => r.data);
export const approveBrokerRule = (id: number) =>
  api.post<{ rules: BrokerRule[]; waiting: number }>(`/broker/programme-rules/${id}/approve`, {}).then(r => r.data);
export const requestBrokerRuleChanges = (id: number, note: string) =>
  api.post<{ rules: BrokerRule[]; waiting: number }>(`/broker/programme-rules/${id}/request-changes`, { note })
    .then(r => r.data);

// ── the reinsurer, on its treaty link ───────────────────────────────────────
export interface ReinsurerRule {
  id: number; name: string; description: string | null; sentence: string;
  severity: RuleSeverity; programme: string | null; treaties: string[];
  status: AnswerStatus; note: string | null; by: string | null; at: string | null;
}

const onLink = (token: string) => ({ headers: { "X-Treaty-Session": getViewSession(token) } });

export const getLinkRules = (token: string) =>
  api.get<{ rules: ReinsurerRule[] }>(`/treaty-view/${encodeURIComponent(token)}/programme-rules`, onLink(token))
    .then(r => r.data);
export const approveLinkRule = (token: string, id: number, signerName: string) =>
  api.post<{ rules: ReinsurerRule[] }>(`/treaty-view/${encodeURIComponent(token)}/programme-rules/${id}/approve`,
    { signer_name: signerName }, onLink(token)).then(r => r.data);
export const requestLinkRuleChanges = (token: string, id: number, note: string) =>
  api.post<{ rules: ReinsurerRule[] }>(`/treaty-view/${encodeURIComponent(token)}/programme-rules/${id}/request-changes`,
    { note }, onLink(token)).then(r => r.data);
