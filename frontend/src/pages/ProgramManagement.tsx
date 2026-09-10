// Program Management — oversight of ONE carrier's program book.
//
// Deliberately separate from /home. That dashboard is operational ("what did I
// process today" — runs, exceptions, turnaround); this one answers the
// oversight questions: how big is the book, how is it spread, what is coming
// due, and what has already slipped.
//
// The screen is carrier-scoped by design: with no ?carrier= in the URL it shows
// a carrier picker first, and only then the dashboard. Every tile, chart and
// table row below belongs to the selected carrier.
//
// Data: GET /program-management/stats?mga&carrier_party_id&horizon_days for the
// dashboard, and GET /program-management/carriers for the picker — the latter
// already returns each carrier's program count and overdue-bordereaux count, so
// the picker can show what distinguishes one carrier from another instead of a
// grid of identical cards.
// Charts are hand-rolled inline SVG/CSS — same approach as the platform
// dashboard, so no charting dependency is added.
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

// Palette aligned with proto.css tokens (mirrors KavachioAdminDashboard).
const C = {
  ink: "#0E1320", muted: "#566071", faint: "#8B93A2", line: "#E5E8EE",
  blue: "#3149C6", ok: "#0E9F6E", warn: "#C77A12", crit: "#D32F45",
  info: "#17A2B8", purple: "#7C5CFC", slate: "#5B6B85",
};

// Stage → colour. The stage LABELS come from the API (derived from real
// columns), so anything unrecognised still renders with a fallback colour
// rather than disappearing from the donut.
const STAGE_COLOR: Record<string, string> = {
  "Active": C.ok,
  "Under Review": C.warn,
  "Runoff": C.slate,
  "Onboarding": C.info,
  "In Setup": C.purple,
  "Draft": C.faint,
};
const FALLBACK = [C.blue, C.purple, C.info, C.slate, C.warn, C.ok];

// Program Book page size. 10 to match every other page-level list (Carriers,
// Users & Roles, Tenants, Recent Runs) — ProgramCalendar's 12 is the odd one out
// and it is an embedded component, not a page.
//
// Client-side on purpose: the endpoint is one aggregate call whose KPI cards and
// charts are computed over the WHOLE book, so every row is already in hand —
// paging it server-side would cost a second request without reducing the first,
// and would risk the table disagreeing with the charts above it.
const PAGE_SIZE = 10;

// Window for the "newly added" figure on the Active Programs card. Same presets
// and labels as the platform dashboard's filter, so the two read alike. Applied
// CLIENT-SIDE: every program is already in the payload, so switching the range
// re-counts instantly instead of refetching the whole dashboard.
const NEW_RANGES: { value: string; label: string; days: number | null }[] = [
  { value: "7d",  label: "Last 7 Days",    days: 7 },
  { value: "30d", label: "Last 30 Days",   days: 30 },
  { value: "90d", label: "Last 90 Days",   days: 90 },
  { value: "12m", label: "Last 12 Months", days: 365 },
  { value: "all", label: "All Time",       days: null },
];

// Timing buckets colour by urgency, matching the KPI cards above them.
const BUCKET_COLOR: Record<string, string> = {
  overdue: C.crit, this_week: C.crit, this_month: C.warn, "30_60": C.ok, "60_90": C.ok,
};

type Party = { id: number; legal_name: string; dba_name?: string | null; party_type?: string | null };

// One row of the carrier picker — mirrors /program-management/carriers items.
// `programs` and `overdue_bordereaux` are what make the picker worth looking at:
// they are the only things that distinguish one carrier from another here.
type PickerRow = {
  id: number; legal_name: string; dba_name: string | null;
  party_type: string | null; programs: number; overdue_bordereaux: number;
};

// Carriers per page in the picker — 10, matching every other page-level list.
const CARRIER_PAGE_SIZE = 10;

type Stats = {
  carrier: { id: number; legal_name: string; dba_name: string | null; party_type: string | null };
  horizon_days: number;
  as_of: string;
  active_programs: number;
  new_active_30d: number;
  programs_under_review: number;
  overdue_bordereaux: { programs: number; submissions: number };
  overdue_audits: number | null;
  programs_per_stage: { label: string; value: number }[];
  active_per_segment: { label: string; value: number }[];
  review_buckets: { key: string; label: string; value: number }[];
  programs: {
    program_id: number; program_name: string; business_segment: string | null;
    segment_label: string;
    product_line: string | null; status: string; stage: string;
    bdx_frequency: string | null; inception_dt: string | null; term_end: string | null;
    created_at: string | null;
    days_to_term_end: number | null; contract_id: number | null;
    contract_status: string | null; overdue_bordereaux: number;
  }[];
  total_programs: number;
  calendar_configured: boolean;
};

const nf = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString());

/** A date the way oversight reads it — "12 Mar 2026", or an em-dash. */
const fmtDate = (iso: string | null) => {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
};

/** "in 42 days" / "18 days overdue" / "—". */
const fmtDays = (d: number | null) => {
  if (d == null) return "—";
  if (d < 0) return `${Math.abs(d)}d overdue`;
  if (d === 0) return "due today";
  return `in ${d}d`;
};

const card: React.CSSProperties = {
  background: "#fff", border: `1px solid ${C.line}`, borderRadius: 14, padding: 18,
};

// Carrier-picker table cells. Digits use tabular-nums so the columns line up
// when the counts differ in width.
const thBase: React.CSSProperties = {
  fontSize: 10, fontWeight: 700, letterSpacing: ".08em", textTransform: "uppercase",
  color: C.faint, padding: "10px 14px", background: "#F7F8FB",
  borderBottom: `1px solid ${C.line}`, whiteSpace: "nowrap",
};
const thL: React.CSSProperties = { ...thBase, textAlign: "left" };
const thC: React.CSSProperties = { ...thBase, textAlign: "center" };
const tdBase: React.CSSProperties = { padding: "12px 14px", fontSize: 13 };
const tdNum: React.CSSProperties = {
  ...tdBase, textAlign: "center", fontWeight: 700,
  fontVariantNumeric: "tabular-nums",
};

function cardHead(title: string, sub?: string, info?: string) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 12 }}>
      <div style={{ fontSize: 15, fontWeight: 700, color: C.ink, display: "flex", alignItems: "center", gap: 5 }}>
        {title}{info && <InfoTip text={info} />}
      </div>
      {sub && <div style={{ fontSize: 12, color: C.faint, fontWeight: 500 }}>{sub}</div>}
    </div>
  );
}

// ---------- donut (programs per stage) ----------
function Donut({ segments, total }: {
  segments: { label: string; value: number; color: string }[]; total: number;
}) {
  const R = 58, SW = 20, cx = 74, cy = 78, Cc = 2 * Math.PI * R;
  const sum = segments.reduce((a, s) => a + s.value, 0) || 1;
  let off = 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
      <svg width="148" height="156" viewBox="0 0 148 156">
        <circle cx={cx} cy={cy} r={R} fill="none" stroke="#F0F2F7" strokeWidth={SW} />
        {segments.map((s, i) => {
          const len = (Cc * s.value) / sum;
          const el = (
            <circle key={i} cx={cx} cy={cy} r={R} fill="none" stroke={s.color} strokeWidth={SW}
              strokeDasharray={`${len} ${Cc - len}`} strokeDashoffset={-off}
              transform={`rotate(-90 ${cx} ${cy})`}>
              <title>{`${s.label}: ${s.value} (${Math.round((s.value / sum) * 100)}%)`}</title>
            </circle>
          );
          off += len; return el;
        })}
        <text x={cx} y={cy - 1} fontSize="24" fontWeight="800" fill={C.ink} textAnchor="middle">{total}</text>
        <text x={cx} y={cy + 17} fontSize="10.5" fill={C.muted} textAnchor="middle">programs</text>
      </svg>
      <div style={{ flex: 1 }}>
        {segments.map((s, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0" }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: s.color }} />
            <span style={{ fontSize: 12.5, color: C.ink, fontWeight: 600, flex: 1 }}>{s.label}</span>
            <span style={{ fontSize: 12.5, color: C.ink, fontWeight: 700 }}>{s.value}</span>
            <span style={{ fontSize: 11, color: C.faint, width: 38, textAlign: "right" }}>
              {Math.round((s.value / sum) * 100)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------- page ----------
export default function ProgramManagement() {
  const mga = currentMga();
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
  const carrierParam = sp.get("carrier");
  const carrierId = carrierParam ? Number(carrierParam) : null;

  const [carriers, setCarriers] = useState<Party[]>([]);
  const [q, setQ] = useState("");
  const [d, setD] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // The picker's own list: server-paged and server-searched, and carrying the
  // counts that make a carrier worth opening. Skipped entirely once a carrier is
  // chosen — the hook must still be CALLED (hook order), but the fetcher short-
  // circuits rather than firing a request the dashboard will never read.
  const qDebounced = useDebouncedValue(q, 300);
  const picker = useServerList<PickerRow>(
    (pg, size) =>
      carrierId != null && !Number.isNaN(carrierId)
        ? Promise.resolve({ items: [], total: 0 })
        : api.get<{ items: PickerRow[]; total: number }>(
            "/program-management/carriers",
            { params: { mga, q: qDebounced || undefined, page: pg, page_size: size } },
          ).then(r => r.data),
    `${mga}|${qDebounced}|${carrierId ?? ""}`,
    CARRIER_PAGE_SIZE,
  );

  // The FULL carrier list, unpaged — this powers the switcher in the page head
  // once a carrier is chosen, which needs every option, not one page of them.
  useEffect(() => {
    api.get<{ items: Party[] }>("/parties", { params: { mga } })
      .then(r => setCarriers(r.data.items ?? []))
      .catch(() => setCarriers([]));
  }, [mga]);

  useEffect(() => {
    if (carrierId == null || Number.isNaN(carrierId)) { setD(null); return; }
    setLoading(true);
    api.get<Stats>("/program-management/stats",
      { params: { mga, carrier_party_id: carrierId } })
      .then(r => { setD(r.data); setErr(null); })
      .catch(() => setErr("Could not load this carrier's program book."))
      .finally(() => setLoading(false));
  }, [mga, carrierId]);

  const [page, setPage] = useState(1);
  const [newRange, setNewRange] = useState("30d");

  // Derived above the effects below (and above the carrier-picker early return,
  // so hook order never changes between the two renders of this component).
  const bookRows = d?.programs ?? [];
  const totalItems = bookRows.length;

  // Programs that are WRITING BUSINESS and were created inside the chosen window
  // — the same population the Active Programs headline counts, so the footer can
  // never describe a different set of programs from the number above it.
  const rangeMeta = NEW_RANGES.find(r => r.value === newRange) ?? NEW_RANGES[1];
  const newInRange = bookRows.filter(p => {
    if (p.stage !== "Active" && p.stage !== "Under Review") return false;
    if (rangeMeta.days == null) return true;
    if (!p.created_at) return false;
    const t = new Date(p.created_at).getTime();
    return Number.isFinite(t)
      && t >= Date.now() - rangeMeta.days * 86_400_000;
  }).length;
  const pageCount = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const pageRows = bookRows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // A new carrier is a new book — page 3 of the last one means nothing here.
  useEffect(() => { setPage(1); }, [carrierId]);
  // Clamp if the visible book shrinks under the current page (carrier switch
  // races, or a reload returning fewer rows).
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [pageCount, page]);

  // How many carriers on THIS page have something late. Deliberately not a
  // whole-tenant figure: the endpoint pages, so claiming a book-wide total from
  // one page's rows would be wrong.
  const attention = picker.items.filter(c => c.overdue_bordereaux > 0).length;

  const selectCarrier = (id: number) => setSp({ carrier: String(id) });

  // A carrier with no programs has no book to open — the row says "Set up", so
  // it must go somewhere you can actually set one up. /programs/new takes the
  // carrier as ?party= and pre-fills it, so the flow continues instead of
  // dead-ending on an empty dashboard.
  const openCarrier = (c: PickerRow) =>
    c.programs === 0 ? nav(`/programs/new?party=${c.id}`) : selectCarrier(c.id);

  // ---- step 1: pick a carrier -------------------------------------------
  if (carrierId == null || Number.isNaN(carrierId)) {
    return (
      <div className="proto">
        <div className="view full">
          <div className="page-head">
            <div className="t">
              <h2>Program Management</h2>
              <p>Choose a carrier to see the state of its program book.</p>
            </div>
          </div>
          <div className="card">
            <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
                          padding: "14px 16px", borderBottom: `1px solid ${C.line}` }}>
              <input placeholder="Search carriers…" value={q} onChange={e => setQ(e.target.value)}
                style={{ flex: "1 1 220px", maxWidth: 320, border: `1px solid ${C.line}`,
                         borderRadius: 9, padding: "9px 12px", fontSize: 13 }} />
              {/* Only worth saying when it is true — silence when nothing is late. */}
              {attention > 0 && (
                <span style={{ fontSize: 11.5, fontWeight: 700, color: C.crit,
                               background: "#FBE7EA", borderRadius: 999, padding: "4px 11px" }}>
                  {attention} {attention === 1 ? "carrier needs" : "carriers need"} attention
                </span>
              )}
            </div>

            {picker.loading && picker.items.length === 0 ? (
              <div className="empty" style={{ padding: 28 }}>Loading carriers…</div>
            ) : picker.items.length === 0 ? (
              <div className="empty" style={{ padding: 28 }}>
                {q.trim()
                  ? "No carrier matches that search."
                  : <>No carriers yet — add one from{" "}
                      <Link className="linkish" to="/users">Users &amp; Roles</Link>{" "}
                      to get started.</>}
              </div>
            ) : (
              <>
                <div style={{ overflowX: "auto" }}>
                  <table className="tbl" style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        <th style={thL}>Carrier</th>
                        <th style={thC}>Programs</th>
                        <th style={thC}>Overdue BDX</th>
                        <th style={{ ...thBase, width: 90, textAlign: "right" }}>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {picker.items.map(c => {
                        const late = c.overdue_bordereaux > 0;
                        const empty = c.programs === 0;
                        return (
                          <tr key={c.id} onClick={() => openCarrier(c)}
                            title={c.dba_name || undefined}
                            style={{ cursor: "pointer", borderBottom: `1px solid ${C.line}` }}>
                            {/* Left edge carries the state, so a row reads before
                                its numbers do: red = something late, grey = quiet. */}
                            <td style={{ ...tdBase, fontWeight: 700, color: C.ink, fontSize: 13.5,
                                         boxShadow: `inset 3px 0 0 ${late ? C.crit : C.line}` }}>
                              {c.legal_name}
                              {c.dba_name && (
                                <span style={{ display: "block", fontSize: 11, fontWeight: 500,
                                               color: C.faint, marginTop: 2 }}>{c.dba_name}</span>
                              )}
                            </td>
                            <td style={{ ...tdNum, color: empty ? C.faint : C.ink }}>
                              {empty ? "—" : c.programs}
                            </td>
                            <td style={{ ...tdNum, color: late ? C.crit : C.faint,
                                         fontWeight: late ? 800 : 500 }}>
                              {late ? c.overdue_bordereaux : "—"}
                            </td>
                            <td style={{ ...tdBase, textAlign: "right", whiteSpace: "nowrap" }}>
                              {/* `linkish` rather than a local colour, so the row
                                  action reads the same here as in All Carriers and
                                  Users & Roles. A carrier with no programs needs
                                  setting up, not reviewing — say so instead of
                                  opening an empty book. */}
                              <span className="linkish">{empty ? "Set up →" : "View →"}</span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {picker.pageCount > 1 && (
                  <Pagination page={picker.page} pageCount={picker.pageCount}
                    pageSize={CARRIER_PAGE_SIZE} totalItems={picker.total}
                    onPageChange={picker.setPage} noun="carriers" />
                )}
              </>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ---- step 2: the carrier's dashboard -----------------------------------
  const stageSegments = (d?.programs_per_stage ?? []).map((s, i) => ({
    label: s.label, value: s.value, color: STAGE_COLOR[s.label] ?? FALLBACK[i % FALLBACK.length],
  }));
  const segMax = Math.max(1, ...(d?.active_per_segment ?? []).map(s => s.value));
  // The overdue bucket only earns a bar when something is actually overdue —
  // an always-present empty "Overdue" column reads as a standing alarm.
  const buckets = (d?.review_buckets ?? []).filter(b => b.key !== "overdue" || b.value > 0);
  const bucketMax = Math.max(1, ...buckets.map(b => b.value));
  const od = d?.overdue_bordereaux;
  // Deep link into My Calendar with the picker already narrowed. The carrier is
  // always known here; the program only when exactly ONE is behind — with several
  // there is no single right answer, so the carrier is pre-selected and the user
  // picks which one to chase.
  const behind = (d?.programs ?? []).filter(p => p.overdue_bordereaux > 0);
  const calendarHref = `/calendar?carrier=${carrierId}`
    + (behind.length === 1 ? `&program=${behind[0].program_id}` : "");

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Program Management</h2>
            <p>
              {d ? <>Program book for <b>{d.carrier.legal_name}</b> · {d.total_programs} program
                {d.total_programs === 1 ? "" : "s"}</>
                : "Loading the program book…"}
            </p>
          </div>
          <div className="actions" style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <select value={String(carrierId)} onChange={e => selectCarrier(Number(e.target.value))}
              style={{ border: `1px solid ${C.line}`, borderRadius: 9, padding: "8px 12px",
                       fontSize: 13, fontWeight: 600, color: C.ink, background: "#fff", cursor: "pointer" }}>
              {/* The directory list excludes deactivated parties, so a bookmarked
                  (or since-deactivated) carrier would otherwise be missing from
                  the switcher and the <select> would silently display someone
                  else's name over this carrier's numbers. */}
              {d && !carriers.some(c => c.id === d.carrier.id) && (
                <option value={d.carrier.id}>{d.carrier.legal_name}</option>
              )}
              {carriers.map(c => <option key={c.id} value={c.id}>{c.legal_name}</option>)}
            </select>
            <Link className="btn" to="/program-management">Change Carrier</Link>
          </div>
        </div>

        {err && <div className="card" style={{ padding: 16, color: C.crit }}>{err}</div>}
        {loading && !d && <div className="card" style={{ padding: 20 }}><div className="empty">Loading…</div></div>}

        {d && (
          <div key={carrierId}>
            {/* ---- the four KPI cards ---- */}
            <div className="tiles" style={{ marginBottom: 18 }}>
              <div className="tile kd-in">
                <div className="k">
                  Active Programs
                  <InfoTip text={"Programs writing business for this carrier, added within the "
                    + `selected window (${rangeMeta.label.toLowerCase()}). Choose All Time for the `
                    + "full live book. Runoff and not-yet-live programs are never counted."} />
                </div>
                <div className="v">{nf(newInRange)}</div>
                {/* The window is the reader's to choose, so the footer states the
                    count and the picker sets the window it counts over. Counted
                    client-side from the rows already in hand — no refetch. */}
                <div className="foot" style={{ display: "flex", alignItems: "center",
                  gap: 8, flexWrap: "wrap" }}>
                  <select className="fbar-select" aria-label="Window for active programs"
                    value={newRange} onChange={e => setNewRange(e.target.value)}
                    style={{ fontSize: 11.5, padding: "3px 22px 3px 8px" }}>
                    {NEW_RANGES.map(r => (
                      <option key={r.value} value={r.value}>{r.label}</option>
                    ))}
                  </select>
                  {/* The whole live book stays on screen while a window is applied,
                      so a narrowed headline is never mistaken for the total — and
                      it is what the stage donut below counts. */}
                  {rangeMeta.days != null && (
                    <span>of {nf(d.active_programs)} active</span>
                  )}
                </div>
              </div>

              <div className="tile kd-in">
                <div className="k">
                  Programs Under Review
                  <InfoTip text={`Active programs whose contract term ends within the next ${d.horizon_days} days, `
                    + "or has already passed — the continue / reprice / terminate decisions coming due."} />
                </div>
                <div className="v">{nf(d.programs_under_review)}</div>
                <div className="foot">term ends within {d.horizon_days} days</div>
              </div>

              <div className={`tile kd-in${(od?.programs ?? 0) > 0 ? " alert" : ""}`}>
                <div className="k">
                  Overdue Bordereaux
                  <InfoTip text={"Programs with at least one bordereau past its due date and not yet received. "
                    + "The sub-figure is the total number of late submissions, so a program missing several "
                    + "still reads as one problem to chase."} />
                </div>
                <div className="v" style={{ color: (od?.programs ?? 0) > 0 ? C.crit : undefined }}>
                  {d.calendar_configured ? nf(od?.programs) : "—"}
                  {d.calendar_configured && (od?.programs ?? 0) > 0 && <small>            programs affected</small>}
                </div>
                <div className="foot">
                  {!d.calendar_configured
                    ? <Link className="linkish" to={`/calendar?carrier=${carrierId}`}>
                        No submission schedule set up →</Link>
                    : (od?.programs ?? 0) === 0
                      ? "Nothing overdue"
                      : <Link className="linkish" to={calendarHref}>
                          {nf(od?.submissions)} bordereaux overdue →</Link>}
                </div>
              </div>

              {/* Underwriting audits have no source table. Showing the card with a
                  dash keeps the layout the document asked for while being honest
                  that we don't track it — a 0 here would read as "all clear". */}
              <div className="tile kd-in">
                <div className="k">
                  Overdue Audits
                  <InfoTip text={"Underwriting audits past their due date. Kavachio does not track UW audit "
                    + "schedules yet, so this card has no source data — it is shown for completeness."} />
                </div>
                <div className="v" style={{ color: C.faint }}>—</div>
                <div className="foot">Not tracked yet</div>
              </div>
            </div>

            {/* ---- the three charts ---- */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 18, marginBottom: 18 }}>
              <div className="kd-in" style={card}>
                {cardHead("Programs per Stage", `${d.total_programs} total`,
                  "Where each program sits in its lifecycle. Stages are derived from the program's status "
                  + "and its contract term: Onboarding (no contract yet), In Setup (contract still being "
                  + "processed), Active, Under Review (term ending inside the look-ahead window) and "
                  + "Runoff (term already ended). Each program appears in exactly one stage.")}
                {stageSegments.length === 0
                  ? <div className="empty">No programs for this carrier.</div>
                  : <Donut segments={stageSegments} total={d.total_programs} />}
              </div>

              <div className="kd-in" style={card}>
                {cardHead("Active Programs per Segment", `${d.active_programs} active`,
                  "Active programs grouped by their business segment, exactly as recorded on the program. "
                  + "Programs with no segment set are grouped as “Unspecified”.")}
                {d.active_per_segment.length === 0
                  ? <div className="empty">No active programs.</div>
                  : d.active_per_segment.map((s, i) => (
                    // Segment names are free text and can run to a full sentence,
                    // so the row truncates and carries the full label as a tooltip.
                    <div key={s.label} style={{ margin: "10px 0" }} title={`${s.label} — ${s.value}`}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 10, fontSize: 12.5, marginBottom: 4 }}>
                        <span style={{ color: C.ink, fontWeight: 600, minWidth: 0, overflow: "hidden",
                                       textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.label}</span>
                        <span style={{ color: C.ink, fontWeight: 700, flexShrink: 0 }}>{s.value}</span>
                      </div>
                      <div style={{ height: 10, background: "#F0F2F7", borderRadius: 5 }}>
                        <div className="kd-bar" style={{
                          ["--kd-w" as any]: `${(s.value / segMax) * 100}%`, height: "100%",
                          background: FALLBACK[i % FALLBACK.length], borderRadius: 5,
                          ["--kd-d" as any]: `${120 + i * 70}ms`,
                        }} />
                      </div>
                    </div>
                  ))}
              </div>

              <div className="kd-in" style={card}>
                {cardHead("Upcoming Reviews & Continuations", `next ${d.horizon_days} days`,
                  "The same programs the “Under Review” card counts, spread across a timeline by how soon "
                  + "the contract term ends — so end-of-term pressure is visible before it slips.")}
                {d.programs_under_review === 0 ? (
                  <div className="empty">Nothing due in the next {d.horizon_days} days.</div>
                ) : (
                  <div style={{ display: "flex", alignItems: "flex-end", gap: 10, height: 160, paddingTop: 8 }}>
                    {buckets.map((b, i) => (
                      <div key={b.key} style={{ flex: 1, textAlign: "center" }}>
                        <div style={{ fontSize: 12.5, fontWeight: 800, color: C.ink, marginBottom: 4 }}>
                          {b.value || ""}
                        </div>
                        <div style={{ height: 100, display: "flex", alignItems: "flex-end" }}>
                          <div style={{
                            width: "100%", height: `${Math.max(4, (b.value / bucketMax) * 100)}%`,
                            background: BUCKET_COLOR[b.key] ?? C.blue, borderRadius: "5px 5px 0 0",
                            opacity: b.value === 0 ? 0.25 : 1,
                            transition: `height .6s cubic-bezier(.2,.7,.3,1) ${i * 80}ms`,
                          }} title={`${b.label}: ${b.value}`} />
                        </div>
                        <div style={{ fontSize: 10.5, color: C.muted, marginTop: 6 }}>{b.label}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* ---- the program book, row by row ---- */}
            <div className="kd-in" style={{ ...card, padding: 0 }}>
              <div className="card-h" style={{ padding: "16px 18px" }}>
                <h3>Program Book</h3>
                <span className="sub">
                  Every program for {d.carrier.legal_name}, most urgent term first.
                  {totalItems > PAGE_SIZE && ` · ${totalItems} programs`}
                </span>
                <div className="right">
                  <Link className="linkish" to={`/parties/${d.carrier.id}`}>Carrier Details →</Link>
                </div>
              </div>
              {d.programs.length === 0 ? (
                <div className="empty" style={{ padding: 24 }}>
                  No programs for this carrier yet — <Link className="linkish" to="/programs">add one</Link>.
                </div>
              ) : (
                <div className="tbl-wrap">
                  <table>
                    <thead><tr>
                      <th className="l">Program</th><th>Segment</th><th>Stage</th>
                      <th>Inception</th><th>Term End</th><th>Due In</th>
                      <th>BDX</th><th className="r">Overdue BDX</th>
                    </tr></thead>
                    <tbody>
                      {pageRows.map((p, i) => {
                        const late = p.overdue_bordereaux > 0;
                        const soon = p.days_to_term_end != null && p.days_to_term_end <= 30;
                        return (
                          <tr key={p.program_id} className="kd-tr"
                            style={{ ["--kd-d" as any]: `${i * 40}ms` }}>
                            <td className="l"><b>{p.program_name}</b></td>
                            {/* The classified segment; the contract's own wording
                                stays available as the cell tooltip. */}
                            <td className="muted" title={p.business_segment || undefined}>
                              {p.segment_label || "—"}
                            </td>
                            <td>
                              <span className="badge" style={{
                                background: `${STAGE_COLOR[p.stage] ?? C.slate}18`,
                                color: STAGE_COLOR[p.stage] ?? C.slate, fontWeight: 700,
                              }}>{p.stage}</span>
                            </td>
                            <td className="muted">{fmtDate(p.inception_dt)}</td>
                            <td className="muted">{fmtDate(p.term_end)}</td>
                            <td style={{ color: soon ? C.crit : C.muted, fontWeight: soon ? 700 : 400 }}>
                              {fmtDays(p.days_to_term_end)}
                            </td>
                            <td className="muted">{p.bdx_frequency || "—"}</td>
                            <td className="r" style={{ color: late ? C.crit : C.faint, fontWeight: late ? 700 : 400 }}>
                              {late ? p.overdue_bordereaux : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              {/* Only when it earns its place — a single page of programs needs no
                  controls, matching the calendar's period list. */}
              {pageCount > 1 && (
                <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
                  totalItems={totalItems} onPageChange={setPage} noun="programs" />
              )}
            </div>

            <div className="muted" style={{ fontSize: 11.5, marginTop: 16 }}>
              As of {fmtDate(d.as_of)}.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
