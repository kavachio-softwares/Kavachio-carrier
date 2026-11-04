/**
 * Mid-term change, on the contract record: the change in progress — who has
 * to do what next — and the changes already live.
 *
 * Both sides see the same card and act from it, the way they do on the
 * contract itself: the broker agrees or asks for changes, the carrier signs
 * first and the broker second, and the broker's signature puts it live. Which
 * buttons appear is the server's answer (`actions`), never worked out here.
 */
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, FileText, PenLine, Undo2 } from "lucide-react";
import {
  acceptMidTerm, agreeMidTerm, applyMidTerm, downloadDocument, fieldErrors,
  requestMidTermChanges, signMidTerm, withdrawMidTerm,
  type ContractRecord, type MidTermChange, type MidTermClauseChange,
  type MidTermSetupFollowUp,
} from "../api/contractRecord";
import { InfoTip } from "./InfoTip";
import { isBrokerSeat } from "../auth";
import { fmtDate, fmtStamp } from "../utils/date";

const STEP_WORDS: Record<string, string> = {
  sent: "Sent to the broker", uploaded: "Signed document uploaded",
  changes_requested: "Broker asked for changes",
  agreed: "Broker agreed", signed: "Signed", accepted: "Accepted",
  live: "Live", withdrawn: "Withdrawn",
};

function changeLine(ch: MidTermChange["changes"][number]) {
  if (ch.kind === "amended") return <span>{ch.question}: {ch.from} → <b>{ch.to}</b></span>;
  if (ch.kind === "added") return <span>{ch.question}: <b>{ch.to}</b> (new)</span>;
  return <span>{ch.question}: {ch.from} — no longer applies</span>;
}

/** A clause the change re-words, adds or deletes. A clause re-worded only
 *  because its term moved is said by the term's own line. */
function clauseLines(list: MidTermClauseChange[] | undefined) {
  return (list ?? []).filter(c => !c.auto).map((c, i) => (
    <li key={`cl${i}`} title={c.to_text ?? c.from_text ?? undefined}>
      {c.action === "added" ? <>New clause <b>“{c.title}”</b></>
        : c.action === "removed" ? <>Clause “{c.from_title ?? c.title}” deleted</>
        : <>Clause <b>“{c.from_title ?? c.title}”</b> re-worded</>}
    </li>
  ));
}

/** After a change is live: the checks it added, and whether each Bordereau
 *  Setup already feeds them — with the two ways to finish the job. The same
 *  templates do it unless a check needs a column the output template does not
 *  have; Kavachio says which, and recommends that one. */
function SetupFollowUp({ fu }: { fu: MidTermSetupFollowUp }) {
  if (fu.all_ready) {
    return (
      <div className="sub" style={{ marginTop: 6 }}>
        <b>{fu.new_checks} new check{fu.new_checks === 1 ? "" : "s"}</b> — every column{" "}
        {fu.new_checks === 1 ? "it reads is" : "they read is"} already mapped in Bordereau Setup,
        so the next file is checked with {fu.new_checks === 1 ? "it" : "them"}.
      </div>
    );
  }
  const same = fu.recommended === "same_templates";
  const unmapped = Array.from(new Set(fu.setups.flatMap(s => s.unmapped)));
  const missing = Array.from(new Set(fu.setups.flatMap(s => s.no_column)));
  const first = fu.setups[0];
  return (
    <div className="note warn" style={{ marginTop: 8 }}>
      <b>{fu.no_setup ? "This contract has no Bordereau Setup yet."
        : `This contract now includes ${fu.new_checks || "new"} new check${fu.new_checks === 1 ? "" : "s"} — the setup may need new mapping.`}</b>
      {!fu.no_setup && (
        <ul style={{ margin: "4px 0", paddingLeft: 18 }}>
          {unmapped.length > 0 && <li>Not mapped yet in the setup: {unmapped.join(", ")}</li>}
          {missing.length > 0 && <li>No column for it in the output template: {missing.join(", ")}</li>}
          {(fu.clauses_without_column ?? []).map((c, i) => (
            <li key={i}>“{c.clause}” — no check could be made: {c.reason}</li>
          ))}
        </ul>
      )}
      <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
        {!fu.no_setup && first && (
          <div>
            <Link className={`btn sm${same ? " pri" : ""}`} to={first.edit_link}>
              1 · Use the previous input and output templates
            </Link>
            {same && <span className="badge b-ok" style={{ marginLeft: 8 }}><span className="d" />Recommended</span>}
            <div className="sub" style={{ marginTop: 2 }}>
              Opens “{first.name}” with the same templates and this contract — map the new
              column{unmapped.length === 1 ? "" : "s"}, then save and activate the setup.
            </div>
          </div>
        )}
        <div>
          <Link className={`btn sm${!same || fu.no_setup ? " pri" : ""}`} to={fu.setup_link}>
            {fu.no_setup ? "Go to Bordereau Setup" : "2 · Go to Bordereau Setup with new templates"}
          </Link>
          {(!same || fu.no_setup) && <span className="badge b-ok" style={{ marginLeft: 8 }}><span className="d" />Recommended</span>}
          <div className="sub" style={{ marginTop: 2 }}>
            {fu.no_setup
              ? "Set it up with an input and an output template — it reads the contract's wording as it now stands."
              : "Upload a new input and output template that carry the missing column, and set the pipeline up again."}
          </div>
        </div>
      </div>
    </div>
  );
}

export function MidTermCard({ rec, onRecord, onNotice }: {
  rec: ContractRecord;
  onRecord: (r: ContractRecord) => void;
  onNotice: (m: string) => void;
}) {
  const mt = rec.mid_term;
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [asking, setAsking] = useState(false);
  const [note, setNote] = useState("");
  const [signing, setSigning] = useState(false);
  const [signer, setSigner] = useState("");
  const [title, setTitle] = useState("");
  const [consent, setConsent] = useState(false);
  // Opened from the broker's dashboard, the contract list or the email
  // (?show=mid-term): the change is what they came for, so the page opens on
  // it rather than on the top of the summary.
  const [params] = useSearchParams();
  const box = useRef<HTMLDivElement>(null);
  const shown = !!mt && (!!mt.open || mt.history.length > 0);
  useEffect(() => {
    if (shown && params.get("show") === "mid-term") {
      box.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [shown, params]);
  if (!mt || !shown) return null;
  const a = mt.open;

  async function act(what: string, run: () => Promise<ContractRecord>, done: string) {
    setBusy(what);
    setErr("");
    try {
      const r = await run();
      onRecord(r);
      onNotice(done);
      setAsking(false); setSigning(false); setNote(""); setConsent(false);
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally { setBusy(""); }
  }

  const can = (x: string) => !!a?.actions?.includes(x as never);
  // The reader's move — marked the way a contract waiting on them is.
  const myMove = can("agree") || can("sign") || can("accept");
  const sides = a?.signatures ?? {};

  return (
    <div id="mid-term-change" ref={box} style={{ scrollMarginTop: 16 }}>
      {a && (
        <div className="note" style={{ marginTop: 14 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
            <b>Mid-term change {a.number}</b>
            <span className={`badge ${myMove ? "b-warn" : "b-info"}`}>
              <span className="d" />{a.status_words}
            </span>
            {a.boundary && (
              <span className="sub">
                Up to {a.boundary.old_until}: current terms · from {a.boundary.new_from}: new terms
              </span>
            )}
          </div>
          <ul style={{ margin: "6px 0", paddingLeft: 18, lineHeight: 1.7 }}>
            {a.changes.map(ch => <li key={ch.key}>{changeLine(ch)}</li>)}
            {clauseLines(a.clause_changes)}
          </ul>
          {a.reason && <div className="sub">Reason: {a.reason}</div>}
          {a.rules && (a.rules.closing.length > 0 || a.rules.opening.length > 0) && (
            <div className="sub" style={{ marginTop: 4 }}>
              Checks: {a.rules.opening.map(o => o.replaces
                ? `${o.rule_name} ${a.rules?.closing.find(c => c.rule_id === o.replaces)?.value ?? ""} until ${a.boundary?.old_until}, ${o.value} from ${o.from}`
                : `new ${o.rule_name} ${o.value} from ${o.from}`).join("; ")}
              {typeof a.checks_unchanged === "number" && ` · ${a.checks_unchanged} other checks unchanged`}
              <InfoTip text="Files keep being checked on the current terms until this change is
                agreed and signed by both sides. Then each row is checked by the version in force
                on its transaction effective date." />
            </div>
          )}
          {a.change_request && (
            <div style={{ marginTop: 6 }}><b>The broker asked:</b> {a.change_request}</div>
          )}
          {/* The change dates the contract's checks, and this contract has none
              yet: it can't go live until it is set up (the card above). Setting
              it up works the change's checks out again by itself. */}
          {!isBrokerSeat() && rec.checks_setup && rec.checks_setup.state !== "ready" && (
            <div style={{ marginTop: 6, color: "var(--p-warn-ink)" }}>
              <b>This contract has no checks yet.</b> Set it up before the change
              goes live — once it is, the change's checks are worked out again
              against them, and nothing the broker agreed changes.
            </div>
          )}
          {a.held && (
            <div style={{ marginTop: 6, color: "var(--p-crit-ink)" }}>
              <b>Signed, but not applied yet.</b> {a.held.message}
            </div>
          )}
          <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 6, fontSize: 12.5 }}>
            {a.document && (
              <span className="linkish" role="button"
                    style={{ display: "inline-flex", alignItems: "center", gap: 4 }}
                    onClick={() => downloadDocument(rec.id, a.document!.id, a.document!.filename)}>
                <FileText size={12} /> {a.document.filename}
              </span>
            )}
            {a.road === "uploaded_signed" ? (
              <span className="sub">Signed on paper by both sides — the carrier accepts it</span>
            ) : (["carrier", "counterparty"] as const).map(side => (
              <span key={side} className="sub">
                {side === "carrier" ? "Carrier" : "Broker"}:{" "}
                {sides[side] ? `signed by ${sides[side].signer_name}, ${fmtStamp(sides[side].signed_at)}`
                  : "not signed"}
              </span>
            ))}
          </div>

          {err && <div style={{ marginTop: 8, color: "var(--p-crit-ink)" }}>{err}</div>}

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
            {can("accept") && (
              <button className="btn pri" type="button" disabled={!!busy}
                      onClick={() => act("accept", () => acceptMidTerm(rec.id, a.id),
                        `Mid-term change ${a.number} accepted — live from ${fmtDate(a.effective_from)}.`)}>
                <Check size={13} /> {busy === "accept" ? "Accepting…" : "Accept the signed change"}
              </button>
            )}
            {can("agree") && (
              <button className="btn pri" type="button" disabled={!!busy}
                      onClick={() => act("agree", () => agreeMidTerm(rec.id, a.id),
                        `You agreed mid-term change ${a.number}. The carrier signs first, then you.`)}>
                <Check size={13} /> {busy === "agree" ? "Agreeing…" : "Agree this change"}
              </button>
            )}
            {can("request_changes") && !asking && (
              <button className="btn" type="button" onClick={() => setAsking(true)}>
                Ask for changes
              </button>
            )}
            {can("sign") && !signing && (
              <button className="btn pri" type="button" onClick={() => setSigning(true)}>
                <PenLine size={13} /> Sign the change
              </button>
            )}
            {can("revise") && (
              <Link className="btn" to={`/contracts/${rec.id}/mid-term?revise=1`}>
                Revise and send again
              </Link>
            )}
            {can("apply") && (
              <button className="btn pri" type="button" disabled={!!busy}
                      onClick={() => act("apply", () => applyMidTerm(rec.id, a.id, { agreed_limits: {} }),
                        `Mid-term change ${a.number} is live.`)}>
                Apply it now
              </button>
            )}
            {can("withdraw") && (
              <button className="btn" type="button" disabled={!!busy}
                      onClick={() => {
                        if (window.confirm(`Withdraw mid-term change ${a.number}? Nothing has been applied, so nothing is undone.`)) {
                          void act("withdraw", () => withdrawMidTerm(rec.id, a.id),
                            `Mid-term change ${a.number} was withdrawn.`);
                        }
                      }}>
                <Undo2 size={13} /> Withdraw
              </button>
            )}
          </div>

          {asking && (
            <div style={{ marginTop: 10 }}>
              <div className="field" style={{ marginBottom: 6 }}>
                <label>What should change?</label>
                <textarea rows={3} value={note} onChange={e => setNote(e.target.value)} />
              </div>
              <button className="btn pri" type="button" disabled={!note.trim() || !!busy}
                      onClick={() => act("ask", () => requestMidTermChanges(rec.id, a.id, note.trim()),
                        "Sent back to the carrier with your request.")}>
                Send back to the carrier
              </button>{" "}
              <button className="btn" type="button" onClick={() => setAsking(false)}>Cancel</button>
            </div>
          )}

          {signing && (
            <div style={{ marginTop: 10 }}>
              <div className="grid g-2" style={{ maxWidth: 560 }}>
                <div className="field" style={{ marginBottom: 6 }}>
                  <label>Your name <span className="muted">— leave blank to use your account name</span></label>
                  <input value={signer} onChange={e => setSigner(e.target.value)} />
                </div>
                <div className="field" style={{ marginBottom: 6 }}>
                  <label>Title <span className="muted">— optional</span></label>
                  <input value={title} onChange={e => setTitle(e.target.value)} />
                </div>
              </div>
              <label style={{ display: "block", fontSize: 13, marginBottom: 8 }}>
                <input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />
                {" "}I have read mid-term change {a.number} and sign it for my organisation.
              </label>
              <button className="btn pri" type="button" disabled={!consent || !!busy}
                      onClick={() => act("sign",
                        () => signMidTerm(rec.id, a.id, { signer_name: signer.trim() || undefined,
                                                          signer_title: title.trim() || undefined }),
                        a.status === "carrier_signed"
                          ? `Signed. Mid-term change ${a.number} is live from ${fmtDate(a.effective_from)}.`
                          : "Signed. The broker signs next — the change goes live when they do.")}>
                <PenLine size={13} /> {busy === "sign" ? "Signing…" : "Sign"}
              </button>{" "}
              <button className="btn" type="button" onClick={() => setSigning(false)}>Cancel</button>
            </div>
          )}

          {!!a.history?.length && (
            <div className="sub" style={{ marginTop: 10 }}>
              {a.history.map((h, i) => (
                <span key={i}>{i ? " · " : ""}{STEP_WORDS[h.step] ?? h.step}
                  {h.by ? ` (${h.by})` : ""}, {fmtStamp(h.at)}</span>
              ))}
            </div>
          )}
        </div>
      )}

      {mt.history.filter(h => h.status === "live").map(h => (
        <div className="note" key={h.id} style={{ marginTop: 10 }}>
          <b>Mid-term change {h.number}</b> — live from {fmtDate(h.effective_from)}
          {h.executed_date && <span className="sub"> · signed {fmtDate(h.executed_date)}</span>}
          <ul style={{ margin: "4px 0", paddingLeft: 18 }}>
            {h.changes.map(ch => <li key={ch.key}>{changeLine(ch)}</li>)}
            {clauseLines(h.clause_changes)}
          </ul>
          {/* "No setup yet" is the contract's own card's to say now (ChecksSetupCard,
              above): it offers the existing setup, and setting the contract up
              rebuilds the checks from before this change. */}
          {h.setup_follow_up && !isBrokerSeat()
            && !(h.setup_follow_up.no_setup && rec.checks_setup)
            && <SetupFollowUp fu={h.setup_follow_up} />}
          {h.document && (
            <span className="linkish" role="button"
                  style={{ fontSize: 12.5, display: "inline-flex", alignItems: "center", gap: 4 }}
                  onClick={() => downloadDocument(rec.id, h.document!.id, h.document!.filename)}>
              <FileText size={12} /> {h.document.filename}
            </span>
          )}
          {!!h.applied?.recheck?.length && !isBrokerSeat() && (
            <div style={{ marginTop: 6 }}>
              <b>Files to re-check</b>{" "}
              <InfoTip text="Checked before this change went live, for a period that runs past its
                date. Open each one and re-check it to apply the dated checks — Kavachio never
                re-runs a file by itself." />
              <ul style={{ margin: "2px 0 0", paddingLeft: 18, fontSize: 12.5 }}>
                {h.applied.recheck.map(f => (
                  <li key={f.export_id}>
                    <Link className="linkish"
                          to={`/uploads/${f.export_id}/exceptions?download=${f.export_id}&from=files`}>
                      {f.filename}
                    </Link>
                    {f.period ? ` · ${f.period}` : ""}
                    {f.checked_at ? ` · checked ${fmtStamp(f.checked_at)}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
