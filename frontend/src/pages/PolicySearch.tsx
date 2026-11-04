// Policy Search (Phase 2, 43.1) — the policies themselves, not the files.
//
// Every policy in the current processed bordereaux, one line each, searched
// by policy number or insured and filtered by programme, broker (carrier
// seat), state and a date range (backend policy_search.py). A carrier sees
// every policy its brokers have sent; a broker sees only its own, on the
// carrier picked in the sidebar's carrier switch (all carriers when none is).
//
// Deliberately NOT a sidebar entry: it is opened from the Dashboard (both
// seats) and from File Submissions (carrier), like Party and Contracts.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Search } from "lucide-react";
import {
  getPolicyDetail, searchPolicies, type PolicyDateField, type PolicyDetail,
  type PolicyLine, type PolicyRow, type PolicySearchResult,
} from "../api/policies";
import { useBrokerCarrierId } from "../brokerCarrier";
import { isBrokerSeat } from "../auth";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { periodLabel } from "../utils/date";

const PAGE_SIZE = 25;

const DATE_FIELDS: { key: PolicyDateField; label: string }[] = [
  { key: "start", label: "Cover start" },
  { key: "end", label: "Cover end" },
  { key: "month", label: "Bordereau month" },
];

/** "2026-10-01" → "1 Oct 2026"; text that is not a date is shown as written. */
function fmtDay(v: string | null): string {
  if (!v) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
  if (!m) return v;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
    .toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

function fmtMoney(n: number | null, currency: string | null): string {
  if (n == null) return "—";
  const v = n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency ? `${currency} ${v}` : v;
}

const idOrNull = (v: string | null) => (v && /^\d+$/.test(v) ? Number(v) : null);

export default function PolicySearch() {
  const broker = isBrokerSeat();
  // A broker works one carrier at a time when the sidebar says so.
  const carrierId = useBrokerCarrierId();
  const [params] = useSearchParams();

  const [typed, setTyped] = useState(params.get("q") ?? "");
  const q = useDebouncedValue(typed.trim(), 350);
  const [programId, setProgramId] = useState<number | null>(idOrNull(params.get("program")));
  const [brokerId, setBrokerId] = useState<number | null>(idOrNull(params.get("broker")));
  const [state, setState] = useState("");
  const [dateField, setDateField] = useState<PolicyDateField>("start");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);

  const [data, setData] = useState<PolicySearchResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState<PolicyLine | null>(null);

  // A new search or filter starts again from the first page.
  useEffect(() => { setPage(1); },
    [q, programId, brokerId, state, dateField, from, to, carrierId]);

  useEffect(() => {
    let live = true;
    setLoading(true); setFailed(false);
    searchPolicies({
      q, program_id: programId, broker_party_id: broker ? null : brokerId,
      carrier_id: broker ? carrierId : null, state,
      date_field: dateField, date_from: from, date_to: to,
      page, page_size: PAGE_SIZE,
    })
      .then(r => { if (live) setData(r); })
      .catch(() => { if (live) setFailed(true); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [q, programId, brokerId, state, dateField, from, to, page, carrierId, broker]);

  const f = data?.filters;
  const filtered = !!(q || programId != null || brokerId != null || state || from || to);
  // The carrier column earns its place only when a broker is looking across
  // more than one carrier.
  const showInsurer = broker && carrierId == null && (f?.insurers.length ?? 0) > 1;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Policies
              <InfoTip text={broker
                ? "Every policy in the bordereaux you have sent, as each file's latest version. One line per policy; open it to see every row behind it."
                : "Every policy in the bordereaux your brokers have sent, as each file's latest version. One line per policy; open it to see every row behind it."} />
            </h2>
          </div>
          <div className="actions">
            <Link className="btn" to={broker ? "/broker" : "/home"}>← Dashboard</Link>
          </div>
        </div>

        <div className="card" style={{ overflow: "visible", marginBottom: 14 }}>
          <div className="card-h" style={{ gap: 14, flexWrap: "wrap", borderBottom: "none",
                                           alignItems: "flex-end" }}>
            <label className="fbar-field grow">
              <span className="sub">Search</span>
              <span className="search">
                <Search className="ic" />
                <input type="search" placeholder="Policy number or insured"
                  aria-label="Search policies" value={typed}
                  onChange={e => setTyped(e.target.value)} />
              </span>
            </label>

            <label className="fbar-field">
              <span className="sub">Programme</span>
              <select className="fbar-select" aria-label="Programme" value={programId ?? ""}
                onChange={e => setProgramId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All programmes</option>
                {(f?.programmes ?? []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>

            {!broker && (
              <label className="fbar-field">
                <span className="sub">Broker</span>
                <select className="fbar-select" aria-label="Broker" value={brokerId ?? ""}
                  onChange={e => setBrokerId(e.target.value ? Number(e.target.value) : null)}>
                  <option value="">All brokers</option>
                  {(f?.brokers ?? []).map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
                </select>
              </label>
            )}

            <label className="fbar-field" style={{ minWidth: 120 }}>
              <span className="sub">State</span>
              <select className="fbar-select" aria-label="State" value={state}
                onChange={e => setState(e.target.value)}>
                <option value="">All states</option>
                {(f?.states ?? []).map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>

            <label className="fbar-field" style={{ minWidth: 150 }}>
              <span className="sub">Date</span>
              <select className="fbar-select" aria-label="Which date" value={dateField}
                onChange={e => setDateField(e.target.value as PolicyDateField)}>
                {DATE_FIELDS.map(d => <option key={d.key} value={d.key}>{d.label}</option>)}
              </select>
            </label>

            <label className="fbar-field">
              <span className="sub">From / to</span>
              <span className="fbar-daterange">
                <input type="date" className="fbar-date" aria-label="From date"
                  value={from} max={to || undefined} onChange={e => setFrom(e.target.value)} />
                <span className="sub">–</span>
                <input type="date" className="fbar-date" aria-label="To date"
                  value={to} min={from || undefined} onChange={e => setTo(e.target.value)} />
              </span>
            </label>

            {filtered && (
              <span className="linkish fbar-clear-link" onClick={() => {
                setTyped(""); setProgramId(null); setBrokerId(null); setState("");
                setDateField("start"); setFrom(""); setTo("");
              }}>Clear filters</span>
            )}
          </div>
        </div>

        <div className="card">
          {failed ? (
            <div className="empty">Could not load the policies.</div>
          ) : !data ? (
            <div className="empty">Loading…</div>
          ) : data.total === 0 ? (
            <div className="empty">
              {filtered ? "No policies match these filters."
                : data.files === 0 ? "No bordereaux have been processed yet."
                : "No policy numbers were found in the processed bordereaux."}
            </div>
          ) : (
            <>
              <div className="tbl-wrap" style={loading ? { opacity: 0.6 } : undefined}>
                <table>
                  <thead>
                    <tr>
                      <th className="l">Policy number</th>
                      <th className="l">Insured</th>
                      <th className="l">Programme</th>
                      {!broker && <th className="l">Broker</th>}
                      {showInsurer && <th className="l">Carrier</th>}
                      <th>State</th>
                      <th>Cover</th>
                      <th>Latest transaction</th>
                      <th>Premium</th>
                      <th>Latest bordereau</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map(p => (
                      <tr key={`${p.insurer_id}:${p.program_id}:${p.broker_party_id}:${p.policy}`}
                          style={{ cursor: "pointer" }} onClick={() => setOpen(p)}>
                        <td className="l" style={{ whiteSpace: "nowrap" }}>
                          {/* The whole row opens the policy; the number is the visible link. */}
                          <span className="linkish mono">{p.policy}</span>
                        </td>
                        <td className="l">{p.insured ?? <span className="muted">—</span>}</td>
                        <td className="l">{p.programme ?? <span className="muted">—</span>}</td>
                        {!broker && <td className="l">{p.broker ?? <span className="muted">No broker</span>}</td>}
                        {showInsurer && <td className="l">{p.insurer ?? "—"}</td>}
                        <td>{p.state ?? <span className="muted">—</span>}</td>
                        <td style={{ whiteSpace: "nowrap" }}>
                          {p.start || p.end ? `${fmtDay(p.start)} – ${fmtDay(p.end)}` : <span className="muted">—</span>}
                        </td>
                        <td>{p.transaction ?? <span className="muted">—</span>}</td>
                        <td style={{ whiteSpace: "nowrap" }}>{fmtMoney(p.premium, p.currency)}</td>
                        <td style={{ whiteSpace: "nowrap" }}>
                          {p.last_period ? periodLabel(p.last_period) : <span className="muted">—</span>}
                          {p.files > 1 && <div className="sub">{p.files} bordereaux</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={page} pageSize={PAGE_SIZE} totalItems={data.total}
                pageCount={Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
                onPageChange={setPage} noun="policies" />
            </>
          )}
        </div>
      </div>

      <PolicyDrawer line={open} carrierId={broker ? carrierId : null}
        onClose={() => setOpen(null)} />
    </div>
  );
}

/** One policy: who it is with, and every bordereau row behind it. */
function PolicyDrawer({ line, carrierId, onClose }: {
  line: PolicyLine | null; carrierId: number | null; onClose: () => void;
}) {
  const [detail, setDetail] = useState<PolicyDetail | null>(null);
  const [err, setErr] = useState(false);
  const [shown, setShown] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!line) return;
    let live = true;
    setDetail(null); setErr(false); setShown(new Set());
    getPolicyDetail(line, carrierId)
      .then(d => { if (live) setDetail(d); })
      .catch(() => { if (live) setErr(true); });
    return () => { live = false; };
  }, [line, carrierId]);

  useEffect(() => {
    if (!line) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [line, onClose]);

  const toggle = (k: string) => setShown(prev => {
    const next = new Set(prev);
    if (next.has(k)) next.delete(k); else next.add(k);
    return next;
  });

  return (
    <>
      <div className={`scrim${line ? " on" : ""}`} onClick={onClose} />
      <aside className={`drawer wide${line ? " on" : ""}`} aria-hidden={!line}>
        <div className="drawer-h">
          <div>
            <h4 className="mono">{line?.policy}</h4>
            <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>{line?.insured ?? " "}</div>
          </div>
          <button type="button" className="closeb" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="drawer-b">
          {line && (
            <div style={{ marginBottom: 16 }}>
              {detail?.seat === "broker"
                ? <div className="kv"><span className="k">Carrier</span><span className="v">{line.insurer ?? "—"}</span></div>
                : <div className="kv"><span className="k">Broker</span><span className="v">{line.broker ?? "No broker"}</span></div>}
              <div className="kv"><span className="k">Programme</span><span className="v">{line.programme ?? "—"}</span></div>
              <div className="kv"><span className="k">State</span><span className="v">{line.state ?? "—"}</span></div>
              <div className="kv"><span className="k">Cover</span>
                <span className="v">{fmtDay(line.start)} – {fmtDay(line.end)}</span></div>
              <div className="kv"><span className="k">{line.rows > 1 ? "Premium, all rows" : "Premium"}</span>
                <span className="v">{fmtMoney(line.premium, line.currency)}</span></div>
            </div>
          )}

          <h4 style={{ margin: "0 0 10px", fontSize: 13.5 }}>
            Bordereau rows{detail ? ` (${detail.rows.length})` : ""}
          </h4>
          {err && <div className="note warn">Could not load this policy's rows.</div>}
          {!detail && !err && <div className="muted">Loading…</div>}
          {detail?.rows.map(r => {
            const k = `${r.export_id}:${r.row}`;
            return (
              <div key={k} className="card" style={{ marginBottom: 10, padding: "12px 14px" }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "baseline" }}>
                  <b>{r.period ? periodLabel(r.period) : "No reporting month"}</b>
                  <span style={{ whiteSpace: "nowrap" }}>{fmtMoney(r.premium, r.currency)}</span>
                </div>
                <div className="sub" style={{ marginTop: 4 }}>
                  {r.transaction ?? "No transaction type"} · row {r.row}
                </div>
                <div className="mono" style={{ fontSize: 12, marginTop: 4, wordBreak: "break-all" }}>
                  {r.filename}
                  {r.version_no != null && r.version_no > 1 && <span className="vchip">Version {r.version_no}</span>}
                </div>
                <div style={{ display: "flex", gap: 14, marginTop: 8 }}>
                  <Link className="linkish" style={{ fontSize: 12.5 }}
                    to={`/uploads/${r.export_id}/exceptions?download=${r.export_id}&from=policies`}>
                    Open file →
                  </Link>
                  <span className="linkish" style={{ fontSize: 12.5 }} onClick={() => toggle(k)}>
                    {shown.has(k) ? "Hide all columns" : `All columns (${r.columns.length})`}
                  </span>
                </div>
                {shown.has(k) && <AllColumns columns={r.columns} />}
              </div>
            );
          })}
        </div>
      </aside>
    </>
  );
}

/** One row's every column. On opening, the drawer scrolls its card to the top
 *  and the list takes the rest of the drawer's height (re-measured on resize);
 *  a longer list scrolls inside it. */
function AllColumns({ columns }: { columns: PolicyRow["columns"] }) {
  const box = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number>();

  useLayoutEffect(() => {
    const el = box.current;
    const card = el?.closest<HTMLElement>(".card");
    const pane = el?.closest<HTMLElement>(".drawer-b");
    if (!el || !card || !pane) return;
    const px = (v: string) => parseFloat(v) || 0;
    // The card stops the same gap below the drawer's top as the cards keep between them.
    const gap = () => px(getComputedStyle(card).marginBottom);
    const fit = () => {
      const p = getComputedStyle(pane), c = getComputedStyle(card);
      const room = pane.clientHeight - gap() - px(p.paddingBottom);
      const above = el.getBoundingClientRect().top - card.getBoundingClientRect().top;
      setHeight(Math.max(200, room - above - px(c.paddingBottom) - px(c.borderBottomWidth)));
    };
    fit();
    pane.scrollTo({
      top: pane.scrollTop + card.getBoundingClientRect().top - pane.getBoundingClientRect().top - gap(),
      behavior: "smooth",
    });
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);

  const L: React.CSSProperties = { textAlign: "left" };
  return (
    <div ref={box} className="tbl-wrap" style={{ marginTop: 10, maxHeight: height, overflow: "auto" }}>
      <table>
        <tbody>
          {columns.map(c => (
            <tr key={c.name}>
              <td style={{ ...L, color: "var(--p-muted)", width: "45%" }}>{c.name}</td>
              <td style={{ ...L, wordBreak: "break-word" }}>{String(c.value ?? "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
