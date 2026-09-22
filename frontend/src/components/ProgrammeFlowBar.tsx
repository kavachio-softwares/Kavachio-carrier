// The Configure Program stepper, shown on the screens the flow hands off to —
// Create a Contract (step 3) and Bordereau Setup (step 4) — so leaving the
// Configure Program screen does not mean losing sight of where the programme
// is. Each step leads back into the flow at that step.
//
// Shown only when the screen was opened FROM the flow (the link carries
// ?flow=1): the same screens reached from anywhere else are not part of a
// programme's set-up and get no stepper.
import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { getHierarchy, type HierarchyProgramme } from "../api/hierarchy";
import { FlowStepper, type FlowStep } from "./FlowStepper";

const ENDED = new Set(["expired", "terminated", "superseded"]);

export const FLOW_PARAM = "flow";

/** `children` is drawn inside the stepper, under the step this screen is —
 *  Create a Contract puts its own four steps there. */
export function ProgrammeFlowBar({ programId, at, children }: {
  programId: number | null; at: 3 | 4; children?: ReactNode;
}) {
  const nav = useNavigate();
  const [prog, setProg] = useState<HierarchyProgramme | null>(null);

  useEffect(() => {
    if (!programId) { setProg(null); return; }
    let stale = false;
    getHierarchy()
      .then(h => { if (!stale) setProg(h.programmes.find(p => p.id === programId) ?? null); })
      .catch(() => {});
    return () => { stale = true; };
  }, [programId]);

  // Until the programme is in (or if it cannot be found), whatever was nested
  // is still shown on its own — the contract's steps must never go missing.
  if (!programId || !prog) return children ? <div className="mb-5">{children}</div> : null;

  const brokers = prog.brokers.filter(b => b.link_status === "active");
  const withContract = brokers.filter(b => b.contracts.some(c => !ENDED.has(c.lifecycle ?? "")));
  const withSetup = withContract.filter(b => b.setup_status === "active");
  const brokersDone = brokers.length > 0;
  const contractsDone = brokersDone && withContract.length === brokers.length;
  const setupDone = contractsDone && withSetup.length === withContract.length;
  const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const go = (stage: number) => () => nav(`/programs/${programId}/setup?stage=${stage}`);

  const steps: FlowStep[] = [
    { key: "programme", label: "Configure program", sub: prog.name,
      state: "done", enabled: true, onClick: go(1) },
    { key: "brokers", label: "Assign brokers",
      sub: brokersDone ? `${plural(brokers.length, "broker")} assigned` : "No broker yet",
      state: brokersDone ? "done" : "todo", enabled: true, onClick: go(2) },
    { key: "contracts", label: "Contracts",
      sub: `${withContract.length} of ${brokers.length} brokers have a contract`,
      state: contractsDone ? "done" : "todo", enabled: brokersDone, onClick: go(3) },
    { key: "setup", label: "Bordereau setup",
      sub: `${withSetup.length} of ${withContract.length} setups ready`,
      state: setupDone ? "done" : "todo", enabled: withContract.length > 0, onClick: go(4) },
  ];
  // This screen IS the step it stands for: highlighted, and going "to" it from
  // here would leave the work on screen, so it is not a way out.
  steps.forEach((s, i) => {
    s.open = i + 1 === at;
    if (s.open) {
      if (s.state !== "done") s.state = "current";
      s.enabled = true;
      s.onClick = () => {};
    }
  });

  return <FlowStepper steps={steps} label={`${prog.name} set-up steps`}>{children}</FlowStepper>;
}
