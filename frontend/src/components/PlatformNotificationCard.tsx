// The sign-in message for Kavachio platform admins: a card in the top-right
// corner naming what happened at their brokers while they were away — today,
// how many Bordereau Setups were activated.
//
// Shown ONCE per sign-in (Login arms the flag, this consumes it), and only when
// something is actually waiting. Dismissing marks everything read, so the next
// sign-in reports only what's new since — an admin never sees the same
// activation twice, and the count can't grow without bound.
//
// Nothing here knows what a "setup" is: the headline wording, the per-item
// title and the detail rows all come from the notification the backend wrote,
// so a new event type appears correctly with no change to this file.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Bell, X } from "lucide-react";
import { isKavachioAdmin } from "../auth";
import {
  consumeLoginNotice, fetchNotifications, markNotificationsRead,
  summarizeUnread, type NotificationFeed, type PlatformNotification,
} from "../api/notifications";

/** "just now" / "12 minutes ago" / "3 days ago" for a UTC ISO timestamp. */
function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.round((then - Date.now()) / 1000);
  const abs = Math.abs(secs);
  if (abs < 45) return "just now";
  const units: [Intl.RelativeTimeFormatUnit, number][] =
    [["day", 86400], ["hour", 3600], ["minute", 60]];
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (abs >= size) return rtf.format(Math.round(secs / size), unit);
  }
  return rtf.format(Math.round(secs / 60), "minute");
}

/** The most identifying facts for a one-line subtitle, in the order the writer
 *  listed them — skipping the one already shown as the broker name. */
function subtitle(n: PlatformNotification): string {
  return n.facts
    .filter(f => f.value && f.value !== n.tenant_name)
    .slice(0, 2)
    .map(f => f.value)
    .join(" · ");
}

export default function PlatformNotificationCard() {
  const nav = useNavigate();
  const [feed, setFeed] = useState<NotificationFeed | null>(null);

  useEffect(() => {
    // Only on a fresh sign-in, and only for platform admins — the endpoint is
    // kavachio_admin-only, so calling it as anyone else would just 403.
    if (!isKavachioAdmin() || !consumeLoginNotice()) return;
    let cancelled = false;
    fetchNotifications(5)
      .then(r => { if (!cancelled && r.data.unread > 0) setFeed(r.data); })
      .catch(() => { /* a missed notice must never break the app shell */ });
    return () => { cancelled = true; };
  }, []);

  if (!feed) return null;

  const dismiss = () => {
    // Mark read only up to what this card actually reported: anything that
    // arrives between the fetch and the dismiss stays unread for next time.
    markNotificationsRead(feed.latest_at).catch(() => { /* best-effort */ });
    setFeed(null);
  };

  const open = (n: PlatformNotification) => {
    dismiss();
    if (n.link_path) nav(n.link_path);
  };

  const shown = feed.items.filter(n => n.unread);
  const more = feed.unread - shown.length;

  return (
    // z-[1050]: above page content and the loading overlay, below the global
    // error popup (z-1100) so a real failure is never hidden behind this.
    <div
      role="status"
      aria-live="polite"
      className="fixed top-5 right-5 z-[1050] w-[360px] max-w-[calc(100vw-2.5rem)]
                 rounded-xl border border-border bg-white shadow-xl overflow-hidden"
    >
      <div className="flex items-start gap-3 px-4 pt-4 pb-3 border-b border-border">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full
                         bg-navy/10 text-navy">
          <Bell size={16} strokeWidth={2} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-ink leading-tight">
            {summarizeUnread(feed)}
          </div>
          <div className="text-xs text-ink-muted mt-0.5">
            Since you last checked
          </div>
        </div>
        <button
          onClick={dismiss}
          aria-label="Dismiss notifications"
          className="text-ink-soft hover:text-ink transition shrink-0"
        >
          <X size={16} />
        </button>
      </div>

      <ul className="max-h-[46vh] overflow-y-auto divide-y divide-border">
        {shown.map(n => {
          const sub = subtitle(n);
          return (
            <li key={n.id}>
              <button
                onClick={() => open(n)}
                className="w-full text-left px-4 py-3 hover:bg-surface-2 transition
                           focus-visible:outline-none focus-visible:bg-surface-2"
              >
                <div className="text-[13px] font-medium text-ink leading-snug">
                  {n.title}
                </div>
                {sub && (
                  <div className="text-xs text-ink-muted mt-1 truncate" title={sub}>
                    {sub}
                  </div>
                )}
                <div className="text-[11px] text-ink-soft mt-1">
                  {relativeTime(n.created_at)}
                </div>
              </button>
            </li>
          );
        })}
      </ul>

      <div className="px-4 py-2.5 bg-surface-2 flex items-center justify-between">
        <span className="text-[11px] text-ink-muted">
          {more > 0 ? `+${more} more` : " "}
        </span>
        <button
          onClick={dismiss}
          className="text-xs font-semibold text-navy hover:text-navy-dark transition"
        >
          Mark as read
        </button>
      </div>
    </div>
  );
}
