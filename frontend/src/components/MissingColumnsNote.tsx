import { ReactNode, useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, ArrowRight, CheckCircle2, ChevronDown, ChevronRight,
  FileWarning, Info, Loader2, Quote, RefreshCw,
} from "lucide-react";
import { api } from "../api/client";
import { fmtStamp } from "../utils/date";
import { InfoTip } from "./InfoTip";
import { isLibraryText } from "./DirectMappingWidgets";
import { MissingColumn, MissingColumnsResp, UnmappedClause, clauseHeading } from "../utils/directSetup";

// The contract-vs-bordereau gap NOTE, shared by the setup's read-only page
// (where it sits between Setup Overview and Contracts) and the build-completion
// modal — so both show the same saved finding in the same words.
//
// Reading is free: the server stores the check's result. A model call is spent
// only at the end of a build, or the first time a setup that has never been
// checked is opened — there is no manual re-run control.

const SEV_PILL: Record<string, string> = {
  required: "bg-red-50 text-red-700 border border-red-100",
  recommended: "bg-amber-50 text-amber-700 border border-amber-100",
};
const SEV_LABEL: Record<string, string> = {
  required: "Required", recommended: "Recommended",
};
const sevPill = (s: string) => SEV_PILL[s] ?? "bg-gray-100 text-gray-700 border border-gray-200";
const sevLabel = (s: string) => SEV_LABEL[s] ?? s;

/** "Show the contract's words" — the toggle above a proof block.
 *
 *  The quote is the LONGEST thing in a row and the least often read: the name,
 *  the severity and the reason are what a reviewer scans, and the verbatim
 *  clause is what they open when one of those findings surprises them. Shown
 *  always, three or four quoted paragraphs buried the list of findings itself.
 *  Shut by default, a row is one finding again and the proof is one click away.
 */
function ProofToggle({ open, onClick, tone }: {
  open: boolean; onClick: () => void; tone: "amber" | "sky";
}) {
  const colour = tone === "amber"
    ? "text-amber-700 hover:bg-amber-100/70 border-amber-200"
    : "text-sky-700 hover:bg-sky-100/70 border-sky-200";
  return (
    <button type="button" onClick={onClick} aria-expanded={open}
      className={`mt-1.5 inline-flex items-center gap-1 rounded-md border bg-white/70
                  px-2 py-[3px] text-[11px] font-medium transition-colors ${colour}`}>
      {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
      <Quote size={10} />
      {open ? "Hide contract wording" : "Show contract wording"}
    </button>
  );
}

/** One missing column. Everything below the name is optional — a finding with
 *  only a name still renders cleanly. */
function MissingColumnRow({ item }: { item: MissingColumn }) {
  // Per ROW, not per list: opening one finding's proof must not open twenty.
  const [showProof, setShowProof] = useState(false);
  return (
    <li className="px-3.5 py-3 first:pt-3 hover:bg-amber-50/40 transition-colors">
      <div className="flex items-start gap-2 flex-wrap">
        <span className="font-semibold text-sm text-ink break-words">{item.column_name}</span>
        <span className={`pill shrink-0 ${sevPill(item.severity)}`}>{sevLabel(item.severity)}</span>
        {item.sheet_key && (
          <span className="pill pill-grey shrink-0 font-mono !text-[10px]">{item.sheet_key}</span>
        )}
      </div>
      {item.reason && (
        <p className="mt-1 text-[12.5px] leading-snug text-ink-muted">{item.reason}</p>
      )}
      {item.related_output_field && (
        <p className="mt-1.5 flex items-center gap-1 text-[11px] text-ink-soft">
          <ArrowRight size={11} className="shrink-0" />
          Feeds output column <span className="font-medium text-ink-muted">{item.related_output_field}</span>
        </p>
      )}
      {item.contract_reference && (
        <ProofToggle open={showProof} tone="amber"
          onClick={() => setShowProof(v => !v)} />
      )}
      {item.contract_reference && showProof && (
        // The contract's OWN words — quoted verbatim by the check, never
        // paraphrased — plus where to find them, so a reviewer can go straight
        // to the clause instead of taking the finding on trust.
        <div className="mt-1.5 rounded-md bg-white/70 border border-amber-100 px-2.5 py-1.5">
          <div className="flex items-start gap-1.5">
            <Quote size={11} className="mt-1 shrink-0 text-amber-500" />
            <p className="text-[11.5px] italic leading-snug text-ink-muted">
              {item.contract_reference}
            </p>
          </div>
          {(item.clause_label || item.source_page != null) && (
            <p className="mt-1 pl-[18px] text-[10.5px] font-medium text-amber-700/90">
              {item.clause_label}
              {item.clause_label && item.source_page != null && " · "}
              {item.source_page != null && `Page ${item.source_page}`}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

/** The scrollable list itself. A contract can name many gaps, so the list is
 *  capped in height and scrolls INSIDE its own box — the page (or the modal)
 *  never grows unusably long, and the count above it stays visible. */
export function MissingColumnsList({ items, maxHeight = "22rem" }: {
  items: MissingColumn[]; maxHeight?: string;
}) {
  if (items.length === 0) return null;
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50/50 overflow-hidden">
      <ul className="divide-y divide-amber-200/70 overflow-y-auto overscroll-contain"
        style={{ maxHeight }}>
        {items.map(it => <MissingColumnRow key={it.id} item={it} />)}
      </ul>
    </div>
  );
}

/** One rule-bearing clause with no output column yet. Unlike a MissingColumn
 *  this is NOT a model finding — the extraction decided the clause deserves a
 *  rule and recorded why no column fitted, so `reason` is its own words. */
function UnmappedClauseRow({ item, n }: { item: UnmappedClause; n: number }) {
  const [showProof, setShowProof] = useState(false);
  return (
    <li className="grid grid-cols-[22px_minmax(0,1fr)] items-start gap-3 px-3.5 py-3 hover:bg-surface-2/50 transition-colors">
      <span className="mt-0.5 grid h-[22px] w-[22px] place-items-center rounded-full bg-amber-50 text-[11px] font-bold text-amber-700">
        {n}
      </span>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[13.5px] font-semibold text-ink">
          <span className="break-words">{clauseHeading(item)}</span>
          {/* Why no column fitted — behind the ⓘ, as on the setup's own page. */}
          {item.reason && (
            <span className="tip-inline inline-flex">
              <InfoTip text={`Why it has no column: ${item.reason}`} />
            </span>
          )}
          <span className="ml-1 inline-flex rounded-full bg-amber-50 px-2 py-0.5 text-[10.5px] font-semibold text-amber-700 ring-1 ring-inset ring-amber-200">Unmapped Clause</span>
          {item.source_page != null && (
            <span className="text-[11px] font-normal text-ink-soft">page {item.source_page}</span>
          )}
        </div>
        {item.clause_text && (
          <ProofToggle open={showProof} tone="sky"
            onClick={() => setShowProof(v => !v)} />
        )}
        {item.clause_text && showProof && (
          <div className="mt-1.5 rounded-md bg-surface-2/60 border border-border px-2.5 py-1.5">
            <div className="flex items-start gap-1.5">
              <Quote size={11} className="mt-1 shrink-0 text-ink-soft" />
              <p className="text-[11.5px] italic leading-snug text-ink-muted">
                {item.clause_text}
              </p>
            </div>
          </div>
        )}
      </div>
    </li>
  );
}

/** The clause list. Same scroll-inside-its-own-box rule as MissingColumnsList —
 *  a contract can leave dozens of clauses awaiting a column. */
export function UnmappedClausesList({ items, maxHeight = "22rem" }: {
  items: UnmappedClause[]; maxHeight?: string;
}) {
  if (items.length === 0) return null;
  return (
    <div className="rounded-lg border border-border bg-white overflow-hidden">
      <ul className="divide-y divide-border overflow-y-auto overscroll-contain"
        style={{ maxHeight }}>
        {items.map((it, i) => (
          <UnmappedClauseRow key={`${it.clause_id ?? "x"}-${i}`} item={it} n={i + 1} />
        ))}
      </ul>
    </div>
  );
}

/** Counts, as pills — the headline a reviewer reads before the list. */
function CountPills({ counts }: { counts: MissingColumnsResp["counts"] }) {
  return (
    <div className="flex items-center gap-1.5">
      {counts.required > 0 && (
        <span className="pill pill-red">{counts.required} Required</span>
      )}
      {counts.recommended > 0 && (
        <span className="pill pill-amber">{counts.recommended} Recommended</span>
      )}
    </div>
  );
}

/**
 * The NOTE as it appears on a saved setup: header + counts + scrollable list,
 * or a single quiet line when there is nothing to report.
 *
 * Self-loading. `autoCheck` lets a setup that has never been checked (one built
 * before this existed) run the check once on first view — after that the stored
 * result is reused and no model call is made.
 */
export function MissingColumnsNote({ pipelineId, autoCheck = true, refreshKey = 0,
                                     defaultCollapsed = false,
                                     className = "" }: {
  pipelineId: number | string; autoCheck?: boolean;
  /** Bump to re-read the note. The server drops findings whose output field is
   *  already mapped, so setting a field on a rule makes its entry disappear —
   *  this is how the page says "I just changed a mapping, look again". Re-reading
   *  is free: it never spends a model call on an already-analyzed setup. */
  refreshKey?: number;
  /** Start shut. For a page that is READ — a saved setup shown alongside its
   *  contracts and its sheets, both of which start shut — the header already
   *  carries the whole finding: how many clauses, how many columns, and how
   *  many of those are required. The lists are what you open when you have
   *  decided to act on it. Left false, the note opens as it always has, which
   *  is what a page you are still EDITING wants. */
  defaultCollapsed?: boolean;
  className?: string;
}) {
  const [data, setData] = useState<MissingColumnsResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(!defaultCollapsed);

  const runCheck = useCallback(async (force: boolean) => {
    setChecking(true); setFailed(false);
    try {
      const { data: d } = await api.post<MissingColumnsResp>(
        `/pipelines/${pipelineId}/missing-columns/analyze`, null, { params: { force } });
      setData(d);
    } catch { setFailed(true); } finally { setChecking(false); }
  }, [pipelineId]);

  // Blank out and show the full spinner only when the SETUP changes. A
  // refreshKey bump re-reads in place, so the note updates after a mapping
  // change instead of collapsing to a loading line and back.
  useEffect(() => {
    setLoading(true); setData(null); setFailed(false);
  }, [pipelineId]);

  useEffect(() => {
    let alive = true;
    api.get<MissingColumnsResp>(`/pipelines/${pipelineId}/missing-columns`)
      .then(r => {
        if (!alive) return;
        setData(r.data);
        // Never checked → check once now, so an older setup gets its NOTE on
        // the first visit instead of staying silently unverified. `analyzed`
        // reflects the stored snapshot, NOT the filtered list — a setup whose
        // every finding is now mapped must not look unchecked and re-run.
        if (autoCheck && !r.data.analyzed) runCheck(false);
      })
      .catch(() => { if (alive) setFailed(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [pipelineId, autoCheck, runCheck, refreshKey]);

  if (loading || checking) {
    return (
      <div className={`flex items-center gap-2 rounded-md bg-surface-2 px-4 py-2.5 text-sm text-ink-muted ${className}`}>
        <Loader2 size={15} className="animate-spin shrink-0" />
        {checking ? "Checking this bordereau against the contract…" : "Loading contract check…"}
      </div>
    );
  }

  // A read that failed outright tells us nothing about either half.
  if (failed || !data) {
    return (
      <div className={`flex flex-wrap items-center gap-2 rounded-md bg-surface-2 px-4 py-2.5 text-sm text-ink-muted ${className}`}>
        <Info size={15} className="shrink-0" />
        <span>The contract check couldn't be loaded.</span>
        <button type="button" onClick={() => runCheck(true)}
          className="linkish ml-auto inline-flex items-center gap-1">
          <RefreshCw size={12} /> Check Now
        </button>
      </div>
    );
  }

  // The two halves are independent. Clauses are DERIVED (always current, no
  // model), findings are the model's stored answer — so an unchecked setup can
  // still have a full clause list, and must still say the other half is unknown.
  const clauses = data.unmapped_clauses ?? [];
  const items = data.items;
  const c = clauses.length;
  const n = items.length;

  // Nothing in either half, and the model half really did run — the one case
  // that can honestly claim "complete".
  if (c === 0 && n === 0 && data.analyzed) {
    return (
      <div className={`flex flex-wrap items-center gap-2 rounded-md bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700 ${className}`}>
        <CheckCircle2 size={15} className="shrink-0" />
        <span>
          Every contract clause is mapped to a column, and your bordereau provides
          everything the contract asks for.
          {data.analyzed_at && (
            <span className="text-emerald-700/70"> Checked {fmtStamp(data.analyzed_at)}.</span>
          )}
        </span>
      </div>
    );
  }

  // Nothing derived to show and the model half never ran — say so plainly rather
  // than implying the setup is clean.
  if (c === 0 && !data.analyzed) {
    const why = data.skipped_reason;
    return (
      <div className={`flex flex-wrap items-center gap-2 rounded-md bg-surface-2 px-4 py-2.5 text-sm text-ink-muted ${className}`}>
        <Info size={15} className="shrink-0" />
        <span>
          {why
            ? `This setup hasn't been checked for missing bordereau columns — ${why}.`
            : "This setup hasn't been checked for missing bordereau columns yet."}
        </span>
        <button type="button" onClick={() => runCheck(true)}
          className="linkish ml-auto inline-flex items-center gap-1">
          <RefreshCw size={12} /> Check Now
        </button>
      </div>
    );
  }

  return (
    <section className={`rounded-lg border border-amber-200 bg-white shadow-card overflow-hidden ${className}`}>
      {/* The whole header is the toggle — the same shape the contracts and the
          sheets on this page use, so three sections that shut do not shut in
          three different ways. It stays a full-width button even when the note
          opens by default: a section you can close is better than one you
          cannot, and nothing about it is hidden while it is open. */}
      <button type="button" onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className={`w-full text-left flex flex-wrap items-center gap-x-3 gap-y-2
                    px-5 py-3.5 bg-amber-50 transition hover:bg-amber-100/70
                    ${open ? "border-b border-amber-200" : ""}`}>
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-amber-100 text-amber-700">
          <FileWarning size={17} />
        </span>
        <div className="min-w-0">
          <h2 className="flex items-center gap-1.5 text-base font-semibold text-amber-900">
            {open
              ? <ChevronDown size={15} className="shrink-0 text-amber-700" />
              : <ChevronRight size={15} className="shrink-0 text-amber-700" />}
            Note ·{" "}
            {c > 0 && <>{c} Clause{c === 1 ? "" : "s"} Awaiting a Column</>}
            {c > 0 && n > 0 && " · "}
            {n > 0 && <>{n} Column{n === 1 ? "" : "s"} May Be Missing</>}
          </h2>
          <p className="text-[11.5px] text-amber-800/80">
            {c > 0 && <>
              {c === 1 ? "This clause needs" : "These clauses need"} a rule, but no
              output column fitted {c === 1 ? "it" : "them"} — pick one on the contract below.
            </>}
            {c > 0 && n > 0 && " "}
            {n > 0 && <>
              Separately, the contract asks for {n === 1 ? "a data point" : "data points"}{" "}
              your bordereau file doesn't carry as a column.
            </>}
          </p>
        </div>
        {n > 0 && <div className="ml-auto"><CountPills counts={data.counts} /></div>}
      </button>
      {/* Hidden rather than unmounted, unlike the sheets above it: both lists
          scroll INSIDE their own capped box, and unmounting would send someone
          who shut the note to compare it with the contract back to the top of a
          list they had scrolled halfway down. */}
      <div className={`p-3 space-y-4${open ? "" : " hidden"}`}>
        {c > 0 && (
          <div>
            <h3 className="mb-1.5 px-1 text-xs font-semibold text-ink-muted">
              Rule-bearing clauses with no column yet ({c})
            </h3>
            <UnmappedClausesList items={clauses} />
            <p className="mt-2.5 flex items-start gap-1.5 px-1 text-[11px] text-ink-soft">
              <ArrowRight size={11} className="mt-0.5 shrink-0" />
              Open the contract below and choose a column for each — that generates its
              rule and removes it from this list. No re-check needed.
            </p>
          </div>
        )}
        {n > 0 && (
          <div>
            {/* Deliberately about the BORDEREAU, not about rules: the check only
                compares contract prose against the BDX columns, so an entry here
                may still have a rule behind it (e.g. a name covered by an
                underwriter rule). Claiming "no rule covers this" would overstate
                what was actually verified. */}
            <h3 className="mb-1.5 px-1 text-xs font-semibold text-ink-muted">
              Asked for by the contract, not a column in your bordereau ({n})
            </h3>
            <MissingColumnsList items={items} />
            <p className="mt-2.5 flex items-start gap-1.5 px-1 text-[11px] text-ink-soft">
              <AlertTriangle size={11} className="mt-0.5 shrink-0" />
              Add these columns to the bordereau file, then rebuild this setup. Until then
              they stay blank in the output and any contract rule that needs them can't run.
            </p>
          </div>
        )}
        {c > 0 && !data.analyzed && (
          <p className="flex flex-wrap items-center gap-2 px-1 text-[11px] text-ink-soft">
            <Info size={11} className="shrink-0" />
            The bordereau-column check hasn't run for this setup, so the second list
            above is unknown rather than empty.
            <button type="button" onClick={() => runCheck(true)}
              className="linkish inline-flex items-center gap-1">
              <RefreshCw size={11} /> Check Now
            </button>
          </p>
        )}
      </div>
    </section>
  );
}

/** The stored contract-vs-bordereau check for one setup, loaded once by the page
 *  so its parts can be shown in different places (a Contract Rules sub-tab, its
 *  count on a tile and on the Setup Summary) without fetching twice. Same
 *  behaviour as MissingColumnsNote: reading is free, and a setup that has never
 *  been checked runs the check once. */
export type MissingColumnsState = {
  data: MissingColumnsResp | null; loading: boolean; checking: boolean; failed: boolean;
  runCheck: (force: boolean) => Promise<void>;
};
export function useMissingColumns(pipelineId: number | string | null | undefined,
                                  { autoCheck = true, refreshKey = 0 } = {}): MissingColumnsState {
  const [data, setData] = useState<MissingColumnsResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState(false);

  const runCheck = useCallback(async (force: boolean) => {
    if (pipelineId == null) return;
    setChecking(true); setFailed(false);
    try {
      const { data: d } = await api.post<MissingColumnsResp>(
        `/pipelines/${pipelineId}/missing-columns/analyze`, null, { params: { force } });
      setData(d);
    } catch { setFailed(true); } finally { setChecking(false); }
  }, [pipelineId]);

  // Blank out only when the SETUP changes; a refreshKey bump re-reads in place.
  useEffect(() => {
    setLoading(true); setData(null); setFailed(false);
  }, [pipelineId]);

  useEffect(() => {
    if (pipelineId == null) return;
    let alive = true;
    api.get<MissingColumnsResp>(`/pipelines/${pipelineId}/missing-columns`)
      .then(r => {
        if (!alive) return;
        setData(r.data);
        if (autoCheck && !r.data.analyzed) runCheck(false);
      })
      .catch(() => { if (alive) setFailed(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [pipelineId, autoCheck, runCheck, refreshKey]);

  return { data, loading, checking, failed, runCheck };
}

/** Only the "asked for by the contract, not a column in your bordereau" half of
 *  the note — the clauses-with-no-column half is the contract's own
 *  "Rules with no column" list, shown beside it. */
export function MissingBdxColumnsPanel({ state }: { state: MissingColumnsState }) {
  const { data, loading, checking, failed, runCheck } = state;
  if (loading || checking) {
    return (
      <div className="flex items-center gap-2 rounded-md bg-surface-2 px-4 py-2.5 text-sm text-ink-muted">
        <Loader2 size={15} className="animate-spin shrink-0" />
        {checking ? "Checking this bordereau against the contract…" : "Loading contract check…"}
      </div>
    );
  }
  if (failed || !data) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md bg-surface-2 px-4 py-2.5 text-sm text-ink-muted">
        <Info size={15} className="shrink-0" />
        <span>The contract check couldn't be loaded.</span>
        <button type="button" onClick={() => runCheck(true)}
          className="linkish ml-auto inline-flex items-center gap-1">
          <RefreshCw size={12} /> Check Now
        </button>
      </div>
    );
  }
  if (!data.analyzed) {
    const why = data.skipped_reason;
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md bg-surface-2 px-4 py-2.5 text-sm text-ink-muted">
        <Info size={15} className="shrink-0" />
        <span>
          {why
            ? `This setup hasn't been checked for missing bordereau columns — ${why}.`
            : "This setup hasn't been checked for missing bordereau columns yet."}
        </span>
        <button type="button" onClick={() => runCheck(true)}
          className="linkish ml-auto inline-flex items-center gap-1">
          <RefreshCw size={12} /> Check Now
        </button>
      </div>
    );
  }
  const n = data.items.length;
  if (n === 0) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md bg-emerald-50 px-4 py-2.5 text-sm text-emerald-700">
        <CheckCircle2 size={15} className="shrink-0" />
        <span>
          Your bordereau provides everything the contract asks for.
          {data.analyzed_at && (
            <span className="text-emerald-700/70"> Checked {fmtStamp(data.analyzed_at)}.</span>
          )}
        </span>
      </div>
    );
  }
  // Full height in the tab — it is the whole panel, so the page scrolls.
  return <MissingBdxColumnsBody data={data} maxHeight="none" />;
}

/** The "Columns Missing from BDX" list with its counts — ONE body for the
 *  setup's sub-tab and the build-complete popup, so the two read the same. */
function MissingBdxColumnsBody({ data, maxHeight, footer }: {
  data: MissingColumnsResp; maxHeight: string;
  footer?: ReactNode;
}) {
  return (
    <div>
      {/* Deliberately about the BORDEREAU, not about rules: the check only
          compares contract prose against the columns of the broker's sample
          file, so an entry here may still have a rule behind it. */}
      <div className="mb-2 flex justify-end"><CountPills counts={data.counts} /></div>
      <MissingColumnsList items={data.items} maxHeight={maxHeight} />
      <p className="mt-2.5 flex items-start gap-1.5 px-1 text-[11px] text-ink-soft">
        <Info size={11} className="mt-0.5 shrink-0" />
        These columns stay blank in the output until the broker's file carries them, and any contract rule that needs them can't run.
      </p>
      {footer}
    </div>
  );
}

/** The contract's own unmapped clauses — the generic-library ones are not
 *  listed (or counted), the same rule as the setup's "Rules with no column". */
export const ownUnmappedClauses = (data: MissingColumnsResp | null | undefined) =>
  (data?.unmapped_clauses ?? []).filter(c => !isLibraryText(c.clause_text));

/** The two gap lists in the build-complete popup, as the SAME sub-tabs the
 *  setup's Contract Rules tab shows: "Rules with no column" and "Columns
 *  Missing from BDX", each with its who-fixes-it box. Lists scroll inside a
 *  capped box so the popup's buttons stay on screen. */
export function BuildGapTabs({ data, tab, onTab }: {
  data: MissingColumnsResp | null;
  tab: "missing" | "clauses";
  onTab: (t: "missing" | "clauses") => void;
}) {
  const clauses = ownUnmappedClauses(data);
  const missing = data?.items ?? [];
  const btn = (key: "missing" | "clauses", label: string, n: number) => (
    <button type="button" onClick={() => onTab(key)} aria-pressed={tab === key}
      className={`flex-1 whitespace-nowrap rounded-md px-3 py-1.5 text-center text-xs font-semibold transition-colors
        ${tab === key ? "bg-white text-ink shadow-sm" : "text-ink-muted hover:text-ink"}`}>
      {label} · {n}
    </button>
  );
  return (
    <div className="mt-4">
      <div className="mb-3 flex w-full gap-0.5 rounded-lg bg-surface-2 p-1" role="group" aria-label="Show">
        {btn("clauses", "Rules with no column", clauses.length)}
        {btn("missing", "Columns Missing from BDX", missing.length)}
      </div>
      {tab === "missing" ? (
        !data?.analyzed ? (
          <p className="flex items-center gap-2 rounded-md bg-surface-2 px-3 py-2 text-xs text-ink-muted">
            <Info size={13} className="shrink-0" />
            The bordereau-column check didn't run for this build — it runs again when you open the setup.
          </p>
        ) : missing.length === 0 ? (
          <p className="flex items-center gap-1.5 rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
            <CheckCircle2 size={13} className="shrink-0" /> Your bordereau provides everything the contract asks for.
          </p>
        ) : (
          <MissingBdxColumnsBody data={data} maxHeight="16rem" />
        )
      ) : clauses.length === 0 ? (
        <p className="flex items-center gap-1.5 rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
          <CheckCircle2 size={13} className="shrink-0" /> Every rule-bearing clause in this contract has a column.
        </p>
      ) : (
        <UnmappedClausesList items={clauses} maxHeight="16rem" />
      )}
      <p className="mt-2.5 text-[11px] text-ink-soft">
        Both lists stay on the setup — under Contract Rules in Configured Bordereau Setups.
      </p>
    </div>
  );
}

export default MissingColumnsNote;
