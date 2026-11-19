// The Programme Management Report — a sub-screen of the Executive Report
// (backend programme_report.py). The Executive Report shows the programmes in
// charts; this lists every programme and broker for the same day, week, month
// or dates, and opens one pair's detail under its row. Every number is the
// Executive Report's own, narrowed to the programme and broker, and the money
// and volume numbers open the policies behind them (38.1).
import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, CircleDot, Download, FileText, Layers, Search, Wallet, XCircle,
} from "lucide-react";
import {
  downloadProgrammeManagement, getProgrammeBroker, getProgrammeManagement,
  type ContractDetail, type Fig, type ManagementRow, type ProgrammeBroker, type ProgrammeManagement as Report,
} from "../api/executive";
import { drillLink } from "../api/policies";
import { InfoTip } from "../components/InfoTip";
import { showError } from "../components/ResultPopup";
import { fmtMonth, plural } from "../utils/reportFormat";
import {
  C, FIXED, PeriodBar, WRAP, checksLink, fmtPeriod, money, periodQuery, shortDay, usePeriod, type Period,
} from "./ExecutiveReport";

const VS = { day: "the day before", week: "last month", month: "last month", custom: "last month" } as const;

/** The period (and the programme picked) stay in the address, so a link, Back or a reload shows the same report. */
function usePeriodInUrl(): Period {
  const [params, setParams] = useSearchParams();
  const p = usePeriod({ cadence: params.get("cadence"), period: params.get("period") });
  const ok = p.cadence !== "custom" || p.customOk;
  useEffect(() => {
    if (ok && (params.get("cadence") !== p.cadence || params.get("period") !== p.period)) {
      const next = new URLSearchParams(params);
      next.set("cadence", p.cadence);
      next.set("period", p.period);
      setParams(next, { replace: true });
    }
  }, [p.cadence, p.period, ok, params, setParams]);
  return p;
}

export default function ProgrammeManagement() {
  const p = usePeriodInUrl();
  const [data, setData] = useState<Report | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [busy, setBusy] = useState<"pdf" | "excel" | null>(null);
  const [menu, setMenu] = useState(false);
  useEffect(() => {
    if (p.cadence === "custom" && !p.customOk) return;
    let live = true;
    setFailed(null);
    getProgrammeManagement(p)
      .then(r => { if (live) setData(r); })
      .catch(e => { if (live) setFailed(e?.response?.data?.detail ?? "Could not load the report."); });
    return () => { live = false; };
  }, [p.cadence, p.period, p.customOk]);
  const r = data && data.cadence === p.cadence && data.period === p.period ? data : null;
  const wait = p.cadence === "custom" && !p.customOk ? "Pick a from date on or before the to date."
    : failed ?? (!r ? "Loading…" : null);

  async function download(format: "pdf" | "excel") {
    setMenu(false);
    setBusy(format);
    try { await downloadProgrammeManagement({ cadence: p.cadence, period: p.period, format }); }
    catch (e) { showError(e, "Could not make the file"); }
    finally { setBusy(null); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="sub" style={{ marginBottom: 4 }}>
          <Link className="linkish" to={`/executive-report?${periodQuery(p)}`}>Executive Report</Link>
          <span style={{ margin: "0 6px" }}>/</span>Programme Management Report
        </div>
        <div className="page-head">
          <div className="t">
            <h2>Programme Management Report
              <InfoTip text={"Every programme and broker for the Executive Report's period.\nClick View details to see a broker's files, errors, premium and contract."} />
            </h2>
          </div>
          <div className="actions" style={{ position: "relative" }}>
            <button className="btn pri" onClick={() => setMenu(m => !m)} disabled={busy != null || !r}>
              <Download size={15} /> {busy ? "Making…" : "Export"} <ChevronDown size={14} />
            </button>
            {menu && (
              <div className="card" style={{ position: "absolute", right: 0, top: "calc(100% + 6px)", zIndex: 20,
                                             minWidth: 160, padding: 6, display: "grid", gap: 2 }}>
                <button className="btn sm ghost" style={{ justifyContent: "flex-start" }} onClick={() => download("excel")}>Excel</button>
                <button className="btn sm ghost" style={{ justifyContent: "flex-start" }} onClick={() => download("pdf")}>PDF</button>
              </div>
            )}
          </div>
        </div>
        <PeriodBar p={p} r={r} />
        {wait ? <div className="card"><div className="empty">{wait}</div></div> : r && <Body r={r} />}
      </div>
    </div>
  );
}

const STATUS_TIP = "Attention: a file is missing or late, or there are critical errors. OK: none of those.";

function Body({ r }: { r: Report }) {
  const [params, setParams] = useSearchParams();
  const t = r.totals;
  const cur = r.currency;
  const programme = params.get("programme") ?? "";
  const setProgramme = (v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set("programme", v); else next.delete("programme");
    setParams(next, { replace: true });
  };
  const [broker, setBroker] = useState("");
  const [status, setStatus] = useState("");
  const [find, setFind] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const programmes = useMemo(() => [...new Map(r.rows.map(x => [x.program_id, x.programme])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1])), [r.rows]);
  const brokers = useMemo(() => [...new Map(r.rows.filter(x => x.broker_party_id != null)
    .map(x => [x.broker_party_id as number, x.broker])).entries()].sort((a, b) => a[1].localeCompare(b[1])), [r.rows]);
  const rows = r.rows.filter(x => (!programme || String(x.program_id) === programme)
    && (!broker || String(x.broker_party_id) === broker) && (!status || x.status === status)
    && (!find.trim() || `${x.programme} ${x.broker} ${x.line_of_business ?? ""}`.toLowerCase().includes(find.trim().toLowerCase())));
  const soFar = r.cadence !== "month" && r.cadence !== "day" && r.money_month ? `${fmtMonth(r.money_month)} so far` : null;
  const premiumLink = (x: ManagementRow) => {
    const scope = { program: x.program_id, broker: x.broker_party_id };
    const label = `Gross premium · ${x.programme} · ${x.broker}`;
    return r.money_month
      ? drillLink({ from: "financial", label, basis: "month", period: r.money_month, metric: "gross_premium" }, scope)
      : checksLink(r, `${label} · files processed that day`, "files", scope);
  };
  const tiles: [string, string, typeof Layers, string, string?][] = [
    ["Total programmes", String(t.programmes), Layers, C.brand, `${t.writing} writing business`],
    ["Expected files", String(t.expected), FileText, C.info, "Due by the end of the period"],
    ["Received files", String(t.received), CheckCircle2, "#16a34a"],
    ["Missing files", String(t.missing), XCircle, C.crit],
    ["Validation errors", String(t.errors), AlertTriangle, C.warn],
    ["Premium reported", money(t.premium, cur), Wallet, "#16a34a", soFar ?? (r.cadence === "day" ? "Files processed that day" : undefined)],
  ];
  return (
    <>
      <div className="card" style={{ marginBottom: 14, overflow: "visible" }}>
        <div className="card-h" style={{ gap: 10, flexWrap: "wrap", borderBottom: "none" }}>
          <select className="fbar-select" value={programme} onChange={e => { setProgramme(e.target.value); setOpen(null); }}>
            <option value="">All programmes</option>
            {programmes.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
          <select className="fbar-select" value={broker} onChange={e => { setBroker(e.target.value); setOpen(null); }}>
            <option value="">All brokers</option>
            {brokers.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
          <select className="fbar-select" value={status} onChange={e => { setStatus(e.target.value); setOpen(null); }}>
            <option value="">All status</option>
            <option value="Attention">Attention</option>
            <option value="OK">OK</option>
          </select>
          <span className="search" style={{ flex: "1 1 220px" }}>
            <Search className="ic" />
            <input type="search" placeholder="Search programme…" aria-label="Search programmes and brokers"
              value={find} onChange={e => setFind(e.target.value)} />
          </span>
        </div>
      </div>

      <div style={{ display: "grid", gap: 10, marginBottom: 18, gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))" }}>
        {tiles.map(([k, v, Icon, colour, sub]) => (
          <div className="tile" key={k} style={{ padding: "12px 14px" }}>
            <div className="k" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Icon size={18} color={colour} style={{ flex: "none" }} /> {k}</div>
            <div className="v" style={k === "Missing files" && t.missing ? { color: C.crit } : undefined}>{v}</div>
            {sub && <div className="sub">{sub}</div>}
          </div>))}
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h">
          <h3>Programmes and brokers</h3>
          <InfoTip text={"One row per programme and broker.\nExpected: files due by the end of the period. Missing: due and not yet come."} />
          <span className="sub">{rows.length === r.rows.length ? plural(r.rows.length, "row") : `${rows.length} of ${r.rows.length}`}</span>
        </div>
        {rows.length === 0 ? <div className="empty">{r.rows.length ? "Nothing matches." : "No programme has a broker yet."}</div> : (
          <div className="tbl-wrap"><table style={FIXED}>
            <colgroup>
              <col style={{ width: "13%" }} /><col style={{ width: "12%" }} /><col style={{ width: "10%" }} />
              <col style={{ width: "9%" }} /><col style={{ width: "7%" }} /><col style={{ width: "7%" }} />
              <col style={{ width: "7%" }} /><col style={{ width: "6%" }} /><col style={{ width: "11%" }} />
              <col style={{ width: "8%" }} /><col style={{ width: "10%" }} />
            </colgroup>
            <thead><tr>
              <th className="l">Programme</th><th className="l">Broker</th><th className="l">Line of business</th>
              <th className="l">Reporting period</th><th>Expected files</th><th>Received files</th><th>Missing files</th>
              <th>Errors</th><th>Premium{soFar && <InfoTip text={`Premium is reported by bordereau month, so this is ${soFar}.`} />}</th>
              <th>Status<InfoTip text={STATUS_TIP} /></th><th>View</th>
            </tr></thead>
            <tbody>{rows.map(x => {
              const key = `${x.program_id}:${x.broker_party_id}`;
              const scope = { program: x.program_id, broker: x.broker_party_id };
              const isOpen = open === key;
              return (
                <Fragment key={key}>
                  <tr style={isOpen ? { background: "var(--p-surface-2)" } : undefined}>
                    <td className="l" style={WRAP}>{x.programme}</td>
                    <td className="l" style={WRAP}>{x.broker}</td>
                    <td className="l" style={WRAP}>{x.line_of_business ?? "—"}</td>
                    <td className="l" style={WRAP}>{x.reporting_period ?? "—"}</td>
                    <td>{x.expected}</td>
                    <td>{x.received}</td>
                    <td>{x.missing ? <b style={{ color: C.crit }}>{x.missing}</b> : 0}</td>
                    <td>{x.errors ? <Link className="linkish" title="See the policies behind this number"
                      to={checksLink(r, `Errors · ${x.programme} · ${x.broker}`, "errors", scope)}>{x.errors}</Link> : 0}</td>
                    <td style={WRAP}>{x.premium == null ? "—" : <Link className="linkish" to={premiumLink(x)}
                      title="See the policies behind this number">{money(x.premium, cur)}</Link>}</td>
                    <td><span className={`badge ${x.status === "OK" ? "b-ok" : "b-warn"}`}>{x.status}</span></td>
                    <td>{x.broker_party_id == null ? <span className="sub">—</span> : (
                      <button type="button" className="linkish" onClick={() => setOpen(isOpen ? null : key)}
                        style={{ background: "none", border: 0, padding: 0, cursor: "pointer", whiteSpace: "nowrap",
                                 display: "inline-flex", alignItems: "center", gap: 2 }}>
                        View details {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button>)}</td>
                  </tr>
                  {isOpen && x.broker_party_id != null && (
                    <tr><td colSpan={11} style={{ background: "var(--p-surface-2)", padding: 0 }}>
                      <Detail r={r} row={x} />
                    </td></tr>)}
                </Fragment>);
            })}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}

// ── one programme's broker, under its row ───────────────────────────────────

const TABS = ["Overview", "Submissions", "Validation errors", "Premium & exposure", "Contract details", "Treaties"] as const;
type Tab = typeof TABS[number];

const when = (iso: string | null) => (iso ? new Date(iso.endsWith("Z") ? iso : iso + "Z").toLocaleString("en-GB",
  { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }).replace(",", " –") : "—");

function result(f: ProgrammeBroker["submissions"][number]): { text: string; colour: string } {
  if (f.status === "clean") return { text: "File processed, no errors", colour: "#16a34a" };
  if (f.status === "has_exceptions") return { text: `File processed, ${plural(f.errors, "error")}`, colour: C.warn };
  return { text: "File could not be checked", colour: C.mut };
}

const OWED_BADGE: Record<string, string> = {
  overdue: "b-crit", never_arrived: "b-crit", received_late: "b-warn", due_today: "b-info", on_time: "b-ok" };

function Detail({ r, row }: { r: Report; row: ManagementRow }) {
  const [d, setD] = useState<ProgrammeBroker | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  useEffect(() => {
    let live = true;
    getProgrammeBroker(row.program_id, row.broker_party_id as number, { cadence: r.cadence, period: r.period })
      .then(x => { if (live) setD(x); })
      .catch(e => { if (live) setFailed(e?.response?.data?.detail ?? "Could not load the details."); });
    return () => { live = false; };
  }, [r.cadence, r.period, row.program_id, row.broker_party_id]);
  return (
    <div style={{ padding: "4px 16px 16px" }}>
      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--p-border)", marginBottom: 14, flexWrap: "wrap" }}>
        {TABS.map(k => (
          <button key={k} type="button" onClick={() => setTab(k)}
            style={{ background: "none", border: 0, cursor: "pointer", padding: "10px 12px", fontSize: 13.5,
                     fontWeight: tab === k ? 700 : 500, color: tab === k ? "var(--p-brand, #077282)" : "var(--p-muted)",
                     borderBottom: `2px solid ${tab === k ? "var(--p-brand, #077282)" : "transparent"}`, marginBottom: -1 }}>
            {k}</button>))}
      </div>
      {failed ? <div className="empty">{failed}</div> : !d ? <div className="empty">Loading…</div>
        : tab === "Overview" ? <Overview d={d} />
        : tab === "Submissions" ? <Submissions d={d} />
        : tab === "Validation errors" ? <Errors d={d} />
        : tab === "Premium & exposure" ? <Premium d={d} />
        : tab === "Contract details" ? <Contract d={d} />
        : <Treaties d={d} />}
    </div>
  );
}

const panel = { background: "var(--p-surface, #fff)", minWidth: 0, margin: 0 } as const;

function Overview({ d }: { d: ProgrammeBroker }) {
  const cur = d.money.currency;
  const pct = d.owed.expected ? Math.round(100 * d.owed.received / d.owed.expected) : null;
  const v = d.validation;
  const clean = Math.max(v.rows - v.rows_with_errors, 0);
  const donut = [{ name: "Rows with no errors", n: clean, fill: "#16a34a" },
                 { name: "Rows with errors", n: v.rows_with_errors, fill: C.crit }].filter(x => x.n > 0);
  const late = d.owed.list.filter(o => ["overdue", "never_arrived", "received_late", "due_today"].includes(o.state));
  return (
    <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
      <div className="card" style={panel}>
        <div className="card-h"><h3>Submission history</h3><span className="sub">{plural(d.files_total, "file")}</span></div>
        {d.submissions.length === 0 ? <div className="empty">No files processed in this period.</div> : (
          <div style={{ padding: "10px 16px", display: "grid", gap: 10 }}>
            {d.submissions.slice(0, 3).map((f, i) => {
              const x = result(f);
              return (
                <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                  <CheckCircle2 size={18} color={x.colour} style={{ flex: "none", marginTop: 1 }} />
                  <div><div style={{ fontWeight: 600, fontSize: 13 }}>{when(f.at)}</div>
                    <div className="sub">{x.text}</div></div>
                </div>);
            })}
          </div>)}
      </div>

      <div className="card" style={panel}>
        <div className="card-h"><h3>Expected vs received</h3>
          <InfoTip text="Files due by the end of the period, and how many of them have come." /></div>
        <div style={{ padding: "12px 16px", display: "grid", gap: 10, fontSize: 13.5 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <CircleDot size={14} color={C.info} /> Expected <b style={{ marginLeft: "auto" }}>{d.owed.expected}</b></div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <CircleDot size={14} color={C.brand} /> Received <b style={{ marginLeft: "auto" }}>{d.owed.received}</b></div>
          {pct != null ? (
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ flex: 1, height: 8, borderRadius: 4, background: "var(--p-border)" }}>
                <div style={{ width: `${pct}%`, height: "100%", borderRadius: 4, background: C.brand }} /></div>
              <b>{pct}%</b>
            </div>) : <div className="sub">No files were due.</div>}
        </div>
      </div>

      <div className="card" style={panel}>
        <div className="card-h"><h3>Validation result</h3>
          <InfoTip text="The rows in the files of this period, as each file first arrived." /></div>
        {v.rows === 0 ? <div className="empty">No rows checked.</div> : (
          <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 16px" }}>
            <div style={{ flex: 1, display: "grid", gap: 6, fontSize: 13 }}>
              {[["Rows with no errors", clean, "#16a34a"], ["Rows with errors", v.rows_with_errors, C.crit]].map(([k, n, c]) => (
                <div key={k as string} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 5, background: c as string }} />{k}
                  <b style={{ marginLeft: "auto" }}>{(n as number).toLocaleString("en-GB")}</b>
                  <span className="sub" style={{ width: 40, textAlign: "right" }}>{Math.round(100 * (n as number) / v.rows)}%</span>
                </div>))}
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>
                <span className="badge b-crit">{v.critical} critical</span>
                <span className="badge b-warn">{v.warning} warning</span>
                <span className="badge b-info">{v.info} info</span>
              </div>
            </div>
            <div style={{ width: 96, height: 96, flex: "none" }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart><Pie data={donut} dataKey="n" nameKey="name" innerRadius={28} outerRadius={46} isAnimationActive={false}>
                  {donut.map(x => <Cell key={x.name} fill={x.fill} />)}</Pie><Tooltip /></PieChart>
              </ResponsiveContainer>
            </div>
          </div>)}
      </div>

      <div className="card" style={panel}>
        <div className="card-h"><h3>Premium summary</h3>
          {d.money.month && <span className="sub">{fmtMonth(d.money.month)}</span>}</div>
        <div style={{ padding: "8px 16px 14px", display: "grid", gap: 8, fontSize: 13.5 }}>
          {([["Gross premium", d.money.gross_premium], ["Commission", d.money.commission],
             ["Earned premium", d.money.earned_premium]] as const).map(([k, f]) => (
            <div key={k} style={{ display: "flex" }}>{k}<b style={{ marginLeft: "auto" }}>{money(f?.current, cur)}</b></div>))}
        </div>
      </div>

      <div className="card" style={panel}>
        <div className="card-h"><h3>Contract details</h3></div>
        {!d.contract ? <div className="empty">No contract with this broker.</div> : (
          <div style={{ padding: "8px 16px 14px", display: "grid", gap: 8, fontSize: 13.5 }}>
            <div style={{ display: "flex", gap: 12 }}>Contract
              <Link className="linkish" style={{ marginLeft: "auto", textAlign: "right" }} to={`/contracts/${d.contract.id}`}>{d.contract.name}</Link></div>
            <div style={{ display: "flex" }}>Type<b style={{ marginLeft: "auto" }}>{d.contract.type ?? "—"}</b></div>
            <div style={{ display: "flex" }}>Starts<b style={{ marginLeft: "auto" }}>{d.contract.starts ? shortDay(d.contract.starts) : "—"}</b></div>
            {(d.contracts?.length ?? 0) > 1 && (
              <div style={{ display: "flex" }}>Other contracts<b style={{ marginLeft: "auto" }}>{d.contracts.length - 1}</b></div>)}
            <div style={{ display: "flex" }}>Treaties on this programme<b style={{ marginLeft: "auto" }}>{d.treaties?.length ?? 0}</b></div>
          </div>)}
      </div>

      <div className="card" style={panel}>
        <div className="card-h"><h3>Missing or late files</h3></div>
        {late.length === 0 ? <div className="empty">Nothing missing or late.</div> : (
          <div className="tbl-wrap"><table style={FIXED}>
            <thead><tr><th className="l">Month</th><th>Due</th><th>Status</th></tr></thead>
            <tbody>{late.slice(0, 5).map((o, i) => (
              <tr key={i}><td className="l">{fmtPeriod(o.month)}</td><td>{shortDay(o.due_on)}</td>
                <td><span className={`badge ${OWED_BADGE[o.state] ?? "b-mut"}`}>{o.status}</span></td></tr>))}</tbody>
          </table></div>)}
      </div>
    </div>
  );
}

function Submissions({ d }: { d: ProgrammeBroker }) {
  return (
    <div className="card" style={panel}>
      <div className="card-h"><h3>Files processed</h3>
        <InfoTip text="Each file processed in this period, as it first arrived. Newest first." />
        <span className="sub">{d.submissions.length < d.files_total ? `Showing ${d.submissions.length} of ${d.files_total}` : plural(d.files_total, "file")}</span>
      </div>
      {d.submissions.length === 0 ? <div className="empty">No files processed in this period.</div> : (
        <div className="tbl-wrap"><table style={FIXED}>
          <colgroup><col style={{ width: "20%" }} /><col style={{ width: "30%" }} /><col style={{ width: "13%" }} />
            <col style={{ width: "10%" }} /><col style={{ width: "10%" }} /><col style={{ width: "17%" }} /></colgroup>
          <thead><tr><th className="l">Processed</th><th className="l">File</th><th>Month</th><th>Rows</th><th>Errors</th><th>Result</th></tr></thead>
          <tbody>{d.submissions.map((f, i) => (
            <tr key={i}>
              <td className="l">{when(f.at)}</td>
              <td className="l" style={WRAP}>{f.filename ?? "—"}</td>
              <td>{f.month ? fmtPeriod(f.month) : "—"}</td>
              <td>{f.rows.toLocaleString("en-GB")}</td>
              <td>{f.errors ? <b style={{ color: f.critical ? C.crit : C.warn }}>{f.errors}</b> : 0}</td>
              <td><span className={`badge ${f.status === "clean" ? "b-ok" : f.status === "has_exceptions" ? "b-warn" : "b-mut"}`}>
                {f.status === "clean" ? "No errors" : f.status === "has_exceptions" ? "Errors" : "Not checked"}</span></td>
            </tr>))}</tbody>
        </table></div>)}
    </div>
  );
}

function Errors({ d }: { d: ProgrammeBroker }) {
  const scope = { program: d.programme.id, broker: d.broker.id };
  const v = d.validation;
  return (
    <div className="card" style={panel}>
      <div className="card-h"><h3>Checks that failed most</h3>
        <InfoTip text="The checks that failed most often in this broker's files this period. Click an error count to see the policies." />
        <span className="sub">{plural(v.errors, "error")} · {v.critical} critical · {v.warning} warning · {v.info} info</span>
      </div>
      {v.top_errors.length === 0 ? <div className="empty">No errors found.</div> : (
        <div className="tbl-wrap"><table>
          <thead><tr><th className="l">Check</th><th>Severity</th><th>Files</th><th>Errors</th></tr></thead>
          <tbody>{v.top_errors.map(x => (
            <tr key={x.check}>
              <td className="l">{x.check}</td>
              <td><span className={`badge ${x.severity === "critical" ? "b-crit" : x.severity === "warning" ? "b-warn" : "b-info"}`}>
                {x.severity ? x.severity[0].toUpperCase() + x.severity.slice(1) : "—"}</span></td>
              <td>{x.files}</td>
              <td>{x.key ? <Link className="linkish" title="See the policies behind this number"
                to={checksLink(d, `Errors · ${d.programme.name} · ${d.broker.name} · ${x.check}`, "errors", scope, { check: x.key })}>
                {x.errors}</Link> : x.errors}</td>
            </tr>))}</tbody>
        </table></div>)}
    </div>
  );
}

function Change({ now, then }: { now: number | null; then: number | null | undefined }) {
  if (now == null || then == null || then === 0) return <span className="sub">—</span>;
  const c = Math.round(1000 * (now - then) / Math.abs(then)) / 10;
  return <span style={{ color: c < 0 ? C.crit : "#16a34a", fontWeight: 600 }}>{c < 0 ? "▼" : "▲"} {Math.abs(c)}%</span>;
}

function Premium({ d }: { d: ProgrammeBroker }) {
  const cur = d.money.currency;
  const daily = d.cadence === "day";
  const cap = d.contract?.cap;
  const scope = { program: d.programme.id, broker: d.broker.id };
  const figs: [string, Fig | null | undefined, string][] = [
    ["Gross premium", d.money.gross_premium, "gross_premium"], ["Commission", d.money.commission, "commission_amount"],
    ["Earned premium", d.money.earned_premium, "earned_premium"]];
  return (
    <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
      <div className="card" style={panel}>
        <div className="card-h"><h3>Premium</h3>
          <span className="sub">{daily ? "Files processed that day" : d.money.month ? fmtMonth(d.money.month) : ""}</span></div>
        <div className="tbl-wrap"><table>
          <thead><tr><th className="l"></th><th>This period</th><th>vs {VS[d.cadence]}</th><th>vs last year</th></tr></thead>
          <tbody>{figs.map(([k, f, metric]) => (
            <tr key={k}>
              <td className="l">{k}</td>
              <td>{f?.current == null ? "—" : <Link className="linkish" title="See the policies behind this number" to={d.money.month
                ? drillLink({ from: "financial", label: `${k} · ${d.programme.name} · ${d.broker.name}`, basis: "month",
                              period: d.money.month, metric }, scope)
                : checksLink(d, `${k} · ${d.programme.name} · ${d.broker.name} · files processed that day`, "files", scope)}>
                {money(f.current, cur)}</Link>}</td>
              <td><Change now={f?.current ?? null} then={f?.previous} /></td>
              <td><Change now={f?.current ?? null} then={f?.last_year} /></td>
            </tr>))}</tbody>
        </table></div>
      </div>
      <div className="card" style={panel}>
        <div className="card-h"><h3>Premium cap used</h3>
          <InfoTip text="Premium written so far in the contract's term, against its premium cap. Near cap from 85%." /></div>
        {!cap ? <div className="empty">{d.contract ? "This contract has no premium cap set." : "No contract with this broker."}</div> : (
          <div style={{ padding: "12px 16px", display: "grid", gap: 10, fontSize: 13.5 }}>
            <div style={{ display: "flex" }}>Premium cap<b style={{ marginLeft: "auto" }}>{money(cap.cap, cap.currency)}</b></div>
            <div style={{ display: "flex" }}>Written so far<b style={{ marginLeft: "auto" }}>{money(cap.written, cap.currency)}</b></div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div style={{ flex: 1, height: 8, borderRadius: 4, background: "var(--p-border)" }}>
                <div style={{ width: `${Math.min(cap.used_pct ?? 0, 100)}%`, height: "100%", borderRadius: 4,
                              background: cap.flag === "Over cap" ? C.crit : cap.flag ? C.warn : C.brand }} /></div>
              <b>{cap.used_pct == null ? "—" : `${cap.used_pct}%`}</b>
              {cap.flag && <span className={`badge ${cap.flag === "Over cap" ? "b-crit" : "b-warn"}`}>{cap.flag}</span>}
            </div>
          </div>)}
      </div>
    </div>
  );
}

/** "in_review" → "In review". */
const words = (v: string | null) => (v ? (v[0].toUpperCase() + v.slice(1)).replace(/_/g, " ") : null);
const term = (c: ContractDetail) =>
  c.starts || c.ends ? `${c.starts ? shortDay(c.starts) : "—"} – ${c.ends ? shortDay(c.ends) : "—"}` : "—";

function Contract({ d }: { d: ProgrammeBroker }) {
  const list = d.contracts ?? (d.contract ? [d.contract] : []);
  const [pick, setPick] = useState<number | null>(null);
  const c = list.find(x => x.id === pick) ?? list[0];
  const rows: [string, string | null][] = c ? [
    ["Contract", c.name], ["Type", c.type], ["Status", words(c.status)],
    ["Starts", c.starts ? shortDay(c.starts) : null], ["Ends", c.ends ? shortDay(c.ends) : null],
    ["UMR", c.umr], ["Class of business", c.class_of_business], ["Year of account", c.year_of_account],
    ["Premium cap", c.cap ? money(c.cap.cap, c.cap.currency) : null]] : [];
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div className="card" style={panel}>
        <div className="card-h"><h3>{d.broker.name}&rsquo;s contracts ({list.length})</h3>
          {list.length > 1 && <InfoTip text={"This broker holds more than one contract on this programme.\nClick a contract to see its details."} />}</div>
        {list.length === 0 ? <div className="empty">No contract with this broker.</div> : (
          <div className="tbl-wrap"><table>
            <thead><tr><th className="l">Contract</th><th className="l">Type</th><th className="l">Status</th>
              <th className="l">Term</th><th className="l">UMR</th><th>Premium cap used</th></tr></thead>
            <tbody>{list.map(x => (
              <tr key={x.id} onClick={() => setPick(x.id)} style={{ cursor: "pointer",
                background: x.id === c?.id && list.length > 1 ? "var(--p-hover, #f5f7fb)" : undefined }}>
                <td className="l"><b>{x.name}</b>{x.current && list.length > 1 && <span className="badge b-ok" style={{ marginLeft: 6 }}>Current</span>}</td>
                <td className="l">{x.type ?? "—"}</td>
                <td className="l">{words(x.status) ?? "—"}</td>
                <td className="l">{term(x)}</td>
                <td className="l">{x.umr ?? "—"}</td>
                <td>{x.cap?.used_pct == null ? "—" : `${x.cap.used_pct}%`}</td>
              </tr>))}</tbody>
          </table></div>)}
      </div>

      {c && (
        <div className="card" style={panel}>
          <div className="card-h"><h3>Contract details</h3>
            <Link className="linkish right" style={{ fontSize: 13 }} to={`/contracts/${c.id}`}>Open contract</Link></div>
          <div className="tbl-wrap"><table style={FIXED}>
            <colgroup><col style={{ width: "35%" }} /><col style={{ width: "65%" }} /></colgroup>
            <tbody>{rows.map(([k, v]) => (
              <tr key={k}><td className="l sub">{k}</td><td className="l" style={WRAP}>{v || "—"}</td></tr>))}</tbody>
          </table></div>
        </div>)}

    </div>
  );
}

function Treaties({ d }: { d: ProgrammeBroker }) {
  const treaties = d.treaties ?? [];
  return (
    <div className="card" style={panel}>
      <div className="card-h"><h3>Treaties on this programme ({treaties.length})</h3>
        <InfoTip text={"Reinsurance treaties that protect this programme.\nThey cover the business of every broker on it."} /></div>
      {treaties.length === 0 ? <div className="empty">No reinsurance treaties on this programme.</div> : (
        <div className="tbl-wrap"><table>
          <thead><tr><th className="l">Treaty</th><th className="l">Type</th><th className="l">Reinsurers</th>
            <th className="l">Status</th><th className="l">Term</th><th className="l">UMR</th></tr></thead>
          <tbody>{treaties.map(t => (
            <tr key={t.id}>
              <td className="l"><Link className="linkish" to={`/contracts/${t.id}`}>{t.name}</Link></td>
              <td className="l">{t.type ?? "—"}</td>
              <td className="l" style={WRAP}>{t.reinsurers.join(", ") || "—"}</td>
              <td className="l">{words(t.status) ?? "—"}</td>
              <td className="l">{term(t)}</td>
              <td className="l">{t.umr ?? "—"}</td>
            </tr>))}</tbody>
        </table></div>)}
    </div>
  );
}
