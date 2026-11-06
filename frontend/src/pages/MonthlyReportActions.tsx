// MOPR 42.2 — the Monthly Report's own buttons, beside the tabs so they show on
// every tab (it is one report): download the month as a PDF or Excel file, open
// Send & Schedule (its tag says whether the report goes out by itself, and
// when), and send it now. What happened after a click is handed up as a note,
// so the page can show it under the tabs.
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { CalendarClock, ChevronDown, Download, FileSpreadsheet, FileText } from "lucide-react";
import {
  downloadMonthlyReport, getDelivery, sendMonthlyReport,
  type DeliverySettings, type DeliveryView,
} from "../api/reports";
import { Modal } from "../components/ui/Modal";
import { fmtDay, fmtMonth, ordinal, plural } from "../utils/reportFormat";
import type { ReportFilters } from "./MonthlyReport";

export type ActionNote = { tone: "ok" | "warn"; text: string } | null;

/** The "Sent as" line of the Send now popup. */
function sentAs(s: DeliverySettings) {
  if (s.delivery === "link") return "A link in the email (they sign in to open it)";
  const file = s.file_format === "both" ? "PDF and Excel" : s.file_format === "pdf" ? "PDF" : "Excel";
  return `${file}, attached to the email`;
}

/** "Sachin Argade (sachin@…)", or just the address when there is no name. */
function nameAndEmail(p: { name: string | null; email: string }) {
  return p.name ? `${p.name} (${p.email})` : p.email;
}

/** "3 Dec", for the tag on the Send & Schedule button. */
function shortDay(iso: string) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

const BTN = { height: 38 };

export default function MonthlyReportActions({ filters, onNote }: {
  filters: ReportFilters;
  onNote: (note: ActionNote) => void;
}) {
  const { month, programId, brokerId } = filters;
  const [delivery, setDelivery] = useState<DeliveryView | null>(null);
  const [menu, setMenu] = useState(false);
  const [fileBusy, setFileBusy] = useState<"pdf" | "excel" | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [sending, setSending] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getDelivery().then(setDelivery).catch(() => setDelivery(null));
  }, []);

  // The Download menu closes on a click anywhere else, or on Escape.
  useEffect(() => {
    if (!menu) return;
    const away = (e: MouseEvent) => { if (!menuRef.current?.contains(e.target as Node)) setMenu(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [menu]);

  async function download(format: "pdf" | "excel") {
    setMenu(false); setFileBusy(format); onNote(null);
    try {
      await downloadMonthlyReport({ month, format, program_id: programId, broker_party_id: brokerId });
    } catch {
      onNote({ tone: "warn", text: "Could not make the file. Try again." });
    } finally {
      setFileBusy(null);
    }
  }

  async function sendNow() {
    setSending(true); onNote(null);
    try {
      const r = await sendMonthlyReport({ month });
      const sent = r.recipients.filter(x => x.delivered).map(nameAndEmail).join(", ");
      const missed = r.recipients.filter(x => !x.delivered).map(nameAndEmail).join(", ");
      onNote(r.failed === 0
        ? { tone: "ok", text: `The ${fmtMonth(r.month)} report was sent to ${sent}.` }
        : { tone: "warn", text: (r.delivered ? `The ${fmtMonth(r.month)} report was sent to ${sent}. ` : "")
            + `It could not be delivered to ${missed}.` });
      getDelivery().then(setDelivery).catch(() => undefined);
    } catch (e: any) {
      onNote({ tone: "warn", text: e?.response?.data?.detail ?? "Could not send the report." });
    } finally {
      setSending(false);
      setConfirm(false);
    }
  }

  const cfg = delivery?.settings;
  const recipients = delivery?.recipients ?? [];
  const next = cfg?.auto_send ? delivery?.next : null;
  const status = next && cfg
    ? `Sent automatically on the ${ordinal(cfg.send_day)} working day to `
      + `${plural(recipients.length, "person", "people")}. Next: ${fmtDay(next.send_on)}.`
    : "Not sent automatically yet.";
  const filtered = programId != null || brokerId != null;
  // Stored times are UTC, and so is the month the report counts to.
  const running = month >= new Date().toISOString().slice(0, 7);

  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, marginLeft: "auto" }}>
      <div ref={menuRef} style={{ position: "relative" }}>
        <button type="button" className="btn" style={BTN} onClick={() => setMenu(o => !o)}
          aria-haspopup="menu" aria-expanded={menu} disabled={fileBusy != null}>
          <Download size={15} aria-hidden="true" />
          {fileBusy ? "Making…" : "Download"}
          <ChevronDown size={14} aria-hidden="true" style={{ color: "var(--p-faint)" }} />
        </button>
        {menu && (
          <div role="menu" style={{ position: "absolute", top: "calc(100% + 6px)", left: 0, zIndex: 30,
                                    minWidth: 180, padding: 6, background: "var(--p-surface)",
                                    border: "1px solid var(--p-border)", borderRadius: "var(--p-r-sm)",
                                    boxShadow: "var(--p-sh)" }}>
            {([["pdf", "PDF", FileText], ["excel", "Excel", FileSpreadsheet]] as const).map(([f, label, Icon]) => (
              <button key={f} type="button" role="menuitem" onClick={() => download(f)}
                className="flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-left text-[13.5px] font-medium hover:bg-surface-2">
                <Icon size={16} aria-hidden="true" style={{ color: "var(--p-primary)" }} />
                {label}
              </button>
            ))}
          </div>
        )}
      </div>

      <Link to="/monthly-report/send-schedule" className="btn" style={BTN} title={status}>
        <CalendarClock size={15} aria-hidden="true" />
        Send &amp; Schedule
        <span className={`badge ${next ? "b-info" : "b-mut"}`} style={{ padding: "2px 8px" }}>
          {next ? shortDay(next.send_on) : "Off"}
        </span>
      </Link>

      <button type="button" className="btn pri" style={BTN} disabled={!delivery}
        onClick={() => { onNote(null); setConfirm(true); }}>
        Send now →
      </button>

      <Modal open={confirm} size="md" title={`Send the ${fmtMonth(month)} report now?`}
        onClose={() => { if (!sending) setConfirm(false); }}
        footer={
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
            <button type="button" className="btn" onClick={() => setConfirm(false)} disabled={sending}>Cancel</button>
            <button type="button" className="btn pri" onClick={sendNow} disabled={sending || recipients.length === 0}>
              {sending ? "Sending…" : "Send now"}
            </button>
          </div>
        }>
        {recipients.length > 0 && cfg ? (
          <div style={{ display: "grid", gridTemplateColumns: "76px 1fr", rowGap: 14, fontSize: 14, lineHeight: 1.45 }}>
            <div style={{ color: "var(--p-muted)" }}>To</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {recipients.map(p => (
                <div key={p.user_id}>
                  <div style={{ fontWeight: 600 }}>{p.name ?? p.email}</div>
                  {p.name && <div style={{ color: "var(--p-muted)" }}>{p.email}</div>}
                </div>
              ))}
            </div>
            <div style={{ color: "var(--p-muted)" }}>Sent as</div>
            <div>{sentAs(cfg)}</div>
          </div>
        ) : (
          <p style={{ margin: 0, fontSize: 14 }}>
            This carrier has no active login with an email, so there is no one to send the report to.
          </p>
        )}
        {(filtered || running) && (
          <div className="note warn" style={{ marginTop: 16 }}>
            {filtered && <div>It covers the whole carrier, not only the programme or broker picked.</div>}
            {running && <div>{fmtMonth(month)} is not over yet, so the numbers count to today.</div>}
          </div>
        )}
      </Modal>
    </div>
  );
}
