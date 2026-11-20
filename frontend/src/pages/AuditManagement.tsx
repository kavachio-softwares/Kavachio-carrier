// Audit Management (backend audit_management.py, 53.1 / 53.2) — the Audits tab
// of Audit Logs: audits the carrier records against its brokers and their
// contracts, each finding followed until it is closed. Also the finding card
// the broker's "Audit Findings" page shows.
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { CheckCircle2, ClipboardCheck, Clock, FileSearch, Paperclip, Plus } from "lucide-react";
import {
  addAudit, addFinding, carrierFile, carrierReply, closeAudit, editAudit, editFinding, getAudit, getAudits,
  moveFinding, type Audit, type AuditInput, type AuditList, type Finding, type FindingInput,
} from "../api/audits";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { AUDIT_PAGE, usePagedRows } from "../hooks/usePagedRows";
import { showDone, showError } from "../components/ResultPopup";
import { Modal } from "../components/ui/Modal";
import { fmtDate, fmtDateTime } from "../utils/date";
import { Facts, SidePanel, Tile, tiles } from "./AuditHistoryTabs";

const L: React.CSSProperties = { textAlign: "left" };
const STATUS_BADGE: Record<string, string> = { planned: "b-mut", in_progress: "b-info", closed: "b-ok", open: "b-warn" };
const SEV_BADGE: Record<string, string> = { low: "b-mut", medium: "b-info", high: "b-warn", critical: "b-crit" };
const field: React.CSSProperties = { display: "grid", gap: 4, fontSize: 13.5 };
const input: React.CSSProperties = { width: "100%", padding: "7px 9px", border: "1px solid var(--p-border-2, #d5d9e0)", borderRadius: 6, fontSize: 14 };
const err = (e: Record<string, string>, k: string) => e[k] && <span style={{ color: "var(--p-crit, #dc2626)", fontSize: 12.5 }}>{e[k]}</span>;
const fieldErrors = (e: unknown): Record<string, string> =>
  ((e as { response?: { data?: { detail?: { errors?: Record<string, string> } } } })?.response?.data?.detail?.errors) ?? {};

export function AuditsTab() {
  const [data, setData] = useState<AuditList | null>(null);
  const [failed, setFailed] = useState(false);
  const [broker, setBroker] = useState<number | "">("");
  const [status, setStatus] = useState("");
  // The dashboard card and the "broker replied" email open one audit: ?audit=12.
  const [search] = useSearchParams();
  const [open, setOpen] = useState<number | null>(() => Number(search.get("audit")) || null);
  const [form, setForm] = useState<Audit | "new" | null>(null);
  const load = () => getAudits({ broker, status }).then(d => { setData(d); setFailed(false); }).catch(() => setFailed(true));
  useEffect(() => { load(); }, [broker, status]); // eslint-disable-line react-hooks/exhaustive-deps
  const paged = usePagedRows(data?.audits ?? [], AUDIT_PAGE, `${broker}|${status}`);

  if (failed) return <div className="card"><div className="empty">Could not load the audits. Try again in a moment.</div></div>;
  if (!data) return <div className="card"><div className="empty">Loading…</div></div>;
  if (!data.ready) return <div className="card"><div className="empty">{data.message}</div></div>;
  const c = data.counts;
  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none", alignItems: "flex-end" }}>
          <label className="fbar-field"><span className="sub">Broker</span>
            <select className="fbar-select" value={broker} onChange={e => setBroker(e.target.value ? Number(e.target.value) : "")}>
              <option value="">All brokers</option>
              {data.form.brokers.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select></label>
          <label className="fbar-field"><span className="sub">Status</span>
            <select className="fbar-select" value={status} onChange={e => setStatus(e.target.value)}>
              <option value="">Any status</option><option value="planned">Planned</option>
              <option value="in_progress">In progress</option><option value="closed">Closed</option>
            </select></label>
          <button className="btn pri sm" style={{ marginLeft: "auto" }} onClick={() => setForm("new")}><Plus size={14} /> New audit</button>
        </div>
      </div>
      <div style={tiles(4)}>
        <Tile icon={ClipboardCheck} label="Total audits" value={c.total} colour="#7c3aed" info="Audits that match the filters." />
        <Tile icon={Clock} label="In progress" value={c.in_progress} colour="#2563eb" info="Audits that have started and are not closed yet." />
        <Tile icon={FileSearch} label="Open findings" value={c.open_findings} colour="#d97706" info="Findings not closed yet, across these audits." />
        <Tile icon={CheckCircle2} label="Closed" value={c.closed} colour="#16a34a" info="Audits closed — every finding was closed first." />
      </div>
      <div className="card">
        <div className="card-h"><h3>Audits
          <InfoTip text={"Audits of your brokers and their contracts.\nClick an audit to see its findings, add one or close it."} /></h3></div>
        {data.audits.length === 0 ? (
          <div className="empty">No audits yet. Click <b>New audit</b> to record one.</div>
        ) : (
          <div className="tbl-wrap"><table>
            <thead><tr><th style={L}>Audit no.</th><th style={L}>Audit name</th><th style={L}>Broker</th><th style={L}>Contract</th>
              <th style={L}>Assigned to</th><th style={L}>Due date</th><th style={L}>Findings</th><th style={L}>Status</th></tr></thead>
            <tbody>{paged.rows.map(a => (
              <tr key={a.id} onClick={() => setOpen(a.id)} style={{ cursor: "pointer" }}>
                <td style={L} className="muted">{a.ref}</td>
                <td style={L}><b>{a.name}</b><div className="sub">{a.type_label}</div></td>
                <td style={L}>{a.broker ?? "—"}</td>
                <td style={{ ...L, whiteSpace: "normal" }}>{a.contract ?? "—"}</td>
                <td style={L}>{a.assigned_to ?? "—"}</td>
                <td style={L}>{a.due_date ? fmtDate(a.due_date) : "—"}
                  {a.overdue && <div><span className="badge b-crit">Overdue</span></div>}</td>
                <td style={L}>{a.findings_open ? <b>{a.findings_open} open</b> : a.findings_total ? "All closed" : "None"}</td>
                <td style={L}><span className={`badge ${STATUS_BADGE[a.status]}`}>{a.status_label}</span></td>
              </tr>))}</tbody>
          </table></div>)}
        <Pagination {...paged.pager} noun="audits" />
      </div>
      <AuditPanel id={open} onClose={() => setOpen(null)} onChanged={load} onEdit={a => setForm(a)} />
      {form && <AuditForm list={data} audit={form === "new" ? null : form} onClose={() => setForm(null)}
        onSaved={a => { setForm(null); load(); setOpen(a.id); }} />}
    </>
  );
}

// ── the audit form ─────────────────────────────────────────────────────────
function AuditForm({ list, audit, onClose, onSaved }: {
  list: AuditList; audit: Audit | null; onClose: () => void; onSaved: (a: Audit) => void;
}) {
  const [v, setV] = useState<AuditInput>({
    name: audit?.name ?? "", type: audit?.type ?? "annual", broker_party_id: audit?.broker_id ?? "",
    contract_id: audit?.contract_id ?? "", scope: audit?.scope ?? "", assigned_to: audit?.assigned_to ?? "",
    start_date: audit?.start_date ?? "", due_date: audit?.due_date ?? "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const contracts = useMemo(() => list.form.contracts.filter(c => c.broker_id === v.broker_party_id), [list, v.broker_party_id]);
  const set = (k: keyof AuditInput) => (e: { target: { value: string } }) =>
    setV(x => ({ ...x, [k]: ["broker_party_id", "contract_id"].includes(k) ? (e.target.value ? Number(e.target.value) : "") : e.target.value,
                 ...(k === "broker_party_id" ? { contract_id: "" } : {}) }));
  async function save() {
    setBusy(true); setErrors({});
    try { onSaved(audit ? await editAudit(audit.id, v) : await addAudit(v)); showDone(audit ? "Audit saved." : "Audit recorded."); }
    catch (e) { const f = fieldErrors(e); if (Object.keys(f).length) setErrors(f); else showError(e, "Could not save the audit"); }
    finally { setBusy(false); }
  }
  return (
    <Modal open size="md" title={audit ? "Change the audit" : "New audit"} onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn pri" onClick={save} disabled={busy}>{busy ? "Saving…" : audit ? "Save" : "Record audit"}</button></div>}>
      <div style={{ display: "grid", gap: 14 }}>
        <label style={field}>Audit name<input style={input} value={v.name} onChange={set("name")} placeholder="e.g. Annual audit 2026" />{err(errors, "name")}</label>
        <label style={field}>Type<select style={input} value={v.type} onChange={set("type")}>
          {list.types.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}</select>{err(errors, "type")}</label>
        <label style={field}>Broker<select style={input} value={v.broker_party_id} onChange={set("broker_party_id")} disabled={!!audit?.findings_total}>
          <option value="">Pick a broker…</option>
          {list.form.brokers.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}</select>{err(errors, "broker_party_id")}</label>
        <label style={field}><span>Contract <span className="sub">(optional)</span></span>
          <select style={input} value={v.contract_id} onChange={set("contract_id")} disabled={!v.broker_party_id}>
            <option value="">{v.broker_party_id ? "All of this broker's business" : "Pick a broker first"}</option>
            {contracts.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select>{err(errors, "contract_id")}</label>
        <label style={field}><span>What it covers <span className="sub">(optional)</span></span>
          <textarea style={input} rows={2} value={v.scope} onChange={set("scope")} placeholder="e.g. Premium figures and on-time filing, Jan–Oct 2026" /></label>
        <label style={field}><span>Assigned to <span className="sub">(optional)</span></span>
          <input style={input} value={v.assigned_to} onChange={set("assigned_to")} placeholder="Who runs the audit" /></label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label style={field}>Start date<input type="date" style={input} value={v.start_date} onChange={set("start_date")} />{err(errors, "start_date")}</label>
          <label style={field}>Due date<input type="date" style={input} value={v.due_date} onChange={set("due_date")} />{err(errors, "due_date")}</label>
        </div>
      </div>
    </Modal>
  );
}

// ── the audit's details ────────────────────────────────────────────────────
function AuditPanel({ id, onClose, onChanged, onEdit }: {
  id: number | null; onClose: () => void; onChanged: () => void; onEdit: (a: Audit) => void;
}) {
  const [a, setA] = useState<Audit | null>(null);
  const [adding, setAdding] = useState<Finding | "new" | null>(null);
  const [closing, setClosing] = useState(false);
  useEffect(() => {
    setA(null);
    if (id != null) getAudit(id).then(setA).catch(e => { showError(e, "Could not open this audit"); onClose(); });
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps
  const changed = (x: Audit) => { setA(x); onChanged(); };
  return (
    <SidePanel open={id != null} title={a ? `${a.ref} · ${a.name}` : "Audit"} sub={a && `${a.type_label} · ${a.broker ?? "—"}`} onClose={onClose}>
      {!a ? <div className="muted">Loading…</div> : (
        <>
          <Facts rows={[["Status", <span className={`badge ${STATUS_BADGE[a.status]}`}>{a.status_label}</span>],
                        ["Broker", a.broker], ["Contract", a.contract ?? "All of this broker's business"],
                        ["What it covers", a.scope], ["Assigned to", a.assigned_to],
                        ["Dates", `${a.start_date ? fmtDate(a.start_date) : "—"} – ${a.due_date ? fmtDate(a.due_date) : "—"}`],
                        ...(a.status === "closed" ? [["Closed", `${a.closed_at ? fmtDate(a.closed_at) : ""}${a.closing_note ? ` · ${a.closing_note}` : ""}`] as [string, string]] : [])]} />
          {a.status !== "closed" && (
            <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
              <button className="btn pri sm" onClick={() => setAdding("new")}><Plus size={14} /> Add finding</button>
              <button className="btn sm" onClick={() => onEdit(a)}>Change audit</button>
              <button className="btn sm" onClick={() => setClosing(true)}>Close audit</button>
            </div>)}
          <h4 style={{ margin: "4px 0 10px" }}>Findings ({a.findings_total})</h4>
          {!a.findings?.length ? <div className="empty">No findings yet.</div>
            : a.findings.map(f => <FindingCard key={f.id} f={f} side="carrier" audit={a} onChanged={changed}
                                               onEdit={() => setAdding(f)} />)}
          {adding && <FindingForm audit={a} finding={adding === "new" ? null : adding} onClose={() => setAdding(null)}
                                  onSaved={x => { setAdding(null); changed(x); }} />}
          {closing && <CloseAudit audit={a} onClose={() => setClosing(false)} onSaved={x => { setClosing(false); changed(x); }} />}
        </>)}
    </SidePanel>
  );
}

function CloseAudit({ audit, onClose, onSaved }: { audit: Audit; onClose: () => void; onSaved: (a: Audit) => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const open = audit.findings_open;
  return (
    <Modal open size="sm" title="Close this audit?" onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn pri" disabled={busy || open > 0} onClick={async () => {
          setBusy(true);
          try { onSaved(await closeAudit(audit.id, note)); showDone("Audit closed."); }
          catch (e) { showError(e, "Could not close the audit"); } finally { setBusy(false); }
        }}>Close audit</button></div>}>
      {open > 0 ? <p>Close its {open} open {open === 1 ? "finding" : "findings"} first.</p> : (
        <label style={field}><span>Closing note <span className="sub">(optional)</span></span>
          <textarea style={input} rows={3} value={note} onChange={e => setNote(e.target.value)} placeholder="e.g. All findings fixed and checked." /></label>)}
    </Modal>
  );
}

function FindingForm({ audit, finding, onClose, onSaved }: {
  audit: Audit; finding: Finding | null; onClose: () => void; onSaved: (a: Audit) => void;
}) {
  const [v, setV] = useState<FindingInput>({ title: finding?.title ?? "", severity: finding?.severity ?? "medium",
    action: finding?.action ?? "", owner: finding?.owner ?? "", due_date: finding?.due_date ?? "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const set = (k: keyof FindingInput) => (e: { target: { value: string } }) => setV(x => ({ ...x, [k]: e.target.value }));
  async function save() {
    setBusy(true); setErrors({});
    try { onSaved(finding ? await editFinding(audit.id, finding.id, v) : await addFinding(audit.id, v)); showDone(finding ? "Finding saved." : "Finding added."); }
    catch (e) { const f = fieldErrors(e); if (Object.keys(f).length) setErrors(f); else showError(e, "Could not save the finding"); }
    finally { setBusy(false); }
  }
  return (
    <Modal open size="md" title={finding ? "Change the finding" : "Add a finding"} onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn pri" onClick={save} disabled={busy}>{busy ? "Saving…" : finding ? "Save" : "Add finding"}</button></div>}>
      <div style={{ display: "grid", gap: 14 }}>
        <label style={field}>What was found<textarea style={input} rows={2} value={v.title} onChange={set("title")} placeholder="e.g. Two October files arrived late" />{err(errors, "title")}</label>
        <label style={field}><span>How serious <InfoTip text={"Low: a small point to tidy up.\nMedium: should be fixed.\nHigh: must be fixed soon.\nCritical: a serious breach — fix now. Makes the broker High risk until closed."} /></span>
          <select style={input} value={v.severity} onChange={set("severity")}>
            <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="critical">Critical</option>
          </select>{err(errors, "severity")}</label>
        <label style={field}><span>What must be done <span className="sub">(optional)</span></span>
          <textarea style={input} rows={2} value={v.action} onChange={set("action")} placeholder="e.g. Send each month's file by the 10th" /></label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label style={field}><span>Owner <span className="sub">(optional)</span></span><input style={input} value={v.owner} onChange={set("owner")} placeholder="Who fixes it" /></label>
          <label style={field}><span>Due date <span className="sub">(optional)</span></span><input type="date" style={input} value={v.due_date} onChange={set("due_date")} />{err(errors, "due_date")}</label>
        </div>
        <div className="sub">{audit.broker ?? "The broker"} will see this finding and can reply and add evidence.</div>
      </div>
    </Modal>
  );
}

// ── one finding, for either side ───────────────────────────────────────────
export function FindingCard({ f, side, audit, onChanged, onEdit, onReply, onFile }: {
  f: Finding; side: "carrier" | "broker"; audit?: Audit; onChanged?: (a: Audit) => void; onEdit?: () => void;
  onReply?: (note: string, file: File | null) => Promise<void>; onFile?: (eventId: number, name: string) => void;
}) {
  const [note, setNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [closing, setClosing] = useState(false);
  const [why, setWhy] = useState("");
  const [showAll, setShowAll] = useState(false);
  const closed = f.status === "closed";
  const send = async () => {
    setBusy(true);
    try {
      if (onReply) await onReply(note, file);
      else if (audit && onChanged) onChanged(await carrierReply(audit.id, f.id, note, file));
      setNote(""); setFile(null); showDone("Added.");
    } catch (e) { showError(e, "Could not add that"); } finally { setBusy(false); }
  };
  const move = async (status: string, text = "") => {
    if (!audit || !onChanged) return;
    setBusy(true);
    try { onChanged(await moveFinding(audit.id, f.id, status, text)); setClosing(false); setWhy(""); }
    catch (e) { showError(e, "Could not change the finding"); } finally { setBusy(false); }
  };
  const open = (eid: number, name: string) => onFile ? onFile(eid, name)
    : audit && carrierFile(audit.id, f.id, eid, name).catch(e => showError(e, "Could not download the file"));
  const steps = showAll ? f.history : f.history.slice(-4);
  return (
    <div className="card" style={{ padding: 14, marginBottom: 12, borderLeft: `3px solid ${f.severity === "critical" ? "#dc2626" : f.severity === "high" ? "#d97706" : "#cbd5e1"}` }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <span className={`badge ${SEV_BADGE[f.severity]}`}>{f.severity_label}</span>
        <span className={`badge ${STATUS_BADGE[f.status]}`}>{f.status_label}</span>
        {f.overdue && <span className="badge b-crit">Overdue</span>}
        {side === "broker" && f.carrier && <span className="sub">{f.carrier} · {f.audit_ref} {f.audit}</span>}
      </div>
      <div style={{ fontWeight: 600, margin: "8px 0 4px", whiteSpace: "pre-wrap" }}>{f.title}</div>
      {f.action && <div style={{ fontSize: 13.5 }}><span className="sub">What must be done: </span>{f.action}</div>}
      <div className="sub" style={{ marginTop: 2 }}>
        {[f.owner && `Owner: ${f.owner}`, f.due_date && `Due ${fmtDate(f.due_date)}`, side === "broker" && f.contract && `Contract: ${f.contract}`]
          .filter(Boolean).join(" · ")}</div>
      {closed && f.resolution && <div className="note ok" style={{ marginTop: 8 }}>How it was fixed: {f.resolution}</div>}

      <div style={{ marginTop: 10, borderTop: "1px solid var(--p-border)", paddingTop: 8 }}>
        {f.history.length > 4 && <button className="linkish" style={{ fontSize: 12.5 }} onClick={() => setShowAll(x => !x)}>
          {showAll ? "Show the latest only" : `Show all ${f.history.length} steps`}</button>}
        {steps.map(h => (
          <div key={h.id} style={{ fontSize: 13, padding: "4px 0" }}>
            <b>{h.title}</b>{h.to && h.kind !== "created" ? <span className="sub"> → {h.to}</span> : null}
            <span className="sub"> · {h.by} · {h.at ? fmtDateTime(h.at) : ""}</span>
            {h.note && <div style={{ whiteSpace: "pre-wrap" }}>{h.note}</div>}
            {h.file && <button className="linkish" style={{ display: "inline-flex", gap: 4, alignItems: "center", fontSize: 12.5 }}
              onClick={() => open(h.id, h.file!)}><Paperclip size={12} /> {h.file}</button>}
          </div>))}
      </div>

      {!closed && (
        <div style={{ marginTop: 8, display: "grid", gap: 8 }}>
          <textarea style={input} rows={2} value={note} onChange={e => setNote(e.target.value)}
            placeholder={side === "broker" ? "Reply to the carrier — what you did or will do" : "Add a note"} />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <label className="btn sm" style={{ cursor: "pointer" }}><Paperclip size={13} /> {file ? file.name : "Add evidence"}
              <input type="file" hidden onChange={e => setFile(e.target.files?.[0] ?? null)} /></label>
            <button className="btn pri sm" disabled={busy || (!note.trim() && !file)} onClick={send}>{side === "broker" ? "Send reply" : "Add note"}</button>
            {side === "carrier" && (<>
              {f.status === "open" && <button className="btn sm" disabled={busy} onClick={() => move("in_progress")}>Mark in progress</button>}
              <button className="btn sm" disabled={busy} onClick={() => setClosing(true)}><CheckCircle2 size={13} /> Close finding</button>
              {onEdit && <button className="btn sm ghost" onClick={onEdit}>Change</button>}
            </>)}
          </div>
        </div>)}
      {closed && side === "carrier" && <button className="btn sm" style={{ marginTop: 8 }} disabled={busy} onClick={() => move("open")}>Reopen finding</button>}
      {closing && (
        <Modal open size="sm" title="Close this finding?" onClose={() => !busy && setClosing(false)}
          footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
            <button className="btn" onClick={() => setClosing(false)} disabled={busy}>Cancel</button>
            <button className="btn pri" disabled={busy || !why.trim()} onClick={() => move("closed", why)}>Close finding</button></div>}>
          <label style={field}>How was it fixed?<textarea style={input} rows={3} value={why} onChange={e => setWhy(e.target.value)}
            placeholder="e.g. Checked November and December: both files on time." /></label>
        </Modal>)}
    </div>
  );
}
