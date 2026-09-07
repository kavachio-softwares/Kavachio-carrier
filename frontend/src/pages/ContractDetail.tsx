import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft, FileText, ChevronDown, ChevronRight, Quote, ShieldAlert,
  ArrowRight, Wand2, AlertTriangle, CheckCircle2, X,
} from "lucide-react";
import { api } from "../api/client";
import { fmtStamp } from "../utils/date";
import Card from "../components/ui/Card";
import { PageBody, PageHeader } from "../components/Layout";
import { LoadingOverlay } from "../components/Busy";

type Rule = {
  validation_rule_id: number;
  rule_engine: string;
  rule_name: string;
  rule_description?: string;
  severity?: string;
  canonical_target?: any;
  rule_spec?: any;
  error_message?: string | null;
  rule_status?: string;
  generation_confidence?: number | null;
  source_clause?: { title?: string | null; text?: string | null; page_number?: number | null };
};

type ContractDetailPayload = {
  contract: {
    id: number; filename: string | null; status: string;
    output_template_id: number | null; extracted: any; created_at: string | null;
  };
  output_template: null | {
    id: number; name: string; version: number;
    fields?: Array<{ name: string; sheet?: string; canonical_field?: string | null }>;
  };
  rules: Rule[];
  clause_routing?: ClauseRouting[];
};

type CreatedRule = {
  rule_id: number;
  rule_name?: string | null;
  output_field?: string | null;
  // How many of the template's sample rows this rule would flag, when the
  // verifier could measure it. null when there was no sample data to run against.
  sample_impact?: { flagged: number; total: number } | null;
};

type ResolveResult = {
  ok: boolean;
  reason?: string;
  created_rules?: CreatedRule[];
};

type ClauseRouting = {
  clause_id: number | null;
  bucket: string;          // "review" (needs a field) | "control" (governance)
  rule_name?: string | null;
  clause_text?: string | null;
  source_page?: number | null;
  reason?: string | null;
};

const SEV: Record<string, string> = {
  critical: "pill pill-red",
  warning: "pill pill-amber",
  info: "pill pill-blue",
};

function outputField(rule: Rule): string {
  const t = rule.canonical_target || {};
  const s = rule.rule_spec || {};
  const vals = [
    t.output_field,
    ...(Array.isArray(t.output_fields) ? t.output_fields : []),
    s.field,
    ...(Array.isArray(s.fields) ? s.fields : []),
  ].filter(Boolean);
  return vals[0] || "Unmapped";
}

export default function ContractDetail() {
  const { programId, contractId } = useParams();
  const [data, setData] = useState<ContractDetailPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showTerms, setShowTerms] = useState(false);
  const [generatingRule, setGeneratingRule] = useState(false);
  // Outcome of the last resolution, held at CARD level. A successful resolve
  // takes the clause out of the queue, so its row — and any message inside it —
  // unmounts before anyone can read it. The reviewer would see the row vanish
  // and nothing else: not which rule was written, not that it flags most of the
  // sample data. This banner outlives the row.
  const [resolved, setResolved] =
    useState<null | { clause_id: number | null; rules: CreatedRule[] }>(null);

  const load = useCallback((opts?: { silent?: boolean }) => {
    if (!programId || !contractId) return;
    if (!opts?.silent) setLoading(true);
    setError(null);
    return api.get<ContractDetailPayload>(`/programs/${programId}/contracts/${contractId}`)
      .then(r => setData(r.data))
      .catch(err => {
        const detail = err?.response?.data?.detail;
        setError(typeof detail === "string" ? detail : "Could not load contract.");
      })
      .finally(() => { if (!opts?.silent) setLoading(false); });
  }, [programId, contractId]);

  useEffect(() => { load(); }, [load]);

  // Output field name → data model (canonical) field, from the template.
  const dataModelByField = useMemo(() => {
    const m = new Map<string, string>();
    for (const f of data?.output_template?.fields ?? []) {
      if (f.name && f.canonical_field) m.set(f.name, f.canonical_field);
    }
    return m;
  }, [data]);

  // Group rules by their output template field.
  const groups = useMemo(() => {
    const map = new Map<string, Rule[]>();
    for (const r of data?.rules ?? []) {
      const f = outputField(r);
      if (!map.has(f)) map.set(f, []);
      map.get(f)!.push(r);
    }
    return Array.from(map.entries());
  }, [data]);

  // Output-template field names for the "assign a field" dropdown.
  const fieldOptions = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const f of data?.output_template?.fields ?? []) {
      if (f.name && !seen.has(f.name)) { seen.add(f.name); out.push(f.name); }
    }
    return out.sort((a, b) => a.localeCompare(b));
  }, [data]);

  // Rule-bearing clauses that aren't mapped to any field yet (the review queue).
  const reviewClauses = useMemo(
    () => (data?.clause_routing ?? []).filter(r => r.bucket === "review"),
    [data]);

  const terms = useMemo(() => {
    const meta = data?.contract.extracted?.program_metadata || {};
    return Object.entries(meta)
      .map(([key, raw]: [string, any]) => ({
        key, value: raw?.value ?? raw, source: raw?.source_text,
      }))
      .filter(row => row.value != null && row.value !== "");
  }, [data]);

  return (
    <>
      {generatingRule && (
        <LoadingOverlay label="Generating the validation rule from this clause — this can take a minute…" />
      )}
      <PageHeader title="Contract"
        subtitle="Contract clauses mapped to output template fields and their validation rules." />
      <PageBody>
        <div className="mb-3">
          <Link to="/programs" className="inline-flex items-center gap-1 text-sm">
            <ArrowLeft size={14} /> Back To Programs
          </Link>
        </div>

        {error && !loading && <Card><p className="text-sm text-red-700">{error}</p></Card>}

        {data && !loading && (
          <div className="space-y-4">
            {/* Compact header */}
            <Card>
              <div className="flex items-start justify-between gap-4 flex-wrap">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <FileText size={16} className="text-accent shrink-0" />
                    <h2 className="text-base font-semibold truncate">
                      {data.contract.filename || `Contract #${data.contract.id}`}
                    </h2>
                    <span className="pill pill-green">{data.contract.status}</span>
                  </div>
                  <div className="mt-1.5 text-xs text-ink-muted">
                    Uploaded {fmtStamp(data.contract.created_at)}
                    {data.output_template && <> · Output template <span className="font-medium text-ink">{data.output_template.name} v{data.output_template.version}</span></>}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-2xl font-semibold">{data.rules.length}</div>
                  <div className="text-xs text-ink-muted">
                    rule{data.rules.length !== 1 ? "s" : ""} across {groups.length} field{groups.length !== 1 ? "s" : ""}
                  </div>
                </div>
              </div>
            </Card>

            {/* Mapping: Contract → Output field → Data model field, with rules */}
            <Card title="Mapping & Contract Rules"
              action={
                <span className="text-[11px] text-ink-muted flex items-center gap-1">
                  Contract Clause <ArrowRight size={10} /> Output Column <ArrowRight size={10} /> Data Model Field
                </span>
              }>
              {groups.length === 0 ? (
                /* Distinguish "nothing came out of it" from "nothing could yet".
                   A contract added before any Bordereau Setup exists has no
                   template to write rules against, and that is a complete,
                   expected outcome — not a failed extraction. */
                <p className="text-sm text-ink-muted py-2">
                  {data.output_template
                    ? "No validation rules were generated for this contract."
                    : "No output template was in place when this contract was read, "
                      + "and a rule is written against a template's columns — so its "
                      + "clauses were saved but no rules were written yet. The "
                      + "rule-bearing ones are listed below."}
                </p>
              ) : (
                <div className="space-y-3">
                  {groups.map(([field, rules]) => {
                    const dataModel = dataModelByField.get(field);
                    return (
                    <div key={field} className="rounded-lg border border-border overflow-hidden">
                      <div className="px-3 py-2 bg-surface-2 flex items-center justify-between gap-2 flex-wrap">
                        <span className="flex items-center gap-1.5 text-sm min-w-0">
                          <span className="font-medium truncate">{field}</span>
                          <ArrowRight size={12} className="text-ink-soft shrink-0" />
                          {dataModel
                            ? <span className="font-mono text-[11px] text-accent truncate">{dataModel}</span>
                            : <span className="text-[11px] text-ink-soft italic">unmapped in template</span>}
                        </span>
                        <span className="text-[11px] text-ink-muted shrink-0">
                          {rules.length} rule{rules.length !== 1 ? "s" : ""}
                        </span>
                      </div>
                      <div className="divide-y divide-border">
                        {rules.map(r => (
                          <div key={r.validation_rule_id} className="px-3 py-2.5">
                            <div className="flex items-center gap-2 flex-wrap">
                              <ShieldAlert size={13} className="text-accent shrink-0" />
                              <span className="font-medium text-sm">{r.rule_name}</span>
                              {r.severity && <span className={`${SEV[r.severity] || "pill pill-grey"} text-[11px]`}>{r.severity}</span>}
                            </div>
                            {r.rule_description && (
                              <p className="mt-1 text-xs text-ink-muted">{r.rule_description}</p>
                            )}
                            {r.source_clause?.text && (
                              <div className="mt-1.5 flex items-start gap-1.5 text-[11px] text-ink-muted">
                                <Quote size={11} className="shrink-0 mt-0.5 text-accent" />
                                <span className="italic leading-relaxed">
                                  "{r.source_clause.text}"
                                  {r.source_clause.page_number && <span className="not-italic text-ink-soft"> · p.{r.source_clause.page_number}</span>}
                                </span>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  );})}
                </div>
              )}
            </Card>

            {/* Review queue: rule-bearing clauses with no output field assigned.
                Stays on screen while `resolved` is set even once the queue empties,
                so the confirmation for the LAST clause resolved is still readable. */}
            {(reviewClauses.length > 0 || resolved) && (
              <Card title="Clauses Awaiting a Field"
                action={
                  reviewClauses.length > 0 ? (
                    <span className="pill pill-amber text-[11px]">
                      {reviewClauses.length} In Review
                    </span>
                  ) : (
                    <span className="pill pill-green text-[11px]">Queue clear</span>
                  )
                }>
                {resolved && <ResolvedBanner
                  rules={resolved.rules}
                  onDismiss={() => setResolved(null)} />}

                {reviewClauses.length === 0 ? (
                  <p className="text-sm text-ink-muted py-1">
                    Every rule-bearing clause on this contract is now bound to an
                    output column.
                  </p>
                ) : (
                <>
                <p className="text-xs text-ink-muted mb-3">
                  {data.output_template
                    ? <>These clauses are rule-bearing but couldn't be auto-mapped to an
                        output template field. Pick the field each one applies to and add
                        a reference note describing the rule logic to enforce — then
                        generate its validation rule.</>
                    : <>These clauses carry a rule, but this contract was read with no
                        output template, so there were no columns to bind them to. They
                        are held here until a Bordereau Setup gives this programme a
                        template — the document does not need reading again.</>}
                </p>
                <div className="space-y-3">
                  {reviewClauses.map((rc, i) => (
                    <ReviewQueueRow
                      key={`${rc.clause_id ?? "x"}-${i}`}
                      item={rc}
                      fieldOptions={fieldOptions}
                      onResolve={async (outputFields, note) => {
                        setGeneratingRule(true);
                        try {
                          const r = await api.post<ResolveResult>(
                            `/programs/${programId}/contracts/${contractId}` +
                            `/clause-routing/${rc.clause_id}/resolve`,
                            { output_fields: outputFields, note: note || undefined },
                          );
                          if (r.data?.ok) {
                            setResolved({
                              clause_id: rc.clause_id,
                              rules: r.data.created_rules ?? [],
                            });
                            await load({ silent: true });
                          }
                          return r.data;
                        } finally { setGeneratingRule(false); }
                      }}
                    />
                  ))}
                </div>
                </>
                )}
              </Card>
            )}

            {/* Collapsible AI-extracted terms */}
            {terms.length > 0 && (
              <Card>
                <button onClick={() => setShowTerms(s => !s)}
                  className="w-full flex items-center gap-2 text-sm font-medium">
                  {showTerms ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  AI-Extracted Terms ({terms.length})
                </button>
                {showTerms && (
                  <div className="mt-3 overflow-x-auto">
                    <table>
                      <thead><tr><th>Term</th><th>Value</th><th>Source</th></tr></thead>
                      <tbody>
                        {terms.map(row => (
                          <tr key={row.key}>
                            <td className="font-medium capitalize">{row.key.replace(/_/g, " ")}</td>
                            <td>{String(row.value)}</td>
                            <td className="max-w-md text-xs text-ink-muted">{row.source || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            )}
          </div>
        )}
      </PageBody>
    </>
  );
}

/** What the last resolution produced, kept on screen after its clause row leaves
 *  the queue. Names each rule and the column it bound to, and — where the verifier
 *  could measure it — how much of the template's sample data the rule flags. A rule
 *  that fires on most sample rows is the readable sign that the chosen column was
 *  the wrong one, which is otherwise invisible until a bordereau is run. */
function ResolvedBanner({
  rules, onDismiss,
}: {
  rules: CreatedRule[];
  onDismiss: () => void;
}) {
  // "Most" is deliberately a majority rather than a tuned number: sample rows are
  // compliant example data, so a correct rule should flag few of them. Half is the
  // point past which "the rule is wrong" beats "the data has exceptions".
  const noisy = rules.filter(r =>
    r.sample_impact && r.sample_impact.total > 0 &&
    r.sample_impact.flagged * 2 > r.sample_impact.total);

  return (
    <div className={`mb-3 rounded-lg border p-3 ${
      noisy.length ? "border-amber-300 bg-amber-50" : "border-green-300 bg-green-50"}`}>
      <div className="flex items-start gap-2">
        {noisy.length
          ? <AlertTriangle size={14} className="text-amber-600 shrink-0 mt-0.5" />
          : <CheckCircle2 size={14} className="text-green-600 shrink-0 mt-0.5" />}
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">
            {rules.length === 0
              ? "Clause resolved."
              : `Generated ${rules.length} rule${rules.length !== 1 ? "s" : ""}.`}
          </p>
          <ul className="mt-1 space-y-0.5">
            {rules.map(r => (
              <li key={r.rule_id} className="text-[11px] text-ink-muted">
                <span className="font-medium text-ink">{r.rule_name}</span>
                {r.output_field && <> <ArrowRight size={9} className="inline" />{" "}
                  <span className="font-mono">{r.output_field}</span></>}
                {r.sample_impact && r.sample_impact.total > 0 && (
                  <> · flags {r.sample_impact.flagged} of {r.sample_impact.total}{" "}
                    sample row{r.sample_impact.total !== 1 ? "s" : ""}</>
                )}
              </li>
            ))}
          </ul>
          {noisy.length > 0 && (
            <p className="mt-1.5 text-[11px] text-amber-800">
              This flags most of the template's sample data. Sample rows are
              compliant examples, so that usually means the rule landed on the
              wrong column — check it in the Rule Library before a bordereau runs
              against it.
            </p>
          )}
        </div>
        <button type="button" onClick={onDismiss}
          className="text-ink-muted hover:text-ink shrink-0" aria-label="Dismiss">
          <X size={13} />
        </button>
      </div>
    </div>
  );
}

function ReviewQueueRow({
  item, fieldOptions, onResolve,
}: {
  item: ClauseRouting;
  fieldOptions: string[];
  onResolve: (outputFields: string[], note: string) =>
    Promise<{ ok: boolean; reason?: string; created_rules?: any[] }>;
}) {
  const [fields, setFields] = useState<string[]>([]);
  const [pick, setPick] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] =
    useState<null | { ok: boolean; message: string }>(null);

  const addField = (f: string) => {
    if (!f) return;
    setFields(prev => prev.includes(f) ? prev : [...prev, f]);
    setPick("");
  };
  const removeField = (f: string) => setFields(prev => prev.filter(x => x !== f));
  const remaining = fieldOptions.filter(o => !fields.includes(o));

  const submit = async () => {
    if (fields.length === 0 || busy) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await onResolve(fields, note.trim());
      if (r.ok) {
        const n = r.created_rules?.length ?? 0;
        setResult({ ok: true, message: `Generated ${n} rule${n !== 1 ? "s" : ""}.` });
      } else {
        setResult({ ok: false, message: r.reason || "Could not generate a rule for these fields." });
      }
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      setResult({ ok: false, message: typeof detail === "string" ? detail : "Request failed." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-border p-3">
      <div className="flex items-center gap-2">
        <AlertTriangle size={13} className="text-amber-500 shrink-0" />
        <span className="font-medium text-sm">
          {item.rule_name || "Unmapped Clause"}
        </span>
        {item.source_page && (
          <span className="text-[11px] text-ink-soft">p.{item.source_page}</span>
        )}
      </div>
      {item.clause_text && (
        <div className="mt-1.5 flex items-start gap-1.5 text-[11px] text-ink-muted">
          <Quote size={11} className="shrink-0 mt-0.5 text-accent" />
          <span className="italic leading-relaxed line-clamp-3">"{item.clause_text}"</span>
        </div>
      )}
      {/* The reason is a RECORD of what happened when the contract was read, not a
          statement about now — a contract read before its programme had an output
          template keeps saying so long after a template exists. Dating the line
          stops it contradicting the paragraph above, which describes today. */}
      {item.reason && (
        <p className="mt-1.5 text-[11px] text-amber-700">
          Why it wasn't auto-mapped when the contract was read: {item.reason}
        </p>
      )}

      {/* Step 1 — choose the field(s) */}
      <label className="mt-2.5 block text-[11px] font-medium text-ink-muted">
        1. Output Column(s) — pick one, or several when the rule spans columns (first is primary)
      </label>
      <select
        value={pick}
        onChange={e => addField(e.target.value)}
        disabled={busy}
        className="mt-1 text-sm border border-border rounded-md px-2 py-1.5 bg-surface max-w-xs">
        <option value="">{fields.length ? "Add Another Field…" : "Select Output Column…"}</option>
        {remaining.map(f => <option key={f} value={f}>{f}</option>)}
      </select>
      {fields.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {fields.map((f, i) => (
            <span key={f} className="inline-flex items-center gap-1 text-[11px] rounded-full
                                     bg-surface border border-border px-2 py-0.5">
              {i === 0 && <span className="text-accent font-semibold">Primary</span>}
              <span>{f}</span>
              <button type="button" disabled={busy} onClick={() => removeField(f)}
                className="text-ink-muted hover:text-red-600 disabled:opacity-50"
                aria-label={`Remove ${f}`}>
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Step 2 — describe the rule logic */}
      <label className="mt-2.5 block text-[11px] font-medium text-ink-muted">
        2. Reference note — the rule logic for this field/clause (and why)
      </label>
      <textarea
        value={note}
        onChange={e => setNote(e.target.value)}
        disabled={busy}
        rows={3}
        placeholder="e.g. If Country is USA, the state must not be Puerto Rico, US Virgin Islands, or US Territories — this column reports the insured's home state."
        className="mt-1 w-full text-xs border border-border rounded-md px-2 py-1.5
                   bg-surface resize-y placeholder:text-ink-soft" />

      {/* Step 3 — generate */}
      <div className="mt-2.5">
        <button
          onClick={submit}
          disabled={fields.length === 0 || busy}
          className="inline-flex items-center gap-1.5 text-sm rounded-md px-3 py-1.5
                     bg-accent text-white disabled:opacity-50">
          <Wand2 size={13} /> Generate Rule
        </button>
      </div>

      {result && (
        <p className={`mt-2 text-[11px] flex items-center gap-1 ${result.ok ? "text-green-700" : "text-red-700"}`}>
          {result.ok ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
          {result.message}
        </p>
      )}
    </div>
  );
}
