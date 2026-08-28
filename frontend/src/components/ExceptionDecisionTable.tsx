/**
 * ExceptionDecisionTable — the "one Decision dropdown per row" review table
 * from the Exception Triage wireframe (Screen B).
 *
 * Per row the reviewer opens the Decision dropdown (4 options) and selecting an
 * option opens ITS prompt:
 *   • Approve         — quick confirm, auto reason ("matches recommendation")
 *   • Fix…            — collects a corrected value
 *   • Dismiss (keep)… — keep the actual value (optional note)
 *   • Reject (exclude)— collects a reason, drops the row from output
 *
 * FRONTEND-ONLY for now: recommendation = the rule's expected_value, decisions
 * live in local state, and "Save decisions" is a stub (logs the payload).
 */
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Check, Wrench, Hand, Ban, ChevronDown, ChevronRight, ArrowLeft, Table2 } from "lucide-react";
import { api } from "../api/client";
import { saveDecisions, saveFields, saveExportDecisions, type StoredException, type ExceptionDecision } from "../api/validation";
import { getUser } from "../auth";
import { policyLabel, type RuleGroup } from "./ExceptionCards";
import { fetchExportGrid, flagStyle, isWarnSeverity, type Sheet } from "./OutputRows";
import Button from "./ui/Button";

type DecisionKind = "none" | "approve" | "fix" | "dismiss" | "reject";
export type Decision = { kind: DecisionKind; value?: string; reason?: string };

/** Persisted validation_exception.status → decision kind, for restoring saved decisions on load. */
const STATUS_KIND: Record<string, Exclude<DecisionKind, "none">> = {
  approved: "approve", fixed: "fix", dismissed: "dismiss", rejected: "reject",
};

/** The SCD-2 write-back overrides status to the generic 'resolved'; recover the
 *  original decision kind from the resolution_note in that case. */
function kindFromNote(note: string): Exclude<DecisionKind, "none"> | null {
  if (/^\s*fixed/i.test(note)) return "fix";
  if (/^\s*approved/i.test(note)) return "approve";
  if (/^\s*dismissed/i.test(note)) return "dismiss";
  if (/^\s*rejected/i.test(note)) return "reject";
  return null;
}

/** Seed local decision state from each exception's persisted status + resolution_note. */
function initDecisions(items: StoredException[]): Record<number, Decision> {
  const out: Record<number, Decision> = {};
  for (const e of items) {
    const status = (e.status || "").toLowerCase();
    const note = e.resolution_note ?? "";
    const kind = STATUS_KIND[status] ?? (status === "resolved" ? kindFromNote(note) : null);
    if (!kind) continue;
    const d: Decision = { kind };
    if (kind === "fix") {
      const m = note?.match(/Fixed with user value:\s*([^—]+)/i);
      if (m) d.value = m[1].trim();
      else if (note) d.reason = note;
    } else if (kind === "approve") {
      const m = note?.match(/Approved with value:\s*([^—]+)/i);
      if (m) d.value = m[1].trim();
    } else if (kind === "dismiss" || kind === "reject") {
      if (note) d.reason = note;
    }
    out[e.exception_id] = d;
  }
  return out;
}

// Reject was removed from the reviewer options — decisions are Approve / Fix /
// Dismiss only. The "reject" kind is kept in the types/restore maps so any
// previously-saved rejected rows still display, but it can't be chosen anymore.
const OPTIONS: { kind: Exclude<DecisionKind, "none">; label: string; desc: string; icon: React.ReactNode; tone: string }[] = [
  { kind: "approve", label: "Approve",              desc: "Accept the recommended value", icon: <Check size={14} />,  tone: "text-emerald-600" },
  { kind: "fix",     label: "Fix…",                 desc: "Enter a corrected value",      icon: <Wrench size={14} />, tone: "text-blue-600" },
  { kind: "dismiss", label: "Dismiss (keep anyway)…", desc: "Keep the actual value",      icon: <Hand size={14} />,   tone: "text-amber-600" },
];

const DECISION_PILL: Record<DecisionKind, string> = {
  none: "pill pill-grey", approve: "pill pill-green", fix: "pill pill-blue", dismiss: "pill pill-amber", reject: "pill pill-red",
};
const DECISION_LABEL: Record<DecisionKind, string> = {
  none: "Select decision", approve: "Approved", fix: "Fixed", dismiss: "Dismissed", reject: "Rejected",
};

/** Success-banner text after a save. */
const savedToast = (n: number) => `Saved ${n} decision${n === 1 ? "" : "s"}.`;

/** Recommendation shown to the reviewer: backend-derived value, else expected_value. */
export function recommendation(e: StoredException): string | null {
  return e.recommendation ?? e.expected_value ?? null;
}

/**
 * Split a comma-joined list into its values, leaving THOUSANDS SEPARATORS alone.
 * The backend formats every number for a human ("20,000"), so a plain
 * `split(",")` tore one amount into two candidates and an aggregate bound
 * rendered as "sum <= 20 · 000". A comma only separates values when it is not
 * sitting between a digit and a 3-digit group.
 */
function splitValueList(raw: string): string[] {
  const parts: string[] = [];
  let buf = "";
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];
    const groups = ch === "," && /^\d/.test(raw[i - 1] ?? "") && /^\d{3}(?!\d)/.test(raw.slice(i + 1));
    if (ch === "," && !groups) { parts.push(buf); buf = ""; } else buf += ch;
  }
  parts.push(buf);
  return parts.map(s => s.trim()).filter(Boolean);
}

/** A comparison operator standing on its own ANYWHERE in the text — the mark of
 *  an EXPRESSION ("sum <= 20,000", "≤ NWP * 0.1") rather than a value. Only the
 *  leading form was recognized before, so an aggregate bound (whose operator
 *  follows the aggregation word) fell through every expression guard below. */
const RELATIONAL = /(?:^|\s)(?:≤|≥|<=|>=|<|>|=)(?:\s|$)/;

/** The engine's wording for a recommendation that DESCRIBES the acceptable
 *  values instead of naming one. Each alternative is one wording the backend
 *  emits (see _expected_from_ir), matched tightly enough that a real value
 *  starting with the same word ("Unique Risk Ltd") is still a value: none of
 *  these is writable, and "matches ^\d{4}$" in a bordereau cell is the bug this
 *  list exists to stop. */
const CONSTRAINT_TEXT = new RegExp("^\\s*(" + [
  "not:",                                 // value_not_in_set → "not: A, B"
  "between ",                             // range_check      → "between 1 and 9"
  "matches ",                             // pattern_check    → "matches ^\\d{4}$"
  "multiple of ",                         // AJV multipleOf   → "multiple of 5"
  "on\\/(after|before) ",                 // AJV date bound   → "on/after 2025-01-01"
  "unique(:|\\s*$)",                      // uniqueness       → "unique" / "unique: A, B"
  "required(\\s*$|\\s+when\\b|\\s*\\()",  // required_field / conditional_required
  "(present|n\\/a)\\s*$",                 // legacy presence wording
].join("|") + ")", "i");

/** True when the recommendation text is a constraint/format rather than a value
 *  the reviewer can accept as-is. The one definition — display (recoParts),
 *  write-back (writeValue) and pre-fill all read it, so they cannot disagree. */
function isConstraintText(raw: string): boolean {
  return CONSTRAINT_TEXT.test(raw) || RELATIONAL.test(raw);
}

/** The illustrative example for a format rule ("1234" for a 4-digit code), or
 *  null. Present only when the backend has no value to recommend, so its
 *  presence is itself the signal that this recommendation cannot be approved
 *  or pre-filled — it shows the reviewer the SHAPE, not the answer. */
export function recoExample(e: StoredException): string | null {
  const s = (e.recommendation_example ?? "").trim();
  return s || null;
}

/**
 * True when the recommendation describes the SHAPE the value must have rather
 * than the value itself — a format rule's pattern (the backend sends an example
 * of the shape instead), or a type check's "a numeric amount (digits, optional
 * . , $ %)". Nothing in a shape is writable: it is neither a value to approve
 * nor a set of values to pick from, so only the reviewer can supply the real
 * one. Both callers below read this, so display, Approve and pre-fill agree.
 */
export function isFormatRecommendation(e: StoredException): boolean {
  return e.error_class === "type_mismatch" || e.code === "type_check"
    || !!recoExample(e) || !!(e.recommendation_format ?? "").trim();
}

/** A bare recommendation with no recognized operator/keyword prefix, split on
 *  commas into 2+ non-empty candidates (e.g. "Specialty, Cayman" from a
 *  value_in_set rule whose structured recommendation_options didn't come
 *  through) — the reviewer picks one instead of the whole joined string being
 *  written back as if it were a single value. Returns null when the text is an
 *  expression rather than a list (so "between 1 and 9", "not: X, Y",
 *  "unique: A, B", "sum <= 20,000" etc. are never mistaken for a plain value
 *  list) or fewer than 2 segments. */
function splitBareValues(raw: string): string[] | null {
  if (/^\s*(≤|≥|<=|>=|<|>|=)/.test(raw)) return null;
  if (isConstraintText(raw)) return null;
  const parts = splitValueList(raw);
  return parts.length > 1 ? parts : null;
}

/** What the Recommendation column shows for one exception.
 *
 *  `illustrative` marks text the reviewer must NOT read as "the value to use":
 *  it is the shape the cell has to have, not its content. `sample` says that
 *  text is a concrete example of that shape, so it renders as "e.g. 1234".
 */
export type RecoParts = {
  value: string;
  note: string | null;
  illustrative?: boolean;
  sample?: boolean;
};

/**
 * Split a recommendation into the clean actionable VALUE the reviewer can approve
 * and an optional CONSTRAINT note. A bound like "≤ 25,000,000" becomes
 * value "25,000,000" (what Approve writes) + note "must be ≤ 25,000,000".
 * Equality/enum-single keep just the value; relational or multi-value constraints
 * (e.g. "≤ NWP * 0.1", "between 1 and 9") stay as the expression. Enum choices
 * render as a plain "A · B" list — the engine's "one of:" prefix is never shown.
 */
export function recoParts(e: StoredException): RecoParts | null {
  // Structured enum options render as a comma-safe list (values may contain
  // commas, e.g. "Palms Insurance Company, Limited") — never split on comma.
  const opts = e.recommendation_options;
  if (Array.isArray(opts) && opts.length > 1)
    return { value: opts.join(" · "), note: null };
  // A FORMAT rule (a pattern) has no value to recommend. Show one value of the
  // right shape instead of the rule's own machine wording — the raw regex the
  // reviewer used to be handed here is neither readable nor usable.
  const sample = recoExample(e);
  const shape = (e.recommendation_format ?? "").trim() || null;
  if (sample) return { value: sample, note: shape && `must be ${shape}`, illustrative: true, sample: true };
  if (shape)  return { value: shape,  note: null, illustrative: true };
  const raw = (recommendation(e) ?? "").trim();
  if (!raw) return null;
  // Any other shape description (a type check's "a numeric amount (digits,
  // optional . , $ %)") is one sentence, so it is shown whole: the value-list
  // split below would otherwise chop it at its commas into "optional ." and
  // read as a set of choices.
  if (isFormatRecommendation(e)) return { value: raw, note: null, illustrative: true };
  // A pattern the backend could neither sample nor put into words leaves only
  // its regex, and a regex is not something a reviewer can act on. Fall through
  // to the caller's own hint ("No recommendation — review") rather than print
  // `matches ^[^0-9]{3}$` in the Recommendation column.
  if (/^matches\s/i.test(raw)) return null;
  // "one of: A, B" arrives as a plain string when the backend didn't send the
  // structured options. Show just the allowed values (same "·" list as the
  // structured branch above) — the "one of:" prefix is engine wording, not
  // something the reviewer needs to read.
  const oneOf = raw.match(/^one of:\s*(.+)$/i);
  if (oneOf) {
    const vals = splitValueList(oneOf[1]);
    return { value: vals.join(" · "), note: null };
  }
  // No single value: keep the whole expression as-is. (A format with no example
  // to show lands here too — the pattern itself is all there is to say.)
  if (CONSTRAINT_TEXT.test(raw))
    return { value: raw, note: null };
  const m = raw.match(/^(≤|≥|<=|>=|<|>|=)\s*(.+)$/);
  if (!m) {
    // No leading operator — a bare comma list is ≥2 distinct candidates, not
    // one literal (see enumOptions/writeValue); show it the same "A · B" way.
    const bare = splitBareValues(raw);
    return { value: bare ? bare.join(" · ") : raw, note: null };
  }
  const op = m[1], rest = m[2].trim();
  const sym = op === "<=" ? "≤" : op === ">=" ? "≥" : op;
  if (op === "=") return { value: rest, note: null };   // exact value
  // Concrete numeric bound → the boundary is the actionable value.
  if (/^\$?[\d,]+(\.\d+)?$/.test(rest)) return { value: rest, note: `must be ${sym} ${rest}` };
  return { value: raw, note: null };                    // relational expression
}

// Rule-generation confidence (0..1) the AI assigned when it built the rule from
// the contract. Colour-coded so a low-confidence rule visibly warrants a closer
// look: green ≥80%, amber ≥50%, red below — soft fill + ring for the chip.
function confChipTone(c: number): string {
  return c >= 0.8 ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
    : c >= 0.5 ? "bg-amber-50 text-amber-700 ring-amber-200"
    : "bg-red-50 text-red-700 ring-red-200";
}

/**
 * Confidence as a small pill/chip, e.g. a green "100%" badge. Renders nothing
 * when the backend didn't supply a score. `showLabel` prefixes "Confidence" for
 * standalone use (where there's no recommendation nearby for context).
 */
export function ConfidenceChip({ e, showLabel = false, className = "" }: {
  e: StoredException; showLabel?: boolean; className?: string;
}) {
  const c = e.confidence;
  if (c == null || Number.isNaN(c)) return null;
  const pct = Math.round(c * 100);
  return (
    <span
      className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[10px] font-semibold ring-1 ${confChipTone(c)} ${className}`}
      title="How confident the AI was when it generated this rule from the contract">
      {showLabel ? `Confidence ${pct}%` : `${pct}%`}
    </span>
  );
}

/** How much of a recommended value is shown before it is folded away. A
 *  value_in_set rule can carry the whole allowed set — a hundred class-of-
 *  business names — and one such row buried every other row on the screen.
 *  Long enough to recognise the recommendation, short enough to keep the row
 *  a row. */
const RECO_CLAMP_CHARS = 140;

/**
 * A long recommended value, cut to one readable line with the rest a click
 * away. Short values render exactly as before — no toggle, no wrapper.
 *
 * The cut lands on the last separator before the limit rather than mid-word,
 * so what remains reads as whole items rather than a severed one. Open/closed
 * is per row and lives here, so opening one row leaves every other row alone.
 */
function ClampedValue({ value, className }: { value: string; className?: string }) {
  const [open, setOpen] = useState(false);
  if (value.length <= RECO_CLAMP_CHARS) {
    return <span className={className}>{value}</span>;
  }
  // Prefer a separator, then any space; fall back to a hard cut for a single
  // unbroken token (one very long word has nowhere better to break).
  const head = value.slice(0, RECO_CLAMP_CHARS);
  const at = Math.max(head.lastIndexOf(", "), head.lastIndexOf(" · "),
                      head.lastIndexOf("; "), head.lastIndexOf(" "));
  const shown = open ? value : head.slice(0, at > RECO_CLAMP_CHARS / 2 ? at : head.length);
  // A block, not an inline run: the button sits on its own line under the text
  // so it reads as a control rather than as one more word of the value.
  return (
    <span className="block">
      <span className={className}>
        {shown}
        {!open && <span className="text-ink-soft">…</span>}
      </span>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        // Not a <Link> and not inside the row's own click target: this only
        // folds text, and must never count as reviewing the exception.
        className="mt-1.5 inline-flex items-center gap-1 rounded-md border border-border
                   bg-surface-2 px-2 py-1 font-sans text-[11px] font-semibold
                   text-ink-muted transition hover:border-navy/40 hover:bg-white
                   hover:text-navy focus-visible:outline-none focus-visible:ring-2
                   focus-visible:ring-navy/30"
      >
        {open ? "View less" : "View more"}
        <ChevronDown size={11} strokeWidth={2.5}
          className={`transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
    </span>
  );
}

/**
 * The recommended value marked as recommended — a green check + the value + the
 * confidence chip (e.g. "✓ Primary · Excess  [90%]"). This is the headline
 * recommendation display; use it wherever the recommended value shows.
 *
 * `illustrative` (a format rule's example / shape) drops the green check and the
 * confidence chip and greys the text: those say "this is the value to use", and
 * an example is not — it shows what the value must LOOK like. `sample` prefixes
 * "e.g." so the one concrete value can't be mistaken for the required one.
 */
export function RecommendationValue({ e, value, illustrative = false, sample = false }: {
  e: StoredException; value: string; illustrative?: boolean; sample?: boolean;
}) {
  if (illustrative) {
    return (
      <span className="inline-flex items-center gap-1 flex-wrap">
        {sample && <span className="text-[11px] text-ink-soft shrink-0">e.g.</span>}
        <ClampedValue value={value} className="font-mono text-ink-muted break-words" />
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 flex-wrap">
      <Check size={12} className="text-emerald-600 shrink-0" aria-label="Recommended" />
      <ClampedValue value={value} className="font-mono text-emerald-700 break-words" />
      <ConfidenceChip e={e} />
    </span>
  );
}

/** When there's no concrete recommendation, explain why instead of a blank "—". */
function recoHint(e: StoredException): string {
  switch (e.root_cause) {
    case "mapping_gap":     return "Field not mapped — review";
    case "rule_incomplete": return "No expected value — review";
    // The cell holds text where a number is needed. Only the reviewer knows the
    // right figure, so the screen asks for one rather than offering the rule's
    // own expected value — which is about a comparison that never ran here.
    case "type_mismatch":   return "Not a number — enter the correct value";
    default:
      return e.contract_clause_text ? "See rule / clause" : "No recommendation — review";
  }
}

/**
 * The concrete value to write back to canonical for a decision, or null when
 * there isn't a single value to write (e.g. Approve on a "≤ 25,000,000" bound,
 * an enum, or "Required"). Fix → the entered value; Approve → the recommended
 * value only when it is a clean scalar (operators/ranges/enums are skipped).
 */
/** Options when the recommendation is a multi-choice enum ("one of: A, B"), else null. */
export function enumOptions(e: StoredException): string[] | null {
  // A format description is one sentence about the cell, not a list of allowed
  // values — splitting it on its commas produced choices like "optional ." and
  // offered them for Approve, which then wrote that fragment into the row.
  if (isFormatRecommendation(e)) return null;
  // Prefer the backend's structured list — each entry is one allowed value,
  // even if it contains commas. Only fall back to parsing the joined string.
  const structured = e.recommendation_options;
  if (Array.isArray(structured) && structured.length > 1) return structured;
  const raw = (recommendation(e) ?? "").trim();
  if (!raw) return null;
  const m = raw.match(/^one of:\s*(.+)$/i);
  if (m) {
    const opts = splitValueList(m[1].replace(/…\s*$/, ""));
    return opts.length > 1 ? opts : null;
  }
  // No structured options and no "one of:" prefix — if the recommendation is a
  // bare, un-prefixed comma list, treat each segment as its own candidate
  // rather than one literal value (see splitBareValues).
  return splitBareValues(raw);
}

export function writeValue(e: StoredException, d: Decision): string | null {
  if (d.kind === "fix") return d.value?.trim() || null;
  if (d.kind !== "approve") return null;

  // Explicitly chosen value (e.g. a multi-enum option picked in the Approve prompt).
  if (d.value?.trim()) return d.value.trim();

  // A FORMAT recommendation is a description of the shape ("a numeric amount
  // (digits, optional . , $ %)", or a pattern rule's example "1234") — there is
  // nothing auto-writable in it, and approving it would write the description,
  // or one example value, into every flagged row. The reviewer fixes instead.
  if (isFormatRecommendation(e)) return null;

  // Structured enum options: auto-usable only when there's exactly one choice.
  const structured = e.recommendation_options;
  if (Array.isArray(structured)) return structured.length === 1 ? structured[0] : null;

  let r = (recommendation(e) ?? "").trim();
  if (!r) return null;

  // enum recommendation ("one of: A, B") — only auto-usable if a single option.
  const enumM = r.match(/^one of:\s*(.+)$/i);
  if (enumM) {
    const opts = splitValueList(enumM[1].replace(/…\s*$/, ""));
    return opts.length === 1 ? opts[0] : null;
  }

  // strip a leading comparison operator (≤ ≥ <= >= < > =) → use the bound value.
  const opM = r.match(/^\s*(≤|≥|<=|>=|<|>|=)\s*/);
  const op = opM ? opM[1] : null;
  r = r.replace(/^\s*(≤|≥|<=|>=|<|>|=)\s*/, "").trim();
  // A constraint DESCRIBES the acceptable values ("between 1 and 9", "not: A, B",
  // "matches ^\d{4}$", "Required") — there is no single value in it to accept, so
  // Approve is off and the reviewer supplies one with Fix. Without this the whole
  // description was written into the cell as if it were the value.
  if (isConstraintText(r)) return null;
  // An operator still standing INSIDE the text means the recommendation is an
  // expression, not a value — "sum <= 20,000" caps a group TOTAL, and writing it
  // into one cell would be nonsense. (Reached only for the non-leading form; a
  // leading operator was stripped just above.)
  if (RELATIONAL.test(r)) return null;

  const num = r.replace(/[$,\s]/g, "");
  if (/^-?[\d.]+$/.test(num)) return num;                    // numeric bound → e.g. 25000000
  // Equality against a literal (e.g. "= Palms Specialty Insurance Company Inc.")
  // → the whole value is what must be written, even if multi-word.
  if (op === "=" && r) return r;
  // A relational bound against another column/expression (e.g. "<= NWP * 0.1")
  // has no single writable scalar — the reviewer must Fix with a computed value.
  if (op && op !== "=" && /\s/.test(r)) return null;
  // A bare, un-prefixed comma list ("Specialty, Cayman") is 2+ distinct
  // candidate values, not one literal — Approve can't auto-write it; the
  // reviewer picks one via the enum picker (see enumOptions) or uses Fix.
  if (splitBareValues(r)) return null;
  // Any remaining plain recommendation IS the value to write — including a
  // multi-word literal like "Demoshield Specialty". (Operators, ranges, enums
  // and "required" were already handled above, so what's left is a concrete
  // value.) Previously only single tokens were accepted, which made multi-word
  // recommendations un-approvable and forced a needless confirm prompt.
  return r || null;
}

/**
 * ACTUAL after applying the reviewer's decision. Approve/Fix show the corrected
 * value (write-back target); Dismiss/Reject keep the original.
 */
function effectiveActual(e: StoredException, d: Decision): { text: string | null; changed: boolean } {
  const v = writeValue(e, d);
  if (v != null) return { text: v, changed: v !== e.actual_value };
  return { text: e.actual_value, changed: false };
}

/** The value (or marker) that ends up in the output for a given decision. */
function outputFor(e: StoredException, d: Decision): { text: string; tone: string } {
  switch (d.kind) {
    case "approve": return { text: writeValue(e, d) ?? e.actual_value ?? "—", tone: "text-emerald-700" };
    case "fix":     return { text: d.value?.trim() ? d.value : "(enter value)", tone: "text-blue-700" };
    case "dismiss": return { text: e.actual_value ?? "(kept)", tone: "text-amber-700" };
    case "reject":  return { text: "excluded", tone: "text-red-700 line-through" };
    default:        return { text: "—", tone: "text-ink-soft" };
  }
}

// ─── Decision dropdown → prompt ──────────────────────────────────────────────

function DecisionCell({
  e, decision, onChange, trigger,
}: {
  e: StoredException;
  decision: Decision;
  onChange: (d: Decision) => void;
  /** Optional custom trigger — e.g. the flagged bordereau cell acting as the
   *  clickable anchor. Defaults to the "Select decision ▾" pill. */
  trigger?: (props: { onClick: () => void; open: boolean }) => ReactNode;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [prompt, setPrompt] = useState<Exclude<DecisionKind, "none"> | null>(null);
  const [draftValue, setDraftValue] = useState("");
  const [draftReason, setDraftReason] = useState("");
  // Display form of the recommendation (engine wording like the "one of:" prefix
  // already stripped), used for the prompt text and the Fix placeholder. An
  // example keeps its "e.g." here: as a PLACEHOLDER it shows the reviewer the
  // shape to type without ever becoming the field's value.
  const rp = recoParts(e);
  const reco = rp ? (rp.sample ? `e.g. ${rp.value}` : rp.value) : null;
  // Approve needs something to write: a single recommended scalar OR a set of
  // allowed values to pick. With neither, there's no recommendation to accept,
  // so Approve is disabled and the reviewer must Fix.
  const canApprove = !!enumOptions(e) || writeValue(e, { kind: "approve" }) != null;

  // Popover is positioned with viewport-fixed coords (computed from the trigger)
  // so it escapes the table's overflow-x-auto clipping, and flips upward when
  // there isn't room below (e.g. the last rows). A span wraps whatever trigger
  // is used so positioning works for both the default pill and a custom cell.
  const btnRef = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number;
                                   maxH?: number } | null>(null);

  function place(panelH: number) {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    const panelW = 288;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - panelW - 8));
    // Anchor the panel next to the button on the side with room, and CAP it to
    // that side's space — a panel taller than the gap (long clause text, many
    // enum options) scrolls inside itself instead of clipping past the edge.
    const spaceBelow = window.innerHeight - r.bottom - 12;
    const spaceAbove = r.top - 12;
    if (spaceBelow < panelH && spaceAbove > spaceBelow)
      setPos({ left, bottom: window.innerHeight - r.top + 4,
               maxH: Math.max(150, spaceAbove - 4) });
    else
      setPos({ left, top: r.bottom + 4, maxH: Math.max(150, spaceBelow - 4) });
  }

  const close = () => { setMenuOpen(false); setPrompt(null); };

  function openMenu() {
    if (menuOpen) { setMenuOpen(false); return; }
    setPrompt(null);
    place(220);
    setMenuOpen(true);
  }

  function pick(kind: Exclude<DecisionKind, "none">) {
    setMenuOpen(false);
    // Approve / Dismiss apply straight from the dropdown — no second confirm
    // step. Approve still needs the prompt in the two cases where it can't be
    // decided on its own: several allowed values (pick one), or no single value
    // to write at all (a range/relational rule — steer the reviewer to Fix).
    if (kind === "dismiss") { onChange({ kind: "dismiss" }); return; }
    if (kind === "approve" && !enumOptions(e) && writeValue(e, { kind: "approve" }) != null) {
      onChange({ kind: "approve" });
      return;
    }
    if (kind === "approve") setDraftValue(decision.value ?? "");
    // Pre-fill Fix with the CLEAN recommended scalar (operator/commas stripped,
    // e.g. "≤ 25,000,000" → "25000000"); empty for relational/multi bounds and
    // for an EXAMPLE — a format rule's "1234" illustrates the shape, and pre-
    // filling it invites the reviewer to save a value the rule never asked for.
    // writeValue is null in exactly those cases, so the field starts empty and
    // the example stays where it belongs: the placeholder.
    if (kind === "fix")    setDraftValue(decision.value ?? writeValue(e, { kind: "approve" }) ?? "");
    if (kind === "reject") setDraftReason(decision.reason ?? "");
    place(200);
    setPrompt(kind);
  }

  function confirm() {
    if (prompt === "approve")      onChange({ kind: "approve", value: draftValue.trim() || undefined });
    else if (prompt === "fix")     onChange({ kind: "fix", value: draftValue });
    else if (prompt === "dismiss") onChange({ kind: "dismiss" });
    else if (prompt === "reject")  onChange({ kind: "reject", reason: draftReason });
    setPrompt(null);
  }

  return (
    <div className="relative">
      <span ref={btnRef} className="inline-block">
        {trigger
          ? trigger({ onClick: openMenu, open: menuOpen || !!prompt })
          : (
            <button
              onClick={openMenu}
              className="inline-flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-md border border-border bg-white hover:bg-surface-2 transition">
              <span className={DECISION_PILL[decision.kind]}>{DECISION_LABEL[decision.kind]}</span>
              <ChevronDown size={12} className="text-ink-soft" />
            </button>
          )}
      </span>

      {(menuOpen || prompt) && <div className="fixed inset-0 z-30" onClick={close} />}

      {/* dropdown — the 4 options */}
      {menuOpen && pos && (
        <div className="fixed z-40 w-64 rounded-lg border border-border bg-white shadow-lg p-1 overflow-y-auto"
          style={{ left: pos.left, top: pos.top, bottom: pos.bottom, maxHeight: pos.maxH }}>
          {OPTIONS.map(opt => {
            const disabled = opt.kind === "approve" && !canApprove;
            return (
              <button key={opt.kind} onClick={() => pick(opt.kind)} disabled={disabled}
                title={disabled ? "No recommended value to approve — use Fix" : undefined}
                className={`w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-md ${
                  disabled ? "opacity-40 cursor-not-allowed"
                  : `hover:bg-surface-2 ${decision.kind === opt.kind ? "bg-surface-2" : ""}`
                }`}>
                <span className={`mt-0.5 ${opt.tone}`}>{opt.icon}</span>
                <span>
                  <span className="block text-xs font-medium">{opt.label}</span>
                  <span className="block text-[11px] text-ink-soft">
                    {disabled ? "No recommended value — use Fix" : opt.desc}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}

      {/* prompt — opens for the picked option */}
      {prompt && pos && (
        <div className="fixed z-40 w-72 rounded-lg border border-border bg-white shadow-lg p-3 text-xs overflow-y-auto"
          style={{ left: pos.left, top: pos.top, bottom: pos.bottom, maxHeight: pos.maxH }}>
          {prompt === "approve" && (() => {
            const opts = enumOptions(e);
            if (opts) {
              return (
                <>
                  <div className="font-semibold text-emerald-700 mb-1 flex items-center gap-1.5">
                    <Check size={13} /> Approve — choose a value
                  </div>
                  <p className="text-[11px] text-ink-soft mb-1.5">
                    The recommendation allows several values — pick one to apply.
                  </p>
                  <div className="max-h-40 overflow-y-auto space-y-0.5">
                    {opts.map(o => (
                      <label key={o}
                        className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-surface-2 cursor-pointer">
                        <input type="radio" name={`approve-${e.exception_id}`}
                          checked={draftValue === o} onChange={() => setDraftValue(o)}
                          className="h-3 w-3" />
                        <span className="font-mono">{o}</span>
                      </label>
                    ))}
                  </div>
                </>
              );
            }
            // A range / relational / "required" recommendation has no single
            // value to write (e.g. "between 0.3 and 0.4"). Don't present it as an
            // approvable value — it would mark the row approved while the
            // out-of-range value stays. Steer the reviewer to Fix instead.
            const writable = writeValue(e, { kind: "approve" });
            if (writable == null) {
              return (
                <>
                  <div className="font-semibold text-emerald-700 mb-1 flex items-center gap-1.5">
                    <Check size={13} /> Approve
                  </div>
                  <p className="text-ink-muted">
                    This field must be{" "}
                    <span className="font-mono text-ink">{reco ?? "—"}</span>, but there’s no
                    single value to fill in automatically.
                  </p>
                  <p className="text-[11px] text-ink-soft mt-1">
                    Use <b>Fix</b> to enter a corrected value within the allowed range.
                  </p>
                </>
              );
            }
            return (
              <>
                <div className="font-semibold text-emerald-700 mb-1 flex items-center gap-1.5">
                  <Check size={13} /> Approve
                </div>
                <p className="text-ink-muted">
                  Accept the recommended value{" "}
                  <span className="font-mono text-emerald-700">{writable}</span>.
                </p>
                <p className="text-[11px] text-ink-soft mt-1">Reason: auto — matches recommendation.</p>
              </>
            );
          })()}

          {prompt === "fix" && (
            <>
              <div className="font-semibold text-blue-700 mb-1 flex items-center gap-1.5">
                <Wrench size={13} /> Fix — enter value
              </div>
              <label className="block text-[11px] text-ink-soft mb-1">Your corrected value</label>
              <input autoFocus value={draftValue} onChange={ev => setDraftValue(ev.target.value)}
                placeholder={reco ?? "Corrected value…"}
                className="input py-1 text-xs w-full" />
            </>
          )}

          {prompt === "dismiss" && (
            <>
              <div className="font-semibold text-amber-700 mb-1 flex items-center gap-1.5">
                <Hand size={13} /> Dismiss — keep anyway
              </div>
              <p className="text-ink-muted">
                Keep the actual value{" "}
                <span className="font-mono text-amber-700">{e.actual_value ?? "(empty)"}</span>{" "}
                and pass it through unchanged.
              </p>
            </>
          )}

          {prompt === "reject" && (
            <>
              <div className="font-semibold text-red-700 mb-1 flex items-center gap-1.5">
                <Ban size={13} /> Reject — exclude
              </div>
              <label className="block text-[11px] text-ink-soft mb-1">Reason to exclude</label>
              <input autoFocus value={draftReason} onChange={ev => setDraftReason(ev.target.value)}
                placeholder="e.g. invalid / not in scope"
                className="input py-1 text-xs w-full" />
            </>
          )}

          <div className="flex items-center justify-end gap-2 mt-3">
            <button onClick={() => setPrompt(null)}
              className="text-xs px-2 py-1 rounded-md hover:bg-surface-2 text-ink-muted">
              Cancel
            </button>
            <Button onClick={confirm} disabled={
              (prompt === "reject" && !draftReason.trim()) ||
              (prompt === "approve" && !!enumOptions(e) && !draftValue.trim()) ||
              // No single value to approve (range / relational / required) → must Fix.
              (prompt === "approve" && !enumOptions(e) && writeValue(e, { kind: "approve" }) == null)
            }>
              {prompt === "approve" ? "Approve"
                : prompt === "fix" ? "Save value"
                : prompt === "dismiss" ? "Keep in output"
                : "Confirm reject"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Table ───────────────────────────────────────────────────────────────────

export default function ExceptionDecisionTable({
  group, uploadId, templateId, contractId, exportId, onSaved, backLink, backState,
}: {
  group: RuleGroup;
  uploadId?: number | string;
  /** Output template the contract is bound to — enables Fix/Approve write-back. */
  templateId?: number | string;
  contractId?: number | string;
  /** Set on the OUTPUT (per-download) screen — routes Save through the
   *  export-scoped decide endpoint (persist + write-back in one call). */
  exportId?: number | string;
  /** Called after a successful save so the parent can refresh. */
  onSaved?: () => void;
  backLink?: string;
  /** Router state carried to the Exceptions screen so it can prompt the
   *  reviewer to apply the decisions they just recorded (Fix & Validate). */
  backState?: unknown;
}) {
  // "View in Bordereau" — expands the exception inline to show its FULL
  // bordereau row (every column + header, offending cells highlighted) plus the
  // Approve/Fix control. Only meaningful on the output screen, where exportId +
  // the row's sheet/row exist.
  //
  // Just the ONE row is fetched, not the workbook. This used to pull
  // `?full=1&marks=1` — every sheet, up to 20,000 rows — to render a single
  // row: on a large BDX that is tens of megabytes parsed and held in memory the
  // first time anyone expands anything, which is enough to hang the tab. The
  // endpoint's `sheet` + `row_indices` window returns the same header, the same
  // whole-sheet marks and just that row.
  const [expandedId, setExpandedId] = useState<number | null>(null);
  /** `sheet|row` → the one-row grid for that exception. Keyed rather than
   *  single-slot because each exception can sit on a different sheet/row. */
  const [bdxRows, setBdxRows] = useState<Map<string, Sheet[]>>(new Map());
  const [bdxBusy, setBdxBusy] = useState(false);
  const rowKey = (e: StoredException) => `${e.source_sheet ?? ""}|${e.source_row ?? ""}`;

  async function toggleExpand(id: number, e: StoredException) {
    if (expandedId === id) { setExpandedId(null); return; }
    setExpandedId(id);
    const key = rowKey(e);
    if (exportId == null || e.source_row == null || bdxRows.has(key)) return;
    setBdxBusy(true);
    try {
      const { data } = await api.get<{ sheets: Sheet[] }>(
        `/export/downloads/${exportId}/data`,
        { params: { marks: 1, sheet: e.source_sheet ?? "", row_indices: String(e.source_row) },
          silent: true });
      // `BordereauRowDetail` addresses the row by its absolute index
      // (`rows[source_row]`), so the window is re-seated at that index instead
      // of being handed back packed. A sparse array keeps that lookup — and the
      // `Array.isArray(rows[i])` validity check — working unchanged.
      const sheets = (Array.isArray(data.sheets) ? data.sheets : []).map(sh => {
        const gis = (sh as any).row_gis as number[] | undefined;
        if (!gis?.length) return sh;
        const rows: any[][] = [];
        rows[0] = sh.rows[0] ?? [];
        gis.forEach((gi, i) => { rows[gi] = sh.rows[i + 1]; });
        return { ...sh, rows };
      });
      setBdxRows(prev => new Map(prev).set(key, sheets));
    } catch {
      setBdxRows(prev => new Map(prev).set(key, []));
    } finally { setBdxBusy(false); }
  }

  const [decisions, setDecisions] = useState<Record<number, Decision>>(
    () => initDecisions(group.items)
  );
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Prominent, auto-dismissing success banner (matches the app toast convention
  // in Welcome.tsx) — the small "Saved" pill alone was too easy to miss.
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showToast = (msg: string) => {
    setToast(msg);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 3000);
  };

  const setDecision = (id: number, d: Decision) => {
    setDecisions(prev => ({ ...prev, [id]: d }));
    setSaved(false);
  };

  // row selection (checkboxes) + bulk action on the selected rows
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkMenuOpen, setBulkMenuOpen] = useState(false);
  const [bulkPrompt, setBulkPrompt] = useState<Exclude<DecisionKind, "none"> | null>(null);
  const [bulkValue, setBulkValue] = useState("");
  const [bulkReason, setBulkReason] = useState("");
  const allChecked  = selected.size === group.items.length && group.items.length > 0;
  const someChecked = selected.size > 0 && !allChecked;

  function toggleAll() {
    setSelected(allChecked ? new Set() : new Set(group.items.map(e => e.exception_id)));
  }
  function toggleOne(id: number) {
    setSelected(prev => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  }
  /** Picking a bulk decision: Dismiss applies straight away; Fix/Approve/Reject
   *  open a prompt to collect the value/choice/reason applied to all selected. */
  function openBulk(kind: Exclude<DecisionKind, "none">) {
    setBulkMenuOpen(false);
    if (kind === "dismiss") {
      setDecisions(prev => {
        const next = { ...prev };
        for (const e of group.items)
          if (selected.has(e.exception_id)) next[e.exception_id] = { kind: "dismiss" };
        return next;
      });
      setSaved(false);
      return;
    }
    // All selected rows share one rule → a single value/reason/choice applies to all.
    const rep = group.items.find(e => selected.has(e.exception_id));
    setBulkValue(kind === "fix" && rep ? (writeValue(rep, { kind: "approve" }) ?? "") : "");
    setBulkReason("");
    setBulkPrompt(kind);
  }

  function applyBulk() {
    const kind = bulkPrompt;
    if (!kind) return;
    setDecisions(prev => {
      const next = { ...prev };
      for (const e of group.items) {
        if (!selected.has(e.exception_id)) continue;
        if (kind === "fix")          next[e.exception_id] = { kind: "fix", value: bulkValue };
        else if (kind === "approve") {
          // A typed bulk value applies to all; otherwise only auto-approve rows
          // that have a concrete value (skip ranges / relational / required).
          if (bulkValue.trim()) next[e.exception_id] = { kind: "approve", value: bulkValue.trim() };
          else if (isAutoApprovable(e)) next[e.exception_id] = { kind: "approve" };
        }
        else if (kind === "reject")  next[e.exception_id] = { kind: "reject", reason: bulkReason };
        else                         next[e.exception_id] = { kind };
      }
      return next;
    });
    setBulkPrompt(null); setBulkValue(""); setBulkReason(""); setSaved(false);
  }

  const decided = useMemo(
    () => group.items.filter(e => (decisions[e.exception_id]?.kind ?? "none") !== "none").length,
    [decisions, group.items]
  );

  // per-decision tally for the bottom bar
  const counts = useMemo(() => {
    const c = { approve: 0, fix: 0, dismiss: 0, reject: 0 };
    for (const e of group.items) {
      const k = decisions[e.exception_id]?.kind;
      if (k && k !== "none") c[k] += 1;
    }
    return c;
  }, [decisions, group.items]);

  // Any row that actually has a recommendation to approve? (else "Approve all
  // (recommended)" would approve rows marked "No recommendation — review").
  // Approve is only meaningful when it yields a CONCRETE value to write (a single
  // enum option, an equality, or a single numeric bound). Ranges ("between 0.3
  // and 0.4"), relational bounds and "required" have no single value → those must
  // be Fixed, and "Approve all" must skip them (else it keeps the offending
  // actual value while marking the row approved).
  const isAutoApprovable = (e: StoredException) =>
    writeValue(e, { kind: "approve" }) != null;
  const anyApprovable = useMemo(
    () => group.items.some(isAutoApprovable), [group.items]);

  function bulkApprove() {
    setDecisions(prev => {
      const next = { ...prev };
      for (const e of group.items) {
        if (isAutoApprovable(e)) next[e.exception_id] = { kind: "approve" };
      }
      return next;
    });
    setSaved(false);
  }

  async function save() {
    // Rows that actually have a decision.
    const decided = group.items
      .map(e => ({ e, d: decisions[e.exception_id] }))
      .filter((x): x is { e: StoredException; d: Decision } => !!x.d && x.d.kind !== "none");

    if (decided.length === 0) { setSaved(true); return; }

    setSaving(true); setSaveErr(null);
    try {
      // OUTPUT (per-download) screen: exceptions have no real backing row, so
      // persist + write-back go through the export-scoped endpoint in one call
      // (keyed by rule + policy + field, resolved server-side).
      if (exportId != null) {
        const res = await saveExportDecisions(
          exportId,
          decided.map(({ e, d }) => ({
            rule_id: e.rule_id ?? null,
            policy_number: e.policy_number ?? null,
            field: e.field_path ?? null,
            kind: d.kind as ExceptionDecision["kind"],
            value: d.kind === "approve" ? (writeValue(e, d) ?? d.value ?? null)
                 : d.kind === "fix"     ? (d.value ?? null) : null,
            reason: d.reason ?? null,
            actual_value: e.actual_value ?? null,
            // Direct-lane decision key (resolved to the source input cell server-side).
            sheet: e.source_sheet ?? null,
            row: e.source_row ?? null,
          })),
          getUser()?.id,          // decided_by — track who fixed/approved
        );
        const wb = res.writeback;
        let warned = false;
        if (wb && (wb.ok === false || wb.applied === false)) {
          setSaveErr(wb.reason ?? "Decisions saved; some values could not be written back.");
          warned = true;
        }
        // The direct lane always returns writeback:null, so `wb` alone can never
        // reveal a partial save — anything the server skipped has to be read from
        // `skipped` or it lands under a green "saved" and the user never learns
        // their fix didn't stick.
        const skipped = res.skipped ?? [];
        if (skipped.length) {
          setSaveErr(
            `${skipped.length} of ${decided.length} could not be applied — ` +
            skipped.map(sk => `${sk.field ?? "field"}: ${sk.reason}`).join("; "));
          warned = true;
        }
        setSaved(true);
        if (!warned) showToast(savedToast(decided.length));
        onSaved?.();
        return;
      }

      // 1. Record the decisions (status + reason) on the exceptions.
      await saveDecisions(
        decided.map(({ e, d }) => ({
          exception_id: e.exception_id,
          kind: d.kind as ExceptionDecision["kind"],
          // Persist the resolved value for Approve too (e.g. "≤ 25000000" → 25000000)
          // so it restores on reload, just like Fix.
          value: d.kind === "approve" ? (writeValue(e, d) ?? d.value ?? null) : (d.value ?? null),
          reason: d.reason ?? null,
        })),
        getUser()?.id,            // resolved_by_user_id — track who decided
      );

      // 2. Write Fix/Approve concrete values back into canonical data (SCD-2),
      //    so ACTUAL truly changes. Needs the contract's output template.
      const edits = decided
        .map(({ e, d }) => ({ e, v: writeValue(e, d) }))
        .filter((x): x is { e: StoredException; v: string } => x.v != null && !!x.e.field_path)
        .map(({ e, v }) => ({
          fieldPath: e.field_path as string,
          newValue: v,
          policyId: e.source_entity_id ?? null,
          exceptionId: e.exception_id,
          actualValue: e.actual_value,
        }));

      let warned = false;
      if (edits.length > 0) {
        if (uploadId == null || templateId == null) {
          setSaveErr("Decisions saved, but values can't be written back — no output template is linked to this contract.");
          warned = true;
        } else {
          const res = await saveFields({ uploadId, templateId, contractId, edits, apply: true });
          if (!res.ok || res.applied === false) {
            setSaveErr(res.reason ?? "Decisions saved; some values could not be written back.");
            warned = true;
          }
        }
      }

      setSaved(true);
      if (!warned) showToast(savedToast(decided.length));
      onSaved?.();   // refresh so ACTUAL reflects the saved decisions / new versions
    } catch (err: any) {
      setSaveErr(err?.response?.data?.detail ?? err?.message ?? "Failed to save.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="bg-white">
      {/* success banner — fixed, auto-dismisses (matches the app toast style) */}
      {toast && (
        <div role="status" aria-live="polite"
          className="fixed top-4 left-1/2 -translate-x-1/2 z-50 flex items-center gap-1.5
            bg-emerald-600 text-white text-sm font-medium px-4 py-2 rounded-md shadow-lg">
          <Check size={15} /> {toast}
        </div>
      )}

      {/* toolbar — light green (selection / bulk-decision bar) */}
      {/* Lift the whole toolbar's stacking context above the sticky bottom bar
          (z-20) while its dropdown/prompt is open, so a short table (e.g. a single
          exception) doesn't render the bulk menu behind the bottom bar. */}
      <div className={`sticky top-0 ${bulkMenuOpen || bulkPrompt ? "z-30" : "z-20"} flex items-center gap-2 px-3 py-2 flex-wrap rounded-lg bg-emerald-50 border border-emerald-200`}>
        {selected.size > 0 ? (
          <>
            <span className="text-sm font-medium">{selected.size} selected</span>
            <span className="text-sm text-ink-soft">Bulk Decision:</span>
            <div className="relative">
              <button onClick={() => setBulkMenuOpen(o => !o)}
                className="inline-flex items-center gap-1.5 text-sm font-medium px-2 py-1 rounded-md border border-border bg-white hover:bg-surface-2 transition text-ink-muted">
                Choose decision <ChevronDown size={14} className="text-ink-soft" />
              </button>
              {bulkMenuOpen && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setBulkMenuOpen(false)} />
                  <div className="absolute z-20 mt-1 w-64 rounded-lg border border-border bg-white shadow-lg p-1">
                    {OPTIONS.map(opt => (
                      <button key={opt.kind}
                        onClick={() => openBulk(opt.kind)}
                        className="w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-md hover:bg-surface-2">
                        <span className={`mt-0.5 ${opt.tone}`}>{opt.icon}</span>
                        <span>
                          <span className="block text-xs font-medium">{opt.label}</span>
                          <span className="block text-[11px] text-ink-soft">{opt.desc}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
            <button onClick={() => setSelected(new Set())}
              className="text-xs underline text-ink-muted ml-1">Clear</button>
          </>
        ) : (
          <>
            <span className="text-sm text-ink-muted">{decided}/{group.items.length} Decided</span>
            <Button variant="secondary" onClick={bulkApprove} disabled={!anyApprovable}
              title={anyApprovable ? undefined
                : "No auto-approvable values for this rule — use Fix (e.g. range / relational bounds)"}>
              <Check size={14} /> Approve all (recommended)
            </Button>
          </>
        )}
      </div>

      {/* bulk prompt — value (Fix) / choice (Approve enum) / reason (Reject) for all selected */}
      {bulkPrompt && (() => {
        const rep = group.items.find(e => selected.has(e.exception_id));
        const opts = rep ? enumOptions(rep) : null;
        const reco = rep ? recommendation(rep) : null;
        // No single value to write (range / relational / required) — bulk Approve
        // must steer to Fix instead of silently keeping the offending value.
        const repWritable = rep ? writeValue(rep, { kind: "approve" }) : null;
        return (
          <div className="rounded-lg border border-border bg-white shadow-sm p-3 mt-2 text-xs">
            {bulkPrompt === "fix" && (
              <>
                <div className="font-semibold text-blue-700 mb-1">
                  Fix {selected.size} selected — value to apply to all
                </div>
                <input autoFocus value={bulkValue} onChange={ev => setBulkValue(ev.target.value)}
                  placeholder={reco ?? "Corrected value…"} className="input py-1 text-xs w-full max-w-xs" />
              </>
            )}
            {bulkPrompt === "approve" && (opts ? (
              <>
                <div className="font-semibold text-emerald-700 mb-1">
                  Approve {selected.size} selected — choose a value
                </div>
                <p className="text-[11px] text-ink-soft mb-1.5">Applied to every selected row.</p>
                <div className="max-h-40 overflow-y-auto space-y-0.5">
                  {opts.map(o => (
                    <label key={o} className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-surface-2 cursor-pointer">
                      <input type="radio" name="bulk-approve" checked={bulkValue === o}
                        onChange={() => setBulkValue(o)} className="h-3 w-3" />
                      <span className="font-mono">{o}</span>
                    </label>
                  ))}
                </div>
              </>
            ) : repWritable == null ? (
              <>
                <div className="font-semibold text-emerald-700 mb-1">Approve {selected.size} selected</div>
                <p className="text-ink-muted">
                  This field must be{" "}
                  <span className="font-mono text-ink">{reco ?? "—"}</span>, but there’s no single
                  value to fill in automatically.
                </p>
                <p className="text-[11px] text-ink-soft mt-1">
                  Use <b>Fix</b> to enter a corrected value within the allowed range.
                </p>
              </>
            ) : (
              <>
                <div className="font-semibold text-emerald-700 mb-1">Approve {selected.size} selected</div>
                <p className="text-ink-muted">
                  Each row is set to its recommended value{" "}
                  (<span className="font-mono text-emerald-700">{repWritable}</span>).
                </p>
              </>
            ))}
            {bulkPrompt === "reject" && (
              <>
                <div className="font-semibold text-red-700 mb-1">
                  Reject {selected.size} selected — reason for all
                </div>
                <input autoFocus value={bulkReason} onChange={ev => setBulkReason(ev.target.value)}
                  placeholder="e.g. invalid / not in scope" className="input py-1 text-xs w-full max-w-xs" />
              </>
            )}
            <div className="flex items-center justify-end gap-2 mt-3">
              <button onClick={() => setBulkPrompt(null)}
                className="text-xs px-2 py-1 rounded-md hover:bg-surface-2 text-ink-muted">Cancel</button>
              <Button onClick={applyBulk} disabled={
                (bulkPrompt === "fix" && !bulkValue.trim()) ||
                (bulkPrompt === "reject" && !bulkReason.trim()) ||
                (bulkPrompt === "approve" && !!opts && !bulkValue.trim()) ||
                // No single value to approve (range / relational / required) → must Fix.
                (bulkPrompt === "approve" && !opts && repWritable == null)
              }>
                Apply to {selected.size}
              </Button>
            </div>
          </div>
        );
      })()}

      {/* table */}
      <div className="overflow-x-auto rounded-lg border border-border mt-4">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-surface-2 text-ink-muted text-[11px] uppercase tracking-wide">
              <th className="px-3 py-2 w-8">
                <input type="checkbox" className="h-3.5 w-3.5 align-middle"
                  checked={allChecked}
                  ref={el => { if (el) el.indeterminate = someChecked; }}
                  onChange={toggleAll} />
              </th>
              <th className="font-medium px-3 py-2">Policy</th>
              <th className="font-medium px-3 py-2">Actual</th>
              <th className="font-medium px-3 py-2">Recommendation</th>
              <th className="font-medium px-3 py-2">Reason</th>
              <th className="font-medium px-3 py-2">Decision</th>
              <th className="font-medium px-3 py-2">Output</th>
            </tr>
          </thead>
          <tbody>
            {group.items.map(e => {
              const d = decisions[e.exception_id] ?? { kind: "none" as const };
              const out = outputFor(e, d);
              const canExpand = exportId != null && e.source_row != null && !!e.source_sheet;
              const expanded = expandedId === e.exception_id;
              return (
                <Fragment key={e.exception_id}>
                <tr className={`border-t border-border align-top ${selected.has(e.exception_id) ? "bg-blue-50/50" : ""}`}>
                  <td className="px-3 py-2">
                    <input type="checkbox" className="h-3.5 w-3.5 align-middle"
                      checked={selected.has(e.exception_id)}
                      onChange={() => toggleOne(e.exception_id)} />
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <div className="font-mono font-medium">{policyLabel(e)}</div>
                    {canExpand && (
                      <button type="button"
                        className="mt-1 inline-flex items-center gap-1 text-[11px] text-navy hover:underline"
                        title="Show this policy's full bordereau row"
                        onClick={() => toggleExpand(e.exception_id, e)}>
                        {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                        <Table2 size={12} /> View in Bordereau
                      </button>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {(() => {
                      const ea = effectiveActual(e, d);
                      if (ea.changed) {
                        return (
                          <span className="whitespace-nowrap">
                            <span className="font-mono text-emerald-700">{ea.text}</span>
                            {e.actual_value && (
                              <span className="ml-1.5 font-mono text-[10px] text-ink-soft line-through">{e.actual_value}</span>
                            )}
                          </span>
                        );
                      }
                      return e.actual_value
                        ? <span className="font-mono text-red-700">{e.actual_value}</span>
                        : <span className="italic text-ink-soft">(empty)</span>;
                    })()}
                  </td>
                  <td className="px-3 py-2 max-w-[220px]">
                    {(() => {
                      const rp = recoParts(e);
                      // No concrete recommended value → no confidence chip either;
                      // the score only qualifies an actual recommendation.
                      if (!rp) return <span className="text-amber-600 text-[11px] italic">{recoHint(e)}</span>;
                      return (
                        <div className="break-words" title={rp.note ?? undefined}>
                          <RecommendationValue e={e} value={rp.value}
                            illustrative={rp.illustrative} sample={rp.sample} />
                          {rp.note && <div className="text-[10px] text-ink-soft break-words">{rp.note}</div>}
                        </div>
                      );
                    })()}
                  </td>
                  <td className="px-3 py-2 text-ink-muted max-w-[260px]">{e.error_message ?? "—"}</td>
                  <td className="px-3 py-2">
                    <DecisionCell e={e} decision={d} onChange={nd => setDecision(e.exception_id, nd)} />
                  </td>
                  <td className="px-3 py-2"><span className={`font-mono ${out.tone}`}>{out.text}</span></td>
                </tr>
                {expanded && (
                  <tr className="border-t border-border bg-surface-2/40">
                    <td colSpan={7} className="px-3 py-3">
                      {bdxBusy && !bdxRows.has(rowKey(e))
                        ? <span className="text-xs text-ink-muted">Loading bordereau row…</span>
                        : <BordereauRowDetail sheets={bdxRows.get(rowKey(e)) ?? null} e={e}
                            decision={d} onDecision={nd => setDecision(e.exception_id, nd)} />}
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* bottom bar — sticky so Save is always reachable without scrolling */}
      <div className="sticky bottom-0 z-20 flex items-center gap-3 px-3 py-3 mt-2 border-t border-border flex-wrap
                      bg-white/95 supports-[backdrop-filter]:bg-white/80 backdrop-blur rounded-b-lg
                      shadow-[0_-2px_8px_rgba(15,23,42,0.06)]">
        <div className="flex items-center gap-1.5 flex-wrap text-[11px]">
          <span className="pill pill-green">Approved {counts.approve}</span>
          <span className="pill pill-blue">Fixed {counts.fix}</span>
          <span className="pill pill-amber">Dismissed {counts.dismiss}</span>
          <span className="pill pill-grey">Pending {group.items.length - decided}</span>
          <span className="text-ink-soft ml-1">{decided}/{group.items.length} decided</span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {saveErr && <span className="text-[11px] text-danger">{saveErr}</span>}
          {saved && !saveErr && <span className="pill pill-green text-[11px]">Saved</span>}
          {backLink && (
            <Link to={backLink} state={backState}>
              <Button variant="ghost"><ArrowLeft size={14} /> Back</Button>
            </Link>
          )}
          <Button onClick={() => { setSaveErr(null); setConfirmOpen(true); }}
                  disabled={decided === 0 || saving}>
            Save decisions
          </Button>
        </div>
      </div>

      {/* Confirmation before committing decisions. */}
      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"
             onClick={() => setConfirmOpen(false)}>
          <div className="w-full max-w-sm rounded-xl border border-border bg-white shadow-xl p-5"
               onClick={e => e.stopPropagation()}>
            <div className="font-semibold text-base">Save decisions?</div>
            <p className="text-sm text-ink-muted mt-1">
              You're about to save {decided} decision{decided === 1 ? "" : "s"} for this rule.
            </p>
            <div className="flex items-center gap-1.5 flex-wrap text-[11px] mt-3">
              {counts.approve > 0 && <span className="pill pill-green">Approved {counts.approve}</span>}
              {counts.fix > 0 && <span className="pill pill-blue">Fixed {counts.fix}</span>}
              {counts.dismiss > 0 && <span className="pill pill-amber">Dismissed {counts.dismiss}</span>}
            </div>
            {exportId != null && (counts.fix > 0 || counts.approve > 0) && (
              <p className="text-[11px] text-ink-soft mt-3">
                Fixed &amp; Approved values reach the output only after you click{" "}
                <span className="font-medium">Re-generate output</span>.
              </p>
            )}
            <div className="flex justify-end gap-2 mt-5">
              <Button variant="ghost" onClick={() => setConfirmOpen(false)} disabled={saving}>Cancel</Button>
              <Button onClick={() => { setConfirmOpen(false); void save(); }} disabled={saving}>
                Confirm & save
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Cell tint once a decision is recorded, so the acted-on cell reads at a glance.
// Dismissed is neutral slate rather than amber — amber sat too close to the
// pending-warning orange to tell apart mid-scroll (see BdxInlineReview.DONE_BG).
const DECISION_CELL_BG: Record<DecisionKind, string> = {
  none: "", approve: "bg-emerald-100", fix: "bg-blue-100",
  dismiss: "bg-slate-200 text-slate-600", reject: "bg-red-100",
};

/**
 * The expanded "View in Bordereau" panel: one exception's FULL bordereau row —
 * every column with its header — that works like triage's View BDX: the
 * offending cell is clickable and opens the Approve/Fix/Dismiss popover right on
 * it. Decisions batch into the table's Save, same as the rest of the table.
 */
function BordereauRowDetail({ sheets, e, decision, onDecision }: {
  sheets: Sheet[] | null;
  e: StoredException;
  decision: Decision;
  onDecision: (d: Decision) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const cellRef = useRef<HTMLTableCellElement>(null);
  const norm = (s: string) => s.trim().toLowerCase();
  const sheet = (sheets ?? []).find(s => norm(s.sheet) === norm(e.source_sheet ?? ""));
  const rowIdx = e.source_row;
  const valid = !!sheet && rowIdx != null && Array.isArray(sheet.rows[rowIdx]);

  const header: any[] = valid ? (sheet!.rows[0] ?? []) : [];
  const row: any[] = valid ? sheet!.rows[rowIdx!] : [];
  // Cells the backend flagged on THIS row, and which of them are non-critical
  // (light orange in the workbook) rather than critical (light red).
  const flaggedCols = new Set(
    valid ? (sheet!.marks ?? []).filter(m => m[0] === rowIdx).map(m => m[1]) : []);
  const warnCols = new Set(
    valid ? (sheet!.warn_marks ?? []).filter(m => m[0] === rowIdx).map(m => m[1]) : []);
  // This exception's own cell is coloured from its own severity — it is the one
  // cell whose exception record we hold, so no need to infer it from the fill.
  const warnTarget = isWarnSeverity(e.severity);
  const noteAt = new Map(
    valid ? (sheet!.notes ?? []).filter(n => n.r === rowIdx).map(n => [n.c, n.text]) : []);
  // THIS exception's own column: match its field_path to a header, else fall back
  // to the (usually only) flagged cell. That's the cell made actionable.
  const fp = norm(e.field_path ?? "");
  let targetCol = fp ? header.findIndex(h => norm(String(h ?? "")) === fp) : -1;
  if (targetCol < 0 && flaggedCols.size) targetCol = [...flaggedCols][0];

  // Scroll the offending cell into the middle of the panel horizontally, so the
  // expansion opens already pointing at where the exception is (a wide BDX would
  // otherwise open on the leftmost columns). Only the panel's own scrollbox
  // moves — computed from bounding rects rather than scrollIntoView, which would
  // also nudge the whole page.
  useEffect(() => {
    const box = scrollRef.current, cell = cellRef.current;
    if (!box || !cell) return;
    const br = box.getBoundingClientRect(), cr = cell.getBoundingClientRect();
    box.scrollLeft += (cr.left - br.left) - (box.clientWidth - cr.width) / 2;
  }, [valid, targetCol, e.exception_id]);

  if (!valid) {
    return <span className="text-xs text-ink-muted">This row is no longer available in the current output.</span>;
  }

  return (
    // width:0 + minWidth:100% is the "child can't widen its parent" trick: the
    // panel fills the row's visible width but never forces the exception table
    // wider, so ONLY this grid scrolls sideways — the outer table stays put.
    <div style={{ width: 0, minWidth: "100%" }}>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-[11px] font-semibold text-ink-muted">Bordereau row · {sheet!.sheet}</span>
        {targetCol >= 0 && (
          <span className="text-[11px] text-ink-soft">Click the highlighted cell to Approve or Fix</span>
        )}
      </div>
      {/* flex:none + minWidth:100% override tbl-scroll-x's flex defaults so the
          scroll box is bounded by the panel above rather than by its content. */}
      <div ref={scrollRef} className="tbl-wrap tbl-scroll-x border border-border rounded-md"
        style={{ flex: "none", minWidth: "100%" }}>
        <table>
          <thead>
            <tr>
              {header.map((h, i) => (
                <th key={i} className={
                  i === targetCol
                    ? (warnTarget ? "!bg-[#FDF0DF]" : "!bg-[#FCE9EC]")
                    : flaggedCols.has(i)
                      ? (warnCols.has(i) ? "!bg-[#FDF0DF]" : "!bg-[#FCE9EC]")
                      : undefined}>
                  {String(h ?? "")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              {row.map((c, i) => {
                const text = String(c ?? "");
                if (i === targetCol) {
                  // The actionable cell — its own Approve/Fix/Dismiss popover.
                  return (
                    <td key={i} ref={cellRef} className="!p-0">
                      <DecisionCell e={e} decision={decision} onChange={onDecision}
                        trigger={({ onClick, open }) => (
                          <button type="button" onClick={onClick}
                            title={noteAt.get(i) || "Click to Approve or Fix"}
                            className={`w-full h-full px-3 py-2.5 inline-flex items-center justify-center gap-1 cursor-pointer transition
                              ${decision.kind !== "none"
                                ? DECISION_CELL_BG[decision.kind]
                                : warnTarget
                                  ? "bg-[#FFE0B2] text-[#8F580D] font-semibold"
                                  : "bg-[#FFC7CE] text-[#9B1C2E] font-semibold"}
                              ${open ? "ring-2 ring-inset ring-navy" : "hover:brightness-95"}`}>
                            <span>{text}</span>
                            <ChevronDown size={11} className="opacity-60" />
                          </button>
                        )} />
                    </td>
                  );
                }
                const flagged = flaggedCols.has(i);
                return (
                  <td key={i} title={flagged ? (noteAt.get(i) || "Failed validation") : undefined}
                    style={flagged ? flagStyle(warnCols.has(i)) : undefined}>
                    {text}
                  </td>
                );
              })}
            </tr>
          </tbody>
        </table>
      </div>
      {/* Fallback when no cell could be matched to this exception — keep the
          panel actionable with the standard decision control. */}
      {targetCol < 0 && (
        <div className="mt-2.5 flex items-center gap-2 text-xs">
          <span className="text-ink-muted">Decision:</span>
          <DecisionCell e={e} decision={decision} onChange={onDecision} />
        </div>
      )}
    </div>
  );
}
