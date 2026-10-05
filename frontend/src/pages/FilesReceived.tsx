// Feature 10 — the Inbox tab of "Files".
//
// One queue for everything that has landed, however it landed. The "Came in by"
// column is the only trace of which door a file used, because after arrival it
// stops mattering.
//
// ONE table, not two. Splitting by outcome put held files under a heading
// reading "Turned away on arrival", which contradicted their own badge — and it
// meant "what came in today" had to be read in two places. The tiles do what the
// second table was doing, and the counts stay visible while filtered.
//
// Clicking a row opens a drawer with the checks. "Why was my file refused?" is
// the most common support question and the answer was one line of text in a
// cell, truncated by the column beside it.
//
// This used to be a page of its own at /intake/arrivals with a page head and a
// button across to How Files Arrive. It is a tab now (see Files.tsx) — when two
// screens each need a shortcut to the other, they are one screen.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  discardArrival, downloadArrival, isHeld, listArrivals, releaseArrival, rerunArrival,
  type Arrival, type Channel, type RunResult,
} from "../api/intake";
import { fmtStamp, periodLabel } from "../utils/date";
import { Pagination } from "../components/Pagination";
import { InfoTip } from "../components/InfoTip";
import SubmissionPanel from "../components/SubmissionPanel";

// Each door gets a name and a tone, as in the carrier-centric design: email is
// the one with a reply path so it reads as info, an upload was done by a person
// so it reads as ok, and the two machine doors are quiet greys.
export const CAME_IN_BY: Record<Channel, { label: string; tone: "ok" | "info" | "mut" }> = {
  upload: { label: "Manual Upload", tone: "ok" },
  email: { label: "Email", tone: "info" },
  sftp: { label: "SFTP", tone: "mut" },
  api: { label: "API", tone: "mut" },
  cloud_folder: { label: "Cloud Folder", tone: "mut" },
};

// What became of a file once it was run — the second half of "What happened".
// The door (went through / held / turned away) says whether it got IN; this says
// what the run did with it. One set of words, used by the tiles, the row badge
// and the drawer alike.
export const RUN_META: Record<RunResult, {
  label: string; tone: "ok" | "warn" | "crit" | "mut"; meaning: string;
}> = {
  ingested: { label: "Clean", tone: "ok",
    meaning: "All rows passed validation." },
  exceptions: { label: "With Exceptions", tone: "warn",
    meaning: "Valid rows loaded; exceptions awaiting review." },
  failed: { label: "Failed", tone: "crit",
    meaning: "Processing stopped. No data was saved — safe to retry." },
  not_checked: { label: "Not Validated", tone: "mut",
    meaning: "Processed, but contract checks could not be applied." },
  not_run: { label: "Not Processed", tone: "mut",
    meaning: "Could not be processed automatically — open for details." },
};

/** The Status filter. Five boxes at most on the page, so the processing
 *  results (Clean / With Exceptions / Failed) are filtered from here instead
 *  of from boxes of their own. Values are the same ones the boxes set. */
const STATUS_OPTIONS = [
  ["ok", "Accepted"], ["held", "On Hold"], ["away", "Rejected"],
  ["run:ingested", "Clean"], ["run:exceptions", "With Exceptions"], ["run:failed", "Failed"],
] as const;

/** What an accepted file that has no result yet is doing. */
function awaitingRun(a: Arrival): string {
  if (a.run_state === "running") return "Processing…";
  if (a.run_state === "pre_autorun") return "Received before auto-processing — process manually";
  return "Queued for processing";
}

// Every real way in, in the order the filter offers them. This is a fixed list
// rather than "whichever doors happen to appear in the rows", because a door
// with nothing through it yet is exactly the one you want to filter to in order
// to find that out — and leaving "Uploaded" out until a hand-uploaded file
// turned up made it look as though uploads were not counted here at all.
const WAY_IN_ORDER: Channel[] = ["upload", "email", "sftp", "api"];

// Rows drawn at once. Paged IN THE BROWSER, deliberately: every tile count, the
// filter options, the queue-first sort and the "new files have landed" check all
// read the whole fetched set, so moving the page to the server would quietly
// turn each of them into a fact about ten rows. This changes what is DRAWN and
// nothing else.
const PAGE_SIZE = 10;   // same page size as the other lists (Contracts, Parties…)

// The checks in the order land_file() runs them. That order is the whole point:
// it stops at the FIRST failure, so a file that fails check three has passed one
// and two and the rest never ran at all.
//
// Everything above "Can we open it?" is settled WITHOUT opening the file — that
// is deliberate (12.1), because opening a spreadsheet is the one step that runs
// a stranger's choices through our parsers.
const CHECKS: [string, string][] = [
  ["File size",
   "The file must be within the size limit and not empty."],
  ["File type",
   "Only Excel, CSV, XML or JSON files are accepted."],
  ["File safety",
   "The file is checked for macros and abnormal compression."],
  ["Sender verification",
   "The sender must be a broker on one of your programmes."],
  ["Programme, contract & period",
   "The file must say which reporting period it is for — and which programme and contract, when the sender has more than one."],
  ["Duplicate check",
   "Previously received files are not loaded again, preventing double-counted premium."],
  ["Security scan",
   "External files are scanned for malware before opening."],
  ["File readability",
   "Incomplete or password-protected files cannot be opened."],
  ["Data present",
   "The file must contain at least one data row."],
  ["Required columns",
   "The file must include the columns the programme reports on."],
  ["Active contract",
   "An active contract is required to validate the file."],
];

// Which check produced this reason. The backend writes the sentence, not the
// index, so this reads it back — matched against the phrases in
// intake_service.py / intake_safety.py / intake_required_fields.py rather than
// the whole string, so wording can be tuned without silently breaking the
// drawer. -1 when nothing matches, and then the drawer shows the reason on its
// own instead of guessing.
function failedCheck(reason: string | null): number {
  if (!reason) return -1;
  const r = reason.toLowerCase();
  if (/we can accept files up to|nothing arrived at all/.test(r)) return 0;
  if (/not a spreadsheet|not an excel workbook|we can read /.test(r)) return 1;
  if (/expands to|internal parts|contains macros/.test(r)) return 2;
  if (/recognise the sender|not linked to a broker|been switched off/.test(r)) return 3;
  if (/no reporting period|not a reporting period|has not ended yet|which programme\?|which contract\?|contract is not one/.test(r)) return 4;
  if (/same file we already loaded/.test(r)) return 5;
  if (/security scan/.test(r)) return 6;
  if (/could not open it/.test(r)) return 7;
  if (/no rows in it/.test(r)) return 8;
  if (/columns this programme reports on|missing \d+ column/.test(r)) return 9;
  if (/no (live|active) contract/.test(r)) return 10;
  return -1;
}

// "" is every row. The three outcome values are three of the tiles, and a tile
// is a toggle: pressing the one you are already in clears it.
type Filter = "" | "ok" | "held" | "away" | `run:${RunResult}`;
type Sort = "queue" | "new" | "old";
type Range = "all" | "today" | "30" | "90" | "month";

/** What the Received filter keeps, and the words the Total tile says it. */
const RANGE_FOOT: Record<Range, string> = {
  all: "All time", today: "Since midnight", month: "This month",
  "30": "Last 30 days", "90": "Last 90 days",
};

function inRange(a: Arrival, range: Range): boolean {
  if (range === "all") return true;
  if (!a.received_at) return false;
  const t = new Date(a.received_at);
  if (range === "today") return t.toDateString() === new Date().toDateString();
  const cutoff = range === "30" ? Date.now() - 30 * 86400000
    : range === "90" ? Date.now() - 90 * 86400000
    : new Date(new Date().getFullYear(), new Date().getMonth(), 1).getTime();
  return t.getTime() >= cutoff;
}

export function state(a: Arrival): "ok" | "held" | "away" {
  return a.outcome === "accepted" ? "ok" : isHeld(a) ? "held" : "away";
}

/** Days since it landed. Only ever shown on a file still waiting on somebody. */
/** Each submission once, as its newest file that COUNTS. A file somebody
 *  discarded (a duplicate, say) never stands for the submission — it stays in
 *  the row's History — and one still waiting on a decision keeps a row of its
 *  own beside it: the queue must never hide work. A submission whose every
 *  file was discarded still shows, as its newest, so it never vanishes. */
function latestVersions(rows: Arrival[]): Arrival[] {
  const newer = (a: Arrival, b?: Arrival) => !b || (a.version_no ?? 0) > (b.version_no ?? 0);
  const counting = new Map<string, Arrival>();
  const any = new Map<string, Arrival>();
  for (const a of rows) {
    if (!a.submission_ref) continue;
    if (newer(a, any.get(a.submission_ref))) any.set(a.submission_ref, a);
    if (a.resolution === "discarded" || isWaiting(a)) continue;
    if (newer(a, counting.get(a.submission_ref))) counting.set(a.submission_ref, a);
  }
  return rows.filter(a => !a.submission_ref || isWaiting(a)
    || (counting.get(a.submission_ref) ?? any.get(a.submission_ref)) === a);
}

function waitDays(a: Arrival): number {
  if (!a.received_at) return 0;
  return Math.floor((Date.now() - new Date(a.received_at).getTime()) / 86400000);
}

/** Held AND undecided — the only rows that are actually a queue. */
function isWaiting(a: Arrival): boolean {
  return state(a) === "held" && !a.resolution;
}

export function Badge({ tone, children }:
  { tone: "ok" | "warn" | "crit" | "mut" | "info"; children: React.ReactNode }) {
  return <span className={`badge b-${tone}`}><span className="d" />{children}</span>;
}

function bytes(n: number | null): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** Rows, not kilobytes — a bordereau is measured in rows. `null` is "we could
 *  not open it", which is why it is a dash and not a zero. */
function rowsOf(n: number | null): string {
  return n == null ? "—" : n.toLocaleString();
}

/** "1 file" / "3 files". */
const nFiles = (n: number) => `${n} file${n === 1 ? "" : "s"}`;

/** The action at the end of the row. Every one of them opens the drawer, so
 *  the word says what you will be doing there rather than naming the control:
 *  a held file needs a decision, a refused one needs an explanation. */
function actionLabel(a: Arrival): string {
  const st = state(a);
  if (a.resolution) return "View →";       // already decided; nothing to do
  return st === "held" ? "Review →" : st === "away" ? "View Reason →" : "View →";
}

// A few words for each check a file can stop at, in the order of CHECKS. The
// backend's full sentence is kept for the tooltip and the drawer.
const SHORT_REASON = [
  "Invalid file size", "Unsupported file type", "Unsafe file content",
  "Unrecognised sender", "No reporting period", "Duplicate file", "Failed security scan",
  "Unable to open file", "No data rows", "Required columns missing", "No active contract",
];

/** The line under the filename: what happened, in a few words. The full
 *  sentence is the line's tooltip, and is in the drawer. */
function subline(a: Arrival): string {
  // A decision is the most recent true thing about the file, so it wins over
  // the reason that made somebody decide.
  if (a.resolution === "discarded") return "Discarded";
  if (a.outcome === "accepted") {
    // Once it has been run, the run is the news — and a failed run says why.
    if (a.run_state === "failed" || a.run_state === "not_run") {
      const first = (a.run_error ?? "").split(/(?<=\.)\s/)[0];
      return first.length > 60 ? first.slice(0, 57).trimEnd() + "…" : first;
    }
    if (a.run_result) return a.resolution === "released" ? "Manually released" : "";
    return a.resolution === "released"
      ? `Manually released — ${awaitingRun(a).toLowerCase()}` : awaitingRun(a);
  }
  const reason = fullReason(a);
  const i = failedCheck(reason);
  if (i === 9) {
    const n = reason.match(/missing (\d+) column/i)?.[1];
    if (n) return `${n} required columns missing`;
    if (/none of the columns/i.test(reason)) return "Unrecognised file layout";
  }
  if (i === 4) {
    // One step answers three questions; say which one was left open.
    if (/which programme\?/i.test(reason)) return "Which programme?";
    if (/which contract\?|contract is not one/i.test(reason)) return "Which contract?";
    if (/has not ended yet/i.test(reason)) return "Period not over yet";
    if (/is not a reporting period/i.test(reason)) return "Unknown period";
  }
  if (i >= 0) return SHORT_REASON[i];
  // Nothing matched: the first sentence, cut short.
  const first = reason.split(/(?<=\.)\s/)[0];
  return first.length > 60 ? first.slice(0, 57).trimEnd() + "…" : first;
}

// The backend writes held reasons as "Held — the file is …". The badge already
// says On Hold, so the prefix goes and the sentence starts with a capital.
const fullReason = (a: Arrival) => {
  const r = (a.turned_away_reason ?? "").replace(/^Held — /, "");
  return r.charAt(0).toUpperCase() + r.slice(1);
};

export default function InboxTab({ onWaitingCount, onRows, active, refreshKey, liveTick = 0 }: {
  /** Every row fetched, reported up so the Ingestion panel reads the same list
   *  rather than fetching its own. */
  onRows?: (rows: Arrival[]) => void;
  /** Reported up so a caller can carry the count. */
  onWaitingCount?: (n: number) => void;
  /** False while the Ingestion options panel is showing. Both panes stay mounted so the
   *  tab badge stays live, but only the visible one fetches changes. */
  active: boolean;
  /** Bumped by Refresh in the page head. */
  refreshKey: number;
  /** Bumped when the server says files changed (Files.tsx holds the feed). */
  liveTick?: number;
}) {
  const [rows, setRows] = useState<Arrival[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [filter, setFilter] = useState<Filter>("");
  const [sort, setSort] = useState<Sort>("queue");
  const [q, setQ] = useState("");
  const [range, setRange] = useState<Range>("all");
  const [fChannel, setFChannel] = useState<string>("");
  const [fBroker, setFBroker] = useState<string>("");
  const [fProgramme, setFProgramme] = useState<string>("");
  const [open, setOpen] = useState<Arrival | null>(null);
  const [page, setPage] = useState(1);

  // Ticked rows, by arrival_id. Cleared whenever the visible set changes, so a
  // selection can never outlive the rows it was made on.
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [bulkErr, setBulkErr] = useState<string | null>(null);

  // Arrivals fetched by the background poll but NOT yet shown. Rows are never
  // reordered under the cursor — the bar is the invitation to take them.
  const [pending, setPending] = useState<Arrival[] | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setRows((await listArrivals()).rows);
      setPending(null); setErr(null);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? e?.message ?? "Failed to load.");
    } finally { setBusy(false); }
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);

  // Files land by SFTP, email and API at any moment. The server says so the
  // instant one commits — Files.tsx holds that feed and bumps `liveTick` — which
  // replaced asking for the whole list every 60 seconds just in case.
  //
  // What changed is fetched into `pending`, never applied under the cursor.
  // While the tab is hidden, a row is open or a load is running, the change is
  // remembered and fetched the moment that ends: there is no next tick to catch
  // it any more.
  const seen = useRef<number>(0);
  useEffect(() => {
    if (rows) seen.current = rows.length ? Math.max(...rows.map(r => r.arrival_id)) : 0;
  }, [rows]);
  const gate = useRef({ active, busy, open: !!open });
  gate.current = { active, busy, open: !!open };
  const stale = useRef(false);
  const lookForNew = useCallback(async () => {
    const g = gate.current;
    if (!g.active || g.busy || g.open || document.hidden) { stale.current = true; return; }
    stale.current = false;
    try {
      const fresh = (await listArrivals()).rows;
      const newest = fresh.length ? Math.max(...fresh.map(r => r.arrival_id)) : 0;
      if (newest > seen.current) setPending(fresh);
    } catch { /* not worth an error bar; the next change or Refresh tries again */ }
  }, []);
  useEffect(() => { if (liveTick) lookForNew(); }, [liveTick, lookForNew]);
  useEffect(() => {
    if (stale.current) lookForNew();
  }, [active, busy, open, lookForNew]);
  useEffect(() => {
    const onVisible = () => { if (!document.hidden && stale.current) lookForNew(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [lookForNew]);

  // One row per submission: a corrected file is the next version of the same
  // submission, so it replaces the one before it here. The earlier versions
  // are listed in the file panel.
  const all = useMemo(() => latestVersions(rows ?? []), [rows]);
  useEffect(() => { if (rows) onRows?.(rows); }, [rows, onRows]);

  // The tiles count over the Received period, so "Today" in the filter is what
  // the old Received Today tile was, and Total Files is every file in it.
  const counts = useMemo(() => {
    const ranged = all.filter(a => inRange(a, range));
    return {
      all: all.length,
      total: ranged.length,
      ok: ranged.filter(a => state(a) === "ok").length,
      away: ranged.filter(a => state(a) === "away").length,
      // 12.3 — the queue is HELD AND UNRESOLVED. A held file somebody has
      // already decided is finished, and counting it keeps a tile amber for
      // work that is done.
      waiting: ranged.filter(isWaiting).length,
      // The oldest thing still waiting. A queue nobody opens is the same as no
      // queue, and one number that says "eleven days" is what makes somebody
      // open it.
      oldestWaitDays: Math.max(0, ...ranged.filter(isWaiting).map(waitDays)),
    };
  }, [all, range]);

  // The badge elsewhere is the whole queue, whatever period is picked here.
  const waitingAll = useMemo(() => all.filter(isWaiting).length, [all]);
  useEffect(() => { onWaitingCount?.(waitingAll); }, [waitingAll, onWaitingCount]);

  // Brokers and programmes come from the rows — those are open sets, and a
  // filter naming a broker who has never sent anything is just noise. The ways
  // in are a closed set, so all of them are offered.
  const options = useMemo(() => ({
    channels: [...new Set([...WAY_IN_ORDER,
      ...all.map(a => a.channel).filter((c): c is Channel => !!c)])],
    brokers: [...new Set(all.map(a => a.broker_name).filter((b): b is string => !!b))].sort(),
    programmes: [...new Set(all.map(a => a.program_name)
      .filter((p): p is string => !!p))].sort(),
  }), [all]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();

    const list = all.filter(a => {
      if (filter === "held") {
        // The tile above counts held AND UNDECIDED, so the filter has to mean
        // the same thing — a tile that is a control must hand you the rows it
        // counted. A held file somebody has already dealt with is reachable
        // with the filter cleared.
        if (!isWaiting(a)) return false;
      } else if (filter.startsWith("run:")) {
        if (a.run_result !== filter.slice(4)) return false;
      } else if (filter && state(a) !== filter) return false;
      if (!inRange(a, range)) return false;
      if (fChannel && a.channel !== fChannel) return false;
      if (fBroker && a.broker_name !== fBroker) return false;
      if (fProgramme && a.program_name !== fProgramme) return false;
      if (needle) {
        const hay = `${a.filename} ${a.broker_name ?? ""} ${a.claimed_sender ?? ""} ${a.contract_name ?? ""}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });

    // Newest-first buried the queue: the tile said "oldest has waited 6 days"
    // and put that exact file at the bottom of the table. The default now leads
    // with what needs a decision, oldest of those first, and everything already
    // dealt with falls in behind it newest-first.
    const at = (a: Arrival) => a.received_at ? new Date(a.received_at).getTime() : 0;
    return list.sort((a, b) => {
      if (sort === "new") return at(b) - at(a);
      if (sort === "old") return at(a) - at(b);
      const aw = isWaiting(a) ? 0 : 1, bw = isWaiting(b) ? 0 : 1;
      if (aw !== bw) return aw - bw;
      return aw === 0 ? at(a) - at(b) : at(b) - at(a);
    });
  }, [all, filter, sort, q, range, fChannel, fBroker, fProgramme]);

  // What the table actually draws. `shown` stays the whole filtered set, so the
  // count beside the sort, the select-all tick and the bulk actions all go on
  // meaning what they meant.
  const pageCount = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const current = Math.min(page, pageCount);
  const paged = shown.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE);

  // Re-sorting reorders the whole queue, so page 3 of the old order is not page
  // 3 of the new one.
  useEffect(() => { setPage(1); }, [sort]);

  // A tick belongs to the row it was put on. The moment the visible set moves
  // under it, it is gone.
  useEffect(() => {
    setPicked(new Set()); setBulkErr(null); setPage(1);
  }, [filter, q, range, fChannel, fBroker, fProgramme]);

  const filtered = !!filter || !!q.trim() || range !== "all"
    || !!fChannel || !!fBroker || !!fProgramme;

  function clearFilters() {
    setFilter(""); setQ(""); setRange("all");
    setFChannel(""); setFBroker(""); setFProgramme("");
  }

  // Only an undecided row can be decided, and only a HELD one can be released:
  // a turned-away file is one we could not read, and releasing it would hand
  // processing something broken.
  const pickedRows = shown.filter(a => picked.has(a.arrival_id));
  const canRelease = pickedRows.filter(a => state(a) === "held" && !a.resolution);
  const canDiscard = pickedRows.filter(a => state(a) !== "ok" && !a.resolution);

  async function decideMany(what: "release" | "discard") {
    const targets = what === "release" ? canRelease : canDiscard;
    if (targets.length === 0) return;
    setBusy(true); setBulkErr(null);
    const call = what === "release" ? releaseArrival : discardArrival;
    const note = targets.length > 1
      ? `Bulk action with ${targets.length - 1} other file${targets.length === 2 ? "" : "s"}`
      : undefined;
    const results = await Promise.allSettled(
      targets.map(a => call(a.arrival_id, note)));
    const failed = results.filter(r => r.status === "rejected").length;
    setPicked(new Set());
    // Reload either way: some of them may well have gone through.
    await load();
    if (failed > 0) {
      setBulkErr(failed === targets.length
        ? "The action could not be completed. No files were changed."
        : `${nFiles(targets.length - failed)} updated; ${failed} could not be updated and `
          + `${failed === 1 ? "remains" : "remain"} in the list.`);
    }
  }

  const toggleAll = (on: boolean) =>
    setPicked(on ? new Set(shown.map(a => a.arrival_id)) : new Set());

  return (
    <>
      {err && <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>}

      {/* Four tiles, and they ARE the filter. All four count over the Received
          period picked below (Today / This Month / …), so the period lives in
          one place instead of as tiles of its own. */}
      <div className="tiles" style={{ marginBottom: 18 }}>
        {/* Every file in the period — accepted, on hold and rejected. Pressing
            it clears the outcome filter rather than toggling one. */}
        <button type="button" className="tile" aria-pressed={false}
          onClick={() => setFilter("")} title="Show every file in this period">
          <div className="k">Total Files</div>
          <div className="v">{rows ? counts.total : "—"}</div>
          <div className="foot">{RANGE_FOOT[range]}</div>
        </button>

        {([
          ["ok", "Accepted", counts.ok, "Passed all intake checks", "",
            "Show accepted files only"],
          ["held", "On Hold", counts.waiting,
            counts.waiting > 0 && counts.oldestWaitDays > 0
              ? `Oldest: ${counts.oldestWaitDays} day${counts.oldestWaitDays === 1 ? "" : "s"}`
              : "Awaiting your decision",
            counts.waiting > 0 ? "warnl" : "", "Show files on hold only"],
          ["away", "Rejected", counts.away, "Failed intake checks",
            counts.away > 0 ? "alert" : "", "Show rejected files only"],
        ] as const).map(([f, label, n, foot, tone, hint]) => {
          const on = filter === f;
          const colour = tone === "warnl" ? "var(--p-warn)"
            : tone === "alert" ? "var(--p-crit)" : undefined;
          return (
            <button type="button" key={f} className={`tile ${tone}`} aria-pressed={on}
              onClick={() => setFilter(on ? "" : f)}
              title={on ? "Filter applied — click to clear" : hint}>
              <div className="k">{label}{on && <span className="on">Filtered</span>}</div>
              <div className="v" style={colour ? { color: colour } : undefined}>
                {rows ? n : "—"}</div>
              <div className="foot">{foot}</div>
            </button>);
        })}
      </div>

      <div className="filters">
        <label className="searchbox">
          <svg className="si" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" aria-hidden="true">
            <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input type="search" value={q} onChange={e => setQ(e.target.value)}
            placeholder="Search by file, broker, sender or contract…" aria-label="Search files" />
        </label>
        <span className="spacer">
          <select className="sel" value={range} aria-label="Received"
            onChange={e => setRange(e.target.value as Range)}>
            <option value="all">All Time</option>
            <option value="today">Today</option>
            <option value="month">This Month</option>
            <option value="30">Last 30 Days</option>
            <option value="90">Last 90 Days</option>
          </select>
          <select className="sel" aria-label="Status"
            value={STATUS_OPTIONS.some(([v]) => v === filter) ? filter : ""}
            onChange={e => setFilter(e.target.value as Filter)}>
            <option value="">All Statuses</option>
            {STATUS_OPTIONS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
          <select className="sel" value={fChannel} onChange={e => setFChannel(e.target.value)}
            aria-label="Channel">
            <option value="">All Channels</option>
            {options.channels.map(c => (
              <option key={c} value={c}>{CAME_IN_BY[c].label}</option>))}
          </select>
          <select className="sel" value={fBroker} onChange={e => setFBroker(e.target.value)}
            aria-label="Broker">
            <option value="">All Brokers</option>
            {options.brokers.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
          <select className="sel" value={fProgramme} aria-label="Programme"
            onChange={e => setFProgramme(e.target.value)}>
            <option value="">All Programmes</option>
            {options.programmes.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          {filtered && (
            <button type="button" className="linkbtn" onClick={clearFilters}>
              Clear Filters</button>)}
        </span>
      </div>

      <div className="card">
        {/* Something landed while you were reading. Taking it is a click, so
            nothing moves under the cursor mid-decision. */}
        {pending && (
          <div className="newbar">
            <b>{pending.length - all.length > 0
              ? `${pending.length - all.length} new file${pending.length - all.length === 1 ? "" : "s"} received`
              : "Updates available"}</b>
            <button type="button" className="linkbtn"
              onClick={() => { setRows(pending); setPending(null); }}>Refresh List →</button>
          </div>)}

        {picked.size > 0 && (
          <div className="bulkbar">
            <span>{picked.size} selected</span>
            {canDiscard.length !== picked.size && (
              <span style={{ fontWeight: 500, color: "var(--p-muted)" }}>
                {picked.size - canDiscard.length} selected{" "}
                {picked.size - canDiscard.length === 1 ? "file requires" : "files require"} no action
              </span>)}
            <span className="sp">
              <button type="button" className="btn sm pri" disabled={busy || canRelease.length === 0}
                onClick={() => decideMany("release")}
                title="Only files on hold can be released">
                {busy ? "Processing…" : `Release ${canRelease.length}`}</button>
              <button type="button" className="btn sm" disabled={busy || canDiscard.length === 0}
                onClick={() => decideMany("discard")}>Discard {canDiscard.length}</button>
              <button type="button" className="btn sm" onClick={() => setPicked(new Set())}>
                Cancel</button>
            </span>
          </div>)}

        {bulkErr && <div className="note crit" style={{ margin: 0, borderRadius: 0 }}>{bulkErr}</div>}

        <div className="card-h">
          <h3>Received Files</h3>
          {/* Counts stay visible while filtered, so nothing is ever hidden
              without saying how much. */}
          <span className="right faint" style={{
            fontSize: 12, display: "flex", alignItems: "center", gap: 12,
          }}>
            <select className="sel" value={sort} aria-label="Sort order"
              onChange={e => setSort(e.target.value as Sort)}>
              <option value="queue">On Hold First</option>
              <option value="new">Newest First</option>
              <option value="old">Oldest First</option>
            </select>
            {!rows ? "" : filtered
              ? `${shown.length} of ${nFiles(counts.all)}`
              : nFiles(counts.all)}
          </span>
        </div>

        {rows && counts.all === 0 ? (
          <div style={{ textAlign: "center", padding: "36px 20px", color: "var(--p-muted)" }}>
            <b style={{ display: "block", color: "var(--p-ink)", fontSize: 14, marginBottom: 4 }}>
              No files received yet</b>
            {/* The long explanation of what this screen is lives HERE, in the
                one state where somebody genuinely does not know — not in a grey
                slab above the title on every visit. */}
            <p style={{ margin: "0 auto", fontSize: 12.5, maxWidth: 430, lineHeight: 1.6 }}>
              Files submitted by email, manual upload, SFTP or API appear here in order of
              receipt, and each passes the same intake checks. To get started, set up a
              channel for a broker under <b>Ingestion Channels</b>.
            </p>
          </div>
        ) : rows && shown.length === 0 ? (
          <div style={{ textAlign: "center", padding: "36px 20px", color: "var(--p-muted)" }}>
            <b style={{ display: "block", color: "var(--p-ink)", fontSize: 14, marginBottom: 4 }}>
              No files match the selected filters</b>
            <p style={{ margin: 0, fontSize: 12.5 }}>
              <span className="linkish" onClick={clearFilters}>Clear filters</span> to view
              all {nFiles(counts.all)}.
            </p>
          </div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="cbcell">
                    <input type="checkbox" aria-label="Select every row shown"
                      checked={shown.length > 0 && picked.size === shown.length}
                      onChange={e => toggleAll(e.target.checked)} />
                  </th>
                  <th>File Name</th><th>Channel</th><th>Broker</th><th>Programme</th>
                  <th title="Contract used for validation">Contract</th>
                  <th title="The reporting period this file is for">Reporting Period</th>
                  <th>Rows</th><th>Status</th><th>Received</th><th />
                </tr>
              </thead>
              <tbody>
                {paged.map(a => {
                  const st = state(a);
                  const sub = subline(a);
                  const on = picked.has(a.arrival_id);
                  return (
                    <tr key={a.arrival_id} className={`arow ${st === "held" && a.resolution ? "decided" : st}${on ? " sel" : ""}`} tabIndex={0}
                      onClick={() => setOpen(a)}
                      onKeyDown={e => { if (e.key === "Enter") setOpen(a); }}>
                      <td className="cbcell" onClick={e => e.stopPropagation()}>
                        <input type="checkbox" checked={on}
                          aria-label={`Select ${a.filename}`}
                          onChange={e => setPicked(prev => {
                            const next = new Set(prev);
                            if (e.target.checked) next.add(a.arrival_id);
                            else next.delete(a.arrival_id);
                            return next;
                          })} />
                      </td>
                      <td>
                        <div className="fname">
                          {a.filename}
                          {(a.version_no ?? 0) > 1 &&
                            <span className="vchip">Version {a.version_no}</span>}
                          {/* The wait, on the row. Without it the number was
                              only ever on the tile, describing a file the sort
                              order then hid at the bottom of the table. */}
                          {isWaiting(a) && waitDays(a) > 0 &&
                            <span className="aged">
                              Pending {waitDays(a)} day{waitDays(a) === 1 ? "" : "s"}</span>}
                        </div>
                        {/* A few words; the whole sentence is the tooltip
                            and is in the drawer. */}
                        {sub && <div className="sub"
                          title={(a.outcome === "accepted" ? a.run_error : fullReason(a)) || undefined}>
                          {sub}</div>}
                      </td>
                      {/* The way in is a badge, not plain text: it is the one
                          thing on the row that is a fixed set of five, and it
                          is read by shape rather than word. */}
                      <td>{a.channel
                        ? <Badge tone={CAME_IN_BY[a.channel].tone}>
                            {CAME_IN_BY[a.channel].label}</Badge>
                        : <span className="muted">—</span>}</td>
                      <td>{a.broker_name ?? <span className="muted">Unidentified sender</span>}</td>
                      <td className="muted">
                        {a.program_name ?? <span className="faint">—</span>}</td>
                      <td>{a.contract_name ?? <span className="faint">—</span>}</td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        {a.reporting_period ? periodLabel(a.reporting_period)
                          : <span className="faint">—</span>}</td>
                      <td className="mono">{rowsOf(a.row_count)}</td>
                      <td>
                        {/* Two steps: did it get in, then what the run did. */}
                        {st === "ok" ? <Badge tone="ok">Accepted</Badge>
                          : st === "held" ? (a.resolution === "discarded"
                            ? <Badge tone="mut">Discarded</Badge>
                            : <Badge tone="warn">On Hold</Badge>)
                          : <Badge tone="crit">Rejected</Badge>}
                        {st === "ok" && (
                          <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 4 }}>
                            <span className="faint" aria-hidden="true">↳</span>
                            {a.run_result
                              ? <span title={RUN_META[a.run_result].meaning}>
                                  <Badge tone={RUN_META[a.run_result].tone}>
                                    {RUN_META[a.run_result].label}</Badge></span>
                              : <span className="faint" style={{ fontSize: 11 }}>{awaitingRun(a)}</span>}
                          </div>)}
                      </td>
                      <td className="mono faint" style={{ fontSize: 12 }}>
                        {fmtStamp(a.received_at)}</td>
                      {/* Every row ends in the one thing you would do next.
                          All of them open the drawer — the word just says
                          what you will find when you get there. */}
                      <td><span className="linkish">{actionLabel(a)}</span></td>
                    </tr>);
                })}
              </tbody>
            </table>
          </div>
        )}
        {/* Always drawn, like every other paginated list — the "X–Y of Z"
            bar is how the other screens read, even on a single page. */}
        <Pagination
          page={current} pageCount={pageCount} pageSize={PAGE_SIZE}
          totalItems={shown.length} onPageChange={setPage} noun="files" />
        <div className="note" style={{
          margin: 0, border: 0, borderTop: "1px solid var(--p-border)", borderRadius: 0,
        }}>
          <b>All files are retained.</b> Rejected files are stored exactly as received, and
          files on hold remain available until reviewed. Select a row to view its intake
          checks, or select several to act on them together.
        </div>
      </div>

      <ArrivalDrawer arrival={open} onClose={() => setOpen(null)} onResolved={load}
        onOpenArrival={id => {
          const r = (rows ?? []).find(x => x.arrival_id === id);
          if (r) setOpen(r);
        }} />
    </>
  );
}

// ── the row drawer ──────────────────────────────────────────────────────────
// The decision sits directly beneath the evidence for it, which is the whole
// reason this is a drawer and not a tooltip.
/** One headline and one sentence: what happened to this file, in the words
 *  the reader needs first. Everything else in the drawer is supporting detail. */
function verdict(a: Arrival, st: "ok" | "held" | "away"):
    { tone: "ok" | "warn" | "crit" | ""; title: string; text: string } {
  if (st === "away") return { tone: "crit", title: "Rejected at intake",
    text: fullReason(a) || "No reason recorded." };
  // The headline alone: the reason is the failed step under Intake Checks,
  // and the buttons below say what can be done about it.
  if (st === "held") return { tone: "warn",
    title: a.resolution === "discarded" ? "Discarded" : "On hold — needs your decision",
    text: "" };
  const n = a.run_exception_count ?? 0;
  switch (a.run_result) {
    case "exceptions": return { tone: "warn",
      title: `${n.toLocaleString()} exception${n === 1 ? "" : "s"} found`,
      text: "Some rows don't meet the contract rules. Open View Results to review them." };
    case "ingested": return { tone: "ok", title: "All rows passed",
      text: "No exceptions were found in this file." };
    case "failed": return { tone: "crit", title: "Processing failed",
      text: a.run_error || RUN_META.failed.meaning };
    case "not_run": return { tone: "warn", title: "Not processed",
      text: a.run_error || RUN_META.not_run.meaning };
    case "not_checked": return { tone: "", title: "Not validated",
      text: RUN_META.not_checked.meaning };
    default: return { tone: "", title: awaitingRun(a),
      text: "The result will appear here once processing finishes." };
  }
}

function ArrivalDrawer({ arrival, onClose, onResolved, onOpenArrival }: {
  arrival: Arrival | null; onClose: () => void; onResolved: () => void;
  /** Open another version of the same submission. */
  onOpenArrival?: (arrivalId: number) => void;
}) {
  const st = arrival ? state(arrival) : "ok";
  const failed = arrival && st !== "ok" ? failedCheck(arrival.turned_away_reason) : -1;
  const nav = useNavigate();
  const run = arrival?.run_result ? RUN_META[arrival.run_result] : null;

  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  // The checklist and the minor facts start folded away: the verdict at the
  // top is what a first-time reader needs, the rest is one click down.
  const [showMore, setShowMore] = useState(false);

  // A fresh drawer must not inherit the last file's half-typed note, its
  // error or its open sections — they belong to the row that is gone.
  useEffect(() => {
    setNote(""); setErr(null); setBusy(false); setShowMore(false);
  }, [arrival?.arrival_id]);

  // Escape closes the drawer, and the scrim behind it is clickable — the two
  // ways out people try first.
  useEffect(() => {
    if (!arrival) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [arrival, onClose]);

  // The one place a decision is made. Both actions are the same shape: send it,
  // reload the list so the row shows resolved, and close — the file has been
  // dealt with, and leaving the drawer open invites clicking again.
  // Back into the auto-run queue. The list reloads so the row says "Waiting to
  // be run", and the result arrives on its own a few seconds later.
  async function runAgain() {
    if (!arrival) return;
    setBusy(true); setErr(null);
    try {
      await rerunArrival(arrival.arrival_id);
      onResolved();
      onClose();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setErr(detail || "The action could not be completed. No changes were made.");
    } finally {
      setBusy(false);
    }
  }

  async function decide(what: "release" | "discard") {
    if (!arrival) return;
    setBusy(true); setErr(null);
    try {
      if (what === "release") await releaseArrival(arrival.arrival_id, note);
      else await discardArrival(arrival.arrival_id, note);
      onResolved();
      onClose();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setErr(detail || "The action could not be completed. No changes were made.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className={`scrim${arrival ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer${arrival ? " on" : ""}`} role="dialog" aria-modal="true"
        aria-hidden={!arrival} aria-label={arrival?.filename ?? "File detail"}>
        {arrival && (<>
          <div className="drawer-h">
            <div style={{ minWidth: 0 }}>
              <h4>{arrival.filename}</h4>
            </div>
            <button type="button" className="closeb" aria-label="Close"
              onClick={onClose}>×</button>
          </div>

          <div className="drawer-b">
            {/* The verdict, first and alone: one headline and one sentence. */}
            {(() => {
              const v = verdict(arrival, st);
              return (
                <div className={`note ${v.tone}`} style={{ marginTop: 0 }}>
                  <b style={{ display: "block", fontSize: 14, marginBottom: v.text ? 3 : 0 }}>{v.title}</b>
                  {v.text}
                  {arrival.resolution && (
                    <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
                      {arrival.resolution === "released" ? "Released" : "Discarded"}
                      {arrival.resolved_by_name ? ` by ${arrival.resolved_by_name}` : ""}
                      {arrival.resolved_at ? ` on ${fmtStamp(arrival.resolved_at)}` : ""}
                      {arrival.resolution_note ? ` · “${arrival.resolution_note}”` : ""}
                    </div>)}
                </div>);
            })()}

            {/* Four facts a reader actually looks for. */}
            <div style={{ marginTop: 14 }}>
              <div className="kv"><span className="k">Programme</span>
                <span className="v">{arrival.program_name ?? "Not linked to a programme"}</span></div>
              <div className="kv"><span className="k">Broker</span>
                <span className="v">{arrival.broker_name ?? "Unidentified sender"}</span></div>
              {arrival.contract_name && (
                <div className="kv"><span className="k">Contract</span>
                  <span className="v">{arrival.contract_name}</span></div>)}
              {arrival.reporting_period && (
                <div className="kv"><span className="k">Reporting Period</span>
                  <span className="v">{periodLabel(arrival.reporting_period)}</span></div>)}
              <div className="kv"><span className="k">Channel</span>
                <span className="v">
                  {arrival.channel
                    ? <Badge tone={CAME_IN_BY[arrival.channel].tone}>
                        {CAME_IN_BY[arrival.channel].label}</Badge>
                    : "—"}</span></div>
              <div className="kv"><span className="k">Received</span>
                <span className="v">{fmtStamp(arrival.received_at)}</span></div>
              <div className="kv"><span className="k">Rows</span>
                <span className="v mono">{rowsOf(arrival.run_rows ?? arrival.row_count)}</span></div>
            </div>

            {/* The broker exception loop: reference, progress, and whether
                the broker was told. Shown once the file has a submission. */}
            <SubmissionPanel arrivalId={arrival.arrival_id} onChanged={onResolved}
              onOpenVersion={onOpenArrival} />

            {/* The ten intake checks as a stepper, in the order they ran. They
                stop at the first failure, so anything after it never ran — a
                tick there would be a lie. Each description sits behind an ⓘ;
                only the failed step's reason is written out. */}
            <div className="sub-h">Intake Checks</div>
            <ol style={{ listStyle: "none", margin: "4px 0 0", padding: 0 }}>
              {CHECKS.map(([what, why], i) => {
                const cls = failed === -1 ? (st === "ok" ? "pass" : "todo")
                  : i < failed ? "pass"
                  : i === failed ? (st === "held" ? "fail" : "stop")
                  : "skip";
                const colour = cls === "pass" ? "var(--p-ok)" : cls === "fail" ? "var(--p-warn)"
                  : cls === "stop" ? "var(--p-crit)" : "var(--p-faint)";
                const last = i === CHECKS.length - 1;
                return (
                  <li key={what} style={{ display: "flex", gap: 12 }}>
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", width: 18 }}>
                      <span aria-hidden="true" style={{
                        width: 18, height: 18, borderRadius: 99, flex: "0 0 auto",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 10, fontWeight: 700, color: "#fff",
                        background: cls === "skip" || cls === "todo" ? "transparent" : colour,
                        border: `1.5px solid ${colour}`,
                      }}>
                        {cls === "pass" ? "✓" : cls === "fail" || cls === "stop" ? "!" : ""}
                      </span>
                      {!last && <span style={{ width: 1.5, flex: 1, minHeight: 18, margin: "3px 0",
                        background: cls === "pass" ? "var(--p-ok)" : "var(--p-border)" }} />}
                    </div>
                    <div style={{ paddingBottom: last ? 0 : 18, minWidth: 0 }}>
                      <div style={{ fontSize: 12.5, lineHeight: "18px",
                        color: cls === "skip" ? "var(--p-faint)" : "var(--p-ink)",
                        fontWeight: cls === "fail" || cls === "stop" ? 600 : 500 }}>
                        {what}
                        <InfoTip text={cls === "skip" ? `${why} Not run — an earlier check failed.` : why} />
                      </div>
                      {i === failed && (
                        <div style={{ fontSize: 12, color: colour, marginTop: 2 }}>{fullReason(arrival)}</div>)}
                    </div>
                  </li>);
              })}
            </ol>

            {/* Everything else, one click down. */}
            <div className="sub-h" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              More Details
              <button type="button" className="linkbtn" style={{ marginLeft: "auto", textTransform: "none", letterSpacing: 0 }}
                onClick={() => setShowMore(o => !o)}>{showMore ? "Hide" : "Show"}</button>
            </div>
            {showMore && (<>
              <div className="kv"><span className="k">Submission</span>
                <span className="v mono">#{arrival.arrival_id}</span></div>
              {/* A manual upload's sender is the person who uploaded it; for
                  the other channels, which key or folder tells two of a
                  broker's systems apart. */}
              {arrival.channel === "upload" ? (
                <div className="kv"><span className="k">Uploaded by</span>
                  <span className="v">{arrival.submitted_by_name ?? "—"}</span></div>
              ) : (
                <div className="kv"><span className="k">Sender</span>
                  <span className="v mono" style={{ fontSize: 11 }}>
                    {arrival.claimed_sender ?? arrival.route_address ?? "—"}</span></div>)}
              <div className="kv"><span className="k">File Size</span>
                <span className="v mono">{bytes(arrival.file_size_bytes)}</span></div>
              {arrival.run_at && arrival.run_state !== "running" && (
                <div className="kv"><span className="k">Processed At</span>
                  <span className="v">{fmtStamp(arrival.run_at)}</span></div>)}
              {arrival.sender_notified_at && (
                <div className="kv"><span className="k">Sender Notified</span>
                  <span className="v">{arrival.sender_notified_via ?? "Notified"}{" "}
                    {fmtStamp(arrival.sender_notified_at)}</span></div>)}
            </>)}

            {arrival.is_infected && (
              <div className="note crit" style={{ marginTop: 12 }}>
                <b>This file failed the security scan.</b> It has been quarantined and cannot
                be downloaded or released. The only available action is to discard it.
              </div>)}

            {arrival.bytes_purged_at && (
              <div className="note" style={{ marginTop: 12 }}>
                The file was deleted on {fmtStamp(arrival.bytes_purged_at)} under the data
                retention policy. This record is retained.
              </div>)}

            {/* Why somebody decided is the half of the record that is worth
                having three months later. Optional, and asked for at the moment
                of deciding rather than in a separate screen nobody opens. */}
            {st !== "ok" && !arrival.resolution && (
              <label className="kv" style={{ marginTop: 12, display: "block" }}>
                <span className="k">Note (optional)</span>
                <input className="inp" value={note} disabled={busy}
                  placeholder="Reason for releasing or discarding this file"
                  onChange={e => setNote(e.target.value)} style={{ width: "100%" }} />
              </label>)}

            {err && <div className="note crit" style={{ marginTop: 12 }}>{err}</div>}
          </div>

          <div className="drawer-f">
            {arrival.can_download && (
              <button className="btn" disabled={busy}
                onClick={() => downloadArrival(arrival.arrival_id, arrival.filename)}
                title="All downloads are logged">Download</button>)}

            {/* Release is for HELD files only. A turned-away file is one we
                could not read — releasing a PDF would hand processing something
                broken, and the fix is a file we can open, not an override. */}
            {st === "held" && !arrival.resolution && (
              <button className="btn pri" disabled={busy}
                onClick={() => decide("release")}>
                {busy ? "Processing…" : "Release File"}</button>)}

            {st !== "ok" && !arrival.resolution && (
              <button className="btn" disabled={busy}
                onClick={() => decide("discard")}>Discard</button>)}

            {st === "ok" && (arrival.run_state === "failed" || arrival.run_state === "not_run"
              || arrival.run_state === "pre_autorun") && (
              <button className="btn" disabled={busy} onClick={runAgain}
                title="Reprocess this file using the programme's current setup">
                {busy ? "Processing…" : arrival.run_state === "pre_autorun" ? "Process Now" : "Reprocess"}</button>)}

            {arrival.run_export_id && (
              <button className="btn pri"
                onClick={() => nav(`/uploads/${arrival.run_export_id}/exceptions?download=${arrival.run_export_id}&from=files`)}
                title="View the output file and its exceptions">
                View Results →</button>)}

            <button className="btn" onClick={onClose}
              style={{ marginLeft: "auto" }}>Close</button>
          </div>
        </>)}
      </aside>
    </>
  );
}
