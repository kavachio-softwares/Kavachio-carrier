/**
 * Has this broker actually come on board?
 *
 * The answer used to live one level away, on the admin's own login, and people
 * kept looking for it on the broker and not finding it. It is now a column on
 * the broker row — derived by the database from their people, so it can never
 * disagree with what the Users list shows.
 *
 * Each state says what to DO about it, because that is the only reason anyone
 * looks: an invitation nobody opened needs chasing, a broker with no login at
 * all needs inviting.
 */
export type OnboardingStatus = "not_invited" | "invited" | "active" | "suspended";

const LOOK: Record<OnboardingStatus, { label: string; cls: string; title: string }> = {
  not_invited: {
    label: "Not invited",
    cls: "bg-surface-2 text-ink-muted",
    title: "This broker is on your list but nobody there has a login yet.",
  },
  invited: {
    label: "Invited",
    cls: "bg-warn/10 text-warn",
    title: "Their admin was invited and hasn't used the link yet — worth chasing.",
  },
  active: {
    label: "Active",
    cls: "bg-success/10 text-success",
    title: "Someone there has set a password and signed in.",
  },
  suspended: {
    label: "Suspended",
    cls: "bg-danger/10 text-danger",
    title: "The company itself was switched off. Their history stays readable.",
  },
};

export function OnboardingBadge({ status }: { status: string | null | undefined }) {
  const look = status ? LOOK[status as OnboardingStatus] : undefined;
  // A party that isn't a producer carries no onboarding state at all, and an
  // unknown value is a bug worth seeing rather than dressing up as a status.
  if (!look) return <span className="text-ink-soft">—</span>;
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${look.cls}`}
      title={look.title}
    >
      {look.label}
    </span>
  );
}
