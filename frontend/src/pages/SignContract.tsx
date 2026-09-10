/**
 * The signing page — Create-a-Contract step 4, seen by the person signing.
 *
 * TWO DOORS ONTO ONE SCREEN.
 *
 *   ?token=…     the emailed link. No account, no login: the token is the whole
 *                credential, so it is only half of one — a code from the same
 *                message has to be typed before anything is shown.
 *   ?contract=…  from inside the app. No email and no code: the page asks the
 *                server to let the logged-in seat in, and the server works out
 *                which party they are from their own login rather than from
 *                anything in the URL. This is how the carrier signs once both
 *                sides have agreed the terms, and how the broker can sign from
 *                their dashboard instead of hunting for the email.
 *
 * Past that point there is one screen and one set of calls. The token the
 * in-app door hands back is held in state and never put in the address bar:
 * it does not need to be there, so it never becomes something to forward.
 *
 * WHAT THE SIGNER SEES, AND WHY
 * -----------------------------
 * The whole document, because somebody signing it has to be able to read it.
 * Every signature box on it, because a contract with half its blocks hidden
 * looks like a different contract. But only their OWN boxes respond: the other
 * side's are drawn locked, labelled with whose they are, and already filled in
 * when that party has signed.
 *
 * The lock here is presentation. The server holds the real rule — a box belongs
 * to the party_key on it, and a POST naming somebody else's box comes back 403
 * — so nothing on this page is load-bearing for security. It is load-bearing
 * for comprehension, which is a different job and just as necessary: a signer
 * who cannot tell which boxes are theirs signs in the wrong place or gives up.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Check, ChevronDown, Download, KeyRound, Loader2, Lock, Mail, PenLine,
  ShieldCheck, Upload, X,
} from "lucide-react";
import {
  clearUnlockSession, declineSignature, isLocked, openForSigning,
  openInAppSigning, resendCode, sideLabel, signingPageUrl, signingPdfUrl,
  submitSignature, verifyCode,
  type EsignField, type LockedView, type SigningView,
} from "../api/esign";
import KavachioLogo from "../components/KavachioLogo";

const TEAL = "#077282";

/** The cursive face a typed signature is drawn in — matched to what the PDF
 *  stamps (Times Italic) so the preview is not a promise the document breaks. */
const SIG_FONT = "'Snell Roundhand','Apple Chancery','Segoe Script','Brush Script MT',Georgia,serif";

type Draft = Record<number, string>;

export default function SignContract() {
  const [params] = useSearchParams();
  const linkToken = params.get("token") ?? "";
  // Which contract to open when there is no link — the in-app door. Read once:
  // the whole page is about one contract and swapping it under the signer
  // mid-signature is not a thing to support.
  const contractId = Number(params.get("contract") ?? "") || 0;

  // The live token. From the URL when an emailed link brought them here, or
  // fetched from the server when their own login did. Held in state rather
  // than in the address bar so an in-app signing URL is not forwardable.
  const [token, setToken] = useState(linkToken);

  const [view, setView] = useState<SigningView | null>(null);
  // Non-null while the one-time code is still to be entered. Until it clears,
  // the server has told us nothing about the contract but which inbox the code
  // went to — so there is nothing to render behind the prompt.
  const [gate, setGate] = useState<LockedView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState<Draft>({});
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [adopt, setAdopt] = useState(false);
  // The boxes that stopped a Finish attempt. Held in state rather than shown as
  // a banner because the signer is usually scrolled to the signature page when
  // they press Finish, and a message at the top of the document is a message
  // nobody reads.
  const [blocked, setBlocked] = useState<EsignField[] | null>(null);
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState("");
  const [sigImage, setSigImage] = useState<string | null>(null);
  // Initials are a SECOND mark, not the signature reused. They are a different
  // act on paper — a signature closes the document, initials acknowledge a page
  // or a clause — and a document where both are the same scrawl is one where
  // neither means anything in particular.
  const [sigInitials, setSigInitials] = useState("");
  const [initialsImage, setInitialsImage] = useState<string | null>(null);
  const [sigName, setSigName] = useState("");
  const [focus, setFocus] = useState<number | null>(null);

  // The in-app door: exchange the seat you are logged into for a signing
  // session. Runs once, before the load below has anything to open.
  useEffect(() => {
    if (token || !contractId) return;
    let alive = true;
    openInAppSigning(contractId)
      .then(s => { if (alive) setToken(s.token); })
      .catch(e => {
        if (!alive) return;
        setErr(e?.response?.data?.detail
          ?? "This contract could not be opened for signature.");
        setLoading(false);
      });
    return () => { alive = false; };
  }, [contractId, token]);

  const load = useCallback(() => {
    if (!token) {
      // Still fetching one, if a contract was named. Otherwise there is
      // genuinely nothing here to open.
      if (!contractId) {
        setErr("This link is missing its token.");
        setLoading(false);
      }
      return;
    }
    setLoading(true);
    openForSigning(token)
      .then(res => {
        if (isLocked(res)) { setGate(res); setView(null); setErr(null); return; }
        setGate(null);
        const v = res;
        setView(v);
        setSigName(v.me.signature_name || v.me.name);
        // Pre-fill what the platform already knows. A signer should be
        // confirming these, not typing their own name into a form they were
        // sent by name.
        const d: Draft = {};
        const today = new Date().toLocaleDateString("en-GB",
          { day: "2-digit", month: "short", year: "numeric" });
        for (const f of v.fields) {
          if (!f.mine) continue;
          if (f.value) d[f.id] = f.value;
          else if (f.type === "name") d[f.id] = v.me.name;
          else if (f.type === "title") d[f.id] = v.me.title ?? "";
          else if (f.type === "date") d[f.id] = today;
        }
        setDraft(d);
        setErr(null);
      })
      .catch(e => setErr(e?.response?.data?.detail ??
        "This signing link could not be opened."))
      .finally(() => setLoading(false));
  }, [token, contractId]);
  useEffect(load, [load]);

  const mine = useMemo(() => (view?.fields ?? []).filter(f => f.mine), [view]);
  const sigFields = useMemo(
    () => mine.filter(f => f.type === "signature" || f.type === "initial"), [mine]);
  const outstanding = useMemo(
    () => mine.filter(f => f.required && !(draft[f.id] ?? "").trim()), [mine, draft]);

  function setValue(id: number, v: string) {
    setDraft(d => ({ ...d, [id]: v }));
  }

  /** Put a box in front of the signer and mark it. Used by "take me to it" and
   *  by the dialog that fires when Finish is pressed too early. */
  function goTo(f: EsignField | undefined) {
    if (!f) return;
    setFocus(f.id);
    document.getElementById(`fld-${f.id}`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /** Adopting fills EVERY box of yours at once — the same act applied
   *  everywhere it is needed, which is what signing a document means. Clicking
   *  them one at a time is a form, not a signature.
   *
   *  Two marks, though, not one used twice: the signature goes in the signature
   *  boxes and the initials in the initials boxes. */
  function applySignature(name: string, marks: {
    initials: string; image: string | null; initialsImage: string | null;
  }) {
    setSigName(name);
    setSigImage(marks.image);
    setSigInitials(marks.initials);
    setInitialsImage(marks.initialsImage);
    setDraft(d => {
      const next = { ...d };
      for (const f of sigFields) {
        next[f.id] = f.type === "initial" ? marks.initials : name;
      }
      return next;
    });
    setAdopt(false);
  }

  async function finish() {
    if (!view) return;
    // Stop, and say so in front of them. Signing is the one act on this page
    // that cannot be undone, so "you have not actually signed yet" has to be
    // impossible to miss rather than merely present somewhere on the page.
    if (outstanding.length) {
      setBlocked(outstanding);
      return;
    }
    setBusy(true); setErr(null);
    try {
      const r = await submitSignature(token, {
        signature_name: sigName.trim() || view.me.name,
        signature_image: sigImage,
        // Separate on the wire too, or the server has no way to stamp anything
        // but one image in both kinds of box.
        initials_image: initialsImage,
        fields: mine.map(f => ({ field_id: f.id, value: (draft[f.id] ?? "").trim() })),
        agreed,
      });
      // Signed. The link is spent server-side and the session with it, so drop
      // it here too rather than leaving a live credential in memory behind a
      // "thank you" screen.
      clearUnlockSession();
      setDone(r.message);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not record your signature.");
    } finally { setBusy(false); }
  }

  async function doDecline() {
    setBusy(true); setErr(null);
    try {
      const r = await declineSignature(token, reason);
      clearUnlockSession();
      setDone(r.message);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not send your reason.");
    } finally { setBusy(false); }
  }

  // ── states that are not the signing screen ────────────────────────────────
  if (loading) return <Splash><Loader2 className="animate-spin" size={22} /> Opening the contract…</Splash>;

  // The code comes before everything: the document, the parties, even the
  // contract's name. All of that is behind it on the server too.
  if (gate && !done) return (
    <CodeGate token={token} gate={gate} onUnlocked={() => { setGate(null); load(); }} />
  );

  if (done) return (
    <Splash>
      <div className="text-center max-w-md">
        <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-full"
             style={{ background: "#E1F1F3", color: TEAL }}><Check size={26} /></div>
        <h1 className="text-lg font-semibold text-ink mb-2">Done</h1>
        <p className="text-sm text-ink-muted leading-relaxed">{done}</p>
        <p className="mt-5 text-xs text-ink-soft">You can close this page.</p>
      </div>
    </Splash>
  );

  if (err && !view) return (
    <Splash>
      <div className="text-center max-w-md">
        <div className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-full bg-[#FDECEC] text-danger">
          <X size={26} />
        </div>
        <h1 className="text-lg font-semibold text-ink mb-2">This link doesn't work</h1>
        <p className="text-sm text-ink-muted leading-relaxed">{err}</p>
        <p className="mt-4 text-xs text-ink-soft">
          Links expire, and one that has already been used stops working. Ask
          whoever sent it to send a fresh one.
        </p>
      </div>
    </Splash>
  );

  if (!view) return null;
  const { envelope, me, others, already_signed } = view;
  const readOnly = !me.my_turn || me.status === "signed" || me.status === "declined";

  return (
    <div className="min-h-screen bg-[#F3F4F7]">
      {/* ── bar ──────────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-30 border-b border-border bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1500px] items-center gap-4 px-5 py-3">
          <KavachioLogo className="h-7 w-7 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold text-ink">{envelope.title}</div>
            <div className="truncate text-xs text-ink-soft">
              {[envelope.programme, envelope.term].filter(Boolean).join(" · ") ||
                "Ready for your signature"}
            </div>
          </div>
          <a href={signingPdfUrl(token)} target="_blank" rel="noreferrer"
             className="hidden items-center gap-1.5 rounded-md border border-border px-3 py-2
                        text-[13px] font-medium text-ink hover:bg-surface-2 sm:inline-flex">
            <Download size={15} /> Download
          </a>
          {!readOnly && (
            <button onClick={finish} disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-md px-4 py-2 text-[13px]
                         font-semibold text-white disabled:opacity-60"
              style={{ background: TEAL }}>
              {busy ? <Loader2 className="animate-spin" size={15} /> : <PenLine size={15} />}
              {outstanding.length ? `Finish — ${outstanding.length} left` : "Finish signing"}
            </button>
          )}
        </div>
      </header>

      <div className="mx-auto grid max-w-[1500px] gap-5 px-5 py-5 lg:grid-cols-[1fr_340px]">
        {/* ── the document ──────────────────────────────────────────────── */}
        <main className="min-w-0 space-y-4">
          {err && (
            <div className="rounded-lg border border-[#F3C7C7] bg-[#FDECEC] px-4 py-3 text-[13px] text-[#8A2222]">
              {err}
            </div>
          )}
          {already_signed.length > 0 && (
            <div className="flex items-start gap-2.5 rounded-lg border px-4 py-3 text-[13px]"
                 style={{ borderColor: "#BEE3E8", background: "#EFF9FA", color: "#0B4A54" }}>
              <ShieldCheck size={16} className="mt-0.5 shrink-0" />
              <div>
                <b>{already_signed.map(a => `${a.org ?? a.name}`).join(", ")} has already signed.</b>{" "}
                What you are reading is that same document, with their signature
                on it — not a fresh copy.
              </div>
            </div>
          )}
          {readOnly && (
            <div className="rounded-lg border border-[#F0DCA8] bg-[#FFF8E6] px-4 py-3 text-[13px] text-[#6B5410]">
              {me.status === "signed"
                ? "You have already signed this contract."
                : me.status === "declined"
                ? "You declined this contract and it has gone back to the sender."
                : `It is not your turn yet — it is with ${
                    others.find(o => o.status === "sent" || o.status === "viewed")?.org ??
                    "the other party"}.`}
            </div>
          )}

          {Array.from({ length: envelope.page_count }, (_, i) => i + 1).map(pageNo => (
            <PageView
              key={pageNo}
              token={token}
              pageNo={pageNo}
              version={envelope.pdf_version}
              ratio={ratioFor(envelope.pages, pageNo)}
              fields={view.fields.filter(f => f.page === pageNo)}
              draft={draft}
              sigImage={sigImage}
              initialsImage={initialsImage}
              readOnly={readOnly}
              focus={focus}
              onFocus={setFocus}
              onChange={setValue}
              onSignature={() => setAdopt(true)}
            />
          ))}
        </main>

        {/* ── who you are and what is yours ────────────────────────────── */}
        <aside className="lg:sticky lg:top-[70px] lg:self-start space-y-4">
          <section className="rounded-xl border border-border bg-white p-4 shadow-card">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-ink-soft">
              You are signing as
            </div>
            <div className="text-[15px] font-semibold text-ink">{me.name}</div>
            <div className="text-[13px] text-ink-muted">{me.title}</div>
            <div className="mt-1 text-[13px] font-medium text-ink">{me.org}</div>
            <div className="mt-1 text-xs text-ink-soft break-all">{me.email}</div>
            {/* Shown, not hidden: this is the answer to "why are those boxes
                mine?", and a signer who was forwarded the wrong link sees it
                immediately. */}
            <div className="mt-3 rounded-lg bg-surface-2 px-3 py-2">
              <div className="text-[11px] font-medium uppercase tracking-wide text-ink-soft">
                Identified as
              </div>
              <div className="mt-0.5 text-[13px] font-medium text-ink">
                {me.side === "insurer" ? "The insurer" : "The broker"} on this contract
              </div>
              <div className="mt-1 text-[11.5px] leading-snug text-ink-soft">
                Boxes marked for {sideLabel(me.side)} are yours. No other box on
                this document will accept your signature.
              </div>
            </div>
          </section>

          <section className="rounded-xl border border-border bg-white p-4 shadow-card">
            <div className="mb-3 text-[11px] font-semibold uppercase tracking-wide text-ink-soft">
              Who signs this
            </div>
            {/* Everybody, in the order they are asked — the reader included.
                Numbered from the round rather than from this list: a side that
                sends two people makes this three or four rows long, and "you
                are 1" printed above somebody else who is also 1 is worse than
                no number at all. */}
            {[{ order: me.order, key: me.party_key, name: me.name,
                org: me.org, status: me.status, you: true },
              ...others.map(o => ({ order: o.order, key: o.party_key,
                                    name: o.name, org: o.org,
                                    status: o.status, you: false }))]
              .sort((a, b) => a.order - b.order)
              .map(r => (
                <Signer key={r.key} order={r.order} name={r.name}
                        org={r.org} status={r.status} you={r.you} />
              ))}
          </section>

          {!readOnly && (
            <section className="rounded-xl border border-border bg-white p-4 shadow-card">
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ink-soft">
                Your boxes
              </div>
              <div className="text-[13px] text-ink">
                {mine.length} on this document, {outstanding.length} still to fill.
              </div>
              <button
                onClick={() => goTo(outstanding[0] ?? mine[0])}
                className="mt-3 inline-flex w-full items-center justify-center gap-1.5 rounded-md
                           border border-border px-3 py-2 text-[13px] font-medium text-ink
                           hover:bg-surface-2">
                <ChevronDown size={15} /> Take me to the next one
              </button>

              <label className="mt-4 flex cursor-pointer items-start gap-2.5 text-[12.5px] leading-snug text-ink-muted">
                <input type="checkbox" checked={agreed} onChange={e => setAgreed(e.target.checked)}
                       className="mt-0.5 h-4 w-4 shrink-0 accent-[#077282]" />
                <span>
                  I agree to sign this contract electronically, on behalf of{" "}
                  <b className="text-ink">{me.org}</b>. My name, the time and this
                  device are recorded with the signature.
                </span>
              </label>

              <button onClick={finish} disabled={busy || !agreed}
                className="mt-3 flex w-full items-center justify-center gap-1.5 rounded-md px-3 py-2.5
                           text-[13px] font-semibold text-white disabled:opacity-50"
                style={{ background: TEAL }}>
                {busy ? <Loader2 className="animate-spin" size={15} /> : <PenLine size={15} />}
                {outstanding.length ? `Finish — ${outstanding.length} left` : "Finish signing"}
              </button>
              <button onClick={() => setDeclining(true)}
                className="mt-2 w-full rounded-md px-3 py-2 text-[12.5px] font-medium
                           text-ink-muted hover:bg-surface-2">
                Something needs changing
              </button>
            </section>
          )}
        </aside>
      </div>

      {adopt && (
        <AdoptSignature
          initialName={sigName}
          onCancel={() => setAdopt(false)}
          onAdopt={applySignature}
        />
      )}

      {blocked && (
        <NothingSignedYet
          outstanding={blocked}
          onClose={() => setBlocked(null)}
          onGo={f => { setBlocked(null); goTo(f); }}
        />
      )}

      {declining && (
        <DeclineDialog
          reason={reason} setReason={setReason} busy={busy}
          onCancel={() => setDeclining(false)} onSend={doDecline}
        />
      )}
    </div>
  );
}

// ── one page of the document, with its boxes laid over it ──────────────────
function PageView({
  token, pageNo, version, ratio, fields, draft, sigImage, initialsImage,
  readOnly, focus,
  onFocus, onChange, onSignature,
}: {
  token: string; pageNo: number; version: number; ratio: number;
  fields: EsignField[]; draft: Draft;
  sigImage: string | null; initialsImage: string | null; readOnly: boolean;
  focus: number | null;
  onFocus: (id: number | null) => void;
  onChange: (id: number, v: string) => void;
  onSignature: () => void;
}) {
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-white shadow-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <span className="text-[12px] font-medium text-ink-muted">Page {pageNo}</span>
        <span className="text-[11.5px] text-ink-soft">
          {fields.filter(f => f.mine).length
            ? `${fields.filter(f => f.mine).length} of your boxes on this page`
            : "nothing for you on this page"}
        </span>
      </div>
      {/* The box positions are fractions of the page, so the wrapper only has
          to hold the page's aspect ratio and everything inside lands correctly
          at any width. */}
      <div className="relative w-full" style={{ paddingTop: `${ratio * 100}%` }}>
        <img
          src={signingPageUrl(token, pageNo, version, 2)}
          alt={`Page ${pageNo}`}
          loading={pageNo <= 2 ? "eager" : "lazy"}
          className="absolute inset-0 h-full w-full object-contain"
        />
        {fields.map(f => (
          <FieldBox
            key={f.id} f={f} value={draft[f.id] ?? f.value ?? ""}
            sigImage={f.type === "initial" ? initialsImage : sigImage}
            readOnly={readOnly} focused={focus === f.id}
            onFocus={onFocus} onChange={onChange} onSignature={onSignature}
          />
        ))}
      </div>
    </section>
  );
}

/** One box. Mine = fillable and teal. Theirs = locked, greyed, and labelled
 *  with whose it is — hiding it would make the document look altered. */
function FieldBox({
  f, value, sigImage, readOnly, focused, onFocus, onChange, onSignature,
}: {
  f: EsignField; value: string; sigImage: string | null; readOnly: boolean;
  focused: boolean;
  onFocus: (id: number | null) => void;
  onChange: (id: number, v: string) => void;
  onSignature: () => void;
}) {
  const pos = {
    left: `${f.x * 100}%`, top: `${f.y * 100}%`,
    width: `${f.w * 100}%`, height: `${f.h * 100}%`,
  } as const;
  const isSig = f.type === "signature" || f.type === "initial";

  if (!f.mine) {
    return (
      <div
        style={pos}
        title={`${f.owner_org ?? "The other party"} — ${sideLabel(f.owner_side)}`}
        className={`absolute flex items-center gap-1 overflow-hidden rounded border
                    border-dashed px-1.5 text-[10px] leading-none
                    ${f.filled
                      ? "border-transparent bg-transparent"
                      : "border-[#CBD2DC] bg-[#F6F7F9]/70 text-ink-soft"}`}
      >
        {/* A filled box shows nothing: the value is burned into the page image
            underneath, and drawing it twice would look like a correction. */}
        {!f.filled && (
          <>
            <Lock size={9} className="shrink-0" />
            <span className="truncate">{f.owner_org ?? f.owner_name ?? "Other party"}</span>
          </>
        )}
      </div>
    );
  }

  if (isSig) {
    const signed = !!value;
    return (
      <button
        id={`fld-${f.id}`}
        type="button"
        disabled={readOnly}
        onClick={() => { onFocus(f.id); onSignature(); }}
        // An empty box is deliberately loud: the commonest failure in a signing
        // flow is a person who cannot find where to sign. A filled one goes
        // quiet, because by then the signature itself is the thing to look at.
        style={signed
          ? pos
          : { ...pos, background: "rgba(7,114,130,.08)",
              boxShadow: `inset 0 0 0 2px ${TEAL}` }}
        className={`absolute flex items-center justify-start overflow-hidden rounded px-1.5
                    transition ${focused ? "ring-2 ring-[#B7DEE4]" : ""}`}
      >
        {signed ? (
          sigImage
            ? <img src={sigImage}
                   alt={f.type === "initial" ? "Your initials" : "Your signature"}
                   className="max-h-full max-w-full object-contain object-left-bottom" />
            : <span className="truncate text-ink"
                    style={{ fontFamily: SIG_FONT, fontSize: "clamp(11px,1.5vw,22px)" }}>
                {value}
              </span>
        ) : (
          <span className="flex items-center gap-1 text-[10px] font-semibold uppercase
                           tracking-wide" style={{ color: TEAL }}>
            <PenLine size={10} /> {f.type === "initial" ? "Initial" : "Sign"}
          </span>
        )}
      </button>
    );
  }

  return (
    <input
      id={`fld-${f.id}`}
      value={value}
      disabled={readOnly}
      placeholder={f.label ?? ""}
      onFocus={() => onFocus(f.id)}
      onChange={e => onChange(f.id, e.target.value)}
      style={{
        ...pos,
        fontSize: "clamp(8px,0.85vw,12px)",
        ...(value
          ? { border: "1px solid transparent", background: "transparent" }
          : { boxShadow: `inset 0 0 0 2px ${TEAL}`, background: "rgba(7,114,130,.06)" }),
      }}
      className={`absolute rounded px-1 text-ink outline-none transition
                  ${focused ? "ring-2 ring-[#B7DEE4]" : ""}`}
    />
  );
}

function Signer({ order, name, org, status, you = false }: {
  order: number; name: string; org: string | null; status: string; you?: boolean;
}) {
  const tone =
    status === "signed" ? { bg: "#E7F6EC", fg: "#166534", label: "Signed" } :
    status === "declined" ? { bg: "#FDECEC", fg: "#8A2222", label: "Declined" } :
    status === "sent" || status === "viewed" ? { bg: "#FFF3D6", fg: "#6B5410", label: "Their turn" } :
    { bg: "#F1F2F5", fg: "#6B7280", label: "Waiting" };
  return (
    <div className="flex items-center gap-2.5 border-t border-border py-2.5 first:border-t-0 first:pt-0">
      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-surface-2
                       text-[11px] font-semibold text-ink-muted">{order}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium text-ink">
          {name}{you && <span className="ml-1 text-[11px] font-normal text-ink-soft">(you)</span>}
        </span>
        <span className="block truncate text-[11.5px] text-ink-soft">{org}</span>
      </span>
      <span className="shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-semibold"
            style={{ background: tone.bg, color: tone.fg }}>{tone.label}</span>
    </div>
  );
}

// ── adopting a signature ───────────────────────────────────────────────────

/** How wide an uploaded signature is kept. A signature box on the page is a
 *  couple of inches; anything past this is detail nobody will ever see, paid
 *  for in a fatter PDF on every page it is stamped on. */
const UPLOAD_MAX_PX = 1000;
/** Anything lighter than this is paper, not ink. Deliberately generous: a photo
 *  of a page is never white, it is grey with a shadow across one side, and a
 *  strict threshold leaves that shadow behind as a dirty rectangle. */
const PAPER_LUMA = 205;
/** What a phone camera will hand over. Bigger than this is a mistake — a
 *  screenshot of a whole desktop, a RAW file — and the message says so rather
 *  than letting the page appear to hang while it decodes 40 megapixels. */
const MAX_UPLOAD_BYTES = 12 * 1024 * 1024;

/** Turn a photo or scan of a signature into something that can be stamped onto
 *  a page without covering it up.
 *
 *  This is not decoration. A signature is stamped ON the document, over the
 *  ruled line and next to printed text, so an image with its paper still
 *  attached lands as a white postcard pasted across the page. Three things have
 *  to happen and none of them can happen server-side without guessing:
 *
 *    · the paper is dropped   — anything lighter than PAPER_LUMA becomes
 *                               transparent, so what is left is the ink;
 *    · the ink is cropped to  — a phone photo is mostly desk, and left uncropped
 *      what was actually      the signature shrinks to fit the desk inside the
 *      written                box rather than filling it;
 *    · it is scaled down      — a 4000px photo carries nothing a 1000px one does
 *                               not, at ten times the bytes, in a file that is
 *                               then embedded in every version of the PDF.
 *
 *  Rejects rather than guesses when there is no ink to find: a blank page or a
 *  photo of a dark room produces either nothing or a solid block, and a solid
 *  block stamped over a contract is worse than being told to try again.
 */
async function signatureFromImage(file: File): Promise<string> {
  if (!file.type.startsWith("image/")) {
    throw new Error("That is not an image. A photo or a scan — PNG or JPEG.");
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error("That image is too big. Anything up to 12 MB — a photo "
                    + "straight from a phone is fine.");
  }

  const url = URL.createObjectURL(file);
  let img: HTMLImageElement;
  try {
    img = await new Promise<HTMLImageElement>((ok, no) => {
      const el = new Image();
      el.onload = () => ok(el);
      el.onerror = () => no(new Error("That image could not be opened. Try a "
                                      + "PNG or a JPEG."));
      el.src = url;
    });
  } finally {
    URL.revokeObjectURL(url);
  }

  const scale = Math.min(1, UPLOAD_MAX_PX / Math.max(img.width, img.height));
  const w = Math.max(1, Math.round(img.width * scale));
  const h = Math.max(1, Math.round(img.height * scale));
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("This browser could not read that image.");
  ctx.drawImage(img, 0, 0, w, h);

  let data: ImageData;
  try {
    data = ctx.getImageData(0, 0, w, h);
  } catch {
    // Only reachable if the canvas was somehow tainted. Nothing here is
    // cross-origin, but a thrown SecurityError must read as an explanation.
    throw new Error("That image could not be read. Try saving it as a PNG.");
  }
  const px = data.data;
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      // Already transparent — a PNG someone has cut out already. Leave it that
      // way and keep it out of the crop, or the bounding box is the whole
      // canvas and the crop does nothing.
      if (px[i + 3] < 16) { px[i + 3] = 0; continue; }
      const luma = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
      if (luma >= PAPER_LUMA) { px[i + 3] = 0; continue; }
      // Ink. Fade the near-paper greys out proportionally instead of leaving a
      // hard edge — that edge is what makes a cut-out look cut out.
      px[i + 3] = Math.round(255 * Math.min(1, (PAPER_LUMA - luma) / 60));
      if (x < x0) x0 = x;
      if (y < y0) y0 = y;
      if (x > x1) x1 = x;
      if (y > y1) y1 = y;
    }
  }
  if (x1 < 0) {
    throw new Error("No signature could be found in that image — it came out "
                    + "blank. A darker pen, or a photo with more light on it.");
  }
  ctx.putImageData(data, 0, 0);

  // A couple of pixels of air, so the ink is not flush against the edge of its
  // own box when the PDF scales it up.
  const pad = 2;
  x0 = Math.max(0, x0 - pad); y0 = Math.max(0, y0 - pad);
  x1 = Math.min(w - 1, x1 + pad); y1 = Math.min(h - 1, y1 + pad);
  const cw = x1 - x0 + 1, ch = y1 - y0 + 1;
  const out = document.createElement("canvas");
  out.width = cw; out.height = ch;
  out.getContext("2d")?.drawImage(c, x0, y0, cw, ch, 0, 0, cw, ch);
  return out.toDataURL("image/png");
}

/** The initials somebody would write: one letter per name, so "Mahesh H Sutar"
 *  gives MHS. A starting point, not a rule — the field is editable, because
 *  plenty of people initial with something else entirely. */
function initialsFrom(name: string): string {
  return name.trim().split(/\s+/).filter(Boolean)
    .map(w => w[0]).join("").toUpperCase().slice(0, 5);
}

/** One drawable box. There are two on this dialog, and they are two different
 *  marks — see the dialog's own comment. */
function useSketch(active: boolean) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const drawing = useRef(false);
  const [hasInk, setHasInk] = useState(false);

  useEffect(() => {
    const c = ref.current;
    if (!c || !active) return;
    // Match the backing store to the CSS size × DPR, or the line is a blurry
    // rectangle on every retina screen.
    const dpr = window.devicePixelRatio || 1;
    const r = c.getBoundingClientRect();
    c.width = Math.round(r.width * dpr);
    c.height = Math.round(r.height * dpr);
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.lineWidth = 2.2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#0E1320";
    setHasInk(false);
  }, [active]);

  function at(e: React.PointerEvent<HTMLCanvasElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }
  return {
    ref, hasInk,
    handlers: {
      onPointerDown(e: React.PointerEvent<HTMLCanvasElement>) {
        const ctx = ref.current?.getContext("2d"); if (!ctx) return;
        e.currentTarget.setPointerCapture(e.pointerId);
        drawing.current = true;
        const p = at(e); ctx.beginPath(); ctx.moveTo(p.x, p.y);
      },
      onPointerMove(e: React.PointerEvent<HTMLCanvasElement>) {
        if (!drawing.current) return;
        const ctx = ref.current?.getContext("2d"); if (!ctx) return;
        const p = at(e); ctx.lineTo(p.x, p.y); ctx.stroke(); setHasInk(true);
      },
      onPointerUp() { drawing.current = false; },
      onPointerLeave() { drawing.current = false; },
    },
    clear() {
      const c = ref.current; const ctx = c?.getContext("2d");
      if (c && ctx) { ctx.clearRect(0, 0, c.width, c.height); setHasInk(false); }
    },
    dataUrl(): string | null {
      return hasInk && ref.current ? ref.current.toDataURL("image/png") : null;
    },
  };
}

/** Type it, draw it, or upload one, then it goes on every signature box at
 *  once. All three are offered because all three are what people expect: a
 *  drawn scrawl is what a signature looks like, a typed one is what a signature
 *  on a phone is, and an uploaded one is what someone who signs a lot of
 *  contracts already has sitting in a file.
 *
 *  TWO MARKS COME OUT OF IT, not one used twice. A signature and a set of
 *  initials do different jobs on a contract — the signature closes the
 *  document, the initials acknowledge a page, a clause, or an amendment — and
 *  stamping the same scrawl in both places means neither says which was meant.
 *  It also stops being defensible: initials that ARE the signature let anyone
 *  holding one page of the document reproduce the mark on the last one. */
function AdoptSignature({ initialName, onCancel, onAdopt }: {
  initialName: string;
  onCancel: () => void;
  onAdopt: (name: string, marks: {
    initials: string; image: string | null; initialsImage: string | null;
  }) => void;
}) {
  const [tab, setTab] = useState<"type" | "draw" | "upload">("type");
  const [name, setName] = useState(initialName);
  // Follows the name until somebody types their own, then stops — plenty of
  // people initial with something that is not their initials.
  const [initials, setInitials] = useState(initialsFrom(initialName));
  const [initialsEdited, setInitialsEdited] = useState(false);
  const sig = useSketch(tab === "draw");
  const ini = useSketch(tab === "draw");
  // The uploaded signature, already cleaned. Held as the finished data URL
  // rather than the File, so what is previewed is exactly what is adopted —
  // and what the document will carry.
  const file = useRef<HTMLInputElement | null>(null);
  const [uploaded, setUploaded] = useState<string | null>(null);
  const [uploadErr, setUploadErr] = useState<string | null>(null);
  const [reading, setReading] = useState(false);

  async function pick(f: File | null | undefined) {
    if (!f) return;
    setReading(true); setUploadErr(null);
    try {
      setUploaded(await signatureFromImage(f));
    } catch (e) {
      setUploaded(null);
      setUploadErr(e instanceof Error ? e.message
                                      : "That image could not be used.");
    } finally {
      setReading(false);
      // Cleared so choosing the SAME file again after a failure still fires a
      // change event. Without this, a signer who fixes the lighting and picks
      // the same filename gets nothing at all.
      if (file.current) file.current.value = "";
    }
  }

  function adopt() {
    const trimmed = name.trim();
    if (!trimmed) return;
    const mark = tab === "draw" ? sig.dataUrl()
               : tab === "upload" ? uploaded
               : null;   // typed: the name itself is the mark, stamped as text
    onAdopt(trimmed, {
      // Never empty: an initials box stamps this text when there is no drawing
      // for it, and an empty box would fail the "still to fill in" check with
      // nothing the signer could do about it.
      initials: (initials.trim() || initialsFrom(trimmed) || trimmed[0]).toUpperCase(),
      image: mark,
      // Only ever a DRAWN one. Typed initials are stamped as text, and an
      // uploaded signature is a signature — reusing it for the initials would
      // put the two marks back together, which is the thing this separates.
      initialsImage: tab === "draw" ? ini.dataUrl() : null,
    });
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onCancel} />
      <div className="relative z-10 w-full max-w-lg rounded-xl border border-border bg-white shadow-xl">
        <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-base font-semibold text-ink">Adopt your signature</h2>
          <button onClick={onCancel} className="text-ink-soft hover:text-ink"><X size={18} /></button>
        </header>
        <div className="px-5 py-4">
          <div className="flex gap-3">
            <div className="min-w-0 flex-1">
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink">Full name</label>
              <input value={name}
                     onChange={e => {
                       setName(e.target.value);
                       if (!initialsEdited) setInitials(initialsFrom(e.target.value));
                     }}
                     className="w-full rounded-lg border border-border px-3 py-2.5 text-[13.5px] text-ink
                                outline-none focus:border-[#077282] focus:ring-[3px] focus:ring-[#E1F1F3]" />
            </div>
            <div className="w-[110px] shrink-0">
              <label className="mb-1.5 block text-[12.5px] font-medium text-ink">Initials</label>
              <input value={initials} maxLength={5}
                     onChange={e => {
                       setInitials(e.target.value.toUpperCase());
                       setInitialsEdited(true);
                     }}
                     className="w-full rounded-lg border border-border px-3 py-2.5 text-[13.5px] text-ink
                                outline-none focus:border-[#077282] focus:ring-[3px] focus:ring-[#E1F1F3]" />
            </div>
          </div>

          <div className="mt-4 inline-flex rounded-lg bg-surface-2 p-1 text-[12.5px] font-medium">
            {(["type", "draw", "upload"] as const).map(t => (
              <button key={t} onClick={() => setTab(t)}
                className={`rounded-md px-3.5 py-1.5 transition ${
                  tab === t ? "bg-white text-ink shadow-sm" : "text-ink-muted"}`}>
                {t === "type" ? "Type it" : t === "draw" ? "Draw it" : "Upload it"}
              </button>
            ))}
          </div>

          {tab === "type" ? (
            <div className="mt-3 flex gap-3">
              <div className="grid h-[120px] flex-1 place-items-center rounded-lg
                              border border-border bg-[#FAFBFC]">
                <span className="px-4 text-center text-ink"
                      style={{ fontFamily: SIG_FONT, fontSize: 34 }}>
                  {name || "Your name"}
                </span>
              </div>
              <div className="grid h-[120px] w-[110px] shrink-0 place-items-center
                              rounded-lg border border-border bg-[#FAFBFC]">
                <span className="px-2 text-center text-ink"
                      style={{ fontFamily: SIG_FONT, fontSize: 28 }}>
                  {initials || initialsFrom(name) || "—"}
                </span>
              </div>
            </div>
          ) : tab === "draw" ? (
            <div className="mt-3">
              <div className="flex gap-3">
                <div className="min-w-0 flex-1">
                  <canvas ref={sig.ref} {...sig.handlers}
                          className="h-[120px] w-full touch-none rounded-lg border border-border bg-[#FAFBFC]" />
                  <div className="mt-1 flex items-center justify-between">
                    <span className="text-[11.5px] text-ink-soft">Signature</span>
                    <button onClick={sig.clear}
                      className="text-[12px] font-medium text-ink-muted hover:text-ink">
                      Clear
                    </button>
                  </div>
                </div>
                <div className="w-[110px] shrink-0">
                  <canvas ref={ini.ref} {...ini.handlers}
                          className="h-[120px] w-full touch-none rounded-lg border border-border bg-[#FAFBFC]" />
                  <div className="mt-1 flex items-center justify-between">
                    <span className="text-[11.5px] text-ink-soft">Initials</span>
                    <button onClick={ini.clear}
                      className="text-[12px] font-medium text-ink-muted hover:text-ink">
                      Clear
                    </button>
                  </div>
                </div>
              </div>
              {!ini.hasInk && (
                <p className="mt-1.5 text-[12px] text-ink-soft">
                  Leave the initials box empty and “{initials || initialsFrom(name)}”
                  is written in the initials boxes instead. What is never done is
                  putting your signature in them.
                </p>
              )}
            </div>
          ) : (
            <div className="mt-3">
              {/* The preview is shown on a CHEQUERED ground, not on white. What
                  is adopted has no background of its own — it goes over the
                  ruled line on the page — and a preview on white would hide
                  exactly the failure this needs to make visible: paper that did
                  not come off. */}
              <div
                className="grid h-[120px] place-items-center rounded-lg border border-border"
                style={uploaded ? {
                  backgroundColor: "#FFFFFF",
                  backgroundImage:
                    "linear-gradient(45deg,#EEF1F4 25%,transparent 25%,transparent 75%,#EEF1F4 75%),"
                    + "linear-gradient(45deg,#EEF1F4 25%,transparent 25%,transparent 75%,#EEF1F4 75%)",
                  backgroundSize: "14px 14px",
                  backgroundPosition: "0 0, 7px 7px",
                } : { background: "#FAFBFC" }}
              >
                {reading ? (
                  <span className="flex items-center gap-2 text-[12.5px] text-ink-muted">
                    <Loader2 className="animate-spin" size={15} /> Reading it…
                  </span>
                ) : uploaded ? (
                  <img src={uploaded} alt="Your signature"
                       className="max-h-[104px] max-w-[92%] object-contain" />
                ) : (
                  <button onClick={() => file.current?.click()}
                    className="flex flex-col items-center gap-1.5 px-4 text-center">
                    <Upload size={18} className="text-ink-soft" />
                    <span className="text-[12.5px] font-medium text-ink">
                      Choose a photo or scan
                    </span>
                    <span className="text-[11.5px] text-ink-soft">
                      PNG or JPEG. The paper is taken off for you.
                    </span>
                  </button>
                )}
              </div>
              <input ref={file} type="file" accept="image/*" className="hidden"
                     onChange={e => pick(e.target.files?.[0])} />
              {uploaded && (
                <button onClick={() => { setUploaded(null); setUploadErr(null); }}
                  className="mt-1.5 text-[12px] font-medium text-ink-muted hover:text-ink">
                  Choose a different image
                </button>
              )}
              {uploadErr && (
                <p className="mt-1.5 text-[12px] leading-relaxed text-[#8A2222]">
                  {uploadErr}
                </p>
              )}
              <p className="mt-1.5 text-[12px] leading-relaxed text-ink-soft">
                This is your signature. The initials boxes get
                “{initials || initialsFrom(name)}” — draw them instead on the
                Draw it tab if you would rather they were in your own hand.
              </p>
            </div>
          )}

          <p className="mt-4 text-[12px] leading-relaxed text-ink-soft">
            The signature goes on every signature box that is yours on this
            document, and the initials on every initials box — two marks, never
            one used twice. It
            is applied on behalf of your organisation, not you personally, and
            the time and device are recorded beside it. Whichever way you make
            it, the same name, time and address are recorded — an uploaded
            signature carries no more weight than a typed one, and no less.
          </p>
        </div>
        <footer className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
          <button onClick={onCancel}
            className="rounded-md border border-border px-3.5 py-2 text-[13px] font-medium text-ink
                       hover:bg-surface-2">Cancel</button>
          {/* Blocked on the upload tab until there IS one. Falling back to the
              typed name there would hand back a signature the signer can see
              they did not choose, on the one screen where that matters most. */}
          <button onClick={adopt}
            disabled={!name.trim() || reading || (tab === "upload" && !uploaded)}
            className="rounded-md px-4 py-2 text-[13px] font-semibold text-white disabled:opacity-50"
            style={{ background: TEAL }}>
            Adopt and place it
          </button>
        </footer>
      </div>
    </div>
  );
}

/** Raised when Finish is pressed with boxes still empty.
 *
 *  It leads with the SIGNATURE when that is what is missing, because that is
 *  the case the signer is actually in — they read the contract, ticked the
 *  agreement box, and went straight for the button without putting a signature
 *  anywhere. "Still to fill in: Signature, Date" states the same fact and
 *  teaches them nothing about what to do next.
 */
function NothingSignedYet({ outstanding, onClose, onGo }: {
  outstanding: EsignField[];
  onClose: () => void;
  onGo: (f: EsignField) => void;
}) {
  const unsigned = outstanding.find(f => f.type === "signature" || f.type === "initial");
  const target = unsigned ?? outstanding[0];
  const others = outstanding.filter(f => f !== unsigned);
  const otherNames = [...new Set(others.map(f => f.label ?? f.type))];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative z-10 w-full max-w-md rounded-xl border border-border bg-white shadow-xl">
        <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-base font-semibold text-ink">
            {unsigned ? "You haven't signed it yet" : "Something is still to fill in"}
          </h2>
          <button onClick={onClose} className="text-ink-soft hover:text-ink"><X size={18} /></button>
        </header>
        <div className="px-5 py-4">
          <div className="mb-3 flex items-start gap-2.5 rounded-lg px-3 py-2.5 text-[13px]"
               style={{ background: "#FFF8E6", color: "#6B5410" }}>
            <PenLine size={16} className="mt-0.5 shrink-0" />
            <span>
              {unsigned
                ? <>Your signature box on page {unsigned.page} is still empty. Please
                    sign the document first, then press Finish.</>
                : <>These are still empty: <b>{otherNames.join(", ")}</b>.</>}
            </span>
          </div>
          {unsigned && otherNames.length > 0 && (
            <p className="mb-3 text-[12.5px] leading-relaxed text-ink-muted">
              Also still to fill in: {otherNames.join(", ")}.
            </p>
          )}
          <p className="text-[12.5px] leading-relaxed text-ink-muted">
            Nothing has been sent and nothing is recorded against your name.
            The contract only goes to the other party once you have signed it.
          </p>
        </div>
        <footer className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
          <button onClick={onClose}
            className="rounded-md border border-border px-3.5 py-2 text-[13px] font-medium text-ink
                       hover:bg-surface-2">Close</button>
          <button onClick={() => onGo(target)} autoFocus
            className="rounded-md px-4 py-2 text-[13px] font-semibold text-white"
            style={{ background: TEAL }}>
            {unsigned ? "Take me to my signature" : "Take me to it"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function DeclineDialog({ reason, setReason, busy, onCancel, onSend }: {
  reason: string; setReason: (v: string) => void; busy: boolean;
  onCancel: () => void; onSend: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onCancel} />
      <div className="relative z-10 w-full max-w-md rounded-xl border border-border bg-white shadow-xl">
        <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-base font-semibold text-ink">Ask for a change</h2>
          <button onClick={onCancel} className="text-ink-soft hover:text-ink"><X size={18} /></button>
        </header>
        <div className="px-5 py-4">
          <p className="mb-3 text-[13px] leading-relaxed text-ink-muted">
            Nothing is signed and no half-agreed version is created. Your reason
            goes straight back to whoever sent it, so say which term is wrong and
            what it should be.
          </p>
          <textarea value={reason} onChange={e => setReason(e.target.value)} rows={4}
            placeholder="e.g. Commission should be 12.5%, not 15%."
            className="w-full rounded-lg border border-border px-3 py-2.5 text-[13.5px] text-ink
                       outline-none focus:border-[#077282] focus:ring-[3px] focus:ring-[#E1F1F3]" />
        </div>
        <footer className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
          <button onClick={onCancel}
            className="rounded-md border border-border px-3.5 py-2 text-[13px] font-medium text-ink
                       hover:bg-surface-2">Keep reading</button>
          <button onClick={onSend} disabled={busy || reason.trim().length < 4}
            className="rounded-md bg-danger px-4 py-2 text-[13px] font-semibold text-white
                       disabled:opacity-50">
            {busy ? "Sending…" : "Send it back"}
          </button>
        </footer>
      </div>
    </div>
  );
}

// ── the one-time code ──────────────────────────────────────────────────────
/** Everything a signer sees until they type the code from their email.
 *
 *  It says almost nothing on purpose. The contract's title, the parties, even
 *  the number of pages are all behind the code on the SERVER — so there is
 *  nothing here to show, and nothing a stranger with the URL can learn beyond
 *  "this link is real and the code went to m****o@…", which is what a signer
 *  who has three inboxes genuinely needs.
 */
function CodeGate({ token, gate, onUnlocked }: {
  token: string; gate: LockedView; onUnlocked: () => void;
}) {
  const LEN = 6;
  const [digits, setDigits] = useState<string[]>(Array(LEN).fill(""));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [left, setLeft] = useState(gate.lockout_seconds);
  const boxes = useRef<(HTMLInputElement | null)[]>([]);

  // Count the lockout down in front of them. "Try again in 15 minutes" with no
  // clock is the point most people give up and telephone somebody.
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
    // A pasted or fast-typed run fills forward from here rather than landing
    // its whole length in one box.
    setDigits(d => {
      const next = [...d];
      for (let k = 0; k < only.length && i + k < LEN; k++) next[i + k] = only[k];
      return next;
    });
    boxes.current[Math.min(i + only.length, LEN - 1)]?.focus();
  }

  function onKey(i: number, e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Backspace" && !digits[i] && i > 0) boxes.current[i - 1]?.focus();
    if (e.key === "ArrowLeft" && i > 0) boxes.current[i - 1]?.focus();
    if (e.key === "ArrowRight" && i < LEN - 1) boxes.current[i + 1]?.focus();
  }

  async function submit(e?: React.FormEvent) {
    e?.preventDefault();
    if (code.length !== LEN || busy || lockedOut) return;
    setBusy(true); setErr(null); setNote(null);
    try {
      await verifyCode(token, code);
      onUnlocked();
    } catch (ex: any) {
      const detail = ex?.response?.data?.detail ?? "That code did not work.";
      setErr(detail);
      setDigits(Array(LEN).fill(""));
      boxes.current[0]?.focus();
      // The server owns the lockout; read it back rather than guessing.
      const m = /(\d+)\s*minutes?/.exec(detail);
      if (ex?.response?.status === 429) setLeft(m ? Number(m[1]) * 60 : 15 * 60);
    } finally { setBusy(false); }
  }

  // Submit as soon as the last digit lands — nobody wants to reach for a button
  // after typing six numbers.
  useEffect(() => {
    if (code.length === LEN && !busy && !lockedOut) submit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  async function resend() {
    setBusy(true); setErr(null); setNote(null);
    try {
      const r = await resendCode(token);
      setNote(r.ok
        ? `A new code is on its way to ${r.sent_to}.`
        : "We could not send a new code just now.");
    } catch (ex: any) {
      setErr(ex?.response?.data?.detail ?? "Could not send a new code.");
    } finally { setBusy(false); }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-[#F3F4F7] px-5 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-2">
          <KavachioLogo className="h-8 w-8" />
          <span className="text-[15px] font-semibold text-ink">Kavachio</span>
        </div>
        <form onSubmit={submit}
          className="rounded-2xl border border-border bg-white px-7 py-8 shadow-card">
          <div className="mx-auto mb-4 grid h-12 w-12 place-items-center rounded-full"
               style={{ background: "#E1F1F3", color: TEAL }}>
            <KeyRound size={22} />
          </div>
          <h1 className="text-center text-[17px] font-semibold text-ink">
            Enter your code
          </h1>
          <p className="mx-auto mt-2 max-w-[22rem] text-center text-[13px] leading-relaxed text-ink-muted">
            This contract is protected. The same email that brought you here has a
            six-digit code in it — type it below to open the document.
          </p>
          <div className="mt-3 flex items-center justify-center gap-1.5 text-[12.5px] text-ink-soft">
            <Mail size={13} /> {gate.email_hint}
          </div>

          <div className="mt-6 flex justify-center gap-2">
            {digits.map((d, i) => (
              <input
                key={i}
                ref={el => { boxes.current[i] = el; }}
                value={d}
                onChange={e => put(i, e.target.value)}
                onKeyDown={e => onKey(i, e)}
                disabled={busy || lockedOut}
                inputMode="numeric"
                autoComplete={i === 0 ? "one-time-code" : "off"}
                maxLength={LEN}
                aria-label={`Digit ${i + 1}`}
                className="h-14 w-11 rounded-lg border text-center text-[22px] font-semibold
                           text-ink outline-none transition focus:border-[#077282]
                           focus:ring-[3px] focus:ring-[#E1F1F3] disabled:bg-surface-2
                           disabled:text-ink-soft"
                style={{ borderColor: err ? "#DC2626" : "#D2D7E0" }}
              />
            ))}
          </div>

          {err && (
            <p className="mt-4 text-center text-[13px] text-danger">{err}</p>
          )}
          {note && (
            <p className="mt-4 text-center text-[13px]" style={{ color: TEAL }}>{note}</p>
          )}
          {lockedOut && (
            <p className="mt-4 rounded-lg bg-[#FFF8E6] px-3 py-2.5 text-center text-[12.5px] text-[#6B5410]">
              Too many wrong codes. Try again in{" "}
              <b>{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</b>.
            </p>
          )}
          {!err && !lockedOut && gate.attempts_left > 0 && gate.attempts_left < 5 && (
            <p className="mt-4 text-center text-[12.5px] text-ink-soft">
              {gate.attempts_left} attempt{gate.attempts_left === 1 ? "" : "s"} left.
            </p>
          )}

          <button type="submit" disabled={busy || lockedOut || code.length !== LEN}
            className="mt-6 flex w-full items-center justify-center gap-1.5 rounded-lg py-3
                       text-sm font-semibold text-white disabled:opacity-50"
            style={{ background: TEAL }}>
            {busy ? <Loader2 className="animate-spin" size={16} /> : <Lock size={15} />}
            Open the contract
          </button>

          {gate.can_resend && (
            <button type="button" onClick={resend} disabled={busy}
              className="mt-3 w-full rounded-lg py-2 text-[12.5px] font-medium
                         text-ink-muted hover:bg-surface-2 disabled:opacity-50">
              Didn't get it? Send a new code
            </button>
          )}

          <p className="mt-5 border-t border-border pt-4 text-center text-[11.5px]
                        leading-relaxed text-ink-soft">
            Nobody at Kavachio will ever ask you for this code. If you were not
            expecting this contract, do not enter it — tell the sender instead.
          </p>
        </form>
      </div>
    </div>
  );
}

function Splash({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid min-h-screen place-items-center bg-[#F3F4F7] px-5">
      <div className="flex items-center gap-2 text-[14px] text-ink-muted">{children}</div>
    </div>
  );
}

/** height / width for a page, so the wrapper reserves the right space before
 *  the image arrives and the boxes never jump once it does. */
function ratioFor(pages: { width: number; height: number }[], pageNo: number): number {
  const p = pages[pageNo - 1];
  return p && p.width > 0 ? p.height / p.width : 842 / 595;
}
