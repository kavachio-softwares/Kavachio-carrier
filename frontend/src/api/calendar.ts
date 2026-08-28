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
  period: string;
  period_start: string | null;
  period_end: string | null;
  due_date: string | null;
  status: CalendarStatus;
  received_at: string | null;
  received_export_id: number | null;
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
