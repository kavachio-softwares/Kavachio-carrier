// Integrity & Retention — the Audit Logs tab for tasks 52.1 and 52.2
// (backend record_integrity.py, retention.py, integrity_routes.py).
// Record integrity: every audit record is fingerprinted and locked; a check
// works every fingerprint out again. Retention: how long each kind of record is
// kept, what the next daily run would delete, legal holds, and the deletion log.
// Carrier and Kavachio only.
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Fingerprint, ShieldCheck } from "lucide-react";
import {
  checkIntegrity, getDeletions, getIntegrity, getRetention, liftHold, placeHold, removeRetentionRule, saveRetentionRule,
  type CheckResult, type DeletionRow, type Hold, type IntegritySummary, type RetentionOverview, type RetentionRule,
} from "../api/integrity";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { showError } from "../components/ResultPopup";
import { Modal } from "../components/ui/Modal";
import { fmtDate, fmtDateTime } from "../utils/date";
import { AUDIT_PAGE, usePagedRows } from "../hooks/usePagedRows";
import { Tile, tiles } from "./AuditHistoryTabs";

const L: React.CSSProperties = { textAlign: "left" };
const field: React.CSSProperties = { display: "grid", gap: 4, fontSize: 13.5 };
const input: React.CSSProperties = { width: "100%", padding: "7px 9px", border: "1px solid var(--p-border-2, #d5d9e0)", borderRadius: 6, fontSize: 14 };
const n = (x: number | null | undefined) => (x ?? 0).toLocaleString();
const kept = (m: number | null) => !m ? "Kept for good"
  : m % 12 === 0 ? `${m / 12} year${m === 12 ? "" : "s"}` : `${m} month${m === 1 ? "" : "s"}`;

export function IntegrityRetention() {
  const [search] = useSearchParams();
  const [view, setView] = useState<"integrity" | "retention">(search.get("view") === "retention" ? "retention" : "integrity");
  return (
    <>
      <span className="seg" style={{ marginBottom: 14, display: "inline-flex" }}>
        <button type="button" className={view === "integrity" ? "on" : ""} onClick={() => setView("integrity")}>Record integrity</button>
        <button type="button" className={view === "retention" ? "on" : ""} onClick={() => setView("retention")}>Retention</button>
      </span>
      {view === "integrity" ? <Integrity /> : <Retention />}
    </>
  );
}

// ── 52.1 ───────────────────────────────────────────────────────────────────
function Integrity() {
  const [sum, setSum] = useState<IntegritySummary | null>(null);
  const [failed, setFailed] = useState(false);
  const [result, setResult] = useState<CheckResult | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => getIntegrity().then(d => { setSum(d); setFailed(false); }).catch(() => setFailed(true));
  useEffect(() => { load(); }, []);
  const problems = usePagedRows(result?.problems ?? [], AUDIT_PAGE, result?.at);

  async function run() {
    setBusy(true);
    try { setResult(await checkIntegrity()); load(); }
    catch (e) { showError(e, "Could not check the records"); }
    finally { setBusy(false); }
  }

  if (failed) return <div className="card"><div className="empty">Could not load this. Try again in a moment.</div></div>;
  if (!sum) return <div className="card"><div className="empty">Loading…</div></div>;
  if (!sum.ready) return <div className="card"><div className="empty">{sum.message}</div></div>;
  const last = sum.last_check;
  const good = result ? result.result === "All unchanged" : last?.result === "All unchanged";
  return (
    <>
      <div style={tiles(4)}>
        <Tile icon={Fingerprint} label="Records locked" value={sum.sealed?.toLocaleString()} colour="#2563eb"
          info="Audit records with a fingerprint. Each one is locked: nobody can change or delete it." />
        <Tile icon={ShieldCheck} label="Last checked" value={last ? fmtDate(last.at) : "Never"} colour="#0d9488"
          info={last ? `By ${last.by ?? "—"}, ${fmtDateTime(last.at)}.` : "No check has been run yet."} />
        <Tile icon={good ? CheckCircle2 : AlertTriangle} label="Last result" value={result?.result ?? last?.result ?? "—"}
          colour={good ? "#16a34a" : "#dc2626"} />
        <Tile icon={AlertTriangle} label="Problems found" value={result?.problems_total ?? last?.problems ?? "—"} colour="#d97706"
          info="Records changed or missing since they were locked." />
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-h">
          <h3>Record integrity
            <InfoTip text={`Every audit record gets a fingerprint once it is ${sum.settle_minutes ?? 5} minutes old (in the daily run, and before every check), and is then locked.\nEach fingerprint is linked to the one before it, so a changed record, a removed record or a changed fingerprint all show.\n"Check now" works every fingerprint out again from the records as they are today.`} /></h3>
          <button className="btn pri right" onClick={run} disabled={busy}>{busy ? "Checking… this can take a minute" : "Check now"}</button>
        </div>
        {result && (
          <div className={`note ${result.result === "All unchanged" ? "ok" : "crit"}`} style={{ margin: "12px 16px 0" }}>
            {result.result === "All unchanged"
              ? `All ${result.checked.toLocaleString()} records are unchanged, and the chain of fingerprints is whole.`
              : <>Problem found: {result.problems_total.toLocaleString()} record{result.problems_total === 1 ? "" : "s"} changed or missing
                {!result.chain_ok && <> · the chain of fingerprints is broken at fingerprint {result.broken_at}</>}.
                Tell Kavachio support straight away.</>}
          </div>)}
        <div className="tbl-wrap"><table>
          {result ? (<>
            <thead><tr><th style={L}>Kind of record</th><th style={L}>Checked</th><th style={L}>Unchanged</th><th style={L}>Changed</th>
              <th style={L}>Missing</th><th style={L}>Deleted by retention</th>
              <th style={L}>Not locked yet <InfoTip text="Written in the last few minutes. They are locked in the next run." /></th>
              <th style={L}>Result</th></tr></thead>
            <tbody>{result.kinds.map(k => (
              <tr key={k.table}>
                <td style={L}><b>{k.type}</b> <InfoTip text={k.info} /></td><td style={L}>{n(k.checked)}</td><td style={L}>{n(k.unchanged)}</td>
                <td style={L}>{k.changed ? <b style={{ color: "var(--p-crit, #dc2626)" }}>{n(k.changed)}</b> : "—"}</td>
                <td style={L}>{k.missing ? <b style={{ color: "var(--p-crit, #dc2626)" }}>{n(k.missing)}</b> : "—"}</td>
                <td style={L}>{k.gone ? n(k.gone) : "—"}</td><td style={L}>{k.not_sealed_yet ? n(k.not_sealed_yet) : "—"}</td>
                <td style={L}><span className={`badge ${k.result === "All unchanged" ? "b-ok" : "b-crit"}`}>{k.result}</span></td>
              </tr>))}</tbody>
          </>) : (<>
            <thead><tr><th style={L}>Kind of record</th><th style={L}>Records locked</th><th style={L}>Deleted by retention</th>
              <th style={L}>Last locked</th></tr></thead>
            <tbody>{(sum.kinds ?? []).map(k => (
              <tr key={k.table}>
                <td style={L}><b>{k.type}</b> <InfoTip text={k.info} /></td><td style={L}>{n(k.sealed - k.gone)}</td>
                <td style={L}>{k.gone ? n(k.gone) : "—"}</td><td style={L}>{fmtDateTime(k.last_sealed)}</td>
              </tr>))}</tbody>
          </>)}
        </table></div>
      </div>

      {result && result.problems.length > 0 && (
        <div className="card">
          <div className="card-h"><h3>Records with a problem ({result.problems_total.toLocaleString()})
            <InfoTip text={"Changed: the record is not what it was when it was locked.\nMissing: it was locked and is gone, and retention did not delete it.\nThe first 200 are listed."} /></h3></div>
          <div className="tbl-wrap"><table>
            <thead><tr><th style={L}>Kind of record</th><th style={L}>Record no.</th><th style={L}>Written</th><th style={L}>Locked</th><th style={L}>What is wrong</th></tr></thead>
            <tbody>{problems.rows.map(p => (
              <tr key={`${p.type}-${p.record}`}>
                <td style={L}>{p.type}</td><td style={L}>{p.record}</td><td style={L}>{fmtDateTime(p.created)}</td>
                <td style={L}>{fmtDateTime(p.sealed)}</td><td style={L}><span className="badge b-crit">{p.result}</span></td>
              </tr>))}</tbody>
          </table></div>
          <Pagination {...problems.pager} noun="records" />
        </div>)}
    </>
  );
}

// ── 52.2 ───────────────────────────────────────────────────────────────────
function Retention() {
  const [o, setO] = useState<RetentionOverview | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState<RetentionRule | "new" | null>(null);
  const [removing, setRemoving] = useState<RetentionRule | null>(null);
  const [holding, setHolding] = useState(false);
  const [lifting, setLifting] = useState<Hold | null>(null);
  const [logKey, setLogKey] = useState(0);
  const load = () => getRetention().then(d => { setO(d); setFailed(false); }).catch(() => setFailed(true));
  useEffect(() => { load(); }, []);
  const rules = usePagedRows(o?.rules ?? []);
  const holdRows = usePagedRows(o?.holds ?? []);

  if (failed) return <div className="card"><div className="empty">Could not load this. Try again in a moment.</div></div>;
  if (!o) return <div className="card"><div className="empty">Loading…</div></div>;
  if (!o.ready) return <div className="card"><div className="empty">{o.message}</div></div>;
  const perCarrier = (o.kinds ?? []).some(k => k.oldest !== null) || o.can_hold;
  const anyPeriod = (o.rules ?? []).some(r => r.months);
  const after = () => { load(); setLogKey(k => k + 1); };
  return (
    <>
      {!anyPeriod && <div className="note warn" style={{ marginBottom: 14 }}>No retention periods are set, so nothing is ever deleted.
        {o.can_set_periods ? " Add the periods legal has given below." : " Kavachio sets them once legal has given the periods."}</div>}
      {o.whole_held && <div className="note warn" style={{ marginBottom: 14 }}>A legal hold covers all your records, so nothing of yours is deleted while it is on.</div>}

      {perCarrier && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-h"><h3>How long your records are kept
            <InfoTip text={`${o.country ? `Your country is ${o.country}: a period set for ${o.country} applies, otherwise the one for everywhere.` : "Your address has no country, so the periods for everywhere apply."}\nRecords past their period are deleted in the daily run, unless a legal hold keeps them.\nFiles and bordereau data are not covered here.`} /></h3></div>
          <div className="tbl-wrap"><table>
            <thead><tr><th style={L}>Kind of record</th><th style={L}>Kept for</th><th style={L}>Set for</th><th style={L}>Oldest record</th>
              <th style={L}>Next run deletes</th><th style={L}>Kept by a hold</th></tr></thead>
            <tbody>{(o.kinds ?? []).map(k => (
              <tr key={k.key}>
                <td style={L}><b>{k.type}</b> <InfoTip text={k.info} /></td><td style={L}>{kept(k.months)}</td>
                <td style={L}>{k.applies ?? "—"}</td><td style={L}>{fmtDate(k.oldest)}</td>
                <td style={L}>{k.months ? (k.next_count ? <b>{n(k.next_count)} records</b> : "Nothing") : "Nothing"}
                  {k.cutoff && <div className="sub">written before {fmtDate(k.cutoff)}</div>}</td>
                <td style={L}>{k.held ? n(k.held) : "—"}</td>
              </tr>))}</tbody>
          </table></div>
        </div>)}

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-h"><h3>Retention periods
          <InfoTip text={"How many months each kind of record is kept, for one country or everywhere.\nA country's own period wins over the one for everywhere. No period means the records are kept for good.\nKavachio sets these from what legal gives."} /></h3>
          {o.can_set_periods && <button className="btn pri right" onClick={() => setEditing("new")}>Add a period</button>}</div>
        {(o.rules ?? []).length === 0 ? <div className="empty">No periods set yet.</div> : (
          <div className="tbl-wrap"><table>
            <thead><tr><th style={L}>Kind of record</th><th style={L}>Country</th><th style={L}>Kept for</th><th style={L}>Note</th>
              <th style={L}>Changed</th>{o.can_set_periods && <th />}</tr></thead>
            <tbody>{rules.rows.map(r => (
              <tr key={r.id}>
                <td style={L}><b>{r.type}</b></td><td style={L}>{r.country}</td><td style={L}>{kept(r.months)}</td>
                <td style={{ ...L, whiteSpace: "normal", minWidth: 200 }}>{r.note || "—"}</td><td style={L}>{fmtDate(r.updated_at)}</td>
                {o.can_set_periods && <td style={{ whiteSpace: "nowrap" }}>
                  <button className="btn sm" onClick={() => setEditing(r)}>Change</button>{" "}
                  <button className="btn sm" onClick={() => setRemoving(r)}>Remove</button></td>}
              </tr>))}</tbody>
          </table></div>)}
        <Pagination {...rules.pager} noun="periods" />
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-h"><h3>Legal holds
          <InfoTip text={"A hold stops records being deleted, whatever their period, until it is lifted.\nA hold on everything keeps all of a carrier's records.\nA hold on one broker keeps its exception decisions, its audits, and what its own people did and when they signed in."} /></h3>
          {o.can_hold && <button className="btn pri right" onClick={() => setHolding(true)}>Place a hold</button>}</div>
        {(o.holds ?? []).length === 0 ? <div className="empty">No holds.</div> : (
          <div className="tbl-wrap"><table>
            <thead><tr>{!o.can_hold && <th style={L}>Carrier</th>}<th style={L}>Keeps</th><th style={L}>Why</th><th style={L}>Placed</th>
              <th style={L}>Status</th>{o.can_hold && <th />}</tr></thead>
            <tbody>{holdRows.rows.map(h => (
              <tr key={h.id}>
                {!o.can_hold && <td style={L}>{h.carrier}</td>}
                <td style={L}><b>{h.covers}</b></td><td style={{ ...L, whiteSpace: "normal", minWidth: 220 }}>{h.reason}</td>
                <td style={L}>{fmtDate(h.placed_at)}<div className="sub">{h.placed_by ?? "—"}</div></td>
                <td style={L}>{h.on ? <span className="badge b-warn">On</span>
                  : <><span className="badge b-mut">Lifted {fmtDate(h.lifted_at)}</span><div className="sub" style={{ whiteSpace: "normal" }}>{h.lifted_by}: {h.lift_note}</div></>}</td>
                {o.can_hold && <td>{h.on && <button className="btn sm" onClick={() => setLifting(h)}>Lift</button>}</td>}
              </tr>))}</tbody>
          </table></div>)}
        <Pagination {...holdRows.pager} noun="holds" />
      </div>

      <DeletionLog key={logKey} />

      {editing && <RuleForm rule={editing === "new" ? null : editing} types={o.types ?? []} onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); after(); }} />}
      {removing && <Confirm title="Remove this retention period?" danger
        text={`${removing.type}, ${removing.country}. Without it, the period for everywhere applies, or the records are kept for good.`}
        go={() => removeRetentionRule(removing.id)} onClose={() => setRemoving(null)} onDone={() => { setRemoving(null); after(); }} />}
      {holding && <HoldForm brokers={o.brokers ?? []} onClose={() => setHolding(false)} onSaved={() => { setHolding(false); after(); }} />}
      {lifting && <LiftForm hold={lifting} onClose={() => setLifting(null)} onSaved={() => { setLifting(null); after(); }} />}
    </>
  );
}

function DeletionLog() {
  const PAGE = AUDIT_PAGE;
  const [page, setPage] = useState(1);
  const [d, setD] = useState<{ items: DeletionRow[]; total: number } | null>(null);
  useEffect(() => { getDeletions(page, PAGE).then(setD).catch(() => setD({ items: [], total: 0 })); }, [page]);
  return (
    <div className="card">
      <div className="card-h"><h3>Deleted by retention{d?.total ? ` (${d.total})` : ""}
        <InfoTip text={"Every daily run that deleted records, or held some back.\nThis list itself can never be changed or deleted."} /></h3></div>
      {!d ? <div className="empty">Loading…</div> : d.items.length === 0 ? <div className="empty">Nothing has been deleted.</div> : (
        <>
          <div className="tbl-wrap"><table>
            <thead><tr><th style={L}>Date</th><th style={L}>Carrier</th><th style={L}>Kind of record</th><th style={L}>Kept for</th>
              <th style={L}>Deleted</th><th style={L}>Kept by a hold</th><th style={L}>Written between</th></tr></thead>
            <tbody>{d.items.map(x => (
              <tr key={x.id}>
                <td style={L}>{fmtDateTime(x.at)}</td><td style={L}>{x.carrier}</td><td style={L}>{x.type}</td>
                <td style={L}>{kept(x.months)}<div className="sub">{x.country}</div></td>
                <td style={L}><b>{n(x.deleted)}</b></td><td style={L}>{x.held ? n(x.held) : "—"}</td>
                <td style={L}>{x.oldest ? `${fmtDate(x.oldest)} – ${fmtDate(x.newest ?? x.oldest)}` : "—"}</td>
              </tr>))}</tbody>
          </table></div>
          <Pagination page={page} pageSize={PAGE} totalItems={d.total} pageCount={Math.max(1, Math.ceil(d.total / PAGE))}
            onPageChange={setPage} noun="runs" />
        </>)}
    </div>
  );
}

// ── the popups ─────────────────────────────────────────────────────────────
function errorsOf(e: unknown): Record<string, string> {
  const d = (e as { response?: { data?: { detail?: { errors?: Record<string, string> } } } })?.response?.data?.detail;
  return d && typeof d === "object" && d.errors ? d.errors : {};
}

function RuleForm({ rule, types, onClose, onSaved }: {
  rule: RetentionRule | null; types: { id: string; name: string }[]; onClose: () => void; onSaved: () => void;
}) {
  const [type, setType] = useState(rule?.key ?? types[0]?.id ?? "");
  const [country, setCountry] = useState(rule && rule.country !== "Everywhere" ? rule.country : "");
  const [months, setMonths] = useState(rule?.months ? String(rule.months) : "");
  const [note, setNote] = useState(rule?.note ?? "");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true); setErrors({});
    try { await saveRetentionRule({ record_type: type, country, months: months ? Number(months) : null, note }); onSaved(); }
    catch (e) { const er = errorsOf(e); if (Object.keys(er).length) setErrors(er); else showError(e, "Could not save the period"); }
    finally { setBusy(false); }
  }
  return (
    <Modal open size="md" title={rule ? "Change a retention period" : "Add a retention period"} onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn pri" onClick={save} disabled={busy || !type}>{busy ? "Saving…" : "Save"}</button></div>}>
      <div style={{ display: "grid", gap: 14 }}>
        <label style={field}>Kind of record
          <select style={input} value={type} disabled={!!rule} onChange={e => setType(e.target.value)}>
            {types.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
          {errors.record_type && <span className="sub" style={{ color: "var(--p-crit)" }}>{errors.record_type}</span>}</label>
        <label style={field}><span>Country <span className="sub">(leave empty for everywhere)</span></span>
          <input style={input} value={country} disabled={!!rule} onChange={e => setCountry(e.target.value)} placeholder="e.g. India" /></label>
        <label style={field}><span>Keep for (months) <span className="sub">(leave empty to keep for good)</span></span>
          <input style={input} type="number" min={1} max={1200} value={months} onChange={e => setMonths(e.target.value)} placeholder="e.g. 84 for 7 years" />
          {errors.months && <span className="sub" style={{ color: "var(--p-crit)" }}>{errors.months}</span>}</label>
        <label style={field}><span>Note <span className="sub">(optional, e.g. the law or legal's advice)</span></span>
          <textarea style={{ ...input, minHeight: 60 }} value={note} onChange={e => setNote(e.target.value)} /></label>
        {months && <div className="note warn">Records older than this are deleted in the next daily run, for every carrier this applies to, unless a legal hold keeps them. This cannot be undone.</div>}
      </div>
    </Modal>
  );
}

function HoldForm({ brokers, onClose, onSaved }: { brokers: { id: number; name: string }[]; onClose: () => void; onSaved: () => void }) {
  const [broker, setBroker] = useState<number | "">("");
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true); setErrors({});
    try { await placeHold({ reason, broker_party_id: broker || null }); onSaved(); }
    catch (e) { const er = errorsOf(e); if (Object.keys(er).length) setErrors(er); else showError(e, "Could not place the hold"); }
    finally { setBusy(false); }
  }
  return (
    <Modal open size="md" title="Place a legal hold" onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn pri" onClick={save} disabled={busy || !reason.trim()}>{busy ? "Placing…" : "Place hold"}</button></div>}>
      <div style={{ display: "grid", gap: 14 }}>
        <div className="sub">While the hold is on, none of the records it covers are deleted, whatever their retention period.</div>
        <label style={field}>Keep
          <select style={input} value={broker} onChange={e => setBroker(e.target.value ? Number(e.target.value) : "")}>
            <option value="">Everything of ours</option>{brokers.map(b => <option key={b.id} value={b.id}>Only {b.name}'s records</option>)}</select>
          {errors.broker_party_id && <span className="sub" style={{ color: "var(--p-crit)" }}>{errors.broker_party_id}</span>}</label>
        <label style={field}>Why
          <textarea style={{ ...input, minHeight: 70 }} value={reason} onChange={e => setReason(e.target.value)} placeholder="e.g. Court order 12/2026, dispute with the broker" />
          {errors.reason && <span className="sub" style={{ color: "var(--p-crit)" }}>{errors.reason}</span>}</label>
      </div>
    </Modal>
  );
}

function LiftForm({ hold, onClose, onSaved }: { hold: Hold; onClose: () => void; onSaved: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try { await liftHold(hold.id, note); onSaved(); }
    catch (e) { showError(e, "Could not lift the hold"); }
    finally { setBusy(false); }
  }
  return (
    <Modal open size="md" title="Lift this legal hold?" onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn pri" onClick={save} disabled={busy || !note.trim()}>{busy ? "Lifting…" : "Lift hold"}</button></div>}>
      <div style={{ display: "grid", gap: 14 }}>
        <div><b>{hold.covers}</b><div className="sub">{hold.reason}</div></div>
        <div className="note warn">Once lifted, records past their retention period are deleted in the next daily run.</div>
        <label style={field}>Why can it be lifted?
          <textarea style={{ ...input, minHeight: 60 }} value={note} onChange={e => setNote(e.target.value)} placeholder="e.g. Case closed on 3 Dec 2026" /></label>
      </div>
    </Modal>
  );
}

function Confirm({ title, text, go, onClose, onDone, danger }: {
  title: string; text: string; go: () => Promise<unknown>; onClose: () => void; onDone: () => void; danger?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Modal open size="md" title={title} onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className={`btn ${danger ? "danger" : "pri"}`} disabled={busy}
          onClick={async () => { setBusy(true); try { await go(); onDone(); } catch (e) { showError(e); } finally { setBusy(false); } }}>
          {busy ? "Working…" : "Remove"}</button></div>}>
      <div>{text}</div>
    </Modal>
  );
}
