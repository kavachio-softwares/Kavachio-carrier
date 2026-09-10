/**
 * Which carrier a broker is currently working on.
 *
 * A broker is put on programmes by SEVERAL carriers, and until now every
 * broker screen merged them: one dashboard counting five contracts waiting
 * across three companies, one contract list, one pile of bordereau contracts
 * labelled with the carrier's name in brackets. That answers "what do I have"
 * and never "what does Northgate need from me", which is the question somebody
 * actually sits down with — and it makes it easy to process a file against the
 * wrong carrier's contract, because the two look alike in a list.
 *
 * So the seat carries a selection, and every broker screen reads it.
 *
 * WHY LOCAL AND NOT ON THE TOKEN. It is a view preference, not a permission.
 * The server already scopes every broker read to the programmes that broker is
 * actually on (`_links`), and passing a carrier_id only NARROWS that — an id
 * the broker is not linked to yields nothing rather than more. So a tampered
 * value cannot widen what they see, and keeping it out of the token means
 * switching carrier does not mean re-issuing credentials.
 *
 * `null` means "all carriers", which stays the default: a broker on one
 * carrier should never have to choose it, and one who wants the merged view
 * can go back to it.
 */
import { useEffect, useState } from "react";

const KEY = "kav.broker.carrier";

/** Broadcast so every mounted screen re-reads at once. Without it the sidebar
 *  switches and the page behind it keeps showing the previous carrier. */
export const BROKER_CARRIER_EVENT = "kav:broker-carrier";

export function getBrokerCarrierId(): number | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    // Private mode, or storage blocked. "All carriers" is the right fallback:
    // it is what the screens did before there was a selection at all.
    return null;
  }
}

export function setBrokerCarrierId(id: number | null): void {
  try {
    if (id == null) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, String(id));
  } catch {
    /* nothing to do — the event below still switches this session */
  }
  window.dispatchEvent(new CustomEvent(BROKER_CARRIER_EVENT));
}

/** Clear it on sign-out, so the next person in this browser does not start on
 *  somebody else's carrier. */
export function clearBrokerCarrier(): void {
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}

/** The current selection, kept live. Every broker screen uses this rather than
 *  reading storage once at mount, so switching carrier updates the page you
 *  are already looking at instead of on the next navigation. */
export function useBrokerCarrierId(): number | null {
  const [id, setId] = useState<number | null>(getBrokerCarrierId);
  useEffect(() => {
    const sync = () => setId(getBrokerCarrierId());
    window.addEventListener(BROKER_CARRIER_EVENT, sync);
    // Another tab switching carrier counts too — two tabs silently disagreeing
    // about which carrier you are on is exactly how a file goes to the wrong one.
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(BROKER_CARRIER_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return id;
}
