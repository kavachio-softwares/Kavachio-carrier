// Platform-admin notifications — cross-tenant events Kavachio staff need to
// know about (today: a broker taking a Bordereau Setup live).
//
// Both endpoints are kavachio_admin-only; callers must gate on isKavachioAdmin()
// rather than relying on the 403.
import { api } from "./client";

export type NotificationFact = { label: string; value: string | null };

export type PlatformNotification = {
  id: number;
  kind: string;                       // e.g. "bordereau_setup_activated"
  title: string;
  body: string | null;
  target: string | null;              // e.g. "pipeline:123"
  actor: string | null;               // email of the user who acted
  tenant_id: number | null;
  tenant_name: string | null;
  /** Labelled detail rows, exactly as they appear in the notification email. */
  facts: NotificationFact[];
  /** In-app destination for this event, when it has one. */
  link_path: string | null;
  /** The single next step being asked for, in the words of the screen it
   *  points at (e.g. "Set Up Kavachio Mapping"). Null when purely FYI. */
  action: string | null;
  details: Record<string, unknown>;
  created_at: string | null;
  unread: boolean;
};

/** Unread total for one event type, with the wording it was written with —
 *  so the UI can say "3 bordereau setups activated" without a kind→text table
 *  of its own (a new event type needs no frontend change). */
export type UnreadKind = {
  kind: string; count: number; label: string; label_plural: string;
};

export type NotificationFeed = {
  items: PlatformNotification[];
  /** Every unread notification, not just the ones in `items`. */
  unread: number;
  unread_by_kind: UnreadKind[];
  seen_at: string | null;
  /** Newest notification's timestamp — hand it back to markRead so anything
   *  that arrives while the admin is reading stays unread. */
  latest_at: string | null;
};

/** `silent` keeps this off the global loading overlay: it's a background poll
 *  the user never asked for, and blanking the screen for it would be wrong. */
export function fetchNotifications(limit = 10) {
  return api.get<NotificationFeed>("/admin/notifications",
    { params: { limit }, silent: true });
}

export function markNotificationsRead(upto?: string | null) {
  return api.post<{ unread: number; seen_at: string | null }>(
    "/admin/notifications/read", { upto: upto ?? null }, { silent: true });
}

/** One line summarising what's waiting: "3 bordereau setups activated".
 *  Falls back to a neutral count when several event types are mixed. */
export function summarizeUnread(feed: NotificationFeed): string {
  const groups = feed.unread_by_kind ?? [];
  if (groups.length === 1) {
    const g = groups[0];
    return `${g.count} ${g.count === 1 ? g.label : g.label_plural}`;
  }
  return `${feed.unread} new notification${feed.unread === 1 ? "" : "s"}`;
}

// --- "show it once, on sign-in" trigger -------------------------------------
// The corner message is a SIGN-IN event, not a per-page one, so it can't just
// fire on mount — Layout mounts on every route. Login arms this flag and the
// toast consumes it, so navigating around (or reloading) never re-opens it,
// while a genuine new sign-in always does. sessionStorage, so it dies with the
// tab and never leaks between accounts on a shared machine.
const LOGIN_FLAG = "kavachio:notify-on-login";

export function armLoginNotice(): void {
  try { sessionStorage.setItem(LOGIN_FLAG, "1"); } catch { /* private mode */ }
}

/** True at most once per sign-in — reading it clears the flag. */
export function consumeLoginNotice(): boolean {
  try {
    const armed = sessionStorage.getItem(LOGIN_FLAG) === "1";
    if (armed) sessionStorage.removeItem(LOGIN_FLAG);
    return armed;
  } catch {
    return false;
  }
}
