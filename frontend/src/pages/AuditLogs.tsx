import { useEffect, useMemo, useState } from "react";
import { Download, Search } from "lucide-react";
import {
  downloadAuditLogs, getAuditLogs, getAuditOptions,
  type AuditOptions, type AuditQuery, type AuditRow,
} from "../api/audit";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useServerList } from "../hooks/useServerList";
import { Pagination } from "../components/Pagination";
import { fmtDateTime } from "../utils/date";

const PAGE_SIZE = 15;

/** What this seat's trail actually covers — said once, at the top, because the
 *  answer is different for every role and a table of rows cannot say it. */
const SUBTITLE: Record<string, string> = {
  platform:
    "Everything that happens on Kavachio — every carrier, every broker and every person, named.",
  carrier_admin:
    "Everyone at your company, and every broker you work with. A broker's people are shown as the broker.",
  carrier_user:
    "Your own trail, and everything the brokers you work with have done. A broker's people are shown as the broker.",
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
  { key: "90", label: "Last 90 days" },
  { key: "all", label: "All time" },
  { key: "custom", label: "Custom range…" },
];

function rangeDates(key: string, from: string, to: string): { from?: string; to?: string } {
  if (key === "custom") return { from: from || undefined, to: to || undefined };
  if (key === "all") return {};
  return { from: daysAgo(Number(key)), to: localDay(new Date()) };
}

/**
 * Audit Logs — who did what, when, and how it ended.
 *
 * One screen for all five seats. The filters and the columns are the same
 * everywhere; what changes is WHOSE rows the server sends back and what it is
 * willing to call them (audit_feed.py). So there is no role branching here
 * beyond the sentence at the top — if this screen ever needed to hide a row,
 * that would mean the server had already sent one it should not have.
 */
export default function AuditLogs() {
  const [range, setRange] = useState("30");
  const [from, setFrom] = useState(daysAgo(30));
  const [to, setTo] = useState(localDay(new Date()));
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("all");
  const [typed, setTyped] = useState("");
  const q = useDebouncedValue(typed, 300);
  const [opts, setOpts] = useState<AuditOptions | null>(null);
  const [busy, setBusy] = useState<"" | "csv" | "xlsx">("");
  const [failed, setFailed] = useState(false);

  useEffect(() => { getAuditOptions().then(setOpts).catch(() => setOpts(null)); }, []);

  const query: AuditQuery = useMemo(
    () => ({ ...rangeDates(range, from, to), actor, action, q }),
    [range, from, to, actor, action, q],
  );

  const list = useServerList<AuditRow, { seat?: string }>(
    (page, size) => getAuditLogs(query, page, size)
      .then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify(query),
    PAGE_SIZE,
  );

  const seat = list.extra?.seat ?? "";
  const showCarrier = seat === "platform";
  const filtered = Boolean(actor || (action && action !== "all") || q || range !== "30");

  const save = async (format: "csv" | "xlsx") => {
    setBusy(format);
    try { await downloadAuditLogs(query, format); } finally { setBusy(""); }
  };

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Audit Logs</h2>
            <p>{SUBTITLE[seat] ?? "Who did what, when, and how it ended."}</p>
          </div>
          <div className="actions" style={{ display: "flex", gap: 8 }}>
            {/* Two formats because they are read two different ways: CSV feeds
                a system, Excel is what a person opens to filter and keep. */}
            <button className="btn sm" disabled={!!busy || list.total === 0}
              onClick={() => save("csv")}>
              <Download size={13} /> {busy === "csv" ? "Preparing…" : "Download CSV"}
            </button>
            <button className="btn pri sm" disabled={!!busy || list.total === 0}
              onClick={() => save("xlsx")}>
              <Download size={13} /> {busy === "xlsx" ? "Preparing…" : "Download Excel"}
            </button>
          </div>
        </div>

        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none" }}>
            <label className="fbar-field">
              <span className="sub">Date range</span>
              <select className="fbar-select" aria-label="Date range" value={range}
                onChange={e => setRange(e.target.value)}>
                {RANGES.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}
              </select>
            </label>

            {range === "custom" && (
              <label className="fbar-field">
                <span className="sub">From / to</span>
                <span className="fbar-daterange">
                  <input type="date" className="fbar-date" aria-label="From date"
                    value={from} max={to} onChange={e => setFrom(e.target.value)} />
                  <span className="sub">–</span>
                  <input type="date" className="fbar-date" aria-label="To date"
                    value={to} min={from} onChange={e => setTo(e.target.value)} />
                </span>
              </label>
            )}

            <label className="fbar-field">
              <span className="sub">Role / actor</span>
              <select className="fbar-select" aria-label="Role or actor" value={actor}
                onChange={e => setActor(e.target.value)}>
                <option value="">Everyone in your reach</option>
                {opts?.actors.map(a => (
                  <option key={a.key} value={a.key}>{a.label} — {a.role}</option>
                ))}
              </select>
            </label>

            <label className="fbar-field">
              <span className="sub">Action type</span>
              <select className="fbar-select" aria-label="Action type" value={action}
                onChange={e => setAction(e.target.value)}>
                {(opts?.action_groups ?? [{ key: "all", label: "All actions", actions: [] }])
                  .map(g => <option key={g.key} value={g.key}>{g.label}</option>)}
              </select>
            </label>

            <label className="search" style={{ minWidth: 220 }}>
              <Search className="ic" />
              <input type="search" placeholder="Search a person, file or action"
                aria-label="Search the audit trail"
                value={typed} onChange={e => setTyped(e.target.value)} />
            </label>

            {filtered && (
              <span className="linkish" onClick={() => {
                setRange("30"); setFrom(daysAgo(30)); setTo(localDay(new Date()));
                setActor(""); setAction("all"); setTyped("");
              }}>Clear filters</span>
            )}
          </div>
        </div>

        <div className="card">
          {failed ? (
            <div className="empty">Could not load the audit trail.</div>
          ) : list.loading && list.items.length === 0 ? (
            <div className="empty">Loading…</div>
          ) : list.total === 0 ? (
            <div className="empty">
              {filtered
                ? "Nothing matches these filters. Try a wider date range."
                : "Nothing has been recorded yet."}
            </div>
          ) : (
            <>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th style={{ whiteSpace: "nowrap" }}>Timestamp ↓</th>
                      <th>Actor / role</th>
                      <th style={{ minWidth: 320 }}>Action / what exactly</th>
                      <th>Target / file</th>
                      {showCarrier && <th>Carrier</th>}
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.items.map(r => (
                      <tr key={r.id}>
                        <td className="muted" style={{ whiteSpace: "nowrap" }}>
                          {fmtDateTime(r.at)}
                        </td>
                        <td>
                          <b>{r.actor}</b>
                          <div className="sub">{r.actor_role}</div>
                        </td>
                        <td>
                          {r.action_label}
                          {/* Exactly what was done — the field, the policy, the
                              old and new value. An audit line that says only
                              "Corrected a value" has recorded that something
                              happened, not what. */}
                          {r.detail && (
                            <div className="sub" style={{ marginTop: 2, whiteSpace: "normal" }}>
                              {r.detail}
                            </div>
                          )}
                        </td>
                        <td>{r.target}</td>
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
    </div>
  );
}
