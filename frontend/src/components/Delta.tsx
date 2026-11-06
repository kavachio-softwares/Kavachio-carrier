// The change between two months, as one pill: green when it got better, red
// when worse, grey when the number only says there was more or less work
// (files, rows, policies) — Monthly Report, MOPR 42.2.
import { fmtTook, num } from "../utils/reportFormat";

/** Which way is better: up, down, or neither (a volume). */
export type Better = "up" | "down" | "none";
/** How the change is written: by how much (+3), by what share (+5%), in
 *  percentage points (▲ 5 pts), or as a time (▼ 21 h). */
export type ChangeKind = "count" | "share" | "points" | "time";

export function deltaOf(now: number | null | undefined, then: number | null | undefined,
                        kind: ChangeKind, better: Better): { text: string; tone: "ok" | "crit" | "mut" } | null {
  if (now == null || then == null) return null;
  const d = now - then;
  if (Math.abs(d) < 0.05) return { text: "No change", tone: "mut" };
  const arrow = better === "none" ? (d > 0 ? "+" : "−") : (d > 0 ? "▲ " : "▼ ");
  let size: string;
  if (kind === "share") {
    if (then === 0) return { text: d > 0 ? "New" : "No change", tone: "mut" };
    size = `${Math.round(Math.abs(100 * d / then))}%`;
  } else if (kind === "points") {
    size = `${Math.round(Math.abs(d))} pts`;
  } else if (kind === "time") {
    size = fmtTook(Math.abs(d));
  } else {
    size = num(Math.round(Math.abs(d) * 10) / 10);
  }
  const tone = better === "none" ? "mut" : ((d > 0) === (better === "up") ? "ok" : "crit");
  return { text: `${arrow}${size}`, tone };
}

export function Delta({ now, then, kind, better }: {
  now: number | null | undefined; then: number | null | undefined;
  kind: ChangeKind; better: Better;
}) {
  const d = deltaOf(now, then, kind, better);
  if (!d) return <span className="muted">—</span>;
  return <span className={`badge b-${d.tone}`}>{d.text}</span>;
}
