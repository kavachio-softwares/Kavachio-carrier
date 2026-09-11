/**
 * One contract — its terms, its documents, and where it is in its life.
 *
 * Distinct from ContractDetail, which shows what a contract PRODUCED: the
 * clauses that were read out of it and the rules they became. This shows the
 * contract itself. The two are linked, not merged, because they answer
 * different questions and are read by different people at different moments.
 *
 * Everything on this page that can be DONE comes from `record.actions`, decided
 * by the server. Nothing here re-derives whether a contract may be submitted or
 * put in force — a screen that worked that out itself would eventually offer a
 * button the API refuses, and the user would have no way to tell which of the
 * two was wrong.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, ArrowRight, CheckCircle2, ChevronDown,
  ChevronRight, Download, ExternalLink,
  Eye, FileText, History, MessagesSquare, Paperclip, PenLine, Plus, RefreshCw,
  Send, ShieldCheck, Trash2, Upload, XCircle,
} from "lucide-react";
import { currentMga, getTenantBrand } from "../auth";
import { SignaturePlacer, signerTargets }
  from "../components/SignaturePlacer";
import { InfoTip } from "../components/InfoTip";
import { ClauseText } from "../components/ClauseText";
import { WordingEditor } from "../components/WordingEditor";
import { fmtDate, fmtStamp } from "../utils/date";
import { describeChecks } from "../utils/contractChecks";
import { TermDurationField, useTermDuration } from "../components/TermDuration";
import { EXPIRY_FIELD, INCEPTION_FIELD, type TermSpec } from "../utils/term";
import {
  chipEdits, readTermMove, restoreChips, type TermMove,
} from "../utils/wordingEdits";
import { getApprovalHistory, type ApprovalEvent } from "../api/hierarchy";
import {
  getContractRound, inAppSigningUrl, type ContractRound,
} from "../api/esign";
import {
  acceptTerms, activateContract, deactivateDocument, downloadDocument,
  openDocument,
  bindChecks,
  generateRules, getContract, getContractTypes, renewContract, requestChanges,
  sendForReview, skipReview, submitSigned, terminateContract,
  downloadContractPdf, previewWording,
  updateContract, uploadDocument, fieldErrors,
  type ContractDocumentKind, type ContractField, type ContractRecord as Rec,
  type AgreedLimits, type AgreedLimitSpec, type ContractTypeSpec, type FieldErrors,
  type Lifecycle, type LimitGroup,
  type SignatureBlockSpec, type SignatureLayout, type SeveritySpec,
  getContractClauses,
  type ContractClause, type ContractClauseRule,
  type ProposedChange, type WordingSection,
} from "../api/contractRecord";

const STATE: Record<Lifecycle, { label: string; cls: string; note: string }> = {
  // The note is filled in per contract — a carrier's own draft has nobody to
  // submit it to, so "not submitted" would be describing a queue that does not
  // exist for it. See draftNote().
  draft: { label: "Draft", cls: "b-mut", note: "" },
  pending: { label: "Pending", cls: "b-warn",
             note: "Submitted and waiting on the carrier's decision." },
  in_review: { label: "Out for review", cls: "b-warn",
               note: "The terms are with the broker. They can agree them or "
                   + "ask for changes." },
  changes_requested: { label: "Changes requested", cls: "b-warn",
                       note: "The broker has pushed back. Revise the terms and "
                           + "send them again." },
  agreed: { label: "Terms agreed", cls: "b-ok",
            note: "Both sides have settled the terms. The broker signs next, "
                + "and returns it." },
  signed: { label: "Signed", cls: "b-ok",
            note: "The broker signed and returned it. It is with the carrier "
                + "to place and put in force." },
  active: { label: "In force", cls: "b-ok",
            note: "Signed by both sides and running. Bordereaux can be "
                + "produced against it." },
  expired: { label: "Expired", cls: "b-mut",
             note: "Its term has run out. Renew it into a successor." },
  terminated: { label: "Terminated", cls: "b-crit",
                note: "Ended early. Nothing more can be attached to it." },
  superseded: { label: "Superseded", cls: "b-mut",
                note: "Replaced by a renewal." },
};

const DOC_KIND: Record<ContractDocumentKind, { label: string; blurb: string }> = {
  // One sentence each. These sit beside the Attach button as you pick a kind,
  // so they have to be readable at a glance — the reasoning behind each rule
  // is in the code, not on the screen.
  contract: {
    label: "Wording",
    blurb: "The contract itself — attaching a new one retires the previous.",
  },
  reference: {
    label: "Reference",
    blurb: "A document the wording defers to, and cannot be checked without.",
  },
  endorsement: {
    label: "Endorsement",
    blurb: "A change agreed later; it sits alongside the wording rather than "
         + "replacing it.",
  },
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="sub">{label}</div>
      <div style={{ fontSize: 13, marginTop: 2 }}>{children ?? "—"}</div>
    </div>
  );
}

export default function ContractRecord() {
  const { contractId } = useParams();
  const id = Number(contractId);
  const nav = useNavigate();
  // The raise flow attaches documents AFTER creating the contract, so a
  // document that failed to attach is reported here — where it can be retried —
  // rather than as a failure to create a contract that in fact exists.
  const handoff = (useLocation().state as { notice?: string } | null)?.notice;

  const [rec, setRec] = useState<Rec | null>(null);
  const [history, setHistory] = useState<ApprovalEvent[]>([]);
  // What this contract SAYS (its clauses) and what it CHECKS (the rules those
  // became). Loaded here rather than reached through a link, because both are
  // facts about the contract on this page.
  const [clauses, setClauses] = useState<ContractClause[] | null>(null);
  const [clauseRules, setClauseRules] = useState<ContractClauseRule[]>([]);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");

  // Upload form
  const [kind, setKind] = useState<ContractDocumentKind>("contract");
  const [file, setFile] = useState<File | null>(null);
  const [satisfies, setSatisfies] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  // Whether the wording being attached is the SIGNED copy rather than a draft.
  const [executedCopy, setExecutedCopy] = useState(false);
  // The rules on file were generated without whatever was just attached.
  const [rulesStale, setRulesStale] = useState(false);
  // Reading a document IN the page. A .docx cannot be rendered by a browser, so
  // only PDFs open inline; everything else says so and offers the download
  // rather than opening a blank frame.
  const [viewing, setViewing] = useState<
    { id: number; name: string; url: string; type: string } | null>(null);

  // Where the electronic signing round has got to. Read from the server so the
  // button and the endpoint behind it cannot disagree about whether pressing it
  // will work — see api/esign.getContractRound.
  const [round, setRound] = useState<ContractRound | null>(null);

  // Negotiation
  const [showSign, setShowSign] = useState(false);
  const [signedDocId, setSignedDocId] = useState("");
  const [executedDate, setExecutedDate] = useState("");
  const [showRequest, setShowRequest] = useState(false);
  const [reqNote, setReqNote] = useState("");
  const [reqChanges, setReqChanges] = useState<ProposedChange[]>([]);
  const [reviewNote] = useState("");
  // Skipping the review is asked for rather than done on the click: it spends
  // the other side's chance to object, and the reason for spending it is worth
  // more on the record than the click was.
  const [showSkip, setShowSkip] = useState(false);
  const [skipNote, setSkipNote] = useState("");

  // Termination / renewal
  const [showTerminate, setShowTerminate] = useState(false);
  const [reason, setReason] = useState("");
  const [showRenew, setShowRenew] = useState(false);
  const [renewFrom, setRenewFrom] = useState("");
  const [renewTo, setRenewTo] = useState("");
  const [termSpec, setTermSpec] = useState<TermSpec | null>(null);

  // Editing the terms. The inputs are built from the SERVER's field spec, the
  // same one the create form uses and the same one the server validates
  // against — so "what this type requires" is stated once, not three times.
  const [specs, setSpecs] = useState<ContractTypeSpec[]>([]);
  // The limits vocabulary, so this screen shows what was agreed in the same
  // words and the same three groups the create flow asked for it in.
  const [limitSpec, setLimitSpec] = useState<AgreedLimitSpec[]>([]);
  const [limitGroups, setLimitGroups] = useState<LimitGroup[]>([]);
  // The signature block: the vocabulary from the server, and this contract's
  // own choice. Editable here as well as at step 4 — a block is a term of the
  // contract like any other, and a carrier who got it wrong should not have to
  // raise the contract again to fix it.
  const [sigSpec, setSigSpec] = useState<SignatureBlockSpec | null>(null);
  // The severity words, from the server — see SeveritySpec. Typing them here
  // is what let this screen and the create form drift apart.
  const [sevSpec, setSevSpec] = useState<SeveritySpec[]>([]);
  const sevLabel = (k?: string | null) =>
    sevSpec.find(x => x.key === k)?.label ?? k ?? "—";
  const [draftSig, setDraftSig] = useState<SignatureLayout | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  // The agreed limits, editable while the contract is still a draft. A draft is
  // a contract nobody has agreed to, so its substance has to be changeable —
  // being able to edit its name but not its commission would be a strange
  // half-draft.
  const [draftLimits, setDraftLimits] = useState<AgreedLimits>({});
  const [showAllLimits, setShowAllLimits] = useState(false);
  const [fieldErrs, setFieldErrs] = useState<FieldErrors>({});

  // The wording is edited on its own card rather than inside the terms form.
  // It is a document, not a field: it wants the width of the page, one section
  // at a time, and the chips that tie its sentences to the terms above.
  const [wEditing, setWEditing] = useState(false);
  const [wSections, setWSections] = useState<WordingSection[]>([]);
  // Collapsed by default — see the What was agreed, Clauses and Rules cards
  // below.
  const [agreedOpen, setAgreedOpen] = useState(false);
  const [clausesOpen, setClausesOpen] = useState(false);
  const [rulesOpen, setRulesOpen] = useState(false);
  // Which heading is open for renaming. A heading is not part of the clause
  // text and must not be typed into by accident — it is the thing a reader
  // navigates by — so it is a label until it is asked to be an input.
  const [wRenaming, setWRenaming] = useState<string | null>(null);
  const [wTokens, setWTokens] = useState<Record<string, string>>({});
  // Bumped only when the sections are REPLACED wholesale (rebuilt from the
  // terms). The editor rebuilds its DOM on this, and rebuilding it on every
  // keystroke would throw the caret to the start of the clause.
  const [wVersion, setWVersion] = useState(0);
  // Terms a chip was typed over, waiting to be saved WITH the wording — one
  // request, because a clause saying 15% beside a term still reading 11% is
  // exactly the state this exists to prevent, and two requests can leave it.
  const [wMoves, setWMoves] = useState<AgreedLimits>({});

  useEffect(() => {
    getContractTypes()
      .then(d => {
        setSpecs(d.types);
        setLimitSpec(d.agreed_limits);
        setLimitGroups(d.limit_groups);
        setTermSpec(d.term);
        setSigSpec(d.signature_block);
        setSevSpec(d.severities);
      })
      .catch(() => setSpecs([]));
  }, []);

  const load = useCallback(() => {
    getContract(id)
      .then(r => {
        setRec(r);
        // Needs the programme: clauses and rules are stored against the
        // (programme, contract) pair, which is the scope they are read in.
        if (r.programme?.id) {
          getContractClauses(r.programme.id, id)
            .then(d => { setClauses(d.clauses); setClauseRules(d.rules); })
            .catch(() => { setClauses([]); setClauseRules([]); });
        } else {
          setClauses([]);
        }
      })
      .catch(e => setErr(e?.response?.data?.detail || "Could not load this contract."));
    getApprovalHistory(id).then(setHistory).catch(() => setHistory([]));
    // Its own call and its own failure: a round that cannot be read must never
    // stop the contract being shown. Absent, the buttons fall back to the
    // signature screen, which is where they pointed before there were rounds.
    getContractRound(id).then(setRound).catch(() => setRound(null));
  }, [id]);
  useEffect(load, [load]);

  // An authored contract cannot take a replacement wording, so never leave the
  // picker sitting on a kind that is not offered.
  useEffect(() => {
    if (rec?.wording_sections?.length && kind === "contract") setKind("reference");
  }, [rec, kind]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setErr("");
    setNote("");
    try {
      await fn();
      load();
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  async function doUpload() {
    if (!file) return;
    await run("upload", async () => {
      const r = await uploadDocument(id, file, {
        kind,
        satisfiesReference: kind === "reference" ? satisfies || null : null,
        effectiveFrom: kind === "endorsement" ? effectiveFrom || null : null,
        isExecutedCopy: kind === "contract" ? executedCopy : false,
      });
      setFile(null);
      setSatisfies("");
      setEffectiveFrom("");
      setExecutedCopy(false);
      if (r.rules_stale) setRulesStale(true);
      setNote(
        r.rules_stale
          ? `Attached. The rules on file were read without it — re-read the `
            + `contract to bring them up to date.`
          : "Attached.");
    });
  }

  /** Terms → checks. See bindChecks: the server resolves which bordereau
   *  template this contract reports into, so there is nothing to choose. */
  async function doGenerate() {
    await run("rules", async () => {
      const r = await generateRules(id, rec?.output_template?.id ?? null);
      setRulesStale(false);
      const bits = [
        r.counts ? `${r.counts.validation_rule ?? 0} rules` : null,
        r.endorsements_applied.length
          ? `${r.endorsements_applied.length} endorsement(s) applied` : null,
        r.references_applied.length
          ? `${r.references_applied.length} reference(s) resolved` : null,
      ].filter(Boolean);
      setNote(`Re-read from the active documents — ${bits.join(", ") || "done"}.`);
    });
  }

  // ── hooks stop here ──────────────────────────────────────────────────────
  // Every hook on this screen has to be ABOVE the guard below. React counts
  // them, and the loading render (no contract yet) runs a shorter list than the
  // loaded one — which is "Rendered more hooks than during the previous
  // render", and it takes the whole page down rather than degrading.
  //
  // TWO terms live here and they must not share a picker: the one being edited,
  // and the one a renewal would start. A single hook would carry the length
  // chosen for the successor back onto the contract in force.
  const editTerm = useTermDuration({
    spec: termSpec,
    inception: draft[INCEPTION_FIELD] ?? "",
    expiry: draft[EXPIRY_FIELD] ?? "",
    setInception: v => setDraft(d => ({ ...d, [INCEPTION_FIELD]: v })),
    setExpiry: v => setDraft(d => ({ ...d, [EXPIRY_FIELD]: v })),
  });
  const renewTerm = useTermDuration({
    spec: termSpec,
    inception: renewFrom, expiry: renewTo,
    setInception: setRenewFrom, setExpiry: setRenewTo,
  });

  if (!rec) {
    return (
      <div className="proto">
        <div className="view full">
          <div className="page-head"><div className="t"><h2>Contract</h2></div></div>
          {err
            ? <div className="note warn" style={{ maxWidth: 620 }}>{err}</div>
            : <div className="empty">Loading…</div>}
        </div>
      </div>
    );
  }

  const st = STATE[rec.lifecycle] ?? STATE.draft;
  // A draft belongs to the carrier — there is no other kind now — so it means
  // one thing: written down, not live, still editable.
  const draftNote = "Not live yet — nothing is checked against it. Its terms "
    + "can still be changed, and you can make it live when you are ready.";
  const a = rec.actions;
  const docs = rec.documents ?? [];
  const active = docs.filter(d => d.is_active);
  const retired = docs.filter(d => !d.is_active);

  // Was this contract WRITTEN here, or does it come from a document somebody
  // else drafted? The answer changes what may be attached to it.
  const authored = !!rec.wording_sections?.length;
  // No sections and no uploaded wording either: the contract exists as terms
  // alone. That is a legitimate way to start — the terms are what get
  // negotiated — but it must not be a dead end, so a draft in this state is
  // offered the wording rather than being shown nothing.
  const hasUploadedWording = (rec.documents ?? [])
    .some(d => d.kind === "contract" && d.is_active);
  const canWriteWording = rec.actions.edit && !authored && !hasUploadedWording;

  // An authored contract's wording is generated from its terms, and the two are
  // tied: the sentences hold tokens, so changing a term moves the wording and
  // the check together. Uploading a replacement wording would put a document on
  // the record whose text is tied to nothing — the record would show one set of
  // agreed terms and a wording saying something else. So it is not offered.
  // Changing an authored contract's wording means changing its terms, or
  // endorsing it.
  const attachableKinds = (Object.keys(DOC_KIND) as ContractDocumentKind[])
    .filter(k => !(authored && k === "contract"));

  const spec = specs.find(t => t.key === rec.contract_type) ?? null;
  // The counterparty is not editable here: for a broker's own contract it is
  // always that broker, and moving a carrier's contract to a different
  // counterparty is a different contract, not an edit.
  const editable = (spec?.fields ?? []).filter(f => f.name !== "counterparty_party_id");

  /** Every term a change request may name, in one list.
   *
   *  TWO VOCABULARIES, and the negotiation needs both. The contract's identity
   *  fields — name, term, class of business — are one; the AGREED LIMITS are
   *  the other, and they are where the money is: commission, brokerage, the
   *  per-risk limit. A broker pushes back on the commission rate far more often
   *  than on the contract's name, and while only the identity fields were
   *  offered the negotiation could argue about everything except the terms.
   *
   *  The server has always accepted both (contract_routes.request_changes
   *  checks `field_names | AGREED_LIMITS`) — it was only this list that was
   *  half of it. */
  const termChoices = [
    ...editable.map(f => ({ name: f.name, label: f.label, limit: false })),
    ...limitSpec.map(l => ({ name: l.name, label: l.question, limit: true })),
  ];

  /** Whether a named term is an agreed limit rather than a contract field.
   *  They are written to different places, so this decides how a request is
   *  applied as well as where its current value is read from. */
  function isLimit(field: string): boolean {
    return limitSpec.some(l => l.name === field);
  }

  /** What a term says right now, as text. Sent with a change request so the
   *  carrier can tell a request that is still about the live value from one
   *  that an edit overtook in the meantime.
   *
   *  A limit is not a column on the contract — it lives in `agreed_limits`
   *  under its own key — so reading it off the record directly returned
   *  nothing, and every request against one looked like it was about a blank. */
  function currentValue(field: string): string {
    if (isLimit(field)) {
      const v = rec?.agreed_limits?.[field]?.value;
      return v == null ? "" : String(v);
    }
    const v = (rec as unknown as Record<string, unknown>)[field];
    return v == null ? "" : String(v);
  }

  /** What the wording preview needs. The same shape the create flow sends, so
   *  a clause edited here and one written there are computed identically.
   *
   *  A PLAIN function, deliberately. Everything below this point runs only
   *  after the `if (!rec)` guard above, so a hook here would run on some
   *  renders and not others — which is what React counts, and miscounting it
   *  takes the whole page down. Nothing here is expensive enough to memoise. */
  function wordingInput(secs?: WordingSection[] | null) {
    return {
      contract_type: rec?.contract_type ?? undefined,
      values: {
        name: rec?.name, inception_dt: rec?.inception_dt,
        expiry_dt: rec?.expiry_dt, class_of_business: rec?.class_of_business,
        schedule_key: rec?.schedule_key,
        notice_period_days: rec?.notice_period_days,
      },
      agreed_limits: rec?.agreed_limits ?? {},
      sections: secs ?? undefined,
      carrier_name: getTenantBrand()?.legal_name || currentMga(),
      counterparty_name: rec?.counterparty?.name ?? null,
      programme_name: rec?.programme?.name ?? null,
    };
  }

  /** Open the wording for editing. Asks the server to resolve the tokens first,
   *  so the chips carry today's values rather than the ones the wording was
   *  written with. */
  async function beginWording(rebuild = false) {
    setErr("");
    try {
      const pv = await previewWording(
        wordingInput(rebuild ? null : rec?.wording_sections ?? null));
      setWSections(pv.sections);
      setWTokens(pv.tokens);
      setWMoves({});
      setWVersion(v => v + 1);
      setWRenaming(null);
      setWEditing(true);
      if (rebuild) {
        setNote("Rewritten from the terms. Nothing is saved until you save it.");
      }
    } catch (e) {
      setErr(fieldErrors(e).message);
    }
  }

  /** A chip was typed over, so the term follows the words. See the same
   *  function in ContractNew — one gesture, one meaning, both screens. */
  function chipsEdited(before: string, after: string, at: number) {
    const live: AgreedLimits = { ...(rec?.agreed_limits ?? {}), ...wMoves };
    const moves: TermMove[] = [];
    for (const edit of chipEdits(before, after)) {
      const term = limitSpec.find(l => l.name === edit.token);
      if (!term) continue;
      const move = readTermMove(edit, { kind: term.kind, choices: term.choices });
      if (!move) continue;
      if (String(live[edit.token]?.value ?? "") === move.value) continue;
      moves.push(move);
    }
    if (!moves.length) return;

    const next: AgreedLimits = { ...live };
    for (const m of moves) {
      next[m.token] = { ...(next[m.token] ?? {}), value: m.value };
    }
    const body = restoreChips(before, after, moves);
    const nextSections = wSections.map(
      (x, j) => j === at ? { ...x, body } : x);

    setWMoves(w => {
      const out = { ...w };
      for (const m of moves) out[m.token] = next[m.token];
      return out;
    });
    setWSections(nextSections);
    setWVersion(v => v + 1);
    setNote(
      moves.map(m => {
        const term = limitSpec.find(l => l.name === m.token);
        return `${term?.question ?? m.token} is now `
             + `${m.value}${term?.unit ?? ""}`;
      }).join(", ")
      + " — the term moves with the sentence. Save the wording to keep it.");

    // The chip has to come back carrying the new value, which means re-reading
    // with the new terms rather than the ones on the record.
    previewWording({ ...wordingInput(nextSections), agreed_limits: next })
      .then(pv => setWTokens(pv.tokens))
      .catch(() => {});
  }

  /** A chip edited in place on the record. Held with the other pending term
   *  moves and saved WITH the wording — see chipsEdited. */
  function chipValue(token: string, text: string) {
    const live: AgreedLimits = { ...(rec?.agreed_limits ?? {}), ...wMoves };
    const term = limitSpec.find(l => l.name === token);
    if (!term) return;
    const move = readTermMove({ token, text },
                              { kind: term.kind, choices: term.choices });
    if (!move) return;
    if (String(live[token]?.value ?? "") === move.value) return;
    const entry = { ...(live[token] ?? {}), value: move.value };
    setWMoves(w => ({ ...w, [token]: entry }));
    setNote(`${term.question} is now ${move.value}${term.unit ?? ""} — save the `
          + "wording to keep it.");
    previewWording({ ...wordingInput(wSections),
                     agreed_limits: { ...live, [token]: entry } })
      .then(pv => setWTokens(pv.tokens))
      .catch(() => {});
  }

  async function saveWording() {
    if (!wSections.length) {
      setErr("A contract needs at least one clause. Add one, or rewrite the "
           + "wording from the terms.");
      return;
    }
    setBusy("wording");
    setErr("");
    try {
      const moved = Object.keys(wMoves);
      const saved = await updateContract(id, {
        wording_sections: wSections.map(
          ({ key, title, body, origin }) => ({ key, title, body, origin })),
        // Only when a chip was typed over. Absent means unchanged, so a plain
        // wording edit never touches what was agreed.
        ...(moved.length
          ? { agreed_limits: { ...(rec?.agreed_limits ?? {}), ...wMoves } }
          : {}),
      });
      setWMoves({});
      setWEditing(false);
      // A figure typed where a chip used to be is tied back to its term on the
      // way in, so the sentence keeps moving when the term does. Said out loud
      // — it is the text of a contract.
      const retied = saved.wording_retied ?? [];
      if (moved.length) {
        setNote(
          "The wording was updated, and so were the terms it quotes: "
          + moved.map(k => {
              const term = limitSpec.find(l => l.name === k);
              return `${term?.question ?? k} is now `
                   + `${saved.agreed_limits?.[k]?.value ?? ""}`;
            }).join(", ")
          + ". The checks moved with them.");
        setWEditing(false);
        load();
        return;
      }
      setNote(retied.length
        ? `The wording was updated. ${retied.join(" and ")} `
          + `${retied.length === 1 ? "was" : "were"} typed in as a figure, so `
          + `${retied.length === 1 ? "it has" : "they have"} been tied back to `
          + `the term — the clause now moves when the term does.`
        : "The wording was updated.");
      load();
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

    /** The human label for a term, for showing a change request back. */
  function labelOf(field: string): string {
    return termChoices.find(f => f.name === field)?.label ?? field;
  }

  /** Whether the change request says anything the carrier can act on yet.
   *
   *  Prose, OR a named term carrying a value. The note used to be the only way
   *  in, so a broker who had filled the row exactly — the term, what it says
   *  now, what they want, why — was left staring at a dead button with nothing
   *  on screen telling them what was missing. A named term is not a wall; it is
   *  the most precise form the request can take. The server keeps the same
   *  rule, so this cannot drift into offering a button the API refuses. */
  const requestSaysSomething =
    !!reqNote.trim()
    || reqChanges.some(ch => !!ch.field && !!(ch.proposed ?? "").trim());

  /** Whether every term the broker named already says what they asked for.
   *
   *  Pressing Apply again would be a no-op, and a primary button that does
   *  nothing is how somebody concludes the screen is broken. What is left to
   *  do at that point is send the revised terms back, so the row says so. */
  function changeRequestApplied(): boolean {
    const named = (rec?.open_change_request?.proposed_changes ?? [])
      .filter(ch => !!ch.proposed);
    return named.length > 0
      && named.every(ch => currentValue(ch.field) === ch.proposed);
  }

  function beginEdit() {
    const seed: Record<string, string> = {};
    for (const f of editable) {
      const v = (rec as unknown as Record<string, unknown>)[f.name];
      seed[f.name] = v == null ? "" : String(v);
    }
    setDraft(seed);
    setDraftLimits(rec?.agreed_limits ?? {});
    // The server normalises this on the way out, so there is always a layout to
    // seed from — an older contract seeds with the four lines it already has.
    setDraftSig(rec?.signature_layout ?? sigSpec?.default ?? null);
    setFieldErrs({});
    setEditing(true);
  }

  function setDraftLimit(key: string, patch: Partial<AgreedLimits[string]>) {
    setDraftLimits(l => {
      const cur = l[key] ?? { value: "" };
      const next = { ...cur, ...patch };
      if (next.value === "") { const { [key]: _d, ...rest } = l; return rest; }
      return { ...l, [key]: next };
    });
  }

  async function saveEdit() {
    setBusy("edit");
    setErr("");
    setFieldErrs({});
    try {
      const body: Record<string, unknown> = {};
      for (const f of editable) {
        const raw = (draft[f.name] ?? "").trim();
        body[f.name] =
          raw === "" ? null
          : f.kind === "int" ? Number.parseInt(raw, 10)
          : f.kind === "decimal" ? Number.parseFloat(raw)
          : raw;
      }
      await updateContract(id, {
        ...(body as Record<string, unknown>),
        // Sent together so one save covers the whole contract — the identity
        // and the substance change in one step, or not at all.
        agreed_limits: draftLimits,
        ...(draftSig ? { signature_layout: draftSig } : {}),
      });
      setEditing(false);
      setNote("Contract updated.");
      load();
    } catch (e) {
      const { message, errors } = fieldErrors(e);
      setErr(message);
      setFieldErrs(errors);
    } finally {
      setBusy("");
    }
  }

  function editInput(f: ContractField) {
    const bad = fieldErrs[f.name];
    // Same rule as the new-contract form: while a length is in force the
    // duration owns the expiry, and Custom is how you take it back.
    const shut = f.name === EXPIRY_FIELD && editTerm.ready && !editTerm.custom;
    return (
      <div className="field" key={f.name} style={{ marginBottom: 0 }}>
        <label>
          {f.label}{f.required && <span style={{ color: "var(--p-crit-ink)" }}>*</span>}
        </label>
        <input
          type={f.kind === "date" ? "date"
               : f.kind === "int" || f.kind === "decimal" ? "number" : "text"}
          step={f.kind === "decimal" ? "0.01" : undefined}
          // The same served reference text the create form shows. Editing a
          // contract used to offer bare boxes, so the shape of an answer was
          // only ever explained on the way in.
          placeholder={f.example ?? undefined}
          value={draft[f.name] ?? ""}
          disabled={shut}
          onChange={e => (
            f.name === INCEPTION_FIELD ? editTerm.onInception(e.target.value)
            : f.name === EXPIRY_FIELD ? editTerm.onExpiry(e.target.value)
            : setDraft(d => ({ ...d, [f.name]: e.target.value })))}
          style={bad ? { borderColor: "var(--p-crit)" } : undefined}
        />
        {bad ? (
          <div className="hint" style={{ color: "var(--p-crit-ink)" }}>{bad}</div>
        ) : shut ? (
          <div className="hint">
            Set by the duration — choose Custom to type a date.
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>{rec.name}</h2>
            <p>
              {[rec.contract_type_label ?? "type not set",
                rec.counterparty?.name, rec.programme?.name]
                .filter(Boolean).join(" · ")}
            </p>
          </div>
          <div className="actions">
            {/* The whole contract, not one part of it — schedule, wording and
                a signature page in one PDF. In the header rather than on the
                wording card, because it is the contract that gets sent, and
                somebody looking for it should not have to work out which
                section of the page owns it. Offered to whoever can read the
                contract, since a broker reviewing terms needs the file more
                than the carrier who wrote them does. */}
            {(authored || (rec.agreed_limits
                           && Object.keys(rec.agreed_limits).length > 0)) && (
              <button
                className="btn" type="button" disabled={busy === "pdf"}
                onClick={() => run("pdf", () => downloadContractPdf(id, rec.name))}
                title="Composed now, from the terms and wording as they stand.
                       It is a separate document — it does not change when a
                       term does, and it is not kept anywhere."
              >
                <Download size={14} />{" "}
                {busy === "pdf" ? "Composing…" : "Download contract"}
              </button>
            )}
            <Link to="/contracts" className="btn">
              <ArrowLeft size={14} /> Contracts
            </Link>
          </div>
        </div>

        {err && (
          <div className="note warn" style={{ marginBottom: 14, maxWidth: 700 }}>{err}</div>
        )}
        {note && (
          <div className="note ok" style={{ marginBottom: 14, maxWidth: 700 }}>{note}</div>
        )}
        {handoff && !note && (
          <div className="note warn" style={{ marginBottom: 14, maxWidth: 700 }}>{handoff}</div>
        )}

        {/* ── the signatures ──
            Shown wherever a contract is close to going live or already has,
            because "why is this not live yet" and "who signed this" are the
            two questions this page gets asked most once the terms are settled.
            Read-only here: signing itself is done on the signature screen,
            where the contract can be read first. */}
        {(rec.signatures.length > 0
          || ["agreed", "signed"].includes(rec.lifecycle)) && (
          <div className="card" style={{ marginBottom: 14 }}>
            <div className="card-h">
              <PenLine size={16} className="ci" />
              <h3>Signatures</h3>
              <span className="sub">
                {rec.unsigned_sides.length === 0
                  ? "both sides have signed"
                  : `waiting on ${rec.unsigned_sides
                      .map(u => u === "carrier"
                        ? "the carrier"
                        : rec.counterparty?.name ?? "the counterparty")
                      .join(" and ")}`}
              </span>
              <span className="right">
                {/* Who signed and when is on this card; WHAT HAPPENED — sent,
                    opened, reminded, withdrawn — is the round's own trail, and
                    this is the only way into it now that Signatures is not a
                    sidebar tab. Filtered to this contract. */}
                <Link className="btn sm" to={`/contracts/signatures?contract=${id}`}>
                  <History size={12} /> Signature history
                </Link>
                <Link className="btn sm" to={`/contracts/${id}/signature`}>
                  <ArrowRight size={12} /> Signature page
                </Link>
              </span>
            </div>
            <div style={{ padding: "14px 20px" }}>
              <div className="grid g-2">
                {(["carrier", "counterparty"] as const).map(sd => {
                  const done = rec.signatures.filter(g => g.side === sd);
                  return (
                    <div key={sd}>
                      <div className="sub" style={{ marginBottom: 6 }}>
                        {sd === "carrier"
                          ? "The carrier"
                          : rec.counterparty?.name ?? "Counterparty"}
                      </div>
                      {done.length === 0 ? (
                        <span className="badge b-mut">
                          <span className="d" /> Not signed
                        </span>
                      ) : done.map(g => (
                        <div key={g.id} style={{ marginBottom: 6 }}>
                          <b style={{ fontSize: 13 }}>{g.signer_name}</b>
                          <div className="sub">
                            {[g.signer_title,
                              g.signed_at && fmtDate(g.signed_at),
                              g.method === "typed"
                                ? "signed in Kavachio"
                                : "signed elsewhere, recorded here"]
                              .filter(Boolean).join(" · ")}
                          </div>
                        </div>
                      ))}
                    </div>
                  );
                })}
              </div>
              {rec.unsigned_sides.length > 0 && (
                <div className="hint" style={{ marginTop: 10 }}>
                  A contract goes in force when both sides have signed it — the
                  second signature normally does it on its own.
                </div>
              )}
            </div>
          </div>
        )}

        {/* ── whose move it is ──
            A negotiation that does not say whose turn it is becomes two people
            each assuming the other is looking at it. */}
        {rec.whose_turn && (
          <div className="note" style={{ marginBottom: 14 }}>
            {rec.whose_turn === "broker"
              ? <>Waiting on <b>{rec.counterparty?.name ?? "the broker"}</b>{" "}
                  {rec.lifecycle === "in_review"
                    ? "to agree these terms or ask for changes."
                    : "to answer."}</>
              : <>Waiting on <b>the carrier</b>{" "}
                  {rec.lifecycle === "changes_requested"
                    ? "to answer the changes asked for below."
                    : "to decide."}</>}
          </div>
        )}

        {/* The gate. Named early because while it stands, most of the page's
            actions are refused and the reason is not otherwise visible. */}
        {rec.missing_references.length > 0 && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            <b>
              <AlertTriangle size={13} style={{ verticalAlign: "-2px" }} />{" "}
              This contract defers to {rec.missing_references.length} document
              {rec.missing_references.length === 1 ? "" : "s"} nobody has supplied.
            </b>{" "}
            {rec.missing_references.join(", ")} — the clauses pointing at
            {rec.missing_references.length === 1 ? " it" : " them"} cannot become
            rules until {rec.missing_references.length === 1 ? "it is" : "they are"}{" "}
            attached below. Until then the contract cannot be submitted, put in
            force, or re-read.
          </div>
        )}

        {rulesStale && (
          <div className="note" style={{ marginBottom: 14 }}>
            <b>The rules on file are out of date.</b> A document was attached
            after they were generated. Re-read the contract to rebuild them from
            everything that is now active.{" "}
            {a.generate_rules && (
              <button
                className="btn pri sm" type="button" disabled={!!busy}
                onClick={doGenerate} style={{ marginLeft: 6 }}
              >
                {busy === "rules" ? "Reading…" : "Re-read now"}
              </button>
            )}
          </div>
        )}

        {/* ── what the broker asked to change ── */}
        {rec.open_change_request && (
          <div className="card" style={{ marginBottom: 18 }}>
            <div className="card-h">
              <MessagesSquare size={16} className="ci" />
              <h3>Changes requested</h3>
              <span className="sub">
                asked {fmtStamp(rec.open_change_request.acted_at)}
              </span>
            </div>
            {/* A request can now be made entirely of named terms, with no
                prose at all — so an empty paragraph here is a real case and
                not a missing value. Say which it is rather than rendering a
                blank box that reads as a bug. */}
            <div style={{ padding: "16px 20px" }}>
              {rec.open_change_request.note?.trim() ? (
                <p style={{ margin: 0, fontSize: 13 }}>
                  {rec.open_change_request.note}
                </p>
              ) : (
                <p className="muted" style={{ margin: 0, fontSize: 13 }}>
                  {rec.open_change_request.proposed_changes.length > 0
                    ? "They named the terms rather than writing it out — what "
                      + "they want is below."
                    : "No reason was given."}
                </p>
              )}
            </div>
            {rec.open_change_request.proposed_changes.length > 0 && (
              <>
                <div className="tbl-wrap">
                  <table>
                    <thead>
                      <tr><th>Term</th><th>Now</th><th>Wanted</th><th>Why</th></tr>
                    </thead>
                    <tbody>
                      {rec.open_change_request.proposed_changes.map((ch, i) => {
                        // THREE states here, not two. A value that moved
                        // because the carrier applied this very request has
                        // been ANSWERED — flagging it as "changed since" reads
                        // as a warning about the one outcome that is entirely
                        // correct, and it is the state the row is in for the
                        // whole of the rest of the negotiation. A value that
                        // moved some OTHER way is the one worth flagging: the
                        // carrier may have already answered this, or answered
                        // something else, and either way the request is now
                        // about a value that no longer exists.
                        const live = currentValue(ch.field);
                        const applied = !!ch.proposed && live === ch.proposed;
                        const stale = !applied && (ch.current ?? "") !== live;
                        return (
                          <tr key={i}>
                            <td><b>{labelOf(ch.field)}</b></td>
                            <td className="mono">
                              {live || "—"}
                              {/* A div, not a span: `.sub` in a td is styled
                                  as a second line under the value, and inline
                                  it rendered glued to the end of it. */}
                              {applied && (
                                <div className="sub"
                                     style={{ color: "var(--p-ok-ink)" }}>
                                  applied — this is what they asked for
                                </div>
                              )}
                              {stale && (
                                <div
                                  className="sub"
                                  style={{ color: "var(--p-warn-ink)" }}
                                  title={`They asked when it said "${ch.current || "—"}"`}
                                >
                                  changed since they asked
                                </div>
                              )}
                            </td>
                            <td className="mono">{ch.proposed || "—"}</td>
                            <td className="muted">{ch.comment || "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {a.edit && (
                  <div style={{ padding: "14px 20px", borderTop: "1px solid var(--p-border)" }}>
                    <div className="rowacts" style={{ marginTop: 0 }}>
                      <button
                        className="btn pri" type="button"
                        disabled={!!busy || changeRequestApplied()}
                        onClick={() => run("apply", async () => {
                          // Apply exactly what was asked for, then leave it — the
                          // carrier still has to look and re-send. Applying and
                          // sending in one click would put terms out that nobody
                          // read.
                          //
                          // The two vocabularies go to different places. A
                          // contract field is a column; an agreed limit lives
                          // in `agreed_limits` under its own key, and writing
                          // one as a top-level field would be sending a term
                          // the API has never heard of.
                          const body: Record<string, unknown> = {};
                          const limits: AgreedLimits = { ...(rec.agreed_limits ?? {}) };
                          let movedALimit = false;
                          for (const ch of rec.open_change_request!.proposed_changes) {
                            if (ch.proposed == null || ch.proposed === "") continue;
                            if (isLimit(ch.field)) {
                              // Spread what is there so a limit's severity —
                              // whether a breach stops a file or flags it —
                              // survives a change to its value. That was
                              // agreed separately and is not what the broker
                              // asked about.
                              limits[ch.field] = {
                                ...(limits[ch.field] ?? {}), value: ch.proposed };
                              movedALimit = true;
                              continue;
                            }
                            const f = editable.find(x => x.name === ch.field);
                            body[ch.field] =
                              f?.kind === "int" ? Number.parseInt(ch.proposed, 10)
                              : f?.kind === "decimal" ? Number.parseFloat(ch.proposed)
                              : ch.proposed;
                          }
                          if (movedALimit) body.agreed_limits = limits;
                          if (Object.keys(body).length === 0) return;
                          await updateContract(id, body as never);
                          setNote("Applied what the broker asked for. Look it over, "
                                + "then send the revised terms back."
                                + (movedALimit
                                   ? " A limit moved, so the wording quoting it "
                                     + "and the check behind it moved with it — "
                                     + "and any signature already on this "
                                     + "contract has been withdrawn, because it "
                                     + "was given on the terms as they were."
                                   : ""));
                        })}
                      >
                        {busy === "apply" ? "Applying…" : "Apply these values"}
                      </button>
                      <span className="sub">
                        {changeRequestApplied()
                          ? "Already applied — the terms now say what they "
                            + "asked for. Send the revised terms back."
                          : "Fills the terms in. You still review and re-send."}
                      </span>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* ── state + what can be done ── */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <span className={`badge ${st.cls}`}><span className="d" />{st.label}</span>
            <span className="sub">
              {rec.lifecycle === "draft" ? draftNote : st.note}
            </span>
          </div>
          <div style={{ padding: "16px 20px" }}>
            {rec.submitted_at && (
              <div className="hint" style={{ marginTop: 0 }}>
                Raised {fmtStamp(rec.submitted_at)}
              </div>
            )}
            {rec.lifecycle === "terminated" && rec.termination_reason && (
              <div className="hint" style={{ color: "var(--p-crit-ink)" }}>
                Terminated {fmtDate(rec.terminated_date)} — {rec.termination_reason}
              </div>
            )}
            {rec.lifecycle === "agreed" && (
              <div className="hint">
                Terms are settled. The broker signs and returns it, then the
                carrier puts it in force.{" "}
                <Link to={`/contracts/${rec.id}/signature`} className="linkish">
                  Go to signature →
                </Link>
              </div>
            )}
            {rec.lifecycle === "signed" && (
              <div className="hint">
                Signed and returned{rec.executed_date
                  ? ` on ${fmtDate(rec.executed_date)}` : ""}. The carrier places
                it and puts it in force — <b>placement is not built in Kavachio
                yet</b>, so for now it goes straight to in force.
              </div>
            )}
            {rec.renews_contract_id && (
              <div className="hint">
                Renews{" "}
                <Link to={`/contracts/${rec.renews_contract_id}`} className="linkish">
                  contract {rec.renews_contract_id}
                </Link>
              </div>
            )}

            <div className="rowacts" style={{ marginTop: 14 }}>
              {a.send_for_review && (
                <button
                  className="btn pri" type="button" disabled={!!busy}
                  onClick={() => run("review", () => sendForReview(id, reviewNote || undefined))}
                >
                  <MessagesSquare size={13} />
                  {rec.lifecycle === "changes_requested"
                    ? "Send revised terms" : "Send to broker for review"}
                </button>
              )}
              {/* Second-tier on purpose. Sending it out is the ordinary road
                  and stays the primary button; this is the exception, and it
                  should look like one. */}
              {a.skip_review && (
                <button className="btn" type="button" disabled={!!busy}
                        onClick={() => setShowSkip(v => !v)}>
                  <PenLine size={13} /> Skip the review — sign it now
                </button>
              )}
              {a.accept_terms && (
                <button
                  className="btn pri" type="button" disabled={!!busy}
                  onClick={() => run("accept", () => acceptTerms(id))}
                >
                  <CheckCircle2 size={13} /> Agree these terms
                </button>
              )}
              {a.submit_signed && !round?.can_sign && (
                <button
                  className="btn pri" type="button" disabled={!!busy}
                  onClick={() => setShowSign(v => !v)}
                >
                  <PenLine size={13} /> Sign and return
                </button>
              )}
              {a.request_changes && (
                <button className="btn" type="button"
                        onClick={() => setShowRequest(v => !v)}>
                  <MessagesSquare size={13} /> Request changes
                </button>
              )}
              {/* Signing is the road to being live, so it is offered where the
                  other lifecycle actions are and not only on its own screen.

                  When a round can be opened this goes STRAIGHT to the document
                  in a new tab — signing deserves seeing what you are signing,
                  and the document is the thing being signed, so a stop on an
                  intermediate screen adds a click and shows less. Everything
                  else still goes to the signature screen, which is where
                  signatories are named and a paper signature is recorded. */}
              {round?.can_sign ? (
                <a className="btn pri" href={inAppSigningUrl(id)}
                   target="_blank" rel="noreferrer">
                  <PenLine size={13} /> Sign the contract
                  <ExternalLink size={12} style={{ marginLeft: 6 }} />
                </a>
              ) : (a.sign || a.record_signature) && !a.submit_signed && (
                <Link className="btn pri" to={`/contracts/${id}/signature`}>
                  <PenLine size={13} />{" "}
                  {a.sign ? "Sign the contract" : "Record their signature"}
                </Link>
              )}
              {a.activate && (
                <button
                  className="btn pri" type="button" disabled={!!busy}
                  onClick={() => run("activate", () => activateContract(id))}
                >
                  <CheckCircle2 size={13} /> Put in force
                </button>
              )}
              {a.renew && (
                <button className="btn" type="button"
                        onClick={() => setShowRenew(v => !v)}>
                  <ArrowRight size={13} /> Renew
                </button>
              )}
              {a.terminate && (
                <button className="btn danger" type="button"
                        onClick={() => setShowTerminate(v => !v)}>
                  <XCircle size={13} /> Terminate
                </button>
              )}
              {/* No "Clauses & rules", "Re-read rules" or "Bind checks"
                  here. This page is the CONTRACT — its terms, its wording, who
                  signed it. What those terms became downstream is a different
                  question, asked from the programme, and three buttons about
                  it crowded the row of actions that are actually about the
                  contract in front of you. */}
              {/* Screen only — no signing provider is connected yet, which the
                  screen itself says before anything else on it. */}
              <Link className="btn" to={`/contracts/${rec.id}/signature`}>
                <PenLine size={13} /> Signature
              </Link>
            </div>

            {/* ── carrier: settle the terms alone ── */}
            {showSkip && (
              <div className="note warn" style={{ marginTop: 14 }}>
                <b>Agree these terms without sending them out</b>
                <p style={{ margin: "6px 0 0" }}>
                  The contract goes straight to signing. The
                  {" "}{rec.contract_type === "insurer_reinsurer"
                        ? "reinsurer" : "broker"}{" "}
                  is not asked to read the terms first and gets no chance to
                  push back on them — so this is for a contract with nothing
                  left to agree, or one whose counterparty has no seat in
                  Kavachio to read it in.
                </p>
                <p style={{ margin: "6px 0 0" }}>
                  It is written into the contract's history as
                  {" "}<b>review skipped</b>, under your name, so a reader later
                  can tell it apart from terms the other side agreed to.
                </p>
                <div className="field" style={{ marginTop: 12, marginBottom: 0 }}>
                  <textarea rows={2} value={skipNote}
                            onChange={e => setSkipNote(e.target.value)}
                            placeholder="Why is no review needed? (optional)" />
                </div>
                <div className="rowacts">
                  <button
                    className="btn pri" type="button" disabled={!!busy}
                    onClick={() => run("skip", async () => {
                      await skipReview(id, skipNote.trim() || undefined);
                      setShowSkip(false);
                      setSkipNote("");
                      setNote("Terms settled. It is ready to sign.");
                    })}
                  >
                    {busy === "skip" ? "Settling…" : "Yes, go straight to signing"}
                  </button>
                  <button className="btn" type="button"
                          onClick={() => setShowSkip(false)}>
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {/* ── broker: sign and hand it back ── */}
            {showSign && (
              <div className="note" style={{ marginTop: 14 }}>
                <b>Sign and return to the carrier</b>
                <p style={{ margin: "6px 0 0" }}>
                  Kavachio does not witness the signing — it records that it
                  happened. Say which attachment is the signed copy and when it
                  was executed, and the contract goes back to the carrier.
                </p>
                <div className="grid g-3" style={{ marginTop: 12 }}>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>The signed copy</label>
                    <select value={signedDocId}
                            onChange={e => setSignedDocId(e.target.value)}>
                      <option value="">The current wording</option>
                      {active.filter(d => d.kind === "contract").map(d => (
                        <option key={d.id} value={String(d.id)}>{d.filename}</option>
                      ))}
                    </select>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Executed on</label>
                    <input type="date" value={executedDate}
                           onChange={e => setExecutedDate(e.target.value)} />
                  </div>
                </div>
                <div className="rowacts">
                  <button
                    className="btn pri" type="button" disabled={!!busy}
                    onClick={() => run("sign", async () => {
                      await submitSigned(id, {
                        document_id: signedDocId ? Number(signedDocId) : null,
                        executed_date: executedDate || null,
                      });
                      setShowSign(false);
                      setNote("Signed and returned to the carrier.");
                    })}
                  >
                    {busy === "sign" ? "Returning…" : "Sign and return"}
                  </button>
                  <button className="btn" type="button"
                          onClick={() => setShowSign(false)}>Cancel</button>
                </div>
                {!rec.has_wording && (
                  <div className="hint" style={{ color: "var(--p-warn-ink)" }}>
                    There is no wording attached, so there is nothing signed to
                    return. Attach the executed copy below first.
                  </div>
                )}
              </div>
            )}

            {/* ── broker: push back on the terms ── */}
            {showRequest && (
              <div className="note" style={{ marginTop: 14 }}>
                <b>Ask for changes</b>
                <p style={{ margin: "6px 0 0" }}>
                  Say what needs to change — in words, by naming the terms, or
                  both. Naming a term is the clearer of the two: it lets the
                  carrier see what you want beside what the contract currently
                  says, and apply it in one move instead of working it out from
                  prose and retyping.
                </p>
                <div className="field" style={{ marginTop: 12, marginBottom: 0 }}>
                  <textarea
                    rows={3} value={reqNote}
                    onChange={e => setReqNote(e.target.value)}
                    placeholder="What needs to change, and why?"
                  />
                </div>

                {reqChanges.map((ch, i) => (
                  <div className="grid g-3" key={i} style={{ marginTop: 12 }}>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Term</label>
                      <select
                        value={ch.field}
                        onChange={e => setReqChanges(list => list.map((x, j) =>
                          j === i ? { ...x, field: e.target.value,
                                      current: currentValue(e.target.value) } : x))}
                      >
                        {/* Starts on NOTHING. It used to start on the first
                            field in the list — the contract's name — so a
                            broker who typed what they wanted and never opened
                            the dropdown proposed renaming the contract to a
                            sentence. A picker with a default is a picker that
                            answers for you. */}
                        <option value="">— choose a term —</option>
                        <optgroup label="The terms you agreed">
                          {termChoices.filter(f => f.limit).map(f => (
                            <option key={f.name} value={f.name}>{f.label}</option>
                          ))}
                        </optgroup>
                        <optgroup label="The contract itself">
                          {termChoices.filter(f => !f.limit).map(f => (
                            <option key={f.name} value={f.name}>{f.label}</option>
                          ))}
                        </optgroup>
                      </select>
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Now</label>
                      <input className="ro" readOnly value={ch.current ?? "—"} />
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Wanted</label>
                      <input
                        value={ch.proposed ?? ""}
                        onChange={e => setReqChanges(list => list.map((x, j) =>
                          j === i ? { ...x, proposed: e.target.value } : x))}
                      />
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Why</label>
                      <input
                        value={ch.comment ?? ""}
                        onChange={e => setReqChanges(list => list.map((x, j) =>
                          j === i ? { ...x, comment: e.target.value } : x))}
                      />
                    </div>
                    <div style={{ display: "flex", alignItems: "end" }}>
                      <button
                        className="btn sm" type="button"
                        aria-label="Remove this term"
                        onClick={() => setReqChanges(list => list.filter((_, j) => j !== i))}
                      >
                        <XCircle size={13} /> Remove
                      </button>
                    </div>
                  </div>
                ))}

                <div className="rowacts">
                  <button
                    className="btn sm" type="button"
                    disabled={termChoices.length === 0}
                    onClick={() => setReqChanges(list => [...list, {
                      field: "", current: "", proposed: "", comment: "",
                    }])}
                  >
                    <Plus size={12} /> Name a term
                  </button>
                  <button
                    className="btn pri" type="button"
                    disabled={!requestSaysSomething || !!busy}
                    onClick={() => run("request", async () => {
                      await requestChanges(id, reqNote.trim(),
                        reqChanges.filter(ch => ch.field));
                      setShowRequest(false);
                      setReqNote("");
                      setReqChanges([]);
                    })}
                  >
                    Send back to the carrier
                  </button>
                  <button className="btn" type="button"
                          onClick={() => setShowRequest(false)}>Cancel</button>
                  {/* A disabled button that does not say why is indis-
                      tinguishable from a broken one. */}
                  {!requestSaysSomething && (
                    <span className="sub">
                      Nothing to send yet — write what needs to change, or name
                      a term and what you want it to say.
                    </span>
                  )}
                </div>
              </div>
            )}

            {showTerminate && (
              <div className="note warn" style={{ marginTop: 14 }}>
                <b>End this contract early</b>
                <p style={{ margin: "6px 0 0" }}>
                  It stays on the record with the reason attached — everything
                  already produced against it keeps its meaning. This cannot be
                  undone; a contract that ends is renewed into a successor rather
                  than restarted.
                  {rec.notice_period_days != null && (
                    <> This contract asks for {rec.notice_period_days} days' notice.</>
                  )}
                </p>
                <div className="field" style={{ marginTop: 12, marginBottom: 0 }}>
                  <textarea rows={2} value={reason}
                            onChange={e => setReason(e.target.value)}
                            placeholder="Why is it ending?" />
                </div>
                <div className="rowacts">
                  <button
                    className="btn danger" type="button"
                    disabled={!reason.trim() || !!busy}
                    onClick={() => run("terminate", async () => {
                      const r = await terminateContract(id, reason.trim());
                      setShowTerminate(false);
                      setReason("");
                      if (r.warning) setErr(r.warning);
                    })}
                  >
                    Terminate
                  </button>
                  <button className="btn" type="button"
                          onClick={() => setShowTerminate(false)}>Cancel</button>
                </div>
              </div>
            )}

            {showRenew && (
              <div className="note" style={{ marginTop: 14 }}>
                <b>Renew into a successor</b>
                <p style={{ margin: "6px 0 0" }}>
                  This creates a NEW contract for the next term, pointing back at
                  this one. This contract's own term is left exactly as it is —
                  everything checked against it has to keep meaning what it
                  meant. The successor starts with no documents: last year's
                  wording is not this year's.
                </p>
                <div className="grid g-3" style={{ marginTop: 12 }}>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Inception</label>
                    <input type="date" value={renewFrom}
                           onChange={e => renewTerm.onInception(e.target.value)} />
                  </div>
                  {/* A renewal is nearly always the same length as the term it
                      succeeds, so this is where saying "12 months" saves the
                      most typing. */}
                  {renewTerm.ready && <TermDurationField term={renewTerm} />}
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Expiry</label>
                    <input type="date" value={renewTo}
                           disabled={renewTerm.ready && !renewTerm.custom}
                           onChange={e => renewTerm.onExpiry(e.target.value)} />
                    {renewTerm.ready && !renewTerm.custom && (
                      <div className="hint">
                        Set by the duration — choose Custom to type a date.
                      </div>
                    )}
                  </div>
                </div>
                <div className="rowacts">
                  <button
                    className="btn pri" type="button"
                    disabled={!renewFrom || !renewTo || !!busy}
                    onClick={() => run("renew", async () => {
                      const created = await renewContract(id, {
                        inception_dt: renewFrom, expiry_dt: renewTo });
                      nav(`/contracts/${created.id}`);
                    })}
                  >
                    Create successor
                  </button>
                  <button className="btn" type="button"
                          onClick={() => setShowRenew(false)}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ── two columns from here down ──
            LEFT is the contract itself: what it is, what was agreed, what it
            says, and the words it says it in. RIGHT is the paper trail about
            it — the files attached and how it got here. They were stacked, so
            reaching the history meant scrolling past the whole wording, and
            the wording is the longest thing on the page. */}
        <div className="rec-split">
          <div className="rec-main">

        {/* ── the record ── */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <h3>The contract</h3>
            {a.edit && spec && !editing && (
              <span className="right">
                <button className="btn sm" type="button" onClick={beginEdit}>
                  Edit terms
                </button>
              </span>
            )}
          </div>
          <div style={{ padding: "16px 20px" }}>
            {editing && spec ? (
              <>
                <div className="grid g-3">
                  {editable.flatMap(f => f.name === EXPIRY_FIELD && editTerm.ready
                    ? [<TermDurationField key="term-duration" term={editTerm} />,
                       editInput(f)]
                    : [editInput(f)])}
                </div>

                {/* The agreed limits, in the same three groups and the same
                    words the create flow asked for them in. Editing a draft's
                    name but not its commission would be a strange half-draft —
                    and a change request is nearly always about a limit, so this
                    is also how the carrier answers one. */}
                {limitGroups.map(g => {
                  const rows = limitSpec.filter(
                    l => l.group === g.key
                      && (showAllLimits || draftLimits[l.name] !== undefined));
                  if (!rows.length) return null;
                  return (
                    // Panelled, exactly as the create flow asks for them. The
                    // group name used to be a loose caption between two runs of
                    // rows, which made the last row of one group read as the
                    // first row of the next — and these two screens edit the
                    // same terms, so they must not disagree about where a term
                    // belongs.
                    <div className="limgrp" key={g.key}>
                      <div className="limgrp-h">
                        <div className="sub-h">{g.label}</div>
                        <div className="hint">{g.sub}</div>
                      </div>
                      <div className="limgrp-b">
                      <div className="lim lim-h">
                        <div className="lq"><b>What you agreed</b></div>
                        <div className="sub">Contract Limit</div>
                        <div className="sub">Severity Classification</div>
                      </div>
                      {rows.map(l => {
                        const e = draftLimits[l.name];
                        const sev = e?.severity ?? l.default_severity;
                        return (
                          <div className="lim" key={l.name}>
                            <div className="lq">
                              <b>{l.question}</b><span>{l.sub}</span>
                            </div>
                            <div className={l.kind === "choice" ? "" : "unit"}>
                              {l.choices ? (
                                <select
                                  value={String(e?.value ?? "")}
                                  onChange={ev => setDraftLimit(
                                    l.name, { value: ev.target.value })}
                                >
                                  <option value="">Not agreed</option>
                                  {l.choices.map(ch => (
                                    <option key={ch} value={ch}>{ch}</option>
                                  ))}
                                </select>
                              ) : (
                                <>
                                  <input
                                    value={String(e?.value ?? "")}
                                    placeholder="—"
                                    onChange={ev => setDraftLimit(
                                      l.name, { value: ev.target.value })}
                                  />
                                  {(l.unit || l.kind === "money") && (
                                    <em>{l.unit
                                         || String(draftLimits.currency?.value ?? "")
                                              .toUpperCase() || "—"}</em>
                                  )}
                                </>
                              )}
                            </div>
                            <div>
                              {!e ? (
                                <span className="sub">—</span>
                              ) : l.checkable ? (
                                <>
                                  <div className="segpick">
                                    {sevSpec.map(sv => (
                                      <button
                                        key={sv.key} type="button"
                                        title={sv.hint}
                                        className={sev === sv.key ? "on" : ""}
                                        onClick={() => setDraftLimit(
                                          l.name, { severity: sv.key })}
                                      >
                                        {sv.label}
                                      </button>
                                    ))}
                                  </div>
                                  <div className="sub" style={{ marginTop: 4 }}>
                                    {sevSpec.find(x => x.key === sev)?.action ?? ""}
                                  </div>
                                </>
                              ) : (
                                <span className="sub">
                                  Goes in the wording. Nothing in a file to
                                  check it against.
                                </span>
                              )}
                            </div>
                          </div>
                        );
                      })}
                      </div>
                    </div>
                  );
                })}

                {/* The signature block, offered from the server's own list so
                    this screen and step 4 cannot drift apart about what a block
                    may contain. */}
                {sigSpec && draftSig && (
                  <div className="limgrp">
                    <div className="limgrp-h">
                      <div className="sub-h">What each side signs</div>
                      <div className="hint">
                        the lines that appear under their signature on the
                        contract's signature page
                      </div>
                    </div>
                    <div className="limgrp-b" style={{ paddingTop: 12 }}>
                      <div className="grid g-2">
                        {sigSpec.sides.map(side => (
                          <div key={side}>
                            <div className="sub-h" style={{ marginTop: 0 }}>
                              {side === "carrier"
                                ? "You"
                                : rec.counterparty?.name ?? "The counterparty"}
                            </div>
                            {sigSpec.fields.map(f => {
                              const on = (draftSig.fields[side] ?? []).includes(f.key);
                              return (
                                <label key={f.key} className="kv"
                                       style={{ alignItems: "flex-start",
                                                cursor: f.fixed ? "default" : "pointer" }}>
                                  <span className="k">
                                    <input type="checkbox" checked={on}
                                           disabled={f.fixed}
                                           style={{ marginRight: 9 }}
                                           onChange={() => setDraftSig(l => l && ({
                                             ...l,
                                             fields: {
                                               ...l.fields,
                                               [side]: on
                                                 ? (l.fields[side] ?? []).filter(k => k !== f.key)
                                                 : [...(l.fields[side] ?? []), f.key],
                                             },
                                           }))} />
                                    <b style={{ color: "var(--p-ink)" }}>{f.label}</b>
                                    <div className="sub" style={{ marginLeft: 24 }}>
                                      {f.hint}{f.fixed && " · always on"}
                                    </div>
                                  </span>
                                </label>
                              );
                            })}
                          </div>
                        ))}
                      </div>
                      <div className="field"
                           style={{ marginTop: 12, marginBottom: 0, maxWidth: 320 }}>
                        <label>How the two blocks sit on the page</label>
                        <select value={draftSig.arrangement}
                                onChange={e => setDraftSig(
                                  l => l && { ...l, arrangement: e.target.value })}>
                          {sigSpec.arrangements.map(a => (
                            <option key={a.key} value={a.key}>{a.label}</option>
                          ))}
                        </select>
                        <div className="hint">
                          {sigSpec.arrangements
                            .find(a => a.key === draftSig.arrangement)?.hint}
                        </div>
                      </div>

                      {/* Placed by hand: the blocks are dragged onto the pages
                          of this contract, and the document is drawn where they
                          were left. Only offered here and not in the create
                          wizard, because there is no document to drag onto
                          until the contract exists. */}
                      {draftSig.arrangement === "placed" && (
                        <div style={{ marginTop: 14 }}>
                          <SignaturePlacer
                            source={{ kind: "contract", id }}
                            layout={draftSig}
                            block={sigSpec.placed_block}
                            targets={signerTargets(
                              sigSpec.sides, rec.signers,
                              side => (side === "carrier"
                                ? "You"
                                : rec.counterparty?.name
                                  ?? "The counterparty"))}
                            onPlace={(side, spot) => setDraftSig(l => l && ({
                              ...l,
                              blocks: { ...(l.blocks ?? {}), [side]: spot },
                            }))}
                            onRemove={side => setDraftSig(l => {
                              if (!l) return l;
                              const rest = { ...(l.blocks ?? {}) };
                              delete rest[side];
                              return { ...l, blocks: rest };
                            })}
                            onAnchor={(side, after) => setDraftSig(l => l && ({
                              ...l, blocks: { ...(l.blocks ?? {}), [side]: { after } },
                            }))}
                          />
                          <div className="hint" style={{ marginTop: 8 }}>
                            The pages are this contract as it stands. Everybody
                            named to sign gets a block of their own here, so
                            three people can sign in three different places;
                            anybody left unplaced signs under their side.
                            Names are added on the signature page. Save the
                            terms to keep where you put the blocks — and if the
                            wording later grows or shrinks, a block left past
                            the end is drawn on the last page rather than lost.
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                <div className="rowacts">
                  <button className="btn sm" type="button"
                          onClick={() => setShowAllLimits(v => !v)}>
                    {showAllLimits
                      ? "Show only what is set"
                      : `＋ Change or add a limit (${limitSpec.length})`}
                  </button>
                  <span className="sub">
                    Changing a limit changes the wording that quotes it and the
                    check behind it — they move together.
                  </span>
                </div>

                <div className="rowacts" style={{ marginTop: 16 }}>
                  <button className="btn pri" type="button" disabled={!!busy}
                          onClick={saveEdit}>
                    {busy === "edit" ? "Saving…" : "Save terms"}
                  </button>
                  <button
                    className="btn" type="button"
                    onClick={() => { setEditing(false); setFieldErrs({}); }}
                  >
                    Cancel
                  </button>
                  <span className="sub">
                    A field this type requires can be corrected, but not cleared.
                  </span>
                </div>
              </>
            ) : (
              <>
                <div className="grid g-3">
                  <Row label="Type">{rec.contract_type_label}</Row>
                  <Row label={rec.counterparty?.party_type === "reinsurer"
                              ? "Reinsurer" : "Broker"}>
                    {rec.counterparty?.name}
                  </Row>
                  <Row label="Programme">{rec.programme?.name}</Row>
                  <Row label="Term">
                    {rec.inception_dt && rec.expiry_dt
                      ? `${fmtDate(rec.inception_dt)} → ${fmtDate(rec.expiry_dt)}`
                      : "—"}
                  </Row>
                  <Row label="UMR">{rec.umr}</Row>
                  <Row label="Class of business">{rec.class_of_business}</Row>
                  <Row label="Year of account">{rec.year_of_account}</Row>
                  <Row label="Premium cap">
                    {rec.premium_cap_amount != null
                      ? `${rec.premium_cap_currency ?? ""} ${rec.premium_cap_amount.toLocaleString()}`.trim()
                      : "—"}
                  </Row>
                  <Row label="Notice period">
                    {rec.notice_period_days != null
                      ? `${rec.notice_period_days} days` : "—"}
                  </Row>
                  <Row label="Executed">
                    {rec.executed_date ? fmtDate(rec.executed_date) : "—"}
                  </Row>
                  <Row label="Schedule key">{rec.schedule_key}</Row>
                  <Row label="Output template">
                    {rec.output_template
                      ? `${rec.output_template.name} v${rec.output_template.version}`
                      : "none — no rules can be written without one"}
                  </Row>
                  {/* What is MEASURED on a file, beside the template that
                      measures it. Two facts that only mean anything together:
                      a contract with a template and no checks is one nobody has
                      bound, and it looks identical to a finished one until this
                      says otherwise. */}
                  <Row label="Checks in force">
                    <ChecksInForce checks={rec.checks} />
                  </Row>
                  {/* WHICH SHEET, AND WHY IT IS THAT ONE. A term says
                      "commission is 17%" and never says which tab of which
                      workbook that is measured on — the sheet comes from the
                      programme's output template, which binding resolves on its
                      own. That is right (two answers to "which template" is how
                      a file gets checked against one nothing reports into) and
                      it was also invisible: a carrier who writes nothing in the
                      United States could be measured on a Lloyd's US layout and
                      never be told. Shown, so the wrong template is noticed
                      here rather than in an exception report. */}
                  {rec.checks.sheets.length > 0 && (
                    <Row label="Measured on">
                      {rec.checks.sheets.join(", ")}
                      <div className="hint" style={{ marginTop: 2 }}>
                        From the programme's output template, not from this
                        contract. Change it on the programme and re-bind.
                      </div>
                    </Row>
                  )}
                  <Row label="Raised">{fmtStamp(rec.created_at)}</Row>
                </div>
                {a.edit && (
                  <>
                    <div className="divider" />
                    <div className="hint" style={{ marginTop: 0 }}>
                      This contract is still {rec.lifecycle}, so its terms can be
                      corrected. Once it is in force they cannot — a contract
                      that is live is changed by an endorsement, so what it said
                      when a bordereau was checked against it stays on the record.
                    </div>
                  </>
                )}
              </>
            )}
          </div>
        </div>

        {/* ── what was agreed ──
            The same 26 terms the create flow asked for, in the same three
            groups and the same words, each showing what happens when a file
            breaks it. Without this the record showed a contract's identity and
            none of its substance. */}
        {/* Collapsed by default, same reasoning as Clauses and Rules below. */}
        {rec.agreed_limits && Object.keys(rec.agreed_limits).length > 0 && (
          <div className="card" style={{ marginBottom: 18 }}>
            <button type="button" className="card-h" style={{ width: "100%",
              textAlign: "left", cursor: "pointer", border: "none",
              background: "none" }}
              onClick={() => setAgreedOpen(o => !o)}>
              {agreedOpen
                ? <ChevronDown size={16} className="ci" />
                : <ChevronRight size={16} className="ci" />}
              <h3>What was agreed</h3>
              <span className="sub">
                each one is a clause in the wording and a check on every row
              </span>
            </button>
            {agreedOpen && (
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr><th>Term</th><th>Agreed</th><th>Severity Classification</th></tr>
                  </thead>
                  <tbody>
                    {limitGroups.map(g => {
                      const rows = limitSpec.filter(
                        l => l.group === g.key && rec.agreed_limits?.[l.name]);
                      if (!rows.length) return null;
                      return (
                        <>
                          <tr key={g.key}>
                            <td colSpan={3} style={{ textAlign: "left",
                                 background: "var(--p-surface-2)" }}>
                              <span className="sub-h">{g.label}</span>
                            </td>
                          </tr>
                          {rows.map(l => {
                            const e = rec.agreed_limits![l.name];
                            return (
                              <tr key={l.name}>
                                <td>
                                  <b>{l.question}</b>
                                  <div className="sub">{l.sub}</div>
                                </td>
                                <td className="mono">
                                  {String(e.value)}{l.unit ? ` ${l.unit}` : ""}
                                </td>
                                <td>
                                  {!l.checkable ? (
                                    <span className="sub">
                                      In the wording only
                                    </span>
                                  ) : (
                                    <span className={`badge ${
                                      e.severity === "critical" ? "b-crit" : "b-warn"}`}>
                                      <span className="d" />
                                      {sevLabel(e.severity)}
                                    </span>
                                  )}
                                </td>
                              </tr>
                            );
                          })}
                        </>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* ── what it says ──
            The clauses of this contract. For one written here they are its
            wording with the terms resolved — a clause row is READ, so it holds
            words rather than the tokens the wording keeps.

            Collapsed by default: the header already says how many clauses
            there are, which is the answer most visits are after, and the
            table can run long. */}
        {!!clauses?.length && (
          <div className="card" style={{ marginBottom: 18 }}>
            <button type="button" className="card-h" style={{ width: "100%",
              textAlign: "left", cursor: "pointer", border: "none",
              background: "none" }}
              onClick={() => setClausesOpen(o => !o)}>
              {clausesOpen
                ? <ChevronDown size={16} className="ci" />
                : <ChevronRight size={16} className="ci" />}
              <FileText size={16} className="ci" />
              <h3>Clauses</h3>
              <span className="sub">
                {clauses.length} clause{clauses.length === 1 ? "" : "s"}
              </span>
            </button>
            {clausesOpen && (
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr><th style={{ width: 220 }}>Clause</th><th>What it says</th></tr>
                  </thead>
                  <tbody>
                    {clauses.map(cl => (
                      <tr key={cl.clause_id}>
                        <td>
                          <b>{cl.title || cl.section_header || "Untitled"}</b>
                          {cl.page_number != null && (
                            <div className="sub">page {cl.page_number}</div>
                          )}
                        </td>
                        <td>
                          <ClauseText text={cl.text} className="muted" />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* ── and what is checked ──
            A SEPARATE list, not a column beside the clauses. Most rules do not
            come from a clause at all — the ones written from agreed terms have
            no clause to point at — so a per-clause column was blank for them
            and read as "nothing checks this", which was false. Shown only when
            rules exist: until BDX setup binds the terms to a template there are
            none, and an empty table says nothing worth the space. */}
        {/* Collapsed by default, same reasoning as Clauses above. */}
        {!!clauseRules.length && (
          <div className="card" style={{ marginBottom: 18 }}>
            <button type="button" className="card-h" style={{ width: "100%",
              textAlign: "left", cursor: "pointer", border: "none",
              background: "none" }}
              onClick={() => setRulesOpen(o => !o)}>
              {rulesOpen
                ? <ChevronDown size={16} className="ci" />
                : <ChevronRight size={16} className="ci" />}
              <ShieldCheck size={16} className="ci" />
              <h3>Rules</h3>
              <span className="sub">
                {clauseRules.length} check{clauseRules.length === 1 ? "" : "s"}
                {" "}run on every bordereau row
              </span>
            </button>
            {rulesOpen && (
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr><th>Rule</th><th>From</th><th>If a row breaks it</th></tr>
                  </thead>
                  <tbody>
                    {clauseRules.map(r => {
                      const from = clauses?.find(
                        cl => cl.clause_id === r.source_clause_id);
                      return (
                        <tr key={r.validation_rule_id}>
                          <td>
                            <b>{r.rule_name || `Rule ${r.validation_rule_id}`}</b>
                            {r.rule_description && (
                              <div className="sub">{r.rule_description}</div>
                            )}
                          </td>
                          <td>
                            {/* Only a rule written from a typed term is "the
                                agreed terms". A Setup rule quoting no clause is
                                a standard check — calling those the terms
                                credited 83 of them to terms on one contract. */}
                            {from
                              ? (from.title || from.section_header || "a clause")
                              : <span className="sub">
                                  {r.rule_spec?.source === "contract_terms"
                                    ? "the agreed terms"
                                    : r.source_clause_id ? "a clause" : "standard check"}
                                </span>}
                          </td>
                          <td>
                            <span className={`badge ${
                              r.severity === "critical" ? "b-crit" : "b-warn"}`}>
                              <span className="d" />
                              {r.severity === "critical" ? "Stopped" : "Flagged"}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {/* ── the wording it was written from ──
            Read as a document by default; editable in place while the contract
            is still a draft, because the wording IS the contract and a draft
            whose terms could be changed but whose sentences could not would be
            editable in name only. */}
        {(authored || wEditing || canWriteWording) && (
          <div className="card" style={{ marginBottom: 18 }}>
            <div className="card-h">
              <h3>The wording</h3>
              <span className="sub">
                {authored || wEditing
                  ? `${(wEditing ? wSections : rec.wording_sections ?? []).length} `
                    + "sections, written from the terms above"
                  : "not written yet"}
              </span>
              <span className="right">
                {a.edit && (
                  <>
                  {wEditing ? (
                    <>
                      <button
                        className="btn sm" type="button"
                        onClick={() => beginWording(true)}
                        title="Throw away these edits and write the wording
                               again from the terms as they stand now"
                      >
                        <RefreshCw size={12} /> Rewrite from the terms
                      </button>
                      <button className="btn sm" type="button"
                              onClick={() => setWEditing(false)}>
                        Cancel
                      </button>
                      <button
                        className="btn sm pri" type="button"
                        disabled={busy === "wording"}
                        onClick={saveWording}
                      >
                        {busy === "wording" ? "Saving…" : "Save the wording"}
                      </button>
                    </>
                  ) : (
                    <button
                      className={`btn sm${authored ? "" : " pri"}`} type="button"
                      onClick={() => beginWording(!authored)}
                    >
                      <PenLine size={12} />{" "}
                      {authored ? "Edit the wording" : "Write the wording"}
                    </button>
                  )}
                  </>
                )}
              </span>
            </div>

            {wEditing ? (
              <div style={{ padding: "20px" }}>
                {/* EDITED WHERE IT IS READ. The wording used to be edited in a
                    rail-and-panel layout — pick a clause on the left, type it
                    on the right — which is a good shape for a form and the
                    wrong one for a document: the thing on the screen stopped
                    looking like the contract at the moment somebody was
                    changing its words, and a clause could only be judged
                    against the one before it by clicking away from it.

                    So the document IS the editor. Same page, same type, same
                    numbering; every clause editable in place. What is NOT
                    editable in place is the headings — they are what a reader
                    navigates by, and a stray keystroke in one is silent damage
                    — so a heading is a label with Rename beside it and becomes
                    an input only when asked.

                    Deliberately no toolbar. There is nothing to format in a
                    contract clause: the only things that can be done to one are
                    write it, rename its heading, remove it, or add another, and
                    all four are on this page with no menu in front of them. */}
                <div className="contract-doc editing">
                  {wSections.map((sec, i) => (
                    <div key={sec.key} className="sec-e">
                      <h4>
                        <span className="n">{i + 1}.</span>
                        {wRenaming === sec.key ? (
                          <input
                            className="hd" autoFocus
                            value={sec.title}
                            placeholder="Name this clause"
                            aria-label="Clause heading"
                            onChange={e => {
                              const v = e.target.value;
                              setWSections(ss => ss.map(
                                (s2, j) => j === i ? { ...s2, title: v } : s2));
                            }}
                            onBlur={() => setWRenaming(null)}
                            onKeyDown={e => {
                              if (e.key === "Enter" || e.key === "Escape") {
                                e.preventDefault();
                                setWRenaming(null);
                              }
                            }}
                          />
                        ) : (
                          <>
                            <span className={sec.title ? "tx" : "tx faint"}>
                              {sec.title || "Untitled clause"}
                            </span>
                            <span className="acts">
                              <button type="button" className="linkish"
                                      onClick={() => setWRenaming(sec.key)}>
                                Rename
                              </button>
                              <button
                                type="button" className="linkish rm"
                                title="Remove this clause from the contract"
                                onClick={() => {
                                  setWSections(ss => ss.filter(s2 => s2.key !== sec.key));
                                  setWVersion(v => v + 1);
                                }}
                              >
                                Remove
                              </button>
                            </span>
                          </>
                        )}
                      </h4>
                      <WordingEditor
                        onChipsEdited={(b, a2) => chipsEdited(b, a2, i)}
                        onChipValue={chipValue}
                        chipEditable={tk => limitSpec.some(l => l.name === tk)}
                        sectionKey={`${wVersion}:${sec.key}`}
                        body={sec.body}
                        tokens={wTokens}
                        labels={Object.fromEntries(
                          limitSpec.map(l => [l.name, l.question]))}
                        onChange={body => setWSections(ss => ss.map(
                          (s2, j) => j === i
                            ? { ...s2, body,
                                origin: s2.origin.includes("edited")
                                  ? s2.origin : `${s2.origin} · edited` }
                            : s2))}
                      />
                    </div>
                  ))}

                  {!wSections.length && (
                    <div className="empty">
                      Every clause has been removed. Add one, or rewrite the
                      wording from the terms.
                    </div>
                  )}

                  {/* At the end, where a new clause goes. It opens with its
                      heading already asking to be named, because a clause with
                      no heading is one nobody can find again. */}
                  <button
                    className="btn sm add-sec" type="button"
                    onClick={() => {
                      const key = `own_${Date.now()}`;
                      // Empty, not "New clause". A heading you have to delete
                      // before you can type your own is a box that arrives
                      // already wrong; the words belong in the placeholder,
                      // where they describe the box rather than fill it.
                      setWSections(ss => [...ss, {
                        key, title: "", origin: "your own words", body: "",
                      }]);
                      setWVersion(v => v + 1);
                      setWRenaming(key);
                    }}
                  >
                    <Plus size={12} /> Add a clause
                  </button>
                </div>

                <div className="note" style={{ marginTop: 12 }}>
                  <b>The shaded values are tied to the terms above.</b> They are
                  not text — they follow the term they came from, so a clause and
                  the check behind it can never end up saying different things.
                  Type around them; deleting one removes the link with it.
                </div>
              </div>
            ) : (
              <div style={{ padding: "20px" }}>
                {!authored ? (
                  <div className="empty">
                    This contract is terms only — nothing has been written yet.
                    Kavachio can draft it from the terms above, and you can then
                    change any clause before anyone sees it.
                  </div>
                ) : (
                <>
                {/* A term that is checked on every row and stated nowhere in
                    the document either side signs. Worked out by the server —
                    it is a fact about the wording, not about this screen. */}
                {(rec.wording_unquoted ?? []).length > 0 && (
                  <div className="note warn" style={{ marginBottom: 12 }}>
                    <b>
                      The wording does not quote{" "}
                      {(rec.wording_unquoted ?? [])
                        .map(u => `“${u.question}”`).join(", ")}.
                    </b>{" "}
                    {(rec.wording_unquoted ?? []).length === 1
                      ? "That term is"
                      : "Those terms are"}{" "}
                    agreed and checked on every row, but no clause states
                    {(rec.wording_unquoted ?? []).length === 1 ? " it" : " them"}.
                    If a clause has the figure typed into it, it will go on
                    saying the old one — edit that clause and the value comes
                    back as a chip, or rewrite the wording from the terms.
                  </div>
                )}

                {/* Typeset as a document, not listed as fields. This IS the
                    contract, so it should read like one — and it is the same
                    text the PDF carries, set the same way, so the screen and
                    the download are recognisably one document. */}
                <div className="contract-doc">
                  {(rec.wording_sections ?? []).map((sec, i) => (
                    <div key={sec.key}>
                      <h4>{i + 1}. &nbsp;{sec.title}</h4>
                      {/* `rendered` comes from the server, which is the only
                          place that knows how a percentage, a money amount or a
                          date is written. This used to resolve tokens here and
                          knew about agreed limits only — so the clause naming
                          the parties read "carrier_name" and the term read
                          "from inception to expiry". */}
                      {(sec.rendered ?? sec.body).split("\n").map((line, j) => {
                        // The clause number hangs in the margin, as on paper.
                        const m = line.match(/^(\d+\.\d+)\s+(.*)$/s);
                        return (
                          <p key={j}>
                            {m
                              ? <><span className="cl">{m[1]}</span>{m[2]}</>
                              : line}
                          </p>
                        );
                      })}
                    </div>
                  ))}
                </div>
                <div className="note" style={{ marginTop: 12 }}>
                  <b>This is the contract.</b> It is not a file — it is held as
                  text tied to the terms above, so changing a term changes the
                  sentence quoting it and the check behind it together.{" "}
                  <b>Download contract</b> at the top of the page composes the
                  whole thing — schedule, these clauses and a signature page —
                  as one PDF to send, print or sign. That copy is a separate
                  document and does not change when a term does.
                </div>
                </>
                )}
              </div>
            )}
          </div>
        )}

        {/* ── reading one in the page ──
            A PDF renders inline. A .docx cannot — no browser renders one — so
            rather than showing an empty frame it says why and points at the two
            things that DO read on screen: the wording card above for a contract
            written here, and the download for everything else. */}
        {viewing && (
          <div className="card" style={{ marginBottom: 18 }}>
            <div className="card-h">
              <FileText size={16} className="ci" />
              <h3>{viewing.name}</h3>
              <span className="right">
                <button
                  className="btn sm" type="button"
                  onClick={() => downloadDocument(id, viewing.id, viewing.name)}
                >
                  <Download size={12} /> Download
                </button>
                <button
                  className="btn sm" type="button"
                  onClick={() => { URL.revokeObjectURL(viewing.url); setViewing(null); }}
                >
                  Close
                </button>
              </span>
            </div>
            {viewing.type.includes("pdf") ? (
              <object data={viewing.url} type="application/pdf"
                      style={{ width: "100%", height: "70vh", display: "block" }}>
                <div className="empty">
                  Your browser will not display this PDF in the page.{" "}
                  <span className="linkish" role="button"
                        onClick={() => downloadDocument(id, viewing.id, viewing.name)}>
                    Download it instead
                  </span>.
                </div>
              </object>
            ) : (
              <div style={{ padding: "16px 20px" }}>
                <div className="note">
                  <b>This one cannot be shown in the page.</b> It is a Word
                  document, and no browser renders one — the file has to be
                  opened in Word or Pages.
                  {authored && (
                    <> The good news is you do not need to: this contract was
                      written here, so the full wording is on this page under{" "}
                      <b>The wording</b> above — that is the same text this file
                      contains.</>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

          </div>

          <div className="rec-side">
          {/* ── documents ── */}
          <div className="card" style={{ marginBottom: 18 }}>
            <div className="card-h">
              <Paperclip size={16} className="ci" />
              <h3>Documents</h3>
              <InfoTip text="Files attached to this contract: references the wording relies on, endorsements agreed later, and the signed copy." />
            </div>
            <div style={{ padding: "16px 20px" }}>
              {active.length === 0 ? (
                <div className="empty" style={{ padding: "12px 10px" }}>
                  {authored
                    ? "Nothing attached, and nothing needs to be — this "
                      + "contract's wording is written above."
                    : "Nothing attached yet — without a wording this contract "
                      + "has no clauses."}
                </div>
              ) : (
                /* A grid of tiles rather than wide rows: in a side column a
                   row of name-then-buttons wraps into an unreadable ribbon,
                   and each document is one thing you act on as a unit. */
                <div className="doc-grid">
                {active.map(d => (
                  <div className="doc-tile" key={d.id}>
                    <span className="k">
                      <span className="badge b-mut" style={{ marginRight: 8 }}>
                        <span className="d" />{DOC_KIND[d.kind]?.label ?? d.kind}
                      </span>
                      {d.is_executed_copy && (
                        <span className="badge b-ok">
                          <span className="d" />signed copy
                        </span>
                      )}
                      <b className="fn" title={d.filename ?? undefined}>{d.filename}</b>
                      <div className="sub">
                        {d.satisfies_reference && <>answers “{d.satisfies_reference}” · </>}
                        {d.effective_from && <>effective {fmtDate(d.effective_from)} · </>}
                        attached {fmtStamp(d.created_at)}
                      </div>
                    </span>
                    <span className="acts">
                      {d.has_file && (
                        <>
                          <button
                            className="btn sm" type="button" disabled={!!busy}
                            onClick={() => run("view", async () => {
                              if (viewing) URL.revokeObjectURL(viewing.url);
                              const { url, type } = await openDocument(id, d.id);
                              setViewing({ id: d.id, name: d.filename ?? "Document",
                                           url, type });
                            })}
                          >
                            <Eye size={12} /> View
                          </button>
                          <button
                            className="btn sm" type="button" disabled={!!busy}
                            onClick={() => run("download", () =>
                              downloadDocument(id, d.id, d.filename))}
                          >
                            <Download size={12} /> Download
                          </button>
                        </>
                      )}
                      {a.upload_documents && (
                        <button
                          className="btn sm" type="button" disabled={!!busy}
                          title="Retire it — never deleted, because the rules in force were read from it"
                          onClick={() => run("retire", async () => {
                            const r = await deactivateDocument(id, d.id);
                            if (r.rules_stale) setRulesStale(true);
                          })}
                        >
                          <Trash2 size={12} /> Retire
                        </button>
                      )}
                    </span>
                  </div>
                ))}
                </div>
              )}

              {retired.length > 0 && (
                <details style={{ marginTop: 12 }}>
                  <summary className="sub" style={{ cursor: "pointer" }}>
                    {retired.length} retired document{retired.length === 1 ? "" : "s"}
                  </summary>
                  <div className="hint">
                    Kept, not deleted, so the rules they produced stay
                    explainable.
                    {retired.map(d => (
                      <div key={d.id}>
                        {DOC_KIND[d.kind]?.label ?? d.kind}: {d.filename}
                      </div>
                    ))}
                  </div>
                </details>
              )}

              {a.upload_documents && (
                <>
                  <div className="divider" />
                  <div className="grid gap-12" style={{ marginBottom: 12 }}>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Kind</label>
                      <select
                        value={kind}
                        onChange={e => setKind(e.target.value as ContractDocumentKind)}
                      >
                        {attachableKinds.map(k => (
                          <option key={k} value={k}>{DOC_KIND[k].label}</option>
                        ))}
                      </select>
                    </div>

                    {kind === "reference" && (
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Answers which reference</label>
                        <select value={satisfies}
                                onChange={e => setSatisfies(e.target.value)}>
                          <option value="">(match by name)</option>
                          {rec.missing_references.map(n => (
                            <option key={n} value={n}>{n}</option>
                          ))}
                        </select>
                      </div>
                    )}

                    {kind === "endorsement" && (
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Effective from</label>
                        <input type="date" value={effectiveFrom}
                               onChange={e => setEffectiveFrom(e.target.value)} />
                      </div>
                    )}

                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>File</label>
                      <input type="file"
                             onChange={e => setFile(e.target.files?.[0] ?? null)} />
                    </div>
                  </div>

                  {kind === "contract" && (
                    <label className="hint" style={{ display: "block", marginTop: 10 }}>
                      <input
                        type="checkbox" checked={executedCopy}
                        onChange={e => setExecutedCopy(e.target.checked)}
                        style={{ marginRight: 6, width: "auto" }}
                      />
                      This is the signed copy
                    </label>
                  )}

                  <div className="rowacts">
                    <button className="btn pri" type="button"
                            disabled={!file || !!busy} onClick={doUpload}>
                      <Upload size={13} />{" "}
                      {busy === "upload" ? "Attaching…" : "Attach"}
                    </button>
                    <span className="sub">{DOC_KIND[kind].blurb}</span>
                  </div>
                  {authored && (
                    <div className="hint">
                      This contract's wording was written here, not uploaded —
                      change it above rather than attaching a new one.
                    </div>
                  )}
                </>
              )}
            </div>
          </div>

          {/* ── how it got here ── */}
          {history.length > 0 && (
            <div className="card">
              <div className="card-h">
                <h3>History</h3>
                <InfoTip text="View the complete record of contract changes, discussions, approvals, and actions, with details on who made each change and when." />
              </div>
              {/* A STEPPER, oldest at the top. History is a sequence — sent,
                  pushed back, revised, agreed, signed — and a two-column table
                  of label-and-note hid the one thing it is: an order. The
                  latest step is marked, because "where did this get to" is
                  what the card is opened for. */}
              <div style={{ padding: "16px 20px" }}>
                <ol className="hist">
                  {history.map((h, i) => (
                    <li key={i} className={i === history.length - 1 ? "now" : ""}>
                      <span className="dot" aria-hidden="true" />
                      <div className="what">
                        {h.action.replace(/_/g, " ")}
                      </div>
                      <div className="when">
                        {fmtStamp(h.acted_at)}
                        {h.acted_by && <> · {h.acted_by.full_name}</>}
                      </div>
                      {h.note && <div className="said">“{h.note}”</div>}
                      {h.proposed_changes?.length > 0 && h.proposed_changes.map((ch, j) => (
                        <div className="moved" key={j}>
                          <b>{labelOf(ch.field)}</b>{" "}
                          <span className="mono">{ch.current || "—"}</span>
                          <span className="arrow">→</span>
                          <span className="mono">{ch.proposed || "—"}</span>
                          {ch.comment && <div className="sub">{ch.comment}</div>}
                        </div>
                      ))}
                    </li>
                  ))}
                </ol>
              </div>
            </div>
          )}
          </div>
        </div>
      </div>
    </div>
  );
}

/** What this contract checks, in the same words as the contracts list. */
function ChecksInForce({ checks }: { checks: Rec["checks"] }) {
  const w = describeChecks(checks);
  return (
    <>
      {w.none ? "None yet" : w.badge}
      {/* Helper line off for now (see contractChecks.ts) — w.detail/sources are "". */}
      {(w.detail || w.sources) && (
        <div className="hint" style={{ marginTop: 2 }}>
          {w.detail}
          {w.sources && w.sources !== w.detail && <> · {w.sources}</>}
        </div>
      )}
    </>
  );
}
