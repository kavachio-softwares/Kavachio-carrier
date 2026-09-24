import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { InfoTip } from "./InfoTip";
import { ChartCard } from "./StatCard";

// The validated outcome pair (see BrokerCharts.tsx): resolved vs still open.
const RESOLVED = "#0E9F6E";
const OPEN = "#C77A12";
// A file with nothing wrong on it. Grey, not a third outcome colour: there is
// nothing here to act on, and the eye should go to the bars that need someone.
const CLEAN = "#DDE1E8";
const TRACK = "#F0F2F6";

/** The broker's most recent file with issues, counted exactly as its
 *  Exception Triage screen counts them — the bar and the screen it opens
 *  always agree (issues = resolved + open). */
type Latest = {
  export_id: number; source_upload_id: number | null;
  issues: number; resolved: number; open: number; run_at: string | null;
};

export type BrokerPerf = {
  id: number; name: string;
  runs: number; clean: number; flagged: number; not_checked: number;
  last_run_at: string | null;
  latest: Latest | null;
};

/** The same Exception Triage screen the dashboard's own runs open. */
const triagePath = (l: Latest) =>
  `/uploads/${l.source_upload_id ?? l.export_id}/exceptions?download=${l.export_id}&from=home`;

const DAYS = 30;

/**
 * How each broker company is working and how its exceptions are being put
 * right — the five brokers that most recently sent this carrier a file. Both
 * carrier seats see the same brokers, because every other number on this
 * dashboard is counted over the whole carrier too.
 *
 * One stacked horizontal bar per broker — the issues on its latest file:
 * resolved (green) then still open (amber), counted as its Exception Triage
 * screen counts them. Each bar is that broker's own 100%, so it shows how far
 * that file has been put right, never how it compares with another broker —
 * the counts on the right carry the volume. A broker with nothing outstanding
 * reports its latest clean file instead, drawn as one grey bar.
 * Clicking a broker opens that screen.
 * Every number is also written out on the row, so colour is never the only
 * way to read it.
 */
export default function BrokerPerformance({ mga }: { mga: string }) {
  const [rows, setRows] = useState<BrokerPerf[] | null>(null);
  const [activeTotal, setActiveTotal] = useState(0);

  useEffect(() => {
    api.get<{ items: BrokerPerf[]; active_total: number }>(
      "/dashboard/broker-performance", { params: { mga, days: DAYS, limit: 5 } })
      .then(r => { setRows(r.data.items); setActiveTotal(r.data.active_total); })
      .catch(() => { setRows([]); setActiveTotal(0); });
  }, [mga]);

  return (
    <ChartCard
      title="Broker Performance"
      info={<InfoTip text={
        `The broker companies that sent files most recently, over the last ${DAYS} days. `
        + "Each bar is all the issues on that broker's latest file: green is the "
        + "share resolved, amber the share still open — the same numbers as its "
        + "exception screen, or one grey bar when that file is clean. Bars are not "
        + "compared with each other; the counts on the right are. Click a broker to "
        + "open it."} />}
    >
      {rows === null ? (
        <div className="empty">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="empty">
          No broker company has sent a file in the last {DAYS} days.
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 16, fontSize: 12, color: "var(--p-muted)", marginBottom: 14 }}>
            <Swatch color={RESOLVED} label="Resolved" />
            <Swatch color={OPEN} label="Still open" />
            <Swatch color={CLEAN} label="Clean file" />
            <span style={{ marginLeft: "auto", color: "var(--p-faint)" }}>
              Most recent {rows.length} · last {DAYS} days
            </span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {rows.map(r => <Row key={r.id} r={r} />)}
          </div>

          {activeTotal > rows.length && (
            <div style={{ textAlign: "right", marginTop: 14, fontSize: 13 }}>
              <span style={{ color: "var(--p-faint)" }}>
                {activeTotal - rows.length} more active {activeTotal - rows.length === 1 ? "broker" : "brokers"} ·{" "}
              </span>
              <Link className="linkish" to="/brokers">All brokers →</Link>
            </div>
          )}
        </>
      )}
    </ChartCard>
  );
}

function Row({ r }: { r: BrokerPerf }) {
  const l = r.latest;
  const cleanPct = r.runs ? Math.round((r.clean / r.runs) * 100) : 0;
  const tip = [
    r.name,
    `Last ${DAYS} days: ${r.runs} ${r.runs === 1 ? "file" : "files"} — ${r.clean} clean, `
      + `${r.flagged} with issues` + (r.not_checked ? `, ${r.not_checked} not checked` : ""),
    !l ? "No file checked"
      : l.issues ? `Latest file with issues: ${l.issues} issues — `
                   + `${l.resolved} resolved, ${l.open} still open`
      : "Latest file: clean, no issues found",
    l ? "Click to open its exceptions" : "",
  ].filter(Boolean).join("\n");

  const body = (
    <>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: "var(--p-ink)",
                       overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {r.name}
        </span>
        <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
          {r.runs} {r.runs === 1 ? "file" : "files"} · {cleanPct}% clean
        </span>
      </span>

      {l ? (
        <span style={{ height: 12, borderRadius: 6, background: TRACK, display: "block" }}>
          <span style={{ display: "flex", gap: 2, height: "100%", width: "100%" }}>
            {l.issues === 0 ? (
              <span style={{ flex: 1, background: CLEAN, borderRadius: 6 }} />
            ) : (<>
              {l.resolved > 0 && (
                <span style={{ flex: l.resolved, background: RESOLVED, borderRadius: 6, minWidth: 4 }} />
              )}
              {l.open > 0 && (
                <span style={{ flex: l.open, background: OPEN, borderRadius: 6, minWidth: 4 }} />
              )}
            </>)}
          </span>
        </span>
      ) : (
        <span style={{ fontSize: 12, color: "var(--p-faint)" }}>No file checked</span>
      )}

      <span style={{ fontSize: 12.5, textAlign: "right", fontVariantNumeric: "tabular-nums",
                     color: "var(--p-muted)" }}>
        {!l ? "—" : !l.issues ? (
          <><b style={{ color: "var(--p-ink)" }}>Clean</b>
            <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
              no issues found
            </span></>
        ) : (
          <><b style={{ color: "var(--p-ink)" }}>{l.open}</b> of {l.issues} open
            <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
              {l.resolved} resolved
            </span></>
        )}
      </span>
    </>
  );

  const grid = { display: "grid", gridTemplateColumns: "minmax(120px, 180px) 1fr 130px",
                 alignItems: "center", gap: 14 } as const;
  return l ? (
    <Link to={triagePath(l)} title={tip} className="rb-row"
          style={{ ...grid, color: "inherit", textDecoration: "none" }}>
      {body}
    </Link>
  ) : (
    <div title={tip} style={grid}>{body}</div>
  );
}

function Swatch({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span style={{ width: 10, height: 10, borderRadius: 3, background: color }} />{label}
    </span>
  );
}
