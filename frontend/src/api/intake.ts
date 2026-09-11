import { api, streamNdjson } from "./client";
import { currentMga } from "../auth";

// Feature 10 — File Intake Channels. Backs "How Files Arrive" and
// "Files Received".

// The five ways a file can reach a carrier. Fixed: "you cannot invent a sixth".
// Adding a route gives ONE BROKER their own address on one of these.
export type Channel = "upload" | "email" | "sftp" | "api" | "cloud_folder";

// What a normal file from this broker looks like. Not a correctness switch —
// rows already loaded are recognised either way.
export type FileStyle = "whole_book" | "changes_only";

export type IntakeRoute = {
  route_id: number;
  channel: Channel;
  /** Path only, e.g. "northwind-insurance/bridge-brokers". */
  address: string;
  /** The full address to hand a broker — composed server-side from SFTP_HOST. */
  display_address: string;
  /** Email routes only: the plus-address to hand THIS broker. Email is the one
   *  channel where the address a broker sends FROM and the address they send TO
   *  are different things, so the screen needs both. */
  send_to: string | null;
  display_name: string | null;
  broker_party_id: number | null;
  broker_name: string | null;
  /** The programme this route is for. NULL = broker-wide: the broker is known
   *  but the programme is not, so an API sender must name one per file. */
  program_id: number | null;
  program_name: string | null;
  is_enabled: boolean;
  file_style: FileStyle;
  fallback_rank: number | null;
  note: string | null;
  files_this_month: number;
  /** True once something actually collects from this channel. Only sftp so far. */
  collecting: boolean;
  created_at: string | null;
  disabled_at: string | null;
  /** Only returned by create — the folder that was made on disk. */
  folder?: string | null;
};

export type BrokerLite = { party_id: number; legal_name: string };

/** An address we already hold for a broker — their portal login, NOT necessarily
 *  the mailbox their export job sends as. Offered as a suggestion the user can
 *  overwrite, never taken on trust. */
export type BrokerEmail = { email: string; name: string | null; status: string };
export type ProgrammeLite = { program_id: number; name: string };

export type RoutesResponse = {
  routes: IntakeRoute[];
  brokers: BrokerLite[];
  /** Which programmes each broker is on, keyed by party_id as a string. */
  broker_programmes: Record<string, ProgrammeLite[]>;
  /** Known addresses per broker, active first. Keyed by party_id as a string. */
  broker_emails: Record<string, BrokerEmail[]>;
  channels: Channel[];
  /** Channels we PULL from, as opposed to being pushed to. */
  collecting: Channel[];
  /** Channels that are wired end to end and can be created. */
  creatable: Channel[];
  sftp_host: string;
  /** 10.3 — the inbox brokers email. Config like sftp_host, not data. */
  email_mailbox: string | null;
  /** True once IMAP_HOST/USER/PASS are set, so the screen can say whether the
   *  mailbox is actually reachable rather than implying it is. */
  email_ready: boolean;
  /** How each pulled channel is noticing files right now. Optional so an older
   *  server that does not send it still renders. */
  collector?: { sftp: CollectorStatus; email: CollectorStatus };
  tiles: {
    ways_on: number;
    ways_total: number;
    files_this_month: number;
    most_used_channel: Channel | null;
    most_used_files: number;
    turned_away: number;
    held: number;
  };
};

// Three outcomes. "held" — a suspected duplicate, an empty file, or one with no
// live contract yet — became a real value in migration 10_2. It is NOT a
// refusal: the file arrived and is kept, it is just waiting on a person.
export type Outcome = "accepted" | "held" | "turned_away";

export type Arrival = {
  arrival_id: number;
  filename: string;
  channel: Channel | null;
  route_id: number | null;
  route_address: string | null;
  broker_party_id: number | null;
  broker_name: string | null;
  claimed_sender: string | null;
  file_size_bytes: number | null;
  /** Rows in the file, counted by the arrival checks. `null` means we could
   *  not open it — which is not the same as 0, "we opened it and it is empty".
   *  Also null on arrivals that landed before the column existed. */
  row_count: number | null;
  file_hash_sha256: string | null;
  received_at: string | null;
  outcome: Outcome;
  turned_away_reason: string | null;
  /** The programme the route this file arrived on is pinned to. Null for a
   *  broker-wide route: the broker is known, the programme is not. */
  program_id: number | null;
  program_name: string | null;
  sender_notified_at: string | null;
  sender_notified_via: string | null;
  bdx_upload_id: number | null;
  /** The reference a partner quotes back at us. */
  public_ref?: string | null;
  // ── 12.3 — what a PERSON decided ─────────────────────────────────────────
  /** null means nobody has looked at this yet — which is what makes it a queue. */
  resolution: "released" | "discarded" | null;
  resolved_at: string | null;
  resolved_by_user_id: number | null;
  resolution_note: string | null;
  /** Set once retention has deleted the stored bytes. The row outlives them. */
  bytes_purged_at: string | null;
  /** False when there is no stored copy, it has been purged, or the file failed
   *  the security scan — an infected file is never handed to anybody. */
  can_download: boolean;
  is_infected: boolean;
};

export type ArrivalsResponse = {
  rows: Arrival[];
  counts: { total: number; accepted: number; held: number; turned_away: number;
            /** Held AND unresolved — the only number anybody has to act on. */
            waiting: number };
};

/** How a pulled channel notices a file, as the server reports it.
 *  `watching` (SFTP, a filesystem watcher) and `idle` (email, IMAP IDLE) both
 *  mean the moment it lands; `timer` is the fallback when that cannot run.
 *  `check_seconds` is the backup sweep for the first two, the timer for the
 *  third. */
export type CollectorStatus = {
  mode: "watching" | "idle" | "timer" | "connecting" | "off" | "not_configured";
  check_seconds: number;
};

/** A refused file the design would call "held": kept, and waiting on a person. */
export function isHeld(a: Arrival): boolean {
  if (a.outcome === "held") return true;
  // Rows written before migration 10_2, when there was no value for "held" and
  // the reason text carried the distinction instead.
  return a.outcome === "turned_away" && !!a.turned_away_reason?.startsWith("Held —");
}

export async function listRoutes(): Promise<RoutesResponse> {
  const { data } = await api.get("/intake/routes", { params: { mga: currentMga() } });
  return data;
}

export async function createRoute(body: {
  channel: Channel; broker_party_id: number;
  /** Pin the route to one programme. Strongly preferred for API: the sender
   *  then supplies nothing but the file. */
  program_id?: number | null;
  /** REQUIRED for channel "email": the address this broker sends FROM. Every
   *  broker emails the same inbox, so the mailbox cannot tell routes apart —
   *  who the mail comes from is what does. */
  sender_email?: string;
  display_name?: string; file_style?: FileStyle; note?: string;
}): Promise<IntakeRoute> {
  const { data } = await api.post("/intake/routes", body, { params: { mga: currentMga() } });
  return data;
}

export async function patchRoute(routeId: number, body: {
  file_style?: FileStyle; is_enabled?: boolean; display_name?: string; note?: string;
}): Promise<IntakeRoute> {
  const { data } = await api.patch(`/intake/routes/${routeId}`, body,
    { params: { mga: currentMga() } });
  return data;
}

export async function listArrivals(limit = 100): Promise<ArrivalsResponse> {
  const { data } = await api.get("/intake/arrivals",
    { params: { mga: currentMga(), limit } });
  return data;
}

// ── the review queue (12.3) ─────────────────────────────────────────────────
// A refused file is kept so somebody can look at it. These are the three things
// they can do about it — until they existed, the screen's buttons were built
// disabled because a decision had nowhere to be recorded.

export type ReviewResult = {
  arrival_id: number; outcome: Outcome;
  resolution: "released" | "discarded"; resolved_at: string | null;
};

/** "This is fine, load it." Held files only — a turned-away file is one we
 *  could not read, and releasing it would hand processing something broken. */
export async function releaseArrival(arrivalId: number, note?: string): Promise<ReviewResult> {
  const { data } = await api.post(`/intake/arrivals/${arrivalId}/release`,
    { note: note || null }, { params: { mga: currentMga() } });
  return data;
}

/** "Ignore this." The row and the reason stay; only the decision is added. */
export async function discardArrival(arrivalId: number, note?: string): Promise<ReviewResult> {
  const { data } = await api.post(`/intake/arrivals/${arrivalId}/discard`,
    { note: note || null }, { params: { mga: currentMga() } });
  return data;
}

/** Open the stored copy. Saved through the browser rather than fetched into
 *  memory: these are whole bordereaux, and the download is logged server-side
 *  either way. */
export async function downloadArrival(arrivalId: number, filename: string): Promise<void> {
  const res = await api.get(`/intake/arrivals/${arrivalId}/download`,
    { params: { mga: currentMga() }, responseType: "blob" });
  const url = URL.createObjectURL(res.data as Blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename || "arrival";
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

/** Calls `onChange` whenever this carrier's files change — a file landed by any
 *  way in, or somebody decided one. The server pushes it the moment the change
 *  commits (GET /intake/events), which replaced a 60-second poll.
 *
 *  Keeps itself connected until `signal` aborts. A dropped stream, a server
 *  restart and the server's own periodic close (it re-checks the token) all
 *  reconnect — and a REconnect calls `onChange` too, because anything that
 *  landed in the gap was announced to nobody. */
export function watchArrivals(onChange: () => void, signal: AbortSignal): void {
  void (async () => {
    let connectedBefore = false;
    let wait = 1000;
    while (!signal.aborted) {
      const opened = Date.now();
      try {
        await streamNdjson("/intake/events", { mga: currentMga() ?? undefined }, msg => {
          if (msg?.type === "ready") {
            if (connectedBefore) onChange();
            connectedBefore = true;
          } else if (msg?.type === "arrivals") {
            onChange();
          }
        }, signal, { quiet: true });
      } catch { /* dropped, restarting or signed out — retried below */ }
      if (signal.aborted) return;
      // A stream that ran a while ended normally: reconnect at once. One that
      // failed straight away backs off, so a server that is down is not hammered.
      wait = Date.now() - opened > 30_000 ? 1000 : Math.min(wait * 2, 30_000);
      await new Promise<void>(resolve => {
        const t = window.setTimeout(resolve, wait);
        signal.addEventListener("abort", () => { window.clearTimeout(t); resolve(); },
          { once: true });
      });
    }
  })();
}


// ── API keys (feature 10.2) ─────────────────────────────────────────────────
// SFTP knows a sender by the folder the file landed in. An API caller has no
// folder, so the key does that job: it is bound to one route, and the route
// carries the broker and the programme.

export type IntakeKey = {
  credential_id: number;
  label: string | null;
  /** Masked — `kv_live_7d2e4b9016fa_…3aQe`. The real key is never returned. */
  key: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  is_live: boolean;
};

/** The ONE time the plaintext key exists outside the broker's hands. */
export type NewIntakeKey = {
  credential_id: number;
  label: string | null;
  api_key: string;
  warning: string;
};

export async function listKeys(routeId: number): Promise<IntakeKey[]> {
  const { data } = await api.get(`/intake/routes/${routeId}/keys`,
    { params: { mga: currentMga() } });
  return data;
}

export async function createKey(routeId: number, label?: string): Promise<NewIntakeKey> {
  const { data } = await api.post(`/intake/routes/${routeId}/keys`, { label: label || null },
    { params: { mga: currentMga() } });
  return data;
}

/** Revoke, never delete: every file that arrived on this key still points at it. */
export async function revokeKey(credentialId: number): Promise<{ credential_id: number }> {
  const { data } = await api.delete(`/intake/keys/${credentialId}`,
    { params: { mga: currentMga() } });
  return data;
}
