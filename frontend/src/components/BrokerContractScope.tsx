/**
 * The Broker half of the scope picker — and the contract it brings with it.
 *
 * ONE SELECT, THEN AT MOST ONE MORE. A contract belongs to exactly one
 * (programme, broker) pair, so the broker narrows the field to what that
 * pairing holds — which is why the broker is asked for first and asked for on
 * its own. What that leaves is either a single contract, which is not a choice
 * and is simply reported, or several, which IS one: a broker with three live
 * contracts has three different sets of terms and only the user knows which
 * govern the bordereau being set up. ContractPicker asks in that case only.
 *
 * BOTH ARE OPTIONAL. A programme with no brokers on it, and every setup made
 * before the broker level existed, leave them blank and keep working at
 * (carrier, programme) scope. That is why the empty state explains itself
 * rather than looking like a fault.
 *
 * One deliberate inclusion: a contract with no broker on it is the CARRIER's
 * own. Those predate the broker level and still govern the programme, so they
 * stay in the bound set whichever broker is picked — hiding them would make an
 * existing setup look empty.
 *
 * AND ONE DELIBERATE EXCLUSION, which is the same rule read the other way. With
 * no broker picked the server has nothing to narrow on and returns every
 * contract on the programme — including ones held by brokers the user has not
 * chosen. Binding those would let one broker's terms validate another broker's
 * bordereau, decided by nothing more than which filename matched which sheet.
 * So the bound set is narrowed here: carrier-held always, broker-held only once
 * that broker is picked. The rest are reported as `awaitingBroker` — visible,
 * so the screen can say what is waiting, and attached to nothing.
 */
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, FileText } from "lucide-react";
import { Field, Select } from "./ui/Field";
import {
  getProgrammeBrokers, getScopedContracts,
  type ProgrammeBroker, type ScopedContract,
} from "../api/outputTemplate";

export function useBrokerContractScope(programId: number | "") {
  const [brokers, setBrokers] = useState<ProgrammeBroker[]>([]);
  // Everything the server returned for this scope, before narrowing.
  const [allContracts, setAllContracts] = useState<ScopedContract[]>([]);
  const [brokerPartyId, setBrokerPartyId] = useState<number | "">("");
  const [loading, setLoading] = useState(false);
  const [contractsLoading, setContractsLoading] = useState(false);

  // A new programme invalidates the pick — a broker from the last programme is
  // not on this one.
  useEffect(() => {
    setBrokerPartyId(""); setAllContracts([]);
    if (programId === "") { setBrokers([]); return; }
    setLoading(true);
    getProgrammeBrokers(Number(programId))
      .then(rows => setBrokers(rows.filter(b => b.status !== "inactive")))
      .catch(() => setBrokers([]))
      .finally(() => setLoading(false));
  }, [programId]);

  // The contracts the broker brings. Loaded with no broker picked too, so a
  // programme's carrier-held contracts are reachable straight away.
  useEffect(() => {
    if (programId === "") { setAllContracts([]); return; }
    setContractsLoading(true);
    getScopedContracts(Number(programId),
                       brokerPartyId === "" ? null : Number(brokerPartyId), true)
      .then(setAllContracts)
      .catch(() => setAllContracts([]))
      .finally(() => setContractsLoading(false));
  }, [programId, brokerPartyId]);

  // With a broker picked the server has already narrowed to them plus the
  // carrier's own. With none picked it returned the whole programme, so only
  // the carrier-held ones are in scope — a contract belongs to one (programme,
  // broker) pair, and half a pair decides nothing.
  const contracts = useMemo(
    () => (brokerPartyId === ""
      ? allContracts.filter(c => c.broker_party_id == null)
      : allContracts),
    [allContracts, brokerPartyId]);

  // The broker-held contracts on this programme that the current pick does NOT
  // bind. Shown so "no contract yet" is never said about a programme that has
  // several — the answer is "pick whose".
  // Only the ones a pick can actually reach. Taking a broker off a programme
  // deactivates the link and LEAVES their contracts readable, so the server
  // still returns them — but they are gone from the dropdown, and telling
  // someone to "pick the broker" for a broker who is not there is a dead end.
  const awaitingBroker = useMemo(() => {
    if (brokerPartyId !== "") return [];
    const selectable = new Set(brokers.map(b => b.id));
    return allContracts.filter(c => c.broker_party_id != null
                                 && selectable.has(c.broker_party_id));
  }, [allContracts, brokerPartyId, brokers]);

  // The contract this scope resolves to. One live contract means the scope is
  // that contract; several means the broker holds more than one and the scope
  // stops at the broker — a template made here covers all of them, which is
  // what a broker-level template is for.
  const contractId = useMemo<number | "">(
    () => (contracts.length === 1 ? contracts[0].id : ""),
    [contracts]);

  return {
    brokers, contracts, awaitingBroker, loading, contractsLoading,
    /** How many brokers hold the contracts we are NOT binding — the number the
     *  hint quotes when it asks the user to pick one. */
    awaitingBrokerCount: new Set(awaitingBroker.map(c => c.broker_party_id)).size,
    brokerPartyId, setBrokerPartyId,
    contractId,
    /** Has the carrier put ANY broker on this programme? Until they have, there
     *  is no production relationship to build a setup against — which is why
     *  the setup screen holds its uploads shut on this. `null` while loading,
     *  so the screen never flashes the empty answer before it has one. */
    hasBrokers: programId === "" ? false : (loading ? null : brokers.length > 0),
    brokerName: brokers.find(b => b.id === brokerPartyId)?.legal_name ?? null,
    contractName: contracts.length === 1
      ? (contracts[0].filename || `Contract ${contracts[0].id}`) : null,
    /** Every contract the scope covers — what a setup binds, and what the
     *  screen shows in place of the dropdown that used to be here. */
    boundContracts: contracts,
    reset: () => setBrokerPartyId(""),
  };
}

export function BrokerSelect({ scope, disabled }: {
  scope: ReturnType<typeof useBrokerContractScope>;
  disabled?: boolean;
}) {
  const noBrokers = !scope.loading && scope.brokers.length === 0;
  // With nobody on the programme there is nothing to choose, so this stops
  // being a dropdown. A disabled select still reads as a list with an option
  // in it — and "All brokers on this programme" naming a set that is empty is
  // the wrong thing for it to appear to offer.
  if (noBrokers) {
    return (
      <Field label="Broker">
        <div className="input flex items-center bg-surface-2 text-ink-muted">
          {scope.loading ? "Looking…" : "No brokers on this programme yet"}
        </div>
      </Field>
    );
  }
  return (
    <Field label="Broker">
      <Select value={scope.brokerPartyId} disabled={disabled}
        onChange={e => scope.setBrokerPartyId(
          e.target.value ? Number(e.target.value) : "")}>
        {/* Reads as an action, matching "Select Program…" above it: with
            brokers on the programme, picking one is what you are here to do,
            and it is what decides the contract.

            It is NOT "All brokers on this programme" — that label described the
            old behaviour, where leaving it blank pulled in every broker's
            contracts and let one broker's terms validate another's bordereau.

            Leaving it unselected is still valid and still builds a
            programme-wide setup on the carrier's own contracts. A placeholder
            cannot say that and stay a placeholder, so ProgrammeWideNote says it
            under the dropdown instead. */}
        <option value="" disabled>Select Broker…</option>
        {scope.brokers.map(b => (
          <option key={b.id} value={b.id}>
            {b.legal_name} — {brokerNote(b)}
          </option>
        ))}
      </Select>
    </Field>
  );
}

/** What this broker actually brings, counted the way the screen means it.
 *
 *  `contract_count` includes contracts still waiting on the carrier, so a
 *  broker could be offered as having "1 contract" on a screen that says in the
 *  next breath that the programme has nothing approved. The approved count is
 *  the one that decides whether they can be worked with, and a broker with none
 *  says so rather than looking ready. */
function brokerNote(b: ProgrammeBroker): string {
  const live = b.approved_contract_count ?? b.contract_count;
  if (live > 0) return `${live} live contract${live === 1 ? "" : "s"}`;
  if (b.pending_approvals > 0) {
    return `${b.pending_approvals} contract${b.pending_approvals === 1 ? "" : "s"} awaiting your approval`;
  }
  return "no contract yet";
}

/** The contracts on file for this scope — and WHICH of them the setup uses.
 *
 *  This was a read-only list, on the reasoning stated at the top of the file:
 *  a contract belongs to one (programme, broker) pair, so the broker decides
 *  it and asking again would only offer one answer.
 *
 *  That holds for a broker with ONE contract. It does not hold for a broker
 *  with several. There the list bound every one of them, so a setup ran all of
 *  a broker's contracts against the same bordereau — and which contract governs
 *  the bordereau is a real question that only the user can answer. So when
 *  there is something to choose, this asks.
 *
 *  ONE AT A TIME. These are radios, not tick boxes: a setup runs on one
 *  contract, and picking a second replaces the first rather than adding to it.
 *  Two contracts bound at once is what produced the behaviour above.
 *
 *  One contract is still not a choice: it arrives selected (see DirectSetup's
 *  default) and this reads as the report it always was.
 *
 *  Selection lives in the PARENT, not here, because the Contracts field below
 *  shows the same contract and can drop it. Two copies of that state would let
 *  the radio and the chip disagree about what the build will actually run.
 */
export function ContractPicker({ scope, programPicked, selectedId, onSelect, onClear }: {
  scope: ReturnType<typeof useBrokerContractScope>;
  programPicked: boolean;
  /** The one contract this setup runs on, or null while none is picked. */
  selectedId: number | null;
  onSelect: (id: number) => void;
  onClear: () => void;
}) {
  if (!programPicked) return null;

  if (!scope.loading && scope.brokers.length === 0) {
    return (
      <Hint>
        No broker has been put on this programme yet, so this setup covers the
        whole programme. Add one on the Brokers screen to scope it further.
      </Hint>
    );
  }
  if (scope.contractsLoading) return <Hint>Finding the live contracts…</Hint>;

  // Nothing on file for this pairing. There is nothing to pick, so no picker —
  // the field below is where a contract gets uploaded instead.
  if (scope.contracts.length === 0) {
    // Nothing bound, but that can mean two different things and only one of
    // them is a problem. Contracts DO exist here — they just belong to brokers
    // nobody has chosen yet, and saying "no approved contract" would send
    // someone off to upload a duplicate of one already on file.
    if (scope.awaitingBroker.length > 0) {
      const n = scope.awaitingBroker.length;
      const b = scope.awaitingBrokerCount;
      return (
        <>
        <Hint>
          {n} approved contract{n === 1 ? "" : "s"} on this programme, held by{" "}
          {b} broker{b === 1 ? "" : "s"}. <b>Pick the broker</b> to bring theirs
          in — a contract belongs to one broker, so it cannot be attached until
          you say whose.
        </Hint>
        <ProgrammeWideNote scope={scope} />
        </>
      );
    }
    return (
      <>
      <Hint tone="warn">
        {scope.brokerPartyId === ""
          ? "This programme has no approved contract yet. A contract a broker uploads stays out until the carrier approves it."
          : "This broker has no approved contract on the programme yet. A contract they uploaded stays out of this list until the carrier approves it."}
      </Hint>
      <ProgrammeWideNote scope={scope} />
      </>
    );
  }

  const many = scope.contracts.length > 1;
  const chosen = scope.contracts.some(c => c.id === selectedId);

  return (
    <div className="rounded-lg border border-border bg-white px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase
          tracking-wide text-ink-muted">
          <FileText size={12} className="text-ink-soft" />
          {many
            ? "Choose the contract this setup runs on"
            : "Contract bound to this selection"}
        </div>
        {/* No "select all" — there is no such state. Clear stays: leaving the
            contract out and uploading a replacement is a real thing to want,
            and a radio cannot be unset by clicking it again. */}
        {chosen && (
          <button type="button" className="text-[11px] text-navy hover:underline shrink-0"
            onClick={onClear}>Clear</button>
        )}
      </div>

      <ul className="mt-1.5 space-y-1">
        {scope.contracts.map(c => {
          const on = c.id === selectedId;
          return (
            <li key={c.id}>
              {/* A radio even when there is only one, though that one arrives
                  already selected and so asks nothing. It is there because the
                  Contracts field below can drop a contract from the build, and
                  a state you can leave needs a way back into. */}
              <label className={`flex min-w-0 cursor-pointer items-center gap-2.5 rounded-md
                border px-2.5 py-2 text-[12.5px] transition
                ${on ? "border-navy/40 bg-navy/[0.05]"
                     : "border-transparent hover:border-border hover:bg-surface-2"}`}>
                <input type="radio" name="bdx-setup-contract" checked={on}
                  className="shrink-0 accent-navy"
                  onChange={() => onSelect(c.id)} />
                <span className={`min-w-0 flex-1 truncate ${on ? "font-medium" : "text-ink-muted"}`}>
                  {c.filename || `Contract ${c.id}`}
                </span>
                <span className="shrink-0 rounded bg-surface-2 px-1.5 py-0.5 text-[10.5px] text-ink-muted">
                  {c.broker_name ? c.broker_name : "carrier held"}
                </span>
              </label>
            </li>
          );
        })}
      </ul>

      {/* What the current pick means for the build. The empty case is the one
          that matters: nothing selected and nothing uploaded is a setup with no
          contract, and the Build button would refuse it with a message that
          does not mention the list the user is looking at. */}
      {!chosen ? (
        <p className="mt-2 flex items-start gap-1.5 rounded-md bg-warn/10 px-2.5 py-1.5
          text-[11.5px] leading-relaxed text-warn">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>
            {many
              ? "None selected — pick the contract this bordereau is written under, or upload one on the Contracts field below."
              : "Left out — pick it to put it back, or upload a replacement on the Contracts field below."}
          </span>
        </p>
      ) : many ? (
        <p className="text-[11px] text-ink-soft mt-1">
          One contract at a time — picking another replaces this one.
        </p>
      ) : (
        <p className="text-[11px] text-ink-soft mt-1">
          A contract belongs to one programme and one broker, so picking the
          broker decides it.
        </p>
      )}

      {/* Listed the ones this pick reaches, but the programme also holds broker
          contracts nobody has asked for. Say so, or the list reads as the whole
          truth. */}
      {scope.awaitingBroker.length > 0 && (
        <p className="text-[11px] text-ink-soft mt-1">
          {scope.awaitingBroker.length} more on this programme belong to{" "}
          {scope.awaitingBrokerCount} broker
          {scope.awaitingBrokerCount === 1 ? "" : "s"} — pick one to include theirs.
        </p>
      )}
    </div>
  );
}

/** What happens if the broker is left unselected.
 *
 *  The dropdown reads "Select Broker…" so it names the thing you are meant to
 *  do — but leaving it alone is not a dead end, it builds a setup covering the
 *  whole programme. A placeholder cannot say that and stay a placeholder, so it
 *  is said here. Without it the one supported route to a programme-wide setup
 *  would be undiscoverable. */
function ProgrammeWideNote({ scope }: {
  scope: ReturnType<typeof useBrokerContractScope>;
}) {
  if (scope.brokerPartyId !== "" || scope.brokers.length === 0) return null;
  return (
    <Hint>
      No broker selected, so this setup covers the whole programme and uses only
      the carrier&rsquo;s own contracts.
    </Hint>
  );
}

function Hint({ children, tone }: { children: React.ReactNode; tone?: "warn" }) {
  return (
    <p className={`text-[11px] mt-1 ${tone === "warn" ? "text-amber-700" : "text-ink-soft"}`}>
      {children}
    </p>
  );
}
