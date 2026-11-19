/**
 * Reinsurance setups are listed with the broker setups on Bordereau Setup
 * (Setup type: Reinsurance bordereau). This keeps the old address working and
 * the status names the setup pages share.
 */
import { Navigate } from "react-router-dom";

export const SETUP_STATUS: Record<string, { label: string; cls: string }> = {
  active: { label: "Active", cls: "b-ok" },
  pending_approval: { label: "Awaiting approval", cls: "b-info" },
  draft: { label: "Draft", cls: "b-warn" },
  none: { label: "Not set up", cls: "b-mut" },
};

/** The old address: the reinsurance setups now live in Bordereau Setup. */
export default function ReinsuranceSetups() {
  return <Navigate to="/direct/setups?tab=reinsurance" replace />;
}
