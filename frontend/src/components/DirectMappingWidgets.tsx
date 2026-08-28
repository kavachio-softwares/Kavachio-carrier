import { ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, ArrowRight, CheckCircle2, ChevronDown, ChevronRight, Plus,
  RotateCcw, Save, Search, ShieldAlert, Sparkles, Trash2, X,
} from "lucide-react";
import { api } from "../api/client";
import { getUser } from "../auth";
import Banner from "./ui/Banner";
import Button from "./ui/Button";
import { Modal } from "./ui/Modal";
import { ClauseText } from "./ClauseText";
import {
  ClauseRouting, ContractDetailT, ContractRule, VariationDecision, VariationRemoval,
  errText,
} from "../utils/directSetup";

// Templates whose matching is widened by a surface SPELLING of a value the
// contract names. Mirrors rule_editor.VARIATION_TEMPLATES on the server.
const VARIATION_TEMPLATES = [
  "value_in_set", "value_not_in_set", "conditional_value", "conditional_all",
];

function isVariationRule(r: ContractRule): boolean {
  return VARIATION_TEMPLATES.includes(r.rule_template ?? "");
}

// LIBRARY content — the shared house rules and the formulas we derive from the
// bordereau's own columns. They carry these markers in their text (set by
// generic_rule_library / rule_explainer) and they are the bulk of what a
// contract shows: on a real setup, 27 of 45 rules and 27 of 32 awaiting-a-field
// clauses. They are worth keeping reachable but not worth putting first — a
// reviewer opens a contract to see what THIS contract says.
const _LIBRARY_TEXT = /^\s*\[\s*(generic rule|derived (formula|rule))\s*\]/i;
const isLibraryText = (s: string | null | undefined) => _LIBRARY_TEXT.test(s ?? "");
/** A rule with no quoted clause behind it did not come from this contract's
 *  prose — that absence is exactly what the card renders as "no quote". */
const isLibraryRule = (r: ContractRule) => !(r.source_clause?.text ?? "").trim();

/** A section that folds away. Used for the library content above, so the
 *  contract's own rules and clauses are what you see on open. */
function Foldaway({ label, count, children, defaultOpen = false }: {
  label: string; count: number; children: ReactNode; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (count === 0) return null;
  return (
    <div className="rounded-md border border-border/70 bg-surface-2/40">
      <button type="button" onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-1.5 px-2 py-1.5 text-xs text-ink-muted hover:text-ink">
        {open ? <ChevronDown size={12} className="shrink-0" />
              : <ChevronRight size={12} className="shrink-0" />}
        <span>{label}</span>
        <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[10px]">{count}</span>
      </button>
      {open && <div className="px-2 pb-2">{children}</div>}
    </div>
  );
}

/** Label for a rule's variation list. The two enum templates mean OPPOSITE
 *  things — one widens what passes, the other widens what is caught — so they
 *  must never share wording. */
function spellingLabel(r: ContractRule): string {
  return r.rule_template === "value_not_in_set"
    ? "Variations Of The Prohibited Values"
    : "Accepted Variations";
}

// Small widgets shared between the Bordereau Setup mapping UIs (the builder's
// pre-build helpers and the setup editor) — a rule's confidence chip, a
// contract's status pill, the AI-extracted-terms formatter, the searchable
// output-field combo box, and the full contract inline editor (terms, rules,
// clause routing, tolerance bands). Kept in one place so both surfaces render
// a contract identically instead of drifting apart.

export function ScoreChip({ score, label = "Kavachio" }: { score: number; label?: string }) {
  const pct = Math.round(score * 100);
  const tone = score >= 0.9 ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
    : score >= 0.6 ? "bg-amber-50 text-amber-700 ring-amber-200"
    : "bg-surface-2 text-ink-muted ring-border";
  return (
    <span className={`inline-flex items-center gap-1 text-[10px] rounded-full px-1.5 py-0.5 ring-1 ${tone}`}>
      <Sparkles size={9} /> {label} {pct}%
    </span>
  );
}

// Monospace source-sheet / label pill (matches the prototype's tag-pill).
export function TagPill({ children }: { children: React.ReactNode }) {
  return (
    <span className="font-mono text-[11px] bg-surface-2 border border-border rounded px-1.5 py-0.5 text-ink-muted">
      {children}
    </span>
  );
}

// Severity badge for a generated rule (critical / warning / info).
export function SevBadge({ severity }: { severity?: string }) {
  const s = (severity || "").toLowerCase();
  const { tone, label } = s.includes("crit") || s === "error"
    ? { tone: "bg-red-50 text-red-700", label: "Critical" }
    : s.includes("warn")
      ? { tone: "bg-amber-50 text-amber-700", label: "Warning" }
      : s
        ? { tone: "bg-blue-50 text-blue-700", label: severity! }
        : { tone: "bg-surface-2 text-ink-muted", label: "Rule" };
  return (
    <span className={`text-[9px] font-bold uppercase tracking-wide rounded px-1.5 py-0.5 ${tone}`}>
      {label}
    </span>
  );
}

export function ClauseMatchChip({ match, score }: { match: string; score?: number }) {
  const exact = match === "exact";
  return (
    <span className={`text-[10px] rounded-full px-1.5 py-0.5 ${
      exact ? "bg-emerald-50 text-emerald-700" : "bg-surface-2 text-ink-muted"}`}>
      {exact ? "Exact Match" : "Related"}{score != null ? ` · ${Math.round(score * 100)}%` : ""}
    </span>
  );
}

export function ContractStatusChip({ status }: { status: string }) {
  const tone = status === "active" ? "bg-emerald-100 text-emerald-700"
    : status === "superseded" ? "bg-surface-2 text-ink-muted"
    : status === "failed" ? "bg-danger/10 text-danger"
    : "bg-amber-50 text-amber-700";
  return <span className={`text-[11px] rounded-full px-2 py-0.5 shrink-0 ${tone}`}>{status}</span>;
}

export function fmtTermValue(v: unknown): string {
  if (v == null || v === "") return "—";
  if (typeof v === "object") {
    const o = v as { value?: unknown };
    return o.value != null ? String(o.value) : JSON.stringify(v);
  }
  return String(v);
}

export function Combo({ value, options, placeholder, onSelect, clearable = true, disabled = false }: {
  value?: string; options: string[]; placeholder: string;
  onSelect: (v: string | null) => void; clearable?: boolean; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const btnRef = useRef<HTMLButtonElement>(null);
  // Fixed-position menu so it escapes the table's overflow clipping; flips up
  // when there isn't enough room below the trigger.
  const [box, setBox] = useState<{ left: number; top: number; bottom: number; width: number; up: boolean; maxH: number } | null>(null);

  const place = useCallback(() => {
    const el = btnRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const below = window.innerHeight - r.bottom;
    const above = r.top;
    const up = below < 240 && above > below;
    setBox({ left: r.left, top: r.top, bottom: r.bottom, width: r.width, up, maxH: Math.max(160, (up ? above : below) - 12) });
  }, []);

  useEffect(() => {
    if (!open) return;
    place();
    const onMove = () => place();
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
    return () => { window.removeEventListener("scroll", onMove, true); window.removeEventListener("resize", onMove); };
  }, [open, place]);

  const close = () => { setOpen(false); setQ(""); };
  const filtered = options.filter(o => o.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <button ref={btnRef} type="button" disabled={disabled} onClick={() => setOpen(o => !o)}
        className="input w-full text-left flex items-center justify-between disabled:opacity-50 disabled:cursor-not-allowed">
        <span className={value ? "" : "text-ink-muted"}>{value || placeholder}</span>
        <Search size={13} className="text-ink-muted shrink-0" />
      </button>
      {open && box && (
        <>
          <div className="fixed inset-0 z-40" onClick={close} />
          <div className="fixed z-50 bg-white border border-border rounded-md shadow-lg overflow-auto"
            style={{
              left: box.left, width: box.width, maxHeight: box.maxH,
              ...(box.up
                ? { bottom: window.innerHeight - box.top + 4 }
                : { top: box.bottom + 4 }),
            }}>
            <input autoFocus value={q} onChange={e => setQ(e.target.value)} placeholder="Search…"
              className="input m-1" style={{ width: "calc(100% - 0.5rem)" }} />
            {clearable && (
              <button className="block w-full text-left px-3 py-1.5 text-sm hover:bg-surface-2 text-ink-muted"
                onClick={() => { onSelect(null); close(); }}>— Not Mapped —</button>
            )}
            {filtered.map(o => (
              <button key={o} className="block w-full text-left px-3 py-1.5 text-sm hover:bg-surface-2"
                onClick={() => { onSelect(o); close(); }}>{o}</button>
            ))}
            {filtered.length === 0 && <div className="px-3 py-2 text-xs text-ink-muted">No matches.</div>}
          </div>
        </>
      )}
    </>
  );
}

// Inline detail for one contract — extracted terms + clause→field rules, the
// same context the standalone Contract page shows, without leaving setup.
// Each rule can be retargeted to a different output field or removed.
export function ContractInline({ detail, programId, contractId, mga, onChanged,
                                 readOnly = false, canEditVariations = false }: {
  detail: ContractDetailT; programId: number | ""; contractId: number;
  mga: string; onChanged: () => Promise<void> | void; readOnly?: boolean;
  /** Show the "add a spelling" control on value-matching rules. Deliberately
   *  SEPARATE from `readOnly`: the read-only viewer (Bordereau Setups)
   *  is exactly where a tenant_admin corrects a spelling, so gating on !readOnly
   *  would make the feature dead on the one screen it exists for. */
  canEditVariations?: boolean;
}) {
  const rules = detail.rules ?? [];
  const terms = detail.terms ?? [];
  const maps = detail.field_mappings ?? [];
  const fieldOptions = Array.from(new Set((detail.output_template?.fields ?? [])
    .map(f => f.name).filter(Boolean)));
  // Rule-bearing clauses that couldn't be auto-mapped to any output field,
  // split so the contract's own come first and the house rules fold away.
  const reviewClauses = (detail.clause_routing ?? []).filter(r => r.bucket === "review");
  const ownClauses = reviewClauses.filter(r => !isLibraryText(r.clause_text));
  const libraryClauses = reviewClauses.filter(r => isLibraryText(r.clause_text));
  // Same split for rules: this contract's, then the shared/derived ones.
  const ownRules = rules.filter(r => !isLibraryRule(r));
  const libraryRules = rules.filter(r => isLibraryRule(r));

  // The server keys field_mappings by (clause, column), so a rule spanning
  // three columns arrives as three rows repeating one long clause string.
  // Collapse to one row per clause with its columns as chips.
  const mapGroups = useMemo(() => {
    const by = new Map<string, { clause: string; fields: string[] }>();
    for (const m of maps) {
      const g = by.get(m.contract_field) ?? { clause: m.contract_field, fields: [] };
      if (!g.fields.includes(m.output_field)) g.fields.push(m.output_field);
      by.set(m.contract_field, g);
    }
    return [...by.values()];
  }, [maps]);
  const [busyRule, setBusyRule] = useState<number | null>(null);
  const [ruleErr, setRuleErr] = useState<string | null>(null);
  // The rule pending removal — drives the in-app confirm dialog. A native
  // window.confirm() here looked like a browser popup rather than the product.
  const [ruleToRemove, setRuleToRemove] = useState<ContractRule | null>(null);
  // Staged (unsaved) output-field changes, keyed by `${ruleId}::${currentField}`
  // → newField. Nothing hits the server until the user clicks Save.
  const [pending, setPending] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const pendingCount = Object.keys(pending).length;
  // Per-rule tolerance-band drafts (ruleId → {flag %, reject %}); saved
  // independently of the output-field batch above via the tolerance endpoint.
  const [tolDraft, setTolDraft] = useState<Record<number, { tolerance_pct: string; reject_pct: string }>>({});
  const [tolBusy, setTolBusy] = useState<number | null>(null);
  // "Add a spelling" dialog. `varResult` holds the server's DECISION — which is a
  // 200 whether or not the spelling was accepted, so the dialog stays open and
  // shows the reason (and the contract's own words) rather than throwing.
  const [varRule, setVarRule] = useState<ContractRule | null>(null);
  // Committed chips, plus whatever is being typed. Keeping the two apart is what
  // lets a variation containing a comma survive: only the typed text is ever
  // split, and only at the moment it is committed.
  const [varChips, setVarChips] = useState<string[]>([]);
  const [varDraft, setVarDraft] = useState("");
  const [varBusy, setVarBusy] = useState(false);
  const [varResult, setVarResult] = useState<VariationDecision | null>(null);
  // Variations marked for removal but NOT yet sent — ✕ stages, the Remove button
  // commits, so a misclick costs nothing and several removals are one write.
  const [varDrop, setVarDrop] = useState<string[]>([]);
  const [varRemoving, setVarRemoving] = useState(false);
  const [varRemoved, setVarRemoved] = useState<VariationRemoval | null>(null);
  // Errors raised INSIDE the dialog stay inside it — surfacing them on the page
  // behind would mean closing the dialog to read why the click did nothing.
  const [varErr, setVarErr] = useState<string | null>(null);
  const varInputRef = useRef<HTMLInputElement>(null);

  const actor = getUser()?.email;

  const tolOrig = (r: ContractRule) => ({
    tolerance_pct: r.tolerance_pct != null ? String(r.tolerance_pct) : "",
    reject_pct: r.reject_pct != null ? String(r.reject_pct) : "",
  });
  const tolValue = (r: ContractRule) => tolDraft[r.validation_rule_id] ?? tolOrig(r);
  function setTol(r: ContractRule, patch: Partial<{ tolerance_pct: string; reject_pct: string }>) {
    setTolDraft(prev => ({ ...prev, [r.validation_rule_id]: { ...tolValue(r), ...patch } }));
  }
  function tolDirty(r: ContractRule): boolean {
    const d = tolDraft[r.validation_rule_id];
    if (!d) return false;
    const o = tolOrig(r);
    return d.tolerance_pct !== o.tolerance_pct || d.reject_pct !== o.reject_pct;
  }

  function stageField(rule: ContractRule, oldField: string, newField: string | null) {
    setPending(prev => {
      const key = `${rule.validation_rule_id}::${oldField}`;
      const next = { ...prev };
      if (!newField || newField === oldField) delete next[key];   // back to original = no change
      else next[key] = newField;
      return next;
    });
  }

  async function saveChanges() {
    const entries = Object.entries(pending);
    if (!entries.length || saving) return;
    setSaving(true); setRuleErr(null);
    try {
      for (const [key, newField] of entries) {
        const sep = key.indexOf("::");
        const ruleId = Number(key.slice(0, sep));
        const oldField = key.slice(sep + 2);
        await api.put(
          `/programs/${programId}/contracts/${contractId}/rules/${ruleId}/output-field`,
          { new_field: newField, old_field: oldField || undefined, mga, actor });
      }
      setPending({});
      await onChanged();
    } catch (e: unknown) { setRuleErr(errText(e)); } finally { setSaving(false); }
  }
  async function removeRule(rule: ContractRule) {
    if (busyRule !== null) return;
    setBusyRule(rule.validation_rule_id); setRuleErr(null);
    try {
      await api.delete(
        `/programs/${programId}/contracts/${contractId}/rules/${rule.validation_rule_id}`,
        { params: { mga, actor } });
      setRuleToRemove(null);
      await onChanged();
    } catch (e: unknown) {
      setRuleErr(errText(e));
      setRuleToRemove(null);   // surface the error on the page, not behind the dialog
    } finally { setBusyRule(null); }
  }
  async function saveTolerance(r: ContractRule) {
    if (tolBusy !== null) return;
    const d = tolValue(r);
    const body: Record<string, unknown> = { mga, actor };
    // Blank flag% leaves the existing value; blank reject% clears the reject band.
    if (d.tolerance_pct.trim() !== "") body.tolerance_pct = Number(d.tolerance_pct);
    if (d.reject_pct.trim() === "") body.clear_reject = true;
    else body.reject_pct = Number(d.reject_pct);
    setTolBusy(r.validation_rule_id); setRuleErr(null);
    try {
      await api.put(
        `/programs/${programId}/contracts/${contractId}/rules/${r.validation_rule_id}/tolerance`,
        body);
      setTolDraft(prev => { const n = { ...prev }; delete n[r.validation_rule_id]; return n; });
      await onChanged();
    } catch (e: unknown) { setRuleErr(errText(e)); } finally { setTolBusy(null); }
  }

  /** Every piece of dialog state, cleared together. Split out because opening and
   *  closing must reset the SAME set — a leftover staged removal or result banner
   *  from the previous rule is the kind of thing that gets acted on by mistake. */
  function resetVariationState() {
    setVarResult(null); setVarRemoved(null); setVarErr(null);
    setVarChips([]); setVarDraft(""); setVarDrop([]);
  }

  function openVariation(r: ContractRule) {
    setRuleErr(null); resetVariationState(); setVarRule(r);
  }

  function closeVariation() {
    setVarRule(null); resetVariationState();
  }

  /** Split text into separate variations. Used for the typed box and for PASTE,
   *  so pasting a column out of a spreadsheet still lands as one chip per value.
   *  NEWLINE/TAB/SEMICOLON only — never comma. A spreadsheet column pastes as
   *  newline- or tab-separated, never comma-separated, and a comma is routine
   *  inside a single value (a legal suffix: "Demoshield Specialty Insurance
   *  Company, Inc."). Splitting on it there tore one company into two chips —
   *  the same reason varKeyDown below no longer treats a typed comma as a
   *  commit either. */
  function parseVariations(draft: string): string[] {
    return draft.split(/[\n;\t]/).map(s => s.trim()).filter(Boolean);
  }

  const varNorm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");

  /** Turn the typed text into chips. Duplicates — of each other, or of something
   *  the rule already accepts — are dropped here rather than sent, because the
   *  server would only refuse them and the round trip teaches nothing. */
  function commitDraft(text?: string): boolean {
    const raw = text ?? varDraft;
    const parts = parseVariations(raw);
    if (!parts.length) return false;
    const known = new Set([
      ...varChips,
      ...(varRule?.variation_values ?? []),
      ...(varRule?.vocabulary_values ?? []),
    ].map(varNorm));
    const next = [...varChips];
    for (const p of parts) {
      const k = varNorm(p);
      if (!k || known.has(k)) continue;
      known.add(k);
      next.push(p);
    }
    setVarChips(next);
    setVarDraft("");
    return next.length > varChips.length;
  }

  function varKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault(); commitDraft(); void submitVariation(); return;
    }
    // Comma is NOT a commit key, on purpose — a company's own legal name
    // routinely carries one ("Demoshield Specialty Insurance Company, Inc."),
    // and treating it as "value ends here" tore that single name into two
    // chips while it was being typed, same as the paste case parseVariations
    // guards against above.
    if (e.key === "Enter" || e.key === "Tab") {
      // Tab only steals focus when there is something to commit, so keyboard
      // navigation through an empty box still works.
      if (e.key === "Tab" && !varDraft.trim()) return;
      e.preventDefault(); commitDraft(); return;
    }
    // Backspace on an empty box pulls the last chip BACK INTO the box rather than
    // deleting it outright — a mistyped variation is then corrected, not retyped.
    if (e.key === "Backspace" && !varDraft && varChips.length) {
      e.preventDefault();
      setVarDraft(varChips[varChips.length - 1]);
      setVarChips(varChips.slice(0, -1));
    }
  }

  /** Chips plus whatever is still half-typed — nobody should lose a variation for
   *  not pressing Enter before clicking the button. */
  function pendingVariations(): string[] {
    const out: string[] = [];
    const seen = new Set<string>();
    for (const v of [...varChips, ...parseVariations(varDraft)]) {
      const k = varNorm(v);
      if (!k || seen.has(k)) continue;
      seen.add(k);
      out.push(v);
    }
    return out;
  }

  async function submitVariation() {
    const variations = pendingVariations();
    if (!varRule || !variations.length || varBusy) return;
    setVarBusy(true); setVarResult(null); setVarRemoved(null);
    try {
      // `silent` keeps the app-wide loading overlay from blanking this dialog —
      // the checker may run a model call, so it is seconds, not milliseconds.
      const res = await api.post<VariationDecision>(
        `/programs/${programId}/contracts/${contractId}` +
        `/rules/${varRule.validation_rule_id}/variation-values`,
        { variations, mga, actor }, { silent: true });
      setVarResult(res.data);
      // Accepted chips have done their job and are now shown in the rule's own
      // list; REFUSED ones stay as chips so they can be edited rather than
      // retyped from scratch.
      const refused = new Set((res.data?.results || [])
        .filter(r => !r.accepted).map(r => varNorm(r.spelling)));
      setVarChips(variations.filter(v => refused.has(varNorm(v))));
      setVarDraft("");
      if (res.data?.accepted_count) await onChanged();
    } catch (e: unknown) {
      // 4xx only: wrong rule type, wrong role, or not yours. A refused VARIATION
      // is not an error and never lands here.
      setRuleErr(errText(e)); setVarRule(null);
    } finally { setVarBusy(false); }
  }

  /** Stage / unstage a ✕ click. Nothing is sent until "Remove" is pressed, so a
   *  misclick costs nothing and several removals commit as ONE recompile and one
   *  write instead of one of each. */
  function toggleRemoval(spelling: string) {
    setVarErr(null);
    setVarDrop(prev => prev.some(s => varNorm(s) === varNorm(spelling))
      ? prev.filter(s => varNorm(s) !== varNorm(spelling))
      : [...prev, spelling]);
  }

  /** Commit the staged removals. No checker runs — narrowing a rule back toward
   *  what the contract says needs no permission from a model. */
  async function commitRemovals() {
    if (!varRule || !varDrop.length || varBusy || varRemoving) return;
    setVarRemoving(true); setVarResult(null); setVarRemoved(null); setVarErr(null);
    try {
      const res = await api.post<VariationRemoval>(
        `/programs/${programId}/contracts/${contractId}` +
        `/rules/${varRule.validation_rule_id}/variation-values/remove`,
        { variations: varDrop, mga, actor }, { silent: true });
      setVarRemoved(res.data);
      // Anything the server refused stays staged, so the reason sits next to a
      // chip that is still marked — nothing silently un-marks itself.
      const refused = new Set((res.data?.results || [])
        .filter(r => !r.removed).map(r => varNorm(r.spelling)));
      setVarDrop(varDrop.filter(s => refused.has(varNorm(s))));
      if (res.data?.removed_count) await onChanged();
    } catch (e: unknown) {
      // A 400 here is a wrong rule type / role / ownership problem, not a refused
      // spelling. Show it in the dialog rather than closing it.
      setVarErr(errText(e));
    } finally { setVarRemoving(false); }
  }

  // One awaiting-a-field clause. Same reason as renderRule — two lists, one
  // row, so a folded standard rule stays as resolvable as a contract clause.
  function renderReviewClause(rc: ClauseRouting) {
    return (
      <ReviewClauseRow key={`${rc.clause_id ?? "x"}-${rc.rule_name ?? ""}`}
        item={rc} fieldOptions={fieldOptions} readOnly={readOnly}
        onResolve={async (outputFields, note) => {
          const r = await api.post(
            `/programs/${programId}/contracts/${contractId}` +
            `/clause-routing/${rc.clause_id}/resolve`,
            { output_fields: outputFields, note: note || undefined, actor, mga });
          if (r.data?.ok) await onChanged();
          return r.data as { ok: boolean; reason?: string; created_rules?: unknown[] };
        }} />
    );
  }

  // One rule card. Extracted so the contract's own rules and the folded
  // library rules render identically from two lists.
  function renderRule(r: ContractRule) {
    const busy = busyRule === r.validation_rule_id;
            const canRetarget = r.rule_kind === "ir_v1" && fieldOptions.length > 0;
            const boundFields = (r.output_fields && r.output_fields.length)
              ? r.output_fields : (r.output_field ? [r.output_field] : []);
            const multiField = boundFields.length > 1;
            return (
              <div key={r.validation_rule_id} className="rounded-md border border-border/70 p-2 text-xs">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <ShieldAlert size={12} className="text-navy shrink-0" />
                  <span className="font-medium">{r.rule_name}</span>
                  {r.severity && <span className="rounded bg-surface-2 px-1.5 py-0.5">{r.severity}</span>}
                  {!readOnly && (
                    <button onClick={() => { setRuleErr(null); setRuleToRemove(r); }} disabled={busy}
                      title="Remove this rule"
                      className="ml-auto inline-flex items-center gap-1 text-ink-muted hover:text-danger disabled:opacity-50">
                      <Trash2 size={12} /> Remove
                    </button>
                  )}
                </div>
                {r.rule_description && <p className="mt-0.5 text-ink-muted">{r.rule_description}</p>}
                {r.source_clause?.text && (
                  <div className="mt-0.5 italic text-ink-muted">
                    <ClauseText text={r.source_clause.text} />
                    {r.source_clause.page_number ? <span className="not-italic"> · p.{r.source_clause.page_number}</span> : null}
                  </div>
                )}
                <div className="mt-1.5 space-y-1">
                  <div className="flex items-center gap-2 text-ink-muted">
                    Mapped to Output Column{multiField ? "s" : ""}:
                  </div>
                  {canRetarget ? boundFields.map(bf => {
                    const key = `${r.validation_rule_id}::${bf}`;
                    const staged = pending[key];
                    return (
                    <div key={bf} className="flex items-center gap-2">
                      <div className="w-56">
                        <Combo value={staged ?? bf} options={fieldOptions} clearable={false}
                          disabled={readOnly || busy || saving}
                          placeholder="Choose output column…" onSelect={f => stageField(r, bf, f)} />
                      </div>
                      {!readOnly && staged && staged !== bf && (
                        <span className="text-[11px] text-amber-700">Unsaved</span>
                      )}
                    </div>
                  );}) : (
                    <span className="font-medium">
                      {boundFields.join(", ") || "—"}
                      <span className="ml-1 text-ink-soft font-normal">(re-upload the contract to change)</span>
                    </span>
                  )}
                </div>
                {r.rule_template === "cross_field_math" && (
                  <div className="mt-2 border-t border-border/60 pt-2">
                    <div className="flex items-center gap-1.5 text-ink-muted mb-1">
                      Tolerance Band
                      <span className="text-[11px] text-ink-soft">(reported vs formula)</span>
                    </div>
                    <div className="flex items-center gap-3 flex-wrap">
                      <label className="flex items-center gap-1">
                        <span className="text-[11px]">Flag Over</span>
                        <input type="number" step="0.1" min="0" value={tolValue(r).tolerance_pct}
                          disabled={readOnly || tolBusy === r.validation_rule_id}
                          onChange={e => setTol(r, { tolerance_pct: e.target.value })}
                          className="w-16 rounded border border-border px-1.5 py-0.5 text-xs" />
                        <span className="text-[11px]">%</span>
                      </label>
                      <label className="flex items-center gap-1">
                        <span className="text-[11px]">Reject Over</span>
                        <input type="number" step="0.1" min="0" value={tolValue(r).reject_pct}
                          placeholder="off" disabled={readOnly || tolBusy === r.validation_rule_id}
                          onChange={e => setTol(r, { reject_pct: e.target.value })}
                          className="w-16 rounded border border-border px-1.5 py-0.5 text-xs" />
                        <span className="text-[11px]">%</span>
                      </label>
                      {!readOnly && tolDirty(r) && (
                        <button onClick={() => saveTolerance(r)} disabled={tolBusy !== null}
                          className="inline-flex items-center gap-1 rounded bg-navy px-2 py-0.5 text-white disabled:opacity-50">
                          <Save size={12} /> Save
                        </button>
                      )}
                    </div>
                    <p className="text-[11px] text-ink-soft mt-1">
                      Within <b>{tolValue(r).tolerance_pct || "0"}%</b> of the formula passes.{" "}
                      {tolValue(r).reject_pct.trim() !== ""
                        ? <>Beyond <b>{tolValue(r).reject_pct}%</b> is rejected ({r.severity || "critical"}); in between is a warning.</>
                        : <>Anything beyond is flagged as {r.severity || "a warning"}.</>}
                    </p>
                  </div>
                )}
                {isVariationRule(r) && (
                  <div className="mt-2 border-t border-border/60 pt-2">
                    <div className="flex items-center gap-1.5 text-ink-muted mb-1 flex-wrap">
                      {spellingLabel(r)}
                      <span className="text-[11px] text-ink-soft">
                        ({(r.variation_values ?? []).length + (r.vocabulary_values ?? []).length})
                      </span>
                      {canEditVariations && (
                        <button onClick={() => openVariation(r)}
                          className="ml-auto inline-flex items-center gap-1 text-navy hover:underline">
                          <Plus size={11} /> Add Variation
                        </button>
                      )}
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {(r.variation_values ?? []).length + (r.vocabulary_values ?? []).length === 0 ? (
                        <span className="text-[11px] text-ink-soft">
                          No variations recorded — this rule matches the contract's
                          wording exactly.
                        </span>
                      ) : <>
                        {(r.variation_values ?? []).map((v, i) => (
                          <span key={`v-${v}-${i}`}
                            title={(r.enum_values ?? []).includes(v)
                              ? "Named in the contract" : "Variation recorded on this rule"}
                            className={`rounded px-1.5 py-0.5 text-[11px] ${
                              (r.enum_values ?? []).includes(v)
                                ? "bg-navy/10 text-navy" : "bg-surface-2 text-ink-muted"}`}>
                            {v}
                          </span>
                        ))}
                        {(r.vocabulary_values ?? []).map((v, i) => (
                          <span key={`d-${v}-${i}`}
                            title="Understood everywhere — from the shared dictionary, not stored on this rule"
                            className="rounded border border-dashed border-border px-1.5 py-0.5 text-[11px] text-ink-soft">
                            {v}
                          </span>
                        ))}
                      </>}
                    </div>
                    <p className="text-[11px] text-ink-soft mt-1">
                      {r.rule_template === "value_not_in_set"
                        ? "A row is flagged when this column matches any of these."
                        : "A row passes when this column matches any of these."}
                      {" "}Capitals and punctuation are ignored.
                      {(r.vocabulary_values ?? []).length > 0 &&
                        " Dashed ones come from the shared dictionary — every contract understands them already."}
                    </p>
                  </div>
                )}
              </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-ink-muted">
        <span><b className="text-ink">{rules.length}</b> rule{rules.length !== 1 ? "s" : ""}</span>
        <span><b className="text-ink">{maps.length}</b> mapped field{maps.length !== 1 ? "s" : ""}</span>
        <span><b className="text-ink">{terms.length}</b> extracted term{terms.length !== 1 ? "s" : ""}</span>
        {detail.output_template && (
          <span>Output Template <b className="text-ink">{detail.output_template.name} v{detail.output_template.version}</b></span>
        )}
      </div>

      {mapGroups.length > 0 && (
        // Folded by default: every pair here is already shown on the rule it
        // belongs to, under "Mapped to Output Column". This is the same data
        // indexed clause-first, useful for scanning coverage, not for acting.
        <Foldaway label="Contract Clause → Output Column" count={mapGroups.length}>
          <div className="space-y-1 pt-1">
            {mapGroups.map((g, i) => (
              <div key={i} className="flex items-start gap-2 text-xs">
                <span className="truncate max-w-[14rem]" title={g.clause}>{g.clause}</span>
                <ArrowRight size={12} className="text-ink-soft mt-0.5 shrink-0" />
                <span className="flex flex-wrap gap-1">
                  {g.fields.map(f => (
                    <span key={f} className="rounded bg-surface-2 px-1.5 py-0.5 font-medium">{f}</span>
                  ))}
                </span>
              </div>
            ))}
          </div>
        </Foldaway>
      )}

      {terms.length > 0 && (
        <div>
          <div className="text-xs font-medium mb-1.5">AI-Extracted Terms ({terms.length})</div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-ink-muted border-b border-border">
                  <th className="py-1 pr-3 font-medium">Term</th>
                  <th className="py-1 pr-3 font-medium">Value</th>
                  <th className="py-1 font-medium">Source</th>
                </tr>
              </thead>
              <tbody>
                {terms.map(t => (
                  <tr key={t.id} className="border-b border-border/60 align-top">
                    <td className="py-1 pr-3 font-medium capitalize">{String(t.category ?? "").replace(/_/g, " ") || "—"}</td>
                    <td className="py-1 pr-3">{fmtTermValue(t.value)}</td>
                    <td className="py-1 text-ink-muted max-w-md">{t.source_text ? <ClauseText text={t.source_text} /> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {rules.length > 0 && (
        <div>
          <div className="text-xs font-medium mb-1.5">
            Validation Rules ({rules.length})
            {libraryRules.length > 0 && (
              <span className="ml-1.5 font-normal text-ink-soft">
                · {ownRules.length} from this contract
              </span>
            )}
          </div>
          {ruleErr && (
            <div className="mb-2 rounded-md bg-danger/10 text-danger px-2 py-1 text-[11px]">{ruleErr}</div>
          )}
          <div className="space-y-2">
            {ownRules.map(r => renderRule(r))}
          </div>
          {libraryRules.length > 0 && (
            <div className="mt-2">
              <Foldaway label="Standard & derived rules (not from this contract)"
                count={libraryRules.length}>
                <div className="space-y-2 pt-1">{libraryRules.map(r => renderRule(r))}</div>
              </Foldaway>
            </div>
          )}

          {!readOnly && pendingCount > 0 && (
            <div className="mt-3 flex items-center gap-2 border-t border-border pt-3">
              <Button onClick={saveChanges} disabled={saving}>
                <Save size={14} />
                Save {pendingCount} Change{pendingCount !== 1 ? "s" : ""}
              </Button>
              <button onClick={() => setPending({})} disabled={saving}
                className="text-xs text-ink-muted hover:text-ink disabled:opacity-50">
                Discard
              </button>
            </div>
          )}
        </div>
      )}

      {reviewClauses.length > 0 && (
        <div>
          <div className="text-xs font-medium mb-1.5 flex items-center gap-1.5">
            <AlertTriangle size={12} className="text-amber-500" />
            Clauses Awaiting a Field ({ownClauses.length})
            {libraryClauses.length > 0 && (
              <span className="font-normal text-ink-soft">
                · {libraryClauses.length} standard rule{libraryClauses.length === 1 ? "" : "s"} folded below
              </span>
            )}
          </div>
          <p className="text-[11px] text-ink-muted mb-2">
            {readOnly
              ? "These clauses are rule-bearing but weren't auto-mapped to an output column. " +
                "Open this setup in Bordereau Setup to pick a column and generate their rules."
              : "Pick the column each one applies to and add a reference note describing the " +
                "rule logic to enforce — then generate its rule."}
          </p>
          <div className="space-y-2">
            {ownClauses.map(rc => renderReviewClause(rc))}
          </div>
          {libraryClauses.length > 0 && (
            // Kept reachable, not shown first: these are the shared library's
            // checks (claims, currency, NAICS…) that this template has no column
            // for. They are still resolvable, but they are not what this
            // contract asks for, and they outnumber those that are 27 to 5.
            <div className="mt-2">
              <Foldaway label="Standard rules with no column in this template"
                count={libraryClauses.length}>
                <div className="space-y-2 pt-1">
                  {libraryClauses.map(rc => renderReviewClause(rc))}
                </div>
              </Foldaway>
            </div>
          )}
        </div>
      )}

      {rules.length === 0 && terms.length === 0 && maps.length === 0 && reviewClauses.length === 0 && (
        <p className="text-xs text-ink-muted">No terms or rules were extracted from this contract.</p>
      )}

      {/* In-app confirm for rule removal (replaces window.confirm, which looked
          like a browser popup and couldn't carry the app's styling). */}
      <Modal open={ruleToRemove !== null} title="Remove this rule?" size="md"
        onClose={() => { if (busyRule === null) setRuleToRemove(null); }}
        footer={
          <div className="flex items-center justify-end gap-2">
            <Button variant="secondary" disabled={busyRule !== null}
              onClick={() => setRuleToRemove(null)}>Cancel</Button>
            <Button variant="danger" disabled={busyRule !== null}
              onClick={() => ruleToRemove && removeRule(ruleToRemove)}>
              {busyRule !== null ? "Removing…" : "Remove rule"}
            </Button>
          </div>
        }>
        <div className="text-sm text-ink-muted space-y-2">
          <p>
            <strong className="text-ink">{ruleToRemove?.rule_name}</strong> will no longer
            validate the output.
          </p>
          <p>You can restore it by re-uploading the contract.</p>
        </div>
      </Modal>

      {/* Add a spelling. The rule keeps meaning exactly what the contract says —
          this only teaches it another way the bordereau writes the same value. */}
      <Modal open={varRule !== null} title="Add a variation" size="lg"
        onClose={() => { if (!varBusy && !varRemoving) closeVariation(); }}
        footer={
          <div className="flex items-center justify-end gap-2">
            <Button variant="secondary" disabled={varBusy || varRemoving}
              onClick={closeVariation}>
              {(varResult?.accepted || varRemoved?.removed_count) ? "Done" : "Cancel"}
            </Button>
            {/* Removals are STAGED by ✕ and committed here, so this button only
                exists while something is marked — it can never be a no-op. */}
            {varDrop.length > 0 && (
              <Button variant="danger" disabled={varBusy || varRemoving}
                onClick={commitRemovals}>
                {varRemoving ? "Removing…" : `Remove ${varDrop.length}`}
              </Button>
            )}
            <Button disabled={varBusy || varRemoving
              || (!varChips.length && !varDraft.trim())} onClick={submitVariation}>
              {varBusy ? "Checking…" : "Check And Add"}
            </Button>
          </div>
        }>
        <div className="space-y-3 text-sm">
          <div className="text-xs text-ink-muted">
            <div className="font-medium text-ink">{varRule?.rule_name}</div>
            {varRule?.enum_field && <div className="mt-0.5">Column: {varRule.enum_field}</div>}
          </div>

          {(varRule?.enum_values ?? []).length > 0 && (
            <div className="rounded-md bg-surface-2 px-3 py-2">
              <div className="text-[11px] uppercase tracking-wide text-ink-soft mb-1">
                {varRule?.rule_template === "value_not_in_set"
                  ? "Values this contract prohibits"
                  : "Values this contract names"}
              </div>
              <div className="flex flex-wrap gap-1">
                {(varRule?.enum_values ?? []).map((v, i) => (
                  <span key={`${v}-${i}`} className="rounded bg-navy/10 px-1.5 py-0.5 text-[11px] text-navy">{v}</span>
                ))}
              </div>
            </div>
          )}

          {/* Everything the rule ALREADY accepts, so nobody types a spelling that
              is quietly covered and gets told "no" for a reason they can't see.
              The ones an admin added carry an ✕; the contract's own values and the
              shared dictionary's do NOT, because neither is this admin's to drop. */}
          {((varRule?.variation_values ?? []).length + (varRule?.vocabulary_values ?? []).length) > 0 && (
            <div className="rounded-md border border-border px-3 py-2">
              <div className="text-[11px] uppercase tracking-wide text-ink-soft mb-1">
                Already accepted — no need to add these
                {canEditVariations && (varRule?.removable_variations ?? []).length > 0 && (
                  <span className="normal-case tracking-normal"> · ✕ marks one for removal</span>
                )}
              </div>
              <div className="flex flex-wrap gap-1">
                {(varRule?.variation_values ?? []).map((v, i) => {
                  const canRemove = (varRule?.removable_variations ?? []).includes(v);
                  const staged = varDrop.some(s => varNorm(s) === varNorm(v));
                  return (
                    <span key={`v-${v}-${i}`}
                      title={staged
                        ? "Marked for removal — press Remove to apply, or click ↩ to keep it"
                        : canRemove
                          ? "Added here — ✕ marks it for removal"
                          : "Named in the contract — re-upload the contract to change it"}
                      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] ${
                        staged ? "bg-red-50 text-red-700 line-through"
                          : canRemove ? "bg-surface-2 text-ink-muted"
                            : "bg-navy/10 text-navy"}`}>
                      {v}
                      {canRemove && canEditVariations && (
                        <button type="button" disabled={varBusy || varRemoving}
                          onClick={() => toggleRemoval(v)}
                          aria-label={staged ? `Keep ${v}` : `Mark ${v} for removal`}
                          className={`rounded-full p-0.5 no-underline disabled:opacity-40 ${
                            staged ? "hover:bg-red-200" : "hover:bg-red-100 hover:text-red-700"}`}>
                          {staged ? <RotateCcw size={10} /> : <X size={10} />}
                        </button>
                      )}
                    </span>
                  );
                })}
                {(varRule?.vocabulary_values ?? []).map((v, i) => (
                  <span key={`d-${v}-${i}`}
                    title="From the shared dictionary — understood by every contract, not stored on this rule"
                    className="rounded border border-dashed border-border px-1.5 py-0.5 text-[11px] text-ink-soft">{v}</span>
                ))}
              </div>
            </div>
          )}

          {/* Type a variation and press Enter — it becomes a block, like the ones
              above, so several are visible at a glance instead of hiding as lines
              inside a text box. */}
          <div>
            <div className="text-xs text-ink-muted">
              How does the bordereau write one of these values?
              <span className="text-ink-soft"> Press Enter after each.</span>
            </div>
            <div
              onClick={() => varInputRef.current?.focus()}
              className={`mt-1 flex flex-wrap items-center gap-1 rounded border px-2 py-1.5 ${
                varBusy ? "border-border bg-surface-2" : "border-border bg-white"}`}>
              {varChips.map((c, i) => (
                <span key={`c-${c}-${i}`}
                  className="inline-flex items-center gap-1 rounded bg-navy/10 px-1.5 py-0.5 text-[11px] text-navy">
                  {c}
                  <button type="button" disabled={varBusy} aria-label={`Remove ${c}`}
                    onClick={e => { e.stopPropagation(); setVarChips(varChips.filter((_, j) => j !== i)); }}
                    className="rounded-full p-0.5 hover:bg-navy/20 disabled:opacity-40">
                    <X size={10} />
                  </button>
                </span>
              ))}
              <input ref={varInputRef} autoFocus value={varDraft} disabled={varBusy}
                onChange={e => setVarDraft(e.target.value)}
                onKeyDown={varKeyDown}
                onBlur={() => commitDraft()}
                onPaste={e => {
                  // A pasted column of values becomes one chip per value instead
                  // of a single chip containing every line.
                  const text = e.clipboardData.getData("text");
                  if (/[\n;\t]/.test(text)) { e.preventDefault(); commitDraft(varDraft + text); }
                }}
                placeholder={varChips.length ? "Add another…" : "e.g. SSIC"}
                className="min-w-[8rem] flex-1 border-0 bg-transparent p-0 text-sm outline-none placeholder:text-ink-soft" />
            </div>
            <p className="mt-1 text-[11px] text-ink-soft">
              {varChips.length > 0
                ? `${varChips.length} to check${varDraft.trim() ? " + what you're typing" : ""} — all checked together in one go.`
                : "Each is checked against the contract before anything changes."}
            </p>
          </div>

          {varErr && <Banner kind="error">{varErr}</Banner>}

          {varRemoved && (
            <div className="space-y-1.5">
              <div className="text-xs text-ink-muted">
                {varRemoved.removed_count} removed
                {varRemoved.refused_count > 0 && ` · ${varRemoved.refused_count} not removed`}
              </div>
              {varRemoved.results.map((r, i) => (
                <Banner key={`rm-${r.spelling}-${i}`} kind={r.removed ? "ok" : "warn"}
                  className="!items-start !py-2">
                  <div className="space-y-0.5">
                    <div className="font-medium text-xs">
                      {r.removed ? "✓" : "✕"} “{r.spelling}”
                    </div>
                    {!r.removed && <div className="text-[11px]">{r.reason}</div>}
                    {r.removed && (r.still_matched_by_dictionary ? (
                      <div className="text-[11px]">
                        Gone from this rule's list — but the shared dictionary still
                        treats it as another way of writing{" "}
                        <b>{r.still_matched_by_dictionary}</b>, so rows spelled this
                        way keep matching.
                      </div>
                    ) : (
                      <div className="text-[11px]">
                        {varRule?.rule_template === "value_not_in_set"
                          ? "Rows using this spelling will no longer be flagged."
                          : "Rows using this spelling will no longer pass on account of it."}
                      </div>
                    ))}
                  </div>
                </Banner>
              ))}
              {varRemoved.removed_count > 0 && varRemoved.vocabulary_removed === 0 && (
                <div className="text-[11px] text-ink-soft">
                  The shared dictionary was left as it is — only an entry this rule
                  itself added can be taken back from here.
                </div>
              )}
            </div>
          )}

          {varResult && (
            <div className="space-y-1.5">
              {(varResult.accepted_count > 0 || varResult.refused_count > 0) && (
                <div className="text-xs text-ink-muted">
                  {varResult.accepted_count} added
                  {varResult.refused_count > 0 && ` · ${varResult.refused_count} not added`}
                  {varResult.accepted_count > 0 && (
                    varRule?.rule_template === "value_not_in_set"
                      ? " — rows using them will now be flagged."
                      : " — rows using them will now pass.")}
                </div>
              )}
              {varResult.results.map((r, i) => (
                <Banner key={`${r.spelling}-${i}`} kind={r.accepted ? "ok" : "warn"}
                  className="!items-start !py-2">
                  <div className="space-y-0.5">
                    <div className="font-medium text-xs">
                      {r.accepted ? "✓" : "✕"} “{r.spelling}”
                      {r.accepted && r.matched_value && (
                        <span className="font-normal"> — recorded as another way of writing <b>{r.matched_value}</b></span>
                      )}
                    </div>
                    {!r.accepted && <div className="text-[11px]">{r.reason}</div>}
                    {!r.accepted && r.clause_quote && (
                      <div className="mt-1 rounded bg-white/50 px-2 py-1 text-[11px] italic">
                        <ClauseText text={r.clause_quote} />
                        {varResult.clause_page ? <span className="not-italic"> · p.{varResult.clause_page}</span> : null}
                      </div>
                    )}
                    {r.accepted && !r.vocabulary_written && (
                      <div className="text-[11px]">Applies to this rule only — it could not be filed in the shared dictionary.</div>
                    )}
                  </div>
                </Banner>
              ))}
            </div>
          )}
        </div>
      </Modal>
    </div>
  );
}

// One review-queue clause: pick one or more output fields and generate its rule.
function ReviewClauseRow({ item, fieldOptions, onResolve, readOnly = false }: {
  item: ClauseRouting; fieldOptions: string[]; readOnly?: boolean;
  onResolve: (outputFields: string[], note: string) => Promise<{ ok: boolean; reason?: string; created_rules?: unknown[] }>;
}) {
  const [fields, setFields] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<null | { ok: boolean; message: string }>(null);

  const addField = (f: string | null) => {
    if (!f) return;
    setFields(prev => prev.includes(f) ? prev : [...prev, f]);
  };
  const removeField = (f: string) => setFields(prev => prev.filter(x => x !== f));
  const remaining = fieldOptions.filter(o => !fields.includes(o));

  async function submit() {
    if (fields.length === 0 || busy) return;
    setBusy(true); setResult(null);
    try {
      const r = await onResolve(fields, note.trim());
      if (r.ok) {
        const n = r.created_rules?.length ?? 0;
        setResult({ ok: true, message: `Generated ${n} rule${n !== 1 ? "s" : ""}.` });
      } else {
        setResult({ ok: false, message: r.reason || "Could not generate a rule for these fields." });
      }
    } catch (e) { setResult({ ok: false, message: errText(e) }); } finally { setBusy(false); }
  }

  return (
    <div className="rounded-md border border-border/70 p-2 text-xs">
      <div className="flex items-center gap-1.5">
        <AlertTriangle size={12} className="text-amber-500 shrink-0" />
        <span className="font-medium">{item.rule_name || "Unmapped Clause"}</span>
        {item.source_page ? <span className="text-ink-soft">· p.{item.source_page}</span> : null}
      </div>
      {item.clause_text && <div className="mt-0.5 italic text-ink-muted"><ClauseText text={item.clause_text} /></div>}
      {item.reason && <p className="mt-0.5 text-amber-700">Why Unmapped: {item.reason}</p>}

      {!readOnly && (
        <>
          {/* Step 1 — choose the field(s) */}
          <label className="mt-2 block text-[11px] font-medium text-ink-muted">
            1. Output column(s) — pick one, or several when the rule spans columns (first is primary)
          </label>
          <div className="mt-1 w-56">
            <Combo value="" options={remaining} clearable={false} disabled={busy}
              placeholder={fields.length ? "Add another column…" : "Choose output column…"}
              onSelect={addField} />
          </div>
          {fields.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {fields.map((f, i) => (
                <span key={f} className="inline-flex items-center gap-1 rounded-full bg-surface-2 border border-border px-2 py-0.5">
                  {i === 0 && <span className="text-[10px] text-accent font-semibold">Primary</span>}
                  <span>{f}</span>
                  <button type="button" disabled={busy} onClick={() => removeField(f)}
                    className="text-ink-muted hover:text-danger disabled:opacity-50" aria-label={`Remove ${f}`}>
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
            className="mt-1 w-full border border-border rounded-md px-2 py-1.5 bg-surface resize-y placeholder:text-ink-soft" />

          {/* Step 3 — generate */}
          <div className="mt-2.5">
            <Button className="!py-1 !px-2 !text-xs" onClick={submit} disabled={fields.length === 0 || busy}>
              <Sparkles size={12} />
              Generate Rule
            </Button>
          </div>
        </>
      )}
      {result && (
        <p className={`mt-1.5 flex items-center gap-1 ${result.ok ? "text-green-700" : "text-danger"}`}>
          {result.ok ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
          {result.message}
        </p>
      )}
    </div>
  );
}
