import { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { TipBody } from "./tipText";

// The bubble sizes to its text: a short note stays compact, a longer one wraps
// at BUBBLE_MAX (kept in step with max-width in proto.css). It is measured
// once drawn, so it is clamped by its REAL width and height, never a guess.
const BUBBLE_MAX = 340;
const GAP = 9;  // between the icon and the bubble
const EDGE = 8; // min gap kept from the viewport's left/right/top/bottom edges

// Small "i" icon that reveals a floating tooltip bubble on hover/focus.
// Use next to a heading/label to move explanatory copy out of the flow —
// keeps the layout tight while the detail stays one hover away.
//
// The bubble is portaled to document.body and positioned via fixed
// coordinates computed from the icon's own bounding rect. A plain
// position:absolute child would get silently clipped the moment InfoTip
// sits inside a `.card` (overflow:hidden, used for its rounded corners) —
// no z-index fixes that, since overflow clipping isn't a stacking issue.
// Portaling out of the DOM tree sidesteps it entirely, everywhere InfoTip
// is used, not just here.
//
// Placement is viewport-aware: it opens above the icon by default, flips
// below when there isn't enough headroom (icon near the top of the screen),
// and its horizontal center is clamped so it never runs off the left or
// right edge either.
export function InfoTip({ text }: { text: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const bubble = useRef<HTMLSpanElement>(null);
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number; arrow: number; placement: "above" | "below" } | null>(null);

  function show() {
    const r = ref.current?.getBoundingClientRect();
    if (r) setAnchor(r);
  }
  function hide() { setAnchor(null); setPos(null); }

  // Place the bubble from its measured size: above the icon when it fits,
  // otherwise below; centred on the icon but never past either edge.
  useLayoutEffect(() => {
    const b = bubble.current;
    if (!anchor || !b) return;
    const w = Math.min(b.offsetWidth || BUBBLE_MAX, window.innerWidth - 2 * EDGE);
    const h = b.offsetHeight;
    const placement: "above" | "below" = anchor.top - GAP - h >= EDGE ? "above" : "below";
    const half = w / 2;
    const center = anchor.left + anchor.width / 2;
    const left = Math.min(Math.max(center, half + EDGE), window.innerWidth - EDGE - half);
    // The arrow points at the icon, not the bubble's middle, once clamped.
    const arrow = Math.min(Math.max(center - (left - half), 12), w - 12);
    setPos({ top: placement === "above" ? anchor.top : anchor.bottom, left, arrow, placement });
  }, [anchor, text]);

  return (
    <span ref={ref} className="info-tip" tabIndex={0}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      <svg className="ic" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.4" />
        <path d="M8 7.2v4.3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        <circle cx="8" cy="4.7" r="0.9" fill="currentColor" />
      </svg>
      {anchor && createPortal(
        <span ref={bubble} className="info-tip-bubble" role="tooltip"
          data-placement={pos?.placement ?? "above"}
          style={pos ? {
            top: pos.top, left: pos.left,
            ["--tip-arrow" as string]: `${pos.arrow}px`,
            transform: pos.placement === "above"
              ? `translate(-50%, -100%) translateY(-${GAP}px)`
              : `translate(-50%, 0) translateY(${GAP}px)`,
          } : { top: 0, left: 0, visibility: "hidden" }}>
          <TipBody text={text} />
        </span>,
        document.body,
      )}
    </span>
  );
}
