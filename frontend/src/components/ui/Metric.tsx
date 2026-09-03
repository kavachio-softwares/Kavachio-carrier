/**
 * One number and what it counts, as a chip.
 *
 * These lists are mostly counts — programmes, contracts, users — and as loose
 * grey text they all weigh the same, so a broker with no contracts looks
 * exactly like one with nine. A chip gives the number its own box and lets the
 * ones that need attention say so, instead of relying on the reader to notice a
 * zero in a sentence.
 *
 * `tone="attention"` is for a count that BLOCKS something: a programme with no
 * broker cannot hold a contract, a broker with no contract cannot be set up.
 * It is deliberately not "error" — nothing has gone wrong, there is just a next
 * step — so it borrows the warning tint, not the danger one.
 */
import { ReactNode } from "react";

export function Metric({ icon, value, label, tone = "plain", title }: {
  icon?: ReactNode;
  value: ReactNode;
  label: string;
  tone?: "plain" | "attention";
  title?: string;
}) {
  const cls = tone === "attention"
    ? "bg-warn/10 text-warn border-warn/20"
    : "bg-surface-2 text-ink-muted border-transparent";
  return (
    <span title={title}
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11.5px] ${cls}`}>
      {icon}
      <span className="font-semibold tabular-nums">{value}</span>
      <span>{label}</span>
    </span>
  );
}

export default Metric;
