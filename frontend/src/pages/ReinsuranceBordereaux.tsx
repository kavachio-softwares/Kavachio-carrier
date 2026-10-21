/**
 * Reinsurance Bordereaux — the carrier's ceded statements, every treaty, every
 * period.
 *
 * Opening this page prepares whatever is due (a period whose inward
 * bordereaux are in and has no statement yet, or one whose inward files were
 * corrected after sending). Nothing is sent from here without pressing Send
 * on the statement itself.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { FileSpreadsheet, FileUp, Loader2, Search, Sparkles } from "lucide-react";
import { FileDrop } from "../components/ui/FileDrop";
import { TreatyChecks } from "../components/treaty/TreatyRules";
import { InfoTip } from "../components/InfoTip";
import { fieldErrors } from "../api/contractRecord";
import {
  cededPeriods, checkCeded, listCeded, prepareCeded, uploadPeriodInput,
  type SetupPreview,
  type CededList, type CededStatus,
} from "../api/ceded";
import { fmtDate } from "../utils/date";

export const CEDED_STATUS: Record<CededStatus, { label: string; cls: string }> = {
  draft: { label: "Ready to send", cls: "b-info" },
  sent: { label: "Sent", cls: "b-mut" },
  queried: { label: "Queried", cls: "b-warn" },
  agreed: { label: "Agreed", cls: "b-ok" },
  superseded: { label: "Replaced", cls: "b-mut" },
};

export function money(v: number | null | undefined, cur?: string | null) {
  if (v == null) return "—";
  return `${cur ? cur + " " : ""}${v.toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const FILTERS: { key: string; label: string }[] = [
  { key: "", label: "All" },
  { key: "draft", label: "Ready to send" },
  { key: "sent", label: "Sent" },
  { key: "queried", label: "Queried" },
  { key: "agreed", label: "Agreed" },
  { key: "overdue", label: "Overdue" },
];

export default function ReinsuranceBordereaux() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const treatyParam = params.get("treaty");
  const [data, setData] = useState<CededList | null>(null);
  const [filter, setFilter] = useState(params.get("status") ?? "");
  const [showOld, setShowOld] = useState(false);
  const [err, setErr] = useState("");
  const [treaty, setTreaty] = useState(treatyParam ?? "");
  const [periods, setPeriods] = useState<string[] | null>(null);
  const [period, setPeriod] = useState("");
  // Which action is running, so only its button spins — as Process Bordereau.
  const [busy, setBusy] = useState<"" | "check" | "run">("");
  // Check's answer: the statement worked out, nothing saved.
  const [checked, setChecked] = useState<SetupPreview | null>(null);
  // A treaty whose setup reads the carrier's own file: the period is typed
  // and that period's file uploaded, instead of picked from what came in.
  const [ownFile, setOwnFile] = useState<File | null>(null);
  const [ownPeriod, setOwnPeriod] = useState("");
  const picked = data?.treaties.find(t => String(t.contract_id) === treaty);
  const ownInput = picked?.input_source === "own";
  const freq = picked?.frequency ?? "monthly";

  useEffect(() => {
    listCeded(treatyParam ? Number(treatyParam) : undefined)
      .then(setData).catch(e => setErr(fieldErrors(e).message));
  }, [treatyParam]);

  useEffect(() => {
    setPeriods(null); setPeriod("");
    if (!treaty) return;
    cededPeriods(Number(treaty)).then(p => { setPeriods(p); setPeriod(p[0] ?? ""); })
      .catch(() => setPeriods([]));
  }, [treaty]);

  // With a file of its own the period is typed — it need not be one the
  // brokers' bordereaux have come in for.
  const typedPeriod = ownInput || !!ownFile;
  const runPeriod = typedPeriod ? ownPeriod.trim() : period;
  const canRun = !!treaty && !!runPeriod && (!ownInput || !!ownFile);

  async function check() {
    setBusy("check"); setErr("");
    try {
      setChecked(await checkCeded(Number(treaty), runPeriod, ownFile));
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  /** Make the statements — from this period's file when the setup reads one.
   *  `resolve`: then open its exceptions, to decide the rows the treaty's
   *  rules caught — as a broker resolves a processed bordereau's. */
  async function generate(resolve = false) {
    setBusy("run"); setErr("");
    try {
      const d = ownFile
        ? await uploadPeriodInput(Number(treaty), runPeriod, ownFile)
        : await prepareCeded(Number(treaty), runPeriod);
      nav(`/reinsurance/bordereaux/${d.id}${resolve ? "/exceptions" : ""}`);
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  const items = useMemo(() => (data?.items ?? []).filter(i =>
    (showOld || i.status !== "superseded")
    && (!filter || (filter === "overdue" ? i.overdue : i.status === filter))),
  [data, filter, showOld]);

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Process Bordereau
              <InfoTip text={"What you cede to your reinsurers, per treaty and period, "
                + "worked out from the bordereaux your brokers sent. Each reinsurer gets "
                + "its own statement — its share of every line — and agrees it or raises "
                + "a query on its private link."} />
            </h2>
            <p>
              Process a treaty&rsquo;s bordereau for a period, then send each
              reinsurer its statement. Treaties that read your brokers&rsquo;
              bordereaux are also prepared on their own once a period is in.
            </p>
          </div>
          <div className="actions">
            <Link to="/direct/setups?tab=reinsurance" className="btn">Bordereau Setup</Link>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
        {!!data?.prepared_now && (
          <div className="note ok" style={{ marginBottom: 14 }}>
            {data.prepared_now} new statement{data.prepared_now === 1 ? " was" : "s were"}{" "}
            prepared from bordereaux that came in since you last looked.
          </div>
        )}

        {!!data?.needs_setup.length && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            <b>{data.needs_setup.length} treat{data.needs_setup.length === 1 ? "y is" : "ies are"} not
            set up yet</b> — no statement is prepared for{" "}
            {data.needs_setup.length === 1 ? "it" : "them"} until{" "}
            {data.needs_setup.length === 1 ? "its" : "their"} setup is active:{" "}
            {data.needs_setup.map((t, i) => (
              <span key={t.contract_id}>
                {i > 0 && ", "}
                <Link className="linkish" to={`/reinsurance/setups/${t.contract_id}`}>{t.name}</Link>
                {t.setup_status === "pending_approval" && " (awaiting approval)"}
                {t.setup_status === "draft" && " (draft)"}
              </span>
            ))}
          </div>
        )}

        {/* ── Process: the reinsurance counterpart of Process Bordereau ──
            Treaty → period → (its own file, when the setup reads one) →
            Check, or Generate. Its setup decides what is read and written. */}
        <div className="card pad" style={{ marginBottom: 18 }}>
          <div className="fh" style={{ marginTop: 0 }}>
            Process a reinsurance bordereau
            <InfoTip text="Pick the treaty and the period. Check works the statement out and shows it without saving anything; Generate makes each reinsurer's statement, ready to send." />
          </div>
          <div className="row2">
            <div className="field">
              <label>Treaty</label>
              <select value={treaty} onChange={e => { setTreaty(e.target.value); setChecked(null); setOwnFile(null); }}>
                <option value="">Choose a treaty…</option>
                {(data?.treaties ?? []).map(t => (
                  <option key={t.contract_id} value={t.contract_id}>{t.name}</option>
                ))}
                {data && data.treaties.length === 0 && (
                  <option value="" disabled>No treaty is set up yet</option>
                )}
              </select>
            </div>
            <div className="field">
              <label>Reporting period</label>
              {!treaty ? (
                <input readOnly disabled value="Choose a treaty first" />
              ) : typedPeriod ? (
                freq === "monthly" ? (
                  <input type="month" value={ownPeriod}
                         onChange={e => { setOwnPeriod(e.target.value); setChecked(null); }} />
                ) : (
                  <input value={ownPeriod} placeholder={freq === "quarterly" ? "2027-Q1" : "2027"}
                         onChange={e => { setOwnPeriod(e.target.value); setChecked(null); }} />
                )
              ) : (
                <select value={period} disabled={!periods?.length}
                        onChange={e => { setPeriod(e.target.value); setChecked(null); }}>
                  {periods?.length === 0 && <option value="">No bordereaux in yet</option>}
                  {(periods ?? []).map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              )}
            </div>
          </div>

          {/* What this treaty's setup reads and writes — answered before
              anything is run, as Process Bordereau shows its template. */}
          {picked && (
            <div className="note" style={{ marginBottom: 14, display: "flex", gap: 8,
                 alignItems: "center", flexWrap: "wrap" }}>
              <span>Input <b>{ownInput ? (picked.input_file ?? "your input template")
                : "your brokers' bordereaux"}</b></span>
              <span className="sub">→</span>
              <span>Output <b>{picked.output === "uploaded" ? (picked.output_file ?? "your output template")
                : picked.output === "standard" ? "Lloyd's risk sheet" : "Kavachio's columns"}</b></span>
              <span className="tag-pill">{{ monthly: "Monthly", quarterly: "Quarterly",
                annually: "Yearly" }[freq]}</span>
              <Link className="linkish" style={{ marginLeft: "auto" }}
                    to={`/reinsurance/setups/${treaty}`}>Open setup</Link>
            </div>
          )}
          {treaty && !ownInput && !ownFile && periods?.length === 0 && (
            <div className="note warn" style={{ marginBottom: 14 }}>
              No bordereau has been received yet on the programmes this treaty
              protects, within its term.
            </div>
          )}

          {/* The input file — as Process Bordereau takes one. Required when the
              setup reads the carrier's own file; otherwise optional, and when
              given it is used for this period instead of the brokers'
              bordereaux. */}
          {treaty && (
            <div style={{ marginBottom: 14 }}>
              <FileDrop label="Input file" icon={<FileUp size={15} />}
                tone={ownInput ? "required" : "optional"} required={ownInput}
                file={ownFile}
                onPick={f => {
                  setOwnFile(f); setChecked(null);
                  // Carry the picked period over to the typed one.
                  if (f && !ownInput && !ownPeriod && period) setOwnPeriod(period);
                }}
                accept=".xlsx,.xlsm,.csv"
                hint={ownInput
                  ? `This period's ${picked?.input_file ? `copy of ${picked.input_file}` : "file"} — the same columns as your input template`
                  : "Optional — upload a file to use instead of your brokers' bordereaux for this period"} />
              {ownFile && !ownInput && (
                <div className="sub" style={{ marginTop: 6 }}>
                  This period&rsquo;s statement is worked out from <b>{ownFile.name}</b> instead
                  of the brokers&rsquo; bordereaux. Its columns are matched to theirs by heading.
                </div>
              )}
            </div>
          )}

          <div className="rowacts" style={{ justifyContent: "flex-end", marginTop: 0 }}>
            <button className="btn" type="button" disabled={!canRun || !!busy} onClick={check}
                    title="Work the statement out and show it — nothing is saved">
              {busy === "check" ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
              Check
            </button>
            <button className="btn pri" type="button" disabled={!canRun || !!busy} onClick={() => generate()}>
              {busy === "run" ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              Generate reinsurance BDX
            </button>
          </div>
        </div>

        {/* ── Check: the statement, not saved ── */}
        {checked && (
          <div className="card pad" style={{ marginBottom: 18 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
              <b>Check · {picked?.name} · {checked.period}</b>
              <span className="badge b-info"><span className="d" />Nothing saved</span>
              <button className="btn sm pri" type="button" style={{ marginLeft: "auto" }}
                      disabled={!!busy} onClick={() => generate()}>
                <Sparkles size={12} /> Generate it
              </button>
            </div>
            <div className="grid g-3" style={{ gap: 14, marginBottom: 10 }}>
              <div>
                {(["ceded_premium", "ceding_commission", "net_premium", "recovery"] as const)
                  .filter(k => checked.totals[k] != null).map(k => (
                    <div className="kv" key={k}>
                      <span className="k">{{ ceded_premium: "Ceded premium", ceding_commission: "Ceding commission",
                        net_premium: "Net premium due", recovery: "Recovery" }[k]}</span>
                      <span className="v">{money(checked.totals[k] as number, checked.currency)}</span>
                    </div>
                  ))}
                <div className="kv"><span className="k">Lines</span>
                  <span className="v">{checked.lines_total ?? 0}{checked.skipped ? ` · ${checked.skipped} not ceded` : ""}</span></div>
              </div>
              <div>
                <div className="sub" style={{ marginBottom: 4 }}>Per reinsurer</div>
                {checked.reinsurers.map((r, i) => (
                  <div className="kv" key={i}>
                    <span className="k">{r.name}</span>
                    <span className="v">{money((r.totals.net_premium ?? r.totals.recovery ?? r.totals.ceded_premium) as number, checked.currency)}</span>
                  </div>
                ))}
              </div>
              <div>
                <div className="sub" style={{ marginBottom: 4 }}>Read from</div>
                {checked.sources.length === 0 ? <span className="sub">nothing yet</span>
                  : checked.sources.map((x, i) => (
                    <div key={i} className="sub">{x.filename ?? x.programme} · {x.rows} rows</div>
                  ))}
              </div>
            </div>
            {checked.checks && checked.checks.length > 0 && (
              // Resolved on the statement's Exceptions — Generate saves it first.
              <TreatyChecks checks={checked.checks} busy={!!busy}
                            resolveLabel="Generate & resolve exceptions →"
                            onResolve={() => generate(true)} />
            )}
            {[...checked.notes, ...(checked.missing.length
              ? [`No bordereau for ${checked.period} yet from ${checked.missing.map(m => m.name).join(", ")}.`] : [])]
              .map((n, i) => <div key={i} className="note" style={{ marginBottom: 6 }}>{n}</div>)}
            {!!checked.table?.rows.length && (
              <div className="tbl-wrap" style={{ maxHeight: 300, overflow: "auto" }}>
                <table style={{ fontSize: 12 }}>
                  <thead><tr>{checked.table.headings.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
                  <tbody>
                    {checked.table.rows.slice(0, 15).map((r, i) => (
                      <tr key={i}>{r.map((x, j) => (
                        <td key={j} style={{ padding: "6px 8px" }}>
                          {x == null || x === "" ? "—" : typeof x === "number" ? x.toLocaleString() : String(x)}
                        </td>
                      ))}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        <div className="card">
          <div className="card-h" style={{ flexWrap: "wrap", gap: 10 }}>
            <FileSpreadsheet size={16} className="ci" />
            <h3>Statements</h3>
            <div className="seg sm" style={{ marginLeft: 8 }}>
              {FILTERS.map(f => (
                <button key={f.key} type="button" className={filter === f.key ? "on" : ""}
                        onClick={() => setFilter(f.key)}>
                  {f.label}
                  {f.key && data && (
                    <span className="n" style={{ marginLeft: 5 }}>
                      {data.counts[f.key as CededStatus | "overdue"] ?? 0}
                    </span>
                  )}
                </button>
              ))}
            </div>
            <label className="sub" style={{ marginLeft: "auto", display: "flex",
                   alignItems: "center", gap: 6 }}>
              <input type="checkbox" checked={showOld} onChange={e => setShowOld(e.target.checked)} />
              Show replaced versions
            </label>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Treaty</th><th>Period</th><th>Status</th>
                  <th>Ceded premium</th><th>Recovery</th><th>Reinsurers</th>
                  <th>Due</th><th />
                </tr>
              </thead>
              <tbody>
                {items.map(i => (
                  <tr key={i.id} style={{ cursor: "pointer" }}
                      onClick={() => nav(`/reinsurance/bordereaux/${i.id}`)}>
                    <td>
                      <b>{i.treaty}</b>
                      <div className="sub">{i.type_label ?? "—"}</div>
                    </td>
                    <td className="mono">
                      {i.period}
                      {i.version_no > 1 && <div className="sub">version {i.version_no}</div>}
                    </td>
                    <td>
                      <span className={`badge ${CEDED_STATUS[i.status].cls}`}>
                        <span className="d" />{CEDED_STATUS[i.status].label}
                      </span>
                      {i.is_correction && i.status === "draft" && (
                        <div className="sub">correction of a sent version</div>
                      )}
                      {i.missing_programmes > 0 && (
                        <div className="sub" style={{ color: "var(--p-warn-ink)" }}>
                          {i.missing_programmes} programme{i.missing_programmes === 1 ? "" : "s"} without a bordereau
                        </div>
                      )}
                    </td>
                    <td className="mono">{money(i.totals.ceded_premium, i.currency)}</td>
                    <td className="mono">{money(i.totals.recovery, i.currency)}</td>
                    <td>
                      {i.status === "draft" ? <span className="sub">{i.reinsurers}</span> : (
                        <span className="sub">
                          {i.agreed} of {i.reinsurers} agreed
                          {i.queried ? ` · ${i.queried} queried` : ""}
                        </span>
                      )}
                    </td>
                    <td className="sub">
                      {i.status === "draft" && i.due_on ? (
                        <span style={i.overdue ? { color: "var(--p-crit-ink)", fontWeight: 600 } : undefined}>
                          {i.overdue ? "Overdue · " : ""}{fmtDate(i.due_on)}
                        </span>
                      ) : i.sent_at ? `Sent ${fmtDate(i.sent_at)}` : "—"}
                    </td>
                    <td className="r">
                      <Link className="linkish" to={`/reinsurance/bordereaux/${i.id}`}
                            onClick={e => e.stopPropagation()}>Open</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data && items.length === 0 && (
              <div className="empty">
                {data.items.length === 0
                  ? "No statements yet. They appear here once bordereaux arrive on a "
                    + "programme a treaty protects."
                  : "No statements match this filter."}
              </div>
            )}
            {!data && !err && <div className="empty">Preparing…</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
