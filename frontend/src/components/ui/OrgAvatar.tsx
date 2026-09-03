/**
 * An organisation's initials on a coloured disc.
 *
 * A list of companies reads as a wall of identical grey text: every row the
 * same shape, so finding the one you came for means reading each line. A mark
 * that differs per name gives the eye something to aim at, and lets a row be
 * recognised before it is read.
 *
 * The colour is DERIVED FROM THE NAME, never random and never stored: the same
 * organisation is the same colour on every screen and after every reload, which
 * is the only thing that makes it worth having. The palette is a fixed set of
 * tints that all carry legible dark text, so no name can produce an unreadable
 * disc.
 */
import { initials } from "../../branding";

const TINTS = [
  "bg-teal-100 text-teal-800",
  "bg-sky-100 text-sky-800",
  "bg-indigo-100 text-indigo-800",
  "bg-violet-100 text-violet-800",
  "bg-rose-100 text-rose-800",
  "bg-amber-100 text-amber-800",
  "bg-emerald-100 text-emerald-800",
];

/** Stable per name: sum of code points, so it never moves between renders. */
function tintFor(name: string): string {
  let n = 0;
  for (let i = 0; i < name.length; i++) n = (n + name.charCodeAt(i)) % 9973;
  return TINTS[n % TINTS.length];
}

const SIZE = {
  sm: "h-7 w-7 text-[10.5px]",
  md: "h-9 w-9 text-[12px]",
};

export function OrgAvatar({ name, size = "md", muted }: {
  name: string;
  size?: "sm" | "md";
  /** An organisation taken off a programme is drawn flat — it should not carry
   *  the same visual weight as one that is live. */
  muted?: boolean;
}) {
  return (
    <span
      aria-hidden
      className={`inline-flex shrink-0 items-center justify-center rounded-lg font-semibold
        ${SIZE[size]} ${muted ? "bg-surface-2 text-ink-soft" : tintFor(name)}`}
    >
      {initials(name)}
    </span>
  );
}

export default OrgAvatar;
