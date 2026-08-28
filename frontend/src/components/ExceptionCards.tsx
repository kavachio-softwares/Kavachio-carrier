/**
 * Shared exception-triage UI — used by UploadExceptions page and Outputs modal.
 *
 * Groups flat StoredException[] by rule type so 13 identical errors collapse
 * to one card.  When a mapper spec is provided, each exception row also shows
 * the original source sheet + column name from the uploaded BDX file.
 */
import { useState, useMemo } from "react";
import {
  ShieldAlert, AlertTriangle, Info, ChevronDown, ChevronRight,
  FileDown, FileSpreadsheet,
} from "lucide-react";
import { downloadFile } from "../api/client";
import type { StoredException, RuleExplanation } from "../api/validation";
import ExceptionDecisionTable from "./ExceptionDecisionTable";
import RuleExplanationBlock, { hasExplanation } from "./RuleExplanation";

// ─── types ───────────────────────────────────────────────────────────────────

type Sev = "critical" | "warning" | "info";

const SHEET_SEP = " :: ";

/** Reverse-mapped source location for a canonical field. */
type SourceLoc = { sheet: string; col: string };

// ─── mapper spec helpers ─────────────────────────────────────────────────────

/**
 * Build a lookup: canonical_field (or partial match) → SourceLoc.
 * spec_by_sheet shape: {sheet: {canonical: "Sheet :: Col" | ["Sheet :: Col", ...]}}
 */
export function buildReverseSpec(
  spec: Record<string, Record<string, string | string[]>> | null | undefined
): Map<string, SourceLoc> {
  const out = new Map<string, SourceLoc>();
  if (!spec) return out;
  for (const [sheet, mapping] of Object.entries(spec)) {
    for (const [canonical, srcVal] of Object.entries(mapping)) {
      const srcs = Array.isArray(srcVal) ? srcVal : [srcVal];
      for (const sv of srcs) {
        const s = String(sv);
        let sh = sheet, col = s;
        if (s.includes(SHEET_SEP)) {
          [sh, col] = s.split(SHEET_SEP, 2);
          sh  = sh.trim();
          col = col.trim();
        }
        // Store under the full canonical key AND the bare field name (last segment)
        const bare = canonical.includes(".") ? canonical.split(".").pop()! : canonical;
        if (!out.has(canonical)) out.set(canonical, { sheet: sh, col });
        if (!out.has(bare))      out.set(bare, { sheet: sh, col });
      }
    }
  }
  return out;
}

// ─── constants ───────────────────────────────────────────────────────────────

const SEV_ORDER: Record<string, number> = { critical: 0, warning: 1, info: 2 };
const SEV_CLASSES: Record<string, string> = {
  critical: "bg-red-50 border-red-200 text-red-800",
  warning:  "bg-amber-50 border-amber-200 text-amber-800",
  info:     "bg-blue-50 border-blue-200 text-blue-800",
};
const SEV_PILL: Record<string, string> = {
  critical: "pill pill-red",
  warning:  "pill pill-amber",
  info:     "pill pill-blue",
};
const SEV_ICON: Record<string, React.ReactNode> = {
  critical: <ShieldAlert size={14} />,
  warning:  <AlertTriangle size={14} />,
  info:     <Info size={14} />,
};

// ─── grouping ────────────────────────────────────────────────────────────────

export type RuleGroup = {
  ruleKey: string;
  ruleId: number | null;
  ruleName: string;
  fieldPath: string | null;
  severity: Sev;
  count: number;
  contractClause: string | null;
  clausePage: number | null;
  contractFilename: string | null;
  contractId: number | null;
  errorMessage: string | null;
  /** Backend-derived plain-English explanation of the rule (see
   *  rule_explainer.py). Lifted from the group's first exception — it is a
   *  property of the RULE, so every exception in the group carries the same one. */
  explanation: RuleExplanation | null;
  /** Set when the group holds rows a rule flagged for something other than its
   *  own check (see StoredException.check_kind) — those form their own group, so
   *  a rule can appear as more than one card and `ruleId` is no longer unique.
   *  null for an ordinary rule group. */
  checkKind: string | null;
  items: StoredException[];
};

// ── Decision tallies ─────────────────────────────────────────────────────────
export type DecisionKind = "approve" | "fix" | "dismiss" | "reject";
export type DecisionCounts = Record<DecisionKind, number> & { pending: number };

/** Persisted validation_exception.status → decision kind. */
const STATUS_KIND: Record<string, DecisionKind> = {
  approved: "approve", fixed: "fix", dismissed: "dismiss", rejected: "reject",
};

/** The SCD-2 write-back overrides status to the generic 'resolved'; recover the
 *  original kind from the resolution_note in that case. */
function kindFromNote(note: string): DecisionKind | null {
  if (/^\s*fixed/i.test(note)) return "fix";
  if (/^\s*approved/i.test(note)) return "approve";
  if (/^\s*dismissed/i.test(note)) return "dismiss";
  if (/^\s*rejected/i.test(note)) return "reject";
  return null;
}

/** The saved decision kind for one exception, or null if it's still pending. */
export function decisionKindOf(e: StoredException): DecisionKind | null {
  const status = (e.status || "").toLowerCase();
  return STATUS_KIND[status]
    ?? (status === "resolved" ? kindFromNote(e.resolution_note ?? "") : null);
}

/** Tally decisions across a set of exceptions (Screen A per-rule counts and
 *  Screen B bottom bar share this, so the two never diverge). */
export function tallyDecisions(items: StoredException[]): DecisionCounts {
  const c: DecisionCounts = { approve: 0, fix: 0, dismiss: 0, reject: 0, pending: 0 };
  for (const e of items) {
    const k = decisionKindOf(e);
    if (k) c[k] += 1; else c.pending += 1;
  }
  return c;
}

export function policyLabel(e: StoredException): string {
  return (
    e.policy_number ??
    e.external_policy_number ??
    e.certificate_number ??
    (e.source_entity_id != null
      ? `#${e.source_entity_id}`
      : e.source_row != null
        ? `Row ${e.source_row}`
        : "Dataset-level")
  );
}

export function groupByRule(exceptions: StoredException[]): RuleGroup[] {
  const m = new Map<string, RuleGroup>();
  for (const e of exceptions) {
    // A card carries ONE heading, one explanation and one "how to fix", so rows
    // a rule flagged for a DIFFERENT reason than its own check cannot share it:
    // a cell that is not a number never reached the rule's comparison, and the
    // rule's name, requirement and recommended value all describe that
    // comparison. The backend tags those rows (check_kind) and re-titles them;
    // splitting the group here is what lets their own heading show.
    const kind = e.check_kind ? `::${e.check_kind}` : "";
    const key = e.rule_id != null
      ? `rule_${e.rule_id}${kind}`
      : `no_rule_${e.field_path ?? "unknown"}${kind}`;
    let g = m.get(key);
    if (!g) {
      g = {
        ruleKey: key,
        ruleId: e.rule_id,
        ruleName: e.rule_name ?? (e.rule_id != null ? `Rule ${e.rule_id}` : "Unknown rule"),
        fieldPath: e.field_path,
        severity: (e.severity as Sev) ?? "info",
        count: 0,
        contractClause: e.contract_clause_text,
        clausePage: e.contract_clause_page,
        contractFilename: e.contract_filename,
        contractId: e.rule_contract_id,
        errorMessage: e.error_message,
        explanation: e.explanation ?? null,
        checkKind: e.check_kind ?? null,
        items: [],
      };
      m.set(key, g);
    }
    g.count += 1;
    g.items.push(e);
  }
  return [...m.values()].sort(
    (a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9) || b.count - a.count
  );
}

// ─── CSV export ──────────────────────────────────────────────────────────────

export function exportCSV(
  groups: RuleGroup[],
  label: string,
  revSpec?: Map<string, SourceLoc>
) {
  const rows: string[][] = [
    ["Severity", "Rule", "What the rule checks", "Rule source", "Field",
     "Source Sheet", "Source Column", "Policy", "Actual value", "Expected",
     "Status", "Contract clause"],
  ];
  for (const g of groups) {
    const loc = revSpec?.get(g.fieldPath ?? "") ?? revSpec?.get(g.fieldPath?.split(".").pop() ?? "");
    for (const e of g.items) {
      rows.push([
        e.severity ?? "",
        g.ruleName,
        // Newlines are scrubbed like the clause column: the quoting below
        // escapes only `"`, so an embedded newline would split the CSV row.
        (g.explanation?.requirement ?? "").replace(/[\r\n]+/g, " "),
        g.explanation?.origin_label ?? "",
        e.field_path ?? "",
        loc?.sheet ?? "",
        loc?.col ?? "",
        policyLabel(e),
        e.actual_value ?? "",
        e.expected_value ?? "",
        e.status ?? "open",
        (g.contractClause ?? "").replace(/[\r\n]+/g, " "),
      ]);
    }
  }
  const csv = rows.map(r => r.map(c => `"${c.replace(/"/g, '""')}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a"); a.href = url;
  a.download = `exceptions_${label}.csv`; a.click();
  URL.revokeObjectURL(url);
}

// ─── RuleCard ────────────────────────────────────────────────────────────────

function RuleCard({
  g, uploadId, revSpec,
}: {
  g: RuleGroup;
  uploadId?: number | string;
  revSpec?: Map<string, SourceLoc>;
}) {
  const [open, setOpen] = useState(false);

  // Resolve source location for this rule's field
  const loc = revSpec
    ? (revSpec.get(g.fieldPath ?? "")
       ?? revSpec.get(g.fieldPath?.split(".").pop() ?? ""))
    : undefined;

  return (
    <div className={`rounded-xl border ${SEV_CLASSES[g.severity]} mb-3 overflow-hidden`}>
      {/* ── header ── */}
      <button className="w-full text-left px-4 py-3 flex items-start gap-3"
        onClick={() => setOpen(o => !o)}>
        <div className="mt-0.5 shrink-0">{SEV_ICON[g.severity]}</div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`pill text-[11px] font-semibold ${SEV_PILL[g.severity]}`}>
              {g.severity}
            </span>
            <span className="font-semibold text-sm">{g.ruleName}</span>
            {g.fieldPath && (
              <span className="text-[11px] font-mono bg-black/5 px-1.5 py-0.5 rounded">
                {g.fieldPath}
              </span>
            )}
            {/* source location badge */}
            {loc && (
              <span className="inline-flex items-center gap-1 text-[11px] bg-black/5 px-1.5 py-0.5 rounded">
                <FileSpreadsheet size={10} />
                {loc.sheet} · <span className="font-mono">{loc.col}</span>
              </span>
            )}
            <span className="ml-auto text-xs font-medium shrink-0">
              {g.count} {g.count === 1 ? "policy" : "policies"} affected
            </span>
          </div>

          {/* Plain-English explanation of the rule. Falls back to the raw error
              message + clause only for rows the backend could not explain. */}
          {hasExplanation(g.explanation) || g.contractClause ? (
            <div className="mt-2">
              <RuleExplanationBlock
                explanation={g.explanation}
                variant="tw"
                clauseFallback={g.contractClause}
                clausePage={g.clausePage}
              />
            </div>
          ) : (
            g.errorMessage && <p className="text-xs mt-1 opacity-80">{g.errorMessage}</p>
          )}

          <div className="flex items-center gap-3 mt-2 flex-wrap">
            {g.contractFilename && g.contractId && (
              // NOTE: /programs/contract/{id}/file does not exist in the backend
              // yet (pre-existing gap) — authenticated call kept so it works the
              // moment the route is added.
              <button
                className="inline-flex items-center gap-1 text-[11px] underline opacity-70 hover:opacity-100"
                onClick={e => {
                  e.stopPropagation();
                  downloadFile(`/programs/contract/${g.contractId}/file`, g.contractFilename ?? undefined)
                    .catch(() => alert("Contract download unavailable"));
                }}>
                <FileDown size={11} /> Download contract ({g.contractFilename})
              </button>
            )}
            <span className="text-[11px] opacity-60 ml-auto flex items-center gap-1">
              {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              {open ? "Hide" : "Show"} {g.count} affected {g.count === 1 ? "row" : "rows"}
            </span>
          </div>
        </div>
      </button>

      {/* ── expanded: per-row Decision table (wireframe Screen B) ── */}
      {open && (
        <div className="border-t border-current/20 bg-white/60 px-4 py-3">
          <ExceptionDecisionTable group={g} uploadId={uploadId} />
          <p className="text-[11px] text-ink-muted pt-2 mt-2">
            <strong>Tip:</strong>{" "}
            {loc
              ? <>Source: sheet <strong>{loc.sheet}</strong>, column{" "}
                  <strong className="font-mono">"{loc.col}"</strong> in your BDX file.</>
              : <>Source column: <span className="font-mono">{g.fieldPath ?? "—"}</span>.</>
            }{" "}
            Decisions are held in the browser for now — saving is wired to the backend separately.
          </p>
        </div>
      )}
    </div>
  );
}

// ─── ExceptionCards (exported) ───────────────────────────────────────────────

export function ExceptionCards({
  exceptions,
  label = "exceptions",
  mapperSpec,
  uploadId,
}: {
  exceptions: StoredException[];
  label?: string;
  mapperSpec?: Record<string, Record<string, string | string[]>> | null;
  uploadId?: number | string;
}) {
  const groups  = useMemo(() => groupByRule(exceptions), [exceptions]);
  const revSpec = useMemo(() => buildReverseSpec(mapperSpec), [mapperSpec]);
  const [sev, setSev] = useState<Sev | "all">("all");

  const critical = groups.filter(g => g.severity === "critical").reduce((s, g) => s + g.count, 0);
  const warning  = groups.filter(g => g.severity === "warning" ).reduce((s, g) => s + g.count, 0);
  const info     = groups.filter(g => g.severity === "info"    ).reduce((s, g) => s + g.count, 0);
  const filtered = sev === "all" ? groups : groups.filter(g => g.severity === sev);

  if (exceptions.length === 0) {
    return <p className="text-sm text-ink-muted text-center py-6">No exceptions.</p>;
  }

  return (
    <div>
      {/* filter bar + export */}
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <div className="flex rounded-lg border border-border bg-white overflow-hidden text-xs">
          {([["all", "All", exceptions.length], ["critical", "Critical", critical],
             ["warning", "Warning", warning], ["info", "Info", info]] as const).map(([s, lbl, count]) => (
            <button key={s} onClick={() => setSev(s as Sev | "all")}
              className={`px-3 py-1.5 font-medium border-r border-border last:border-0 transition ${
                sev === s ? "bg-navy text-white" : "hover:bg-surface-2 text-ink-muted"
              }`}>
              {lbl} <span className="opacity-70">({count})</span>
            </button>
          ))}
        </div>
        <button onClick={() => exportCSV(groups, label, revSpec)}
          className="ml-auto inline-flex items-center gap-1 text-xs underline text-accent">
          <FileDown size={13} /> Export CSV
        </button>
      </div>

      <div className="text-xs text-ink-muted mb-2">
        {filtered.length} rule type{filtered.length !== 1 ? "s" : ""}
        {" · "}{filtered.reduce((s, g) => s + g.count, 0)} exception{filtered.reduce((s, g) => s + g.count, 0) !== 1 ? "s" : ""}
      </div>

      {filtered.map(g => (
        <RuleCard
          key={g.ruleKey}
          g={g}
          uploadId={uploadId}
          revSpec={revSpec.size > 0 ? revSpec : undefined}
        />
      ))}
    </div>
  );
}
