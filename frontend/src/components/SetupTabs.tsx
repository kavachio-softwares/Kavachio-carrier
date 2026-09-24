// The tabs across a saved Bordereau Setup — the read-only view and its Edit
// screen share the same ones (Edit leaves out "Needs attention", whose fixes
// are made on its other tabs), so moving between the two lands on the same
// section. The open tab lives in the URL (?tab=), which is how View's Edit
// button carries it across and how a refresh keeps it.
//
// SheetChips is the second level inside Field mapping: one input sheet at a
// time instead of a stack of collapsed cards.
import { useSearchParams } from "react-router-dom";

// "details" and "documents" belong to the unsaved Bordereau Setup screen
// (pages/DirectSetup.tsx), which splits the same one-card form into tabs;
// "output" is shared with the saved-setup screens below.
export type SetupTabKey = "details" | "documents" | "overview" | "mapping"
  | "contracts" | "output" | "attention" | "calendar";

export type SetupTab = {
  key: SetupTabKey;
  label: string;
  /** A small count beside the label. */
  count?: number;
  /** Amber when the count is something to fix. */
  warn?: boolean;
  /** Its place in an order worth showing, on the screens that have one. The
   *  saved-setup screens are a set of views and number nothing; the unsaved
   *  Bordereau Setup is a form read front to back, and saying so is half of
   *  what tells someone there is more of it after the tab they are on. */
  step?: number;
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
    // A SEGMENTED CONTROL, not a row of underlined words. The open tab is a
    // filled dark box that sits in a recessed track: a 2.5px rule and a change
    // of text colour were both easy to read past, and a dark box floating on
    // white read as a stray button rather than as one of a set. The track is
    // what makes the unselected tabs look selectable.
    <div role="tablist" aria-label="Setup sections"
      className="mb-5 inline-flex max-w-full gap-1 overflow-x-auto rounded-xl
        border border-border bg-surface-2 p-1">
      {tabs.map(t => {
        const on = t.key === current;
        return (
          <button key={t.key} type="button" role="tab" aria-selected={on}
            onClick={() => onChange(t.key)}
            className={`inline-flex shrink-0 items-center gap-2 whitespace-nowrap rounded-lg px-3.5 py-2 text-[13.5px] font-semibold transition-all
              ${on ? "bg-ink text-white shadow-sm"
                   : "text-ink-muted hover:bg-white/70 hover:text-ink"}
              focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-navy`}>
            {t.step != null && (
              <span className={`grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full text-[10.5px] font-bold tabular-nums
                ${on ? "bg-white/20 text-white" : "bg-white text-ink-soft"}`}>
                {t.step}
              </span>
            )}
            {t.label}
            {t.count != null && (
              // A count that is something to FIX keeps its amber on either
              // background — it is a signal, not decoration. A plain count has
              // to lift off the dark box instead of sinking into it.
              <span className={`rounded-full px-[7px] py-px text-[11px] font-bold tabular-nums
                ${t.warn ? "bg-amber-50 text-amber-700"
                  : on ? "bg-white/20 text-white" : "bg-white text-ink-muted"}`}>
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
