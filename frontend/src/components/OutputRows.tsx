import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useRowWindow, WINDOW_MIN_ROWS } from "../hooks/useRowWindow";

/** The generated-output row viewer, shared by every screen that has an export
 *  id: Process Bordereau (after a run) and Exception Triage (for the export it
 *  is triaging). One implementation, so the grid, the tabs and the failed-cell
 *  highlighting are identical everywhere — and identical to the downloaded
 *  workbook.
 *
 */

// A rendered output sheet. `marks`/`notes` (populated when the grid is fetched
// with ?marks=1) carry the SAME failed-validation cells the downloaded workbook
// highlights, so the in-site view can show them identically. Each mark is a
// 0-based [rowIndex, colIndex] into `rows` (row 0 is the header).
export type Sheet = {
  sheet: string; rows: any[][];
  marks?: number[][];
  // The subset of `marks` whose exception is non-critical — painted light orange
  // rather than light red, matching the downloaded workbook cell for cell.
  warn_marks?: number[][];
  notes?: { r: number; c: number; text: string }[];
  // True for the actual policy-data sheets (role='data' in the setup), false for
  // the template's static spec/instruction sheets that also live in the file.
  // The preview steers to these so it shows real rows, not documentation.
  is_data?: boolean;
};

// Prefer the first real data sheet; fall back to the first sheet when the file
// has no classification (legacy) so nothing regresses.
export function firstDataSheet(sheets: Sheet[]): Sheet | null {
  return sheets.find(s => s.is_data) ?? sheets[0] ?? null;
}
// Data sheets first (that's what the user wants to verify), spec sheets after —
// stable within each group so tab order is otherwise unchanged.
export function dataFirst(sheets: Sheet[]): Sheet[] {
  return [...sheets].sort((a, b) => (a.is_data === b.is_data ? 0 : a.is_data ? -1 : 1));
}

// Shared light-red highlight (matches the downloaded workbook's Excel "Light Red
// Fill", RGB FFC7CE) so a flagged cell reads the same in-site as in the file.
export const HL_BG = "#FFC7CE";
export const HL_BD = "#E9A0A9";
export const HL_FG = "#9B1C2E";

// Non-critical (warning) cells get the light orange the workbook paints on them
// — RGB FFE0B2, `exporter._WARNING_FILL_RGB`. Same idea, softer colour: severity
// is readable at a glance without opening a single comment, on screen and in the
// downloaded file alike.
export const HL_WARN_BG = "#FFE0B2";
export const HL_WARN_BD = "#E8BE86";
export const HL_WARN_FG = "#8F580D";

// Per-cell flag styling, picked from the two mark sets a sheet carries.
export function flagStyle(isWarn: boolean) {
  return isWarn
    ? { background: HL_WARN_BG, color: HL_WARN_FG, fontWeight: 600 as const }
    : { background: HL_BG, color: HL_FG, fontWeight: 600 as const };
}

// A sheet's warning cells as a "row:col" lookup. Sheets served by an older
// backend have no `warn_marks`, so the set is empty and every flag stays red —
// exactly the previous behaviour.
export function warnKeySet(sheet: { warn_marks?: number[][] }) {
  return new Set((sheet.warn_marks ?? []).map(m => `${m[0]}:${m[1]}`));
}

// Severity spellings the workbook treats as non-critical — the same set
// `exporter._NON_CRITICAL_SEVERITIES` paints light orange. Anything else,
// including an unknown or missing severity, counts as critical, so a cell is
// only ever softened when its severity positively says so.
const NON_CRITICAL_SEVERITIES = new Set([
  "warning", "warn", "medium", "med", "info", "informational", "low", "notice",
]);
export function isWarnSeverity(severity?: string | null) {
  return NON_CRITICAL_SEVERITIES.has((severity ?? "").trim().toLowerCase());
}

// Render one sheet's grid, painting the cells the backend flagged (same cells the
// download highlights) and tinting the header of any column that contains a flag.
// `limit` caps the data rows shown (the inline preview); omit it to show them all.
//
// `sticky` (used by the full-output modal, where the grid is both tall and wide)
// freezes the header row and the first column in place, so scrolling right still
// shows which column you're in and scrolling down still shows which row.
//
// `focusRow` is a grid row index (same coordinate as the backend marks: 0 is the
// header, 1 the first data row) to jump to — the whole row is highlighted and
// scrolled into view, so opening the BDX from an exception lands on that policy.
export function HighlightGrid({ sheet, limit, sticky, focusRow }: {
  sheet: Sheet; limit?: number; sticky?: boolean; focusRow?: number | null;
}) {
  const header: any[] = sheet.rows[0] ?? [];
  const dataRows = sheet.rows.slice(1, limit != null ? limit + 1 : undefined);
  const marked = new Set((sheet.marks ?? []).map(m => `${m[0]}:${m[1]}`));
  const warned = warnKeySet(sheet);
  const markedCols = new Set((sheet.marks ?? []).map(m => m[1]));
  const noteAt = new Map((sheet.notes ?? []).map(n => [`${n.r}:${n.c}`, n.text]));

  // Only the rows on screen are rendered — a full BDX sheet is tens of
  // thousands of rows, and putting them all in the DOM locks up the tab. The
  // capped previews (`limit`) and short sheets render whole, as before.
  const tableRef = useRef<HTMLTableElement>(null);
  const win = useRowWindow(tableRef, dataRows.length,
                           limit == null && dataRows.length >= WINDOW_MIN_ROWS);
  const windowRows = win.start === 0 && win.end === dataRows.length
    ? dataRows : dataRows.slice(win.start, win.end);

  // Scroll the focused row into the middle of the scroll container once it (or
  // the sheet) changes. A short delay lets the grid finish laying out first.
  // `scrollToRow` positions by arithmetic rather than scrollIntoView, because
  // the target row may not be in the DOM yet — that is precisely the case
  // windowing creates, and it is the one that matters here.
  const focusRef = useRef<HTMLTableRowElement>(null);
  const scrollToRow = win.scrollToRow;
  useEffect(() => {
    if (focusRow == null) return;
    const t = setTimeout(() => {
      if (focusRef.current) focusRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
      else scrollToRow(focusRow);
    }, 60);
    return () => clearTimeout(t);
  }, [focusRow, sheet, scrollToRow]);

  // Frozen header row / first column. Two things they each need:
  //  - an opaque background of their own, because a sticky cell holds its place
  //    while the other rows slide *underneath* it, and a transparent one would
  //    let that content show through;
  //  - their divider drawn as an inset box-shadow rather than a border, because
  //    the table is border-collapse:collapse and a collapsed border belongs to
  //    the table grid, not the cell — so it scrolls away and leaves the frozen
  //    header/column looking like it is bleeding into the data.
  const EDGE_B = "inset 0 -1px 0 var(--p-border)";   // under the header row
  const EDGE_R = "inset -1px 0 0 var(--p-border)";   // right of the first column
  const stickyHead: React.CSSProperties = sticky
    ? { position: "sticky", top: 0, zIndex: 2, background: "var(--p-surface-2)", boxShadow: EDGE_B } : {};
  const stickyCol: React.CSSProperties = sticky
    ? { position: "sticky", left: 0, zIndex: 1, background: "var(--p-surface)", boxShadow: EDGE_R } : {};

  return (
    <table ref={tableRef}>
      <thead>
        <tr>
          {header.map((h, i) => (
            <th key={i} style={{
              ...stickyHead,
              // The top-left cell is frozen BOTH ways, so it sits above the two
              // frozen bands and carries both dividers.
              ...(i === 0 && sticky
                ? { left: 0, zIndex: 3, boxShadow: `${EDGE_B}, ${EDGE_R}` } : {}),
              ...(markedCols.has(i) ? { background: "#FCE9EC" } : {}),
            }}>{String(h ?? "")}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {win.padTop > 0 && (
          <tr aria-hidden="true" style={{ height: win.padTop }}>
            <td colSpan={header.length} style={{ padding: 0, border: 0 }} />
          </tr>
        )}
        {windowRows.map((row, wi) => {
          const idx = win.start + wi;
          const gi = idx + 1; // grid row index (row 0 is the header)
          const focused = focusRow != null && gi === focusRow;
          return (
            <tr key={idx} ref={focused ? focusRef : (wi === 0 ? win.rowRef : undefined)}
              style={focused ? {
                // A left rail + soft tint mark the whole exception row, sitting
                // under the per-cell flag colour (which still wins on its cells).
                boxShadow: "inset 3px 0 0 var(--p-primary)",
                background: "var(--p-primary-soft)",
              } : undefined}>
              {row.map((c, ci) => {
                const key = `${gi}:${ci}`;
                const bad = marked.has(key);
                return (
                  <td key={ci} className={ci === 0 ? "mono" : ""}
                    title={bad ? (noteAt.get(key) || "Failed validation") : undefined}
                    style={{
                      ...(ci === 0 ? stickyCol : {}),
                      // On the focused row, a sticky first column needs the row
                      // tint too, or it shows the plain surface colour instead.
                      ...(ci === 0 && focused ? { background: "var(--p-primary-soft)" } : {}),
                      // The flag colour always wins, including on the frozen column.
                      ...(bad ? flagStyle(warned.has(key)) : {}),
                    }}>
                    {String(c ?? "")}
                  </td>
                );
              })}
            </tr>
          );
        })}
        {win.padBottom > 0 && (
          <tr aria-hidden="true" style={{ height: win.padBottom }}>
            <td colSpan={header.length} style={{ padding: 0, border: 0 }} />
          </tr>
        )}
      </tbody>
    </table>
  );
}

// The full generated output rendered INLINE (inside the caller's card), laid out
// like the Exception Triage "BDX Review" grid — a tab per sheet, an
// Exceptions/All-rows filter, frozen header + first column, and the same
// failed-validation highlighting as the download.
//
// Read-only by design: this is the Process Bordereau preview, where a run has
// just been generated and there are no decisions to record — cells show why they
// failed on hover, but nothing here is clickable or editable. Fixing happens on
// the Exception Triage screen (BdxInlineReview), which owns that behaviour.
export function InlineAllRows({ title, subtitle, sheets, busy, actions }: {
  title: string; subtitle?: React.ReactNode; sheets: Sheet[]; busy: boolean;
  /** Extra controls for the card header, right of the row filter. */
  actions?: React.ReactNode;
}) {
  // Data sheets first, so the default tab is real policy data, not a spec sheet.
  const ordered = dataFirst(sheets);
  const [tab, setTab] = useState(0);
  // "all" shows every row; "exceptions" only the rows carrying a flagged cell.
  const [rowFilter, setRowFilter] = useState<"all" | "exceptions">("all");

  const g = ordered[tab];
  const header: any[] = g?.rows[0] ?? [];
  const marks = g?.marks ?? [];
  const totalRows = Math.max(0, (g?.rows.length ?? 0) - 1);
  // Grid row indices (1-based; 0 is the header) that have at least one flag.
  const exceptionGis = [...new Set(marks.map(m => m[0]))].filter(gi => gi >= 1).sort((a, b) => a - b);
  const visibleGis = rowFilter === "exceptions"
    ? exceptionGis.filter(gi => gi < (g?.rows.length ?? 0))
    : Array.from({ length: totalRows }, (_, i) => i + 1);

  // The sheet as displayed: only the visible rows, with the marks/notes re-mapped
  // onto their new row positions (they address rows by grid index, so filtering
  // rows out without re-mapping would paint the wrong cells).
  const posOf = new Map(visibleGis.map((gi, i) => [gi, i + 1]));
  const filtered: Sheet | null = g ? {
    ...g,
    rows: [header, ...visibleGis.map(gi => g.rows[gi] ?? [])],
    marks: marks.flatMap(m => {
      const r = posOf.get(m[0]);
      return r == null ? [] : [[r, m[1]] as number[]];
    }),
    notes: (g.notes ?? []).flatMap(n => {
      const r = posOf.get(n.r);
      return r == null ? [] : [{ ...n, r }];
    }),
  } : null;

  return (
    <div className="card">
      <div className="card-h">
        <h3>{title}</h3>
        {subtitle && <span className="sub">{subtitle}</span>}
        <div className="right" style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {!busy && g && totalRows > 0 && (
            <div className="seg" role="tablist"
              style={{ display: "inline-flex", border: "1px solid var(--p-border)", borderRadius: 8, overflow: "hidden" }}>
              {([["exceptions", `Exceptions (${exceptionGis.length})`], ["all", `All rows (${totalRows})`]] as const).map(([val, label]) => (
                <button key={val} onClick={() => setRowFilter(val)} className="btn sm" style={{
                  borderRadius: 0, border: 0,
                  background: rowFilter === val ? "var(--p-primary)" : "transparent",
                  color: rowFilter === val ? "#fff" : "var(--p-muted)",
                }}>
                  {label}
                </button>
              ))}
            </div>
          )}
          {actions}
        </div>
      </div>

      {!busy && ordered.length > 1 && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", padding: "10px 20px", borderBottom: "1px solid var(--p-border)" }}>
          {ordered.map((s, i) => {
            const spec = s.is_data === false;
            return (
              <button key={s.sheet} className={`btn sm ${i === tab ? "pri" : ""}`}
                onClick={() => { setTab(i); setRowFilter("all"); }}
                style={spec && i !== tab ? { opacity: 0.65 } : undefined}
                title={spec ? "Template spec / instructions — not policy data" : undefined}>
                {s.sheet} ({Math.max(0, s.rows.length - 1)}){spec ? " · info" : ""}
              </button>
            );
          })}
        </div>
      )}

      <div style={{ padding: 16 }}>
        {busy ? (
          <div className="note">Loading all rows…</div>
        ) : !g || totalRows === 0 ? (
          <div className="note">No rows in this sheet.</div>
        ) : rowFilter === "exceptions" && exceptionGis.length === 0 ? (
          <div className="note">No exceptions on this sheet — switch to “All rows” to see the full output.</div>
        ) : (
          // The grid sizes to its CONTENT and scrolls both ways inside this box —
          // a generated BDX is usually far wider than the card, and capping the
          // height keeps the page navigable with thousands of rows.
          <div className="tbl-wrap tbl-scroll-x" style={{ maxHeight: "64vh" }}>
            <HighlightGrid sheet={filtered!} sticky />
          </div>
        )}
      </div>

      {!busy && g && (
        <div style={{
          display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap",
          padding: "10px 20px", borderTop: "1px solid var(--p-border)",
          fontSize: 12, color: "var(--p-muted)",
        }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: HL_BG, border: `1px solid ${HL_BD}` }} />
            Critical
          </span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 11, height: 11, borderRadius: 3, background: HL_WARN_BG, border: `1px solid ${HL_WARN_BD}` }} />
            Warning
          </span>
          <span style={{ marginLeft: "auto" }}>
            {totalRows.toLocaleString()} rows · {marks.length.toLocaleString()} highlighted cell{marks.length === 1 ? "" : "s"} ·
            {" "}read-only — decide on these in <strong>Review Exceptions</strong>.
          </span>
        </div>
      )}
    </div>
  );
}

// ── shared grid fetch (one request per export, app-wide) ─────────────────────
// `/export/downloads/{id}/data` re-parses the generated workbook server-side, so
// it's the most expensive read in the app. Two callers still want the whole grid
// — this hook's modal, and the exception table's "View in Bordereau" row
// expander — and each fetching independently meant one Exception Triage screen
// could parse the same workbook twice.
//
// (The Triage grid itself and the Process Bordereau preview now stream instead,
// so they don't come through here.)
//
// The response is immutable for a given export id — a re-run mints a NEW id — so
// it's safe to cache. In-flight requests are shared too, so components mounting
// together coalesce into one call rather than racing.
type GridResp = { filename?: string; sheets: Sheet[] };
const gridCache = new Map<string, GridResp>();
const gridInflight = new Map<string, Promise<GridResp>>();

/** The parsed grid for an export — fetched once, then served from cache. */
export function fetchExportGrid(exportId: string | number): Promise<GridResp> {
  const id = String(exportId);
  const hit = gridCache.get(id);
  if (hit) return Promise.resolve(hit);
  const flying = gridInflight.get(id);
  if (flying) return flying;
  const p = api.get<GridResp>(`/export/downloads/${id}/data?full=1&marks=1`)
    .then(r => {
      const data: GridResp = {
        filename: r.data?.filename,
        sheets: Array.isArray(r.data?.sheets) ? r.data.sheets : [],
      };
      gridCache.set(id, data);
      return data;
    })
    .finally(() => { gridInflight.delete(id); });
  gridInflight.set(id, p);
  return p;
}

