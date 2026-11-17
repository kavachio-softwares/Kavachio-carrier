import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { InfoTip } from "./InfoTip";

type Status = "files_missing" | "waiting_on_you" | "exceptions_open"
  | "contract_ending" | "on_track" | "setup_missing";

export type ProgrammeRow = {
  program_id: number; name: string;
  /** The most urgent cause — the one status shown; `statuses` holds every one. */
  status: Status; statuses: Status[];
  brokers: number; brokers_without_setup: number;
  contracts: number; contract_ends_soon: string | null;
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
const STATUS: Record<Status, { label: string; cls: string }> = {
  files_missing: { label: "Files missing", cls: "b-crit" },
  waiting_on_you: { label: "Waiting on you", cls: "b-info" },
  exceptions_open: { label: "Exceptions open", cls: "b-warn" },
  contract_ending: { label: "Contract ending", cls: "b-warn" },
  on_track: { label: "On track", cls: "b-ok" },
  setup_missing: { label: "Setup missing", cls: "b-mut" },
};

// Rows shown before "Show all": the worst ones, since the list is worst first.
const FIRST = 5;

const monthName = (m: string) => {
  const [y, mo] = m.split("-").map(Number);
  return y && mo ? new Date(y, mo - 1, 1).toLocaleString("en-GB", { month: "long", year: "numeric" }) : m;
};
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/**
 * One row per programme — the top of the hierarchy (programme → broker →
 * contract → bordereau) — worst first, with the one next step for each.
 *
 * Every figure is read from the screen that owns it (see
 * /dashboard/programme-overview): this month's files from the Bordereau
 * Calendar, open problems from the Open Problems page, contracts from the
 * Contracts list. Clicking a row picks that programme, and the dashboard's
 * Broker Performance card then shows only its brokers.
 */
export default function ProgrammeOverview({ mga, selected, onSelect }: {
  mga: string;
  selected: SelectedProgramme | null;
  onSelect: (p: SelectedProgramme | null) => void;
}) {
  const [data, setData] = useState<Overview | null>(null);
  const [failed, setFailed] = useState(false);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    let live = true;
    setFailed(false);
    api.get<Overview>("/dashboard/programme-overview", { params: { mga } })
      .then(r => { if (live) setData(r.data); })
      .catch(() => { if (live) { setData(null); setFailed(true); } });
    return () => { live = false; };
  }, [mga]);

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
          "One row per programme, the ones that need you first. Status names what is waiting; "
          + "when more than one thing is, it says Needs attention (hover to see which). "
          + "Files missing — a bordereau is overdue or never arrived (any month). "
          + "Waiting on you — a dispute, a file on hold or a contract to sign. "
          + "Exceptions open — a broker's file still has exceptions to fix. "
          + `Contract ending — a contract ends within ${data?.expiring_days ?? 30} days. `
          + "Setup missing — no broker has a live Bordereau Setup, so their files can't be checked. "
          + "Files this month come from the Bordereau Calendar. Exceptions and Clean cover the files "
          + `checked in the last ${data?.days ?? 30} days, counted as Broker Performance counts them. `
          + "Click a programme to see only its brokers in Broker Performance."} />
        {data && (
          <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--p-faint)" }}>
            {waiting > 0 ? `${waiting} of ${rows.length} need you · `
              : rows.length ? "All on track · " : ""}
            {monthName(data.month)}
          </span>
        )}
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
                  <th>Files This Month</th><th>Clean</th><th>Open Exceptions</th>
                  <th className="l">Next Step</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(r => (
                  <Row key={r.program_id} r={r} days={data.days}
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

function Row({ r, days, picked, onPick }: {
  r: ProgrammeRow; days: number; picked: boolean; onPick: () => void;
}) {
  const setupMissing = r.status === "setup_missing";
  // One cause → its own name. More than one → "Needs attention", in the most
  // urgent cause's colour, with every cause named on hover; each still has its
  // own link under Next Step.
  const causes = setupMissing ? [] : r.statuses;
  const st = causes.length > 1
    ? { label: "Needs attention", cls: STATUS[r.status].cls }
    : STATUS[r.status];
  const hover = causes.length > 1
    ? causes.map(k => STATUS[k].label).join(" · ") : undefined;
  const missing = r.overdue + r.never;
  // Missing in earlier months, beyond this month's.
  const earlier = Math.max(r.files_missing - missing, 0);
  const cleanPct = r.files ? Math.round((r.clean_files / r.files) * 100) : null;
  const stop = (e: React.MouseEvent) => e.stopPropagation();
  const faint = { color: "var(--p-faint)" };

  return (
    <tr className="arow" onClick={onPick} tabIndex={0}
      onKeyDown={e => { if (e.key === "Enter") onPick(); }}
      title={picked ? "Showing only this programme in Broker Performance — click again to show all"
        : "Click to see only this programme's brokers in Broker Performance"}
      style={picked ? { background: "var(--p-primary-soft)",
                        boxShadow: "inset 4px 0 0 var(--p-primary)" } : undefined}>
      <td>
        <b>{r.name}</b>
        <div className="sub">
          {r.contract_ends_soon
            ? `A contract ends ${new Date(r.contract_ends_soon).toLocaleDateString("en-GB",
                { day: "numeric", month: "short", year: "numeric" })}`
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
        {setupMissing ? (
          <span style={faint}>Can't be checked yet</span>
        ) : r.due === 0 ? (
          <span style={faint}>Nothing due</span>
        ) : (<>
          <b>{r.received}</b> of {r.due} received
          {missing > 0 && <span style={{ color: "var(--p-crit-ink)", fontWeight: 600 }}> · {missing} overdue</span>}
          {r.late > 0 && <span style={{ color: "var(--p-warn-ink)" }}> · {r.late} late</span>}
        </>)}
        {!setupMissing && earlier > 0 && (
          <div className="sub" style={{ color: "var(--p-crit-ink)" }}>
            + {plural(earlier, "file", "files")} missing from earlier months
          </div>
        )}
      </td>
      <td title={r.files ? `${r.clean_files} of ${plural(r.files, "file", "files")} clean, last ${days} days` : `No file checked in the last ${days} days`}>
        {cleanPct == null ? <span style={faint}>—</span> : (
          <b style={{ color: cleanPct >= 80 ? "var(--p-ok-ink)" : cleanPct >= 50 ? "var(--p-warn-ink)" : "var(--p-crit-ink)" }}>
            {cleanPct}%
          </b>
        )}
      </td>
      <td title={r.exceptions
        ? `${r.exceptions_open} of ${r.exceptions} still open, across ${plural(r.files, "file", "files")} checked in the last ${days} days`
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
