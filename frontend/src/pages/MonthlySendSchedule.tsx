// MOPR 42.2 — Send & Schedule: how the Monthly Report is delivered.
//
// Whether it goes out by itself, on which working day after the month ends,
// and as which file, attached or as a link — plus every report sent,
// downloadable as it was sent (backend report_delivery.py). Reached from the
// Monthly Report's tab row; not a tab, because it is set once and rarely
// touched, and it has no month of its own.
// The report always goes to the carrier's own logins, in full; nothing is
// emailed until the carrier turns automatic sending on.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, CalendarDays, FileText, History, Send } from "lucide-react";
import {
  downloadSentReport, getDelivery, saveDelivery,
  type DeliverySettings, type DeliveryView, type SentReport,
} from "../api/reports";
import { InfoTip } from "../components/InfoTip";
import { fmtDay, fmtMonth, ordinal, plural } from "../utils/reportFormat";

const KIND_WORDS: Record<SentReport["kind"], string> = {
  scheduled: "Automatic", now: "Sent by hand", test: "Test",
};

const FILE_WORDS: Record<DeliverySettings["file_format"], string> = {
  pdf: "PDF", excel: "Excel", both: "PDF and Excel",
};

/** "a PDF attached to the email" / "a link to sign in and open it". */
function howWords(s: DeliverySettings): string {
  if (s.delivery === "link") return "an email with a link — people sign in to open it";
  return s.file_format === "both" ? "a PDF and an Excel file attached to the email"
    : `${s.file_format === "pdf" ? "a PDF" : "an Excel file"} attached to the email`;
}

function StatusBadge({ s }: { s: SentReport }) {
  if (s.status === "sent") return <span className="badge b-ok"><span className="d" />Sent</span>;
  if (s.status === "partly") {
    return <span className="badge b-warn"><span className="d" />{s.failed} not delivered</span>;
  }
  return <span className="badge b-crit"><span className="d" />Not sent</span>;
}

export default function MonthlySendSchedule() {
  const [view, setView] = useState<DeliveryView | null>(null);
  const [draft, setDraft] = useState<DeliverySettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  function take(v: DeliveryView) {
    setView(v);
    setDraft(v.settings);
  }

  useEffect(() => {
    getDelivery().then(take)
      .catch(e => setErr(e?.response?.data?.detail ?? "Could not load Send & Schedule."));
  }, []);

  const dirty = useMemo(() => !!view && !!draft && JSON.stringify(view.settings) !== JSON.stringify(draft),
    [view, draft]);

  const set = (patch: Partial<DeliverySettings>) => { setDraft(d => (d ? { ...d, ...patch } : d)); setMsg(null); };

  async function save() {
    if (!draft) return;
    setBusy(true); setMsg(null);
    try {
      take(await saveDelivery(draft));
      setMsg({ tone: "ok", text: "Saved. The change is kept in Audit Logs." });
    } catch (e: any) {
      setMsg({ tone: "warn", text: e?.response?.data?.detail ?? "Could not save." });
    } finally {
      setBusy(false);
    }
  }

  async function download(s: SentReport, format: "pdf" | "excel") {
    try {
      await downloadSentReport(s, format);
    } catch {
      setMsg({ tone: "warn", text: "Could not download that report." });
    }
  }

  const radio = (name: string, checked: boolean, onChange: () => void, label: ReactNode) => (
    <label style={{ display: "flex", alignItems: "center", gap: 10, minHeight: 36, fontSize: 14, cursor: "pointer" }}>
      <input type="radio" name={name} checked={checked} onChange={onChange}
        style={{ width: 17, height: 17, accentColor: "var(--p-primary)" }} />
      {label}
    </label>
  );
  const check = (checked: boolean, onChange: () => void, label: ReactNode) => (
    <label style={{ display: "inline-flex", alignItems: "center", gap: 10, minHeight: 36, fontSize: 14, cursor: "pointer" }}>
      <input type="checkbox" checked={checked} onChange={onChange}
        style={{ width: 17, height: 17, accentColor: "var(--p-primary)" }} />
      {label}
    </label>
  );

  // Who it goes to, as names with their addresses: "Sachin Argade (sachin@…)".
  const who = (view?.recipients ?? []).map((p, i) => (
    <span key={p.user_id}>{i > 0 && ", "}<b>{p.name ?? p.email}</b>{p.name && ` (${p.email})`}</span>
  ));

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="t">
            <h2>Send &amp; Schedule</h2>
            <p>When the monthly report is emailed to you, and in what form.</p>
          </div>
          <div className="actions" style={{ alignItems: "center" }}>
            <Link to="/monthly-report" className="btn"><ArrowLeft size={14} /> Monthly Report</Link>
            <button type="button" className="btn pri" onClick={save} disabled={!dirty || busy}>
              {busy ? "Saving…" : "Save changes"}
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}
        {msg && <div className={`note ${msg.tone}`} style={{ marginBottom: 16 }}>{msg.text}</div>}

        {view && draft && (
          <>
            {/* ---- what happens next -------------------------------------- */}
            <div style={{ display: "flex", gap: 14, alignItems: "flex-start", marginBottom: 18,
                          background: "var(--p-primary-soft)", border: "1px solid #C7E3E7",
                          borderRadius: "var(--p-r)", padding: "16px 20px" }}>
              <span style={{ flex: "none", width: 36, height: 36, borderRadius: 18, background: "var(--p-surface)",
                             display: "inline-flex", alignItems: "center", justifyContent: "center",
                             color: "var(--p-primary)" }}>
                <Send size={18} aria-hidden="true" />
              </span>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <div style={{ fontSize: 12, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase",
                              color: "var(--p-info-ink)" }}>
                  Next send
                </div>
                <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.55, color: "var(--p-ink)" }}>
                  {view.recipients.length === 0
                    ? <>This carrier has no active login with an email, so there is no one to send the report to.</>
                    : view.next && view.settings.auto_send
                    ? <>The <b>{fmtMonth(view.next.month)}</b> report goes to {who} on{" "}
                        <b>{fmtDay(view.next.send_on)}</b>, as {howWords(view.settings)}.</>
                    : <>Automatic sending is <b>off</b>. Turn it on below and save, and the report goes to {who}.</>}
                </p>
              </div>
            </div>

            <div className="grid g-2" style={{ marginBottom: 18 }}>
              {/* ---- when ---------------------------------------------------- */}
              <div className="card">
                <div className="card-h">
                  <CalendarDays className="ci" />
                  <h3>When to send</h3>
                  <InfoTip text="Working days are Monday to Friday. The numbers are taken when the report is sent and kept with it, so everyone gets the same figures and a sent report never changes. If the app was down that day, it goes out at the next daily run that week." />
                </div>
                <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 12 }}>
                  {check(draft.auto_send, () => set({ auto_send: !draft.auto_send }),
                    <b>Send the report automatically every month</b>)}
                  <label style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 14 }}>
                    Send on the
                    <select className="fbar-select" value={draft.send_day}
                      onChange={e => set({ send_day: Number(e.target.value) })} style={{ minWidth: 90 }}>
                      {view.send_days.map(d => <option key={d} value={d}>{ordinal(d)}</option>)}
                    </select>
                    working day after the month ends
                  </label>
                </div>
              </div>

              {/* ---- format ---------------------------------------------------- */}
              <div className="card">
                <div className="card-h">
                  <FileText className="ci" />
                  <h3>Format and delivery</h3>
                </div>
                <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>
                  <div>
                    <div className="sub" style={{ fontWeight: 600, marginBottom: 4 }}>File type</div>
                    {radio("fmt", draft.file_format === "pdf", () => set({ file_format: "pdf" }),
                      <span><b>PDF</b> <span className="muted">— easy to read and print</span></span>)}
                    {radio("fmt", draft.file_format === "excel", () => set({ file_format: "excel" }),
                      <span><b>Excel</b> <span className="muted">— numbers you can work with</span></span>)}
                    {radio("fmt", draft.file_format === "both", () => set({ file_format: "both" }), <b>Both</b>)}
                  </div>
                  <div>
                    <div className="sub" style={{ fontWeight: 600, marginBottom: 4 }}>How it is sent</div>
                    {radio("how", draft.delivery === "attach", () => set({ delivery: "attach" }),
                      "Email with the file attached")}
                    {radio("how", draft.delivery === "link", () => set({ delivery: "link" }),
                      "Email with a link — people sign in to open it")}
                  </div>
                </div>
              </div>
            </div>

            {/* ---- history ------------------------------------------------------ */}
            <div className="card" style={{ marginBottom: 18 }}>
              <div className="card-h">
                <History className="ci" />
                <h3>Reports sent</h3>
                <InfoTip text="Every report sent, newest first. Download gives the report as it was sent. Every change on this page, and every send, is kept in Audit Logs." />
                <div className="right"><span className="sub">Newest first</span></div>
              </div>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th className="l">Report month</th><th className="l">Sent on</th><th className="l">How</th>
                      <th className="r">Sent to</th><th className="l">File</th><th className="l">Status</th>
                      <th className="r">Download</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view.sends.length === 0 && (
                      <tr><td colSpan={7} className="muted" style={{ textAlign: "center" }}>No report has been sent yet.</td></tr>
                    )}
                    {view.sends.map(s => (
                      <tr key={s.id}>
                        <td className="l" style={{ fontWeight: 600 }}>{fmtMonth(s.month)}</td>
                        <td className="l">{fmtDay(s.sent_at)}</td>
                        <td className="l muted">{KIND_WORDS[s.kind]}</td>
                        <td className="r">{plural(s.people, "person", "people")}</td>
                        <td className="l">{FILE_WORDS[s.file_format]}{s.delivery === "link" ? " · link" : ""}</td>
                        <td className="l"><StatusBadge s={s} /></td>
                        <td className="r" style={{ whiteSpace: "nowrap" }}>
                          {s.has_file ? (
                            <span style={{ display: "inline-flex", gap: 6 }}>
                              <button type="button" className="btn sm" onClick={() => download(s, "pdf")}>PDF</button>
                              <button type="button" className="btn sm" onClick={() => download(s, "excel")}>Excel</button>
                            </span>
                          ) : <span className="muted">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
