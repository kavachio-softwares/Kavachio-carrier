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
import {
  discardArrival, downloadArrival, isHeld, listArrivals, releaseArrival,
  type Arrival, type Channel,
} from "../api/intake";
import { fmtStamp } from "../utils/date";

// Each door gets a name and a tone, as in the carrier-centric design: email is
// the one with a reply path so it reads as info, an upload was done by a person
// so it reads as ok, and the two machine doors are quiet greys.
const CAME_IN_BY: Record<Channel, { label: string; tone: "ok" | "info" | "mut" }> = {
  upload: { label: "Uploaded", tone: "ok" },
  email: { label: "Emailed", tone: "info" },
  sftp: { label: "Server folder", tone: "mut" },
  api: { label: "Sent by machine", tone: "mut" },
  cloud_folder: { label: "Shared folder", tone: "mut" },
};

// Every real way in, in the order the filter offers them. This is a fixed list
// rather than "whichever doors happen to appear in the rows", because a door
// with nothing through it yet is exactly the one you want to filter to in order
// to find that out — and leaving "Uploaded" out until a hand-uploaded file
// turned up made it look as though uploads were not counted here at all.
const WAY_IN_ORDER: Channel[] = ["upload", "email", "sftp", "api"];

// The checks in the order land_file() runs them. That order is the whole point:
// it stops at the FIRST failure, so a file that fails check three has passed one
// and two and the rest never ran at all.
//
// Everything above "Can we open it?" is settled WITHOUT opening the file — that
// is deliberate (12.1), because opening a spreadsheet is the one step that runs
// a stranger's choices through our parsers.
const CHECKS: [string, string][] = [
  ["Is it small enough to read?",
   "A file far larger than a bordereau is a mistake, not a month of business."],
  ["Is it a spreadsheet at all?",
   "A PDF or a photo of a spreadsheet cannot be read."],
  ["Is it safe to open?",
   "A small file that unpacks to gigabytes is not a spreadsheet."],
  ["Do we know who sent it?",
   "Every file has to belong to a broker on one of your programmes."],
  ["Is it the same file we already have?",
   "Brokers often send twice. Loading it twice would double your premium."],
  ["Does it pass the security scan?",
   "Files from outside are scanned before anything opens them."],
  ["Can we open it?",
   "Half-uploaded and password-protected files look fine until you try."],
  ["Does it have any rows in it?",
   "An empty file usually means an export that silently failed."],
  ["Does it have the columns we need?",
   "The right file with the wrong layout produces a page of blanks."],
  ["Is there a live contract to check it against?",
   "There is nothing to check a file against until the contract is agreed."],
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
  if (/same file we already loaded/.test(r)) return 4;
  if (/security scan/.test(r)) return 5;
  if (/could not open it/.test(r)) return 6;
  if (/no rows in it/.test(r)) return 7;
  if (/columns this programme reports on|missing \d+ column/.test(r)) return 8;
  if (/no live contract/.test(r)) return 9;
  return -1;
}

// "" is every row. The four values are the four tiles, and a tile is a toggle:
// pressing the one you are already in clears it.
type Filter = "" | "today" | "ok" | "held" | "away";
type Sort = "queue" | "new" | "old";
type Range = "all" | "30" | "90" | "month";

function state(a: Arrival): Exclude<Filter, "" | "today"> {
  return a.outcome === "accepted" ? "ok" : isHeld(a) ? "held" : "away";
}

/** Days since it landed. Only ever shown on a file still waiting on somebody. */
function waitDays(a: Arrival): number {
  if (!a.received_at) return 0;
  return Math.floor((Date.now() - new Date(a.received_at).getTime()) / 86400000);
}

/** Held AND undecided — the only rows that are actually a queue. */
function isWaiting(a: Arrival): boolean {
  return state(a) === "held" && !a.resolution;
}

function Badge({ tone, children }:
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

/** The action at the end of the row. Every one of them opens the drawer, so
 *  the word says what you will be doing there rather than naming the control:
 *  a held file needs a decision, a refused one needs an explanation. */
function actionLabel(a: Arrival): string {
  const st = state(a);
  if (a.resolution) return "Open →";       // already decided; nothing to do
  return st === "held" ? "Decide →" : st === "away" ? "Why →" : "Open →";
}

/** The line under the filename: what happened, in the fewest words that are
 *  still true. The full sentence is in the drawer. */
function subline(a: Arrival): string {
  // A decision is the most recent true thing about the file, so it wins over
  // the reason that made somebody decide.
  if (a.resolution === "released") return "released by hand — waiting to be run";
  if (a.resolution === "discarded") return "discarded";
  if (a.outcome === "accepted") return a.bdx_upload_id ? "" : "waiting to be run";
  return (a.turned_away_reason ?? "").replace(/^Held — /, "");
}

export default function InboxTab({ onWaitingCount, active, refreshKey }: {
  /** Reported up so a caller can carry the count. */
  onWaitingCount?: (n: number) => void;
  /** False while the Ways in tab is showing. Both panes stay mounted so the
   *  tab badge stays live, but only the visible one polls. */
  active: boolean;
  /** Bumped by Refresh in the page head. */
  refreshKey: number;
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

  // Files land by SFTP and email on a five-minute sweep and by API at any
  // moment, but the screen only ever changed when somebody pressed Refresh.
  // Poll while the tab is actually being looked at; skip while it is hidden,
  // because a backgrounded tab polling every minute is just load.
  const seen = useRef<number>(0);
  useEffect(() => {
    if (rows) seen.current = rows.length ? Math.max(...rows.map(r => r.arrival_id)) : 0;
  }, [rows]);
  useEffect(() => {
    const id = window.setInterval(async () => {
      if (!active || document.hidden || busy || open) return;
      try {
        const fresh = (await listArrivals()).rows;
        const newest = fresh.length ? Math.max(...fresh.map(r => r.arrival_id)) : 0;
        if (newest > seen.current) setPending(fresh);
      } catch { /* a failed poll is not worth an error bar; Refresh still works */ }
    }, 60_000);
    return () => window.clearInterval(id);
  }, [active, busy, open]);

  const all = rows ?? [];

  const counts = useMemo(() => {
    const today = new Date().toDateString();
    const now = new Date();
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
    return {
      // Same definition the routes endpoint uses for tiles.files_this_month —
      // received_at on or after the first of this calendar month. It used to be
      // a tile on the Ways in panel, which is the wrong place for it: it counts
      // files, and this is the screen about files.
      month: all.filter(a => a.received_at &&
        new Date(a.received_at).getTime() >= monthStart).length,
      today: all.filter(a => a.received_at &&
        new Date(a.received_at).toDateString() === today).length,
      all: all.length,
      ok: all.filter(a => state(a) === "ok").length,
      held: all.filter(a => state(a) === "held").length,
      away: all.filter(a => state(a) === "away").length,
      // 12.3 — the queue is HELD AND UNRESOLVED. A held file somebody has
      // already decided is finished, and counting it keeps a tile amber for
      // work that is done.
      waiting: all.filter(isWaiting).length,
      // The oldest thing still waiting. A queue nobody opens is the same as no
      // queue, and one number that says "eleven days" is what makes somebody
      // open it.
      oldestWaitDays: Math.max(0, ...all.filter(isWaiting).map(waitDays)),
    };
  }, [all]);

  useEffect(() => { onWaitingCount?.(counts.waiting); }, [counts.waiting, onWaitingCount]);

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
    const today = new Date().toDateString();
    const cutoff = range === "all" ? 0
      : range === "30" ? Date.now() - 30 * 86400000
      : range === "90" ? Date.now() - 90 * 86400000
      : new Date(new Date().getFullYear(), new Date().getMonth(), 1).getTime();
    const needle = q.trim().toLowerCase();

    const list = all.filter(a => {
      if (filter === "today") {
        if (!a.received_at || new Date(a.received_at).toDateString() !== today) return false;
      } else if (filter === "held") {
        // The tile above counts held AND UNDECIDED, so the filter has to mean
        // the same thing — a tile that is a control must hand you the rows it
        // counted. A held file somebody has already dealt with is reachable
        // with the filter cleared.
        if (!isWaiting(a)) return false;
      } else if (filter && state(a) !== filter) return false;
      if (cutoff && (!a.received_at || new Date(a.received_at).getTime() < cutoff)) return false;
      if (fChannel && a.channel !== fChannel) return false;
      if (fBroker && a.broker_name !== fBroker) return false;
      if (fProgramme && a.program_name !== fProgramme) return false;
      if (needle) {
        const hay = `${a.filename} ${a.broker_name ?? ""} ${a.claimed_sender ?? ""}`.toLowerCase();
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

  // A tick belongs to the row it was put on. The moment the visible set moves
  // under it, it is gone.
  useEffect(() => { setPicked(new Set()); setBulkErr(null); }, [filter, q, range, fChannel, fBroker, fProgramme]);

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
      ? `Decided together with ${targets.length - 1} other file${targets.length === 2 ? "" : "s"}`
      : undefined;
    const results = await Promise.allSettled(
      targets.map(a => call(a.arrival_id, note)));
    const failed = results.filter(r => r.status === "rejected").length;
    setPicked(new Set());
    // Reload either way: some of them may well have gone through.
    await load();
    if (failed > 0) {
      setBulkErr(failed === targets.length
        ? "None of those went through. Nothing has changed."
        : `${targets.length - failed} went through, ${failed} did not. The ones that failed are still here.`);
    }
  }

  const toggleAll = (on: boolean) =>
    setPicked(on ? new Set(shown.map(a => a.arrival_id)) : new Set());

  return (
    <>
      {err && <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>}

      {/* Four tiles, and they ARE the filter. The chip row underneath used to
          repeat these same four counts one row down, and only the chips did
          anything — the tiles were decoration on top of the control. */}
      <div className="tiles five" style={{ marginBottom: 18 }}>
        {/* Not an outcome like the four beside it — it is the period the rest
            are read against — so it drives the date range rather than the
            outcome filter, and shows as pressed when that range is on. */}
        <button type="button" className="tile" aria-pressed={range === "month"}
          onClick={() => setRange(range === "month" ? "all" : "month")}
          title={range === "month"
            ? "Showing this month only — click to show every arrival"
            : "Show only what arrived this month"}>
          <div className="k">Files this month
            {range === "month" && <span className="on">filtering</span>}</div>
          <div className="v">{rows ? counts.month : "—"}</div>
          <div className="foot">across every way in</div>
        </button>

        {([
          ["today", "Arrived today", counts.today, "however they came in", ""],
          ["ok", "Went through", counts.ok, "passed every check", ""],
          ["held", "Waiting on you", counts.waiting,
            counts.waiting > 0 && counts.oldestWaitDays > 0
              ? `oldest has waited ${counts.oldestWaitDays} day${counts.oldestWaitDays === 1 ? "" : "s"}`
              : "a person has to decide",
            counts.waiting > 0 ? "warnl" : ""],
          ["away", "Turned away", counts.away, "never got as far as processing",
            counts.away > 0 ? "alert" : ""],
        ] as const).map(([f, label, n, foot, tone]) => {
          const on = filter === f;
          const colour = tone === "warnl" ? "var(--p-warn)"
            : tone === "alert" ? "var(--p-crit)" : undefined;
          return (
            <button type="button" key={f} className={`tile ${tone}`} aria-pressed={on}
              onClick={() => setFilter(on ? "" : f)}
              title={on ? "Showing only these — click to show everything"
                        : `Show only ${label.toLowerCase()}`}>
              <div className="k">{label}{on && <span className="on">filtering</span>}</div>
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
            placeholder="Search filename or sender…" aria-label="Search files" />
        </label>
        <span className="spacer">
          <select className="sel" value={range} aria-label="When it arrived"
            onChange={e => setRange(e.target.value as Range)}>
            <option value="all">Any time</option>
            <option value="month">This month</option>
            <option value="30">Last 30 days</option>
            <option value="90">Last 90 days</option>
          </select>
          <select className="sel" value={fChannel} onChange={e => setFChannel(e.target.value)}
            aria-label="Way in">
            <option value="">Any way in</option>
            {options.channels.map(c => (
              <option key={c} value={c}>{CAME_IN_BY[c].label}</option>))}
          </select>
          <select className="sel" value={fBroker} onChange={e => setFBroker(e.target.value)}
            aria-label="Broker">
            <option value="">Any broker</option>
            {options.brokers.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
          <select className="sel" value={fProgramme} aria-label="Programme"
            onChange={e => setFProgramme(e.target.value)}>
            <option value="">Any programme</option>
            {options.programmes.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          {filtered && (
            <button type="button" className="linkbtn" onClick={clearFilters}>
              Clear filters</button>)}
        </span>
      </div>

      <div className="card">
        {/* Something landed while you were reading. Taking it is a click, so
            nothing moves under the cursor mid-decision. */}
        {pending && (
          <div className="newbar">
            <b>{pending.length - all.length > 0
              ? `${pending.length - all.length} new file${pending.length - all.length === 1 ? "" : "s"} arrived`
              : "Something changed"}</b>
            <button type="button" className="linkbtn"
              onClick={() => { setRows(pending); setPending(null); }}>Show them →</button>
          </div>)}

        {picked.size > 0 && (
          <div className="bulkbar">
            <span>{picked.size} selected</span>
            {canDiscard.length !== picked.size && (
              <span style={{ fontWeight: 500, color: "var(--p-muted)" }}>
                {picked.size - canDiscard.length} of them need no decision —
                already dealt with, or they went straight through
              </span>)}
            <span className="sp">
              <button type="button" className="btn sm pri" disabled={busy || canRelease.length === 0}
                onClick={() => decideMany("release")}
                title="Held files only — a file we could not read cannot be released">
                {busy ? "Working…" : `Load ${canRelease.length} anyway`}</button>
              <button type="button" className="btn sm" disabled={busy || canDiscard.length === 0}
                onClick={() => decideMany("discard")}>Discard {canDiscard.length}</button>
              <button type="button" className="btn sm" onClick={() => setPicked(new Set())}>
                Cancel</button>
            </span>
          </div>)}

        {bulkErr && <div className="note crit" style={{ margin: 0, borderRadius: 0 }}>{bulkErr}</div>}

        <div className="card-h">
          <h3>What has come in</h3>
          <span className="sub">
            {sort === "queue" ? "needs a decision first, then newest"
              : sort === "new" ? "newest first" : "oldest first"}</span>
          {/* Counts stay visible while filtered, so nothing is ever hidden
              without saying how much. */}
          <span className="right faint" style={{
            fontSize: 12, display: "flex", alignItems: "center", gap: 12,
          }}>
            <select className="sel" value={sort} aria-label="Sort order"
              onChange={e => setSort(e.target.value as Sort)}>
              <option value="queue">Needs a decision first</option>
              <option value="new">Newest first</option>
              <option value="old">Oldest first</option>
            </select>
            {!rows ? "" : filtered
              ? `showing ${shown.length} of ${counts.all}`
              : `showing all ${counts.all}`}
          </span>
        </div>

        {rows && counts.all === 0 ? (
          <div style={{ textAlign: "center", padding: "36px 20px", color: "var(--p-muted)" }}>
            <b style={{ display: "block", color: "var(--p-ink)", fontSize: 14, marginBottom: 4 }}>
              Nothing has arrived yet</b>
            {/* The long explanation of what this screen is lives HERE, in the
                one state where somebody genuinely does not know — not in a grey
                slab above the title on every visit. */}
            <p style={{ margin: "0 auto", fontSize: 12.5, maxWidth: 430, lineHeight: 1.6 }}>
              Emailed, uploaded, dropped on a server or sent by a machine — every spreadsheet
              that reaches you ends up here, in the order it arrived, and gets the same checks
              whichever way it came. Set a broker up on <b>Ways in</b> first.
            </p>
          </div>
        ) : rows && shown.length === 0 ? (
          <div style={{ textAlign: "center", padding: "36px 20px", color: "var(--p-muted)" }}>
            <b style={{ display: "block", color: "var(--p-ink)", fontSize: 14, marginBottom: 4 }}>
              Nothing matches these filters</b>
            <p style={{ margin: 0, fontSize: 12.5 }}>
              <span className="linkish" onClick={clearFilters}>Clear them</span> to see
              all {counts.all}.
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
                  <th>File</th><th>Came in by</th><th>From</th><th>Programme</th>
                  <th>Rows</th><th>What happened</th><th>When</th><th />
                </tr>
              </thead>
              <tbody>
                {shown.map(a => {
                  const st = state(a);
                  const sub = subline(a);
                  const on = picked.has(a.arrival_id);
                  return (
                    <tr key={a.arrival_id} className={`arow ${st}${on ? " sel" : ""}`} tabIndex={0}
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
                          {/* The wait, on the row. Without it the number was
                              only ever on the tile, describing a file the sort
                              order then hid at the bottom of the table. */}
                          {isWaiting(a) && waitDays(a) > 0 &&
                            <span className="aged">{waitDays(a)}d waiting</span>}
                        </div>
                        {/* Capped, because a refusal reason is a whole
                            sentence and the full one is in the drawer. */}
                        {sub && <div className="sub" style={{ maxWidth: 330 }}>{sub}</div>}
                      </td>
                      {/* The way in is a badge, not plain text: it is the one
                          thing on the row that is a fixed set of five, and it
                          is read by shape rather than word. */}
                      <td>{a.channel
                        ? <Badge tone={CAME_IN_BY[a.channel].tone}>
                            {CAME_IN_BY[a.channel].label}</Badge>
                        : <span className="muted">—</span>}</td>
                      <td>{a.broker_name ?? <span className="muted">unknown sender</span>}</td>
                      <td className="muted">
                        {a.program_name ?? <span className="faint">—</span>}</td>
                      <td className="mono">{rowsOf(a.row_count)}</td>
                      <td>
                        {st === "ok"
                          ? (a.bdx_upload_id ? <Badge tone="ok">Processed</Badge>
                            : <Badge tone="ok">Waiting to be run</Badge>)
                          : st === "held" ? <Badge tone="warn">Held</Badge>
                          : <Badge tone="crit">Turned away</Badge>}
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
        <div className="note" style={{
          margin: 0, border: 0, borderTop: "1px solid var(--p-border)", borderRadius: 0,
        }}>
          <b>Nothing here is lost.</b> A file that was turned away is kept exactly as it
          arrived, and a held file has landed — it is only waiting on somebody. Click any row
          to see which check decided it, or tick several to decide them together.
        </div>
      </div>

      <ArrivalDrawer arrival={open} onClose={() => setOpen(null)} onResolved={load} />
    </>
  );
}

// ── the row drawer ──────────────────────────────────────────────────────────
// The decision sits directly beneath the evidence for it, which is the whole
// reason this is a drawer and not a tooltip.
function ArrivalDrawer({ arrival, onClose, onResolved }: {
  arrival: Arrival | null; onClose: () => void; onResolved: () => void;
}) {
  const st = arrival ? state(arrival) : "ok";
  const failed = arrival && st !== "ok" ? failedCheck(arrival.turned_away_reason) : -1;

  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);

  // A fresh drawer must not inherit the last file's half-typed note or its
  // error — they belong to the row that is gone.
  useEffect(() => { setNote(""); setErr(null); setBusy(false); }, [arrival?.arrival_id]);

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
      setErr(detail || "That did not work. Nothing has changed.");
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
              <div className="ref">
                arrival #{arrival.arrival_id}
                {isWaiting(arrival) && waitDays(arrival) > 0
                  && ` · waiting ${waitDays(arrival)} days`}
              </div>
            </div>
            <button type="button" className="closeb" aria-label="Close"
              onClick={onClose}>×</button>
          </div>

          <div className="drawer-b">
            <div className="kv"><span className="k">What happened</span>
              <span className="v">
                {st === "ok" ? <Badge tone="ok">Passed every check</Badge>
                  : st === "held" ? <Badge tone="warn">Held</Badge>
                  : <Badge tone="crit">Turned away</Badge>}
              </span></div>
            <div className="kv"><span className="k">Came in by</span>
              <span className="v">
                {arrival.channel
                  ? <Badge tone={CAME_IN_BY[arrival.channel].tone}>
                      {CAME_IN_BY[arrival.channel].label}</Badge>
                  : "—"}</span></div>
            {/* Which key or which folder, not just which channel — it is how
                you tell two of a broker's systems apart. */}
            <div className="kv"><span className="k">Sender</span>
              <span className="v mono" style={{ fontSize: 11 }}>
                {arrival.claimed_sender ?? arrival.route_address ?? "—"}</span></div>
            <div className="kv"><span className="k">From</span>
              <span className="v">
                {arrival.broker_name ?? "unknown sender"}
                {arrival.program_name ? ` → ${arrival.program_name}` : ""}</span></div>
            {!arrival.program_name && (
              <div className="kv"><span className="k">Programme</span>
                <span className="v faint" style={{ fontWeight: 500 }}>
                  this way in is not tied to one</span></div>)}
            <div className="kv"><span className="k">Received</span>
              <span className="v">{fmtStamp(arrival.received_at)}</span></div>
            <div className="kv"><span className="k">Size · rows</span>
              <span className="v mono">{bytes(arrival.file_size_bytes)}
                {" · "}{rowsOf(arrival.row_count)} rows</span></div>
            {/* How a resend gets spotted — the duplicate check is a comparison
                of exactly this. */}
            <div className="kv"><span className="k">Fingerprint</span>
              <span className="v mono" style={{ fontSize: 11 }}>
                {arrival.file_hash_sha256
                  ? `${arrival.file_hash_sha256.slice(0, 12)}…` : "—"}</span></div>
            {arrival.sender_notified_at && (
              <div className="kv"><span className="k">Sender told</span>
                <span className="v">{arrival.sender_notified_via ?? "Told"}{" "}
                  {fmtStamp(arrival.sender_notified_at)}</span></div>)}

            <div className="sub-h">The checks, in the order they ran</div>
            <ul className="checks">
              {CHECKS.map(([what, why], i) => {
                // The checks stop at the first failure, so anything after it
                // genuinely never ran. A tick there would be a lie.
                const cls = failed === -1 ? (st === "ok" ? "pass" : "")
                  : i < failed ? "pass"
                  : i === failed ? (st === "held" ? "fail" : "stop")
                  : "skip";
                return (
                  <li key={what} className={cls}>
                    <span className="m" aria-hidden="true">
                      {cls === "pass" ? "✓" : cls === "skip" ? "·"
                        : cls === "" ? "·" : "▲"}</span>
                    <span>
                      <span className="t">{what}</span>
                      {i === failed
                        ? <span className="why">
                            {arrival.turned_away_reason?.replace(/^Held — /, "")}</span>
                        : cls === "skip"
                        ? <span className="why">not reached — an earlier check stopped it</span>
                        : <span className="why">{why}</span>}
                    </span>
                  </li>);
              })}
            </ul>

            {/* Only when the reason matches none of the checks — better to show
                the sentence on its own than to point at the wrong one. */}
            {failed === -1 && st !== "ok" && (
              <div className="note warn" style={{ marginTop: 12 }}>
                {arrival.turned_away_reason ?? "No reason was recorded."}
              </div>)}

            {st === "held" && !arrival.resolution && (
              <div className="note" style={{ marginTop: 12 }}>
                <b>Held is not refused.</b> The file arrived and is kept exactly as it came in.
                Letting it through, or discarding it, is a decision somebody has to make.
              </div>)}

            {/* 12.3 — once somebody has decided, the decision IS the record.
                Shown above the buttons so a resolved file cannot be re-worked
                by accident, and so "who let this through?" is answered on the
                same screen that asked the question. */}
            {arrival.resolution && (
              <div className="note" style={{ marginTop: 12 }}>
                <b>{arrival.resolution === "released" ? "Released" : "Discarded"}</b>
                {arrival.resolved_at ? ` on ${fmtStamp(arrival.resolved_at)}` : ""}
                {arrival.resolved_by_user_id ? ` by user ${arrival.resolved_by_user_id}` : ""}.
                {arrival.resolution_note ? ` “${arrival.resolution_note}”` : ""}
              </div>)}

            {arrival.is_infected && (
              <div className="note crit" style={{ marginTop: 12 }}>
                <b>This file failed the security scan.</b> It is not kept where anybody
                can open it, and it cannot be downloaded or released. Discarding it is
                the only thing to do here.
              </div>)}

            {arrival.bytes_purged_at && (
              <div className="note" style={{ marginTop: 12 }}>
                The file itself was deleted on {fmtStamp(arrival.bytes_purged_at)} under the
                retention rule. This record of it stays.
              </div>)}

            {/* Why somebody decided is the half of the record that is worth
                having three months later. Optional, and asked for at the moment
                of deciding rather than in a separate screen nobody opens. */}
            {st !== "ok" && !arrival.resolution && (
              <label className="kv" style={{ marginTop: 12, display: "block" }}>
                <span className="k">Note (optional)</span>
                <input className="inp" value={note} disabled={busy}
                  placeholder="Why are you letting this through, or dropping it?"
                  onChange={e => setNote(e.target.value)} style={{ width: "100%" }} />
              </label>)}

            {err && <div className="note crit" style={{ marginTop: 12 }}>{err}</div>}
          </div>

          <div className="drawer-f">
            {arrival.can_download && (
              <button className="btn" disabled={busy}
                onClick={() => downloadArrival(arrival.arrival_id, arrival.filename)}
                title="Every download is recorded">Download</button>)}

            {/* Release is for HELD files only. A turned-away file is one we
                could not read — releasing a PDF would hand processing something
                broken, and the fix is a file we can open, not an override. */}
            {st === "held" && !arrival.resolution && (
              <button className="btn pri" disabled={busy}
                onClick={() => decide("release")}>
                {busy ? "Working…" : "Load it anyway"}</button>)}

            {st !== "ok" && !arrival.resolution && (
              <button className="btn" disabled={busy}
                onClick={() => decide("discard")}>Discard</button>)}

            <button className="btn" onClick={onClose}
              style={{ marginLeft: "auto" }}>Close</button>
          </div>
        </>)}
      </aside>
    </>
  );
}
