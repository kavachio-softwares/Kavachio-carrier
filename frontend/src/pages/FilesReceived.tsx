// Feature 10 — "Files Received".
//
// One queue for everything that has landed, however it landed. The "Came in by"
// column is the only trace of which door a file used, because after arrival it
// stops mattering.
//
// ONE table, not two. Splitting by outcome put held files under a heading
// reading "Turned away on arrival", which contradicted their own badge — and it
// meant "what came in today" had to be read in two places. The filter chips do
// what the second table was doing, and the counts stay visible while filtered.
//
// Clicking a row opens a drawer with the six checks. "Why was my file refused?"
// is the most common support question and the answer was one line of text in a
// cell, truncated by the column beside it.
//
// Styled with `.proto` (proto.css) to match the wireframe, like FilesArrive.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
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

type Filter = "all" | "ok" | "held" | "away";

function state(a: Arrival): Exclude<Filter, "all"> {
  return a.outcome === "accepted" ? "ok" : isHeld(a) ? "held" : "away";
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

export default function FilesReceived() {
  const nav = useNavigate();
  const [rows, setRows] = useState<Arrival[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [filter, setFilter] = useState<Filter>("all");
  const [fChannel, setFChannel] = useState<string>("");
  const [fBroker, setFBroker] = useState<string>("");
  const [fProgramme, setFProgramme] = useState<string>("");
  const [open, setOpen] = useState<Arrival | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setRows((await listArrivals()).rows); setErr(null);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? e?.message ?? "Failed to load.");
    } finally { setBusy(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // Escape closes the drawer, and the scrim behind it is clickable — the two
  // ways out people try first.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const all = rows ?? [];

  const counts = useMemo(() => {
    const today = new Date().toDateString();
    return {
      today: all.filter(a => a.received_at &&
        new Date(a.received_at).toDateString() === today).length,
      all: all.length,
      ok: all.filter(a => state(a) === "ok").length,
      held: all.filter(a => state(a) === "held").length,
      away: all.filter(a => state(a) === "away").length,
      // 12.3 — the queue is HELD AND UNRESOLVED. A held file somebody has
      // already decided is finished, and counting it keeps a tile amber for
      // work that is done.
      waiting: all.filter(a => state(a) === "held" && !a.resolution).length,
      // The oldest thing still waiting. A queue nobody opens is the same as no
      // queue, and one number that says "eleven days" is what makes somebody
      // open it.
      oldestWaitDays: Math.max(0, ...all
        .filter(a => state(a) === "held" && !a.resolution && a.received_at)
        .map(a => Math.floor(
          (Date.now() - new Date(a.received_at as string).getTime()) / 86400000))),
    };
  }, [all]);

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

  const shown = useMemo(() => all.filter(a =>
    (filter === "all" || state(a) === filter) &&
    (!fChannel || a.channel === fChannel) &&
    (!fBroker || a.broker_name === fBroker) &&
    (!fProgramme || a.program_name === fProgramme)
  ), [all, filter, fChannel, fBroker, fProgramme]);

  const filtered = filter !== "all" || !!fChannel || !!fBroker || !!fProgramme;

  return (
    <div className="proto">
      <section className="view full">
        <div className="note" style={{ marginBottom: 18 }}>
          <b>Everything that has landed, in one list.</b> Emailed, uploaded, dropped on the
          server or sent by a machine — it all ends up here, in the order it arrived. The only
          place the way in shows up is one column, because after that it stops mattering.
        </div>

        <div className="page-head">
          <div className="t">
            <h2>Files Received</h2>
            <p>Every spreadsheet that has reached you, whichever way it came in, and what
              happened to it.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/intake")}>← How files arrive</button>
            <button className="btn" onClick={load} disabled={busy}>
              {busy ? "Refreshing…" : "Refresh"}</button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>}

        <div className="tiles" style={{ marginBottom: 18 }}>
          <div className="tile">
            <div className="k">Arrived today</div>
            <div className="v">{rows ? counts.today : "—"}</div>
            <div className="foot">however they came in</div>
          </div>
          <div className="tile">
            <div className="k">Went through</div>
            <div className="v">{rows ? counts.ok : "—"}</div>
            <div className="foot">passed every check</div>
          </div>
          {/* Held and turned away are genuinely different situations — one is
              waiting on a person, the other is finished — so they keep separate
              tiles and separate colours. */}
          <div className={`tile${counts.waiting > 0 ? " warnl" : ""}`}>
            <div className="k">Waiting on you</div>
            <div className="v" style={counts.waiting > 0 ? { color: "var(--p-warn)" } : undefined}>
              {rows ? counts.waiting : "—"}</div>
            <div className="foot">
              {counts.waiting > 0 && counts.oldestWaitDays > 0
                ? `oldest has waited ${counts.oldestWaitDays} day${counts.oldestWaitDays === 1 ? "" : "s"}`
                : "a person has to decide"}</div>
          </div>
          <div className={`tile${counts.away > 0 ? " alert" : ""}`}>
            <div className="k">Turned away</div>
            <div className="v" style={counts.away > 0 ? { color: "var(--p-crit)" } : undefined}>
              {rows ? counts.away : "—"}</div>
            <div className="foot">never got as far as processing</div>
          </div>
        </div>

        <div className="filters">
          {([["all", "All", ""], ["ok", "Went through", ""],
             ["held", "Held", "warnc"], ["away", "Turned away", "critc"]] as const)
            .map(([f, label, tone]) => (
              <button key={f} type="button" className={`chip ${tone}`}
                aria-pressed={filter === f} onClick={() => setFilter(f)}>
                {label} <span className="n">{counts[f]}</span>
              </button>))}
          <span className="spacer">
            <select className="sel" value={fChannel} onChange={e => setFChannel(e.target.value)}>
              <option value="">Any way in</option>
              {options.channels.map(c => (
                <option key={c} value={c}>{CAME_IN_BY[c].label}</option>))}
            </select>
            <select className="sel" value={fBroker} onChange={e => setFBroker(e.target.value)}>
              <option value="">Any broker</option>
              {options.brokers.map(b => <option key={b} value={b}>{b}</option>)}
            </select>
            <select className="sel" value={fProgramme}
              onChange={e => setFProgramme(e.target.value)}>
              <option value="">Any programme</option>
              {options.programmes.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </span>
        </div>

        <div className="card">
          <div className="card-h">
            <h3>What has come in</h3><span className="sub">newest first</span>
            {/* Counts stay visible while filtered, so nothing is ever hidden
                without saying how much. */}
            <span className="right faint" style={{ fontSize: 12 }}>
              {!rows ? "" : filtered
                ? `showing ${shown.length} of ${counts.all}`
                : `showing all ${counts.all}`}
            </span>
          </div>

          {rows && counts.all === 0 ? (
            <div style={{ textAlign: "center", padding: "36px 20px", color: "var(--p-muted)" }}>
              <b style={{ display: "block", color: "var(--p-ink)", fontSize: 14, marginBottom: 4 }}>
                Nothing has arrived yet</b>
              <p style={{ margin: "0 auto", fontSize: 12.5, maxWidth: 400, lineHeight: 1.55 }}>
                Once a broker drops a file in their folder it appears here.{" "}
                <span className="linkish" onClick={() => nav("/intake")}>Set a way in up first</span>.
              </p>
            </div>
          ) : rows && shown.length === 0 ? (
            <div style={{ textAlign: "center", padding: "36px 20px", color: "var(--p-muted)" }}>
              <b style={{ display: "block", color: "var(--p-ink)", fontSize: 14, marginBottom: 4 }}>
                Nothing matches these filters</b>
              <p style={{ margin: 0, fontSize: 12.5 }}>
                <span className="linkish" onClick={() => {
                  setFilter("all"); setFChannel(""); setFBroker(""); setFProgramme("");
                }}>Clear them</span> to see all {counts.all}.
              </p>
            </div>
          ) : (
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr><th>File</th><th>Came in by</th><th>From</th><th>Programme</th>
                    <th>Rows</th><th>What happened</th><th>When</th><th /></tr>
                </thead>
                <tbody>
                  {shown.map(a => {
                    const st = state(a);
                    const sub = subline(a);
                    return (
                      <tr key={a.arrival_id} className={`arow ${st}`} tabIndex={0}
                        onClick={() => setOpen(a)}
                        onKeyDown={e => { if (e.key === "Enter") setOpen(a); }}>
                        <td>
                          <div className="fname">{a.filename}</div>
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
            to see which check decided it.
          </div>
        </div>
      </section>

      <ArrivalDrawer arrival={open} onClose={() => setOpen(null)} onResolved={load} />
    </div>
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
              <div className="ref">arrival #{arrival.arrival_id}</div>
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

            <div className="sub-h">The six checks</div>
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

            {/* Only when the reason matches none of the six — better to show the
                sentence on its own than to point at the wrong check. */}
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
