/**
 * How far one broker has come on ONE programme.
 *
 * Of the brokers on a programme, which are actually ready to send you files,
 * and what is each one waiting on. A programme with three brokers can be live
 * for one and stalled for the other two, so this is asked per broker — a
 * single programme-level answer cannot show that, which is why there is no
 * longer one.
 *
 * The four steps, each blocked by the one before it:
 *
 *   1. Invited        somebody there was given a login
 *   2. Signed in      they used it — until then nobody at the broker can act
 *   3. On programme   the program_broker link; the grant to produce here
 *   4. Contract       a contract with this broker, on this programme
 *
 * Steps 1 and 2 come from `onboarding_status`, which the DATABASE derives from
 * the broker's people (see OnboardingBadge), so it cannot disagree with the
 * Users list. Step 3 is the link this screen manages. Step 4 counts the
 * contracts already listed on the card.
 *
 * Deliberately not shown once every step is done: a broker that is ready needs
 * no commentary, and four green ticks on every card would bury the one that is
 * actually stuck.
 */
import { Check } from "lucide-react";
import type { OnboardingStatus } from "./OnboardingBadge";

type Props = {
  /** not_invited | invited | active | suspended — from the broker row. */
  onboardingStatus: string | null | undefined;
  /** Is their program_broker link still active? */
  onProgramme: boolean;
  /** Contracts with this broker ON THIS PROGRAMME. */
  contractCount: number;
};

type Step = { label: string; done: boolean; waiting: string };

export function brokerOnboardingSteps(
  { onboardingStatus, onProgramme, contractCount }: Props,
): Step[] {
  const st = (onboardingStatus ?? "not_invited") as OnboardingStatus;
  return [
    {
      label: "Invited",
      done: st === "invited" || st === "active",
      waiting: "Nobody there has a login yet",
    },
    {
      label: "Signed in",
      done: st === "active",
      waiting: "Their admin hasn't used the invite link — worth chasing",
    },
    {
      label: "On programme",
      done: onProgramme,
      waiting: "Taken off this programme, so they produce nothing new",
    },
    {
      label: "Contract",
      done: contractCount > 0,
      waiting: "No contract with them on this programme yet",
    },
  ];
}

export function BrokerOnboarding(props: Props) {
  // Suspended is not a stage of onboarding — it is the company switched off.
  // Showing it as "stuck at step 1" would misdescribe it, so say what it is.
  if ((props.onboardingStatus ?? "") === "suspended") {
    return (
      <p className="mb-3 text-xs text-danger">
        This broker is suspended. Their history stays readable, but nobody there
        can sign in.
      </p>
    );
  }

  const steps = brokerOnboardingSteps(props);
  const blocked = steps.find(s => !s.done);
  if (!blocked) return null;          // ready — nothing worth saying

  return (
    <div className="mb-3 rounded border border-border bg-surface-2/50 px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {steps.map(s => (
          <span
            key={s.label}
            className={`inline-flex items-center gap-1 text-xs ${
              s.done ? "text-success"
                : s === blocked ? "font-medium text-navy" : "text-ink-soft"}`}
          >
            {s.done
              ? <Check size={11} aria-hidden="true" />
              : <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    s === blocked ? "bg-navy" : "bg-border"}`}
                  aria-hidden="true"
                />}
            {s.label}
          </span>
        ))}
      </div>
      <p className="mt-1 text-xs text-ink-muted">{blocked.waiting}</p>
    </div>
  );
}
