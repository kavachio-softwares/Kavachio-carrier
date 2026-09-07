import { api } from "./client";
import { currentMga } from "../auth";

// Group 3 — "Never miss a deadline": the broker's own submission calendar.

// The deadline ladder, in order: nothing owed yet → warned → the day itself →
// missed. There is no grace period, so "overdue" is the only past-due state —
// the retired "late" was a second name for the same thing.
export type CalendarStatus =
  | "scheduled" | "due_soon" | "due_today" | "overdue"
  | "on_time" | "received_late";

export type CalendarRow = {
  id: number;
  program_id: number;
  // Which broker owes this one. Null on a programme that has no broker on it
  // yet — nobody can be late when there is nobody to be late.
  broker_party_id: number | null;
  broker_name: string | null;
  period: string;
  period_start: string | null;
  period_end: string | null;
  due_date: string | null;
  status: CalendarStatus;
  // The FIRST arrival, and the only one that decides on-time vs late.
  received_at: string | null;
  received_export_id: number | null;
  // Versions and release — requirement 17.2. `version_count` is 1 for a period
  // filed once, 2+ once it has been corrected and resent; `released_at` is when
  // the file went ONWARD to its recipient, which is a different act from
  // producing it.
  version_count: number;
  version_label: string;
  latest_received_at: string | null;
  released_at: string | null;
  released_count: number;
  /** Whether the file the recipient would get TODAY has actually gone. False on
   *  a period corrected after it was sent — they hold a superseded version. */
  latest_version_released: boolean;
  chased_at: string | null;
  chase_count: number;
};

export type CalendarResponse = {
  rows: CalendarRow[];
  counts: Partial<Record<CalendarStatus, number>>;
};

export type ScheduleState = {
  exists: boolean;
  resolved: boolean;
  reason: string | null;                 // not_set | need_frequency | need_start_date | need_frequency_and_start
  contract_frequency?: string | null;    // what the contract supplies (fallback for the override fields)
  contract_anchor?: string | null;
  program_id?: number;
  contract_id?: number | null;
  frequency_override?: string | null;
  anchor_date_override?: string | null;
  due_day_of_month?: number | null;      // monthly/quarterly deadline
  due_offset_days?: number | null;       // weekly deadline
  soon_window_days?: number | null;
  effective_frequency?: string;
  effective_anchor?: string;
};

export type ScheduleBody = {
  frequency_override?: string | null;
  anchor_date_override?: string | null;   // 'YYYY-MM-DD'
  due_day_of_month?: number | null;
  due_offset_days?: number | null;
  soon_window_days?: number | null;
  contract_id?: number | null;
};

// `party_id` is the carrier FK and the authoritative link — `lead_carrier` is a
// free-text column that is often blank, so it is only ever a display fallback.
export type ProgramLite = {
  id: number; name: string; bdx_frequency?: string | null;
  party_id?: number | null; lead_carrier?: string | null;
};

export type CarrierLite = { id: number; legal_name: string; dba_name?: string | null };

export async function getCalendar(programId?: number): Promise<CalendarResponse> {
  const { data } = await api.get<CalendarResponse>("/calendar", {
    params: programId != null ? { program_id: programId } : {},
  });
  return data;
}

export async function getSchedule(programId: number): Promise<ScheduleState> {
  const { data } = await api.get<ScheduleState>(`/programs/${programId}/schedule`);
  return data;
}

export async function putSchedule(
  programId: number, body: ScheduleBody,
): Promise<{ schedule: ScheduleState; calendar: { resolved: boolean; count: number } }> {
  const { data } = await api.put(`/programs/${programId}/schedule`, body);
  return data;
}

export async function listPrograms(): Promise<ProgramLite[]> {
  const { data } = await api.get<ProgramLite[]>("/programs", {
    params: { mga: currentMga() },
  });
  return data;
}

/** Carrier names for the calendar's carrier picker. Only used to label the
 *  party_id already on each program, so a failure here is non-fatal — the
 *  picker falls back to the program's own lead_carrier text. */
export async function listCarriers(): Promise<CarrierLite[]> {
  const { data } = await api.get<{ items: CarrierLite[] }>("/parties", {
    params: { mga: currentMga() },
  });
  return data.items ?? [];
}


// ---------------------------------------------------------------------------
// The carrier's Bordereau Calendar — what each broker owes, what turned up,
// what went onward. Keyed on the DUE MONTH rather than the reporting period,
// because programmes on different frequencies have to share one page: a monthly
// programme's July file and a quarterly programme's Q2 file are both due in
// August, and August is the only heading both belong under.
// ---------------------------------------------------------------------------

export type BoardRow = {
  id: number;
  program_id: number;
  program_name: string;
  broker_party_id: number | null;
  broker_name: string | null;
  /** True when the programme has no broker on it — it owes nothing. */
  unassigned: boolean;
  period: string;
  due_date: string | null;
  received_at: string | null;
  latest_received_at: string | null;
  status: CalendarStatus;
  /** Days past the due date with nothing received. */
  days_over: number | null;
  /** Days between the due date and the arrival, when it arrived late. */
  days_late: number | null;
  released_at: string | null;
  released_count: number;
  latest_version_released: boolean;
  /** Who at the broker would be told, best contact first. Empty when the broker
   *  has no user account — "we don't know who to tell", not "nobody". */
  contacts: BrokerContact[];
  version_count: number;
  version_label: string;
  chased_at: string | null;
  chase_count: number;
};

export type BrokerContact = {
  name: string; email: string; role: string | null;
  /** 'active' or 'invited' — an invited contact has been set up but has never
   *  signed in, which is worth showing before you rely on reaching them. */
  status: string;
};

export type BoardSchedule = {
  program_id: number;
  program_name: string;
  frequency: string | null;
  frequency_label: string;
  due_rule: string;
  next_due: string | null;
  broker_count: number;
  /** When the contract stops. This is what bounds the calendar — deadlines are
   *  no longer generated past it. Null when no contract states an expiry, in
   *  which case a rolling horizon is used instead. */
  covers_until: string | null;
};

export type BoardResponse = {
  month: string;                       // 'YYYY-MM' — the due month on screen
  months: string[];                    // every month that has anything in it
  rows: BoardRow[];
  counts: { due: number; on_time: number; late: number;
            never: number; released: number;
            /** Corrected after being sent, and the correction never went out. */
            unsent_correction: number };
  programme_count: number;
  schedules: BoardSchedule[];
};

export type SubmissionVersionRow = {
  id: number;
  version_no: number;
  kind: "original" | "corrected";
  received_at: string | null;
  received_export_id: number | null;
  source_filename: string | null;
  /** How the period was decided: explicit | date | filename | oldest_open.
   *  "the file said July" and "we assumed July" are different confidences. */
  period_source: string | null;
  released_at: string | null;
  released_to: string | null;
  released_by: string | null;
  release_ref: string | null;
  note: string | null;
};

export async function getBoard(month?: string): Promise<BoardResponse> {
  const { data } = await api.get<BoardResponse>("/calendar/board", {
    params: { mga: currentMga(), ...(month ? { month } : {}) },
  });
  return data;
}

export async function getVersions(expectedId: number)
  : Promise<{ expected_id: number; period: string; versions: SubmissionVersionRow[] }> {
  const { data } = await api.get(`/calendar/${expectedId}/versions`);
  return data;
}

export async function releasePeriod(expectedId: number, body: {
  released_on?: string; released_to?: string;
  release_ref?: string; version_no?: number; note?: string;
}) {
  const { data } = await api.post(`/calendar/${expectedId}/release`, body);
  return data;
}

/** Record a chase against the given periods, with an optional note kept on the
 *  record. Passing no ids chases everything overdue for the tenant. */
export async function chase(expectedIds?: number[], note?: string)
  : Promise<{ chased: number; rows: { expected_id: number; period: string;
                                      days_over: number }[] }> {
  const { data } = await api.post("/calendar/chase",
    { expected_ids: expectedIds ?? null, note: note ?? null },
    { params: { mga: currentMga() } });
  return data;
}
