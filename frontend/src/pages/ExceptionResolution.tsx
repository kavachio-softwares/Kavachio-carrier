// MOPR 41 — Exception Resolution Performance.
//
// How fast the problems the checks find get fixed, against the carrier's own
// target, and the age and owner of every problem still open (backend
// fix_performance.py). Same look as Data Quality: a one-line summary of the
// month, four tiles, two small tables, then the detail. Numbers only — no charts.
//   41.1  average time from problem found to problem fixed
//   41.2  how many were fixed within the target (which the carrier sets here)
//   41.3  every problem still open: how old, and who has to act
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Clock, Hourglass, ListChecks, Target, Users2 } from "lucide-react";
import {
  getExceptionResolution, setFixTarget, type ExceptionResolutionReport, type FixGroupRow,
} from "../api/reports";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";

/** "2026-10" → "October 2026". */
function fmtMonth(key: string): string {
  const [y, m] = key.split("-").map(Number);
  if (!y || !m) return key;
  return new Date(y, m - 1, 1).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

/** This month and the eleven before it, newest first. */
function lastMonths(n = 12): string[] {
  const now = new Date();
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  });
}

const num = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("en-GB"));
const pct = (n: number | null | undefined) => (n == null ? "—" : `${n}%`);
const plural = (n: number, w: string, ws = `${w}s`) => `${num(n)} ${n === 1 ? w : ws}`;
const share = (n: number, of: number) => (of > 0 ? `${Math.round((100 * n) / of)}%` : "—");

/** A fix time: hours under a day, else days to one decimal. */
function fmtTook(d: number | null | undefined): string {
  if (d == null) return "—";
  if (d < 1) return `${Math.max(1, Math.round(d * 24))} h`;
  const r = Math.round(d * 10) / 10;
  return `${Number.isInteger(r) ? r : r.toFixed(1)} ${r === 1 ? "day" : "days"}`;
}

/** How long a problem has been open, in whole days. */
function fmtAge(d: number | null | undefined): string {
  if (d == null) return "—";
  return d < 1 ? "Under 1 day" : plural(Math.floor(d), "day");
}

const PAGE_SIZE = 10;

const SEVERITY: Record<string, { label: string; cls: string }> = {
  critical: { label: "Critical", cls: "b-crit" },
  warning: { label: "Warning", cls: "b-warn" },
  info: { label: "Info", cls: "b-mut" },
};

/** "In October 2026, 1,240 problems were fixed, taking 2.3 days on average. …" */
function summaryLine(d: ExceptionResolutionReport, filtered: boolean): string {
  const f = d.fixed, o = d.open, month = fmtMonth(d.month);
  const fixedPart = f.problems > 0
    ? `In ${month}, ${plural(f.problems, "problem")} ${f.problems === 1 ? "was" : "were"} fixed, `
      + `taking ${fmtTook(f.avg_days)} on average. ${pct(f.within_target_pct)} `
      + `${f.within_target === 1 ? "was" : "were"} fixed within the ${d.target_days}-day target.`
    : `No problems were fixed in ${month}${filtered ? " for this selection" : ""}.`;
  const openPart = o.problems > 0
    ? ` ${plural(o.problems, "problem")} ${o.problems === 1 ? "is" : "are"} open now`
      + (o.past_target > 0 ? ` — ${num(o.past_target)} of them past the target.` : ", none past the target.")
    : " Nothing is open now.";
  return fixedPart + openPart;
}

/** One row per broker on each programme, most past target first. */
function ByBrokerProgramme({ rows }: { rows: FixGroupRow[] }) {
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [rows]);
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const pageNow = Math.min(page, pageCount);
  const shown = rows.slice((pageNow - 1) * PAGE_SIZE, pageNow * PAGE_SIZE);
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        <Users2 className="ci" />
        <h3>By broker and programme</h3>
        <div className="right"><span className="sub">Most past target first</span></div>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th className="l">Broker</th><th className="l">Programme</th>
              <th className="r">Fixed</th><th className="r">Average fix time</th>
              <th className="r">Within target</th><th className="r">Open now</th>
              <th className="r">Past target</th><th className="r">Oldest open</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={8} className="muted" style={{ textAlign: "center" }}>No problems this month.</td></tr>
            )}
            {shown.map(r => (
              <tr key={`${r.broker_party_id}:${r.program_id}`}>
                <td className="l" style={{ fontWeight: 600 }}>{r.broker_party_id ? r.broker_name : "No broker (carrier)"}</td>
                <td className="l">{r.programme_name}</td>
                <td className="r">{num(r.fixed)}</td>
                <td className="r">{fmtTook(r.avg_days)}</td>
                <td className="r" style={{ whiteSpace: "nowrap" }}>
                  {r.fixed > 0 ? <><b>{num(r.within_target)} of {num(r.fixed)}</b>{" "}
                    <span className="muted">({pct(r.within_target_pct)})</span></> : <span className="muted">—</span>}
                </td>
                <td className="r">{num(r.open)}</td>
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

export default function ExceptionResolution() {
  const months = useMemo(() => lastMonths(), []);
  const [month, setMonth] = useState(months[0]);
  const [programId, setProgramId] = useState<number | null>(null);
  const [brokerId, setBrokerId] = useState<number | null>(null);
  const [openPage, setOpenPage] = useState(1);
  const [data, setData] = useState<ExceptionResolutionReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  // The target box: what is typed, and what saving it said.
  const [targetDraft, setTargetDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [targetMsg, setTargetMsg] = useState<string | null>(null);

  // A new month or filter starts the open list from its first page.
  useEffect(() => { setOpenPage(1); }, [month, programId, brokerId]);

  useEffect(() => {
    let live = true;
    setLoading(true); setErr(null);
    getExceptionResolution({ month, program_id: programId, broker_party_id: brokerId,
                             page: openPage, page_size: PAGE_SIZE })
      .then(d => { if (live) { setData(d); setTargetDraft(String(d.target_days)); } })
      .catch(e => { if (live) setErr(e?.response?.data?.detail ?? "Could not load the report."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [month, programId, brokerId, openPage, reload]);

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

  return (
    <div className="proto">
      <div className="view full">
        {/* ---- title, filters --------------------------------------------- */}
        <div className="page-head" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="t">
            <h2>Exception Resolution</h2>
            <p>How fast problems found in files get fixed, and what is still open.</p>
          </div>
          <div className="actions" style={{ alignItems: "flex-end" }}>
            <label className="fbar-field">
              <span className="sub">Month</span>
              <select className="fbar-select" value={month} onChange={e => setMonth(e.target.value)}>
                {months.map(m => <option key={m} value={m}>{fmtMonth(m)}</option>)}
              </select>
            </label>
            <label className="fbar-field">
              <span className="sub">Programme</span>
              <select className="fbar-select" value={programId ?? ""}
                onChange={e => setProgramId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All programmes</option>
                {(data?.programmes ?? []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            <label className="fbar-field">
              <span className="sub">Broker</span>
              <select className="fbar-select" value={brokerId ?? ""}
                onChange={e => setBrokerId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All brokers</option>
                {(data?.brokers ?? []).map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
              </select>
            </label>
          </div>
        </div>

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

        {/* ---- 41.2: the target, set by the carrier ------------------------ */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h" style={{ flexWrap: "wrap", gap: 12, borderBottom: "none" }}>
            <Target className="ci" />
            <h3>Target</h3>
            <InfoTip text="How quickly every problem should be fixed. Each change is saved in Audit Logs with the old and new value." />
            <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 14 }}>
              Fix every problem within
              <input type="number" min={1} max={365} value={targetDraft}
                onChange={e => setTargetDraft(e.target.value)}
                style={{ width: 72, height: 36, padding: "0 10px", border: "1px solid var(--p-border-2)",
                         borderRadius: "var(--p-r-sm)", font: "inherit" }} aria-label="Target in days" />
              days
            </label>
            <button type="button" className="btn sm pri" onClick={saveTarget}
              disabled={saving || targetDraft === String(target ?? "")}>
              {saving ? "Saving…" : "Save"}
            </button>
            {targetMsg && <span className="sub">{targetMsg}</span>}
          </div>
        </div>

        {/* ---- the four tiles --------------------------------------------- */}
        <div className="tiles" style={{ marginBottom: 18 }}>
          <div className="tile">
            <div className="k">
              Average fix time
              <InfoTip text="For the problems fixed this month: the time from when each was first found to when it was fixed, on average." />
            </div>
            <div className="v">{first ? "…" : fmtTook(f?.avg_days)}</div>
          </div>
          <div className="tile">
            <div className="k">
              Fixed within target
              <InfoTip text="Problems fixed this month in no more than the target time." />
            </div>
            <div className="v" style={{ color: "var(--p-ok-ink)" }}>
              {first ? "…" : `${num(f?.within_target)} of ${num(f?.problems)}`}{" "}
              <small>{f?.within_target_pct != null ? `(${f.within_target_pct}%)` : ""}</small>
            </div>
            <div className="foot">{target != null ? `Target: ${plural(target, "day")}` : " "}</div>
          </div>
          <div className="tile">
            <div className="k">
              Waiting to be fixed
              <InfoTip text="Every problem not fixed yet, whatever month it was found in." />
            </div>
            <div className="v">{first ? "…" : num(o?.problems)}</div>
            <div className="foot">
              {o && o.problems > 0 ? `From all months · longest waiting: ${fmtAge(o.oldest_days).toLowerCase()}` : " "}
            </div>
          </div>
          <div className="tile" style={(o?.past_target ?? 0) > 0 ? { borderColor: "#F3D2D7" } : undefined}>
            <div className="k">
              Waiting longer than target
              <InfoTip text="Problems waiting longer than the target — the ones to chase first." />
            </div>
            <div className="v" style={{ color: (o?.past_target ?? 0) > 0 ? "var(--p-crit-ink)" : undefined }}>
              {first ? "…" : num(o?.past_target)}
            </div>
          </div>
        </div>

        <div className="grid g-2" style={{ marginBottom: 18 }}>
          {/* ---- how the month's problems were closed ---------------------- */}
          <div className="card">
            <div className="card-h">
              <ListChecks className="ci" />
              <h3>How problems were resolved</h3>
              <InfoTip text="Fixed, approved as it is, dismissed or rejected are decisions someone made on the problem. Gone in a corrected version means a later file no longer had it." />
            </div>
            <div className="tbl-wrap">
              <table>
                <thead><tr><th className="l">Way</th><th className="r">Problems</th><th className="r">Share</th></tr></thead>
                <tbody>
                  {(f?.by_how ?? []).map(h => (
                    <tr key={h.key}>
                      <td className="l" style={{ fontWeight: 600 }}>{h.label}</td>
                      <td className="r">{num(h.problems)}</td>
                      <td className="r muted">{share(h.problems, f?.problems ?? 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* ---- 41.3: how old the open problems are ------------------------ */}
          <div className="card">
            <div className="card-h">
              <Hourglass className="ci" />
              <h3>How long problems have been open</h3>
              <InfoTip text="Every problem still open now, by how long ago it was found." />
            </div>
            <div className="tbl-wrap">
              <table>
                <thead><tr><th className="l">Open for</th><th className="r">Problems</th><th className="r">Share</th></tr></thead>
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
        </div>

        <ByBrokerProgramme rows={data?.by_broker_programme ?? []} />

        {/* ---- 41.3: every open problem, oldest first ---------------------- */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <Hourglass className="ci" />
            <h3>Waiting to be fixed</h3>
            <InfoTip text="Every problem not fixed yet, oldest first. The owner is who has to act: the broker whose file it is, or the carrier for a file no broker sent." />
            <div className="right"><span className="sub">Oldest first</span></div>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="l">Owner</th><th className="l">Programme</th>
                  <th className="l">Open for</th><th className="l">Problem</th><th className="r">Severity</th>
                  <th className="l">Policy / row</th><th className="l">File</th><th className="r">Action</th>
                </tr>
              </thead>
              <tbody>
                {(o?.items ?? []).length === 0 && (
                  <tr><td colSpan={8} className="muted" style={{ textAlign: "center" }}>Nothing is open.</td></tr>
                )}
                {(o?.items ?? []).map((p, i) => {
                  const sev = p.severity ? SEVERITY[p.severity] : null;
                  return (
                    <tr key={`${p.export_id}:${p.sheet}:${p.row}:${p.field}:${p.check}:${i}`}>
                      <td className="l" style={{ fontWeight: 600 }}>{p.owner}</td>
                      <td className="l">{p.programme_name}</td>
                      <td className="l" style={{ whiteSpace: "nowrap", fontWeight: 600,
                                                  color: p.past_target ? "var(--p-crit-ink)" : undefined }}>
                        {fmtAge(p.age_days)}
                        {p.past_target && <div className="sub">Past target</div>}
                      </td>
                      <td className="l">
                        <div style={{ fontWeight: 600 }}>{p.check ?? "—"}</div>
                        {p.message && <div className="sub">{p.message}</div>}
                      </td>
                      <td className="r">
                        {sev ? <span className={`badge ${sev.cls}`}><span className="d" />{sev.label}</span>
                             : <span className="muted">—</span>}
                      </td>
                      <td className="l">
                        {p.policy_number ?? (p.row != null ? `Row ${p.row}` : "—")}
                        {p.field && <div className="sub">{p.field}</div>}
                      </td>
                      <td className="l">
                        {p.file ?? "—"}
                        {p.period && <div className="sub">Period {p.period}</div>}
                      </td>
                      <td className="r">
                        {p.export_id != null
                          ? <Link className="btn sm" to={`/uploads/${p.export_id}/exceptions?download=${p.export_id}`}>Review →</Link>
                          : <span className="muted">—</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {o && (
            <Pagination page={o.page} pageCount={Math.max(1, Math.ceil(o.total / o.page_size))}
              pageSize={o.page_size} totalItems={o.total} onPageChange={setOpenPage} noun="problems" />
          )}
        </div>
      </div>
    </div>
  );
}
