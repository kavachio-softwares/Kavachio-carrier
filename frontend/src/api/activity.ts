// C-9 — the activity feed that backs the notification bell. The backend already
// writes ActivityEvents (e.g. submission_overdue / submission_due_soon from the
// calendar sweep); this just reads them back for the in-app bell.
import { api } from "./client";

export type ActivityEvent = {
  id: number;
  actor: string | null;
  action: string;
  target: string | null;
  details: Record<string, any> | null;
  created_at: string | null;
};

/** `actions` narrows to specific event types SERVER-SIDE, before the limit bites.
 *  Without it the notification surfaces ask for the newest 25 rows of a feed
 *  that carries every activity this tenant produces, and reminders lose their
 *  slots to uploads and logins — 19 of 93 reached the screen, with each new
 *  reminder quietly displacing an older one. */
export async function getActivity(
  mga: string, limit = 25, actions?: string[],
): Promise<ActivityEvent[]> {
  const { data } = await api.get<ActivityEvent[]>("/activity", {
    params: { mga, limit, ...(actions?.length ? { actions: actions.join(",") } : {}) },
  });
  return Array.isArray(data) ? data : [];
}
