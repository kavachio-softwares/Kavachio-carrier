import { useEffect, useState } from "react";
import { useParams, useSearchParams, useNavigate, useLocation } from "react-router-dom";
import {
  getUploadExceptions, getDownloadExceptions, runValidation, rerenderExport,
  type UploadExceptionsResponse,
} from "../api/validation";
import { groupByRule, exportCSV, buildReverseSpec, tallyDecisions } from "../components/ExceptionCards";
import RuleExplanationBlock, { hasExplanation } from "../components/RuleExplanation";
import { downloadFile } from "../api/client";
import { isBrokerSeat } from "../auth";
import { CheckCircle2 } from "lucide-react";
import { LoadingOverlay } from "../components/Busy";
import BdxInlineReview from "../components/BdxInlineReview";
import Modal from "../components/ui/Modal";

/** Fix & re-run reloads the page — the success toast crosses the reload here. */
const RERUN_TOAST_KEY = "kavachio.rerun-toast";

const SEV_SPINE: Record<string, string> = { critical: "crit", warning: "warn", info: "info" };
const SEV_BADGE: Record<string, string> = { critical: "b-crit", warning: "b-warn", info: "b-info" };
const SEV_LABEL: Record<string, string> = { critical: "Critical", warning: "Warning", info: "Info" };

export default function UploadExceptions() {
  const { uploadId = "" } = useParams();
  const [params] = useSearchParams();
  const downloadId = params.get("download");   // output-stage (per-download) view
  // Carries the sidebar-highlight context (Dashboard vs Process Bordereau)
  // through to the per-rule review sub-screen.
  const fromParam = params.get("from");
  const navigate = useNavigate();
  // Set when the reviewer comes back from a rule's Decisions screen having
  // recorded decisions — we prompt them that Approved/Fixed values are NOT in
  // the output until Fix & Validate runs. Read once, then cleared from history
  // so a refresh / back-forward doesn't re-open it.
  const location = useLocation();
  const reviewed = (location.state as any)?.reviewed as
    | { ruleName?: string; approve: number; fix: number; dismiss: number; pending: number }
    | undefined;
  const [reviewPrompt, setReviewPrompt] = useState(reviewed ?? null);
  useEffect(() => {
    if (!reviewed) return;
    setReviewPrompt(reviewed);
    navigate(location.pathname + location.search, { replace: true, state: null });
  }, [reviewed]);
  const [data, setData] = useState<UploadExceptionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [revalidating, setRevalidating] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [doneOpen, setDoneOpen] = useState(false);
  const [search, setSearch] = useState("");
  // "View BDX" — the generated workbook rendered IN the page (every sheet, every
  // row, failed cells highlighted and actionable in place). The modal viewer is
  // still used elsewhere; here the review happens inline.
  const [bdxOpen, setBdxOpen] = useState(false);

  // Where "back" goes. This used to be a hardcoded /direct, which is the
  // CARRIER's Process Bordereau — a broker reviewing their own run was sent to
  // a route access.ts refuses them, and bounced to their dashboard.
  //
  // `from` is already carried through every link into this screen (it is what
  // keeps the sidebar highlight honest), so it is the same answer, and the
  // label follows the destination rather than always claiming "Process
  // Bordereau". The role check is the fallback for a deep link or a bookmark
  // that carries no `from` at all.
  const back = (() => {
    if (fromParam === "broker") return { to: "/broker/bordereau", label: "Process Bordereau" };
    if (fromParam === "home") return { to: "/home", label: "Dashboard" };
    if (fromParam === "direct") return { to: "/direct", label: "Process Bordereau" };
    return isBrokerSeat()
      ? { to: "/broker/bordereau", label: "Process Bordereau" }
      : { to: "/direct", label: "Process Bordereau" };
  })();

  // Success confirmation — a "Done" modal the reviewer must acknowledge,
  // confirming a Fix & re-run finished (which previously completed silently
  // with no confirmation shown).
  function flash(text: string) {
    setMsg(text);
    setDoneOpen(true);
  }

  async function load() {
    setLoading(true); setErr(null);
    try {
      if (downloadId) {
        // Output-stage: the generated download's exceptions, mapped to the
        // shared shape so the same UI renders them.
        const exceptions = await getDownloadExceptions(downloadId);
        setData({
          success: true, uploadId, validated: true, run: null, exceptions,
          source_file: null, mga: null, mapper_id: null,
          has_source_blob: false, mapper_spec: null,
        });
      } else {
        setData(await getUploadExceptions(uploadId));
      }
    }
    catch (e: any) { setErr(e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? "Failed to load."); }
    finally { setLoading(false); }
  }
  useEffect(() => { load(); }, [uploadId, downloadId]);

  // Show the Fix & re-run confirmation left behind by the pre-reload page.
  useEffect(() => {
    const t = sessionStorage.getItem(RERUN_TOAST_KEY);
    if (t) { sessionStorage.removeItem(RERUN_TOAST_KEY); flash(t); }
  }, []);

  async function revalidate() {
    setRevalidating(true); setErr(null); setMsg(null);
    try {
      await runValidation({ uploadId: Number(uploadId), stage: "input" });
      await load();
      flash("Re-validation complete — exceptions refreshed.");
    }
    catch (e: any) { setErr(e?.response?.data?.message ?? e?.message ?? "Validation failed."); }
    finally { setRevalidating(false); }
  }

  // Direct-lane: re-generate the output BDX with saved Fix/Approve corrections
  // applied, then FULLY reload the page — a soft state refresh isn't enough
  // because the inline BDX view caches the rendered grid per export id, and an
  // in-place re-render keeps the same id, so stale rows/highlights would linger.
  // The success toast survives the reload via sessionStorage (shown on mount).
  async function regenerate() {
    if (!downloadId) return;
    setRegenerating(true); setErr(null); setMsg(null);
    try {
      const res = await rerenderExport(downloadId);
      sessionStorage.setItem(RERUN_TOAST_KEY,
        "Your corrections have been applied.");
      if (res?.export_id && String(res.export_id) !== String(downloadId)) {
        const qs = new URLSearchParams();
        qs.set("download", String(res.export_id));
        if (fromParam) qs.set("from", fromParam);
        window.location.assign(`/uploads/${uploadId}/exceptions?${qs.toString()}`);
      } else {
        window.location.reload();
      }
      // keep the overlay up until the browser actually reloads
    }
    catch (e: any) {
      setErr(e?.response?.data?.detail ?? e?.message ?? "Re-generate failed.");
      setRegenerating(false);
    }
  }

  const run        = data?.run ?? null;
  const exceptions = data?.exceptions ?? [];
  const allGroups  = groupByRule(exceptions);
  const revSpec    = buildReverseSpec(data?.mapper_spec);
  const filteredGroups = groupByRule(
    search.trim()
      ? exceptions.filter(e => {
          const q = search.toLowerCase();
          return (
            (e.policy_number ?? "").toLowerCase().includes(q) ||
            (e.field_path ?? "").toLowerCase().includes(q) ||
            (e.rule_name ?? "").toLowerCase().includes(q) ||
            (e.contract_clause_text ?? "").toLowerCase().includes(q)
          );
        })
      : exceptions
  );

  // Tiles — Total / Critical / Warning / Resolved / Open, from the decision tallies.
  const critical = run?.critical_count ?? exceptions.filter(e => e.severity === "critical").length;
  const warning  = run?.warning_count  ?? exceptions.filter(e => e.severity === "warning").length;
  const tallies = allGroups.map(g => tallyDecisions(g.items));
  const resolved = tallies.reduce((a, t) => a + t.approve + t.fix + t.dismiss + t.reject, 0);
  const open = tallies.reduce((a, t) => a + t.pending, 0);

  const ruleRoute = (g: { ruleId: number | null; ruleKey: string; checkKind?: string | null }) => {
    const qs = new URLSearchParams();
    if (downloadId) qs.set("download", downloadId);
    if (fromParam) qs.set("from", fromParam);
    const q = qs.toString();
    // encodeURIComponent: rule-less groups key on the FIELD NAME (`no_rule_…`),
    // which can carry characters that are invalid raw in a path segment — a
    // literal "%" (e.g. "100% Terrorism Written Premium") produces a malformed
    // percent-sequence that react-router throws URIError on. useParams()
    // decodes, so RuleReview still receives the original key.
    // One rule can now head more than one card (its own violations, and rows it
    // flagged for a different reason — see RuleGroup.checkKind), so the rule id
    // alone no longer identifies a card. Those route by the group KEY; every
    // other link keeps the rule-id URL it has always had.
    const id = g.checkKind ? g.ruleKey : (g.ruleId ?? g.ruleKey);
    return `/uploads/${uploadId}/exceptions/rule/${encodeURIComponent(String(id))}${q ? `?${q}` : ""}`;
  };

  return (
    <div className="proto">
      {revalidating && (
        <LoadingOverlay label="Validating — checking every row against the contract rules. This can take a few minutes…" />
      )}
      {regenerating && (
        <LoadingOverlay label="Re-generating the output — applying your corrections. This can take a few minutes…" />
      )}
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Exception Triage{" "}
              <span className="tag-pill">{downloadId ? `EXPORT-${downloadId}` : `UPLOAD-${uploadId}`}</span>
            </h2>
            <p>
              {data?.source_file ? `${data.source_file} — ` : ""}
              {exceptions.length} exception{exceptions.length === 1 ? "" : "s"} across{" "}
              {allGroups.length} rule{allGroups.length === 1 ? "" : "s"}.
            </p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => navigate(back.to)}>
              ← {back.label}
            </button>
            {allGroups.length > 0 && (
              <button className="btn" onClick={() => exportCSV(allGroups, downloadId ? `export_${downloadId}` : `upload_${uploadId}`, revSpec)}>
                Export CSV
              </button>
            )}
            {downloadId && (
              <button className={`btn ${bdxOpen ? "pri" : ""}`} onClick={() => setBdxOpen(o => !o)}>
                {bdxOpen ? "Hide BDX" : "View BDX"}
              </button>
            )}
            {downloadId && (
              <button className="btn"
                onClick={() => downloadFile(`/export/downloads/${downloadId}/file`)
                  .catch(() => alert("We couldn't download that file — please try again."))}>
                Download BDX
              </button>
            )}
            {downloadId ? (
              <button className="btn pri" onClick={regenerate} disabled={regenerating || loading || !data}>
                Fix &amp; Validate
              </button>
            ) : (
              <button className="btn pri" onClick={revalidate} disabled={revalidating || loading || !data}>
                Fix &amp; Validate
              </button>
            )}
          </div>
        </div>

        {/* Post-review prompt — decisions are recorded but NOT yet written to the
            output; spell out what was decided and what still has to happen.
            Held back until every loader has settled: rendering it while the page
            is still fetching made it flash, get covered by the overlay, then
            reappear. `loading` starts true, so it never shows before the data. */}
        {reviewPrompt && !loading && !revalidating && !regenerating && (() => {
          const r = reviewPrompt;
          const applied = r.approve + r.fix;
          const parts = [
            r.approve ? `${r.approve} approved` : null,
            r.fix ? `${r.fix} fixed` : null,
            r.dismiss ? `${r.dismiss} dismissed` : null,
          ].filter(Boolean).join(" · ");
          return (
            <div className="proto-modal-overlay" onClick={() => setReviewPrompt(null)}>
              <div className="proto-modal" onClick={e => e.stopPropagation()}>
                <div className="m-h">
                  <h3>Decisions Recorded{r.ruleName ? ` — ${r.ruleName}` : ""}</h3>
                  <button className="x" onClick={() => setReviewPrompt(null)} aria-label="Close">×</button>
                </div>
                <div className="m-b">
                  <div style={{ marginBottom: 10 }}><b>{parts}</b></div>
                  {applied > 0 ? (
                    <>
                      Your approved and fixed values are saved, but they are <b>not in the output yet</b>.
                      Run <b>Fix &amp; Validate</b> to write them into the bordereau and re-check the
                      remaining exceptions.
                    </>
                  ) : (
                    <>
                      Dismissed rows keep their existing values and pass through unchanged. Run{" "}
                      <b>Fix &amp; Validate</b> to re-check the remaining exceptions.
                    </>
                  )}
                </div>
                <div className="m-f">
                  <button className="btn" onClick={() => setReviewPrompt(null)}>Later</button>
                  <button className="btn pri"
                    disabled={revalidating || regenerating || loading || !data}
                    onClick={() => { setReviewPrompt(null); (downloadId ? regenerate : revalidate)(); }}>
                    Fix &amp; Validate
                  </button>
                </div>
              </div>
            </div>
          );
        })()}

        {/* success confirmation — a "Done" modal instead of a silent/transient toast */}
        <Modal open={doneOpen} title="Done" size="sm"
          onClose={() => setDoneOpen(false)}
          footer={
            <div className="flex justify-center w-full">
              <button className="btn pri" style={{ minWidth: 96, display:"flex", justifyContent:"center" }} onClick={() => setDoneOpen(false)}>
                OK
              </button>
            </div>
          }>
          <div className="flex flex-col items-center text-center gap-3 py-2">
            <div className="flex items-center justify-center w-14 h-14 rounded-full bg-emerald-100">
              <CheckCircle2 size={30} className="text-emerald-600" strokeWidth={2.25} />
            </div>
            <p className="text-[15px] font-medium text-ink leading-relaxed">{msg}</p>
          </div>
        </Modal>
        {err && <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>}

        {/* In-page BDX review — the whole generated workbook with each flagged
            cell expandable (error + rule + recommendation) and decidable in
            place. Additional to the rule-card flow below, which is unchanged. */}
        {bdxOpen && downloadId && data && (
          <BdxInlineReview
            exportId={downloadId}
            exceptions={exceptions}
            onSaved={n => {
              load();
              if (n > 0) flash(`Saved ${n} decision${n === 1 ? "" : "s"} — click Fix & Validate to write them into the output.`);
            }}
            onClose={() => setBdxOpen(false)}
          />
        )}

        {loading ? null : !data ? (
          err ? null : (
            <div className="card pad">
              <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                {downloadId ? "This export" : "This upload"} could not be found.
              </p>
            </div>
          )
        ) : !data.validated ? (
          <div className="card pad">
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              This upload hasn't been validated yet. Run validation to generate exception reports.
            </p>
            <button className="btn pri" onClick={revalidate} disabled={revalidating}>
              Run validation
            </button>
          </div>
        ) : exceptions.length === 0 ? (
          <div className="card">
            <div className="empty" style={{ color: "var(--p-ok-ink)" }}>
              ✓ No exceptions — {downloadId ? "the generated output is clean." : "upload passed validation."}
            </div>
          </div>
        ) : (
          <>
            {/* ── tiles ── */}
            <div className="tiles five" style={{ marginBottom: 18 }}>
              <div className="tile"><div className="k">Total</div><div className="v">{exceptions.length}</div></div>
              <div className="tile"><div className="k">Critical</div><div className="v" style={{ color: "var(--p-crit)" }}>{critical}</div></div>
              <div className="tile"><div className="k">Warning</div><div className="v" style={{ color: "var(--p-warn)" }}>{warning}</div></div>
              <div className="tile"><div className="k">Resolved</div><div className="v" style={{ color: "var(--p-ok)" }}>{resolved}</div></div>
              <div className="tile"><div className="k">Open</div><div className="v">{open}</div></div>
            </div>

            {/* ── how to resolve ── */}
            <div className="card" style={{ marginTop: 18, marginBottom: 18 }}>
              <div className="card-h"><h3>How to resolve</h3></div>
              <div className="grid g-3" style={{ padding: "16px 20px" }}>
                {[
                  { n: 1, cls: "b-crit", title: "Open a rule to review",
                    body: "Each rule above shows the contract clause it came from and how many policies are affected. Click Review to open its table — one row per affected policy with its actual value and the recommendation." },
                  { n: 2, cls: "b-warn", title: "Decide each policy",
                    body: "For every row pick a decision — Approve the recommendation, Fix with a corrected value, or Dismiss (keep as-is). Select rows to apply a bulk decision, then Save decisions." },
                  { n: 3, cls: "b-info", title: downloadId ? "Re-generate the output" : "Re-upload & re-validate",
                    body: downloadId
                      ? "After saving, click Fix & Validate to produce a corrected BDX — Fixed & Approved values are written in, Dismissed rows keep their value, and decisions carry over."
                      : "Upload the corrected file, then Fix & Validate to re-validate and confirm the exceptions are resolved." },
                ].map(s => (
                  <div key={s.n} style={{ display: "flex", gap: 10 }}>
                    <span className={`badge ${s.cls}`}
                      style={{ width: 24, height: 24, justifyContent: "center", borderRadius: "50%", padding: 0, flex: "0 0 auto" }}>
                      {s.n}
                    </span>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: 13 }}>{s.title}</div>
                      <div className="muted" style={{ fontSize: 12, marginTop: 2, lineHeight: 1.5 }}>{s.body}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* ── search ── */}
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
              <div className="search" style={{ minWidth: 280 }}>
                <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m20 20-3-3" /></svg>
                <input value={search} onChange={e => setSearch(e.target.value)}
                  placeholder="Search rule, field, policy, clause…" />
              </div>
              {search && <span className="linkish" onClick={() => setSearch("")}>Clear</span>}
              <span className="muted" style={{ marginLeft: "auto", fontSize: 12 }}>
                {filteredGroups.length} rule{filteredGroups.length === 1 ? "" : "s"} shown
              </span>
            </div>

            {/* ── rule cards ── */}
            <div className="grid">
              {filteredGroups.map(g => {
                const t = tallyDecisions(g.items);
                // Which sheet(s) this rule fired on — with per-schedule scoping an
                // exception on a mapped sheet comes only from that sheet's contract.
                const sheets = Array.from(new Set(
                  g.items.map(it => it.source_sheet).filter(Boolean)
                )) as string[];
                return (
                  <div key={g.ruleKey}
                    className={`card spine ${SEV_SPINE[g.severity] ?? "info"} click`}
                    onClick={() => navigate(ruleRoute(g))}>
                    <div className="card-h">
                      <h3>{g.ruleName}</h3>
                      <span className="sub mono">{g.ruleId != null ? `RULE-${g.ruleId}` : g.ruleKey}</span>
                      <div className="right">
                        <span className={`badge ${SEV_BADGE[g.severity] ?? "b-info"}`}>
                          <span className="d" />{SEV_LABEL[g.severity] ?? g.severity} · {g.count} {g.count === 1 ? "policy" : "policies"}
                        </span>
                        <span className="linkish">Review →</span>
                      </div>
                    </div>
                    <div style={{ padding: "0 20px 14px" }}>
                      {(g.contractFilename || sheets.length > 0) && (
                        <div style={{ color: "var(--p-muted)", fontSize: 11.5, marginBottom: 8,
                                      display: "flex", gap: 10, flexWrap: "wrap" }}>
                          {g.contractFilename && (
                            <span>Enforced by <strong>{g.contractFilename}</strong>
                              {g.clausePage ? ` · p.${g.clausePage}` : ""}</span>
                          )}
                          {sheets.length > 0 && (
                            <span>Sheet{sheets.length > 1 ? "s" : ""}: <strong>{sheets.join(", ")}</strong></span>
                          )}
                        </div>
                      )}
                      {/* What the rule requires, in plain English, with the raw
                          clause demoted into a collapsible. Falls back to the
                          old error message for rules the backend can't explain. */}
                      {hasExplanation(g.explanation) || g.contractClause ? (
                        <div style={{ marginBottom: 8 }}>
                          <RuleExplanationBlock
                            explanation={g.explanation}
                            clauseFallback={g.contractClause}
                            clausePage={g.clausePage}
                          />
                        </div>
                      ) : (
                        g.errorMessage && (
                          <div style={{ color: "var(--p-muted)", fontSize: 12.5, marginBottom: 8 }}>
                            {g.errorMessage}
                          </div>
                        )
                      )}
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        <span className="badge b-ok">Approved {t.approve}</span>
                        <span className="badge b-info">Fixed {t.fix}</span>
                        <span className="badge b-warn">Dismissed {t.dismiss}</span>
                        <span className="badge b-mut">Pending {t.pending}</span>
                      </div>
                    </div>
                  </div>
                );
              })}
              {filteredGroups.length === 0 && (
                <div className="card"><div className="empty">No rules match "{search}".</div></div>
              )}
            </div>

            
            
          </>
        )}
      </div>
    </div>
  );
}
