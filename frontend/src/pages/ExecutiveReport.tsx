// Executive Reporting 38.2 — the Executive Report: the carrier's whole book for
// one day, week or month, or any dates picked (backend executive_report.py). Programmes,
// Needs attention and Reinsurance follow the period too, as at its last day.
// To read here, download
// as a PDF or Excel file, send now to anyone by email, or send by itself on a
// schedule. Every number is the count its own screen makes, and the money and
// volume numbers open the policies behind them (38.1).
import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  AlertCircle, AlertTriangle, ArrowRight, BarChart3, CalendarClock, CheckCircle2, Clock, Database, FileSignature,
  FileSpreadsheet, FileText, History, Landmark, Layers, Percent, RefreshCw, Search, Send, TrendingUp, Trophy, Users,
} from "lucide-react";
import {
  clearExecutiveSends, downloadExecutiveReport, downloadSentExecutive, getExecutiveDelivery, getExecutiveReport,
  getMoneyTrend, type MoneyTrend,
  saveExecutiveDelivery, sendExecutiveReport,
  type Cadence, type ExecutiveDelivery, type ExecutiveReport as Report, type ExecutiveSettings,
  type FileFormat, type Login, type ReportWindow, type ScheduleCadence, type ScoreRow, type TrendPoint, type TrendUnit,
} from "../api/executive";
import { drillLink } from "../api/policies";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { showDone, showError } from "../components/ResultPopup";
import { FormatPick, lines, Recipients, SendByEmailModal } from "../components/SendByEmail";
import { Modal } from "../components/ui/Modal";
import { fmtDay, fmtMonth, lastMonths, ordinal, plural } from "../utils/reportFormat";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
export const CADENCES: { key: Cadence; label: string }[] = [
  { key: "day", label: "Daily" }, { key: "week", label: "Weekly" },
  { key: "month", label: "Monthly" }, { key: "custom", label: "Custom" }];
const FORMATS: { key: FileFormat; label: string }[] = [
  { key: "pdf", label: "PDF" }, { key: "excel", label: "Excel" }, { key: "both", label: "Both" }];

/** The last twelve complete weeks' Mondays, newest first, as YYYY-MM-DD. */
export function lastWeeks(n = 12): string[] {
  const now = new Date();
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() - 7 * (i + 1));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  });
}

/** "2 – 8 Nov 2026". */
export function weekLabel(monday: string): string {
  const [y, m, d] = monday.split("-").map(Number);
  const a = new Date(y, m - 1, d), b = new Date(y, m - 1, d + 6);
  return `${a.getDate()}${a.getMonth() !== b.getMonth() ? " " + a.toLocaleDateString("en-GB", { month: "short" }) : ""}`
    + ` – ${b.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}`;
}

export function money(v: number | null | undefined, currency: string | null): string {
  if (v == null) return "—";
  const t = v.toLocaleString("en-GB", { maximumFractionDigits: 0 });
  return currency && currency !== "none" && currency !== "review" ? `${currency} ${t}` : t;
}

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
export const dayBefore = (n: number) => { const d = new Date(); d.setDate(d.getDate() - n); return iso(d); };

/** "1 – 31 Oct 2026" from two YYYY-MM-DD days; one day reads "11 Nov 2026". */
export function span(from: string, to: string): string {
  if (from === to) return fmtDay(from);
  const [ya, ma, da] = from.split("-").map(Number), [yb, mb, db] = to.split("-").map(Number);
  const a = new Date(ya, ma - 1, da), b = new Date(yb, mb - 1, db);
  const tail = b.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  if (ya !== yb) return `${a.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })} – ${tail}`;
  return `${a.getDate()}${ma !== mb ? " " + a.toLocaleDateString("en-GB", { month: "short" }) : ""} – ${tail}`;
}

/** "vs last week" — what a report is compared with. */
/** Colours of the charts, as the app's own (proto.css) — SVG takes them as values. */
export const C = { crit: "#e11d48", warn: "#f59e0b", info: "#3b82f6", brand: "#077282", violet: "#8b5cf6", mut: "#94a3b8" };
export const STAGE_COLOUR: Record<string, string> = {
  Active: C.brand, "Under Review": C.warn, Runoff: C.violet, "In Setup": C.info, Onboarding: C.mut };

/** A problem tile turns orange (open problems) or red (escalations) while it is above nought. */
function alert(label: string, value: string): string | null {
  const n = Number(value.replace(/[^0-9.]/g, ""));
  if (!n) return null;
  if (label === "Escalations" || label === "New escalations") return C.crit;
  if (label === "Open problems" || label === "New problems") return C.warn;
  return null;
}

export const HISTORY_TITLE: Record<string, string> = {
  day: "Error history — last 14 days", week: "Error history — last 12 weeks", month: "Error history — last 12 months" };

export const TREND_TITLE: Record<TrendUnit, string> = {
  hour: "Errors by hour", day: "Errors by day", week: "Errors by week", month: "Errors by month" };
const VS: Record<Cadence, string> = { day: "the day before", week: "last week", month: "last month", custom: "the period before" };

/** "USD 125,000 (+25.0% vs last month)" → ["USD 125,000", "+25.0% vs last month"]. */
export function split(v: string): [string, string | null] {
  const m = /^(.*?) \((.*)\)$/.exec(v);
  return m ? [m[1], m[2]] : [v, null];
}

/** The report picked: Daily / Weekly / Monthly / Custom and its day, week,
 *  month or dates — the Executive Report's and Programme reports'. */
export function usePeriod(start?: { cadence?: string | null; period?: string | null }) {
  const months = useMemo(() => lastMonths(), []);
  const weeks = useMemo(() => lastWeeks(), []);
  const first = (CADENCES.some(c => c.key === start?.cadence) ? start!.cadence : "month") as Cadence;
  const given = start?.period || null;
  const [cadence, setCadence] = useState<Cadence>(first);
  // Custom: the from and to dates typed; the report asks only once both make sense.
  const [from, setFrom] = useState(first === "custom" && given ? given.split("..")[0] : dayBefore(30));
  const [to, setTo] = useState(first === "custom" && given ? given.split("..")[1] ?? dayBefore(1) : dayBefore(1));
  const fallback = (c: Cadence) =>
    c === "month" ? months[1] : c === "week" ? weeks[0] : c === "day" ? dayBefore(0) : `${from}..${to}`;
  const [period, setPeriod] = useState<string>(given ?? fallback(first));
  const customOk = !!from && !!to && from <= to;
  useEffect(() => {
    if (cadence === "custom" && customOk) setPeriod(`${from}..${to}`);
  }, [cadence, from, to, customOk]);
  const pick = (c: Cadence) => { setCadence(c); setPeriod(fallback(c)); };
  // A month or week from a link may not be in the picker's list: show it too.
  const monthList = months.includes(period) || cadence !== "month" ? months : [period, ...months];
  const weekList = weeks.includes(period) || cadence !== "week" ? weeks : [period, ...weeks];
  return { cadence, period, setPeriod, from, setFrom, to, setTo, customOk, pick, months: monthList, weeks: weekList };
}
export type Period = ReturnType<typeof usePeriod>;

/** "?cadence=month&period=2026-10" — a report's period in a link. */
export const periodQuery = (p: { cadence: Cadence; period: string }) =>
  `cadence=${p.cadence}&period=${encodeURIComponent(p.period)}`;

/** The period picker, as one toolbar: what the report covers on the left;
 *  Reporting period, Daily / Weekly / Monthly / Custom and refresh on the right. */
export function PeriodBar({ p, r, onRefresh }: { p: Period; r: ReportWindow | null; onRefresh?: () => void }) {
  const { cadence, period, setPeriod, from, setFrom, to, setTo, pick } = p;
  return (
    <div className="card" style={{ overflow: "visible", marginBottom: 16 }}>
      <div className="card-h" style={{ gap: 12, flexWrap: "wrap", borderBottom: "none", alignItems: "center" }}>
        <span className="sub" style={{ flex: "1 1 260px" }}>
          {r && (r.cadence === "day"
            ? <>Today so far, {fmtDay(r.from)}, compared with yesterday. Only what happened today.</>
            : <>Covers {span(r.from, r.to)}, compared with {span(r.previous.from, r.previous.to)}.
                {!r.complete && " Still running, so the numbers count to today."}</>)}
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <b style={{ fontSize: 13.5 }}>Reporting period:</b>
          {(cadence === "week" || cadence === "month") && (
            <select className="fbar-select" value={period} onChange={e => setPeriod(e.target.value)}
              aria-label={cadence === "week" ? "Week" : "Month"}>
              {(cadence === "week" ? p.weeks : p.months).map(x => (
                <option key={x} value={x}>{cadence === "week" ? weekLabel(x) : fmtMonth(x)}</option>
              ))}
            </select>
          )}
          {cadence === "day" && r && <span className="sub">{fmtDay(r.from)}</span>}
          {cadence === "custom" && (<>
            <input type="date" className="fbar-date" value={from} max={to || dayBefore(0)} aria-label="From"
              onChange={e => setFrom(e.target.value)} />
            <span className="sub">to</span>
            <input type="date" className="fbar-date" value={to} min={from || undefined} max={dayBefore(0)} aria-label="To"
              onChange={e => setTo(e.target.value)} />
          </>)}
          <span className="seg">
            {CADENCES.map(c => (
              <button key={c.key} type="button" className={cadence === c.key ? "on" : ""}
                onClick={() => pick(c.key)}>{c.label}</button>
            ))}
          </span>
          {onRefresh && (
            <button type="button" className="btn sm" onClick={onRefresh} title="Refresh" aria-label="Refresh">
              <RefreshCw size={15} /></button>)}
        </span>
      </div>
    </div>
  );
}

export default function ExecutiveReport() {
  const [params] = useSearchParams();
  const picked = usePeriod({ cadence: params.get("cadence"), period: params.get("period") });
  const { cadence, period, customOk } = picked;
  const [data, setData] = useState<Report | null>(null);
  /** Why the report could not load — the server's own words when it gives them. */
  const [failed, setFailed] = useState<string | null>(null);
  const [delivery, setDelivery] = useState<ExecutiveDelivery | null>(null);
  const [busy, setBusy] = useState<"pdf" | "excel" | null>(null);
  const [sendOpen, setSendOpen] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [sentOpen, setSentOpen] = useState(false);

  // Refresh asks again for the same period.
  const [asked, setAsked] = useState(0);
  useEffect(() => {
    let live = true;
    setFailed(null);
    getExecutiveReport({ cadence, period })
      .then(r => { if (live) setData(r); })
      .catch(e => { if (live) setFailed(e?.response?.data?.detail ?? "Could not load the report."); });
    return () => { live = false; };
  }, [cadence, period, asked]);

  const reloadDelivery = () => getExecutiveDelivery().then(setDelivery).catch(() => setDelivery(null));
  useEffect(() => { reloadDelivery(); }, []);

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
            <p>Overall performance and key insights at a glance</p>
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

        <PeriodBar p={picked} r={r} onRefresh={() => { setData(null); setAsked(n => n + 1); }} />

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

/** 38.1 — the policies in the files checked in the report's own dates (a month
 *  by its month, anything else by its dates), or in one piece of a chart. */
/** What a link needs of a report: its dates. */
export type Win = Pick<ReportWindow, "cadence" | "period" | "from" | "to">;
export type Scope = { program?: number | null; broker?: number | null };

export function checksLink(r: Win, label: string, measure: "files" | "clean" | "errors" | "critical",
                    scope: { program?: number | null; broker?: number | null } = {},
                    extra: { dates?: string; check?: string | null } = {}): string {
  const when = extra.dates ? { dates: extra.dates }
    : r.cadence === "month" ? { month: r.period } : { dates: `${r.from}..${r.to}` };
  return drillLink({ from: "monthly", label, measure, ...when, ...(extra.check ? { check: extra.check } : {}) }, scope);
}

/** A chart piece's dates as the drill reads them: `to` is not included. */
export function pieceDates(p: TrendPoint): string {
  const a = p.from.slice(0, 10);
  const last = new Date(new Date(p.to).getTime() - 1).toISOString().slice(0, 10);
  return `${a}..${last < a ? a : last}`;
}

/** Where a headline number leads (38.1), or null. */
function headlineLink(r: Report, label: string): string | null {
  const month = r.money.month;
  // A daily report's money is from the files processed that day, not a reporting month.
  if (month != null && label.startsWith("Gross premium"))
    return drillLink({ from: "financial", label: "Gross premium", basis: "month", period: month, metric: "gross_premium" });
  if (month != null && label.startsWith("Commission"))
    return drillLink({ from: "financial", label: "Commission", basis: "month", period: month, metric: "commission_amount" });
  if (month != null && label.startsWith("Earned premium"))
    return drillLink({ from: "financial", label: "Earned premium", basis: "month", period: month, metric: "earned_premium" });
  // Daily premium is from the files processed that day: their policies.
  if (month == null && /^(Gross premium|Commission|Earned premium)/.test(label))
    return checksLink(r, `${label} · files processed that day`, "files");
  if (label === "Files processed") return checksLink(r, "Files handled", "files");
  if (label === "Clean first time") return checksLink(r, "Files clean first time", "clean");
  if (label === "On time") return r.timeliness.month ? `/bordereau-calendar?month=${r.timeliness.month}` : "/bordereau-calendar";
  // Open problems / escalations linked to /open-problems — hidden 17 Nov 2026.
  return null;
}

/** The money tiles — the headline's first row. */
const MONEY = /^(Gross premium|Commission|Earned premium)/;

/** Needs attention shows this many at a time. */
const ATTENTION_PAGE = 5;

/** Each headline tile's icon and colour, as in the rest of the app's charts. */
const TILE: { test: RegExp; icon: typeof FileText; colour: string; soft: string }[] = [
  { test: /^Gross premium/, icon: Database, colour: "#2563eb", soft: "#dbeafe" },
  { test: /^Commission/, icon: Percent, colour: "#059669", soft: "#d1fae5" },
  { test: /^Earned premium/, icon: Trophy, colour: "#7c3aed", soft: "#ede9fe" },
  { test: /^Files processed/, icon: FileText, colour: "#2563eb", soft: "#dbeafe" },
  { test: /^On time/, icon: Clock, colour: "#0d9488", soft: "#ccfbf1" },
  { test: /^Clean first time/, icon: CheckCircle2, colour: "#059669", soft: "#d1fae5" },
  { test: /problems$/, icon: AlertTriangle, colour: "#d97706", soft: "#fef3c7" },
  { test: /scalations$/, icon: AlertCircle, colour: "#e11d48", soft: "#ffe4e6" },
];
const tileOf = (label: string) => TILE.find(t => t.test.test(label)) ?? TILE[3];
const TREND_KEY: Record<string, "gross" | "commission" | "earned"> = {
  "Gross premium": "gross", Commission: "commission", "Earned premium": "earned" };

/** "+8% vs last month · +9% vs last year" → each change in green (up) or red (down), with an arrow. */
function Changes({ text }: { text: string }) {
  return (
    <span style={{ display: "inline-flex", flexWrap: "wrap", gap: "2px 8px" }}>
      {text.split(" · ").map(part => {
        const m = /^([+-−])?(.*)$/.exec(part)!;
        const down = m[1] === "-" || m[1] === "−";
        const up = m[1] === "+";
        return (
          <span key={part} style={{ whiteSpace: "nowrap" }}>
            {(up || down) && <b style={{ color: down ? C.crit : "#059669" }}>{down ? "↓" : "↑"} {m[2].split(" ")[0]}</b>}
            {(up || down) ? <span> {m[2].split(" ").slice(1).join(" ")}</span> : part}
          </span>);
      })}
    </span>);
}

/** A small trend line under a money tile. */
function Spark({ data, k, colour }: { data: MoneyTrend["months"]; k: "gross" | "commission" | "earned"; colour: string }) {
  const pts = data.filter(d => d[k] != null);
  if (pts.length < 2) return null;
  const id = `spark-${k}`;
  return (
    <div style={{ width: 130, height: 46, flex: "none" }} title="Last six months">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={pts} margin={{ top: 4, right: 2, bottom: 2, left: 2 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colour} stopOpacity={0.35} />
              <stop offset="100%" stopColor={colour} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area type="monotone" dataKey={k} stroke={colour} strokeWidth={2} fill={`url(#${id})`} isAnimationActive={false} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>);
}

/** A card's title with its icon, as across the report. */
function Title({ icon: Icon, children }: { icon: typeof FileText; children: ReactNode }) {
  return <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}><Icon size={17} style={{ color: "var(--p-ink)" }} />{children}</h3>;
}

function Body({ r }: { r: Report }) {
  const navigate = useNavigate();
  // Needs attention, a page at a time; a new report starts on page 1.
  const escalations = r.problems.escalation_list;
  const attnPages = Math.max(1, Math.ceil(escalations.length / ATTENTION_PAGE));
  const [attnPage, setAttnPage] = useState(1);
  useEffect(() => { setAttnPage(1); }, [r.cadence, r.period]);
  const escalationsShown = escalations.slice((Math.min(attnPage, attnPages) - 1) * ATTENTION_PAGE,
                                             Math.min(attnPage, attnPages) * ATTENTION_PAGE);
  // The money tiles' trend lines: asked for after the report, never holding it up.
  const [trend, setTrend] = useState<MoneyTrend["months"]>([]);
  useEffect(() => {
    let live = true;
    setTrend([]);
    if (r.cadence !== "day")
      getMoneyTrend({ cadence: r.cadence, period: r.period })
        .then(t => { if (live && t.currency === r.money.currency) setTrend(t.months); }).catch(() => undefined);
    return () => { live = false; };
  }, [r.cadence, r.period, r.money.currency]);
  const cur = r.money.currency;
  const vs = VS[r.cadence];
  const daily = r.cadence === "day";
  const short = r.cadence !== "month" && !daily;
  // Shorter than a month: premium and timeliness are for the month the period ends in.
  const soFar = (label: string) => short
    && (label.startsWith("Gross premium") || label.startsWith("Commission")
        || label.startsWith("Earned premium") || label === "On time")
    ? `${fmtMonth((label === "On time" ? r.timeliness.month : r.money.month) ?? "").split(" ")[0]} so far` : null;
  // "as at 31 Oct 2026" — short, so it fits beside a card's title.
  const asAt = `as at ${new Date(r.as_at + "T00:00:00").toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric" })}`;
  const tiles = r.headline;
  const groups = [tiles.filter(h => MONEY.test(h.label)), tiles.filter(h => !MONEY.test(h.label))];
  const row = { display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 18 } as const;
  const sectionHead = { display: "flex", alignItems: "baseline", gap: 12, margin: "4px 0 8px", fontSize: 11.5, fontWeight: 600,
                        letterSpacing: ".04em", textTransform: "uppercase", color: "var(--p-muted)" } as const;
  return (
    <>
      {/* The money first, in larger tiles with their trend; then files, timeliness and problems. */}
      {groups.map((group, gi) => (
      <div key={gi} style={{ display: "grid", gap: 14, marginBottom: 14,
        gridTemplateColumns: `repeat(auto-fit, minmax(max(${gi ? 180 : 260}px, calc((100% - ${(group.length - 1) * 14}px) / ${group.length})), 1fr))` }}>
        {group.map(h => {
          const [big, small] = split(h.value);
          const to = headlineLink(r, h.label);
          const t = tileOf(h.label);
          const Icon = t.icon;
          const isMoney = MONEY.test(h.label);
          const warn = alert(h.label, big);
          const overdue = /^(\d[\d,]*) overdue$/.exec(small ?? "");
          const sparkKey = TREND_KEY[h.label.replace(/ so far$/, "")];
          return (
            <div className="tile" key={h.label} style={{ padding: isMoney ? "16px 18px" : "14px 16px",
              display: "flex", alignItems: "center", gap: 14, ...(warn ? { borderLeft: `3px solid ${warn}` } : {}) }}>
              <span style={{ width: isMoney ? 52 : 40, height: isMoney ? 52 : 40, borderRadius: "50%", background: t.soft,
                             color: t.colour, display: "grid", placeItems: "center", flex: "none" }}>
                <Icon size={isMoney ? 24 : 19} /></span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="k" style={{ display: "flex", alignItems: "center", fontWeight: 600, color: "var(--p-ink)" }}>
                  {h.label}
                  {short && h.label.startsWith("Gross premium") && (
                    <InfoTip text={`Premium is reported by bordereau month, so this is ${fmtMonth(r.money.month ?? "")} so far.`} />
                  )}
                  {h.label.startsWith("Earned premium") && (
                    <InfoTip text="The part of the premium for cover already given." />
                  )}
                  {h.label === "Escalations" && (
                    <InfoTip text="Late files marked delinquent, disputes waiting for your decision, and anything past its due date." />
                  )}
                </div>
                <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
                  <div className="v" style={{ fontSize: isMoney ? 28 : 24, ...(warn ? { color: warn } : {}) }}>
                    {to ? <Link className="linkish" style={{ color: "inherit" }} to={to} title="See what is behind this number">{big}</Link> : big}</div>
                  {overdue && <b style={{ color: Number(overdue[1].replace(/,/g, "")) ? C.crit : "var(--p-muted)", fontSize: 13 }}>
                    {Number(overdue[1].replace(/,/g, "")) ? "↑ " : ""}{small}</b>}
                </div>
                {!overdue && <div className="sub">{small ? <Changes text={small} /> : `No comparison with ${vs}`}</div>}
                {soFar(h.label) && <div className="sub">{soFar(h.label)}</div>}
              </div>
              {isMoney && sparkKey && <Spark data={trend} k={sparkKey} colour={t.colour} />}
            </div>
          );
        })}
      </div>))}

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ borderBottom: "none", gap: 18, flexWrap: "wrap" }}>
          <Title icon={FileText}>Files by status</Title>
          <InfoTip text="Each file as it first arrived, before anyone fixed it. Not checked: the file could not be read or failed a safety check." />
          {([["Clean", r.quality.clean, "b-ok", "clean"], ["With errors", r.quality.with_errors, "b-crit", "errors"],
             ["Not checked", r.quality.not_checked, "b-mut", null]] as const).map(([k, v, tone, measure]) => (
            <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span className={`badge ${tone}`} style={{ padding: "2px 8px" }}>{k}</span>
              <b style={{ fontSize: 16 }}>{measure && v
                ? <Link className="linkish" to={checksLink(r, `Files ${k.toLowerCase()}`, measure)}>{v}</Link>
                : v}</b>
            </span>))}
        </div>
      </div>

      <PortfolioHealth r={r} asAt={asAt} />

      <div style={row}>
        <Scorecard r={r} style={{ flex: "3 1 560px", minWidth: 0, marginBottom: 0 }} />
        <ContractsCard r={r} asAt={asAt} style={{ flex: "2 1 380px", minWidth: 0, marginBottom: 0 }} />
      </div>

      <div style={row}>
        <div className="card" style={{ flex: "3 1 560px", minWidth: 0, background: "#fff7f7", borderColor: "#fecdd3" }}>
          <div className="card-h">
            <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <AlertCircle size={18} style={{ color: C.crit }} /> Needs attention
              <span style={{ color: C.crit, fontWeight: 600, fontSize: 14 }}>
                ({plural(r.problems.escalations, daily ? "new escalation" : "escalation")})</span>
            </h3>
            <InfoTip text={daily
              ? "New escalations today: late files marked delinquent, disputes raised, or anything past its due date. Each shows whether it is still open."
              : "Late files marked delinquent, disputes waiting for your decision, and anything past its due date, as at the end of that day.\nContracts awaiting signature count only when the period runs to today."} />
            {!daily && <span className="sub">{asAt}</span>}
            {/* Hidden 17 Nov 2026 with the Open Problems page.
            <Link className="linkish right" to="/open-problems" style={{ fontSize: 13 }}>Open Problems</Link> */}
          </div>
          {escalations.length === 0 ? <div className="empty">
            {daily ? "Nothing became an escalation today." : "Nothing needs escalating."}</div> : daily ? (
            <div className="tbl-wrap"><table>
              <thead><tr><th className="l">Description</th><th className="l">Who must act</th><th className="l">Escalated because</th><th>Status</th></tr></thead>
              <tbody>{escalationsShown.map((x, i) => (
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
              <thead><tr><th className="l">Description</th><th className="l">Who must act</th><th>Open for</th><th>Due date</th><th>Status</th></tr></thead>
              <tbody>{escalationsShown.map((x, i) => (
                <tr key={i}>
                  <td className="l">{x.link ? <Link className="linkish" to={x.link}>{x.type_label}: {x.title}</Link>
                    : <>{x.type_label}: {x.title}</>}</td>
                  <td className="l">{x.owner ?? "—"}</td>
                  <td>{x.age_days != null ? plural(x.age_days, "day") : "—"}</td>
                  <td>{shortDay(x.due_on ?? "") || "—"}</td>
                  <td>{x.overdue ? <span className="badge b-crit">Overdue</span>
                    : <span className="badge b-warn">{x.status || "Open"}</span>}</td>
                </tr>))}</tbody>
            </table></div>
          )}
          <Pagination page={attnPage} pageCount={attnPages} pageSize={ATTENTION_PAGE}
            totalItems={escalations.length} onPageChange={setAttnPage} noun="escalations" />
        </div>

        <div className="card" style={{ flex: "1 1 280px", minWidth: 0, display: "flex", flexDirection: "column",
                                       alignItems: "center", justifyContent: "center", textAlign: "center", padding: "24px 20px", gap: 14 }}>
          <span style={{ width: 64, height: 64, borderRadius: 16, background: "#dbeafe", color: "#2563eb",
                         display: "grid", placeItems: "center" }}><Search size={30} /></span>
          <p style={{ margin: 0, fontSize: 14.5, color: "var(--p-ink)", lineHeight: 1.5 }}>
            View detailed programme-wise reports and manage submissions.</p>
          <Link className="btn pri" to={programmesLink(r)} style={{ justifyContent: "center", width: "100%" }}>
            Open Programme Management Report <ArrowRight size={15} /></Link>
        </div>
      </div>

      {/* Everything else the report holds, in the same style. */}
      <div style={sectionHead}>More detail</div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 18, marginBottom: 18 }}>
        {([["Top programmes by premium", r.money.top_programmes, "program", C.info, Layers],
           ["Top brokers by premium", r.money.top_brokers, "broker", C.violet, Users]] as const).map(([title, rows, key, fill, icon]) => (
          <div className="card" key={title} style={{ minWidth: 0 }}>
            <div className="card-h"><Title icon={icon}>{title}</Title>
              <InfoTip text="Click a bar to see the policies behind it." />
              {short && r.money.month && <span className="sub">{fmtMonth(r.money.month)} so far</span>}
              {daily && <span className="sub">processed today</span>}</div>
            {rows.length === 0 ? <div className="empty">No premium reported.</div> : (
              <div style={{ height: Math.max(rows.length * 38 + 20, 120), padding: "8px 16px 8px 8px" }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={[...rows]} layout="vertical" margin={{ left: 8, right: 90 }}>
                    <XAxis type="number" hide />
                    <YAxis type="category" dataKey="name" width={120} tick={{ fontSize: 12 }} />
                    <Tooltip formatter={v => money(Number(v), cur)} />
                    <Bar dataKey="gross_premium" name="Gross premium" fill={fill} radius={[0, 4, 4, 0]} barSize={16}
                      isAnimationActive={false} cursor="pointer"
                      label={{ position: "right", fontSize: 11, formatter: (v: unknown) => money(Number(v), cur) }}
                      onClick={(d: { payload?: { id: number; name: string } }) => d?.payload && navigate(r.money.month
                        ? drillLink({ from: "financial", label: `Gross premium · ${d.payload.name}`, basis: "month",
                                      period: r.money.month, metric: "gross_premium" }, { [key]: d.payload.id })
                        : checksLink(r, `Gross premium · ${d.payload.name} · files processed that day`, "files", { [key]: d.payload.id }))} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        ))}
      </div>

      <div style={row}>
        <div className="card" style={{ flex: "2 1 480px", minWidth: 0 }}>
          <div className="card-h">
            <Title icon={BarChart3}>{TREND_TITLE[r.trend[0]?.unit ?? "day"]}</Title>
            <InfoTip text="Errors found and files clean first time over the report's dates, by hour, day, week or month depending on its length. Times are UTC." />
          </div>
          <ErrorsChart data={r.trend} r={r} />
        </div>
        <TopErrors r={r} />
      </div>

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h">
          <Title icon={Landmark}>Reinsurance statements due</Title>
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

/** 37 — Portfolio health, in one card: programmes by stage, errors by
 *  programme and late or missing by programme; then the error history (37.2)
 *  and escalations by type. */
function PortfolioHealth({ r, asAt }: { r: Report; asAt: string }) {
  const daily = r.cadence === "day";
  const stages = (r.portfolio.by_stage ?? []).filter(x => x.programmes > 0);
  const stageTotal = stages.reduce((n, x) => n + x.programmes, 0);
  const errors = r.errors_by_programme;
  const late = r.scorecard.programme.rows.filter(x => x.late_or_missing > 0)
    .sort((a, b) => b.late_or_missing - a.late_or_missing).slice(0, 5);
  const byType = Object.entries(r.problems.escalation_list.reduce<Record<string, number>>(
    (m, x) => ({ ...m, [x.type_label]: (m[x.type_label] ?? 0) + 1 }), {}))
    .map(([name, n]) => ({ name, n })).sort((a, b) => b.n - a.n);
  const TYPE_COLOUR = ["#2563eb", "#7c3aed", "#0d9488", "#d97706", "#e11d48", C.mut];
  const history = r.history ?? [];
  const navigate = useNavigate();
  const part = { minWidth: 0, padding: "4px 18px 14px" } as const;
  const partHead = { margin: "6px 0 10px", fontSize: 14.5, fontWeight: 700, display: "flex", alignItems: "center", gap: 4 } as const;
  const bars = (rows: number) => Math.max(rows * 38 + 30, 140);
  const friendly = (icon: ReactNode, title: string, sub: string) => (
    <div style={{ display: "grid", placeItems: "center", textAlign: "center", padding: "18px 8px", gap: 6 }}>
      {icon}<b style={{ fontSize: 14 }}>{title}</b><span className="sub">{sub}</span></div>);
  return (
    <>
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h">
          <Title icon={BarChart3}>Portfolio health</Title>
          <Link className="linkish right" to={programmesLink(r)} style={{ fontSize: 13, whiteSpace: "nowrap" }}>
            View details →</Link>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
          <div style={{ ...part, borderRight: "1px solid var(--p-border)" }}>
            <div style={partHead}>Programmes by stage
              <InfoTip text={"Programmes that existed on that day, by the stage of their contract.\nActive and Under Review are writing business; Runoff has ended but still reports."} />
              <Link className="linkish" to="/programs" style={{ marginLeft: "auto", fontSize: 12.5, fontWeight: 500 }}>Open Programmes</Link>
            </div>
            {stageTotal === 0 ? friendly(<Layers size={28} color={C.mut} />, "No programmes yet", "Add a programme to see it here.") : (
              <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                <div style={{ width: 120, height: 120, flex: "none" }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={stages} dataKey="programmes" nameKey="stage" innerRadius={36} outerRadius={58}
                        paddingAngle={1} isAnimationActive={false}>
                        {stages.map(x => <Cell key={x.stage} fill={STAGE_COLOUR[x.stage] ?? C.mut} />)}
                      </Pie>
                      <Tooltip />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
                <div style={{ flex: 1, display: "grid", gap: 8, fontSize: 13 }}>
                  {stages.map(x => (
                    <div key={x.stage} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{ width: 9, height: 9, borderRadius: "50%", background: STAGE_COLOUR[x.stage] ?? C.mut, flex: "none" }} />
                      <span style={{ flex: 1 }}>{x.stage === "Runoff" ? "Run-off" : x.stage}</span>
                      <Link className="linkish" to="/programs" style={{ fontWeight: 700 }}>{x.programmes}</Link>
                      <span className="sub" style={{ width: 44, textAlign: "right" }}>
                        ({Math.round(100 * x.programmes / stageTotal)}%)</span>
                    </div>))}
                </div>
              </div>
            )}
            <div className="sub" style={{ marginTop: 8 }}>
              {plural(r.portfolio.total, "programme")} in all · {asAt}
              {daily && r.portfolio.changes && <> · today {r.portfolio.new ?? 0} new, {r.portfolio.went_live ?? 0} went
                live, {r.portfolio.into_runoff ?? 0} into run-off</>}
            </div>
          </div>

          <div style={{ ...part, borderRight: "1px solid var(--p-border)" }}>
            <div style={partHead}>Errors by programme
              <InfoTip text="Errors found in the period's files, by programme and how serious they are." />
              {errors && errors.total > errors.rows.length &&
                <span className="sub" style={{ marginLeft: "auto", fontWeight: 400 }}>Showing {errors.rows.length} of {errors.total}</span>}
            </div>
            {!errors || errors.rows.length === 0
              ? friendly(<CheckCircle2 size={30} color="#059669" />, "No errors found", "Great! No errors in programmes.") : (
              <div style={{ height: bars(errors.rows.length) }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={errors.rows} layout="vertical" margin={{ left: 0, right: 8 }}>
                    <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
                    <YAxis type="category" dataKey="name" width={100} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Legend iconSize={9} wrapperStyle={{ fontSize: 12 }} />
                    <Bar dataKey="critical" name="Critical" stackId="e" fill={C.crit} isAnimationActive={false}
                      cursor="pointer" onClick={(d: { payload?: { id: number; name: string } }) => d?.payload &&
                        navigate(checksLink(r, `Errors · ${d.payload.name}`, "critical", { program: d.payload.id }))} />
                    <Bar dataKey="warning" name="Warning" stackId="e" fill={C.warn} isAnimationActive={false}
                      cursor="pointer" onClick={(d: { payload?: { id: number; name: string } }) => d?.payload &&
                        navigate(checksLink(r, `Errors · ${d.payload.name}`, "errors", { program: d.payload.id }))} />
                    <Bar dataKey="info" name="Info" stackId="e" fill={C.info} isAnimationActive={false}
                      cursor="pointer" onClick={(d: { payload?: { id: number; name: string } }) => d?.payload &&
                        navigate(checksLink(r, `Errors · ${d.payload.name}`, "errors", { program: d.payload.id }))} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>

          <div style={part}>
            <div style={partHead}>Late or missing by programme
              <InfoTip text="Files due in the period that arrived late or had not arrived by its end, by programme. Click one to see that programme's brokers." />
            </div>
            {late.length === 0 ? friendly(<CheckCircle2 size={30} color="#059669" />, "Nothing late or missing", "Every file due came on time.") : (
              <div style={{ display: "flex", alignItems: "center", gap: 18 }}>
                <span style={{ width: 54, height: 54, borderRadius: "50%", background: "#ffe4e6", color: C.crit,
                               display: "grid", placeItems: "center", flex: "none" }}><Clock size={28} /></span>
                <div style={{ flex: 1, display: "grid", gap: 8, fontSize: 13.5 }}>
                  {late.map(x => (
                    <Link key={x.id} to={programmesLink(r, x.id)} className="linkish"
                      style={{ display: "flex", gap: 10, color: "var(--p-ink)" }}>
                      <span style={{ flex: 1 }}>{x.name}</span>
                      <b style={{ color: C.crit }}>{x.late_or_missing}</b>
                    </Link>))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginBottom: 18 }}>
        <div className="card" style={{ flex: "3 1 520px", minWidth: 0 }}>
          <div className="card-h">
            <Title icon={TrendingUp}>{HISTORY_TITLE[history[0]?.unit ?? "month"]}</Title>
            <InfoTip text="Errors and files clean first time over the periods before this one, so you can see whether quality is getting better or worse." />
          </div>
          {history.length === 0 ? <div className="empty">No history yet.</div>
            : <ErrorsChart data={history} r={r} line />}
        </div>
        <div className="card" style={{ flex: "2 1 320px", minWidth: 0 }}>
          <div className="card-h">
            <Title icon={AlertTriangle}>Escalations by type</Title>
            <InfoTip text={daily ? "Today's new escalations, by what they are." : "Open escalations, by what they are."} />
          </div>
          {byType.length === 0
            ? friendly(<CheckCircle2 size={30} color="#059669" />, daily ? "Nothing became an escalation today" : "Nothing needs escalating", "")
            : (
            <div style={{ display: "flex", alignItems: "center", gap: 18, padding: "14px 18px" }}>
              <div style={{ width: 130, height: 130, flex: "none" }}>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={byType} dataKey="n" nameKey="name" innerRadius={38} outerRadius={62} paddingAngle={1} isAnimationActive={false}>
                      {byType.map((x, i) => <Cell key={x.name} fill={TYPE_COLOUR[i % TYPE_COLOUR.length]} />)}
                    </Pie>
                    <Tooltip />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <div style={{ flex: 1, display: "grid", gap: 8, fontSize: 13 }}>
                {byType.map((x, i) => (
                  <div key={x.name} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ width: 9, height: 9, borderRadius: "50%", background: TYPE_COLOUR[i % TYPE_COLOUR.length], flex: "none" }} />
                    <span style={{ flex: 1 }}>{x.name}</span><b>{x.n}</b>
                  </div>))}
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/** The Programme Management Report for this report's period — one programme's rows when given. */
export function programmesLink(r: { cadence: Cadence; period: string }, programme?: number): string {
  return `/executive-report/programmes?${periodQuery(r)}${programme ? `&programme=${programme}` : ""}`;
}

/** Errors (bars) and files clean first time (line) — the period's chart and its
 *  history. A bar opens the policies its errors named (38.1). */
export function ErrorsChart({ data, r, scope = {}, name, line = false }: {
  data: TrendPoint[]; r: Win; scope?: Scope; name?: string;
  /** Errors as a line (the history) rather than bars; a point still opens its policies. */
  line?: boolean;
}) {
  const navigate = useNavigate();
  const open = (p?: TrendPoint) => {
    if (p && p.errors) navigate(checksLink(r, `Errors · ${name ? name + " · " : ""}${p.label}`, "errors", scope, { dates: pieceDates(p) }));
  };
  return (
    <div style={{ height: 260, padding: "8px 16px 16px" }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="label" tick={{ fontSize: 11 }} />
          <YAxis yAxisId="l" allowDecimals={false} tick={{ fontSize: 11 }} />
          <YAxis yAxisId="r" orientation="right" domain={[0, 100]} unit="%" tick={{ fontSize: 11 }} />
          <Tooltip />
          <Legend />
          {line
            ? <Line yAxisId="l" dataKey="errors" name="Errors" stroke="#2563eb" strokeWidth={2.5} dot={{ r: 3.5 }}
                activeDot={{ r: 6, cursor: "pointer",
                  onClick: (_e: unknown, d: unknown) => open((d as { payload?: TrendPoint })?.payload) }} />
            : <Bar yAxisId="l" dataKey="errors" name="Errors" fill={C.crit} cursor="pointer"
                onClick={(d: { payload?: TrendPoint }) => open(d?.payload)} />}
          <Line yAxisId="r" dataKey="clean_first_time_pct" name="Clean first time %" stroke={C.brand} connectNulls />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/** The checks that failed most in the period. */
export function TopErrors({ r, scope = {}, name }: { r: Win & Pick<Report, "quality">; scope?: Scope; name?: string }) {
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
              <td>{x.files}</td>
              <td>{x.key ? <Link className="linkish" to={checksLink(r, `Errors · ${name ? name + " · " : ""}${x.check}`, "errors", scope, { check: x.key })}
                title="See the policies behind this number">{x.errors}</Link> : x.errors}</td>
            </tr>))}</tbody>
        </table></div>
      )}
    </div>
  );
}

export const shortDay = (d: string) => new Date(d + "T00:00:00").toLocaleDateString("en-GB",
  { day: "numeric", month: "short", year: "numeric" });
export const fmtPeriod = (p: string) => (/^\d{4}-\d{2}$/.test(p) ? fmtMonth(p) : p);

/** A share and how it moved since the period before: red when worse, green when better. */
export function Share({ now, then }: { now: number | null; then: number | null }) {
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
function Scorecard({ r, style }: { r: Report; style?: React.CSSProperties }) {
  // Programmes are on Programme reports; the scorecard here is the brokers'.
  const kind = "broker" as "broker" | "programme";
  const [open, setOpen] = useState<number | null>(null);
  const part = r.scorecard[kind];
  const cur = r.money.currency;
  const daily = r.cadence === "day";
  const scopeOf = (x: ScoreRow) => (kind === "broker" ? { broker: x.id } : { program: x.id });
  const lateOf = (x: ScoreRow) => r.late.list.filter(l => (kind === "broker" ? l.broker_party_id : l.program_id) === x.id);
  return (
    <div className="card" style={{ marginBottom: 18, ...style }}>
      <div className="card-h">
        <Title icon={Users}>Broker scorecard</Title>
        <InfoTip text={"Each " + kind + "'s premium and files: processed, on time, clean first time, late or missing. Most late or missing first.\n▲ / ▼ = change since the period before. Click a late or missing number to see the files."} />
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
                <td style={WRAP}>{x.premium == null ? "—" : <Link className="linkish" to={r.money.month
                  ? drillLink({ from: "financial", label: `Gross premium · ${x.name}`, basis: "month",
                                period: r.money.month, metric: "gross_premium" }, scopeOf(x))
                  : checksLink(r, `Gross premium · ${x.name} · files processed that day`, "files", scopeOf(x))}
                  title="See the policies behind this number">{money(x.premium, cur)}</Link>}</td>
                <td>{x.files ? <Link className="linkish" to={checksLink(r, `Files · ${x.name}`, "files", scopeOf(x))}
                  title="See the policies behind this number">{x.files}</Link> : x.files}</td>
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
          to={r.cadence === "month" ? `/partner-performance?period=${r.period}` : "/partner-performance"}>See all brokers ranked →</Link>
        <Link className="linkish"
          to={r.timeliness.month ? `/bordereau-calendar?month=${r.timeliness.month}` : "/bordereau-calendar"}>All submissions →</Link>
      </div>
    </div>
  );
}

// Column widths set by the header, not by the longest text: long names wrap inside their column.
export const FIXED = { tableLayout: "fixed" } as const;
export const WRAP = { overflowWrap: "anywhere" } as const;

/** Contract utilization: which bound contracts sent files, and how much of each premium cap is used. */
export function ContractsCard({ r, asAt, style }: {
  r: Pick<Report, "cadence" | "contracts">; asAt: string; style?: React.CSSProperties;
}) {
  const c = r.contracts;
  const daily = r.cadence === "day";
  return (
    <div className="card" style={{ marginBottom: 18, ...style }}>
      <div className="card-h">
        <Title icon={FileSignature}>Contract utilization</Title>
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
            <InfoTip text={"Premium written so far in each contract's term, against its premium cap. Near cap from 85%."
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

/** Send now — the shared popup (components/SendByEmail, also used by the
 *  Policies list), filled in from the saved schedule's people and format. */
function SendModal({ open, onClose, report, logins, settings, onSent }: {
  open: boolean; onClose: () => void; report: Report; logins: Login[];
  settings: ExecutiveSettings; onSent: () => void;
}) {
  return (
    <SendByEmailModal open={open} onClose={onClose} title="Send the Executive Report" noun="report"
      what={<><b>{report.title}</b>{!report.complete && <span className="sub"> · still running, counts to today</span>}</>}
      logins={logins} ticked={settings.user_ids} emails={settings.emails}
      formats={FORMATS} format={settings.file_format}
      send={({ user_ids, emails, file_format }) => sendExecutiveReport({ cadence: report.cadence,
        period: report.period, user_ids, emails, file_format: file_format ?? settings.file_format })}
      onSent={onSent} />
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
        <FormatPick value={format} onChange={setFormat} formats={FORMATS} />
      </div>
    </Modal>
  );
}
