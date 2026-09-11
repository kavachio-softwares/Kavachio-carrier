// Requirement 17.2 — the carrier's Bordereau Calendar.
//
// Follows the carrier-centric design's `c-calendar` screen: five headline
// counts, one row for every file somebody owes this month, and how often each
// programme reports. The shape of the screen is the argument it makes — you
// read what is owed, then what turned up, then what went onward, in that order,
// because that is the order the questions get asked.
//
// KEYED ON THE DUE MONTH, not on the reporting period. Programmes on different
// frequencies have to share one page: a monthly programme's July file and a
// quarterly programme's Q2 file are both due in August, and August is the only
// heading both of them belong under. The period each row is FOR is shown in the
// row itself, so nothing is lost by grouping this way.
//
// Everything renders inside `.proto` — the badge / tile / card / note classes
// are all defined as `.proto .x` in proto.css, and without the wrapper this
// markup renders as unstyled text.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CalendarDays, Send, X } from "lucide-react";
import {
  chase, getBoard, getVersions, releasePeriod,
  type BoardResponse, type BoardRow, type BrokerContact,
  type CalendarStatus, type SubmissionVersionRow,
} from "../api/calendar";
import NotificationBell from "../components/NotificationBell";
import { Pagination } from "../components/Pagination";

// Same vocabulary as ProgramCalendar's status badges, so a period reads the
// same on the carrier's board as it does inside a bordereau setup.
const STATUS_META: Record<CalendarStatus, { label: string; cls: string }> = {
  scheduled:     { label: "Not due yet",  cls: "b-mut" },
  due_soon:      { label: "Due soon",     cls: "b-info" },
  due_today:     { label: "Due today",    cls: "b-warn" },
  overdue:       { label: "Overdue",      cls: "b-crit" },
  on_time:       { label: "On time",      cls: "b-ok" },
  received_late: { label: "Arrived late", cls: "b-warn" },
};

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** ISO date → "15 Aug". The year is in the month heading above the table, so
 *  repeating it on every row is noise. */
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
  return d.toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric" });
}

/** "2026-08" → "August 2026". */
function fmtMonth(key?: string | null): string {
  if (!key) return "—";
  const [y, m] = key.split("-").map(Number);
  if (!y || !m) return key;
  return new Date(y, m - 1, 1).toLocaleDateString("en-GB",
    { month: "long", year: "numeric" });
}

const todayISO = () => new Date().toISOString().slice(0, 10);

// Rows per page in the programme list — the same size Users & Roles pages at.
const PAGE_SIZE = 10;

export default function BordereauCalendar() {
  const [board, setBoard] = useState<BoardResponse | null>(null);
  const [month, setMonth] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  // No page-level busy flag: the page no longer performs the chase itself. The
  // confirm dialog does, and owns the spinner on its own button.
  // The period whose version history is open. One at a time: the panel answers
  // "what did we actually send for this one", which is a question about a
  // single row.
  const [openRow, setOpenRow] = useState<BoardRow | null>(null);
  // The rows a pending chase would cover. null = no dialog open. One row for
  // "Chase them", every late row for "Chase what is late".
  const [chasing, setChasing] = useState<BoardRow[] | null>(null);
  const [schedPage, setSchedPage] = useState(1);

  const load = useCallback(async (m?: string) => {
    setLoading(true); setErr(null);
    try {
      const data = await getBoard(m);
      setBoard(data);
      setMonth(data.month);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? e?.message ?? "Could not load the calendar.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const rows = board?.rows ?? [];
  const counts = board?.counts;

  // The programme list arrives whole (one row per programme), so it pages here
  // rather than on the server. A reload can leave fewer programmes than the
  // page we were on, hence the clamp.
  const schedules = board?.schedules ?? [];
  const schedPageCount = Math.max(1, Math.ceil(schedules.length / PAGE_SIZE));
  const schedPageNow = Math.min(schedPage, schedPageCount);
  const schedRows = schedules.slice((schedPageNow - 1) * PAGE_SIZE, schedPageNow * PAGE_SIZE);

  // Who is actually late, for the header button. Unattributed rows are excluded
  // for the same reason they are excluded from the counts: there is nobody to
  // chase on a programme with no broker.
  const overdue = useMemo(
    () => rows.filter(r => !r.unassigned && r.status === "overdue"), [rows]);

  // CHASING ASKS FIRST. A chase is a message about somebody being late, so the
  // screen names who it is about and who would hear about it before anything is
  // recorded — and gives you somewhere to say why. Firing straight off the click
  // meant the only way to find out what a chase did was to do one.
  async function confirmChase(rows: BoardRow[], note: string) {
    if (rows.length === 0) return;      // never let an empty selection through
    const res = await chase(rows.map(r => r.id), note);
    setErr(null);
    setMsg(res.chased === 0
      ? "Nothing was chased — those files are no longer late."
      : `Reminder recorded against ${plural(res.chased, "late file")}.`);
    await load(month);
  }

  const worstFirst = useMemo(() => {
    // Most overdue first, then the rest by due date. The row that needs doing
    // something about should not be somewhere in the middle of the table.
    const rank: Record<string, number> = {
      overdue: 0, due_today: 1, due_soon: 2, received_late: 3,
      scheduled: 4, on_time: 5,
    };
    return [...rows].sort((a, b) => {
      if (a.unassigned !== b.unassigned) return a.unassigned ? 1 : -1;
      const ra = rank[a.status] ?? 9, rb = rank[b.status] ?? 9;
      if (ra !== rb) return ra - rb;
      return (a.due_date ?? "").localeCompare(b.due_date ?? "");
    });
  }, [rows]);

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Bordereau Calendar</h2>
            <p>What each broker owes you and when, what has actually turned up,
              and what you have sent on.</p>
          </div>
          <div className="actions">
            <NotificationBell placement="inline" />
            <Link className="btn" to="/files">Files received →</Link>
            <button className="btn pri" onClick={() => setChasing(overdue)}
              disabled={overdue.length === 0}
              title={overdue.length === 0 ? "Nothing is late" : ""}>
              {overdue.length === 0 ? "Nothing is late"
                : `Chase what is late (${overdue.length})`}
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}
        {msg && <div className="note" style={{ marginBottom: 16 }}>{msg}</div>}

        {/* ---- the five headline counts ---------------------------------- */}
        <div className="tiles five" style={{ marginBottom: 18 }}>
          <div className="tile">
            <div className="k">Due this month</div>
            <div className="v">{counts?.due ?? "—"}</div>
            <div className="foot">
              across {plural(board?.programme_count ?? 0, "programme")}
            </div>
          </div>
          <div className="tile">
            <div className="k">Arrived on time</div>
            <div className="v" style={{ color: "var(--p-ok)" }}>{counts?.on_time ?? "—"}</div>
            <div className="foot">
              {counts && counts.due > 0
                ? `${Math.round((counts.on_time / counts.due) * 100)}% of what was due`
                : "nothing due"}
            </div>
          </div>
          <div className="tile">
            <div className="k">Arrived late</div>
            <div className="v" style={{ color: "var(--p-warn)" }}>{counts?.late ?? "—"}</div>
            <div className="foot">
              {(() => {
                const late = rows.filter(r => !r.unassigned && r.days_late != null);
                if (late.length === 0) return "none";
                const days = late.map(r => r.days_late!).sort((a, b) => a - b);
                return days.length === 1 ? plural(days[0], "day") + " over"
                  : `${days[0]} to ${days[days.length - 1]} days over`;
              })()}
            </div>
          </div>
          <div className={`tile${(counts?.never ?? 0) > 0 ? " alert" : ""}`}>
            <div className="k">Never arrived</div>
            <div className="v" style={{ color: "var(--p-crit)" }}>{counts?.never ?? "—"}</div>
            <div className="foot">
              {overdue.length === 0 ? "nothing outstanding"
                : (() => {
                  const worst = [...overdue].sort(
                    (a, b) => (b.days_over ?? 0) - (a.days_over ?? 0))[0];
                  return `${worst.broker_name ?? "Unnamed broker"} · ${
                    plural(worst.days_over ?? 0, "day")} over`;
                })()}
            </div>
          </div>
          <div className="tile">
            <div className="k">Sent onward</div>
            <div className="v">{counts?.released ?? "—"}</div>
            <div className="foot">
              {(counts?.unsent_correction ?? 0) > 0
                ? <span style={{ color: "var(--p-warn)" }}>
                    {plural(counts!.unsent_correction, "correction")} not sent on
                  </span>
                : "of the files that arrived"}
            </div>
          </div>
        </div>

        {/* ---- one row per file somebody owes this month ------------------ */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <CalendarDays className="ci" />
            <h3>{fmtMonth(month)}</h3>
            <span className="sub">
              one row for every file somebody owes you, by the date it is due
            </span>
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
                  <th>Programme</th><th>Broker</th><th>Period</th><th>Due by</th>
                  <th>Turned up</th><th>How it went</th><th>Sent onward</th>
                  <th>Version</th><th></th>
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td colSpan={9} style={{ padding: "18px 12px", textAlign: "center" }}
                    className="muted">Loading…</td></tr>
                )}
                {!loading && worstFirst.length === 0 && (
                  <tr><td colSpan={9} style={{ padding: "18px 12px", textAlign: "center" }}
                    className="muted">
                    Nothing is due in this month.{" "}
                    <Link className="linkish" to="/programs">Set a programme's frequency →</Link>
                  </td></tr>
                )}
                {!loading && worstFirst.map(r => {
                  // A PROGRAMME WITH NO BROKER OWES NOTHING. It shows a blank
                  // row rather than an overdue one, because there is nobody to
                  // be late — and it stays in the list so the programme reads
                  // as idle instead of being forgotten about.
                  if (r.unassigned) {
                    return (
                      <tr key={r.id}>
                        <td><b>{r.program_name}</b></td>
                        <td className="muted">no broker on it yet</td>
                        <td className="mono">{r.period}</td>
                        <td className="muted">—</td>
                        <td className="muted">—</td>
                        <td><span className="badge b-mut"><span className="d" />Nothing is owed</span></td>
                        <td className="muted">—</td>
                        <td className="muted">—</td>
                        <td>
                          <Link className="linkish" to={`/programs/${r.program_id}/brokers`}>
                            Add a broker →
                          </Link>
                        </td>
                      </tr>
                    );
                  }
                  const m = STATUS_META[r.status]
                    ?? { label: String(r.status), cls: "b-mut" };
                  // The badge says what happened; this says how far off it was,
                  // which the badge cannot carry.
                  const detail = r.status === "overdue"
                    ? `${plural(r.days_over ?? 0, "day")} over, nothing yet`
                    : r.days_late != null ? `${plural(r.days_late, "day")} late` : null;
                  return (
                    <tr key={r.id}>
                      <td>{r.program_name}</td>
                      <td><b>{r.broker_name ?? `Broker ${r.broker_party_id}`}</b></td>
                      <td className="mono">{r.period}</td>
                      <td className="mono">{fmtDay(r.due_date)}</td>
                      <td className="mono">
                        {fmtDay(r.received_at)}
                        {r.latest_received_at && r.latest_received_at !== r.received_at && (
                          <div className="sub">latest {fmtDay(r.latest_received_at)}</div>
                        )}
                      </td>
                      <td>
                        <span className={`badge ${m.cls}`}><span className="d" />
                          {detail ?? m.label}</span>
                        {r.chase_count > 0 && (
                          <div className="sub" style={{ marginTop: 3 }}>
                            chased {r.chase_count > 1 ? `${r.chase_count}×` : ""}{" "}
                            {fmtDay(r.chased_at)}
                          </div>
                        )}
                      </td>
                      <td className="mono">
                        {r.released_at ? fmtDay(r.released_at)
                          : <span className="muted">—</span>}
                        {/* Something went, but not the file they would get now.
                            The date alone would read as "done" on a period that
                            was corrected after it was sent. */}
                        {r.released_at && !r.latest_version_released && (
                          <div className="sub" style={{ color: "var(--p-warn)" }}>
                            correction not sent
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
                        {r.version_count > 0 && (
                          <span className="linkish" onClick={() => setOpenRow(r)}>
                            Versions →
                          </span>
                        )}
                        {r.status === "overdue" && (
                          <span className="linkish" style={{ marginLeft: 8 }}
                            onClick={() => setChasing([r])}>
                            Chase them →
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="note" style={{ margin: 0, border: 0,
            borderTop: "1px solid var(--p-border)", borderRadius: 0 }}>
            <b>A programme with no broker owes you nothing.</b> It shows a blank
            row rather than an overdue one, because there is nobody to be late.
            It stays in the list so you can see it is idle instead of forgetting
            it exists.
          </div>
        </div>

        {/* ---- what fills the calendar ---------------------------------- */}
        <div className="card">
          <div className="card-h">
            <h3>How often each programme reports</h3>
            <span className="sub">this is what fills the calendar</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Programme</th><th>How often</th><th>Due</th>
                  <th>Next one</th><th>Covered until</th><th></th></tr>
              </thead>
              <tbody>
                {schedules.length === 0 && (
                  <tr><td colSpan={6} className="muted"
                    style={{ padding: "14px 12px" }}>No programmes yet.</td></tr>
                )}
                {schedRows.map(sch => (
                  <tr key={sch.program_id}>
                    <td>
                      <b>{sch.program_name}</b>
                      <div className="sub">
                        {sch.broker_count === 0 ? "no brokers on it"
                          : plural(sch.broker_count, "broker")}
                      </div>
                    </td>
                    <td>
                      {sch.frequency
                        ? sch.frequency_label
                        : <span className="badge b-warn"><span className="d" />Not set</span>}
                    </td>
                    <td className="l">{sch.due_rule}</td>
                    <td className="mono">{fmtFull(sch.next_due)}</td>
                    {/* Why the deadlines stop where they do. Without this the
                        list just runs out and the reader has to guess whether
                        that is the contract ending or the screen truncating. */}
                    <td className="mono">
                      {sch.covers_until
                        ? fmtFull(sch.covers_until)
                        : <span className="sub">no end date on the contract</span>}
                    </td>
                    <td>
                      <Link className="linkish"
                        to={`/calendar?program=${sch.program_id}`}>Change →</Link>
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

      {openRow && (
        <VersionPanel row={openRow} onClose={() => setOpenRow(null)}
          onChanged={() => load(month)} />
      )}

      {chasing && (
        <ChaseModal rows={chasing} onClose={() => setChasing(null)}
          onConfirm={confirmChase} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The version history for one period, and the place a release is recorded.
//
// Release lives HERE rather than on the table row because sending onward is a
// statement about a specific version — "Munich Re got the corrected file on the
// 3rd" — and the row cannot express which one without the chain beside it.
// ---------------------------------------------------------------------------
function VersionPanel({ row, onClose, onChanged }: {
  row: BoardRow; onClose: () => void; onChanged: () => void;
}) {
  const [versions, setVersions] = useState<SubmissionVersionRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    released_on: todayISO(), released_to: "", release_ref: "",
  });

  const reload = useCallback(async () => {
    setErr(null);
    try { setVersions((await getVersions(row.id)).versions); }
    catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not load the versions.");
    }
  }, [row.id]);

  useEffect(() => { reload(); }, [reload]);

  async function save() {
    setBusy(true); setErr(null);
    try {
      await releasePeriod(row.id, {
        released_on: form.released_on || undefined,
        released_to: form.released_to.trim() || undefined,
        release_ref: form.release_ref.trim() || undefined,
      });
      await reload();
      onChanged();
      setForm(f => ({ ...f, released_to: "", release_ref: "" }));
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not record the release.");
    } finally { setBusy(false); }
  }

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,20,28,.45)",
      display: "flex", justifyContent: "flex-end", zIndex: 60 }}
      onClick={onClose}>
      <div className="card" style={{ width: "min(560px, 100%)", height: "100%",
        borderRadius: 0, overflowY: "auto", margin: 0 }}
        onClick={e => e.stopPropagation()}>
        <div className="card-h">
          <h3>{row.program_name} · {row.period}</h3>
          <span className="sub">{row.broker_name ?? "unattributed"}</span>
          <div className="right">
            <span className="linkish" onClick={onClose}
              role="button" aria-label="Close"><X size={14} /></span>
          </div>
        </div>

        <div style={{ padding: "16px 20px" }}>
          {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}

          <h4 style={{ margin: "0 0 10px", fontSize: 13 }}>
            Every file sent for this period
          </h4>
          {versions === null && <p className="muted" style={{ fontSize: 13 }}>Loading…</p>}
          {versions?.length === 0 && (
            <div className="note">Nothing has been submitted for this period yet.</div>
          )}
          {versions?.map(v => (
            <div key={v.id} className="card" style={{ marginBottom: 12, padding: "12px 14px",
              boxShadow: "none" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8,
                flexWrap: "wrap", marginBottom: 6 }}>
                <span className={`badge ${v.kind === "original" ? "b-mut" : "b-warn"}`}>
                  <span className="d" />
                  {v.kind === "original" ? "First version" : `Correction ${v.version_no - 1}`}
                </span>
                <span className="mono" style={{ fontSize: 12.5 }}>
                  arrived {fmtFull(v.received_at)}
                </span>
                {/* "the file said July" and "we assumed July" are different
                    levels of confidence, and an operator checking a wrong month
                    needs to know which one this was. */}
                {v.period_source === "oldest_open" && (
                  <span className="sub" title="No period could be read from the file, so this was matched to the oldest period still open.">
                    period assumed
                  </span>
                )}
              </div>
              {v.source_filename && (
                <div className="sub" style={{ marginBottom: 4 }}>{v.source_filename}</div>
              )}
              <div className="sub">
                {v.released_at
                  ? <>Sent onward {fmtFull(v.released_at)}
                    {v.released_to && <> to <b>{v.released_to}</b></>}
                    {v.release_ref && <> · ref {v.release_ref}</>}</>
                  : "Not sent onward yet"}
              </div>
            </div>
          ))}

          {/* Recording a release. Producing a file and sending it are separate
              acts, which is why this is a deliberate entry and not something
              the system infers from the file existing. */}
          {(versions?.length ?? 0) > 0 && (
            <>
              <h4 style={{ margin: "18px 0 10px", fontSize: 13 }}>
                Record that you sent it on
              </h4>
              <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px" }}>
                Attaches to the newest version — that is the file the recipient
                would have got.
              </p>
              <div className="field">
                <label>Who received it</label>
                <input value={form.released_to} placeholder="e.g. Munich Re"
                  onChange={e => setForm(f => ({ ...f, released_to: e.target.value }))} />
              </div>
              <div className="field">
                <label>When</label>
                <input type="date" value={form.released_on}
                  onChange={e => setForm(f => ({ ...f, released_on: e.target.value }))} />
              </div>
              <div className="field">
                <label>Their reference <span className="sub">optional</span></label>
                <input value={form.release_ref} placeholder="if they give you one"
                  onChange={e => setForm(f => ({ ...f, release_ref: e.target.value }))} />
              </div>
              <button className="btn pri" onClick={save} disabled={busy}>
                <Send size={13} /> {busy ? "Saving…" : "Record the send"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CHASING, WITH A CONFIRM STEP.
//
// Follows the prototype's chase dialog: it names what is late, who would hear
// about it and how far over the deadline it is, and gives you a line of your own
// to add before anything is written down.
//
// One component covers both entry points. "Chase them" passes a single row;
// "Chase what is late" passes every overdue row on the board. The difference is
// only how many rows arrive, so the dialog counts them rather than branching.
//
// The dialog is deliberately honest about the state of the feature: it records
// the chase against each period and keeps your note with it. Sending the mail is
// not wired up yet, and the dialog says so rather than letting the button imply
// an email left the building.
// ---------------------------------------------------------------------------
function ChaseModal({ rows, onClose, onConfirm }: {
  rows: BoardRow[];
  onClose: () => void;
  onConfirm: (rows: BoardRow[], note: string) => Promise<void>;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const single = rows.length === 1;
  // Everyone who would hear about it, deduplicated — one broker can be late on
  // several programmes, and telling them three times is one conversation.
  const recipients = useMemo(() => {
    const byEmail = new Map<string, BrokerContact & { broker: string }>();
    for (const r of rows) {
      for (const c of r.contacts ?? []) {
        if (!byEmail.has(c.email)) {
          byEmail.set(c.email, { ...c, broker: r.broker_name ?? "this broker" });
        }
      }
    }
    return [...byEmail.values()];
  }, [rows]);

  // A broker with no user account has nobody to tell. Worth saying out loud —
  // it is the one case where recording a chase achieves nothing at all.
  const unreachable = useMemo(
    () => [...new Set(rows.filter(r => (r.contacts ?? []).length === 0)
      .map(r => r.broker_name ?? "an unnamed broker"))],
    [rows]);

  async function go() {
    setBusy(true); setErr(null);
    try {
      await onConfirm(rows, note.trim());
      onClose();
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not record the chase.");
      setBusy(false);
    }
  }

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,20,28,.45)",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: 24, zIndex: 70 }}
      onClick={onClose}>
      <div className="card" style={{ width: "min(560px, 100%)", maxHeight: "85vh",
        overflowY: "auto", margin: 0 }}
        role="dialog" aria-modal="true"
        onClick={e => e.stopPropagation()}>
        <div className="card-h">
          <h3>{single
            ? `Chase ${rows[0].broker_name ?? "this broker"}`
            : "Chase everything that is late"}</h3>
          <div className="right">
            <span className="linkish" onClick={onClose}
              role="button" aria-label="Close"><X size={14} /></span>
          </div>
        </div>

        <div style={{ padding: "16px 20px" }}>
          {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}

          <div className="note" style={{ marginBottom: 14 }}>
            {single ? (
              <>
                Their <b>{rows[0].period}</b> file for {rows[0].program_name} was
                due on <b>{fmtFull(rows[0].due_date)}</b> and has still not
                arrived. This records that you asked.
              </>
            ) : (
              <>
                One reminder each, for the person who sends the file. Nothing goes
                to a broker who is up to date.
              </>
            )}
          </div>

          {/* WHAT IS LATE. One row per period for a bulk chase; for a single
              chase the facts are broken out the way the prototype does it. */}
          {single ? (
            <>
              <div className="kv">
                <span className="k">Goes to</span>
                <span className="v">
                  {recipients.length === 0
                    ? <span className="muted">nobody on record</span>
                    : recipients.map(c => (
                      <span key={c.email} style={{ display: "block" }}>
                        {c.name} · {c.email}
                        {/* An invited contact is the right address but an
                            unconfirmed one — say so before it is relied on. */}
                        {c.status === "invited" && (
                          <span className="sub"> · invited, not signed in yet</span>
                        )}
                      </span>
                    ))}
                </span>
              </div>
              <div className="kv">
                <span className="k">About</span>
                <span className="v">{rows[0].program_name} · {rows[0].period}</span>
              </div>
              <div className="kv">
                <span className="k">Days overdue</span>
                <span className="v">{rows[0].days_over ?? 0}</span>
              </div>
              {rows[0].chase_count > 0 && (
                <div className="kv">
                  <span className="k">Already chased</span>
                  <span className="v">
                    {rows[0].chase_count === 1 ? "once" : `${rows[0].chase_count} times`}
                    {rows[0].chased_at && `, last on ${fmtFull(rows[0].chased_at)}`}
                  </span>
                </div>
              )}
            </>
          ) : (
            rows.map(r => (
              <div className="kv" key={r.id}>
                <span className="k">{r.broker_name ?? "Unnamed broker"}</span>
                <span className="v">
                  {r.program_name} · {r.period} ·{" "}
                  <b>{plural(r.days_over ?? 0, "day")} over</b>
                </span>
              </div>
            ))
          )}

          {/* Nobody to tell is a real outcome, not an edge case to hide. */}
          {unreachable.length > 0 && (
            <div className="note warn" style={{ marginTop: 14 }}>
              <AlertTriangle size={13} style={{ verticalAlign: "-2px" }} />{" "}
              {unreachable.length === 1
                ? <><b>{unreachable[0]}</b> has nobody on record to contact.</>
                : <><b>{unreachable.length} of these brokers</b> have nobody on
                  record to contact: {unreachable.join(", ")}.</>}{" "}
              The chase is still recorded against the period, but you will have to
              reach them yourself.
            </div>
          )}

          <div className="field" style={{ margin: "14px 0 0" }}>
            <label>Anything to add?</label>
            <input value={note} onChange={e => setNote(e.target.value)}
              placeholder="Optional — e.g. we need this before month end" />
            <div className="hint">Kept with the chase, so what you asked for is
              readable later.</div>
          </div>

          {/* The one thing this dialog must not do is imply an email left the
              building. Sending is not wired up; the record is. */}
          <div className="note" style={{ marginTop: 14 }}>
            Every reminder is recorded, so if it ever goes to a dispute you can
            show what was asked and when. <b>Email delivery is not connected
            yet</b> — for now this writes the reminder to each period's record
            rather than sending it.
          </div>

          <div style={{ display: "flex", gap: 10, marginTop: 16 }}>
            <button className="btn pri" onClick={go} disabled={busy}>
              <Send size={13} />{" "}
              {busy ? "Recording…"
                : single ? "Send the reminder"
                : `Send ${plural(rows.length, "reminder")}`}
            </button>
            <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}
