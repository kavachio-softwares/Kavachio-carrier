import { useEffect, useMemo, useRef, useState } from "react";
import { Download, X } from "lucide-react";
import {
  getPreviousSubmission, getPreviousSubmissionRows,
  type PreviousSubmission, type PrevFilter, type PrevOutcome, type PrevOutcomeInfo, type PrevRow,
} from "../api/validation";
import { downloadFile } from "../api/client";
import { InfoTip } from "./ui/InfoTip";

/** The Previous Submission tab (plan 23): this version of the file against the
 *  last version sent to the carrier, row by row.
 *
 *  Laid out like BDX Review — sheet buttons, a Changes / All rows switch, a
 *  frozen header and first column, rows fetched a window at a time and drawn
 *  only near the viewport — with each row's outcome in front. A changed value
 *  shows the old one crossed out beside it; a row the earlier version had and
 *  this one dropped is put back where it was, crossed out. The words, counts
 *  and colours all come from the server (version_diff_sheet), so the tab and
 *  its CSV always say the same thing. Read-only for everyone. */

/** Rows asked for at a time, fetched ahead of the viewport, and drawn beyond it. */
const PAGE_ROWS = 500;
const PREFETCH_ROWS = 200;
const ROW_OVERSCAN = 30;
const EST_ROW_H = 33;

const EDGE_B = "inset 0 -1px 0 var(--p-border)";
const EDGE_R = "inset -1px 0 0 var(--p-border)";
const stickyHead: React.CSSProperties =
  { position: "sticky", top: 0, zIndex: 2, background: "var(--p-surface-2)", boxShadow: EDGE_B };
const stickyCol: React.CSSProperties =
  { position: "sticky", left: 0, zIndex: 1, boxShadow: EDGE_R };

export default function PreviousSubmissionPanel({ exportId }: { exportId: string }) {
  const [info, setInfo] = useState<PreviousSubmission | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sheetName, setSheetName] = useState<string | null>(null);
  const [filter, setFilter] = useState<PrevFilter>("changes");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");

  // The rows of what is on screen (sheet + filter + search), by position.
  const [rows, setRows] = useState<Map<number, PrevRow>>(new Map());
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [rowsErr, setRowsErr] = useState<string | null>(null);
  const pagesRef = useRef<Set<number>>(new Set());
  const inFlightRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const rowHRef = useRef(0);
  const [viewport, setViewport] = useState({ top: 0, height: 0 });
  const [pop, setPop] = useState<{ word: string; text: string; left: number; top: number } | null>(null);

  useEffect(() => {
    let live = true;
    setInfo(null); setErr(null);
    getPreviousSubmission(exportId)
      .then(r => {
        if (!live) return;
        setInfo(r);
        const shown = r.sheets.filter(s => !s.message);
        setSheetName((shown.find(s => s.rows > 0) ?? shown[0])?.sheet ?? null);
        setFilter(r.outcomes.some(o => o.code !== "unchanged" && o.count > 0) ? "changes" : "all");
      })
      .catch((e: any) => {
        if (live) setErr(e?.response?.data?.detail ?? e?.message ?? "Could not compare this file.");
      });
    return () => { live = false; };
  }, [exportId]);

  // Typing settles for a moment before it asks the server.
  useEffect(() => {
    const t = setTimeout(() => setQuery(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  // Anything that changes which rows are listed starts the list again.
  const viewKey = `${exportId}|${sheetName}|${filter}|${query}`;
  const viewKeyRef = useRef(viewKey);
  viewKeyRef.current = viewKey;
  useEffect(() => {
    inFlightRef.current?.abort();
    inFlightRef.current = null;
    pagesRef.current = new Set();
    setRows(new Map()); setTotal(null); setRowsErr(null); setLoading(false); setPop(null);
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
    setViewport(v => ({ ...v, top: 0 }));
  }, [viewKey]);

  const sheet = info?.sheets.find(s => s.sheet === sheetName) ?? null;
  const count = total ?? 0;

  // Only the rows near the viewport are put in the DOM (as BDX Review does):
  // spacer rows above and below keep the scrollbar true to the whole list.
  const rowH = rowHRef.current || EST_ROW_H;
  const viewH = viewport.height || 600;
  const lastIdx = Math.min(count, Math.ceil((viewport.top + viewH) / rowH) + ROW_OVERSCAN);
  const firstIdx = Math.max(0, Math.min(lastIdx - 1, Math.floor(viewport.top / rowH) - ROW_OVERSCAN));
  const padTop = firstIdx * rowH;
  const padBottom = Math.max(0, (count - lastIdx) * rowH);

  // One request at a time: the first page, then whichever page the viewport
  // (plus a margin) reaches that has not been asked for.
  useEffect(() => {
    if (!info || !sheet || sheet.message || loading || rowsErr) return;
    let page: number | null = null;
    if (total === null) page = 0;
    else {
      const upto = Math.min(count, lastIdx + PREFETCH_ROWS);
      for (let i = firstIdx; i < upto; i++) {
        const p = Math.floor(i / PAGE_ROWS);
        if (!pagesRef.current.has(p)) { page = p; break; }
      }
    }
    if (page === null) return;
    const ctl = new AbortController();
    const key = viewKey;
    inFlightRef.current = ctl;
    pagesRef.current.add(page);
    setLoading(true);
    getPreviousSubmissionRows(exportId, {
      sheet: sheet.sheet, filter, q: query, offset: page * PAGE_ROWS, limit: PAGE_ROWS,
    }, ctl.signal)
      .then(r => {
        if (key !== viewKeyRef.current) return;
        setTotal(r.total);
        setRows(prev => {
          const next = new Map(prev);
          r.rows.forEach((row, i) => next.set(r.offset + i, row));
          return next;
        });
      })
      .catch((e: any) => {
        if (ctl.signal.aborted || key !== viewKeyRef.current) return;
        setRowsErr(e?.response?.data?.detail ?? e?.message ?? "Could not load the rows.");
      })
      .finally(() => {
        if (inFlightRef.current === ctl) { inFlightRef.current = null; setLoading(false); }
      });
  }, [info, sheet, filter, query, total, count, firstIdx, lastIdx, loading, rowsErr, viewKey, exportId]);

  useEffect(() => {
    if (!pop) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setPop(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pop]);

  const byCode = useMemo(() => {
    const m = new Map<PrevOutcome, PrevOutcomeInfo>();
    (info?.outcomes ?? []).forEach(o => m.set(o.code, o));
    return m;
  }, [info]);

  if (err) return <div className="note warn">{err}</div>;
  if (!info) return <div className="note">Comparing with the last version sent to the carrier…</div>;

  if (info.state !== "compared") {
    return (
      <div className="card pad" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ padding: 0, border: 0, marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>Previous Submission</h3>
        </div>
        <p className="muted" style={{ fontSize: 13, margin: 0 }}>{info.message}</p>
      </div>
    );
  }

  const accVersion = info.accepted?.version;
  const shownSheets = info.sheets.filter(s => !s.message);
  const notCompared = info.sheets.filter(s => s.message);
  const sheetChanges = sheet ? sheet.rows - (sheet.counts.unchanged ?? 0) : 0;
  // The colour key lists only the outcomes the grid is showing.
  const listed = (code: PrevOutcome) =>
    filter === "all" || (filter === "changes" ? code !== "unchanged" : code === filter);
  const onSheet = info.outcomes.filter(o => (sheet?.counts[o.code] ?? 0) > 0 && listed(o.code));

  const tile = (label: string, value: number, meaning: string, to: PrevFilter, ink?: string) => (
    <div className="tile click" key={label} data-testid={`prev-tile-${to}`}
      style={{ cursor: "pointer", outline: filter === to ? "2px solid var(--p-primary)" : undefined }}
      onClick={() => setFilter(filter === to && to !== "all" ? "changes" : to)}>
      <div className="k" style={{ display: "flex", alignItems: "center", gap: 4 }}>
        {label} <InfoTip text={meaning} />
      </div>
      <div className="v" style={{ color: ink }}>{value}</div>
    </div>
  );

  return (
    <div>
      {/* ── Summary ── */}
      <div className="card pad" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ padding: 0, border: 0, marginBottom: 12, flexWrap: "wrap", gap: 10 }}>
          <h3 style={{ margin: 0 }}>Previous Submission</h3>
          <span className="sub">{info.about[0]}</span>
          <div className="right">
            <button className="btn sm" onClick={() =>
              downloadFile(`/export/downloads/${exportId}/previous-submission.csv`)
                .catch(() => alert("We couldn't download that file — please try again."))}>
              <Download size={13} /> Download CSV
            </button>
          </div>
        </div>
        <div className="tiles five">
          {tile("All", info.total, info.all_meaning, "all")}
          {info.outcomes.filter(o => o.code !== "unchanged")
            .map(o => tile(o.word, o.count, o.meaning, o.code, o.ink))}
        </div>
        {info.about.slice(1).map(line => (
          <p key={line} className="muted" style={{ fontSize: 12.5, margin: "10px 0 0" }}>{line}</p>
        ))}
        {info.warnings.map(w => (
          <div key={w} className="note warn" style={{ marginTop: 12 }}>{w}</div>
        ))}
      </div>

      {/* ── Row by row ── */}
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ flexWrap: "wrap", gap: 10 }}>
          <h3 style={{ margin: 0 }}>Row by Row</h3>
          <span className="sub">Click an outcome to see what happened. Old values are crossed out.</span>
          <div className="right" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {sheet && (
              <div className="seg" role="group" aria-label="Show rows">
                <button className={filter === "changes" ? "on" : ""} onClick={() => setFilter("changes")}>
                  Changes ({sheetChanges})
                </button>
                <button className={filter === "all" ? "on" : ""} onClick={() => setFilter("all")}>
                  All rows ({sheet.rows})
                </button>
              </div>
            )}
            <div className="search" style={{ minWidth: 240 }}>
              <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m20 20-3-3" /></svg>
              <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search policy, insured or value…" />
            </div>
          </div>
        </div>

        {shownSheets.length > 1 && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", padding: "10px 20px", borderBottom: "1px solid var(--p-border)" }}>
            {shownSheets.map(s => (
              <button key={s.sheet} className={`btn sm ${s.sheet === sheetName ? "pri" : ""}`}
                onClick={() => setSheetName(s.sheet)}>
                {s.sheet} ({s.rows})
              </button>
            ))}
          </div>
        )}

        <div style={{ padding: 16 }}>
          {notCompared.map(s => (
            <div key={s.sheet} className="note" style={{ marginBottom: 12 }}>
              <strong>{s.sheet}:</strong> {s.message}
            </div>
          ))}
          {!sheet ? (
            <div className="note">No sheet of this file could be compared.</div>
          ) : rowsErr ? (
            <div className="note warn">{rowsErr}</div>
          ) : total === null ? (
            <div className="note">Loading the rows…</div>
          ) : total === 0 ? (
            <div className="note">
              {query ? "No rows match."
                : filter === "changes" ? "No changes on this sheet — switch to “All rows” to see every row."
                : "No rows to show."}
            </div>
          ) : (
            <div className="tbl-wrap tbl-scroll-x" style={{ maxHeight: "64vh" }}
              ref={el => {
                scrollRef.current = el;
                if (el && !viewport.height) {
                  setViewport(v => (v.height ? v : { ...v, height: el.clientHeight }));
                }
              }}
              onScroll={e => {
                const el = e.currentTarget;
                setPop(null);
                setViewport(v => (v.top === el.scrollTop && v.height === el.clientHeight
                  ? v : { top: el.scrollTop, height: el.clientHeight }));
              }}>
              <table>
                <thead>
                  <tr>
                    <th style={{ ...stickyHead, left: 0, zIndex: 3, boxShadow: `${EDGE_B}, ${EDGE_R}` }}>Outcome</th>
                    <th style={stickyHead}>Row in this file</th>
                    <th style={stickyHead}>{accVersion ? `Row in version ${accVersion}` : "Row before"}</th>
                    {sheet.columns.map((c, ci) => <th key={ci} style={stickyHead}>{c}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {padTop > 0 && (
                    <tr aria-hidden="true" style={{ height: padTop }}>
                      <td colSpan={sheet.columns.length + 3} style={{ padding: 0, border: 0 }} />
                    </tr>
                  )}
                  {Array.from({ length: Math.max(0, lastIdx - firstIdx) }, (_, k) => firstIdx + k).map((i, wi) => {
                    const row = rows.get(i);
                    const measure = wi === 0 ? ((el: HTMLTableRowElement | null) => {
                      const h = el?.getBoundingClientRect().height;
                      if (h && !rowHRef.current) rowHRef.current = h;
                    }) : undefined;
                    if (!row) {
                      return (
                        <tr key={i} ref={measure}>
                          {Array.from({ length: sheet.columns.length + 3 }, (_, ci) => (
                            <td key={ci} style={ci === 0 ? { ...stickyCol, background: "var(--p-surface)" } : undefined}>
                              <span style={{ display: "inline-block", height: 8, width: ci === 0 ? 64 : "70%",
                                borderRadius: 3, background: "var(--p-border)", opacity: 0.5 }} />
                            </td>
                          ))}
                        </tr>
                      );
                    }
                    const o = byCode.get(row.outcome);
                    const tinted = row.gone || row.outcome === "new";
                    const strike: React.CSSProperties = row.gone
                      ? { textDecoration: "line-through", color: "var(--p-muted)" } : {};
                    return (
                      <tr key={i} ref={measure} style={tinted ? { background: o?.fill } : undefined}>
                        <td style={{ ...stickyCol, background: o?.fill ?? "var(--p-surface)", color: o?.ink,
                                     fontWeight: 700, whiteSpace: "nowrap",
                                     cursor: row.note ? "pointer" : undefined }}
                          title={row.note ? "Click to see what happened" : undefined}
                          onClick={row.note ? ev => {
                            const r = (ev.currentTarget as HTMLElement).getBoundingClientRect();
                            setPop({ word: o?.word ?? row.outcome, text: row.note,
                                     left: Math.min(r.left, window.innerWidth - 380),
                                     top: Math.min(r.bottom + 6, window.innerHeight - 220) });
                          } : undefined}>
                          {o?.word ?? row.outcome}
                        </td>
                        <td className="mono" style={{ textAlign: "right", ...strike }}>{row.row ?? ""}</td>
                        <td className="mono" style={{ textAlign: "right" }}>{row.accepted_row ?? ""}</td>
                        {row.cells.map((c, ci) => {
                          const old = row.was[String(ci)];
                          return (
                            <td key={ci}
                              style={{ ...strike,
                                       ...(old !== undefined ? { background: o?.cell ?? undefined, fontWeight: 600 } : {}) }}
                              title={old !== undefined && accVersion ? `Version ${accVersion}: ${old}` : undefined}>
                              {c}
                              {old !== undefined && (
                                <>{" "}<span style={{ textDecoration: "line-through", opacity: 0.6,
                                                      fontWeight: 400, fontSize: 11 }}>{old}</span></>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                  {padBottom > 0 && (
                    <tr aria-hidden="true" style={{ height: padBottom }}>
                      <td colSpan={sheet.columns.length + 3} style={{ padding: 0, border: 0 }} />
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* colour key */}
        {sheet && onSheet.length > 0 && (
          <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap",
                        padding: "10px 20px", borderTop: "1px solid var(--p-border)",
                        fontSize: 12, color: "var(--p-muted)" }}>
            {onSheet.map(o => (
              <span key={o.code} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span style={{ width: 11, height: 11, borderRadius: 3, background: o.cell ?? o.fill,
                               border: `1px solid ${o.ink}` }} />
                <b style={{ color: o.ink }}>{o.word}</b> · {o.legend}
              </span>
            ))}
            {total !== null && total > 0 && (
              <span style={{ marginLeft: "auto" }}>{total} {total === 1 ? "row" : "rows"}</span>
            )}
          </div>
        )}
      </div>

      {pop && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setPop(null)} />
          <div className="fixed z-40 w-[360px] rounded-lg border border-border bg-white shadow-xl p-3"
            role="dialog" style={{ left: Math.max(8, pop.left), top: Math.max(8, pop.top) }}>
            <div className="flex items-center justify-between mb-1.5">
              <span className="font-semibold text-ink text-xs">{pop.word}</span>
              <button className="text-ink-soft hover:text-ink" aria-label="Close" onClick={() => setPop(null)}>
                <X size={13} />
              </button>
            </div>
            <p className="text-xs text-ink-muted" style={{ whiteSpace: "pre-line", margin: 0 }}>{pop.text}</p>
          </div>
        </>
      )}
    </div>
  );
}
