/**
 * Contracts — every contract this carrier holds, in one place.
 *
 * The view the app never had. Contracts could only be reached one broker or one
 * programme at a time, which answers "what does CRC hold?" but not the
 * questions people actually arrive with: what is expiring, what is waiting on
 * me, where is that contract.
 *
 * Two ways a contract gets here and they are different jobs: uploading one that
 * EXISTS reads its terms out of the wording; raising one states terms being
 * agreed now, with the document to follow. Both end at the same record.
 *
 * Signature history hangs off this screen rather than off the sidebar: what is
 * out for signature is a fact about the contracts listed here, and reading it
 * anywhere else means first remembering which contract you meant.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { FilePlus2, History, Upload } from "lucide-react";
import { getHierarchy, type HierarchyProgramme } from "../api/hierarchy";
import {
  listContracts,
  type ContractRecord, type Lifecycle,
} from "../api/contractRecord";
import { fmtDate } from "../utils/date";
import { describeChecks } from "../utils/contractChecks";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { ListFilterBar } from "../components/ListFilterBar";

/** What the lifecycle means to someone scanning the list.
 *
 *  `expired` is derived by the server from the term rather than stored, so a
 *  contract that lapsed overnight reads correctly here without anything having
 *  run to update it. */
const STATE: Record<Lifecycle, { label: string; cls: string; note: string }> = {
  // Neutral on purpose: this list carries both a carrier's own drafts (nobody
  // to submit to) and a broker's (waiting to be submitted).
  draft: { label: "Draft", cls: "b-mut", note: "not live yet" },
  pending: { label: "Pending", cls: "b-warn", note: "waiting on a decision" },
  in_review: { label: "Out for review", cls: "b-warn", note: "with the broker" },
  changes_requested: { label: "Changes requested", cls: "b-warn",
                       note: "the broker pushed back — your move" },
  agreed: { label: "Terms agreed", cls: "b-ok", note: "the broker signs next" },
  signed: { label: "Signed", cls: "b-ok",
            note: "returned — yours to place and put in force" },
  // "In force", not "Live". Live is what a website is: it says the row is
  // switched on and nothing about the contract. In force is the state an
  // insurance contract is actually in — signed, running, and the only state a
  // bordereau can be produced against — and it is the phrase the wording, the
  // lifecycle and the rest of this app already use.
  active: { label: "In force", cls: "b-ok",
            note: "cover is running — bordereaux can be produced against it" },
  expired: { label: "Expired", cls: "b-mut", note: "its term has run out" },
  terminated: { label: "Terminated", cls: "b-crit", note: "ended early" },
  superseded: { label: "Superseded", cls: "b-mut", note: "replaced by a renewal" },
};

const TYPE_LABEL: Record<string, string> = {
  insurer_broker: "Insurer ↔ Broker",
  insurer_reinsurer: "Insurer ↔ Reinsurer",
};

export default function Contracts() {
  const [rows, setRows] = useState<ContractRecord[] | null>(null);
  const [programmes, setProgrammes] = useState<HierarchyProgramme[]>([]);
  const [err, setErr] = useState("");

  const [programme, setProgramme] = useState("");
  const [lifecycle, setLifecycle] = useState("");
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  // Debounced because this search runs on the SERVER — filtering happens in
  // SQL, so an undebounced box is one request per keystroke.
  const query = useDebouncedValue(q, 300);

  useEffect(() => {
    getHierarchy()
      .then(h => setProgrammes(h.programmes))
      .catch(() => setProgrammes([]));
  }, []);

  const load = useCallback(() => {
    listContracts({
      program_id: programme ? Number(programme) : undefined,
      lifecycle: lifecycle || undefined,
      contract_type: type || undefined,
      q: query.trim() || undefined,
    })
      .then(setRows)
      .catch(e => setErr(e?.response?.data?.detail || "Could not load contracts."));
  }, [programme, lifecycle, type, query]);
  useEffect(load, [load]);

  const filtersActive = !!(programme || lifecycle || type || q);

  // Surfaced above the table because it is the one thing on this screen that
  // blocks work: a contract that names a document nobody supplied cannot be
  // submitted, activated, or have its rules generated.
  const blocked = useMemo(
    () => (rows ?? []).filter(r => r.missing_references.length > 0).length,
    [rows]);

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Contracts</h2>
            <p>Every contract you hold, across all your programmes and brokers.</p>
          </div>
          <div className="actions">
            {/* Where the Signatures sidebar tab went. Watching a signing round
                is watching a contract, so the way in is from the contracts you
                hold rather than a tab of its own — and the same link, carrying
                a contract id, is what the record's Signatures card opens. */}
            <Link to="/contracts/signatures" className="btn">
              <History size={14} /> Signature history
            </Link>
            <Link to="/contracts/upload" className="btn">
              <Upload size={14} /> Upload existing
            </Link>
            <Link to="/contracts/new" className="btn pri">
              <FilePlus2 size={14} /> Create Contract
            </Link>
          </div>
        </div>

        {err && (
          <div className="note warn" style={{ marginBottom: 16, maxWidth: 620 }}>
            {err}
          </div>
        )}

        {blocked > 0 && (
          <div className="note warn" style={{ marginBottom: 16 }}>
            <b>
              {blocked} contract{blocked === 1 ? "" : "s"} refer to a document
              that has not been supplied.
            </b>{" "}
            Until it is, some of their clauses cannot be checked — so they
            cannot be submitted or put in force.
          </div>
        )}

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ,
                      placeholder: "Name, UMR or filename" }}
            selects={[
              {
                key: "programme", ariaLabel: "Filter by programme",
                value: programme, onChange: setProgramme,
                options: [{ value: "", label: "All programmes" },
                  ...programmes.map(p => ({ value: String(p.id), label: p.name }))],
              },
              {
                key: "type", ariaLabel: "Filter by type",
                value: type, onChange: setType,
                options: [
                  { value: "", label: "All types" },
                  { value: "insurer_broker", label: "Insurer ↔ Broker" },
                  { value: "insurer_reinsurer", label: "Insurer ↔ Reinsurer" },
                ],
              },
              {
                key: "state", ariaLabel: "Filter by state",
                value: lifecycle, onChange: setLifecycle,
                options: [{ value: "", label: "All states" },
                  ...(Object.keys(STATE) as Lifecycle[]).map(k => ({
                    value: k, label: STATE[k].label }))],
              },
            ]}
            onClear={() => {
              setProgramme(""); setLifecycle(""); setType(""); setQ("");
            }}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Contract</th><th>Counterparty</th><th>Programme</th>
                  <th>Term</th><th>State</th><th>Documents</th>
                  {/* What is MEASURED, which is not what the contract says. A
                      contract can carry ten agreed limits and nought checks
                      until it is bound to the bordereau template it reports
                      into — and nothing on this screen used to say so. */}
                  <th>Checks</th>
                </tr>
              </thead>
              <tbody>
                {(rows ?? []).map(c => {
                  const st = STATE[c.lifecycle] ?? STATE.draft;
                  return (
                    <tr key={c.id}>
                      <td>
                        <Link to={`/contracts/${c.id}`}><b>{c.name}</b></Link>
                        <div className="sub">
                          {c.contract_type
                            ? TYPE_LABEL[c.contract_type] ?? c.contract_type
                            : "type not set"}
                          {c.umr && <> · {c.umr}</>}
                        </div>
                      </td>
                      <td>
                        {c.counterparty?.name ?? "—"}
                        {c.counterparty && (
                          <div className="sub">{c.counterparty.party_type}</div>
                        )}
                      </td>
                      <td>{c.programme?.name ?? "—"}</td>
                      <td className="mono">
                        {c.inception_dt && c.expiry_dt
                          ? `${fmtDate(c.inception_dt)} → ${fmtDate(c.expiry_dt)}`
                          : "—"}
                      </td>
                      <td>
                        <span className={`badge ${st.cls}`}>
                          <span className="d" />{st.label}
                        </span>
                        <div className="sub">{st.note}</div>
                      </td>
                      <td>
                        {c.has_wording
                          ? <span className="muted">Wording on file</span>
                          : <span className="faint">No wording yet</span>}
                        {c.endorsement_count > 0 && (
                          <div className="sub">
                            +{c.endorsement_count} endorsement
                            {c.endorsement_count === 1 ? "" : "s"}
                          </div>
                        )}
                        {c.missing_references.length > 0 && (
                          <div className="sub" style={{ color: "var(--p-warn-ink)" }}>
                            {c.missing_references.length} document
                            {c.missing_references.length === 1 ? "" : "s"} missing
                          </div>
                        )}
                      </td>
                      <td><Checks c={c} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {rows !== null && rows.length === 0 && (
              <div className="empty">
                {filtersActive
                  ? "No contracts match those filters."
                  : "No contracts yet. Upload a wording you already have, or raise "
                    + "one from its terms and let the wording follow."}
              </div>
            )}
            {rows === null && !err && <div className="empty">Loading…</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * How much of one contract is actually measured on a file.
 *
 * The gap this exists to close: a carrier writes a contract with ten limits on
 * it, every one of them stated in the wording and shown on the record, and not
 * one of them is checked against anything, because a check is a comparison
 * against a bordereau column and nothing had ever bound the two together. The
 * count was invisible, so the contract looked finished.
 *
 * A contract read out of a PDF gets its rules from its clauses instead, and has
 * no agreed limits at all, so it says where its rules came from.
 *
 * The words come from describeChecks, which the contract page uses too.
 *
 * Read-only. There used to be a "Bind checks" / "Re-bind" button here; it was
 * taken off this screen.
 */
function Checks({ c }: { c: ContractRecord }) {
  const w = describeChecks(c.checks);
  if (w.none) {
    // Helper text off for now (see contractChecks.ts) — w.detail is "".
    return <span className="faint">None</span>;
  }
  return (
    <>
      <span className={`badge ${w.ok ? "b-ok" : "b-warn"}`}
        title={w.sources && w.sources !== w.detail ? w.sources : undefined}>
        <span className="d" />
        {w.badge}
      </span>
      {/* Helper line off for now (see contractChecks.ts) — w.detail is "". */}
      {!!w.detail && <div className="sub">{w.detail}</div>}
    </>
  );
}
