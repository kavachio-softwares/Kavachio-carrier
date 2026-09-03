import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Bell, X } from "lucide-react";
import { currentMga, isBrokerSeat } from "../auth";
import { getActivity, type ActivityEvent } from "../api/activity";
import { listPrograms, type ProgramLite } from "../api/calendar";

// C-9 — in-app reminder bell. Reads the activity feed, surfaces only the
// notification-worthy events (deadline reminders today), and shows how many are
// still outstanding. Which reminders have been cleared is tracked client-side,
// per reminder, in localStorage — opening the bell clears nothing.
// Extend NOTIFY to surface more event types in the bell later.
// The three deadline moments, each its own reminder: ahead of the due date, on
// the day, and once it has passed. Tones escalate blue → amber → red to match
// the status badges on My Calendar, so the same event reads the same in both.
export const NOTIFY: Record<string, { label: string; tone: string; to: string }> = {
  submission_overdue:   { label: "Bordereau overdue",   tone: "#c0392b", to: "/calendar" },
  submission_due_today: { label: "Bordereau due today", tone: "#b7791f", to: "/calendar" },
  submission_due_soon:  { label: "Bordereau due soon",  tone: "#2c6fbb", to: "/calendar" },
};
export const NOTIFY_ACTIONS = Object.keys(NOTIFY);
/** Every outstanding reminder, not a recent-activity sample: a deadline stays
 *  true until it is met, so the list must not have a tail that falls off. */
export const NOTIFY_LIMIT = 500;

/** The reminders taken off the list. This is the ONLY state the bell keeps.
 *
 *  There used to be a second one — "read" — set by clicking a row, and it is
 *  what made the badge disagree with the card beside it: the card counted every
 *  reminder still on the list (91 overdue) while the badge counted only the ones
 *  not yet clicked (3). Two numbers describing the same list can only ever be
 *  read as a bug. One list, one count.
 *
 *  Removing a reminder removes the REMINDER, never the deadline: the period is
 *  still late, still on My Calendar, still in the Deadlines tile. */
export const DISMISSED_KEY = "kavachio.notif.dismissedIds";

export const NOTIF_CHANGED = "kavachio:notif-changed";

export function announceNotifChange() {
  window.dispatchEvent(new Event(NOTIF_CHANGED));
}

export function dismissedIds(): Set<number> {
  try {
    const raw = JSON.parse(localStorage.getItem(DISMISSED_KEY) ?? "[]");
    return new Set(Array.isArray(raw) ? raw.filter(n => typeof n === "number") : []);
  } catch { return new Set(); }
}

/** Take reminders off the list for good. */
export function dismissNotifs(ids: number[]): Set<number> {
  const next = dismissedIds();
  for (const id of ids) next.add(id);
  // Bounded: an unbounded list would only ever grow with ids nothing can show.
  try { localStorage.setItem(DISMISSED_KEY, JSON.stringify([...next].slice(-2000))); }
  catch { /* private mode */ }
  announceNotifChange();
  return next;
}

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

export default function NotificationBell({
  placement = "sidebar",
}: {
  // "sidebar" → dropdown opens to the right (used in the nav rail);
  // "inline"  → dropdown opens below, right-aligned (used in a page header).
  placement?: "sidebar" | "inline";
}) {
  const mga = currentMga();
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [programs, setPrograms] = useState<ProgramLite[]>([]);
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState<Set<number>>(dismissedIds);
  const ref = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left?: number; right?: number; top?: number; bottom?: number }>({});

  // The panel is rendered into <body>, so it has to be told where the bell is.
  // `position: sticky` on the sidebar makes it a stacking context, which traps
  // any descendant no matter how large its z-index — the panel was sliding
  // behind page cards with z-index 20000000000 set on it. Escaping the context
  // is the only fix; a fixed-position portal measured off the button keeps it
  // anchored without inheriting the trap.
  const place = useCallback(() => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    setPos(placement === "inline"
      ? { right: Math.max(8, window.innerWidth - r.right), top: r.bottom + 8 }
      : { left: r.right + 10, bottom: Math.max(8, window.innerHeight - r.bottom) });
  }, [placement]);

  // Program names (to label a reminder by program, since the event carries only id).
  // Not for a broker seat: /programs is carrier-tenant data and a broker has no
  // tenant, so this is a guaranteed 403 rather than a request that might work.
  useEffect(() => {
    if (isBrokerSeat()) return;
    listPrograms().then(setPrograms).catch(() => setPrograms([]));
  }, [mga]);

  // Fetch once when the bell mounts. Asks for reminder events ONLY, so the limit
  // is spent on what this list actually shows — the feed is shared with every
  // other activity this tenant records.
  useEffect(() => {
    // Same reason as above: the activity feed is this TENANT's, and a broker
    // seat is bound to none. currentMga() hands back the literal "default" for
    // them, which resolves to nothing — so the call cannot succeed and is not
    // worth making.
    if (isBrokerSeat()) return;
    let live = true;
    getActivity(mga, NOTIFY_LIMIT, NOTIFY_ACTIONS)
      .then(e => { if (live) setEvents(e); }).catch(() => {});
    return () => { live = false; };
  }, [mga]);

  // Re-read whenever anything else clears a reminder — the card's Dismiss,
  // or another tab.
  useEffect(() => {
    const sync = () => setDismissed(dismissedIds());
    window.addEventListener(NOTIF_CHANGED, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(NOTIF_CHANGED, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  // Measure before paint so the panel never shows up in the wrong place first.
  useLayoutEffect(() => { if (open) place(); }, [open, place]);
  useEffect(() => {
    if (!open) return;
    const onMove = () => place();
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);   // capture: any scroller
    return () => {
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open, place]);

  // Close on outside click. The panel is no longer a DOM descendant of the
  // bell, so a click inside it would otherwise read as "outside".
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const notifs = useMemo(
    () => events.filter(e => NOTIFY[e.action] && !dismissed.has(e.id)),
    [events, dismissed]);
  // The badge IS the length of the list below it. Nothing else to disagree with.
  const outstanding = notifs.length;

  const programName = (id?: number) =>
    programs.find(p => p.id === id)?.name ?? (id ? `Program ${id}` : "");

  // Group reminders by program → one header per program in the dropdown.
  // Group order = overdue programs first, then most-recent reminder;
  // within a group, newest first.
  const groups = useMemo(() => {
    const byProg = new Map<number, ActivityEvent[]>();
    for (const e of notifs) {
      const pid = Number(e.details?.program_id ?? 0);
      const arr = byProg.get(pid);
      if (arr) arr.push(e); else byProg.set(pid, [e]);
    }
    const out = [...byProg.entries()].map(([pid, evs]) => {
      evs.sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
      return {
        pid,
        name: programName(pid) || "Other",
        events: evs,
        newest: evs[0]?.created_at ?? "",
        hasOverdue: evs.some(e => e.action === "submission_overdue"),
        // Triage rank, not just the boolean: with three deadline moments a
        // program due TODAY must outrank one merely due soon, which hasOverdue
        // alone could not express — both were simply "not overdue".
        urgency: Math.max(...evs.map(e =>
          e.action === "submission_overdue" ? 2
            : e.action === "submission_due_today" ? 1 : 0)),
      };
    });
    out.sort((a, b) =>
      b.urgency - a.urgency ||              // overdue, then due today, then soon
      b.newest.localeCompare(a.newest));    // then most-recent
    return out;
  }, [notifs, programs]);

  // Opening the list is not the same as reading it. Looking at fourteen overdue
  // bordereaux and closing the panel leaves fourteen still overdue, so the count
  // stands until each one is actually opened — or Dismiss says otherwise.
  function toggle() { setOpen(o => !o); }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={toggle}
        title="Notifications"
        aria-label={`Notifications${outstanding ? ` (${outstanding} outstanding)` : ""}`}
        style={{
          position: "relative", display: "inline-flex", alignItems: "center",
          justifyContent: "center", width: 30, height: 30, borderRadius: 8,
          background: open ? "rgba(255,255,255,0.14)" : "transparent",
          border: "none", color: "inherit", cursor: "pointer",
        }}
      >
        <Bell size={16} strokeWidth={1.8} />
        {outstanding > 0 && (
          <span style={{
            position: "absolute", top: 1, right: 1, minWidth: 15, height: 15,
            padding: "0 3px", borderRadius: 8, background: "#e5484d", color: "#fff",
            fontSize: 9.5, fontWeight: 700, lineHeight: "15px", textAlign: "center",
          }}>{outstanding > 99 ? "99+" : outstanding}</span>
        )}
      </button>

      {open && createPortal(
        <div ref={panelRef} style={{
          position: "fixed", width: 320, ...pos,
          maxHeight: 380, overflowY: "auto", background: "#fff", color: "#1a2230",
          border: "1px solid #e4e8ee", borderRadius: 12,
          boxShadow: "0 12px 32px rgba(20,30,50,0.18)",
          // In <body> now, so this only has to clear the app's own layers
          // (modals sit at 50) rather than fight a stacking context it can't win.
          zIndex: 1000,
        }}>
          {/* Ninety rows is a lot of ✕ clicking, so the bulk action is offered
              rather than assumed — opening the panel still clears nothing. */}
          <div style={{
            display: "flex", alignItems: "center", gap: 10,
            padding: "11px 14px", borderBottom: "1px solid #eef1f5",
          }}>
            <span style={{ fontWeight: 700, fontSize: 13 }}>Notifications</span>
            {outstanding > 0 && (
              <button
                onClick={() => setDismissed(dismissNotifs(notifs.map(e => e.id)))}
                style={{
                  marginLeft: "auto", border: "none", background: "transparent",
                  padding: 0, fontSize: 11.5, fontWeight: 600, color: "#2f6f8f",
                  cursor: "pointer",
                }}>
                Clear all
              </button>
            )}
          </div>
          {groups.length === 0 ? (
            <div style={{ padding: "18px 14px", fontSize: 12.5, color: "#6b7686" }}>
              You're all caught up.
            </div>
          ) : (
            groups.map(g => (
              <div key={g.pid}>
                {/* program header — sticky so it stays while scrolling a long list */}
                <div style={{
                  position: "sticky", top: 0, zIndex: 1,
                  display: "flex", alignItems: "center", gap: 7,
                  padding: "7px 14px", background: "#f7f8fa",
                  borderBottom: "1px solid #eef1f5",
                  fontSize: 10.5, fontWeight: 700, letterSpacing: 0.4,
                  textTransform: "uppercase", color: "#6b7686",
                }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {g.name}
                  </span>
                  {g.hasOverdue && (
                    <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#c0392b", flex: "0 0 auto" }} />
                  )}
                  <span style={{ marginLeft: "auto", color: "#9aa4b2", fontWeight: 600 }}>{g.events.length}</span>
                </div>

                {g.events.map(e => {
                  const meta = NOTIFY[e.action];
                  const d = e.details ?? {};
                  return (
                    <div key={e.id} style={{
                      display: "flex", alignItems: "flex-start",
                      borderBottom: "1px solid #f2f4f7",
                    }}>
                      <div style={{ flex: 1, minWidth: 0, padding: "10px 4px 10px 14px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 3 }}>
                          <span style={{ width: 7, height: 7, borderRadius: "50%", background: meta.tone, flex: "0 0 auto" }} />
                          <span style={{ fontSize: 12.5, fontWeight: 600 }}>{meta.label}</span>
                          <span style={{ marginLeft: "auto", fontSize: 11, color: "#9aa4b2" }}>{relTime(e.created_at)}</span>
                        </div>
                        <div style={{ fontSize: 12, color: "#5b6675", paddingLeft: 14 }}>
                          {d.period ?? ""}{d.due_date ? ` · due ${d.due_date}` : ""}
                        </div>
                      </div>
                      <button
                        title="Remove from this list"
                        aria-label={`Remove ${meta.label}${d.period ? ` for ${d.period}` : ""}`}
                        onClick={() => setDismissed(dismissNotifs([e.id]))}
                        style={{
                          flex: "0 0 auto", alignSelf: "stretch", padding: "0 10px",
                          border: "none", background: "transparent", cursor: "pointer",
                          color: "#9aa4b2", lineHeight: 0,
                        }}>
                        <X size={13} />
                      </button>
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>,
        document.body)}
    </div>
  );
}
