// Feature 10 — the "Ways in" tab of "Files" (was the "How Files Arrive" page).
//
// A broker should not have to log in to send you a file. Most already email
// their spreadsheet or drop it on a server, and asking them to change that is
// usually what stalls a new programme. So there are four ways in, and whichever
// one a file uses it lands in the same queue and gets the same checks.
//
// This is a Configure screen, not part of the monthly run: a route is set up
// once when a broker is onboarded and then rarely touched.
//
// Styled with the `.proto` design system (proto.css) rather than the Tailwind
// page shell, because that is what the wireframe for this screen is drawn in —
// same page-head / tiles / card / badge vocabulary, so it sits beside the
// prototype without a visual seam. The page wraps itself in `.proto .view.full`
// the way Calendar.tsx does; modal bodies use `.proto.proto-embed` so they
// resolve .field/.kv/.badge without painting a grey slab inside the dialog.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createKey, createRoute, emailRouteGuide, listKeys, listRoutes, newSftpLogin, patchRoute,
  revokeKey, testSftp,
  type BrokerEmail, type BrokerLite, type Channel, type IntakeKey, type IntakeRoute,
  type GuideSent, type NewIntakeKey,
  type ProgrammeLite, type RoutesResponse,
  type SftpAfter, type SftpAuth, type SftpConnection, type SftpInterval, type SftpLogin,
  type SftpRouteInfo, type SftpServerInfo, type SftpTestResult,
} from "../api/intake";
import { Check, ChevronDown, ChevronUp, Mail, Server, Upload, Zap } from "lucide-react";
import { Modal } from "../components/ui/Modal";
import { getRouteContacts, putRouteContacts } from "../api/submissions";
import { InfoTip } from "../components/InfoTip";

// Copy lives here, not in the API, because it is interface language rather than
// data — the backend has no opinion on what "the old-fashioned way" means.
const API_BASE: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

const CHANNEL_COPY: Record<Channel, { title: string; sub: string; hint: string }> = {
  upload: {
    // "someone" was accurate while a carrier seat could upload too. It cannot
    // any more — Process Bordereau is the broker's screen now (access.ts) — so
    // this names the broker rather than leaving a carrier reading their own
    // intake page to conclude they are the someone.
    title: "Manual Upload", sub: "the broker signs in and uploads the file",
    hint: "their Process Bordereau screen",
  },
  email: {
    title: "Email", sub: "the broker emails the file to their own address",
    hint: "an address you give the broker",
  },
  sftp: {
    title: "SFTP", sub: "the broker uploads the file to a secure folder",
    hint: "a folder of their own",
  },
  api: {
    title: "API", sub: "the broker's system sends the file automatically",
    hint: "POST /v1/bordereaux",
  },
  cloud_folder: {
    title: "Cloud Folder", sub: "the broker saves the file in a folder we watch",
    hint: "S3 · SharePoint · Google Drive",
  },
};

// A mark per channel, so four groups that are otherwise four identical grey
// rows can be told apart by shape. The tones deliberately match CAME_IN_BY on
// the Inbox tab: the same way in is the same colour on both tabs, which is what
// makes "SFTP" on a row and the same heading here read as
// one thing rather than two.
const CHANNEL_MARK: Record<Channel, { Icon: React.ElementType; tone: string }> = {
  upload: { Icon: Upload, tone: "ok" },
  email: { Icon: Mail, tone: "info" },
  sftp: { Icon: Server, tone: "" },
  api: { Icon: Zap, tone: "" },
  cloud_folder: { Icon: Server, tone: "" },
};

// The ways in this carrier actually uses. `cloud_folder` is in the Channel type
// because the database still knows the value, but it is not a requirement here
// and is deliberately absent — a door nobody asked for reading "Not built yet"
// is noise, not honesty.
const CHANNEL_ORDER: Channel[] = ["upload", "email", "sftp", "api"];

// What a server folder actually does. Fixed behaviour, not settings — which is
// why it reads as a description and not as a form.
// What each way in actually DOES. Fixed behaviour, not settings — which is why
// it reads as a description and not as a form. Keyed by channel so the Settings
// dialog describes the route in front of you rather than always describing SFTP.
const CHANNEL_DETAIL: Partial<Record<Channel, [string, string][]>> = {
  sftp: [
    ["How they sign in", "Their own user name and password on Kavachio's SFTP server, emailed to them"],
    ["What they can see", "Only /incoming, to upload, and /outbound, our answer to each file — never another broker's files"],
    ["When we pick it up", "The moment it has finished uploading — nobody has to press anything"],
    ["After we take it", "Moved out of /incoming so it cannot be read twice"],
    ["If the file is still being written", "We wait until the upload is complete"],
    ["What they hear back", "A receipt, then the result and any exceptions — in /outbound and by email"],
    ["Reporting period", "Required, in the file name, e.g. Premium_BDX_2026-07.xlsx — a file without one is rejected"],
    ["Programme & contract", "Taken as read when the broker has only one; otherwise named in the file name, e.g. Demonity_RiskContract_2026-07.xlsx"],
    ["A later file for the same month", "Becomes the next version of that file — however the first one came in"],
  ],
  email: [
    ["Where they send it", "Kavachio's intake mailbox, with you copied (Cc)"],
    ["How we know it is them", "The From: address, and your address in Cc — without it the file is turned away"],
    ["When we pick it up", "The moment it reaches the mailbox"],
    ["After we take it", "Filed into /Processed so it cannot be read twice"],
    ["Attachments we ignore", "Signatures, logos and anything not a spreadsheet"],
    ["If we cannot use it", "The sender can be notified — email is the only channel with a reply path"],
    ["Reporting period", "Required, in the subject line or file name, e.g. \"Bordereau – July 2026\" — a file without one is rejected"],
    ["Programme & contract", "Taken as read when the broker has only one; otherwise named in the subject line, e.g. \"Demonity – Risk Contract – July 2026\""],
    ["A later file for the same month", "Becomes the next version of that file — however the first one came in"],
  ],
  api: [
    ["Reporting period", "Sent as period, e.g. 2026-07"],
    ["Programme & contract", "Use the programme and contract name, or their code"],
  ],
};

// No API entry: its dialog opens straight on the keys, with no intro above them.
const CHANNEL_BLURB: Partial<Record<Channel, string>> = {
  sftp: "The oldest way of moving a file, and still the most dependable. The broker signs in to Kavachio's SFTP server with a login of their own, uploads the file, and we pick it up.",
};

function Badge({ tone, children }:
  { tone: "ok" | "warn" | "crit" | "mut"; children: React.ReactNode }) {
  return <span className={`badge b-${tone}`}><span className="d" />{children}</span>;
}

/** How a pulled channel is noticing files right now, as a tail for its group
 *  heading — or nothing. Read from the server, because "the moment it lands" is
 *  only true while the folder watcher or the mailbox connection is actually up. */
function pickupNote(ch: Channel, collector: RoutesResponse["collector"]): string {
  const s = ch === "sftp" ? collector?.sftp : ch === "email" ? collector?.email : undefined;
  if (!s) return "";
  const every = s.check_seconds >= 120
    ? `${Math.round(s.check_seconds / 60)} minutes` : `${s.check_seconds} seconds`;
  switch (s.mode) {
    case "watching":
    case "idle": return " · picked up the moment it lands";
    case "timer": return ` · checked every ${every}`;
    case "connecting": return " · reconnecting to the mailbox…";
    case "off": return " · collecting is switched off on the server";
    default: return "";
  }
}

// ── external SFTP pull ──────────────────────────────────────────────────────
// A pull route is Kavachio signing in to somebody else's server on a timer, so
// what identifies it is that server, not a folder of ours.

/** How a pull route is written everywhere: sftp://user@host:port/dir. */
function sftpUrl(s: Pick<SftpRouteInfo, "host" | "port" | "username" | "remote_dir">): string {
  const dir = s.remote_dir.startsWith("/") ? s.remote_dir : `/${s.remote_dir}`;
  return `sftp://${s.username}@${s.host}:${s.port}${dir}`;
}

/** The address a route shows: the server it is collected from, for a pull
 *  route; where the broker signs in, for one on Kavachio's own server. */
function routeAddress(r: IntakeRoute): string {
  return r.sftp ? sftpUrl(r.sftp) : r.login ? loginUrl(r.login) : r.display_address;
}

// ── Kavachio's own SFTP server ──────────────────────────────────────────────
// The other SFTP kind: the broker signs in to OUR server with a login of their
// own, uploads into /incoming, and finds our answer to each file in /outbound.

/** An SFTP channel on Kavachio's own server — the kind with a broker login. */
function isHosted(r: IntakeRoute): boolean {
  return r.channel === "sftp" && !r.sftp;
}

/** sftp://user@host:port — how FileZilla and friends take it in one line. */
function loginUrl(l: Pick<SftpLogin, "username" | "host" | "port">): string {
  return `sftp://${l.username}@${l.host}:${l.port}`;
}

/** Who an email really reaches: its recipients, less any the test guard
 *  (MAIL_ALLOWED_RECIPIENTS) holds back. */
function delivered(g?: GuideSent | null): string[] {
  if (!g || !g.sending) return [];
  const held = new Set((g.blocked ?? []).map(x => x.toLowerCase()));
  return g.recipients.filter(r => !held.has(r.toLowerCase()));
}

/** "sftp.example.com/outgoing", for a sentence. */
/** A test-connection note that only reports what was done, not a problem. */
function isInfoNote(w: string): boolean {
  return w.startsWith("We created the folder");
}

function hostAndFolder(host: string, dir: string): string {
  return `${host}${dir.startsWith("/") ? "" : "/"}${dir}`;
}

/** Offered as the processed folder until somebody types their own. */
function defaultProcessed(dir: string): string {
  return `${dir.trim().replace(/\/+$/, "")}/processed`;
}

const PULL_BLURB = "Kavachio signs in to this SFTP server on a schedule and collects finished "
  + "files from one folder.";

/** What a pull route does, read from what the server kept — never the secret. */
function pullDetail(s: SftpRouteInfo): [string, string][] {
  const shared = (CHANNEL_DETAIL.sftp ?? []).filter(([k]) =>
    k === "Reporting period" || k === "Programme & contract" || k === "A later file for the same month");
  return [
    ["How we sign in", `${s.auth === "key" ? "Private key" : "Password"} — stored encrypted, never shown`],
    ["After we take it", s.after === "delete" ? "Deleted from the server"
      : `Moved to ${s.processed_dir || defaultProcessed(s.remote_dir)} so it cannot be collected twice`],
    ["How often we check", `Every ${s.interval_minutes} minutes`],
    ["Last checked", s.last_checked_at ? new Date(s.last_checked_at).toLocaleString() : "Not yet"],
    ["Server fingerprint", s.fingerprint ?? "—"],
    ["If the file is still being written", "We wait until it stops changing"],
    ...shared,
  ];
}

/** What SFTP logins a broker already has on Kavachio's server — one per
 *  programme, or one for all of them (program_id null). Collect-from-their-
 *  own-server channels are not logins and do not count. */
export type SftpLoginScope = { program_id: number | null; username: string | null };

function sftpLoginsOf(routes: IntakeRoute[]): Record<string, SftpLoginScope[]> {
  const out: Record<string, SftpLoginScope[]> = {};
  for (const r of routes) {
    if (r.channel !== "sftp" || r.broker_party_id == null || r.address.startsWith("sftp://"))
      continue;
    (out[String(r.broker_party_id)] ??= []).push(
      { program_id: r.program_id, username: r.login?.username ?? null });
  }
  return out;
}

export default function WaysInTab({ onSummary, onDialogOpen, onAddData, refreshKey, liveTick = 0 }: {
  /** Reported up for the button that opens this panel: how many ways in exist,
   *  and whether any of them looks live and accepts nothing. */
  onSummary?: (s: { routes: number; needsAttention: number }) => void;
  /** True while Settings is open. Modal has
   *  its own window-level Escape handler, so without this the same keypress
   *  closes the dialog AND the panel underneath it. */
  onDialogOpen?: (open: boolean) => void;
  /** AddRouteModal is rendered by the shell, OUTSIDE this panel, so it can open
   *  on its own — a closed panel is `visibility: hidden` and its children
   *  inherit that, which is why the two used to have to open together. It still
   *  needs what only this fetch knows, so that goes up. */
  onAddData?: (d: {
    brokers: BrokerLite[];
    programmesByBroker: Record<string, ProgrammeLite[]>;
    emailsByBroker: Record<string, BrokerEmail[]>;
    creatable: Channel[];
    mailbox: string | null;
    carrierCc: string | null;
    mailReady: boolean;
    sftpServer: SftpServerInfo | null;
    sftpLoginsByBroker: Record<string, SftpLoginScope[]>;
  }) => void;
  /** Bumped by Refresh, and after a route is created. */
  refreshKey?: number;
  /** Bumped when the server says files changed — this month's counts move. */
  liveTick?: number;
}) {
  const [data, setData] = useState<RoutesResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [settingsFor, setSettingsFor] = useState<IntakeRoute | null>(null);
  // A key just made from a card's Email Instructions button: the keys dialog
  // opens showing it, because this is the only moment its text can be copied.
  const [madeKey, setMadeKey] = useState<NewIntakeKey | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setData(await listRoutes()); setErr(null); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? e?.message ?? "Failed to load."); }
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);
  // A file landing moves this month's counts, and nobody presses anything for
  // it to be collected any more — so the panel re-reads when the server says so.
  useEffect(() => { if (liveTick) load(); }, [liveTick, load]);

  // One row per channel, with its routes hung underneath. A channel with no
  // routes still renders — it is one of the four whether or not it is used, and
  // that is what lets email and API light up a row later rather than needing a
  // redesign.
  const byChannel = useMemo(() => {
    const map = new Map<Channel, IntakeRoute[]>();
    for (const c of CHANNEL_ORDER) map.set(c, []);
    for (const r of data?.routes ?? []) map.get(r.channel)?.push(r);
    return map;
  }, [data]);

  const emailRoutes = byChannel.get("email") ?? [];

  // An API route with no live key looks On and accepts nothing — the single
  // most confusing state on this screen, and invisible until now. Keys are
  // fetched per route because there is no bulk endpoint; only API routes have
  // any, so this is at most a handful of requests.
  const [keys, setKeys] = useState<Record<number, IntakeKey[]>>({});
  useEffect(() => {
    const apiRoutes = (data?.routes ?? []).filter(r => r.channel === "api");
    if (apiRoutes.length === 0) { setKeys({}); return; }
    let dead = false;
    Promise.all(apiRoutes.map(async r =>
      [r.route_id, await listKeys(r.route_id).catch(() => [] as IntakeKey[])] as const))
      .then(pairs => { if (!dead) setKeys(Object.fromEntries(pairs)); });
    return () => { dead = true; };
  }, [data]);

  // Every channel starts collapsed; the header line already says how many
  // brokers use it, so the detail opens only when someone asks for it.
  const [expanded, setExpanded] = useState<Set<Channel>>(new Set());

  // Four things count: a way in switched off, an API route with no key, an
  // SFTP server we last failed to reach, and an SFTP channel on our server
  // with no login. Each looks fine and takes no file.
  const needsAttention = useMemo(() => (data?.routes ?? []).filter(r =>
    !r.is_enabled || !!r.sftp?.last_error || (isHosted(r) && !r.login?.live) ||
    (r.channel === "api" && keys[r.route_id] !== undefined &&
     keys[r.route_id].every(k => !k.is_live))).length, [data, keys]);

  // Upload always shows — it is real and needs no setup. Everything else shows
  // if it can be created or already has routes.
  const creatable = data?.creatable ?? [];
  const builtChannels = CHANNEL_ORDER.filter(c =>
    c === "upload" || creatable.includes(c) || (byChannel.get(c) ?? []).length > 0);

  // Switching a route off silently stops a broker's files. It used to be a link
  // in a row of links, at the same weight as Settings, with nothing between a
  // mis-click and a programme going quiet until month-end.
  async function toggle(route: IntakeRoute) {
    if (route.is_enabled && !window.confirm(
      `Switch off this channel for ${route.broker_name ?? "this broker"}?\n\n`
      + `Files sent to ${routeAddress(route)} will stop being collected. `
      + `Nothing already received is affected, and you can switch it back on.`)) return;
    setBusy(true);
    try { await patchRoute(route.route_id, { is_enabled: !route.is_enabled }); await load(); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not change it."); }
    finally { setBusy(false); }
  }
  const routeCount = data?.routes.length ?? 0;
  useEffect(() => {
    onSummary?.({ routes: routeCount, needsAttention });
  }, [routeCount, needsAttention, onSummary]);

  useEffect(() => {
    if (!data) return;
    onAddData?.({
      brokers: data.brokers, programmesByBroker: data.broker_programmes,
      emailsByBroker: data.broker_emails ?? {},
      creatable: data.creatable, mailbox: data.email_mailbox,
      carrierCc: data.carrier_cc ?? null,
      mailReady: data.email_ready,
      sftpServer: data.sftp_server ?? null,
      sftpLoginsByBroker: sftpLoginsOf(data.routes),
    });
  }, [data, onAddData]);

  const dialogOpen = !!settingsFor;
  useEffect(() => { onDialogOpen?.(dialogOpen); }, [dialogOpen, onDialogOpen]);


  return (
    <>
      {err && <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>}

      {/* One row per CHANNEL crammed every broker into stacked cells — three
          addresses in one cell, three names in the next — and you matched
          them by vertical position, so adding a broker shifted everything
          below it. A channel is a group now, and each broker is a card you
          read across. */}
      <div className="card">
        <div className="card-h">
          <h3>Ingestion Channels</h3>
          <span className="sub">Grouped by channel</span>
        </div>

        {builtChannels.map(ch => {
          const routes = byChannel.get(ch) ?? [];
          const open = expanded.has(ch);
          const files = routes.reduce((n, r) => n + r.files_this_month, 0);
          const brokers = new Set(routes.map(r => r.broker_party_id)).size;
          return (
            <div className="chan-grp" key={ch}>
              <button type="button" className="chan-hd" aria-expanded={open}
                aria-controls={`grp-${ch}`}
                onClick={() => setExpanded(prev => {
                  const next = new Set(prev);
                  if (next.has(ch)) next.delete(ch); else next.add(ch);
                  return next;
                })}>
                <span className="caret" aria-hidden="true" />
                <span className={`ci ${CHANNEL_MARK[ch].tone}`} aria-hidden="true">
                  {(() => { const { Icon } = CHANNEL_MARK[ch]; return <Icon size={14} />; })()}
                </span>
                <span className="ttl">{CHANNEL_COPY[ch].title}</span>
                <span className="meta">
                  {ch === "upload"
                    ? "always on · anyone with a login · nothing to set up"
                    : routes.length === 0 ? "no brokers on this channel yet"
                    : `${brokers} broker${brokers === 1 ? "" : "s"} · ${files} file${files === 1 ? "" : "s"} this month${
                      /* The watcher's mode describes our own folder; a pull
                         route says its own schedule on its card. */
                      ch === "sftp" && routes.every(r => r.sftp) ? "" : pickupNote(ch, data?.collector)}`}
                </span>
              </button>

              {open && (
                <div className="routes" id={`grp-${ch}`}>
                  {ch === "upload" ? (
                    <div className="note">
                      A broker can drag a file in on their own{" "}
                      <b>Process Bordereau</b> screen. It is the fallback for
                      everything else, so it cannot be switched off and there is
                      nothing to configure.
                    </div>
                  ) : routes.length === 0 ? (
                    <div className="note">
                      No broker uses this channel yet. Use <b>Configure Ingestion Channel</b> to give
                      one their own address on it.
                    </div>
                  ) : routes.map(r => (
                    <RouteCard key={r.route_id} route={r} busy={busy}
                      cc={data?.carrier_cc ?? null}
                      brokerProgrammes={data?.broker_programmes?.[String(r.broker_party_id)] ?? []}
                      keys={keys[r.route_id]}
                      onSettings={() => { setMadeKey(null); setSettingsFor(r); }}
                      onKeyMade={k => { setMadeKey(k); setSettingsFor(r); }}
                      onToggle={() => !busy && toggle(r)} />
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {/* Four explainer cards used to sit below this one — roughly 60% of the
            page height, read once, and printing every SFTP and email address a
            SECOND time under a "in detail" heading, with no Copy button beside
            the copy people actually needed. Folded away, addresses removed. */}
      </div>

      <SettingsModal route={settingsFor} madeKey={madeKey} server={data?.sftp_server ?? null}
        onClose={() => { setSettingsFor(null); setMadeKey(null); load(); }}
        onSaved={() => { setSettingsFor(null); setMadeKey(null); load(); }} />
    </>
  );
}

// ── one broker's way in ─────────────────────────────────────────────────────
// A card rather than a table row, because the four things people want are of
// different shapes: who it is, what state it is in, what you can do to it, and
// the address — which is the deliverable of this screen and gets its own line.
function RouteCard({ route, busy, cc = null, brokerProgrammes = [], keys, onSettings, onKeyMade, onToggle }: {
  route: IntakeRoute; busy: boolean;
  /** The broker's programmes — named on a route that takes any of them, so
   *  two routes of one broker are told apart by more than the broker's name. */
  brokerProgrammes?: ProgrammeLite[];
  /** Email: the carrier address the broker must copy. */
  cc?: string | null;
  /** undefined until the keys for this route have been fetched. */
  keys: IntakeKey[] | undefined;
  onSettings: () => void; onToggle: () => void;
  /** An API card's Email Instructions made a new key — hand it up to show. */
  onKeyMade: (k: NewIntakeKey) => void;
}) {
  const isApi = route.channel === "api";
  const live = (keys ?? []).filter(k => k.is_live);
  // Only claim "no key" once we have actually looked. Before that the honest
  // answer is that we do not know yet.
  const needsKey = isApi && keys !== undefined && live.length === 0;
  const pull = route.sftp ?? null;
  // On Kavachio's own server: the broker's login — without one, nobody can
  // sign in, which looks like a working channel that receives nothing.
  const hosted = isHosted(route);
  const login = route.login ?? null;
  const needsLogin = hosted && !login?.live;
  // Email is the one channel where the address you HAND a broker and the
  // address they send FROM are different things.
  const handOut = route.channel === "email"
    ? (route.send_to ?? route.display_address) : routeAddress(route);
  const lastUsed = live
    .map(k => k.last_used_at).filter((d): d is string => !!d)
    .sort().pop();

  return (
    <div className={`route${needsKey || needsLogin || pull?.last_error ? " needs" : ""}`}>
      {/* ── who ── */}
      <div className="route-id">
        <div className="idtop">
          <span className="who">{route.broker_name ?? "No broker linked"}</span>
          {/* The programme is what 10.2 made meaningful, and it was invisible
              without opening Settings. A chip, because it is a name from a
              fixed set — italic and unchipped for "Any", which is not a
              programme name, and for an API route means the sender has to name
              one on every file. */}
          {route.program_name
            ? <span className="prog">{route.program_name}</span>
            : brokerProgrammes.length > 0
            ? (<>
                <span className="prog any">All programmes:</span>
                {brokerProgrammes.map(p => <span key={p.program_id} className="prog">{p.name}</span>)}
              </>)
            : <span className="prog any">Any programme</span>}
        </div>

        {/* The deliverable of this screen — people are usually about to paste
            it into an email to a broker — directly under the broker it belongs
            to. A field with its Copy attached, sized to the address rather than
            to a share of the row. */}
        <div className="route-addr">
          <code>{handOut}</code>
          <CopyBtn text={handOut} />
        </div>

        {/* The one extra true thing about this route, if there is one. Nothing
            for a folder of ours: "picked up the moment it lands" is true of
            every one and the group heading already says it. A server we PULL
            from is different — each has its own schedule, and can fail. */}
        {pull && (
          <div className="route-sub">
            checked every {pull.interval_minutes} minutes{" · "}
            {pull.last_checked_at
              ? `last checked ${new Date(pull.last_checked_at).toLocaleString()}`
              : "not checked yet"}
            {pull.last_collected
              ? ` · ${pull.last_collected} file${pull.last_collected === 1 ? "" : "s"} on the last check` : ""}
          </div>)}
        {pull?.last_error && (
          <div className="route-sub warnt">Last check failed: {pull.last_error}</div>)}
        {hosted && (login?.live
          ? <div className="route-sub">
              user <span className="mono">{login.username}</span>{" · "}
              {login.last_login_at
                ? `last signed in ${new Date(login.last_login_at).toLocaleString()}`
                : "not signed in yet"}
            </div>
          : <div className="route-sub warnt">
              No login yet — nobody can sign in to send files. Use Make a login.
            </div>)}
        {route.channel === "email" && (
          <div className="route-sub">
            sends from <span className="mono">{route.address}</span>
            {cc && <> · Cc <span className="mono">{cc}</span> (required)</>}
          </div>)}
        {isApi && (needsKey
          ? <div className="route-sub warnt">Nothing can be sent through this channel until a key exists.</div>
          : keys === undefined
          ? <div className="route-sub">checking keys…</div>
          : <div className="route-sub">
              {live.length} live key{live.length === 1 ? "" : "s"}
              {" · "}<span className="mono">{live[0].key}</span>
              {" · "}{lastUsed ? `last used ${new Date(lastUsed).toLocaleString()}`
                               : "never used"}
            </div>)}
      </div>

      {/* ── the controls ──
          There is no Collect now: a file is taken the moment it lands, so
          there is nothing to go and fetch early. Only Switch off was ever worth
          protecting, and it is protected by its confirm dialog rather than by
          being hard to find. */}
      <div className="route-acts">
        {needsKey && <Badge tone="warn">No key</Badge>}
        {needsLogin && <Badge tone="warn">No login</Badge>}

        {/* The state IS the control. An "On" badge beside a "Switch off" button
            was two controls saying one thing. */}
        <button type="button" className="sw" role="switch" aria-checked={route.is_enabled}
          disabled={busy} onClick={onToggle}
          aria-label={`${route.is_enabled ? "Switch off" : "Switch on"} this channel for ${route.broker_name ?? "this broker"}`}
          title={route.is_enabled
            ? "Switch off — files sent here stop being collected"
            : "Switch on — start collecting from here again"}>
          <span className="track" aria-hidden="true"><span className="knob" /></span>
          <span className="txt">{route.is_enabled ? "On" : "Off"}</span>
        </button>
        {/* Only API routes have anything here. Everything the old Settings
            dialog showed for a folder or a mailbox is already on this row —
            broker, programme, address — apart from a second on/off control
            weaker than the switch beside it, because it did not confirm. Keys
            are the exception: they exist nowhere else. */}
        {/* Keys (API only) and who hears about each file's result (every
            channel) — the two things that exist nowhere else on the row. */}
        <GuideBtn routeId={route.route_id} brokerName={route.broker_name}
          onKeyMade={isApi ? onKeyMade : undefined} />
        <button type="button" className="btn sm" onClick={onSettings}>
          {isApi ? (needsKey ? "Make a key" : "API keys")
            : hosted ? (needsLogin ? "Make a login" : "SFTP login") : "Notifications"}
        </button>
      </div>
    </div>
  );
}

/** True when the instructions really reached the broker's inbox. */
function guideEmailed(g?: GuideSent | null): g is GuideSent {
  return !!g && g.sending && g.recipients.length > 0;
}

/** What happened to the broker's copy of the instructions, in one line. */
function GuideNote({ guide, broker, what = "instructions" }: {
  guide: GuideSent; broker: string | null;
  /** What was emailed: the instructions, or an API key with them. */
  what?: "instructions" | "key";
}) {
  const who = broker ?? "the broker";
  const thing = what === "key" ? "the key and how to use it" : "the instructions";
  const copy = what === "key" ? "copy the key above and send it to them privately"
    : "copy the example below and send it to them";
  if (!guide.recipients.length) {
    return (
      <div className="note warn" style={{ marginTop: 14 }}>
        No email address is on file for {who}, so {thing} were not
        emailed — {copy}.
      </div>);
  }
  if (!guide.sending) {
    return (
      <div className="note warn" style={{ marginTop: 14 }}>
        Broker emails are switched off, so {thing} were not emailed — {copy}.
      </div>);
  }
  return (
    <div className="note ok" style={{ marginTop: 14 }}>
      {what === "key"
        ? <>The key, the endpoint and an example request have been emailed to{" "}
            {guide.recipients.join(", ")}.</>
        : <>The instructions below have been emailed to {guide.recipients.join(", ")}.</>}
    </div>);
}

/** Send the broker this channel's instructions again — for a channel made
 *  before they were emailed, or a broker who lost the email.
 *
 *  API is different: the instructions carry the key, and a key is stored only
 *  as a fingerprint — it cannot be read back. So here the button makes a NEW
 *  key and emails it with the instructions (`onKeyMade`); the keys already
 *  there keep working until they are revoked. */
function GuideBtn({ routeId, brokerName, onKeyMade }: {
  routeId: number; brokerName?: string | null;
  /** Given for an API channel only. */
  onKeyMade?: (k: NewIntakeKey) => void;
}) {
  const [state, setState] = useState<"idle" | "busy" | "sent" | "off" | "none" | "err">("idle");
  const [to, setTo] = useState("");
  const label = { idle: "Email Instructions", busy: "Sending…", sent: "Sent",
                  off: "Emails off", none: "No email on file", err: "Not sent" }[state];
  async function send() {
    if (onKeyMade && !window.confirm(
      `Email ${brokerName ?? "the broker"} their instructions with a NEW API key?\n\n` +
      "A key can't be read back once it is made, so this makes a new one. " +
      "Their current key keeps working until you revoke it under API keys.")) return;
    setState("busy");
    try {
      if (onKeyMade) {
        const k = await createKey(routeId, "Emailed with instructions");
        const g = k.guide;
        setTo(g?.recipients.join(", ") ?? "");
        setState(!g?.recipients.length ? "none" : g.sending ? "sent" : "off");
        onKeyMade(k);
      } else {
        const g = await emailRouteGuide(routeId);
        setTo(g.recipients.join(", "));
        setState(!g.recipients.length ? "none" : g.sending ? "sent" : "off");
      }
    } catch (e: any) {
      setState("err");
      if (onKeyMade) window.alert(e?.response?.data?.detail ?? "Could not make a key.");
    }
  }
  return (
    <button type="button" className="btn sm" disabled={state === "busy"}
      title={state === "sent" ? `Emailed to ${to}`
        : state === "off" ? "Broker emails are switched off (BROKER_NOTIFY_ENABLED)"
        : state === "none" ? "This broker has no email address on file"
        : onKeyMade ? "Make a new API key and email it to the broker with how to send files"
        : "Email the broker how to name and send their files on this channel"}
      onClick={send}>{label}</button>);
}

/** Copy-to-clipboard that says so. The address is usually on its way into an
 *  email to a broker, and one typo is a route that silently never fires. */
function CopyBtn({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="btn sm" onClick={() => {
      navigator.clipboard?.writeText(text);
      setDone(true);
      window.setTimeout(() => setDone(false), 1600);
    }}>{done ? "Copied" : "Copy"}</button>
  );
}

// ── settings ────────────────────────────────────────────────────────────────
// Two live controls, and that is deliberate: everything else about a route is
// fixed behaviour rather than configuration, so showing it as a form would
// imply a choice that does not exist.
// API routes only, and about one thing: the keys. Switching a route on and off
// used to live here too, as a dropdown duplicating the switch on the row —
// and the weaker of the two, because it did not confirm. The row owns it.
//
// Nothing in here is edited any more, so there is nothing to save. "Done"
// closes AND reloads, because minting or revoking a key changes what the row
// says about live keys.
function SettingsModal({ route, madeKey = null, server = null, onClose, onSaved }:
  { route: IntakeRoute | null; madeKey?: NewIntakeKey | null;
    /** Kavachio's own SFTP server, for a channel on it. */
    server?: SftpServerInfo | null;
    onClose: () => void; onSaved: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (route) setErr(null); }, [route]);
  // Where a broker on our server signs in — their own user once they have one.
  const signIn = route?.login ? loginUrl(route.login)
    : server ? `sftp://${server.host}:${server.port}` : route?.display_address ?? "";

  return (
    <Modal open={!!route} size="2xl" onClose={onClose}
      title={route ? `${route.channel === "api" ? "API keys & notifications"
        : isHosted(route) ? "SFTP login & notifications" : "Notifications"} — ${route.broker_name ?? "this broker"}` : ""}
      footer={<div className="proto proto-embed" style={{ display: "flex", gap: 10 }}>
        <button className="btn pri" style={{ marginLeft: "auto" }}
          onClick={onSaved}>Done</button>
      </div>}>
      {route && (
        <div className="proto proto-embed">
          {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
          {route.sftp?.last_error && (
            <div className="note warn" style={{ marginBottom: 14 }}>
              <b>The last check failed.</b> {route.sftp.last_error}
            </div>)}
          {route.channel !== "api" && (
            <div className="note" style={{ marginBottom: 14 }}>
              {route.sftp ? PULL_BLURB
                : CHANNEL_BLURB[route.channel] ?? CHANNEL_COPY[route.channel].sub}
            </div>)}
          <div className="kv"><span className="k">Broker</span><span>{route.broker_name}</span></div>
          <div className="kv">
            <span className="k">Programme</span>
            <span>{route.program_name ?? (
              /* A broker-wide route knows WHO but not WHICH programme. For SFTP
                 that only widens the contract check; for API it means the sender
                 has to name a programme on every file. */
              <span className="muted">Any — this broker's files are not tied to one</span>
            )}</span>
          </div>
          <div className="kv">
            <span className="k">{route.sftp ? "Server" : route.channel === "sftp" ? "Sign in at"
              : route.channel === "email" ? "They send from" : "Address"}</span>
            <span className="mono" style={{ fontSize: 11.5, overflowWrap: "anywhere" }}>
              {isHosted(route) ? signIn : routeAddress(route)}</span></div>
          {/* Email is the one channel where "their address" is two addresses:
              who the mail comes FROM identifies them, and the +address is what
              you actually hand them. Showing only one of the two is what makes
              an email route confusing to support. */}
          {route.channel === "email" && route.send_to && (
            <div className="kv">
              <span className="k">They send to</span>
              <span className="mono" style={{ fontSize: 11.5 }}>{route.send_to}</span></div>)}
          {(route.sftp ? pullDetail(route.sftp) : CHANNEL_DETAIL[route.channel] ?? []).map(([k, v]) => (
            <div className="kv" key={k}><span className="k">{k}</span>
              <span className={k === "Server fingerprint" ? "mono" : undefined}
                style={{ overflowWrap: "anywhere" }}>{v}</span></div>))}
          {route.channel !== "api" && (
            <div className="kv"><span className="k">Received This Month</span>
              <span>{route.files_this_month}</span></div>)}

          {/* Keys are the API route's whole identity mechanism — the equivalent
              of the folder for SFTP — so they belong in this panel, not on a
              separate screen. */}
          {route.channel === "api" && <KeyPanel route={route} initialMinted={madeKey} />}

          {/* The broker's login is an SFTP channel's identity on our server,
              the way a key is an API route's — so it lives here too. */}
          {isHosted(route) && <LoginPanel route={route} server={server} />}

          <NotifyPanel route={route} />

        </div>
      )}
    </Modal>
  );
}

// ── Who hears about each file ───────────────────────────────────────────────
// A broker sending by API, email or SFTP is not in this app, so the result of
// every file — the exceptions, and the secure link to fix them — goes to these
// addresses. Empty: the email address the file came from (email channel) or
// the broker's own login.
function NotifyPanel({ route }: { route: IntakeRoute }) {
  const [text, setText] = useState("");
  const [saved, setSaved] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);

  useEffect(() => {
    setMsg(null);
    getRouteContacts(route.route_id)
      .then((e) => { setSaved(e); setText(e.join(", ")); })
      .catch(() => setSaved([]));
  }, [route.route_id]);

  async function save() {
    setBusy(true); setMsg(null);
    const emails = text.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);
    try {
      const e = await putRouteContacts(route.route_id, emails);
      setSaved(e); setText(e.join(", "));
      setMsg({ tone: "ok", text: e.length ? "Saved." : "Cleared — the broker's own login will be told." });
    } catch (err) {
      const d = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setMsg({ tone: "warn", text: d || "Could not save the addresses." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ marginTop: 18 }}>
      <div className="sub-h">
        Notify These Emails
        <InfoTip text={"Who is told each file's result — exceptions, the secure link to fix them, "
          + "and delivery — on top of the reply on this channel. Leave empty to use the sender's "
          + "address (email channel) or the broker's own login."} />
      </div>
      <input className="inp" style={{ width: "100%" }} value={text} disabled={busy || saved === null}
        placeholder="ops@broker.com, bordereaux@broker.com"
        onChange={(e) => setText(e.target.value)} />
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8 }}>
        <button className="btn sm" disabled={busy || saved === null} onClick={save}>
          {busy ? "Saving…" : "Save Addresses"}</button>
        {msg && <span className={msg.tone === "ok" ? "ok" : "warn"} style={{ fontSize: 12 }}>{msg.text}</span>}
      </div>
    </div>
  );
}

// ── API keys ────────────────────────────────────────────────────────────────
// The key IS the identity: it names the broker and the programme, which is why
// the sender's request carries nothing but the file. We never store the key
// itself, only a fingerprint — so it is shown once and genuinely cannot be
// recovered, and the panel has to say so before it disappears.
function KeyPanel({ route, initialMinted = null }:
  { route: IntakeRoute; initialMinted?: NewIntakeKey | null }) {
  const [keys, setKeys] = useState<IntakeKey[] | null>(null);
  const [minted, setMinted] = useState<NewIntakeKey | null>(initialMinted);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try { setKeys(await listKeys(route.route_id)); setErr(null); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not load keys."); }
  }, [route.route_id]);
  useEffect(() => { load(); }, [load]);

  const live = (keys ?? []).filter(k => k.is_live);

  async function mint() {
    setBusy(true);
    try {
      setMinted(await createKey(route.route_id, label.trim() || undefined));
      setLabel(""); setCopied(false); await load();
    } catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not create the key."); }
    finally { setBusy(false); }
  }

  async function revoke(k: IntakeKey) {
    // Revoking is immediate and cannot be undone — the broker's job stops
    // working the moment it lands, so it is worth one confirmation.
    if (!window.confirm(
      `Revoke ${k.label ? `"${k.label}"` : "this key"}?\n\n` +
      "Anything still sending with it starts being refused straight away. " +
      "Files it already brought in keep their history.")) return;
    setBusy(true);
    try { await revokeKey(k.credential_id); await load(); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not revoke it."); }
    finally { setBusy(false); }
  }

  return (
    <div style={{ marginTop: 18, paddingTop: 16, borderTop: "1px solid var(--p-border)" }}>
      <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Their key</h3>
      <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px", lineHeight: 1.55 }}>
        Use the keys below to send a bordereau to the respective programme and contract.
      </p>

      {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}

      {minted && (
        <div className="note ok" style={{ marginBottom: 12 }}>
          <b>Copy this now — it is not stored and cannot be shown again.</b>
          <div className="keybox" style={{ marginTop: 9 }}>
            <code>{minted.api_key}</code>
            <button type="button" className="btn sm" onClick={() => {
              navigator.clipboard?.writeText(minted.api_key);
              setCopied(true);
            }}>{copied ? "Copied" : "Copy"}</button>
          </div>
          <div style={{ fontSize: 12, marginTop: 8, color: "var(--p-muted)" }}>
            We keep only a fingerprint, so if it is lost the only fix is to revoke it and make
            another.
          </div>
          {minted.guide && <GuideNote guide={minted.guide} broker={route.broker_name} what="key" />}
        </div>
      )}

      {keys === null ? <div className="muted" style={{ fontSize: 12.5 }}>Loading…</div>
        : keys.length === 0 ? (
          <div className="note" style={{ marginBottom: 12 }}>
            No key yet, so nothing can be sent through this channel. Create one and give it to the broker.
          </div>
        ) : (
          <div className="tbl-wrap" style={{ marginBottom: 12 }}>
            <table>
              <thead><tr><th>Key</th><th>Name</th><th>Last used</th><th>Status</th><th></th></tr></thead>
              <tbody>
                {keys.map(k => (
                  <tr key={k.credential_id}>
                    <td className="mono" style={{ fontSize: 11.5 }}>{k.key}</td>
                    <td>{k.label ?? <span className="muted">—</span>}</td>
                    <td className="muted">{k.last_used_at
                      ? new Date(k.last_used_at).toLocaleString()
                      /* Never used is worth seeing: it usually means the broker
                         was never actually given the key. */
                      : "never"}</td>
                    <td>{k.is_live ? <Badge tone="ok">Live</Badge>
                                   : <Badge tone="mut">Revoked</Badge>}</td>
                    <td>{k.is_live && (
                      <button type="button" className="linkbtn crit" disabled={busy}
                        onClick={() => revoke(k)}>Revoke</button>)}</td>
                  </tr>))}
              </tbody>
            </table>
          </div>
        )}

      {/* Two live keys at once, so a broker can move to a new one before the old
          is killed. More than that and "rotate" quietly becomes "accumulate". */}
      {live.length < 2 ? (
        <div className="field" style={{ margin: 0 }}>
          <label>Name this key</label>
          {/* Input and button on one line, with the label above BOTH and the
              hint below both. They used to be flex siblings aligned to
              flex-end, which aligned the button to the bottom of the field —
              and the field's bottom is under its hint, not its input. */}
          <div style={{ display: "flex", gap: 8 }}>
            <input value={label} onChange={e => setLabel(e.target.value)}
              placeholder="e.g. nightly job" style={{ flex: 1, minWidth: 0 }} />
            <button type="button" className="btn pri" onClick={mint} disabled={busy}
              style={{ flex: "0 0 auto" }}>
              {busy ? "Making…" : live.length === 0 ? "Make a key" : "Make a second key"}
            </button>
          </div>
          <div className="hint">Just so you can tell two apart later.</div>
        </div>
      ) : (
        <div className="note">
          Two live keys already. That is the limit — it exists so a broker can switch to a new
          key before the old one is killed. Revoke one to make another.
        </div>
      )}
    </div>
  );
}

// ── the broker's SFTP login ─────────────────────────────────────────────────
// A channel on Kavachio's own server is reached with a login of the broker's
// own. Only a fingerprint of the password is kept, so — like an API key — it
// cannot be shown again: "lost it" is answered with a new one, emailed.

/** What became of a login just made: emailed — or, only when the email cannot
 *  carry it, the password itself, to copy now and pass on privately. */
/** The login just made, as a broker types it into an SFTP client: one row
 *  per field, a Copy button on what gets typed. Never the password — that
 *  went to their inbox only. */
function LoginCard({ login, sentTo }: { login: SftpLogin; sentTo: string[] }) {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (key: string, value: string) => {
    navigator.clipboard?.writeText(value);
    setCopied(key);
    window.setTimeout(() => setCopied(c => (c === key ? null : c)), 1500);
  };
  const rows: [string, string, boolean][] = [
    ["Server", login.host, true],
    ["Port", String(login.port), true],
    ["User name", login.username, true],
    ["Password", sentTo.length ? `Emailed to ${sentTo.join(", ")}` : "Emailed to the broker", false],
    ["Upload to", login.folder ?? "/incoming", false],
    ["Results in", login.results_folder ?? "/outbound", false],
  ];
  return (
    <div style={{ marginTop: 14, textAlign: "left", border: "1px solid var(--p-border)",
                  borderRadius: "var(--p-r)", padding: "6px 14px" }}>
      {rows.map(([k, v, canCopy]) => (
        <div key={k} className="kv" style={{ alignItems: "center", gap: 10 }}>
          <span className="k">{k}</span>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8,
                         minWidth: 0, color: "var(--p-ink)" }}>
            <span className={canCopy || v.startsWith("/") ? "mono" : undefined}
              style={{ overflowWrap: "anywhere", textAlign: "right" }}>{v}</span>
            {canCopy && (
              <button type="button" className="btn sm" style={{ flexShrink: 0 }}
                onClick={() => copy(k, v)}>{copied === k ? "Copied" : "Copy"}</button>)}
          </span>
        </div>))}
    </div>);
}

function LoginMade({ login, guide, broker }:
  { login: SftpLogin; guide?: GuideSent | null; broker: string | null }) {
  const [copied, setCopied] = useState(false);
  const to = delivered(guide);
  if (to.length > 0) {
    const held = guide?.blocked ?? [];
    return (
      <div className="note ok" style={{ marginTop: 12 }}>
        The login has been emailed to {to.join(", ")}.
        {held.length > 0 && <> Not to {held.join(", ")} — test mode holds back mail to them.</>}
      </div>);
  }
  const why = !guide?.recipients.length ? `No email address is on file for ${broker ?? "the broker"}`
    : !guide.sending ? "Broker emails are switched off"
    : `Test mode (MAIL_ALLOWED_RECIPIENTS) holds back mail to ${guide.recipients.join(", ")}`;
  return (
    <div className="note warn" style={{ marginTop: 12 }}>
      <b>{why}, so the login was not emailed. Copy the password now and send it to them
        privately — it is not stored and cannot be shown again.</b>
      <div className="keybox" style={{ marginTop: 9 }}>
        <code>{login.password}</code>
        <button type="button" className="btn sm" onClick={() => {
          navigator.clipboard?.writeText(login.password ?? ""); setCopied(true);
        }}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <div style={{ fontSize: 12, marginTop: 8, color: "var(--p-muted)" }}>
        User name <span className="mono">{login.username}</span> · server{" "}
        <span className="mono">{login.host}</span> · port <span className="mono">{login.port}</span>
      </div>
    </div>);
}

function LoginPanel({ route, server }: { route: IntakeRoute; server: SftpServerInfo | null }) {
  const [login, setLogin] = useState<SftpLogin | null>(route.login ?? null);
  const [made, setMade] = useState<{ login: SftpLogin; guide: GuideSent } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setLogin(route.login ?? null); setMade(null); setErr(null); },
    [route.route_id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function make() {
    // A new password locks the broker's running job out — worth one confirm.
    if (login?.live && !window.confirm(
      `Make ${route.broker_name ?? "the broker"} a new SFTP password?\n\n`
      + "Their current password stops working at once, and anyone signed in with it is "
      + "disconnected. The new one is emailed to them.")) return;
    setBusy(true); setErr(null);
    try { const r = await newSftpLogin(route.route_id); setMade(r); setLogin(r.login); }
    catch (e: any) { setErr(detailText(e, "Could not make the login.")); }
    finally { setBusy(false); }
  }

  const down = !!server && server.status?.mode !== "listening";
  const fp = server?.fingerprints?.[0];
  return (
    <div style={{ marginTop: 18, paddingTop: 16, borderTop: "1px solid var(--p-border)" }}>
      <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Their SFTP login</h3>
      <p className="muted" style={{ fontSize: 12.5, margin: "0 0 12px", lineHeight: 1.55 }}>
        {route.broker_name ?? "The broker"} signs in to Kavachio's SFTP server with this login and
        sees only their own two folders: <span className="mono">/incoming</span> to upload, and{" "}
        <span className="mono">/outbound</span> for our answer to each file.
      </p>
      {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}
      {down && (
        <div className="note warn" style={{ marginBottom: 12 }}>
          Kavachio's SFTP server is not running here
          {server?.status?.error ? ` (${server.status.error})` : ""} — nobody can sign in until it is.
        </div>)}
      {login?.live ? (<>
        <div className="kv"><span className="k">Server</span><span className="mono">{login.host}</span></div>
        <div className="kv"><span className="k">Port</span><span className="mono">{login.port}</span></div>
        <div className="kv"><span className="k">User name</span><span className="mono">{login.username}</span></div>
        <div className="kv"><span className="k">Password</span>
          <span>{made ? "New — see below"
            : `Emailed${login.issued_at ? ` on ${new Date(login.issued_at).toLocaleDateString()}` : ""}`
              + " · never shown again"}</span></div>
        <div className="kv"><span className="k">Last signed in</span>
          <span>{login.last_login_at ? new Date(login.last_login_at).toLocaleString() : "Not yet"}</span></div>
        {fp && (
          <div className="kv"><span className="k">Server fingerprint</span>
            <span className="mono" style={{ overflowWrap: "anywhere", fontSize: 11.5 }}>
              {fp.type} {fp.fingerprint}</span></div>)}
      </>) : (
        <div className="note warn" style={{ marginBottom: 12 }}>
          No login yet, so nobody can sign in to send files on this channel. Make one — it is
          emailed to {route.broker_name ?? "the broker"}.
        </div>)}
      {made && <LoginMade login={made.login} guide={made.guide} broker={route.broker_name} />}
      <div style={{ marginTop: 12 }}>
        <button type="button" className={`btn${login?.live ? "" : " pri"}`} disabled={busy}
          onClick={make}>
          {busy ? "Making…" : login?.live ? "Make a New Password" : "Make a Login"}</button>
      </div>
    </div>);
}

// ── add a way in ────────────────────────────────────────────────────────────
const CODE_ROW: React.CSSProperties = {
  display: "flex", alignItems: "center", gap: 10, padding: "8px 12px",
};
const CODE_LABEL: React.CSSProperties = {
  fontSize: 10.5, fontWeight: 600, letterSpacing: ".4px", textTransform: "uppercase",
  color: "var(--p-faint)",
};
const CODE_NAME: React.CSSProperties = {
  fontSize: 13, color: "var(--p-ink)", overflow: "hidden", textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

/** A programme or contract code, copied with one click. */
function CodeChip({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button type="button" title="Copy code"
      onClick={() => {
        navigator.clipboard?.writeText(code); setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      }}
      style={{
        flex: "none", minWidth: 96, fontFamily: "var(--p-mono)", fontSize: 12, fontWeight: 600,
        color: copied ? "var(--p-ok-ink)" : "var(--p-ink)",
        background: "var(--p-surface)", border: "1px solid var(--p-border-2)",
        borderRadius: 6, padding: "4px 9px", cursor: "pointer", letterSpacing: ".3px",
      }}>
      {copied ? "Copied" : code}
    </button>);
}

/** What one bordereau looks like on its way in — the email to write, or the
 *  file to drop — filled from the choices in the dialog, for last month. */
function ChannelExample({ channel, from, to, cc = null, folder, programmes, shared }: {
  channel: "email" | "sftp"; from: string; to: string;
  /** SFTP: the folder on the server the file is written to. */
  folder: string;
  /** Email only: the carrier address the broker must copy. */
  cc?: string | null;
  /** The programme(s) this address takes; the example uses the first. */
  programmes: ProgrammeLite[];
  /** One address for all the broker's programmes: the file must say which. */
  shared: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [showCodes, setShowCodes] = useState(false);
  const d = new Date(); d.setDate(0);                  // last day of last month
  const month = d.toLocaleString("en-GB", { month: "long", year: "numeric" });
  const ym = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  const slug = (s: string) => s.trim().replace(/\s+/g, "-");
  // The example writes the short codes — nothing to misspell; the names work too.
  const prog = programmes[0];
  const programme = prog?.code ?? prog?.name ?? "Programme";
  const first = prog?.contracts?.[0];
  const contract = first ? (first.code ?? first.name) : null;
  const text = channel === "email"
    ? `From:     ${from || "<their sending address>"}\n`
      + `To:       ${to}\n`
      + `Cc:       ${cc || "<your email>"}\n`
      + `Subject:  ${programme} - ${contract ? `${contract} - ` : ""}${month}\n`
      + `Attach:   the bordereau (.xlsx or .csv)`
    // Just the file name — what Copy hands over. The folder is shown in front
    // of it, quietly, so it is clear where the file goes.
    : `${slug(programme)}_${contract ? `${slug(contract)}_` : ""}${ym}.xlsx`;
  const dir = channel === "sftp" ? `${folder.trim().replace(/\/+$/, "")}/` : "";
  const where = channel === "email" ? "The subject or the file name" : "The file name";
  return (
    <div className="field">
      {/* Named for what it is — the required format, shown as a worked example. */}
      <label>
        {channel === "email" ? "Required Email Format" : "Required File Name Format"}
        <span style={{ fontWeight: 400, color: "var(--p-muted)" }}> — follow this example</span>
      </label>
      <div className="keybox" style={{ alignItems: "flex-start" }}>
        <code style={{ whiteSpace: "pre-wrap", wordBreak: "normal", overflowWrap: "anywhere" }}>
          {dir && <span style={{ color: "var(--p-faint)" }}>{dir}</span>}{text}</code>
        <button type="button" className="btn sm" onClick={() => {
          navigator.clipboard?.writeText(text); setCopied(true);
        }}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <div className="hint">
        {/* One line: what must be in it, and when the contract is needed. */}
        {where} must include the month{shared && programmes.length > 1
          ? " and programme (code or name)" : ""}; the contract only if Required.
        {channel === "email" && <> The Cc is required — an email that does not copy{" "}
          <span className="mono">{cc || "you"}</span> is turned away.</>}
      </div>

      {/* Every code this address takes: one card per programme, its contracts
          beneath it, each code a chip that copies itself. */}
      {programmes.some(p => p.code) && (
        <div style={{ marginTop: 12 }}>
          {/* Folded at first — the example above is enough for most; open it
              for the full list of codes. */}
          <button type="button" className="btn sm" aria-expanded={showCodes}
            onClick={() => setShowCodes(v => !v)}
            style={{ display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
            {showCodes ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            {showCodes ? "Hide Codes" : `View Codes They Can Use (${programmes.length})`}
          </button>
          {showCodes && (
          <div style={{ border: "1px solid var(--p-border-2)", borderRadius: "var(--p-r-sm)",
                        overflow: "hidden" }}>
            {programmes.map((p, i) => {
              const cs = p.contracts ?? [];
              return (
                <div key={p.program_id}
                  style={{ borderTop: i ? "1px solid var(--p-border-2)" : "none" }}>
                  <div style={{ ...CODE_ROW, background: "var(--p-surface-2)" }}>
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div style={CODE_LABEL}>Programme</div>
                      <div style={{ ...CODE_NAME, fontWeight: 600 }}>{p.name}</div>
                    </div>
                    {p.code && <CodeChip code={p.code} />}
                  </div>
                  {cs.map(c => (
                    <div key={c.contract_id} style={{ ...CODE_ROW, paddingLeft: 26 }}>
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={CODE_LABEL}>Contract</div>
                        <div style={CODE_NAME} title={c.name}>{c.name}</div>
                      </div>
                      {cs.length > 1
                        ? <span className="badge b-warn" title="This programme has more than one contract, so the file must say which">
                            Required</span>
                        : <span className="badge b-mut" title="This programme has only one contract, so it can be left out">
                            Optional</span>}
                      {c.code && <CodeChip code={c.code} />}
                    </div>))}
                  {cs.length === 0 && (
                    <div style={{ ...CODE_ROW, paddingLeft: 26, fontSize: 12, color: "var(--p-faint)" }}>
                      No live contract yet
                    </div>)}
                </div>);
            })}
          </div>)}
        </div>)}
    </div>
  );
}

// ── the SFTP server a pull route collects from ─────────────────────────────
type SftpForm = {
  host: string; port: string; username: string; auth: SftpAuth;
  password: string; privateKey: string; passphrase: string;
  remoteDir: string; after: SftpAfter;
  /** null: follow the folder ("<folder>/processed") until somebody types one. */
  processedDir: string | null;
  every: SftpInterval;
};
const SFTP_BLANK: SftpForm = {
  host: "", port: "22", username: "", auth: "password", password: "", privateKey: "",
  passphrase: "", remoteDir: "/outgoing", after: "move", processedDir: null, every: 15,
};

/** A request error in plain words. FastAPI sends a string for a refusal but a
 *  list for a malformed body — rendering the list as-is would crash the page. */
function detailText(e: any, fallback: string): string {
  const d = e?.response?.data?.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d)) {
    const msgs = d.map((x: any) => x?.msg).filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  return fallback;
}

/** A field label, with its explanation behind an ⓘ rather than in grey under it. */
function Lbl({ htmlFor, tip, children }: { htmlFor?: string; tip?: string; children: React.ReactNode }) {
  return tip
    ? <label htmlFor={htmlFor} style={{ display: "inline-flex", alignItems: "center" }}>
        {children}<InfoTip text={tip} /></label>
    : <label htmlFor={htmlFor}>{children}</label>;
}

/** Where Kavachio signs in, how, which folder it takes files from, and what
 *  it does with them after — and the test that has to pass before any of it
 *  is saved. The secret goes one way: typed here, never shown back. */
function SftpServerSection({ sf, put, processedDir, testing, result, canTest, onTest }: {
  sf: SftpForm; put: (patch: Partial<SftpForm>) => void;
  /** The processed folder as it will be sent (the default until edited). */
  processedDir: string;
  testing: boolean;
  /** The last test, only while it still describes what is typed. */
  result: SftpTestResult | null;
  canTest: boolean; onTest: () => void;
}) {
  const keyFile = useRef<HTMLInputElement>(null);
  const [keyNote, setKeyNote] = useState<string | null>(null);

  async function loadKey(f: File | undefined) {
    if (!f) return;
    // A private key is a few KB at most; anything bigger is the wrong file.
    if (f.size > 64 * 1024) { setKeyNote("That file is too large to be a private key."); return; }
    try { put({ privateKey: await f.text() }); setKeyNote(null); }
    catch { setKeyNote("That file could not be read."); }
  }

  const found = result?.files_found ?? 0;
  const warnings = result?.warnings ?? [];
  return (
    <div style={{ border: "1px solid var(--p-border)", borderRadius: "var(--p-r)",
                  padding: "14px 16px 16px", marginBottom: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--p-ink)", marginBottom: 12 }}>
        SFTP Server Connection
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 110px", gap: 16 }}>
        <div className="field">
          <Lbl htmlFor="sftp-host" tip={"The SFTP server Kavachio signs in to — yours or the "
            + "broker's. A name such as sftp.example.com, or an IP address."}>Host</Lbl>
          <input id="sftp-host" value={sf.host} placeholder="sftp.example.com"
            autoComplete="off" spellCheck={false}
            onChange={e => put({ host: e.target.value })} />
        </div>
        <div className="field">
          <Lbl htmlFor="sftp-port">Port</Lbl>
          <input id="sftp-port" inputMode="numeric" value={sf.port}
            onChange={e => put({ port: e.target.value.replace(/\D/g, "").slice(0, 5) })} />
        </div>
      </div>

      <div className="row2">
        <div className="field">
          <Lbl htmlFor="sftp-user">Username</Lbl>
          <input id="sftp-user" value={sf.username} autoComplete="off" spellCheck={false}
            onChange={e => put({ username: e.target.value })} />
        </div>
        <div className="field">
          <Lbl htmlFor="sftp-auth" tip={"The password or key is stored encrypted, and is never "
            + "shown again — not here, and not to anyone else."}>Login Method</Lbl>
          <select id="sftp-auth" value={sf.auth}
            onChange={e => put({ auth: e.target.value as SftpAuth })}>
            <option value="password">Password</option>
            <option value="key">Private Key</option>
          </select>
        </div>
      </div>

      {sf.auth === "password" ? (
        <div className="field">
          <Lbl htmlFor="sftp-pass">Password</Lbl>
          <input id="sftp-pass" type="password" autoComplete="new-password" value={sf.password}
            onChange={e => put({ password: e.target.value })} />
        </div>
      ) : (
        <>
          <div className="field">
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                          gap: 10, marginBottom: 6 }}>
              <label htmlFor="sftp-key" style={{ display: "inline-flex", alignItems: "center", margin: 0 }}>
                Private Key
                <InfoTip text={"Paste the private key, or upload the key file. OpenSSH, RSA, "
                  + "Ed25519 and ECDSA keys work; a PuTTY .ppk key must first be exported in "
                  + "OpenSSH format."} />
              </label>
              <button type="button" className="btn sm" onClick={() => keyFile.current?.click()}>
                Upload Key File</button>
              <input ref={keyFile} type="file" style={{ display: "none" }}
                onChange={e => { loadKey(e.target.files?.[0]); e.target.value = ""; }} />
            </div>
            <textarea id="sftp-key" rows={5} spellCheck={false} autoComplete="off"
              value={sf.privateKey} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              // Masked like a password once there is something in it — a private
              // key is a secret, and the screen may be shared.
              style={{ fontFamily: "var(--p-mono)", fontSize: 11.5, resize: "vertical",
                       ...(sf.privateKey ? { WebkitTextSecurity: "disc" } as React.CSSProperties : {}) }}
              onChange={e => put({ privateKey: e.target.value })} />
            {keyNote && <div className="hint" style={{ color: "var(--p-warn)" }}>{keyNote}</div>}
          </div>
          <div className="field">
            <Lbl htmlFor="sftp-phrase">Key Passphrase (Optional)</Lbl>
            <input id="sftp-phrase" type="password" autoComplete="new-password"
              value={sf.passphrase} onChange={e => put({ passphrase: e.target.value })} />
          </div>
        </>
      )}

      <div className="row2">
        <div className="field">
          <Lbl htmlFor="sftp-dir" tip={"The folder the broker's finished files are written to. "
            + "Kavachio takes only complete files, and skips anything still being written."}>
            Folder to Collect From</Lbl>
          <input id="sftp-dir" value={sf.remoteDir} placeholder="/outgoing" spellCheck={false}
            onChange={e => put({ remoteDir: e.target.value })} />
        </div>
        <div className="field">
          <Lbl htmlFor="sftp-every">Check Every</Lbl>
          <select id="sftp-every" value={sf.every}
            onChange={e => put({ every: Number(e.target.value) as SftpInterval })}>
            {([5, 15, 60] as const).map(m => <option key={m} value={m}>{m} minutes</option>)}
          </select>
        </div>
      </div>

      <div className="row2">
        <div className="field">
          <Lbl htmlFor="sftp-after" tip={"Moving keeps a copy on the server and means a file is "
            + "never collected twice."}>After Collecting</Lbl>
          <select id="sftp-after" value={sf.after}
            onChange={e => put({ after: e.target.value as SftpAfter })}>
            <option value="move">Move to Processed (Recommended)</option>
            <option value="delete">Delete from server</option>
          </select>
        </div>
        {sf.after === "move" && (
          <div className="field">
            <Lbl htmlFor="sftp-processed" tip="Created on the server if it does not exist yet.">
              Processed Folder</Lbl>
            <input id="sftp-processed" value={processedDir} spellCheck={false}
              onChange={e => put({ processedDir: e.target.value })} />
          </div>)}
      </div>

      <div style={{ display: "flex", alignItems: "center" }}>
        <button type="button" className="btn" disabled={!canTest || testing} onClick={onTest}>
          {testing ? "Testing…" : "Test Connection"}</button>
        <InfoTip text={"Kavachio signs in, looks in the folder and records the server's "
          + "fingerprint. Create is available once the test succeeds; changing the server, "
          + "login or folder means testing again."} />
      </div>

      {result && (result.ok ? (
        <>
          <div className="note ok" style={{ marginTop: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 600 }}>
              <Check size={15} strokeWidth={2.5} />
              <span title={result.sample?.length ? result.sample.join("\n") : undefined}>
                Connected · {found} file{found === 1 ? "" : "s"} found</span>
            </div>
            {result.fingerprint ? (
              <div style={{ marginTop: 9 }}>
                <div style={{ display: "flex", alignItems: "center", fontSize: 11.5, fontWeight: 600 }}>
                  Server Fingerprint
                  <InfoTip text={"Confirm this matches your server. Kavachio remembers it, and "
                    + "will refuse to connect if the server's key ever changes."} />
                </div>
                <div className="mono" style={{ fontSize: 11.5, marginTop: 3, color: "var(--p-ink)",
                                               overflowWrap: "anywhere" }}>
                  {result.key_type && <>{result.key_type} </>}{result.fingerprint}
                </div>
              </div>
            ) : (
              <div style={{ marginTop: 6 }}>
                The server did not report its fingerprint, so this channel cannot be saved.
              </div>)}
          </div>
          {/* "We created the folder …" is news, not a problem — shown as
              information; everything else the test reports is a real warning. */}
          {[{ tone: "ok", list: warnings.filter(isInfoNote) },
            { tone: "warn", list: warnings.filter(w => !isInfoNote(w)) }]
            .filter(g => g.list.length > 0).map(g => (
            <div key={g.tone} className={`note ${g.tone}`} style={{ marginTop: 10 }}>
              {g.list.length === 1 ? g.list[0] : (
                <ul style={{ margin: 0, paddingLeft: 18, display: "grid", gap: 3, listStyle: "disc" }}>
                  {g.list.map((w, i) => <li key={i}>{w}</li>)}
                </ul>)}
            </div>))}
        </>
      ) : (
        <div className="note crit" style={{ marginTop: 12 }}>
          {result.error || "Could not connect to the server."}
        </div>
      ))}
    </div>
  );
}

/** Where an SFTP channel's files live: on our server (the broker uploads with
 *  a login we make), or on the broker's own (we sign in and collect).
 *  Since 9 Oct 2026 a new channel can only be on Kavachio's server, so the
 *  broker's-own option is no longer offered. Channels already made that way
 *  keep working (sftp_pull), and the code for them stays. */
type SftpWhere = "kavachio" | "broker";
const HOSTED_SFTP_TIP = "The broker gets a login of their own, emailed to them, and uploads. "
  + "Only they can see their folder. Our answer to each file appears in /outbound.";

/** Kavachio's own SFTP server: nothing to type. The broker's login is made
 *  when the channel is, and emailed — this says where they will sign in. Who
 *  it actually reached is reported after Create, from what the server did:
 *  the broker's contacts are more than the admin logins this dialog knows. */
function HostedSftpSection({ server }: { server: SftpServerInfo | null }) {
  const down = !!server && server.status?.mode !== "listening";
  const right: React.CSSProperties = { textAlign: "right", overflowWrap: "anywhere" };
  return (
    <div style={{ border: "1px solid var(--p-border)", borderRadius: "var(--p-r)",
                  padding: "12px 16px 10px", marginBottom: 16 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--p-ink)", marginBottom: 4 }}>
        Broker's Login
      </div>
      {/* A few words per row — the heading's "i" says what this is. */}
      <div className="kv"><span className="k">Server</span>
        <span className="mono" style={right}>
          {server ? `${server.host} : ${server.port}` : "—"}</span></div>
      <div className="kv"><span className="k">User name</span>
        <span style={right}>Created automatically</span></div>
      <div className="kv"><span className="k">Password</span>
        <span style={right}>Emailed to the broker</span></div>
      <div className="kv"><span className="k">Upload to</span>
        <span className="mono" style={right}>/incoming</span></div>
      <div className="kv"><span className="k">Results in</span>
        <span className="mono" style={right}>/outbound</span></div>
      {down && (
        <div className="note warn" style={{ margin: "8px 0 4px" }}>
          The SFTP server is not running
          {server?.status?.error ? ` (${server.status.error})` : ""}. Brokers can't sign
          in until it is.
        </div>)}
    </div>
  );
}

export function AddRouteModal({ open, brokers, programmesByBroker, emailsByBroker,
                               creatable, mailbox, carrierCc = null,
                        mailReady, sftpServer = null, sftpLoginsByBroker = {},
                        onClose, onCreated }: {
  open: boolean; brokers: { party_id: number; legal_name: string }[];
  programmesByBroker: Record<string, ProgrammeLite[]>;
  /** Addresses already on file per broker, active first. */
  emailsByBroker: Record<string, BrokerEmail[]>;
  creatable: Channel[];
  /** The inbox brokers email, so the +address can be previewed here. */
  mailbox: string | null;
  /** The carrier address brokers must copy (Cc) on every email. */
  carrierCc?: string | null;
  /** False when IMAP is not configured — the route can still be made, but
      nothing will collect from it, and saying so now beats a silent no-op. */
  mailReady: boolean;
  /** Kavachio's own SFTP server — where an SFTP channel's broker signs in. */
  sftpServer?: SftpServerInfo | null;
  /** SFTP logins each broker already has on Kavachio's server, by programme
   *  (null = all their programmes) — so the same one is never made twice. */
  sftpLoginsByBroker?: Record<string, SftpLoginScope[]>;
  onClose: () => void; onCreated: () => void;
}) {
  const [channel, setChannel] = useState<Channel>("sftp");
  // SFTP: Kavachio's server (the broker uploads) unless the carrier says the
  // files are on the broker's own server (Kavachio collects).
  const [sftpWhere, setSftpWhere] = useState<SftpWhere>("kavachio");
  const pullSftp = channel === "sftp" && sftpWhere === "broker";
  const [senderEmail, setSenderEmail] = useState("");
  const [brokerId, setBrokerId] = useState<number | "">("");
  // "any": every programme this broker is on, the file naming which one. A
  // broker has ONE mailbox address and ONE folder, so email and SFTP cannot be
  // split per programme the way API keys can.
  const [programId, setProgramId] = useState<number | "" | "any">("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<IntakeRoute | null>(null);
  const [minted, setMinted] = useState<NewIntakeKey | null>(null);
  const [copied, setCopied] = useState(false);
  const errRef = useRef<HTMLDivElement>(null);

  // SFTP: the server we will collect from, and the last test — kept with
  // exactly what it was run against, so editing the server, login or folder
  // retires it without anyone having to remember to.
  const [sf, setSf] = useState<SftpForm>(SFTP_BLANK);
  const putSf = useCallback((patch: Partial<SftpForm>) => setSf(prev => ({ ...prev, ...patch })), []);
  const [sftpTest, setSftpTest] = useState<{ sig: string; result: SftpTestResult } | null>(null);
  const [testing, setTesting] = useState(false);

  // Keyed on the channel LIST, not the array: the panel behind this dialog
  // re-reads whenever a file lands, and a fresh array used to wipe the form
  // mid-typing.
  useEffect(() => {
    if (open) {
      // Email first: it is the way most brokers already send.
      setChannel(creatable.includes("email") ? "email" : creatable[0] ?? "sftp");
      setSftpWhere("kavachio");
      setBrokerId(""); setProgramId("");
      setSenderEmail("");
      setErr(null); setCreated(null); setMinted(null);
      setCopied(false);
    }
  }, [open, creatable.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  // Opening or closing clears the server details — a typed password or key
  // does not outlive the dialog.
  useEffect(() => { setSf(SFTP_BLANK); setSftpTest(null); setTesting(false); }, [open]);
  useEffect(() => {
    if (err) errRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [err]);

  const progs = programmesByBroker[String(brokerId)] ?? [];
  const knownEmails = emailsByBroker[String(brokerId)] ?? [];
  // Pre-select when there is only one — a choice of one is not a choice.
  //
  // The sending address is filled in the same pass, as a SUGGESTION. What we
  // hold is the broker's portal login; the route needs the mailbox their export
  // job sends as, which is often a service account nobody signs in with.
  // Filling it saves the typing when the two are the same, and the addresses are
  // listed under the field so it is visible WHERE the value came from rather
  // than the box simply appearing full.
  const sharedWayIn = channel === "email" || channel === "sftp";
  // SFTP logins this broker already has (on Kavachio's server). A programme
  // that has one cannot get a second; "all programmes" covers every one, so
  // it is shut once any exists — and nothing is left once it exists itself.
  const hadSftp = channel === "sftp" && !pullSftp
    ? (sftpLoginsByBroker[String(brokerId)] ?? []) : [];
  const sftpForAll = hadSftp.find(x => x.program_id === null) ?? null;
  const sftpTaken = (pid: number) =>
    !!sftpForAll || hadSftp.some(x => x.program_id === pid);
  const anyTaken = hadSftp.length > 0;
  const freeProgs = progs.filter(p => !sftpTaken(p.program_id));
  const sftpFull = channel === "sftp" && !pullSftp && progs.length > 0 && freeProgs.length === 0;
  /** The first choice still open: all programmes, else the first free one. */
  const firstChoice = (): number | "" | "any" =>
    progs.length === 1 ? (sftpTaken(progs[0].program_id) ? "" : progs[0].program_id)
      : progs.length > 1 && sharedWayIn && !anyTaken ? "any"
      : channel === "sftp" ? (freeProgs[0]?.program_id ?? "")
      : "";
  useEffect(() => {
    setProgramId(firstChoice());
    setSenderEmail(knownEmails.length > 0 ? knownEmails[0].email : "");
  }, [brokerId]); // eslint-disable-line react-hooks/exhaustive-deps
  // Switching channel re-decides the default: "all programmes" is offered
  // only where the broker has one address for all of them.
  useEffect(() => {
    if (programId === "any" && !sharedWayIn) setProgramId("");
    else if (programId === "" || (programId === "any" && anyTaken)
             || (typeof programId === "number" && channel === "sftp" && sftpTaken(programId)))
      setProgramId(firstChoice());
  }, [channel]); // eslint-disable-line react-hooks/exhaustive-deps

  const processedDir = sf.processedDir ?? defaultProcessed(sf.remoteDir);
  const sftpSecret = sf.auth === "password" ? sf.password : sf.privateKey;
  const sftpConn: SftpConnection = {
    host: sf.host.trim(), port: Number(sf.port), username: sf.username.trim(), auth: sf.auth,
    ...(sf.auth === "password" ? { password: sf.password }
      : { private_key: sf.privateKey, ...(sf.passphrase ? { passphrase: sf.passphrase } : {}) }),
    remote_dir: sf.remoteDir.trim(), after: sf.after,
    ...(sf.after === "move" ? { processed_dir: processedDir.trim() } : {}),
  };
  // What a test result is only good for: change any of these and it is gone.
  const sftpSig = JSON.stringify([sftpConn.host, sf.port.trim(), sftpConn.username, sf.auth,
    sftpSecret, sf.auth === "key" ? sf.passphrase : "", sftpConn.remote_dir,
    // The test also tries the move, so where files go after collecting counts too.
    sf.after, sf.after === "move" ? processedDir.trim() : ""]);
  const sftpResult = sftpTest && sftpTest.sig === sftpSig ? sftpTest.result : null;
  const portOk = Number.isInteger(sftpConn.port) && sftpConn.port >= 1 && sftpConn.port <= 65535;
  const canTestSftp = !!sftpConn.host && !!sftpConn.username && portOk && !!sftpSecret.trim()
    && !!sftpConn.remote_dir && (sf.after === "delete" || !!processedDir.trim());
  const sftpReady = canTestSftp && !!sftpResult?.ok && !!sftpResult.fingerprint;

  async function runSftpTest() {
    const sig = sftpSig;
    setTesting(true);
    try { setSftpTest({ sig, result: await testSftp(sftpConn) }); }
    catch (e: any) {
      setSftpTest({ sig, result: {
        ok: false, fingerprint: null, key_type: null, files_found: null, sample: [],
        can_write: null, warnings: [], error_code: null,
        error: detailText(e, "The connection could not be tested. Try again in a moment."),
      } });
    } finally { setTesting(false); }
  }

  async function create() {
    if (brokerId === "" || programId === "") return;
    if (pullSftp && !(sftpReady && sftpResult?.fingerprint)) return;
    setSaving(true); setErr(null);
    try {
      // No server settings = a channel on Kavachio's own server: the reply
      // carries the broker's new login, and it is already on its way to them.
      const route = await createRoute({
        channel, broker_party_id: Number(brokerId),
        program_id: programId === "any" ? null : programId,
        ...(channel === "email" ? { sender_email: senderEmail.trim() } : {}),
        ...(pullSftp && sftpResult?.fingerprint ? { sftp: {
          ...sftpConn, after: sf.after, interval_minutes: sf.every,
          fingerprint: sftpResult.fingerprint,
        } } : {}),
      });
      setCreated(route);
      // Saved and encrypted server-side; no reason to keep it in the page.
      if (pullSftp) putSf({ password: "", privateKey: "", passphrase: "" });
      // An API route with no key cannot receive anything, so minting the first
      // one here is part of creating it rather than a second errand.
      if (channel === "api") {
        try { setMinted(await createKey(route.route_id, "first key")); }
        catch { /* the route exists; the key can be made from Settings */ }
      }
    } catch (e: any) { setErr(detailText(e, "Could not create it.")); }
    finally { setSaving(false); }
  }

  // A channel just made on Kavachio's own server, and the login made with it.
  const hostedLogin = created && isHosted(created) ? created.login ?? null : null;
  // Done when there is nothing left for the carrier to pass on: a pull route
  // (Kavachio collects), or the broker's login / instructions / key really
  // reached their inbox. Otherwise the details stay up to be passed on by hand.
  const handedOver = !!created && (created.channel === "sftp"
    ? !!created.sftp || !hostedLogin || delivered(created.guide).length > 0
    : guideEmailed(created.channel === "api" ? minted?.guide : created.guide));

  // The address is the deliverable of this dialog, so it is previewed as the
  // broker is chosen rather than appearing only after the fact.
  const preview = useMemo(() => {
    const b = brokers.find(x => x.party_id === Number(brokerId));
    if (!b) return null;
    // Every API sender posts to the SAME endpoint — what differs between them
    // is the key, not the address. So there is nothing per-broker to preview.
    if (channel === "api") return "POST /v1/bordereaux";
    // Email: Kavachio's intake mailbox, the same for every broker — who sent
    // it and the carrier in Cc say whose file it is.
    if (channel === "email") {
      if (!mailbox || !mailbox.includes("@")) return "no intake mailbox configured yet";
      return mailbox;
    }
    // SFTP: nothing to preview — the server is typed in below, not made up.
    return null;
  }, [brokerId, brokers, channel, mailbox]);

  return (
    <Modal open={open} title="Configure Ingestion Channel" onClose={onClose} size="2xl"
      footer={<div className="proto proto-embed" style={{ display: "flex", gap: 10 }}>
        {created
          ? <button className="btn pri" onClick={onCreated}>Done</button>
          : <>
              <button className="btn" onClick={onClose}>Cancel</button>
              <button className="btn pri" onClick={create}
                title={pullSftp && !sftpReady
                  ? "Test the connection first" : undefined}
                disabled={saving || brokerId === "" ||
                  programId === "" || sftpFull ||
                  (channel === "email" && !senderEmail.includes("@")) ||
                  (pullSftp && !sftpReady)}>
                {saving ? "Creating…" : "Create"}</button>
            </>}
      </div>}>
      <div className="proto proto-embed">
        {created ? (
          // Once the instructions (and, for API, the key) are in the broker's
          // inbox there is nothing for the carrier to pass on — just say it
          // is done. Only when the email could NOT go does the screen fall
          // back to the details, so the carrier can pass them on by hand.
          // SFTP pull: Kavachio collects from a server, so there is nothing to
          // hand over — it is always done. SFTP on our server: done once the
          // login is in their inbox.
          handedOver ? (
          <div style={{ textAlign: "center", padding: "18px 8px 6px" }}>
            <div style={{ width: 52, height: 52, borderRadius: 999, margin: "0 auto 14px",
                          background: "var(--p-ok-bg, #DCFCE7)", color: "var(--p-ok, #16A34A)",
                          display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Check size={26} strokeWidth={2.5} />
            </div>
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--p-ink)" }}>
              Channel configuration complete
            </div>
            <div style={{ fontSize: 13, color: "var(--p-muted)", marginTop: 8, lineHeight: 1.6,
                          overflowWrap: "anywhere" }}>
              {hostedLogin ? (
                // Our server: the broker signs in with the login just emailed.
                <><b style={{ color: "var(--p-ink)" }}>{created.broker_name}</b> can now upload
                  bordereaux to Kavachio's SFTP server.
                  <LoginCard login={hostedLogin} sentTo={delivered(created.guide)} /></>
              ) : created.channel === "sftp" ? (
                // A pull route: the broker sends nothing — Kavachio collects.
                <>Kavachio will collect <b style={{ color: "var(--p-ink)" }}>{created.broker_name}</b>'s
                  {" "}bordereaux from{" "}
                  <span className="mono" style={{ color: "var(--p-ink)" }}>
                    {hostAndFolder(
                      `${created.sftp?.host ?? sf.host.trim()}:${created.sftp?.port ?? sf.port.trim()}`,
                      created.sftp?.remote_dir ?? sf.remoteDir.trim())}</span>{" "}
                  every {created.sftp?.interval_minutes ?? sf.every} minutes.
                  {guideEmailed(created.guide)
                    && <><br />The file-name rules have been emailed to them.</>}</>
              ) : (<>
              <b style={{ color: "var(--p-ink)" }}>{created.broker_name}</b> can now send
              bordereaux by <b style={{ color: "var(--p-ink)" }}>{
                created.channel === "email" ? "Email" : "API"}</b>.
              <br />
              {created.channel === "api"
                ? "Their API key and sending instructions have been emailed to them."
                : "The sending instructions and required format have been emailed to them."}
              </>)}
            </div>
          </div>
          ) : (
          <>
            <div className="note ok" style={{ marginBottom: 14 }}>
              <b>{created.broker_name}</b> now has their own channel
              {created.program_name ? <> on <b>{created.program_name}</b></> : null}.
            </div>
            <div className="drop filled" style={{ padding: "13px 15px", textAlign: "left" }}>
              <span className="mono" style={{ fontSize: 12.5 }}>
                {hostedLogin ? loginUrl(hostedLogin) : created.display_address}</span>
              <div style={{ fontSize: 12, marginTop: 5, color: "var(--p-muted)" }}>
                {created.channel === "api"
                  ? <>The key below identifies {created.broker_name}.</>
                  : created.channel === "email"
                  ? <>The address they send from.</>
                  : hostedLogin
                  ? <>Where they sign in. They upload to <span className="mono">/incoming</span> and
                      find our answer to each file in <span className="mono">/outbound</span>.</>
                  : <>They upload to <span className="mono">/incoming</span>; collected files
                      move to <span className="mono">/processed</span>.</>}
              </div>
            </div>

            {created.channel === "email" && created.send_to && (
              <div className="note ok" style={{ marginTop: 14 }}>
                <b>Tell {created.broker_name} to send to this address:</b>
                <div className="mono" style={{ fontSize: 12.5, marginTop: 7 }}>
                  {created.send_to}</div>
                {(created.cc ?? carrierCc) && (
                  <div style={{ fontSize: 12, marginTop: 8, color: "var(--p-muted)" }}>
                    …and to copy <span className="mono">{created.cc ?? carrierCc}</span> (Cc) on
                    every email — without it the file is turned away.
                  </div>)}
              </div>)}

            {hostedLogin
              ? <LoginMade login={hostedLogin} guide={created.guide} broker={created.broker_name} />
              : created.guide && <GuideNote guide={created.guide} broker={created.broker_name} />}

            {created.channel === "email" && (
              <div style={{ marginTop: 14 }}>
                {(() => {
                  const all = programmesByBroker[String(created.broker_party_id)] ?? [];
                  const mine = all.filter(p => p.program_id === created.program_id);
                  return (
                    <ChannelExample channel={created.channel} from={created.display_address}
                      to={created.send_to ?? ""} cc={created.cc ?? carrierCc}
                      folder={created.display_address}
                      shared={!created.program_id}
                      programmes={created.program_id ? mine : all} />);
                })()}
              </div>)}

            {minted && (
              <div className="note ok" style={{ marginTop: 14 }}>
                <b>Copy this key now — it is not stored and cannot be shown again.</b>
                <div className="keybox" style={{ marginTop: 9 }}>
                  <code>{minted.api_key}</code>
                  <button type="button" className="btn sm" onClick={() => {
                    navigator.clipboard?.writeText(minted.api_key); setCopied(true);
                  }}>{copied ? "Copied" : "Copy"}</button>
                </div>
                <div style={{ fontSize: 12, marginTop: 8, color: "var(--p-muted)" }}>
                  If it is lost, revoke it and create a new one.
                </div>
                {minted.guide && <GuideNote guide={minted.guide} broker={created.broker_name} what="key" />}
                {/* What their system actually sends. `period` is the reporting
                    period the file is for — GET /v1/whoami lists the values
                    this key may use. */}
                {(() => {
                  const d = new Date(); d.setDate(0);     // last day of last month
                  const eg = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
                  const curl = `curl -X POST ${API_BASE}/v1/bordereaux \\\n`
                    + `  -H "X-API-Key: ${minted.api_key}" \\\n`
                    + `  -F "file=@bordereau.xlsx" \\\n`
                    + `  -F "period=${eg}"`;
                  return (<>
                    <b style={{ display: "block", marginTop: 12 }}>Example request</b>
                    <div className="keybox" style={{ marginTop: 6, alignItems: "flex-start" }}>
                      <code style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{curl}</code>
                      <button type="button" className="btn sm"
                        onClick={() => navigator.clipboard?.writeText(curl)}>Copy</button>
                    </div>
                  </>);
                })()}
              </div>
            )}
            {created.channel === "api" && !minted && (
              <div className="note warn" style={{ marginTop: 14 }}>
                The channel was created but its key was not. Create one from this channel's
                {" "}<b>Settings</b> before they can send.
              </div>
            )}
          </>
          )
        ) : (
          <>
            {err && <div ref={errRef} className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
            <div className="field">
              <label>Select Broker</label>
              <select value={brokerId}
                onChange={e => setBrokerId(e.target.value === "" ? "" : Number(e.target.value))}>
                <option value="">Select a broker…</option>
                {brokers.map(b => <option key={b.party_id} value={b.party_id}>{b.legal_name}</option>)}
              </select>
              {brokers.length === 0 && (
                <div className="hint">No broker is on a programme yet.</div>)}
            </div>

            <div className="field">
              <label>Select Channel</label>
              <select value={channel} onChange={e => setChannel(e.target.value as Channel)}>
                {creatable.map(c => (
                  <option key={c} value={c}>{CHANNEL_COPY[c].title}</option>))}
              </select>
              {/* One short line each: how the file travels. */}
              <div className="hint">
                {channel === "api"
                  ? "Their system sends the file to ours directly."
                  : channel === "email"
                  ? "They email the file as an attachment."
                  : pullSftp
                  ? "Kavachio collects the file from the broker's own SFTP server."
                  : "They upload the file to Kavachio's SFTP server, with a login we email them."}
              </div>
              {channel === "email" && !mailReady && (
                <div className="hint" style={{ color: "var(--p-warn)" }}>
                  No intake mailbox is set up yet, so nothing will be collected until it is.
                </div>)}
            </div>

            {channel === "email" && (
              <div className="field">
                <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  Sender Email Address
                  <InfoTip text={"The broker's own address — the one their bordereau emails "
                    + "come from. Only emails sent from it are accepted."} />
                </label>
                <input type="email" value={senderEmail} placeholder="ops@bridgebrokers.com"
                  onChange={e => setSenderEmail(e.target.value)} />
              </div>)}

            <div className="field">
              <label>Select Programme</label>
              <select value={programId} disabled={brokerId === ""}
                onChange={e => setProgramId(e.target.value === "" ? ""
                  : e.target.value === "any" ? "any" : Number(e.target.value))}>
                {/* The blank option is a prompt, not a choice. */}
                <option value="" disabled>Select a programme…</option>
                {sharedWayIn && progs.length > 1 && (
                  <option value="any" disabled={anyTaken}>
                    All of this broker's programmes{anyTaken ? " — already has SFTP" : ""}</option>)}
                {progs.map(p => {
                  const taken = channel === "sftp" && !pullSftp && sftpTaken(p.program_id);
                  return (
                    <option key={p.program_id} value={p.program_id} disabled={taken}>
                      {p.name}{taken ? " — already has SFTP" : ""}</option>);
                })}
              </select>
              {/* Information, not a problem: the broker is already set up. */}
              {sftpFull && (
                <div className="note" style={{ marginTop: 8 }}>
                  {brokers.find(b => b.party_id === brokerId)?.legal_name ?? "This broker"}{" "}
                  already has an SFTP login for {sftpForAll ? "all their programmes"
                    : progs.length === 1 ? "their programme" : "each of their programmes"}
                  {hadSftp.length === 1 && hadSftp[0].username
                    ? <> (user <span className="mono">{hadSftp[0].username}</span>)</> : null}.
                  {" "}To send them a new password, use <b>Make a New Password</b> on that
                  channel under Ingestion Channels.
                </div>)}
              {/* Email only: one sending address can belong to only ONE email
                  channel, so a channel kept to one programme takes every file
                  from that address. An SFTP channel gets a folder and login of
                  its own (since 9 Oct 2026), so a broker can have one per
                  programme — nothing to warn about there. */}
              {channel === "email" && typeof programId === "number" && progs.length > 1 && (
                <div className="hint">
                  Every email from this address will be filed under this programme. To
                  receive files for all {progs.length} of their programmes from it, pick
                  “All of this broker's programmes”.
                </div>)}
              {(brokerId === "" || progs.length === 0) && (
                <div className="hint">
                  {brokerId === "" ? "Select a broker first." : "This broker is not on a programme yet."}
                </div>)}
            </div>

            {channel === "sftp" ? (<>
              {/* One place for the files now — nothing to choose, so no radios:
                  the heading, and what it means behind the "i". */}
              <div style={{ display: "inline-flex", alignItems: "center", gap: 6,
                            fontSize: 12, fontWeight: 600, color: "var(--p-muted)",
                            marginBottom: 8 }}>
                Kavachio's SFTP server
                <InfoTip text={HOSTED_SFTP_TIP} />
              </div>
              {pullSftp
                ? <SftpServerSection sf={sf} put={putSf} processedDir={processedDir}
                    testing={testing} result={sftpResult} canTest={canTestSftp}
                    onTest={runSftpTest} />
                : <HostedSftpSection server={sftpServer} />}
            </>) : (
            <div className="field">
              {/* Email: not the broker's address — Kavachio's intake mailbox,
                  where every broker sends, with the carrier copied. */}
              <label>{channel === "email" ? "Send To (Kavachio Intake Mailbox)"
                : "Broker's Address"}</label>
              <div className="drop filled" style={{ padding: "13px 15px", textAlign: "left" }}>
                <span className="mono" style={{ fontSize: 12.5 }}>
                  {preview ?? "Select a broker to see their address"}</span>
                <div style={{ fontSize: 12, marginTop: 5, color: "var(--p-muted)" }}>
                  {channel === "email" && preview && preview.includes("@") ? (
                    // Three short rules, one per line — easier to scan than a paragraph.
                    <ul style={{ margin: "4px 0 0", paddingLeft: 18, display: "grid", gap: 3, listStyle: "disc",
                                 lineHeight: 1.5 }}>
                      <li><b>To:</b> the broker sends the bordereau to this address.</li>
                      <li><b>Cc:</b> always copy{" "}
                        <span className="mono">{carrierCc ?? "your email"}</span> — without it
                        the email is not accepted.</li>
                      <li><b>Subject or file name:</b> the programme and contract (code or name)
                        decide where the file goes.</li>
                    </ul>
                  ) : "Generated automatically."}
                </div>
              </div>
            </div>
            )}

            {sharedWayIn && brokerId !== "" && programId !== "" && (channel === "sftp" || preview) && (
              <ChannelExample channel={channel as "email" | "sftp"} from={senderEmail.trim()}
                to={preview ?? ""} cc={carrierCc}
                folder={pullSftp ? sf.remoteDir.trim() || "/" : "/incoming"}
                shared={programId === "any"}
                programmes={programId === "any" ? progs : progs.filter(p => p.program_id === programId)} />)}

          </>
        )}
      </div>
    </Modal>
  );
}
