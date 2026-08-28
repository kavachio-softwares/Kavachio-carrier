import { api } from "./client";

// Managed BDX rule-library entries. Two scopes decided server-side from the
// caller's token: kavachio_admin manages GLOBAL rules (apply to every broker);
// a tenant_admin manages their own tenant's rules (apply to all their programs,
// invisible to other tenants). The client never sets the scope.

export type RuleClass = { class_name: string; label: string; hint?: string; operator: string };
export type RuleScope = "global" | "tenant";

export type Rule = {
  id: number;
  rule_name: string;
  class_name: string;
  class_label: string;              // friendly label for class_name (generic or legacy)
  severity: string;                 // Critical | Major | Minor
  validation_logic: string | null;
  is_active: boolean;
  scope: RuleScope;
  tenant_id: number | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type RuleInput = {
  rule_name: string;
  class_name: string;
  severity: string;
  validation_logic?: string | null;
  is_active: boolean;
};

// The rule-type catalogue driving the create/edit dropdown. A rule only runs if
// its class_name is one of these, so the form offers exactly these choices.
export async function getRuleCatalogue() {
  const r = await api.get<{ classes: RuleClass[]; severities: string[] }>(
    "/rule-library/classes",
  );
  return r.data;
}

export async function listRules() {
  const r = await api.get<{ items: Rule[]; total: number }>("/rule-library");
  return r.data;
}

// No dedicated GET-one endpoint: the list is small and scope-filtered, so the
// edit form loads it once and picks the row by id.
export async function getRule(id: number): Promise<Rule | undefined> {
  const { items } = await listRules();
  return items.find(r => r.id === id);
}

export async function createRule(body: RuleInput) {
  const r = await api.post<Rule>("/rule-library", body);
  return r.data;
}

export async function updateRule(id: number, body: RuleInput) {
  const r = await api.put<Rule>(`/rule-library/${id}`, body);
  return r.data;
}

export async function toggleRule(id: number, is_active: boolean) {
  const r = await api.patch<Rule>(`/rule-library/${id}`, { is_active });
  return r.data;
}

export async function deleteRule(id: number) {
  await api.delete(`/rule-library/${id}`);
}
