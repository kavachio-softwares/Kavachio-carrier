/**
 * What the mapper could and could not work out.
 *
 * Kavachio does not need the incoming file to use the same words as the output
 * template — "Commission" is fed by a column called "CM" without anyone being
 * asked. But that only holds while the match is certain. Below the confidence
 * bar, or when the values in a column do not suit the field, the pairing is
 * unproven rather than wrong, and this is where it surfaces.
 *
 * The distinction that matters here is REQUIRED versus optional. An optional
 * field with no source comes out blank and nobody minds. A required one comes
 * out blank too — and looks exactly like a successful delivery until the person
 * waiting for it opens the file. So those are shown first, in red, with the
 * columns that were considered.
 */
import { AlertTriangle, CheckCircle2 } from "lucide-react";

export type MappingReviewEntry = {
  sheet: string;
  field: string;
  field_key: string;
  confidence: number;
  reason: string;
  candidates: string[];
};

export type MappingReviewData = {
  checked: number;
  auto_mapped: number;
  needs_review: number;
  unresolved_required: MappingReviewEntry[];
  unmapped_optional: MappingReviewEntry[];
  threshold: number;
};

export default function MappingReview({ review, compact }: {
  review: MappingReviewData | null | undefined;
  /** Build-summary version: counts and the blockers only. */
  compact?: boolean;
}) {
  // A setup built before the mapper recorded its reasoning has nothing to show.
  // Silence is the honest answer there — not "all clear".
  if (!review || !review.checked) return null;

  const blockers = review.unresolved_required ?? [];
  const optional = review.unmapped_optional ?? [];
  const clean = blockers.length === 0 && optional.length === 0;

  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <div className="px-3 py-2 bg-surface-2 flex items-center gap-2 flex-wrap">
        <span className="text-[12.5px] font-medium">Column mapping</span>
        <span className="text-[11.5px] text-ink-muted">
          {review.auto_mapped} of {review.checked} output columns matched
          automatically
        </span>
        <span className="ml-auto text-[11px] text-ink-soft">
          a match is accepted on its own above {Math.round(review.threshold * 100)}%
        </span>
      </div>

      {clean ? (
        <div className="px-3 py-2 text-[12px] text-emerald-700 flex items-center gap-2">
          <CheckCircle2 size={14} /> Every output column has a source.
        </div>
      ) : (
        <div className="divide-y divide-border">
          {blockers.map(e => (
            <Row key={`${e.sheet}|${e.field_key}`} e={e} required />
          ))}
          {!compact && optional.map(e => (
            <Row key={`${e.sheet}|${e.field_key}`} e={e} />
          ))}
          {compact && optional.length > 0 && (
            <div className="px-3 py-1.5 text-[11.5px] text-ink-muted">
              {optional.length} optional column
              {optional.length === 1 ? "" : "s"} also had no confident match —
              they will come out blank.
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Row({ e, required }: { e: MappingReviewEntry; required?: boolean }) {
  return (
    <div className="px-3 py-2 text-[12px] flex items-start gap-2">
      <span className={`mt-0.5 shrink-0 ${required ? "text-red-500" : "text-amber-500"}`}>
        <AlertTriangle size={13} />
      </span>
      <div className="min-w-0">
        <div>
          <b>{e.field}</b>
          {required && <span className="text-red-600"> — required, and empty</span>}
          <span className="text-ink-soft"> · {e.sheet}</span>
        </div>
        <div className="text-ink-muted mt-0.5">{e.reason}</div>
        {e.candidates.length > 0 && (
          <div className="text-[11px] text-ink-soft mt-0.5">
            Columns considered: {e.candidates.join(", ")}
          </div>
        )}
      </div>
    </div>
  );
}
