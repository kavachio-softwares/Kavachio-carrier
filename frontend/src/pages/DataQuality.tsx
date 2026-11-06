// MOPR 40 — Data Quality & Volumes, the Monthly Report's second tab.
//
// One month, read from the shared check record (backend check_record.py) that
// every check writes to — the file-arrival checks, the quick lane and the full
// data-model lane alike:
//   40.2  files and rows handled, policies split new / renewed / cancelled
//   40.3  how many files were clean first time
//   40.1  what the checks found: passed, failed, could not run, and the errors
//
// Layout as agreed in the Monthly Report proposal: a one-line summary of the
// month, four tiles each beside last month, transaction types beside the
// checks, and the checks that failed most. The split by broker and programme
// is not shown here (it stays in the API and in the Monthly Report's PDF /
// Excel). The month, programme and broker are picked above the tabs
// (MonthlyReport).
import { useEffect, useState } from "react";
import { AlertTriangle, BarChart3, LineChart, ListChecks, ShieldCheck } from "lucide-react";
import { getDataQuality, type DataQualityReport, type TxnGroup } from "../api/reports";
import { Delta } from "../components/Delta";
import { InfoTip } from "../components/InfoTip";
import { fmtMonth, fmtMonthName, num, pct, plural, share, shiftMonth } from "../utils/reportFormat";
import type { TabProps } from "./MonthlyReport";

// One colour per transaction type, told apart by lightness as well as hue.
const TYPE_COLOUR: Record<TxnGroup["key"], string> = {
  new: "#0E5E6F", renewed: "#5FB1BE", cancelled: "#E08A3C",
  other: "#7C5CBF", unclassified: "#C3C9D4",
};

const SEVERITY: Record<string, { label: string; cls: string }> = {
  critical: { label: "Critical", cls: "b-crit" },
  warning: { label: "Warning", cls: "b-warn" },
  info: { label: "Info", cls: "b-mut" },
};

const dot = (colour: string, round = true) => (
  <span aria-hidden="true" style={{
    flex: "none", width: 12, height: 12, borderRadius: round ? 6 : 3, background: colour,
  }} />
);

/** "In October 2026, 5 files (3,390 rows) came in from 2 brokers. …" */
function summaryLine(d: DataQualityReport, filtered: boolean): string {
  const v = d.volumes, q = d.quality, h = d.no_human_help;
  const month = fmtMonth(d.month);
  if (v.files === 0) {
    return `No files were checked in ${month}${filtered ? " for this selection" : ""}. `
      + "This report counts files checked since it was switched on — earlier months stay empty.";
  }
  const from = v.brokers > 0 ? ` from ${plural(v.brokers, "broker")}` : "";
  const clean = `${num(h.files)} of ${num(v.files)} ${v.files === 1 ? "was" : "were"} clean first time.`;
  const top = q.top_failing_checks[0]?.check;
  const errors = q.errors === 0 ? "No errors were found."
    : `${plural(q.errors, "error")} ${q.errors === 1 ? "was" : "were"} found`
      + (top ? ` — most of them “${top}”.` : ".");
  return `In ${month}, ${plural(v.files, "file")} (${plural(v.rows, "row")}) came in${from}. `
    + `${clean} ${errors}`;
}

/** "▲ 5 pts vs September (45%)" under a tile — nothing when last month is empty. */
function VsLastMonth({ now, then, shown, month, kind, better }: {
  now: number | null | undefined; then: number | null | undefined; shown: string; month: string;
  kind: "count" | "share" | "points"; better: "up" | "down" | "none";
}) {
  if (then == null) return null;
  return (
    <div className="foot" style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
      <Delta now={now} then={then} kind={kind} better={better} /> vs {fmtMonthName(month)} ({shown})
    </div>
  );
}

export default function DataQuality({ filters, onLists }: TabProps) {
  const { month, programId, brokerId } = filters;
  const before = shiftMonth(month, -1);
  const [data, setData] = useState<DataQualityReport | null>(null);
  // Last month, for the line under each tile.
  const [prev, setPrev] = useState<DataQualityReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true); setErr(null);
    const ask = (m: string) => getDataQuality({ month: m, program_id: programId, broker_party_id: brokerId });
    Promise.all([ask(month), ask(before).catch(() => null)])
      .then(([d, p]) => { if (live) { setData(d); setPrev(p); onLists(d); } })
      .catch(e => { if (live) setErr(e?.response?.data?.detail ?? "Could not load the report."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [month, before, programId, brokerId, onLists]);

  const v = data?.volumes;
  const q = data?.quality;
  const h = data?.no_human_help;
  // Last month counts only when it had files at all.
  const pv = prev && prev.volumes.files > 0 ? prev : null;
  const types = v?.transaction_types ?? [];
  // Shares of the transaction types: of the policies when files carry a
  // policy column, of the rows otherwise.
  const byPolicies = !!v?.policies_known;
  const typeTotal = types.reduce((n, g) => n + (byPolicies ? g.policies : g.rows), 0);
  const unclassified = types.find(g => g.key === "unclassified");
  const checksRun = q?.checks_run ?? 0;
  const filtered = programId != null || brokerId != null;
  const checks = [
    { label: "Passed", n: q?.checks_passed ?? 0, colour: "var(--p-ok)", ink: "var(--p-ok-ink)" },
    { label: "Failed", n: q?.checks_failed ?? 0, colour: "var(--p-crit)", ink: "var(--p-crit-ink)" },
    { label: "Could not run", n: q?.checks_not_run ?? 0, colour: "var(--p-faint)", ink: "var(--p-ink)",
      tip: "The file has no data for this rule, so it could not be checked." },
  ];

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
          <LineChart size={18} aria-hidden="true" />
        </span>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ fontSize: 12, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase",
                        color: "var(--p-info-ink)" }}>
            {fmtMonth(month)} at a glance
          </div>
          <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.55, color: "var(--p-ink)" }}>
            {loading || !data ? "Loading…" : summaryLine(data, filtered)}
          </p>
        </div>
      </div>

      {/* ---- 2. the four tiles (40.2, 40.3, 40.1) ----------------------- */}
      <div className="tiles" style={{ marginBottom: 18 }}>
        <div className="tile">
          <div className="k">
            Files handled
            <InfoTip text="Every file processed this month, counted once. A re-generate, self-check or preview is not a new file." />
          </div>
          <div className="v">{loading ? "…" : num(v?.files)}</div>
          <div className="foot">
            {v && v.files > 0
              ? `from ${plural(v.brokers, "broker")} · ${plural(v.programmes, "programme")}`
              : " "}
          </div>
          <VsLastMonth now={v?.files} then={pv?.volumes.files} shown={num(pv?.volumes.files)}
            month={before} kind="count" better="none" />
        </div>
        <div className="tile">
          <div className="k">
            Rows handled
            <InfoTip text="The rows the checks ran over — blank, header and total rows are left out." />
          </div>
          <div className="v">{loading ? "…" : num(v?.rows)}</div>
          <div className="foot">{v?.policies != null ? plural(v.policies, "policy", "policies") : " "}</div>
          <VsLastMonth now={v?.rows} then={pv?.volumes.rows} shown={num(pv?.volumes.rows)}
            month={before} kind="share" better="none" />
        </div>
        <div className="tile">
          <div className="k">
            Files clean first time
            <InfoTip text="Files that passed every check when they arrived, so nobody had to fix, re-send or release them." />
          </div>
          <div className="v" style={{ color: "var(--p-ok-ink)" }}>
            {loading ? "…" : `${num(h?.files)} of ${num(v?.files)}`}{" "}
            <small>files{h?.files_pct != null ? ` (${pct(h.files_pct)})` : ""}</small>
          </div>
          <div className="foot">
            {h && v ? <>Rows with no errors: <b style={{ color: "var(--p-ink)" }}>{num(h.rows)} of {num(v.rows)}</b> ({pct(h.rows_pct)})</> : " "}
          </div>
          <VsLastMonth now={h?.files_pct} then={pv?.no_human_help.files_pct}
            shown={pct(pv?.no_human_help.files_pct)} month={before} kind="points" better="up" />
        </div>
        <div className="tile" style={(q?.errors ?? 0) > 0 ? { borderColor: "#F3D2D7" } : undefined}>
          <div className="k">
            Errors found
            <InfoTip text="Every error the checks found, as each file first arrived." />
          </div>
          <div className="v" style={{ color: (q?.errors ?? 0) > 0 ? "var(--p-crit-ink)" : undefined }}>
            {loading ? "…" : num(q?.errors)}
          </div>
          <div className="foot" style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
            {q ? (
              <>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {dot("var(--p-crit)", false)}<b style={{ color: "var(--p-ink)" }}>{num(q.critical)}</b> critical
                </span>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {dot("var(--p-warn)", false)}<b style={{ color: "var(--p-ink)" }}>{num(q.warning)}</b> warning
                </span>
                {q.info > 0 && (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    {dot("var(--p-faint)", false)}<b style={{ color: "var(--p-ink)" }}>{num(q.info)}</b> info
                  </span>
                )}
              </>
            ) : " "}
          </div>
          <VsLastMonth now={q?.errors} then={pv?.quality.errors} shown={num(pv?.quality.errors)}
            month={before} kind="share" better="down" />
        </div>
      </div>

      <div className="grid g-2" style={{ marginBottom: 18 }}>
        {/* ---- 3. policies by transaction type (40.2) ------------------- */}
        <div className="card">
          <div className="card-h">
            <BarChart3 className="ci" />
            <h3>Policies by transaction type</h3>
            <InfoTip text="Read from each file's Transaction Type column. Not classified = blank, “UNK”, or a code the system does not know. A policy with more than one type is counted under each. Files with no policy number column are counted by row." />
            <div className="right">
              <span className="sub">
                {v ? (byPolicies ? plural(v.policies ?? 0, "policy", "policies") : plural(v.rows, "row")) : ""}
              </span>
            </div>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="l">Type</th>
                  <th className="r">{byPolicies ? "Policies" : "Rows"}</th>
                  <th className="r">Share</th>
                </tr>
              </thead>
              <tbody>
                {types.map(g => (
                  <tr key={g.key}>
                    <td className="l">
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 10,
                                     fontWeight: g.key === "unclassified" ? 400 : 600 }}>
                        {dot(TYPE_COLOUR[g.key], false)}{g.label}
                        {g.key === "other" && (
                          <InfoTip text="Endorsements, adjustments, instalments, reinstatements, audits" />
                        )}
                      </span>
                      {g.key === "unclassified" && (unclassified?.rows ?? 0) > 0 && (
                        <div style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: 22,
                                      marginTop: 4, fontSize: 12, color: "var(--p-warn-ink)" }}>
                          <AlertTriangle size={14} aria-hidden="true" />
                          Check the Transaction Type column in these files
                        </div>
                      )}
                    </td>
                    <td className="r" style={{ fontWeight: 600 }}>{num(byPolicies ? g.policies : g.rows)}</td>
                    <td className="r muted">{share(byPolicies ? g.policies : g.rows, typeTotal)}</td>
                  </tr>
                ))}
                {types.length === 0 && (
                  <tr><td colSpan={3} className="muted" style={{ textAlign: "center" }}>—</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* ---- 4. checks (40.1) ----------------------------------------- */}
        <div className="card">
          <div className="card-h">
            <ShieldCheck className="ci" />
            <h3>Checks</h3>
            <InfoTip text="Every check run on the files handled — each contract rule and each date/number check." />
          </div>
          <div style={{ padding: "18px 20px", display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12 }}>
              <span style={{ fontSize: 14, fontWeight: 600 }}>Total checks</span>
              <span style={{ fontFamily: "var(--p-disp)", fontSize: 26, fontWeight: 600, letterSpacing: "-.5px" }}>
                {loading ? "…" : num(q?.checks_run)}
              </span>
            </div>
            {checksRun > 0 && (
              <div aria-hidden="true" style={{ display: "flex", height: 8, borderRadius: 4, overflow: "hidden",
                                               background: "var(--p-surface-3)" }}>
                {checks.map(r => (
                  <span key={r.label} style={{ width: `${(100 * r.n) / checksRun}%`, background: r.colour }} />
                ))}
              </div>
            )}
            <div style={{ display: "flex", flexDirection: "column" }}>
              {checks.map((r, i) => (
                <div key={r.label} style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 0",
                                            borderTop: i ? "1px solid var(--p-border)" : undefined }}>
                  {dot(r.colour)}
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 14 }}>
                    {r.label}{r.tip && <InfoTip text={r.tip} />}
                  </span>
                  <span style={{ marginLeft: "auto", fontSize: 14, fontWeight: 600, color: r.ink }}>{num(r.n)}</span>
                  <span className="muted" style={{ width: 48, textAlign: "right", fontSize: 13 }}>
                    {share(r.n, checksRun)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ---- 5. checks that failed most (40.1) -------------------------- */}
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h">
          <ListChecks className="ci" />
          <h3>Checks that failed most</h3>
          <InfoTip text="This shows which data problems are most common, so the carrier knows what to raise with the broker." />
          <div className="right"><span className="sub">Top 10, most errors first</span></div>
        </div>
        <div className="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th className="r" style={{ width: 40 }}>#</th>
                <th className="l">Check</th><th className="r">Severity</th>
                <th className="r">Files</th><th className="r">Errors</th>
                <th className="r">Share of all errors</th>
              </tr>
            </thead>
            <tbody>
              {(q?.top_failing_checks ?? []).length === 0 && (
                <tr><td colSpan={6} className="muted" style={{ textAlign: "center" }}>No check failed this month.</td></tr>
              )}
              {(q?.top_failing_checks ?? []).map((c, i) => {
                const sev = c.severity ? SEVERITY[c.severity] : null;
                return (
                  <tr key={`${c.kind}:${c.check}`}>
                    <td className="r muted" style={{ fontWeight: 600 }}>{i + 1}</td>
                    <td className="l" style={{ fontWeight: 600 }}>{c.check}</td>
                    <td className="r">
                      {sev ? <span className={`badge ${sev.cls}`}><span className="d" />{sev.label}</span>
                           : <span className="muted">—</span>}
                    </td>
                    <td className="r">{num(c.files)}</td>
                    <td className="r" style={{ fontWeight: 600 }}>{num(c.errors)}</td>
                    <td className="r muted">{share(c.errors, q?.errors ?? 0)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
