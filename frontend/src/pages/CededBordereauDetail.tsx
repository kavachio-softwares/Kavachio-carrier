/**
 * One reinsurance bordereau — one treaty, one period, one version.
 *
 * What it is made of (the inward bordereaux), what could not be ceded and why,
 * each reinsurer's statement (its share of every line), and Send. A sent
 * statement shows each reinsurer's answer — agreed, or its query — as it
 * comes in on the reinsurer's private link.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, Download, FileSpreadsheet, Loader2, RefreshCw, Send,
} from "lucide-react";
import { InfoTip } from "../components/InfoTip";
import { TreatyChecks } from "../components/treaty/TreatyRules";
import { fieldErrors } from "../api/contractRecord";
import {
  downloadStatement, getCeded, prepareCeded, sendCeded, type CededDetail,
  type CededTotals,
} from "../api/ceded";
import { fmtDate, fmtStamp } from "../utils/date";
import { CEDED_STATUS, money } from "./ReinsuranceBordereaux";

const TOTALS: { key: keyof CededTotals; label: string }[] = [
  { key: "ceded_premium", label: "Ceded premium" },
  { key: "ceding_commission", label: "Ceding commission" },
  { key: "net_premium", label: "Net premium due" },
  { key: "claims_paid", label: "Claims paid" },
  { key: "claims_outstanding", label: "Outstanding" },
  { key: "recovery", label: "Recovery" },
];

const ANSWER: Record<string, { label: string; cls: string }> = {
  pending: { label: "Not sent", cls: "b-mut" },
  sent: { label: "Waiting for answer", cls: "b-mut" },
  agreed: { label: "Agreed", cls: "b-ok" },
  queried: { label: "Queried", cls: "b-warn" },
};

export default function CededBordereauDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const cbId = Number(id);
  const [d, setD] = useState<CededDetail | null>(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [sendNote, setSendNote] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [lineLimit, setLineLimit] = useState(100);

  useEffect(() => {
    setD(null);
    getCeded(cbId).then(setD).catch(e => setErr(fieldErrors(e).message));
  }, [cbId]);

  const used = useMemo(() => (d?.columns ?? []).filter(c =>
    c.key !== "layer" || (d?.lines ?? []).some(l => l.layer)).filter(c =>
    (d?.lines ?? []).some(l => l[c.key] != null && l[c.key] !== "")), [d]);
  const shownTotals = useMemo(() => TOTALS.filter(t =>
    d?.totals?.[t.key] != null), [d]);

  /** Prepare it again — and, with `resolve`, open its exceptions. */
  async function rebuild(resolve = false) {
    if (!d) return;
    setBusy("prepare"); setErr(""); setNote("");
    try {
      const nd = await prepareCeded(d.contract_id, d.period);
      if (resolve) nav(`/reinsurance/bordereaux/${nd.id}/exceptions`);
      else if (nd.id !== d.id) nav(`/reinsurance/bordereaux/${nd.id}`);
      else { setD(nd); setNote("Prepared again from the latest bordereaux."); }
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  async function send() {
    setBusy("send"); setErr(""); setNote("");
    try {
      const nd = await sendCeded(cbId, sendNote.trim());
      setD(nd);
      setConfirm(false);
      const s = nd.sent;
      setNote(`Sent. ${s?.emails ?? 0} of ${s?.of ?? 0} emails went out, each with `
        + "that reinsurer's BDX file attached."
        + (s?.no_contact?.length
          ? ` ${s.no_contact.join(", ")} has no contact with an email — add one on `
            + "the reinsurer's page; their statement is still on their private link."
          : ""));
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  if (!d) {
    return (
      <div className="proto"><div className="view full">
        {err ? <div className="note warn">{err}</div> : <div className="sub">Loading…</div>}
      </div></div>
    );
  }

  const st = CEDED_STATUS[d.status];

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2 style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              {d.treaty} · {d.period}
              <span className={`badge ${st.cls}`}><span className="d" />{st.label}</span>
            </h2>
            <p>
              {[d.type_label, d.summary_headline,
                d.version_no > 1 ? `version ${d.version_no}` : null,
                d.status === "draft" && d.due_on
                  ? `${d.overdue ? "overdue — was due" : "due"} ${fmtDate(d.due_on)}` : null,
                d.sent_at ? `sent ${fmtStamp(d.sent_at)}` : null]
                .filter(Boolean).join(" · ")}
            </p>
          </div>
          <div className="actions">
            <Link to="/reinsurance/bordereaux" className="btn">
              <ArrowLeft size={14} /> All statements
            </Link>
            <Link to={`/contracts/${d.contract_id}?tab=treaty`} className="btn">Treaty</Link>
            {d.status === "draft" && (
              <button className="btn" type="button" disabled={!!busy} onClick={() => rebuild()}>
                {busy === "prepare" ? <Loader2 size={14} className="animate-spin" />
                                    : <RefreshCw size={14} />} Prepare again
              </button>
            )}
            {d.can_send && (
              <button className="btn pri" type="button" disabled={!!busy}
                      onClick={() => setConfirm(true)}>
                <Send size={14} /> Send to reinsurers
              </button>
            )}
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
        {note && <div className="note ok" style={{ marginBottom: 14 }}>{note}</div>}

        {d.stale && d.status !== "superseded" && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            <b>A bordereau for this period has changed since this was prepared.</b>{" "}
            {d.status === "draft"
              ? "Prepare again to take it in before you send."
              : "A corrected version is being prepared — it will appear in the list "
                + "as ready to send, showing each reinsurer the difference."}
            {d.status === "draft" && (
              <button className="btn sm" type="button" style={{ marginLeft: 10 }}
                      onClick={() => rebuild()} disabled={!!busy}>Prepare again</button>
            )}
          </div>
        )}
        {d.status === "superseded" && (
          <div className="note" style={{ marginBottom: 14 }}>
            A newer version of this statement has been sent.{" "}
            {d.versions[0] && d.versions[0].id !== d.id && (
              <Link className="linkish" to={`/reinsurance/bordereaux/${d.versions[0].id}`}>
                Open version {d.versions[0].version_no}
              </Link>
            )}
          </div>
        )}
        {d.missing.length > 0 && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            <AlertTriangle size={13} style={{ verticalAlign: "-2px" }} />{" "}
            <b>No bordereau for {d.period} yet from:</b>{" "}
            {d.missing.map(m => m.name).join(", ")}. Their business is not in this
            statement — if one arrives, it is prepared again.
          </div>
        )}

        {confirm && (
          <div className="note" style={{ marginBottom: 14 }}>
            <b>Send the BDX to {d.statements.length} reinsurer{d.statements.length === 1 ? "" : "s"}.</b>{" "}
            Each is emailed with its own BDX file attached — its share of every
            line, and nobody else&rsquo;s — and can agree it or raise a query on its
            private link.
            {d.is_correction && " This replaces the version sent before; each statement shows the difference."}
            {/* Who gets which file — said before Send is pressed. */}
            <div className="tbl-wrap" style={{ marginTop: 10, background: "var(--p-surface)", borderRadius: 8 }}>
              <table className="panel-tbl">
                <thead><tr><th>Reinsurer</th><th>Emailed to</th><th>BDX file attached</th></tr></thead>
                <tbody>
                  {(d.send_plan ?? []).map(sp => {
                    const st = d.statements.find(x => x.id === sp.statement_id);
                    return (
                      <tr key={sp.statement_id}>
                        <td><b>{sp.reinsurer}</b></td>
                        <td>
                          {sp.to.length ? sp.to.map(t => (
                            <div key={t.email}>{t.name} <span className="sub">{t.email}</span></div>
                          )) : (
                            <span className="sub" style={{ color: "var(--p-warn-ink)" }}>
                              No contact with an email — on its private link only
                            </span>
                          )}
                        </td>
                        <td>
                          {sp.attachment ? (
                            <button type="button" className="linkish"
                                    style={{ background: "none", border: 0, padding: 0, cursor: "pointer", textAlign: "left" }}
                                    onClick={() => st && downloadStatement(d.id, st)}>
                              <FileSpreadsheet size={12} style={{ verticalAlign: "-2px" }} /> {sp.attachment}
                            </button>
                          ) : "—"}
                          <div className="sub">{(sp.attachment_bytes / 1024).toFixed(1)} KB</div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {!!d.cc?.length && (
              <div className="sub" style={{ marginTop: 6 }}>Copied to {d.cc.join(", ")}</div>
            )}
            <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
              <textarea rows={2} value={sendNote} onChange={e => setSendNote(e.target.value)}
                        placeholder="A note for the reinsurers (optional)" />
            </div>
            <div className="rowacts">
              <button className="btn pri" type="button" disabled={!!busy} onClick={send}>
                {busy === "send" ? "Sending…" : "Send now"}
              </button>
              <button className="btn" type="button" onClick={() => setConfirm(false)}>Cancel</button>
            </div>
          </div>
        )}

        {/* The treaty's rules — the rows they caught are decided on
            Exceptions, as a broker's are; a Critical one stops Send until
            each of its rows is. */}
        {(() => {
          const decidable = (d.checks ?? []).filter(c => c.severity !== "untested" && c.items?.length);
          if (!decidable.length) {
            // Prepared before each row was kept: prepared again, it can be resolved.
            return d.checks && d.checks.length > 0
              ? <TreatyChecks checks={d.checks} busy={!!busy}
                  onResolve={d.status === "draft" ? () => rebuild(true) : undefined} />
              : null;
          }
          const total = decidable.reduce((n, c) => n + (c.items?.length ?? 0), 0);
          const open = decidable.reduce((n, c) => n + (c.open ?? 0), 0);
          const blocked = (!!d.send_blocked_by?.length || !!d.unapplied) && d.status === "draft";
          return (
            <div className={`note ${blocked ? "warn" : open ? "" : "ok"}`}
                 style={{ marginBottom: 14, display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
              <span style={{ flex: 1, minWidth: 260 }}>
                {blocked ? <><b>Not ready to send — {d.unapplied ? "run Fix & Validate" : "decide the exceptions first"}.</b>{" "}
                  {!!d.send_blocked_by?.length && <>Critical: {d.send_blocked_by.join("; ")}.</>}</>
                  : <b>{total} exception{total === 1 ? "" : "s"} from the treaty&rsquo;s rules</b>}{" "}
                {open ? `${open} still pending, ${total - open} resolved.`
                      : "All resolved — approved, fixed or dismissed."}
                {!!d.unapplied && d.status === "draft"
                  && ` ${d.unapplied} decision${d.unapplied === 1 ? "" : "s"} recorded but not in the statement yet — run Fix & Validate.`}
              </span>
              <Link className="btn sm" to={`/reinsurance/bordereaux/${d.id}/exceptions`}>
                {d.status === "draft" && open ? "Review exceptions →" : "View exceptions →"}
              </Link>
            </div>
          );
        })()}

        {/* totals */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
                      gap: 14, marginBottom: 18 }}>
          {shownTotals.length === 0 && (
            <div className="card pad"><div className="sub">Nothing ceded for this period.</div></div>
          )}
          {shownTotals.map(t => (
            <div key={t.key} className="card pad">
              <div className="sub">{t.label} · whole treaty</div>
              <div style={{ fontSize: 20, fontWeight: 700, marginTop: 4 }}>
                {money(d.totals[t.key] as number, d.currency)}
              </div>
            </div>
          ))}
        </div>

        {/* each reinsurer */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <FileSpreadsheet size={16} className="ci" />
            <h3>Each reinsurer&rsquo;s statement</h3>
            <InfoTip text="Every line × the reinsurer's share of that line's layer. This is what each one is sent." />
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Reinsurer</th>
                  {shownTotals.map(t => <th key={t.key}>{t.label}</th>)}
                  <th>Answer</th><th />
                </tr>
              </thead>
              <tbody>
                {d.statements.map(s => (
                  <tr key={s.id}>
                    <td>
                      <b>{s.reinsurer}</b>
                      <div className="sub">
                        {Object.entries(s.shares).map(([k, v]) => `${k} ${v}%`).join(" · ")}
                      </div>
                    </td>
                    {shownTotals.map(t => {
                      const cur = s.totals[t.key] as number | null | undefined;
                      const prev = s.previous_totals?.[t.key] as number | null | undefined;
                      const diff = s.previous_totals ? (cur ?? 0) - (prev ?? 0) : null;
                      return (
                        <td key={t.key} className="mono">
                          {money(cur, d.currency)}
                          {diff != null && Math.abs(diff) > 0.004 && (
                            <div className="sub" style={{ color: "var(--p-warn-ink)" }}>
                              {diff > 0 ? "+" : ""}{money(diff, null)} vs sent
                            </div>
                          )}
                        </td>
                      );
                    })}
                    <td>
                      <span className={`badge ${ANSWER[s.status].cls}`}>
                        <span className="d" />{ANSWER[s.status].label}
                      </span>
                      {s.answered_by && (
                        <div className="sub">{s.answered_by} · {fmtDate(s.answered_at)}</div>
                      )}
                      {s.answer_note && (
                        <div className="sub" style={{ color: "var(--p-warn-ink)", maxWidth: 260 }}>
                          &ldquo;{s.answer_note}&rdquo;
                        </div>
                      )}
                      {s.status !== "pending" && !s.sent_to.length && (
                        <div className="sub">No email on file — on its private link only</div>
                      )}
                    </td>
                    <td className="r">
                      <button className="btn sm" type="button"
                              onClick={() => downloadStatement(d.id, s)}>
                        <Download size={12} /> {s.filename?.toLowerCase().endsWith(".csv") ? "CSV" : "Excel"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {d.statements.length === 0 && (
              <div className="empty">No reinsurer holds a share of this treaty yet.</div>
            )}
          </div>
        </div>

        {d.notes.length > 0 && (
          <div className="note" style={{ marginBottom: 18 }}>
            {d.notes.map((n, i) => <div key={i}>{n}</div>)}
          </div>
        )}

        {/* where it came from */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <h3>Built from</h3>
            <span className="sub">the latest version of each bordereau for {d.period}</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Programme</th><th>Broker</th><th>File</th><th>Rows</th><th>Carries</th></tr>
              </thead>
              <tbody>
                {d.sources.map(s => (
                  <tr key={s.export_id}>
                    <td>{s.programme ?? "—"}</td>
                    <td>{s.broker ?? "—"}</td>
                    <td className="sub">{s.filename}{s.version_no && s.version_no > 1 ? ` · v${s.version_no}` : ""}</td>
                    <td className="mono">{s.rows}</td>
                    <td className="sub">
                      {[s.has_premium && "premium", s.has_sum_insured && "sum insured",
                        s.has_claims && "claims"].filter(Boolean).join(", ") || "nothing usable"}
                      {s.notes.map((n, i) => (
                        <div key={i} style={{ color: "var(--p-warn-ink)" }}>{n}</div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {d.sources.length === 0 && <div className="empty">No bordereau for this period yet.</div>}
          </div>
        </div>

        {d.skipped.length > 0 && (
          <details className="card" style={{ marginBottom: 18 }}>
            <summary className="card-h" style={{ cursor: "pointer" }}>
              <AlertTriangle size={15} className="ci" />
              <h3>Not ceded</h3>
              <span className="sub">{d.skipped.length} row{d.skipped.length === 1 ? "" : "s"}, and why</span>
            </summary>
            <div className="tbl-wrap">
              <table>
                <thead><tr><th>Programme</th><th>Policy</th><th>Why</th></tr></thead>
                <tbody>
                  {d.skipped.slice(0, 200).map((s, i) => (
                    <tr key={i}>
                      <td>{s.programme ?? "—"}</td>
                      <td className="mono">{s.policy ?? "—"}</td>
                      <td className="sub">{s.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        )}

        <div className="card">
          <div className="card-h">
            <h3>Lines</h3>
            <span className="sub">the whole treaty&rsquo;s share · {d.lines_total} line{d.lines_total === 1 ? "" : "s"}</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead><tr>{used.map(c => <th key={c.key}>{c.label}</th>)}</tr></thead>
              <tbody>
                {d.lines.slice(0, lineLimit).map((l, i) => (
                  <tr key={i}>
                    {used.map(c => (
                      <td key={c.key} className={c.kind === "text" ? undefined : "mono"}>
                        {l[c.key] == null ? "—"
                          : c.kind === "money" ? money(l[c.key] as number, null)
                          : c.kind === "pct" ? `${+(l[c.key] as number).toFixed(4)}%`
                          : String(l[c.key])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {d.lines.length === 0 && <div className="empty">No lines.</div>}
          </div>
          {d.lines.length > lineLimit && (
            <div style={{ padding: "10px 20px 14px" }}>
              <button className="btn sm" type="button" onClick={() => setLineLimit(n => n + 200)}>
                Show more ({d.lines.length - lineLimit} more)
              </button>
            </div>
          )}
          {d.lines_total > d.lines.length && (
            <div className="sub" style={{ padding: "0 20px 14px" }}>
              Showing the first {d.lines.length}. Every line is in each reinsurer&rsquo;s Excel file.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
