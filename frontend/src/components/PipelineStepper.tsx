import { Link } from "react-router-dom";
import { Check } from "lucide-react";

// The workflow stages, in order. "template" and "output" both live on the
// BDX Output screen (/outputs) — the flow visits it twice (set up, then generate).
export type StageKey = "party" | "program" | "template" | "output";

const STAGES: { key: StageKey; label: string; to: string }[] = [
  { key: "party",    label: "Party",    to: "/parties"  },
  { key: "program",  label: "Program",  to: "/programs" },
  { key: "template", label: "Template", to: "/outputs/new-template" },
  { key: "output",   label: "Output",   to: "/outputs/generate" },
];

// Always-on horizontal pipeline stepper. Sits directly under the page header on
// every flow screen so the user can see (and jump to) any of the five stages.
export default function PipelineStepper({ current }: { current: StageKey }) {
  const idx = STAGES.findIndex((s) => s.key === current);
  return (
    <nav aria-label="Workflow progress"
      className="border-b border-border bg-white px-8 py-3">
      <ol className="flex items-center">
        {STAGES.map((s, i) => {
          const done = i < idx;
          const active = i === idx;
          return (
            <li key={`${s.key}-${i}`} className="flex items-center flex-1 last:flex-none">
              <Link to={s.to} title={s.label}
                className="flex items-center gap-2 shrink-0 group">
                <span className={`w-7 h-7 rounded-full flex items-center justify-center
                  text-[11px] font-semibold transition ${
                    done ? "bg-emerald-100 text-emerald-700"
                      : active ? "bg-navy text-white"
                      : "bg-surface-2 text-ink-soft"}`}>
                  {done ? <Check size={14} /> : i + 1}
                </span>
                <span className={`text-[13px] transition group-hover:text-ink ${
                  active ? "text-ink font-medium"
                    : done ? "text-ink-muted"
                    : "text-ink-soft"}`}>
                  {s.label}
                </span>
              </Link>
              {i < STAGES.length - 1 && (
                <span className={`flex-1 h-0.5 mx-3 rounded ${
                  done ? "bg-emerald-200" : "bg-border"}`} />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
