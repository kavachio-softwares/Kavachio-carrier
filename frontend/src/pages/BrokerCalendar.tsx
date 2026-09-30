// The broker's Bordereau Calendar — the other side of the carrier's.
//
// The carrier's calendar asks "what is each broker late with"; this one asks
// "what do I owe, and when". It reads the SAME rows (GET /broker/calendar is
// calendar_board narrowed to this broker and the programmes it is still on),
// so a file the carrier sees as "arrived late" is one this screen calls
// "done late" — one fact, two readers, never two numbers.
//
// A file counts as done the moment it is PROCESSED here: that is when the
// carrier's calendar marks it as arrived, so it is the only moment that
// matters for the deadline.
//
// Everything renders inside `.proto` — the badge / tile / card classes are all
// defined as `.proto .x` in proto.css.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { CalendarDays, X } from "lucide-react";
import { getBrokerCalendar, getBrokerCalendarVersions, type BrokerCalendar as Board,
         type BrokerCalendarRow } from "../api/broker";
import type { SubmissionVersionRow } from "../api/calendar";
import { ExportFileViewer } from "../components/ExportFileViewer";
import { useBrokerCarrierId } from "../brokerCarrier";
import { canAccessPath } from "../access";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { fmtDateTime } from "../utils/date";

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** ISO date → "15 Aug". The year is in the month heading above the table. */
function fmtDay(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function fmtFull(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** "2026-08" → "August 2026". */
function fmtMonth(key?: string | null): string {
  if (!key) return "—";
  const [y, m] = key.split("-").map(Number);
  if (!y || !m) return key;
  return new Date(y, m - 1, 1).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

/** Whole days from today to an ISO date (negative once it has passed). */
function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const due = new Date(`${iso}T00:00:00`).getTime();
  const t = new Date(); t.setHours(0, 0, 0, 0);
  return Math.round((due - t.getTime()) / 86_400_000);
}

const DONE = new Set(["on_time", "received_late"]);

/** "Arrival Status" — the carrier calendar's wording, so both sides read one
 *  row the same way. */
function statusBadge(r: BrokerCalendarRow): { label: string; cls: string } {
  switch (r.status) {
    case "overdue":
      return { label: `${plural(r.days_over ?? 0, "day")} over, nothing yet`, cls: "b-crit" };
    case "due_today":
      return { label: "Due today", cls: "b-warn" };
    case "due_soon": {
      const n = daysUntil(r.due_date);
      return { label: n != null && n > 0 ? `Due in ${plural(n, "day")}` : "Due soon", cls: "b-info" };
    }
    case "scheduled":
      return { label: "Not due yet", cls: "b-mut" };
    case "on_time":
      return { label: "On time", cls: "b-ok" };
    case "received_late":
      return { label: r.days_late != null ? `${plural(r.days_late, "day")} late` : "Arrived late",
               cls: "b-warn" };
    default:
      return { label: String(r.status), cls: "b-mut" };
  }
}

// Same page size as the carrier's calendar.
const PAGE_SIZE = 10;

export default function BrokerCalendar() {
  const carrierId = useBrokerCarrierId();
  // The dashboard's Upcoming Files tile opens a specific month — the oldest
  // missed file's, or the next one due. The server falls back to its own
  // default when the month has nothing in it.
  const [qs] = useSearchParams();
  const askedMonth = qs.get("month") || undefined;
  const [board, setBoard] = useState<Board | null>(null);
  const [month, setMonth] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  // The period whose version history is open — one at a time, as on the
  // carrier's calendar.
  const [openRow, setOpenRow] = useState<BrokerCalendarRow | null>(null);
  // The period whose processed file is open in the viewer.
  const [viewRow, setViewRow] = useState<BrokerCalendarRow | null>(null);
  const [schedPage, setSchedPage] = useState(1);
  const canProcess = canAccessPath("/broker/bordereau");

  const load = useCallback(async (m?: string) => {
    setLoading(true); setErr(null);
    try {
      const data = await getBrokerCalendar(m, carrierId);
      setBoard(data); setMonth(data.month);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? e?.message ?? "Could not load your calendar.");
    } finally {
      setLoading(false);
    }
  }, [carrierId]);

  // A different carrier means a different set of months, so start over on the
  // asked-for month, or the one the server picks.
  useEffect(() => { load(askedMonth); }, [load, askedMonth]);

  const rows = board?.rows ?? [];
  // The server's counts — the same four the carrier's calendar shows.
  const counts = board?.counts;

  // What needs doing first: overdue, then due today, then soonest; done last.
  const sorted = useMemo(() => {
    const rank: Record<string, number> = {
      overdue: 0, due_today: 1, due_soon: 2, scheduled: 3,
      received_late: 4, on_time: 5,
    };
    return [...rows].sort((a, b) => {
      const ra = rank[a.status] ?? 9, rb = rank[b.status] ?? 9;
      if (ra !== rb) return ra - rb;
      return (a.due_date ?? "").localeCompare(b.due_date ?? "");
    });
  }, [rows]);

  const shown = sorted;

  const schedules = board?.schedules ?? [];
  const schedPageCount = Math.max(1, Math.ceil(schedules.length / PAGE_SIZE));
  const schedPageNow = Math.min(schedPage, schedPageCount);
  const schedRows = schedules.slice((schedPageNow - 1) * PAGE_SIZE, schedPageNow * PAGE_SIZE);

  // One carrier chosen in the sidebar: every row is theirs, so the column
  // would say the same thing on every line.
  const showCarrier = carrierId == null;
  const cols = showCarrier ? 8 : 7;


  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Bordereau Calendar
              <InfoTip text={"Every file you owe your carriers, by the date it is due. "
                + "A file counts as done the moment you process it in Process Bordereau — "
                + "that is when your carrier sees it arrive."} />
            </h2>
          </div>
          <div className="actions">
            {/* The carrier's "Files received →", from the sending side. */}
            <Link className="btn" to="/broker/runs">Processed files →</Link>
            {canProcess && (
              <Link className="btn pri" to="/broker/bordereau">＋ Process Bordereau</Link>
            )}
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

        {/* ---- the four headline counts, as on the carrier's calendar ------ */}
        <div className="tiles" style={{ marginBottom: 18 }}>
          <div className="tile">
            <div className="k">Due this month</div>
            <div className="v">{counts?.due ?? "—"}</div>
          </div>
          <div className="tile">
            <div className="k">Sent on time</div>
            <div className="v" style={{ color: "var(--p-ok)" }}>{counts?.on_time ?? "—"}</div>
          </div>
          <div className="tile">
            <div className="k">Sent late</div>
            <div className="v" style={{ color: "var(--p-warn)" }}>{counts?.late ?? "—"}</div>
          </div>
          <div className={`tile${(counts?.never ?? 0) > 0 ? " alert" : ""}`}>
            <div className="k">Never sent</div>
            <div className="v" style={{ color: "var(--p-crit)" }}>{counts?.never ?? "—"}</div>
          </div>
        </div>

        {/* ---- one row for every file this broker owes this month --------- */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <CalendarDays className="ci" />
            <h3>{fmtMonth(month)}</h3>
            <InfoTip text="Every file you owe, by the date it is due." />
            <div className="right">
              <select className="fbar-select" value={month ?? ""}
                onChange={e => load(e.target.value)}
                disabled={loading || (board?.months?.length ?? 0) === 0}>
                {(board?.months ?? []).map(m =>
                  <option key={m} value={m}>{fmtMonth(m)}</option>)}
                {(board?.months?.length ?? 0) === 0 && <option value="">No deadlines yet</option>}
              </select>
            </div>
          </div>

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  {showCarrier && <th>Carrier</th>}
                  <th>Programme</th><th>Period</th><th>Due by</th>
                  <th>Sent On</th><th>Arrival Status</th><th>Version</th><th>Action</th>
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td colSpan={cols} className="muted"
                    style={{ padding: "18px 12px", textAlign: "center" }}>Loading…</td></tr>
                )}
                {!loading && shown.length === 0 && (
                  <tr><td colSpan={cols} className="muted"
                    style={{ padding: "18px 12px", textAlign: "center" }}>
                    Nothing is due from you in this month.
                  </td></tr>
                )}
                {!loading && shown.map(r => {
                  const b = statusBadge(r);
                  const done = DONE.has(r.status);
                  return (
                    <tr key={r.id}>
                      {showCarrier && <td className="muted">{r.carrier_name ?? "—"}</td>}
                      <td><b>{r.program_name}</b></td>
                      <td className="mono">{r.period}</td>
                      <td className="mono">{fmtDay(r.due_date)}</td>
                      <td className="mono">
                        {fmtDay(r.received_at)}
                        {r.latest_received_at && r.latest_received_at !== r.received_at && (
                          <div className="sub">latest {fmtDay(r.latest_received_at)}</div>
                        )}
                      </td>
                      <td>
                        <span className={`badge ${b.cls}`}><span className="d" />{b.label}</span>
                        {/* The carrier's reminder, read back to the one it was
                            about — the most direct "this one is wanted" there is. */}
                        {r.chase_count > 0 && (
                          <div className="sub" style={{ marginTop: 3 }}>
                            carrier reminded you {r.chase_count > 1 ? `${r.chase_count}× · ` : ""}
                            {fmtDay(r.chased_at)}
                          </div>
                        )}
                      </td>
                      <td>
                        {r.version_count === 0
                          ? <span className="muted">—</span>
                          : (
                            <>
                              <span className={`badge ${r.version_count > 1 ? "b-warn" : "b-mut"}`}>
                                <span className="d" />{r.version_label}
                              </span>
                              {r.uploaded_at && (
                                <div className="sub" style={{ marginTop: 3 }}>
                                  uploaded {fmtDateTime(r.uploaded_at)}
                                </div>
                              )}
                            </>
                          )}
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <span style={{ display: "inline-flex", gap: 10 }}>
                        {r.version_count > 0 && (
                          <span className="linkish" onClick={() => setOpenRow(r)}>Versions →</span>
                        )}
                        {done && r.export_id != null && (
                          <span className="linkish" onClick={() => setViewRow(r)}>View file →</span>
                        )}
                        {!done && canProcess && r.status !== "scheduled" && (
                          <Link className="linkish" to="/broker/bordereau">Process →</Link>
                        )}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* ---- what fills the calendar ------------------------------------ */}
        <div className="card">
          <div className="card-h">
            <h3>Your deadlines</h3>
            <InfoTip text="How often each programme expects a file from you." />
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>{showCarrier && <th>Carrier</th>}<th>Programme</th><th>Reporting Frequency</th>
                  <th>Due</th><th>Next Due Date</th>
                  <th>Contract Ends On
                    <InfoTip text={"The day this programme's contract ends. Deadlines are set up to "
                    + "this date and stop after it."} />
                  </th></tr>
              </thead>
              <tbody>
                {schedules.length === 0 && (
                  <tr><td colSpan={showCarrier ? 6 : 5} className="muted"
                    style={{ padding: "14px 12px" }}>
                    No deadlines yet — your carrier sets how often each programme reports.
                  </td></tr>
                )}
                {schedRows.map(sch => (
                  <tr key={sch.program_id}>
                    {showCarrier && <td className="muted">{sch.carrier_name ?? "—"}</td>}
                    <td><b>{sch.program_name}</b></td>
                    <td>
                      {sch.frequency
                        ? sch.frequency_label
                        : <span className="badge b-warn"><span className="d" />Not set</span>}
                    </td>
                    <td>{sch.due_rule}</td>
                    <td className="mono">{fmtFull(sch.next_due)}</td>
                    <td className="mono">
                      {sch.covers_until
                        ? fmtFull(sch.covers_until)
                        : <span className="sub">no end date on the contract</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={schedPageNow} pageCount={schedPageCount}
            pageSize={PAGE_SIZE} totalItems={schedules.length}
            onPageChange={setSchedPage} noun="programmes" />
        </div>
      </div>

      {openRow && <VersionPanel row={openRow} onClose={() => setOpenRow(null)} />}
      {viewRow?.export_id != null && (
        <ExportFileViewer exportId={viewRow.export_id}
          title={`${viewRow.program_name} · ${viewRow.period}`}
          reviewTo={`/uploads/${viewRow.export_id}/exceptions?download=${viewRow.export_id}&from=broker`}
          onClose={() => setViewRow(null)} />
      )}
    </div>
  );
}

/** Every file sent for one period — the carrier calendar's version panel,
 *  read-only, from GET /broker/calendar/{id}/versions. */
function VersionPanel({ row, onClose }: { row: BrokerCalendarRow; onClose: () => void }) {
  const [versions, setVersions] = useState<SubmissionVersionRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The ONE version whose file is open. The viewer mounts only when a version
  // is clicked and streams just the rows on screen, so the panel itself costs
  // one small request however many versions — or rows — there are.
  const [viewing, setViewing] = useState<SubmissionVersionRow | null>(null);
  useEffect(() => {
    getBrokerCalendarVersions(row.id).then(d => setVersions(d.versions))
      .catch(e => setErr(e?.response?.data?.detail ?? "Could not load the versions."));
  }, [row.id]);
  useEffect(() => {
    // While a file is open, Escape belongs to the viewer: it closes the file
    // and leaves this list where it was, rather than closing both at once.
    if (viewing) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, viewing]);

  const label = (v: SubmissionVersionRow) =>
    `Version ${v.version_no}`;
  // The newest version is the file that counts; every earlier one was replaced.
  const newest = versions?.length ? versions[versions.length - 1] : null;

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,20,28,.45)",
      display: "flex", justifyContent: "flex-end", zIndex: 60 }} onClick={onClose}>
      <div className="card" style={{ width: "min(560px, 100%)", height: "100%",
        borderRadius: 0, overflowY: "auto", margin: 0 }} onClick={e => e.stopPropagation()}>
        <div className="card-h">
          <h3>{row.program_name} · {row.period}</h3>
          <span className="sub">{row.carrier_name ?? ""}</span>
          <div className="right">
            <span className="linkish" onClick={onClose} role="button" aria-label="Close"><X size={14} /></span>
          </div>
        </div>
        <div style={{ padding: "16px 20px" }}>
          {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
          <h4 style={{ margin: "0 0 10px", fontSize: 13 }}>Every file you sent for this period</h4>
          <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px" }}>
            The newest one is the file that counts — the earlier ones are kept
            so a correction can be told apart from the original.
          </p>
          {versions === null && !err && <p className="muted" style={{ fontSize: 13 }}>Loading…</p>}
          {versions?.length === 0 && <div className="note">Nothing has been sent for this period yet.</div>}
          {versions?.map(v => (
            <div key={v.id} className="card" style={{ marginBottom: 12, padding: "12px 14px", boxShadow: "none" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
                <span className={`badge ${v.kind === "original" ? "b-mut" : "b-warn"}`}>
                  <span className="d" />{label(v)}
                </span>
                <span className="mono" style={{ fontSize: 12.5 }}>
                  uploaded {v.uploaded_at ? fmtDateTime(v.uploaded_at) : fmtFull(v.received_at)}
                </span>
                {v.period_source === "oldest_open" && (
                  <span className="sub" title="No period could be read from the file, so this was matched to the oldest period still open.">
                    period assumed
                  </span>
                )}
              </div>
              {v.source_filename && <div className="sub">{v.source_filename}</div>}
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8 }}>
                {newest && v.id !== newest.id ? (
                  <span className="badge b-mut"><span className="d" />Replaced by {label(newest)}</span>
                ) : (
                  <span className="badge b-ok"><span className="d" />The file that counts</span>
                )}
                {v.received_export_id != null && (
                  <span className="linkish" style={{ marginLeft: "auto" }}
                    onClick={() => setViewing(v)}>View file →</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
      {viewing?.received_export_id != null && (
        // Outside the drawer's click-to-close area, so closing the file never
        // closes the list behind it.
        <div onClick={e => e.stopPropagation()}>
          <ExportFileViewer exportId={viewing.received_export_id}
            title={`${row.program_name} · ${row.period} · ${label(viewing)}`}
            // A replaced version is history: its problems are worked on the
            // newest file, so only that one offers the exceptions screen.
            reviewTo={viewing.id === newest?.id
              ? `/uploads/${viewing.received_export_id}/exceptions?download=${viewing.received_export_id}&from=broker`
              : undefined}
            onClose={() => setViewing(null)} />
        </div>
      )}
    </div>
  );
}
