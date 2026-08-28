/**
 * BdxInlineReview — the generated output BDX rendered directly IN the Exception
 * Triage page (instead of the View BDX modal), with the flagged cells actionable
 * in place:
 *
 *   • every sheet, every row, with the SAME light-red failed-cell highlighting
 *     as the downloaded workbook (marks come from the stored blob);
 *   • clicking a highlighted cell expands the error right there — rule, contract
 *     clause, actual vs recommended — with the same Approve / Fix / Dismiss
 *     decisions as the rule review screen, saved immediately;
 *   • a column header whose column has pending flags offers "Approve all", which
 *     approves every flagged row in that column with its recommended value.
 *
 * Decisions go through the same export decide endpoint as the rule tables
 * (keyed by rule + policy + field + sheet/row), so the two views never diverge —
 * the parent reloads exceptions after each save and the cell tints update:
 * green = approved, blue = fixed, amber = dismissed, red = still pending.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, Wrench, Hand, X } from "lucide-react";
import { streamNdjson } from "../api/client";
import { useRowWindow, WINDOW_MIN_ROWS } from "../hooks/useRowWindow";
import { getUser } from "../auth";
import { saveExportDecisions, type StoredException } from "../api/validation";
import { decisionKindOf, type DecisionKind } from "./ExceptionCards";
import RuleExplanationBlock, { hasExplanation } from "./RuleExplanation";
import { writeValue, enumOptions, recoParts, RecommendationValue, type Decision } from "./ExceptionDecisionTable";
import {
  dataFirst, HL_BG, HL_BD, HL_FG, HL_WARN_BG, HL_WARN_BD,
  flagStyle, isWarnSeverity, type Sheet,
} from "./OutputRows";
import Button from "./ui/Button";

/** Normalised name, memoised.
 *
 *  Indexing calls this a few times per exception, and a large export carries
 *  85k of them across a dozen sheets — but the DISTINCT values are a handful of
 *  sheet names and column headers. Without the cache the regex + trim +
 *  lowercase runs a quarter of a million times per pass for a few dozen
 *  answers, which measured as the single largest cost in `indexSheet`. Bounded
 *  so a pathological export can't grow it without limit. */
const _normCache = new Map<unknown, string>();
const norm = (s: unknown) => {
  const hit = _normCache.get(s);
  if (hit !== undefined) return hit;
  const v = String(s ?? "").replace(/\s+/g, " ").trim().toLowerCase();
  if (_normCache.size < 4096) _normCache.set(s, v);
  return v;
};

/** Corrected value recovered from a saved decision's resolution note (same
 *  format ExceptionDecisionTable writes / restores). */
function decidedValue(e: StoredException): string | null {
  const m = (e.resolution_note ?? "").match(/(?:Fixed with user value|Approved with value):\s*([^—]+)/i);
  return m ? m[1].trim() : null;
}

const SEV_BADGE: Record<string, string> = { critical: "b-crit", warning: "b-warn", info: "b-info" };
const SEV_LABEL: Record<string, string> = { critical: "Critical", warning: "Warning", info: "Info" };

// Decided-cell tints (pending keeps the workbook's own highlight — light red for
// critical, light orange for warning). Dismissed is deliberately NEUTRAL slate,
// not amber: amber sat right next to the pending-warning orange and the two read
// as the same state at a glance. Grey also says what dismissing means — kept
// as-is, no change written — so the four decided states now separate by hue
// (green / blue / grey) as well as by outline.
const DONE_BG: Record<string, string> = { approve: "#DCFCE7", fix: "#DBEAFE", dismiss: "#E2E8F0", reject: "#FEE2E2" };
const DONE_FG: Record<string, string> = { approve: "#166534", fix: "#1D4ED8", dismiss: "#475569", reject: "#991B1B" };
const DONE_BD: Record<string, string> = { approve: "#A7F3D0", fix: "#BFDBFE", dismiss: "#CBD5E1", reject: "#FECACA" };
const DONE_LABEL: Record<string, string> = { approve: "Approved", fix: "Fixed", dismiss: "Dismissed", reject: "Rejected" };

// ─── sheet ⇄ exception indexing ──────────────────────────────────────────────

type SheetIndex = {
  /** `${gridRow}:${col}` → exceptions on that cell. */
  byCell: Map<string, StoredException[]>;
  /** col index → its flagged exceptions, split pending / decided. */
  colFlags: Map<number, { pending: StoredException[]; decided: number }>;
  mapped: Set<StoredException>;
};

/** Largest row shift considered when aligning exceptions to grid rows.
 *
 *  The shift models a template carrying extra header rows — a handful of them,
 *  never thousands. Without a bound, a sheet with one flagged column and 10k
 *  rows proposes one candidate shift PER ROW (each row's distance to that
 *  column's first mark), and scoring them all is quadratic in the sheet: on a
 *  real 10,368-row export that came to 859 million probes, which locks the tab
 *  for minutes. Any shift beyond this isn't a header offset, it's noise. */
const MAX_ROW_SHIFT = 200;

/** Exceptions grouped by their normalised source sheet.
 *
 *  Every sheet needs only its OWN exceptions, but finding them used to mean
 *  each sheet scanning the whole export — and the tab bar indexes a dozen
 *  sheets, so opening the grid walked 85k exceptions a dozen times over before
 *  a row appeared. Bucketing once turns that back into a single pass. */
function bucketBySheet(exceptions: StoredException[]): Map<string, StoredException[]> {
  const m = new Map<string, StoredException[]>();
  for (const e of exceptions) {
    if (e.source_row == null || !e.field_path) continue;
    const k = norm(e.source_sheet);
    const a = m.get(k);
    if (a) a.push(e); else m.set(k, [e]);
  }
  return m;
}

/** Candidate offsets are scored on a sample of at most this many exceptions
 *  before the best few are re-scored in full. Scoring every candidate against
 *  every exception is the product of the two — 201 offsets x 83k exceptions =
 *  16.7M probes, ~550ms of blocked main thread every time the active sheet
 *  changes. The sample is a filter, not the answer: whichever offsets look best
 *  on it are then measured exactly, so the result is the same one the full
 *  search picks while costing a fraction of it. */
const OFFSET_SAMPLE = 2048;
const OFFSET_FINALISTS = 5;

/** `totalRows` is the sheet's true extent. It can't be taken from `sheet.rows`
 *  any more: rows now arrive by streaming, so callers pass just the header and
 *  `rows.length` would be 1 — which silently discarded every exception, leaving
 *  the grid with nothing clickable and "0 pending" however many there were.
 *
 *  `sheetExceptions` are already narrowed to this sheet (see `bucketBySheet`). */
function indexSheet(sheet: Sheet, sheetExceptions: StoredException[], totalRows?: number): SheetIndex {
  const header: any[] = sheet.rows[0] ?? [];
  const colOf = new Map<string, number>();
  header.forEach((h, i) => { const n = norm(h); if (n && !colOf.has(n)) colOf.set(n, i); });

  // Row/column of each exception, resolved ONCE. The offset search below probes
  // these millions of times on a large sheet, and re-normalising a field name
  // per probe is what made that unaffordable.
  const own: StoredException[] = [];
  const cols: number[] = [];
  for (const e of sheetExceptions) {
    const ci = colOf.get(norm(e.field_path));
    if (ci === undefined) continue;
    own.push(e);
    cols.push(ci);
  }
  const extent = totalRows ?? Math.max(0, sheet.rows.length - 1);
  const n = own.length;
  const ownRow = new Int32Array(n);
  const ownCol = Int32Array.from(cols);
  for (let i = 0; i < n; i++) ownRow[i] = own[i].source_row as number;

  // Marks as packed numbers rather than "row:col" strings, so a probe is an
  // integer Set lookup instead of building a string that is thrown away.
  const marks = sheet.marks ?? [];
  const packed = (r: number, c: number) => r * 16384 + c;
  const markKeys = new Set<number>();
  const firstMarkRowByCol = new Map<number, number>();
  for (const m of marks) {
    markKeys.add(packed(m[0], m[1]));
    if (!firstMarkRowByCol.has(m[1])) firstMarkRowByCol.set(m[1], m[0]);
  }

  // Row offset between the exception's 1-based record index and its grid row.
  // Normally record i sits at grid row i (header = row 0); a template with extra
  // header rows shifts everything, so pick the offset that lands the most
  // exceptions on cells the workbook actually highlighted.
  const offsets: number[] = [0];
  const seenOffsets = new Set<number>([0]);
  for (let i = 0; i < n; i++) {
    const mr = firstMarkRowByCol.get(ownCol[i]);
    if (mr === undefined) continue;
    const o = mr - ownRow[i];
    if (o >= -MAX_ROW_SHIFT && o <= MAX_ROW_SHIFT && !seenOffsets.has(o)) {
      seenOffsets.add(o);
      offsets.push(o);
    }
  }
  const score = (o: number, step: number) => {
    let hits = 0;
    for (let i = 0; i < n; i += step)
      if (markKeys.has(packed(ownRow[i] + o, ownCol[i]))) hits++;
    return hits;
  };
  let offset = 0;
  if (offsets.length > 1) {
    let finalists = offsets;
    const step = Math.ceil(n / OFFSET_SAMPLE);
    if (step > 1 && offsets.length > OFFSET_FINALISTS) {
      const keep = new Set(
        offsets
          .map(o => ({ o, s: score(o, step) }))
          .sort((a, b) => b.s - a.s)
          .slice(0, OFFSET_FINALISTS)
          .map(x => x.o),
      );
      keep.add(0);   // 0 is the norm; never let sampling drop it
      // Back into candidate order, so an exact tie still resolves to the
      // earliest candidate exactly as the unsampled search did.
      finalists = offsets.filter(o => keep.has(o));
    }
    let bestHits = -1;
    for (const o of finalists) {
      const hits = score(o, 1);
      if (hits > bestHits) { bestHits = hits; offset = o; }
    }
  }

  const byCell = new Map<string, StoredException[]>();
  const colFlags = new Map<number, { pending: StoredException[]; decided: number }>();
  const mapped = new Set<StoredException>();
  for (let i = 0; i < n; i++) {
    const e = own[i];
    const ci = ownCol[i];
    const gi = ownRow[i] + offset;
    if (gi < 1 || gi > extent) continue;
    const k = `${gi}:${ci}`;
    if (!byCell.has(k)) byCell.set(k, []);
    byCell.get(k)!.push(e);
    mapped.add(e);
    if (!colFlags.has(ci)) colFlags.set(ci, { pending: [], decided: 0 });
    const cf = colFlags.get(ci)!;
    if (decisionKindOf(e)) cf.decided += 1; else cf.pending.push(e);
  }
  return { byCell, colFlags, mapped };
}

// ─── column Approve-all, grouped by rule/clause ──────────────────────────────
// One column can be checked by SEVERAL clauses (rules), each recommending its
// own value — so a single blanket "approve everything in this column" would be
// ambiguous. The header popover therefore lists the rules separately and the
// user ticks which one(s) to approve.

type ColRuleGroup = {
  key: string;
  ruleId: number | null;
  ruleName: string;
  severity: string;
  clause: string | null;
  /** Plain-English requirement — what this rule wants on this column. Shown in
   *  place of the raw clause, which was unreadable truncated to one line. */
  requirement: string | null;
  pending: StoredException[];
  approvable: StoredException[];
  /** The recommended value when every approvable row shares one; null if mixed. */
  value: string | null;
};

function groupColPending(pending: StoredException[]): ColRuleGroup[] {
  const m = new Map<string, ColRuleGroup>();
  for (const e of pending) {
    const key = e.rule_id != null ? `r${e.rule_id}` : `n${e.rule_name ?? e.error_message ?? "?"}`;
    let g = m.get(key);
    if (!g) {
      g = {
        key, ruleId: e.rule_id, ruleName: e.rule_name ?? "Validation rule",
        severity: e.severity ?? "info", clause: e.contract_clause_text,
        requirement: e.explanation?.requirement ?? null,
        pending: [], approvable: [], value: null,
      };
      m.set(key, g);
    }
    g.pending.push(e);
    if (writeValue(e, { kind: "approve" } as Decision) != null) g.approvable.push(e);
  }
  for (const g of m.values()) {
    const vals = new Set(g.approvable.map(e => writeValue(e, { kind: "approve" } as Decision)));
    g.value = vals.size === 1 ? ([...vals][0] ?? null) : null;
  }
  return [...m.values()];
}

// ─── popover placement (viewport-fixed, flips up when out of room) ───────────

type Pos = { left: number; top?: number; bottom?: number; maxH: number };
// estH covers the tallest realistic popover: the plain-English rule explanation
// (chip + requirement + what's-wrong) sits above the actual/recommended grid and
// the action buttons. Under-estimating it makes the panel run off the bottom of
// the viewport on lower rows instead of flipping up.
function place(r: DOMRect, w = 360, estH = 400): Pos {
  const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
  // Pick the side of the cell with room, and CAP the popover to that side's
  // space — a tall popover (a cell with several rules stacked) then scrolls
  // inside itself instead of growing past the viewport edge and clipping.
  const spaceBelow = window.innerHeight - r.bottom - 12;
  const spaceAbove = r.top - 12;
  if (spaceBelow < estH && spaceAbove > spaceBelow)
    return { left, bottom: window.innerHeight - r.top + 4,
             maxH: Math.max(160, spaceAbove - 4) };
  return { left, top: r.bottom + 4, maxH: Math.max(160, spaceBelow - 4) };
}

// ─── one exception's detail + actions inside the cell popover ────────────────

function ExcDetail({ e, saving, onDecide }: {
  e: StoredException;
  saving: boolean;
  onDecide: (e: StoredException, kind: "approve" | "fix" | "dismiss", value?: string | null) => void;
}) {
  const [mode, setMode] = useState<null | "fix" | "approve-enum">(null);
  const [draft, setDraft] = useState("");
  const opts = enumOptions(e);
  const approvable = writeValue(e, { kind: "approve" } as Decision) != null;
  const rp = recoParts(e);
  const saved = decisionKindOf(e) as DecisionKind | null;
  const clause = e.contract_clause_text
    ? (e.contract_clause_text.length > 200 ? e.contract_clause_text.slice(0, 200) + "…" : e.contract_clause_text)
    : null;

  function startApprove() {
    if (opts) { setDraft(""); setMode("approve-enum"); return; }
    onDecide(e, "approve", writeValue(e, { kind: "approve" } as Decision));
  }
  function startFix() {
    setDraft(decidedValue(e) ?? writeValue(e, { kind: "approve" } as Decision) ?? "");
    setMode("fix");
  }

  return (
    <div>
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`badge ${SEV_BADGE[e.severity ?? "info"] ?? "b-info"}`}>
          <span className="d" />{SEV_LABEL[e.severity ?? "info"] ?? e.severity}
        </span>
        <span className="font-semibold text-xs">{e.rule_name ?? "Validation rule"}</span>
        {e.rule_id != null && <span className="text-[10px] font-mono text-ink-soft">RULE-{e.rule_id}</span>}
        {saved && (
          <span className="ml-auto text-[10px] font-semibold px-1.5 py-0.5 rounded"
            style={{ background: DONE_BG[saved], color: DONE_FG[saved] }}>
            {DONE_LABEL[saved]}
          </span>
        )}
      </div>

      {/* What the rule requires, in plain English. `compact` drops the
          collapsible and the how-to-fix — the popover is only 360px wide and
          its placement maths assumes a bounded height. The full clause stays
          reachable as the hover title. */}
      {hasExplanation(e.explanation) ? (
        <div className="mt-1.5" title={e.contract_clause_text ?? undefined}>
          <RuleExplanationBlock explanation={e.explanation} variant="tw" compact />
        </div>
      ) : (
        <>
          {e.error_message && (
            <p className="text-xs text-ink-muted mt-1.5 leading-relaxed">{e.error_message}</p>
          )}
          {clause && (
            <p className="text-[11px] italic text-ink-soft mt-1 leading-relaxed"
              title={e.contract_clause_text ?? undefined}>
              "{clause}"{e.contract_clause_page ? ` — p.${e.contract_clause_page}` : ""}
            </p>
          )}
        </>
      )}

      <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 mt-2 text-xs">
        <span className="text-ink-soft">Actual</span>
        <span className="font-mono" style={{ color: HL_FG }}>
          {e.actual_value?.trim() ? e.actual_value : <i>(empty)</i>}
        </span>
        {/* A format rule has no recommended value — the row shows an EXAMPLE of
            the required shape, so it is labelled as one rather than as the value
            to accept. */}
        <span className="text-ink-soft">{rp?.illustrative ? "Example" : "Recommended"}</span>
        <span>
          {rp
            ? <>
                <RecommendationValue e={e} value={rp.value}
                  illustrative={rp.illustrative} sample={rp.sample} />
                {rp.note && <span className="block text-[10px] text-ink-soft">{rp.note}</span>}
              </>
            : <span className="italic text-amber-600">No single recommendation — enter a fix</span>}
        </span>
      </div>

      {/* actions */}
      {mode === null && (
        <div className="flex items-center gap-2 mt-3">
          <button
            onClick={startApprove}
            disabled={saving || (!opts && !approvable)}
            title={!opts && !approvable ? "No single recommended value — use Fix" : undefined}
            className="inline-flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-md border
                       border-emerald-300 bg-emerald-50 text-emerald-700 hover:bg-emerald-100
                       disabled:opacity-45 disabled:cursor-not-allowed transition">
            <Check size={13} /> Approve
          </button>
          <button
            onClick={startFix} disabled={saving}
            className="inline-flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-md border
                       border-blue-300 bg-blue-50 text-blue-700 hover:bg-blue-100 disabled:opacity-45 transition">
            <Wrench size={13} /> Fix…
          </button>
          <button
            onClick={() => onDecide(e, "dismiss")} disabled={saving}
            title="Keep the actual value and pass it through unchanged"
            className="inline-flex items-center gap-1 text-xs font-medium px-2.5 py-1 rounded-md border
                       border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100 disabled:opacity-45 transition">
            <Hand size={13} /> Dismiss
          </button>
        </div>
      )}

      {mode === "fix" && (
        <div className="mt-3">
          <label className="block text-[11px] text-ink-soft mb-1">Corrected value</label>
          <input autoFocus value={draft} onChange={ev => setDraft(ev.target.value)}
            onKeyDown={ev => { if (ev.key === "Enter" && draft.trim()) onDecide(e, "fix", draft.trim()); }}
            placeholder={rp ? (rp.sample ? `e.g. ${rp.value}` : rp.value) : "Corrected value…"}
            className="input py-1 text-xs w-full" />
          <div className="flex justify-end gap-2 mt-2">
            <button className="text-xs px-2 py-1 rounded-md hover:bg-surface-2 text-ink-muted"
              onClick={() => setMode(null)}>Cancel</button>
            <Button onClick={() => onDecide(e, "fix", draft.trim())} disabled={saving || !draft.trim()}>
              Save fix
            </Button>
          </div>
        </div>
      )}

      {mode === "approve-enum" && opts && (
        <div className="mt-3">
          <div className="text-[11px] text-ink-soft mb-1">The rule allows several values — pick one.</div>
          <div className="max-h-36 overflow-y-auto space-y-0.5">
            {opts.map(o => (
              <label key={o} className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-surface-2 cursor-pointer text-xs">
                <input type="radio" checked={draft === o} onChange={() => setDraft(o)} className="h-3 w-3" />
                <span className="font-mono">{o}</span>
              </label>
            ))}
          </div>
          <div className="flex justify-end gap-2 mt-2">
            <button className="text-xs px-2 py-1 rounded-md hover:bg-surface-2 text-ink-muted"
              onClick={() => setMode(null)}>Cancel</button>
            <Button onClick={() => onDecide(e, "approve", draft)} disabled={saving || !draft.trim()}>
              Approve
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── main component ──────────────────────────────────────────────────────────

// Rows per streamed chunk (the server caps it at 5000). This was 5 while the
// mechanism was being eyeballed, which is fine for a 50-row sheet and fatal for
// a real one: a 10,000-row sheet became 2,000 chunks, so 2,000 state updates
// and 2,000 re-renders of a table that grows to 10,000 rows. 500 keeps the
// grid filling in visibly while making that 20 messages instead of 2,000.
const STREAM_CHUNK = 500;

/** Both knobs are overridable from the page URL so the streaming can be watched
 *  without a rebuild:
 *      ?stream_chunk=1     rows per chunk
 *      ?stream_delay=300   ms the server waits between chunks
 *  A window arrives in a few milliseconds — far quicker than a screen can show
 *  — so `stream_delay` is what makes the chunk-by-chunk arrival visible.
 *  Neither is set in normal use. */
function streamOpts(): { chunk?: number; delayMs?: number } {
  try {
    const q = new URLSearchParams(window.location.search);
    const n = (k: string) => {
      const v = Number(q.get(k));
      return Number.isFinite(v) && v > 0 ? v : undefined;
    };
    return { chunk: n("stream_chunk"), delayMs: n("stream_delay") };
  } catch {
    return {};
  }
}

/** Extra rows rendered above and below the viewport, so a flick of the scroll
 *  wheel doesn't outrun React and expose blank space. */
const ROW_OVERSCAN = 30;
/** Row height assumed before a real row has been measured — only ever used for
 *  the very first paint, after which the actual height takes over. */
const EST_ROW_H = 33;

/** Rows asked for in one request. Matches the server's own page size — it caps
 *  anything larger anyway — and is deliberately a WINDOW rather than the sheet:
 *  the grid fetches what it is about to show and comes back for more, so
 *  opening a 10k-row sheet costs the same as opening a 400-row one. */
const PAGE_ROWS = 600;
/** How far past the rendered window to keep rows loaded, so ordinary scrolling
 *  lands on rows that are already there instead of on placeholders. */
const PREFETCH_ROWS = 200;
/** Rows per request when they have to be listed individually (the Exceptions
 *  filter, whose rows are scattered) — they travel in the query string, so this
 *  is smaller than a contiguous page. */
const SCATTERED_ROWS = 300;

type RawSheetPage = {
  sheet: string;
  rows: any[][];
  row_gis?: number[];
  marks: number[][];
  warn_marks?: number[][];
  notes: { r: number; c: number; text: string }[];
  total_rows: number;
  is_data?: boolean;
};

type SheetState = {
  header: any[];
  marks: number[][];
  /** The non-critical subset of `marks` — the cells the workbook paints light
   *  orange. Only needed for flagged cells with no exception record of their
   *  own; where there IS one, its `severity` decides the colour. */
  warnMarks: number[][];
  /** "r:c" → comment text, for the rows loaded so far. A Map rather than an
   *  array because the alternative — keeping the raw list and rebuilding a
   *  lookup with `useMemo` — rebuilt it on every chunk, since each chunk
   *  produces a fresh array and so invalidates the memo. */
  notes: Map<string, string>;
  totalRows: number;
  isData?: boolean;
  /** gi (1-based data row; header is 0) → that row's cell values. Sparse: only
   *  the windows actually fetched are present. */
  cells: Map<number, any[]>;
  /** Rows a completed request asked for but did not return — they exist in the
   *  sheet's declared extent but carry no values. Tracked so the grid stops
   *  asking for them; without this the loader would re-request the same gap
   *  forever and never settle. */
  absent: Set<number>;
};

const emptySheet = (): SheetState => ({
  header: [], marks: [], warnMarks: [], notes: new Map(), totalRows: 0,
  cells: new Map(), absent: new Set(),
});

// Stand-ins for "no sheet active yet". Shared frozen instances rather than
// fresh literals: these feed the row-loading effect's dependency list, and a
// new empty Map on every render would re-run it on every render.
const EMPTY_CELLS: Map<number, any[]> = new Map();
const EMPTY_ABSENT: Set<number> = new Set();
const EMPTY_NOTES: Map<string, string> = new Map();

// Folds the stream's opening metadata line into per-sheet state. Every sheet in
// it contributes its header/total_rows/is_data (cheap, always sent); only
// `activeSheet` — the one being streamed — carries real marks, so that is the
// only such field ever overwritten (a sheet's marks/cells, once loaded, are
// never blown away by a later stream of a DIFFERENT sheet returning it as a
// cheap, marks-less bystander). Rows and notes arrive on the chunk lines that
// follow, not here.
function mergeSheetPages(prev: Map<string, SheetState>, activeSheet: string, raw: RawSheetPage[]): Map<string, SheetState> {
  const next = new Map(prev);
  for (const sh of raw) {
    const isActive = sh.sheet === activeSheet;
    const old = next.get(sh.sheet) ?? emptySheet();
    next.set(sh.sheet, {
      ...old,
      header: sh.rows?.[0] ?? old.header,
      totalRows: sh.total_rows ?? old.totalRows,
      isData: sh.is_data ?? old.isData,
      marks: isActive ? (sh.marks ?? []) : old.marks,
      warnMarks: isActive ? (sh.warn_marks ?? []) : old.warnMarks,
    });
  }
  return next;
}

export default function BdxInlineReview({ exportId, exceptions, onSaved, onClose }: {
  exportId: string;
  exceptions: StoredException[];
  /** Called after a successful save so the parent can reload exceptions. */
  onSaved: (saved: number) => void;
  onClose: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  /** Measured height of one body row, used to size the spacers that stand in
   *  for the rows not currently rendered. Measured rather than assumed so the
   *  scrollbar stays truthful whatever the theme or zoom does to row height. */
  const rowHRef = useRef<number>(0);
  /** The one request currently open, and which sheet it is filling. At most one
   *  runs at a time — starting another aborts this — so two streams can never
   *  interleave writes into the same state. `sheet` is "" until the opening
   *  stream's metadata line names the sheet the server chose. */
  const inFlightRef = useRef<{ ctl: AbortController; sheet: string } | null>(null);
  /** Sheets whose header/marks have arrived. Only the FIRST request for a sheet
   *  asks for them: they are whole-sheet and, on a heavily flagged export, the
   *  marks alone are ~0.8 MB that would otherwise be re-sent on every scroll. */
  const metaRef = useRef<Set<string>>(new Set());

  // `sheetOrder` is the data-first tab order (sheet NAMES only); `sheetStates`
  // holds each sheet's header/marks/total row count plus the rows received so
  // far. A sheet's dimensions arrive first, then the grid pulls row WINDOWS as
  // it needs them — see `loadWindow` — rather than the whole sheet up front.
  const [sheetOrder, setSheetOrder] = useState<string[] | null>(null);
  const [sheetStates, setSheetStates] = useState<Map<string, SheetState>>(new Map());

  // Incoming row chunks are written directly into the SheetState.cells Maps
  // held here, then a single re-render is scheduled per animation frame.
  // `cellsRev` is the render trigger — `cells` is mutated in place, so React
  // has no reference change to notice on its own.
  // The ref — not the state — is the authoritative copy, and every write goes
  // through it FIRST. Chunks arrive faster than React commits, so a `rows`
  // message can land before the `meta` that created the sheet has rendered;
  // syncing the ref from state during render would make those rows vanish.
  const sheetStatesRef = useRef<Map<string, SheetState>>(new Map());
  const [cellsRev, setCellsRev] = useState(0);
  const flushRef = useRef<number | null>(null);
  const scheduleRowFlush = useCallback(() => {
    if (flushRef.current !== null) return;   // a flush is already queued
    flushRef.current = window.requestAnimationFrame(() => {
      flushRef.current = null;
      setCellsRev(v => v + 1);
    });
  }, []);
  useEffect(() => () => {
    if (flushRef.current !== null) window.cancelAnimationFrame(flushRef.current);
  }, []);
  const [name, setName] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [streamErr, setStreamErr] = useState<string | null>(null);
  /** True while a window request is open. Doubles as the loader's trigger: it
   *  flips false when a request settles, which re-runs the effect below to
   *  fetch whatever the view still lacks. */
  const [loadingRows, setLoadingRows] = useState(false);
  const [tab, setTab] = useState(0);
  /** Where the grid is scrolled to, and how tall it is — the two numbers that
   *  decide which rows are worth rendering. */
  const [viewport, setViewport] = useState({ top: 0, height: 0 });
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  // Row filter: "all" shows every policy; "exceptions" shows only rows with a
  // flagged cell — the ones needing a decision.
  const [rowFilter, setRowFilter] = useState<"all" | "exceptions">("all");
  // open popovers — a flagged cell, or a column header's Approve-all (never both)
  const [cellPop, setCellPop] = useState<{ key: string; pos: Pos } | null>(null);
  const [colPop, setColPop] = useState<{ ci: number; pos: Pos } | null>(null);
  // Which rules (clauses) are ticked in the column Approve-all popover.
  const [colSel, setColSel] = useState<Set<string>>(new Set());

  useEffect(() => {
    rootRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  // One request per WINDOW of rows, consumed as it arrives.
  //
  // `loadWindow` opens the NDJSON stream for a bounded set of rows and folds
  // each line into state as it lands. The first request for a sheet also brings
  // every sheet's header/row-count plus this sheet's whole-sheet marks (so the
  // tab bar, the highlighting and the pending/resolved counts are right before
  // a single row shows up); later requests skip all of that and carry rows
  // alone. Each chunk line appends its rows, so a window visibly fills in
  // rather than appearing at once.
  //
  // What this deliberately does NOT do is fetch the whole sheet. Doing so made
  // opening a 10k-row tab cost ~9.5 MB of JSON and dozens of full re-renders
  // before anything was usable — for the ~60 rows that fit on screen — which is
  // what wedged the page. Cost now tracks what the user actually looks at.
  const loadWindow = useRef<(sheetName: string | undefined, want: {
    offset?: number; max_rows?: number; row_gis?: number[];
  }) => Promise<void>>(async () => {});
  loadWindow.current = async (sheetName, want) => {
    const ctl = new AbortController();
    inFlightRef.current?.ctl.abort();   // never let two streams write the same state
    inFlightRef.current = { ctl, sheet: sheetName ?? "" };
    setLoadingRows(true);
    let target = sheetName ?? "";
    const needMeta = !sheetName || !metaRef.current.has(sheetName);
    try {
      const { chunk, delayMs } = streamOpts();
      await streamNdjson(
        `/export/downloads/${exportId}/data/stream`,
        {
          marks: needMeta ? 1 : 0,
          meta: needMeta ? 1 : 0,
          ...(chunk ? { chunk } : {}),
          ...(delayMs ? { delay_ms: delayMs } : {}),
          ...(sheetName ? { sheet: sheetName } : {}),
          ...(want.offset != null ? { offset: want.offset } : {}),
          ...(want.max_rows != null ? { max_rows: want.max_rows } : {}),
          ...(want.row_gis?.length ? { row_gis: want.row_gis.join(",") } : {}),
        },
        msg => {
          if (msg.type === "meta") {
            // The opening request doesn't name a sheet — the server picks one,
            // and says which here.
            target = msg.target ?? "";
            if (inFlightRef.current?.ctl === ctl) inFlightRef.current.sheet = target;
            if (msg.filename) setName(msg.filename);
            if (msg.sheets) {
              const raw: RawSheetPage[] = msg.sheets;
              // Both the sheet the server chose AND the one asked for. They
              // always agree in practice, but recording only the server's would
              // let a disagreement spin the loader below forever, since it
              // re-requests any sheet whose meta it hasn't seen.
              if (target) metaRef.current.add(target);
              if (sheetName) metaRef.current.add(sheetName);
              setSheetStates(prev => mergeSheetPages(prev, target, raw));
              // Establish tab order once, from the first request of the export.
              setSheetOrder(prev => prev ?? dataFirst(
                raw.map(s => ({ sheet: s.sheet, rows: [s.rows?.[0] ?? []], is_data: s.is_data })) as Sheet[],
              ).map(s => s.sheet));
            }
            setBusy(false);          // headers are enough to draw the grid
          } else if (msg.type === "rows") {
            setSheetStates(prev => {
              const st = prev.get(msg.sheet);
              if (!st) return prev;
              const cells = new Map(st.cells);
              (msg.row_gis ?? []).forEach((gi: number, i: number) => cells.set(gi, msg.rows[i]));
              // Each chunk brings the cell comments for its own rows (the whole
              // sheet's would dwarf everything else), so they accumulate
              // alongside the rows they belong to.
              const chunkNotes: { r: number; c: number; text: string }[] = msg.notes ?? [];
              let notes = st.notes;
              if (chunkNotes.length) {
                notes = new Map(st.notes);
                for (const n of chunkNotes) notes.set(`${n.r}:${n.c}`, n.text);
              }
              const next = new Map(prev);
              next.set(msg.sheet, { ...st, cells, notes });
              return next;
            });
          } else if (msg.type === "done") {
            // Rows the request asked for but that carry no values. Recorded so
            // the loader below stops asking for them — otherwise it would see
            // them missing, request them again, and never settle.
            const gap: number[] = msg.absent ?? [];
            if (gap.length && msg.sheet) {
              setSheetStates(prev => {
                const st = prev.get(msg.sheet);
                if (!st) return prev;
                const absent = new Set(st.absent);
                for (const gi of gap) absent.add(gi);
                const next = new Map(prev);
                next.set(msg.sheet, { ...st, absent });
                return next;
              });
            }
          } else if (msg.type === "error") {
            setStreamErr(msg.detail ?? "The output could not be streamed.");
          }
        },
        ctl.signal,
      );
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        setStreamErr(e?.message ?? "The output could not be streamed.");
        setSheetOrder(prev => prev ?? []);
      }
    } finally {
      // Only the CURRENT request may clear the shared flags. An aborted one —
      // a tab switch, or a new export — settles a tick AFTER its replacement
      // has already started, so clearing unconditionally would report the
      // replacement as finished: the loader would see an idle slot, fire a
      // second request for the same rows, and abort the one already running.
      if (inFlightRef.current?.ctl === ctl) {
        inFlightRef.current = null;
        setBusy(false);
        setLoadingRows(false);
      }
    }
  };

  // Opening the grid — fetch the workbook's default sheet's dimensions and its
  // first window. Everything after this is driven by what the viewport needs.
  useEffect(() => {
    setBusy(true); setStreamErr(null);
    sheetStatesRef.current = new Map();   // authoritative copy — reset it too
    setSheetStates(new Map()); setSheetOrder(null); setTab(0);
    metaRef.current = new Set();
    void loadWindow.current(undefined, { offset: 0, max_rows: PAGE_ROWS });
    return () => { inFlightRef.current?.ctl.abort(); inFlightRef.current = null; };
  }, [exportId]);

  const activeSheetName = sheetOrder?.[tab];
  const activeState = activeSheetName ? sheetStates.get(activeSheetName) : undefined;

  // A new sheet, or a new row filter, starts at the top. Without this the old
  // scroll offset would still be in effect while the row list under it is a
  // different length — on a shorter sheet that offset is past the end, and the
  // window would resolve to no rows at all.
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
    setViewport(v => (v.top === 0 ? v : { ...v, top: 0 }));
  }, [tab, rowFilter, exportId]);

  const header: any[] = activeState?.header ?? [];
  const totalRows = activeState?.totalRows ?? 0;
  const marks = activeState?.marks ?? [];
  const cells = activeState?.cells ?? EMPTY_CELLS;
  const absent = activeState?.absent ?? EMPTY_ABSENT;
  const noteAt = activeState?.notes ?? EMPTY_NOTES;

  // Exceptions narrowed to their own sheet, ONCE for the whole export, so
  // neither the active sheet's index nor the dozen tab-bar counts below has to
  // scan all 85k of them to find its own.
  const bySheet = useMemo(() => bucketBySheet(exceptions), [exceptions]);

  const sheetForIdx = useMemo(() => ({
    sheet: activeSheetName ?? "", rows: [header], marks, is_data: activeState?.isData,
  }), [activeSheetName, header, marks, activeState?.isData]);
  const idx = useMemo(
    () => indexSheet(sheetForIdx, bySheet.get(norm(activeSheetName)) ?? [], totalRows),
    [sheetForIdx, bySheet, activeSheetName, totalRows]);
  const marked = useMemo(() => new Set(marks.map(m => `${m[0]}:${m[1]}`)), [marks]);
  const warnedCells = useMemo(
    () => new Set((activeState?.warnMarks ?? []).map(m => `${m[0]}:${m[1]}`)),
    [activeState?.warnMarks]);

  /** Open-decision count per sheet, for the tab bar.
   *
   *  Computing one costs a walk over that sheet's exceptions, and the tab bar
   *  shows a dozen sheets and re-renders on every chunk — so doing it inline
   *  meant repeated full passes while rows were arriving, which locks the page.
   *  The count depends only on a sheet's marks, and those are replaced exactly
   *  once (when that sheet is first opened), so caching against the marks
   *  array's identity recomputes when the answer can actually have changed and
   *  never otherwise. */
  const pendCacheRef = useRef<{ exceptions: StoredException[]; bySheet: Map<string, { marks: any; count: number }> }>(
    { exceptions, bySheet: new Map() });
  if (pendCacheRef.current.exceptions !== exceptions) {
    pendCacheRef.current = { exceptions, bySheet: new Map() };
  }
  const pendingForSheet = (sName: string, st: SheetState): number => {
    const hit = pendCacheRef.current.bySheet.get(sName);
    if (hit && hit.marks === st.marks) return hit.count;
    const sIdx = indexSheet(
      { sheet: sName, rows: [st.header], marks: st.marks, is_data: st.isData } as Sheet,
      bySheet.get(norm(sName)) ?? [], st.totalRows);
    const count = [...sIdx.colFlags.values()].reduce((a, c) => a + c.pending.length, 0);
    pendCacheRef.current.bySheet.set(sName, { marks: st.marks, count });
    return count;
  };

  // Every gi (1-based row) with a flagged cell, whole-sheet — independent of
  // which rows have actually been fetched, since it's derived from `marks` /
  // `idx.byCell` (both whole-sheet, delivered before any row) rather than from
  // row values. That is what lets the Exceptions filter show its true extent
  // immediately instead of growing as rows trickle in.
  const exceptionGis = useMemo(() => {
    const s = new Set<number>();
    for (const k of idx.byCell.keys()) s.add(Number(k.split(":")[0]));
    for (const m of marks) s.add(m[0]);
    return [...s].filter(gi => gi >= 1).sort((a, b) => a - b);
  }, [idx, marks]);

  // The row list is the sheet's FULL extent, not what has been fetched — the
  // scrollbar is truthful from the first paint and the user can scroll straight
  // to row 9,000 without waiting for the 8,999 before it. Rows that aren't in
  // yet render as placeholders and are requested by the effect below.
  //
  // For "all" that list is 1..totalRows, which is not materialised: building a
  // 10k-element array on every render (and re-rendering per chunk) is pure
  // waste when the window only ever needs ~60 of them.
  const rowCount = rowFilter === "exceptions" ? exceptionGis.length : totalRows;
  const giAt = (i: number) => (rowFilter === "exceptions" ? exceptionGis[i] : i + 1);

  // Only the rows near the viewport are put in the DOM. A BDX sheet is wide —
  // 77 columns on a real one — so painting every row means hundreds of
  // thousands of <td>s, and the browser stalls for many seconds no matter how
  // quickly the data arrived. The rows above and below are stood in for by two
  // spacer rows of the right height, so the scrollbar, its position and the
  // total extent all behave exactly as if everything were rendered.
  const rowH = rowHRef.current || EST_ROW_H;
  const viewH = viewport.height || 600;
  const lastIdx = Math.min(rowCount,
    Math.ceil((viewport.top + viewH) / rowH) + ROW_OVERSCAN);
  // Clamped against lastIdx as well as 0: a shorter row list under a scroll
  // offset taken a moment ago would otherwise give a first index past the end,
  // rendering an empty grid rather than the last rows.
  const firstIdx = Math.max(0, Math.min(lastIdx - 1,
    Math.floor(viewport.top / rowH) - ROW_OVERSCAN));
  const windowGis: number[] = [];
  for (let i = firstIdx; i < lastIdx; i++) {
    const gi = giAt(i);
    if (gi != null) windowGis.push(gi);
  }
  const padTop = firstIdx * rowH;
  const padBottom = Math.max(0, (rowCount - lastIdx) * rowH);

  // ── fetching what the viewport needs ───────────────────────────────────────
  // Runs after every render that could change what's on screen — a scroll, a
  // tab switch, a filter change, or the arrival of a chunk. It asks for the
  // rendered window plus a prefetch margin, minus what is already held or known
  // to be empty, and only ever has ONE request open: `loadingRows` gates it,
  // and flipping false when a request settles is what re-runs it for the next
  // window. Nothing here loops on rows the server can't supply, because the
  // `done` line records those in `absent`.
  useEffect(() => {
    if (busy || loadingRows || !activeSheetName || !activeState) return;
    // A tab opened for the first time needs its header and marks before
    // anything else can be decided. In the Exceptions view especially: that
    // row list is DERIVED from the marks, so until they arrive the sheet looks
    // like it has no flagged rows, `rowCount` is 0, and the check below would
    // return early — leaving the tab permanently empty because nothing would
    // ever ask for them.
    if (!metaRef.current.has(activeSheetName)) {
      void loadWindow.current(activeSheetName, { offset: 0, max_rows: PAGE_ROWS });
      return;
    }
    if (rowCount === 0) return;
    const upto = Math.min(rowCount, lastIdx + PREFETCH_ROWS);
    const missing: number[] = [];
    for (let i = firstIdx; i < upto; i++) {
      const gi = giAt(i);
      if (gi != null && !cells.has(gi) && !absent.has(gi)) missing.push(gi);
      if (missing.length >= PAGE_ROWS) break;
    }
    if (!missing.length) return;
    // A contiguous run travels as offset+count; scattered rows (the Exceptions
    // filter picks rows from all over the sheet) have to be listed, and go in
    // smaller batches because they ride in the query string.
    const span = missing[missing.length - 1] - missing[0] + 1;
    void loadWindow.current(activeSheetName,
      span <= PAGE_ROWS
        ? { offset: missing[0] - 1, max_rows: span }
        : { row_gis: missing.slice(0, SCATTERED_ROWS) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, loadingRows, activeSheetName, activeState, rowFilter,
      rowCount, firstIdx, lastIdx, cells, absent]);

  const pendingRows = rowCount > 0 && windowGis.some(gi => !cells.has(gi) && !absent.has(gi));

  // Exceptions on THIS sheet that never resolved to a visible cell (unmapped
  // column, out-of-range row) — scoped to the active sheet since other
  // sheets' marks aren't necessarily loaded yet.
  const unmapped = useMemo(() => {
    if (!activeSheetName) return 0;
    const activeNorm = norm(activeSheetName);
    const relevant = exceptions.filter(e => norm(e.source_sheet) === activeNorm).length;
    return Math.max(0, relevant - idx.mapped.size);
  }, [exceptions, idx, activeSheetName]);

  const pendingCells = useMemo(() => {
    let n = 0;
    for (const excs of idx.byCell.values()) if (excs.some(e => !decisionKindOf(e))) n++;
    return n;
  }, [idx]);
  const decidedCells = useMemo(() => {
    let n = 0;
    for (const excs of idx.byCell.values()) if (excs.every(e => !!decisionKindOf(e))) n++;
    return n;
  }, [idx]);

  async function saveBatch(items: Array<{ e: StoredException; kind: "approve" | "fix" | "dismiss"; value?: string | null; reason?: string | null }>) {
    if (items.length === 0) return;
    setSaving(true); setSaveErr(null);
    try {
      const res = await saveExportDecisions(
        exportId,
        items.map(({ e, kind, value, reason }) => ({
          rule_id: e.rule_id ?? null,
          policy_number: e.policy_number ?? null,
          field: e.field_path ?? null,
          kind,
          value: value ?? null,
          reason: reason ?? null,
          actual_value: e.actual_value ?? null,
          sheet: e.source_sheet ?? null,
          row: e.source_row ?? null,
        })),
        getUser()?.id,
      );
      const skipped = res.skipped ?? [];
      if (skipped.length) {
        setSaveErr(`${skipped.length} of ${items.length} could not be applied — ` +
          skipped.map(sk => `${sk.field ?? "field"}: ${sk.reason}`).join("; "));
      } else {
        setCellPop(null); setColPop(null);
      }
      onSaved(items.length - skipped.length);
    } catch (err: any) {
      setSaveErr(err?.response?.data?.detail ?? err?.message ?? "Failed to save.");
    } finally {
      setSaving(false);
    }
  }

  const onDecide = (e: StoredException, kind: "approve" | "fix" | "dismiss", value?: string | null) =>
    void saveBatch([{ e, kind, value }]);

  function approveColumn(ci: number) {
    const cf = idx?.colFlags.get(ci);
    if (!cf) return;
    // Only the rules (clauses) the user ticked in the popover.
    const items = groupColPending(cf.pending)
      .filter(gr => colSel.has(gr.key))
      .flatMap(gr => gr.approvable)
      .map(e => ({
        e, kind: "approve" as const, value: writeValue(e, { kind: "approve" } as Decision),
      }));
    void saveBatch(items);
  }

  const closePops = () => { setCellPop(null); setColPop(null); setSaveErr(null); };

  // Frozen header row / first column — same technique as the modal grid (opaque
  // background + inset box-shadow divider, since collapsed borders scroll away).
  const EDGE_B = "inset 0 -1px 0 var(--p-border)";
  const EDGE_R = "inset -1px 0 0 var(--p-border)";
  const stickyHead: React.CSSProperties =
    { position: "sticky", top: 0, zIndex: 2, background: "var(--p-surface-2)", boxShadow: EDGE_B };
  const stickyCol: React.CSSProperties =
    { position: "sticky", left: 0, zIndex: 1, background: "var(--p-surface)", boxShadow: EDGE_R };

  const popExcs = cellPop ? (idx.byCell.get(cellPop.key) ?? []) : [];
  const popCol = colPop ? (idx.colFlags.get(colPop.ci) ?? null) : null;
  const popColGroups = useMemo(
    () => (popCol ? groupColPending(popCol.pending) : []), [popCol]);
  const popColSelected = popColGroups.filter(gr => colSel.has(gr.key));
  const popColCount = popColSelected.reduce((a, gr) => a + gr.approvable.length, 0);
  const popColSkipped = popColSelected.reduce((a, gr) => a + (gr.pending.length - gr.approvable.length), 0);

  return (
    <div className="card" style={{ marginBottom: 18 }} ref={rootRef}>
      <div className="card-h">
        <h3>BDX Review</h3>
        <span className="sub">
          {name ?? "Output"} — click a highlighted cell to see its error and fix it right here.
        </span>
        
        <div className="right" style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {!busy && activeSheetName && totalRows > 0 && (
          <div style={{ display: "flex" }}>
            <div className="seg" role="tablist" style={{ display: "inline-flex", border: "1px solid var(--p-border)", borderRadius: 8, overflow: "hidden" }}>
              {([["exceptions", `Exceptions (${exceptionGis.length})`], ["all", `All rows (${totalRows})`]] as const).map(([val, label]) => (
                <button key={val} onClick={() => { setRowFilter(val); closePops(); }}
                  className="btn sm" style={{
                    borderRadius: 0, border: 0,
                    background: rowFilter === val ? "var(--p-primary)" : "transparent",
                    color: rowFilter === val ? "#fff" : "var(--p-muted)",
                  }}>
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
          <span className="muted" style={{ fontSize: 12 }}>
            {pendingCells} pending · {decidedCells} resolved
          </span>
          <button className="btn sm" onClick={onClose}><X size={13} /> Hide</button>
        </div>
      </div>

      {!busy && sheetOrder && sheetOrder.length > 1 && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", padding: "10px 20px", borderBottom: "1px solid var(--p-border)" }}>
          {sheetOrder.map((sName, i) => {
            const st = sheetStates.get(sName);
            const spec = st?.isData === false;
            // Pending count for a not-yet-visited sheet reads 0 until its
            // marks load (see `mergeSheetPages`) — it self-corrects once that
            // tab is opened.
            const pend = st ? pendingForSheet(sName, st) : 0;
            return (
              <button key={sName} className={`btn sm ${i === tab ? "pri" : ""}`} onClick={() => { setTab(i); closePops(); }}
                style={spec && i !== tab ? { opacity: 0.65 } : undefined}
                title={spec ? "Template spec / instructions — not policy data" : undefined}>
                {sName} ({st?.totalRows ?? 0})
                {pend > 0 ? ` · ${pend} open` : ""}{spec ? " · info" : ""}
              </button>
            );
          })}
        </div>
      )}

      <div style={{ padding: 16 }}>
        
        {streamErr ? (
          <div className="note warn">{streamErr}</div>
        ) : busy ? (
          <div className="note">Loading the generated output…</div>
        ) : !activeSheetName || totalRows === 0 ? (
          <div className="note">No rows in this sheet.</div>
        ) : rowFilter === "exceptions" && exceptionGis.length === 0 ? (
          <div className="note">No exceptions on this sheet — switch to “All rows” to see the full output.</div>
        ) : (
          <div className="tbl-wrap tbl-scroll-x" style={{ maxHeight: "64vh" }}
            ref={el => {
              scrollRef.current = el;
              // The height is needed for the very first window, before any
              // scrolling has happened to report it.
              if (el && !viewport.height) {
                setViewport(v => (v.height ? v : { ...v, height: el.clientHeight }));
              }
            }}
            onScroll={e => {
              const el = e.currentTarget;
              setViewport(v =>
                v.top === el.scrollTop && v.height === el.clientHeight
                  ? v : { top: el.scrollTop, height: el.clientHeight });
            }}>
            <table>
              <thead>
                <tr>
                  {header.map((h, ci) => {
                    const cf = idx.colFlags.get(ci);
                    const pend = cf?.pending.length ?? 0;
                    return (
                      <th key={ci} style={{
                        ...stickyHead,
                        ...(ci === 0 ? { left: 0, zIndex: 3, boxShadow: `${EDGE_B}, ${EDGE_R}` } : {}),
                        ...(pend > 0 ? { background: "#FCE9EC" } : {}),
                      }}>
                        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
                          <span>{String(h ?? "").trim() || `Column ${ci + 1}`}</span>
                          {pend > 0 && (
                            <button
                              onClick={ev => {
                                ev.stopPropagation();
                                setSaveErr(null); setCellPop(null);
                                // Pre-tick every clause that has something approvable;
                                // the popover lets the user untick clauses they don't want.
                                const groups = groupColPending(cf?.pending ?? []);
                                setColSel(new Set(groups.filter(gr => gr.approvable.length > 0).map(gr => gr.key)));
                                setColPop({ ci, pos: place(ev.currentTarget.getBoundingClientRect(), 340, 300) });
                              }}
                              title={`Approve all ${pend} flagged ${pend === 1 ? "row" : "rows"} in this column`}
                              style={{
                                display: "inline-flex", alignItems: "center", gap: 3,
                                fontSize: 10, fontWeight: 700, textTransform: "none", letterSpacing: 0,
                                color: "#047857", background: "#ECFDF5", border: "1px solid #A7F3D0",
                                borderRadius: 999, padding: "1px 7px", cursor: "pointer",
                              }}>
                              <Check size={10} /> Approve all · {pend}
                            </button>
                          )}
                        </div>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {padTop > 0 && (
                  <tr aria-hidden="true" style={{ height: padTop }}>
                    <td colSpan={header.length} style={{ padding: 0, border: 0 }} />
                  </tr>
                )}
                {windowGis.map((gi, wi) => {
                  const row = cells.get(gi);
                  // Rows the viewport has reached but the fetch hasn't. Drawn at
                  // full height so the scroll position stays put when the values
                  // land, and kept deliberately cheap — scrolling fast passes
                  // over a lot of these.
                  if (!row) {
                    return (
                      <tr key={gi}
                        ref={wi === 0 ? (el => {
                          const h = el?.getBoundingClientRect().height;
                          if (h && !rowHRef.current) rowHRef.current = h;
                        }) : undefined}>
                        {header.map((_, ci) => (
                          <td key={ci} style={ci === 0 ? stickyCol : undefined}>
                            <span style={{
                              display: "inline-block", height: 8, width: ci === 0 ? 64 : "70%",
                              borderRadius: 3, background: "var(--p-border)", opacity: 0.5,
                            }} />
                          </td>
                        ))}
                      </tr>
                    );
                  }
                  return (
                    <tr key={gi}
                      ref={wi === 0 ? (el => {
                        // One measurement is enough: every body row is the same
                        // height, and re-measuring mid-scroll would move the
                        // spacers under the user.
                        const h = el?.getBoundingClientRect().height;
                        if (h && !rowHRef.current) rowHRef.current = h;
                      }) : undefined}>
                      {header.map((_, ci) => {
                        const c = row[ci];
                        const key = `${gi}:${ci}`;
                        const excs = idx.byCell.get(key);
                        const pending = excs?.some(e => !decisionKindOf(e)) ?? false;
                        const doneKind = excs && !pending ? decisionKindOf(excs[0]) : null;
                        const newVal = doneKind === "approve" || doneKind === "fix"
                          ? decidedValue(excs![0]) : null;
                        const plainMark = !excs && marked.has(key);
                        const clickable = !!excs;
                        // Warning tint only when nothing critical sits on the
                        // cell — same precedence the workbook's fill uses. A
                        // flagged cell with no exception record falls back to
                        // the colour the file itself was painted.
                        const warnCell = excs?.length
                          ? excs.every(e => isWarnSeverity(e.severity))
                          : warnedCells.has(key);
                        const style: React.CSSProperties = {
                          ...(ci === 0 ? stickyCol : {}),
                          ...(pending || plainMark
                            ? flagStyle(warnCell)
                            : doneKind
                              ? { background: DONE_BG[doneKind], color: DONE_FG[doneKind], fontWeight: 600 }
                              : {}),
                          // Every state carries a matching outline, not just the
                          // pending ones — fill + border together read as one
                          // deliberate state instead of a wash of colour.
                          ...(clickable ? { cursor: "pointer", boxShadow: `inset 0 0 0 1px ${
                            pending ? (warnCell ? HL_WARN_BD : HL_BD)
                              : doneKind ? DONE_BD[doneKind] : "transparent"}` } : {}),
                        };
                        return (
                          <td key={ci} className={ci === 0 ? "mono" : ""} style={style}
                            title={excs
                              ? (pending
                                  ? (excs[0].error_message ?? "Failed validation — click to review")
                                  : `${DONE_LABEL[doneKind!]} — click to change`)
                              : plainMark ? (noteAt.get(key) || "Failed validation") : undefined}
                            onClick={clickable ? ev => {
                              setSaveErr(null); setColPop(null);
                              setCellPop({ key, pos: place((ev.currentTarget as HTMLElement).getBoundingClientRect()) });
                            } : undefined}>
                            {newVal != null && newVal !== String(c ?? "")
                              ? <>
                                  <span>{newVal}</span>{" "}
                                  <span style={{ textDecoration: "line-through", opacity: 0.55, fontWeight: 400, fontSize: "11px" }}>
                                    {String(c ?? "")}
                                  </span>
                                </>
                              : String(c ?? "")}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
                {padBottom > 0 && (
                  <tr aria-hidden="true" style={{ height: padBottom }}>
                    <td colSpan={header.length} style={{ padding: 0, border: 0 }} />
                  </tr>
                )}
                {(loadingRows || pendingRows) && (
                  <tr aria-live="polite">
                    <td colSpan={header.length} style={{ textAlign: "center", padding: "12px" }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 8, color: "var(--p-muted)", fontSize: 12 }}>
                        <span aria-hidden="true" style={{
                          width: 13, height: 13, borderRadius: "50%",
                          border: "2px solid var(--p-border)", borderTopColor: "var(--p-primary)",
                          animation: "k-spin 0.8s linear infinite",
                        }} />
                        Loading rows {windowGis[0] ?? 1}–{windowGis[windowGis.length - 1] ?? 1} of {rowCount}…
                      </span>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* legend / footer */}
      {!busy && activeSheetName && (
        <div style={{
          display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap",
          padding: "10px 20px", borderTop: "1px solid var(--p-border)",
          fontSize: 12, color: "var(--p-muted)",
        }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: HL_BG, border: `1px solid ${HL_BD}` }} />
            Needs decision · Critical
          </span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: HL_WARN_BG, border: `1px solid ${HL_WARN_BD}` }} />
            Needs decision · Warning
          </span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: DONE_BG.approve, border: `1px solid ${DONE_BD.approve}` }} />
            Approved
          </span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: DONE_BG.fix, border: `1px solid ${DONE_BD.fix}` }} />
            Fixed
          </span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: DONE_BG.dismiss, border: `1px solid ${DONE_BD.dismiss}` }} />
            Dismissed
          </span>
          <span style={{ marginLeft: "auto" }}>
            Approved &amp; fixed values are written into the output when you click <strong>Fix &amp; Validate</strong>.
            {unmapped > 0 && <> · {unmapped} exception{unmapped === 1 ? "" : "s"} not tied to a cell — use the rule cards below.</>}
          </span>
        </div>
      )}

      {/* backdrop + popovers */}
      {(cellPop || colPop) && <div className="fixed inset-0 z-30" onClick={closePops} />}

      {cellPop && popExcs.length > 0 && (
        <div className="fixed z-40 w-[360px] rounded-lg border border-border bg-white shadow-xl p-3 overflow-y-auto"
          style={{ left: cellPop.pos.left, top: cellPop.pos.top, bottom: cellPop.pos.bottom,
                   maxHeight: cellPop.pos.maxH }}>
          {popExcs.map((e, i) => (
            <div key={e.exception_id} className={i > 0 ? "mt-3 pt-3 border-t border-border" : ""}>
              <ExcDetail e={e} saving={saving} onDecide={onDecide} />
            </div>
          ))}
          {saveErr && <p className="text-[11px] text-danger mt-2">{saveErr}</p>}
          <p className="text-[10px] text-ink-soft mt-2">
            Saved immediately — the corrected value reaches the output on Fix &amp; Validate.
          </p>
        </div>
      )}

      {colPop && popCol && (
        <div className="fixed z-40 w-[340px] rounded-lg border border-border bg-white shadow-xl p-3 text-xs overflow-y-auto"
          style={{ left: colPop.pos.left, top: colPop.pos.top, bottom: colPop.pos.bottom,
                   maxHeight: colPop.pos.maxH }}>
          <div className="font-semibold text-emerald-700 mb-1 flex items-center gap-1.5">
            <Check size={13} /> Approve all — {String(header[colPop.ci] ?? "")}
          </div>

          {popColGroups.some(gr => gr.approvable.length > 0) ? (
            <>
              <p className="text-ink-muted mb-1.5">
                {popColGroups.length === 1
                  ? "Approve the flagged rows with their recommended value:"
                  : `This column is checked by ${popColGroups.length} clauses — tick which to approve:`}
              </p>
              {/* one row per rule/clause, so approving one clause never silently
                  approves another clause's errors on the same column */}
              <div className="max-h-52 overflow-y-auto space-y-1">
                {popColGroups.map(gr => {
                  const canApprove = gr.approvable.length > 0;
                  return (
                    <label key={gr.key}
                      className={`flex items-start gap-2 px-1.5 py-1.5 rounded border border-border ${
                        canApprove ? "hover:bg-surface-2 cursor-pointer" : "opacity-70 cursor-not-allowed"}`}>
                      <input type="checkbox" className="h-3.5 w-3.5 mt-0.5" disabled={!canApprove}
                        checked={canApprove && colSel.has(gr.key)}
                        onChange={() => setColSel(prev => {
                          const n = new Set(prev);
                          n.has(gr.key) ? n.delete(gr.key) : n.add(gr.key);
                          return n;
                        })} />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5 flex-wrap">
                          <span className={`badge ${SEV_BADGE[gr.severity] ?? "b-info"}`}>
                            <span className="d" />{SEV_LABEL[gr.severity] ?? gr.severity}
                          </span>
                          <span className="font-semibold">{gr.ruleName}</span>
                          {gr.ruleId != null && (
                            <span className="text-[10px] font-mono text-ink-soft">RULE-{gr.ruleId}</span>
                          )}
                          <span className="ml-auto text-ink-soft shrink-0">
                            {gr.approvable.length}/{gr.pending.length} row{gr.pending.length === 1 ? "" : "s"}
                          </span>
                        </span>
                        {canApprove ? (
                          <span className="block text-[11px] mt-0.5">
                            → <span className="font-mono text-emerald-700">
                              {gr.value ?? "each row's own recommended value"}
                            </span>
                          </span>
                        ) : (
                          <span className="block text-[11px] text-amber-600 mt-0.5">
                            No single recommended value — Fix these cells individually.
                          </span>
                        )}
                        {(gr.requirement || gr.clause) && (
                          <span className="block text-[10px] text-ink-soft mt-0.5 truncate"
                            title={gr.clause ?? gr.requirement ?? undefined}>
                            {gr.requirement ?? `"${gr.clause}"`}
                          </span>
                        )}
                      </span>
                    </label>
                  );
                })}
              </div>
              {popColSkipped > 0 && (
                <p className="text-[11px] text-amber-600 mt-1.5">
                  {popColSkipped} of the ticked rows {popColSkipped === 1 ? "has" : "have"} no single
                  recommended value and will be skipped.
                </p>
              )}
            </>
          ) : (
            <p className="text-ink-muted">
              None of the flagged rows in this column has a single recommended value to
              approve — click each cell and use <b>Fix</b> instead.
            </p>
          )}

          {saveErr && <p className="text-[11px] text-danger mt-2">{saveErr}</p>}
          <div className="flex justify-end gap-2 mt-3">
            <button className="text-xs px-2 py-1 rounded-md hover:bg-surface-2 text-ink-muted"
              onClick={closePops}>Cancel</button>
            {popColGroups.some(gr => gr.approvable.length > 0) && (
              <Button onClick={() => approveColumn(colPop.ci)} disabled={saving || popColCount === 0}>
                Approve {popColCount}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
