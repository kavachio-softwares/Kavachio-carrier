/**
 * Signing a contract — and it is the only way one goes live.
 *
 * WHAT IS REAL HERE. Signing. Both sides sign on this page, one row each in
 * `contract_signature`, and the second signature puts the contract in force.
 * Nothing else does: creation cannot produce a live contract and "put it in
 * force" refuses while a side is missing.
 *
 * HOW IT IS SIGNED NOW. On the document, not on this page. Once both sides
 * have agreed the terms, "Sign the contract" opens the contract itself in a
 * new tab with a signature box waiting on its execution page — and what comes
 * out the other end is a PDF carrying both signatures, sealed so any later
 * change to it shows up as a broken signature. Nothing is emailed to start it:
 * the carrier is already logged in, and that is a better answer to "who is
 * this?" than a link sent to an inbox. The broker is emailed the moment the
 * carrier has signed, and can sign from their own dashboard instead.
 *
 * WHAT THIS PAGE STILL DOES. Names the signatories, shows what has been
 * signed, and holds the one path the round cannot cover: RECORDING a signature
 * made outside Kavachio. A reinsurer has no seat here, so a contract with one
 * on the other side can never be signed in the app at all — recording it is
 * the only honest way to hold it, and it stays.
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
  AlertTriangle, ArrowLeft, Check, Clock, ExternalLink, Globe, Move, PenLine,
} from "lucide-react";
import { fmtDate } from "../utils/date";
import {
  fieldErrors, getContract, getContractTypes, signContract, unsignContract,
  updateContract,
  type ContractRecord as Rec, type SignatureBlockSpec, type SignatureLayout,
} from "../api/contractRecord";
import { isBrokerSeat } from "../auth";
import {
  getContractRound, inAppSigningUrl, lookupSigner,
  type ContractRound, type SignerLookup,
} from "../api/esign";
import { SignaturePlacer, signerTargets, type PlaceTarget }
  from "../components/SignaturePlacer";
import { Modal } from "../components/ui/Modal";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

/** Which side of the contract a signatory signs for. */
type Side = "carrier" | "counterparty";

type Signatory = {
  id: number;
  name: string;
  email: string;
  role: string;
  side: Side;
  /** Whether this person is given a way to sign IN Kavachio: boxes of their
   *  own on the document and a link that opens them. False means their lines
   *  are printed and they sign the paper copy — which is how plenty of people
   *  named on a contract have always signed it. */
  access: boolean;
  /** Whether Kavachio recognised the address when they were added. Kept so the
   *  list can go on saying "from outside" without asking the server again. */
  outside?: boolean;
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

  // What the server knows about the address being typed, and — when it knows
  // nobody — what the carrier decided to do about that.
  const [look, setLook] = useState<SignerLookup | null>(null);
  const [grantAccess, setGrantAccess] = useState(true);

  // Where the signature blocks sit on the page, and the screen that drags
  // them there. The spec is served: what a block may contain and how big a
  // hand-placed one is are the server's to say, so the box dragged here is the
  // size of the block that gets drawn.
  const [sigSpec, setSigSpec] = useState<SignatureBlockSpec | null>(null);
  const [placing, setPlacing] = useState(false);
  const [draftLayout, setDraftLayout] = useState<SignatureLayout | null>(null);
  const [placeSaving, setPlaceSaving] = useState(false);

  const [saved, setSaved] = useState("");
  // Where the electronic round has got to. The SERVER answers this — whether
  // one is running, whose move it is — so the button and the endpoint behind
  // it cannot disagree about whether pressing it will work.
  const [round, setRound] = useState<ContractRound | null>(null);
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
          access: sg.access,
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

  // 400ms: long enough that typing an address is one question rather than
  // twenty, short enough that the answer is there before the name is.
  const dEmail = useDebouncedValue(email.trim(), 400);

  useEffect(() => {
    // Its own call and its own failure. Without the spec the page simply does
    // not offer to place the blocks, which is better than not loading.
    getContractTypes().then(d => setSigSpec(d.signature_block)).catch(() => {});
  }, []);

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
            // Missing means yes — every contract written before the question
            // existed gave everybody named a way to sign.
            access: sg.access !== false,
          })));
          setNextId(r.signers.length + 1);
        }
      })
      .catch(e => setErr(e?.response?.data?.detail || "Could not load this contract."));
    // Its own call, and its own failure: a round that cannot be read must not
    // stop the contract being shown. Absent, the page simply offers nothing to
    // sign, which is the honest fallback.
    getContractRound(id).then(setRound).catch(() => setRound(null));
  }, [id]);

  /** Sign on the document itself, in a new tab.
   *
   *  A new tab rather than this one because signing is a job of its own — the
   *  signer reads a whole contract and comes back — and because losing this
   *  page's state to a navigation would be losing the signatory list they may
   *  have just typed. The URL names only the contract; the signing page asks
   *  the server for its own token, so nothing forwardable ends up in it. */
  function signOnTheDocument() {
    window.open(inAppSigningUrl(id), "_blank", "noopener");
  }

  /** Ask the server about the address as it is typed.
   *
   *  It answers only about the two organisations already on this contract, so
   *  "we do not know them" is the useful half: that person is from outside,
   *  and somebody has to say whether they are being let in to sign here or
   *  only printed on the page. Asking while they type means the question is
   *  put before the name is added, not after the round has gone out. */
  useEffect(() => {
    if (!mayNameSigners || !dEmail.includes("@")) { setLook(null); return; }
    let stale = false;
    lookupSigner(id, dEmail)
      .then(r => {
        if (stale) return;
        setLook(r);
        // A fresh address is a fresh decision. Left alone, a "no link" chosen
        // for one person would silently follow the next one typed.
        setGrantAccess(true);
        // Their own details rather than a second guess at them — but only into
        // boxes still empty, so nothing typed is ever overwritten.
        if (r.known) {
          if (r.name) setName(n => n.trim() || r.name!);
          if (r.role) setRole(x => x.trim() || r.role!);
        }
      })
      .catch(() => { if (!stale) setLook(null); });
    return () => { stale = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dEmail, id, mayNameSigners]);

  /** Whether what the lookup says still describes what is in the box. A reply
   *  that arrived for an address since edited is about somebody else. */
  const lookFits = !!look
    && look.email.trim().toLowerCase() === email.trim().toLowerCase();
  const outside = lookFits && !look!.known;

  function add() {
    if (!name.trim() || !email.trim()) return;
    setSignatories(s => [...s, {
      id: nextId, name: name.trim(), email: email.trim(),
      role: role.trim() || "Authorised signatory",
      side,
      // Somebody Kavachio already knows always signs here. Somebody it does
      // not signs here only if the carrier said so.
      access: outside ? grantAccess : true,
      outside: lookFits ? !look!.known : undefined,
    }]);
    setNextId(n => n + 1);
    setName(""); setEmail(""); setRole("");
    setLook(null); setGrantAccess(true);
  }

  /** Open the page and drag the blocks onto it.
   *
   *  Placing by hand IS an arrangement — the third one — so opening this puts
   *  the draft into it rather than inventing a mode of its own. Cancelling
   *  throws the draft away and the contract keeps the arrangement it had. */
  function openPlacer() {
    if (!sigSpec) return;
    const base = rec?.signature_layout ?? sigSpec.default;
    setDraftLayout({ ...base, arrangement: "placed",
                     blocks: { ...(base.blocks ?? {}) } });
    setPlacing(true);
  }

  async function savePlacement() {
    if (!draftLayout) return;
    setPlaceSaving(true); setErr(""); setSaved("");
    try {
      // The NAMES go with it. A block keyed `carrier#3` is the third carrier
      // signatory's, so saving where it sits without saving who they are would
      // store a placement for somebody the contract does not have yet — and
      // the document, which draws a block per person actually named, would
      // quietly ignore it.
      const r = await updateContract(id, {
        signers: signatories.map(sg => ({
          name: sg.name, email: sg.email, role: sg.role, side: sg.side,
          access: sg.access,
        })),
        signature_layout: draftLayout,
      });
      setRec(r);
      setPlacing(false);
      setSaved("Saved. The signature blocks are drawn where you put them.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setPlaceSaving(false);
    }
  }

  const bySide = useMemo(() => ({
    carrier: signatories.filter(s => s.side === "carrier"),
    counterparty: signatories.filter(s => s.side === "counterparty"),
  }), [signatories]);

  /** Whether a name typed HERE is still how this contract gets signed.
   *
   *  Once a round is open on the document it owns the signature, and the
   *  server refuses a typed one — so leaving the box up would be offering a
   *  button that 409s. Recording a signature made on paper is untouched by
   *  this: that is a fact from outside Kavachio, and a round cannot capture
   *  it. It is also the only way an insurer ↔ reinsurer contract is ever
   *  signed on both sides. */
  const maySignHere = !!rec?.actions.sign && !round?.started;


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

  // EVERY BLOCK THAT CAN BE PLACED: each side, and each person named to sign
  // for it. Keyed by party key — `carrier`, `carrier#2`, `carrier#3` — the
  // same spelling the server keys the signing boxes by, so the block dragged
  // for somebody and the boxes they are asked to fill are one thing.
  //
  // Slot 1 IS the side key. A side that has named nobody offers exactly one
  // block, which is what it has always offered, and naming three people turns
  // that one into three rather than adding a mode.
  //
  // Read from the list ON SCREEN, not the saved one, because somebody places
  // the people they can see — which is why savePlacement saves both.
  const placerTargets: PlaceTarget[] = signerTargets(
    sigSpec?.sides ?? [], signatories,
    side => (side === "carrier"
      ? "You" : rec.counterparty?.name ?? "The counterparty"));
  // The server refuses a hand-placed layout that leaves a SIDE unplaced — a
  // contract with nowhere to sign is worse than the wrong layout — so Save
  // says so rather than offering a button that 400s.
  const unplaced = placerTargets
    .filter(t => t.required && !draftLayout?.blocks?.[t.key]).map(t => t.label);

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
                : "as many a side as actually sign. Everybody named gets their "
                  + "own line and their own boxes on the signature page."}
            </span>
            {mayNameSigners && (
              <span className="right" style={{ display: "flex", gap: 8 }}>
                {/* Where the blocks go, dragged onto the contract itself. The
                    two automatic arrangements answer "side by side or one
                    above the other?", which is the whole question for most
                    contracts and none of it for one that has to be
                    countersigned beside a particular clause. */}
                {sigSpec && (
                  <button className="btn sm" type="button" onClick={openPlacer}
                          disabled={noWording}
                          title={noWording
                            ? "There is no wording yet, so there is no page to "
                              + "put a block on"
                            : "Drag each side\u2019s signature block onto the "
                              + "page where it should sit"}>
                    <Move size={12} /> Place the signature boxes
                  </button>
                )}
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
                            {(sig.outside || !sig.access) && (
                              <div style={{ display: "flex", gap: 6, marginTop: 4,
                                            flexWrap: "wrap" }}>
                                {sig.outside && (
                                  <span className="badge b-warn">
                                    <Globe size={11} /> Outside Kavachio
                                  </span>
                                )}
                                <span className={`badge ${sig.access ? "b-info" : "b-mut"}`}>
                                  {sig.access
                                    ? "Signs here"
                                    : "Printed only · signs on paper"}
                                </span>
                                {mayNameSigners && (
                                  <span
                                    className="linkish" role="button"
                                    onClick={() => setSignatories(l => l.map(
                                      x => x.id === sig.id
                                        ? { ...x, access: !x.access } : x))}
                                  >
                                    {sig.access ? "Print only" : "Let them sign here"}
                                  </span>
                                )}
                              </div>
                            )}
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

            {signatories.length > 2 && (
              <div className="hint" style={{ marginTop: 12 }}>
                They are asked <b>in the order they are listed</b>, your side
                first: each one is emailed a link once the person before them
                has signed, and the document they open already carries that
                signature.
              </div>
            )}

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

              {/* Asked BEFORE the name is added, because it is a decision about
                  a person and not a setting: somebody from outside is named on
                  plenty of contracts without ever being let into this system,
                  and turning every typed address into a signing link would be
                  this screen making that call on the carrier’s behalf. */}
              {lookFits && (outside ? (
                <div className="note warn" style={{ marginTop: 12 }}>
                  <b>
                    <Globe size={13} style={{ verticalAlign: "-2px" }} />{" "}
                    This person is outside Kavachio.
                  </b>{" "}
                  Nobody at <span className="mono">{look!.email}</span> holds an
                  account on either side of this contract. Say what that means
                  for them:
                  <div style={{ display: "flex", gap: 20, marginTop: 10,
                                flexWrap: "wrap" }}>
                    {[
                      { on: true, title: "Let them sign here",
                        why: "They get their own boxes on the contract and an "
                           + "emailed link with a one-time code. No Kavachio "
                           + "account is created and they see nothing else." },
                      { on: false, title: "Print their name only",
                        why: "Their lines appear on the signature page with "
                           + "nothing to click on. They sign the printed copy." },
                    ].map(o => (
                      <label key={String(o.on)}
                             style={{ display: "flex", gap: 8, cursor: "pointer",
                                      alignItems: "flex-start", maxWidth: 330 }}>
                        <input type="radio" name="signer-access"
                               checked={grantAccess === o.on}
                               onChange={() => setGrantAccess(o.on)} />
                        <span>
                          <b style={{ color: "var(--p-ink)" }}>{o.title}</b>
                          <div className="sub">{o.why}</div>
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="note ok" style={{ marginTop: 12 }}>
                  <b>
                    <Check size={13} style={{ verticalAlign: "-2px" }} />{" "}
                    {look!.name}
                  </b>{" "}
                  {[look!.role, look!.org].filter(Boolean).join(" at ")} —
                  already on Kavachio, so they can sign here.
                </div>
              ))}

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
              {round?.can_sign ? (
                <button className="btn pri" type="button"
                        onClick={signOnTheDocument}
                        title="Opens the contract in a new tab with your
                               signature box on it">
                  <PenLine size={14} /> Sign the contract
                  <ExternalLink size={12} style={{ marginLeft: 6 }} />
                </button>
              ) : round?.started ? (
                <button className="btn" type="button"
                        onClick={signOnTheDocument}
                        title="Open the document this round is running on">
                  <ExternalLink size={13} /> Open the document
                </button>
              ) : null}
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
            ) : round?.can_sign ? (
              <div className="note" style={{ marginBottom: 14 }}>
                <b>It is your turn to sign.</b>{" "}
                Sign the contract opens the document in a new tab with your
                signature box waiting on its execution page. You are already
                logged in, so there is no email and no code to wait for.{" "}
                {round.started
                  ? "The other side has been asked and this round is already running."
                  : "Nothing goes to the other side until you have signed — "
                    + "they are emailed the document carrying your signature, "
                    + "and it also appears on their dashboard."}
              </div>
            ) : round?.started && !round.can_sign ? (
              <div className="note warn" style={{ marginBottom: 14 }}>
                <b>Out for signature{round.waiting_on_name
                    ? ` — waiting on ${round.waiting_on_name}`
                    : ""}.</b>{" "}
                {round.i_have_signed
                  ? "You have signed. Everybody gets the signed copy by email "
                    + "once the other side has."
                  : round.why ?? "It is not your move."}
              </div>
            ) : (
              <div className="note" style={{ marginBottom: 14 }}>
                <b>A contract goes in force when both sides have signed it.</b>{" "}
                There is no other way to make one live, and the second signature
                normally does it on its own.{" "}
                {round && !round.started && round.why
                  ? `It cannot be signed yet — ${round.why}.`
                  : ""}
              </div>
            )}

            <div className="grid g-2">
              {(["carrier", "counterparty"] as Side[]).map(sd => {
                const done = rec.signatures.filter(g => g.side === sd);
                const mine = sd === (maySignHere ? myS : null);
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

            {(maySignHere || rec.actions.record_signature) && (
              <>
                <div className="divider" />
                {maySignHere ? (
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
                      onClick={() => doSign(!maySignHere)}
                    >
                      <PenLine size={14} />{" "}
                      {signing ? "Signing\u2026"
                        : maySignHere ? "Sign the contract"
                        : "Record their signature"}
                    </button>
                  </div>
                </div>

                {/* Both are offered when the carrier can do either, so
                    recording somebody else\u2019s is a deliberate second click
                    and never the default. */}
                {maySignHere && rec.actions.record_signature && (
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

      {/* ── where the blocks go ──
          The pages are this contract as it stands, composed on the server, and
          the box dragged here is the size of the block that gets drawn — so
          what is on the screen is what comes out of the printer, and a signing
          box lands exactly where the block was left. */}
      {sigSpec && draftLayout && (
        <Modal
          open={placing} size="3xl"
          title="Place the signature boxes"
          onClose={() => setPlacing(false)}
          footer={
            <>
              <span className="sub" style={{ marginRight: "auto" }}>
                {unplaced.length === 0
                  ? "Every side has somewhere to sign. Drag any block to move "
                    + "it — anybody left unplaced signs under their side."
                  : `Still to place: ${unplaced.join(" and ")}.`}
              </span>
              <button className="btn" type="button"
                      onClick={() => setPlacing(false)}>
                Cancel
              </button>
              <button className="btn pri" type="button"
                      disabled={placeSaving || unplaced.length > 0}
                      onClick={savePlacement}>
                {placeSaving ? "Saving\u2026" : "Save where they go"}
              </button>
            </>
          }
        >
          <p className="hint" style={{ marginTop: 0 }}>
            Pick a block, then click the page where it should sit — or drag one
            that is already there. <b>Everybody named gets a block of their
            own</b>, so three people signing can go in three different places.
            Everything that person signs moves with their block: the ruled line,
            the printed name, the job title and the date. Leave somebody
            unplaced and they sign under their side, stacked, as before.
          </p>
          <SignaturePlacer
            source={{ kind: "contract", id }}
            layout={draftLayout}
            block={sigSpec.placed_block}
            targets={placerTargets}
            onPlace={(sideKey, spot) => setDraftLayout(l => l && ({
              ...l, blocks: { ...(l.blocks ?? {}), [sideKey]: spot },
            }))}
            onRemove={sideKey => setDraftLayout(l => {
              if (!l) return l;
              const rest = { ...(l.blocks ?? {}) };
              delete rest[sideKey];
              return { ...l, blocks: rest };
            })}
            onAnchor={(sideKey, after) => setDraftLayout(l => l && ({
              ...l, blocks: { ...(l.blocks ?? {}), [sideKey]: { after } },
            }))}
          />
        </Modal>
      )}
    </div>
  );
}