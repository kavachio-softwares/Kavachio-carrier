// ---------------------------------------------------------------------------
// The Phase 2 switch, as the screens see it.
//
// One server setting, PHASE2_ENABLED (backend/python-services/phase2.py),
// shows or hides every Phase 2 feature — the "Phase 2" tab of the carrier plan
// (tasks 23–54): reports, Policy Explorer, Financial Summary, Partner
// Performance, reinsurance, late-file cases, programme rules and the rest.
// Audit Logs are not behind it.
//
// The value is read ONCE, before the app first draws (main.tsx), so routes and
// the sidebar never flicker. Off by default: if the server cannot be reached
// or does not know the setting, Phase 2 stays hidden. Hiding here is the UX
// half only — the server also answers 404 on every Phase 2 route while off.
//
// To hide a Phase 2 screen: mark its ROUTE_ACCESS rule `phase2: true`
// (access.ts) — the route, its sidebar entry and every canAccessPath() link
// follow. For a card, tab or button inside a shared screen, check
// phase2Enabled() where it is drawn.
// ---------------------------------------------------------------------------

import axios from "axios";
import { api } from "./api/client";

let phase2 = false;

/** True when the Phase 2 features are switched on (server PHASE2_ENABLED). */
export function phase2Enabled(): boolean {
  return phase2;
}

/** Read the switches from the server. Never throws; settles within ~5 s. */
export async function loadFeatures(): Promise<void> {
  try {
    // Plain axios, not `api`: no login needed, and a server that is down at
    // boot must not raise the global error popup before anything is drawn.
    const r = await axios.get<{ phase2?: boolean }>(
      `${api.defaults.baseURL}/features`, { timeout: 5000 });
    phase2 = r.data?.phase2 === true;
  } catch {
    phase2 = false;
  }
}
