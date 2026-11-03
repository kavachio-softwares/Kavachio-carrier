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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Cloud, Mail, MonitorUp, Plug, Server } from "lucide-react";
import { rerunArrival, type Arrival, type Channel } from "../api/intake";
import { Badge, CAME_IN_BY, RUN_META } from "../pages/FilesReceived";
import { fmtStamp } from "../utils/date";
import { InfoTip } from "./InfoTip";
import { Pagination } from "./Pagination";

type Tab = "all" | "attention" | "done";

// What each tab holds. Behind the ⓘ, not printed under the tabs. The two
// narrower tabs do not overlap, so their counts add up to All's: a file is
// either waiting on somebody or it is not. ("Finished" used to take in files
// with exceptions as well, so every tab could show the same number.)
const TAB_MEANING: Record<Tab, string> = {
  all: "Every file that has been run.",
  attention: "A person has to act: the run failed, could not run on its own, "
    + "or found exceptions waiting for a decision.",
  done: "Processed, with nothing waiting on you: clean files, and files whose "
    + "contract checks could not be applied.",
};

// Files per page. The list is in memory already (the sidebar's one fetch), so
// the pages are cut here; filtering always comes first, then the page.
const PAGE_SIZE = 10;

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
  const [page, setPage] = useState(1);
  // A new filter starts from its first page.
  useEffect(() => { setPage(1); }, [tab, via]);
  const bodyRef = useRef<HTMLDivElement>(null);
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
  // Done = processed with nothing waiting on anyone — everything Needs You is
  // not. A file with exceptions is Needs You only, so All = Needs You + Done.
  const isDone = (a: Arrival) => !needsYou(a);
  const done = ran.filter(isDone);
  const shown = tab === "all" ? ran : tab === "attention" ? attention : done;

  // Ways in present in the current tab, with how many files each brought.
  const inTab = (a: Arrival) => tab === "all" ? true : tab === "attention" ? needsYou(a) : isDone(a);
  const viaTally = new Map<Channel, number>();
  for (const a of ranAll) if (a.channel && inTab(a)) viaTally.set(a.channel, (viaTally.get(a.channel) ?? 0) + 1);
  // The picked one stays listed even at nought, so the dropdown never shows a
  // value it has no option for.
  const viaCounts = CHANNEL_ORDER.filter(c => viaTally.has(c) || c === via)
    .map(c => [c, viaTally.get(c) ?? 0] as const);
  const inTabAll = ranAll.filter(inTab).length;
  // Only worth offering when files came in more than one way — with one, it
  // filters nothing. Kept while a pick is set, so it can always be undone.
  const channelsSeen = new Set(ranAll.map(a => a.channel).filter(Boolean));
  const showChannel = channelsSeen.size > 1 || via !== "all";

  // This page of the filtered list. Clamped, so a list that shrinks (a row
  // hidden with ✕) never strands you on an empty page.
  const pageCount = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const pageNow = Math.min(page, pageCount);
  const pageRows = shown.slice((pageNow - 1) * PAGE_SIZE, pageNow * PAGE_SIZE);
  function goToPage(n: number) {
    setPage(n);
    bodyRef.current?.scrollTo({ top: 0 });
  }

  // Grouped by contract, the way the reference groups by binder: the contract
  // is what the file was answerable to. Grouped within the page, after it is cut.
  const groups = useMemo(() => {
    const m = new Map<string, Arrival[]>();
    for (const a of pageRows) {
      const key = a.contract_name ?? a.program_name ?? "No contract recorded";
      m.set(key, [...(m.get(key) ?? []), a]);
    }
    return [...m.entries()];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown, pageNow]);

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
        aria-hidden={!open} aria-label="Ingestion Panel">
        <div className="drawer-h">
          <div style={{ minWidth: 0 }}>
            <h4>Ingestion Panel</h4>
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

        {/* The filters: one control per line, each the full width. The first
            splits the run files in two — what needs somebody and what does
            not — so its counts add up. The second, where the files came from
            (the web portal, email, SFTP, API…), narrows the list and those
            counts to one way in; it only appears when there is a choice. */}
        <div style={{ display: "grid", gap: 10, padding: "12px 20px",
          borderBottom: "1px solid var(--p-border)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div className="seg full" role="group" aria-label="Show">
              {([["all", "All", ran.length], ["attention", "Needs You", attention.length],
                 ["done", "Done", done.length],
              ] as const).map(([k, label, n]) => (
                <button key={k} type="button" className={tab === k ? "on" : ""}
                  aria-pressed={tab === k} title={TAB_MEANING[k]}
                  onClick={() => setTab(k)}>{label}<span className="n">{n}</span></button>
              ))}
            </div>
            <InfoTip text={`All — ${TAB_MEANING.all}\nNeeds You — ${TAB_MEANING.attention}\n`
              + `Done — ${TAB_MEANING.done}`} />
          </div>
          {showChannel && (
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontSize: 12.5, fontWeight: 500, color: "var(--p-muted)",
                whiteSpace: "nowrap" }}>Channel</span>
              <select className="fbar-select" aria-label="Channel" value={via}
                style={{ flex: 1, height: 34, paddingTop: 0, paddingBottom: 0 }}
                onChange={e => setVia(e.target.value as Channel | "all")}>
                <option value="all">All channels ({inTabAll})</option>
                {viaCounts.map(([c, n]) => (
                  <option key={c} value={c}>{CAME_IN_BY[c].label} ({n})</option>
                ))}
              </select>
            </div>
          )}
        </div>

        <div className="drawer-b" ref={bodyRef}>
          {err && <div className="note crit" style={{ marginBottom: 12 }}>{err}</div>}
          {groups.length === 0 ? (
            <div style={{ textAlign: "center", padding: "32px 12px", color: "var(--p-muted)", fontSize: 12.5 }}>
              Nothing here.<br />
              {tab === "attention" && via === "all" ? "Nothing is waiting on you."
                : tab === "done" && via === "all" ? "No files are done yet."
                : via === "all" ? "Files appear here once they have been run."
                : `No ${tab === "all" ? "files" : tab === "attention" ? "files needing attention" : "done files"} received via ${CAME_IN_BY[via].label}.`}
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

        {/* Outside the scrolling list, so it is always in reach. */}
        <Pagination page={pageNow} pageCount={pageCount} pageSize={PAGE_SIZE}
          totalItems={shown.length} onPageChange={goToPage} noun="files" />

        <div className="drawer-f">
          <span className="faint" style={{ fontSize: 11.5 }}>Every file that has been run, by contract.</span>
          <button type="button" className="btn" style={{ marginLeft: "auto" }} onClick={onClose}>Done</button>
        </div>
      </aside>
    </>
  );
}
