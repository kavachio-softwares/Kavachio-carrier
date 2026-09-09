/**
 * The reviewer-facing explanation of one validation rule.
 *
 * Replaces the old presentation — a terse machine reason plus a raw blob under a
 * "Contract Clause" heading — which for a Kavachio standard rule showed the
 * library's internal wording ("[Generic rule] … must not fall after pol_exp_dt
 * or tran_exp_dt …"): warehouse column names, the wrong provenance label, and a
 * description of a DIFFERENT check from the one that actually ran.
 *
 * The content is derived server-side from the rule's IR (see backend
 * rule_explainer.py), so it cannot drift from the check. This component only
 * lays it out:
 *
 *      What this rule checks   ← the requirement (headline)
 *      What's wrong            ← the problem, when the backend derived one
 *      Applies only to …       ← the row scope, when the rule is scoped
 *      ▸ Where this comes from ← provenance + the original wording (collapsed)
 *      How to fix              ← the reviewer's next action
 *
 * VARIANTS. The app has two non-overlapping CSS vocabularies and this renders in
 * both: `proto` for screens under a `.proto` root (Exceptions, Rule Review, BDX
 * inline review) and `tw` for the Tailwind cards in the triage listing, which
 * have no `.proto` ancestor and would render unstyled under proto.css.
 */
import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { RuleExplanation as Explanation } from "../api/validation";
import { ClauseText } from "./ClauseText";

/** Provenance chip colour — a contract rule and a platform default are not the
 *  same kind of thing, and the reviewer acts on them differently. */
const ORIGIN_TONE: Record<string, { proto: string; tw: string }> = {
  contract: { proto: "b-info", tw: "bg-blue-50 text-blue-700 border-blue-200" },
  standard: { proto: "b-mut", tw: "bg-slate-100 text-slate-700 border-slate-200" },
  derived: { proto: "b-mut", tw: "bg-slate-100 text-slate-700 border-slate-200" },
};

/**
 * True only when there is real, rule-specific content to show.
 *
 * Deliberately does NOT count how_to_fix / origin_label: those are always
 * derivable, so counting them would make this function true for every exception
 * and permanently disable each caller's own fallback — including for exceptions
 * that have no rule at all (structural type checks carry rule_id NULL), whose
 * own message is the only useful thing on the card.
 */
export function hasExplanation(e?: Explanation | null): boolean {
  return !!(e && (e.requirement || e.problem || e.source_text));
}

export default function RuleExplanation({
  explanation, variant = "proto", clauseFallback, clausePage, compact = false,
}: {
  explanation?: Explanation | null;
  variant?: "proto" | "tw";
  /** Shown when the backend produced no source_text (legacy rows). */
  clauseFallback?: string | null;
  clausePage?: number | null;
  /** Drops the collapsible + how-to-fix — for the narrow inline-review popover. */
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const e = explanation ?? {};
  const sourceText = e.source_text ?? clauseFallback ?? null;

  // Nothing derived and no clause to fall back on → render nothing rather than
  // an empty labelled box.
  if (!hasExplanation(e) && !sourceText) return null;

  const tw = variant === "tw";
  const tone = ORIGIN_TONE[e.origin ?? "contract"] ?? ORIGIN_TONE.contract;

  const Label = ({ children }: { children: React.ReactNode }) =>
    tw ? (
      <div className="text-[10px] font-semibold uppercase tracking-wide opacity-60 mb-0.5">
        {children}
      </div>
    ) : (
      <div style={{
        fontSize: 9.5, letterSpacing: ".6px", textTransform: "uppercase",
        fontWeight: 700, color: "var(--p-faint)", marginBottom: 2,
      }}>
        {children}
      </div>
    );

  return (
    <div className={tw ? "text-xs" : undefined}
      style={tw ? undefined : { fontSize: 12.5 }}>

      {/* provenance chip, plus the rule KIND when it is not a plain compliance
          check — a referral trigger is not a breach and must not read like one */}
      {(e.origin_label || e.kind_label) && (
        <div style={tw ? undefined : { marginBottom: 6, display: "flex", gap: 6, flexWrap: "wrap" }}
          className={tw ? "mb-1.5 flex gap-1.5 flex-wrap" : undefined}>
          {e.origin_label && (
            <span
              className={tw
                ? `inline-block border rounded px-1.5 py-0.5 text-[10px] font-medium ${tone.tw}`
                : `badge ${tone.proto}`}>
              {!tw && <span className="d" />}{e.origin_label}
            </span>
          )}
          {e.kind_label && (
            <span
              className={tw
                ? "inline-block border rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-50 text-amber-700 border-amber-200"
                : "badge b-warn"}>
              {!tw && <span className="d" />}{e.kind_label}
            </span>
          )}
        </div>
      )}

      {/* the headline — what the rule actually requires */}
      {e.requirement && (
        <div className={tw ? "mb-1.5" : undefined} style={tw ? undefined : { marginBottom: 8 }}>
          <Label>What this rule checks</Label>
          <div style={tw ? undefined : { color: "var(--p-ink)", lineHeight: 1.5 }}
            className={tw ? "opacity-90 leading-relaxed" : undefined}>
            {e.requirement}
          </div>
        </div>
      )}

      {e.problem && (
        <div className={tw ? "mb-1.5" : undefined} style={tw ? undefined : { marginBottom: 8 }}>
          <Label>What's wrong</Label>
          <div style={tw ? undefined : { color: "var(--p-muted)", lineHeight: 1.5 }}
            className={tw ? "opacity-80 leading-relaxed" : undefined}>
            {e.problem}
          </div>
        </div>
      )}

      {e.applies_to && (
        <div className={tw ? "mb-1.5 opacity-75" : undefined}
          style={tw ? undefined : { color: "var(--p-muted)", marginBottom: 8 }}>
          {e.applies_to}
        </div>
      )}

      {e.accepts_also && (
        <div className={tw ? "mb-1.5 opacity-75" : undefined}
          style={tw ? undefined : { color: "var(--p-muted)", marginBottom: 8 }}>
          Also accepted: {e.accepts_also}
        </div>
      )}

      {/* provenance + original wording, collapsed by default — it is evidence,
          not the explanation, and it is what used to dominate the card */}
      {!compact && sourceText && (
        <div style={tw ? undefined : { marginBottom: 8 }} className={tw ? "mb-1.5" : undefined}>
          {/* A <span role="button"> rather than a <button> in the `tw` variant:
              that card's entire header is itself a <button>, and a nested one is
              invalid HTML (React logs a validateDOMNesting error). The proto
              sites sit inside a <div onClick> so either element is fine there. */}
          {(() => {
            const toggle = (ev: React.SyntheticEvent) => {
              // These cards are themselves navigation targets; without this the
              // toggle also opens the rule's review screen.
              ev.stopPropagation();
              setOpen(o => !o);
            };
            const label = (
              <>
                {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                {open ? "Hide" : "Where this comes from"}
              </>
            );
            return tw ? (
              <span
                role="button"
                tabIndex={0}
                onClick={toggle}
                onKeyDown={ev => {
                  if (ev.key === "Enter" || ev.key === " ") toggle(ev);
                }}
                className="inline-flex items-center gap-1 text-[11px] underline opacity-70 hover:opacity-100 cursor-pointer">
                {label}
              </span>
            ) : (
              <button
                type="button"
                onClick={toggle}
                className="linkish"
                style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  background: "none", border: 0, padding: 0, fontSize: 11.5,
                  cursor: "pointer",
                }}>
                {label}
              </button>
            );
          })()}
          {open && (
            <div
              className={tw ? "mt-1 rounded-md bg-white/60 border border-current/20 px-3 py-2" : undefined}
              style={tw ? undefined : {
                marginTop: 6, background: "var(--p-info-soft)",
                border: "1px solid #D4DAF6", borderRadius: "var(--p-r-xs)",
                padding: "10px 12px",
              }}
              onClick={ev => ev.stopPropagation()}>
              {e.origin_note && (
                <div className={tw ? "opacity-70 mb-1.5" : undefined}
                  style={tw ? undefined : {
                    color: "var(--p-info-ink)", marginBottom: 6, fontSize: 11.5,
                  }}>
                  {e.origin_note}
                </div>
              )}
              <div className={tw ? "italic leading-relaxed opacity-90" : undefined}
                style={tw ? undefined : {
                  fontStyle: "italic", color: "var(--p-info-ink)",
                  fontSize: 12, lineHeight: 1.5,
                }}>
                <ClauseText text={sourceText} />
              </div>
              {clausePage != null && !e.origin_label?.includes("p.") && (
                <div className={tw ? "opacity-60 mt-1" : undefined}
                  style={tw ? undefined : {
                    color: "var(--p-faint)", marginTop: 4, fontSize: 11,
                  }}>
                  Page {clausePage}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {!compact && e.how_to_fix && (
        <div>
          <Label>How to fix</Label>
          <div className={tw ? "opacity-80 leading-relaxed" : undefined}
            style={tw ? undefined : { color: "var(--p-muted)", lineHeight: 1.5 }}>
            {e.how_to_fix}
          </div>
        </div>
      )}
    </div>
  );
}
