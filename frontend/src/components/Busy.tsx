// One loading affordance, used app-wide for EVERYTHING — buttons, dropdown
// fetches, table loads, heavy AI ops, all of it:
//
//  <LoadingOverlay label="…" />  — blurs and blocks the whole page with a
//    centered coin-flip spinner so the user can't fire the action twice or
//    navigate mid-operation. Render conditionally: {busy && <LoadingOverlay/>}
//    Pages with a well-understood, named operation (contract extraction,
//    output generation, setup builds, AI mapping) render this explicitly with
//    a specific label. Everything else — small saves, dropdown fetches, quick
//    edits — is covered automatically by <GlobalLoadingOverlay/> below (mounted
//    once in Layout), which shows this same component with a generic label
//    whenever ANY api request is in flight. Either way, it's the same visual
//    everywhere — no more BarLoader/inline-spinner/plain-"Loading…" variety.

import { useEffect, useState } from "react";
import { netActive, subscribeNetActivity } from "../api/client";
import KavachioLogo from "./KavachioLogo";

export function LoadingOverlay({ label = "Processing…" }: { label?: string }) {
  return (
    <div className="k-overlay" role="alert" aria-busy="true">
      <div className="k-coin-stage">
        <span className="k-spinner-ring" aria-hidden="true" />
        <span className="k-coin k-coin-logo">
          <KavachioLogo size={34} />
        </span>
      </div>
      <div className="k-overlay-label">{label}</div>
    </div>
  );
}

// Fallback overlay that appears automatically whenever ANY api request is in
// flight (driven by the counter in api/client) and no page-specific
// LoadingOverlay is already covering it. Mounted once in Layout, so every
// screen gets the SAME loading feedback even where no local busy state exists
// — button clicks, dropdown fetches, table loads, quick saves, all of it.
//
// Two timing rules keep it from flickering:
//  - SHOW_DELAY (200ms): must be continuously in-flight for this long before
//    it appears at all, so fast calls never flash it.
//  - MIN_VISIBLE (600ms): once shown, stays up for at least this long from the
//    moment it appeared, even if the request finishes sooner — just enough to
//    avoid an abrupt on/off blink, without padding a quick load with fake wait.
// Together these also cover bursts of quick successive requests: netActive()
// is a shared counter, so back-to-back calls just keep it in one continuous
// visible window instead of toggling per request; even a brief gap between
// two near-simultaneous calls is absorbed by the pending min-visible timer.
const SHOW_DELAY = 50;
const MIN_VISIBLE = 600;

export function GlobalLoadingOverlay() {
  const [show, setShow] = useState(false);
  useEffect(() => {
    let showTimer: number | undefined;
    let hideTimer: number | undefined;
    let shownAt: number | null = null;
    const clearShowTimer = () => { if (showTimer !== undefined) { window.clearTimeout(showTimer); showTimer = undefined; } };
    const clearHideTimer = () => { if (hideTimer !== undefined) { window.clearTimeout(hideTimer); hideTimer = undefined; } };

    const sync = () => {
      if (netActive()) {
        // Something is in flight again — cancel any pending hide so a brief
        // gap between two near-simultaneous requests doesn't flicker it off.
        clearHideTimer();
        if (shownAt === null && showTimer === undefined) {
          showTimer = window.setTimeout(() => {
            showTimer = undefined;
            shownAt = Date.now();
            setShow(true);
          }, SHOW_DELAY);
        }
      } else {
        clearShowTimer();
        if (shownAt !== null) {
          const remaining = MIN_VISIBLE - (Date.now() - shownAt);
          if (remaining <= 0) {
            shownAt = null;
            setShow(false);
          } else if (hideTimer === undefined) {
            hideTimer = window.setTimeout(() => {
              hideTimer = undefined;
              shownAt = null;
              setShow(false);
            }, remaining);
          }
        }
      }
    };
    sync();
    const unsub = subscribeNetActivity(sync);
    return () => { unsub(); clearShowTimer(); clearHideTimer(); };
  }, []);
  if (!show) return null;
  return <LoadingOverlay label="Loading…" />;
}
