// MOPR 40 — Data Quality & Volumes.
//
// One month, read from the shared check record (backend check_record.py) that
// every check writes to — the file-arrival checks, the quick lane and the full
// data-model lane alike:
//   40.2  files and rows handled, policies split new / renewed / cancelled
//   40.3  how many files were clean first time
//   40.1  what the checks found: passed, failed, could not run, and the errors
//
// Layout as agreed in the UI proposal: a one-line summary of the month, four
// tiles, transaction types beside the checks, the checks that failed most, and
// one table by broker and programme. Numbers and words only — no charts.
import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, BarChart3, LineChart, ListChecks, RotateCw, ShieldCheck, Users2,
} from "lucide-react";
import {
  getDataQuality, type BrokerProgrammeRow, type DataQualityReport, type TxnGroup,
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
/** Whole-number share, "—" when there is nothing to share out. */
const share = (n: number, of: number) => (of > 0 ? `${Math.round((100 * n) / of)}%` : "—");

// Same page size as the calendars and the other lists.
const PAGE_SIZE = 10;

const CHECK_KIND: Record<string, string> = {
  contract_rule: "Contract rule", type_check: "Value type", intake: "File arrival",
};

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

/** One row per broker on each programme, most errors first. */
function ByBrokerProgramme({ rows }: { rows: BrokerProgrammeRow[] }) {
  const [page, setPage] = useState(1);
  // A new month or filter is a new list: start it from its first page.
  useEffect(() => { setPage(1); }, [rows]);
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const pageNow = Math.min(page, pageCount);
  const shown = rows.slice((pageNow - 1) * PAGE_SIZE, pageNow * PAGE_SIZE);
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        <Users2 className="ci" />
        <h3>By broker and programme</h3>
        <div className="right"><span className="sub">Most errors first</span></div>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th className="l">Broker</th>
              <th className="l">Programme</th>
              <th className="r">Files</th><th className="r">Rows</th>
              <th className="r">Errors</th>
              <th className="r">Rows with errors</th>
              <th className="r">Clean first time</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={7} className="muted" style={{ textAlign: "center" }}>No files this month.</td></tr>
            )}
            {shown.map(r => (
              <tr key={`${r.broker_party_id}:${r.program_id}`}>
                <td className="l" style={{ fontWeight: 600 }}>{r.broker_name}</td>
                <td className="l">{r.programme_name}</td>
                <td className="r">{num(r.files)}</td>
                <td className="r">{num(r.rows)}</td>
                <td className="r" style={{ fontWeight: 600, color: r.errors > 0 ? "var(--p-crit-ink)" : undefined }}>
                  {num(r.errors)}
                </td>
                <td className="r muted">{pct(r.rows_with_errors_pct)}</td>
                <td className="r" style={{ whiteSpace: "nowrap" }}>
                  <b>{num(r.straight_through)} of {num(r.files)}</b>{" "}
                  <span className="muted">({pct(r.straight_through_pct)})</span>
                </td>
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

export default function DataQuality() {
  const months = useMemo(() => lastMonths(), []);
  const [month, setMonth] = useState(months[0]);
  const [programId, setProgramId] = useState<number | null>(null);
  const [brokerId, setBrokerId] = useState<number | null>(null);
  const [data, setData] = useState<DataQualityReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true); setErr(null);
    getDataQuality({ month, program_id: programId, broker_party_id: brokerId })
      .then(d => { if (live) setData(d); })
      .catch(e => { if (live) setErr(e?.response?.data?.detail ?? "Could not load the report."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [month, programId, brokerId]);

  const v = data?.volumes;
  const q = data?.quality;
  const h = data?.no_human_help;
  const types = v?.transaction_types ?? [];
  // Shares of the transaction types: of the policies when files carry a
  // policy column, of the rows otherwise.
  const byPolicies = !!v?.policies_known;
  const typeTotal = types.reduce((n, g) => n + (byPolicies ? g.policies : g.rows), 0);
  const unclassified = types.find(g => g.key === "unclassified");
  const checksRun = q?.checks_run ?? 0;
  const filtered = programId != null || brokerId != null;

  return (
    <div className="proto">
      <div className="view full">
        {/* ---- 1. title, filters ------------------------------------------ */}
        <div className="page-head" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="t">
            <h2>Data Quality &amp; Volumes</h2>
            <p>What came in this month, and how clean it was when it arrived.</p>
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
                : " "}
            </div>
          </div>
          <div className="tile">
            <div className="k">
              Rows handled
              <InfoTip text="The rows the checks ran over — blank, header and total rows are left out." />
            </div>
            <div className="v">{loading ? "…" : num(v?.rows)}</div>
            <div className="foot">{v?.policies != null ? plural(v.policies, "policy", "policies") : " "}</div>
          </div>
          <div className="tile">
            <div className="k">
              Files clean first time
              <InfoTip text="Files that passed every check when they arrived, so nobody had to fix, re-send or release them." />
            </div>
            <div className="v" style={{ color: "var(--p-ok-ink)" }}>
              {loading ? "…" : `${num(h?.files)} of ${num(v?.files)}`}{" "}
              <small>files{h?.files_pct != null ? ` (${h.files_pct}%)` : ""}</small>
            </div>
            <div className="foot">
              {h && v ? <>Rows with no errors: <b style={{ color: "var(--p-ink)" }}>{num(h.rows)} of {num(v.rows)}</b> ({pct(h.rows_pct)})</> : " "}
            </div>
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
              ) : " "}
            </div>
          </div>
        </div>

        <div className="grid g-2" style={{ marginBottom: 18 }}>
          {/* ---- 3. policies by transaction type (40.2) ------------------- */}
          <div className="card">
            <div className="card-h">
              <BarChart3 className="ci" />
              <h3>Policies by transaction type</h3>
              <InfoTip text="Read from each file's Transaction Type column. Not classified = blank, “UNK”, or a code the system does not know. A policy with more than one type is counted under each." />
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
                    <th className="l">Type</th><th className="r">Policies</th>
                    <th className="r">Share</th><th className="r">Rows</th>
                  </tr>
                </thead>
                <tbody>
                  {types.map(g => (
                    <tr key={g.key}>
                      <td className="l">
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 10,
                                       fontWeight: g.key === "unclassified" ? 400 : 600 }}>
                          {dot(TYPE_COLOUR[g.key], false)}{g.label}
                        </span>
                        {g.key === "other" && (
                          <div className="sub" style={{ marginLeft: 22 }}>
                            Endorsements, adjustments, instalments, reinstatements, audits
                          </div>
                        )}
                        {g.key === "unclassified" && (unclassified?.rows ?? 0) > 0 && (
                          <div style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: 22,
                                        marginTop: 4, fontSize: 12, color: "var(--p-warn-ink)" }}>
                            <AlertTriangle size={14} aria-hidden="true" />
                            Check the Transaction Type column in these files
                          </div>
                        )}
                      </td>
                      <td className="r" style={{ fontWeight: 600 }}>{byPolicies ? num(g.policies) : "—"}</td>
                      <td className="r muted">{share(byPolicies ? g.policies : g.rows, typeTotal)}</td>
                      <td className="r muted">{num(g.rows)}</td>
                    </tr>
                  ))}
                  {types.length === 0 && (
                    <tr><td colSpan={4} className="muted" style={{ textAlign: "center" }}>—</td></tr>
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
            <div style={{ padding: "18px 20px", display: "flex", flexDirection: "column", gap: 16 }}>
              <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12 }}>
                <span style={{ fontSize: 14, fontWeight: 600 }}>Total checks</span>
                <span style={{ fontFamily: "var(--p-disp)", fontSize: 26, fontWeight: 600, letterSpacing: "-.5px" }}>
                  {loading ? "…" : num(q?.checks_run)}
                </span>
              </div>
              <div style={{ display: "flex", flexDirection: "column" }}>
                {[
                  { label: "Passed", n: q?.checks_passed ?? 0, colour: "var(--p-ok)", ink: "var(--p-ok-ink)" },
                  { label: "Failed", n: q?.checks_failed ?? 0, colour: "var(--p-crit)", ink: "var(--p-crit-ink)" },
                  { label: "Could not run", n: q?.checks_not_run ?? 0, colour: "var(--p-faint)", ink: "var(--p-ink)",
                    sub: "A rule that reads a column the file does not fill, or whose query failed" },
                ].map((r, i) => (
                  <div key={r.label} style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "10px 0",
                                              borderTop: i ? "1px solid var(--p-border)" : undefined }}>
                    <span style={{ marginTop: 4 }}>{dot(r.colour)}</span>
                    <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                      <span style={{ fontSize: 14 }}>{r.label}</span>
                      {r.sub && <span className="sub">{r.sub}</span>}
                    </span>
                    <span style={{ marginLeft: "auto", fontSize: 14, fontWeight: 600, color: r.ink }}>{num(r.n)}</span>
                    <span className="muted" style={{ width: 48, textAlign: "right", fontSize: 13 }}>
                      {share(r.n, checksRun)}
                    </span>
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: "var(--p-muted)" }}>
                  Files: clean / with errors / not checked
                </span>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10 }}>
                  {[
                    { label: "Clean", n: q?.files_clean, bg: "var(--p-ok-soft)", line: "#C5E7D3", ink: "var(--p-ok-ink)" },
                    { label: "With errors", n: q?.files_with_errors, bg: "var(--p-crit-soft)", line: "#F2C4CB", ink: "var(--p-crit-ink)" },
                    { label: "Not checked", n: q?.files_not_validated, bg: "var(--p-surface-2)", line: "var(--p-border)", ink: "var(--p-muted)" },
                  ].map(b => (
                    <div key={b.label} style={{ border: `1px solid ${b.line}`, background: b.bg, borderRadius: 10,
                                                padding: "10px 12px", display: "flex", flexDirection: "column", gap: 2 }}>
                      <span style={{ fontSize: 22, fontWeight: 700, color: b.ink }}>{num(b.n)}</span>
                      <span style={{ fontSize: 13, color: "var(--p-ink)" }}>{b.label}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, paddingTop: 12,
                            borderTop: "1px solid var(--p-border)", fontSize: 13, color: "var(--p-muted)" }}>
                <RotateCw size={15} aria-hidden="true" />
                <span>Checking rounds: <b style={{ color: "var(--p-ink)" }}>{num(q?.check_runs)}</b> — including re-generates, self-checks and previews</span>
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
                  <th className="l">Check</th><th className="r">Severity</th><th className="l">Kind</th>
                  <th className="r">Files</th><th className="r">Errors</th>
                  <th className="r">Share of all errors</th>
                </tr>
              </thead>
              <tbody>
                {(q?.top_failing_checks ?? []).length === 0 && (
                  <tr><td colSpan={7} className="muted" style={{ textAlign: "center" }}>No check failed this month.</td></tr>
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
                      <td className="l muted">{CHECK_KIND[c.kind] ?? c.kind}</td>
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

        {/* ---- 6. by broker and programme --------------------------------- */}
        <ByBrokerProgramme rows={data?.by_broker_programme ?? []} />
      </div>
    </div>
  );
}
