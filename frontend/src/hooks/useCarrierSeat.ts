// ---------------------------------------------------------------------------
// Which of the two CARRIER seats the signed-in person holds.
//
// Everyone at a carrier has the same `carrier_admin` DB role; what separates
// the carrier admin from a carrier user is the organisation's owner pointer.
// Both do the carrier's work — programmes, contracts, Process Bordereau, and
// inviting broker companies. The seat decides two things only, and the API
// enforces both (_assert_is_carrier_admin / _brokers_in_reach):
//
//   admin  the carrier admin — everything a carrier user does, plus adding and
//          removing carrier users; sees and manages EVERY broker company the
//          carrier works with, whoever invited it.
//   user   a carrier user — invites broker companies and manages the ones
//          THEY invited; never adds carrier users.
//   both   Kavachio staff, or an organisation with no owner recorded. The
//          server lets these through either way, so the screens do too.
//   null   not a carrier seat, or the organisation has not loaded yet. Show
//          neither action rather than guess and flash the wrong one.
//
// Read through the store so a screen re-renders when the sidebar's fetch of
// the organisation lands (or after ownership is transferred).
// ---------------------------------------------------------------------------

import { useSelector } from "react-redux";
import type { RootState } from "../store";
import { normalizeRole } from "../auth";

export type CarrierSeat = "admin" | "user" | "both" | null;

export function useCarrierSeat(): CarrierSeat {
  const user = useSelector((s: RootState) => s.auth.user);
  const brand = useSelector((s: RootState) => s.auth.tenantBrand);
  return seatOf(user, brand);
}

/** The same answer outside React (route guards read it through access.ts). */
export function seatOf(user: RootState["auth"]["user"],
                       brand: RootState["auth"]["tenantBrand"]): CarrierSeat {
  const role = user ? normalizeRole(user.role) : null;
  if (role === "kavachio_admin") return "both";
  if (role !== "carrier_admin") return null;
  if (!brand || brand.mga !== user?.mga || brand.owner_user_id === undefined) return null;
  if (brand.owner_user_id === null) return "both";
  return brand.owner_user_id === user?.id ? "admin" : "user";
}

/** May add and remove carrier users. */
export const addsCarrierUsers = (seat: CarrierSeat) => seat === "admin" || seat === "both";
/** May invite broker companies and manage their admins — every carrier seat. */
export const invitesBrokers = (seat: CarrierSeat) => seat !== null;
/** Reaches every broker company the carrier works with, not only the ones
 *  this person invited (resend / withdraw anyone's invitation). */
export const seesAllBrokers = (seat: CarrierSeat) => seat === "admin" || seat === "both";
