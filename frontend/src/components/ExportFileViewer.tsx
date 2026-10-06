import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { X } from "lucide-react";
import BdxInlineReview from "./BdxInlineReview";
import { getDownloadExceptions, type StoredException } from "../api/validation";

/** The processed bordereau ITSELF, over the page, read-only.
 *
 *  It is the Exception Triage screen's own BDX view (BdxInlineReview) in its
 *  read-only mode, so a long file behaves the same here as there: rows are
 *  STREAMED in and only the ones on screen are drawn. Every flagged cell still
 *  opens to show its error, rule and recommendation — but nothing can be
 *  decided here. That happens on the exceptions screen, one click away in the
 *  header. */
export function ExportFileViewer({ exportId, title, reviewTo, onClose }: {
  exportId: number;
  title: string;
  /** Where "Review exceptions" goes; omitted → no link. */
  reviewTo?: string;
  onClose: () => void;
}) {
  // The flagged cells the grid paints and opens. Loaded first: the grid needs
  // them to know which cells to highlight.
  const [excs, setExcs] = useState<StoredException[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setExcs(null); setErr(null);
    getDownloadExceptions(exportId).then(setExcs)
      .catch(e => setErr(e?.response?.data?.detail ?? "Could not open this file."));
  }, [exportId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,20,28,.45)",
      display: "flex", alignItems: "center", justifyContent: "center", padding: 24, zIndex: 70 }}
      onClick={onClose}>
      <div className="card" role="dialog" aria-modal="true"
        style={{ width: "min(1400px, 100%)", maxHeight: "92vh", margin: 0, overflow: "auto",
                 borderRadius: 14, display: "flex", flexDirection: "column" }}
        onClick={e => e.stopPropagation()}>
        <div className="card-h">
          <h3>{title}</h3>
          <span className="sub">View only</span>
          <div className="right" style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
            {reviewTo && <Link className="btn sm" to={reviewTo}>Review exceptions →</Link>}
            <button type="button" className="btn sm" onClick={onClose} aria-label="Close">
              <X size={14} />
            </button>
          </div>
        </div>
        <div style={{ padding: 16 }}>
          {err ? (
            <div className="note warn">{err}</div>
          ) : excs === null ? (
            <div className="note">Opening the file…</div>
          ) : (
            <BdxInlineReview
              readOnly
              // A replaced version has no Review exceptions link, so its note
              // must not point at one.
              readOnlyNote={reviewTo
                ? "View only — approve, fix or dismiss these on Review exceptions."
                : "View only — this version was replaced; its exceptions are worked on the newest version."}
              exportId={String(exportId)}
              exceptions={excs}
              onSaved={() => {}}
              onClose={onClose}
            />
          )}
        </div>
      </div>
    </div>
  );
}
