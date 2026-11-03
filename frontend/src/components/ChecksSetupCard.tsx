/**
 * Whether files sent under this contract can be checked against it — on the
 * contract record, for the carrier.
 *
 * A contract is checked through its CHECKS for the Bordereau Setup that runs
 * its files. A contract uploaded to replace another, or added to a programme
 * that already had a setup, used to get none: Process Bordereau opened anyway
 * and its files came back "Clean" having been checked against nothing. Now the
 * upload stays closed until it is set up, and this card says so and offers the
 * two ways — the setup's existing templates (recommended when there is one),
 * or a new setup. What it says is the server's answer (`checks_setup`).
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowRight, CheckCircle2 } from "lucide-react";
import {
  fieldErrors, getContract, makeChecksWithSetup, saveSetupDateColumn,
  type ContractRecord,
} from "../api/contractRecord";

export function ChecksSetupCard({ rec, onRecord, onNotice }: {
  rec: ContractRecord;
  onRecord: (r: ContractRecord) => void;
  onNotice: (m: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [dateCol, setDateCol] = useState("");
  const cs = rec.checks_setup;
  if (!cs) return null;

  async function saveDateColumn() {
    if (!cs?.setup || !dateCol) return;
    setBusy(true); setErr("");
    try {
      await saveSetupDateColumn(cs.setup.id, dateCol);
      onRecord(await getContract(rec.id));
      onNotice(`“${dateCol}” now dates each row for Bordereau Setup “${cs.setup.name}”.`);
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally { setBusy(false); }
  }

  async function make() {
    if (!cs?.setup) return;
    setBusy(true); setErr("");
    try {
      const r = await makeChecksWithSetup(rec.id, cs.setup.id);
      onRecord(r);
      const m = r.checks_made;
      onNotice(m && m.checks > 0
        ? `${m.checks} check${m.checks === 1 ? "" : "s"} made for “${cs.setup.name}”.`
          + (m.files_queued ? ` ${m.files_queued} waiting file${m.files_queued === 1 ? " is" : "s are"} being processed now.` : "")
          + (m.mid_term_refreshed ? ` Mid-term change ${m.mid_term_refreshed.number}'s checks were worked out again.` : "")
          + (m.mid_term_history
            ? (m.mid_term_history.error
              ? ` The checks for the days before mid-term change ${m.mid_term_history.changes.join(", ")} could not be made: ${m.mid_term_history.error}`
              : ` Rows dated before mid-term change ${m.mid_term_history.changes.join(", ")} are checked on the wording they had then (${m.mid_term_history.earlier_checks} earlier check${m.mid_term_history.earlier_checks === 1 ? "" : "s"}).`)
            : "")
        : "No checks could be made from this contract for that setup's template — see the Clauses tab.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally { setBusy(false); }
  }

  if (cs.state === "ready") {
    const d = cs.dates;
    return (
      <>
        <div className="hint" style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
          <CheckCircle2 size={13} style={{ color: "var(--p-ok)", flex: "none", position: "relative", top: 2 }} />
          <span>
            Files under this contract are checked against its {cs.checks} check{cs.checks === 1 ? "" : "s"}
            {cs.setup && <> on Bordereau Setup <b>{cs.setup.name}</b></>}
            {d?.column && <> · each row dated by <b>{d.column}</b></>}.
          </span>
        </div>
        {/* Dated checks (a mid-term change) place each row by its date. With
            no column named they can't, and those rows go unchecked. */}
        {d && !d.column && cs.setup && (
          <div className="note warn" style={{ marginTop: 10 }}>
            <b>Choose the column that dates each row.</b> {d.dated_checks} of this
            contract's checks change on a date (a mid-term change), and Bordereau
            Setup <b>{cs.setup.name}</b> doesn't say which column holds each row's
            transaction effective date — until it does, those checks are not run.
            <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
              <select value={dateCol} onChange={e => setDateCol(e.target.value)}
                      aria-label="Transaction effective date column">
                <option value="">Choose a column…</option>
                {d.columns.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
              <button className="btn pri" type="button" disabled={!dateCol || busy}
                      onClick={saveDateColumn}>
                {busy ? "Saving…" : "Save"}
              </button>
            </div>
            {err && <div style={{ marginTop: 8, color: "var(--p-crit-ink)" }}>{err}</div>}
          </div>
        )}
      </>
    );
  }

  if (cs.state === "no_setup") {
    return (
      <div className="note" style={{
        marginTop: 12, display: "flex", gap: 14, flexWrap: "wrap",
        alignItems: "center", justifyContent: "space-between",
        background: "var(--p-primary-soft)", borderColor: "#BFE0E4",
      }}>
        <div>
          <b style={{ color: "var(--p-primary-h)" }}>
            Next: set up this broker's bordereaux.
          </b>
          <div style={{ marginTop: 4 }}>
            There is no Bordereau Setup for this programme and broker yet, so no
            file can be processed under this contract. Setup opens with them
            already chosen.
          </div>
        </div>
        <Link className="btn pri" to={cs.new_setup_link}>
          Set up bordereau <ArrowRight size={13} />
        </Link>
      </div>
    );
  }

  // needs_checks: a live setup exists; this contract has no checks on it.
  const tpl = cs.setup?.output_template?.name;
  return (
    <div className="note warn" style={{ marginTop: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
        <AlertTriangle size={14} style={{ flex: "none", position: "relative", top: 2 }} />
        <div>
          <b>This contract has no checks yet.</b> Files sent under it can't be
          checked against it, so Process Bordereau stays closed for it — and files
          arriving by email, SFTP or API are kept until it is set up.
        </div>
      </div>
      <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
        <div className="card" style={{ padding: "10px 12px" }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
            <b>1 · Use the existing setup's templates</b>
            <span className="badge b-ok"><span className="d" />Recommended</span>
          </div>
          <div className="sub" style={{ marginTop: 4 }}>
            Bordereau Setup <b>{cs.setup?.name}</b>{tpl && <> (output template <b>{tpl}</b>)</>} stays
            as it is. Kavachio makes this contract's checks from its clauses for that
            template — the step Bordereau Setup runs — and puts the contract on the
            setup in place of the one it replaced. Takes a few minutes.
          </div>
          <div style={{ marginTop: 8 }}>
            <button className="btn pri" type="button" disabled={busy} onClick={make}>
              {busy ? "Making the checks… this takes a few minutes" : "Make the checks"}
            </button>
          </div>
        </div>
        <div className="card" style={{ padding: "10px 12px" }}>
          <b>2 · Set up with new templates</b>
          <div className="sub" style={{ marginTop: 4 }}>
            When this contract's bordereaux need a different input or output
            template from the existing setup's.
          </div>
          <div style={{ marginTop: 8 }}>
            <Link className="btn" to={cs.new_setup_link}>
              Go to Bordereau Setup <ArrowRight size={13} />
            </Link>
          </div>
        </div>
      </div>
      {err && <div style={{ marginTop: 8, color: "var(--p-crit-ink)" }}>{err}</div>}
    </div>
  );
}
