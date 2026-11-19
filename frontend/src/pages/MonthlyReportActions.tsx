// MOPR 42.2 — the Operational Report's own buttons, beside the tabs so they show on
// every tab (it is one report), laid out as the Executive Report's: download the
// period picked — a month, or a day, a week or dates — as a PDF or Excel file;
// Sent reports (a drawer of every report sent); Schedule ("Send it
// automatically" in a popup; its tag says when it next goes); and Send — the
// period picked, now, in the shared "send by email" popup: tick which of the
// carrier's logins get it, type any other addresses, and pick the format. A
// download that fails is handed up as a note, so the page can show it under the tabs.
import { useEffect, useRef, useState } from "react";
import { CalendarClock, ChevronDown, Download, FileSpreadsheet, FileText, History, Send } from "lucide-react";
import {
  downloadMonthlyReport, getDelivery, sendMonthlyReport, type DeliveryView,
} from "../api/reports";
import { SendByEmailModal } from "../components/SendByEmail";
import {
  fmtDay, fmtHour, periodRunning, periodTitle, plural, scheduleWords,
} from "../utils/reportFormat";
import type { ReportFilters } from "./MonthlyReport";
import { FORMATS, ScheduleModal, SENT_PAGE, SentDrawer, tzLabel } from "./MonthlyReportDelivery";

export type ActionNote = { tone: "ok" | "warn"; text: string } | null;

/** "3 Dec", for the tag on the Schedule button. */
function shortDay(iso: string) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

const BTN = { height: 38 };

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
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [sentOpen, setSentOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // The first page of reports sent comes too, for the Sent reports drawer.
  const reload = () => getDelivery({ page: 1, page_size: SENT_PAGE }).then(setDelivery)
    .catch(() => setDelivery(null));
  useEffect(() => { reload(); }, []);

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
  // "Sent automatically every Wednesday at 09:00 UK time to 2 people. Next: …"
  const people = cfg ? recipients.filter(p => cfg.user_ids == null || cfg.user_ids.includes(p.user_id)).length
    + (cfg.emails?.length ?? 0) : 0;
  const tz = delivery && cfg ? tzLabel(delivery, cfg.time_zone) : "";
  const status = next && cfg
    ? `Sent automatically ${scheduleWords(cfg)} at ${fmtHour(cfg.send_hour)} ${tz} to `
      + `${plural(people, "person", "people")}. Next: ${fmtDay(next.send_on)}.`
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

      <button type="button" className="btn" style={BTN} onClick={() => setSentOpen(true)} disabled={!delivery}>
        <History size={15} aria-hidden="true" /> Sent reports
        {delivery && (
          <span className={`badge ${delivery.sends.length ? "b-info" : "b-mut"}`} style={{ padding: "2px 8px" }}>
            {delivery.sends_total ?? delivery.sends.length}
          </span>
        )}
      </button>

      <button type="button" className="btn" style={BTN} title={status} onClick={() => setScheduleOpen(true)}
        disabled={!delivery}>
        <CalendarClock size={15} aria-hidden="true" /> Schedule
        <span className={`badge ${next ? "b-info" : "b-mut"}`} style={{ padding: "2px 8px" }}>
          {next ? shortDay(next.send_on) : "Off"}
        </span>
      </button>

      <button type="button" className="btn pri" style={BTN} disabled={!delivery}
        onClick={() => { onNote(null); setConfirm(true); }}>
        <Send size={15} aria-hidden="true" /> Send
      </button>

      {delivery && (<>
        <SentDrawer open={sentOpen} view={delivery} onClose={() => setSentOpen(false)} onChanged={setDelivery} />
        {/* Saving changes the schedule, not what has been sent: the list stays. */}
        <ScheduleModal open={scheduleOpen} onClose={() => setScheduleOpen(false)} view={delivery}
          onSaved={v => {
            setDelivery(prev => ({ ...v, sends: prev?.sends ?? v.sends, sends_total: prev?.sends_total }));
            setScheduleOpen(false);
          }} />
      </>)}

      <SendByEmailModal open={confirm} onClose={() => setConfirm(false)}
        title="Send the Operational Report" noun="report"
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
        onSent={() => { setConfirm(false); reload(); }} />
    </div>
  );
}
