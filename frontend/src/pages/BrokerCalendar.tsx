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
import { CalendarDays } from "lucide-react";
import { getBrokerCalendar, type BrokerCalendar as Board,
         type BrokerCalendarRow } from "../api/broker";
import { useBrokerCarrierId } from "../brokerCarrier";
import { canAccessPath } from "../access";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";

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
const UPCOMING = new Set(["scheduled", "due_soon", "due_today"]);

/** What the row says, in the broker's words rather than the carrier's. */
function statusBadge(r: BrokerCalendarRow): { label: string; cls: string } {
  switch (r.status) {
    case "overdue":
      return { label: `${plural(r.days_over ?? 0, "day")} overdue`, cls: "b-crit" };
    case "due_today":
      return { label: "Due today", cls: "b-warn" };
    case "due_soon": {
      const n = daysUntil(r.due_date);
      return { label: n != null && n > 0 ? `Due in ${plural(n, "day")}` : "Due soon", cls: "b-info" };
    }
    case "scheduled":
      return { label: "Not due yet", cls: "b-mut" };
    case "on_time":
      return { label: "Done on time", cls: "b-ok" };
    case "received_late":
      return { label: r.days_late != null ? `Done ${plural(r.days_late, "day")} late` : "Done late",
               cls: "b-warn" };
    default:
      return { label: String(r.status), cls: "b-mut" };
  }
}

type Filter = "all" | "pending" | "upcoming" | "done";

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
  const [filter, setFilter] = useState<Filter>("all");
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
  const counts = useMemo(() => ({
    due: rows.length,
    done: rows.filter(r => DONE.has(r.status)).length,
    pending: rows.filter(r => r.status === "overdue").length,
    upcoming: rows.filter(r => UPCOMING.has(r.status)).length,
  }), [rows]);

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

  const shown = sorted.filter(r =>
    filter === "all" ? true
    : filter === "done" ? DONE.has(r.status)
    : filter === "pending" ? r.status === "overdue"
    : UPCOMING.has(r.status));

  const schedules = board?.schedules ?? [];
  const schedPageCount = Math.max(1, Math.ceil(schedules.length / PAGE_SIZE));
  const schedPageNow = Math.min(schedPage, schedPageCount);
  const schedRows = schedules.slice((schedPageNow - 1) * PAGE_SIZE, schedPageNow * PAGE_SIZE);

  // One carrier chosen in the sidebar: every row is theirs, so the column
  // would say the same thing on every line.
  const showCarrier = carrierId == null;
  const cols = showCarrier ? 8 : 7;

  const chip = (key: Filter, label: string, n: number) => (
    <button type="button" className={`btn sm${filter === key ? " pri" : ""}`}
            onClick={() => setFilter(key)}>
      {label} ({n})
    </button>
  );

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
          {canProcess && (
            <div className="actions">
              <Link className="btn pri" to="/broker/bordereau">＋ Process Bordereau</Link>
            </div>
          )}
        </div>

        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

        {/* ---- the four headline counts, each one a filter ---------------- */}
        <div className="tiles" style={{ marginBottom: 18 }}>
          <div className="tile" style={{ cursor: "pointer" }} onClick={() => setFilter("all")}>
            <div className="k">Due this month</div>
            <div className="v">{board ? counts.due : "—"}</div>
          </div>
          <div className="tile" style={{ cursor: "pointer" }} onClick={() => setFilter("done")}>
            <div className="k">Done</div>
            <div className="v" style={{ color: "var(--p-ok)" }}>{board ? counts.done : "—"}</div>
          </div>
          <div className={`tile${counts.pending > 0 ? " alert" : ""}`}
               style={{ cursor: "pointer" }} onClick={() => setFilter("pending")}>
            <div className="k">Overdue</div>
            <div className="v" style={{ color: "var(--p-crit)" }}>{board ? counts.pending : "—"}</div>
          </div>
          <div className="tile" style={{ cursor: "pointer" }} onClick={() => setFilter("upcoming")}>
            <div className="k">Upcoming</div>
            <div className="v">{board ? counts.upcoming : "—"}</div>
          </div>
        </div>

        {/* ---- one row for every file this broker owes this month --------- */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <CalendarDays className="ci" />
            <h3>{fmtMonth(month)}</h3>
            <span className="sub">every file you owe, by the date it is due</span>
            <div className="right">
              <select className="fbar-select" value={month ?? ""}
                onChange={e => { setFilter("all"); load(e.target.value); }}
                disabled={loading || (board?.months?.length ?? 0) === 0}>
                {(board?.months ?? []).map(m =>
                  <option key={m} value={m}>{fmtMonth(m)}</option>)}
                {(board?.months?.length ?? 0) === 0 && <option value="">No deadlines yet</option>}
              </select>
            </div>
          </div>

          <div style={{ display: "flex", gap: 8, padding: "12px 20px 4px", flexWrap: "wrap" }}>
            {chip("all", "All", counts.due)}
            {chip("pending", "Overdue", counts.pending)}
            {chip("upcoming", "Upcoming", counts.upcoming)}
            {chip("done", "Done", counts.done)}
          </div>

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  {showCarrier && <th>Carrier</th>}
                  <th>Programme</th><th>Period</th><th>Due by</th>
                  <th>Processed</th><th>Status</th><th>Version</th><th>Action</th>
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
                    {rows.length === 0
                      ? "Nothing is due from you in this month."
                      : "No files in this group."}
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
                        {r.chase_count > 0 && !done && (
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
                            <span className={`badge ${r.version_count > 1 ? "b-warn" : "b-mut"}`}>
                              <span className="d" />{r.version_label}
                            </span>
                          )}
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        {done && r.export_id != null && (
                          <Link className="linkish"
                            to={`/uploads/${r.export_id}/exceptions?download=${r.export_id}&from=broker`}>
                            View file →
                          </Link>
                        )}
                        {!done && canProcess && r.status !== "scheduled" && (
                          <Link className="linkish" to="/broker/bordereau">Process →</Link>
                        )}
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
            <span className="sub">how often each programme expects a file from you</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>{showCarrier && <th>Carrier</th>}<th>Programme</th><th>How often</th>
                  <th>Due</th><th>Next one</th><th>Covered until</th></tr>
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
    </div>
  );
}
