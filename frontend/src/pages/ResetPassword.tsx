import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import KavachioLogo from "../components/KavachioLogo";
import { PasswordInput } from "../components/ui/PasswordInput";

// Password policy — kept in sync with the backend (POST /auth/reset).
const RULES: { label: string; test: (p: string) => boolean }[] = [
  { label: "At least 8 characters", test: p => p.length >= 8 },
  { label: "One uppercase letter (A–Z)", test: p => /[A-Z]/.test(p) },
  { label: "One lowercase letter (a–z)", test: p => /[a-z]/.test(p) },
  { label: "One number (0–9)", test: p => /[0-9]/.test(p) },
  { label: "One special character", test: p => /[^A-Za-z0-9]/.test(p) },
];

export default function ResetPassword() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  // null = still checking the link; true = valid; false = invalid/expired
  const [linkValid, setLinkValid] = useState<boolean | null>(null);
  // true = the validate request itself failed (network) — kept distinct from an
  // invalid token so a connectivity blip isn't mislabeled as "expired".
  const [checkFailed, setCheckFailed] = useState(false);
  // Which flow issued this token — reported by the backend from the account's
  // real status, not guessed here — so a first-time invite reads as
  // "complete your onboarding" rather than a plain password reset.
  const [mode, setMode] = useState<"invite" | "reset">("reset");
  const isInvite = mode === "invite";

  // Check the token on load so an expired/invalid link shows a message here,
  // not the set-password form.
  useEffect(() => {
    if (!token) return;
    setCheckFailed(false); setLinkValid(null);
    api.get("/auth/reset/validate", { params: { token } })
      .then(r => { setLinkValid(!!r.data?.valid); setMode(r.data?.mode === "invite" ? "invite" : "reset"); })
      .catch(() => setCheckFailed(true));
  }, [token]);

  const met = RULES.map(r => r.test(password));
  const allMet = met.every(Boolean);
  const matches = password.length > 0 && password === confirm;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!allMet) { setErr("Password doesn't meet all the requirements below."); return; }
    if (!matches) { setErr("Passwords don't match."); return; }
    setBusy(true);
    try {
      await api.post("/auth/reset", { token, password });
      setDone(true);
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not reset your password.");
    } finally { setBusy(false); }
  }

  const inputCls =
    "w-full rounded-lg border px-[13px] py-3 text-[13.5px] outline-none transition " +
    "focus:border-[#077282] focus:ring-[3px] focus:ring-[#E1F1F3] placeholder:text-[#8B93A2]";
  const inputStyle = { borderColor: "#D2D7E0", color: "#0E1320" } as const;
  const priBtn =
    "mt-2 flex w-full items-center justify-center rounded-lg bg-[#077282] py-3 text-sm " +
    "font-semibold text-white transition hover:bg-[#065E6B] disabled:cursor-not-allowed disabled:opacity-60";
  const priShadow = { boxShadow: "0 1px 2px rgba(7,114,130,.3), 0 6px 16px -6px rgba(7,114,130,.5)" } as const;

  return (
    <div
      className="relative min-h-screen grid place-items-center overflow-hidden"
      style={{ background: "radial-gradient(130% 100% at 50% -20%, #1E2A42 0%, #0B0F18 65%)" }}
    >
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
        style={{ borderColor: "rgba(255,255,255,.5)", boxShadow: "0 30px 70px -18px rgba(8,12,22,.55)" }}
      >
        <div className="flex items-center gap-3 mb-2">
          <KavachioLogo size={34} style={{ filter: "drop-shadow(0 4px 10px rgba(7,114,130,.45))" }} />
          <h1 className="m-0 text-[23px] font-bold tracking-[.2px]"
            style={{ fontFamily: "'Montserrat', sans-serif", color: "#0E1320" }}>
            Kavachio
          </h1>
        </div>

        {done ? (
          <div className="text-center">
            <div className="mx-auto mt-1.5 mb-3.5 grid h-[52px] w-[52px] place-items-center rounded-full"
              style={{ background: "#E4F5EC", color: "#0E9F6E" }}>
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M5 12l5 5L20 7" />
              </svg>
            </div>
            <h3 className="m-0 mb-1.5 text-[18px] font-semibold"
              style={{ fontFamily: "'Montserrat', sans-serif", color: "#0E1320" }}>
              {isInvite ? "Onboarding Complete" : "Password Updated"}
            </h3>
            <p className="mb-[18px] text-sm" style={{ color: "#566071" }}>
              {isInvite
                ? "Your account is now active. You can sign in with your new password."
                : "Your password has been changed. You can now sign in with your new password."}
            </p>
            <button onClick={() => nav("/login")} className={priBtn} style={priShadow}>Go to Sign In</button>
          </div>
        ) : !token ? (
          <>
            <p className="mb-6 text-sm" style={{ color: "#566071" }}>Set a new password</p>
            <div className="text-sm" style={{ color: "#D32F45" }}>
              This reset link is missing its token. Please use the link from your email, or request a new one.
            </div>
            <div className="mt-5 text-center text-[12.5px]">
              <a href="#" onClick={(e) => { e.preventDefault(); nav("/login"); }} style={{ color: "#077282" }}>
                ← Back to Sign In
              </a>
            </div>
          </>
        ) : checkFailed ? (
          <div className="text-center">
            <p className="mb-5 text-sm" style={{ color: "#566071" }}>Set a new password</p>
            <div className="mb-[18px] text-sm" style={{ color: "#D32F45" }}>
              We couldn't verify your link — please check your connection and try again.
            </div>
            <button onClick={() => window.location.reload()} className={priBtn} style={priShadow}>Try Again</button>
          </div>
        ) : linkValid === null ? (
          <p className="mb-6 text-sm" style={{ color: "#566071" }}>Set a new password</p>
        ) : !linkValid ? (
          <div className="text-center">
            <div className="mx-auto mt-1.5 mb-3.5 grid h-[52px] w-[52px] place-items-center rounded-full"
              style={{ background: "#FBE7EA", color: "#D32F45" }}>
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
                <path d="M12 9v4" /><path d="M12 17h.01" />
              </svg>
            </div>
            <h3 className="m-0 mb-1.5 text-[18px] font-semibold"
              style={{ fontFamily: "'Montserrat', sans-serif", color: "#0E1320" }}>
              Link Invalid or Expired
            </h3>
            <p className="mb-[18px] text-sm" style={{ color: "#566071" }}>
              This link is invalid, has expired, or was replaced by a newer one. Open the most
              recent email, or request a new link from the sign-in page (Forgot password) — or ask
              your admin to resend the invite.
            </p>
            <button onClick={() => nav("/login")} className={priBtn} style={priShadow}>Back to Sign In</button>
          </div>
        ) : (
          <>
            <p className="mb-6 text-sm" style={{ color: "#566071" }}>
              {isInvite
                ? "Complete your onboarding — set a password to activate your account."
                : "Set a new password for your account."}
            </p>
            <form onSubmit={submit}>
              <div className="mb-4">
                <label className="mb-1.5 block text-xs font-semibold" style={{ color: "#566071" }}>New password</label>
                <PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} required autoFocus
                  placeholder="Create a strong password" className={inputCls} style={inputStyle} />
              </div>

              {/* live requirements checklist */}
              <ul className="mb-4 space-y-1">
                {RULES.map((r, i) => {
                  const ok = met[i];
                  return (
                    <li key={r.label} className="flex items-center gap-2 text-[12px]"
                      style={{ color: ok ? "#0A6E4C" : "#8B93A2" }}>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6">
                        {ok ? <path d="M5 12l5 5L20 7" /> : <circle cx="12" cy="12" r="9" strokeWidth="1.8" />}
                      </svg>
                      {r.label}
                    </li>
                  );
                })}
              </ul>

              <div className="mb-2">
                <label className="mb-1.5 block text-xs font-semibold" style={{ color: "#566071" }}>Confirm password</label>
                <PasswordInput value={confirm} onChange={(e) => setConfirm(e.target.value)} required
                  placeholder="Re-enter password" className={inputCls} style={inputStyle} />
                {confirm.length > 0 && !matches && (
                  <div className="mt-1.5 text-[12px]" style={{ color: "#D32F45" }}>Passwords don't match.</div>
                )}
              </div>

              {err && <div className="mb-2 text-sm" style={{ color: "#D32F45" }}>{err}</div>}
              <button type="submit" disabled={busy || !allMet || !matches} className={priBtn} style={priShadow}>
                {isInvite ? "Complete Onboarding" : "Update Password"}
              </button>
            </form>
            <div className="mt-5 text-center text-[12.5px]">
              <a href="#" onClick={(e) => { e.preventDefault(); nav("/login"); }} style={{ color: "#077282" }}>
                ← Back to Sign In
              </a>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
