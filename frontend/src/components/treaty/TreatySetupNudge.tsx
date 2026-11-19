/**
 * "Next: set up this treaty's reinsurance bordereau" — the treaty's version
 * of the broker contract's "Set up bordereau" button, shown once the treaty is
 * active. Says where the setup stands, so an active one is not offered again
 * as a first step.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { getSetup, type SetupRecord } from "../../api/ceded";

export default function TreatySetupNudge({ contractId, compact = false }: {
  contractId: number;
  /** Just the button — for a row that already says the treaty is active. */
  compact?: boolean;
}) {
  const [rec, setRec] = useState<SetupRecord | null>(null);
  useEffect(() => {
    getSetup(contractId).then(setRec).catch(() => setRec(null));
  }, [contractId]);
  if (!rec) return null;

  const status = rec.setup?.status ?? null;
  const to = `/reinsurance/setups/${contractId}`;
  const label = status === "active" ? "Open bordereau setup"
    : status ? "Finish bordereau setup" : "Set up bordereau";
  const button = (
    <Link className={`btn ${status === "active" ? "" : "pri"}`} to={to}>
      {label} <ArrowRight size={13} />
    </Link>
  );
  if (compact) return button;

  return (
    <div className="note" style={{
      marginTop: 12, display: "flex", gap: 14, flexWrap: "wrap",
      alignItems: "center", justifyContent: "space-between",
      background: "var(--p-primary-soft)", borderColor: "#BFE0E4",
    }}>
      <div>
        <b style={{ color: "var(--p-primary-h)" }}>
          {status === "active"
            ? "Treaty is active and its reinsurance bordereau is set up."
            : "Treaty is active. Next: set up its reinsurance bordereau."}
        </b>
        <div style={{ marginTop: 4 }}>
          {status === "active"
            ? "A statement is prepared for each reinsurer every period from your brokers' bordereaux."
            : status === "pending_approval"
              ? "The setup is waiting for your carrier admin to approve it."
              : "Which programmes feed it, how often, the file layout and who receives it — "
                + "nothing is prepared for the reinsurers until it is active."}
        </div>
      </div>
      {button}
    </div>
  );
}
