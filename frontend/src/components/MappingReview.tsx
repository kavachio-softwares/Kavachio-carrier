/**
 * What the mapper could and could not work out, at a glance.
 *
 * Every output column the bordereau is meant to fill sits in one of four
 * states: mapped automatically, an AI proposal waiting for a person to verify,
 * unmapped, or not matched because the AI never answered. The states ARE the
 * message, so this is chips and one-line rows — the reason and any error sit in
 * each row's tooltip rather than in prose.
 *
 * REQUIRED still matters most: a required column with no source comes out blank
 * and looks like a successful delivery, so those rows carry a red tag and sort
 * first within their state.
 */
import { useState } from "react";
import { CheckCircle2 } from "lucide-react";

export type MappingState = "auto" | "verify" | "unmapped" | "ai_failed";

export type MappingReviewEntry = {
  sheet: string;
  field: string;
  field_key: string;
  confidence: number;
  reason: string;
  candidates: string[];
  state?: MappingState;
  required?: boolean;
  source?: string | null;
  /** Proposed for a person to confirm — not wired up. */
  suggestion?: string | null;
  ai_confidence?: number | null;
  similarity?: number | null;
  error?: string | null;
};

export type MappingReviewData = {
  checked: number;
  auto_mapped: number;
  needs_review: number;
  unresolved_required: MappingReviewEntry[];
  unmapped_optional: MappingReviewEntry[];
  threshold: number;
  review_floor?: number;
  counts?: Record<MappingState, number>;
  entries?: MappingReviewEntry[];
};

const STATES: { key: MappingState; label: string; short: string; pill: string }[] = [
  { key: "auto", label: "Auto-mapped", short: "Auto", pill: "pill-green" },
  { key: "verify", label: "Verify", short: "Verify", pill: "pill-amber" },
  { key: "unmapped", label: "Unmapped", short: "Unmapped", pill: "pill-grey" },
  { key: "ai_failed", label: "AI didn't respond", short: "AI failed", pill: "pill-red" },
];
const ORDER: Record<MappingState, number> = { ai_failed: 0, verify: 1, unmapped: 2, auto: 3 };

const pct = (v?: number | null) => (v == null ? "" : `${Math.round(v * 100)}%`);

/** A response from before the four states carries only the problem lists. */
function entriesOf(r: MappingReviewData): MappingReviewEntry[] {
  if (r.entries) return r.entries;
  return [
    ...(r.unresolved_required ?? []).map(e => ({ ...e, state: "unmapped" as const, required: true })),
    ...(r.unmapped_optional ?? []).map(e => ({ ...e, state: "unmapped" as const })),
  ];
}

export default function MappingReview({ review, compact }: {
  review: MappingReviewData | null | undefined;
  /** Build-summary version: the rows scroll inside their own box. */
  compact?: boolean;
}) {
  // "open" = everything still needing a look, i.e. all but auto-mapped.
  const [show, setShow] = useState<MappingState | "open">("open");
  // A setup built before the mapper recorded its reasoning has nothing to show.
  // Silence is the honest answer there — not "all clear".
  if (!review || !review.checked) return null;

  const entries = entriesOf(review);
  const counts: Record<MappingState, number> = review.counts ?? {
    auto: review.auto_mapped, verify: 0, unmapped: entries.length, ai_failed: 0,
  };
  const rows = entries
    .filter(e => (show === "open" ? e.state !== "auto" : e.state === show))
    .sort((a, b) =>
      ORDER[a.state ?? "unmapped"] - ORDER[b.state ?? "unmapped"]
      || Number(!!b.required) - Number(!!a.required));

  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <div className="px-3 py-2 bg-surface-2 flex items-center gap-1.5 flex-wrap">
        <span className="text-[12.5px] font-medium mr-1">Column mapping</span>
        {STATES.map(s => (
          <button key={s.key} type="button"
            onClick={() => setShow(show === s.key ? "open" : s.key)}
            className={`pill ${s.pill} ${show === s.key ? "ring-1 ring-current" : ""} ${counts[s.key] ? "" : "opacity-50"}`}>
            {s.label} {counts[s.key] ?? 0}
          </button>
        ))}
        <span className="ml-auto text-[11px] text-ink-soft">
          AI ≥{pct(review.threshold)} auto · &gt;{pct(review.review_floor ?? 0.8)} verify
        </span>
      </div>

      {rows.length === 0 ? (
        <div className="px-3 py-2 text-[12px] text-emerald-700 flex items-center gap-2">
          <CheckCircle2 size={14} />
          {show === "open" ? "Every output column has a source." : "None."}
        </div>
      ) : (
        <div className={`divide-y divide-border ${compact ? "max-h-64 overflow-y-auto" : ""}`}>
          {rows.map(e => <Row key={`${e.sheet}|${e.field_key}`} e={e} />)}
        </div>
      )}
    </div>
  );
}

function Row({ e }: { e: MappingReviewEntry }) {
  const state = e.state ?? "unmapped";
  const meta = STATES.find(s => s.key === state)!;
  const tip = [
    e.sheet,
    e.reason,
    e.similarity != null ? `Name similarity ${pct(e.similarity)}` : "",
    e.error ? `Error: ${e.error}` : "",
  ].filter(Boolean).join("\n");
  return (
    <div className="px-3 py-1.5 text-[12px] flex items-center gap-2" title={tip}>
      <b className="truncate min-w-0">{e.field}</b>
      {e.required && state !== "auto" && <span className="pill pill-red shrink-0">Required</span>}
      <span className="ml-auto text-ink-muted truncate min-w-0 text-right">{detail(e, state)}</span>
      <span className={`pill ${meta.pill} shrink-0`}>{meta.short}</span>
    </div>
  );
}

/** One short line per state — never a sentence. */
function detail(e: MappingReviewEntry, state: MappingState): string {
  const ai = e.ai_confidence != null ? ` · AI ${pct(e.ai_confidence)}` : "";
  if (state === "auto") return `← ${e.source ?? ""}${ai}`;
  if (state === "verify") return `← ${e.suggestion ?? "?"}${ai}`;
  if (state === "ai_failed") return e.suggestion ? `similar: ${e.suggestion}` : "";
  return "No suitable match";
}
