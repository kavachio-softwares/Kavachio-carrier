// MOPR 42.2 — the Periodic Report's own buttons, beside the tabs so they show on
// every tab (it is one report): download the period picked — a month, or a
// day, a week or dates — as a PDF or Excel file, open Send & Schedule (its tag
// says whether the report goes out by itself, and when: always monthly), and
// send the period picked now — in the shared "send by email" popup, as on the
// Executive Report: tick which of the carrier's logins get it, type any other
// addresses, and pick the format. A download that
// fails is handed up as a note, so the page can show it under the tabs.
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { CalendarClock, ChevronDown, Download, FileSpreadsheet, FileText } from "lucide-react";
import {
  downloadMonthlyReport, getDelivery, sendMonthlyReport,
  type DeliverySettings, type DeliveryView,
} from "../api/reports";
import { SendByEmailModal } from "../components/SendByEmail";
import {
  fmtDay, ordinal, periodRunning, periodTitle, plural,
} from "../utils/reportFormat";
import type { ReportFilters } from "./MonthlyReport";

export type ActionNote = { tone: "ok" | "warn"; text: string } | null;

/** "3 Dec", for the tag on the Send & Schedule button. */
function shortDay(iso: string) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

const BTN = { height: 38 };

/** The formats Send now offers — the same three as Send & Schedule. */
const FORMATS: { key: DeliverySettings["file_format"]; label: string }[] = [
  { key: "pdf", label: "PDF" }, { key: "excel", label: "Excel" }, { key: "both", label: "Both" }];

export default function MonthlyReportActions({ filters, onNote }: {
  filters: ReportFilters;
  onNote: (note: ActionNote) => void;
}) {
  const { cadence, period, programId, brokerId } = filters;
  // "October 2026", "9 – 15 Nov 2026", "17 Nov 2026"…
  const title = periodTitle(cadence, period);
  const [delivery, setDelivery] = useState<DeliveryView | null>(null);
  const [menu, setMenu] = useState(false);
  const [fileBusy, setFileBusy] = useState<"pdf" | "excel" | null>(null);
  const [confirm, setConfirm] = useState(false);
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
      await downloadMonthlyReport({ cadence, period, format, program_id: programId, broker_party_id: brokerId });
    } catch {
      onNote({ tone: "warn", text: "Could not make the file. Try again." });
    } finally {
      setFileBusy(null);
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
  const running = cadence === "month"
    ? period >= new Date().toISOString().slice(0, 7)
    : periodRunning(cadence, period);

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

      <SendByEmailModal open={confirm} onClose={() => setConfirm(false)}
        title="Send the Periodic Report" noun="report"
        what={<>
          <b>{title}</b>{running && <span className="sub"> · still running, counts to today</span>}
          {filtered && (
            <div className="note warn" style={{ marginTop: 10 }}>
              It covers the whole carrier, not only the programme or broker picked.
            </div>
          )}
        </>}
        logins={recipients} formats={FORMATS} format={cfg?.file_format}
        send={({ user_ids, emails, file_format }) =>
          sendMonthlyReport({ cadence, period, user_ids, emails, file_format })}
        onSent={() => { setConfirm(false); getDelivery().then(setDelivery).catch(() => undefined); }} />
    </div>
  );
}
