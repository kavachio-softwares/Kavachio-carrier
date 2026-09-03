/**
 * The Broker half of the scope picker — and the contract it brings with it.
 *
 * THE CONTRACT IS NOT A CHOICE. A contract already belongs to exactly one
 * (programme, broker) pair, so once the broker is picked the contract is
 * decided: asking again would only offer one answer, or invite a user to file
 * work against a pairing the carrier never created. So this exposes ONE select
 * — the broker — and binds their live contracts behind it.
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
 */
import { useEffect, useMemo, useState } from "react";
import { Field, Select } from "./ui/Field";
import {
  getProgrammeBrokers, getScopedContracts,
  type ProgrammeBroker, type ScopedContract,
} from "../api/outputTemplate";

export function useBrokerContractScope(programId: number | "") {
  const [brokers, setBrokers] = useState<ProgrammeBroker[]>([]);
  const [contracts, setContracts] = useState<ScopedContract[]>([]);
  const [brokerPartyId, setBrokerPartyId] = useState<number | "">("");
  const [loading, setLoading] = useState(false);
  const [contractsLoading, setContractsLoading] = useState(false);

  // A new programme invalidates the pick — a broker from the last programme is
  // not on this one.
  useEffect(() => {
    setBrokerPartyId(""); setContracts([]);
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
    if (programId === "") { setContracts([]); return; }
    setContractsLoading(true);
    getScopedContracts(Number(programId),
                       brokerPartyId === "" ? null : Number(brokerPartyId), true)
      .then(setContracts)
      .catch(() => setContracts([]))
      .finally(() => setContractsLoading(false));
  }, [programId, brokerPartyId]);

  // The contract this scope resolves to. One live contract means the scope is
  // that contract; several means the broker holds more than one and the scope
  // stops at the broker — a template made here covers all of them, which is
  // what a broker-level template is for.
  const contractId = useMemo<number | "">(
    () => (contracts.length === 1 ? contracts[0].id : ""),
    [contracts]);

  return {
    brokers, contracts, loading, contractsLoading,
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
        <option value="">All brokers on this programme</option>
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

/** What the broker pick brought with it — shown, never chosen. */
export function BoundContracts({ scope, programPicked }: {
  scope: ReturnType<typeof useBrokerContractScope>;
  programPicked: boolean;
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

  if (scope.contracts.length === 0) {
    return (
      <Hint tone="warn">
        {scope.brokerPartyId === ""
          ? "This programme has no approved contract yet. A contract a broker uploads stays out until the carrier approves it."
          : "This broker has no approved contract on the programme yet. A contract they uploaded stays out of this list until the carrier approves it."}
      </Hint>
    );
  }

  return (
    <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
      <div className="text-[10.5px] uppercase tracking-wide text-ink-soft">
        Contract{scope.contracts.length === 1 ? "" : "s"} bound to this selection
      </div>
      <ul className="mt-1 space-y-0.5">
        {scope.contracts.map(c => (
          <li key={c.id} className="text-[12.5px] flex items-center gap-2 min-w-0">
            <span className="truncate">{c.filename || `Contract ${c.id}`}</span>
            <span className="text-[11px] text-ink-soft shrink-0">
              {c.broker_name ? c.broker_name : "carrier held"}
            </span>
          </li>
        ))}
      </ul>
      <p className="text-[11px] text-ink-soft mt-1">
        {scope.contracts.length === 1
          ? "A contract belongs to one programme and one broker, so picking the broker decides it."
          : scope.contracts.some(c => !c.broker_name)
            ? "The carrier's own contracts govern the programme whichever broker is picked, so they are included too."
            : "This broker holds more than one live contract, so the setup covers all of them."}
      </p>
    </div>
  );
}

function Hint({ children, tone }: { children: React.ReactNode; tone?: "warn" }) {
  return (
    <p className={`text-[11px] mt-1 ${tone === "warn" ? "text-amber-700" : "text-ink-soft"}`}>
      {children}
    </p>
  );
}
