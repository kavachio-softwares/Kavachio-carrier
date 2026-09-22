import { fmtShortDateTime } from "../utils/date";

/** "by Ines Duarte · 22 Sep, 10:14" under a decided exception.
 *
 *  The name is the server's, already worded for whoever is looking: a broker
 *  user reads as their broker company to the carrier, and as the person to
 *  their own team. Nothing is shown for a decision with no recorded decider. */
export default function DecidedBy({ by, at }: { by?: string | null; at?: string | null }) {
  if (!by) return null;
  return (
    <div className="mt-1 text-[10px] text-ink-soft">
      by {by}{at ? ` · ${fmtShortDateTime(at)}` : ""}
    </div>
  );
}
