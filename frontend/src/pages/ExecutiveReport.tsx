// Executive Reporting 38.2 — the Executive Report: the carrier's whole book for
// one day, week or month, or any dates picked (backend executive_report.py). Programmes,
// Needs attention and Reinsurance follow the period too, as at its last day.
// To read here, download
// as a PDF or Excel file, send now to anyone by email, or send by itself on a
// schedule. Every number is the count its own screen makes, and the money and
// volume numbers open the policies behind them (38.1).
import { Fragment, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { CalendarClock, FileSpreadsheet, FileText, History, Send } from "lucide-react";
import {
  clearExecutiveSends, downloadExecutiveReport, downloadSentExecutive, getExecutiveDelivery, getExecutiveReport,
  saveExecutiveDelivery, sendExecutiveReport,
  type Cadence, type ExecutiveDelivery, type ExecutiveReport as Report, type ExecutiveSettings,
  type FileFormat, type Login, type ScheduleCadence, type ScoreRow, type TrendUnit,
} from "../api/executive";
import { drillLink } from "../api/policies";
import { InfoTip } from "../components/InfoTip";
import { showDone, showError } from "../components/ResultPopup";
import { Modal } from "../components/ui/Modal";
import { fmtDay, fmtMonth, lastMonths, ordinal, plural } from "../utils/reportFormat";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
const CADENCES: { key: Cadence; label: string }[] = [
  { key: "day", label: "Daily" }, { key: "week", label: "Weekly" },
  { key: "month", label: "Monthly" }, { key: "custom", label: "Custom" }];
const FORMATS: { key: FileFormat; label: string }[] = [
  { key: "pdf", label: "PDF" }, { key: "excel", label: "Excel" }, { key: "both", label: "Both" }];

/** The last twelve complete weeks' Mondays, newest first, as YYYY-MM-DD. */
function lastWeeks(n = 12): string[] {
  const now = new Date();
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() - 7 * (i + 1));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  });
}

/** "2 – 8 Nov 2026". */
function weekLabel(monday: string): string {
  const [y, m, d] = monday.split("-").map(Number);
  const a = new Date(y, m - 1, d), b = new Date(y, m - 1, d + 6);
  return `${a.getDate()}${a.getMonth() !== b.getMonth() ? " " + a.toLocaleDateString("en-GB", { month: "short" }) : ""}`
    + ` – ${b.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}`;
}

function money(v: number | null | undefined, currency: string | null): string {
  if (v == null) return "—";
  const t = v.toLocaleString("en-GB", { maximumFractionDigits: 0 });
  return currency && currency !== "none" && currency !== "review" ? `${currency} ${t}` : t;
}

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const dayBefore = (n: number) => { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); };

/** "1 – 31 Oct 2026" from two YYYY-MM-DD days; one day reads "11 Nov 2026". */
function span(from: string, to: string): string {
  if (from === to) return fmtDay(from);
  const [ya, ma, da] = from.split("-").map(Number), [yb, mb, db] = to.split("-").map(Number);
  const a = new Date(ya, ma - 1, da), b = new Date(yb, mb - 1, db);
  const tail = b.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  if (ya !== yb) return `${a.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })} – ${tail}`;
  return `${a.getDate()}${ma !== mb ? " " + a.toLocaleDateString("en-GB", { month: "short" }) : ""} – ${tail}`;
}

/** "vs last week" — what a report is compared with. */
const TREND_TITLE: Record<TrendUnit, string> = {
  hour: "Errors by hour", day: "Errors by day", week: "Errors by week", month: "Errors by month" };
const VS: Record<Cadence, string> = { day: "the day before", week: "last week", month: "last month", custom: "the period before" };

/** "USD 125,000 (+25.0% vs last month)" → ["USD 125,000", "+25.0% vs last month"]. */
function split(v: string): [string, string | null] {
  const m = /^(.*?) \((.*)\)$/.exec(v);
  return m ? [m[1], m[2]] : [v, null];
}

const who = (p: { name: string | null; email: string }) => (p.name ? `${p.name} (${p.email})` : p.email);

export default function ExecutiveReport() {
  const months = useMemo(() => lastMonths(), []);
  const weeks = useMemo(() => lastWeeks(), []);
  const [cadence, setCadence] = useState<Cadence>("month");
  const [period, setPeriod] = useState<string>(months[1]);
  const [data, setData] = useState<Report | null>(null);
  /** Why the report could not load — the server's own words when it gives them. */
  const [failed, setFailed] = useState<string | null>(null);
  const [delivery, setDelivery] = useState<ExecutiveDelivery | null>(null);
  const [busy, setBusy] = useState<"pdf" | "excel" | null>(null);
  const [sendOpen, setSendOpen] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [sentOpen, setSentOpen] = useState(false);

  useEffect(() => {
    let live = true;
    setFailed(null);
    getExecutiveReport({ cadence, period })
      .then(r => { if (live) setData(r); })
      .catch(e => { if (live) setFailed(e?.response?.data?.detail ?? "Could not load the report."); });
    return () => { live = false; };
  }, [cadence, period]);

  const reloadDelivery = () => getExecutiveDelivery().then(setDelivery).catch(() => setDelivery(null));
  useEffect(() => { reloadDelivery(); }, []);

  // Custom: the from and to dates typed; the report asks only once both make sense.
  const [from, setFrom] = useState(dayBefore(30));
  const [to, setTo] = useState(dayBefore(1));
  const customOk = !!from && !!to && from <= to;
  useEffect(() => {
    if (cadence === "custom" && customOk) setPeriod(`${from}..${to}`);
  }, [cadence, from, to, customOk]);

  const pick = (c: Cadence) => {
    setCadence(c);
    setPeriod(c === "month" ? months[1] : c === "week" ? weeks[0] : c === "day" ? dayBefore(0) : `${from}..${to}`);
    setData(null);
  };
  const r = data && data.cadence === cadence && data.period === period ? data : null;

  async function download(format: "pdf" | "excel") {
    setBusy(format);
    try { await downloadExecutiveReport({ cadence, period, format }); }
    catch (e) { showError(e, "Could not make the file"); }
    finally { setBusy(null); }
  }

  const cfg = delivery?.settings;
  const next = cfg?.auto_send ? delivery?.next : null;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Executive Report
              <InfoTip text="Your whole book at a glance for one day, week or month, or any dates you pick. Download it, or send it to anyone by email." />
            </h2>
          </div>
          <div className="actions" style={{ flexWrap: "wrap" }}>
            <button className="btn" onClick={() => download("pdf")} disabled={busy != null}>
              <FileText size={15} /> {busy === "pdf" ? "Making…" : "Download PDF"}
            </button>
            <button className="btn" onClick={() => download("excel")} disabled={busy != null}>
              <FileSpreadsheet size={15} /> {busy === "excel" ? "Making…" : "Download Excel"}
            </button>
            <button className="btn" onClick={() => setSentOpen(true)} disabled={!delivery}>
              <History size={15} /> Sent reports
              {delivery && <span className={`badge ${delivery.sends.length ? "b-info" : "b-mut"}`} style={{ padding: "2px 8px" }}>
                {delivery.sends.length}</span>}
            </button>
            <button className="btn" onClick={() => setScheduleOpen(true)} disabled={!delivery}>
              <CalendarClock size={15} /> Schedule
              <span className={`badge ${next ? "b-info" : "b-mut"}`} style={{ padding: "2px 8px" }}>
                {next ? fmtDay(next.send_on) : "Off"}
              </span>
            </button>
            <button className="btn pri" onClick={() => setSendOpen(true)} disabled={!delivery || !r}>
              <Send size={15} /> Send
            </button>
          </div>
        </div>

        <div className="card" style={{ overflow: "visible", marginBottom: 14 }}>
          <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none", alignItems: "flex-end" }}>
            <label className="fbar-field">
              <span className="sub">Report</span>
              <span className="seg">
                {CADENCES.map(c => (
                  <button key={c.key} type="button" className={cadence === c.key ? "on" : ""}
                    onClick={() => pick(c.key)}>{c.label}</button>
                ))}
              </span>
            </label>
            {cadence === "custom" && (<>
              <label className="fbar-field">
                <span className="sub">From</span>
                <input type="date" className="fbar-date" value={from} max={to || dayBefore(0)}
                  onChange={e => setFrom(e.target.value)} />
              </label>
              <label className="fbar-field">
                <span className="sub">To</span>
                <input type="date" className="fbar-date" value={to} min={from || undefined} max={dayBefore(0)}
                  onChange={e => setTo(e.target.value)} />
              </label>
            </>)}
            {(cadence === "week" || cadence === "month") && (
              <label className="fbar-field">
                <span className="sub">{cadence === "week" ? "Week" : "Month"}</span>
                <select className="fbar-select" value={period} onChange={e => setPeriod(e.target.value)}>
                  {(cadence === "week" ? weeks : months).map(p => (
                    <option key={p} value={p}>{cadence === "week" ? weekLabel(p) : fmtMonth(p)}</option>
                  ))}
                </select>
              </label>
            )}
            {r && <span className="sub" style={{ paddingBottom: 8 }}>
              {r.cadence === "day"
                ? <>Today so far, {fmtDay(r.from)}, compared with yesterday. Only what happened today.</>
                : <>Covers {span(r.from, r.to)}, compared with {span(r.previous.from, r.previous.to)}.
                    {!r.complete && " Still running, so the numbers count to today."}</>}
            </span>}
          </div>
        </div>

        {cadence === "custom" && !customOk
          ? <div className="card"><div className="empty">Pick a from date on or before the to date.</div></div>
          : failed ? <div className="card"><div className="empty">{failed}</div></div>
          : !r ? <div className="card"><div className="empty">Loading…</div></div>
          : <Body r={r} />}

        {delivery && <SentDrawer open={sentOpen} delivery={delivery} onClose={() => setSentOpen(false)}
          onCleared={setDelivery} />}
      </div>

      {delivery && r && (
        <SendModal open={sendOpen} onClose={() => setSendOpen(false)} report={r}
          logins={delivery.logins} settings={delivery.settings}
          onSent={() => { setSendOpen(false); reloadDelivery(); }} />
      )}
      {delivery && (
        <ScheduleModal open={scheduleOpen} onClose={() => setScheduleOpen(false)} delivery={delivery}
          onSaved={d => { setDelivery(d); setScheduleOpen(false); }} />
      )}
    </div>
  );
}

/** Where a headline number leads (38.1), or null. */
function headlineLink(r: Report, label: string): string | null {
  const month = r.money.month;
  // A daily report's money is from the files processed that day, not a reporting month.
  if (month != null && label.startsWith("Gross premium"))
    return drillLink({ from: "financial", label: "Gross premium", basis: "month", period: month, metric: "gross_premium" });
  if (month != null && label.startsWith("Commission"))
    return drillLink({ from: "financial", label: "Commission", basis: "month", period: month, metric: "commission_amount" });
  if (label === "Files processed" && r.cadence === "month")
    return drillLink({ from: "monthly", label: "Files handled", month: r.period, measure: "files" });
  if (label === "Clean first time" && r.cadence === "month")
    return drillLink({ from: "monthly", label: "Files clean first time", month: r.period, measure: "clean" });
  if (label === "On time") return r.timeliness.month ? `/bordereau-calendar?month=${r.timeliness.month}` : "/bordereau-calendar";
  if (["Open problems", "Escalations", "New problems", "New escalations"].includes(label)) return "/open-problems";
  return null;
}

function Body({ r }: { r: Report }) {
  const cur = r.money.currency;
  const vs = VS[r.cadence];
  const daily = r.cadence === "day";
  const short = r.cadence !== "month" && !daily;
  const monthly = r.cadence === "month";
  // Shorter than a month: premium and timeliness are for the month the period ends in.
  const soFar = (label: string) => short
    && (label.startsWith("Gross premium") || label.startsWith("Commission") || label === "On time")
    ? `${fmtMonth((label === "On time" ? r.timeliness.month : r.money.month) ?? "").split(" ")[0]} so far` : null;
  const heading = `For ${r.cadence === "week" ? weekLabel(r.period) : daily ? `today, ${fmtDay(r.period)}`
    : r.cadence === "custom" ? span(r.from, r.to) : r.title}`;
  const rows: [string, typeof r.headline][] = [[heading, r.headline]];
  // "as at 31 Oct 2026" — where the position sections stand.
  // "as at 31 Oct 2026" — short, so it fits beside a card's title.
  const asAt = `as at ${new Date(r.as_at + "T00:00:00").toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric" })}`;
  return (
    <>
      {/* Both groups on one line, each as wide as its tiles; they wrap on a narrow screen. */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16, marginBottom: 18 }}>
      {rows.map(([heading, tiles]) => (
      <section key={heading} style={{ flex: `${tiles.length} 1 ${tiles.length * 160}px`, minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 4, margin: "0 0 6px", fontSize: 11.5, fontWeight: 600,
                    letterSpacing: ".04em", textTransform: "uppercase", color: "var(--p-muted)" }}>
        {heading}
      </div>
      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
        {tiles.map(h => {
          const [big, small] = split(h.value);
          const to = headlineLink(r, h.label);
          return (
            <div className="tile" key={h.label} style={{ padding: "12px 14px" }}>
              <div className="k" style={{ display: "flex", alignItems: "center" }}>
                {h.label}
                {short && h.label.startsWith("Gross premium") && (
                  <InfoTip text={`Premium is reported by bordereau month, so this is ${fmtMonth(r.money.month ?? "")} so far.`} />
                )}
                {h.label === "Escalations" && (
                  <InfoTip text="Late files marked delinquent, disputes waiting for your decision, and anything past its due date." />
                )}
              </div>
              <div className="v">{to ? <Link className="linkish" style={{ color: "inherit" }} to={to}
                title="See what is behind this number">{big}</Link> : big}</div>
              <div className="sub">{small ?? `No comparison with ${vs}`}</div>
              {soFar(h.label) && <div className="sub">{soFar(h.label)}</div>}
            </div>
          );
        })}
      </div>
      </section>))}
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ borderBottom: "none", gap: 18, flexWrap: "wrap" }}>
          <h3>Files by status</h3>
          <InfoTip text="Each file as it first arrived, before anyone fixed it. Not checked: the file could not be read or failed a safety check." />
          {([["Clean", r.quality.clean, "b-ok", "clean"], ["With errors", r.quality.with_errors, "b-crit", "errors"],
             ["Not checked", r.quality.not_checked, "b-mut", null]] as const).map(([k, v, tone, measure]) => (
            <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className={`badge ${tone}`} style={{ padding: "2px 8px" }}>{k}</span>
              <b style={{ fontSize: 16 }}>{monthly && measure && v
                ? <Link className="linkish" to={drillLink({ from: "monthly", label: `Files ${k.toLowerCase()}`, month: r.period, measure })}>{v}</Link>
                : v}</b>
            </span>))}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 18, marginBottom: 18 }}>
        {([["Top programmes by premium", r.money.top_programmes, "program"],
           ["Top brokers by premium", r.money.top_brokers, "broker"]] as const).map(([title, rows, key]) => (
          <div className="card" key={title}>
            <div className="card-h"><h3>{title}</h3>
              {short && r.money.month && <span className="sub">{fmtMonth(r.money.month)} so far</span>}
              {daily && <span className="sub">processed today</span>}</div>
            {rows.length === 0 ? <div className="empty">No premium reported.</div> : (
              <div className="tbl-wrap"><table>
                <tbody>{rows.map(x => (
                  <tr key={x.id}>
                    <td className="l">{x.name}</td>
                    <td>{r.money.month ? <Link className="linkish" to={drillLink(
                      { from: "financial", label: `Gross premium · ${x.name}`, basis: "month",
                        period: r.money.month, metric: "gross_premium" }, { [key]: x.id })}>
                      {money(x.gross_premium, cur)}</Link> : money(x.gross_premium, cur)}</td>
                  </tr>))}</tbody>
              </table></div>
            )}
          </div>
        ))}
        <div className="card">
          <div className="card-h">
            <h3>Programmes</h3>
            <InfoTip text={daily
              ? "Today's changes: programmes added today, whose contract went live today, and whose term ended yesterday so they went into run-off today."
              : "Programmes that existed on that day, each term judged against it. A contract's past status is not kept, so each programme uses its contract's status today."} />
            <Link className="linkish right" to="/programs" style={{ fontSize: 13, whiteSpace: "nowrap" }}>Open Programmes</Link>
          </div>
          {/* Three counts side by side, split by thin lines — no boxes inside the card. */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", padding: "18px 0 6px" }}>
            {(daily
              ? [["New", r.portfolio.new ?? 0], ["Went live", r.portfolio.went_live ?? 0], ["Into run-off", r.portfolio.into_runoff ?? 0]] as const
              : [["Active", r.portfolio.active], ["Run-off", r.portfolio.runoff], ["Not live yet", r.portfolio.not_live]] as const).map(([k, v], i) => (
              <div key={k} style={{ padding: "0 16px", borderLeft: i ? "1px solid var(--p-border)" : undefined }}>
                <div className="sub" style={{ whiteSpace: "nowrap", fontSize: 12.5 }}>{k}</div>
                <div style={{ fontSize: 26, fontWeight: 700, lineHeight: 1.3, color: "var(--p-ink)" }}>{v}</div>
              </div>
            ))}
          </div>
          <div className="sub" style={{ padding: "8px 16px 16px" }}>
            {plural(r.portfolio.total, "programme")} in all{daily ? "" : ` · ${asAt}`}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 18 }}>
      <div className="card" style={{ flex: "2 1 480px", minWidth: 0 }}>
        <div className="card-h">
          <h3>{TREND_TITLE[r.trend[0]?.unit ?? "day"]}</h3>
          <InfoTip text="Errors found and files clean first time, inside the dates the report covers: hour by hour for one day, day by day up to 31 days, week by week up to 26 weeks, month by month beyond that. Times are UTC." />
        </div>
        <div style={{ height: 260, padding: "8px 16px 16px" }}>
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={r.trend}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 11 }} />
              <YAxis yAxisId="l" allowDecimals={false} tick={{ fontSize: 11 }} />
              <YAxis yAxisId="r" orientation="right" domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
              <Tooltip />
              <Legend />
              <Bar yAxisId="l" dataKey="errors" name="Errors" fill="#e11d48" />
              <Line yAxisId="r" dataKey="clean_first_time_pct" name="Clean first time %" stroke="#077282" connectNulls />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>
        <TopErrors r={r} />
      </div>

      <Scorecard r={r} />
      <ContractsCard r={r} asAt={asAt} />

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h">
          <h3>Needs attention</h3>
          <span className="badge b-crit" style={{ padding: "2px 8px" }}>
            {plural(r.problems.escalations, daily ? "new escalation" : "escalation")}</span>
          <InfoTip text={daily
            ? "What became an escalation today: a late file marked delinquent, a dispute raised, or anything that passed its due date. Each says whether it is still open."
            : "Late files marked delinquent, disputes waiting for your decision, and anything past its due date"
              + ", as they stood at the end of that day. Contracts waiting for a signature count only when the period runs to today, because when one started waiting is not kept."} />
          {!daily && <span className="sub">{asAt}</span>}
          <Link className="linkish right" to="/open-problems" style={{ fontSize: 13 }}>Open Problems</Link>
        </div>
        {r.problems.escalation_list.length === 0 ? <div className="empty">
          {daily ? "Nothing became an escalation today." : "Nothing needs escalating."}</div> : daily ? (
          <div className="tbl-wrap"><table>
            <thead><tr><th className="l">Problem</th><th className="l">Who must act</th><th className="l">Escalated because</th><th>Now</th></tr></thead>
            <tbody>{r.problems.escalation_list.map((x, i) => (
              <tr key={i}>
                <td className="l">{x.type_label}: {x.title}</td>
                <td className="l">{x.owner ?? "—"}</td>
                <td className="l">{x.type === "dispute" ? "Disputed" : x.status === "Delinquent" ? "Delinquent"
                  : `Past its due date (${fmtDay(x.due_on)})`}</td>
                <td>{x.still_open ? <span className="badge b-crit">Still open</span>
                  : <span className="badge b-ok">{x.status}</span>}</td>
              </tr>))}</tbody>
          </table></div>
        ) : (
          <div className="tbl-wrap"><table>
            <thead><tr><th className="l">Problem</th><th className="l">Who must act</th><th>Open for</th><th>Due</th></tr></thead>
            <tbody>{r.problems.escalation_list.map((x, i) => (
              <tr key={i}>
                <td className="l">{x.link ? <Link className="linkish" to={x.link}>{x.type_label}: {x.title}</Link>
                  : <>{x.type_label}: {x.title}</>}</td>
                <td className="l">{x.owner ?? "—"}</td>
                <td>{x.age_days != null ? plural(x.age_days, "day") : "—"}</td>
                <td>{fmtDay(x.due_on)}{x.overdue && <span className="badge b-crit" style={{ marginLeft: 6 }}>Overdue</span>}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h">
          <h3>Reinsurance statements due</h3>
          <span className="sub">{[`${r.reinsurance.due} due`,
            r.reinsurance.sent_late ? `${r.reinsurance.sent_late} sent late` : "",
            r.reinsurance.not_sent ? `${r.reinsurance.not_sent} not sent` : ""].filter(Boolean).join(" · ")}</span>
          <InfoTip text="Statements whose due date falls in the period, and whether each was sent on time, as at the end of its last day." />
          <Link className="linkish right" to="/reinsurance/bordereaux" style={{ fontSize: 13 }}
            title="Opens Reinsurance › Process Bordereau">Prepare and send statements →</Link>
        </div>
        {r.reinsurance.statements.length === 0 ? <div className="empty">
          No statements were due.</div> : (
          <div className="tbl-wrap"><table>
            <thead><tr><th className="l">Treaty</th><th>Period</th><th>Due</th><th>Status</th></tr></thead>
            <tbody>{r.reinsurance.statements.slice(0, 10).map((x, i) => (
              <tr key={i}>
                <td className="l"><Link className="linkish" to={`/contracts/${x.contract_id}`}>{x.treaty}</Link></td>
                <td>{x.period}</td>
                <td>{fmtDay(x.due_on)}{x.overdue && <span className="badge b-crit" style={{ marginLeft: 6 }}>Overdue</span>}</td>
                <td>
                  <span className={`badge ${x.status === "Sent on time" ? "b-ok" : x.status === "Sent late" ? "b-warn" : "b-crit"}`}>{x.status}</span>
                  {x.sent_on && <span className="sub"> {fmtDay(x.sent_on)}</span>}
                </td>
              </tr>))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}

/** The checks that failed most in the period. */
function TopErrors({ r }: { r: Report }) {
  return (
    <div className="card" style={{ flex: "1 1 300px", minWidth: 0 }}>
      <div className="card-h"><h3>Most common errors</h3>
        <InfoTip text="The checks that failed most often in this period, counted on each file as it first arrived." /></div>
      {r.quality.top_errors.length === 0 ? <div className="empty">No errors found.</div> : (
        <div className="tbl-wrap"><table>
          <thead><tr><th className="l">Check</th><th>Files</th><th>Errors</th></tr></thead>
          <tbody>{r.quality.top_errors.map(x => (
            <tr key={x.check}>
              <td className="l">{x.check}
                {x.severity === "critical" && <span className="badge b-crit" style={{ marginLeft: 6 }}>Critical</span>}</td>
              <td>{x.files}</td><td>{x.errors}</td>
            </tr>))}</tbody>
        </table></div>
      )}
    </div>
  );
}

const shortDay = (d: string) => new Date(d + "T00:00:00").toLocaleDateString("en-GB",
  { day: "numeric", month: "short", year: "numeric" });
const fmtPeriod = (p: string) => (/^\d{4}-\d{2}$/.test(p) ? fmtMonth(p) : p);

/** A share and how it moved since the period before: red when worse, green when better. */
function Share({ now, then }: { now: number | null; then: number | null }) {
  if (now == null) return <>—</>;
  const d = then == null ? 0 : now - then;
  return (
    <span style={{ whiteSpace: "nowrap" }}>{now}%
      {Math.abs(d) >= 0.05 && (
        <span title={`${then}% the period before`} style={{ marginLeft: 6, fontSize: 12, fontWeight: 600,
          color: d < 0 ? "var(--p-crit, #e11d48)" : "var(--p-ok, #16a34a)" }}>
          {d < 0 ? "▼" : "▲"} {Math.abs(Math.round(d * 10) / 10)} pts</span>)}
    </span>
  );
}

/** Each broker (or programme) in one row: premium, files, on time, clean first time, late or missing.
 *  The late-or-missing number opens that row's files. */
function Scorecard({ r }: { r: Report }) {
  const [kind, setKind] = useState<"broker" | "programme">("broker");
  const [open, setOpen] = useState<number | null>(null);
  const part = r.scorecard[kind];
  const cur = r.money.currency;
  const daily = r.cadence === "day";
  const lateOf = (x: ScoreRow) => r.late.list.filter(l => (kind === "broker" ? l.broker_party_id : l.program_id) === x.id);
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        <h3>Scorecard</h3>
        <InfoTip text={"Each " + kind + " in one line: premium, files processed, files on time, files clean first time, and files late or still missing. "
          + "Most late or missing first. ▲ / ▼ show the change since the period before, in percentage points. Click a late or missing number to see which files."} />
        <span className="seg" style={{ marginLeft: "auto" }}>
          {(["broker", "programme"] as const).map(k => (
            <button key={k} type="button" className={kind === k ? "on" : ""} onClick={() => { setKind(k); setOpen(null); }}>
              {k === "broker" ? "By broker" : "By programme"}</button>))}
        </span>
      </div>
      {part.rows.length === 0 ? <div className="empty">No {kind} had files, premium or files due.</div> : (
        <div className="tbl-wrap"><table style={FIXED}>
          <colgroup><col style={{ width: "28%" }} /><col style={{ width: "16%" }} /><col style={{ width: "10%" }} />
            <col style={{ width: "15%" }} /><col style={{ width: "17%" }} /><col style={{ width: "14%" }} /></colgroup>
          <thead><tr><th className="l">{kind === "broker" ? "Broker" : "Programme"}</th>
            <th>Premium{!daily && r.money.month && r.cadence !== "month" && <InfoTip text={`Premium is reported by bordereau month, so this is ${fmtMonth(r.money.month)} so far.`} />}</th>
            <th>Files</th><th>On time</th><th>Clean first time</th><th>Late or missing</th></tr></thead>
          <tbody>{part.rows.map(x => {
            const late = lateOf(x);
            return (<Fragment key={x.id}>
              <tr>
                <td className="l" style={WRAP}>{x.name}</td>
                <td style={WRAP}>{x.premium == null ? "—" : money(x.premium, cur)}</td>
                <td>{x.files}</td>
                <td>{x.due ? <Share now={x.on_time_pct} then={x.prev_on_time_pct} /> : <span className="sub">None due</span>}</td>
                <td>{x.files ? <Share now={x.clean_first_time_pct} then={x.prev_clean_first_time_pct} /> : "—"}</td>
                <td>{x.late_or_missing && late.length
                  ? <button type="button" className="linkish" style={{ background: "none", border: 0, padding: 0, cursor: "pointer", fontWeight: 700, color: "var(--p-crit, #e11d48)" }}
                      onClick={() => setOpen(open === x.id ? null : x.id)} title="See which files">{x.late_or_missing} {open === x.id ? "▴" : "▾"}</button>
                  : x.late_or_missing || <span className="sub">0</span>}</td>
              </tr>
              {open === x.id && (
                <tr><td colSpan={6} style={{ background: "var(--p-surface-2)", padding: "8px 16px" }}>
                  <table style={{ ...FIXED, fontSize: 12.5 }}><tbody>{late.map((l, i) => (
                    <tr key={i}>
                      <td className="l" style={WRAP}>{kind === "broker" ? l.programme : l.broker}</td>
                      <td>{fmtPeriod(l.month)}</td>
                      <td>Due {shortDay(l.due_on)}</td>
                      <td>{l.arrived_on ? `Arrived ${shortDay(l.arrived_on)}` : <span className="badge b-crit">{l.status}</span>}</td>
                      <td>{l.days_late} day{l.days_late === 1 ? "" : "s"} late</td>
                    </tr>))}</tbody></table>
                </td></tr>)}
            </Fragment>);
          })}</tbody>
        </table></div>
      )}
      <div className="sub" style={{ padding: "8px 16px 12px", display: "flex", gap: 12 }}>
        {part.total > part.rows.length && <span>Showing {part.rows.length} of {part.total}.</span>}
        <Link className="linkish" style={{ marginLeft: "auto" }}
          to={r.timeliness.month ? `/bordereau-calendar?month=${r.timeliness.month}` : "/bordereau-calendar"}>All submissions →</Link>
      </div>
    </div>
  );
}

// Column widths set by the header, not by the longest text: long names wrap inside their column.
const FIXED = { tableLayout: "fixed" } as const;
const WRAP = { overflowWrap: "anywhere" } as const;

/** Contract utilization: which bound contracts sent files, and how much of each premium cap is used. */
function ContractsCard({ r, asAt }: { r: Report; asAt: string }) {
  const c = r.contracts;
  const daily = r.cadence === "day";
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        <h3>Contract utilization</h3>
        {!daily && <span className="sub">{asAt}</span>}
        <Link className="linkish right" to="/contracts" style={{ fontSize: 13 }}>Open Contracts</Link>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
        <div style={{ borderRight: "1px solid var(--p-border)" }}>
          <div className="card-h" style={{ borderBottom: "none" }}>
            <h4 style={{ margin: 0, fontSize: 14 }}>Sending files</h4>
            <InfoTip text={`Bound contracts that sent at least one file ${daily ? "today" : "in this period"}, and the ones that sent none.`} />
            <span className="sub">{c.with_files} of {c.bound} contract{c.bound === 1 ? "" : "s"}</span>
          </div>
          {c.idle.length === 0 ? <div className="empty">{c.bound ? "Every bound contract sent files." : "No bound contracts."}</div> : (
            <div className="tbl-wrap"><table style={FIXED}>
              <colgroup><col style={{ width: "60%" }} /><col style={{ width: "40%" }} /></colgroup>
              <thead><tr><th className="l">No files {daily ? "today" : "in this period"}</th><th className="l">Broker</th></tr></thead>
              <tbody>{c.idle.map(x => (
                <tr key={x.contract_id}>
                  <td className="l" style={WRAP}><Link className="linkish" to={`/contracts/${x.contract_id}`}>{x.contract}</Link></td>
                  <td className="l" style={WRAP}>{x.broker}</td>
                </tr>))}</tbody>
            </table></div>
          )}
          {c.without_files > c.idle.length && <div className="sub" style={{ padding: "8px 16px 12px" }}>
            Showing {c.idle.length} of {c.without_files}.</div>}
        </div>
        <div>
          <div className="card-h" style={{ borderBottom: "none" }}>
            <h4 style={{ margin: 0, fontSize: 14 }}>Premium cap used</h4>
            <InfoTip text={"Premium written across each contract's whole term so far, against the most premium the contract allows. Near cap from 85%."
              + (daily ? " Only contracts that sent files today." : "")} />
            {(c.over_cap > 0 || c.near_cap > 0) && <span className="sub">
              {[c.over_cap ? `${c.over_cap} over cap` : "", c.near_cap ? `${c.near_cap} near cap` : ""].filter(Boolean).join(" · ")}</span>}
          </div>
          {c.caps.length === 0 ? <div className="empty">
            {daily ? "No contract that sent files today has a premium cap set." : "No contract has a premium cap set."}</div> : (
            <div className="tbl-wrap"><table style={FIXED}>
              <colgroup><col style={{ width: "32%" }} /><col style={{ width: "26%" }} /><col style={{ width: "26%" }} /><col style={{ width: "16%" }} /></colgroup>
              <thead><tr><th className="l">Contract</th><th>Premium cap</th><th>Written so far</th><th>Used</th></tr></thead>
              <tbody>{c.caps.map(x => (
                <tr key={x.contract_id}>
                  <td className="l" style={WRAP}><Link className="linkish" to={`/contracts/${x.contract_id}`}>{x.contract}</Link>
                    <div className="sub">{x.broker}</div></td>
                  <td style={WRAP}>{money(x.cap, x.currency)}</td>
                  <td style={WRAP}>{money(x.written, x.currency)}</td>
                  <td>{x.used_pct == null ? "—" : `${x.used_pct}%`}
                    {x.flag && <div><span className={`badge ${x.flag === "Over cap" ? "b-crit" : "b-warn"}`} style={{ whiteSpace: "nowrap" }}>{x.flag}</span></div>}</td>
                </tr>))}</tbody>
            </table></div>
          )}
          {c.no_cap > 0 && <div className="sub" style={{ padding: "8px 16px 12px" }}>
            {c.no_cap} contract{c.no_cap === 1 ? " has" : "s have"} no premium cap set.</div>}
        </div>
      </div>
    </div>
  );
}

/** Every report sent, newest first, in a right-hand drawer: when, which report, to whom,
 *  whether it was delivered, and the file exactly as it was sent. */
function SentDrawer({ open, delivery, onClose, onCleared }: {
  open: boolean; delivery: ExecutiveDelivery; onClose: () => void; onCleared: (d: ExecutiveDelivery) => void;
}) {
  const [asking, setAsking] = useState(false);
  const [clearing, setClearing] = useState(false);
  useEffect(() => {
    if (!open) { setAsking(false); return; }
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  const sends = delivery.sends;
  const clearAll = () => {
    setClearing(true);
    clearExecutiveSends()
      .then(d => { onCleared(d); setAsking(false); })
      .catch(e => showError(e))
      .finally(() => setClearing(false));
  };
  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h" style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <div style={{ flex: 1 }}>
            <h4>Sent reports</h4>
            <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>
              {sends.length ? `${plural(sends.length, "report")} sent, newest first. Download any one exactly as it was sent.`
                : "Nothing to show."}</div>
          </div>
          {sends.length > 0 && !asking &&
            <button type="button" className="btn sm ghost" onClick={() => setAsking(true)}>Clear all</button>}
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b">
          {asking && sends.length > 0 &&
            <div style={{ border: "1px solid var(--p-border)", borderRadius: 10, padding: "12px 14px",
                          marginBottom: 12 }}>
              <b style={{ fontSize: 14 }}>Clear the list?</b>
              <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                <button type="button" className="btn sm danger" disabled={clearing} onClick={clearAll}>
                  {clearing ? "Clearing…" : "Clear all"}</button>
                <button type="button" className="btn sm" disabled={clearing} onClick={() => setAsking(false)}>Cancel</button>
              </div>
            </div>}
          {sends.length === 0 ? <div className="empty">Reports you send, or that go on the schedule, appear here.</div>
            : <div style={{ display: "grid", gap: 12 }}>{sends.map(s => (
              <div key={s.id} style={{ border: "1px solid var(--p-border)", borderRadius: 10, padding: "12px 14px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <b style={{ fontSize: 14 }}>{s.title}</b>
                  {s.failed === 0 ? <span className="badge b-ok">Delivered to all {s.delivered}</span>
                    : <span className="badge b-crit">{s.delivered} of {s.delivered + s.failed} delivered</span>}
                </div>
                <div className="sub" style={{ marginTop: 4 }}>
                  {s.sent_at ? new Date(s.sent_at).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "—"}
                  {" · "}{s.kind === "scheduled" ? "Sent automatically" : `Sent by ${s.sent_by ?? "—"}`}
                </div>
                <div style={{ marginTop: 8, fontSize: 13, overflowWrap: "anywhere" }}>
                  <span className="sub">To </span>
                  {s.recipients.map((x, i) => (
                    <span key={x.email}>{i ? ", " : ""}
                      <span style={x.delivered ? undefined : { color: "var(--p-crit)" }}
                        title={x.delivered ? x.email : `${x.email} — not delivered`}>{x.name ?? x.email}</span></span>))}
                </div>
                {s.has_file && <div style={{ marginTop: 8, display: "flex", gap: 14, fontSize: 13 }}>
                  <span className="linkish" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    onClick={() => downloadSentExecutive(s, "pdf").catch(e => showError(e))}>
                    <FileText size={14} /> PDF</span>
                  <span className="linkish" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    onClick={() => downloadSentExecutive(s, "excel").catch(e => showError(e))}>
                    <FileSpreadsheet size={14} /> Excel</span>
                </div>}
              </div>))}</div>}
        </div>
      </aside>
    </>
  );
}

/** Logins to tick, and other addresses one per line — shared by both popups. */
function Recipients({ logins, ticked, setTicked, emails, setEmails }: {
  logins: Login[]; ticked: number[]; setTicked: (v: number[]) => void;
  emails: string; setEmails: (v: string) => void;
}) {
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div>
        <div className="sub" style={{ marginBottom: 6 }}>Your company&rsquo;s logins</div>
        {logins.length === 0 ? <div className="sub">No logins with an email.</div> : logins.map(p => (
          <label key={p.user_id} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
            <input type="checkbox" checked={ticked.includes(p.user_id)}
              onChange={e => setTicked(e.target.checked ? [...ticked, p.user_id] : ticked.filter(x => x !== p.user_id))} />
            {who(p)}
          </label>
        ))}
      </div>
      <label style={{ display: "grid", gap: 6 }}>
        <span className="sub">Other email addresses <InfoTip text="One per line. Anyone you add gets the report by email — they do not need a login." /></span>
        <textarea rows={4} value={emails} onChange={e => setEmails(e.target.value)}
          placeholder={"ceo@company.com\ncfo@company.com"}
          style={{ width: "100%", border: "1px solid var(--p-border-2)", borderRadius: 8, padding: 8, fontSize: 14 }} />
      </label>
    </div>
  );
}

const lines = (t: string) => t.split(/[\s,;]+/).map(x => x.trim()).filter(Boolean);

function FormatPick({ value, onChange }: { value: FileFormat; onChange: (f: FileFormat) => void }) {
  return (
    <div>
      <div className="sub" style={{ marginBottom: 6 }}>Format</div>
      <span className="seg">{FORMATS.map(f => (
        <button key={f.key} type="button" className={value === f.key ? "on" : ""} onClick={() => onChange(f.key)}>{f.label}</button>
      ))}</span>
    </div>
  );
}

function SendModal({ open, onClose, report, logins, settings, onSent }: {
  open: boolean; onClose: () => void; report: Report; logins: Login[];
  settings: ExecutiveSettings; onSent: () => void;
}) {
  const [ticked, setTicked] = useState<number[]>([]);
  const [emails, setEmails] = useState("");
  const [format, setFormat] = useState<FileFormat>("pdf");
  const [sending, setSending] = useState(false);
  useEffect(() => {
    if (!open) return;
    setTicked(settings.user_ids ?? logins.map(p => p.user_id));
    setEmails((settings.emails ?? []).join("\n"));
    setFormat(settings.file_format);
  }, [open, settings, logins]);
  const count = ticked.length + lines(emails).length;

  async function send() {
    setSending(true);
    try {
      const r = await sendExecutiveReport({ cadence: report.cadence, period: report.period,
        user_ids: ticked, emails: lines(emails), file_format: format });
      const total = r.delivered + r.failed;
      if (r.failed === 0) showDone(`Sent to ${plural(r.delivered, "person", "people")}.`, "Report sent");
      else showError(new Error(`Sent to ${r.delivered} of ${total}. Not delivered: `
        + r.recipients.filter(x => !x.delivered).map(x => x.email).join(", ")), "Not everyone got the report");
      onSent();
    } catch (e) {
      showError(e, "Could not send the report");
    } finally {
      setSending(false);
    }
  }

  return (
    <Modal open={open} size="md" title="Send the Executive Report" onClose={() => { if (!sending) onClose(); }}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={sending}>Cancel</button>
        <button className="btn pri" onClick={send} disabled={sending || count === 0}>{sending ? "Sending…" : "Send"}</button>
      </div>}>
      <div style={{ display: "grid", gap: 16, fontSize: 14 }}>
        <div><b>{report.title}</b>{!report.complete && <span className="sub"> · still running, counts to today</span>}</div>
        <div><div className="sub" style={{ marginBottom: 6 }}>Send to</div>
          <Recipients logins={logins} ticked={ticked} setTicked={setTicked} emails={emails} setEmails={setEmails} /></div>
        <FormatPick value={format} onChange={setFormat} />
      </div>
    </Modal>
  );
}

function ScheduleModal({ open, onClose, delivery, onSaved }: {
  open: boolean; onClose: () => void; delivery: ExecutiveDelivery; onSaved: (d: ExecutiveDelivery) => void;
}) {
  const s = delivery.settings;
  const [on, setOn] = useState(false);
  const [cadence, setCadence] = useState<ScheduleCadence>("month");
  const [weekDay, setWeekDay] = useState(0);
  const [sendDay, setSendDay] = useState(3);
  const [format, setFormat] = useState<FileFormat>("pdf");
  const [ticked, setTicked] = useState<number[]>([]);
  const [emails, setEmails] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!open) return;
    setOn(s.auto_send); setCadence(s.cadence); setWeekDay(s.week_day); setSendDay(s.send_day);
    setFormat(s.file_format); setTicked(s.user_ids ?? delivery.logins.map(p => p.user_id));
    setEmails((s.emails ?? []).join("\n"));
  }, [open, s, delivery.logins]);

  async function save() {
    setSaving(true);
    try {
      const d = await saveExecutiveDelivery({ auto_send: on, cadence, week_day: weekDay, send_day: sendDay,
        file_format: format, user_ids: ticked, emails: lines(emails) });
      showDone(d.settings.auto_send && d.next
        ? `It will be sent automatically. Next: ${fmtDay(d.next.send_on)} (${d.next.title}).`
        : "It will not be sent automatically.", "Schedule saved");
      onSaved(d);
    } catch (e) {
      showError(e, "Could not save the schedule");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal open={open} size="md" title="Send it automatically" onClose={() => { if (!saving) onClose(); }}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={saving}>Cancel</button>
        <button className="btn pri" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
      </div>}>
      <div style={{ display: "grid", gap: 16, fontSize: 14 }}>
        <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input type="checkbox" checked={on} onChange={e => setOn(e.target.checked)} />
          <b>Send it automatically</b>
        </label>
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div>
            <div className="sub" style={{ marginBottom: 6 }}>How often</div>
            <span className="seg">
              {CADENCES.filter(c => c.key !== "custom").map(c => (
                <button key={c.key} type="button" className={cadence === c.key ? "on" : ""}
                  onClick={() => setCadence(c.key as ScheduleCadence)}>{c.label}</button>
              ))}
            </span>
          </div>
          {cadence === "day" ? null : cadence === "week" ? (
            <label style={{ display: "grid", gap: 6 }}><span className="sub">Every</span>
              <select className="fbar-select" value={weekDay} onChange={e => setWeekDay(Number(e.target.value))}>
                {delivery.week_days.map(d => <option key={d} value={d}>{DAYS[d]}</option>)}
              </select></label>
          ) : (
            <label style={{ display: "grid", gap: 6 }}><span className="sub">On working day</span>
              <select className="fbar-select" value={sendDay} onChange={e => setSendDay(Number(e.target.value))}>
                {delivery.send_days.map(d => <option key={d} value={d}>{ordinal(d)}</option>)}
              </select></label>
          )}
        </div>
        <div className="sub">{cadence === "day" ? "Yesterday's report, every day."
          : cadence === "week"
          ? `Last week's report, every ${DAYS[weekDay]}.`
          : `Last month's report, on the ${ordinal(sendDay)} working day of each month.`}</div>
        <div><div className="sub" style={{ marginBottom: 6 }}>Send to</div>
          <Recipients logins={delivery.logins} ticked={ticked} setTicked={setTicked} emails={emails} setEmails={setEmails} /></div>
        <FormatPick value={format} onChange={setFormat} />
      </div>
    </Modal>
  );
}
