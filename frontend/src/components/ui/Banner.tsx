import { ReactNode } from "react";

export function Banner({ kind, children, className = "" }: {
  kind: "error" | "ok" | "warn" | "info"; children: ReactNode; className?: string;
}) {
  const styles = {
    error: "bg-danger/10 text-danger", ok: "bg-emerald-50 text-emerald-700",
    warn: "bg-amber-50 text-amber-700", info: "bg-surface-2 text-ink-muted",
  }[kind];
  return <div className={`flex flex-wrap items-center gap-2 rounded-md px-4 py-2.5 text-sm ${styles} ${className}`}>{children}</div>;
}
export default Banner;
