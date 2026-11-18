// Task 36 — Partner Performance (backend partner_performance.py): how often each
// broker sends a clean file on time, one row per broker + programme + contract
// (grouped under the broker), with the best and worst brokers — or, with one
// broker picked, where it stands among all of them. Carrier only.
//
// On time and clean first time are the Executive Report scorecard's own
// numbers; "clean and on time" is the one new share: a file due in the period
// that came by its due date AND passed its first check. A file that came on
// time with no check recorded is left out of that share, never counted as 0.
import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Award, Clock, Download, Eye, ShieldCheck } from "lucide-react";
import {
  downloadPartnerPerformance, getPartnerPerformance,
  type PartnerPerformance as Data, type PerfBroker, type PerfCadence, type PerfRow,
} from "../api/partnerPerformance";
import { InfoTip } from "../components/InfoTip";
import { StatCard } from "../components/StatCard";
import { fmtDay, fmtMonth, lastMonths } from "../utils/reportFormat";

const CADENCES: { key: PerfCadence; label: string }[] = [
  { key: "month", label: "Monthly" }, { key: "quarter", label: "Quarterly" }, { key: "year", label: "Yearly" }];
// The single hue for nominal bars (chart conventions): the name is on the row,
// so colour carries nothing else.
const BAR = "#0B8FA0";
const TRACK = "#F0F2F6";
const RED = "var(--p-crit, #e11d48)";

/** This quarter and the seven before it, newest first: "2026-Q4". */
function lastQuarters(n = 8): string[] {
  const now = new Date();
  let y = now.getFullYear(), q = Math.floor(now.getMonth() / 3) + 1;
  const out: string[] = [];
  for (let i = 0; i < n; i++) {
    out.push(`${y}-Q${q}`);
    q -= 1;
    if (q === 0) { q = 4; y -= 1; }
  }
  return out;
}

function lastYears(n = 4): string[] {
  const y = new Date().getFullYear();
  return Array.from({ length: n }, (_, i) => String(y - i));
}

/** "2026-Q3" → "Jul – Sep 2026". */
function quarterLabel(key: string): string {
  const [y, q] = key.split("-Q").map(Number);
  const m = (q - 1) * 3;
  const name = (i: number) => new Date(y, i, 1).toLocaleDateString("en-GB", { month: "short" });
  return `${name(m)} – ${name(m + 2)} ${y}`;
}

const periodLabel = (c: PerfCadence, p: string) => c === "month" ? fmtMonth(p) : c === "quarter" ? quarterLabel(p) : p;

function span(from: string, to: string): string {
  return from === to ? fmtDay(from) : `${fmtDay(from)} – ${fmtDay(to)}`;
}

const fmtPct = (v: number | null | undefined) => (v == null ? "—" : `${v}%`);
const round1 = (v: number) => Math.round(v * 10) / 10;

/** "+33.3 since last month" / "-4.5 since last month" (StatCard reds a
 *  leading "-"), or undefined when there is nothing to compare with. */
function trend(pts: number | null | undefined, word: string): string | undefined {
  if (pts == null) return undefined;
  const r = round1(pts);
  return r === 0 ? `No change since last ${word}` : `${r > 0 ? "+" : ""}${r} since last ${word}`;
}

/** The broker's name as plain text: this page does not open the broker screen. */
function BrokerName({ r }: { r: Pick<PerfBroker, "name"> }) {
  return <span style={{ fontWeight: 600 }}>{r.name}</span>;
}

/** A share with the count behind it underneath. */
function Pct({ v, of }: { v: number | null; of?: string }) {
  if (v == null) return <span className="sub">—</span>;
  return <>{v}%{of && <div className="sub">{of}</div>}</>;
}

export default function PartnerPerformance() {
  const months = useMemo(() => lastMonths(), []);
  const quarters = useMemo(() => lastQuarters(), []);
  const years = useMemo(() => lastYears(), []);
  // ?period=YYYY-MM — the month the Executive Report was showing when its
  // scorecard linked here.
  const [params] = useSearchParams();
  const asked = params.get("period");
  const [cadence, setCadence] = useState<PerfCadence>("month");
  const [period, setPeriod] = useState<string>(asked && /^\d{4}-\d{2}$/.test(asked) ? asked : months[1]);
  const [programId, setProgramId] = useState<number | null>(null);
  const [fileType, setFileType] = useState<string>("");
  const [brokerId, setBrokerId] = useState<number | null>(null);
  const [data, setData] = useState<Data | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setFailed(null);
    getPartnerPerformance({ cadence, period, programId, fileType, brokerId })
      .then(r => { if (live) setData(r); })
      .catch(e => { if (live) setFailed(e?.response?.data?.detail ?? "Could not load partner performance."); });
    return () => { live = false; };
  }, [cadence, period, programId, fileType, brokerId]);

  const pick = (c: PerfCadence) => {
    setCadence(c);
    setPeriod(c === "month" ? months[1] : c === "quarter" ? quarters[0] : years[0]);
  };
  // Show the answer to the question asked, never the one before it.
  const r = data && data.cadence === cadence && data.period === period
    && (data.filters.programme ?? null) === programId
    && (data.filters.file_type ?? "") === fileType
    && (data.filters.broker ?? null) === brokerId ? data : null;
  const opts = (r ?? data)?.options;
  const filtered = programId != null || !!fileType || brokerId != null;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Partner Performance
              <InfoTip text={"How often each broker sends a clean file on time, with the best and worst brokers across your book."} />
            </h2>
          </div>
        </div>

        <div className="card" style={{ overflow: "visible", marginBottom: 14 }}>
          <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none", alignItems: "flex-end" }}>
            <label className="fbar-field">
              <span className="sub">Period</span>
              <span className="seg">
                {CADENCES.map(c => (
                  <button key={c.key} type="button" className={cadence === c.key ? "on" : ""}
                    onClick={() => pick(c.key)}>{c.label}</button>))}
              </span>
            </label>
            <label className="fbar-field">
              <span className="sub">{cadence === "month" ? "Month" : cadence === "quarter" ? "Quarter" : "Year"}</span>
              <select className="fbar-select" value={period} onChange={e => setPeriod(e.target.value)}>
                {(cadence === "month" ? (months.includes(period) ? months : [period, ...months])
                  : cadence === "quarter" ? quarters : years).map(p => (
                  <option key={p} value={p}>{periodLabel(cadence, p)}</option>))}
              </select>
            </label>
            <label className="fbar-field">
              <span className="sub">Broker <InfoTip text="Show one broker. It keeps its rank among all your brokers." /></span>
              <select className="fbar-select" value={brokerId ?? ""}
                onChange={e => setBrokerId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All brokers</option>
                {opts?.brokers.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            <label className="fbar-field">
              <span className="sub">Programme</span>
              <select className="fbar-select" value={programId ?? ""}
                onChange={e => setProgramId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All programmes</option>
                {opts?.programmes.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            <label className="fbar-field">
              <span className="sub">File type <InfoTip text={"How the file came in.\nFiles that never came have no file type, so they drop out when one is picked."} /></span>
              <select className="fbar-select" value={fileType} onChange={e => setFileType(e.target.value)}>
                <option value="">All file types</option>
                {opts?.file_types.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            {filtered && (
              <button type="button" className="btn" style={{ marginBottom: 1 }}
                onClick={() => { setBrokerId(null); setProgramId(null); setFileType(""); }}>Clear filters</button>)}
            {r && <span className="sub" style={{ paddingBottom: 8 }}>
              Covers {span(r.from, r.to)}, compared with {span(r.previous.from, r.previous.to)}.
              {!r.complete && " Still running, so the numbers count to today."}
            </span>}
          </div>
        </div>

        {failed ? <div className="card"><div className="empty">{failed}</div></div>
          : !r ? <div className="card"><div className="empty">Loading…</div></div>
          : <Body r={r} programId={programId} fileType={fileType} brokerId={brokerId} />}
      </div>
    </div>
  );
}

function Body({ r, programId, fileType, brokerId }: {
  r: Data; programId: number | null; fileType: string; brokerId: number | null;
}) {
  const s = r.summary;
  const word = r.cadence;
  const [saving, setSaving] = useState(false);
  const [saveFailed, setSaveFailed] = useState<string | null>(null);
  const download = () => {
    setSaving(true);
    setSaveFailed(null);
    downloadPartnerPerformance({ cadence: r.cadence, period: r.period, programId, fileType, brokerId })
      .catch(() => setSaveFailed("Could not download the file. Please try again."))
      .finally(() => setSaving(false));
  };
  const unranked = new Set(r.rows.filter(x => x.rank == null).map(x => x.broker_id)).size;
  return (<>
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 16, marginBottom: 18 }}>
      <StatCard title="On time" icon={Clock} value={fmtPct(s.on_time_pct)} trend={trend(s.change_pts.on_time_pct, word)}
        subtitle={s.due ? `${s.on_time} of ${s.due} files due came on time` : "No files were due"}
        info="Files due in the period that came by their due date. The same count as the Executive Report." />
      <StatCard title="Passed checks first time" icon={ShieldCheck} value={fmtPct(s.clean_first_time_pct)}
        trend={trend(s.change_pts.clean_first_time_pct, word)}
        subtitle={s.files ? `Of ${s.files} file${s.files === 1 ? "" : "s"} checked` : "No check results recorded"}
        info="Files checked in the period that passed every check the first time, with nobody stepping in." />
      <StatCard title="On time and passed checks" icon={Award} value={fmtPct(s.clean_on_time_pct)}
        trend={trend(s.change_pts.clean_on_time_pct, word)}
        subtitle={s.scored
          ? `${s.clean_on_time} of ${s.scored} files${s.not_checked ? ` · ${s.not_checked} not checked` : ""}`
          : s.not_checked ? `${s.not_checked} not checked` : "No files were due"}
        info={"Files that came on time AND passed every check the first time. Late or missing files count against it; files with no check recorded are left out."} />
    </div>

    {r.picked ? <Standing r={r} /> : (
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 16, marginBottom: 18 }}>
        <Ranked title="Best performers" rows={r.best}
          empty={r.ranked_brokers ? "No broker sent a file on time that passed every check." : undefined} />
        <Ranked title="Worst performers" rows={r.worst}
          empty={r.ranked_brokers ? "Every ranked broker is already in the best list or at 100%." : undefined} />
      </div>)}

    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h" style={{ flexWrap: "wrap", gap: 10 }}>
        <h3>Brokers ranked, by programme and contract</h3>
        <InfoTip text="A broker with several contracts has one row per contract. All its rows share the broker's rank." />
        <span style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          {saveFailed && <span className="sub" style={{ color: RED }}>{saveFailed}</span>}
          <button type="button" className="btn" onClick={download} disabled={saving || r.rows.length === 0}>
            <Download size={14} style={{ marginRight: 6, verticalAlign: -2 }} />
            {saving ? "Preparing…" : "Download Excel"}
          </button>
        </span>
      </div>
      {r.rows.length === 0 ? <div className="empty">No files were due or checked in this period.</div> : (
        <div className="tbl-wrap"><table style={{ minWidth: 1460 }}>
          <thead><tr>
            <th>Rank</th>
            <th className="l">Broker</th>
            <th className="l">Programme</th>
            <th className="l">Contract</th>
            <th>Files due</th>
            <th>Files received</th>
            <th>On time</th>
            <th>Late <InfoTip text="Came in, but after the due date." /></th>
            <th>Missing <InfoTip text="Due, but not come in yet." /></th>
            <th>Passed checks first time</th>
            <th>On time and passed checks <InfoTip text="Files that came on time AND passed every check the first time. This is what brokers are ranked on." /></th>
            <th>Still has problems <InfoTip text="Files that came in whose latest version still has problems not put right. Underneath: how many problems are still open." /></th>
            <th>Problems fixed while cleaning <InfoTip text="Of the problems found when the file first came, how many have been put right since (fixed, accepted or resent)." /></th>
            <th className="l">How it came in</th>
          </tr></thead>
          <tbody>{r.rows.map((x, i) => {
            const first = startsBroker(r.rows, i);
            // Rank and broker are one merged cell over all the broker's rows.
            const span = first ? brokerSpan(r.rows, i) : 0;
            const merged = { verticalAlign: "middle" as const, background: "var(--p-surface-2, #F7F8FB)" };
            return (
            <tr key={x.key} style={first && i > 0 ? { borderTop: "2px solid var(--p-border, #E3E7EE)" } : undefined}>
              {first && <>
                <td rowSpan={span} style={merged}>{x.rank ?? <span className="sub" title="No file with a known result yet">—</span>}</td>
                <td rowSpan={span} className="l" style={{ ...merged, overflowWrap: "anywhere", fontWeight: 600,
                  borderRight: "1px solid var(--p-border, #E3E7EE)" }}>
                  {x.broker}
                </td>
              </>}
              <td className="l" style={{ overflowWrap: "anywhere" }}>{x.programme}</td>
              <td className="l" style={{ overflowWrap: "anywhere" }}>{x.contract}</td>
              <td>{x.due || <span className="sub">0</span>}</td>
              <td>{x.received || <span className="sub">0</span>}</td>
              <td>{x.due ? <Pct v={x.on_time_pct} of={`${x.on_time} of ${x.due}`} /> : <span className="sub">None due</span>}</td>
              <td>{x.late
                ? <span style={{ fontWeight: 700, color: RED }}>{x.late}</span>
                : <span className="sub">0</span>}</td>
              <td>{x.missing
                ? <span style={{ fontWeight: 700, color: RED }}>{x.missing}</span>
                : <span className="sub">0</span>}</td>
              <td>{x.files ? <Pct v={x.clean_first_time_pct} of={`${x.clean_first_time} of ${x.files}`} />
                : <span className="sub">{x.received ? "No check recorded" : "—"}</span>}</td>
              <td>{x.scored ? <Pct v={x.clean_on_time_pct} of={`${x.clean_on_time} of ${x.scored}`} /> : <span className="sub">—</span>}
                {x.not_checked > 0 && <div className="sub">{x.not_checked} not checked</div>}</td>
              <td>{!x.received ? <span className="sub">—</span>
                : x.files_with_problems
                  ? <><span style={{ fontWeight: 700, color: RED }}>{x.files_with_problems} file{x.files_with_problems === 1 ? "" : "s"}</span>
                      <div className="sub" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                        {x.problems_open} open
                        {x.problems_link && <Link to={x.problems_link} className="linkish" style={{ display: "inline-flex" }}
                          title={x.files_with_problems > 1 ? "See the exceptions on the file with the most still open"
                            : "See the exceptions on this file"}
                          aria-label="See the exceptions"><Eye size={15} /></Link>}
                      </div></>
                  : <span className="sub">None</span>}</td>
              <td>{!x.received ? <span className="sub">—</span>
                : x.problems_found
                  ? <>{x.problems_fixed} of {x.problems_found}<div className="sub">{x.fixed_pct}%</div></>
                  : <span className="sub">No problems found</span>}</td>
              <td className="l">{x.file_types.length ? x.file_types.join(", ") : <span className="sub">Not received</span>}</td>
            </tr>);
          })}</tbody>
        </table></div>
      )}
      {unranked > 0 && <div className="sub" style={{ padding: "8px 16px 12px" }}>
        {unranked} {unranked === 1 ? "broker has" : "brokers have"} no file with a known result yet, so {unranked === 1 ? "it is" : "they are"} not ranked.
      </div>}
    </div>
  </>);
}

/** The first of a broker's rows: the one that names it and shows its rank. */
function startsBroker(rows: PerfRow[], i: number): boolean {
  return i === 0 || rows[i - 1].broker_id !== rows[i].broker_id;
}

/** How many rows in a row, from i, belong to the same broker. */
function brokerSpan(rows: PerfRow[], i: number): number {
  let n = 1;
  while (i + n < rows.length && rows[i + n].broker_id === rows[i].broker_id) n++;
  return n;
}

/** One broker picked: where it stands among all the brokers, in place of the
 *  best and worst lists (which would only name it). */
function Standing({ r }: { r: Data }) {
  const p = r.picked!;
  const when = periodLabel(r.cadence, r.period);
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div style={{ padding: "16px 18px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <Award size={20} style={{ color: BAR }} />
        <span style={{ fontSize: 15 }}>
          {p.rank != null
            ? <><BrokerName r={{ broker_id: p.broker_id, name: p.name } as PerfBroker} /> is ranked <b>{p.rank} of {p.of}</b> broker{p.of === 1 ? "" : "s"} in {when}
                {p.clean_on_time_pct != null && <span className="sub"> · {p.clean_on_time_pct}% on time and passed checks</span>}</>
            : <><BrokerName r={{ broker_id: p.broker_id, name: p.name } as PerfBroker} /> has no file with a known result in {when}, so it is not ranked.</>}
        </span>
      </div>
    </div>
  );
}

/** Best or worst brokers as ranked horizontal bars: on time and passed checks, out of 100%. */
function Ranked({ title, rows, empty }: { title: string; rows: PerfBroker[]; empty?: string }) {
  return (
    <div className="card">
      <div className="card-h">
        <h3>{title}</h3>
        <InfoTip text="Brokers by the share of files that came on time and passed every check first time. One file is enough." />
      </div>
      {rows.length === 0 ? (
        <div className="empty">{empty ?? "No broker has a file with a known result yet."}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "14px 16px 16px" }}>
          {rows.map(x => (
            <div key={String(x.key)}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, fontSize: 13, marginBottom: 5 }}>
                <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                  <span className="sub" style={{ marginRight: 6 }}>#{x.rank}</span><BrokerName r={x} />
                </span>
                <span style={{ whiteSpace: "nowrap", fontWeight: 600 }}>
                  {x.clean_on_time_pct}% <span className="sub" style={{ fontWeight: 400 }}>· {x.clean_on_time} of {x.scored}</span>
                </span>
              </div>
              <div style={{ height: 8, borderRadius: 4, background: TRACK, overflow: "hidden" }}
                role="img" aria-label={`${x.name}: ${x.clean_on_time_pct}% on time and passed checks`}>
                <div style={{ width: `${Math.max(0, Math.min(100, x.clean_on_time_pct ?? 0))}%`, height: "100%", background: BAR }} />
              </div>
            </div>))}
        </div>
      )}
    </div>
  );
}
