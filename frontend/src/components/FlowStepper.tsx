// A clickable step trail for a multi-step flow (Configure Program → Assign
// brokers → Contracts → Bordereau setup). Styled to match the Create a Contract
// trail: one card across the full width, "STEP n" over the step's name, › between steps, the
// current step on a soft brand fill. Where a step stands ("2 brokers") and, for
// a step you cannot reach yet, what unlocks it, are in the tooltip.
import { Fragment, type ReactNode } from "react";
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

/** `children`, when given, is drawn inside the same card under the steps —
 *  the steps of whatever the current step is made of (a contract's own four
 *  steps, under "Contracts"), so there is one stepper on screen, not two. */
export function FlowStepper({ steps, label, children }: {
  steps: FlowStep[]; label: string; children?: ReactNode;
}) {
  return (
    <nav aria-label={label}
      className="mb-5 w-full rounded-lg border border-border bg-surface p-[5px] shadow-sm">
      <div className="flex w-full flex-wrap items-center gap-px">
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
      </div>
      {children}
    </nav>
  );
}

export type SubStep = { key: string; label: string };

/** The smaller row inside a step: dots, not numbered circles, so it reads as
 *  part of the step above it rather than as a second stepper. */
export function SubSteps({ caption, steps, current, furthest, onPick }: {
  caption: string;
  steps: readonly SubStep[];
  current: number;
  /** The furthest sub-step reached; everything before it shows as done. */
  furthest: number;
  onPick: (i: number) => void;
}) {
  return (
    <div className="mx-[5px] mb-0.5 mt-1.5 flex flex-wrap items-center gap-1 rounded-md border border-border bg-surface-2 px-2.5 py-2"
      aria-label={caption}>
      <span className="mr-2 whitespace-nowrap text-[11px] font-bold text-navy">{caption}:</span>
      {steps.map((s, i) => {
        const on = i === current;
        const done = !on && (i < furthest || i < current);
        return (
          <Fragment key={s.key}>
            {i > 0 && <span aria-hidden className="px-0.5 text-[13px] text-ink-soft">›</span>}
            <button type="button" onClick={() => !on && onPick(i)}
              aria-current={on ? "step" : undefined}
              title={on ? undefined : `Go to ${s.label}`}
              className={`inline-flex items-center gap-[7px] rounded-md px-2.5 py-1 text-[12.5px] font-semibold transition-colors
                ${on ? "bg-navy/10 text-navy" : done ? "text-ink hover:bg-surface" : "text-ink-muted hover:bg-surface"}
                focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-navy`}>
              <span aria-hidden className={`h-[9px] w-[9px] shrink-0 rounded-full border-2
                ${on ? "border-navy bg-navy ring-[3px] ring-navy/20"
                  : done ? "border-success bg-success" : "border-border bg-transparent"}`} />
              {s.label}
            </button>
          </Fragment>
        );
      })}
    </div>
  );
}
