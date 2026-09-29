// The Ingestion panel — every file that has been RUN, grouped by the contract
// it was checked against, with what the run did.
//
// The Files table answers "what came in?"; this answers "what happened once it
// was inside, and does any of it need me?". Held and turned-away files are
// never here: they have no run. Only the three end results the run can give
// are shown — there is no Queued or Running, because a run is only recorded
// once it has finished.
//
// "Clear ingested" and ✕ hide a row HERE only, in this browser. The file and
// its run stay on the Files screen; a failed run or open exceptions are never
// cleared in bulk, because they are the reason to open this at all.
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { rerunArrival, type Arrival } from "../api/intake";
import { Badge, RUN_META } from "../pages/FilesReceived";
import { fmtStamp } from "../utils/date";

type Tab = "all" | "attention" | "done";

const DISMISSED_KEY = "ingestionPanel.dismissed";

function readDismissed(): Set<number> {
  try {
    const raw = window.localStorage.getItem(DISMISSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as number[]) : []);
  } catch { return new Set(); }
}
function writeDismissed(ids: Set<number>) {
  try { window.localStorage.setItem(DISMISSED_KEY, JSON.stringify([...ids].slice(-500))); }
  catch { /* private window — the panel just forgets on reload */ }
}

/** Files that need somebody: a failed run, open exceptions, or one that could
 *  not be run automatically. */
export const needsYou = (a: Arrival) =>
  a.run_result === "failed" || a.run_result === "exceptions" || a.run_result === "not_run";

export function IngestionPanel({ open, rows, onClose, onChanged }: {
  open: boolean;
  /** The same rows the Files table holds — one fetch for the whole screen. */
  rows: Arrival[];
  onClose: () => void;
  /** After Run again, so the table and this panel both re-read. */
  onChanged: () => void;
}) {
  const nav = useNavigate();
  const [tab, setTab] = useState<Tab>("all");
  const [dismissed, setDismissed] = useState<Set<number>>(readDismissed);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { if (!open) setErr(null); }, [open]);

  const ran = useMemo(() => rows.filter(a => a.run_result && !dismissed.has(a.arrival_id)), [rows, dismissed]);
  const attention = ran.filter(needsYou);
  // Finished = the run completed, whatever it found. Exceptions are both
  // finished AND needing you, so they sit in both tabs.
  const finished = ran.filter(a => a.run_result === "ingested"
    || a.run_result === "exceptions" || a.run_result === "not_checked");
  const shown = tab === "all" ? ran : tab === "attention" ? attention : finished;

  // Grouped by contract, the way the reference groups by binder: the contract
  // is what the file was answerable to.
  const groups = useMemo(() => {
    const m = new Map<string, Arrival[]>();
    for (const a of shown) {
      const key = a.contract_name ?? a.program_name ?? "No contract recorded";
      m.set(key, [...(m.get(key) ?? []), a]);
    }
    return [...m.entries()];
  }, [shown]);

  function dismiss(ids: number[]) {
    const next = new Set(dismissed);
    ids.forEach(i => next.add(i));
    setDismissed(next); writeDismissed(next);
  }

  async function runAgain(a: Arrival) {
    setBusyId(a.arrival_id); setErr(null);
    try { await rerunArrival(a.arrival_id); onChanged(); }
    catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setErr(detail || "That did not work. Nothing has changed.");
    } finally { setBusyId(null); }
  }

  function meta(a: Arrival): React.ReactNode {
    const when = a.run_at ? fmtStamp(a.run_at) : "";
    if (a.run_result === "failed" || a.run_result === "not_run") {
      return <span style={{ color: "var(--p-crit-ink)" }}>{a.run_error ?? "No reason was recorded."}</span>;
    }
    const rowsTxt = a.run_rows != null ? `${a.run_rows.toLocaleString()} rows · ` : "";
    if (a.run_result === "exceptions") {
      return `${rowsTxt}${(a.run_exception_count ?? 0).toLocaleString()} exceptions · ${when}`;
    }
    if (a.run_result === "not_checked") return `${rowsTxt}contract checks did not run · ${when}`;
    return `${rowsTxt}clean · ${when}`;
  }

  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer${open ? " on" : ""}`} role="dialog" aria-modal="true"
        aria-hidden={!open} aria-label="Ingestion panel">
        <div className="drawer-h">
          <div style={{ minWidth: 0 }}>
            <h4>Ingestion panel</h4>
            <div className="ref" style={{ fontFamily: "inherit", fontSize: 12 }}>
              {attention.length
                ? `${attention.length} need${attention.length === 1 ? "s" : ""} you`
                : "All clear"}
              {" · "}
              <button type="button" className="linkbtn" style={{ fontSize: 12 }}
                title="Hides clean runs here only — they stay on the Files screen"
                onClick={() => dismiss(ran.filter(a => a.run_result === "ingested").map(a => a.arrival_id))}>
                Clear ingested</button>
            </div>
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>

        <div style={{ display: "flex", gap: 6, padding: "10px 18px", borderBottom: "1px solid var(--p-border)" }}>
          {([["all", "All", ran.length], ["attention", "Needs you", attention.length],
             ["done", "Finished", finished.length],
          ] as const).map(([k, label, n]) => (
            <button key={k} type="button" className="chip" aria-pressed={tab === k}
              onClick={() => setTab(k)}>{label} <span className="n">{n}</span></button>
          ))}
        </div>

        <div className="drawer-b">
          {err && <div className="note crit" style={{ marginBottom: 12 }}>{err}</div>}
          {groups.length === 0 ? (
            <div style={{ textAlign: "center", padding: "32px 12px", color: "var(--p-muted)", fontSize: 12.5 }}>
              Nothing here.<br />Files appear here once they have been run.
            </div>
          ) : groups.map(([contract, files]) => (
            <div key={contract} style={{ marginBottom: 16 }}>
              <div className="sub-h" style={{ marginTop: 0 }}>{contract}
                {files[0].broker_name && (
                  <span style={{ textTransform: "none", letterSpacing: 0, fontWeight: 500 }}>
                    {" · "}{files[0].broker_name}</span>)}
              </div>
              {files.map(a => {
                const m = RUN_META[a.run_result!];
                return (
                  <div key={a.arrival_id} style={{
                    padding: "9px 0", borderBottom: "1px solid var(--p-border)", display: "grid", gap: 4,
                  }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span className="fname" style={{ minWidth: 0, overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={a.filename}>
                        {a.filename}</span>
                      <span style={{ marginLeft: "auto" }} title={m.meaning}>
                        <Badge tone={m.tone}>{m.label}</Badge></span>
                    </div>
                    <div className="faint" style={{ fontSize: 11.5 }}>{meta(a)}</div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      {a.run_export_id && (
                        <button type="button" className="linkbtn" style={{ fontSize: 12 }}
                          onClick={() => nav(`/uploads/${a.run_export_id}/exceptions?download=${a.run_export_id}&from=files`)}>
                          View result →</button>)}
                      {(a.run_result === "failed" || a.run_result === "not_run") && (
                        <button type="button" className="btn sm" disabled={busyId === a.arrival_id}
                          onClick={() => runAgain(a)}>
                          {busyId === a.arrival_id ? "Working…" : "Run again"}</button>)}
                      <button type="button" className="linkbtn mut" style={{ marginLeft: "auto", fontSize: 12 }}
                        title="Hide it here — it stays on the Files screen"
                        onClick={() => dismiss([a.arrival_id])}>✕</button>
                    </div>
                  </div>);
              })}
            </div>
          ))}
        </div>

        <div className="drawer-f">
          <span className="faint" style={{ fontSize: 11.5 }}>Every file that has been run, by contract.</span>
          <button type="button" className="btn" style={{ marginLeft: "auto" }} onClick={onClose}>Done</button>
        </div>
      </aside>
    </>
  );
}
