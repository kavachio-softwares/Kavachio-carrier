/**
 * A reinsurer's private, read-only view of one treaty — /treaty-view?token=…
 *
 * The reader has no account and never will: reinsurers have no seat in
 * Kavachio. The carrier sent them a link and, in the same email, a six-digit
 * code. The link alone opens NOTHING but the code screen — not even the
 * treaty's name — and the code is asked for again in every new browser
 * session (the unlock lives in sessionStorage).
 *
 * Once open, they see the treaty's structure and THEIR OWN lines: their share
 * of each layer, which layers they lead, how placed each layer is. Never
 * another reinsurer's name or share — the server does not send them.
 *
 * A link the carrier has withdrawn, one past its date, or one whose treaty has
 * ended says exactly that, in a sentence, rather than "invalid link".
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Ban, CalendarClock, CheckCircle2, Download, FileText, KeyRound, Loader2,
  Lock, Mail, MessageSquare, ShieldCheck, Star,
} from "lucide-react";
import KavachioLogo from "../components/KavachioLogo";
import { TreatyTower } from "../components/treaty/TreatyTower";
import {
  approveTreaty, clearViewSession, downloadTreatyDocument,
  downloadTreatyWording, openTreatyView, requestTreatyChanges,
  resendTreatyCode, verifyTreatyCode,
  type TreatyView as View, type TreatyViewLocked, type TreatyViewOpen,
} from "../api/treaty";
import { fmtDate } from "../utils/date";

const LIFE: Record<string, string> = {
  draft: "Draft — not yet in force", pending: "Being settled",
  agreed: "Terms agreed — awaiting signature", signed: "Signed",
  active: "In force", expired: "Expired",
};

function errText(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof d === "string" ? d : fallback;
}

export default function TreatyView() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [view, setView] = useState<View | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    if (!token) { setErr("This link is missing its key. Open it again from the email."); setLoading(false); return; }
    setLoading(true);
    openTreatyView(token)
      .then(v => { setView(v); setErr(""); })
      .catch(e => setErr(errText(e, "This link is not valid.")))
      .finally(() => setLoading(false));
  }, [token]);
  useEffect(load, [load]);

  // Never let the link sit in the page title or get shared by the browser.
  useEffect(() => { document.title = "Treaty · Kavachio"; }, []);

  if (loading && !view) {
    return (
      <div className="proto tv" style={{ display: "grid", placeItems: "center" }}>
        <span className="muted" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Loader2 className="animate-spin" size={18} /> Opening…
        </span>
      </div>
    );
  }

  if (err && !view) {
    return <Message icon={Ban} title="This link doesn't work" body={err}
                    foot="Ask whoever sent it to send you a new one." />;
  }
  if (!view) return null;

  if (view.closed) {
    return <Message icon={view.status === "expired" ? CalendarClock : Ban}
                    title={view.title} body={view.message} />;
  }
  if (view.locked) {
    return <CodeGate token={token} gate={view}
                     onUnlocked={() => { load(); }} />;
  }
  return <Treaty token={token} v={view} onView={setView}
                 onLock={() => { clearViewSession(token); load(); }} />;
}

function Message({ icon: Icon, title, body, foot }: {
  icon: React.ElementType; title: string; body: string; foot?: string;
}) {
  return (
    <div className="proto tv" style={{ display: "grid", placeItems: "center", padding: 16 }}>
      <div style={{ maxWidth: 440, textAlign: "center" }}>
        <div style={{ display: "flex", justifyContent: "center", gap: 8,
                      alignItems: "center", marginBottom: 22 }}>
          <KavachioLogo className="h-7 w-7" /><b>Kavachio</b>
        </div>
        <div className="card pad">
          <div style={{ width: 48, height: 48, borderRadius: 99, margin: "0 auto 14px",
                        display: "grid", placeItems: "center",
                        background: "var(--p-surface-3)", color: "var(--p-muted)" }}>
            <Icon size={22} />
          </div>
          <h1 style={{ fontSize: 17, margin: "0 0 8px" }}>{title}</h1>
          <p className="muted" style={{ fontSize: 13.5, lineHeight: 1.6, margin: 0 }}>{body}</p>
          {foot && <p className="sub" style={{ marginTop: 14 }}>{foot}</p>}
        </div>
      </div>
    </div>
  );
}

// ── the code ─────────────────────────────────────────────────────────────────
function CodeGate({ token, gate, onUnlocked }: {
  token: string; gate: TreatyViewLocked; onUnlocked: () => void;
}) {
  const LEN = 6;
  const [digits, setDigits] = useState<string[]>(Array(LEN).fill(""));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [left, setLeft] = useState(gate.lockout_seconds);
  const boxes = useRef<(HTMLInputElement | null)[]>([]);

  useEffect(() => {
    if (left <= 0) return;
    const t = setInterval(() => setLeft(v => Math.max(0, v - 1)), 1000);
    return () => clearInterval(t);
  }, [left]);
  useEffect(() => { boxes.current[0]?.focus(); }, []);

  const code = digits.join("");
  const lockedOut = left > 0;

  function put(i: number, raw: string) {
    const only = raw.replace(/\D/g, "");
    if (!only) { setDigits(d => d.map((v, n) => (n === i ? "" : v))); return; }
    setDigits(d => {
      const next = [...d];
      for (let k = 0; k < only.length && i + k < LEN; k++) next[i + k] = only[k];
      return next;
    });
    boxes.current[Math.min(i + only.length, LEN - 1)]?.focus();
  }

  async function submit(e?: React.FormEvent) {
    e?.preventDefault();
    if (code.length !== LEN || busy || lockedOut) return;
    setBusy(true); setErr(""); setNote("");
    try {
      await verifyTreatyCode(token, code);
      onUnlocked();
    } catch (ex) {
      const detail = errText(ex, "That code did not work.");
      setErr(detail);
      setDigits(Array(LEN).fill(""));
      boxes.current[0]?.focus();
      const m = /(\d+)\s*minutes?/.exec(detail);
      const status = (ex as { response?: { status?: number } })?.response?.status;
      if (status === 429 && /locked|Too many/.test(detail)) {
        setLeft(m ? Number(m[1]) * 60 : 15 * 60);
      }
    } finally { setBusy(false); }
  }

  useEffect(() => {
    if (code.length === LEN && !busy && !lockedOut) submit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  async function resend() {
    setBusy(true); setErr(""); setNote("");
    try {
      const r = await resendTreatyCode(token);
      setNote(r.ok ? `A new code is on its way to ${r.sent_to}.`
                   : "We could not send a new code just now.");
    } catch (ex) {
      setErr(errText(ex, "Could not send a new code."));
    } finally { setBusy(false); }
  }

  return (
    <div className="proto tv">
      <div className="tv-gate">
        <div style={{ display: "flex", justifyContent: "center", gap: 8,
                      alignItems: "center", marginBottom: 22 }}>
          <KavachioLogo className="h-8 w-8" /><b style={{ fontSize: 15 }}>Kavachio</b>
        </div>
        <form className="card pad" onSubmit={submit} style={{ padding: "30px 26px" }}>
          <div style={{ width: 48, height: 48, borderRadius: 99, margin: "0 auto 14px",
                        display: "grid", placeItems: "center",
                        background: "var(--p-primary-soft)", color: "var(--p-primary)" }}>
            <KeyRound size={22} />
          </div>
          <h1 style={{ textAlign: "center", fontSize: 17, margin: 0 }}>Enter your code</h1>
          <p className="muted" style={{ textAlign: "center", fontSize: 13, lineHeight: 1.6,
                                        margin: "8px auto 0", maxWidth: 330 }}>
            {gate.carrier} shared a treaty with you. The email that brought you
            here has a six-digit code in it — type it below to open it.
          </p>
          <div className="sub" style={{ display: "flex", gap: 6, justifyContent: "center",
                                        alignItems: "center", marginTop: 10 }}>
            <Mail size={13} /> {gate.email_hint}
          </div>
          <div className="tv-digits">
            {digits.map((d, i) => (
              <input key={i} ref={el => { boxes.current[i] = el; }}
                     value={d} className={err ? "bad" : ""}
                     onChange={e => put(i, e.target.value)}
                     onKeyDown={e => {
                       if (e.key === "Backspace" && !digits[i] && i > 0) boxes.current[i - 1]?.focus();
                     }}
                     disabled={busy || lockedOut} inputMode="numeric"
                     autoComplete={i === 0 ? "one-time-code" : "off"}
                     maxLength={LEN} aria-label={`Digit ${i + 1}`} />
            ))}
          </div>
          {err && <p style={{ textAlign: "center", color: "var(--p-crit-ink)",
                              fontSize: 13, marginTop: 14 }}>{err}</p>}
          {note && <p style={{ textAlign: "center", color: "var(--p-primary)",
                               fontSize: 13, marginTop: 14 }}>{note}</p>}
          {lockedOut && (
            <div className="note warn" style={{ marginTop: 14, textAlign: "center" }}>
              Too many wrong codes. Try again in{" "}
              <b>{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</b>.
            </div>
          )}
          {!err && !lockedOut && gate.attempts_left > 0 && gate.attempts_left < 5 && (
            <p className="sub" style={{ textAlign: "center", marginTop: 12 }}>
              {gate.attempts_left} attempt{gate.attempts_left === 1 ? "" : "s"} left.
            </p>
          )}
          <button type="submit" className="btn pri"
                  disabled={busy || lockedOut || code.length !== LEN}
                  style={{ width: "100%", justifyContent: "center", marginTop: 20,
                           padding: "12px 15px" }}>
            {busy ? <Loader2 className="animate-spin" size={15} /> : <Lock size={14} />}
            Open the treaty
          </button>
          {gate.can_resend && (
            <button type="button" className="btn ghost" onClick={resend} disabled={busy}
                    style={{ width: "100%", justifyContent: "center", marginTop: 8 }}>
              Didn't get it? Send a new code
            </button>
          )}
          <p className="sub" style={{ textAlign: "center", marginTop: 18, paddingTop: 14,
                                      borderTop: "1px solid var(--p-border)" }}>
            Nobody at Kavachio will ever ask you for this code. If you were not
            expecting this, do not enter it — tell the sender instead.
          </p>
        </form>
      </div>
    </div>
  );
}

// ── the treaty ───────────────────────────────────────────────────────────────
function Treaty({ token, v, onLock, onView }: {
  token: string; v: TreatyViewOpen; onLock: () => void;
  onView: (v: TreatyViewOpen) => void;
}) {
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const mine = useMemo(
    () => new Set(v.layers.filter(l => l.your_share).map(l => l.index)), [v.layers]);
  const myLayers = v.layers.filter(l => l.your_share);
  const leads = myLayers.filter(l => l.your_share?.is_lead).length;

  async function dl(key: string, fn: () => Promise<void>) {
    setBusy(key); setErr("");
    try { await fn(); }
    catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 401) { onLock(); return; }
      setErr("That download did not work. Try again, or ask the sender for a copy.");
    } finally { setBusy(""); }
  }

  return (
    <div className="proto tv">
      <div className="tv-bar">
        <div className="tv-bar-in">
          <KavachioLogo className="h-7 w-7" />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontWeight: 600, fontSize: 14, overflow: "hidden",
                          textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {v.treaty.name}
            </div>
            <div className="sub" style={{ overflow: "hidden", textOverflow: "ellipsis",
                                          whiteSpace: "nowrap" }}>
              Shared by {v.carrier} with {v.viewer.name}
              {v.viewer.organisation ? `, ${v.viewer.organisation}` : ""}
            </div>
          </div>
          <span className="badge b-mut" title="You can read this page; nothing on it can be changed.">
            <span className="d" />Read only
          </span>
        </div>
      </div>

      <div className="tv-wrap">
        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}

        {/* Their position, first — it is what they came for. */}
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <ShieldCheck size={16} className="ci" />
            <h3>Your lines on this treaty</h3>
            <span className="sub">{v.viewer.organisation}</span>
          </div>
          <div style={{ padding: "16px 20px" }}>
            {myLayers.length === 0 ? (
              <div className="sub">
                {v.carrier} has not recorded a share for your organisation yet.
              </div>
            ) : (
              <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(200px,1fr))",
                                             gap: 12 }}>
                {myLayers.map(l => (
                  <div key={l.index} className="card pad" style={{ boxShadow: "none" }}>
                    <div className="sub">{l.name}</div>
                    <div className="tv-mine">
                      {l.your_share!.share_pct}%
                      <span className="sub" style={{ fontWeight: 500 }}>of the layer</span>
                    </div>
                    {l.your_share!.is_lead && (
                      <div className="badge b-ok" style={{ marginTop: 6 }}>
                        <Star size={11} fill="currentColor" /> You lead this layer
                      </div>
                    )}
                    {l.your_share!.slip_reference && (
                      <div className="sub mono" style={{ marginTop: 6 }}>
                        Slip {l.your_share!.slip_reference}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
            {leads > 0 && myLayers.length > 1 && (
              <div className="sub" style={{ marginTop: 10 }}>
                You lead {leads} of the {myLayers.length} layers you are on.
              </div>
            )}
          </div>
        </div>

        <ApprovalCard token={token} v={v} onView={onView} onLock={onLock} />

        <div className="grid g-2" style={{ alignItems: "start", marginBottom: 16 }}>
          <div className="card">
            <div className="card-h">
              <h3>{v.summary?.headline ?? "The treaty"}</h3>
            </div>
            <div style={{ padding: "16px 20px" }}>
              {v.summary ? (
                <TreatyTower summary={v.summary} mine={mine} carrier={v.carrier}
                             yourShare={v.summary.split
                               ? v.layers[0]?.your_share?.share_pct ?? null : null} />
              ) : (
                <div className="sub">The structure has not been set out yet.</div>
              )}
              {v.summary && !v.summary.split && (
                <div className="sub" style={{ marginTop: 12 }}>
                  &ldquo;Placed&rdquo; on a layer is how much of it all the
                  reinsurers together have signed for.
                </div>
              )}
              <div className="sub" style={{ marginTop: 10 }}>
                Only your own share is shown here. The other reinsurers on this
                treaty are not named, and they cannot see your share.
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-h"><h3>Terms</h3></div>
            <div style={{ padding: "6px 20px 12px" }}>
              <div className="kv"><span className="k">Type</span>
                <span className="v">{v.summary?.type_label ?? "—"}</span></div>
              <div className="kv"><span className="k">Term</span>
                <span className="v">{fmtDate(v.treaty.inception_dt)} → {fmtDate(v.treaty.expiry_dt)}</span></div>
              {v.treaty.year_of_account && (
                <div className="kv"><span className="k">Year of account</span>
                  <span className="v">{v.treaty.year_of_account}</span></div>
              )}
              <div className="kv"><span className="k">Status</span>
                <span className="v">{LIFE[v.treaty.lifecycle] ?? v.treaty.lifecycle}</span></div>
              <div className="kv"><span className="k">Reinsured</span>
                <span className="v">{v.carrier}</span></div>
              {v.treaty.programmes.length > 0 && (
                <div className="kv"><span className="k">Protects</span>
                  <span className="v" style={{ textAlign: "right" }}>
                    {v.treaty.programmes.join(", ")}</span></div>
              )}
              {(v.summary?.facts ?? []).map(f => (
                <div className="kv" key={f.label}>
                  <span className="k">{f.label}</span><span className="v">{f.value}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {v.layers.length > 0 && (
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-h"><h3>Layers</h3></div>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr><th>Layer</th><th>Terms</th><th>Placed in total</th><th>Your share</th></tr>
                </thead>
                <tbody>
                  {v.layers.map(l => (
                    <tr key={l.index}>
                      <td><b>{l.name}</b></td>
                      <td className="sub">
                        {l.facts.length ? l.facts.map(f => `${f.label} ${f.value}`).join(" · ") : "—"}
                      </td>
                      <td className="mono">{l.placed_pct}%</td>
                      <td>
                        {l.your_share
                          ? <b className="mono">{l.your_share.share_pct}%</b>
                          : <span className="sub">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div className="card">
          <div className="card-h"><FileText size={16} className="ci" /><h3>Documents</h3></div>
          <div style={{ padding: "12px 20px 16px" }}>
            {v.documents.length === 0 && !v.wording_pdf && (
              <div className="sub">No document has been shared yet.</div>
            )}
            {v.documents.map(d => (
              <div key={d.id} className="kv">
                <span>
                  <b style={{ fontSize: 13 }}>{d.filename ?? "Treaty wording"}</b>
                  <div className="sub">
                    {d.is_executed_copy ? "Signed copy" : "Wording"}
                    {d.uploaded_at ? ` · added ${fmtDate(d.uploaded_at)}` : ""}
                  </div>
                </span>
                <button className="btn sm" type="button" disabled={!!busy}
                        onClick={() => dl(`d${d.id}`, () =>
                          downloadTreatyDocument(token, d.id, d.filename ?? "treaty"))}>
                  {busy === `d${d.id}` ? <Loader2 className="animate-spin" size={13} />
                                       : <Download size={13} />} Download
                </button>
              </div>
            ))}
            {v.wording_pdf && (
              <div className="kv">
                <span>
                  <b style={{ fontSize: 13 }}>Treaty wording (PDF)</b>
                  <div className="sub">As written — the signed copy will replace it once returned.</div>
                </span>
                <button className="btn sm" type="button" disabled={!!busy}
                        onClick={() => dl("w", () => downloadTreatyWording(token, v.treaty.name))}>
                  {busy === "w" ? <Loader2 className="animate-spin" size={13} />
                                : <Download size={13} />} Download
                </button>
              </div>
            )}
          </div>
        </div>

        <p className="sub" style={{ textAlign: "center", marginTop: 22, lineHeight: 1.6 }}>
          This link is yours alone{v.expires_on ? ` and works until ${fmtDate(v.expires_on)}` : ""}.
          Every visit is recorded for {v.carrier}. Close this page and you will be
          asked for a code again next time.
        </p>
      </div>
    </div>
  );
}


// ── the reinsurer's answer ───────────────────────────────────────────────────
/** Approve your lines and the treaty's terms, or say what should change.
 *  The name typed is the person approving; the code they entered to get here
 *  is what shows they hold the mailbox the link was sent to. An answer is
 *  tied to the treaty as it stands — if the carrier changes it afterwards,
 *  the reinsurer is asked again. */
function ApprovalCard({ token, v, onView, onLock }: {
  token: string; v: TreatyViewOpen; onView: (v: TreatyViewOpen) => void;
  onLock: () => void;
}) {
  const a = v.approval;
  const [mode, setMode] = useState<"" | "approve" | "change">("");
  const [name, setName] = useState(v.viewer.name ?? "");
  const [title, setTitle] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState("");

  async function go(fn: () => Promise<{ view: TreatyViewOpen; terms_agreed?: boolean }>,
                    ok: (agreed: boolean) => string) {
    setBusy(true); setErr("");
    try {
      const r = await fn();
      onView(r.view);
      setMode("");
      setDone(ok(!!r.terms_agreed));
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 401) { onLock(); return; }
      setErr(errText(e, "That did not go through. Try again."));
    } finally {
      setBusy(false);
    }
  }

  const org = v.viewer.organisation ?? "your organisation";

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-h">
        <CheckCircle2 size={16} className="ci" />
        <h3>Your approval</h3>
        <span className={`badge ${a.status === "approved" ? "b-ok"
          : a.status === "changes_requested" ? "b-warn" : "b-mut"}`}>
          <span className="d" />
          {a.status === "approved" ? "Approved"
            : a.status === "changes_requested" ? "Change asked for"
            : a.status === "stale" ? "Changed since you answered" : "Waiting for you"}
        </span>
      </div>
      <div style={{ padding: "14px 20px 16px" }}>
        {done && <div className="note ok" style={{ marginBottom: 12 }}>{done}</div>}
        {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}

        {a.status === "approved" && (
          <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>
            Approved for {org} by <b>{a.signer_name}</b>
            {a.signer_title ? `, ${a.signer_title}` : ""} on {fmtDate(a.at)}.
          </p>
        )}
        {a.status === "changes_requested" && (
          <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>
            You asked for a change on {fmtDate(a.at)}: <i>&ldquo;{a.note}&rdquo;</i>.{" "}
            {v.carrier} will update the treaty and you will be asked again.
          </p>
        )}
        {a.status === "stale" && (
          <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>
            {v.carrier} changed the treaty after you{" "}
            {a.last_status === "approved" ? "approved it" : "asked for a change"}.
            Check it again and give your answer on the treaty as it is now.
          </p>
        )}
        {a.status === "pending" && (
          <p className="muted" style={{ margin: 0, fontSize: 13.5 }}>
            {a.settles_terms
              ? `${v.carrier} is waiting for each reinsurer to approve the treaty. `
                + "Once every reinsurer has approved, its terms are agreed."
              : "This treaty is signed, so its terms are fixed. Approve it to "
                + "confirm the treaty and your lines above are right."}
          </p>
        )}

        {a.can_answer && mode === "" && (
          <div className="rowacts" style={{ marginTop: 12 }}>
            {a.status !== "approved" && (
              <button className="btn pri" type="button" onClick={() => setMode("approve")}>
                <CheckCircle2 size={14} /> Approve
              </button>
            )}
            {a.can_request_changes && (
              <button className="btn" type="button" onClick={() => setMode("change")}>
                <MessageSquare size={14} /> Ask for a change
              </button>
            )}
          </div>
        )}

        {mode === "approve" && (
          <div className="note" style={{ marginTop: 12 }}>
            <div className="grid g-2" style={{ gap: 12 }}>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Your name</label>
                <input value={name} onChange={e => setName(e.target.value)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Your title <span className="muted" style={{ fontWeight: 500 }}>— optional</span></label>
                <input value={title} onChange={e => setTitle(e.target.value)}
                       placeholder="Underwriter" />
              </div>
            </div>
            <label style={{ display: "flex", gap: 8, alignItems: "flex-start",
                            marginTop: 12, fontSize: 13, color: "var(--p-ink)" }}>
              <input type="checkbox" checked={confirm} style={{ marginTop: 3 }}
                     onChange={e => setConfirm(e.target.checked)} />
              <span>
                On behalf of <b>{org}</b>, I approve this treaty and the shares
                shown above as ours.
              </span>
            </label>
            <div className="rowacts">
              <button className="btn pri" type="button"
                      disabled={busy || !name.trim() || !confirm}
                      onClick={() => go(() => approveTreaty(token, name.trim(), title.trim()),
                        agreed => agreed
                          ? `Approved. Every reinsurer has now approved, so the terms are agreed. ${v.carrier} has been told.`
                          : `Approved. ${v.carrier} has been told.`)}>
                {busy ? "Approving…" : "Approve"}
              </button>
              <button className="btn" type="button" onClick={() => setMode("")}>Cancel</button>
            </div>
          </div>
        )}

        {mode === "change" && (
          <div className="note" style={{ marginTop: 12 }}>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>What should change?</label>
              <textarea rows={3} value={note} onChange={e => setNote(e.target.value)}
                        placeholder="e.g. Our line on layer 2 is 20%, not 25%." />
            </div>
            <div className="rowacts">
              <button className="btn pri" type="button" disabled={busy || !note.trim()}
                      onClick={() => go(() => requestTreatyChanges(token, note.trim()),
                        () => `Sent to ${v.carrier}. You will be asked again once the treaty is updated.`)}>
                {busy ? "Sending…" : "Send"}
              </button>
              <button className="btn" type="button" onClick={() => setMode("")}>Cancel</button>
            </div>
          </div>
        )}

        {!a.can_answer && a.why_not && (
          <p className="sub" style={{ marginTop: 10 }}>{a.why_not}</p>
        )}
      </div>
    </div>
  );
}
