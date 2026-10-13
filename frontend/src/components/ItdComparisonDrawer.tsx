/**
 * One file's ITD comparison, row by row, in the client's classes — New,
 * Previously processed, Changed, Cancelled or reversed (and Skipped for a
 * duplicate or a row with no transaction id), each row saying which kind.
 * Opened from Files → Inbox ("View comparison").
 *
 * Two decisions live here, both the carrier admin's:
 *   - a DROPPED row (in an earlier file, missing from this one) is either
 *     removed on purpose or still live, which decides how the next file reads;
 *   - the whole file can be WITHDRAWN from the running total, which changes
 *     what the next file is compared against. The server refuses that when a
 *     later file was already compared against this one, and says so.
 */
import { useEffect, useState } from "react";
import {
  decideItdPosition, getArrivalItdRows, itdClassCounts, itdSummaryLine, withdrawArrivalItd,
  ITD_CLASSES, ITD_CLASS_LABEL, ITD_CLASS_OF, ITD_RESULT_DETAIL,
  type ItdClass, type ItdDecision, type ItdRowsResponse,
} from "../api/itd";
import { isTenantAdmin } from "../auth";
import { refusalMessage } from "../utils/directSetup";
import { fmtDate, periodLabel } from "../utils/date";
import { Pagination } from "./Pagination";

const PAGE_SIZE = 50;

const CLASS_TONE: Record<ItdClass, "ok" | "info" | "mut" | "warn"> = {
  new: "ok", previously_processed: "mut", changed: "info",
  cancelled_or_reversed: "info", held_back: "warn",
};

/** +2,000.00 / -500.00 — always signed, always two places. */
function signed(n: number): string {
  const s = Math.abs(n).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? "-" : "+"}${s}`;
}

// The app's tables centre their cells; rows of text read down the left edge.
const L: React.CSSProperties = { textAlign: "left" };

export function ItdComparisonDrawer({ arrivalId, onClose, onChanged }: {
  arrivalId: number | null;
  onClose: () => void;
  /** Something about the file changed (a withdrawal) — reload the list. */
  onChanged?: () => void;
}) {
  const canDecide = isTenantAdmin();
  const [data, setData] = useState<ItdRowsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cls, setCls] = useState<ItdClass | "">("");
  const [page, setPage] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);
  // Decisions made on dropped rows while the drawer is open, by position.
  const [decided, setDecided] = useState<Record<number, ItdDecision>>({});
  const [deciding, setDeciding] = useState<number | null>(null);
  const [rowErr, setRowErr] = useState<string | null>(null);
  const [confirmWithdraw, setConfirmWithdraw] = useState(false);
  const [withdrawing, setWithdrawing] = useState(false);
  const [withdrawErr, setWithdrawErr] = useState<string | null>(null);
  const [withdrawMsg, setWithdrawMsg] = useState<string | null>(null);

  // A different file starts clean — its filter, page and messages are its own.
  useEffect(() => {
    setData(null); setErr(null); setCls(""); setPage(1);
    setDecided({}); setDeciding(null); setRowErr(null);
    setConfirmWithdraw(false); setWithdrawing(false); setWithdrawErr(null); setWithdrawMsg(null);
  }, [arrivalId]);

  useEffect(() => {
    if (arrivalId == null) return;
    let alive = true;
    setErr(null);
    getArrivalItdRows(arrivalId, {
      cls: cls || undefined, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE,
    })
      .then(r => { if (alive) setData(r); })
      .catch(e => {
        if (alive) setErr(refusalMessage(e)
          ?? "We couldn't load the comparison for this file. Close it and try again.");
      });
    return () => { alive = false; };
  }, [arrivalId, cls, page, reloadKey]);

  useEffect(() => {
    if (arrivalId == null) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [arrivalId, onClose]);

  async function decide(positionId: number, decision: ItdDecision) {
    setDeciding(positionId); setRowErr(null);
    try {
      await decideItdPosition(positionId, decision);
      setDecided(d => ({ ...d, [positionId]: decision }));
    } catch (e) {
      setRowErr(refusalMessage(e) ?? "That decision could not be saved. Nothing was changed.");
    } finally { setDeciding(null); }
  }

  async function withdraw() {
    if (arrivalId == null) return;
    setWithdrawing(true); setWithdrawErr(null);
    try {
      const r = await withdrawArrivalItd(arrivalId);
      setConfirmWithdraw(false);
      setWithdrawMsg(`Withdrawn — ${r.rows_undone.toLocaleString()} row${r.rows_undone === 1 ? "" : "s"} `
        + "taken back out of the running total. The next file is compared without them.");
      setReloadKey(k => k + 1);
      onChanged?.();
    } catch (e) {
      // A 409 here is a sentence saying which later file depends on this one.
      setWithdrawErr(refusalMessage(e)
        ?? "This file could not be withdrawn. Nothing was changed.");
    } finally { setWithdrawing(false); }
  }

  const open = arrivalId != null;
  const counts = data?.class_counts ?? itdClassCounts(data?.counts ?? {});
  const allCount = Object.values(counts).reduce<number>((a, n) => a + (n ?? 0), 0);
  const s = data?.summary ?? null;
  const span = data?.period_start || data?.period_end
    ? `${fmtDate(data?.period_start, "…")} – ${fmtDate(data?.period_end, "…")}` : null;
  // The month the file is as at — its latest reported month. A row reported
  // in it is this month's; a row from before is one the ITD file repeats.
  const asAt = data?.period_end ? data.period_end.slice(0, 7) : null;
  const months = data?.month_counts;
  const showActions = canDecide && (data?.rows ?? []).some(r => r.result === "dropped" && r.position_id != null);
  const withdrawn = s?.status === "withdrawn";
  const pick = (c: ItdClass | "") => { setCls(c); setPage(1); };

  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${open ? " on" : ""}`} role="dialog" aria-modal="true"
        aria-hidden={!open} aria-label="File comparison"
        style={{ width: "min(1080px, 96vw)" }}>
        <div className="drawer-h">
          <div style={{ minWidth: 0 }}>
            <h4>{data?.filename ?? "File comparison"}</h4>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8,
                          fontSize: 12.5, color: "var(--p-muted)" }}>
              {data?.load_type === "itd" && <span className="badge b-info"><span className="d" />ITD</span>}
              {data?.load_type === "regular" && <span className="badge b-mut"><span className="d" />Regular</span>}
              {span && <span>Covers {span}</span>}
              {asAt && <span>· Current month: <b>{periodLabel(asAt)}</b></span>}
            </div>
            {data?.load_type_reason && (
              <div style={{ fontSize: 12.5, color: "var(--p-muted)", marginTop: 4 }}>
                {data.load_type_reason}</div>)}
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>

        <div className="drawer-b">
          {err && <div className="note crit" style={{ marginTop: 0 }}>{err}</div>}
          {!data && !err && <div className="muted">Loading…</div>}

          {data && (<>
            {s?.held === "older_itd" ? (
              <div className="note warn" style={{ marginTop: 0, marginBottom: 14 }}>
                <b>Not processed — a newer ITD file is already loaded.</b> Nothing from this
                file was added to the running total.
              </div>
            ) : s ? (
              <div className="note" style={{ marginTop: 0, marginBottom: 14 }}>
                {itdSummaryLine(s)}
                {months && months.current + months.earlier > 0 && (
                  <div style={{ marginTop: 4 }}>
                    {months.current.toLocaleString()} row{months.current === 1 ? "" : "s"} from
                    the current month{asAt ? ` (${periodLabel(asAt)})` : ""}
                    {months.earlier > 0 && <> · {months.earlier.toLocaleString()} row
                      {months.earlier === 1 ? "" : "s"} from earlier months</>}
                  </div>)}
                {months && months.current + months.earlier === 0 && months.unknown > 0 && (
                  <div style={{ marginTop: 4 }} className="muted">
                    The month of each row is shown for files processed from now on.</div>)}
                {withdrawn && <div style={{ marginTop: 4 }}>
                  {s.replaced_by
                    ? <><b>Replaced by version {s.replaced_by} of this submission.</b> It was never
                        delivered, so it was taken out of the running total and the corrected
                        version was compared without it.</>
                    : <><b>This file was undone.</b> Its rows no longer count as already
                        sent, so the next file is checked without them. The file itself is
                        kept here for the record.</>}</div>}
                {!!s.conflicts && (
                  <div style={{ marginTop: 4, color: "var(--p-warn-ink)" }}>
                    {s.conflicts.toLocaleString()} row{s.conflicts === 1 ? "" : "s"} could not be
                    added to the running total — another file changed the same transactions at
                    the same time.</div>)}
              </div>
            ) : null}

            {withdrawMsg && <div className="note ok" style={{ marginBottom: 14 }}>{withdrawMsg}</div>}

            <div className="filters" role="group" aria-label="Show rows by class">
              <button type="button" className="chip" aria-pressed={cls === ""}
                onClick={() => pick("")}>All <span className="n">{allCount.toLocaleString()}</span></button>
              {/* The client's four classes always show, so a 0 reads as an
                  answer; Skipped only when there is something skipped. */}
              {ITD_CLASSES.filter(c => c !== "held_back" || (counts[c] ?? 0) > 0 || c === cls).map(c => (
                <button key={c} type="button" aria-pressed={cls === c}
                  className={`chip${CLASS_TONE[c] === "warn" ? " warnc" : ""}`}
                  onClick={() => pick(cls === c ? "" : c)}>
                  {ITD_CLASS_LABEL[c]} <span className="n">{(counts[c] ?? 0).toLocaleString()}</span>
                </button>
              ))}
            </div>

            {rowErr && <div className="note crit" style={{ marginBottom: 12 }}>{rowErr}</div>}

            {data.rows.length === 0 ? (
              <div className="muted" style={{ padding: "24px 0", textAlign: "center" }}>
                {cls ? "No rows in this class." : "No rows were compared for this file."}
              </div>
            ) : (
              <div className="card">
                <div className="tbl-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th style={L}>Sheet</th><th style={L}>Month</th>
                        <th style={L}>Transaction ID</th>
                        <th style={L}>Class</th><th style={L}>Money Difference</th>
                        <th style={L}>Changed Fields</th><th style={L}>Note</th>
                        {showActions && <th />}
                      </tr>
                    </thead>
                    <tbody>
                      {data.rows.map(r => {
                        const deltas = Object.entries(r.money_delta ?? {}).filter(([, v]) => v !== 0);
                        const choice = r.position_id != null ? decided[r.position_id] : undefined;
                        return (
                          <tr key={r.id}>
                            <td style={L}>{r.sheet ?? <span className="faint">—</span>}</td>
                            <td style={{ ...L, whiteSpace: "nowrap" }}>
                              {r.row_period ? periodLabel(r.row_period)
                                : <span className="faint">—</span>}
                            </td>
                            <td className="mono" style={{ ...L, fontSize: 12, wordBreak: "break-all" }}>
                              {r.source_ref ?? <span className="faint">—</span>}</td>
                            <td style={{ ...L, whiteSpace: "nowrap" }}>
                              {(() => {
                                const c = r.class ?? ITD_CLASS_OF[r.result];
                                const detail = ITD_RESULT_DETAIL[r.result];
                                return (<>
                                  <span className={`badge b-${CLASS_TONE[c]}`}>
                                    <span className="d" />{ITD_CLASS_LABEL[c]}</span>
                                  {detail && <div className="sub">{detail}</div>}
                                </>);
                              })()}
                              {r.adjust_no != null && (
                                <div className="sub">Adjustment {r.adjust_no}</div>)}
                            </td>
                            <td className="mono" style={{ ...L, fontSize: 12, whiteSpace: "nowrap" }}>
                              {deltas.length === 0 ? <span className="faint">—</span>
                                : deltas.map(([col, v]) => (
                                    <div key={col}>
                                      <span className="faint">{col}</span>{" "}
                                      <span style={{ color: v < 0 ? "var(--p-crit-ink)" : "var(--p-ok-ink)" }}>
                                        {signed(v)}</span>
                                    </div>))}
                            </td>
                            <td style={{ ...L, fontSize: 12.5 }}>
                              {r.changed_fields?.length ? r.changed_fields.join(", ")
                                : <span className="faint">—</span>}</td>
                            <td className="muted" style={{ ...L, fontSize: 12.5 }}>
                              {r.note ?? <span className="faint">—</span>}</td>
                            {showActions && (
                              <td style={{ whiteSpace: "nowrap" }}>
                                {r.result === "dropped" && r.position_id != null && (
                                  choice ? (
                                    <span className="muted" style={{ fontSize: 12 }}>
                                      {choice === "removed" ? "Marked as removed" : "Kept as active"}
                                    </span>
                                  ) : (
                                    <span style={{ display: "inline-flex", gap: 6 }}>
                                      <button type="button" className="btn sm"
                                        disabled={deciding != null}
                                        title="The broker took it out on purpose. Stop expecting it in later ITD files."
                                        onClick={() => decide(r.position_id!, "removed")}>
                                        Mark as removed</button>
                                      <button type="button" className="btn sm"
                                        disabled={deciding != null}
                                        title="The broker left it out by mistake. Keep expecting it in later ITD files."
                                        onClick={() => decide(r.position_id!, "keep")}>
                                        Keep as active</button>
                                    </span>
                                  )
                                )}
                              </td>
                            )}
                          </tr>);
                      })}
                    </tbody>
                  </table>
                </div>
                <Pagination page={page} pageCount={Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
                  pageSize={PAGE_SIZE} totalItems={data.total} onPageChange={setPage} noun="rows" />
              </div>
            )}

            {withdrawErr && !confirmWithdraw && (
              <div className="note crit" style={{ marginTop: 12 }}>{withdrawErr}</div>)}
          </>)}
        </div>

        <div className="drawer-f one-row">
          {canDecide && s && !withdrawn && s.held !== "older_itd" && (
            <button type="button" className="btn danger" disabled={withdrawing || confirmWithdraw}
              onClick={() => { setWithdrawErr(null); setConfirmWithdraw(true); }}>
              Undo this file</button>)}
          <button type="button" className="btn" onClick={onClose}
            style={{ marginLeft: "auto" }}>Close</button>
        </div>

        {/* The undo confirmation, as a popup over the drawer. A refusal (a later
            file depends on this one) is shown inside it, where the eye already is. */}
        {confirmWithdraw && (
          <div className="proto-modal-overlay" onClick={() => !withdrawing && setConfirmWithdraw(false)}>
            <div className="proto-modal" role="alertdialog" aria-modal="true"
              aria-label="Undo this file" onClick={e => e.stopPropagation()}>
              <div className="m-h">
                <h3>Undo this file?</h3>
                <button type="button" className="x" aria-label="Close"
                  onClick={() => !withdrawing && setConfirmWithdraw(false)}>×</button>
              </div>
              <div className="m-b">
                Its rows are taken back out of the running total, so the next file is
                compared as if this one had never arrived. The file itself is kept as
                received.
                {withdrawErr && <div className="note crit" style={{ marginTop: 12 }}>{withdrawErr}</div>}
              </div>
              <div className="m-f">
                <button type="button" className="btn" disabled={withdrawing}
                  onClick={() => setConfirmWithdraw(false)}>Cancel</button>
                <button type="button" className="btn danger" disabled={withdrawing}
                  onClick={withdraw}>{withdrawing ? "Undoing…" : "Yes, undo"}</button>
              </div>
            </div>
          </div>
        )}
      </aside>
    </>
  );
}

export default ItdComparisonDrawer;
