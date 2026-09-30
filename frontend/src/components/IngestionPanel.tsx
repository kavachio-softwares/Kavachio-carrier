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
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Cloud, Mail, MonitorUp, Plug, Server } from "lucide-react";
import { rerunArrival, type Arrival, type Channel } from "../api/intake";
import { Badge, CAME_IN_BY, RUN_META } from "../pages/FilesReceived";
import { fmtStamp } from "../utils/date";
import { InfoTip } from "./InfoTip";

type Tab = "all" | "attention" | "done";

// What each tab holds. Behind the ⓘ, not printed under the tabs.
const TAB_MEANING: Record<Tab, string> = {
  all: "Every file that has been run.",
  attention: "A person has to act: the run failed, could not run on its own, "
    + "or found exceptions waiting for a decision.",
  done: "The run completed — clean, or with exceptions. A file with exceptions "
    + "is in both Needs you and Finished.",
};

// How the file reached us, so a row says where it came from at a glance. The
// words are the Files table's own (CAME_IN_BY); only the icon is added here.
const CHANNEL_ICON: Record<Channel, React.ElementType> = {
  upload: MonitorUp, email: Mail, sftp: Server, api: Plug, cloud_folder: Cloud,
};
const CHANNEL_ORDER: Channel[] = ["upload", "email", "sftp", "api", "cloud_folder"];

/** Who, on that way in: the person for a portal upload, otherwise the sending
 *  address or the folder/key the file arrived on. */
function senderOf(a: Arrival): string | null {
  if (a.channel === "upload") return a.submitted_by_name;
  return a.claimed_sender ?? a.route_address;
}

function CameInBy({ a }: { a: Arrival }) {
  if (!a.channel) return <span className="faint" style={{ fontSize: 11.5 }}>Channel not recorded</span>;
  const Icon = CHANNEL_ICON[a.channel];
  const who = senderOf(a);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
      <span title={`Channel: ${CAME_IN_BY[a.channel].label}`} style={{
        display: "inline-flex", alignItems: "center", gap: 5, flex: "0 0 auto",
        padding: "2px 8px", borderRadius: 99, fontSize: 11, fontWeight: 600,
        background: "var(--p-primary-soft)", color: "var(--p-primary-h)",
      }}>
        <Icon size={12} strokeWidth={2} aria-hidden="true" />
        {CAME_IN_BY[a.channel].label}
      </span>
      {who && (
        <span className="faint" title={who} style={{ fontSize: 11.5, minWidth: 0, overflow: "hidden",
          textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{who}</span>)}
    </div>
  );
}

const DISMISSED_KEY = "ingestionPanel.dismissed";
// Fired in THIS tab when the hidden set changes, so the sidebar badge and the
// Files page button count what the panel shows. Other tabs hear `storage`.
const DISMISSED_EVENT = "ingestionPanel:dismissed";

function readDismissed(): Set<number> {
  try {
    const raw = window.localStorage.getItem(DISMISSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as number[]) : []);
  } catch { return new Set(); }
}
function writeDismissed(ids: Set<number>) {
  try { window.localStorage.setItem(DISMISSED_KEY, JSON.stringify([...ids].slice(-500))); }
  catch { /* private window — the panel just forgets on reload */ }
  window.dispatchEvent(new Event(DISMISSED_EVENT));
}

/** The runs hidden with ✕ or "Clear ingested", kept in step across every
 *  component that reads them. */
export function useDismissedRuns(): [Set<number>, (ids: Set<number>) => void] {
  const [dismissed, setDismissed] = useState<Set<number>>(readDismissed);
  useEffect(() => {
    const sync = () => setDismissed(readDismissed());
    window.addEventListener(DISMISSED_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(DISMISSED_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  const save = useCallback((ids: Set<number>) => { setDismissed(ids); writeDismissed(ids); }, []);
  return [dismissed, save];
}

/** Files that need somebody: a failed run, open exceptions, or one that could
 *  not be run automatically. */
export const needsYou = (a: Arrival) =>
  a.run_result === "failed" || a.run_result === "exceptions" || a.run_result === "not_run";

/** What the panel's "N need you" counts — hidden rows are not waiting on anyone. */
export const countNeedsYou = (rows: Arrival[], dismissed: Set<number>) =>
  rows.filter(a => needsYou(a) && !dismissed.has(a.arrival_id)).length;

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
  const [via, setVia] = useState<Channel | "all">("all");
  const [dismissed, saveDismissed] = useDismissedRuns();
  const [busyId, setBusyId] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { if (!open) setErr(null); }, [open]);

  const ranAll = useMemo(() => rows.filter(a => a.run_result && !dismissed.has(a.arrival_id)), [rows, dismissed]);
  // The header's "N need you" is about everything, whichever way it came in.
  const attentionAll = ranAll.filter(needsYou);
  // The tabs count within the way in picked below, so a number matches the list.
  const ran = via === "all" ? ranAll : ranAll.filter(a => a.channel === via);
  const attention = ran.filter(needsYou);
  // Finished = the run completed, whatever it found. Exceptions are both
  // finished AND needing you, so they sit in both tabs.
  const isFinished = (a: Arrival) => a.run_result === "ingested"
    || a.run_result === "exceptions" || a.run_result === "not_checked";
  const finished = ran.filter(isFinished);
  const shown = tab === "all" ? ran : tab === "attention" ? attention : finished;

  // Ways in present in the current tab, with how many files each brought.
  const inTab = (a: Arrival) => tab === "all" ? true : tab === "attention" ? needsYou(a) : isFinished(a);
  const viaTally = new Map<Channel, number>();
  for (const a of ranAll) if (a.channel && inTab(a)) viaTally.set(a.channel, (viaTally.get(a.channel) ?? 0) + 1);
  const viaCounts = CHANNEL_ORDER.filter(c => viaTally.has(c)).map(c => [c, viaTally.get(c)!] as const);

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
    saveDismissed(next);
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
        aria-hidden={!open} aria-label="Processing Status">
        <div className="drawer-h">
          <div style={{ minWidth: 0 }}>
            <h4>Processing Status</h4>
            <div className="ref" style={{ fontFamily: "inherit", fontSize: 12 }}>
              {attentionAll.length
                ? `${attentionAll.length} need${attentionAll.length === 1 ? "s" : ""} you`
                : "All clear"}
              {" · "}
              <button type="button" className="linkbtn" style={{ fontSize: 12 }}
                title="Hides clean runs here only — they stay on the Files screen"
                onClick={() => dismiss(ranAll.filter(a => a.run_result === "ingested").map(a => a.arrival_id))}>
                Clear ingested</button>
            </div>
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "10px 18px 8px" }}>
          {([["all", "All", ran.length], ["attention", "Needs you", attention.length],
             ["done", "Finished", finished.length],
          ] as const).map(([k, label, n]) => (
            <button key={k} type="button" className="chip" aria-pressed={tab === k}
              title={TAB_MEANING[k]}
              onClick={() => setTab(k)}>{label} <span className="n">{n}</span></button>
          ))}
          <InfoTip text={`Needs you — ${TAB_MEANING.attention}  Finished — ${TAB_MEANING.done}`} />
        </div>

        {/* Where the files came from: the web portal, email, SFTP, API… Picking
            one narrows the list and the tab counts above to that way in. */}
        <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap",
          padding: "0 18px 10px", borderBottom: "1px solid var(--p-border)" }}>
          <span className="faint" style={{ fontSize: 11, fontWeight: 600, marginRight: 2 }}>Channel</span>
          <button type="button" className="chip" aria-pressed={via === "all"}
            style={{ fontSize: 11.5, padding: "3px 9px" }}
            onClick={() => setVia("all")}>All</button>
          {viaCounts.map(([c, n]) => {
            const Icon = CHANNEL_ICON[c];
            return (
              <button key={c} type="button" className="chip" aria-pressed={via === c}
                style={{ fontSize: 11.5, padding: "3px 9px" }}
                title={`Show ${CAME_IN_BY[c].label} files only`}
                onClick={() => setVia(via === c ? "all" : c)}>
                <Icon size={12} strokeWidth={2} aria-hidden="true" />
                {CAME_IN_BY[c].label} <span className="n">{n}</span></button>);
          })}
        </div>

        <div className="drawer-b">
          {err && <div className="note crit" style={{ marginBottom: 12 }}>{err}</div>}
          {groups.length === 0 ? (
            <div style={{ textAlign: "center", padding: "32px 12px", color: "var(--p-muted)", fontSize: 12.5 }}>
              Nothing here.<br />
              {via === "all" ? "Files appear here once they have been run."
                : `No ${tab === "all" ? "files" : tab === "attention" ? "files needing attention" : "finished files"} received via ${CAME_IN_BY[via].label}.`}
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
                    <CameInBy a={a} />
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
