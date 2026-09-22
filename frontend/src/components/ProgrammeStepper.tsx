// How far one programme has got through setting up: Programme → Brokers →
// Contract → Bordereau setup. Every step opens the Configure Program flow for
// THIS programme at that step (flowUrl), so the stepper is a way through the
// flow, not just a picture of it.
//
// Worked out from the /hierarchy payload the Programmes list already loads, so
// it costs no extra request per row.
import { ArrowRight } from "lucide-react";
import { useNavigate } from "react-router-dom";
import type { HierarchyBroker, HierarchyProgramme } from "../api/hierarchy";

export type StepState = "done" | "current" | "todo";
export type Step = {
  key: "programme" | "brokers" | "contract" | "setup";
  label: string;
  /** One short line under the label: "2 brokers", "1 of 2 live". */
  detail: string;
  /** What the pill says: "2 brokers" once done, the plain step name before. */
  pill: string;
  state: StepState;
  /** Started but not finished — some brokers done, others not. */
  partial: boolean;
  /** Where clicking the step takes you. */
  to: string;
  /** The full sentence, for the tooltip and screen readers. */
  hint: string;
};

// A contract a bordereau can run against. Null is a contract raised before
// the lifecycle existed — those were put straight in force.
const LIVE = new Set(["active"]);
const isLive = (lifecycle: string | null | undefined) => lifecycle == null || LIVE.has(lifecycle);
const hasLiveContract = (b: HierarchyBroker) => b.contracts.some(c => isLive(c.lifecycle));
// Contracts that have ended no longer count as work in progress.
const ENDED = new Set(["expired", "terminated", "superseded"]);
const openContract = (b: HierarchyBroker) => b.contracts.find(c => !ENDED.has(c.lifecycle ?? ""));

/** The Configure Program flow for an existing programme, opened at one step
 *  (1 programme · 2 brokers · 3 contracts · 4 bordereau setup), so the stepper
 *  across its top is on screen whichever stage you arrive at. */
export const STAGE: Record<Step["key"], number> = { programme: 1, brokers: 2, contract: 3, setup: 4 };
export const flowUrl = (programId: number, key: Step["key"]) =>
  `/programs/${programId}/setup?stage=${STAGE[key]}`;

export function programmeSteps(p: HierarchyProgramme): Step[] {
  const brokers = p.brokers.filter(b => b.link_status === "active");
  const withContract = brokers.filter(hasLiveContract);
  const withSetup = withContract.filter(b => b.setup_status === "active");
  const programmeUrl = `/programs/${p.id}/brokers`;

  // Contract: the first broker still without a live contract decides where
  // the step leads — their contract in progress, or a new one pre-filled.
  const needsContract = brokers.find(b => !hasLiveContract(b));
  const pending = needsContract ? openContract(needsContract) : undefined;
  const contractTo = !needsContract
    ? `/contracts?program_id=${p.id}`
    : pending
      ? `/contracts/${pending.id}`
      : `/contracts/new?program_id=${p.id}&broker_party_id=${needsContract.id}`;

  // Setup: the first broker with a live contract but no setup in use.
  const needsSetup = withContract.find(b => b.setup_status !== "active");
  const setupTo = needsSetup
    ? `/direct/setup?program_id=${p.id}&broker_party_id=${needsSetup.id}`
    : `/direct/setups?program_id=${p.id}`;

  const brokersDone = brokers.length > 0;
  const contractDone = brokersDone && withContract.length === brokers.length;
  // Every broker who HAS a live contract also has a setup. The step is only done
  // once that is true AND nobody is still waiting on a contract.
  const setupsComplete = withSetup.length === withContract.length;
  const setupDone = contractDone && setupsComplete;
  const done = [true, brokersDone, contractDone, setupDone];
  // The first unfinished step is the current one; everything after it waits.
  const firstOpen = done.indexOf(false);
  const state = (i: number): StepState =>
    done[i] ? "done" : i === firstOpen ? "current" : "todo";

  const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
  return [
    {
      key: "programme", label: "Programme", pill: "Programme", partial: false,
      state: state(0), to: programmeUrl, detail: "configured",
      hint: `${p.name} is configured.`,
    },
    {
      key: "brokers", label: "Brokers", partial: false,
      pill: brokersDone ? plural(brokers.length, "broker") : "Brokers",
      state: state(1), to: programmeUrl,
      detail: brokersDone ? plural(brokers.length, "broker") : "none yet",
      hint: brokersDone
        ? `${plural(brokers.length, "broker")} on this programme.`
        : "No broker on this programme yet — add one so it can hold a contract.",
    },
    {
      key: "contract", label: "Contract",
      partial: brokersDone && !contractDone && withContract.length > 0,
      pill: contractDone ? plural(withContract.length, "contract")
        : withContract.length > 0 ? `${withContract.length} of ${brokers.length} contracts` : "Contract",
      state: state(2), to: contractTo,
      detail: !brokersDone ? "needs a broker"
        : contractDone ? "all live"
        : `${withContract.length} of ${brokers.length} live`,
      hint: !brokersDone ? "A contract needs a broker on the programme first."
        : contractDone ? "Every broker here has a live contract."
        : pending ? `${needsContract!.legal_name}'s contract isn't live yet.`
        : `${needsContract!.legal_name} has no contract yet.`,
    },
    {
      key: "setup", label: "Setup",
      partial: contractDone && withSetup.length > 0 && !setupDone,
      // A fraction only while this is the step being worked on; before that it
      // would read as finished ("1 of 1") while a contract is still missing.
      pill: setupDone ? "Setup ready"
        : contractDone && withSetup.length > 0 ? `${withSetup.length} of ${withContract.length} setups` : "Setup",
      state: state(3), to: setupTo,
      detail: withContract.length === 0 ? "needs a contract"
        : setupDone ? "all ready"
        : `${withSetup.length} of ${withContract.length} ready`,
      hint: withContract.length === 0 ? "A bordereau setup is built from a live contract."
        : setupDone ? "Every broker has a setup in use — files can be checked."
        : needsSetup ? `${needsSetup.legal_name} has no bordereau setup yet, so their file can't be checked.`
        : "Every broker with a live contract has a setup; the rest still need a contract first.",
    },
  ];
}

/** The one thing to do next for this programme: a button label, where it goes,
 *  and the sentence that explains it. Null once the programme is complete. */
export function nextStep(p: HierarchyProgramme): { label: string; to: string; hint: string } | null {
  const steps = programmeSteps(p);
  const cur = steps.find(s => s.state === "current");
  if (!cur) return null;
  const draftSetup = p.brokers.some(b => b.link_status === "active" && b.setup_status === "draft");
  const label =
    cur.key === "brokers" ? "Add a broker"
    : cur.key === "contract" ? (cur.to.startsWith("/contracts/new") ? "Add a contract" : "Open the contract")
    : cur.key === "setup" ? (draftSetup ? "Finish the setup" : "Bordereau Setup")
    : "Open";
  return { label, to: flowUrl(p.id, cur.key), hint: cur.hint };
}

/** One plain sentence for the "What's missing" column. Names the broker when
 *  there is only one; counts them when there are several. Null once complete. */
export function whatsMissing(p: HierarchyProgramme): string | null {
  const brokers = p.brokers.filter(b => b.link_status === "active");
  if (brokers.length === 0) return "No broker on it yet";
  const noContract = brokers.filter(b => !hasLiveContract(b));
  if (noContract.length > 0) {
    const inProgress = noContract.filter(b => openContract(b));
    if (noContract.length === 1) {
      return inProgress.length
        ? `${noContract[0].legal_name}'s contract isn't signed yet`
        : `${noContract[0].legal_name} has no contract yet`;
    }
    return `${noContract.length} brokers have no signed contract yet`;
  }
  const noSetup = brokers.filter(b => b.setup_status !== "active");
  if (noSetup.length === 1) return `${noSetup[0].legal_name} has no setup, so their file can't be checked`;
  if (noSetup.length > 1) return `${noSetup.length} brokers have no setup, so their files can't be checked`;
  return null;
}

/** True once every step is done — the programme can receive files. */
export const programmeReady = (p: HierarchyProgramme) =>
  programmeSteps(p).every(s => s.state === "done");

export function ProgrammeStepper({ programme }: { programme: HierarchyProgramme }) {
  const nav = useNavigate();
  const steps = programmeSteps(programme);
  return (
    <ol className="flex flex-wrap items-center gap-1" aria-label={`How far ${programme.name} has got`}>
      {steps.map((s, i) => {
        // Green once done. Red for the step that is stopping files once brokers
        // are already on the programme (contract or setup missing). Grey for
        // steps not reached — including "Brokers" on a brand-new programme,
        // which is a start, not a problem.
        const blocking = s.state === "current" && (s.partial || i >= 2);
        const tone = s.state === "done" ? "bg-success/10 text-success"
          : blocking ? "bg-danger/10 text-danger"
          : "bg-surface-2 text-ink-soft";
        return (
          <li key={s.key} className="flex items-center gap-1">
            {i > 0 && <ArrowRight size={12} className="shrink-0 text-ink-soft" aria-hidden />}
            <button
              type="button"
              title={s.hint}
              aria-label={`${s.label}: ${s.hint}`}
              aria-current={s.state === "current" ? "step" : undefined}
              // The row goes to where the programme is stuck; a step goes to itself.
              onClick={e => { e.stopPropagation(); nav(flowUrl(programme.id, s.key)); }}
              className={`whitespace-nowrap rounded-md px-2 py-0.5 text-[11.5px] font-semibold ${tone}
                          hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-navy`}
            >
              {s.pill}
            </button>
          </li>
        );
      })}
    </ol>
  );
}
