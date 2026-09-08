/**
 * Signing a contract — and it is the only way one goes live.
 *
 * WHAT IS REAL HERE. Signing. Both sides sign on this page, one row each in
 * `contract_signature`, and the second signature puts the contract in force.
 * Nothing else does: creation cannot produce a live contract and "put it in
 * force" refuses while a side is missing.
 *
 * WHAT IS NOT. Sending. No provider is connected, no envelope goes anywhere
 * and no email is sent — the "Send for signature" button stays disabled, and
 * signing happens here instead. That is a real difference and worth stating:
 * a provider asserts identity, and this page asserts only that a person who
 * was logged in, was the right party, and could see this contract typed their
 * name against it. The smaller claim is the true one.
 *
 * THE TWO METHODS, kept apart everywhere they are shown:
 *   typed     the signatory was here. Attributable to their user.
 *   recorded  they signed on paper or elsewhere and the carrier entered the
 *             fact. Attributable to whoever entered it, never to them. It is
 *             the only way an insurer ↔ reinsurer contract is ever signed on
 *             both sides, because a reinsurer has no seat in Kavachio at all.
 *
 * A SIGNATURE IS ON A VERSION. Editing a draft's terms or wording withdraws
 * the signatures on it — see contract_routes.update_contract. A name left
 * attached to words it was never under is worse than no signature.
 *
 * Related and real, elsewhere: `executed_date` on the contract, and
 * `is_executed_copy` on a document — which attachment is the signed copy.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, Check, Clock, PenLine, Send,
} from "lucide-react";
import { fmtDate } from "../utils/date";
import {
  fieldErrors, getContract, signContract, unsignContract, updateContract,
  type ContractRecord as Rec,
} from "../api/contractRecord";
import { isBrokerSeat } from "../auth";

/** Which side of the contract a signatory signs for. */
type Side = "carrier" | "counterparty";

type Signatory = {
  id: number;
  name: string;
  email: string;
  role: string;
  side: Side;
};

export default function ContractSignature() {
  const { contractId } = useParams();
  const id = Number(contractId);

  const [rec, setRec] = useState<Rec | null>(null);
  const [err, setErr] = useState("");
  const [signatories, setSignatories] = useState<Signatory[]>([]);
  const [nextId, setNextId] = useState(1);

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [side, setSide] = useState<Side>("carrier");
  const [saving, setSaving] = useState(false);

  const [saved, setSaved] = useState("");
  const [signName, setSignName] = useState("");
  const [signTitle, setSignTitle] = useState("");
  const [signing, setSigning] = useState(false);

  // Which side THIS user signs for. Not a choice — a broker signs for the
  // counterparty and a carrier for itself, and the server enforces the same
  // thing. Offering a picker would be offering a button the API refuses.
  const myS: Side = isBrokerSeat() ? "counterparty" : "carrier";

  // Naming the signatories is part of authoring the contract — it is what the
  // signature page of the document is built from — and the broker's part in
  // signing is to sign. The server refuses a broker's edit for the same
  // reason, so showing the controls would only offer a button that 403s.
  const mayNameSigners = !isBrokerSeat() && rec?.actions.edit === true;

  /** Keep the named signatories. Carrier only — see mayNameSigners. */
  async function saveSigners() {
    setSaving(true); setErr(""); setSaved("");
    try {
      await updateContract(id, {
        signers: signatories.map(sg => ({
          name: sg.name, email: sg.email, role: sg.role, side: sg.side,
        })),
      });
      setSaved("Saved. These are the names the contract\u2019s signature page "
             + "is built from.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setSaving(false);
    }
  }

  /** Sign, or record the other side's. The record that comes back says what
   *  happened, including whether the contract just went in force. */
  async function doSign(recorded: boolean) {
    if (!signName.trim()) return;
    setSigning(true); setErr(""); setSaved("");
    try {
      const r = await signContract(id, {
        signer_name: signName.trim(),
        signer_title: signTitle.trim() || null,
        ...(recorded ? { recorded: true, side: "counterparty" as const } : {}),
      });
      setRec(r);
      setSaved(r.signature_note
               || "Signed. Waiting on the other side.");
      setSignName(""); setSignTitle("");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setSigning(false);
    }
  }

  async function withdraw(signatureId: number) {
    setErr(""); setSaved("");
    try {
      setRec(await unsignContract(id, signatureId));
      setSaved("That signature was withdrawn.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    }
  }

  useEffect(() => {
    getContract(id)
      .then(r => {
        setRec(r);
        // Named at step 4 of the create flow. Seeded rather than asked for
        // again — retyping the same four people is exactly the kind of thing
        // that makes a flow feel like two unrelated screens.
        if (r.signers?.length) {
          setSignatories(r.signers.map((sg, i) => ({
            id: i + 1, name: sg.name, email: sg.email,
            role: sg.role || "Authorised signatory",
            side: sg.side,
          })));
          setNextId(r.signers.length + 1);
        }
      })
      .catch(e => setErr(e?.response?.data?.detail || "Could not load this contract."));
  }, [id]);

  function add() {
    if (!name.trim() || !email.trim()) return;
    setSignatories(s => [...s, {
      id: nextId, name: name.trim(), email: email.trim(),
      role: role.trim() || "Authorised signatory",
      side,
    }]);
    setNextId(n => n + 1);
    setName(""); setEmail(""); setRole("");
  }

  const bySide = useMemo(() => ({
    carrier: signatories.filter(s => s.side === "carrier"),
    counterparty: signatories.filter(s => s.side === "counterparty"),
  }), [signatories]);


  if (!rec) {
    return (
      <div className="proto">
        <div className="view full">
          <div className="page-head"><div className="t"><h2>Signature</h2></div></div>
          {err
            ? <div className="note warn" style={{ maxWidth: 620 }}>{err}</div>
            : <div className="empty">Loading…</div>}
        </div>
      </div>
    );
  }

  // The wording is what would be sent. Without one there is nothing to sign,
  // and that is worth saying before anyone fills in a signatory list.
  const noWording = !rec.has_wording;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Signature</h2>
            <p>{[rec.name, rec.counterparty?.name].filter(Boolean).join(" · ")}</p>
          </div>
          <div className="actions">
            <Link to={`/contracts/${id}`} className="btn">
              <ArrowLeft size={14} /> Contract
            </Link>
          </div>
        </div>

        {/* Says what is real before anything else, because the difference
            between "signed here" and "sent to a provider" is exactly the kind
            of thing a screen like this is usually vague about. */}
        <div className="note warn" style={{ marginBottom: 16 }}>
          <b>
            <AlertTriangle size={13} style={{ verticalAlign: "-2px" }} />{" "}
            No signing provider is connected — signing happens on this page.
          </b>{" "}
          Nothing is emailed and no envelope goes anywhere. A signature here
          records that a person who was logged in, was the right party and could
          see this contract typed their name against it. Both sides signing is
          what puts the contract in force, and it is the only thing that does.
        </div>

        {saved && (
          <div className="note ok" style={{ marginBottom: 16 }}>{saved}</div>
        )}

        {noWording && (
          <div className="note" style={{ marginBottom: 16 }}>
            <b>There is no wording attached to this contract.</b> A signature
            round sends a document, so there would be nothing to send. Attach the
            wording first.
          </div>
        )}

        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h"><h3>What would be signed</h3></div>
          <div style={{ padding: "16px 20px" }}>
            <div className="grid g-3">
              <div>
                <div className="sub">Contract</div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{rec.name}</div>
              </div>
              <div>
                <div className="sub">Type</div>
                <div style={{ fontSize: 13 }}>{rec.contract_type_label ?? "—"}</div>
              </div>
              <div>
                <div className="sub">Term</div>
                <div className="mono" style={{ fontSize: 12 }}>
                  {rec.inception_dt && rec.expiry_dt
                    ? `${fmtDate(rec.inception_dt)} → ${fmtDate(rec.expiry_dt)}`
                    : "—"}
                </div>
              </div>
            </div>
            <div className="divider" />
            <div className="hint">
              The wording plus every active endorsement would go into one
              envelope: an endorsement changes the terms being agreed, so signing
              the wording alone would not cover it.
              {rec.endorsement_count > 0 && (
                <> This contract has {rec.endorsement_count} active endorsement
                  {rec.endorsement_count === 1 ? "" : "s"}.</>
              )}
              {rec.executed_date && (
                <> Recorded as executed on {fmtDate(rec.executed_date)}.</>
              )}
            </div>
          </div>
        </div>

        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <h3>Who signs</h3>
            <span className="sub">
              {isBrokerSeat()
                ? "named by the carrier — this is who the contract expects"
                : "named by you, on both sides. It is what the signature page "
                  + "of the contract is built from."}
            </span>
            {mayNameSigners && (
              <span className="right">
                <button className="btn sm" type="button" disabled={saving}
                        onClick={saveSigners}>
                  <PenLine size={12} />{" "}
                  {saving ? "Saving\u2026" : "Save who signs"}
                </button>
              </span>
            )}
          </div>
          <div style={{ padding: "16px 20px" }}>
            <div className="grid g-2">
              {(["carrier", "counterparty"] as Side[]).map(s => (
                <div key={s}>
                  <div className="sub" style={{ marginBottom: 8 }}>
                    {s === myS
                      ? "Your side"
                      : s === "carrier"
                        ? "The carrier"
                        : rec.counterparty?.name ?? "Counterparty"}
                  </div>
                  {bySide[s].length === 0 ? (
                    <div className="empty" style={{ padding: "18px 10px" }}>
                      {isBrokerSeat()
                        ? "Nobody named. You can still sign."
                        : "Nobody added yet"}
                    </div>
                  ) : (
                    bySide[s].map(sig => {
                      // Whether this named person has ACTUALLY signed, read off
                      // the signatures rather than a status this screen keeps
                      // for itself. A badge that says "not sent" beside
                      // somebody who signed yesterday is worse than no badge.
                      const has = rec.signatures.some(
                        g => g.side === s
                          && g.signer_name.trim().toLowerCase()
                             === sig.name.trim().toLowerCase());
                      return (
                        <div className="kv" key={sig.id}>
                          <span className="k">
                            <b style={{ color: "var(--p-ink)" }}>{sig.name}</b>
                            <div className="sub">{sig.role} · {sig.email}</div>
                          </span>
                          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                            {has ? (
                              <span className="badge b-ok">
                                <span className="d" /><Check size={11} /> Signed
                              </span>
                            ) : (
                              <span className="badge b-mut">
                                <span className="d" /><Clock size={11} /> Yet to sign
                              </span>
                            )}
                            {mayNameSigners && (
                              <span
                                className="linkish" role="button"
                                onClick={() => setSignatories(l =>
                                  l.filter(x => x.id !== sig.id))}
                              >
                                Remove
                              </span>
                            )}
                          </span>
                        </div>
                      );
                    })
                  )}
                </div>
              ))}
            </div>

            {mayNameSigners ? (
              <>
              <div className="divider" />

              <div className="grid g-3">
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Side</label>
                  <select value={side} onChange={e => setSide(e.target.value as Side)}>
                    <option value="carrier">Your side</option>
                    <option value="counterparty">
                      {rec.counterparty?.name ?? "Counterparty"}
                    </option>
                  </select>
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Name</label>
                  <input value={name} onChange={e => setName(e.target.value)} />
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Email</label>
                  <input type="email" value={email}
                         onChange={e => setEmail(e.target.value)} />
                </div>
              </div>
              <div className="row2" style={{ marginTop: 14, alignItems: "end" }}>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Role</label>
                  <input value={role} onChange={e => setRole(e.target.value)}
                         placeholder="Authorised signatory" />
                </div>
                <div>
                  <button
                    className="btn" type="button"
                    disabled={!name.trim() || !email.trim()} onClick={add}
                  >
                    Add signatory
                  </button>
                </div>
              </div>
              </>
            ) : (
              <div className="hint" style={{ marginTop: 14 }}>
                {isBrokerSeat()
                  ? "The carrier names who signs — it is part of the contract "
                    + "they write. Your part is to sign it, below."
                  : "These can be changed while the contract is still a draft."}
              </div>
            )}
          </div>
        </div>

        {/* ── the signatures themselves ──
            Real, and the only way a contract goes in force. What is NOT wired
            is sending: nothing leaves Kavachio and no provider is involved.
            Someone signing here is a person who is logged in, is the right
            party, and typed their name against this contract — which is a
            smaller claim than a provider makes, and an honest one. */}
        <div className="card">
          <div className="card-h">
            <h3>Signatures</h3>
            <span className="sub">
              {rec.unsigned_sides.length === 0
                ? "both sides have signed"
                : `waiting on ${rec.unsigned_sides
                    .map(s => s === "carrier" ? "the carrier" : "the counterparty")
                    .join(" and ")}`}
            </span>
            <span className="right">
              <button
                className="btn pri" type="button" disabled
                title="No signing provider is connected yet — signing happens
                       on this page instead"
              >
                <Send size={14} /> Send for signature
              </button>
            </span>
          </div>

          <div style={{ padding: "16px 20px" }}>
            {rec.lifecycle === "active" ? (
              <div className="note ok" style={{ marginBottom: 14 }}>
                <b>Both sides have signed and this contract is in force.</b>{" "}
                Its checks run on every bordereau from here. To change it now,
                endorse it — a running contract is not edited.
              </div>
            ) : rec.unsigned_sides.length === 0 ? (
              <div className="note warn" style={{ marginBottom: 14 }}>
                <b>Both sides have signed, but it is not in force yet.</b>{" "}
                Something else is in the way — see the contract\u2019s page.
              </div>
            ) : (
              <div className="note" style={{ marginBottom: 14 }}>
                <b>A contract goes in force when both sides have signed it.</b>{" "}
                There is no other way to make one live, and the second signature
                normally does it on its own.
              </div>
            )}

            <div className="grid g-2">
              {(["carrier", "counterparty"] as Side[]).map(sd => {
                const done = rec.signatures.filter(g => g.side === sd);
                const mine = sd === (rec.actions.sign ? myS : null);
                return (
                  <div key={sd}>
                    <div className="sub" style={{ marginBottom: 8 }}>
                      {sd === "carrier"
                        ? "Your side"
                        : rec.counterparty?.name ?? "Counterparty"}
                    </div>

                    {done.length === 0 ? (
                      <div className="empty" style={{ padding: "18px 10px" }}>
                        Not signed yet
                      </div>
                    ) : done.map(g => (
                      <div className="kv" key={g.id}>
                        <span className="k">
                          <b style={{ color: "var(--p-ink)" }}>{g.signer_name}</b>
                          <div className="sub">
                            {[g.signer_title, g.signed_at && fmtDate(g.signed_at)]
                              .filter(Boolean).join(" · ")}
                          </div>
                          {/* Said plainly, because the two are different
                              claims: one person was here and signed, the other
                              signed somewhere Kavachio never saw. */}
                          <div className="sub">
                            {g.method === "typed"
                              ? "Signed in Kavachio"
                              : "Signed elsewhere, recorded here"}
                          </div>
                        </span>
                        <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                          <span className="badge b-ok"><span className="d" />
                            <Check size={11} /> Signed
                          </span>
                          {rec.lifecycle !== "active" && (
                            <span
                              className="linkish" role="button" tabIndex={0}
                              onClick={() => withdraw(g.id)}
                            >
                              Withdraw
                            </span>
                          )}
                        </span>
                      </div>
                    ))}
                    {mine && <div className="hint">Yours to sign, below.</div>}
                  </div>
                );
              })}
            </div>

            {(rec.actions.sign || rec.actions.record_signature) && (
              <>
                <div className="divider" />
                {rec.actions.sign ? (
                  <div className="hint" style={{ marginBottom: 10 }}>
                    <b>Signing is an act, not a note.</b> Type your name to sign
                    for {myS === "carrier" ? "the carrier"
                                           : rec.counterparty?.name ?? "your side"}.
                    It is recorded against you and dated, and if it is the
                    second signature the contract goes in force immediately.
                  </div>
                ) : (
                  <div className="hint" style={{ marginBottom: 10 }}>
                    <b>Recording the counterparty\u2019s signature.</b> Use this
                    only when they signed on paper or through a provider. It is
                    stored as recorded rather than signed here, and attributed
                    to you as the person who entered it \u2014 never to them.
                  </div>
                )}

                <div className="grid g-3">
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Name of the person signing</label>
                    <input value={signName}
                           onChange={e => setSignName(e.target.value)}
                           placeholder="Their full name" />
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Title</label>
                    <input value={signTitle}
                           onChange={e => setSignTitle(e.target.value)}
                           placeholder="Authorised signatory" />
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>&nbsp;</label>
                    <button
                      className="btn pri" type="button"
                      disabled={!signName.trim() || !!signing}
                      onClick={() => doSign(!rec.actions.sign)}
                    >
                      <PenLine size={14} />{" "}
                      {signing ? "Signing\u2026"
                        : rec.actions.sign ? "Sign the contract"
                        : "Record their signature"}
                    </button>
                  </div>
                </div>

                {/* Both are offered when the carrier can do either, so
                    recording somebody else\u2019s is a deliberate second click
                    and never the default. */}
                {rec.actions.sign && rec.actions.record_signature && (
                  <div className="hint" style={{ marginTop: 10 }}>
                    Signed on paper by{" "}
                    {rec.counterparty?.name ?? "the counterparty"}?{" "}
                    <span
                      className="linkish" role="button" tabIndex={0}
                      onClick={() => doSign(true)}
                    >
                      Record their signature instead
                    </span>{" "}
                    — stored as recorded, and attributed to you.
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}