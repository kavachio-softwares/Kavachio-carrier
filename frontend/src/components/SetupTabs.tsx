// The tabs across a saved Bordereau Setup — the read-only view and its Edit
// screen share the same ones (Edit leaves out "Needs attention", whose fixes
// are made on its other tabs), so moving between the two lands on the same
// section. The open tab lives in the URL (?tab=), which is how View's Edit
// button carries it across and how a refresh keeps it.
//
// SheetChips is the second level inside Field mapping: one input sheet at a
// time instead of a stack of collapsed cards.
import { useSearchParams } from "react-router-dom";

export type SetupTabKey = "overview" | "mapping" | "contracts" | "attention" | "calendar";

export type SetupTab = {
  key: SetupTabKey;
  label: string;
  /** A small count beside the label. */
  count?: number;
  /** Amber when the count is something to fix. */
  warn?: boolean;
};

/** The open tab, read from and written to ?tab=. Falls back to the first tab
 *  when the URL names one this screen does not have. */
export function useSetupTab(tabs: SetupTab[]): [SetupTabKey, (k: SetupTabKey) => void] {
  const [params, setParams] = useSearchParams();
  const want = params.get("tab") as SetupTabKey | null;
  const current = tabs.some(t => t.key === want) ? want! : tabs[0].key;
  const set = (k: SetupTabKey) => {
    const next = new URLSearchParams(params);
    next.set("tab", k);
    setParams(next, { replace: true });
  };
  return [current, set];
}

export function SetupTabs({ tabs, current, onChange }: {
  tabs: SetupTab[]; current: SetupTabKey; onChange: (k: SetupTabKey) => void;
}) {
  return (
    <div role="tablist" aria-label="Setup sections"
      className="mb-4 flex gap-0.5 overflow-x-auto border-b border-border">
      {tabs.map(t => {
        const on = t.key === current;
        return (
          <button key={t.key} type="button" role="tab" aria-selected={on}
            onClick={() => onChange(t.key)}
            className={`-mb-px inline-flex shrink-0 items-center gap-2 whitespace-nowrap border-b-[2.5px] px-3.5 py-2.5 text-[13.5px] font-semibold transition-colors
              ${on ? "border-navy text-navy" : "border-transparent text-ink-muted hover:text-ink"}
              focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-navy`}>
            {t.label}
            {t.count != null && (
              <span className={`rounded-full px-[7px] py-px text-[11px] font-bold tabular-nums
                ${t.warn ? "bg-amber-50 text-amber-700" : "bg-surface-2 text-ink-muted"}`}>
                {t.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** One chip per input sheet; the picked one is shown below. `unsourced` puts
 *  an amber count on a sheet with output columns still unfilled. */
export function SheetChips({ sheets, current, onPick, unsourced }: {
  sheets: string[]; current: string; onPick: (s: string) => void;
  unsourced?: (sheet: string) => number;
}) {
  if (sheets.length < 2) return null;
  return (
    <div className="mb-3 flex flex-wrap gap-1.5" role="group" aria-label="Input sheets">
      {sheets.map(s => {
        const on = s === current;
        const n = unsourced?.(s) ?? 0;
        return (
          <button key={s} type="button" aria-pressed={on} onClick={() => onPick(s)}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12.5px] font-semibold transition-colors
              ${on ? "border-navy bg-navy/10 text-navy" : "border-border bg-white text-ink-muted hover:text-ink"}
              focus-visible:outline focus-visible:outline-2 focus-visible:outline-navy`}>
            {s}
            {n > 0 && (
              <span className="rounded-full bg-amber-50 px-1.5 text-[10.5px] text-amber-700">{n} unsourced</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
