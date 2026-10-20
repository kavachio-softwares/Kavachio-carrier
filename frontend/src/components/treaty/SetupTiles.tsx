/** The broker setup's tiles, for a treaty's setup: a count that opens its
 *  tab, and a stat with an icon. */
import type { ReactNode } from "react";

export function JumpTile({ n, label, warn, onClick, detail }: {
  n: number; label: ReactNode; warn?: boolean; onClick: () => void; detail?: ReactNode;
}) {
  return (
    <div role="button" tabIndex={0} onClick={onClick}
      onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } }}
      className="grid cursor-pointer content-start gap-0.5 rounded-lg border border-border bg-white px-4 py-3 text-left transition-colors hover:border-navy">
      <span className={`text-xl font-bold tabular-nums ${warn ? "text-amber-600" : "text-ink"}`}>{n}</span>
      <span className="text-xs text-ink-muted">{label} →</span>
      {detail && <span className="mt-1.5 grid gap-0.5 text-[11.5px] leading-snug text-ink-muted">{detail}</span>}
    </div>
  );
}

export function GapLine({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={e => { e.stopPropagation(); onClick(); }}
      className="block text-left hover:text-navy hover:underline">
      • {children}
    </button>
  );
}

export function Stat({ icon: Icon, n, label, warn }: {
  icon: React.ElementType; n: number; label: string; warn?: boolean;
}) {
  return (
    <div className={`flex items-center gap-3 rounded-lg border px-4 py-3
      ${warn ? "border-amber-200 bg-amber-50/40" : "border-border bg-white"}`}>
      <span className={`grid h-9 w-9 place-items-center rounded-md ${warn ? "bg-amber-50 text-amber-600" : "bg-emerald-50 text-emerald-700"}`}>
        <Icon size={16} />
      </span>
      <span>
        <span className="block text-xl font-bold tabular-nums text-ink">{n}</span>
        <span className="text-[12px] text-ink-muted">{label}</span>
      </span>
    </div>
  );
}
