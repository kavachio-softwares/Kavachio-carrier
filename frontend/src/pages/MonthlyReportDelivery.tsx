// MOPR 42.2 — how the Operational Report is sent, laid out as the Executive
// Report's (ExecutiveReport.tsx): "Send it automatically" in a popup from the
// Schedule button, and every report sent in a Sent reports drawer. The
// Operational Report also picks the hour and whose clock it goes by, and
// whether logins get the file or a link (backend report_delivery.py).
import { useEffect, useState } from "react";
import { FileSpreadsheet, FileText } from "lucide-react";
import {
  clearMonthlySends, downloadSentReport, getDelivery, saveDelivery,
  type DeliverySettings, type DeliveryView, type SentReport,
} from "../api/reports";
import { showDone, showError } from "../components/ResultPopup";
import { FormatPick, lines, Recipients } from "../components/SendByEmail";
import { Modal } from "../components/ui/Modal";
import { fmtDay, fmtHour, ordinal, periodTitle, plural, WEEKDAYS } from "../utils/reportFormat";

type Cadence = DeliverySettings["cadence"];
type FileFormat = DeliverySettings["file_format"];

export const FORMATS: { key: FileFormat; label: string }[] = [
  { key: "pdf", label: "PDF" }, { key: "excel", label: "Excel" }, { key: "both", label: "Both" }];
const CADENCES: { key: Cadence; label: string }[] = [
  { key: "day", label: "Daily" }, { key: "week", label: "Weekly" }, { key: "month", label: "Monthly" }];
const HOURS = Array.from({ length: 24 }, (_, h) => h);
/** Sent reports per page of the drawer; "Show older" fetches the next. */
export const SENT_PAGE = 30;

/** "UK time", from the clocks the server offers. */
export const tzLabel = (view: DeliveryView, key: string) =>
  view.time_zones?.find(z => z.key === key)?.label ?? key;

/** A sent report's period in words: "October 2026", "Week of 9 Nov 2026"… */
const sentTitle = (s: SentReport) => s.title ?? periodTitle(s.cadence ?? "month", s.month);

export function ScheduleModal({ open, onClose, view, onSaved }: {
  open: boolean; onClose: () => void; view: DeliveryView; onSaved: (v: DeliveryView) => void;
}) {
  const s = view.settings;
  const [on, setOn] = useState(false);
  const [cadence, setCadence] = useState<Cadence>("month");
  const [weekDay, setWeekDay] = useState(0);
  const [sendDay, setSendDay] = useState(3);
  const [hour, setHour] = useState(9);
  const [zone, setZone] = useState("Europe/London");
  const [format, setFormat] = useState<FileFormat>("pdf");
  const [how, setHow] = useState<DeliverySettings["delivery"]>("attach");
  const [ticked, setTicked] = useState<number[]>([]);
  const [emails, setEmails] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!open) return;
    setOn(s.auto_send); setCadence(s.cadence); setWeekDay(s.week_day); setSendDay(s.send_day);
    setHour(s.send_hour); setZone(s.time_zone); setFormat(s.file_format); setHow(s.delivery);
    setTicked(s.user_ids ?? view.recipients.map(p => p.user_id));
    setEmails((s.emails ?? []).join("\n"));
  }, [open, s, view.recipients]);

  async function save() {
    setSaving(true);
    try {
      const v = await saveDelivery({ auto_send: on, cadence, week_day: weekDay, send_day: sendDay,
        send_hour: hour, time_zone: zone, file_format: format, delivery: how,
        user_ids: ticked, emails: lines(emails) });
      showDone(v.settings.auto_send && v.next
        ? `It will be sent automatically. Next: ${fmtDay(v.next.send_on)} at ${fmtHour(v.settings.send_hour)}`
          + ` ${tzLabel(v, v.settings.time_zone)} (${v.next.title ?? v.next.month}).`
        : "It will not be sent automatically.", "Schedule saved");
      onSaved(v);
    } catch (e) {
      showError(e, "Could not save the schedule");
    } finally {
      setSaving(false);
    }
  }

  const at = `at ${fmtHour(hour)} ${tzLabel(view, zone)}`;
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
              {CADENCES.map(c => (
                <button key={c.key} type="button" className={cadence === c.key ? "on" : ""}
                  onClick={() => setCadence(c.key)}>{c.label}</button>
              ))}
            </span>
          </div>
          {cadence === "day" ? null : cadence === "week" ? (
            <label style={{ display: "grid", gap: 6 }}><span className="sub">Every</span>
              <select className="fbar-select" value={weekDay} onChange={e => setWeekDay(Number(e.target.value))}>
                {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
              </select></label>
          ) : (
            <label style={{ display: "grid", gap: 6 }}><span className="sub">On working day</span>
              <select className="fbar-select" value={sendDay} onChange={e => setSendDay(Number(e.target.value))}>
                {view.send_days.map(d => <option key={d} value={d}>{ordinal(d)}</option>)}
              </select></label>
          )}
          <label style={{ display: "grid", gap: 6 }}><span className="sub">At</span>
            <select className="fbar-select" value={hour} aria-label="Time"
              onChange={e => setHour(Number(e.target.value))}>
              {HOURS.map(h => <option key={h} value={h}>{fmtHour(h)}</option>)}
            </select></label>
          <label style={{ display: "grid", gap: 6 }}><span className="sub">Time zone</span>
            <select className="fbar-select" value={zone} onChange={e => setZone(e.target.value)}>
              {(view.time_zones ?? [{ key: zone, label: zone }]).map(z => (
                <option key={z.key} value={z.key}>{z.label}</option>
              ))}
            </select></label>
        </div>
        <div className="sub">{cadence === "day" ? `Yesterday's report, every day ${at}.`
          : cadence === "week"
          ? `Last week's report (Monday to Sunday), every ${WEEKDAYS[weekDay]} ${at}.`
          : `Last month's report, on the ${ordinal(sendDay)} working day of each month ${at}.`}</div>
        <div><div className="sub" style={{ marginBottom: 6 }}>Send to</div>
          <Recipients logins={view.recipients} ticked={ticked} setTicked={setTicked} emails={emails} setEmails={setEmails} /></div>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <FormatPick value={format} onChange={setFormat} formats={FORMATS} />
          <div>
            <div className="sub" style={{ marginBottom: 6 }}>Your logins get an attachment</div>
            <span className="seg">
              <button type="button" className={how === "attach" ? "on" : ""} onClick={() => setHow("attach")}>The file</button>
            </span>
          </div>
        </div>
        {how === "link" && <div className="sub">Other email addresses always get the file attached.</div>}
      </div>
    </Modal>
  );
}

/** Every report sent, newest first, in a right-hand drawer: when, which report, to whom,
 *  whether it was delivered, and the file exactly as it was sent. */
export function SentDrawer({ open, view, onClose, onChanged }: {
  open: boolean; view: DeliveryView; onClose: () => void; onChanged: (v: DeliveryView) => void;
}) {
  const [asking, setAsking] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [more, setMore] = useState(false);
  useEffect(() => {
    if (!open) { setAsking(false); return; }
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  const sends = view.sends;
  const total = view.sends_total ?? sends.length;
  const clearAll = () => {
    setClearing(true);
    clearMonthlySends()
      .then(v => { onChanged({ ...v, sends: [], sends_total: 0 }); setAsking(false); })
      .catch(e => showError(e))
      .finally(() => setClearing(false));
  };
  // The next page of older reports, added under the ones shown.
  const showOlder = () => {
    setMore(true);
    getDelivery({ page: Math.floor(sends.length / SENT_PAGE) + 1, page_size: SENT_PAGE })
      .then(v => onChanged({ ...view, sends: [...sends, ...v.sends.filter(x => !sends.some(y => y.id === x.id))],
        sends_total: v.sends_total }))
      .catch(e => showError(e))
      .finally(() => setMore(false));
  };
  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h" style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
          <div style={{ flex: 1 }}>
            <h4>Sent reports</h4>
            <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>
              {total ? `${plural(total, "report")} sent, newest first. Download any one exactly as it was sent.`
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
                  <b style={{ fontSize: 14 }}>{sentTitle(s)}</b>
                  {s.failed === 0 ? <span className="badge b-ok">Delivered to all {s.delivered}</span>
                    : <span className="badge b-crit">{s.delivered} of {s.delivered + s.failed} delivered</span>}
                </div>
                <div className="sub" style={{ marginTop: 4 }}>
                  {s.sent_at ? new Date(s.sent_at).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "—"}
                  {" · "}{s.kind === "scheduled" ? "Sent automatically" : `Sent by ${s.sent_by ?? "—"}`}
                </div>
                {(s.recipients?.length ?? 0) > 0 && <div style={{ marginTop: 8, fontSize: 13, overflowWrap: "anywhere" }}>
                  <span className="sub">To </span>
                  {s.recipients!.map((x, i) => (
                    <span key={x.email}>{i ? ", " : ""}
                      <span style={x.delivered ? undefined : { color: "var(--p-crit)" }}
                        title={x.delivered ? x.email : `${x.email} — not delivered`}>{x.name ?? x.email}</span></span>))}
                </div>}
                {s.has_file && <div style={{ marginTop: 8, display: "flex", gap: 14, fontSize: 13 }}>
                  <span className="linkish" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    onClick={() => downloadSentReport(s, "pdf").catch(e => showError(e))}>
                    <FileText size={14} /> PDF</span>
                  <span className="linkish" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    onClick={() => downloadSentReport(s, "excel").catch(e => showError(e))}>
                    <FileSpreadsheet size={14} /> Excel</span>
                </div>}
              </div>))}
              {sends.length < total &&
                <button type="button" className="btn sm" style={{ justifySelf: "center" }} disabled={more}
                  onClick={showOlder}>{more ? "Loading…" : "Show older"}</button>}
            </div>}
        </div>
      </aside>
    </>
  );
}
