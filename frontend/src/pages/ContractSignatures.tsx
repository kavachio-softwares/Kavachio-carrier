/**
 * Signature history — every signing round this carrier has out, and where each
 * one got to.
 *
 * Reached from Contracts ("Signature history") or from a contract's own record,
 * never from the sidebar: a round is something a CONTRACT is going through.
 * `?contract=<id>` narrows it to one contract's rounds — that is the link the
 * record opens, and it is the only difference between the two ways in.
 *
 * A TABLE rather than a stack of cards, and the same one every other list in
 * the app uses. The two questions people arrive with are "which of these are
 * still out?" and "who is holding this one up?", and both are answered by
 * scanning a column — with a card each, two rounds filled the screen and
 * neither question could be answered without scrolling. Everything a card
 * showed is one row-expansion away, so nothing was lost.
 *
 * The signing itself happens in email, on the public /sign page. This screen is
 * the other half of that: what is out, who is holding it up, and the two things
 * a carrier can actually do about it — nudge them, or pull it back.
 *
 * Expanding a row is also what exposes each signer's link. That is not a
 * convenience: an emailed link that lands in a spam folder is the single
 * commonest way a round stalls, and a carrier who can copy it and paste it into
 * a message unblocks it in seconds instead of raising a support ticket.
 */
import { Fragment, useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ArrowLeft, Ban, BellRing, Check, ChevronDown, ChevronRight, Copy, Download,
  FileSignature, Loader2, RefreshCw,
} from "lucide-react";
import {
  ENVELOPE_STATUS, RECIPIENT_STATUS, getEnvelope, keyExplains, listEnvelopes,
  remindEnvelope, voidEnvelope, type Envelope,
} from "../api/esign";
import { downloadFile } from "../api/client";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { fmtDate, fmtDateTime } from "../utils/date";

const PAGE_SIZE = 10;

/** What a status means once you are looking at the row rather than the badge —
 *  the same job the note under a contract's state does on /contracts. */
const STATUS_NOTE: Record<Envelope["status"], string> = {
  draft: "never sent to anyone",
  sent: "waiting on the first signature",
  in_progress: "one side has signed",
  completed: "signed by both, emailed out",
  declined: "a change was asked for",
  voided: "pulled back — the links no longer open",
};

export default function ContractSignatures() {
  const [open, setOpen] = useState<Envelope | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  // One contract, or all of them. Held in the URL rather than in state so the
  // filtered view is a link the record can hand out — and so a refresh, or a
  // link pasted to a colleague, lands on the same contract.
  const [params] = useSearchParams();
  const only = Number(params.get("contract") ?? "") || 0;

  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  // Debounced because the search runs on the SERVER — an undebounced box is one
  // request per keystroke.
  const dq = useDebouncedValue(q, 300);
  const filterKey = [only, status, dq.trim()].join("|");

  const { page, setPage, items, total, loading, pageCount, reload } =
    useServerList<Envelope>(
      (page, pageSize) =>
        listEnvelopes({
          contractId: only || undefined,
          status: status || undefined,
          q: dq.trim() || undefined,
          limit: pageSize,
          offset: (page - 1) * pageSize,
        })
          // The hook turns a failed fetch into an empty page, which reads as
          // "nothing is out for signature" — the one thing this screen must
          // never say when it simply could not ask. So the message is set here
          // and the throw is passed on for the hook to settle its own state.
          .then(r => { setErr(null); return { items: r.envelopes, total: r.total }; })
          .catch(e => {
            setErr("Could not load what is out for signature.");
            throw e;
          }),
      filterKey,
      PAGE_SIZE,
    );

  const refresh = useCallback(() => { setNote(null); reload(); }, [reload]);
  const filtersActive = !!(q || status);

  async function nudge(id: number) {
    setBusy(id); setErr(null); setNote(null);
    try {
      const r = await remindEnvelope(id);
      setNote(r.ok
        ? `Reminder emailed to ${r.reminded}.`
        : `Could not email ${r.reminded} — copy their link and send it yourself.`);
      reload();
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
      reload();
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
            <button className="btn" onClick={refresh}>
              <RefreshCw size={14} /> Refresh
            </button>
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
          <div className="note warn" style={{ marginBottom: 14, maxWidth: 700 }}>{err}</div>
        )}
        {note && (
          <div className="note ok" style={{ marginBottom: 14, maxWidth: 700 }}>{note}</div>
        )}

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Contract name" }}
            selects={[
              {
                key: "status", ariaLabel: "Filter by where it has got to",
                value: status, onChange: setStatus,
                options: [
                  { value: "", label: "All rounds" },
                  ...(Object.keys(ENVELOPE_STATUS) as Envelope["status"][])
                    // A draft was sent to nobody. It is reachable by asking for
                    // it here, but it is not one of "the rounds you have out".
                    .filter(k => k !== "draft")
                    .map(k => ({ value: k, label: ENVELOPE_STATUS[k].label })),
                ],
              },
            ]}
            onClear={() => { setQ(""); setStatus(""); }}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 34 }} />
                  <th>Contract</th><th>Signed by</th><th>Sent</th>
                  <th>Where it has got to</th><th style={{ textAlign: "right" }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map(env => {
                  const s = ENVELOPE_STATUS[env.status];
                  const live = env.status === "sent" || env.status === "in_progress";
                  const signed = env.recipients.filter(r => r.status === "signed").length;
                  const isOpen = expanded === env.id;
                  return (
                    <Fragment key={env.id}>
                      <tr>
                        <td>
                          <button className="btn sm" aria-label={isOpen ? "Hide signers" : "Show signers"}
                            aria-expanded={isOpen} style={{ padding: "5px 6px" }}
                            onClick={() => setExpanded(isOpen ? null : env.id)}>
                            {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                          </button>
                        </td>
                        <td>
                          {/* Both ways round now that this screen hangs off
                              Contracts: the round names its contract, and the
                              name opens it. */}
                          {env.contract_id
                            ? <Link to={`/contracts/${env.contract_id}`}><b>{env.title}</b></Link>
                            : <b>{env.title}</b>}
                          <div className="sub">
                            {env.page_count} page{env.page_count === 1 ? "" : "s"}
                            {" · "}{env.fields.length} signature box
                            {env.fields.length === 1 ? "" : "es"}
                          </div>
                        </td>
                        <td>
                          <b style={{ fontSize: 13 }}>
                            {signed} of {env.recipients.length}
                          </b>
                          <div className="sub">
                            {env.waiting_on
                              ? `waiting on ${env.waiting_on.name}`
                              : signed === env.recipients.length && signed > 0
                                ? "everybody has signed"
                                : "nobody yet"}
                          </div>
                        </td>
                        <td className="mono">{fmtDate(env.sent_at)}</td>
                        <td>
                          <span className={`badge ${s.cls}`}>
                            <span className="d" />{s.label}
                          </span>
                          <div className="sub">{STATUS_NOTE[env.status]}</div>
                        </td>
                        <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                          <button className="btn sm"
                            onClick={() => getEnvelope(env.id).then(setOpen).catch(
                              () => setErr("Could not open that one."))}>
                            History
                          </button>{" "}
                          <button className="btn sm" title={
                            env.status === "completed" ? "Signed copy" : "Draft copy"}
                            aria-label={
                              env.status === "completed" ? "Download the signed copy"
                                                         : "Download the draft copy"}
                            onClick={() => downloadFile(`/esign/envelopes/${env.id}/pdf`)}>
                            <Download size={13} />
                          </button>
                          {live && <>
                            {" "}
                            <button className="btn sm" disabled={busy === env.id}
                              title="Email a reminder to whoever it is with"
                              aria-label="Send a reminder" onClick={() => nudge(env.id)}>
                              {busy === env.id
                                ? <Loader2 className="animate-spin" size={13} />
                                : <BellRing size={13} />}
                            </button>{" "}
                            <button className="btn sm danger" disabled={busy === env.id}
                              title="Withdraw it — every emailed link stops working"
                              aria-label="Withdraw it" onClick={() => pullBack(env.id)}>
                              <Ban size={13} />
                            </button>
                          </>}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr>
                          <td colSpan={6} style={{ background: "var(--p-surface-2)",
                                                   padding: "14px 18px" }}>
                            <Signers env={env} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>

            {/* Only on a cold load. Paging keeps the rows it has until the
                next page arrives, and swapping them for "Loading…" makes the
                table jump on every Next. */}
            {loading && items.length === 0 && <div className="empty">Loading…</div>}
            {!loading && !err && items.length === 0 && (
              <div className="empty" style={{ padding: "40px 16px" }}>
                <div style={{ marginBottom: 10 }}>
                  <FileSignature size={20} />
                </div>
                <b style={{ display: "block", marginBottom: 6, color: "var(--p-ink)" }}>
                  {filtersActive
                    ? "No round matches those filters"
                    : only
                      ? "No round has been started on this contract"
                      : "Nothing is out for signature"}
                </b>
                {!filtersActive && (
                  <div style={{ maxWidth: 460, margin: "0 auto", lineHeight: 1.6 }}>
                    A round begins on a contract whose terms both sides have
                    agreed: open it and press Sign. You sign first, then the
                    broker is emailed the document carrying your signature — and
                    once they have signed, the completed contract goes to you
                    both.
                    <div style={{ marginTop: 12 }}>
                      <Link to={only ? `/contracts/${only}` : "/contracts"}
                            className="btn pri">
                        {only ? "Open the contract" : "Go to contracts"}
                      </Link>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
                      totalItems={total} onPageChange={setPage} noun="rounds" />
        </div>
      </div>

      {open && <HistoryDrawer env={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

/** The detail a card used to carry: who each signer is, where they have got to,
 *  what they asked to be changed, and the link to hand them if their copy never
 *  arrived. */
function Signers({ env }: { env: Envelope }) {
  const [copied, setCopied] = useState<number | null>(null);
  return (
    <>
      <div className="grid g-2" style={{ gap: 12 }}>
        {env.recipients.map(r => {
          const rs = RECIPIENT_STATUS[r.status];
          const theirTurn = env.waiting_on?.id === r.id;
          return (
            <div key={r.id} style={{
              border: `1px solid ${theirTurn ? "#B7DEE4" : "var(--p-border)"}`,
              background: theirTurn ? "#EFF9FA" : "var(--p-surface)",
              borderRadius: "var(--p-r-sm)", padding: "10px 12px",
            }}>
              <div style={{ display: "flex", justifyContent: "space-between",
                            gap: 8, alignItems: "start" }}>
                <span style={{ minWidth: 0 }}>
                  <b style={{ fontSize: 13 }}>{r.order}. {r.name}</b>
                  <div className="sub">{[r.org, r.email].filter(Boolean).join(" · ")}</div>
                </span>
                <span className={`badge ${rs.cls}`}><span className="d" />{rs.label}</span>
              </div>
              <div className="hint">
                <span className="mono" title={keyExplains(r.party_key)}>{r.party_key}</span>
                {r.signed_at && <> · signed {fmtDateTime(r.signed_at)}</>}
              </div>
              {r.decline_reason && (
                <div className="note warn" style={{ marginTop: 8 }}>
                  <b>They asked for a change:</b> {r.decline_reason}
                </div>
              )}
              {r.link && (
                <button className="linkish" style={{ marginTop: 8, fontSize: 12 }}
                  onClick={() => {
                    navigator.clipboard?.writeText(r.link!);
                    setCopied(r.id);
                  }}>
                  <Copy size={11} style={{ verticalAlign: "-1px" }} />{" "}
                  {copied === r.id ? "Copied" : "Copy their signing link"}
                </button>
              )}
            </div>
          );
        })}
      </div>

      {env.status === "completed" && (
        <div className="note ok" style={{ marginTop: 12 }}>
          <Check size={13} style={{ verticalAlign: "-2px" }} />{" "}
          Everybody has signed. The signed copy was emailed to both parties,
          sealed so that any later change to it shows up as a broken signature
          in their PDF reader.
        </div>
      )}
      {env.status === "declined" && (
        <div className="note warn" style={{ marginTop: 12 }}>
          <b>A declined contract is not a failure, it is a negotiation.</b>{" "}
          Change the term they named and send it again — the reason above stays
          on the record.
        </div>
      )}
    </>
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
