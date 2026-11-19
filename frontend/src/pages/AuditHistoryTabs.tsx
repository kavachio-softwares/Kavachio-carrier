// The Audit Logs tabs beyond the trail (backend audit_history.py):
// Downloads & Views, Partner Submission History, Contract History and File
// Versions — plus the pieces the trail shares with them (tiles, the side panel).
// Every word is meant for someone who has never seen the screen before.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  CalendarClock, CheckCircle2, Clock, Download, Eye, FileStack, FileText, History, RefreshCw, Users,
  XCircle, type LucideIcon,
} from "lucide-react";
import {
  downloadVersion, getAccess, getContractHistory, getContractTimeline, getHistoryChoices, getSubmissions,
  getSubmissionTimeline, getVersions, type AccessPage, type Choice, type ContractPage, type ContractTimeline,
  type HistoryFilters, type SubmissionPage, type SubmissionTimeline, type TimelineStep, type VersionPage,
} from "../api/auditHistory";
import type { AuditQuery, AuditRow } from "../api/audit";
import { useServerList } from "../hooks/useServerList";
import { Pagination } from "../components/Pagination";
import { InfoTip } from "../components/InfoTip";
import { showError } from "../components/ResultPopup";
import { fmtDate, fmtDateTime } from "../utils/date";

const PAGE = 25;
const L: React.CSSProperties = { textAlign: "left" };
export const TONE: Record<string, string> = { ok: "b-ok", warn: "b-warn", bad: "b-crit", info: "b-info", muted: "b-mut" };

/** "2026-10" → "Oct 2026"; anything else as it is. */
export function period(p: string | null | undefined): string {
  if (!p) return "—";
  const m = /^(\d{4})-(\d{2})$/.exec(p);
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, 1).toLocaleString("en-GB", { month: "short", year: "numeric" }) : p;
}

// ── tiles ──────────────────────────────────────────────────────────────────
export function Tile({ icon: Icon, label, value, colour, info }: {
  icon: LucideIcon; label: string; value: number | string | null | undefined; colour: string; info?: string;
}) {
  return (
    <div className="card" style={{ padding: "14px 16px", display: "flex", gap: 12, alignItems: "center" }}>
      <span style={{ width: 40, height: 40, borderRadius: 20, display: "grid", placeItems: "center",
                     background: `${colour}1a`, color: colour, flex: "none" }}><Icon size={20} /></span>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 22, fontWeight: 700, lineHeight: 1.1 }}>
          {value == null ? "…" : typeof value === "number" ? value.toLocaleString() : value}</div>
        <div className="sub" style={{ display: "flex", alignItems: "center", gap: 4 }}>
          {label}{info && <InfoTip text={info} />}</div>
      </div>
    </div>
  );
}
export const tiles = (n: number): React.CSSProperties =>
  ({ display: "grid", gap: 14, gridTemplateColumns: `repeat(auto-fit, minmax(${n > 4 ? 170 : 200}px, 1fr))`, marginBottom: 14 });

// ── the date range every tab shares ────────────────────────────────────────
const localDay = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const back = (months: number) => { const d = new Date(); d.setMonth(d.getMonth() - months); return localDay(d); };
const RANGES = [{ k: "1", label: "Last month" }, { k: "3", label: "Last 3 months" }, { k: "6", label: "Last 6 months" },
                { k: "12", label: "Last 12 months" }, { k: "all", label: "All time" }, { k: "custom", label: "Pick dates…" }];

function useRange(start = "6") {
  const [k, setK] = useState(start);
  const [from, setFrom] = useState(back(Number(start) || 6));
  const [to, setTo] = useState(localDay(new Date()));
  const dates = k === "custom" ? { from, to } : k === "all" ? {} : { from: back(Number(k)), to: localDay(new Date()) };
  const field = (label: string) => (
    <>
      <label className="fbar-field">
        <span className="sub">{label}</span>
        <select className="fbar-select" value={k} onChange={e => setK(e.target.value)}>
          {RANGES.map(r => <option key={r.k} value={r.k}>{r.label}</option>)}
        </select>
      </label>
      {k === "custom" && (
        <label className="fbar-field">
          <span className="sub">Between</span>
          <span className="fbar-daterange">
            <input type="date" className="fbar-date" value={from} max={to} onChange={e => setFrom(e.target.value)} />
            <span className="sub">–</span>
            <input type="date" className="fbar-date" value={to} min={from} onChange={e => setTo(e.target.value)} />
          </span>
        </label>)}
    </>
  );
  return { dates, field, reset: () => setK(start), changed: k !== start };
}

function useChoices() {
  const [c, setC] = useState<{ programmes: Choice[]; brokers: Choice[] }>({ programmes: [], brokers: [] });
  useEffect(() => { getHistoryChoices().then(setC).catch(() => {}); }, []);
  return c;
}

function Pick({ label, value, onChange, options, all }: {
  label: string; value: string | number; onChange: (v: string) => void;
  options: { value: string | number; label: string }[]; all: string;
}) {
  return (
    <label className="fbar-field">
      <span className="sub">{label}</span>
      <select className="fbar-select" value={value} onChange={e => onChange(e.target.value)}>
        <option value="">{all}</option>
        {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

const Bar = ({ children, onClear, clear }: { children: ReactNode; onClear: () => void; clear: boolean }) => (
  <div className="card" style={{ marginBottom: 14 }}>
    <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none", alignItems: "flex-end" }}>
      {children}
      {clear && <span className="linkish fbar-clear-link" onClick={onClear}>Clear filters</span>}
    </div>
  </div>
);

// ── the side panel ─────────────────────────────────────────────────────────
export function SidePanel({ open, title, sub, onClose, children }: {
  open: boolean; title: ReactNode; sub?: ReactNode; onClose: () => void; children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [open, onClose]);
  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h">
          <div><h4>{title}</h4>{sub && <div className="sub">{sub}</div>}</div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b">{open && children}</div>
      </aside>
    </>
  );
}

/** Label → value pairs, one per line. */
export const Facts = ({ rows }: { rows: [string, ReactNode][] }) => (
  <div style={{ display: "grid", gap: 8, fontSize: 13.5, marginBottom: 16 }}>
    {rows.map(([k, v]) => (
      <div key={k} style={{ display: "grid", gridTemplateColumns: "130px 1fr", gap: 10 }}>
        <span className="sub">{k}</span><span style={{ wordBreak: "break-word" }}>{v || "—"}</span></div>))}
  </div>
);

const DOT: Record<string, string> = {
  expected: "#64748b", due: "#d97706", reminder: "#2563eb", chase: "#7c3aed", case: "#dc2626",
  received: "#16a34a", Draft: "#64748b", Review: "#2563eb", Signing: "#7c3aed", Amendment: "#d97706",
  Renewal: "#0d9488", Status: "#16a34a", Change: "#475569",
};

function Timeline({ steps, dateOnly }: { steps: (TimelineStep & { before?: string | null; after?: string | null })[]; dateOnly?: (s: TimelineStep) => boolean }) {
  if (!steps.length) return <div className="empty">Nothing recorded yet.</div>;
  return (
    <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {steps.map((s, i) => (
        <li key={i} style={{ display: "grid", gridTemplateColumns: "18px 1fr", gap: 10, paddingBottom: 14, position: "relative" }}>
          <span style={{ position: "relative" }}>
            <span style={{ position: "absolute", top: 4, left: 3, width: 11, height: 11, borderRadius: 6,
                           background: DOT[s.kind] ?? "#64748b" }} />
            {i < steps.length - 1 && <span style={{ position: "absolute", top: 17, left: 8, bottom: -14, width: 1,
                                                    background: "var(--p-border)" }} />}
          </span>
          <div>
            <div style={{ fontWeight: 600 }}>{s.title}</div>
            {s.after && <div className="sub">Status: {s.before ? `${s.before} → ` : ""}<b>{s.after}</b></div>}
            {s.detail && <div style={{ fontSize: 13, margin: "2px 0" }}>{s.detail}</div>}
            <div className="sub">{s.at ? (dateOnly?.(s) ? fmtDate(s.at) : fmtDateTime(s.at)) : "—"} · {s.by}</div>
          </div>
        </li>))}
    </ol>
  );
}

const loading = (l: boolean, n: number, empty: string, failed: boolean) =>
  failed ? <div className="empty">Could not load this. Try again in a moment.</div>
    : l && n === 0 ? <div className="empty">Loading…</div>
    : n === 0 ? <div className="empty">{empty}</div> : null;

// ── Downloads & Views ──────────────────────────────────────────────────────
export function DownloadsViews({ query }: { query: AuditQuery }) {
  const [kind, setKind] = useState("");
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<AuditRow | null>(null);
  const list = useServerList<AuditRow, AccessPage>(
    (p, n) => getAccess(query, kind, p, n).then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify([query, kind]), PAGE);
  const c = list.extra?.counts;
  return (
    <>
      <div style={tiles(3)}>
        <Tile icon={Eye} label="Viewed" value={c?.viewed} colour="#2563eb" info="Times someone opened a file, a policy, a contract document or a report on screen." />
        <Tile icon={Download} label="Downloaded" value={c?.downloaded} colour="#d97706" info="Times someone saved a file, a contract or a report to their computer." />
        <Tile icon={Users} label="People" value={c?.people} colour="#7c3aed" info="Different people who viewed or downloaded something." />
      </div>
      <div className="card">
        <div className="card-h">
          <h3>Who opened or downloaded what
            <InfoTip text={"Every time someone opens or downloads a file, a report, a policy or a contract.\nUses the dates and the person picked above."} /></h3>
          <span className="seg right">{[["", "All"], ["view", "Viewed"], ["download", "Downloaded"]].map(([k, l]) => (
            <button key={k} type="button" className={kind === k ? "on" : ""} onClick={() => setKind(k)}>{l}</button>))}</span>
        </div>
        {loading(list.loading, list.items.length, "Nobody viewed or downloaded anything in these dates.", failed) ?? (
          <>
            <div className="tbl-wrap"><table>
              <thead><tr><th style={L}>Date and time</th><th style={L}>Person</th><th style={L}>What they did</th>
                <th style={L}>What it was</th></tr></thead>
              <tbody>{list.items.map(r => (
                <tr key={r.id} onClick={() => setOpen(r)} style={{ cursor: "pointer" }}>
                  <td style={{ ...L, whiteSpace: "nowrap" }} className="muted">{fmtDateTime(r.at)}</td>
                  <td style={L}><b>{r.actor}</b><div className="sub">{r.actor_role}</div></td>
                  <td style={L}><span className={`badge ${r.status === "Downloaded" ? "b-warn" : "b-info"}`}>{r.status}</span>
                    <div className="sub" style={{ marginTop: 2 }}>{r.action_label}</div></td>
                  <td style={{ ...L, whiteSpace: "normal" }}>{r.target}</td>
                </tr>))}</tbody>
            </table></div>
            <Pagination page={list.page} pageSize={PAGE} totalItems={list.total} pageCount={list.pageCount}
              onPageChange={list.setPage} noun="entries" />
          </>)}
      </div>
      <SidePanel open={!!open} title={open?.action_label} sub={open && fmtDateTime(open.at)} onClose={() => setOpen(null)}>
        {open && <Facts rows={[["Person", <><b>{open.actor}</b> · {open.actor_role}</>], ["Organisation", open.actor_org],
                               ["Viewed or downloaded", open.status], ["What it was", open.target],
                               ["When", fmtDateTime(open.at)]]} />}
      </SidePanel>
    </>
  );
}

// ── Partner Submission History ─────────────────────────────────────────────
const SUB_STATUSES = ["On time", "Late", "Missing", "Due today", "Not due yet", "No file needed"];

export function PartnerSubmissions() {
  const ch = useChoices();
  const range = useRange("6");
  const [f, setF] = useState<HistoryFilters>({});
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const filters = useMemo(() => ({ ...f, ...range.dates }), [f, range.dates.from, range.dates.to]); // eslint-disable-line react-hooks/exhaustive-deps
  const list = useServerList<SubmissionPage["items"][number], SubmissionPage>(
    (p, n) => getSubmissions(filters, p, n).then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify(filters), PAGE);
  const c = list.extra?.counts;
  const set = (k: keyof HistoryFilters) => (v: string) => setF(x => ({ ...x, [k]: k === "status" ? v : v ? Number(v) : "" }));
  return (
    <>
      <Bar clear={!!(f.broker || f.programme || f.status || range.changed)} onClear={() => { setF({}); range.reset(); }}>
        {ch.brokers.length > 0 && <Pick label="Broker" value={f.broker ?? ""} onChange={set("broker")} all="All brokers"
          options={ch.brokers.map(b => ({ value: b.id, label: b.name }))} />}
        <Pick label="Programme" value={f.programme ?? ""} onChange={set("programme")} all="All programmes"
          options={ch.programmes.map(p => ({ value: p.id, label: p.name }))} />
        {range.field("Due date")}
        <Pick label="Status" value={f.status ?? ""} onChange={set("status")} all="Any status"
          options={SUB_STATUSES.map(s => ({ value: s, label: s }))} />
      </Bar>
      <div style={tiles(4)}>
        <Tile icon={CalendarClock} label="Expected" value={c?.expected} colour="#2563eb" info="Files that were due in these dates." />
        <Tile icon={CheckCircle2} label="Received" value={c?.received} colour="#16a34a" info="Due files that have arrived, on time or late." />
        <Tile icon={Clock} label="Late" value={c?.late} colour="#d97706" info="Files that arrived after their due date." />
        <Tile icon={XCircle} label="Missing" value={c?.missing} colour="#dc2626" info="Files past their due date that have not arrived." />
      </div>
      <div className="card">
        <div className="card-h"><h3>Files owed by brokers
          <InfoTip text={"Every file a broker had to send, across all programmes.\nClick a row to see what happened: reminders, chases and when it arrived."} /></h3></div>
        {loading(list.loading, list.items.length, "No files were due in these dates.", failed) ?? (
          <>
            <div className="tbl-wrap"><table>
              <thead><tr><th style={L}>Broker</th><th style={L}>Programme</th><th style={L}>Reporting period</th>
                <th style={L}>Due date</th><th style={L}>Received on</th><th style={L}>Status</th><th style={L}>Reminders</th></tr></thead>
              <tbody>{list.items.map(r => (
                <tr key={r.id} onClick={() => setOpen(r.id)} style={{ cursor: "pointer" }}>
                  <td style={L}><b>{r.broker}</b></td>
                  <td style={L}>{r.programme}</td>
                  <td style={L}>{period(r.period)}</td>
                  <td style={L}>{fmtDate(r.due_on)}</td>
                  <td style={L}>{r.received_on ? fmtDate(r.received_on) : "—"}</td>
                  <td style={L}><span className={`badge ${TONE[r.tone] ?? "b-mut"}`}>{r.status}</span>
                    {r.days_late > 0 && <div className="sub">{r.days_late} {r.days_late === 1 ? "day" : "days"} late</div>}</td>
                  <td style={L}>{r.reminders || "—"}</td>
                </tr>))}</tbody>
            </table></div>
            <Pagination page={list.page} pageSize={PAGE} totalItems={list.total} pageCount={list.pageCount}
              onPageChange={list.setPage} noun="files" />
          </>)}
      </div>
      <SubmissionPanel id={open} onClose={() => setOpen(null)} />
    </>
  );
}

function SubmissionPanel({ id, onClose }: { id: number | null; onClose: () => void }) {
  const [t, setT] = useState<SubmissionTimeline | null>(null);
  useEffect(() => {
    setT(null);
    if (id != null) getSubmissionTimeline(id).then(setT).catch(e => { showError(e, "Could not open this file's history"); onClose(); });
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <SidePanel open={id != null} title="What happened to this file" sub={t && `${t.broker ?? "—"} · ${t.programme ?? "—"}`} onClose={onClose}>
      {!t ? <div className="muted">Loading…</div> : (
        <>
          <Facts rows={[["Reporting period", period(t.period)], ["Due date", fmtDate(t.due_on)], ["Status", t.status]]} />
          <h4 style={{ margin: "4px 0 12px" }}>Timeline</h4>
          <Timeline steps={t.steps} dateOnly={s => s.kind === "due"} />
        </>)}
    </SidePanel>
  );
}

// ── Contract History ───────────────────────────────────────────────────────
export function ContractHistory() {
  const ch = useChoices();
  const range = useRange("12");
  const [f, setF] = useState<HistoryFilters>({});
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const filters = useMemo(() => ({ ...f, ...range.dates }), [f, range.dates.from, range.dates.to]); // eslint-disable-line react-hooks/exhaustive-deps
  const list = useServerList<ContractPage["items"][number], ContractPage>(
    (p, n) => getContractHistory(filters, p, n).then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify(filters), PAGE);
  const c = list.extra?.counts;
  const set = (k: keyof HistoryFilters) => (v: string) => setF(x => ({ ...x, [k]: k === "status" ? v : v ? Number(v) : "" }));
  return (
    <>
      <Bar clear={!!(f.broker || f.programme || f.status || f.contract || range.changed)} onClear={() => { setF({}); range.reset(); }}>
        <Pick label="Contract" value={f.contract ?? ""} onChange={set("contract")} all="All contracts"
          options={(list.extra?.choices.contracts ?? []).map(x => ({ value: x.id, label: x.name }))} />
        {ch.brokers.length > 0 && <Pick label="Broker" value={f.broker ?? ""} onChange={set("broker")} all="All brokers"
          options={ch.brokers.map(b => ({ value: b.id, label: b.name }))} />}
        <Pick label="Programme" value={f.programme ?? ""} onChange={set("programme")} all="All programmes"
          options={ch.programmes.map(p => ({ value: p.id, label: p.name }))} />
        <Pick label="Status now" value={f.status ?? ""} onChange={set("status")} all="Any status"
          options={(list.extra?.choices.statuses ?? []).map(s => ({ value: s, label: s }))} />
        {range.field("Date")}
      </Bar>
      <div style={tiles(4)}>
        <Tile icon={FileText} label="Contracts" value={c?.contracts} colour="#2563eb" info="Contracts with a broker that match the filters." />
        <Tile icon={CheckCircle2} label="Active" value={c?.active} colour="#16a34a" info="Contracts in force today." />
        <Tile icon={History} label="Changed mid-term" value={c?.changed_mid_term} colour="#d97706" info="Contracts with a change made while they were running." />
        <Tile icon={RefreshCw} label="Renewals" value={c?.renewed} colour="#0d9488" info="Contracts that renew an earlier one." />
      </div>
      <div className="card">
        <div className="card-h"><h3>Every step on every contract
          <InfoTip text={"Drafts, reviews, signing, mid-term changes, renewals and status changes — newest first.\nClick a row to see that contract's whole story."} /></h3></div>
        {loading(list.loading, list.items.length, "Nothing happened to these contracts in these dates.", failed) ?? (
          <>
            <div className="tbl-wrap"><table>
              <thead><tr><th style={L}>Date</th><th style={L}>Contract</th><th style={L}>What happened</th>
                <th style={L}>Status</th><th style={L}>By</th></tr></thead>
              <tbody>{list.items.map((r, i) => (
                <tr key={`${r.contract_id}-${i}`} onClick={() => setOpen(r.contract_id)} style={{ cursor: "pointer" }}>
                  <td style={{ ...L, whiteSpace: "nowrap" }} className="muted">{r.at ? fmtDateTime(r.at) : "—"}</td>
                  <td style={{ ...L, whiteSpace: "normal" }}><b>{r.contract}</b><div className="sub">{r.programme} · {r.broker}</div></td>
                  <td style={L}>{r.title}{r.note && <div className="sub" style={{ whiteSpace: "normal" }}>“{r.note}”</div>}</td>
                  <td style={L}>{r.after ? <>{r.before && <span className="sub">{r.before} → </span>}<b>{r.after}</b></>
                    : <span className="sub">No change</span>}</td>
                  <td style={L}>{r.by}</td>
                </tr>))}</tbody>
            </table></div>
            <Pagination page={list.page} pageSize={PAGE} totalItems={list.total} pageCount={list.pageCount}
              onPageChange={list.setPage} noun="steps" />
          </>)}
      </div>
      <ContractPanel id={open} onClose={() => setOpen(null)} />
    </>
  );
}

function ContractPanel({ id, onClose }: { id: number | null; onClose: () => void }) {
  const [t, setT] = useState<ContractTimeline | null>(null);
  useEffect(() => {
    setT(null);
    if (id != null) getContractTimeline(id).then(setT).catch(e => { showError(e, "Could not open this contract's history"); onClose(); });
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <SidePanel open={id != null} title={t?.name ?? "Contract history"} sub={t && `${t.programme ?? "—"} · ${t.broker ?? "—"}`} onClose={onClose}>
      {!t ? <div className="muted">Loading…</div> : (
        <>
          <Facts rows={[["Status now", t.status],
                        ["Runs", t.starts || t.ends ? `${t.starts ? fmtDate(t.starts) : "—"} – ${t.ends ? fmtDate(t.ends) : "—"}` : null],
                        ["Renews", t.renews && <Link className="linkish" to={`/contracts/${t.renews.id}`}>{t.renews.name}</Link>],
                        ["Renewed by", t.renewed_by && <Link className="linkish" to={`/contracts/${t.renewed_by.id}`}>{t.renewed_by.name}</Link>]]} />
          <div style={{ marginBottom: 12 }}><Link className="linkish" to={`/contracts/${t.id}`}>Open the contract →</Link></div>
          <h4 style={{ margin: "4px 0 12px" }}>Timeline</h4>
          <Timeline steps={t.steps.map(s => ({ ...s, detail: s.note ? `“${s.note}”` : null }))} />
          {/* Task 49.3: each draft change opens its before / after (recorded
              from 19 Nov 2026) on Change History. */}
          {t.steps.some(s => s.change_id) && (
            <>
              <h4 style={{ margin: "8px 0 8px" }}>What changed in each draft</h4>
              {t.steps.filter(s => s.change_id).map(s => (
                <div key={s.change_id} style={{ marginBottom: 4 }}>
                  <Link className="linkish" to={`/audit?tab=changes&change=${s.change_id}`}>
                    {s.at ? fmtDateTime(s.at) : "—"} · {s.by} — see before / after →</Link>
                </div>))}
            </>)}
        </>)}
    </SidePanel>
  );
}

// ── File Versions ──────────────────────────────────────────────────────────
export function FileVersions() {
  const ch = useChoices();
  const [f, setF] = useState<HistoryFilters>({});
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<number | null>(null);
  const list = useServerList<VersionPage["items"][number], VersionPage>(
    (p, n) => getVersions(f, p, n).then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify(f), PAGE);
  const c = list.extra?.counts;
  const set = (k: keyof HistoryFilters) => (v: string) => setF(x => ({ ...x, [k]: k === "period" ? v : v ? Number(v) : "" }));
  const save = async (id: number) => {
    setBusy(id);
    try { await downloadVersion(id); } catch (e) { showError(e, "Could not download this version"); } finally { setBusy(null); }
  };
  return (
    <>
      <Bar clear={!!(f.broker || f.programme || f.period)} onClear={() => setF({})}>
        {ch.brokers.length > 0 && <Pick label="Broker" value={f.broker ?? ""} onChange={set("broker")} all="All brokers"
          options={ch.brokers.map(b => ({ value: b.id, label: b.name }))} />}
        <Pick label="Programme" value={f.programme ?? ""} onChange={set("programme")} all="All programmes"
          options={ch.programmes.map(p => ({ value: p.id, label: p.name }))} />
        <label className="fbar-field">
          <span className="sub">Reporting period</span>
          <input type="month" className="fbar-date" value={f.period ?? ""} onChange={e => set("period")(e.target.value)} />
        </label>
      </Bar>
      <div style={tiles(3)}>
        <Tile icon={FileText} label="Files" value={c?.files} colour="#2563eb" info="Files brokers sent — a file and all its versions count once." />
        <Tile icon={FileStack} label="Versions" value={c?.versions} colour="#7c3aed" info="Every version kept, the first one included." />
        <Tile icon={RefreshCw} label="Sent again" value={c?.resent} colour="#d97706" info="Files that have more than one version." />
      </div>
      <div className="card">
        <div className="card-h"><h3>Every version of every file
          <InfoTip text={"A resend for the same month, or a correction, makes the next version.\nThe earlier one is always kept."} /></h3></div>
        {loading(list.loading, list.items.length, "No files match these filters.", failed) ?? (
          <>
            <div className="tbl-wrap"><table>
              <thead><tr><th style={L}>File</th><th style={L}>Broker</th><th style={L}>Reporting period</th>
                <th style={L}>Version</th><th style={L}>Received</th><th style={L}>How it arrived</th><th style={L}>Status</th><th /></tr></thead>
              <tbody>{list.items.map((r, i) => (
                <tr key={`${r.ref}-${r.version}-${i}`}>
                  <td style={{ ...L, whiteSpace: "normal" }}><b>{r.filename}</b><div className="sub">{r.programme}</div></td>
                  <td style={L}>{r.broker}</td>
                  <td style={L}>{period(r.period)}</td>
                  <td style={L}>{r.version} of {r.versions}</td>
                  <td style={{ ...L, whiteSpace: "nowrap" }}>{r.received_at ? fmtDateTime(r.received_at) : "—"}</td>
                  <td style={L}>{r.how}</td>
                  <td style={L}><span className={`badge ${r.current ? "b-ok" : "b-mut"}`}>{r.current ? "Current" : "Replaced"}</span>
                    {r.state && <div className="sub">{r.state}</div>}</td>
                  <td style={L}>{r.export_id != null && (
                    <button className="btn sm" disabled={busy === r.export_id} onClick={() => save(r.export_id!)}>
                      <Download size={13} /> {busy === r.export_id ? "…" : "Download"}</button>)}</td>
                </tr>))}</tbody>
            </table></div>
            <Pagination page={list.page} pageSize={PAGE} totalItems={list.total} pageCount={list.pageCount}
              onPageChange={list.setPage} noun="versions" />
          </>)}
      </div>
    </>
  );
}
