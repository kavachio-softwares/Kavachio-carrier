// Row windowing for the big output grids.
//
// A generated BDX sheet is routinely 10k–80k rows by ~25 columns. Rendering it
// whole is a quarter of a million <td> elements, which freezes the tab (and
// with it the whole browser) — layout, paint and every subsequent React update
// all scale with the row count. Only the rows actually on screen need to exist:
// the ones above and below are represented by two spacer <tr>s, so the
// scrollbar still reflects the full sheet and nothing else about the markup or
// the sticky header/first column has to change.
//
// Deliberately dependency-free — this is ~60 lines against a table whose row
// height is uniform, which is the one case a virtualization library isn't
// worth taking on for.
import { useCallback, useEffect, useRef, useState } from "react";

/** Rows kept rendered beyond each edge of the viewport, so a flick-scroll shows
 *  filled rows rather than blank space while React catches up. */
const OVERSCAN = 12;
/** Used until a real row has been rendered and measured. */
const FALLBACK_ROW_H = 33;
/** Below this, windowing costs more than it saves — render the lot. */
export const WINDOW_MIN_ROWS = 150;

/** Nearest scrolling ancestor of `el`, starting with `el` itself.
 *
 *  Matches on the overflow style ALONE — deliberately not on
 *  `scrollHeight > clientHeight`. This runs on mount, when the grid may still
 *  be short enough not to overflow yet; requiring it to already be scrolling
 *  would skip the real container and latch onto some outer one. */
function scrollParentOf(el: HTMLElement | null): HTMLElement | null {
  for (let n: HTMLElement | null = el; n; n = n.parentElement) {
    const { overflowY } = getComputedStyle(n);
    if (overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay") return n;
  }
  return el;
}

export type RowWindow = {
  /** Slice bounds into the row list: rows[start..end) are the ones to render. */
  start: number;
  end: number;
  /** Spacer heights standing in for the rows outside the window. */
  padTop: number;
  padBottom: number;
  /** Put this on the FIRST rendered row so the real row height gets measured. */
  rowRef: React.RefObject<HTMLTableRowElement>;
  /** Scroll so 1-based data row `gi` sits in the middle of the viewport.
   *  Works for rows that aren't currently rendered — which is the whole point,
   *  since scrollIntoView can't reach a row that isn't in the DOM. */
  scrollToRow: (gi: number) => void;
};

/**
 * @param scrollRef  the scroll container, or any element inside it (the nearest
 *                   scrollable ancestor is used).
 * @param count      total number of rows in the list being windowed.
 * @param enabled    pass false to render everything (small sheets, previews).
 */
export function useRowWindow(
  scrollRef: React.RefObject<HTMLElement | null>,
  count: number,
  enabled = true,
): RowWindow {
  const [rowH, setRowH] = useState(FALLBACK_ROW_H);
  const [range, setRange] = useState({ start: 0, end: Math.min(count, OVERSCAN * 4) });
  const rowRef = useRef<HTMLTableRowElement>(null!);
  const rafRef = useRef<number | null>(null);
  const boxRef = useRef<HTMLElement | null>(null);
  // Read inside callbacks that must not be re-created on every change.
  const countRef = useRef(count); countRef.current = count;
  const rowHRef = useRef(rowH); rowHRef.current = rowH;

  const recompute = useCallback(() => {
    const el = boxRef.current;
    if (!el) return;
    const h = rowHRef.current || FALLBACK_ROW_H;
    // The sticky header sits inside the scroll box, so row 0 starts a little
    // below scrollTop=0; OVERSCAN absorbs the difference.
    const first = Math.floor(el.scrollTop / h);
    const visible = Math.ceil(el.clientHeight / h);
    const start = Math.max(0, first - OVERSCAN);
    const end = Math.min(countRef.current, first + visible + OVERSCAN);
    setRange(prev => (prev.start === start && prev.end === end ? prev : { start, end }));
  }, []);

  // Coalesce to one recompute per frame — scroll fires far more often.
  const onScroll = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      recompute();
    });
  }, [recompute]);

  useEffect(() => {
    if (!enabled) return;
    const box = scrollParentOf(scrollRef.current);
    boxRef.current = box;
    if (!box) return;
    box.addEventListener("scroll", onScroll, { passive: true });
    const ro = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(() => recompute()) : null;
    ro?.observe(box);
    recompute();
    return () => {
      box.removeEventListener("scroll", onScroll);
      ro?.disconnect();
      if (rafRef.current !== null) window.cancelAnimationFrame(rafRef.current);
    };
  }, [scrollRef, enabled, onScroll, recompute]);

  // Row count changed (rows streaming in, sheet switch, filter toggle) — the
  // window may now be short, or past the end.
  useEffect(() => { if (enabled) recompute(); }, [count, enabled, recompute]);

  // Measure a real row once one exists, so the geometry follows the stylesheet
  // rather than a guess. Converges after one extra render.
  useEffect(() => {
    if (!enabled) return;
    const h = rowRef.current?.getBoundingClientRect().height;
    if (h && Math.abs(h - rowH) > 0.5) { setRowH(h); recompute(); }
  });

  const scrollToRow = useCallback((gi: number) => {
    const box = boxRef.current;
    if (!box) return;
    const h = rowHRef.current || FALLBACK_ROW_H;
    box.scrollTop = Math.max(0, (gi - 1) * h - box.clientHeight / 2);
    recompute();
  }, [recompute]);

  if (!enabled) {
    return { start: 0, end: count, padTop: 0, padBottom: 0, rowRef, scrollToRow };
  }
  const start = Math.min(range.start, Math.max(0, count - 1));
  const end = Math.min(Math.max(range.end, start), count);
  return {
    start, end, rowRef, scrollToRow,
    padTop: start * rowH,
    padBottom: Math.max(0, count - end) * rowH,
  };
}
