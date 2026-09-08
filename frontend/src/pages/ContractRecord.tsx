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
  AlertTriangle, ArrowLeft, ArrowRight, CheckCircle2, Download, ExternalLink,
  Eye, FileText, History, MessagesSquare, Paperclip, PenLine, Plus, RefreshCw,
  Send, Trash2, Upload, XCircle,
} from "lucide-react";
import { currentMga, getTenantBrand } from "../auth";
import { WordingEditor } from "../components/WordingEditor";
import { fmtDate, fmtStamp } from "../utils/date";
import { getApprovalHistory, type ApprovalEvent } from "../api/hierarchy";
import {
  getContractRound, inAppSigningUrl, type ContractRound,
} from "../api/esign";
import {
  acceptTerms, activateContract, deactivateDocument, downloadDocument,
  openDocument,
  generateRules, getContract, getContractTypes, renewContract, requestChanges,
  sendForReview, submitContract, submitSigned, terminateContract,
  downloadContractPdf, previewWording,
  updateContract, uploadDocument, fieldErrors,
  type ContractDocumentKind, type ContractField, type ContractRecord as Rec,
  type AgreedLimits, type AgreedLimitSpec, type ContractTypeSpec, type FieldErrors,
  type Lifecycle, type LimitGroup,
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
  active: { label: "Live", cls: "b-ok",
            note: "In force. Bordereaux can be produced against it." },
  expired: { label: "Expired", cls: "b-mut",
             note: "Its term has run out. Renew it into a successor." },
  terminated: { label: "Terminated", cls: "b-crit",
                note: "Ended early. Nothing more can be attached to it." },
  superseded: { label: "Superseded", cls: "b-mut",
                note: "Replaced by a renewal." },
};

const DOC_KIND: Record<ContractDocumentKind, { label: string; blurb: string }> = {
  contract: {
    label: "Wording",
    blurb: "The contract itself. One at a time — attaching a new one retires "
         + "the previous, because a contract has one wording.",
  },
  reference: {
    label: "Reference",
    blurb: "A document the wording defers to. Required once the wording names "
         + "one: the clauses pointing at it cannot be checked without it.",
  },
  endorsement: {
    label: "Endorsement",
    blurb: "A change agreed after the fact. It does NOT replace the wording — "
         + "both stay active, and the endorsed value is the one that becomes "
         + "a rule.",
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

  // Termination / renewal
  const [showTerminate, setShowTerminate] = useState(false);
  const [reason, setReason] = useState("");
  const [showRenew, setShowRenew] = useState(false);
  const [renewFrom, setRenewFrom] = useState("");
  const [renewTo, setRenewTo] = useState("");

  // Editing the terms. The inputs are built from the SERVER's field spec, the
  // same one the create form uses and the same one the server validates
  // against — so "what this type requires" is stated once, not three times.
  const [specs, setSpecs] = useState<ContractTypeSpec[]>([]);
  // The limits vocabulary, so this screen shows what was agreed in the same
  // words and the same three groups the create flow asked for it in.
  const [limitSpec, setLimitSpec] = useState<AgreedLimitSpec[]>([]);
  const [limitGroups, setLimitGroups] = useState<LimitGroup[]>([]);
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
  const [wActive, setWActive] = useState(0);
  const [wTokens, setWTokens] = useState<Record<string, string>>({});
  // Bumped only when the sections are REPLACED wholesale (rebuilt from the
  // terms). The editor rebuilds its DOM on this, and rebuilding it on every
  // keystroke would throw the caret to the start of the clause.
  const [wVersion, setWVersion] = useState(0);

  useEffect(() => {
    getContractTypes()
      .then(d => {
        setSpecs(d.types);
        setLimitSpec(d.agreed_limits);
        setLimitGroups(d.limit_groups);
      })
      .catch(() => setSpecs([]));
  }, []);

  const load = useCallback(() => {
    getContract(id)
      .then(setRec)
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
  // Whose draft this is changes what "draft" means. A broker's is waiting to be
  // submitted; a carrier's is simply not live yet, because a carrier submits to
  // nobody.
  const draftNote = rec.approval_status === "pending_approval"
    ? "Not submitted yet. Its terms can still be corrected."
    : "Not live yet — nothing is checked against it. Its terms can still be "
      + "changed, and you can make it live when you are ready.";
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
      setWVersion(v => v + 1);
      setWActive(0);
      setWEditing(true);
      if (rebuild) {
        setNote("Rewritten from the terms. Nothing is saved until you save it.");
      }
    } catch (e) {
      setErr(fieldErrors(e).message);
    }
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
      await updateContract(id, {
        wording_sections: wSections.map(
          ({ key, title, body, origin }) => ({ key, title, body, origin })),
      });
      setWEditing(false);
      setNote("The wording was updated.");
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
    return (
      <div className="field" key={f.name} style={{ marginBottom: 0 }}>
        <label>
          {f.label}{f.required && <span style={{ color: "var(--p-crit-ink)" }}>*</span>}
        </label>
        <input
          type={f.kind === "date" ? "date"
               : f.kind === "int" || f.kind === "decimal" ? "number" : "text"}
          step={f.kind === "decimal" ? "0.01" : undefined}
          value={draft[f.name] ?? ""}
          onChange={e => setDraft(d => ({ ...d, [f.name]: e.target.value }))}
          style={bad ? { borderColor: "var(--p-crit)" } : undefined}
        />
        {bad && (
          <div className="hint" style={{ color: "var(--p-crit-ink)" }}>{bad}</div>
        )}
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
            <div className="hint" style={{ marginTop: 0 }}>
              Approval: {rec.approval_status.replace("_", " ")}
              {rec.approved_at && <> · decided {fmtStamp(rec.approved_at)}</>}
              {rec.submitted_at && <> · submitted {fmtStamp(rec.submitted_at)}</>}
            </div>
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
              {a.submit && (
                <button
                  className="btn pri" type="button" disabled={!!busy}
                  onClick={() => run("submit", () => submitContract(id))}
                >
                  <Send size={13} /> Submit for approval
                </button>
              )}
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
              {a.generate_rules && rec.has_wording && (
                <button className="btn" type="button" disabled={!!busy}
                        onClick={doGenerate}>
                  <RefreshCw size={13} />
                  {busy === "rules" ? "Reading…" : "Re-read rules"}
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
              {rec.programme && (
                <Link className="btn"
                      to={`/programs/${rec.programme.id}/contracts/${rec.id}`}>
                  <FileText size={13} /> Clauses &amp; rules
                </Link>
              )}
              {/* Screen only — no signing provider is connected yet, which the
                  screen itself says before anything else on it. */}
              <Link className="btn" to={`/contracts/${rec.id}/signature`}>
                <PenLine size={13} /> Signature
              </Link>
            </div>

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
                           onChange={e => setRenewFrom(e.target.value)} />
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Expiry</label>
                    <input type="date" value={renewTo}
                           onChange={e => setRenewTo(e.target.value)} />
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
                <div className="grid g-3">{editable.map(editInput)}</div>

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
                    <div key={g.key} style={{ marginTop: 18 }}>
                      <div className="sub-h">{g.label}</div>
                      <div className="lim lim-h">
                        <div className="lq"><b>What you agreed</b></div>
                        <div className="sub">The limit</div>
                        <div className="sub">If a file breaks it</div>
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
                                <div className="segpick">
                                  <button
                                    type="button"
                                    className={sev === "critical" ? "on" : ""}
                                    onClick={() => setDraftLimit(
                                      l.name, { severity: "critical" })}
                                  >
                                    Stop the row
                                  </button>
                                  <button
                                    type="button"
                                    className={sev === "warning" ? "on" : ""}
                                    onClick={() => setDraftLimit(
                                      l.name, { severity: "warning" })}
                                  >
                                    Just flag it
                                  </button>
                                </div>
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
                  );
                })}

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
                  <Row label="Risk code">{rec.risk_code}</Row>
                  <Row label="Section">{rec.section_number}</Row>
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
        {rec.agreed_limits && Object.keys(rec.agreed_limits).length > 0 && (
          <div className="card" style={{ marginBottom: 18 }}>
            <div className="card-h">
              <h3>What was agreed</h3>
              <span className="sub">
                each one is a clause in the wording and a check on every row
              </span>
            </div>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr><th>Term</th><th>Agreed</th><th>If a file breaks it</th></tr>
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
                                    {e.severity === "critical"
                                      ? "Stop the row" : "Flag it"}
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
                {/* The same rail the create flow uses. One section at a
                    time: a contract is written clause by clause, and a page
                    of stacked editable boxes makes it far too easy to type
                    into the wrong one. */}
                <div className="doc">
                  <div className="doc-rail">
                    {wSections.map((sec, i) => (
                      <div
                        key={sec.key} role="button" tabIndex={0}
                        className={`doc-sec${i === wActive ? " on" : ""}`}
                        onClick={() => setWActive(i)}
                        onKeyDown={e => e.key === "Enter" && setWActive(i)}
                      >
                        <span className="no">{i + 1}</span>
                        <span className="txt">
                          <span className="nm">{sec.title}</span>
                          <span className="st">{sec.origin}</span>
                        </span>
                        <span
                          className="rm" role="button" tabIndex={0}
                          title="Remove this clause"
                          onClick={e => {
                            e.stopPropagation();
                            setWSections(ss => ss.filter(s2 => s2.key !== sec.key));
                            setWActive(k => Math.max(0, k > i ? k - 1 : k));
                            setWVersion(v => v + 1);
                          }}
                          onKeyDown={e => e.stopPropagation()}
                        >×</span>
                      </div>
                    ))}
                    <button
                      className="btn sm" type="button"
                      style={{ width: "100%", marginTop: 6 }}
                      onClick={() => {
                        const key = `own_${Date.now()}`;
                        setWSections(ss => [...ss, {
                          key, title: "New clause", origin: "your own words",
                          body: "",
                        }]);
                        setWActive(wSections.length);
                        setWVersion(v => v + 1);
                      }}
                    >
                      <Plus size={12} /> Add a clause
                    </button>
                  </div>

                  <div className="doc-body">
                    {wSections[wActive] && (
                      <>
                        <div className="field" style={{ marginBottom: 12 }}>
                          <label>Heading</label>
                          <input
                            value={wSections[wActive].title}
                            onChange={e => {
                              const t = e.target.value;
                              setWSections(ss => ss.map(
                                (s2, i) => i === wActive
                                  ? { ...s2, title: t } : s2));
                            }}
                          />
                        </div>
                        <WordingEditor
                          sectionKey={`${wVersion}:${wSections[wActive].key}`}
                          body={wSections[wActive].body}
                          tokens={wTokens}
                          labels={Object.fromEntries(
                            limitSpec.map(l => [l.name, l.question]))}
                          onChange={body => setWSections(ss => ss.map(
                            (s2, i) => i === wActive
                              ? { ...s2, body,
                                  origin: s2.origin.includes("edited")
                                    ? s2.origin : `${s2.origin} · edited` }
                              : s2))}
                        />
                      </>
                    )}
                    {!wSections.length && (
                      <div className="empty">
                        Every clause has been removed. Add one, or rewrite the
                        wording from the terms.
                      </div>
                    )}
                  </div>
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
                {/* Typeset as a document, not listed as fields. This IS the
                    contract, so it should read like one — and it is the same
                    text the PDF carries, set the same way, so the screen and
                    the download are recognisably one document. */}
                <div className="contract-doc">
                  {(rec.wording_sections ?? []).map((sec, i) => (
                    <div key={sec.key}>
                      <h4>{i + 1}. &nbsp;{sec.title}</h4>
                      {sec.body.split("\n").map((line, j) => {
                        const resolved = line.replace(
                          /\{\{([a-z_]+)\}\}/g, (_m, k) => {
                            const fs = limitSpec.find(x => x.name === k);
                            const v = rec.agreed_limits?.[k]?.value;
                            if (v != null) return `${v}${fs?.unit ? ` ${fs.unit}` : ""}`;
                            return fs?.question ?? k;
                          });
                        // The clause number hangs in the margin, as on paper.
                        const m = resolved.match(/^(\d+\.\d+)\s+(.*)$/s);
                        return (
                          <p key={j}>
                            {m
                              ? <><span className="cl">{m[1]}</span>{m[2]}</>
                              : resolved}
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

        {/* ── documents ── */}
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <Paperclip size={16} className="ci" />
            <h3>Documents</h3>
          </div>
          <div style={{ padding: "16px 20px" }}>
            {active.length === 0 ? (
              <div className="empty" style={{ padding: "18px 10px" }}>
                {authored
                  ? "Nothing attached — and nothing needs to be. This contract's "
                    + "wording is above, and its checks come from the terms it "
                    + "was written from. Attach a reference the wording defers "
                    + "to, an endorsement, or the executed copy once it is signed."
                  : "Nothing attached yet. This contract exists as a record — "
                    + "with no wording there are no clauses and no rules, so "
                    + "nothing can be produced against it."}
              </div>
            ) : (
              active.map(d => (
                <div className="kv" key={d.id}>
                  <span className="k">
                    <span className="badge b-mut" style={{ marginRight: 8 }}>
                      <span className="d" />{DOC_KIND[d.kind]?.label ?? d.kind}
                    </span>
                    <b style={{ color: "var(--p-ink)" }}>{d.filename}</b>
                    {d.is_executed_copy && (
                      <span className="badge b-ok" style={{ marginLeft: 8 }}>
                        <span className="d" />signed copy
                      </span>
                    )}
                    <div className="sub">
                      {d.satisfies_reference && <>answers “{d.satisfies_reference}” · </>}
                      {d.effective_from && <>effective {fmtDate(d.effective_from)} · </>}
                      attached {fmtStamp(d.created_at)}
                    </div>
                  </span>
                  <span style={{ display: "flex", gap: 8 }}>
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
              ))
            )}

            {retired.length > 0 && (
              <details style={{ marginTop: 12 }}>
                <summary className="sub" style={{ cursor: "pointer" }}>
                  {retired.length} retired document{retired.length === 1 ? "" : "s"}
                </summary>
                <div className="hint">
                  {retired.map(d => (
                    <div key={d.id}>
                      {DOC_KIND[d.kind]?.label ?? d.kind}: {d.filename} — retired,
                      kept so the rules it produced stay explainable
                    </div>
                  ))}
                </div>
              </details>
            )}

            {a.upload_documents && (
              <>
                <div className="divider" />
                <div className="grid g-3">
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
                    The wording of this contract is not uploaded — it was
                    written from the terms above, and the two are tied to each
                    other. To change what it says, change a term and re-read the
                    contract, or endorse it if it is already running.
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
              <span className="sub">
                the approval gate and the negotiation, in one thread
              </span>
            </div>
            <div style={{ padding: "16px 20px" }}>
              {history.map((h, i) => (
                <div className="kv" key={i} style={{ alignItems: "flex-start" }}>
                  <span className="k" style={{ minWidth: 150 }}>
                    <b style={{ color: "var(--p-ink)", textTransform: "capitalize" }}>
                      {h.action.replace(/_/g, " ")}
                    </b>
                    <div className="sub">
                      {fmtStamp(h.acted_at)}
                      {h.acted_by && <> · {h.acted_by.full_name}</>}
                    </div>
                  </span>
                  <span className="v" style={{ fontWeight: 400, textAlign: "right" }}>
                    {h.note && <div className="muted">“{h.note}”</div>}
                    {h.proposed_changes?.length > 0 && h.proposed_changes.map((ch, j) => (
                      <div className="sub" key={j}>
                        <b>{labelOf(ch.field)}</b>:{" "}
                        <span className="mono">{ch.current || "—"}</span>
                        {" → "}
                        <span className="mono">{ch.proposed || "—"}</span>
                        {ch.comment && <> — {ch.comment}</>}
                      </div>
                    ))}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
