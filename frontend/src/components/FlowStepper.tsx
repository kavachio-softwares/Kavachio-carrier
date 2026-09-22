// A clickable step trail for a multi-step flow (Configure Program → Assign
// brokers → Contracts → Bordereau setup). Styled to match the Create a Contract
// trail: one card across the full width, "STEP n" over the step's name, › between steps, the
// current step on a soft brand fill. Where a step stands ("2 brokers") and, for
// a step you cannot reach yet, what unlocks it, are in the tooltip.
import { Fragment } from "react";
import { Check, ChevronRight } from "lucide-react";

export type FlowStep = {
  key: string;
  label: string;
  /** Where the step stands — shown as the tooltip. */
  sub: string;
  state: "done" | "current" | "todo";
  /** The step whose section is on screen — highlighted even once done. */
  open?: boolean;
  /** False while an earlier step still has to be finished. */
  enabled: boolean;
  /** Tooltip for a disabled step: what unlocks it. */
  title?: string;
  onClick: () => void;
};

export function FlowStepper({ steps, label }: { steps: FlowStep[]; label: string }) {
  return (
    <nav aria-label={label}
      className="mb-5 flex w-full flex-wrap items-center gap-px rounded-lg border border-border bg-surface p-[5px] shadow-sm">
      {steps.map((s, i) => {
        const done = s.state === "done";
        const current = s.open ?? s.state === "current";
        return (
          <Fragment key={s.key}>
            {i > 0 && <ChevronRight aria-hidden size={16} strokeWidth={2.25} className="mx-1 shrink-0 text-ink-muted" />}
            <button
              type="button"
              onClick={s.onClick}
              disabled={!s.enabled}
              title={s.enabled ? s.sub : (s.title ?? s.sub)}
              aria-current={current ? "step" : undefined}
              className={`flex min-w-[150px] flex-1 items-center gap-2.5 rounded-md px-[13px] py-[7px] text-left transition-colors
                ${current ? "bg-navy/10 text-navy" : "text-ink enabled:hover:bg-surface-2"}
                disabled:cursor-not-allowed
                focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-navy`}
            >
              <span className={`grid h-5 w-5 shrink-0 place-items-center rounded-full text-[10.5px] font-bold
                ${done && !current ? "bg-success text-white"
                  : current ? "bg-navy text-white"
                  : "bg-surface-2 text-ink-muted"}`}>
                {done && !current ? <Check size={12} strokeWidth={3} /> : i + 1}
              </span>
              <span className="flex min-w-0 flex-col leading-tight">
                <span className={`text-[8.5px] font-bold uppercase tracking-[1.1px]
                  ${current ? "text-navy/70" : "text-ink-soft"}`}>
                  Step {i + 1}
                </span>
                <span className={`truncate text-[13px] font-semibold
                  ${current ? "text-navy" : s.enabled ? "text-ink" : "text-ink-muted"}`}>
                  {s.label}
                </span>
              </span>
            </button>
          </Fragment>
        );
      })}
    </nav>
  );
}
