// 44 — Exception Ownership & Workload. Carrier only.
//
// Everything still waiting on somebody across the carrier's book, in one list:
// failed checks, late or missing files, disputes, files on hold and contracts
// to sign — each with its status, owner (a broker company, or the carrier),
// due date and age (backend open_problems.py). Same look as Exception
// Resolution: a one-line summary, a tile per type, the workload per owner,
// then the list.
//   44.1  the filterable list of every open problem
//   44.2  one programme's own view — /open-problems?program=<id>, opened from
//         the Programmes list
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlertTriangle, ListChecks, Users2 } from "lucide-react";
import {
  getOpenProblems, type OpenProblemsReport, type ProblemType,
} from "../api/reports";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";

const PAGE_SIZE = 25;
const WORKLOAD_PAGE_SIZE = 10;

const num = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("en-GB"));
const plural = (n: number, w: string, ws = `${w}s`) => `${num(n)} ${n === 1 ? w : ws}`;

/** How long a problem has been open, in whole days. */
function fmtAge(d: number | null | undefined): string {
  if (d == null) return "—";
  return d < 1 ? "Today" : plural(d, "day");
}

/** "2026-11-09" → "9 Nov 2026". */
function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** What each tile explains behind its ⓘ. */
const TYPE_INFO: Record<ProblemType, string> = {
  check: "A check that failed on a broker's file and has not been fixed yet.",
  late_file: "A bordereau that is overdue, delinquent, given more time, or never came.",
  dispute: "The broker says a late mark is wrong. You decide on the Bordereau Calendar.",
  held_file: "A file that arrived and is waiting for you to release or discard it.",
  signature: "An agreed contract waiting for a signature.",
};

/** "40 problems are open — 12 of them overdue. Brokers have 31; you have 9." */
function summaryLine(d: OpenProblemsReport, filtered: boolean): string {
  if (d.total_open === 0) return `Nothing is open${filtered ? " for this selection" : ""}.`;
  const yours = d.workload.find(w => w.owner === "carrier")?.open ?? 0;
  const brokers = d.total_open - yours;
  const head = `${plural(d.total_open, "problem")} ${d.total_open === 1 ? "is" : "are"} open`
    + (d.total_overdue > 0 ? ` — ${num(d.total_overdue)} of them overdue.` : ", none overdue.");
  const who = [brokers > 0 && `Brokers have ${num(brokers)}`, yours > 0 && `you have ${num(yours)}`]
    .filter(Boolean).join("; ");
  return `${head} ${who.charAt(0).toUpperCase()}${who.slice(1)}.`;
}

function numParam(v: string | null): number | null {
  const n = v ? Number(v) : NaN;
  return Number.isInteger(n) && n > 0 ? n : null;
}

export default function OpenProblems() {
  const [params, setParams] = useSearchParams();
  // The programme lives in the address, so a programme's own view (44.2) is a
  // link that can be opened, shared and come back to.
  const programId = numParam(params.get("program"));
  const [brokerId, setBrokerId] = useState<number | null>(null);
  const [kind, setKind] = useState<ProblemType | null>(null);
  const [owner, setOwner] = useState<string | null>(null);
  const [overdueOnly, setOverdueOnly] = useState(false);
  const [sort, setSort] = useState<"age" | "due">("age");
  const [page, setPage] = useState(1);
  const [workloadPage, setWorkloadPage] = useState(1);
  const [data, setData] = useState<OpenProblemsReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function setProgramId(id: number | null) {
    const next = new URLSearchParams(params);
    if (id) next.set("program", String(id)); else next.delete("program");
    setParams(next, { replace: true });
  }

  // A new selection starts the list from its first page.
  useEffect(() => { setPage(1); }, [programId, brokerId, kind, owner, overdueOnly, sort]);
  useEffect(() => { setWorkloadPage(1); }, [programId, brokerId]);

  useEffect(() => {
    let live = true;
    setLoading(true); setErr(null);
    getOpenProblems({ program_id: programId, broker_party_id: brokerId, type: kind, owner,
                      overdue_only: overdueOnly, sort, page, page_size: PAGE_SIZE })
      .then(d => { if (live) setData(d); })
      .catch(e => { if (live) setErr(e?.response?.data?.detail ?? "Could not load the open problems."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [programId, brokerId, kind, owner, overdueOnly, sort, page]);

  const first = loading && !data;
  const filtered = programId != null || brokerId != null;
  const programmeName = programId != null
    ? data?.programmes.find(p => p.id === programId)?.name : undefined;
  const workload = data?.workload ?? [];
  const workloadPages = Math.max(1, Math.ceil(workload.length / WORKLOAD_PAGE_SIZE));
  const workloadNow = Math.min(workloadPage, workloadPages);
  const workloadShown = workload.slice((workloadNow - 1) * WORKLOAD_PAGE_SIZE, workloadNow * WORKLOAD_PAGE_SIZE);
  const pageCount = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE));
  const listFiltered = kind != null || owner != null || overdueOnly;

  return (
    <div className="proto">
      <div className="view full">
        {/* ---- title, filters --------------------------------------------- */}
        <div className="page-head" style={{ flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="t">
            <h2>{programmeName ? `Open Problems · ${programmeName}` : "Open Problems"}</h2>
            <p>Everything still waiting on someone, who has to act, and what is overdue.</p>
          </div>
          <div className="actions" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
            <label className="fbar-field">
              <span className="sub">Programme</span>
              <select className="fbar-select" value={programId ?? ""}
                onChange={e => setProgramId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All programmes</option>
                {(data?.programmes ?? []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            <label className="fbar-field">
              <span className="sub">Broker</span>
              <select className="fbar-select" value={brokerId ?? ""}
                onChange={e => setBrokerId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">All brokers</option>
                {(data?.brokers ?? []).map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
              </select>
            </label>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}

        {/* ---- the book in one sentence ----------------------------------- */}
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start", marginBottom: 18,
                      background: "var(--p-primary-soft)", border: "1px solid #C7E3E7",
                      borderRadius: "var(--p-r)", padding: "16px 20px" }}>
          <span style={{ flex: "none", width: 36, height: 36, borderRadius: 18, background: "var(--p-surface)",
                         display: "inline-flex", alignItems: "center", justifyContent: "center",
                         color: "var(--p-primary)" }}>
            <AlertTriangle size={18} aria-hidden="true" />
          </span>
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <div style={{ fontSize: 12, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase",
                          color: "var(--p-info-ink)" }}>
              Today at a glance
              {data && <InfoTip text={`A problem is overdue once its due date has passed. Most are due ${plural(data.target_days, "day")} after they started — the fix target set on Exception Resolution. A late file is due on its calendar due date.`} />}
            </div>
            <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.55, color: "var(--p-ink)" }}>
              {first || !data ? "Loading…" : summaryLine(data, filtered)}
            </p>
          </div>
        </div>

        {/* ---- a tile per type: picking one filters the list --------------- */}
        <div className="tiles" style={{ marginBottom: 18 }}>
          {(data?.by_type ?? []).map(t => {
            const on = kind === t.type;
            return (
              <button key={t.type} type="button" className="tile"
                onClick={() => setKind(on ? null : t.type)} aria-pressed={on}
                style={{ textAlign: "left", cursor: "pointer", font: "inherit",
                         borderColor: on ? "var(--p-primary)" : t.overdue > 0 ? "#F3D2D7" : undefined }}>
                <div className="k">{t.label}<InfoTip text={TYPE_INFO[t.type]} /></div>
                <div className="v">{num(t.open)}</div>
                <div className="foot" style={{ color: t.overdue > 0 ? "var(--p-crit-ink)" : undefined }}>
                  {t.overdue > 0 ? `${num(t.overdue)} overdue` : "None overdue"}
                </div>
              </button>
            );
          })}
        </div>

        {/* ---- workload: one row per company that has to act -------------- */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <Users2 className="ci" />
            <h3>Workload</h3>
            <InfoTip text="Who has to act on each open problem: the broker company, or you (Carrier) for disputes to decide, files to release and contracts to sign. Pick a row to see only theirs." />
            <div className="right"><span className="sub">Most overdue first</span></div>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="l">Owner</th>
                  <th className="r">Open</th><th className="r">Overdue</th><th className="r">Oldest</th>
                  {(data?.types ?? []).map(t => <th key={t.type} className="r">{t.label}</th>)}
                </tr>
              </thead>
              <tbody>
                {!first && workload.length === 0 && (
                  <tr><td colSpan={4 + (data?.types.length ?? 0)} className="muted" style={{ textAlign: "center" }}>
                    Nobody has anything open.
                  </td></tr>
                )}
                {workloadShown.map(w => {
                  const on = owner === w.owner;
                  return (
                    <tr key={w.owner} onClick={() => setOwner(on ? null : w.owner)}
                      style={{ cursor: "pointer", background: on ? "var(--p-primary-soft)" : undefined }}>
                      <td className="l" style={{ fontWeight: 600 }}>{w.owner_name}</td>
                      <td className="r">{num(w.open)}</td>
                      <td className="r" style={{ fontWeight: 600, color: w.overdue > 0 ? "var(--p-crit-ink)" : undefined }}>
                        {num(w.overdue)}
                      </td>
                      <td className="r muted">{fmtAge(w.oldest_days)}</td>
                      {(data?.types ?? []).map(t => (
                        <td key={t.type} className="r muted">{w.by_type[t.type] ? num(w.by_type[t.type]) : "—"}</td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination page={workloadNow} pageCount={workloadPages} pageSize={WORKLOAD_PAGE_SIZE}
            totalItems={workload.length} onPageChange={setWorkloadPage} noun="owners" />
        </div>

        {/* ---- 44.1: the list ---------------------------------------------- */}
        <div className="card">
          <div className="card-h" style={{ flexWrap: "wrap", gap: 12 }}>
            <ListChecks className="ci" />
            <h3>Open problems</h3>
            <div className="right" style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
              <select className="fbar-select" value={kind ?? ""} aria-label="Type"
                onChange={e => setKind((e.target.value || null) as ProblemType | null)}>
                <option value="">All types</option>
                {(data?.types ?? []).map(t => <option key={t.type} value={t.type}>{t.label}</option>)}
              </select>
              <select className="fbar-select" value={owner ?? ""} aria-label="Owner"
                onChange={e => setOwner(e.target.value || null)}>
                <option value="">All owners</option>
                {workload.map(w => <option key={w.owner} value={w.owner}>{w.owner_name}</option>)}
              </select>
              <select className="fbar-select" value={sort} aria-label="Sort"
                onChange={e => setSort(e.target.value as "age" | "due")}>
                <option value="age">Oldest first</option>
                <option value="due">Due soonest first</option>
              </select>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 14 }}>
                <input type="checkbox" checked={overdueOnly} onChange={e => setOverdueOnly(e.target.checked)} />
                Overdue only
              </label>
              {listFiltered && (
                <button type="button" className="btn sm"
                  onClick={() => { setKind(null); setOwner(null); setOverdueOnly(false); }}>
                  Clear
                </button>
              )}
            </div>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th className="l">Type</th><th className="l">Problem</th><th className="l">Programme</th>
                  <th className="l">Owner</th><th className="l">Status</th>
                  <th className="r">Due</th><th className="r">Age</th><th className="r" />
                </tr>
              </thead>
              <tbody>
                {first && (
                  <tr><td colSpan={8} className="muted" style={{ textAlign: "center" }}>Loading…</td></tr>
                )}
                {!first && (data?.items.length ?? 0) === 0 && (
                  <tr><td colSpan={8} className="muted" style={{ textAlign: "center" }}>
                    {listFiltered ? "Nothing matches these filters." : "Nothing is open."}
                  </td></tr>
                )}
                {(data?.items ?? []).map(p => (
                  <tr key={p.key}>
                    <td className="l" style={{ whiteSpace: "nowrap" }}>{p.type_label}</td>
                    <td className="l">
                      <div style={{ fontWeight: 600 }}>{p.title}</div>
                      {p.detail && <div className="sub">{p.detail}</div>}
                    </td>
                    <td className="l">{p.programme_name}</td>
                    <td className="l">{p.owner_name}</td>
                    <td className="l">
                      {p.overdue
                        ? <span className="badge b-crit"><span className="d" />{p.status}</span>
                        : <span className="badge b-mut"><span className="d" />{p.status}</span>}
                    </td>
                    <td className="r" style={{ whiteSpace: "nowrap", fontWeight: p.overdue ? 600 : undefined,
                                               color: p.overdue ? "var(--p-crit-ink)" : undefined }}>
                      {fmtDate(p.due_on)}
                    </td>
                    <td className="r muted" style={{ whiteSpace: "nowrap" }}>{fmtAge(p.age_days)}</td>
                    <td className="r">
                      {p.link && <Link className="btn sm" to={p.link}>Open →</Link>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={Math.min(page, pageCount)} pageCount={pageCount} pageSize={PAGE_SIZE}
            totalItems={data?.total ?? 0} onPageChange={setPage} noun="problems" />
        </div>
      </div>
    </div>
  );
}
