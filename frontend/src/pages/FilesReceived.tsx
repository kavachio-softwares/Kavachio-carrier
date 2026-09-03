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
  isHeld, listArrivals, listRoutes,
  type Arrival, type Channel, type IntakeRoute,
} from "../api/intake";
import { fmtStamp } from "../utils/date";

const CAME_IN_BY: Record<Channel, string> = {
  upload: "Uploaded", email: "Emailed", sftp: "Server folder",
  api: "Sent by machine", cloud_folder: "Shared folder",
};

// The six checks in the order land_file() runs them. That order is the whole
// point: it stops at the FIRST failure, so a file that fails check three has
// passed one and two and checks four to six never ran at all.
const CHECKS: [string, string][] = [
  ["Is it a spreadsheet at all?",
   "A PDF or a photo of a spreadsheet cannot be read."],
  ["Can we open it?",
   "Half-uploaded and password-protected files look fine until you try."],
  ["Do we know who sent it?",
   "Every file has to belong to a broker on one of your programmes."],
  ["Is it the same file we already have?",
   "Brokers often send twice. Loading it twice would double your premium."],
  ["Does it have any rows in it?",
   "An empty file usually means an export that silently failed."],
  ["Is there a live contract to check it against?",
   "There is nothing to check a file against until the contract is agreed."],
];

// Which check produced this reason. The backend writes the sentence, not the
// index, so this reads it back — matched against the phrases in
// intake_service.py rather than the whole string, so wording can be tuned
// without silently breaking the drawer. -1 when nothing matches, and then the
// drawer shows the reason on its own instead of guessing.
function failedCheck(reason: string | null): number {
  if (!reason) return -1;
  const r = reason.toLowerCase();
  if (/not a spreadsheet|not an excel workbook|we can read /.test(r)) return 0;
  if (/could not open it/.test(r)) return 1;
  if (/recognise the sender|not linked to a broker|been switched off/.test(r)) return 2;
  if (/same file we already loaded/.test(r)) return 3;
  if (/no rows in it/.test(r)) return 4;
  if (/no live contract/.test(r)) return 5;
  return -1;
}

type Filter = "all" | "ok" | "held" | "away";

function state(a: Arrival): Exclude<Filter, "all"> {
  return a.outcome === "accepted" ? "ok" : isHeld(a) ? "held" : "away";
}

function Badge({ tone, children }:
  { tone: "ok" | "warn" | "crit" | "mut"; children: React.ReactNode }) {
  return <span className={`badge b-${tone}`}><span className="d" />{children}</span>;
}

function bytes(n: number | null): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** The line under the filename: what happened, in the fewest words that are
 *  still true. The full sentence is in the drawer. */
function subline(a: Arrival): string {
  if (a.outcome === "accepted") return a.bdx_upload_id ? "" : "waiting to be run";
  return (a.turned_away_reason ?? "").replace(/^Held — /, "");
}

export default function FilesReceived() {
  const nav = useNavigate();
  const [rows, setRows] = useState<Arrival[] | null>(null);
  const [routes, setRoutes] = useState<IntakeRoute[]>([]);
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
      // Routes come along for the programme column: an arrival records which
      // route it came in on, and the route is what knows the programme.
      const [arrivals, r] = await Promise.all([listArrivals(), listRoutes()]);
      setRows(arrivals.rows); setRoutes(r.routes); setErr(null);
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

  const routeById = useMemo(
    () => new Map(routes.map(r => [r.route_id, r])), [routes]);

  /** The programme a file belongs to, when the route it arrived on names one.
   *  A broker-wide route knows WHO but not WHICH, so this is honestly blank
   *  rather than guessed. */
  const programmeOf = useCallback((a: Arrival): string | null => {
    const r = a.route_id != null ? routeById.get(a.route_id) : undefined;
    return r?.program_name ?? null;
  }, [routeById]);

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
    };
  }, [all]);

  // The dropdowns offer only what is actually in the list — a filter that can
  // only ever return nothing is worse than no filter.
  const options = useMemo(() => ({
    channels: [...new Set(all.map(a => a.channel).filter((c): c is Channel => !!c))],
    brokers: [...new Set(all.map(a => a.broker_name).filter((b): b is string => !!b))].sort(),
    programmes: [...new Set(all.map(programmeOf).filter((p): p is string => !!p))].sort(),
  }), [all, programmeOf]);

  const shown = useMemo(() => all.filter(a =>
    (filter === "all" || state(a) === filter) &&
    (!fChannel || a.channel === fChannel) &&
    (!fBroker || a.broker_name === fBroker) &&
    (!fProgramme || programmeOf(a) === fProgramme)
  ), [all, filter, fChannel, fBroker, fProgramme, programmeOf]);

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
          <div className={`tile${counts.held > 0 ? " warnl" : ""}`}>
            <div className="k">Held</div>
            <div className="v" style={counts.held > 0 ? { color: "var(--p-warn)" } : undefined}>
              {rows ? counts.held : "—"}</div>
            <div className="foot">a person has to decide</div>
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
                <option key={c} value={c}>{CAME_IN_BY[c]}</option>))}
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
                    <th>Size</th><th>When</th></tr>
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
                          <div className="rowacts">
                            {st === "ok"
                              ? (a.bdx_upload_id ? <Badge tone="ok">Processed</Badge>
                                : <Badge tone="ok">Passed every check</Badge>)
                              : st === "held" ? <Badge tone="warn">Held</Badge>
                              : <Badge tone="crit">Turned away</Badge>}
                            {sub && <span className="faint" style={{ fontSize: 11.5 }}>{sub}</span>}
                          </div>
                        </td>
                        <td className="muted">{a.channel ? CAME_IN_BY[a.channel] : "—"}</td>
                        <td>{a.broker_name ?? <span className="muted">unknown sender</span>}</td>
                        <td className="muted">
                          {programmeOf(a) ?? <span className="faint">—</span>}</td>
                        <td className="mono">{bytes(a.file_size_bytes)}</td>
                        <td className="faint" style={{ fontSize: 12.5 }}>
                          {fmtStamp(a.received_at)}</td>
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
            to see which of the six checks decided it.
          </div>
        </div>
      </section>

      <ArrivalDrawer arrival={open} programme={open ? programmeOf(open) : null}
        route={open?.route_id != null ? routeById.get(open.route_id) ?? null : null}
        onClose={() => setOpen(null)} />
    </div>
  );
}

// ── the row drawer ──────────────────────────────────────────────────────────
// The decision sits directly beneath the evidence for it, which is the whole
// reason this is a drawer and not a tooltip.
function ArrivalDrawer({ arrival, programme, route, onClose }: {
  arrival: Arrival | null; programme: string | null;
  route: IntakeRoute | null; onClose: () => void;
}) {
  const st = arrival ? state(arrival) : "ok";
  const failed = arrival && st !== "ok" ? failedCheck(arrival.turned_away_reason) : -1;

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
                {arrival.channel ? CAME_IN_BY[arrival.channel] : "—"}</span></div>
            {/* Which key or which folder, not just which channel — it is how
                you tell two of a broker's systems apart. */}
            <div className="kv"><span className="k">Sender</span>
              <span className="v mono" style={{ fontSize: 11 }}>
                {arrival.claimed_sender ?? arrival.route_address ?? "—"}</span></div>
            <div className="kv"><span className="k">From</span>
              <span className="v">
                {arrival.broker_name ?? "unknown sender"}
                {programme ? ` → ${programme}` : ""}</span></div>
            {route && route.program_name == null && (
              <div className="kv"><span className="k">Programme</span>
                <span className="v faint" style={{ fontWeight: 500 }}>
                  this way in is not tied to one</span></div>)}
            <div className="kv"><span className="k">Received</span>
              <span className="v">{fmtStamp(arrival.received_at)}</span></div>
            <div className="kv"><span className="k">Size</span>
              <span className="v mono">{bytes(arrival.file_size_bytes)}</span></div>
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

            {st === "held" && (
              <div className="note" style={{ marginTop: 12 }}>
                <b>Held is not refused.</b> The file arrived and is kept exactly as it came in.
                Letting it through, or discarding it, is a decision somebody has to make —
                and the button for that is not built yet.
              </div>)}
          </div>

          <div className="drawer-f">
            {st === "held" && <>
              {/* Shown, and honestly disabled: the release/discard endpoint does
                  not exist yet, and a button that silently does nothing is
                  worse than one that says why. */}
              <button className="btn pri" disabled title="Not built yet">Load it anyway</button>
              <button className="btn" disabled title="Not built yet">Discard</button>
            </>}
            <button className="btn" onClick={onClose}
              style={{ marginLeft: "auto" }}>Close</button>
          </div>
        </>)}
      </aside>
    </>
  );
}
