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
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createKey, createRoute, listKeys, listRoutes, patchRoute, revokeKey,
  type BrokerEmail, type BrokerLite, type Channel, type IntakeKey, type IntakeRoute,
  type NewIntakeKey,
  type ProgrammeLite, type RoutesResponse,
} from "../api/intake";
import { Mail, Server, Upload, Zap } from "lucide-react";
import { Modal } from "../components/ui/Modal";

// Copy lives here, not in the API, because it is interface language rather than
// data — the backend has no opinion on what "the old-fashioned way" means.
const CHANNEL_COPY: Record<Channel, { title: string; sub: string; hint: string }> = {
  upload: {
    title: "Someone uploads it", sub: "they sign in and drag the file in",
    hint: "Process Bordereau screen",
  },
  email: {
    title: "They email it", sub: "the file comes in as an attachment",
    hint: "an address you give the broker",
  },
  sftp: {
    title: "They drop it on a server", sub: "the old-fashioned way, and still the most reliable",
    hint: "a folder of their own",
  },
  api: {
    title: "Their system sends it by itself", sub: "no person involved — one computer talking to another",
    hint: "POST /v1/bordereaux",
  },
  cloud_folder: {
    title: "We watch a shared folder", sub: "they save the file where they always have",
    hint: "S3 · SharePoint · Google Drive",
  },
};

// A mark per channel, so four groups that are otherwise four identical grey
// rows can be told apart by shape. The tones deliberately match CAME_IN_BY on
// the Inbox tab: the same way in is the same colour on both tabs, which is what
// makes "Server folder" on a row and "They drop it on a server" here read as
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
    ["How they sign in", "A key, not a password"],
    ["When we pick it up", "The moment it lands — nobody has to press anything"],
    ["After we take it", "Moved into /processed so it cannot be read twice"],
    ["If the file is still being written", "We wait until it stops growing"],
    ["If we do not know the folder", "Kept and shown on Files Received — but there is nobody to tell"],
  ],
  email: [
    ["Where they send it", "The intake mailbox, at their own +address"],
    ["How we know it is them", "The address they were given; the From: line as a fallback"],
    ["When we pick it up", "The moment it reaches the mailbox"],
    ["After we take it", "Filed into /Processed so it cannot be read twice"],
    ["Attachments we ignore", "Signatures, logos and anything not a spreadsheet"],
    ["If we cannot use it", "The sender CAN be told — email is the one way in with a reply path"],
  ],
  api: [
    ["Where they send it", "POST /v1/bordereaux"],
    ["How they identify themselves", "An API key that only they hold"],
    ["When it happens", "The moment they send — nothing is polled"],
    ["What they get back", "A reference, and whether it was accepted, straight away"],
    ["If it fails", "They are told at once in the reply, so they can retry"],
  ],
};

const CHANNEL_BLURB: Partial<Record<Channel, string>> = {
  sftp: "The oldest way of moving a file, and still the most dependable. Their system writes the file into a folder and we pick it up.",
  api: "No person involved. Their software hands the file straight to ours, usually overnight, and gets an answer back immediately.",
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
    mailReady: boolean;
  }) => void;
  /** Bumped by Refresh, and after a route is created. */
  refreshKey?: number;
  /** Bumped when the server says files changed — this month's counts move. */
  liveTick?: number;
}) {
  const [data, setData] = useState<RoutesResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [settingsFor, setSettingsFor] = useState<IntakeRoute | null>(null);
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

  // Collapsed by broker-less channel, not by default: a group you cannot see
  // into is a group you forget exists.
  const [collapsed, setCollapsed] = useState<Set<Channel>>(new Set());

  // Two things count: a way in switched off, and an API route with no key.
  // Both look like nothing is wrong and neither will take a file.
  const needsAttention = useMemo(() => (data?.routes ?? []).filter(r =>
    !r.is_enabled ||
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
      `Switch off this way in for ${route.broker_name ?? "this broker"}?\n\n`
      + `Files sent to ${route.display_address} will stop being collected. `
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
      mailReady: data.email_ready,
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
          <h3>Ways in</h3>
          <span className="sub">grouped by how the file gets here</span>
        </div>

        {builtChannels.map(ch => {
          const routes = byChannel.get(ch) ?? [];
          const open = !collapsed.has(ch);
          const files = routes.reduce((n, r) => n + r.files_this_month, 0);
          const brokers = new Set(routes.map(r => r.broker_party_id)).size;
          return (
            <div className="chan-grp" key={ch}>
              <button type="button" className="chan-hd" aria-expanded={open}
                aria-controls={`grp-${ch}`}
                onClick={() => setCollapsed(prev => {
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
                    : routes.length === 0 ? "nobody sends this way yet"
                    : `${brokers} broker${brokers === 1 ? "" : "s"} · ${files} file${files === 1 ? "" : "s"} this month${pickupNote(ch, data?.collector)}`}
                </span>
              </button>

              {open && (
                <div className="routes" id={`grp-${ch}`}>
                  {ch === "upload" ? (
                    <div className="note">
                      Anyone with a login can drag a file in on the{" "}
                      <b>Process Bordereau</b> screen. It is the fallback for
                      everything else, so it cannot be switched off and there is
                      nothing to configure.
                    </div>
                  ) : routes.length === 0 ? (
                    <div className="note">
                      No broker sends this way yet. Use <b>Add a way in</b> to give
                      one their own address on it.
                    </div>
                  ) : routes.map(r => (
                    <RouteCard key={r.route_id} route={r} busy={busy}
                      keys={keys[r.route_id]}
                      onSettings={() => setSettingsFor(r)}
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

      <SettingsModal route={settingsFor} onClose={() => setSettingsFor(null)}
        onSaved={() => { setSettingsFor(null); load(); }} />
    </>
  );
}

// ── one broker's way in ─────────────────────────────────────────────────────
// A card rather than a table row, because the four things people want are of
// different shapes: who it is, what state it is in, what you can do to it, and
// the address — which is the deliverable of this screen and gets its own line.
function RouteCard({ route, busy, keys, onSettings, onToggle }: {
  route: IntakeRoute; busy: boolean;
  /** undefined until the keys for this route have been fetched. */
  keys: IntakeKey[] | undefined;
  onSettings: () => void; onToggle: () => void;
}) {
  const isApi = route.channel === "api";
  const live = (keys ?? []).filter(k => k.is_live);
  // Only claim "no key" once we have actually looked. Before that the honest
  // answer is that we do not know yet.
  const needsKey = isApi && keys !== undefined && live.length === 0;
  // Email is the one channel where the address you HAND a broker and the
  // address they send FROM are different things.
  const handOut = route.channel === "email"
    ? (route.send_to ?? route.display_address) : route.display_address;
  const lastUsed = live
    .map(k => k.last_used_at).filter((d): d is string => !!d)
    .sort().pop();

  return (
    <div className={`route${needsKey ? " needs" : ""}`}>
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
            for SFTP: "picked up the moment it lands" is true of every server
            folder and the group heading already says it, so repeating it on
            each row is noise, not information. */}
        {route.channel === "email" && (
          <div className="route-sub">
            sends from <span className="mono">{route.address}</span>
          </div>)}
        {isApi && (needsKey
          ? <div className="route-sub warnt">Nothing can be sent this way until a key exists.</div>
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

        {/* The state IS the control. An "On" badge beside a "Switch off" button
            was two controls saying one thing. */}
        <button type="button" className="sw" role="switch" aria-checked={route.is_enabled}
          disabled={busy} onClick={onToggle}
          aria-label={`${route.is_enabled ? "Switch off" : "Switch on"} this way in for ${route.broker_name ?? "this broker"}`}
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
        {isApi && (
          <button type="button" className="btn sm" onClick={onSettings}>
            {needsKey ? "Make a key" : "API keys"}
          </button>)}
      </div>
    </div>
  );
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
function SettingsModal({ route, onClose, onSaved }:
  { route: IntakeRoute | null; onClose: () => void; onSaved: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (route) setErr(null); }, [route]);

  return (
    <Modal open={!!route} size="2xl" onClose={onClose}
      title={route ? `API keys — ${route.broker_name ?? "this broker"}` : ""}
      footer={<div className="proto proto-embed" style={{ display: "flex", gap: 10 }}>
        <button className="btn pri" style={{ marginLeft: "auto" }}
          onClick={onSaved}>Done</button>
      </div>}>
      {route && (
        <div className="proto proto-embed">
          {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
          <div className="note" style={{ marginBottom: 14 }}>
            {CHANNEL_BLURB[route.channel] ?? CHANNEL_COPY[route.channel].sub}
          </div>
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
            <span className="k">{route.channel === "sftp" ? "Folder"
              : route.channel === "email" ? "They send from" : "Address"}</span>
            <span className="mono" style={{ fontSize: 11.5 }}>{route.display_address}</span></div>
          {/* Email is the one channel where "their address" is two addresses:
              who the mail comes FROM identifies them, and the +address is what
              you actually hand them. Showing only one of the two is what makes
              an email route confusing to support. */}
          {route.channel === "email" && route.send_to && (
            <div className="kv">
              <span className="k">They send to</span>
              <span className="mono" style={{ fontSize: 11.5 }}>{route.send_to}</span></div>)}
          {(CHANNEL_DETAIL[route.channel] ?? []).map(([k, v]) => (
            <div className="kv" key={k}><span className="k">{k}</span><span>{v}</span></div>))}
          <div className="kv"><span className="k">Files this month</span>
            <span>{route.files_this_month}</span></div>

          {/* Keys are the API route's whole identity mechanism — the equivalent
              of the folder for SFTP — so they belong in this panel, not on a
              separate screen. */}
          {route.channel === "api" && <KeyPanel route={route} />}

        </div>
      )}
    </Modal>
  );
}

// ── API keys ────────────────────────────────────────────────────────────────
// The key IS the identity: it names the broker and the programme, which is why
// the sender's request carries nothing but the file. We never store the key
// itself, only a fingerprint — so it is shown once and genuinely cannot be
// recovered, and the panel has to say so before it disappears.
function KeyPanel({ route }: { route: IntakeRoute }) {
  const [keys, setKeys] = useState<IntakeKey[] | null>(null);
  const [minted, setMinted] = useState<NewIntakeKey | null>(null);
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
        A folder is what tells us who sent an SFTP file. There is no folder here, so the key
        does that job — it carries the broker and the programme, and the sender sends nothing
        but the file.
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
            Send it to {route.broker_name} over something private. We keep only a fingerprint,
            so if it is lost the only fix is to revoke it and make another.
          </div>
        </div>
      )}

      {keys === null ? <div className="muted" style={{ fontSize: 12.5 }}>Loading…</div>
        : keys.length === 0 ? (
          <div className="note" style={{ marginBottom: 12 }}>
            No key yet, so nothing can send this way. Make one and give it to the broker.
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

// ── add a way in ────────────────────────────────────────────────────────────
export function AddRouteModal({ open, brokers, programmesByBroker, emailsByBroker,
                               creatable, mailbox,
                        mailReady, onClose, onCreated }: {
  open: boolean; brokers: { party_id: number; legal_name: string }[];
  programmesByBroker: Record<string, ProgrammeLite[]>;
  /** Addresses already on file per broker, active first. */
  emailsByBroker: Record<string, BrokerEmail[]>;
  creatable: Channel[];
  /** The inbox brokers email, so the +address can be previewed here. */
  mailbox: string | null;
  /** False when IMAP is not configured — the route can still be made, but
      nothing will collect from it, and saying so now beats a silent no-op. */
  mailReady: boolean;
  onClose: () => void; onCreated: () => void;
}) {
  const [channel, setChannel] = useState<Channel>("sftp");
  const [senderEmail, setSenderEmail] = useState("");
  const [brokerId, setBrokerId] = useState<number | "">("");
  const [programId, setProgramId] = useState<number | "">("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<IntakeRoute | null>(null);
  const [minted, setMinted] = useState<NewIntakeKey | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (open) {
      setChannel(creatable[0] ?? "sftp"); setBrokerId(""); setProgramId("");
      setSenderEmail("");
      setErr(null); setCreated(null); setMinted(null);
      setCopied(false);
    }
  }, [open, creatable]);

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
  useEffect(() => {
    setProgramId(progs.length === 1 ? progs[0].program_id : "");
    setSenderEmail(knownEmails.length > 0 ? knownEmails[0].email : "");
  }, [brokerId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function create() {
    if (brokerId === "" || programId === "") return;
    setSaving(true);
    try {
      const route = await createRoute({
        channel, broker_party_id: Number(brokerId),
        program_id: programId,
        ...(channel === "email" ? { sender_email: senderEmail.trim() } : {}),
      });
      setCreated(route);
      // An API route with no key cannot receive anything, so minting the first
      // one here is part of creating it rather than a second errand.
      if (channel === "api") {
        try { setMinted(await createKey(route.route_id, "first key")); }
        catch { /* the route exists; the key can be made from Settings */ }
      }
    } catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not create it."); }
    finally { setSaving(false); }
  }

  // The address is the deliverable of this dialog, so it is previewed as the
  // broker is chosen rather than appearing only after the fact.
  const preview = useMemo(() => {
    const b = brokers.find(x => x.party_id === Number(brokerId));
    if (!b) return null;
    // Every API sender posts to the SAME endpoint — what differs between them
    // is the key, not the address. So there is nothing per-broker to preview.
    if (channel === "api") return "POST /v1/bordereaux";
    const slug = b.legal_name.normalize("NFKD").replace(/[^\w\s-]/g, "")
      .trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    // Email previews the address the broker is GIVEN — the +address — because
    // that is the deliverable of this dialog. What they send FROM is typed in
    // above and is not something we generate.
    if (channel === "email") {
      if (!mailbox || !mailbox.includes("@")) return "no intake mailbox configured yet";
      const [local, domain] = mailbox.split("@");
      return `${local}+${slug}@${domain}`;
    }
    return `sftp://…/${slug}`;
  }, [brokerId, brokers, channel, mailbox]);

  return (
    <Modal open={open} title="Give a broker their own way in" onClose={onClose} size="2xl"
      footer={<div className="proto proto-embed" style={{ display: "flex", gap: 10 }}>
        {created
          ? <button className="btn pri" onClick={onCreated}>Done</button>
          : <>
              <button className="btn" onClick={onClose}>Cancel</button>
              <button className="btn pri" onClick={create}
                disabled={saving || brokerId === "" ||
                  programId === "" ||
                  (channel === "email" && !senderEmail.includes("@"))}>
                {saving ? "Creating…" : "Create it"}</button>
            </>}
      </div>}>
      <div className="proto proto-embed">
        {created ? (
          <>
            <div className="note ok" style={{ marginBottom: 14 }}>
              <b>{created.broker_name}</b> now has their own way in
              {created.program_name ? <> on <b>{created.program_name}</b></> : null}.
            </div>
            <div className="drop filled" style={{ padding: "13px 15px", textAlign: "left" }}>
              <span className="mono" style={{ fontSize: 12.5 }}>{created.display_address}</span>
              <div style={{ fontSize: 12, marginTop: 5, color: "var(--p-muted)" }}>
                {created.channel === "api"
                  ? <>Every sender posts to this same address. What tells us it is{" "}
                      {created.broker_name} is the key below.</>
                  : created.channel === "email"
                  ? <>This is the address they send FROM, and it is what identifies them.</>
                  : <>They write into <span className="mono">/incoming</span>. Once we take a
                      file it moves to <span className="mono">/processed</span>, so it can never
                      be read twice.</>}
              </div>
            </div>

            {created.channel === "email" && created.send_to && (
              <div className="note ok" style={{ marginTop: 14 }}>
                <b>Tell {created.broker_name} to send to this address:</b>
                <div className="mono" style={{ fontSize: 12.5, marginTop: 7 }}>
                  {created.send_to}</div>
                <div style={{ fontSize: 12, marginTop: 8, color: "var(--p-muted)" }}>
                  The tag after the <span className="mono">+</span> is what makes this
                  {" "}<b>their</b> address rather than just the inbox — mail sent to it can
                  only have come from someone who was told it. If their mail server strips the
                  tag, the From: address above still identifies them.
                </div>
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
                  Send it to {created.broker_name} over something private. We keep only a
                  fingerprint, so if it is lost the only fix is to revoke it and make another.
                </div>
              </div>
            )}
            {created.channel === "api" && !minted && (
              <div className="note warn" style={{ marginTop: 14 }}>
                The way in was made but its key was not. Open <b>Settings</b> on this route and
                make one — nothing can be sent until there is a key.
              </div>
            )}
          </>
        ) : (
          <>
            {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
            <div className="note" style={{ marginBottom: 14 }}>
              There are only four ways a file can reach you and you cannot invent a fifth. What
              this does is give <b>one broker their own address</b> on one of them, so you never
              have to work out who sent what.
            </div>

            <div className="field">
              <label>Which broker?</label>
              <select value={brokerId}
                onChange={e => setBrokerId(e.target.value === "" ? "" : Number(e.target.value))}>
                <option value="">Select a broker…</option>
                {brokers.map(b => <option key={b.party_id} value={b.party_id}>{b.legal_name}</option>)}
              </select>
              {brokers.length === 0 && (
                <div className="hint">No broker is on one of your programmes yet. A broker with
                  no programme has nothing for their files to belong to.</div>)}
            </div>

            <div className="field">
              <label>Which way in?</label>
              <select value={channel} onChange={e => setChannel(e.target.value as Channel)}>
                {creatable.map(c => (
                  <option key={c} value={c}>{CHANNEL_COPY[c].title}</option>))}
              </select>
              <div className="hint">
                {channel === "api"
                  ? "Their software sends the file straight to ours and gets an answer back at once. Nobody logs in."
                  : channel === "email"
                  ? "They attach the spreadsheet to an email, the way most brokers already do. We take the attachments off the moment the email arrives."
                  : "Their system writes the file into a folder of their own and we pick it up the moment it lands."}
              </div>
              {channel === "email" && !mailReady && (
                <div className="hint" style={{ color: "var(--p-warn)" }}>
                  No intake mailbox is configured yet, so nothing will be collected from this
                  route until IMAP_HOST, IMAP_USER and IMAP_PASS are set. The route can be
                  made now — it just will not do anything.
                </div>)}
            </div>

            {channel === "email" && (
              <div className="field">
                <label>Which address do they send from?</label>
                <input type="email" value={senderEmail} placeholder="ops@bridgebrokers.com"
                  onChange={e => setSenderEmail(e.target.value)} />
                {/* Where the filled-in value came from, and the other addresses
                    we hold. Shown rather than silently prefilled: these are
                    portal logins, and a login that is not the sending mailbox
                    produces a route that matches nothing — with the field
                    looking perfectly filled in. */}
                {knownEmails.length > 0 && (
                  <div className="hint" style={{ marginTop: 6 }}>
                    {knownEmails.length === 1 ? "Their login" : "Their logins"}:{" "}
                    {knownEmails.map((k, i) => (
                      <span key={k.email}>
                        {i > 0 && " · "}
                        <button type="button" className="linkbtn"
                          onClick={() => setSenderEmail(k.email)}>{k.email}</button>
                        {k.status !== "active" && (
                          <span className="muted"> ({k.status})</span>)}
                      </span>))}
                  </div>)}
                <div className="hint">
                  Use the <b>From:</b> address on their bordereau emails
                  {knownEmails.length > 0 ? " — change it if that isn't their login." : "."}
                </div>
              </div>)}

            <div className="field">
              <label>Which programme?</label>
              <select value={programId} disabled={brokerId === ""}
                onChange={e => setProgramId(e.target.value === "" ? "" : Number(e.target.value))}>
                {/* Every way in is pinned to one programme, so the blank option is
                    a prompt, not a choice. */}
                <option value="">Select a programme…</option>
                {progs.map(p => (
                  <option key={p.program_id} value={p.program_id}>{p.name}</option>))}
              </select>
              <div className="hint">
                {brokerId === "" ? "Pick a broker first."
                  : progs.length === 0 ? "This broker is not on a programme yet."
                  : programId === "" ? "Pick the programme their files are for."
                  : channel === "api"
                  ? "Their files go to this programme and nothing else. They send only the file — no programme, no broker, nothing to get wrong."
                  : "Their files are checked against this programme's contract."}
              </div>
            </div>

            <div className="field">
              <label>The address they will use</label>
              <div className="drop filled" style={{ padding: "13px 15px", textAlign: "left" }}>
                <span className="mono" style={{ fontSize: 12.5 }}>
                  {preview ?? "pick a broker to see their address"}</span>
                <div style={{ fontSize: 12, marginTop: 5, color: "var(--p-muted)" }}>
                  Made for you. It has to match the folder we look in exactly, and one typo is a
                  broker whose files are silently never picked up.
                </div>
              </div>
            </div>

          </>
        )}
      </div>
    </Modal>
  );
}
