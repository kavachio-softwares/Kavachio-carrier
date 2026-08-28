// C-9 — the deadline reminder card: the same overdue / due-soon reminders the
// notification bell shows, but pushed at the broker from the app SHELL so they
// are seen without first navigating to My Calendar.
//
// Mounted in Layout (like PlatformNotificationCard), so it survives navigation
// between screens and appears on all of them. Deliberately fetched ONCE per app
// load — a card that re-appeared on every route change would be noise, and the
// point of C-9 is one clear nudge, not nagging.
//
// Read state is shared with the bell via the same localStorage key, so
// dismissing here also clears the bell's badge and vice versa. Two separate
// notions of "seen" would contradict each other on screen.
//
// Self-gating: renders nothing when there is nothing unread, and nothing for
// Kavachio platform admins (they get PlatformNotificationCard; the submission
// calendar is a broker-side feature).
import { useEffect, useMemo, useState } from "react";
import { CalendarClock, X } from "lucide-react";
import { currentMga, isKavachioAdmin } from "../auth";
import { getActivity, type ActivityEvent } from "../api/activity";
import { listPrograms, type ProgramLite } from "../api/calendar";
import {
  NOTIFY, NOTIFY_ACTIONS, NOTIFY_LIMIT, NOTIF_CHANGED, dismissNotifs, dismissedIds,
} from "./NotificationBell";

/** "just now" / "3h ago" / "2d ago" — matches the bell's wording. */
function relTime(iso: string | null): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60); if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** "I have seen this card" — session-scoped, and deliberately NOT the same thing
 *  as clearing a reminder. Session rather than local storage because the card is
 *  a greeting: it should be waiting for you at the start of the next one. */
const CARD_HIDDEN_KEY = "kavachio.notif.cardHidden";

export default function DeadlineReminderCard() {
  const mga = currentMga();
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [programs, setPrograms] = useState<ProgramLite[]>([]);
  const [dismissed, setDismissed] = useState(() => {
    try { return sessionStorage.getItem(CARD_HIDDEN_KEY) === "1"; } catch { return false; }
  });
  // Captured at mount: the card reports what was unread when the app loaded, and
  // must not empty itself if something else (the bell) reads a reminder while it
  // is on screen.

  useEffect(() => {
    if (isKavachioAdmin()) return;
    let live = true;
    getActivity(mga, NOTIFY_LIMIT, NOTIFY_ACTIONS)
      .then(e => { if (live) setEvents(e); })
      .catch(() => { /* a missed reminder must never break the app shell */ });
    listPrograms()
      .then(p => { if (live) setPrograms(p); })
      .catch(() => { /* names are cosmetic; ids still render */ });
    return () => { live = false; };
  }, [mga]);

  // Reminders opened one at a time in the bell are read, and must not come back
  // as a card on the next screen — the two surfaces show the same feed.
  const [gone, setGone] = useState<Set<number>>(dismissedIds);

  // The bell is on screen at the same time; a ✕ clicked there has to leave this
  // card as well, without waiting for a page load.
  useEffect(() => {
    const sync = () => setGone(dismissedIds());
    window.addEventListener(NOTIF_CHANGED, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(NOTIF_CHANGED, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  // The same set the bell shows, counted the same way. A card that quietly left
  // some out disagreed with the badge three inches below it.
  const pending = useMemo(
    () => events.filter(e => NOTIFY[e.action] && !gone.has(e.id)),
    [events, gone]);

  const programName = (id?: number) =>
    programs.find(p => p.id === id)?.name ?? (id ? `Program ${id}` : "Other");

  // Group by program, overdue programs first, then most recent — same ordering
  // as the bell so the two never disagree about what matters most.
  const groups = useMemo(() => {
    const byProg = new Map<number, ActivityEvent[]>();
    for (const e of pending) {
      const pid = Number(e.details?.program_id ?? 0);
      const arr = byProg.get(pid);
      if (arr) arr.push(e); else byProg.set(pid, [e]);
    }
    return [...byProg.entries()]
      .map(([pid, evs]) => {
        evs.sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
        return {
          pid, events: evs,
          name: programName(pid),
          newest: evs[0]?.created_at ?? "",
          hasOverdue: evs.some(e => e.action === "submission_overdue"),
        };
      })
      .sort((a, b) =>
        Number(b.hasOverdue) - Number(a.hasOverdue) ||
        b.newest.localeCompare(a.newest));
  }, [pending, programs]);

  if (dismissed || isKavachioAdmin() || pending.length === 0) return null;

  const dismiss = () => {
    // Puts THIS CARD away. It does not clear a single reminder — closing the
    // thing that greeted you is not the same as saying you have dealt with
    // ninety-six deadlines, and the bell must still list every one of them.
    // Removing is the ✕ on a row, and only that.
    //
    // Remembered for the session so it stays shut as you move around the app,
    // and greets you again next time you sign in — that is the card's whole job.
    try { sessionStorage.setItem(CARD_HIDDEN_KEY, "1"); } catch { /* private mode */ }
    setDismissed(true);
  };


  // Counted per action rather than "overdue vs everything else": due-today is
  // its own reminder now, and folding it into "due soon" would tell someone the
  // deadline is still ahead of them on the very day it lands.
  const overdue = pending.filter(e => e.action === "submission_overdue").length;
  const dueToday = pending.filter(e => e.action === "submission_due_today").length;
  const dueSoon = pending.length - overdue - dueToday;
  const headline = [
    overdue ? `${overdue} bordereau${overdue === 1 ? "" : "x"} overdue` : "",
    dueToday ? `${dueToday} due today` : "",
    dueSoon ? `${dueSoon} due soon` : "",
  ].filter(Boolean).join(" · ");

  return (
    // z-[1050]: above page content and the loading overlay, below the global
    // error popup (z-1100) so a real failure is never hidden behind this.
    // Same corner as PlatformNotificationCard is safe — the two are mutually
    // exclusive by role (that one is admin-only, this one hides for admins).
    <div
      role="status"
      aria-live="polite"
      className="fixed top-5 right-5 z-[1050] w-[360px] max-w-[calc(100vw-2.5rem)]
                 rounded-xl border border-border bg-white shadow-xl overflow-hidden"
    >
      <div className="flex items-start gap-3 px-4 pt-4 pb-3 border-b border-border">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full
                         bg-navy/10 text-navy">
          <CalendarClock size={16} strokeWidth={2} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-ink leading-tight">{headline}</div>
          <div className="text-xs text-ink-muted mt-0.5">Your submission deadlines</div>
        </div>
        <button
          onClick={dismiss}
          aria-label="Close deadline reminders"
          className="text-ink-soft hover:text-ink transition shrink-0"
        >
          <X size={16} />
        </button>
      </div>

      <div className="max-h-[46vh] overflow-y-auto">
        {groups.map(g => (
          <div key={g.pid}>
            <div className="flex items-center gap-2 px-4 py-1.5 bg-surface-2
                            text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
              <span className="truncate">{g.name}</span>
              {g.hasOverdue && (
                <span className="h-1.5 w-1.5 shrink-0 rounded-full"
                      style={{ background: NOTIFY.submission_overdue.tone }} />
              )}
              <span className="ml-auto">{g.events.length}</span>
            </div>
            <ul className="divide-y divide-border">
              {g.events.map(e => {
                const meta = NOTIFY[e.action];
                return (
                  // Same shape as the bell: the row is text, the ✕ is the only
                  // action. Jumping to the calendar from a row closed this card
                  // and took every other reminder with it.
                  <li key={e.id} className="flex items-start">
                    <div className="min-w-0 flex-1 px-4 py-3">
                      <div className="flex items-start gap-2">
                        <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                              style={{ background: meta.tone }} />
                        <div className="min-w-0 flex-1">
                          <div className="text-[13px] font-medium text-ink leading-snug">
                            {meta.label}
                          </div>
                          <div className="text-xs text-ink-muted mt-1 truncate">
                            {String(e.details?.period ?? "")}
                            {e.details?.due_date ? ` · due ${e.details.due_date}` : ""}
                          </div>
                        </div>
                        <span className="text-[11px] text-ink-soft shrink-0">
                          {relTime(e.created_at)}
                        </span>
                      </div>
                    </div>
                    <button
                      title="Remove from this list"
                      aria-label={`Remove ${meta.label}${e.details?.period ? ` for ${e.details.period}` : ""}`}
                      onClick={() => setGone(dismissNotifs([e.id]))}
                      className="shrink-0 self-stretch px-3 text-ink-soft hover:text-ink transition"
                    >
                      <X size={13} />
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>

      {/* Dismiss closes the card and nothing else. The ✕ on a row is what
          removes a reminder — from here and from the bell alike. */}
      <div className="px-4 py-2.5 bg-surface-2 flex items-center justify-end">
        <button
          onClick={dismiss}
          className="text-xs font-semibold text-ink-muted hover:text-ink transition"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
