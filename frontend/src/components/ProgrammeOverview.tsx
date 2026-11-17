import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { InfoTip } from "./InfoTip";

type Status = "files_missing" | "waiting_on_you" | "exceptions_open"
  | "contract_ended" | "contract_ending" | "on_track" | "setup_missing";

export type ProgrammeRow = {
  program_id: number; name: string;
  /** The most urgent cause — the one status shown; `statuses` holds every one. */
  status: Status; statuses: Status[];
  brokers: number; brokers_without_setup: number;
  contracts: number; contract_ends_soon: string | null;
  /** Every contract has passed its end date (the calendar's last day): nothing more falls due. */
  contract_ended: string | null;
  // This month's bordereaux, from the Bordereau Calendar.
  due: number; received: number; late: number; overdue: number; never: number;
  /** Bordereaux still late or never sent, any month. */
  files_missing: number;
  // Live files checked in the last `days`, tallied as Broker Performance does.
  files: number; clean_files: number;
  exceptions: number; exceptions_open: number;
  /** The exception screen of the file with the most still open. */
  exceptions_link: string | null;
  waiting_on_you: number;
  next_steps: { text: string; link: string }[];
};

type Overview = {
  month: string; days: number; expiring_days: number;
  items: ProgrammeRow[];
  status_counts: Record<Status, number>;
};

export type SelectedProgramme = { id: number; name: string };

// Each status names its cause, so the badge says what is wrong.
// `hint` is what the badge means, shown when you hover over it — the heading's
// ⓘ stays short, so each status explains itself where it is shown.
const STATUS: Record<Status, { label: string; cls: string; hint: string }> = {
  files_missing: { label: "Files missing", cls: "b-crit", hint: "A bordereau is overdue or never arrived (any month)" },
  waiting_on_you: { label: "Waiting on you", cls: "b-info", hint: "A dispute, a file on hold or a contract to sign" },
  exceptions_open: { label: "Exceptions open", cls: "b-warn", hint: "A broker's file still has exceptions to fix" },
  contract_ended: { label: "Contract ended", cls: "b-crit", hint: "A contract on this programme has ended" },
  contract_ending: { label: "Contract ending", cls: "b-warn", hint: "A contract ends soon — time to renew" },
  on_track: { label: "On track", cls: "b-ok", hint: "Nothing is waiting" },
  setup_missing: { label: "Setup missing", cls: "b-mut", hint: "No broker has a live Bordereau Setup, so files can't be checked" },
};

// Rows shown before "Show all": the worst ones, since the list is worst first.
const FIRST = 5;

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const fmtDate = (d: string) => new Date(d).toLocaleDateString("en-GB",
  { day: "numeric", month: "short", year: "numeric" });

/**
 * One row per programme — the top of the hierarchy (programme → broker →
 * contract → bordereau) — worst first, with the one next step for each.
 *
 * Every figure is read from the screen that owns it (see
 * /dashboard/programme-overview): this month's files from the Bordereau
 * Calendar, open problems from the Open Problems page, contracts from the
 * Contracts list. Clicking a row picks that programme, and every card under
 * the dashboard's period pick then shows only that programme.
 *
 * `period` is that pick (YYYY-MM, YYYY or all): the bordereaux due in it and
 * the files checked in it. `periodLabel` names it ("November 2026").
 */
export default function ProgrammeOverview({ mga, period, periodLabel, selected, onSelect }: {
  mga: string;
  period: string;
  periodLabel: string;
  selected: SelectedProgramme | null;
  onSelect: (p: SelectedProgramme | null) => void;
}) {
  const [data, setData] = useState<Overview | null>(null);
  const [failed, setFailed] = useState(false);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    let live = true;
    setFailed(false);
    setData(null);
    api.get<Overview>("/dashboard/programme-overview", { params: { mga, period } })
      .then(r => { if (live) setData(r.data); })
      .catch(() => { if (live) { setData(null); setFailed(true); } });
    return () => { live = false; };
  }, [mga, period]);

  const rows = data?.items ?? [];
  // A picked programme further down the list stays in view.
  const shown = showAll ? rows
    : rows.filter((r, i) => i < FIRST || r.program_id === selected?.id);
  const waiting = rows.filter(r => r.status !== "on_track").length;

  return (
    <div className="card" style={{ padding: 0, marginBottom: 24, overflow: "hidden" }}>
      <div className="card-h" style={{ padding: "20px 20px 16px", margin: 0 }}>
        <h3>Programme Overview</h3>
        <InfoTip text={
          "One row per programme, those needing you first. Status says what is waiting.\n"
          + `Files due, Clean and Open Exceptions are ${periodLabel === "All years" ? "for all years" : `for ${periodLabel}`}.\n`
          + "Click a programme to show only it in every card below; click again to show all."} />
        <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--p-faint)" }}>
          {data && (waiting > 0 ? `${waiting} of ${rows.length} need you · `
            : rows.length ? "All on track · " : "")}
          {periodLabel}
        </span>
      </div>

      {data === null ? (
        <div className="empty">{failed ? "The programme overview could not be loaded." : "Loading…"}</div>
      ) : rows.length === 0 ? (
        <div className="empty">No programmes yet. <Link className="linkish" to="/programs/new">Add a programme →</Link></div>
      ) : (
        <>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Programme</th><th>Status</th><th>Brokers</th><th>Contracts</th>
                  <th>Files Due</th><th>Clean</th><th>Open Exceptions</th>
                  <th className="l">Next Step</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(r => (
                  <Row key={r.program_id} r={r} periodLabel={periodLabel}
                    picked={selected?.id === r.program_id}
                    onPick={() => onSelect(selected?.id === r.program_id
                      ? null : { id: r.program_id, name: r.name })} />
                ))}
              </tbody>
            </table>
          </div>
          {rows.length > FIRST && (
            <div style={{ padding: "12px 20px", fontSize: 13, textAlign: "right",
                          borderTop: "1px solid var(--p-border)" }}>
              <button type="button" className="linkish"
                style={{ background: "none", border: 0, cursor: "pointer", padding: 0 }}
                onClick={() => setShowAll(v => !v)}>
                {showAll ? "Show fewer" : `Show all ${rows.length} programmes`}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Row({ r, periodLabel, picked, onPick }: {
  r: ProgrammeRow; periodLabel: string; picked: boolean; onPick: () => void;
}) {
  const inPeriod = periodLabel === "All years" ? "so far" : `in ${periodLabel}`;
  // No live setup stops files being checked, not being owed — so the files due
  // still show, and the missing setup is one cause among the others.
  const setupMissing = r.statuses.includes("setup_missing");
  // One cause → its own name. More than one → "Needs attention", in the most
  // urgent cause's colour, with every cause named on hover; each still has its
  // own link under Next Step.
  const causes = r.statuses;
  const st = causes.length > 1
    ? { label: "Needs attention", cls: STATUS[r.status].cls }
    : STATUS[r.status];
  const hover = causes.length > 1
    ? causes.map(k => `${STATUS[k].label}: ${STATUS[k].hint}`).join("\n")
    : STATUS[r.status].hint;
  const missing = r.overdue + r.never;
  // Missing in other months, beyond the picked period's.
  const earlier = Math.max(r.files_missing - missing, 0);
  const cleanPct = r.files ? Math.round((r.clean_files / r.files) * 100) : null;
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  const faint = { color: "var(--p-faint)" };

  return (
    <tr className="arow" onClick={onPick} tabIndex={0}
      onKeyDown={e => { if (e.key === "Enter") onPick(); }}
      title={picked ? "Showing only this programme in the cards below — click again to show all"
        : "Click to show only this programme in the cards below"}
      style={picked ? { background: "var(--p-primary-soft)",
                        boxShadow: "inset 4px 0 0 var(--p-primary)" } : undefined}>
      <td>
        <b>{r.name}</b>
        <div className="sub">
          {r.contract_ends_soon
            ? `A contract ends ${fmtDate(r.contract_ends_soon)}`
            : r.brokers_without_setup && !setupMissing
              ? `${plural(r.brokers_without_setup, "broker", "brokers")} without a setup`
              : null}
        </div>
      </td>
      <td>
        <span className={`badge ${st.cls}`} title={hover}>
          <span className="d" />{st.label}
        </span>
      </td>
      <td>{r.brokers}</td>
      <td>{r.contracts}</td>
      <td>
        {r.due === 0 && r.contract_ended ? (
          <span style={{ color: "var(--p-crit-ink)", fontWeight: 600 }}>
            Contract ended {fmtDate(r.contract_ended)}
          </span>
        ) : r.due === 0 && setupMissing ? (
          <span style={faint}>Can't be checked yet</span>
        ) : r.due === 0 ? (
          <span style={faint}>Nothing due</span>
        ) : (<>
          <b>{r.received}</b> of {r.due} received
          {missing > 0 && <span style={{ color: "var(--p-crit-ink)", fontWeight: 600 }}> · {missing} overdue</span>}
          {r.late > 0 && <span style={{ color: "var(--p-warn-ink)" }}> · {r.late} late</span>}
        </>)}
        {earlier > 0 && (
          <div className="sub" style={{ color: "var(--p-crit-ink)" }}>
            {plural(earlier, "file", "files")} missing from other months
          </div>
        )}
      </td>
      <td title={r.files ? `${r.clean_files} of ${plural(r.files, "file", "files")} clean, ${inPeriod}` : `No file checked ${inPeriod}`}>
        {cleanPct == null ? <span style={faint}>—</span> : (
          <b style={{ color: cleanPct >= 80 ? "var(--p-ok-ink)" : cleanPct >= 50 ? "var(--p-warn-ink)" : "var(--p-crit-ink)" }}>
            {cleanPct}%
          </b>
        )}
      </td>
      <td title={r.exceptions
        ? `${r.exceptions_open} of ${r.exceptions} still open, across ${plural(r.files, "file", "files")} checked ${inPeriod}`
        : undefined}>
        {r.exceptions_open === 0 ? (
          <span style={faint}>{r.files ? "None" : "—"}</span>
        ) : r.exceptions_link ? (
          <Link className="linkish" to={r.exceptions_link} onClick={stop}
            title="Open the file with the most exceptions">{r.exceptions_open}</Link>
        ) : r.exceptions_open}
      </td>
      <td className="l">
        {r.next_steps.length === 0 ? (
          <span style={faint}>Nothing to do</span>
        ) : r.next_steps.map(st => (
          <div key={st.link + st.text}>
            {/* The Open Problems page is hidden (17 Nov 2026): that step shows as plain text. */}
            {st.link.startsWith("/open-problems")
              ? <span>{st.text}</span>
              : <Link className="linkish" to={st.link} onClick={stop}>{st.text} →</Link>}
          </div>
        ))}
      </td>
    </tr>
  );
}
