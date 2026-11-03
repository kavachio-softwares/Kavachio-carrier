import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

export type DayRun = {
  export_id: number; source_upload_id: number | null; run_at: string | null;
  result: "clean" | "flagged" | "not_checked"; exceptions: number; current: boolean;
  file: string; programme: string | null;
  carrier: { id: number; name: string; code: string | null };
  broker: { id: number; name: string } | null;
};
export type DayRuns = {
  day: string; items: DayRun[];
  totals: { runs: number; clean: number; flagged: number; not_checked: number };
  carriers: { id: number; name: string; code: string | null; runs: number }[];
};

const nf = (n: number) => n.toLocaleString();
const plural = (n: number, one: string, many: string) => `${nf(n)} ${n === 1 ? one : many}`;
/** "2026-09-07" → "Mon 7 Sep 2026", read as a calendar day (no zone shift). */
function longDay(iso: string) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** The files behind one bar of a runs-per-day chart, in a right-hand drawer.
 *  `load` fetches that day in the chart's own scope, so the list adds up to
 *  the bar. The platform view names each file's carrier and links to every
 *  carrier's own list; a carrier's view is already one carrier, so it names
 *  the broker only. Files whose checks never ran are left out, as the charts
 *  do not draw them. */
export function DayFilesDrawer({ day, load, reloadKey = "", from, platform = false, onClose }: {
  day: string | null;
  load: (day: string) => Promise<DayRuns>;
  /** Changes whenever the chart's scope does, so an open drawer refetches. */
  reloadKey?: string;
  /** Where the exceptions screen's back link returns to. */
  from: "admin" | "home" | "broker";
  platform?: boolean;
  onClose: () => void;
}) {
  const [data, setData] = useState<DayRuns | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!day) return;
    setData(null); setErr(null);
    load(day).then(setData).catch(() => setErr("Could not load the files for this day."));
  }, [day, reloadKey]);
  useEffect(() => {
    if (!day) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [day, onClose]);

  const open = !!day;
  const t = data?.totals;
  const items = (data?.items ?? []).filter(x => x.result !== "not_checked");
  const result = (x: DayRun) => {
    if (!x.current) return <span className="badge b-mut">Replaced by a re-run</span>;
    if (x.result === "clean") return <span className="badge b-ok"><span className="d" />Clean</span>;
    return <span className="badge b-warn"><span className="d" />
      {x.exceptions ? `${nf(x.exceptions)} exception${x.exceptions === 1 ? "" : "s"}` : "Flagged"}</span>;
  };
  // The app's tables centre their cells; a list of files reads down the left edge.
  const L: React.CSSProperties = { textAlign: "left" };
  const triage = (x: DayRun) =>
    `/uploads/${x.source_upload_id ?? x.export_id}/exceptions?download=${x.export_id}&from=${from}`;

  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h">
          <div>
            <h4>Files processed on {day ? longDay(day) : ""}</h4>
            <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>
              {t ? <>{plural(t.runs - t.not_checked, "run", "runs")} · <b style={{ color: "var(--p-ok)" }}>{nf(t.clean)} clean</b>
                {" · "}<b style={{ color: "var(--p-warn)" }}>{nf(t.flagged)} flagged</b></> : " "}
            </div>
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b" style={{ padding: 0 }}>
          {err && <div className="note warn" style={{ margin: 16 }}>{err}</div>}
          {!data && !err && <div className="muted" style={{ padding: 20 }}>Loading…</div>}
          {data && items.length === 0 && <div className="empty" style={{ padding: 20 }}>No files were processed on this day.</div>}
          {data && items.length > 0 && (
            <div className="tbl-wrap">
              <table>
                <thead><tr>{["Time", platform ? "Carrier ← Broker · File"
                  : from === "broker" ? "Carrier · File" : "Broker · File", "Result"].map(h =>
                  <th key={h} style={L}>{h}</th>)}</tr></thead>
                <tbody>
                  {items.map(x => (
                    <tr key={x.export_id} style={x.current ? undefined : { opacity: 0.6 }}>
                      <td className="muted" style={{ ...L, whiteSpace: "nowrap", fontSize: 12.5 }}>
                        {x.run_at ? new Date(x.run_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}</td>
                      <td style={L}>
                        {platform ? (
                          <div><b>{x.carrier.name}</b>{x.broker && <span className="muted"> ← {x.broker.name}</span>}</div>
                        ) : from === "broker" ? (
                          // A broker's own drawer: every row is theirs, so the
                          // useful name is the carrier the file went to.
                          <div><b>{x.carrier.name}</b></div>
                        ) : (
                          <div><b>{x.broker?.name ?? "No broker"}</b></div>
                        )}
                        <div className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>{x.file}</div>
                        {x.programme && <div className="muted" style={{ fontSize: 12 }}>{x.programme}</div>}
                      </td>
                      <td style={{ ...L, whiteSpace: "nowrap" }}>
                        {result(x)}
                        {/* A replaced run is history: its problems are worked on the
                            re-run that replaced it, which is listed as its own row. */}
                        {x.result === "flagged" && x.current && (
                          <div style={{ marginTop: 6 }}>
                            <Link className="linkish" style={{ fontSize: 12.5 }} to={triage(x)}>See exceptions →</Link>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        {platform && data && data.carriers.some(c => c.code) && (
          <div className="drawer-f">
            {data.carriers.filter(c => c.code).map(c => (
              <Link key={c.id} className="btn sm" to={`/tenants/${c.code}?tab=runs&from=${data.day}&to=${data.day}`}>
                Open {c.name}'s files for this day →
              </Link>
            ))}
          </div>
        )}
      </aside>
    </>
  );
}
