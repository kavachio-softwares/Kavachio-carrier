import { useMemo } from "react";
import { AlertTriangle, FileWarning } from "lucide-react";

// A document the contract defers rule content to. `external` entries were named
// by the contract but never supplied at setup time, so the clauses that depend
// on them produced no rule; `provided` are the reference files that were attached.
export type ExternalRef = {
  document_name?: string | null; version_or_date?: string | null; page?: number | null;
};
export type PipelineRefDocs = {
  contract_id: number; filename: string | null;
  external: ExternalRef[]; provided: string[];
};
export type RefDocsSummary = {
  missing: { name: string; detail: string | null; page: number | null; from: string[] }[];
  provided: string[];
};

/** Documents a setup's contract(s) point at. Deduped by name across contracts
 *  (one guideline is usually cited by every schedule), keeping which contracts
 *  cite it so a multi-contract setup still reads unambiguously. */
export function summarizeRefDocs(docs: PipelineRefDocs[] | undefined): RefDocsSummary {
  const missing = new Map<string, RefDocsSummary["missing"][number]>();
  const provided = new Set<string>();
  for (const rd of docs ?? []) {
    const from = rd.filename || `Contract #${rd.contract_id}`;
    for (const e of rd.external ?? []) {
      const name = (e.document_name || "").trim() || "Unnamed document";
      const hit = missing.get(name.toLowerCase());
      if (hit) { if (!hit.from.includes(from)) hit.from.push(from); continue; }
      missing.set(name.toLowerCase(), {
        name, detail: (e.version_or_date || "").trim() || null,
        page: e.page ?? null, from: [from],
      });
    }
    for (const n of rd.provided ?? []) provided.add(n);
  }
  return { missing: [...missing.values()], provided: [...provided] };
}

export function useRefDocs(docs: PipelineRefDocs[] | undefined): RefDocsSummary {
  return useMemo(() => summarizeRefDocs(docs), [docs]);
}

/** The document(s) the contract defers rules to that this setup was built
 *  WITHOUT — the user chose "Continue Without It" (or the contract named them
 *  after the fact). Named on BOTH the read-only setup view and the editor,
 *  because from either screen there is otherwise no way to tell that a clause
 *  produced no rule for want of a document, rather than because the contract
 *  never asked for it. */
export function MissingReferenceDocsNote(
  { refDocs, showSources = false, className = "" }:
  { refDocs: RefDocsSummary; showSources?: boolean; className?: string },
) {
  if (refDocs.missing.length === 0) return null;
  const many = refDocs.missing.length > 1;
  return (
    <div className={`rounded-lg border border-amber-200 bg-amber-50/60 p-3.5 ${className}`}>
      <div className="flex items-center gap-2 text-sm font-semibold text-amber-800">
        <FileWarning size={15} className="shrink-0" />
        Built without {refDocs.missing.length} referenced document{many ? "s" : ""}
      </div>
      <p className="mt-1 text-[12.5px] leading-snug text-ink-muted">
        The contract defers some rules (e.g. authorized / excluded classes of
        business) to the document{many ? "s" : ""} below, which
        {many ? " were" : " was"} not provided at setup — so those clauses
        produced no rules.
      </p>
      <ul className="mt-2.5 space-y-1.5">
        {refDocs.missing.map((d, i) => (
          <li key={i} className="flex items-start gap-2 text-sm text-ink">
            <AlertTriangle size={13} className="mt-1 shrink-0 text-amber-600" />
            <span className="min-w-0">
              <span className="font-medium break-words">{d.name}</span>
              {d.detail && <span className="text-ink-muted"> · {d.detail}</span>}
              {d.page != null && <span className="text-ink-soft"> · Page {d.page}</span>}
              {showSources && (
                <div className="text-[11px] text-ink-soft">
                  Referred to by {d.from.join(", ")}
                </div>
              )}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-2.5 text-[11.5px] text-ink-muted">
        Attach {many ? "them" : "it"} under
        <b> Reference document(s)</b> in Bordereau Setup and rebuild to turn those
        clauses into rules.
      </p>
    </div>
  );
}
