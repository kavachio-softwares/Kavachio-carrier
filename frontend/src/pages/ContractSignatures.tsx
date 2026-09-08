/**
 * Step 4 of Create-a-Contract — Signatures, from the carrier's side.
 *
 * Reached from Contracts ("Signature history") or from a contract's own record,
 * never from the sidebar: a signing round is something a CONTRACT is going
 * through, and a top-level tab made it look like a separate thing to manage.
 * `?contract=<id>` narrows it to one contract's rounds — that is the link the
 * record opens, and it is the only difference between the two ways in.
 *
 * The signing itself happens in email, on the public /sign page. This screen is
 * the other half of that: what is out, who is holding it up, and the two things
 * a carrier can actually do about it — nudge them, or pull it back.
 *
 * It also exposes each signer's link. That is not a convenience: an emailed
 * link that lands in a spam folder is the single commonest way a signing round
 * stalls, and a carrier who can copy the link and paste it into a message
 * unblocks it in seconds instead of raising a support ticket.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ArrowLeft, Ban, BellRing, Check, ChevronRight, Copy, Download, FileSignature,
  Loader2, RefreshCw,
} from "lucide-react";
import {
  ENVELOPE_STATUS, RECIPIENT_STATUS, getEnvelope, keyExplains, listEnvelopes,
  remindEnvelope, voidEnvelope, type Envelope,
} from "../api/esign";
import { downloadFile } from "../api/client";

const TEAL = "#077282";

export default function ContractSignatures() {
  const [rows, setRows] = useState<Envelope[] | null>(null);
  const [open, setOpen] = useState<Envelope | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  // One contract, or all of them. Held in the URL rather than in state so the
  // filtered view is a link the record can hand out — and so a refresh, or a
  // link pasted to a colleague, lands on the same contract.
  const [params] = useSearchParams();
  const only = Number(params.get("contract") ?? "") || 0;

  const load = useCallback(() => {
    setRows(null);
    listEnvelopes(only ? { contractId: only } : {})
      .then(setRows)
      .catch(() => setErr("Could not load what is out for signature."));
  }, [only]);
  useEffect(load, [load]);

  async function nudge(id: number) {
    setBusy(id); setErr(null); setNote(null);
    try {
      const r = await remindEnvelope(id);
      setNote(r.ok
        ? `Reminder emailed to ${r.reminded}.`
        : `Could not email ${r.reminded} — copy their link and send it yourself.`);
      load();
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not send a reminder.");
    } finally { setBusy(null); }
  }

  async function pullBack(id: number) {
    if (!window.confirm(
      "Withdraw this contract? Every link already emailed stops working " +
      "immediately, and anyone part-way through loses what they were doing.")) return;
    setBusy(id); setErr(null); setNote(null);
    try {
      await voidEnvelope(id);
      setNote("Withdrawn. The links no longer open.");
      load();
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not withdraw it.");
    } finally { setBusy(null); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Signature history</h2>
            <p>
              {only
                ? "Every signing round on this contract, and where each one got to."
                : "Every contract out for signature, and where each one has got to."}
              {" "}A round starts on the contract itself once both sides have
              agreed its terms — you sign first, in the app, and the broker is
              emailed the document carrying your signature.
            </p>
          </div>
          <div className="actions">
            <button className="btn" onClick={load}><RefreshCw size={14} /> Refresh</button>
            <Link to={only ? `/contracts/${only}` : "/contracts"} className="btn">
              <ArrowLeft size={14} /> {only ? "The contract" : "Contracts"}
            </Link>
          </div>
        </div>

        {only > 0 && (
          <div className="note" style={{ marginBottom: 14 }}>
            Showing one contract only.{" "}
            <Link to="/contracts/signatures">See every contract out for signature</Link>.
          </div>
        )}

        {err && (
          <div className="mb-4 rounded-lg border border-[#F3C7C7] bg-[#FDECEC] px-4 py-3 text-[13px] text-[#8A2222]">
            {err}
          </div>
        )}
        {note && (
          <div className="mb-4 rounded-lg border border-[#CFE3E7] bg-[#EFF9FA] px-4 py-3 text-[13px]"
               style={{ color: "#0B4A54" }}>{note}</div>
        )}

        {rows === null ? (
          <div className="flex items-center gap-2 py-10 text-[13px] text-ink-muted">
            <Loader2 className="animate-spin" size={16} /> Loading…
          </div>
        ) : rows.length === 0 ? (
          <div className="rounded-xl border border-border bg-white px-6 py-12 text-center shadow-card">
            <div className="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-full bg-surface-2 text-ink-soft">
              <FileSignature size={20} />
            </div>
            <h3 className="mb-1 text-sm font-semibold text-ink">
              {only
                ? "No round has been started on this contract"
                : "Nothing is out for signature"}
            </h3>
            <p className="mx-auto max-w-sm text-[13px] leading-relaxed text-ink-muted">
              A round begins on a contract whose terms both sides have agreed:
              open it and press Sign. You sign first, then the broker is emailed
              the document carrying your signature — and once they have signed,
              the completed contract goes to you both.
            </p>
            <Link to={only ? `/contracts/${only}` : "/contracts"}
              className="mt-4 inline-flex items-center gap-1.5 rounded-md px-4 py-2.5 text-[13px]
                         font-semibold text-white" style={{ background: TEAL }}>
              {only ? "Open the contract" : "Go to contracts"} <ChevronRight size={15} />
            </Link>
          </div>
        ) : (
          <div className="space-y-3">
            {rows.map(e => (
              <EnvelopeRow key={e.id} env={e} busy={busy === e.id}
                           onNudge={() => nudge(e.id)} onVoid={() => pullBack(e.id)}
                           onOpen={() => getEnvelope(e.id).then(setOpen).catch(
                             () => setErr("Could not open that one."))} />
            ))}
          </div>
        )}
      </div>

      {open && <HistoryDrawer env={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function EnvelopeRow({ env, busy, onNudge, onVoid, onOpen }: {
  env: Envelope; busy: boolean;
  onNudge: () => void; onVoid: () => void; onOpen: () => void;
}) {
  const s = ENVELOPE_STATUS[env.status];
  const live = env.status === "sent" || env.status === "in_progress";
  return (
    <section className="rounded-xl border border-border bg-white p-4 shadow-card">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            {/* Both ways round now that this screen hangs off Contracts: the
                round names its contract, and the name opens it. */}
            <h3 className="truncate text-[14.5px] font-semibold text-ink">
              {env.contract_id
                ? <Link to={`/contracts/${env.contract_id}`}>{env.title}</Link>
                : env.title}
            </h3>
            <span className={`badge ${s.cls}`}><span className="d" />{s.label}</span>
          </div>
          <div className="mt-0.5 text-[12.5px] text-ink-soft">
            {env.page_count} pages · {env.fields.length} signature boxes
            {env.sent_at && ` · sent ${new Date(env.sent_at).toLocaleDateString("en-GB",
              { day: "2-digit", month: "short", year: "numeric" })}`}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {live && (
            <button onClick={onNudge} disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2
                         text-[12.5px] font-medium text-ink hover:bg-surface-2 disabled:opacity-50">
              {busy ? <Loader2 className="animate-spin" size={14} /> : <BellRing size={14} />}
              Remind
            </button>
          )}
          <button onClick={onOpen}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2
                       text-[12.5px] font-medium text-ink hover:bg-surface-2">
            History
          </button>
          <button
            onClick={() => downloadFile(`/esign/envelopes/${env.id}/pdf`)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2
                       text-[12.5px] font-medium text-ink hover:bg-surface-2">
            <Download size={14} /> {env.status === "completed" ? "Signed copy" : "Draft copy"}
          </button>
          {live && (
            <button onClick={onVoid} disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2
                         text-[12.5px] font-medium text-danger hover:bg-[#FDECEC] disabled:opacity-50">
              <Ban size={14} /> Withdraw
            </button>
          )}
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {env.recipients.map(r => {
          const rs = RECIPIENT_STATUS[r.status];
          const theirTurn = env.waiting_on?.id === r.id;
          return (
            <div key={r.id}
              className={`rounded-lg border px-3 py-2.5 ${
                theirTurn ? "border-[#B7DEE4] bg-[#EFF9FA]" : "border-border bg-surface-2"}`}>
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0">
                  <span className="block truncate text-[13px] font-medium text-ink">
                    {r.order}. {r.name}
                  </span>
                  <span className="block truncate text-[11.5px] text-ink-soft">
                    {r.org} · {r.email}
                  </span>
                </span>
                <span className={`badge ${rs.cls} shrink-0`}><span className="d" />{rs.label}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1
                              font-mono text-[11px] text-ink-soft">
                <span title={keyExplains(r.party_key)}>{r.party_key}</span>
                {r.signed_at && (
                  <span className="font-sans">
                    signed {new Date(r.signed_at).toLocaleString("en-GB",
                      { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
                  </span>
                )}
              </div>
              {r.decline_reason && (
                <div className="mt-2 rounded bg-[#FFF8E6] px-2.5 py-1.5 text-[12px] text-[#6B5410]">
                  <b>They asked for a change:</b> {r.decline_reason}
                </div>
              )}
              {r.link && (
                <button
                  onClick={() => {
                    navigator.clipboard?.writeText(r.link!);
                  }}
                  className="mt-2 inline-flex items-center gap-1 text-[11.5px] font-medium
                             text-ink-muted hover:text-ink">
                  <Copy size={11} /> Copy their signing link
                </button>
              )}
            </div>
          );
        })}
      </div>

      {env.status === "completed" && (
        <div className="mt-3 flex items-start gap-2 rounded-lg px-3 py-2.5 text-[12.5px]"
             style={{ background: "#E7F6EC", color: "#166534" }}>
          <Check size={15} className="mt-0.5 shrink-0" />
          <span>
            Everybody has signed. The signed copy was emailed to both parties,
            sealed so that any later change to it shows up as a broken signature
            in their PDF reader.
          </span>
        </div>
      )}
      {env.status === "declined" && (
        <div className="mt-3 rounded-lg bg-[#FFF8E6] px-3 py-2.5 text-[12.5px] text-[#6B5410]">
          <b>A declined contract is not a failure, it is a negotiation.</b>{" "}
          Change the term they named and send it again — the reason above stays
          on the record.
        </div>
      )}
    </section>
  );
}

function HistoryDrawer({ env, onClose }: { env: Envelope; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative z-10 flex max-h-[86vh] w-full max-w-2xl flex-col rounded-xl
                      border border-border bg-white shadow-xl">
        <header className="border-b border-border px-5 py-3.5">
          <h2 className="text-base font-semibold text-ink">{env.title} — history</h2>
          <p className="mt-0.5 text-[12.5px] text-ink-soft">
            Everything that happened, in order. This is kept here rather than
            printed onto the contract — the signed PDF is the agreement itself,
            and nothing else.
          </p>
        </header>
        <div className="overflow-y-auto px-5 py-4">
          {(env.events ?? []).length === 0 ? (
            <p className="text-[13px] text-ink-muted">Nothing recorded yet.</p>
          ) : (
            <ol className="space-y-0">
              {(env.events ?? []).map((e, i) => (
                <li key={i} className="flex gap-3 border-l-2 border-border py-2.5 pl-4">
                  <span className="w-40 shrink-0 font-mono text-[11.5px] text-ink-soft">
                    {new Date(e.at).toLocaleString("en-GB",
                      { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[13px] font-medium text-ink">{label(e.type)}</span>
                    <span className="block truncate text-[11.5px] text-ink-soft">
                      {e.actor}{e.ip ? ` · ${e.ip}` : ""}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
        <footer className="flex justify-end border-t border-border px-5 py-3">
          <button onClick={onClose}
            className="rounded-md border border-border px-3.5 py-2 text-[13px] font-medium
                       text-ink hover:bg-surface-2">Close</button>
        </footer>
      </div>
    </div>
  );
}

/** Event names as a person would say them. */
function label(type: string): string {
  return ({
    created: "Set up",
    opened_in_app: "Opened for signature in Kavachio",
    sent: "Emailed to the next signer",
    viewed: "Opened it",
    signed: "Signed",
    declined: "Asked for a change",
    completed: "Everybody has signed",
    sealed: "Sealed against later changes",
    in_force: "The contract went in force",
    signed_not_in_force: "Signed, but something is holding it up",
    reminded: "Reminder sent",
    voided: "Withdrawn",
  } as Record<string, string>)[type] ?? type;
}
