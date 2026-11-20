import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Activity, Calendar, ChevronDown, ChevronUp, Download, Eye, Paperclip, RotateCcw, Search, ShieldCheck, User, Users, X,
} from "lucide-react";
import {
  downloadAuditLogs, downloadCorrectionEvidence, getAuditEvent, getAuditGroup, getAuditLogs, getAuditOptions,
  type AuditEvent, type AuditOptions, type AuditQuery, type AuditRow,
} from "../api/audit";
import { downloadErrorText } from "../api/client";
import { useServerList } from "../hooks/useServerList";
import { Pagination } from "../components/Pagination";
import { fmtDateTime } from "../utils/date";
import { InfoTip } from "../components/InfoTip";
import { getHistoryChoices, getTrailSummary, type Choice, type TrailSummary } from "../api/auditHistory";
import { userRole } from "../auth";
import { AuditsTab } from "./AuditManagement";
import { PackageModal } from "./AuditPackage";
import { IntegrityRetention } from "./IntegrityRetention";
import { Facts, PartnerMonitoring, SidePanel, Tile, tiles } from "./AuditHistoryTabs";
import { AuditRecordFilter } from "../components/AuditRecordFilter";

// 20 a page (every Audit Logs table pages by 20); the page controls under the
// table move between pages.
const PAGE_SIZE = 20;

/** What this seat's trail actually covers — said once, at the top, because the
 *  answer is different for every role and a table of rows cannot say it. */
const SUBTITLE: Record<string, string> = {
  platform:
    "Everything that happens on Kavachio — every carrier, every broker and every person.",
  carrier_admin:
    "Everyone at your company, and every broker you work with — each person by name, with their broker company. Steps Kavachio takes on its own (status emails, reminders) show as Kavachio · Automatic.",
  carrier_user:
    "Your own trail, and everything the brokers you work with have done — each person by name. Steps Kavachio takes on its own show as Kavachio · Automatic.",
  broker_admin:
    "Your whole team — who sent which file, and who cleared which exception.",
  broker_user:
    "Your own trail: what you sent, what you opened and what you put right.",
};

const TONE_CLASS: Record<string, string> = {
  ok: "b-ok", warn: "b-warn", bad: "b-crit", info: "b-info", muted: "b-mut",
};

/** A local calendar day as "YYYY-MM-DD" — never toISOString(), which is UTC and
 *  would name yesterday for anyone east of Greenwich after midnight. */
function localDay(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return localDay(d);
}

const RANGES = [
  { key: "7", label: "Last 7 days" },
  { key: "30", label: "Last 30 days" },
  { key: "90", label: "Last 3 months" },
  { key: "365", label: "Last 12 months" },
  { key: "all", label: "All time" },
  { key: "custom", label: "Custom date" },
];

function rangeDates(key: string, from: string, to: string): { from?: string; to?: string } {
  if (key === "custom") return { from: from || undefined, to: to || undefined };
  if (key === "all") return {};
  return { from: daysAgo(Number(key)), to: localDay(new Date()) };
}

type Module = NonNullable<AuditOptions["modules"]>[number];

/** The ⓘ beside Area: each Area with its Sub-areas, in the server's words. */
function areaHelp(modules: Module[]): string {
  return ["Each Area is a part of the menu; Sub-area narrows it to one part.",
          ...modules.map(m => `• ${m.label}: ${m.subs.map(x => x.label).join(", ")}.`)].join("\n");
}

/** The ⓘ beside Sub-area: what each part of the chosen Area holds. */
function subHelp(m: Module): string {
  return [`The parts of ${m.label}.`, ...m.subs.map(x => `• ${x.label}: ${x.info ?? ""}`)].join("\n");
}

/** "Asha - Wani org (Broker)"; a whole company is "Wani org - all people (Broker)". */
function personLabel(a: { label: string; role: string; org?: string | null; side?: string | null; everyone?: boolean }): string {
  if (!a.side) return `${a.label} — ${a.role}`;
  if (a.everyone) return `${a.org || a.label} - all people (${a.side})`;
  return a.org && a.org !== a.label ? `${a.label} - ${a.org} (${a.side})` : `${a.label} (${a.side})`;
}

/** Carrier people first, then each broker company with its own people right
 *  under it ("Wani org - all people", then Asha, Ravi…), Kavachio last. */
function sortedPeople<T extends { label: string; org?: string | null; side?: string | null; everyone?: boolean }>(xs: T[]): T[] {
  const side = (a: T) => ({ Carrier: 0, Broker: 1 } as Record<string, number>)[a.side ?? ""] ?? 2;
  const key = (a: T) => [side(a), (a.org ?? "").toLowerCase(), a.everyone ? 0 : 1, a.label.toLowerCase()] as const;
  return [...xs].sort((a, b) => {
    const x = key(a), y = key(b);
    for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
    return 0;
  });
}

/** Screens that open from a link (finding emails, the dashboard's audit
 *  findings card) and are not part of the list. */
const PAGES = {
  audits: { title: "Audits", sub: "Record audits of contracts and brokers, and follow each finding until it is closed." },
  partners: { title: "Partner Monitoring", sub: "How each broker is doing: files, lateness, open findings and risk." },
  integrity: { title: "Integrity & Retention",
               sub: "Proof that audit records were never changed, how long they are kept, and legal holds." },
} as const;
type PageKey = keyof typeof PAGES;
const CARRIER_ONLY = new Set<string>(["audits", "partners", "integrity"]);

/** The tabs this list replaced: an old link (?tab=rules) opens the list with
 *  the filter that answers the same question. */
const OLD_TABS: Record<string, { area?: string; verb?: string }> = {
  rules: { area: "rules" }, changes: { verb: "changed" }, access: { area: "m_access" },
  submissions: { area: "files" }, contracts: { area: "contracts" }, treaties: { area: "treaties" },
  versions: { area: "files" },
};

/**
 * Audit Logs — who did what, when, and how it ended. One list for every seat.
 *
 * The filters and the columns are the same everywhere; what changes is WHOSE
 * rows the server sends back and what it is willing to call them
 * (audit_feed.py, audit_one.py). The old tabs are filters now: Area, Action,
 * Programme and Status, and the Old / New value columns.
 */
export default function AuditLogs() {
  const [search, setSearch] = useSearchParams();
  const isBroker = (userRole() ?? "").startsWith("broker");
  const tab = search.get("tab") ?? "";
  if (tab in PAGES && !(isBroker && CARRIER_ONLY.has(tab))) return <LinkedPage which={tab as PageKey} />;
  return <OneList search={search} setSearch={setSearch} />;
}

/** Audits, Partner Monitoring and Integrity & Retention, opened from a link. */
function LinkedPage({ which }: { which: PageKey }) {
  const p = PAGES[which];
  return (
    <div className="proto">
      <div className="view full">
        <Link to="/audit" className="linkish" style={{ display: "inline-block", marginBottom: 10 }}>← Audit Logs</Link>
        <div className="page-head">
          <div className="t"><h2>{p.title}</h2><p>{p.sub}</p></div>
        </div>
        {which === "integrity" ? <IntegrityRetention /> : which === "audits" ? <AuditsTab /> : <PartnerMonitoring />}
      </div>
    </div>
  );
}

type Defaults = { range: string; area: string; verb: string; actor: string; programme: string; status: string };
const START: Defaults = { range: "30", area: "all", verb: "", actor: "", programme: "", status: "" };

function OneList({ search, setSearch }: {
  search: URLSearchParams; setSearch: ReturnType<typeof useSearchParams>[1];
}) {
  // An old tab link, or a notice opening the list on one Area ("?action=contracts").
  const old = OLD_TABS[search.get("tab") ?? ""] ?? {};
  const [range, setRange] = useState(START.range);
  const [from, setFrom] = useState(daysAgo(30));
  const [to, setTo] = useState(localDay(new Date()));
  // Area is a menu module ("m_contracts"); Sub-area one part of it ("signing").
  // A link may name either; a Sub-area finds its Area once the lists arrive.
  const linked = old.area ?? search.get("action") ?? "";
  const [area, setArea] = useState(linked.startsWith("m_") ? linked : START.area);
  const [sub, setSub] = useState(linked && !linked.startsWith("m_") ? linked : "");
  const [verb, setVerb] = useState(old.verb ?? START.verb);
  const [actor, setActor] = useState(START.actor);
  const [programme, setProgramme] = useState(START.programme);
  const [status, setStatus] = useState(START.status);
  const [record, setRecord] = useState(search.get("record") ?? "");
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(true);
  const [packing, setPacking] = useState(false);
  const [opened, setOpened] = useState<AuditRow | null>(null);
  const [sum, setSum] = useState<TrailSummary | null>(null);
  const [evidenceError, setEvidenceError] = useState("");
  const [opts, setOpts] = useState<AuditOptions | null>(null);
  const [programmes, setProgrammes] = useState<Choice[]>([]);
  const [busy, setBusy] = useState<"" | "csv" | "xlsx">("");
  const [failed, setFailed] = useState(false);
  const [openGroup, setOpenGroup] = useState<AuditRow | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => { getAuditOptions().then(setOpts).catch(() => setOpts(null)); }, []);
  useEffect(() => { getHistoryChoices().then(r => setProgrammes(r.programmes)).catch(() => setProgrammes([])); }, []);
  // The old tab is now a filter; drop it from the address so a reload keeps the list.
  useEffect(() => {
    if (search.get("tab") || search.get("action")) {
      const next = new URLSearchParams(search);
      next.delete("tab"); next.delete("action"); next.delete("view");
      setSearch(next, { replace: true });
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const query: AuditQuery = useMemo(
    () => ({ ...rangeDates(range, from, to), actor, action: sub || area, verb, q, record,
             programme: programme ? Number(programme) : null, status }),
    [range, from, to, actor, area, sub, verb, q, record, programme, status],
  );

  useEffect(() => {
    setSum(null);
    getTrailSummary(query).then(setSum).catch(() => setSum(null));
  }, [JSON.stringify(query)]); // eslint-disable-line react-hooks/exhaustive-deps

  const list = useServerList<AuditRow, { seat?: string }>(
    (page, size) => getAuditLogs(query, page, size)
      .then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify(query),
    PAGE_SIZE,
  );

  // A new page starts at its top row, not wherever the last one was scrolled to.
  useEffect(() => { scroller.current?.scrollTo({ top: 0 }); }, [list.page, JSON.stringify(query)]);

  const seat = list.extra?.seat ?? "";
  const showCarrier = seat === "platform";

  const nameOf = (items: { key: string; label: string }[] | undefined, key: string) =>
    items?.find(x => x.key === key)?.label ?? key;
  // With an Area chosen, Action and Status list only what happens in that Area.
  const module = opts?.modules?.find(m => m.key === area);
  const subLabel = module?.subs.find(x => x.key === sub)?.label
    ?? opts?.modules?.flatMap(m => m.subs).find(x => x.key === sub)?.label ?? sub;
  const scope = sub || (area !== "all" ? area : "");
  const actionChoices = (scope ? opts?.area_actions?.[scope] : undefined) ?? opts?.verbs ?? [];
  const statusChoices = (scope ? opts?.area_statuses?.[scope] : undefined) ?? opts?.statuses ?? [];
  // The Action list is the same everywhere: keep the Action and Status picked
  // if the new Area or Sub-area has them.
  const keepChoices = (nextScope: string) => {
    const acts = nextScope ? opts?.area_actions?.[nextScope] : opts?.verbs;
    if (verb && !acts?.some(x => x.key === verb)) setVerb("");
    const keep = nextScope ? opts?.area_statuses?.[nextScope] : opts?.statuses;
    if (status && !keep?.some(x => x.key === status)) setStatus("");
  };
  const pickArea = (next: string) => {
    setArea(next); setSub("");
    keepChoices(next !== "all" ? next : "");
  };
  const pickSub = (next: string) => {
    setSub(next);
    keepChoices(next || (area !== "all" ? area : ""));
  };
  // A link that named a Sub-area: show its Area too.
  useEffect(() => {
    if (sub && area === "all") {
      const m = opts?.modules?.find(x => x.subs.some(y => y.key === sub));
      if (m) setArea(m.key);
    }
  }, [opts]); // eslint-disable-line react-hooks/exhaustive-deps
  const rangeLabel = RANGES.find(r => r.key === range)?.label ?? "";
  // What is narrowing the list, each with its own ✕ (the "Showing:" line).
  const chips: { label: string; clear: () => void }[] = [
    ...(range !== START.range ? [{ label: `Date: ${range === "custom" ? `${from} – ${to}` : rangeLabel}`,
                                   clear: () => setRange(START.range) }] : []),
    ...(area !== "all" ? [{ label: `Area: ${module?.label ?? area}`, clear: () => pickArea("all") }] : []),
    ...(sub ? [{ label: `Sub-area: ${subLabel}`, clear: () => pickSub("") }] : []),
    ...(verb ? [{ label: `Action: ${nameOf(actionChoices, verb)}`, clear: () => setVerb("") }] : []),
    ...(actor ? [{ label: `Person: ${(() => { const a = opts?.actors.find(x => x.key === actor); return a ? personLabel(a) : actor; })()}`,
                   clear: () => setActor("") }] : []),
    ...(programme ? [{ label: `Programme: ${programmes.find(p => String(p.id) === programme)?.name ?? programme}`,
                       clear: () => setProgramme("") }] : []),
    ...(status ? [{ label: `Status: ${nameOf(statusChoices, status)}`, clear: () => setStatus("") }] : []),
    ...(record ? [{ label: `${({ contract: "Contract #", policy: "Policy: ", file: "File: " } as Record<string, string>)[record.split(":")[0]] ?? "Record: "}${record.slice(record.indexOf(":") + 1)}`,
                    clear: () => setRecord("") }] : []),
    ...(q ? [{ label: `Search: “${q}”`, clear: () => { setQ(""); setTyped(""); } }] : []),
  ];

  const reset = () => {
    setRange(START.range); setFrom(daysAgo(30)); setTo(localDay(new Date()));
    setArea(START.area); setSub(""); setVerb(START.verb); setActor(START.actor);
    setProgramme(START.programme); setStatus(START.status); setRecord(""); setTyped(""); setQ("");
  };

  // The audit package takes the filters it can use: dates, programme, broker
  // (a broker company or one broker's person) and policy. All time is the last
  // 24 months, the package's own longest default.
  const packageAsk = () => {
    const d = rangeDates(range, from, to);
    const a = opts?.actors.find(x => x.key === actor);
    const brokerId = actor.startsWith("broker:") ? Number(actor.slice(7)) : undefined;
    const policy = record.startsWith("policy:") ? record.slice(7) : undefined;
    const ask = {
      from: d.from ?? (range === "all" ? daysAgo(730) : undefined), to: d.to ?? localDay(new Date()),
      programme: programme ? Number(programme) : ("" as const), broker: brokerId ?? ("" as const),
      ...(policy ? { policy } : {}),
      ...(range === "all" ? { dates: "Last 24 months" }
        : range !== "custom" ? { dates: RANGES.find(r => r.key === range)?.label } : {}),
    };
    const shown: [string, string][] = [
      ["Dates", range === "all" ? "The last 24 months" : range === "custom" ? `${from} – ${to}`
        : RANGES.find(r => r.key === range)?.label ?? ""],
      ["Programme", programme ? programmes.find(p => String(p.id) === programme)?.name ?? programme : "All programmes"],
      ["Broker", brokerId && a ? (a.org || a.label) : "All brokers"],
      ...(policy ? [["Policy", policy] as [string, string]] : []),
    ];
    const notUsed = [
      ...(area !== "all" ? ["Area"] : []), ...(sub ? ["Sub-area"] : []), ...(verb ? ["Action"] : []),
      ...(status ? ["Status"] : []), ...(q ? ["Search"] : []),
      ...(actor && !brokerId ? ["Person"] : []), ...(record.startsWith("contract:") ? ["Contract"] : []),
    ];
    return { ask, shown, notUsed };
  };

  const save = async (format: "csv" | "xlsx") => {
    setBusy(format);
    try { await downloadAuditLogs(query, format); } finally { setBusy(""); }
  };

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Audit Logs <InfoTip text={SUBTITLE[seat] ?? "Who did what, when, and how it ended."} /></h2>
            <p>Everything anyone did, in one list. Use the filters to narrow it down.</p>
          </div>
          <div className="actions" style={{ display: "flex", gap: 8 }}>
            {/* One file for a policy or a period: every check that ran, its
                contract wording and page, every result and decision (50.1). */}
            <button className="btn sm" onClick={() => setPacking(true)}><Download size={13} /> Audit package</button>
            {/* Two formats because they are read two different ways: CSV feeds
                a system, Excel is what a person opens to filter and keep. */}
            <button className="btn sm" disabled={!!busy || list.total === 0} onClick={() => save("csv")}>
              <Download size={13} /> {busy === "csv" ? "Preparing…" : "Download CSV"}
            </button>
            <button className="btn pri sm" disabled={!!busy || list.total === 0} onClick={() => save("xlsx")}>
              <Download size={13} /> {busy === "xlsx" ? "Preparing…" : "Download Excel"}
            </button>
          </div>
        </div>

        <div className="card afl" style={{ marginBottom: 14 }}>
          <button type="button" className="afl-head" onClick={() => setOpen(o => !o)} aria-expanded={open}>
            {open ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
            <span>Filters</span>
            {chips.length > 0 && <span className="afl-count">{chips.length} applied</span>}
          </button>
          {open && <>
            <div className="afl-grid">
              <label className="afl-f">
                <span>Date</span>
                <span className="afl-box">
                  <Calendar size={14} className="afl-ic" />
                  <select aria-label="Date" value={range} onChange={e => setRange(e.target.value)}
                    className={range !== START.range ? "on" : ""}>
                    {RANGES.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}
                  </select>
                </span>
              </label>
              <label className="afl-f">
                <span>Area{!!opts?.modules?.length && <InfoTip text={areaHelp(opts.modules)} />}</span>
                <span className="afl-box">
                  <select aria-label="Area" value={area} onChange={e => pickArea(e.target.value)}
                    className={area !== "all" ? "on" : ""}>
                    <option value="all">All areas</option>
                    {opts?.modules?.map(m => <option key={m.key} value={m.key}>{m.label}</option>)}
                  </select>
                </span>
              </label>
              <label className="afl-f">
                <span>Sub-area{module && <InfoTip text={subHelp(module)} />}</span>
                <span className="afl-box">
                  <select aria-label="Sub-area" value={sub} onChange={e => pickSub(e.target.value)}
                    disabled={!module} className={sub ? "on" : ""}>
                    <option value="">{module ? `All of ${module.label}` : "Pick an Area first"}</option>
                    {module?.subs.map(x => <option key={x.key} value={x.key}>{x.label}</option>)}
                  </select>
                </span>
              </label>
              <label className="afl-f">
                <span>Action</span>
                <span className="afl-box">
                  <select aria-label="Action" value={verb} onChange={e => setVerb(e.target.value)}
                    className={verb ? "on" : ""}>
                    <option value="">All actions</option>
                    {actionChoices.map(a => <option key={a.key} value={a.key}>{a.label}</option>)}
                  </select>
                </span>
              </label>
              <label className="afl-f">
                <span>Person or company</span>
                <span className="afl-box">
                  <User size={14} className="afl-ic" />
                  <select aria-label="Person or company" value={actor} onChange={e => setActor(e.target.value)}
                    className={actor ? "on" : ""}>
                    <option value="">Everyone</option>
                    {sortedPeople(opts?.actors ?? []).map(a => <option key={a.key} value={a.key}>{personLabel(a)}</option>)}
                  </select>
                </span>
              </label>
              <label className="afl-f">
                <span>Programme</span>
                <span className="afl-box">
                  <select aria-label="Programme" value={programme} onChange={e => setProgramme(e.target.value)}
                    className={programme ? "on" : ""}>
                    <option value="">All programmes</option>
                    {programmes.map(p => <option key={p.id} value={String(p.id)}>{p.name}</option>)}
                  </select>
                </span>
              </label>
              <label className="afl-f">
                <span>Status</span>
                <span className="afl-box">
                  <select aria-label="Status" value={status} onChange={e => setStatus(e.target.value)}
                    className={status ? "on" : ""}>
                    <option value="">All statuses</option>
                    {statusChoices.map(a => <option key={a.key} value={a.key}>{a.label}</option>)}
                  </select>
                </span>
              </label>
            </div>

            {range === "custom" && (
              <div className="afl-row">
                <label className="afl-f" style={{ flex: "0 0 auto" }}>
                  <span>From – to</span>
                  <span className="afl-dates">
                    <input type="date" aria-label="From date" value={from} max={to} onChange={e => setFrom(e.target.value)} />
                    <span className="sub">–</span>
                    <input type="date" aria-label="To date" value={to} min={from} onChange={e => setTo(e.target.value)} />
                  </span>
                </label>
              </div>
            )}

            <form className="afl-row" onSubmit={e => { e.preventDefault(); setQ(typed.trim()); }}>
              <span className="search afl-search">
                <Search className="ic" />
                <input type="search" placeholder="Search anything in the table: a name, file, contract, value, status or date"
                  aria-label="Search the audit logs" value={typed}
                  onChange={e => { setTyped(e.target.value); if (!e.target.value) setQ(""); }} />
              </span>
              <AuditRecordFilter value={record} onChange={setRecord} />
              <button type="submit" className="btn pri"><Search size={14} /> Search</button>
              <button type="button" className="btn" onClick={reset}><RotateCcw size={14} /> Reset</button>
            </form>

            {chips.length > 0 && (
              <div className="afl-chips">
                <span className="sub">Showing:</span>
                {chips.map(c => (
                  <button type="button" key={c.label} className="afl-chip" onClick={c.clear} title="Remove this filter">
                    {c.label} <X size={12} />
                  </button>
                ))}
              </div>
            )}
          </>}
        </div>

        <div style={tiles(5)}>
          <Tile icon={Activity} label="Total activities" value={sum?.activities} colour="#7c3aed"
            info="Everything recorded in these dates that matches the filters." />
          <Tile icon={Users} label="People" value={sum?.people} colour="#2563eb"
            info="Different people who did something in these dates." />
          <Tile icon={Eye} label="Data viewed" value={sum?.viewed} colour="#0d9488"
            info="Times someone opened a file, a policy, a contract document or a report. Pick Area: Downloads and views to list them." />
          <Tile icon={Download} label="Downloads" value={sum?.downloaded} colour="#d97706"
            info="Times someone saved a file, a contract or a report to their computer." />
          <Tile icon={ShieldCheck} label="Rule changes" value={sum?.rule_changes} colour="#16a34a"
            info="Checks that were added, changed, switched on or off, or approved." />
        </div>

        {evidenceError && (
          <div className="note crit" style={{ marginBottom: 14 }}>{evidenceError}</div>
        )}

        <div className="card">
          {failed ? (
            <div className="empty">Could not load the audit logs.</div>
          ) : list.loading && list.items.length === 0 ? (
            <div className="empty">Loading…</div>
          ) : list.total === 0 ? (
            <div className="empty">
              {chips.length
                ? "Nothing matches these filters. Try a wider date range, or press Reset."
                : "Nothing has been recorded yet."}
            </div>
          ) : (
            <>
              <div className="tbl-wrap audit-scroll" ref={scroller}>
                <table className="afl-table">
                  <thead>
                    <tr>
                      <th style={{ width: 118 }}>When ↓</th>
                      <th style={{ width: 130 }}>Who</th>
                      <th style={{ width: 120 }}>Area</th>
                      <th>What they did</th>
                      <th style={{ width: 150 }}>On what</th>
                      <th style={{ width: 120 }}>Old value</th>
                      <th style={{ width: 120 }}>New value</th>
                      {showCarrier && <th style={{ width: 110 }}>Carrier</th>}
                      <th style={{ width: 110 }}>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.items.map(r => (
                      <tr key={r.id} onClick={() => setOpened(r)} style={{ cursor: "pointer" }}
                        title="Click to see the details">
                        <td className="muted">{fmtDateTime(r.at)}</td>
                        <td>
                          <b>{r.actor}</b>
                          <div className="sub">{r.actor_role}</div>
                        </td>
                        <td>
                          {r.module && <div className="sub">{r.module}</div>}
                          {r.area ? <span className="afl-area">{r.area}</span> : <span className="muted">—</span>}
                        </td>
                        <td>
                          {r.action_label}
                          {/* Exactly what was done — the field, the policy, the
                              row counts. An audit line that says only
                              "Corrected a value" has recorded that something
                              happened, not what. */}
                          {r.detail && (
                            <div className="sub" style={{ marginTop: 2, whiteSpace: "normal" }}>{r.detail}</div>
                          )}
                          {/* A bulk save is one line here; every change in it
                              is one click away, exactly as it was recorded. */}
                          {r.group && (
                            <button type="button" className="linkish audit-group-link"
                              onClick={e => { e.stopPropagation(); setOpenGroup(r); }}>
                              View all {r.group.count} {r.group.noun ?? "changes"} →
                            </button>
                          )}
                          {/* The server sends this on Kavachio's rows only: the
                              evidence behind a large contract date correction. */}
                          {r.evidence && (
                            <button type="button" className="linkish audit-group-link"
                              style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                              onClick={e => {
                                e.stopPropagation();
                                setEvidenceError("");
                                downloadCorrectionEvidence(r.evidence!.event_id, r.evidence!.filename)
                                  .catch(async e => setEvidenceError(
                                    await downloadErrorText(e, "Could not open the evidence.")));
                              }}>
                              <Paperclip size={12} /> Open evidence ({r.evidence.filename})
                            </button>
                          )}
                        </td>
                        <td style={{ whiteSpace: "normal" }}>
                          {/* The kind first — a bare name ("prg test") does not
                              say whether it is a programme, a file or a setup. */}
                          {r.target_kind && <div className="sub">{r.target_kind}</div>}
                          {r.target}
                        </td>
                        <td className="afl-val">{r.old ?? <span className="muted">—</span>}</td>
                        <td className="afl-val">{r.new ?? <span className="muted">—</span>}</td>
                        {showCarrier && <td className="muted">{r.carrier ?? "—"}</td>}
                        <td>
                          <span className={`badge ${TONE_CLASS[r.tone] ?? "b-mut"}`}>
                            <span className="d" />{r.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={list.page} pageSize={PAGE_SIZE} totalItems={list.total}
                pageCount={list.pageCount} onPageChange={list.setPage} noun="entries" />
            </>
          )}
        </div>
      </div>
      <GroupDrawer row={openGroup} query={query} onClose={() => setOpenGroup(null)} />
      {packing && <PackageModal {...packageAsk()} onClose={() => setPacking(false)} />}
      <SidePanel open={!!opened} title={opened?.action_label ?? "Event details"}
        sub={opened && <>{fmtDateTime(opened.at)} · <span className={`badge ${TONE_CLASS[opened.tone] ?? "b-mut"}`}><span className="d" />{opened.status}</span></>}
        onClose={() => setOpened(null)}>
        {opened && <EventDetails row={opened} showCarrier={showCarrier}
          onShowRecord={(rec, words) => {
            setOpened(null);
            if (rec) setRecord(rec); else if (words) { setTyped(words); setQ(words); }
          }} />}
      </SidePanel>
    </div>
  );
}

/** The Event details panel: the entry in full, read when it is opened
 *  (audit_one.event). Until it arrives — or for a bulk line, which has no
 *  single entry — it shows what the row itself carries. */
function EventDetails({ row, showCarrier, onShowRecord }: {
  row: AuditRow; showCarrier: boolean; onShowRecord: (record: string | null, words: string | null) => void;
}) {
  const [ev, setEv] = useState<AuditEvent | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "none">("loading");
  useEffect(() => {
    let live = true;
    setEv(null); setState("loading");
    if (row.group) { setState("none"); return; }
    getAuditEvent(row.id)
      .then(e => { if (live) { setEv(e); setState("ok"); } })
      .catch(() => { if (live) setState("none"); });
    return () => { live = false; };
  }, [row.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const Section = ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div className="evd-sec"><div className="evd-h">{title}</div>{children}</div>
  );
  const keep = (rows: [string, React.ReactNode][]) => rows.filter(([, v]) => v !== null && v !== undefined && v !== "");
  const who = ev?.who;
  const what = ev?.what;
  const changes = ev?.changes ?? row.changes ?? [];

  return (
    <div className="evd">
      <Section title="Who">
        <Facts rows={keep([
          ["Person", <b>{who?.person ?? row.actor}</b>],
          ["Role", who?.role ?? row.actor_role],
          ["Organisation", who?.organisation ?? row.actor_org],
          ...(showCarrier ? [["Carrier", who?.carrier ?? row.carrier] as [string, React.ReactNode]] : []),
          ["How", who?.how ?? null],
          ["IP address", who?.ip ?? null],
        ])} />
      </Section>
      <Section title="What">
        <Facts rows={keep([
          ["Area", what?.area ?? row.module ?? null],
          ["Sub-area", what?.sub_area ?? row.area ?? null],
          ["Action type", what?.action_type ?? null],
          ["What they did", row.action_label],
          ["Details", row.detail || null],
          ["On", what?.on ?? (row.target && row.target !== "—" ? `${row.target_kind ? `${row.target_kind}: ` : ""}${row.target}` : null)],
          ["Programme", what?.programme ?? null],
          ["Contract", what?.contract ?? null],
        ])} />
      </Section>
      {(changes.length > 0 || ev?.reason) && (
        <Section title="What changed">
          {changes.length > 0 && (
            <div className="tbl-wrap">
              <table className="evd-tbl">
                <thead><tr><th>Field</th><th>Old value</th><th /><th>New value</th></tr></thead>
                <tbody>
                  {changes.map((c, i) => (
                    <tr key={i}>
                      <td>{c.field ?? "—"}</td>
                      <td>{c.old ?? "—"}</td>
                      <td className="evd-arrow">→</td>
                      <td>{c.new ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {ev?.reason && <Facts rows={[["Reason given", ev.reason]]} />}
        </Section>
      )}
      {!!ev?.more.length && (
        <Section title="More details">
          <Facts rows={ev.more.map(m => [m.label, m.value] as [string, React.ReactNode])} />
        </Section>
      )}
      {!!ev?.earlier.length && (
        <Section title="Earlier on this record">
          <div className="evd-tl">
            {ev.earlier.map((x, i) => (
              <div key={i}><span className="muted">{fmtDateTime(x.at)}</span> {x.who}: {x.what}</div>
            ))}
          </div>
          {(ev.record || ev.search) && (
            <button type="button" className="linkish" onClick={() => onShowRecord(ev.record, ev.search)}>
              Show everything on this record →
            </button>
          )}
        </Section>
      )}
      {state === "loading" && <div className="muted" style={{ fontSize: 12.5 }}>Loading the full details…</div>}
      {ev && <div className="evd-ref">Reference: {ev.reference} (quote this when asking support)</div>}
    </div>
  );
}

// Where a decision was made — said once in the drawer's header, not 40 times.
const WHERE_NOTES = [" · on the exceptions review link", " · on the exceptions screen in the portal"];
// The drawer reads a bulk update 50 changes at a time ("Load more").
const GROUP_PAGE = 20;

/** Every change in one bulk save, in a right-hand drawer — the full record the
 *  single summary line stands for, one row per value, as it was saved. */
function GroupDrawer({ row, query, onClose }: {
  row: AuditRow | null; query: AuditQuery; onClose: () => void;
}) {
  const [items, setItems] = useState<AuditRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [more, setMore] = useState(false);
  const [err, setErr] = useState(false);
  const key = row?.group?.key ?? null;

  useEffect(() => {
    if (!key) return;
    let cancelled = false;
    setItems(null); setTotal(0); setErr(false);
    getAuditGroup(query, key, 0, GROUP_PAGE)
      .then(r => { if (!cancelled) { setItems(r.items); setTotal(r.total); } })
      .catch(() => { if (!cancelled) setErr(true); });
    return () => { cancelled = true; };
    // The filters are those the row was counted under; re-fetch only per row.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // The next 50, added under the ones already read — the list only grows.
  const loadMore = () => {
    if (!key || !items || more) return;
    setMore(true);
    getAuditGroup(query, key, items.length, GROUP_PAGE)
      .then(r => { setItems(prev => [...(prev ?? []), ...r.items]); setTotal(r.total); })
      .catch(() => setErr(true))
      .finally(() => setMore(false));
  };

  useEffect(() => {
    if (!row) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [row, onClose]);

  const open = !!row;
  const noun = row?.group?.noun ?? "changes";
  // Said once in the header rather than on every one of forty lines.
  const where = WHERE_NOTES.find(n => !!items?.length && items.every(i => i.detail.endsWith(n)));
  const L: React.CSSProperties = { textAlign: "left" };

  return (
    <>
      <div className={`scrim${open ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${open ? " on" : ""}`} aria-hidden={!open}>
        <div className="drawer-h">
          <div>
            <h4>{row?.action_label}</h4>
            <div className="sub">
              {row ? <>{row.actor} · {row.actor_role} · {fmtDateTime(row.at)}</> : " "}
            </div>
            {row?.target && row.target !== "—" && (
              <div className="sub">{row.target_kind ? `${row.target_kind}: ` : ""}{row.target}</div>
            )}
            {where && <div className="sub">Made {where.replace(" · ", "")}.</div>}
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b" style={{ padding: 0 }}>
          {err && <div className="note warn" style={{ margin: 16 }}>Could not load these changes.</div>}
          {!items && !err && <div className="muted" style={{ padding: 20 }}>Loading…</div>}
          {items && items.length === 0 && (
            <div className="empty" style={{ padding: 20 }}>Nothing to show for these filters.</div>
          )}
          {items && items.length > 0 && (
            <div className="tbl-wrap">
              <table>
                <thead><tr>
                  <th style={L}>#</th>
                  <th style={L}>{noun === "bordereaux" ? "Bordereau due" : "What changed"}</th>
                  <th style={L}>Status</th>
                </tr></thead>
                <tbody>
                  {items.map((i, n) => (
                    <tr key={i.id}>
                      <td className="muted" style={L}>{n + 1}</td>
                      <td style={{ ...L, whiteSpace: "normal" }}>
                        {where ? i.detail.slice(0, -where.length) : i.detail}
                      </td>
                      <td style={L}>
                        <span className={`badge ${TONE_CLASS[i.tone] ?? "b-mut"}`}>
                          <span className="d" />{i.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="card-h" style={{ justifyContent: "space-between", borderBottom: "none",
                                               borderTop: "1px solid var(--p-border)" }}>
                <span className="sub">Showing {items.length} of {total} {noun}</span>
                {items.length < total && (
                  <button type="button" className="btn sm" disabled={more} onClick={loadMore}>
                    {more ? "Loading…" : `Load ${Math.min(GROUP_PAGE, total - items.length)} more`}
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
