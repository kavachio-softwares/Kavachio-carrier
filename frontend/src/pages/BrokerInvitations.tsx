/**
 * Invitations — where "Join now" lands, presented like the sign-in page.
 *
 * Deliberately NOT inside the app shell. Somebody arriving from an email has
 * one thing to do and no reason to be looking at a sidebar of screens that are
 * mostly about a carrier they have not joined yet. So it is the same dark,
 * centred, single-card page as sign-in: brand, one question, two answers.
 *
 * IT IS NOT A TOKENED LINK. The id in the URL opens nothing on its own — the
 * server only returns invitations addressed to whoever is signed in, so a
 * forwarded email is useless to anybody else and an expired one is not a
 * concept. Signing in IS the authentication; the link is only a destination,
 * and RequireAuth carries it through the login form for them.
 *
 * Accepting is what creates the relationship — until then the carrier has an
 * unanswered invitation and cannot put this broker on anything.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Check, X } from "lucide-react";
import {
  getBrokerInvitations, acceptBrokerInvitation, declineBrokerInvitation,
  type BrokerInvitation,
} from "../api/broker";
import { setBrokerCarrierId } from "../brokerCarrier";
import { fmtDate } from "../utils/date";
import KavachioLogo from "../components/KavachioLogo";
import { isBrokerSeat } from "../auth";
import { landingPath } from "../access";

export default function BrokerInvitations() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const wanted = Number(params.get("id")) || null;

  // This screen is outside the app shell, so RequireAccess does not gate it —
  // the seat check lives here instead. A carrier admin following this URL
  // would otherwise meet a 403 from an endpoint that is not theirs, which
  // reads as breakage rather than as "not your screen".
  const notABroker = !isBrokerSeat();

  const [rows, setRows] = useState<BrokerInvitation[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [joined, setJoined] = useState<string | null>(null);
  const [declined, setDeclined] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    if (notABroker) return;
    getBrokerInvitations().then(setRows)
      .catch(() => setErr("Could not load your invitations."));
  }, [notABroker]);
  useEffect(load, [load]);

  useEffect(() => {
    if (notABroker) nav(landingPath(), { replace: true });
  }, [notABroker, nav]);

  // The one they clicked through for, or the oldest still waiting. One card,
  // one decision — a list of invitations is a screen for a different mood.
  const current = rows?.find(r => r.id === wanted) ?? rows?.[0] ?? null;
  const others = (rows?.length ?? 0) - (current ? 1 : 0);

  async function answer(accept: boolean) {
    if (!current) return;
    setBusy(true); setErr("");
    try {
      if (accept) {
        const r = await acceptBrokerInvitation(current.id);
        if (r?.carrier_id) setBrokerCarrierId(r.carrier_id);
        setJoined(r?.carrier ?? current.carrier);
      } else {
        await declineBrokerInvitation(current.id);
        setDeclined(true);
      }
      load();
    } catch {
      setErr("That did not go through. Try again.");
    } finally { setBusy(false); }
  }

  const priBtn =
    "mt-2 flex w-full items-center justify-center gap-2 rounded-lg bg-[#077282] py-3 " +
    "text-sm font-semibold text-white transition hover:bg-[#065E6B] " +
    "disabled:cursor-not-allowed disabled:opacity-60";
  const priShadow = {
    boxShadow: "0 1px 2px rgba(7,114,130,.3), 0 6px 16px -6px rgba(7,114,130,.5)",
  } as const;
  const secBtn =
    "mt-2 flex w-full items-center justify-center gap-2 rounded-lg border py-3 " +
    "text-sm font-semibold transition hover:bg-[#F6F7F9] disabled:opacity-60";

  return (
    <div
      className="relative min-h-screen grid place-items-center overflow-hidden"
      style={{ background: "radial-gradient(130% 100% at 50% -20%, #1E2A42 0%, #0B0F18 65%)" }}
    >
      {/* The same faint grid as sign-in, so the two read as one doorway. */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          backgroundImage:
            "linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px)," +
            "linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px)",
          backgroundSize: "46px 46px",
          maskImage: "radial-gradient(70% 70% at 50% 30%, #000, transparent)",
          WebkitMaskImage: "radial-gradient(70% 70% at 50% 30%, #000, transparent)",
        }}
      />

      <div
        className="relative w-[392px] rounded-[18px] bg-white px-9 py-10 border"
        style={{ borderColor: "rgba(255,255,255,.5)",
                 boxShadow: "0 30px 70px -18px rgba(8,12,22,.55)" }}
      >
        <div className="flex items-center gap-3 mb-2">
          <KavachioLogo size={38}
                        style={{ filter: "drop-shadow(0 4px 10px rgba(7,114,130,.45))" }} />
          <h1 className="m-0 text-[23px] font-bold tracking-[.2px]"
              style={{ fontFamily: "'Montserrat', sans-serif", color: "#0E1320" }}>
            Kavachio
          </h1>
        </div>

        {err && <div className="mb-3 text-sm" style={{ color: "#D32F45" }}>{err}</div>}

        {joined ? (
          <>
            <p className="mb-1 mt-5 text-[15px] font-semibold" style={{ color: "#0E1320" }}>
              You are now working with {joined}.
            </p>
            <p className="mb-5 text-sm" style={{ color: "#566071" }}>
              They can put you on their programmes from here. It is selected as
              the carrier you are working on — switch any time from the sidebar.
            </p>
            <button className={priBtn} style={priShadow} onClick={() => nav("/broker")}>
              Go to your dashboard
            </button>
            {others > 0 && (
              <button className={secBtn} style={{ borderColor: "#D2D7E0", color: "#0E1320" }}
                      onClick={() => { setJoined(null); setDeclined(false); }}>
                {others} more invitation{others === 1 ? "" : "s"} waiting
              </button>
            )}
          </>
        ) : declined ? (
          <>
            <p className="mb-1 mt-5 text-[15px] font-semibold" style={{ color: "#0E1320" }}>
              Declined.
            </p>
            <p className="mb-5 text-sm" style={{ color: "#566071" }}>
              Nothing was shared and nothing changed. They can invite you again.
            </p>
            <button className={priBtn} style={priShadow} onClick={() => nav("/broker")}>
              Go to your dashboard
            </button>
          </>
        ) : rows === null ? (
          <p className="mt-5 text-sm" style={{ color: "#566071" }}>Loading…</p>
        ) : !current ? (
          <>
            <p className="mb-1 mt-5 text-[15px] font-semibold" style={{ color: "#0E1320" }}>
              Nothing waiting
            </p>
            <p className="mb-5 text-sm" style={{ color: "#566071" }}>
              You have no invitations to answer. If a carrier invites you, it
              arrives by email and appears here.
            </p>
            <button className={priBtn} style={priShadow} onClick={() => nav("/broker")}>
              Go to your dashboard
            </button>
          </>
        ) : (
          <>
            <p className="mb-5 mt-1 text-sm" style={{ color: "#566071" }}>
              An invitation is waiting for you
            </p>
            <p className="mb-1 text-[17px] font-semibold" style={{ color: "#0E1320" }}>
              {current.carrier}
            </p>
            <p className="mb-4 text-sm" style={{ color: "#566071" }}>
              {current.programme
                ? <>has invited you on to <b>{current.programme}</b>.</>
                : <>would like you to produce business for them.</>}
              {current.invited_at && <> Invited {fmtDate(current.invited_at)}.</>}
            </p>

            <div className="mb-5 rounded-lg px-3 py-2.5 text-[12.5px]"
                 style={{ background: "#F6F7F9", color: "#566071" }}>
              You keep the login you have. Joining lets them put you on their
              programmes — nothing about your other carriers is shown to them,
              or theirs to you.
            </div>

            <button className={priBtn} style={priShadow}
                    disabled={busy} onClick={() => answer(true)}>
              <Check size={15} /> {busy ? "Joining…" : `Join ${current.carrier}`}
            </button>
            <button className={secBtn}
                    style={{ borderColor: "#D2D7E0", color: "#566071" }}
                    disabled={busy} onClick={() => answer(false)}>
              <X size={15} /> Decline
            </button>

            {others > 0 && (
              <p className="mt-4 text-center text-[12px]" style={{ color: "#8B93A2" }}>
                {others} other invitation{others === 1 ? "" : "s"} waiting after this one.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
