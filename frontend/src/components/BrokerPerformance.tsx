import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { canAccessPath } from "../access";
import { InfoTip } from "./InfoTip";
import { ChartCard } from "./StatCard";
import type { SelectedProgramme } from "./ProgrammeOverview";

// The validated outcome pair (see BrokerCharts.tsx): resolved vs still open.
const RESOLVED = "#0E9F6E";
const OPEN = "#C77A12";
// A file with nothing wrong on it. Grey, not a third outcome colour: there is
// nothing here to act on, and the eye should go to the bars that need someone.
const CLEAN = "#DDE1E8";
const TRACK = "#F0F2F6";

/** EVERY live file this broker sent in the window, tallied together and
 *  counted exactly as each file's Exception Triage screen counts them —
 *  exceptions = resolved + open. */
type Work = {
  files: number; rows: number;
  exceptions: number; resolved: number; open: number;
};

export type BrokerPerf = {
  id: number; name: string;
  runs: number; clean: number; flagged: number; not_checked: number;
  last_run_at: string | null;
  /** On the picked programme but sent nothing in the period: listed so the
   *  card names who is quiet. `last_run_at` is then its last file ever on
   *  that programme, or null if it never sent one. */
  no_files?: boolean;
  work: Work;
};

/** That broker's own files, split by programme — the level anything can be
 *  done at. The file rows there open the Exception Triage screens. */
const filesPath = (r: BrokerPerf) => `/brokers/${r.id}/files`;

const DAYS = 30;

/**
 * How each broker company is working and how its exceptions are being put
 * right — the five brokers that most recently sent this carrier a file. Both
 * carrier seats see the same brokers, because every other number on this
 * dashboard is counted over the whole carrier too.
 *
 * One stacked horizontal bar per broker — ALL the issues on ALL the files it
 * sent in the window: resolved (green) then still open (amber), counted as
 * each file's Exception Triage screen counts them. Showing only the latest
 * file put one file's numbers under a company's name; a bar under a company is
 * read as that company's workload, and a workload is all of it.
 *
 * Each bar is that broker's own 100%, so it shows how far that broker's pile
 * has been cleared, never how it compares with another broker — the counts on
 * the right carry the volume. A broker whose files are all clean gets one grey
 * bar. Clicking a broker opens its files, split by programme, each with its
 * own way into the exceptions.
 * Every number is also written out on the row, so colour is never the only
 * way to read it.
 */
export default function BrokerPerformance({ mga, programme, period, periodLabel }: {
  mga: string;
  /** Picked on the Programme Overview: only this programme's files count. */
  programme?: SelectedProgramme | null;
  /** The dashboard's period pick (YYYY-MM, YYYY or all) and its name. Without
   *  one the card keeps its own window, the last DAYS days. */
  period?: string;
  periodLabel?: string;
}) {
  const [rows, setRows] = useState<BrokerPerf[] | null>(null);
  const [activeTotal, setActiveTotal] = useState(0);
  const programId = programme?.id;
  // How the card names its window: the dashboard's pick, or its own.
  const when = periodLabel ?? `Last ${DAYS} days`;
  const inWhen = periodLabel == null ? `in the last ${DAYS} days`
    : periodLabel === "All years" ? "so far" : `in ${periodLabel}`;
  const sent = rows?.filter(r => !r.no_files) ?? [];

  useEffect(() => {
    let live = true;
    setRows(null);
    api.get<{ items: BrokerPerf[]; active_total: number }>(
      "/dashboard/broker-performance",
      { params: { mga, days: DAYS, limit: 5, program_id: programId, period } })
      .then(r => { if (live) { setRows(r.data.items); setActiveTotal(r.data.active_total); } })
      .catch(() => { if (live) { setRows([]); setActiveTotal(0); } });
    return () => { live = false; };
  }, [mga, programId, period]);

  return (
    <ChartCard
      title="Broker Performance"
      info={<InfoTip text={
        `Broker companies that sent files most recently, ${inWhen}. Green = exceptions resolved, amber = unresolved, grey = no exceptions.\n`
        + (programme ? `${programme.name}'s brokers that sent nothing show "No file received".\n` : "")
        + "Click a broker to see its files by programme."} />}
    >
      {rows === null ? (
        <div className="empty">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="empty">
          {programme
            ? `${programme.name} has no broker yet.`
            : `No broker company has sent a file ${inWhen}.`}
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: 16, fontSize: 12, color: "var(--p-muted)", marginBottom: 14 }}>
            {/* No key when no broker sent a file: there are no bars to read. */}
            {sent.length > 0 && <>
              <Swatch color={RESOLVED} label="Resolved" />
              <Swatch color={OPEN} label="Unresolved" />
            </>}
            {/* Only when a grey bar is on the chart — a key for something not
                drawn reads as a missing series. */}
            {rows.some(r => r.work.files > 0 && r.work.exceptions === 0) && (
              <Swatch color={CLEAN} label="Clean file" />
            )}
            <span style={{ marginLeft: "auto", color: "var(--p-faint)" }}>
              {programme
                ? `${sent.length} of ${rows.length} sent files · ${when}`
                : `Most recent ${rows.length} · ${when}`}
            </span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {rows.map(r => <Row key={r.id} r={r} when={when} inWhen={inWhen} />)}
          </div>

          {(activeTotal > sent.length || canAccessPath("/partner-performance")) && (
            <div style={{ textAlign: "right", marginTop: 14, fontSize: 13 }}>
              {activeTotal > sent.length && <>
                <span style={{ color: "var(--p-faint)" }}>
                  {activeTotal - sent.length} more active {activeTotal - sent.length === 1 ? "broker" : "brokers"} ·{" "}
                </span>
                <Link className="linkish" to="/brokers">All brokers →</Link>
              </>}
              {/* 36 — the carrier seat only, as the page itself. */}
              {canAccessPath("/partner-performance") && <>
                {activeTotal > sent.length && <span style={{ color: "var(--p-faint)" }}> · </span>}
                <Link className="linkish" to="/partner-performance">View partner performance →</Link>
              </>}
            </div>
          )}
        </>
      )}
    </ChartCard>
  );
}

const monthYear = (iso: string) => new Date(iso).toLocaleDateString("en-GB",
  { month: "long", year: "numeric" });

function Row({ r, when, inWhen }: { r: BrokerPerf; when: string; inWhen: string }) {
  const w = r.work;
  if (r.no_files) {
    const last = r.last_run_at ? `Last file: ${monthYear(r.last_run_at)}` : "Has never sent a file";
    return (
      <div title={`${r.name}\nNo file received ${inWhen}\n${last}`}
        style={{ display: "grid", gridTemplateColumns: "minmax(120px, 180px) 1fr 170px",
                 alignItems: "center", gap: 14 }}>
        <span style={{ minWidth: 0 }}>
          <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: "var(--p-ink)",
                         overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {r.name}
          </span>
          <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>0 files</span>
        </span>
        <span style={{ fontSize: 12.5, color: "var(--p-warn-ink)", fontWeight: 600 }}>
          No file received {inWhen}
        </span>
        <span style={{ fontSize: 11.5, textAlign: "right", color: "var(--p-faint)" }}>{last}</span>
      </div>
    );
  }
  // Files = each month's file once, as its latest version — not every upload
  // or re-check of it (`runs`, which read as "84 files sent").
  const files = `${w.files} ${w.files === 1 ? "file" : "files"}`;
  const tip = [
    r.name,
    w.exceptions
      ? `${when}: ${files} — ${w.exceptions} exceptions, ${w.resolved} resolved, ${w.open} unresolved`
      : w.files ? `${when}: ${files} — no exceptions` : "No file checked",
    w.files ? "Click for its files, split by programme" : "",
  ].filter(Boolean).join("\n");

  const body = (
    <>
      <span style={{ minWidth: 0 }}>
        <span style={{ display: "block", fontSize: 13, fontWeight: 600, color: "var(--p-ink)",
                       overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {r.name}
        </span>
        <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
          {files}
        </span>
      </span>

      {w.files ? (
        <span style={{ height: 12, borderRadius: 6, background: TRACK, display: "block" }}>
          <span style={{ display: "flex", gap: 2, height: "100%", width: "100%" }}>
            {w.exceptions === 0 ? (
              <span style={{ flex: 1, background: CLEAN, borderRadius: 6 }} />
            ) : (<>
              {w.resolved > 0 && (
                <span style={{ flex: w.resolved, background: RESOLVED, borderRadius: 6, minWidth: 4 }} />
              )}
              {w.open > 0 && (
                <span style={{ flex: w.open, background: OPEN, borderRadius: 6, minWidth: 4 }} />
              )}
            </>)}
          </span>
        </span>
      ) : (
        <span style={{ fontSize: 12, color: "var(--p-faint)" }}>No file checked</span>
      )}

      <span style={{ fontSize: 12.5, textAlign: "right", fontVariantNumeric: "tabular-nums",
                     color: "var(--p-muted)" }}>
        {!w.files ? "—" : !w.exceptions ? (
          <><b style={{ color: "var(--p-ink)" }}>Clean</b>
            <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
              no exceptions
            </span></>
        ) : (
          <><b style={{ color: "var(--p-ink)" }}>{w.open}</b> unresolved
            <span style={{ display: "block", fontSize: 11.5, color: "var(--p-faint)" }}>
              {w.resolved} resolved of {w.exceptions} exceptions
            </span></>
        )}
      </span>
    </>
  );

  const grid = { display: "grid", gridTemplateColumns: "minmax(120px, 180px) 1fr 170px",
                 alignItems: "center", gap: 14 } as const;
  return w.files ? (
    <Link to={filesPath(r)} title={tip} className="rb-row"
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
