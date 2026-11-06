// MOPR 41 — Exception Resolution Performance, the Monthly Report's third tab.
//
// How fast the problems the checks find get fixed, against the carrier's own
// target, and the age and owner of every problem still waiting (backend
// fix_performance.py). Same look as Data Quality: a one-line summary of the
// month, four tiles, how long problems have been waiting, then the detail.
// (How the month's problems were closed — fixed, approved, dismissed… — is
// still returned by the API as fixed.by_how, but not shown.)
//   41.1  average time from problem found to problem fixed
//   41.2  how many were fixed within the target (which the carrier sets here)
//   41.3  every problem still waiting: how old, and who has to act
// Two kinds of number sit side by side, and each says which it is: the month
// picked above (what was fixed in it), and right now (what is waiting today,
// from any month).
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Clock, Hourglass, Users2 } from "lucide-react";
import {
  getExceptionResolution, setFixTarget, type ExceptionResolutionReport, type FixGroupRow,
} from "../api/reports";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { fmtAge, fmtMonth, fmtMonthName, fmtTook, num, pct, plural, share } from "../utils/reportFormat";
import type { TabProps } from "./MonthlyReport";

const PAGE_SIZE = 10;

const SEVERITY: Record<string, { label: string; cls: string }> = {
  critical: { label: "Critical", cls: "b-crit" },
  warning: { label: "Warning", cls: "b-warn" },
  info: { label: "Info", cls: "b-mut" },
};

/** Which time a number belongs to: the month picked, or right now. */
function When({ children }: { children: ReactNode }) {
  return (
    <span style={{ marginLeft: "auto", fontSize: 10.5, fontWeight: 700, letterSpacing: ".06em",
                   textTransform: "uppercase", color: "var(--p-faint)" }}>
      {children}
    </span>
  );
}

/** "In October 2026, 1,240 problems were fixed, taking 2.3 days on average. …" */
function summaryLine(d: ExceptionResolutionReport, filtered: boolean): string {
  const f = d.fixed, o = d.open, month = fmtMonth(d.month);
  const fixedPart = f.problems > 0
    ? `In ${month}, ${plural(f.problems, "problem")} ${f.problems === 1 ? "was" : "were"} fixed, `
      + `taking ${fmtTook(f.avg_days)} on average. ${pct(f.within_target_pct)} `
      + `${f.within_target === 1 ? "was" : "were"} fixed within the ${d.target_days}-day target.`
    : `No problems were fixed in ${month}${filtered ? " for this selection" : ""}.`;
  const waitingPart = o.problems > 0
    ? ` ${plural(o.problems, "problem")} ${o.problems === 1 ? "is" : "are"} waiting to be fixed right now`
      + (o.past_target > 0 ? ` — ${num(o.past_target)} of them longer than the target.` : ", none longer than the target.")
    : " Nothing is waiting to be fixed.";
  return fixedPart + waitingPart;
}

/** One row per broker on each programme, most waiting longer than target first. */
function ByBrokerProgramme({ rows, month }: { rows: FixGroupRow[]; month: string }) {
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [rows]);
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const pageNow = Math.min(page, pageCount);
  const shown = rows.slice((pageNow - 1) * PAGE_SIZE, pageNow * PAGE_SIZE);
  const split = { borderLeft: "1px solid var(--p-border)" };
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        <Users2 className="ci" />
        <h3>By broker and programme</h3>
        <div className="right"><span className="sub">Most waiting longer than target first</span></div>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th colSpan={2} />
              <th colSpan={3} style={{ ...split, textAlign: "center" }}>Fixed in {fmtMonthName(month)}</th>
              <th colSpan={3} style={{ ...split, textAlign: "center" }}>Waiting now</th>
            </tr>
            <tr>
              <th className="l">Owner</th><th className="l">Programme</th>
              <th className="r" style={split}>Fixed</th><th className="r">Average fix time</th>
              <th className="r">Within target</th>
              <th className="r" style={split}>Waiting</th>
              <th className="r">Longer than target</th><th className="r">Longest waiting</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={8} className="muted" style={{ textAlign: "center" }}>No problems this month.</td></tr>
            )}
            {shown.map(r => (
              <tr key={`${r.broker_party_id}:${r.program_id}`}>
                <td className="l" style={{ fontWeight: 600 }}>{r.owner}</td>
                <td className="l">{r.programme_name}</td>
                <td className="r" style={split}>{num(r.fixed)}</td>
                <td className="r">{fmtTook(r.avg_days)}</td>
                <td className="r" style={{ whiteSpace: "nowrap" }}>
                  {r.fixed > 0 ? <><b>{num(r.within_target)} of {num(r.fixed)}</b>{" "}
                    <span className="muted">({pct(r.within_target_pct)})</span></> : <span className="muted">—</span>}
                </td>
                <td className="r" style={split}>{num(r.open)}</td>
                <td className="r" style={{ fontWeight: 600, color: r.past_target > 0 ? "var(--p-crit-ink)" : undefined }}>
                  {num(r.past_target)}
                </td>
                <td className="r muted">{r.open > 0 ? fmtAge(r.oldest_days) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={pageNow} pageCount={pageCount} pageSize={PAGE_SIZE}
        totalItems={rows.length} onPageChange={setPage} noun="rows" />
    </div>
  );
}

export default function ExceptionResolution({ filters, onLists }: TabProps) {
  const { month, programId, brokerId } = filters;
  const [severity, setSeverity] = useState("");
  const [openPage, setOpenPage] = useState(1);
  const [data, setData] = useState<ExceptionResolutionReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  // The target, changed in place under its tile: open or not, what is typed,
  // and what saving it said.
  const [editing, setEditing] = useState(false);
  const [targetDraft, setTargetDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [targetMsg, setTargetMsg] = useState<string | null>(null);

  // A new month, filter or severity starts the waiting list from its first page.
  useEffect(() => { setOpenPage(1); }, [month, programId, brokerId, severity]);

  useEffect(() => {
    let live = true;
    setLoading(true); setErr(null);
    getExceptionResolution({ month, program_id: programId, broker_party_id: brokerId,
                             page: openPage, page_size: PAGE_SIZE, severity })
      .then(d => { if (live) { setData(d); onLists(d); setTargetDraft(String(d.target_days)); } })
      .catch(e => { if (live) setErr(e?.response?.data?.detail ?? "Could not load the report."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [month, programId, brokerId, openPage, severity, reload, onLists]);

  async function saveTarget() {
    const days = Number(targetDraft);
    if (!Number.isInteger(days) || days < 1 || days > 365) {
      setTargetMsg("Enter a whole number of days from 1 to 365.");
      return;
    }
    setSaving(true); setTargetMsg(null);
    try {
      const r = await setFixTarget(days);
      setTargetMsg(r.was === r.target_days ? "No change."
        : `Saved — the target is now ${plural(r.target_days, "day")} (was ${r.was}).`);
      setEditing(false);
      setReload(n => n + 1);
    } catch (e: any) {
      setTargetMsg(e?.response?.data?.detail ?? "Could not save the target.");
    } finally {
      setSaving(false);
    }
  }

  const f = data?.fixed;
  const o = data?.open;
  const target = data?.target_days;
  const filtered = programId != null || brokerId != null;
  const first = loading && !data;
  const monthName = fmtMonthName(month);

  return (
    <>
      {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

      {/* ---- the month in one sentence ---------------------------------- */}
      <div style={{ display: "flex", gap: 14, alignItems: "flex-start", marginBottom: 18,
                    background: "var(--p-primary-soft)", border: "1px solid #C7E3E7",
                    borderRadius: "var(--p-r)", padding: "16px 20px" }}>
        <span style={{ flex: "none", width: 36, height: 36, borderRadius: 18, background: "var(--p-surface)",
                       display: "inline-flex", alignItems: "center", justifyContent: "center",
                       color: "var(--p-primary)" }}>
          <Clock size={18} aria-hidden="true" />
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ fontSize: 12, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase",
                        color: "var(--p-info-ink)" }}>
            {fmtMonth(month)} at a glance
          </div>
          <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.55, color: "var(--p-ink)" }}>
            {first || !data ? "Loading…" : summaryLine(data, filtered)}
          </p>
        </div>
      </div>

      {/* ---- the four tiles: two for the month, two for right now -------- */}
      <div className="tiles" style={{ marginBottom: 18 }}>
        <div className="tile">
          <div className="k">
            Average fix time
            <InfoTip text="For the problems fixed this month: the time from when each was first found to when it was fixed, on average." />
            <When>{monthName}</When>
          </div>
          <div className="v">{first ? "…" : fmtTook(f?.avg_days)}</div>
        </div>
        <div className="tile">
          <div className="k">
            Fixed within target
            <InfoTip text="Problems fixed this month in no more than the target time. Each change to the target is saved in Audit Logs with the old and new value." />
            <When>{monthName}</When>
          </div>
          <div className="v" style={{ color: "var(--p-ok-ink)" }}>
            {first ? "…" : `${num(f?.within_target)} of ${num(f?.problems)}`}{" "}
            <small>{f?.within_target_pct != null ? `(${pct(f.within_target_pct)})` : ""}</small>
          </div>
          {editing ? (
            <div className="foot" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--p-ink)" }}>
                Target
                <input type="number" min={1} max={365} value={targetDraft}
                  onChange={e => setTargetDraft(e.target.value)} aria-label="Target in days"
                  style={{ width: 64, height: 32, padding: "0 8px", border: "1px solid var(--p-border-2)",
                           borderRadius: "var(--p-r-sm)", font: "inherit" }} />
                days
              </label>
              <button type="button" className="btn sm pri" onClick={saveTarget}
                disabled={saving || targetDraft === String(target ?? "")}>
                {saving ? "Saving…" : "Save"}
              </button>
              <button type="button" className="btn sm" disabled={saving}
                onClick={() => { setEditing(false); setTargetDraft(String(target ?? "")); setTargetMsg(null); }}>
                Cancel
              </button>
            </div>
          ) : (
            <div className="foot" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {target != null ? `Target: ${plural(target, "day")}` : " "}
              {target != null && (
                <button type="button" className="btn sm" onClick={() => { setEditing(true); setTargetMsg(null); }}>
                  Change
                </button>
              )}
            </div>
          )}
          {targetMsg && <div className="foot">{targetMsg}</div>}
        </div>
        <div className="tile">
          <div className="k">
            Waiting to be fixed
            <InfoTip text="Every problem not fixed yet, whatever month it was found in." />
            <When>Right now</When>
          </div>
          <div className="v">{first ? "…" : num(o?.problems)}</div>
          <div className="foot">
            {o && o.problems > 0 ? `From all months · longest waiting: ${fmtAge(o.oldest_days).toLowerCase()}` : " "}
          </div>
        </div>
        <div className="tile" style={(o?.past_target ?? 0) > 0 ? { borderColor: "#F3D2D7" } : undefined}>
          <div className="k">
            Waiting longer than target
            <InfoTip text="Problems waiting longer than the target — the ones to chase first." />
            <When>Right now</When>
          </div>
          <div className="v" style={{ color: (o?.past_target ?? 0) > 0 ? "var(--p-crit-ink)" : undefined }}>
            {first ? "…" : num(o?.past_target)}
          </div>
        </div>
      </div>

      {/* ---- 41.3: how old the waiting problems are ----------------------- */}
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h">
          <Hourglass className="ci" />
          <h3>How long problems have been waiting</h3>
          <InfoTip text="Every problem waiting to be fixed now, by how long ago it was found." />
          <When>Right now</When>
        </div>
        <div className="tbl-wrap">
          <table>
            <thead><tr><th className="l">Waiting for</th><th className="r">Problems</th><th className="r">Share</th></tr></thead>
            <tbody>
              {(o?.by_age ?? []).map(b => (
                <tr key={b.key}>
                  <td className="l" style={{ fontWeight: 600 }}>
                    {b.label}
                    {b.past_target && b.problems > 0 && (
                      <span className="badge b-crit" style={{ marginLeft: 10 }}><span className="d" />Past target</span>
                    )}
                  </td>
                  <td className="r">{num(b.problems)}</td>
                  <td className="r muted">{share(b.problems, o?.problems ?? 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <ByBrokerProgramme rows={data?.by_broker_programme ?? []} month={month} />

      {/* ---- 41.3: everything waiting, one row per file and problem ------ */}
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ flexWrap: "wrap" }}>
          <Hourglass className="ci" />
          <h3>Waiting to be fixed</h3>
          <InfoTip text="Every problem not fixed yet, one row per file and problem, longest waiting first. The owner is who has to act: the broker whose file it is, or the carrier for a file no broker sent." />
          <div className="right">
            <label style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
              <span className="sub">Severity</span>
              <select className="fbar-select" value={severity} onChange={e => setSeverity(e.target.value)}>
                <option value="">All</option>
                <option value="critical">Critical</option>
                <option value="warning">Warning</option>
                <option value="info">Info</option>
              </select>
            </label>
          </div>
        </div>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th className="l">Owner</th><th className="l">Programme</th>
                <th className="l">Waiting for</th><th className="l">Problem</th><th className="r">Severity</th>
                <th className="r">Problems</th><th className="l">File</th><th className="r">Action</th>
              </tr>
            </thead>
            <tbody>
              {(o?.groups ?? []).length === 0 && (
                <tr><td colSpan={8} className="muted" style={{ textAlign: "center" }}>
                  {severity ? "Nothing of this severity is waiting." : "Nothing is waiting."}
                </td></tr>
              )}
              {(o?.groups ?? []).map((g, i) => {
                const sev = g.severity ? SEVERITY[g.severity] : null;
                return (
                  <tr key={`${g.export_id}:${g.check}:${i}`}>
                    <td className="l" style={{ fontWeight: 600 }}>{g.owner}</td>
                    <td className="l">{g.programme_name}</td>
                    <td className="l" style={{ whiteSpace: "nowrap", fontWeight: 600,
                                                color: g.past_target > 0 ? "var(--p-crit-ink)" : undefined }}>
                      {fmtAge(g.oldest_days)}
                      {g.past_target > 0 && (
                        <div className="sub">
                          {g.past_target === g.problems ? "Past target" : `${num(g.past_target)} past target`}
                        </div>
                      )}
                    </td>
                    <td className="l" style={{ fontWeight: 600 }}>{g.check ?? "—"}</td>
                    <td className="r">
                      {sev ? <span className={`badge ${sev.cls}`}><span className="d" />{sev.label}</span>
                           : <span className="muted">—</span>}
                    </td>
                    <td className="r" style={{ fontWeight: 600 }}>{num(g.problems)}</td>
                    <td className="l">
                      {g.file ?? "—"}
                      {g.period && <div className="sub">Period {g.period}</div>}
                    </td>
                    <td className="r">
                      {g.export_id != null
                        ? <Link className="btn sm" to={`/uploads/${g.export_id}/exceptions?download=${g.export_id}`}>Review →</Link>
                        : <span className="muted">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {o && (
          <Pagination page={o.page} pageCount={Math.max(1, Math.ceil(o.groups_total / o.page_size))}
            pageSize={o.page_size} totalItems={o.groups_total} onPageChange={setOpenPage} noun="rows" />
        )}
      </div>
    </>
  );
}
