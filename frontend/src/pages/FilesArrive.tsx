// Feature 10 — "How Files Arrive".
//
// A broker should not have to log in to send you a file. Most already email
// their spreadsheet or drop it on a server, and asking them to change that is
// usually what stalls a new programme. So there are five ways in, and whichever
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
import { useNavigate } from "react-router-dom";
import {
  createKey, createRoute, listKeys, listRoutes, patchRoute, pollRoute, revokeKey,
  type Channel, type FileStyle, type IntakeKey, type IntakeRoute, type NewIntakeKey,
  type PollResult, type ProgrammeLite, type RoutesResponse,
} from "../api/intake";
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
const CHANNEL_ORDER: Channel[] = ["upload", "email", "sftp", "api", "cloud_folder"];

// What a server folder actually does. Fixed behaviour, not settings — which is
// why it reads as a description and not as a form.
// What each way in actually DOES. Fixed behaviour, not settings — which is why
// it reads as a description and not as a form. Keyed by channel so the Settings
// dialog describes the route in front of you rather than always describing SFTP.
const CHANNEL_DETAIL: Partial<Record<Channel, [string, string][]>> = {
  sftp: [
    ["How they sign in", "A key, not a password"],
    ["How often we look", "Every 5 minutes"],
    ["After we take it", "Moved into /processed so it cannot be read twice"],
    ["If the file is still being written", "We wait until it stops growing"],
    ["If we do not know the folder", "Kept and shown on Files Received — but there is nobody to tell"],
  ],
  email: [
    ["Where they send it", "The intake mailbox, at their own +address"],
    ["How we know it is them", "The address they were given; the From: line as a fallback"],
    ["How often we look", "Every 5 minutes"],
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

const SFTP_DETAIL: [string, string][] = [
  ["How they sign in", "A key, not a password"],
  ["How often we look", "Every 5 minutes"],
  ["After we take it", "Moved into /processed so it cannot be read twice"],
  ["If the file is still being written", "We wait until it stops growing"],
  ["If we do not know the folder", "Kept and shown on Files Received — but there is nobody to tell"],
];

// What a mailbox does, as fixed behaviour rather than settings — the same shape
// as SFTP_DETAIL, because the question people ask is the same one.
const EMAIL_DETAIL: [string, string][] = [
  ["How often we look", "Every 5 minutes"],
  ["What we take", "Attachments only — signatures and logos are ignored"],
  ["One email, several files", "Each attachment is recorded on its own"],
  ["After we read it", "Filed into /Processed so it cannot be read twice"],
  ["Mail with nothing attached", "Ignored entirely, not recorded as a refusal"],
  ["If we cannot use it", "The sender can be told — this is the only way in with a reply path"],
];

const LADDER: [string, string][] = [
  ["Try their usual way in", "Whatever they normally use."],
  ["Try the server folder", "If the mailbox is down, the same file is picked up from the folder if they put one there."],
  ["Ask a person", "Nothing worked, so the broker is told and someone is asked to upload it by hand. The file is never quietly dropped."],
];

// The six checks every file passes, whichever way it came in. Shown because the
// most common support question is "why was my file refused?", and the answer is
// always one of these.
const CHECKS: [string, string, "away" | "held"][] = [
  ["Is it a spreadsheet at all?", "A PDF or a photo of a spreadsheet cannot be read.", "away"],
  ["Can we open it?", "Half-uploaded and password-protected files look fine until you try.", "away"],
  ["Do we know who sent it?", "Every file has to belong to a broker on one of your programmes.", "away"],
  ["Is it the same file we already have?", "Brokers often send twice. Loading it twice would double your premium.", "held"],
  ["Does it have any rows in it?", "An empty file usually means an export that silently failed.", "held"],
  ["Is there a live contract to check it against?", "There is nothing to check a file against until the contract is agreed.", "held"],
];

function Badge({ tone, children }:
  { tone: "ok" | "warn" | "crit" | "mut"; children: React.ReactNode }) {
  return <span className={`badge b-${tone}`}><span className="d" />{children}</span>;
}

/** A note bar flush inside a card, the way the wireframe closes its tables. */
function CardNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="note" style={{
      margin: 0, border: 0, borderTop: "1px solid var(--p-border)", borderRadius: 0,
    }}>{children}</div>
  );
}

export default function FilesArrive() {
  const nav = useNavigate();
  const [data, setData] = useState<RoutesResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [settingsFor, setSettingsFor] = useState<IntakeRoute | null>(null);
  const [adding, setAdding] = useState(false);
  const [poll, setPoll] = useState<PollResult | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setData(await listRoutes()); setErr(null); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? e?.message ?? "Failed to load."); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // One row per channel, with its routes hung underneath. A channel with no
  // routes still renders — it is one of the five whether or not it is used, and
  // that is what lets email and API light up a row later rather than needing a
  // redesign.
  const byChannel = useMemo(() => {
    const map = new Map<Channel, IntakeRoute[]>();
    for (const c of CHANNEL_ORDER) map.set(c, []);
    for (const r of data?.routes ?? []) map.get(r.channel)?.push(r);
    return map;
  }, [data]);

  const sftpRoutes = byChannel.get("sftp") ?? [];
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

  const brokersSetUp = useMemo(
    () => new Set((data?.routes ?? [])
      .map(r => r.broker_party_id).filter((b): b is number => b != null)).size,
    [data]);

  // Two things count: a way in switched off, and an API route with no key.
  // Both look like nothing is wrong and neither will take a file.
  const needsAttention = useMemo(() => (data?.routes ?? []).filter(r =>
    !r.is_enabled ||
    (r.channel === "api" && keys[r.route_id] !== undefined &&
     keys[r.route_id].every(k => !k.is_live))).length, [data, keys]);

  const heldOrAway = (data?.tiles.held ?? 0) + (data?.tiles.turned_away ?? 0);

  // Upload always shows — it is real and needs no setup. Everything else shows
  // if it can be created or already has routes; the rest is honestly unbuilt.
  const creatable = data?.creatable ?? [];
  const builtChannels = CHANNEL_ORDER.filter(c =>
    c === "upload" || creatable.includes(c) || (byChannel.get(c) ?? []).length > 0);
  const unbuiltChannels = CHANNEL_ORDER.filter(c => !builtChannels.includes(c));

  async function toggle(route: IntakeRoute) {
    setBusy(true);
    try { await patchRoute(route.route_id, { is_enabled: !route.is_enabled }); await load(); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not change it."); }
    finally { setBusy(false); }
  }

  async function collectNow(route: IntakeRoute) {
    setBusy(true);
    try { setPoll(await pollRoute(route.route_id)); await load(); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not collect."); }
    finally { setBusy(false); }
  }

  const t = data?.tiles;

  return (
    <div className="proto">
      <section className="view full">
        <div className="note" style={{ marginBottom: 18 }}>
          <b>A broker does not have to log in to send you a file.</b> Most brokers already
          email their spreadsheet or drop it on a server, and asking them to change that is
          usually the thing that stalls a new programme. So Kavachio gives you five ways in.
          Whichever one a file uses, it lands in the same queue and gets the same checks.{" "}
          <b>You do this once, when a broker is onboarded</b>, and then rarely again — which is
          why it sits under Configure rather than in the monthly run. The files that come in
          this way are on <span className="linkish" onClick={() => nav("/intake/arrivals")}>
            Files Received</span>.
        </div>

        <div className="page-head">
          <div className="t">
            <h2>How Files Arrive <Badge tone="mut">Set up once</Badge></h2>
            <p>Where your brokers send their spreadsheets. Set up once when a broker is
              onboarded, then rarely touched.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/intake/arrivals")}>Files received →</button>
            <button className="btn pri" onClick={() => setAdding(true)}>＋ Add a way in</button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>}

        <div className="tiles" style={{ marginBottom: 18 }}>
          {/* Brokers, not doors. "Ways in switched on — 3 of 5" counted doors
              that do not exist yet, so a perfectly healthy setup read as a job
              two-fifths done. How many brokers can send you a file is the
              question people actually ask. */}
          <div className="tile">
            <div className="k">Brokers set up</div>
            <div className="v">{data ? brokersSetUp : "—"}</div>
            <div className="foot">
              {brokersSetUp === 0 ? "nobody can send yet" : "can send you files"}</div>
          </div>
          <div className="tile">
            <div className="k">Files this month</div>
            <div className="v">{t?.files_this_month ?? "—"}</div>
            <div className="foot">across every way in</div>
          </div>
          {/* Replaces "Most used way in", which answered no question anyone
              asks. This is the only tile that ever asks you to do something:
              a way in that looks live and accepts nothing. */}
          <div className={`tile${needsAttention > 0 ? " warnl" : ""}`}>
            <div className="k">Needs attention</div>
            <div className="v" style={needsAttention > 0 ? { color: "var(--p-warn)" } : undefined}>
              {data ? needsAttention : "—"}</div>
            <div className="foot">
              {!data ? "\u00a0"
                : needsAttention === 0 ? "every way in is working"
                : needsAttention === 1 ? "one way in accepts nothing"
                : `${needsAttention} ways in accept nothing`}</div>
          </div>
          <div className={`tile${heldOrAway > 0 ? " alert" : ""}`}>
            <div className="k">Held or turned away</div>
            <div className="v" style={heldOrAway > 0 ? { color: "var(--p-crit)" } : undefined}>
              {t ? heldOrAway : "—"} <small>this month</small></div>
            <div className="foot">
              {heldOrAway === 0 ? "nothing refused yet"
                : <span className="linkish" onClick={() => nav("/intake/arrivals")}>See why →</span>}
            </div>
          </div>
        </div>

        {/* One row per CHANNEL crammed every broker into stacked cells — three
            addresses in one cell, three names in the next — and you matched
            them by vertical position, so adding a broker shifted everything
            below it. A channel is a group now, and each broker is a card you
            read across. */}
        <div className="card" style={{ marginBottom: 18 }}>
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
                  <span className="ttl">{CHANNEL_COPY[ch].title}</span>
                  <span className="meta">
                    {ch === "upload"
                      ? "always on · anyone with a login · nothing to set up"
                      : routes.length === 0 ? "nobody sends this way yet"
                      : `${brokers} broker${brokers === 1 ? "" : "s"} · ${files} file${files === 1 ? "" : "s"} this month`}
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
                        onToggle={() => !busy && toggle(r)}
                        onCollect={() => !busy && collectNow(r)} />
                    ))}
                  </div>
                )}
              </div>
            );
          })}

          {/* Two table rows reading "Not set up" implied you had forgotten to
              configure them, when in fact they do not exist yet. One honest
              line instead. */}
          {unbuiltChannels.length > 0 && (
            <div className="unbuilt">
              <span className="ttl">Not built yet</span>
              <span>{unbuiltChannels.map(c => CHANNEL_COPY[c].title).join("  ·  ")}</span>
            </div>
          )}

          <CardNote>
            <b>The way in never changes what happens next.</b> A spreadsheet that arrives by
            email and the same spreadsheet sent by machine end up in exactly the same place,
            checked in exactly the same way. It only changes how the file got here.
          </CardNote>
          <CardNote>
            <b>So what does “Add a way in” do, if there are only five?</b> It does not invent a
            sixth. It gives one broker <b>their own address</b> on one of the five, so Kavachio
            never has to work out who sent what.
          </CardNote>
        </div>

        <div className="grid g-2" style={{ marginBottom: 18 }}>
          <div className="card pad">
            <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>Server folders in detail</h3>
            <p className="muted" style={{ fontSize: 12.5, margin: "0 0 14px", lineHeight: 1.55 }}>
              Each broker gets a folder of their own. Their system writes the file into it and
              we pick it up — nobody logs in, and the folder is what tells us who sent it.
            </p>
            {sftpRoutes.map(r => (
              <div className="kv" key={r.route_id}>
                <span className="k">{r.broker_name}</span>
                <span className="mono" style={{ fontSize: 11.5 }}>{r.display_address}</span>
              </div>))}
            {SFTP_DETAIL.map(([k, v]) => (
              <div className="kv" key={k}><span className="k">{k}</span><span>{v}</span></div>))}
            {sftpRoutes.length === 0 && (
              <div className="note" style={{ marginTop: 14 }}>
                No broker has a folder yet. Use <b>Add a way in</b> — the address is generated
                for you, so it always matches the folder we look in.
              </div>)}
          </div>

          <div className="card pad">
            <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>If a way in stops working</h3>
            <p className="muted" style={{ fontSize: 12.5, margin: "0 0 14px", lineHeight: 1.55 }}>
              When a broker's usual way in stops working, Kavachio tries the next one down
              rather than losing the file.
            </p>
            {LADDER.map(([name, what], i) => (
              <div key={name} style={{ display: "flex", gap: 12, marginBottom: 12 }}>
                <div style={{
                  flex: "0 0 auto", width: 22, height: 22, borderRadius: "50%",
                  background: "var(--p-surface-3)", color: "var(--p-muted)",
                  display: "grid", placeItems: "center", fontSize: 11, fontWeight: 700,
                }}>{i + 1}</div>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{name}</div>
                  <div style={{ fontSize: 12, color: "var(--p-muted)", lineHeight: 1.5 }}>{what}</div>
                </div>
              </div>))}
            <div className="note">
              Only routes we <b>look in</b> can fall back. A broker whose system pushes to us
              has already chosen their way in, so there is nothing to try next.
            </div>
          </div>
        </div>

        {emailRoutes.length > 0 && (
          <div className="card pad" style={{ marginBottom: 18 }}>
            <h3 style={{ margin: "0 0 4px", fontSize: 14 }}>The mailbox in detail</h3>
            <p className="muted" style={{ fontSize: 12.5, margin: "0 0 14px", lineHeight: 1.55 }}>
              Every broker emails the same inbox — so unlike a folder, the inbox cannot tell
              one from another. Who the mail comes from is what does. Each broker is also given
              a <span className="mono">+address</span> of their own, which is stronger: it can
              only have come from someone who was told it.
            </p>
            <div className="kv">
              <span className="k">The inbox we read</span>
              <span className="mono" style={{ fontSize: 11.5 }}>
                {data?.email_mailbox ?? <span className="muted">not configured yet</span>}</span>
            </div>
            {emailRoutes.map(r => (
              <div className="kv" key={r.route_id}>
                <span className="k">{r.broker_name}</span>
                <span className="mono" style={{ fontSize: 11.5 }}>
                  {r.send_to ?? r.address}
                  <div className="sub">sends from {r.address}</div>
                </span>
              </div>))}
            {EMAIL_DETAIL.map(([k, v]) => (
              <div className="kv" key={k}><span className="k">{k}</span><span>{v}</span></div>))}
            {!data?.email_ready && (
              <div className="note warn" style={{ marginTop: 14 }}>
                <b>No mailbox is configured, so nothing is being collected.</b> These routes
                exist and will start working the moment <span className="mono">IMAP_HOST</span>,
                {" "}<span className="mono">IMAP_USER</span> and
                {" "}<span className="mono">IMAP_PASS</span> are set — nothing here has to be
                made again.
              </div>)}
            <CardNote>
              <b>Email is the one way in that can answer back.</b> A folder has nobody to tell
              and an upload is over before you know anything is wrong — but a refused email can
              be replied to, so the broker finds out from us rather than from a chase at
              month-end. It is off until switched on, because an auto-reply answering another
              auto-reply is how a domain ends up blocked.
            </CardNote>
          </div>)}

        <div className="card">
          <div className="card-h">
            <h3>Checks every file passes before anything else happens</h3>
            <span className="sub">these run in the first second, whichever way the file came in</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead><tr><th>What we check</th><th>Why it matters</th><th>If it fails</th></tr></thead>
              <tbody>
                {CHECKS.map(([what, why, fail]) => (
                  <tr key={what}>
                    <td><b>{what}</b></td>
                    <td className="l">{why}</td>
                    <td>{fail === "away"
                      ? <Badge tone="crit">Turned away</Badge>
                      : <Badge tone="warn">Held, someone decides</Badge>}</td>
                  </tr>))}
              </tbody>
            </table>
          </div>
          <CardNote>
            <b>Better to catch it in the first second than an hour in.</b> Everything these
            catch would otherwise be found halfway through processing, when the broker has gone
            home and the numbers are already half loaded.
          </CardNote>
        </div>
      </section>

      <SettingsModal route={settingsFor} onClose={() => setSettingsFor(null)}
        onSaved={() => { setSettingsFor(null); load(); }} />
      <AddRouteModal open={adding} brokers={data?.brokers ?? []}
        programmesByBroker={data?.broker_programmes ?? {}}
        creatable={data?.creatable ?? ["sftp"]}
        mailbox={data?.email_mailbox ?? null}
        mailReady={data?.email_ready ?? false}
        onClose={() => setAdding(false)}
        onCreated={() => { setAdding(false); load(); }} />
      <PollResultModal result={poll} onClose={() => setPoll(null)} />
    </div>
  );
}

// ── one broker's way in ─────────────────────────────────────────────────────
// A card rather than a table row, because the four things people want are of
// different shapes: who it is, what state it is in, what you can do to it, and
// the address — which is the deliverable of this screen and gets its own line.
function RouteCard({ route, busy, keys, onSettings, onToggle, onCollect }: {
  route: IntakeRoute; busy: boolean;
  /** undefined until the keys for this route have been fetched. */
  keys: IntakeKey[] | undefined;
  onSettings: () => void; onToggle: () => void; onCollect: () => void;
}) {
  const isApi = route.channel === "api";
  const live = (keys ?? []).filter(k => k.is_live);
  // Only claim "no key" once we have actually looked. Before that the honest
  // answer is that we do not know yet.
  const needsKey = isApi && keys !== undefined && live.length === 0;
  // Only a channel we PULL from can be collected early. An API route is pushed
  // to, so there is nothing to go and fetch.
  const canCollect = route.collecting && route.is_enabled;
  // Email is the one channel where the address you HAND a broker and the
  // address they send FROM are different things.
  const handOut = route.channel === "email"
    ? (route.send_to ?? route.display_address) : route.display_address;
  const lastUsed = live
    .map(k => k.last_used_at).filter((d): d is string => !!d)
    .sort().pop();

  return (
    <div className={`route${needsKey ? " needs" : ""}`}>
      <div className="route-top">
        <span className="who">{route.broker_name ?? "No broker linked"}</span>
        {/* The programme is what 10.2 made meaningful, and it was invisible
            without opening Settings. Italic grey for "Any" because it is not a
            programme name — and for an API route it means the sender has to
            name one on every file. */}
        {route.program_name
          ? <span className="prog">{route.program_name}</span>
          : <span className="prog any">Any programme</span>}
        <span className="rt">
          <span className="route-cnt" title="files this month">{route.files_this_month}</span>
          {needsKey ? <Badge tone="warn">No key</Badge>
            : route.is_enabled ? <Badge tone="ok">On</Badge>
            : <Badge tone="mut">Off</Badge>}
          <button type="button" className="linkbtn" onClick={onSettings}>
            {needsKey ? "Make a key" : "Settings"}
          </button>
          <span className="sep" aria-hidden="true">·</span>
          <button type="button" className="linkbtn mut" onClick={onToggle} disabled={busy}>
            {route.is_enabled ? "Switch off" : "Switch on"}
          </button>
          {canCollect && <>
            <span className="sep" aria-hidden="true">·</span>
            <button type="button" className="linkbtn mut" onClick={onCollect} disabled={busy}>
              Collect now
            </button>
          </>}
        </span>
      </div>

      <div className="route-addr">
        <code>{handOut}</code>
        <CopyBtn text={handOut} />
      </div>

      {route.channel === "email" && (
        <div className="route-sub">
          they send from <span className="mono">{route.address}</span>
        </div>)}

      {isApi && (needsKey ? (
        <div className="route-sub" style={{ color: "var(--p-warn-ink)" }}>
          Nothing can be sent this way until a key exists.
        </div>
      ) : keys === undefined ? (
        <div className="route-sub">checking keys…</div>
      ) : (
        <div className="route-sub">
          {live.length} live key{live.length === 1 ? "" : "s"}
          {" · "}<span className="mono">{live[0].key}</span>
          {" · "}{lastUsed ? `last used ${new Date(lastUsed).toLocaleString()}`
                           : "never used"}
        </div>
      ))}
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
function SettingsModal({ route, onClose, onSaved }:
  { route: IntakeRoute | null; onClose: () => void; onSaved: () => void }) {
  const [style, setStyle] = useState<FileStyle>("whole_book");
  const [enabled, setEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (route) { setStyle(route.file_style); setEnabled(route.is_enabled); setErr(null); }
  }, [route]);

  async function save() {
    if (!route) return;
    setSaving(true);
    try { await patchRoute(route.route_id, { file_style: style, is_enabled: enabled }); onSaved(); }
    catch (e: any) { setErr(e?.response?.data?.detail ?? "Could not save."); }
    finally { setSaving(false); }
  }

  return (
    <Modal open={!!route} title={route ? CHANNEL_COPY[route.channel].title : ""}
      onClose={onClose} size="2xl"
      footer={<div className="proto proto-embed" style={{ display: "flex", gap: 10 }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn pri" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save changes"}</button>
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

          <div className="field" style={{ margin: "14px 0 0" }}>
            <label>What do they send each month?</label>
            <select value={style} onChange={e => setStyle(e.target.value as FileStyle)}>
              <option value="whole_book">The whole book so far — everything since the start, again each time</option>
              <option value="changes_only">Only what is new or changed since their last file</option>
            </select>
            <div className="hint">Either is fine. Rows already loaded are recognised and never
              counted twice — the choice just tells the checks what a normal file looks like.</div>
          </div>

          <div className="field" style={{ margin: "14px 0 0" }}>
            <label>Is this way in switched on?</label>
            <select value={enabled ? "on" : "off"} onChange={e => setEnabled(e.target.value === "on")}>
              <option value="on">On — files arriving this way are accepted</option>
              <option value="off">Off — anything sent this way is turned away with a note</option>
            </select>
            <div className="hint">Switching off is not deleting. Files that already came in
              through it keep their history; only new ones are refused.</div>
          </div>
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
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
          <div className="field" style={{ margin: 0, flex: 1 }}>
            <label>Name this key</label>
            <input value={label} onChange={e => setLabel(e.target.value)}
              placeholder="e.g. nightly job" />
            <div className="hint">Just so you can tell two apart later.</div>
          </div>
          <button type="button" className="btn pri" onClick={mint} disabled={busy}>
            {busy ? "Making…" : live.length === 0 ? "Make a key" : "Make a second key"}
          </button>
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
function AddRouteModal({ open, brokers, programmesByBroker, creatable, mailbox,
                        mailReady, onClose, onCreated }: {
  open: boolean; brokers: { party_id: number; legal_name: string }[];
  programmesByBroker: Record<string, ProgrammeLite[]>;
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
  const [style, setStyle] = useState<FileStyle>("whole_book");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<IntakeRoute | null>(null);
  const [minted, setMinted] = useState<NewIntakeKey | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (open) {
      setChannel(creatable[0] ?? "sftp"); setBrokerId(""); setProgramId("");
      setSenderEmail("");
      setStyle("whole_book"); setErr(null); setCreated(null); setMinted(null);
      setCopied(false);
    }
  }, [open, creatable]);

  const progs = programmesByBroker[String(brokerId)] ?? [];
  // Pre-select when there is only one — a choice of one is not a choice, and
  // leaving it blank would silently create a broker-wide route.
  useEffect(() => {
    setProgramId(progs.length === 1 ? progs[0].program_id : "");
  }, [brokerId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function create() {
    if (brokerId === "") return;
    setSaving(true);
    try {
      const route = await createRoute({
        channel, broker_party_id: Number(brokerId), file_style: style,
        program_id: programId === "" ? null : Number(programId),
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
              There are only five ways a file can reach you and you cannot invent a sixth. What
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
                  ? "They attach the spreadsheet to an email, the way most brokers already do. We read the mailbox every five minutes and take the attachments off."
                  : "Their system writes the file into a folder of their own and we pick it up every five minutes."}
                {" "}Shared folders are not built yet.
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
                <div className="hint">
                  Every broker emails the same inbox, so the inbox cannot tell one from
                  another — <b>who the mail comes from is what does</b>. This is the only
                  thing on this screen you have to type, and it has to be the address their
                  system really sends as, not the one a person replies from.
                </div>
              </div>)}

            <div className="field">
              <label>Which programme?</label>
              <select value={programId} disabled={brokerId === ""}
                onChange={e => setProgramId(e.target.value === "" ? "" : Number(e.target.value))}>
                <option value="">
                  {channel === "api"
                    ? "Any — they must name one on every file"
                    : "Any programme this broker is on"}
                </option>
                {progs.map(p => (
                  <option key={p.program_id} value={p.program_id}>{p.name}</option>))}
              </select>
              <div className="hint">
                {brokerId === "" ? "Pick a broker first."
                  : progs.length === 0 ? "This broker is not on a programme yet."
                  : channel === "api" ? (
                    programId === ""
                      ? "Leave it as Any and every file has to say which programme it is for — one typo in their script and a bordereau is filed against the wrong programme. Naming it here is safer: one way in per programme, and the sender supplies nothing but the file."
                      : "Their files go to this programme and nothing else. They send only the file — no programme, no broker, nothing to get wrong.")
                  : "Naming a programme narrows the contract check to that programme's contract."}
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

            <div className="field" style={{ marginBottom: 0 }}>
              <label>What do they send each month?</label>
              <select value={style} onChange={e => setStyle(e.target.value as FileStyle)}>
                <option value="whole_book">The whole book so far</option>
                <option value="changes_only">Only what is new or changed</option>
              </select>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

// ── "collect now" result ────────────────────────────────────────────────────
// Exists so "is my folder working?" is answered on the screen rather than in a
// log file, and so the feature can be shown without waiting five minutes.
function PollResultModal({ result, onClose }:
  { result: PollResult | null; onClose: () => void }) {
  return (
    <Modal open={!!result} title="Collected now" onClose={onClose} size="3xl"
      footer={<div className="proto proto-embed">
        <button className="btn pri" onClick={onClose}>Close</button></div>}>
      {result && (() => {
        // A mailbox sweep and a folder sweep report different things; the
        // presence of a message count is what tells them apart.
        const isMail = result.messages !== undefined;
        return (
        <div className="proto proto-embed">
          <div className="kv"><span className="k">{isMail ? "Mailbox" : "Folder"}</span>
            <span className="mono" style={{ fontSize: 11.5 }}>{result.looked_in}</span></div>
          {isMail && <div className="kv"><span className="k">Messages read</span>
            <span>{result.messages ?? 0}</span></div>}
          <div className="kv"><span className="k">Taken</span><span>{result.accepted}</span></div>
          {isMail && <div className="kv"><span className="k">Held</span>
            <span>{result.held ?? 0}</span></div>}
          <div className="kv"><span className="k">Turned away</span><span>{result.turned_away}</span></div>
          {isMail ? (
            <>
              {/* Mail with nothing attached is not a refusal and is not an
                  arrival — it is just mail. Counted so the number of messages
                  read still adds up, which is the first thing anyone checks. */}
              <div className="kv"><span className="k">Nothing attached</span>
                <span>{result.no_attachment ?? 0}</span></div>
              {!!result.already_seen && <div className="kv">
                <span className="k">Already had them</span>
                <span>{result.already_seen}</span></div>}
              {!!result.too_large && <div className="kv"><span className="k">Too big</span>
                <span>{result.too_large}</span></div>}
              {!!result.unattributable && <div className="kv">
                <span className="k">Left in the mailbox</span>
                <span>{result.unattributable}</span></div>}
            </>
          ) : (
            <div className="kv"><span className="k">Still being written</span>
              <span>{result.skipped_still_writing ?? 0}</span></div>
          )}

          {result.error && <div className="note warn" style={{ marginTop: 14 }}>{result.error}</div>}
          {result.skipped && <div className="note" style={{ marginTop: 14 }}>{result.skipped}</div>}

          {result.files.length === 0
            ? <div className="note" style={{ marginTop: 14 }}>
                {isMail ? "No new mail with anything attached." : "Nothing new in the folder."}
              </div>
            : <div className="tbl-wrap" style={{ marginTop: 14 }}>
                <table>
                  <thead><tr><th>File</th>{isMail && <th>From</th>}
                    <th>What happened</th><th>Why</th></tr></thead>
                  <tbody>
                    {result.files.map(f => (
                      <tr key={f.arrival_id}>
                        <td className="mono" style={{ fontSize: 11.5 }}>{f.filename}</td>
                        {isMail && <td>{f.from ?? <span className="muted">—</span>}
                          {f.matched && <div className="sub">{f.matched}</div>}</td>}
                        <td>{f.outcome === "accepted" ? <Badge tone="ok">Taken</Badge>
                          : f.outcome === "held" || f.reason?.startsWith("Held —")
                          ? <Badge tone="warn">Held</Badge>
                          : <Badge tone="crit">Turned away</Badge>}</td>
                        <td className="l">{f.reason?.replace(/^Held — /, "") ?? ""}</td>
                      </tr>))}
                  </tbody>
                </table>
              </div>}
          <div className="note" style={{ marginTop: 14 }}>
            {isMail
              ? <>One mailbox serves every email route, so this read <b>all</b> new mail, not
                  only this broker's. Each message is filed away once it has been read, so
                  nothing is counted twice.</>
              : <>A file still being written is left where it is and picked up next time. It is
                  not an error — taking it early would load half a bordereau.</>}
          </div>
        </div>
        );
      })()}
    </Modal>
  );
}
