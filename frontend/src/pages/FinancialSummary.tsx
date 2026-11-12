// Financial Summary & Trends (Phase 2, task 35).
//
// Written premium, commission, brokerage, fees, taxes, bordereau net premium
// and earned premium, read from the processed bordereaux (backend
// financial_summary.py), by programme and by broker, for a period against the
// period before it and the same period last year.
//
// Every number says how sure it is: Reported (in the file), Calculated (worked
// out from reported figures by an approved formula), Needs review (the file's
// columns disagree or the template tag is unclear) or Not reported. Currencies
// are never added together — each is its own tab.
//
// Deliberately NOT a sidebar entry: opened from the Dashboard (both seats), as
// Policies is.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  Ban, Banknote, Handshake, Landmark, Layers, Percent, Receipt, TrendingUp, Wallet,
  type LucideIcon,
} from "lucide-react";
import {
  getFinancialRows, getFinancialSummary, type Basis, type CurrencyGroup, type Figure,
  type FigureStatus, type FinancialRows, type FinancialSummary as Summary,
  type MetricKey, type MetricTotals, type PartTotals,
} from "../api/finance";
import { useBrokerCarrierId } from "../brokerCarrier";
import { isBrokerSeat } from "../auth";
import { ChartCard, StatCard } from "../components/StatCard";
import { RankedBars } from "../components/BrokerCharts";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { periodLabel } from "../utils/date";

const ROWS_PAGE = 25;

const BASES: { key: Basis; label: string }[] = [
  { key: "month", label: "Month" }, { key: "quarter", label: "Quarter" }, { key: "year", label: "Year" },
];

const STATUS_LABEL: Record<FigureStatus, string> = {
  reported: "Reported", calculated: "Calculated", needs_review: "Needs review", not_reported: "Not reported",
};
const STATUS_BADGE: Record<FigureStatus, string> = {
  reported: "b-ok", calculated: "b-info", needs_review: "b-warn", not_reported: "b-mut",
};

const METRICS: { key: MetricKey; title: string; icon: LucideIcon; info: string }[] = [
  { key: "gross_premium", title: "Gross premium", icon: Banknote,
    info: "The premium on each row as the bordereau states it. Cancellations and return premiums count as negative." },
  { key: "total_gwp", title: "Total gross written premium (100%)", icon: Layers,
    info: "The whole premium of the risk, before any share is split off. Kept apart from gross premium." },
  { key: "commission_amount", title: "Commission", icon: Handshake,
    info: "Commission as the bordereau states it. Where a row gives only the rate, it is gross premium × rate ÷ 100 (Calculated)." },
  { key: "commission_pct", title: "Commission rate", icon: Percent,
    info: "Total commission ÷ total gross premium, over the rows that carry both. Changes are in percentage points." },
  { key: "brokerage_amount", title: "Brokerage", icon: Handshake,
    info: "Brokerage as the bordereau states it, kept apart from commission. Where a row gives only the rate, it is gross premium × rate ÷ 100 (Calculated)." },
  { key: "brokerage_pct", title: "Brokerage rate", icon: Percent,
    info: "Total brokerage ÷ total gross premium, over the rows that carry both. Changes are in percentage points." },
  { key: "fees", title: "Fees", icon: Receipt,
    info: "Fees as the bordereau states them. Kept separate; never deducted from anything." },
  { key: "taxes", title: "Taxes", icon: Landmark,
    info: "Taxes as the bordereau states them. Kept separate; never deducted from anything." },
  { key: "net_premium", title: "Net premium (bordereau)", icon: Wallet,
    info: "The net premium as the bordereau states it. It is never worked out from other figures." },
  { key: "earned_premium", title: "Earned premium", icon: TrendingUp,
    info: "Shown only where the bordereau reports it." },
];
const TABLE_METRICS: MetricKey[] = ["gross_premium", "total_gwp", "commission_amount", "net_premium"];

const CLASS_LABEL: Record<string, string> = {
  new: "New business", renewal: "Renewal", endorsement: "Endorsement", cancellation: "Cancellation",
  flat_cancellation: "Flat cancellation", return_premium: "Return premium", reinstatement: "Reinstatement",
  installment: "Instalment", adjustment: "Adjustment", audit: "Audit", unclassified: "Not classified",
};

type TabKey = "overview" | "programmes" | "brokers" | "transactions" | "rows" | "coverage";
const TABS: { key: TabKey; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "programmes", label: "By programme" },
  { key: "brokers", label: "By broker" },
  { key: "transactions", label: "Transaction types" },
  { key: "rows", label: "Bordereau rows" },
  { key: "coverage", label: "What's included" },
];

const idOrNull = (v: string) => (v && /^\d+$/.test(v) ? Number(v) : null);

function periodName(p: string | null | undefined): string {
  if (!p) return "—";
  const q = /^(\d{4})-Q([1-4])$/i.exec(p);
  if (q) return `Q${q[2]} ${q[1]}`;
  return periodLabel(p);
}

/** A real currency code goes in front of the number; the two groups that are
 *  not a currency carry their own tab label instead. */
function money(n: number | null, currency: string): string {
  if (n == null) return "—";
  const v = n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency === "none" || currency === "review" ? v : `${currency} ${v}`;
}

function rate(n: number | null): string {
  return n == null ? "—" : `${n.toLocaleString("en-GB", { maximumFractionDigits: 2 })}%`;
}

function change(c: number | null, unit: "money" | "percent"): string | undefined {
  if (c == null) return undefined;
  const sign = c > 0 ? "+" : c < 0 ? "-" : "";
  return unit === "percent" ? `${sign}${Math.abs(c).toFixed(2)} pts` : `${sign}${Math.abs(c).toFixed(1)}%`;
}

function emptyValue(m: MetricTotals): string {
  return m.current.counts.needs_review > 0 ? "Needs review" : "Not reported";
}

/** "18 reported · 2 calculated · 1 needs review" — the facts behind a total. */
function countsLine(m: MetricTotals): string {
  const c = m.current.counts;
  const parts: string[] = [];
  if (c.reported) parts.push(`${c.reported} reported`);
  if (c.calculated) parts.push(`${c.calculated} calculated`);
  if (c.needs_review) parts.push(`${c.needs_review} need${c.needs_review === 1 ? "s" : ""} review`);
  if (c.not_reported) parts.push(`${c.not_reported} not reported`);
  return parts.join(" · ");
}

export default function FinancialSummary() {
  const broker = isBrokerSeat();
  const carrierId = useBrokerCarrierId();

  const [basis, setBasis] = useState<Basis>("month");
  const [period, setPeriod] = useState<string | null>(null);
  const [programId, setProgramId] = useState<number | null>(null);
  const [brokerId, setBrokerId] = useState<number | null>(null);
  const [openProblems, setOpenProblems] = useState<"include" | "exclude">("include");
  const [currency, setCurrency] = useState<string | null>(null);
  const [tab, setTab] = useState<TabKey>("overview");

  const [data, setData] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    setLoading(true); setFailed(false);
    getFinancialSummary({
      basis, period, program_id: programId, broker_party_id: broker ? null : brokerId,
      carrier_id: broker ? carrierId : null, open_problems: openProblems,
    })
      .then(r => { if (live) setData(r); })
      .catch(() => { if (live) setFailed(true); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [basis, period, programId, brokerId, openProblems, carrierId, broker]);

  const groups = data?.groups ?? [];
  const group: CurrencyGroup | undefined =
    groups.find(g => g.currency === currency) ?? groups[0];
  const filtered = programId != null || brokerId != null || openProblems === "exclude";
  const w = data?.windows;
  const notes = data ? coverageItems(data).length : 0;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Financial Summary
              <InfoTip text={broker
                ? "Premium, commission and the other money in the bordereaux you have sent, by programme, for a period against the period before it and the same period last year."
                : "Premium, commission and the other money in the bordereaux your brokers have sent, by programme and by broker, for a period against the period before it and the same period last year."} />
            </h2>
          </div>
          <div className="actions">
            <Link className="btn" to="/policies">Search Policies</Link>
            <Link className="btn" to={broker ? "/broker" : "/home"}>← Dashboard</Link>
          </div>
        </div>

        <div className="card" style={{ overflow: "visible", marginBottom: 14 }}>
          <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none", alignItems: "flex-end" }}>
            <label className="fbar-field">
              <span className="sub">Period type</span>
              <span className="seg">
                {BASES.map(b => (
                  <button key={b.key} type="button" className={basis === b.key ? "on" : ""}
                    onClick={() => { setBasis(b.key); setPeriod(null); }}>{b.label}</button>
                ))}
              </span>
            </label>
            <label className="fbar-field" style={{ minWidth: 150 }}>
              <span className="sub">Period</span>
              <select className="fbar-select" aria-label="Period" value={data?.period ?? ""}
                onChange={e => setPeriod(e.target.value || null)}>
                {(data?.periods ?? []).length === 0 && <option value="">No periods yet</option>}
                {(data?.periods ?? []).map(p => <option key={p} value={p}>{periodName(p)}</option>)}
              </select>
            </label>
            <label className="fbar-field">
              <span className="sub">Programme</span>
              <select className="fbar-select" aria-label="Programme" value={programId ?? ""}
                onChange={e => setProgramId(idOrNull(e.target.value))}>
                <option value="">All programmes</option>
                {(data?.filters.programmes ?? []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            {!broker && (
              <label className="fbar-field">
                <span className="sub">Broker</span>
                <select className="fbar-select" aria-label="Broker" value={brokerId ?? ""}
                  onChange={e => setBrokerId(idOrNull(e.target.value))}>
                  <option value="">All brokers</option>
                  {(data?.filters.brokers ?? []).map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
                </select>
              </label>
            )}
            {groups.length > 1 && (
              <label className="fbar-field">
                <span className="sub">Currency
                  <InfoTip text="Amounts in different currencies are never added together, so each currency is shown on its own." />
                </span>
                <select className="fbar-select" aria-label="Currency" value={group?.currency ?? ""}
                  onChange={e => setCurrency(e.target.value)}>
                  {groups.map(g => (
                    <option key={g.currency} value={g.currency}>
                      {g.label} ({g.rows} row{g.rows === 1 ? "" : "s"} this period)
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="fbar-field">
              <span className="sub">Files with open problems</span>
              <select className="fbar-select" aria-label="Files with open problems" value={openProblems}
                onChange={e => setOpenProblems(e.target.value as "include" | "exclude")}>
                <option value="include">Included</option>
                <option value="exclude">Left out</option>
              </select>
            </label>
            {filtered && (
              <span className="linkish fbar-clear-link" onClick={() => {
                setProgramId(null); setBrokerId(null); setOpenProblems("include");
              }}>Clear filters</span>
            )}
          </div>
        </div>

        {failed ? (
          <div className="card"><div className="empty">Could not load the financial summary.</div></div>
        ) : !data ? (
          <div className="card"><div className="empty">Loading…</div></div>
        ) : !data.period ? (
          <div className="card"><div className="empty">
            {data.coverage.files === 0 ? "No bordereaux have been processed yet."
              : "None of the processed bordereaux names a reporting period yet."}
          </div></div>
        ) : (
          <div style={loading ? { opacity: 0.6 } : undefined}>
            <div className="sub" style={{ marginBottom: 12 }}>
              {periodName(w?.current?.period)} ({w?.current?.files ?? 0} file{w?.current?.files === 1 ? "" : "s"})
              {" "}compared with {periodName(w?.previous?.period)} ({w?.previous?.files ?? 0})
              {basis !== "year" && <> and {periodName(w?.last_year?.period)} ({w?.last_year?.files ?? 0})</>}
            </div>

            <div className="tabs" role="tablist">
              {TABS.filter(t => !(broker && t.key === "brokers")).map(t => (
                <button key={t.key} role="tab" aria-selected={tab === t.key}
                  className={tab === t.key ? "on" : ""} onClick={() => setTab(t.key)}>
                  {t.label}
                  {t.key === "rows" && group && <span className="sub"> · {group.rows}</span>}
                  {t.key === "coverage" && notes > 0 && <span className="sub"> · {notes}</span>}
                </button>
              ))}
            </div>

            {!group ? (
              <div className="card"><div className="empty">No policy rows in this period.</div></div>
            ) : tab === "overview" ? (
              <>
                <MetricCards group={group} basis={basis} nwp={data.net_written_premium} />
                <CompareTable group={group} data={data} />
              </>
            ) : tab === "programmes" ? (
              <Parts title="By programme" parts={group.by_programme} currency={group.currency} />
            ) : tab === "brokers" ? (
              <Parts title="By broker" parts={group.by_broker} currency={group.currency} />
            ) : tab === "transactions" ? (
              <Transactions data={data} />
            ) : tab === "rows" ? (
              <RowsCard basis={basis} period={data.period} programId={programId}
                brokerId={broker ? null : brokerId} carrierId={broker ? carrierId : null}
                openProblems={openProblems} currency={group.currency} showBroker={!broker} />
            ) : (
              <Coverage data={data} />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function MetricCards({ group, basis, nwp }: {
  group: CurrencyGroup; basis: Basis; nwp: Summary["net_written_premium"];
}) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))", gap: 16 }}>
      {METRICS.map(m => {
        const t = group.totals[m.key];
        const v = t.current.value;
        const ly = change(t.change.last_year, t.unit);
        return (
          <StatCard key={m.key} title={m.title} icon={m.icon} info={m.info}
            value={v == null ? emptyValue(t) : t.unit === "percent" ? rate(v) : money(v, group.currency)}
            trend={change(t.change.previous, t.unit)}
            subtitle={<>
              {basis !== "year" && ly && <div>Same period last year: {ly}</div>}
              <div>{countsLine(t)}</div>
            </>} />
        );
      })}
      <StatCard title="Net written premium" icon={Ban} value="Not available"
        info={`${nwp.formula}. ${nwp.reason}`}
        subtitle="Needs assumed reinsurance, not yet recorded" />
    </div>
  );
}

function CompareTable({ group, data }: { group: CurrencyGroup; data: Summary }) {
  const w = data.windows;
  const showLy = data.basis !== "year";
  const fmt = (k: MetricKey, v: number | null) =>
    group.totals[k].unit === "percent" ? rate(v) : money(v, group.currency);
  return (
    <div className="card" style={{ marginTop: 18 }}>
      <div className="card-h"><h3>This period against earlier periods</h3></div>
      <div className="tbl-wrap">
        <table>
          <thead>
            <tr>
              <th className="l">Figure</th>
              <th>{periodName(w.current?.period)}</th>
              <th>{periodName(w.previous?.period)}</th>
              <th>Change</th>
              {showLy && <th>{periodName(w.last_year?.period)}</th>}
              {showLy && <th>Change</th>}
              <th className="l">Rows this period</th>
            </tr>
          </thead>
          <tbody>
            {METRICS.map(m => {
              const t = group.totals[m.key];
              return (
                <tr key={m.key}>
                  <td className="l">{m.title}</td>
                  <td>{t.current.value == null ? <span className="muted">{emptyValue(t)}</span> : fmt(m.key, t.current.value)}</td>
                  <td>{fmt(m.key, t.previous.value)}</td>
                  <td><Change c={t.change.previous} unit={t.unit} /></td>
                  {showLy && <td>{fmt(m.key, t.last_year.value)}</td>}
                  {showLy && <td><Change c={t.change.last_year} unit={t.unit} /></td>}
                  <td className="l"><span className="sub">{countsLine(t)}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Change({ c, unit }: { c: number | null; unit: "money" | "percent" }) {
  const s = change(c, unit);
  if (!s) return <span className="muted">—</span>;
  return <span style={{ color: c! < 0 ? "var(--p-crit)" : c! > 0 ? "var(--p-ok)" : undefined, fontWeight: 600 }}>{s}</span>;
}

function Parts({ title, parts, currency }: { title: string; parts: PartTotals[]; currency: string }) {
  const bars = parts
    .map((p, i) => ({ id: p.id ?? -1 - i, name: p.name, value: p.totals.gross_premium.current.value ?? 0 }))
    .filter(r => r.value > 0);
  return (
    <ChartCard title={title} info={<InfoTip text="Gross premium this period, highest first. The table adds the previous period and the change." />}>
      <RankedBars rows={bars} unit={currency === "none" || currency === "review" ? "" : currency}
        empty="No gross premium reported this period." title={`${title} — gross premium`} />
      {parts.length > 0 && (
        <div className="tbl-wrap" style={{ marginTop: 14 }}>
          <table>
            <thead>
              <tr>
                <th className="l">{title.replace("By ", "").replace(/^./, c => c.toUpperCase())}</th>
                {TABLE_METRICS.map(k => <th key={k}>{METRICS.find(m => m.key === k)!.title.replace(" (100%)", "")}</th>)}
                <th>Gross vs previous</th>
              </tr>
            </thead>
            <tbody>
              {parts.map((p, i) => (
                <tr key={p.id ?? `n${i}`}>
                  <td className="l">{p.name}</td>
                  {TABLE_METRICS.map(k => {
                    const t = p.totals[k];
                    return <td key={k}>{t.current.value == null ? <span className="muted">{emptyValue(t)}</span> : money(t.current.value, currency)}</td>;
                  })}
                  <td><Change c={p.totals.gross_premium.change.previous} unit="money" /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </ChartCard>
  );
}

function Transactions({ data }: { data: Summary }) {
  return (
    <ChartCard title="Transaction types this period"
      info={<InfoTip text="As each bordereau wrote them. Cancellations and return premiums (CN, Cancellation, flat cancellations, Endorsement - Return Premium) count as negative; every other row keeps the sign it was sent with. Codes Kavachio does not know are shown as sent." />}>
      {data.transactions.length === 0 ? <div className="empty">No rows this period.</div> : (
        <div className="tbl-wrap">
          <table>
            <thead><tr><th className="l">As sent</th><th className="l">Read as</th><th>Counted as</th><th>Rows</th></tr></thead>
            <tbody>
              {data.transactions.map((t, i) => (
                <tr key={i}>
                  <td className="l">{t.sent ?? <span className="muted">Not reported</span>}</td>
                  <td className="l">{t.class ? CLASS_LABEL[t.class] ?? t.class : "—"}</td>
                  <td>{t.negative ? <span className="badge b-warn"><span className="d" />Negative</span> : "As sent"}</td>
                  <td>{t.rows}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </ChartCard>
  );
}

type CoverageItem = { n: number; text: ReactNode; files?: { export_id: number; filename: string | null; problem?: string | null }[] };

/** What the totals leave out or cannot vouch for — one list, so the tab's
 *  count and the panel agree. */
function coverageItems(data: Summary): CoverageItem[] {
  const c = data.coverage;
  return [
    { n: c.no_period.length, text: "files name no reporting period, so they are in no period", files: c.no_period },
    { n: c.longer_period.length, text: `files report a longer period than one ${data.basis}, so they count only in a longer view`, files: c.longer_period },
    { n: c.copies.length, text: "files are exact copies of a newer file and are counted once", files: c.copies },
    { n: c.unreadable.length, text: "files could not be read", files: c.unreadable },
    { n: c.open_problems.files, text: c.open_problems.excluded ? "files with open problems are left out" : "files this period still have open problems (included)" },
    { n: c.percent_unverified_templates, text: "output templates write a rate whose form (25% or 0.25) could not be checked; those rates need review" },
    { n: c.headings_not_in_file, text: "tagged columns were not in the file (written empty), so they report nothing" },
  ].filter(i => i.n > 0);
}

function Coverage({ data }: { data: Summary }) {
  const items = coverageItems(data);
  return (
    <ChartCard title="What the totals do and do not include"
      info={<InfoTip text="Only the current version of each file is counted, once. A missing figure is never counted as 0. Different currencies are never added together." />}>
      {items.length === 0 ? <div className="empty">Every file this period was read and placed.</div> : (
        <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 8 }}>
          {items.map((i, k) => (
            <li key={k}>
              <b>{i.n}</b> {i.text}
              {i.files && i.files.length > 0 && (
                <div className="sub">
                  {i.files.slice(0, 4).map(f => f.filename + (f.problem ? ` (${f.problem})` : "")).join(", ")}
                  {i.files.length > 4 && ` and ${i.files.length - 4} more`}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </ChartCard>
  );
}

const ROW_METRICS: { key: MetricKey; label: string }[] = [
  { key: "gross_premium", label: "Gross" }, { key: "total_gwp", label: "Total (100%)" },
  { key: "commission_amount", label: "Commission" }, { key: "commission_pct", label: "Comm. %" },
  { key: "brokerage_amount", label: "Brokerage" }, { key: "fees", label: "Fees" },
  { key: "taxes", label: "Taxes" }, { key: "net_premium", label: "Net" },
  { key: "earned_premium", label: "Earned" },
];

function RowsCard({ basis, period, programId, brokerId, carrierId, openProblems, currency, showBroker }: {
  basis: Basis; period: string; programId: number | null; brokerId: number | null;
  carrierId: number | null; openProblems: "include" | "exclude"; currency: string; showBroker: boolean;
}) {
  const [status, setStatus] = useState<FigureStatus | null>(null);
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<FinancialRows | null>(null);
  const [failed, setFailed] = useState(false);
  const key = useMemo(() => JSON.stringify([basis, period, programId, brokerId, carrierId, openProblems, currency, status]),
    [basis, period, programId, brokerId, carrierId, openProblems, currency, status]);

  useEffect(() => { setPage(1); }, [key]);
  useEffect(() => {
    let live = true;
    setFailed(false);
    getFinancialRows({
      basis, period, program_id: programId, broker_party_id: brokerId, carrier_id: carrierId,
      open_problems: openProblems, currency, status, window: "current", page, page_size: ROWS_PAGE,
    })
      .then(r => { if (live) setRows(r); })
      .catch(() => { if (live) setFailed(true); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, page]);

  return (
    <div className="card">
      <div className="card-h" style={{ flexWrap: "wrap", gap: 12 }}>
        <h3>Bordereau rows behind this period
          <InfoTip text="Every policy row counted above, with each figure's label. Hover a Calculated or Needs review figure to see the working or the reason." />
        </h3>
        <div className="right">
          <span className="seg sm">
            {([null, "calculated", "needs_review", "not_reported"] as (FigureStatus | null)[]).map(s => (
              <button key={s ?? "all"} type="button" className={status === s ? "on" : ""}
                onClick={() => setStatus(s)}>{s ? `With ${STATUS_LABEL[s].toLowerCase()}` : "All rows"}</button>
            ))}
          </span>
        </div>
      </div>
      {failed ? <div className="empty">Could not load the rows.</div>
        : !rows ? <div className="empty">Loading…</div>
        : rows.total === 0 ? <div className="empty">No rows match.</div> : (
          <>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="l">Policy</th>
                    <th className="l">Programme</th>
                    {showBroker && <th className="l">Broker</th>}
                    <th className="l">Transaction</th>
                    <th>Cover</th>
                    {ROW_METRICS.map(m => <th key={m.key}>{m.label}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {rows.items.map(r => (
                    <tr key={`${r.export_id}:${r.sheet}:${r.row}`}>
                      <td className="l mono" style={{ whiteSpace: "nowrap" }}>
                        {r.policy}<div className="sub">{r.filename} · row {r.row}</div>
                      </td>
                      <td className="l">{r.programme ?? "—"}</td>
                      {showBroker && <td className="l">{r.broker ?? "—"}</td>}
                      <td className="l">
                        {r.transaction.sent ?? <span className="muted">—</span>}
                        {r.transaction.negative && <div><span className="badge b-warn"><span className="d" />Negative</span></div>}
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        {r.cover_start.value || r.cover_end.value
                          ? `${r.cover_start.value ?? "—"} – ${r.cover_end.value ?? "—"}` : <span className="muted">—</span>}
                      </td>
                      {ROW_METRICS.map(m => <td key={m.key}><Cell f={r.figures[m.key]} pct={m.key === "commission_pct"} /></td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination page={page} pageSize={ROWS_PAGE} totalItems={rows.total}
              pageCount={Math.max(1, Math.ceil(rows.total / ROWS_PAGE))} onPageChange={setPage} noun="rows" />
          </>
        )}
    </div>
  );
}

/** One figure in the rows table: the number, and its label when it is not
 *  simply reported; the working or the reason behind the ⓘ. */
function Cell({ f, pct }: { f: Figure; pct: boolean }) {
  if (f.status === "not_reported") return <span className="muted">—</span>;
  const shown = f.value == null ? null
    : pct ? rate(f.value) : f.value.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (
    <span style={{ whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 4 }}>
      {shown}
      {f.status !== "reported" && (
        <span className={`badge ${STATUS_BADGE[f.status]}`}>{STATUS_LABEL[f.status]}</span>
      )}
      {f.how && <InfoTip text={f.how} />}
    </span>
  );
}
