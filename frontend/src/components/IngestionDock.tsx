// The Ingestion panel's home in the app shell: a button in the sidebar footer
// beside the bell, and the drawer it opens — on every screen, not only Files.
//
// Open state lives in the URL (?panel=ingestion), the same rule the Files page
// already used, so a link still opens it and a reload keeps it open. The Files
// page head's "Ingestion panel" button sets the same param and this drawer
// answers it — there is ONE panel, never a second copy stacked on the first.
//
// The badge counts what the panel's "N need you" counts. It turns red when a
// run failed or could not be run, because that is the one thing a person has
// to act on; exceptions alone keep it teal.
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useSearchParams } from "react-router-dom";
import { Download } from "lucide-react";
import { listArrivals, watchArrivals, type Arrival } from "../api/intake";
import { IngestionPanel, countNeedsYou, useDismissedRuns } from "./IngestionPanel";

/** Fired after "Run again" so a Files table already on screen re-reads too. */
export const INGESTION_CHANGED = "ingestion:changed";

export default function IngestionDock({ showButton }: {
  /** The sidebar button. Off for a seat with no bell beside it — the drawer is
   *  still mounted so the Files page's own button can open it. */
  showButton: boolean;
}) {
  const [params, setParams] = useSearchParams();
  const open = params.get("panel") === "ingestion";
  const setOpen = useCallback((next: boolean) => {
    setParams(prev => {
      const p = new URLSearchParams(prev);
      if (next) p.set("panel", "ingestion"); else p.delete("panel");
      return p;
    }, { replace: true });
  }, [setParams]);

  // Only worth fetching when something shows it: the badge, or the drawer.
  const wanted = showButton || open;
  const [rows, setRows] = useState<Arrival[]>([]);
  const load = useCallback(async () => {
    try { setRows((await listArrivals()).rows); }
    catch { /* the badge just keeps its last count; the next change retries */ }
  }, []);
  useEffect(() => {
    if (!wanted) return;
    load();
    // The server says the moment this carrier's files change — a run finished,
    // a file landed, somebody decided one. Bursts fold into one re-read.
    const ctl = new AbortController();
    let fold: number | undefined;
    watchArrivals(() => {
      window.clearTimeout(fold);
      fold = window.setTimeout(load, 400);
    }, ctl.signal);
    return () => { ctl.abort(); window.clearTimeout(fold); };
  }, [wanted, load]);
  // Re-read whenever the drawer opens, so it never shows a stale list.
  useEffect(() => { if (open) load(); }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, setOpen]);

  const [dismissed] = useDismissedRuns();
  const need = countNeedsYou(rows, dismissed);
  const stopped = rows.some(a => (a.run_result === "failed" || a.run_result === "not_run")
    && !dismissed.has(a.arrival_id));

  return (
    <>
      {showButton && (
        <button type="button" onClick={() => setOpen(!open)}
          title="Ingestion panel"
          aria-label={`Ingestion panel${need ? ` (${need} need you)` : ""}`}
          aria-expanded={open}
          style={{
            position: "relative", display: "inline-flex", alignItems: "center",
            justifyContent: "center", width: 30, height: 30, borderRadius: 8, flex: "0 0 auto",
            background: open ? "rgba(255,255,255,0.14)" : "transparent",
            border: "none", color: open ? "#fff" : "inherit", cursor: "pointer",
          }}>
          <Download size={16} strokeWidth={1.8} />
          {need > 0 && (
            <span style={{
              position: "absolute", top: 1, right: 1, minWidth: 15, height: 15,
              padding: "0 3px", borderRadius: 8, color: "#fff",
              background: stopped ? "var(--p-crit)" : "var(--p-primary)",
              fontSize: 9.5, fontWeight: 700, lineHeight: "15px", textAlign: "center",
            }}>{need > 99 ? "99+" : need}</span>
          )}
        </button>
      )}

      {/* Portalled to <body>: the sidebar is `position: sticky`, a stacking
          context that would trap a fixed drawer under the page. The `.proto`
          wrapper is what the drawer's styles are scoped to. */}
      {createPortal(
        <div className="proto">
          <IngestionPanel open={open} rows={rows}
            onClose={() => setOpen(false)}
            onChanged={() => { load(); window.dispatchEvent(new Event(INGESTION_CHANGED)); }} />
        </div>,
        document.body,
      )}
    </>
  );
}
