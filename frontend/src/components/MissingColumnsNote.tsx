import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, ArrowRight, CheckCircle2, FileWarning, Info, Loader2, Quote,
  RefreshCw,
} from "lucide-react";
import { api } from "../api/client";
import { fmtStamp } from "../utils/date";
import { MissingColumn, MissingColumnsResp, UnmappedClause } from "../utils/directSetup";

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

/** One missing column. Everything below the name is optional — a finding with
 *  only a name still renders cleanly. */
function MissingColumnRow({ item }: { item: MissingColumn }) {
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
      {item.contract_reference && (
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
      {item.related_output_field && (
        <p className="mt-1.5 flex items-center gap-1 text-[11px] text-ink-soft">
          <ArrowRight size={11} className="shrink-0" />
          Feeds output column <span className="font-medium text-ink-muted">{item.related_output_field}</span>
        </p>
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
function UnmappedClauseRow({ item }: { item: UnmappedClause }) {
  return (
    <li className="px-3.5 py-3 first:pt-3 hover:bg-sky-50/40 transition-colors">
      <div className="flex items-start gap-2 flex-wrap">
        <span className="font-semibold text-sm text-ink break-words">
          {item.rule_name || "Untitled clause"}
        </span>
        {item.source_page != null && (
          <span className="pill pill-grey shrink-0 font-mono !text-[10px]">
            Page {item.source_page}
          </span>
        )}
      </div>
      {item.clause_text && (
        <div className="mt-1.5 rounded-md bg-white/70 border border-sky-100 px-2.5 py-1.5">
          <div className="flex items-start gap-1.5">
            <Quote size={11} className="mt-1 shrink-0 text-sky-500" />
            <p className="text-[11.5px] italic leading-snug text-ink-muted">
              {item.clause_text}
            </p>
          </div>
        </div>
      )}
      {item.reason && (
        <p className="mt-1 text-[12.5px] leading-snug text-ink-muted">
          <span className="font-medium text-ink-soft">Why unmapped: </span>
          {item.reason}
        </p>
      )}
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
    <div className="rounded-lg border border-sky-200 bg-sky-50/50 overflow-hidden">
      <ul className="divide-y divide-sky-200/70 overflow-y-auto overscroll-contain"
        style={{ maxHeight }}>
        {items.map((it, i) => (
          <UnmappedClauseRow key={`${it.clause_id ?? "x"}-${i}`} item={it} />
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
                                     className = "" }: {
  pipelineId: number | string; autoCheck?: boolean;
  /** Bump to re-read the note. The server drops findings whose output field is
   *  already mapped, so setting a field on a rule makes its entry disappear —
   *  this is how the page says "I just changed a mapping, look again". Re-reading
   *  is free: it never spends a model call on an already-analyzed setup. */
  refreshKey?: number;
  className?: string;
}) {
  const [data, setData] = useState<MissingColumnsResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [failed, setFailed] = useState(false);

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
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 px-5 py-3.5 bg-amber-50 border-b border-amber-200">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-amber-100 text-amber-700">
          <FileWarning size={17} />
        </span>
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-amber-900">
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
      </header>
      <div className="p-3 space-y-4">
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

export default MissingColumnsNote;
