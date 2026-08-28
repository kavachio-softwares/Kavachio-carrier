import { useRef, useState } from "react";
import { createPortal } from "react-dom";

// Matches the CSS width below — used to clamp the bubble inside the viewport
// without needing a post-render measurement pass.
const BUBBLE_WIDTH = 230;
// Headroom (px) required above the icon to place the bubble there; below
// this the bubble flips underneath the icon instead. Comfortably covers this
// component's short hint-text bubbles (worst case ~4 lines ≈ 90px tall).
const FLIP_THRESHOLD = 140;
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
  const [pos, setPos] = useState<{ top: number; left: number; placement: "above" | "below" } | null>(null);

  function show() {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const placement: "above" | "below" = r.top < FLIP_THRESHOLD ? "below" : "above";
    const half = BUBBLE_WIDTH / 2;
    const center = r.left + r.width / 2;
    const left = Math.min(Math.max(center, half + EDGE), window.innerWidth - EDGE - half);
    setPos({ top: placement === "above" ? r.top : r.bottom, left, placement });
  }
  function hide() { setPos(null); }

  return (
    <span ref={ref} className="info-tip" tabIndex={0}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      <svg className="ic" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.4" />
        <path d="M8 7.2v4.3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        <circle cx="8" cy="4.7" r="0.9" fill="currentColor" />
      </svg>
      {pos && createPortal(
        <span className="info-tip-bubble" role="tooltip" data-placement={pos.placement}
          style={{
            top: pos.top, left: pos.left,
            transform: pos.placement === "above"
              ? "translate(-50%, -100%) translateY(-9px)"
              : "translate(-50%, 0) translateY(9px)",
          }}>
          {text}
        </span>,
        document.body,
      )}
    </span>
  );
}
