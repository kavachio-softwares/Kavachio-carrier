/**
 * The secure correction page — what the link in a broker's result email opens.
 *
 * ONE submission, no login, no menu, no upload. The broker:
 *   1. confirms who they are with a one-time code sent to the same address;
 *   2. reviews the file in the portal's own components — the five tiles, then
 *      two tabs: the BDX sheet with every flagged cell highlighted
 *      (BdxInlineReview) and the issues grouped by rule (the rule cards);
 *   3. clicks a highlighted cell to Approve, Fix or Dismiss it (saved at once,
 *      exactly as in the portal);
 *   4. submits — Kavachio makes the NEXT version of the submission from those
 *      decisions and checks it again. Nothing already sent is edited.
 *
 * A file needing many changes is corrected in the broker's own system and sent
 * again through their usual channel; it is matched to this submission by itself.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, KeyRound, ListChecks, Loader2, Lock, Mail, Send, ShieldCheck } from "lucide-react";
import KavachioLogo from "../components/KavachioLogo";
import BdxInlineReview, { type ReviewSource } from "../components/BdxInlineReview";
import RuleExplanationBlock from "../components/RuleExplanation";
import { InfoTip } from "../components/InfoTip";
import { decisionKindOf, groupByRule, tallyDecisions } from "../components/ExceptionCards";
import type { StoredException } from "../api/validation";
import {
  decideViaLink, errorStatus, errorText, loadExport, loadSubmission, openLink, sendCode,
  streamPathFor, submitVersion, validateLink, verifyCode,
  type FixExport, type FixGate, type FixSubmission, type FixValidation,
} from "../api/fixLink";

const SEV_SPINE: Record<string, string> = { critical: "crit", warning: "warn", info: "info" };
const SEV_BADGE: Record<string, string> = { critical: "b-crit", warning: "b-warn", info: "b-info" };
const SEV_LABEL: Record<string, string> = { critical: "Critical", warning: "Warning", info: "Info" };

type Tab = "sheet" | "rules";

// A version the broker can review and decide on (anything else is read-only).
const REVIEWABLE = ["with_exceptions", "ready", "held_at_deadline"];

/** What decides how the grid looks: each exception's identity and decision.
 *  A refresh that changes none of it keeps the export object it already has,
 *  so nothing downstream re-indexes or re-renders. */
function exportSignature(x: FixExport): string {
  return `${x.id}|` + x.exceptions.map((e) =>
    `${e.exception_id}:${e.status ?? ""}:${e.resolution_note ?? ""}:${e.decided_at ?? ""}`).join(",");
}

function sessionKey(token: string) {
  return `fix-session:${token.slice(0, 16)}`;
}
function readSession(token: string): string | null {
  try { return sessionStorage.getItem(sessionKey(token)); } catch { return null; }
}
function writeSession(token: string, v: string | null) {
  try {
    if (v) sessionStorage.setItem(sessionKey(token), v);
    else sessionStorage.removeItem(sessionKey(token));
  } catch { /* private window: the session just lasts this page */ }
}

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export default function FixLink() {
  const { token = "" } = useParams();
  const [gate, setGate] = useState<FixGate | null>(null);
  const [session, setSession] = useState<string | null>(() => readSession(token));
  const [doc, setDoc] = useState<FixSubmission | null>(null);
  const [exp, setExp] = useState<FixExport | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [codeSentTo, setCodeSentTo] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("sheet");
  // The last validation of the saved decisions. Cleared by any new decision:
  // what is submitted must be what was checked.
  const [check, setCheck] = useState<FixValidation | null>(null);
  const [search, setSearch] = useState("");
  // Which cells the sheet should jump to (a rule card was clicked).
  const [focus, setFocus] = useState<{ exceptionIds: number[]; nonce: number } | null>(null);

  // What the page shows before the code: who it is for, nothing sensitive.
  useEffect(() => {
    openLink(token).then(setGate).catch((e) => setFatal(errorText(e)));
  }, [token]);

  // Several saves in a row each ask for a refresh; only the newest answer may
  // land, or a slow older one would put stale decisions back on screen.
  const refreshSeq = useRef(0);
  const applyExport = useCallback((next: FixExport | null) => {
    setExp((prev) => (prev && next && exportSignature(prev) === exportSignature(next) ? prev : next));
  }, []);

  const refresh = useCallback(async (s: string) => {
    const seq = ++refreshSeq.current;
    try {
      const d = await loadSubmission(token, s);
      const x = REVIEWABLE.includes(d.status) ? await loadExport(token, s) : null;
      if (seq !== refreshSeq.current) return;
      setDoc(d);
      applyExport(x);
    } catch (e) {
      if (seq !== refreshSeq.current) return;
      if (errorStatus(e) === 401) { writeSession(token, null); setSession(null); }
      else if (errorStatus(e) === 404) setFatal(errorText(e));
      else setErr(errorText(e));
    }
  }, [token, applyExport]);

  // Once per session — not on every change to `gate`'s identity.
  const gateReady = gate !== null;
  useEffect(() => { if (session && gateReady) void refresh(session); }, [session, gateReady, refresh]);

  const onSendCode = async () => {
    setErr(null); setBusy("code");
    try { setCodeSentTo((await sendCode(token)).sent_to); }
    catch (e) { setErr(errorText(e)); }
    finally { setBusy(null); }
  };

  const onVerify = async () => {
    setErr(null); setBusy("verify");
    try {
      const { session: s } = await verifyCode(token, code.trim());
      writeSession(token, s); setSession(s); setCode("");
    } catch (e) { setErr(errorText(e)); }
    finally { setBusy(null); }
  };

  const onSubmit = async () => {
    if (!session) return;
    setErr(null); setMsg(null); setBusy("submit");
    ++refreshSeq.current;
    try {
      // The corrected data is SENT, like a file on the broker's channel: it is
      // received, checked and processed as the next version. The page follows
      // it (below) and opens the new version once its result is ready.
      const d = await submitVersion(token, session);
      setDoc(d);
      setExp(null);
      setCheck(null);
      setTab("sheet");
    } catch (e) { setErr(errorText(e)); }
    finally { setBusy(null); }
  };

  // Follow a version that is being processed, then open it.
  const processing = doc?.status === "processing";
  useEffect(() => {
    if (!processing || !session) return;
    const started = Date.now();
    const id = window.setInterval(async () => {
      try {
        const d = await loadSubmission(token, session);
        if (d.status !== "processing" || Date.now() - started > 5 * 60_000) {
          window.clearInterval(id);
          await refresh(session);
          setMsg(d.status.startsWith("delivered")
            ? `Version ${d.version} delivered to ${d.carrier ?? "the carrier"}.`
            : d.status === "with_exceptions"
              ? `Version ${d.version} checked. ${plural(d.progress.remaining, "exception")} still open.`
              : null);
        }
      } catch { /* keep following; a 401 is handled by refresh */ }
    }, 3000);
    return () => window.clearInterval(id);
  }, [processing, session, token, refresh]);

  // The grid reads and saves through the link, not the portal login.
  const source: ReviewSource | undefined = useMemo(() => session ? {
    streamPath: streamPathFor(token),
    headers: { "X-Fix-Session": session },
    save: async (decisions) => {
      try { return await decideViaLink(token, session, decisions); }
      catch (e) {
        // The 30-minute session ended mid-review: back to the code screen.
        if (errorStatus(e) === 401) { writeSession(token, null); setSession(null); }
        throw e;
      }
    },
  } : undefined, [token, session]);

  // Counted the same way as the portal's exception screen (UploadExceptions).
  const isNotice = (e: { error_class?: string | null; rule_id: number | null }) =>
    e.error_class === "not_checked" || (e.error_class === "not_validated" && e.rule_id == null);
  const exceptions = useMemo(() => (exp?.exceptions ?? []).filter((e) => !isNotice(e)), [exp]);
  const groups = useMemo(() => groupByRule(exceptions), [exceptions]);
  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? groupByRule(exceptions.filter((e) =>
      (e.policy_number ?? "").toLowerCase().includes(q) ||
      (e.field_path ?? "").toLowerCase().includes(q) ||
      (e.rule_name ?? "").toLowerCase().includes(q) ||
      (e.contract_clause_text ?? "").toLowerCase().includes(q))) : groups;
  }, [exceptions, groups, search]);
  const tallies = groups.map((g) => tallyDecisions(g.items));
  const critical = exceptions.filter((e) => e.severity === "critical").length;
  const warning = exceptions.filter((e) => e.severity === "warning").length;
  const resolved = tallies.reduce((a, t) => a + t.approve + t.fix + t.dismiss + t.reject, 0);
  const openCount = tallies.reduce((a, t) => a + t.pending, 0);

  const onSaved = useCallback((n: number) => {
    if (session) void refresh(session);
    if (n > 0) { setMsg(`${plural(n, "decision")} saved.`); setCheck(null); }
  }, [session, refresh]);

  const onValidate = async () => {
    if (!session) return;
    setErr(null); setMsg(null); setBusy("validate");
    try {
      setCheck(await validateLink(token, session));
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      if (errorStatus(e) === 401) { writeSession(token, null); setSession(null); }
      setErr(errorText(e));
    } finally { setBusy(null); }
  };

  // The sheet is built ONCE per version and stays mounted while the Issues tab
  // is open (hidden, not removed), so the rows it has streamed stay in memory:
  // switching back is instant and keeps the scroll, sheet and filter. It
  // re-renders only when the exceptions really change (see exportSignature)
  // and re-streams only for a new version (the key). Typing in the rule search
  // or showing a message does not touch it.
  const exportId = exp?.id;
  const grid = useMemo(() => exportId == null ? null : (
    <BdxInlineReview
      key={exportId}
      exportId={String(exportId)}
      exceptions={exceptions}
      source={source}
      autoScroll={false}
      hideToolbar
      bulkFix
      focus={focus}
      hint={<>{exp?.filename ?? "Output"} · select a highlighted cell to resolve it</>}
      footerNote={<>Decisions are saved instantly and applied when you submit.</>}
      onSaved={onSaved}
    />
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ), [exportId, exceptions, source, onSaved, focus]);

  // A rule card opens the sheet at that rule's first cell still to resolve.
  const openRule = (items: StoredException[]) => {
    const pending = items.filter((e) => !decisionKindOf(e));
    setFocus({ exceptionIds: (pending.length ? pending : items).map((e) => e.exception_id), nonce: Date.now() });
    setTab("sheet");
  };

  if (fatal) {
    return (
      <Splash>
        <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-full bg-[#FDECEC] text-danger">
          <Lock size={24} />
        </div>
        <h1 className="mb-2 text-lg font-semibold text-ink">Link unavailable</h1>
        <p className="text-sm leading-relaxed text-ink-muted">{fatal}</p>
      </Splash>
    );
  }
  if (!gate) return <Splash><Loader2 className="mx-auto animate-spin" size={22} /></Splash>;

  const header = (
    <header style={{ position: "sticky", top: 0, zIndex: 30, background: "rgba(255,255,255,.96)",
                     borderBottom: "1px solid var(--p-border)", backdropFilter: "blur(6px)" }}>
      <div style={{ maxWidth: 1280, margin: "0 auto", padding: "12px 24px",
                    display: "flex", alignItems: "center", gap: 14 }}>
        <KavachioLogo className="h-7 w-7 shrink-0" />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontWeight: 600, fontSize: 14.5 }} className="truncate">
            {doc?.file ?? gate.file}
            {doc?.version ? <span style={{ color: "var(--p-muted)", fontWeight: 500 }}> · Version {doc.version}</span> : null}
          </div>
          <div className="truncate" style={{ fontSize: 12, color: "var(--p-faint)" }}>
            {[gate.programme, gate.period, gate.carrier].filter(Boolean).join(" · ")}
          </div>
        </div>
        <StatusBadge status={doc?.status ?? gate.status} text={doc?.status_text ?? gate.status_text} />
      </div>
    </header>
  );

  // ── one-time code ───────────────────────────────────────────────────────
  if (!session || !doc) {
    return (
      <div className="proto" style={{ minHeight: "100vh" }}>
        {header}
        <div style={{ maxWidth: 420, margin: "72px auto 0", padding: "0 16px" }}>
          <div className="card" style={{ padding: 28 }}>
            <div style={{ width: 44, height: 44, borderRadius: 12, display: "grid", placeItems: "center",
                          background: "var(--p-primary-soft)", color: "var(--p-primary)", marginBottom: 14 }}>
              <ShieldCheck size={22} />
            </div>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>Verify your identity</h1>
            <p style={{ margin: "6px 0 0", fontSize: 13, color: "var(--p-muted)" }}>
              {codeSentTo ? <>Enter the 6-digit code sent to <b>{codeSentTo}</b>.</>
                          : <>A one-time code will be sent to <b>{gate.email}</b>.</>}
            </p>
            {gate.locked && (
              <div className="note warn" style={{ marginTop: 14 }}>Too many attempts. Try again in 15 minutes.</div>
            )}
            {!codeSentTo ? (
              <button className="btn pri" onClick={onSendCode} disabled={busy === "code" || gate.locked}
                      style={{ width: "100%", justifyContent: "center", marginTop: 20, height: 40 }}>
                {busy === "code" ? <Loader2 size={15} className="animate-spin" /> : <Mail size={15} />}
                Send code
              </button>
            ) : (
              <div style={{ marginTop: 18, display: "grid", gap: 10 }}>
                <input value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                       inputMode="numeric" autoFocus placeholder="••••••" maxLength={6}
                       onKeyDown={(e) => { if (e.key === "Enter" && code.length === 6) void onVerify(); }}
                       style={{ width: "100%", height: 46, textAlign: "center", fontSize: 22, letterSpacing: "0.45em",
                                border: "1px solid var(--p-border-2)", borderRadius: 8, fontFamily: "var(--p-mono)" }} />
                <button className="btn pri" onClick={onVerify} disabled={code.length < 6 || busy === "verify"}
                        style={{ width: "100%", justifyContent: "center", height: 40 }}>
                  {busy === "verify" ? <Loader2 size={15} className="animate-spin" /> : <KeyRound size={15} />}
                  Verify
                </button>
                <button onClick={onSendCode} disabled={busy === "code"}
                        style={{ background: "none", border: 0, color: "var(--p-primary)", fontSize: 12.5,
                                 fontWeight: 600, cursor: "pointer" }}>
                  Resend code
                </button>
              </div>
            )}
            {err && <div className="note warn" style={{ marginTop: 14 }}>{err}</div>}
          </div>
          <p style={{ textAlign: "center", fontSize: 12, color: "var(--p-faint)", marginTop: 14 }}>
            Secure link · no account required
          </p>
        </div>
      </div>
    );
  }

  // ── the review ──────────────────────────────────────────────────────────
  const p = doc.progress;
  const pct = p.total ? Math.round((p.fixed / p.total) * 100) : 100;
  const delivered = doc.status.startsWith("delivered");
  const nextVersion = (doc.version ?? 0) + 1;

  return (
    <div className="proto" style={{ minHeight: "100vh", paddingBottom: delivered ? 32 : 96 }}>
      {header}
      <main style={{ maxWidth: 1280, margin: "0 auto", padding: "22px 24px" }}>

        {/* Progress across versions + deadline. */}
        <div className="card" style={{ padding: "18px 22px", marginBottom: 18 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 260 }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                <span style={{ fontSize: 20, fontWeight: 600 }}>{p.fixed} of {p.total}</span>
                <span style={{ fontSize: 13, color: "var(--p-muted)" }}>exceptions resolved</span>
                <InfoTip text={`Counted from version 1. Resolve the highlighted cells, then submit them as version ${nextVersion}. You can also send a corrected file through your usual channel — it is matched to this one automatically.`} />
              </div>
              <div style={{ marginTop: 10, height: 6, borderRadius: 99, background: "var(--p-border)", overflow: "hidden" }}>
                <div style={{ width: `${pct}%`, height: "100%", background: "var(--p-primary)", borderRadius: 99,
                              transition: "width .3s" }} />
              </div>
            </div>
            {doc.deadline && !delivered && (
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 11.5, color: "var(--p-faint)", fontWeight: 600, letterSpacing: ".2px" }}>DUE BY</div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>{fmtDate(doc.deadline)}</div>
              </div>
            )}
          </div>
        </div>

        {msg && <div className="note ok" style={{ marginBottom: 14, display: "flex", gap: 8, alignItems: "center" }}>
          <CheckCircle2 size={15} /> {msg}</div>}
        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}

        {check && !delivered && <ValidationResult check={check} />}

        {delivered ? (
          <div className="card" style={{ padding: 36, textAlign: "center" }}>
            <div style={{ width: 52, height: 52, borderRadius: 99, display: "grid", placeItems: "center", margin: "0 auto 12px",
                          background: "var(--p-ok-soft)", color: "var(--p-ok)" }}>
              <CheckCircle2 size={26} />
            </div>
            <div style={{ fontSize: 16, fontWeight: 600 }}>Delivered to {doc.carrier ?? "the carrier"}</div>
            <div style={{ fontSize: 13, color: "var(--p-muted)", marginTop: 4 }}>
              Version {doc.version}{doc.delivered_at ? ` · ${fmtDate(doc.delivered_at)}` : ""}. No further action needed.
            </div>
          </div>
        ) : processing ? (
          <div className="card" style={{ padding: 40, textAlign: "center" }}>
            <Loader2 className="animate-spin" size={26} style={{ margin: "0 auto 12px", color: "var(--p-primary)" }} />
            <div style={{ fontSize: 16, fontWeight: 600 }}>Checking version {doc.version}</div>
            <div style={{ fontSize: 13, color: "var(--p-muted)", marginTop: 4 }}>
              Your corrected data has been received. The result will appear here in about a minute.
            </div>
          </div>
        ) : !REVIEWABLE.includes(doc.status) ? (
          <div className="card" style={{ padding: 36, textAlign: "center" }}>
            <div style={{ fontSize: 16, fontWeight: 600 }}>{doc.status_text}</div>
            {doc.message && <div style={{ fontSize: 13, color: "var(--p-muted)", marginTop: 4 }}>{doc.message}</div>}
          </div>
        ) : !exp ? (
          <div className="card" style={{ padding: 36, textAlign: "center", color: "var(--p-muted)" }}>
            <Loader2 className="animate-spin" size={20} style={{ margin: "0 auto" }} />
          </div>
        ) : (
          <>
            <div className="tiles five" style={{ marginBottom: 20 }}>
              <div className="tile"><div className="k">Total</div><div className="v">{exceptions.length}</div></div>
              <div className="tile"><div className="k">Critical</div><div className="v" style={{ color: "var(--p-crit)" }}>{critical}</div></div>
              <div className="tile"><div className="k">Warning</div><div className="v" style={{ color: "var(--p-warn)" }}>{warning}</div></div>
              <div className="tile"><div className="k">Resolved</div><div className="v" style={{ color: "var(--p-ok)" }}>{resolved}</div></div>
              <div className="tile"><div className="k">Open</div><div className="v">{openCount}</div></div>
            </div>

            <div className="tabs" role="tablist">
              <button role="tab" className={tab === "sheet" ? "on" : ""} onClick={() => setTab("sheet")}>
                BDX Sheet
              </button>
              <button role="tab" className={tab === "rules" ? "on" : ""} onClick={() => setTab("rules")}>
                Issues by Rule <span style={{ marginLeft: 6, fontSize: 11.5, fontWeight: 600, padding: "1px 7px",
                  borderRadius: 99, background: "var(--p-surface-2)", border: "1px solid var(--p-border)" }}>{groups.length}</span>
              </button>
            </div>

            <div hidden={tab !== "sheet"}>{grid}</div>

            {tab === "rules" && (
              <>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
                  <div className="search" style={{ minWidth: 300 }}>
                    <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m20 20-3-3" /></svg>
                    <input value={search} onChange={(e) => setSearch(e.target.value)}
                      placeholder="Search rule, field or policy" />
                  </div>
                  {search && <span className="linkish" onClick={() => setSearch("")}>Clear</span>}
                  <span className="muted" style={{ marginLeft: "auto", fontSize: 12 }}>
                    {plural(shown.length, "rule")}
                  </span>
                </div>
                <div className="grid">
                  {shown.map((g) => {
                    const t = tallyDecisions(g.items);
                    const sheets = Array.from(new Set(g.items.map((it) => it.source_sheet).filter(Boolean))) as string[];
                    return (
                      <div key={g.ruleKey} className={`card spine ${SEV_SPINE[g.severity] ?? "info"} click`}
                           onClick={() => openRule(g.items)}>
                        <div className="card-h">
                          <h3>{g.ruleName}</h3>
                          <div className="right">
                            <span className={`badge ${SEV_BADGE[g.severity] ?? "b-info"}`}>
                              <span className="d" />{SEV_LABEL[g.severity] ?? g.severity} · {plural(g.count, "policy", "policies")}
                            </span>
                            <span className="linkish">Resolve →</span>
                          </div>
                        </div>
                        <div style={{ padding: "12px 20px", display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                          {sheets.length > 0 && (
                            <span className="badge b-mut"><span className="d" />{sheets.join(", ")}</span>
                          )}
                          <RuleExplanationBlock chipsOnly explanation={g.explanation} />
                          <span style={{ flex: 1 }} />
                          <span className="badge b-ok">Approved {t.approve}</span>
                          <span className="badge b-info">Fixed {t.fix}</span>
                          <span className="badge b-warn">Dismissed {t.dismiss}</span>
                          <span className="badge b-mut">Pending {t.pending}</span>
                        </div>
                      </div>
                    );
                  })}
                  {shown.length === 0 && (
                    <div className="card"><div className="empty">
                      {search ? `No rules match "${search}".` : "No exceptions on this version."}
                    </div></div>
                  )}
                </div>
              </>
            )}
          </>
        )}

        {doc.versions.length > 1 && (
          <div style={{ marginTop: 22, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center",
                        fontSize: 12, color: "var(--p-muted)" }}>
            <span style={{ fontWeight: 600, color: "var(--p-faint)", letterSpacing: ".2px" }}>HISTORY</span>
            {doc.versions.map((v) => (
              <span key={v.version} className="badge b-mut" title={v.source === "secure_link" ? "Submitted on this page" : `Sent by ${v.source}`}>
                v{v.version} · {v.status_text}
              </span>
            ))}
          </div>
        )}
      </main>

      {REVIEWABLE.includes(doc.status) && (
        <footer style={{ position: "fixed", insetInline: 0, bottom: 0, zIndex: 40, background: "rgba(255,255,255,.97)",
                         borderTop: "1px solid var(--p-border)", backdropFilter: "blur(6px)" }}>
          <div style={{ maxWidth: 1280, margin: "0 auto", padding: "12px 24px",
                        display: "flex", alignItems: "center", gap: 12 }}>
            <span style={{ fontSize: 13, color: "var(--p-muted)", marginRight: "auto" }}>
              {!doc.drafts ? "No decisions yet"
                : check ? <><b style={{ color: "var(--p-ink)" }}>{plural(doc.drafts, "decision")}</b> validated</>
                : <><b style={{ color: "var(--p-ink)" }}>{plural(doc.drafts, "decision")}</b> · validate before submitting</>}
            </span>
            <button className="btn" onClick={onValidate} disabled={!doc.drafts || busy !== null}
                    style={{ height: 38 }}>
              {busy === "validate" ? <Loader2 size={14} className="animate-spin" /> : <ListChecks size={14} />}
              {busy === "validate" ? "Validating…" : check ? "Validate Again" : "Validate"}
            </button>
            <button className="btn pri" onClick={onSubmit} disabled={!doc.drafts || !check || busy !== null}
                    title={check ? undefined : "Validate your changes first"} style={{ height: 38 }}>
              {busy === "submit" ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
              Submit Version {nextVersion}
            </button>
          </div>
        </footer>
      )}
    </div>
  );
}

function ValidationResult({ check }: { check: FixValidation }) {
  const failing = check.still_failing_count;
  const remaining = check.open_after;
  const carry = remaining > 0
    ? ` ${plural(remaining, "exception")} will remain open in version ${check.version}.`
    : ` Version ${check.version} will have no open exceptions.`;
  if (!failing) {
    return (
      <div className="note ok" style={{ marginBottom: 14, display: "flex", gap: 8, alignItems: "flex-start" }}>
        <CheckCircle2 size={15} style={{ marginTop: 2, flexShrink: 0 }} />
        <span><b>Validation passed.</b>{check.corrected ? ` ${plural(check.corrected_ok, "corrected value")} meet the rules.` : ""}{carry}</span>
      </div>
    );
  }
  return (
    <div className="card" style={{ marginBottom: 14, borderColor: "#EAD7AE" }}>
      <div className="card-h" style={{ background: "var(--p-warn-soft)" }}>
        <AlertTriangle size={16} style={{ color: "var(--p-warn)" }} />
        <h3>{plural(failing, "correction")} still {failing === 1 ? "fails" : "fail"} the rules</h3>
        <span className="sub" style={{ marginLeft: "auto" }}>{check.corrected_ok} of {check.corrected} passed</span>
      </div>
      <div className="tbl-wrap">
        <table>
          <thead><tr><th>Sheet</th><th>Row</th><th>Column</th><th>Value</th><th>Issue</th></tr></thead>
          <tbody>
            {check.still_failing.slice(0, 10).map((r) => (
              <tr key={r.key}>
                <td>{String(r.sheet ?? "")}</td>
                <td className="mono">{r.row != null ? String(r.row) : r.value_label ? "All rows" : ""}</td>
                <td>{String(r.column ?? "")}</td>
                <td style={{ color: "var(--p-crit)", fontWeight: 600, whiteSpace: "nowrap" }}>
                  {r.value_label ? fmtNum(r.current_value) : String(r.current_value ?? "—")}
                  {r.value_label && (
                    <div style={{ color: "var(--p-muted)", fontWeight: 400, fontSize: 11 }}>{r.value_label}</div>
                  )}
                </td>
                <td style={{ color: "var(--p-muted)" }}>{r.what_to_fix || r.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ padding: "10px 20px", fontSize: 12.5, color: "var(--p-muted)", borderTop: "1px solid var(--p-border)" }}>
        Correct {failing === 1 ? "it" : "them"} in the sheet and validate again.{carry}
        {failing > 10 ? ` Showing 10 of ${failing}.` : ""}
      </div>
    </div>
  );
}

/** A total as a reader writes it: 107,700 — not 107700.0. */
function fmtNum(v: unknown): string {
  const n = Number(String(v ?? "").replace(/,/g, ""));
  return v == null || String(v).trim() === "" || !Number.isFinite(n)
    ? String(v ?? "—") : n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function StatusBadge({ status, text }: { status: string; text: string }) {
  const cls = status.startsWith("delivered") ? "b-ok"
    : status === "with_exceptions" || status === "held_at_deadline" ? "b-warn"
    : status === "failed" || status === "rejected" ? "b-crit" : "b-mut";
  return <span className={`badge ${cls}`}><span className="d" />{text}</span>;
}

function Splash({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-screen place-items-center bg-[#F3F4F7] p-6">
      <div className="max-w-md text-center text-sm text-ink-muted">{children}</div>
    </div>
  );
}
